# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""List Principal Grants handler.

GET /principals/{principalId}/grants

When principalId is the SELF_PRINCIPAL sentinel, resolves the caller's email
and groups from the authorizer context and returns the grants held by the user
and their groups.

Paginated with ``maxResults`` (default and maximum :data:`MAX_PAGE_SIZE`, so a
caller that ignores ``nextToken`` still sees every grant unless it holds more
than that) and an opaque ``nextToken``. The token records which of the caller's
principal keys (self, then each group, in claim order) the next page resumes
in, and where within it; it is only valid for a caller holding that principal
key.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import structlog
from coa_common import sanitize_principal_key
from coa_common.constants import PAGINATION_TOKEN_MAX_LENGTH
from coa_common.dao import DynamoDBDAO, PaginatedResult, QueryParams
from coa_common.logging import setup_logging
from coa_common.response import api_response

from .grant_id import encode_grant_id

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SELF_PRINCIPAL = "me"
"""Sentinel value for principalId that resolves to the authenticated caller."""

_CLAIM_EMAIL = "email"
_CLAIM_GROUPS = "groups"
_CLAIM_COGNITO_GROUPS = "cognito:groups"

_MAX_GROUPS = 250
"""Hard circuit-breaker on group fan-out, not an expected ceiling.

Enterprise SSO users routinely carry 100+ group
memberships (a real observed token had ~140) — a low cap here silently
truncates the group list wherever a caller's granted group happens to fall
past the cutoff, causing a principal's own grant to vanish from THIS listing
(while `coa_control_plane.authorization.handler`'s uncapped `_resolve_roles`
still enforces it correctly) with no error, no indication of an incomplete
result, and no correlation to anything the caller did. That silent split
between "what you're allowed to do" and "what you're shown you're allowed to
do" is what the old `_MAX_GROUPS = 10` produced.

250 is sized to comfortably exceed real-world group counts while still
bounding the fan-out below (see `_MAX_GROUP_QUERY_WORKERS`). If a caller ever
exceeds it, log loudly (``logger.error``, not ``warning``) rather than
truncating silently — see the log call below.
"""

_MAX_GROUP_QUERY_WORKERS = 20
"""Bound on concurrent DynamoDB queries for the group fan-out.

Each principal key (self + each group) is one PrincipalIndex GSI query;
executed sequentially, that scales request latency linearly with the
caller's group count (100+ groups would add seconds to a single page load).
Parallelizing over a small thread pool keeps latency roughly constant
(bounded by the slowest single query, not their sum) while still bounding
concurrent read load against the table — the DAO's boto3 client is safe for
concurrent use across threads.
"""

_GROUP_NAME_RE = re.compile(r"^[\w.@\-]+$")
"""Valid group name pattern — alphanumeric, dots, @, hyphens."""

MAX_PAGE_SIZE = 1000
"""Page-size cap and default; equals ``ListPrincipalGrants$maxResults`` @range max."""

_INDEX_KEY_ATTRS = ("PK", "SK", "principalKey", "resourceRoleKey")
"""Key attributes of a PrincipalIndex item: a valid ``ExclusiveStartKey`` for the index."""

_INVALID_TOKEN = "Invalid nextToken"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """List the grants held by the calling principal and their groups, one page at a time.

    Only self-lookup is permitted: the principal id must be the ``me``
    sentinel. The caller's email and group memberships are resolved from the
    authorizer context; ``maxResults`` / ``nextToken`` page through the
    matching grants.

    Args:
        event: API Gateway proxy event with a ``principalId`` path parameter
            and authorizer-populated caller context.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 with the caller's grants, or a
        4xx/5xx error response.
    """
    path_params = event.get("pathParameters") or {}
    principal_id = path_params.get("principalId", "")
    query_params = event.get("queryStringParameters") or {}

    if not principal_id:
        return api_response(400, {"message": "principalId is required"})

    # Only allow self-lookup — other users' grants require admin endpoints
    if principal_id != SELF_PRINCIPAL:
        return api_response(403, {"message": "Access denied. Use 'me' to query your own grants."})

    region = os.environ.get("AWS_REGION")
    mappings_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE")

    if not region or not mappings_table:
        logger.error("missing_env_vars")
        return api_response(500, {"message": "Internal server error"})

    auth_context = event.get("requestContext", {}).get("authorizer", {})
    claims = auth_context.get("claims", {})

    email = auth_context.get(_CLAIM_EMAIL) or claims.get(_CLAIM_EMAIL, "")
    groups_str = auth_context.get(_CLAIM_GROUPS) or claims.get(_CLAIM_COGNITO_GROUPS, "")

    if not email:
        return api_response(400, {"message": "Unable to resolve caller identity"})

    # Normalize email to lowercase for consistent lookups
    email = email.strip().lower()

    principal_keys: list[str] = [f"User::{sanitize_principal_key(email)}"]

    if groups_str:
        groups_added = 0
        for group in groups_str.split(","):
            group = group.strip()
            if not group:
                continue
            if not _GROUP_NAME_RE.match(group):
                logger.warning("invalid_group_name_skipped", group=group)
                continue
            if groups_added >= _MAX_GROUPS:
                # Loud, not silent: this caller's own grant listing WILL be
                # incomplete past this point (unlike the old cap, which hit
                # real users' normal group counts and dropped their grants
                # with no signal at all). error, not warning, so this is
                # visible without needing debug-level log access.
                logger.error(
                    "group_fanout_cap_hit_grants_may_be_incomplete",
                    total_groups=groups_str.count(",") + 1,
                    max=_MAX_GROUPS,
                    email=email,
                )
                break
            principal_keys.append(f"Group::{sanitize_principal_key(group)}")
            groups_added += 1

    # De-duplicate (repeated group claims) while keeping claim order, which the
    # pagination cursor depends on.
    principal_keys = list(dict.fromkeys(principal_keys))

    try:
        max_results = _parse_max_results(query_params.get("maxResults"))
        cursor = _decode_cursor(query_params.get("nextToken"), principal_keys)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    try:
        mappings_dao = DynamoDBDAO(mappings_table, region=region)

        def _query_key(pk: str, limit: int, start_key: dict[str, Any] | None) -> PaginatedResult:
            return mappings_dao.query(
                QueryParams(
                    key_condition="#pk = :pk",
                    expression_values={":pk": pk},
                    expression_names={"#pk": "principalKey"},
                    index_name="PrincipalIndex",
                    limit=limit,
                    exclusive_start_key=start_key,
                )
            )

        grants, next_cursor = _collect_page(_query_key, principal_keys, cursor, max_results)
        response: dict[str, Any] = {"grants": grants}
        if next_cursor is not None:
            response["nextToken"] = _encode_cursor(next_cursor, principal_keys)
        return api_response(200, response)

    except Exception:
        logger.exception("list_principal_grants_error", principal_id=email)
        return api_response(500, {"message": "Internal server error"})


