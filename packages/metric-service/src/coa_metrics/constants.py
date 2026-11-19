# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metric service constants — re-exports from shared lib for convenience."""

from __future__ import annotations

from coa_common.constants import SqlDialect

VALID_SQL_DIALECTS: frozenset[str] = frozenset(d.value for d in SqlDialect)

# SqlDialect has no ATHENA value: ATHENA names an execution engine, not a SQL
# dialect, so the hint points the caller at the dialect to use.
_DIALECT_HINTS: dict[str, str] = {
    "ATHENA": "for Athena-backed sources, use TRINO",
}

__all__ = ["VALID_SQL_DIALECTS", "SqlDialect", "dialect_hint", "invalid_dialect_message"]


def dialect_hint(dialect: str) -> str | None:
    """Return a hint for an execution-engine name mistakenly passed as a dialect, else None."""
    return _DIALECT_HINTS.get(dialect.upper())


def invalid_dialect_message(dialect: str) -> str:
    """Build the 400 message for an unrecognized SqlDialect value (the value is not echoed back)."""
    hint = dialect_hint(dialect)
    base = f"Invalid dialect. Must be one of: {sorted(VALID_SQL_DIALECTS)}"
    return f"{base} ({hint})" if hint else base
