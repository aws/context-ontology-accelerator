# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Normalize graph-only evidence stored on graph-context entities.

Graph traversal and document retrieval can attach source statements to an
entity's ``supportingStatements`` property. The public property value is a JSON
string because the Smithy ``PropertyMap`` stores string values. Use the helpers
in this module to decode, deduplicate, bound, and re-encode those statements
instead of manipulating the serialized value directly.

The bounds protect response and session size while
``fair_bound_statement_groups`` shares the global budget across active
entities. ``supporting_statements`` is accepted only as an internal legacy
session-restore alias; public responses use ``supportingStatements``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

SUPPORTING_STATEMENTS_KEY = "supportingStatements"
LEGACY_SUPPORTING_STATEMENTS_KEY = "supporting_statements"

MAX_SUPPORTING_STATEMENTS_PER_ENTITY = 100
MAX_SUPPORTING_STATEMENT_CHARS = 1_000
MAX_SUPPORTING_STATEMENT_BYTES = 2_000
MAX_SUPPORTING_STATEMENTS_CHARS = 4_000
MAX_SUPPORTING_STATEMENTS_BYTES = 6_000


def truncate_utf8(text: str, *, max_chars: int, max_bytes: int) -> str:
    """Truncate ``text`` deterministically without splitting a UTF-8 code point."""
    if max_chars <= 0 or max_bytes <= 0:
        return ""
    if len(text) <= max_chars and len(text.encode("utf-8")) <= max_bytes:
        return text

    marker = "…"
    marker_bytes = len(marker.encode("utf-8"))
    if max_chars > 1 and max_bytes > marker_bytes:
        prefix = text[: max_chars - 1]
        prefix = prefix.encode("utf-8")[: max_bytes - marker_bytes].decode("utf-8", errors="ignore")
        return f"{prefix}{marker}" if prefix else marker

    return text[:max_chars].encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def decode_supporting_statements(value: Any) -> list[str]:
    """Decode canonical JSON, restored arrays, or legacy plain statement text."""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = value

    if isinstance(parsed, str):
        candidates: Sequence[Any] = (parsed,)
    elif isinstance(parsed, list | tuple):
        candidates = parsed
    else:
        return []
    return [statement for statement in candidates if isinstance(statement, str) and statement.strip()]


def bound_supporting_statements(
    value: Any,
    *,
    max_count: int = MAX_SUPPORTING_STATEMENTS_PER_ENTITY,
    max_total_chars: int = MAX_SUPPORTING_STATEMENTS_CHARS,
    max_total_bytes: int = MAX_SUPPORTING_STATEMENTS_BYTES,
) -> list[str]:
    """Return an ordered, deduplicated statement list within count and size bounds."""
    bounded: list[str] = []
    seen: set[str] = set()
    remaining_chars = max_total_chars
    remaining_bytes = max_total_bytes

    for statement in decode_supporting_statements(value):
        if len(bounded) >= max_count or remaining_chars <= 0 or remaining_bytes <= 0:
            break
        clipped = truncate_utf8(
            statement,
            max_chars=min(MAX_SUPPORTING_STATEMENT_CHARS, remaining_chars),
            max_bytes=min(MAX_SUPPORTING_STATEMENT_BYTES, remaining_bytes),
        )
        if not clipped.strip() or clipped in seen:
            continue
        seen.add(clipped)
        bounded.append(clipped)
        remaining_chars -= len(clipped)
        remaining_bytes -= len(clipped.encode("utf-8"))

    return bounded


def fair_bound_statement_groups(groups: Sequence[Sequence[str]]) -> list[list[str]]:
    """Bound statements globally while reserving each active entity a fair share."""
    normalized = [bound_supporting_statements(group) for group in groups]
    bounded: list[list[str]] = [[] for _ in normalized]
    positions = [0 for _ in normalized]
    remaining_chars = MAX_SUPPORTING_STATEMENTS_CHARS
    remaining_bytes = MAX_SUPPORTING_STATEMENTS_BYTES

    while remaining_chars > 0 and remaining_bytes > 0:
        active = [index for index, group in enumerate(normalized) if positions[index] < len(group)]
        if not active:
            break

        added = False
        for active_position, index in enumerate(active):
            remaining_slots = len(active) - active_position
            char_share = remaining_chars // remaining_slots
            byte_share = remaining_bytes // remaining_slots
            if char_share <= 0 or byte_share <= 0:
                break

            statement = normalized[index][positions[index]]
            positions[index] += 1
            clipped = truncate_utf8(
                statement,
                max_chars=min(MAX_SUPPORTING_STATEMENT_CHARS, char_share),
                max_bytes=min(MAX_SUPPORTING_STATEMENT_BYTES, byte_share),
            )
            if not clipped.strip():
                continue
            bounded[index].append(clipped)
            remaining_chars -= len(clipped)
            remaining_bytes -= len(clipped.encode("utf-8"))
            added = True

        if not added:
            break

    return bounded


def encode_supporting_statements(statements: Sequence[str]) -> str:
    """Encode bounded statements in the canonical GraphContext property shape."""
    return json.dumps(list(statements), ensure_ascii=False)
