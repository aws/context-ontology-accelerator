# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL and Redshift adapters (SQLAlchemy + asyncpg).

Redshift speaks the PostgreSQL wire protocol, so it reuses the whole asyncpg
transport and differs only in (a) the driver token — a subclassed dialect that
skips the ``standard_conforming_strings`` connect-probe Redshift rejects — and
(b) the sqlglot dialect. Everything else (SSL, search_path, statement timeout) is
identical, so ``RedshiftAdapter`` subclasses ``PostgresAdapter``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, ClassVar

import structlog
from sqlalchemy import text
from sqlalchemy.dialects import registry as _sa_registry
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .base import SSL_CONTEXT, ConnHandle, DBCredentials, EngineAdapter

logger = structlog.get_logger(__name__)

# SQLAlchemy connection pool tuning (per engine).
_POOL_SIZE = 5
_POOL_TIMEOUT_S = 10
_POOL_RECYCLE_S = 300
# Bound the TCP+handshake so an unreachable host can't hang the connect forever
# (asyncpg's own default is 60s; keep ours tighter and explicit).
_CONNECT_TIMEOUT_S = 10
# Grace added over the server-side statement_timeout for the client-side wait_for
# deadline, so the server's own abort normally wins the race and the outer bound
# only catches a stalled socket read the server-side timeout can't cover.
_CLIENT_TIMEOUT_GRACE_S = 5

# Valid unquoted PostgreSQL/Redshift identifier: starts with a letter or
# underscore, then letters/digits/underscores, max 63 chars. Used to validate the
# schema name before it is interpolated into `SET search_path` (which cannot be
# parameterized). Rejects anything that could carry a SQL-injection payload.
_SCHEMA_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


# ---------------------------------------------------------------------------
# Redshift-aware asyncpg dialect.
#
# SQLAlchemy's PGDialect.initialize() runs ``show standard_conforming_strings``
# once per engine (via _set_backslash_escapes) to decide backslash-escaping.
# Amazon Redshift does NOT expose that GUC, so the probe raises
# ``UndefinedObjectError`` AT CONNECT TIME — before any query runs — and every
# Redshift query fails. Override the hook to skip the probe and hardcode the
# modern-Redshift default (standard_conforming_strings=on -> _backslash_escapes
# =False). PostgreSQL sources keep the stock dialect and its probe.
# ---------------------------------------------------------------------------
_REDSHIFT_ASYNCPG_DRIVER = "postgresql+asyncpg_redshift"


class PGDialect_asyncpg_redshift(PGDialect_asyncpg):  # noqa: N801 (SQLAlchemy dialect naming convention)
    """asyncpg dialect for Amazon Redshift: skip the standard_conforming_strings probe."""

    def _set_backslash_escapes(self, connection: Any) -> None:  # type: ignore[override]
        self._backslash_escapes = False


_sa_registry.register(
    "postgresql.asyncpg_redshift",
    "coa_serve.clients.db_adapters.postgres",
    "PGDialect_asyncpg_redshift",
)


