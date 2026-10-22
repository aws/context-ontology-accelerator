# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metric service constants — re-exports from shared lib for convenience."""

from __future__ import annotations

from coa_common.constants import SqlDialect

# Set of valid dialect values for quick membership testing
VALID_SQL_DIALECTS: frozenset[str] = frozenset(d.value for d in SqlDialect)

# SqlDialect has no ATHENA value, and per team discussion it will
# NOT get one — adding it as an alias for TRINO would become wrong once Athena
# drops its Trino backend. Instead, invalid-dialect hints that name the actual
# execution engine (rather than the SQL syntax dialect) point the caller at
# the dialect to use TODAY. Extend this map if other engine-name mix-ups show up.
_DIALECT_HINTS: dict[str, str] = {
    "ATHENA": "for Athena-backed sources, use TRINO",
}

__all__ = ["VALID_SQL_DIALECTS", "SqlDialect", "dialect_hint", "invalid_dialect_message"]


def dialect_hint(dialect: str) -> str | None:
    """Return a hint for an execution-engine name mistakenly passed as a dialect.

    For example, ``for Athena-backed sources, use TRINO``. Returns None when
    there is no hint for the given value.
    """
    return _DIALECT_HINTS.get(dialect.upper())


def invalid_dialect_message(dialect: str) -> str:
    """Build the 400 message for an unrecognized SqlDialect value.

    Appends a targeted hint (e.g. "for Athena-backed sources, use TRINO") when
    the caller passed a known execution-engine name instead of a SQL dialect.
    The caller's value itself is not echoed back; the message lists the
    accepted values instead.
    """
    hint = dialect_hint(dialect)
    base = f"Invalid dialect. Must be one of: {sorted(VALID_SQL_DIALECTS)}"
    return f"{base} ({hint})" if hint else base
