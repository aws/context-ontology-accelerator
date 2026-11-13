# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``run_workflow``'s progress stream, heartbeat, run deadline and backstop."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import coa_workflow_agents.server as server
import pytest
from coa_workflow_agents.auth.caller import Caller, CallerTokenProvider
from coa_workflow_agents.clients.coa_api_client import CoaApiError
from coa_workflow_agents.run_context import (
    current_run,
    remaining_s,
    report_progress,
)
from coa_workflow_agents.tools.polling import PollTimeoutError, poll_to_terminal
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
# A caller whose token outlives every run here (year 2100).
CALLER = Caller(email="a@example.com", groups=(), id_token="t", expires_at=4102444800.0)


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


def test_mcp_server_is_stateful_so_progress_can_stream():
    # Progress notifications and the per-session microVM need the stateful transport.
    assert server.mcp.settings.stateless_http is False


@pytest.mark.asyncio
async def test_run_workflow_heartbeat_reports_progress_while_the_worker_runs():
    def slow(caller, namespace_id, instruction, confirmed_tools) -> str:
        time.sleep(0.2)
        return "done"

    ctx = _ctx()
    with (
        patch.object(server, "_run_supervisor_sync", side_effect=slow),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        patch.object(server, "HEARTBEAT_INTERVAL_S", 0.02),
    ):
        assert await server.run_workflow(ctx, "do something", NS) == "done"
    messages = [call.args[2] for call in ctx.report_progress.await_args_list]
    assert any(message.startswith("still working: ") for message in messages)
    # Progress values increase strictly.
    values = [call.args[0] for call in ctx.report_progress.await_args_list]
    assert values == sorted(set(values))


@pytest.mark.asyncio
async def test_run_workflow_heartbeat_stops_when_the_run_ends():
    ctx = _ctx()
    with (
        patch.object(server, "_run_supervisor_sync", return_value="done"),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        patch.object(server, "HEARTBEAT_INTERVAL_S", 0.01),
    ):
        await server.run_workflow(ctx, "do something", NS)
        calls = ctx.report_progress.await_count
        await asyncio.sleep(0.05)
    assert ctx.report_progress.await_count == calls


@pytest.mark.asyncio
async def test_run_workflow_sets_the_run_context_in_the_worker_thread():
    seen: dict[str, Any] = {}

    def worker(caller, namespace_id, instruction, confirmed_tools) -> str:
        seen["remaining"] = remaining_s()
        report_progress("from the worker")
        return "done"

    ctx = _ctx()
    with (
        patch.object(server, "_run_supervisor_sync", side_effect=worker),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        patch.object(server.config, "run_deadline_s", 500.0),
    ):
        await server.run_workflow(ctx, "do something", NS)
        await asyncio.sleep(0.05)
    assert 490.0 < seen["remaining"] <= 500.0
    assert any(call.args[2] == "from the worker" for call in ctx.report_progress.await_args_list)
    # The event loop's own context is untouched.
    assert current_run() is None


@pytest.mark.asyncio
async def test_run_workflow_ends_the_run_before_the_callers_token_is_refused():
    # The token stops being forwarded 60 s before it expires, and polls stop
    # 60 s before the run deadline: with 120.3 s left on the token, a poll in
    # the run may run ~0.3 s although the run deadline is far off.
    caller = Caller(email="a@example.com", groups=(), id_token="t", expires_at=time.time() + 120.3)

    def worker(caller_arg, namespace_id, instruction, confirmed_tools) -> str:
        provider = CallerTokenProvider(caller_arg)
        try:
            poll_to_terminal(
                "induction job j-1",
                lambda: {"token": provider.get_id_token(), "status": "RUNNING"},
                operation="test_op",
                is_terminal=lambda s: False,
                is_failure=lambda s: False,
                timeout_s=5,
                interval_s=0.01,
            )
        except PollTimeoutError as e:
            return f"{type(e).__name__}: {e.description}"
        return "terminal"

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=worker),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        patch.object(server.config, "run_deadline_s", 3300.0),
    ):
        result = await server.run_workflow(_ctx(), "induce", NS)
    assert result == "RunDeadlineReachedError: induction job j-1"


@pytest.mark.asyncio
async def test_run_workflow_backstop_is_the_run_deadline_plus_grace():
    seen: dict[str, float] = {}
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(awaitable, timeout):
        seen["timeout"] = timeout
        return await real_wait_for(awaitable, timeout)

    with (
        patch.object(server, "_run_supervisor_sync", return_value="done"),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        patch.object(server.config, "run_deadline_s", 3300.0),
        patch.object(server.asyncio, "wait_for", spy_wait_for),
    ):
        await server.run_workflow(_ctx(), "do something", NS)
    assert server.BACKSTOP_GRACE_S == 120.0
    assert seen["timeout"] == 3300.0 + 120.0


@pytest.mark.asyncio
async def test_run_workflow_failed_logs_a_sanitized_error():
    secret_instruction = "onboard host db.internal with password hunter2"
    message = "x" * 300 + secret_instruction
    with (
        patch.object(server, "_run_supervisor_sync", side_effect=RuntimeError(message)),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        capture_logs() as logs,
        pytest.raises(ValueError),
    ):
        await server.run_workflow(_ctx(), secret_instruction, NS)
    failed = next(entry for entry in logs if entry["event"] == "run_workflow_failed")
    # The class only: no part of the message, which can quote caller data.
    assert failed["error"] == "RuntimeError"
    assert "x" * 10 not in json.dumps(logs, default=str)
    assert "hunter2" not in json.dumps(logs, default=str)


@pytest.mark.asyncio
async def test_run_workflow_failed_coa_api_error_logs_method_route_and_status_only():
    body_sentinel = "BODY-SENTINEL-9a2e"
    error = CoaApiError("GET", "/namespaces/n1/metrics/churn-rate", 404, f'{{"message":"{body_sentinel}"}}')
    with (
        patch.object(server, "_run_supervisor_sync", side_effect=error),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        capture_logs() as logs,
        pytest.raises(ValueError),
    ):
        await server.run_workflow(_ctx(), "list metrics", NS)
    failed = next(entry for entry in logs if entry["event"] == "run_workflow_failed")
    assert failed["error"] == "CoaApiError: GET /namespaces/{id}/metrics/{id} -> HTTP 404"
    logged = json.dumps(logs, default=str)
    assert body_sentinel not in logged
    assert "churn-rate" not in logged