@dataclass(frozen=True)
class _Cursor:
    """Where a page starts: a principal key, and the index key to resume after (None = its start)."""

    key_index: int
    start_key: dict[str, Any] | None = None


_QueryFn = Callable[[str, int, "dict[str, Any] | None"], PaginatedResult]


def _parse_max_results(raw: str | None) -> int:
    if raw is None:
        return MAX_PAGE_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("maxResults must be an integer") from exc
    if not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f"maxResults must be between 1 and {MAX_PAGE_SIZE}")
    return value


def _encode_cursor(cursor: _Cursor, principal_keys: list[str]) -> str:
    payload = {"p": principal_keys[cursor.key_index], "k": cursor.start_key}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(token: str | None, principal_keys: list[str]) -> _Cursor:
    """Decode ``nextToken``; it must name one of the caller's own principal keys."""
    if not token:
        return _Cursor(0)
    if len(token) > PAGINATION_TOKEN_MAX_LENGTH:
        raise ValueError(_INVALID_TOKEN)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(_INVALID_TOKEN) from exc
    if not isinstance(payload, dict) or payload.get("p") not in principal_keys:
        raise ValueError(_INVALID_TOKEN)
    principal_key = payload["p"]
    start_key = payload.get("k")
    if start_key is not None and (
        not isinstance(start_key, dict)
        or set(start_key) != set(_INDEX_KEY_ATTRS)
        or not all(isinstance(v, str) for v in start_key.values())
        or start_key["principalKey"] != principal_key
    ):
        raise ValueError(_INVALID_TOKEN)
    return _Cursor(principal_keys.index(principal_key), start_key)


def _index_key(item: dict[str, Any]) -> dict[str, Any]:
    return {attr: item[attr] for attr in _INDEX_KEY_ATTRS}


def _collect_page(
    query: _QueryFn, principal_keys: list[str], cursor: _Cursor, limit: int
) -> tuple[list[dict[str, Any]], _Cursor | None]:
    """Fill one page of grants, walking the principal keys in order from ``cursor``.

    Keys are queried a window at a time in parallel, each asking for at most
    the number of grants the page still needs, so the latency of a caller with
    many groups stays near that of one query per window. Returns the page and
    the cursor of the next one (None when every key is exhausted).
    """
    grants: list[dict[str, Any]] = []
    seen_grant_ids: set[str] = set()
    idx, start_key = cursor.key_index, cursor.start_key
    workers = max(1, min(_MAX_GROUP_QUERY_WORKERS, len(principal_keys) - idx))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while idx < len(principal_keys):
            window = range(idx, min(idx + _MAX_GROUP_QUERY_WORKERS, len(principal_keys)))
            need = limit - len(grants)
            starts = [start_key if i == idx else None for i in window]
            pages = list(pool.map(query, [principal_keys[i] for i in window], [need] * len(window), starts))
            resume: _Cursor | None = None
            for i, page in zip(window, pages, strict=True):
                for pos, item in enumerate(page.items):
                    summary = _to_grant_summary(item)
                    if summary["grantId"] not in seen_grant_ids:
                        seen_grant_ids.add(summary["grantId"])
                        grants.append(summary)
                    if len(grants) >= limit:
                        if pos < len(page.items) - 1 or page.last_evaluated_key:
                            return grants, _Cursor(i, _index_key(item))
                        return grants, (_Cursor(i + 1) if i + 1 < len(principal_keys) else None)
                if page.last_evaluated_key:
                    # This key has more grants and the page is not full yet:
                    # resume on it in the next round.
                    resume = _Cursor(i, page.last_evaluated_key)
                    break
            if resume is not None:
                idx, start_key = resume.key_index, resume.start_key
            else:
                idx, start_key = window.stop, None
    return grants, None


def _to_grant_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "grantId": encode_grant_id(item["PK"], item["SK"]),
        "namespaceId": item.get("resourceId", ""),
        "principalType": item.get("principalType", ""),
        "principalId": item.get("principalId", ""),
        "role": item.get("role", ""),
        "grantedBy": item.get("grantedBy", ""),
        "grantedAt": item.get("grantedAt", ""),
    }
    # ``is not None``, not truthiness: a declared-empty override (e.g.
    # tableAllowlist: []) is a deny-all restriction and must stay visible to
    # the principal — omitting it makes a deny-all grant indistinguishable
    # from an unrestricted one.
    for opt in ("tableAllowlist", "columnDenylist", "allowedMetrics"):
        if item.get(opt) is not None:
            summary[opt] = item[opt]
    return summary
