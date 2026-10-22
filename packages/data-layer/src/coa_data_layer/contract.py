# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime enforcement of the bounds declared in ``serve.smithy``.

Nothing validates a data-plane request against the Smithy model before it reaches
this Lambda: API Gateway has no request validator on these routes and the handler
parses ``body`` by hand. Each bound below is therefore the only place the
contract is actually enforced, so a value outside it is REJECTED with a 400
rather than clamped or forwarded — a clamped value is a silent rewrite of what
the caller asked for, and a forwarded one reaches serve unchecked.

Every constant mirrors one trait in ``models/src/main/smithy/serve.smithy``;
``tests/unit/test_contract_bounds.py`` parses the model and fails if they drift.
Change the model first, then the constant here.

Error messages name the field and the accepted range but never echo the rejected
value: it is unbounded, caller-controlled input, and reflecting it back would
serialize whatever arrived (a multi-KB string, a nested object) into the response.
"""

from __future__ import annotations

import math
import re
from typing import Any

# ── Query ────────────────────────────────────────────────────────────────────
# ``tierOverride``: @range(min: 1, max: 3)
QUERY_TIER_OVERRIDE_MIN = 1
QUERY_TIER_OVERRIDE_MAX = 3
# ``mode``: enum QueryMode. ``agentic`` is the deprecated pre-rename spelling of
# ``deep-reasoning`` and is still part of the contract.
QUERY_MODES: frozenset[str] = frozenset({"standard", "deep-reasoning", "agentic"})
# ``timeoutMs``: @range(min: 1, max: 29000) — the REST budget a caller may narrow.
QUERY_TIMEOUT_MS_MIN = 1
QUERY_TIMEOUT_MS_MAX = 29_000
# ``maxResults``: @range(min: 1, max: 10000)
QUERY_MAX_RESULTS_MIN = 1
QUERY_MAX_RESULTS_MAX = 10_000
# ``dimensions``: DimensionFilterList @length(max: 100) of DimensionFilter, whose
# ``name`` is @length(min: 1, max: 256), ``value`` @length(max: 4000) and
# ``operator`` the DimensionOperator enum.
DIMENSION_FILTER_LIST_MAX_ITEMS = 100
DIMENSION_NAME_MIN_LENGTH = 1
DIMENSION_NAME_MAX_LENGTH = 256
DIMENSION_VALUE_MAX_LENGTH = 4000
DIMENSION_OPERATORS: frozenset[str] = frozenset({"="})

# ── KBSearch ─────────────────────────────────────────────────────────────────
# ``topK``: @range(min: 1, max: 100). The default applies when the field is absent.
KB_SEARCH_TOP_K_MIN = 1
KB_SEARCH_TOP_K_MAX = 100
KB_SEARCH_TOP_K_DEFAULT = 10
# ``minScore``: Float @range(min: 0). No upper bound: the score scale depends on
# the index's similarity metric. ``None`` means the model declares no bound on
# that side, so only "a finite number" is enforced there.
KB_SEARCH_MIN_SCORE_MIN: float | None = 0.0
KB_SEARCH_MIN_SCORE_MAX: float | None = None
# ``sourceFilter`` / ``entityFilter``: FilterValueList @length(max: 50) of
# FilterValue @length(min: 1, max: 2048).
FILTER_VALUE_LIST_MAX_ITEMS = 50
FILTER_VALUE_MIN_LENGTH = 1
FILTER_VALUE_MAX_LENGTH = 2048

# ── GraphTraverse ────────────────────────────────────────────────────────────
# ``startUri``: TraversalStartUri @length(min: 1, max: 2048) + @pattern. The
# pattern is the model's, character for character, and is applied as a full match.
GRAPH_TRAVERSE_START_URI_MIN_LENGTH = 1
GRAPH_TRAVERSE_START_URI_MAX_LENGTH = 2048
GRAPH_TRAVERSE_START_URI_PATTERN = r'^(https?://[^\s<>"{}|\\^`;]+|urn:[a-zA-Z0-9][a-zA-Z0-9._:-]*)$'
# ``maxDepth``: @range(min: 1, max: 5). Defaults apply when the field is absent.
GRAPH_TRAVERSE_MAX_DEPTH_MIN = 1
GRAPH_TRAVERSE_MAX_DEPTH_MAX = 5
GRAPH_TRAVERSE_MAX_DEPTH_DEFAULT = 2
# ``maxResults``: @range(min: 1, max: 1000)
GRAPH_TRAVERSE_MAX_RESULTS_MIN = 1
GRAPH_TRAVERSE_MAX_RESULTS_MAX = 1000
GRAPH_TRAVERSE_MAX_RESULTS_DEFAULT = 100
# ``direction``: enum TraversalDirection
GRAPH_TRAVERSE_DIRECTIONS: frozenset[str] = frozenset({"outgoing", "incoming", "both"})
GRAPH_TRAVERSE_DIRECTION_DEFAULT = "both"
# ``relationshipFilter``: FilterValueList (bounds above).

# ── DescribeSchema ───────────────────────────────────────────────────────────
# ``maxResults`` (query string): @range(min: 1, max: 1000)
DESCRIBE_SCHEMA_MAX_RESULTS_MIN = 1
DESCRIBE_SCHEMA_MAX_RESULTS_MAX = 1000

# A query-string integer: optional sign is not accepted, digits only. Length-capped
# so ``int()`` never sees a pathological string.
_QUERY_INT_RE = re.compile(r"[0-9]{1,9}")
_START_URI_RE = re.compile(GRAPH_TRAVERSE_START_URI_PATTERN)


class ContractViolation(ValueError):
    """A request member is outside its Smithy-declared type or bound.

    The message is safe to return to the caller: it names only the field and the
    accepted values, never the rejected value.
    """


def require_int(value: Any, name: str, lo: int, hi: int) -> int:
    """Return ``value`` if it is a genuine integer in ``[lo, hi]``.

    ``bool`` is rejected even though it subclasses ``int`` (``True`` would pass as
    1), and so are floats and numeric strings — coercion would quietly change what
    the caller sent (``int(1.5) == 1``).
    """
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ContractViolation(f"Invalid {name}. Must be an integer from {lo} to {hi}.")
    return value


def require_bool(value: Any, name: str) -> bool:
    """Return ``value`` if it is a JSON boolean; reject ``0``/``1``/``"true"``."""
    if not isinstance(value, bool):
        raise ContractViolation(f"Invalid {name}. Must be a boolean.")
    return value


def require_enum(value: Any, name: str, allowed: frozenset[str]) -> str:
    """Return ``value`` if it is one of ``allowed``.

    The ``isinstance`` check comes first: an unhashable value (a list or object)
    would otherwise raise ``TypeError`` from the membership test and surface as a
    502 instead of a 400.
    """
    if not isinstance(value, str) or value not in allowed:
        raise ContractViolation(f"Invalid {name}. Must be one of: {', '.join(sorted(allowed))}.")
    return value


def require_number(value: Any, name: str, lo: float | None = None, hi: float | None = None) -> float:
    """Return ``value`` as a float if it is a finite JSON number within any bounds.

    Integers are accepted (JSON does not distinguish ``1`` from ``1.0``); ``bool``,
    strings, ``NaN`` and infinities are not.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractViolation(f"Invalid {name}. Must be a finite number.")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ContractViolation(f"Invalid {name}. Must be {_number_range(lo, hi)}.")
    return float(value)


