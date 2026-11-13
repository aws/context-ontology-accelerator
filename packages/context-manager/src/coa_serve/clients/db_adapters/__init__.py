# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-engine direct-JDBC adapter registry.

Public surface:
- :class:`EngineAdapter`, :class:`DBCredentials`, ``ConnHandle`` — the abstraction.
- :func:`get_adapter` — resolve the adapter for an engine token, fail-safe to
  PostgreSQL (preserves the historical "default postgres" behavior for an unknown
  or missing engine so an unexpected value can never break an existing source).

Adding a new engine: implement an ``EngineAdapter`` subclass in a new module and
add one instance to ``_ADAPTERS`` below. No change to the executor.

Engines without a direct adapter here (Snowflake, Oracle) are served through
Athena federation; :func:`is_direct_query_engine` keeps them off the direct-JDBC
route so they can never be executed through the PostgreSQL fail-safe with the
wrong SQL dialect.
"""

from __future__ import annotations

from .base import ConnHandle, DBCredentials, EngineAdapter
from .mysql import MySQLAdapter
from .postgres import PostgresAdapter, RedshiftAdapter
from .sqlserver import SqlServerAdapter

_ADAPTER_INSTANCES: tuple[EngineAdapter, ...] = (
    PostgresAdapter(),
    RedshiftAdapter(),
    MySQLAdapter(),
    SqlServerAdapter(),
)

_ADAPTERS: dict[str, EngineAdapter] = {a.engine_type: a for a in _ADAPTER_INSTANCES}

# Fail-safe adapter for unknown/missing engine tokens (historical default).
_DEFAULT_ADAPTER: EngineAdapter = _ADAPTERS["POSTGRESQL"]


def get_adapter(engine_type: str | None) -> EngineAdapter:
    """Return the adapter for ``engine_type`` (case-insensitive); PG if unknown.

    Deliberately does NOT raise for an unknown engine: the PostgreSQL fail-safe is
    what keeps an unexpected token from breaking an existing source. Keeping an
    engine without a direct adapter off the direct-JDBC route is the caller's job —
    see :func:`is_direct_query_engine`, which the composite executor consults before
    choosing that route.
    """
    if not engine_type:
        return _DEFAULT_ADAPTER
    return _ADAPTERS.get(engine_type.upper(), _DEFAULT_ADAPTER)


def is_direct_query_engine(engine_type: str | None) -> bool:
    """True when ``engine_type`` may be executed over the direct-JDBC route.

    False for any engine with no adapter of its own (Snowflake, Oracle) — which
    would otherwise resolve to the PostgreSQL fail-safe in :func:`get_adapter` and
    be executed with the wrong SQL dialect. Callers route these through Athena
    federation instead, which speaks Trino natively.
    """
    if not engine_type:
        return False
    return engine_type.upper() in _ADAPTERS


__all__ = [
    "ConnHandle",
    "DBCredentials",
    "EngineAdapter",
    "MySQLAdapter",
    "PostgresAdapter",
    "RedshiftAdapter",
    "SqlServerAdapter",
    "get_adapter",
    "is_direct_query_engine",
]
