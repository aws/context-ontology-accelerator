# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared execute-with-authz primitive (SqlExecutionService)."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.sql_execution import (
    DEFAULT_READ_DIALECT,
    SqlExecutionService,
    SqlExecutionStatus,
)
from coa_serve.tier2.sql_firewall import UnsafeSQLError


def _fw_result(denied=False, reason=None, sql="SELECT 1"):
    r = MagicMock()
    r.denied = denied
    r.reason = reason
    r.authorized_sql = sql
    return r


def _exec_result(rows=None, columns=None, row_count=1, truncated=False, engine="athena"):
    r = MagicMock()
    r.rows = rows if rows is not None else [{"n": 1}]
    r.columns = columns or ["n"]
    r.row_count = row_count
    r.truncated = truncated
    r.engine = engine
    return r


def _firewall(result=None, side_effect=None):
    fw = MagicMock()
    fw.evaluate = MagicMock(return_value=result or _fw_result(), side_effect=side_effect)
    return fw


@pytest.mark.unit
class TestSqlExecutionService:
    @pytest.mark.asyncio
    async def test_ok_path_authorizes_then_executes(self):
        fw = _firewall(_fw_result(sql="SELECT 1 /* authorized */"))
        executor = MagicMock()
        executor.execute = AsyncMock(return_value=_exec_result(row_count=2, truncated=True))

        svc = SqlExecutionService(fw, executor)
        out = await svc.execute("SELECT 1", namespace="ns1", profile={"userId": "u1"}, data_source_id="ds-1")

        assert out.status is SqlExecutionStatus.OK and out.ok
        assert out.row_count == 2 and out.truncated is True and out.engine == "athena"
        assert out.data_source_id == "ds-1"
        # The firewall gate runs FIRST, and the executor receives the AUTHORIZED
        # statement — never the caller's raw input.
        fw.evaluate.assert_called_once()
        assert fw.evaluate.call_args.kwargs["dialect"] == DEFAULT_READ_DIALECT
        assert executor.execute.await_args.args[0] == "SELECT 1 /* authorized */"

    @pytest.mark.asyncio
    async def test_unsafe_sql_is_a_status_not_an_exception(self):
        # Self-correcting callers must be able to observe the refusal and revise,
        # so an unsafe statement comes back as UNSAFE rather than raising.
        executor = MagicMock()
        executor.execute = AsyncMock()
        svc = SqlExecutionService(_firewall(side_effect=UnsafeSQLError("DROP not allowed")), executor)

        out = await svc.execute("DROP TABLE t", namespace="ns1")

        assert out.status is SqlExecutionStatus.UNSAFE
        assert "DROP not allowed" in out.reason
        executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_denied_never_executes(self):
        executor = MagicMock()
        executor.execute = AsyncMock()
        svc = SqlExecutionService(_firewall(_fw_result(denied=True, reason="no grant")), executor)

        out = await svc.execute("SELECT * FROM secrets", namespace="ns1")

        assert out.status is SqlExecutionStatus.DENIED and out.denied
        assert out.reason == "no grant"
        executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_engine_error_is_reported_with_type_and_message(self):
        # The error text is the only feedback a self-correcting caller gets, so it
        # must carry the exception type AND the engine message.
        executor = MagicMock()
        executor.execute = AsyncMock(side_effect=RuntimeError('column "amount" does not exist'))
        svc = SqlExecutionService(_firewall(), executor)

        out = await svc.execute("SELECT amount FROM t", namespace="ns1")

        assert out.status is SqlExecutionStatus.ERROR
        assert "RuntimeError" in out.reason and "does not exist" in out.reason
        assert out.authorized_sql == "SELECT 1"  # the statement that was attempted

    @pytest.mark.asyncio
    async def test_missing_executor_degrades_to_unavailable(self):
        svc = SqlExecutionService(_firewall(), None)

        out = await svc.execute("SELECT 1", namespace="ns1")

        assert out.status is SqlExecutionStatus.UNAVAILABLE
        assert not out.ok and not out.denied

    @pytest.mark.asyncio
    async def test_resolver_sees_authorized_sql_and_wins_over_default(self):
        # Routing precedence: a source resolved from the statement's own tables is
        # more precise than the caller's default, and the resolver is handed the
        # authorized SQL because that is what will actually run.
        fw = _firewall(_fw_result(sql="SELECT * FROM orders"))
        executor = MagicMock()
        executor.execute = AsyncMock(return_value=_exec_result())
        seen = {}

        def resolver(sql: str) -> str:
            seen["sql"] = sql
            return "ds-from-sql"

        svc = SqlExecutionService(fw, executor)
        out = await svc.execute(
            "select * from orders", namespace="ns1", data_source_id="ds-default", data_source_resolver=resolver
        )

        assert seen["sql"] == "SELECT * FROM orders"
        assert out.data_source_id == "ds-from-sql"
        assert executor.execute.await_args.kwargs["data_source_id"] == "ds-from-sql"

    @pytest.mark.asyncio
    async def test_resolver_miss_falls_back_to_default_source(self):
        executor = MagicMock()
        executor.execute = AsyncMock(return_value=_exec_result())
        svc = SqlExecutionService(_firewall(), executor)

        out = await svc.execute(
            "SELECT 1", namespace="ns1", data_source_id="ds-default", data_source_resolver=lambda _sql: ""
        )

        assert out.data_source_id == "ds-default"

    @pytest.mark.asyncio
    async def test_execution_knobs_reach_the_executor(self):
        fw = _firewall()
        executor = MagicMock()
        executor.execute = AsyncMock(return_value=_exec_result())
        svc = SqlExecutionService(fw, executor)

        await svc.execute("SELECT 1", namespace="ns1", max_rows=25, timeout_seconds=7)

        kwargs = executor.execute.await_args.kwargs
        assert kwargs["max_rows"] == 25 and kwargs["timeout_seconds"] == 7 and kwargs["namespace"] == "ns1"
        # Authorized as Trino, the dialect every executor reads the statement in.
        assert fw.evaluate.call_args.kwargs["dialect"] == DEFAULT_READ_DIALECT

    def test_caller_cannot_choose_the_authorization_dialect(self):
        # Another dialect could read comments differently from the Trino parse the
        # executors run, so the firewall would authorize a different statement.
        assert "dialect" not in inspect.signature(SqlExecutionService.execute).parameters