def _number_range(lo: float | None, hi: float | None) -> str:
    """Describe a numeric range for an error message, omitting an unbounded side."""
    if lo is not None and hi is not None:
        return f"a number from {lo:g} to {hi:g}"
    if lo is not None:
        return f"a number of at least {lo:g}"
    return f"a number of at most {hi:g}"


def require_filter_values(value: Any, name: str) -> list[str]:
    """Return ``value`` if it is a ``FilterValueList``: a bounded list of bounded strings."""
    if not isinstance(value, list) or len(value) > FILTER_VALUE_LIST_MAX_ITEMS:
        raise ContractViolation(f"Invalid {name}. Must be a list of at most {FILTER_VALUE_LIST_MAX_ITEMS} strings.")
    for item in value:
        if not isinstance(item, str) or not FILTER_VALUE_MIN_LENGTH <= len(item) <= FILTER_VALUE_MAX_LENGTH:
            raise ContractViolation(
                f"Invalid {name}. Each value must be a string of "
                f"{FILTER_VALUE_MIN_LENGTH} to {FILTER_VALUE_MAX_LENGTH} characters."
            )
    return value


def require_query_int(raw: Any, name: str, lo: int, hi: int) -> int:
    """Parse an ``@httpQuery`` integer (always a string on the wire) and bound it."""
    if not isinstance(raw, str) or not _QUERY_INT_RE.fullmatch(raw):
        raise ContractViolation(f"Invalid {name}. Must be an integer from {lo} to {hi}.")
    return require_int(int(raw), name, lo, hi)