class PostgresAdapter(EngineAdapter):
    """Direct-JDBC adapter for PostgreSQL over SQLAlchemy + asyncpg."""

    engine_type: ClassVar[str] = "POSTGRESQL"
    sqlglot_dialect: ClassVar[str] = "postgres"
    default_port: ClassVar[int] = 5432

    # Driver token for the SQLAlchemy URL — overridden by RedshiftAdapter.
    drivername: ClassVar[str] = "postgresql+asyncpg"

    async def open_connection(self, creds: DBCredentials) -> ConnHandle:
        """Open a SQLAlchemy async engine (asyncpg) with SSL + search_path."""
        url = URL.create(
            self.drivername,
            username=creds.username,
            password=creds.password,
            host=creds.host,
            port=creds.port,
            database=creds.database,
        )
        return create_async_engine(
            url,
            pool_size=_POOL_SIZE,
            # max_overflow=0: cap total connections at pool_size. The SQLAlchemy
            # default (10) would let each cached engine open up to 15 connections,
            # and with MAX_CONN_CACHE_SIZE engines that could exhaust the source
            # DB's max_connections under load.
            max_overflow=0,
            pool_timeout=_POOL_TIMEOUT_S,
            pool_recycle=_POOL_RECYCLE_S,
            # pool_pre_ping: a connection idle-killed server-side (before
            # pool_recycle elapses) is transparently replaced instead of erroring
            # the query with a stale-socket failure.
            pool_pre_ping=True,
            echo=False,
            connect_args={
                # TLS is REQUIRED by Redshift and expected by managed PostgreSQL
                # (RDS/Aurora); without it a require_ssl source refuses the
                # plaintext connection and the query hangs/errors. _SSL_CONTEXT is
                # permissive (no peer-identity pinning) — see base.build_ssl_context.
                "ssl": SSL_CONTEXT,
                # Bound the TCP+handshake (asyncpg `timeout`) so an unreachable host
                # can't hang the connect; pool_timeout only bounds waiting for a
                # free pooled slot, not the underlying connect.
                "timeout": _CONNECT_TIMEOUT_S,
                "server_settings": {"search_path": creds.schema},
            },
        )

    async def run(
        self,
        conn: ConnHandle,
        sql: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
        schema: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run SELECT with SET LOCAL statement_timeout + per-txn search_path.

        A client-side ``asyncio.wait_for`` deadline (server-side timeout + grace)
        wraps the whole transaction so a stalled socket read the server-side
        ``statement_timeout`` can't cover still frees the coroutine.
        """
        engine: AsyncEngine = conn
        timeout_ms = int(timeout_seconds) * 1000
        if not (1 <= timeout_ms <= 300_000):
            raise ValueError(f"timeout_ms out of range: {timeout_ms}")
        client_deadline = float(timeout_seconds) + _CLIENT_TIMEOUT_GRACE_S
        return await asyncio.wait_for(self._run_txn(engine, sql, params, timeout_ms, schema), timeout=client_deadline)

    async def _run_txn(
        self,
        engine: AsyncEngine,
        sql: str,
        params: dict[str, Any] | None,
        timeout_ms: int,
        schema: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        search_path: str | None = None
        if schema:
            schemas = [token.strip() for token in schema.split(",")]
            if any(not token or not _SCHEMA_IDENTIFIER_PATTERN.match(token) for token in schemas):
                logger.warning("search_path_schema_invalid_rejected")
                raise ValueError("search_path contains an invalid schema identifier")
            # Source resolution already chose the authoritative ordered search
            # path. Appending ``public`` here would change that contract and
            # duplicate it when it is already present.
            search_path = ", ".join(schemas)

        async with engine.begin() as db:
            # SET LOCAL cannot be parameterized; timeout_ms validated to 1..300000.
            await db.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
            # Redshift Serverless ignores search_path set via asyncpg server_settings
            # on connection startup, so set it per-transaction here. SET search_path
            # cannot be parameterized, so validate each identifier before interpolation
            # (schema comes from source config, but we never trust it blindly).
            if search_path:
                try:
                    await db.execute(text(f"SET search_path TO {search_path}"))
                except SQLAlchemyError as exc:
                    logger.error("search_path_set_failed", schema=schema, error=str(exc))
                    # PostgreSQL marks the transaction aborted after a failed
                    # SET. Continuing with the customer query in this same
                    # transaction can only fail (and masks the real cause), so
                    # let the context manager roll it back.
                    raise
            result = await db.execute(text(sql), params or {})
            rows_raw = result.fetchall()
            columns = list(result.keys())
        return [dict(row._mapping) for row in rows_raw], columns

    async def close(self, conn: ConnHandle) -> None:
        """Dispose the SQLAlchemy engine and its connection pool."""
        engine: AsyncEngine = conn
        await engine.dispose()


class RedshiftAdapter(PostgresAdapter):
    """Direct-JDBC adapter for Amazon Redshift (native tables over asyncpg).

    Identical to PostgreSQL except the probe-skipping driver token and the
    sqlglot dialect (Redshift diverges from postgres on DATE_ADD, TRY_CAST, and
    type-name normalization — a postgres-target transpile against Redshift raises
    at execution time, hence the distinct dialect).
    """

    engine_type: ClassVar[str] = "REDSHIFT"
    sqlglot_dialect: ClassVar[str] = "redshift"
    default_port: ClassVar[int] = 5439
    drivername: ClassVar[str] = _REDSHIFT_ASYNCPG_DRIVER
