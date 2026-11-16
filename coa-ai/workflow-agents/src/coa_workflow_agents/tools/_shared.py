# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for the tool modules: response-shape tolerance, paging, file sandboxing and URL redaction."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import structlog

from coa_workflow_agents.clients.coa_api_client import CoaApiError, sanitized_route

logger = structlog.get_logger(__name__)


def as_list(value: Any) -> list[Any]:
    """``value`` if it is a list, else ``[]`` (COA sends ``null`` for some empty lists)."""
    return value if isinstance(value, list) else []


def clamp(value: int, low: int, high: int) -> int:
    """``int(value)`` clamped into ``[low, high]``, for model-supplied page sizes."""
    return max(low, min(high, int(value)))


def envelope_items(resp: Any, *plural_keys: str) -> list[dict[str, Any]]:
    """The entities of a list response: a bare list, or the first list under ``items`` or ``plural_keys``.

    Non-object entries are dropped.
    """
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    if isinstance(resp, dict):
        for key in ("items", *plural_keys):
            value = resp.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def collect_pages(fetch: Callable[[str | None], Any], *plural_keys: str) -> list[dict[str, Any]]:
    """Every page's entities, following ``nextToken`` (``fetch(token)`` returns one page).

    An empty page with a ``nextToken`` is followed. A repeated token stops the loop.
    """
    items: list[dict[str, Any]] = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        page = fetch(next_token)
        items.extend(envelope_items(page, *plural_keys))
        token = page.get("nextToken") if isinstance(page, dict) else None
        next_token = token if isinstance(token, str) and token else None
        if token is not None and not isinstance(token, str):
            logger.warning("collect_pages.non_string_next_token_ignored", token_type=type(token).__name__)
        if next_token is None:
            return items
        if next_token in seen_tokens:
            logger.warning("collect_pages.repeated_next_token_stopped", pages=len(seen_tokens) + 1)
            return items
        seen_tokens.add(next_token)


def safe_relative_path(path: str, *, label: str) -> str:
    """Resolve a model-supplied path inside the working directory, or raise ``ValueError``.

    Absolute paths and ``..`` escapes (symlinks resolved) are refused; ``label`` names it in the error.
    """
    if os.path.isabs(path):
        raise ValueError(f"{label} must be relative to the working directory, not absolute: {path!r}")
    cwd = os.path.realpath(os.getcwd())
    resolved = os.path.realpath(os.path.join(cwd, path))
    if resolved != cwd and not resolved.startswith(cwd + os.sep):
        raise ValueError(f"{label} escapes the working directory: {path!r}")
    return resolved


# Largest local file a tool uploads to a presigned S3 URL.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def read_local_file(path: str, *, label: str, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[str, bytes]:
    """``(resolved path, bytes)`` of a sandboxed file; empty or over ``max_bytes`` raises ``ValueError``.

    The bounded read is the size check, so there is no stat to race.
    """
    safe_path = safe_relative_path(path, label=label)
    with open(safe_path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise ValueError(f"{label} must point to a file of 1 byte to {max_bytes} bytes")
    return safe_path, data


def first_present(obj: dict[str, Any], *keys: str) -> Any:
    """First present, non-None value among ``keys`` (snake/camel tolerance)."""
    for k in keys:
        if obj.get(k) is not None:
            return obj[k]
    return None


def first_str(obj: dict[str, Any], *keys: str) -> str | None:
    """``first_present``, as a string: for ids and statuses this package returns typed."""
    value = first_present(obj, *keys)
    return None if value is None else str(value)


def datasource_ids(obj: dict[str, Any]) -> list[str]:
    """Datasource ids on a job or proposal, top level or under ``metadata``, snake or camel case."""
    candidates: list[Any] = []
    for container in (obj, obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}):
        if not isinstance(container, dict):
            continue
        val = first_present(container, "datasource_ids", "datasourceIds", "datasourceId", "datasource_id")
        if isinstance(val, list):
            candidates.extend(val)
        elif val is not None:
            candidates.append(val)
    return [str(x) for x in candidates if x is not None]


PRESIGNED_URL_OMITTED = "<presigned URL omitted>"
# Query parameters of a SigV4 presigned S3 URL.
_PRESIGNED_MARKERS = ("X-Amz-Signature=", "X-Amz-Credential=", "X-Amz-Security-Token=")


def redact_presigned_urls(value: Any) -> Any:
    """Deep copy of ``value`` with every presigned S3 URL replaced: they are bearer credentials.

    Some responses embed them (``GetProposal``'s ``ontology_url``, for one), and tool results
    reach the model and the logs.
    """
    if isinstance(value, dict):
        return {k: redact_presigned_urls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_presigned_urls(v) for v in value]
    if isinstance(value, str) and any(m in value for m in _PRESIGNED_MARKERS):
        return PRESIGNED_URL_OMITTED
    return value


def tolerate_initial_not_found(
    fetch: Callable[[], dict[str, Any]], placeholder: dict[str, Any], grace_s: float
) -> Callable[[], dict[str, Any]]:
    """Wrap a poll's ``fetch`` so a 404 within ``grace_s`` of now reads as ``placeholder``.

    COA's job and source reads are eventually consistent, so right after a
    create they can still answer 404; past the grace a 404 raises as usual.
    """
    not_found_until = time.monotonic() + grace_s

    def tolerant() -> dict[str, Any]:
        try:
            return fetch()
        except CoaApiError as exc:
            if exc.status_code == 404 and time.monotonic() < not_found_until:
                # Visible, so a poll that later fails shows its first states were the placeholder.
                logger.info("poll_initial_not_found_tolerated", route=sanitized_route(exc.path))
                return placeholder
            raise

    return tolerant