def require_query_bool(raw: Any, name: str) -> bool:
    """Parse an ``@httpQuery`` boolean: ``true`` or ``false`` (case-insensitive)."""
    if isinstance(raw, str) and raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    raise ContractViolation(f"Invalid {name}. Must be true or false.")


def require_start_uri(value: Any) -> str:
    """Return ``value`` if it is a ``TraversalStartUri``: a bounded http(s) or urn IRI."""
    if (
        not isinstance(value, str)
        or not GRAPH_TRAVERSE_START_URI_MIN_LENGTH <= len(value) <= GRAPH_TRAVERSE_START_URI_MAX_LENGTH
        or not _START_URI_RE.fullmatch(value)
    ):
        raise ContractViolation(
            f"Invalid startUri. Must be an http(s) or urn IRI of {GRAPH_TRAVERSE_START_URI_MIN_LENGTH} to "
            f"{GRAPH_TRAVERSE_START_URI_MAX_LENGTH} characters, with no whitespace or reserved characters."
        )
    return value


def _is_dimension_value(value: Any) -> bool:
    """Whether ``value`` is a dimension value the Context Manager can bind.

    ``bool`` is checked before ``int`` because it is an ``int`` subclass. Only a
    ``float`` can be NaN or infinite; an ``int`` is always finite (and ``isfinite``
    would raise ``OverflowError`` on one too large for a float).
    """
    if isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, str) and len(value) <= DIMENSION_VALUE_MAX_LENGTH


def require_dimensions(value: Any) -> list[dict[str, Any]] | dict[str, Any]:
    """Return ``value`` if it is a ``DimensionFilterList`` within the model's bounds.

    Each entry needs a ``name`` (1-256 characters, not blank) and a scalar ``value``:
    a string of at most 4000 characters, a finite number, or a boolean. Numbers and
    booleans are accepted for parity with the Context Manager, which binds them as
    typed SQL literals; ``null``, objects, arrays, NaN and infinities are rejected.
    ``operator`` is optional: absent, blank, or ``=`` (after trimming) all mean
    equality, the only operator the SQL templates can express; anything else is
    rejected. Names must be unique case-insensitively: the list is folded into a
    ``{name: value}`` mapping downstream, which would otherwise keep one of two
    filters on the same dimension and silently drop the other.

    A ``{name: value}`` object — the pre-normalized form this handler accepted
    before the list shape was enforced — is still accepted, under the same bounds.
    """
    entries: list[tuple[Any, Any]]
    if isinstance(value, dict):
        entries = list(value.items())
    elif isinstance(value, list):
        entries = []
        for item in value:
            if not isinstance(item, dict):
                raise ContractViolation("Invalid dimensions. Each filter must be an object with name and value.")
            operator = item.get("operator")
            # Blank counts as absent (a generated client may serialize an unset
            # optional string as ""), matching the Context Manager's normalization.
            if operator is not None and (
                not isinstance(operator, str) or (operator.strip() and operator.strip() not in DIMENSION_OPERATORS)
            ):
                raise ContractViolation(
                    f"Invalid dimensions. operator must be one of: {', '.join(sorted(DIMENSION_OPERATORS))}."
                )
            entries.append((item.get("name"), item.get("value")))
    else:
        raise ContractViolation("Invalid dimensions. Must be a list of dimension filters.")
    if len(entries) > DIMENSION_FILTER_LIST_MAX_ITEMS:
        raise ContractViolation(f"Invalid dimensions. Must contain at most {DIMENSION_FILTER_LIST_MAX_ITEMS} filters.")
    seen: set[str] = set()
    for name, dim_value in entries:
        if (
            not isinstance(name, str)
            or not DIMENSION_NAME_MIN_LENGTH <= len(name) <= DIMENSION_NAME_MAX_LENGTH
            or not name.strip()
        ):
            raise ContractViolation(
                f"Invalid dimensions. Each name must be a non-blank string of {DIMENSION_NAME_MIN_LENGTH} to "
                f"{DIMENSION_NAME_MAX_LENGTH} characters."
            )
        if not _is_dimension_value(dim_value):
            raise ContractViolation(
                f"Invalid dimensions. Each value must be a string of at most {DIMENSION_VALUE_MAX_LENGTH} "
                "characters, a finite number, or a boolean."
            )
        key = name.strip().lower()
        if key in seen:
            raise ContractViolation("Invalid dimensions. Each dimension name may appear only once.")
        seen.add(key)
    return value
