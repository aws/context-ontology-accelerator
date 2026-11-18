# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""MySQL adapter (SQLAlchemy + aiomysql).

MySQL speaks its own wire protocol (not PostgreSQL's), so it uses the pure-Python
async ``aiomysql`` driver rather than asyncpg. Key differences from the Postgres
adapter:

- No ``search_path`` — in MySQL the database IS the schema, so unqualified names
  resolve against the connection's default database (set from ``databaseName`` in
  the URL). ``schema`` is therefore ignored.
- Statement timeout is enforced with ``SET SESSION max_execution_time`` (MySQL
  5.7.8+; milliseconds; applies to read-only SELECTs — exactly our workload).
- ``LIMIT`` is the native row-limit idiom (base ``inject_limit`` is correct).
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import structlog
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .base import SSL_CONTEXT, ConnHandle, DBCredentials, EngineAdapter

logger = structlog.get_logger(__name__)

_POOL_SIZE = 5
_POOL_TIMEOUT_S = 10
_POOL_RECYCLE_S = 300
# Bound the TCP+handshake so an unreachable host can't hang the connect forever.
_CONNECT_TIMEOUT_S = 10
# Grace added over the server-side timeout for the client-side wait_for deadline,
# so the server's own abort normally wins the race and the outer bound only catches
# a stalled socket read the server-side timeout can't cover.
_CLIENT_TIMEOUT_GRACE_S = 5


class MySQLAdapter(EngineAdapter):
    """Direct-JDBC adapter for MySQL over SQLAlchemy + aiomysql."""

    engine_type: ClassVar[str] = "MYSQL"
    sqlglot_dialect: ClassVar[str] = "mysql"
    default_port: ClassVar[int] = 3306

    async def open_connection(self, creds: DBCredentials) -> ConnHandle:
        """Open a SQLAlchemy async engine (aiomysql) with TLS."""
        url = URL.create(
            "mysql+aiomysql",
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
            # aiomysql accepts an ssl.SSLContext directly; SSL_CONTEXT is
            # intentionally permissive (see base.build_ssl_context).
            #
            # connect_timeout bounds the TCP+handshake (see _CONNECT_TIMEOUT_S).
            connect_args={"ssl": SSL_CONTEXT, "connect_timeout": _CONNECT_TIMEOUT_S},
        )

    async def run(
        self,
        conn: ConnHandle,
        sql: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
        schema: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Run SELECT with a SET SESSION max_execution_time timeout; no search_path.

        A client-side ``asyncio.wait_for`` deadline (server-side timeout + grace)
        wraps the transaction so a stalled socket read the server-side timeout
        can't cover — or a MariaDB where neither timeout var could be set — still
        frees the coroutine.
        """
        engine: AsyncEngine = conn
        timeout_ms = int(timeout_seconds) * 1000
        if not (1 <= timeout_ms <= 300_000):
            raise ValueError(f"timeout_ms out of range: {timeout_ms}")
        client_deadline = float(timeout_seconds) + _CLIENT_TIMEOUT_GRACE_S
        return await asyncio.wait_for(self._run_txn(engine, sql, params, timeout_ms), timeout=client_deadline)

    async def _run_txn(
        self,
        engine: AsyncEngine,
        sql: str,
        params: dict[str, Any] | None,
        timeout_ms: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        async with engine.begin() as db:
            # Per-statement server-side timeout (see _set_statement_timeout).
            if not await self._set_statement_timeout(db, timeout_ms):
                logger.warning("mysql_statement_timeout_unset", timeout_ms=timeout_ms)
            # No search_path: the URL's database is the active schema. Unqualified
            # names resolve there; fully-qualified `db.table` also works.
            result = await db.execute(text(sql), params or {})
            rows_raw = result.fetchall()
            columns = list(result.keys())
        return [dict(row._mapping) for row in rows_raw], columns

    @staticmethod
    async def _set_statement_timeout(db: Any, timeout_ms: int) -> bool:
        """Set a per-statement timeout, handling MySQL and MariaDB. Returns success.

        MySQL 5.7.8+: ``SET SESSION max_execution_time = <ms>``.
        MariaDB:      ``SET SESSION max_statement_time = <seconds>`` (float).
        """
        try:
            await db.execute(text(f"SET SESSION max_execution_time = {int(timeout_ms)}"))
            return True
        except SQLAlchemyError as exc:
            logger.debug("mysql_max_execution_time_unsupported_trying_maria", error=str(exc))
        try:
            # MariaDB expects seconds (float); convert from ms.
            await db.execute(text(f"SET SESSION max_statement_time = {timeout_ms / 1000.0}"))
            return True
        except SQLAlchemyError as exc:
            logger.warning("mysql_maria_statement_timeout_set_failed", error=str(exc))
            return False

    async def close(self, conn: ConnHandle) -> None:
        """Dispose the SQLAlchemy engine and its connection pool."""
        engine: AsyncEngine = conn
        await engine.dispose()
