# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared execute-with-authz primitive — the firewall + executor pair, composed once.

Every path that runs generated SQL needs the same two steps in the same order:
the :class:`~coa_serve.tier2.sql_firewall.SQLFirewall` gate (structural safety
plus grant/Cedar authorization), then :meth:`QueryExecutor.execute`. The NL→SQL
agent runs its generated queries through here rather than a raw ``run_sql`` tool.

Error contract: unlike the client protocols, which raise, expected failures —
unsafe SQL, a policy denial, an engine error — come back as a
:class:`SqlExecutionOutcome` with a non-OK :class:`SqlExecutionStatus`, because
the callers are self-correcting loops that feed the observation back into the
next generation. Callers decide which statuses are terminal.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from .clients.base import QueryExecutor
from .tier2.sql_firewall import SQLFirewall, UnsafeSQLError

logger = structlog.get_logger(__name__)

# Canonical generation dialect: the SQL writer is prompted for Trino/Presto and
# the firewall parses with sqlglot's ``trino`` reader unless a caller knows the
# statement is already transpiled for a specific engine.
DEFAULT_READ_DIALECT = "trino"


class SqlExecutionStatus(StrEnum):
    """Outcome class of one execute-with-authz attempt."""

    OK = "ok"  # Authorized and executed; rows/columns are populated
    UNSAFE = "unsafe"  # Failed structural safety validation (never executed)
    DENIED = "denied"  # Firewall/Cedar policy denial (never executed)
    ERROR = "error"  # Authorized, but the engine raised on execution
    UNAVAILABLE = "unavailable"  # No executor wired (e.g. Tier-3 reuse of a strategy)


@dataclass(frozen=True)
class SqlExecutionOutcome:
    """Result of one :meth:`SqlExecutionService.execute` call.

    Attributes:
        status: Which of the five outcome classes applies.
        authorized_sql: The SQL the firewall authorized (empty unless allowed).
        rows: Result rows on ``OK``, else empty.
        columns: Result column names on ``OK``, else empty.
        row_count: Number of rows returned on ``OK``.
        truncated: True when the executor capped the result at ``max_rows``.
        engine: Executing engine tag ("athena" | "redshift" | "jdbc"), when set.
        data_source_id: The source the statement was routed to.
        reason: Denial reason, unsafe-SQL message, or engine error text.
        duration_ms: Wall time of the authorize+execute pair.
    """

    status: SqlExecutionStatus
    authorized_sql: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    engine: str = ""
    data_source_id: str = ""
    reason: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """True when the statement was authorized and executed successfully."""
        return self.status is SqlExecutionStatus.OK

    @property
    def denied(self) -> bool:
        """True when the firewall/Cedar gate refused the statement."""
        return self.status is SqlExecutionStatus.DENIED


class SqlExecutionService:
    """Authorize a SQL statement, then execute it against the resolved source."""

    def __init__(self, firewall: SQLFirewall, query_executor: QueryExecutor | None):
        """Bind the primitive to the shared firewall and query executor.

        Args:
            firewall: SQLFirewall enforcing safety and authorization.
            query_executor: Executor that runs authorized SQL. ``None`` makes
                every call return ``UNAVAILABLE`` instead of raising, so a
                strategy reused without an executor degrades predictably.
        """
        self._firewall = firewall
        self._query_executor = query_executor

    async def execute(
        self,
        sql: str,
        *,
        namespace: str,
        profile: dict[str, Any] | None = None,
        data_source_id: str = "",
        data_source_resolver: Callable[[str], str] | None = None,
        max_rows: int = 1000,
        timeout_seconds: int = 35,
        dialect: str = DEFAULT_READ_DIALECT,
    ) -> SqlExecutionOutcome:
        """Run ``sql`` through the firewall and, if allowed, the query executor.

        Args:
            sql: The statement to authorize and execute.
            namespace: Namespace the query targets (also the Cedar resource).
            profile: Caller's authorization profile (grants).
            data_source_id: Source to route to when the resolver yields nothing.
            data_source_resolver: Optional callback mapping the AUTHORIZED SQL to
                a source id — the tables a statement actually references are a
                more precise routing signal than the caller's default. Called
                with the authorized SQL because that (not the input) is what
                will run. Its result takes precedence over ``data_source_id``.
            max_rows: Row cap handed to the executor.
            timeout_seconds: Per-statement execution timeout.
            dialect: sqlglot read-dialect for the firewall's safety parse.

        Returns:
            A :class:`SqlExecutionOutcome`; expected failures are statuses, not
            exceptions (see the module docstring).
        """
        start = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - start) * 1000)

        if not self._query_executor:
            return SqlExecutionOutcome(
                status=SqlExecutionStatus.UNAVAILABLE,
                reason="no query_executor configured",
                duration_ms=elapsed_ms(),
            )

        try:
            fw = self._firewall.evaluate(sql, profile or {}, namespace=namespace, dialect=dialect)
        except UnsafeSQLError as e:
            logger.warning("sql_execution_unsafe", namespace=namespace, security_event="unsafe_sql_attempt")
            return SqlExecutionOutcome(status=SqlExecutionStatus.UNSAFE, reason=str(e)[:200], duration_ms=elapsed_ms())
        if fw.denied:
            logger.warning("sql_execution_denied", namespace=namespace, reason=fw.reason)
            return SqlExecutionOutcome(
                status=SqlExecutionStatus.DENIED, reason=fw.reason or "denied", duration_ms=elapsed_ms()
            )

        resolved_source = (data_source_resolver(fw.authorized_sql) if data_source_resolver else "") or data_source_id
        try:
            result = await self._query_executor.execute(
                fw.authorized_sql,
                namespace=namespace,
                data_source_id=resolved_source,
                max_rows=max_rows,
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            return SqlExecutionOutcome(
                status=SqlExecutionStatus.ERROR,
                authorized_sql=fw.authorized_sql,
                data_source_id=resolved_source,
                reason=f"{type(e).__name__}: {str(e)[:200]}",
                duration_ms=elapsed_ms(),
            )

        return SqlExecutionOutcome(
            status=SqlExecutionStatus.OK,
            authorized_sql=fw.authorized_sql,
            rows=result.rows,
            columns=result.columns,
            row_count=result.row_count,
            truncated=getattr(result, "truncated", False),
            engine=getattr(result, "engine", "") or "",
            data_source_id=resolved_source,
            duration_ms=elapsed_ms(),
        )
