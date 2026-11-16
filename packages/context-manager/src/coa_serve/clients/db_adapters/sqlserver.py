# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQL Server adapter (python-tds / pytds, pure-Python, run in a threadpool).

There is no maintained SQLAlchemy async driver for SQL Server that avoids a
native ODBC dependency, so this adapter uses ``python-tds`` (the same pure-Python
driver the discovery side uses) and runs its blocking calls in
``asyncio.to_thread``.

Connection model — CONNECT-PER-QUERY: pytds connections are synchronous and NOT
concurrency-safe. A shared cached connection could be closed by LRU eviction
while another coroutine is mid-query, and an ``asyncio.wait_for`` timeout cannot
cancel the OS thread, so a reused connection could interleave on a non-thread-safe
socket (TDS protocol corruption). Each ``run()`` therefore opens, executes and
closes a fresh connection in one ``asyncio.to_thread`` unit; the cached
``ConnHandle`` is just the resolved ``DBCredentials``.

Row-limit idiom is ``SELECT TOP N``; ``_limit_fallback`` is overridden so a parse
failure never emits a literal ``LIMIT``. SQL reaching here is fully qualified
(``schema.table``); there is no ``search_path``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import structlog

from .base import ConnHandle, DBCredentials, EngineAdapter

logger = structlog.get_logger(__name__)

# Bounded connect/login timeout (seconds) so an unreachable SQL Server host can't
# hang the connect indefinitely and leak a threadpool worker.
_LOGIN_TIMEOUT_S = 15


class SqlServerAdapter(EngineAdapter):
    """Direct-JDBC adapter for Microsoft SQL Server over python-tds (connect-per-query)."""

    engine_type: ClassVar[str] = "SQLSERVER"
    sqlglot_dialect: ClassVar[str] = "tsql"
    default_port: ClassVar[int] = 1433

    async def open_connection(self, creds: DBCredentials) -> ConnHandle:
        """Return the resolved credentials as the cached handle — NO live socket."""
        return creds

    async def run(
        self,
        conn: ConnHandle,
        sql: str,
        params: dict[str, Any] | None,
        timeout_seconds: int,
        schema: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Open a fresh pytds connection, run the SELECT, and close it — in one thread.

        The timeout is enforced driver-side and with an outer ``asyncio.wait_for``.
        Bind ``params`` are not supported (pytds uses ``%s`` paramstyle); the serve
        layer passes fully-materialized SQL, so non-empty ``params`` is rejected.
        """
        if params:
            raise ValueError(
                "SqlServerAdapter does not support bind params (pytds paramstyle differs); pass literal SQL"
            )
        creds: DBCredentials = conn

        def _connect_execute() -> tuple[list[dict[str, Any]], list[str]]:
            import pytds  # noqa: PLC0415

            # TLS: pytds encrypts the LOGIN handshake but leaves the DATA stream
            # plaintext unless the server requires encryption or a ``cafile`` is
            # supplied. No ``cafile`` is passed: pytds only offers full-stream TLS with
            # CA/hostname verification, and managed SQL Server certs commonly don't
            # chain to the container trust store (see base.build_ssl_context). The
            # boundary is credentials + VPC/SG isolation.
            # timeout = per-query server-side abort; login_timeout bounds connect.
            connection = pytds.connect(
                server=creds.host,
                port=creds.port,
                database=creds.database,
                user=creds.username,
                password=creds.password,
                autocommit=True,
                login_timeout=_LOGIN_TIMEOUT_S,
                timeout=int(timeout_seconds),
            )
            try:
                cur = connection.cursor()
                try:
                    cur.execute(sql)
                    columns = [d[0] for d in cur.description] if cur.description else []
                    rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()] if columns else []
                    return rows, columns
                finally:
                    cur.close()
            finally:
                connection.close()

        # Outer bound: connect + query + close together. Grace over the driver
        # timeout + login timeout so the driver's own aborts normally win the race.
        outer_timeout = float(timeout_seconds) + _LOGIN_TIMEOUT_S + 5
        return await asyncio.wait_for(asyncio.to_thread(_connect_execute), timeout=outer_timeout)

    async def close(self, conn: ConnHandle) -> None:
        """No-op — connections are opened and closed per-query inside ``run()``."""
        return None

    def _limit_fallback(self, sql: str, max_rows: int) -> str:
        """SQL Server row-cap fallback: ``SELECT TOP N`` (never a literal ``LIMIT``).

        Overrides the base hook so the shared AST logic in ``base.inject_limit``
        (which already renders the ROOT ``limit`` node as ``TOP`` for the ``tsql``
        dialect on the happy path) falls back to a TOP-based string rewrite rather
        than an engine-invalid ``LIMIT`` when sqlglot can't parse/render the SQL.
        """
        return self._top_fallback(sql, max_rows)

    @staticmethod
    def _top_fallback(sql: str, max_rows: int) -> str:
        """Inject ``TOP N`` into a leading SELECT when AST parsing fails.

        SQL Server requires ``TOP`` AFTER an optional ``DISTINCT`` /``ALL`` keyword
        (``SELECT DISTINCT TOP N ...``), not before it. This fallback only runs when
        sqlglot cannot parse the SQL — which the firewall (also sqlglot-based) would
        already have rejected upstream — so it is defensive; when the leading shape
        isn't a plain ``SELECT`` we can safely rewrite, we log and return it
        unchanged (the executor's ``max_rows`` slice still bounds what the caller
        sees) rather than emit invalid T-SQL.
        """
        stripped = sql.rstrip(";")
        lowered = stripped.lstrip()
        prefix_len = len(stripped) - len(lowered)
        upper = lowered.upper()
        if not upper.startswith("SELECT "):
            # Cannot safely inject TOP into this shape; see _top_fallback.
            logger.warning("sqlserver_top_fallback_uncapped", sql_len=len(sql), max_rows=max_rows)
            return stripped
        # Preserve a leading DISTINCT / ALL quantifier before TOP.
        after_select = lowered[7:]
        for quant in ("DISTINCT ", "ALL "):
            if after_select.upper().startswith(quant):
                q_len = len(quant)
                return f"{stripped[:prefix_len]}SELECT {after_select[:q_len]}TOP {int(max_rows)} {after_select[q_len:]}"
        return f"{stripped[:prefix_len]}SELECT TOP {int(max_rows)} {after_select}"
