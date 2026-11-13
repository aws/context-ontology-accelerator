# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for run_workflow's thread offload + overall timeout.

Confirms the fix for a blocking, hours-long synchronous call chain
(Agent.__call__ -> requests/boto3) that, run directly inside an
``async def`` MCP handler, would stall the single event loop. Verified
here by running a blocking function through run_workflow, which runs it
with ``loop.run_in_executor(_THREAD_POOL, ...)`` under
``asyncio.wait_for``, and confirming the event loop stays responsive, that
a hang is bounded by the backstop, and which errors reach the caller.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import coa_workflow_agents.server as server
import pytest
from coa_workflow_agents.agents.supervisor import SupervisorAgents
from coa_workflow_agents.auth.caller import Caller
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
# A caller whose token outlives every run here (year 2100).
CALLER = Caller(email="a@example.com", groups=(), id_token="t", expires_at=4102444800.0)


@pytest.mark.asyncio
async def test_run_workflow_does_not_block_the_event_loop():
    """While a blocking _run_supervisor_sync call is in flight (on a worker
    thread), the event loop must still be free to run other coroutines —
    proving the offload actually offloads, not just wraps synchronously."""

    def blocking_supervisor_call(caller, namespace_id, instruction: str, confirmed_tools) -> str:
        time.sleep(0.3)
        return "done"

    heartbeat_ticks = []

    async def heartbeat():
        for _ in range(10):
            heartbeat_ticks.append(time.monotonic())
            await asyncio.sleep(0.03)

    with patch.object(server, "_run_supervisor_sync", side_effect=blocking_supervisor_call):
        ctx = MagicMock()
        ctx.request_context.request.headers = {"authorization": "Bearer fake"}
        with patch.object(server, "_verify_inbound_caller", return_value=CALLER):
            run_task = asyncio.create_task(server.run_workflow(ctx, "do something", NS))
            heartbeat_task = asyncio.create_task(heartbeat())
            result = await run_task
            await heartbeat_task

    assert result == "done"
    # The heartbeat must have ticked multiple times WHILE the 0.3s blocking
    # call was in flight — if the event loop were blocked, these ticks would
    # bunch up at the end instead of being evenly spaced throughout.
    assert len(heartbeat_ticks) >= 5


@pytest.mark.asyncio
async def test_run_workflow_raises_value_error_on_timeout():

    def hangs_forever(caller, namespace_id, instruction: str, confirmed_tools) -> str:
        time.sleep(5)
        return "should not get here"

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=hangs_forever),
        patch.object(server.config, "run_deadline_s", 0.05),
        patch.object(server, "BACKSTOP_GRACE_S", 0.0),
    ):
        ctx = MagicMock()
        ctx.request_context.request.headers = {"authorization": "Bearer fake"}
        with (
            patch.object(server, "_verify_inbound_caller", return_value=CALLER),
            capture_logs() as logs,
            pytest.raises(ValueError, match=rf"^Namespace: {NS}\. Workflow timed out after 0s\. "),
        ):
            await server.run_workflow(ctx, "do something", NS)
    assert [entry["event"] for entry in logs if entry["event"].startswith("run_workflow_")] == [
        "run_workflow_started",
        "run_workflow_timed_out",
        "run_workflow_completed",
    ]


@pytest.mark.asyncio
async def test_a_timeout_error_raised_inside_the_run_is_a_failure_not_the_backstop():

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=TimeoutError("socket read")),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        capture_logs() as logs,
        pytest.raises(ValueError, match=rf"^Namespace: {NS}\. Workflow failed \(TimeoutError\)\. "),
    ):
        await server.run_workflow(MagicMock(), "do something", NS)
    events = [entry["event"] for entry in logs]
    assert "run_workflow_failed" in events
    assert "run_workflow_timed_out" not in events


@pytest.mark.asyncio
async def test_run_workflow_backstop_firing_as_the_run_completes_is_a_timeout():
    """The backstop can fire in the loop iteration ``work`` completes, so ``work`` is done, not cancelled."""

    async def backstop_after_completion(fut, timeout):
        await fut
        raise TimeoutError from asyncio.CancelledError()

    with (
        patch.object(server, "_run_supervisor_sync", return_value="done"),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        patch.object(server.asyncio, "wait_for", backstop_after_completion),
        capture_logs() as logs,
        pytest.raises(ValueError, match=rf"^Namespace: {NS}\. Workflow timed out after "),
    ):
        await server.run_workflow(MagicMock(), "do something", NS)
    events = [entry["event"] for entry in logs]
    assert "run_workflow_timed_out" in events
    assert "run_workflow_failed" not in events


@pytest.mark.asyncio
async def test_run_workflow_returns_only_the_error_class_to_the_caller():
    """The caller gets the class and a hint, never the message: it can name AWS accounts, roles and ARNs."""

    leak = "User: arn:aws:sts::123456789012:assumed-role/runtime-role/x is not authorized"
    with patch.object(server, "_run_supervisor_sync", side_effect=RuntimeError(leak)):
        ctx = MagicMock()
        ctx.request_context.request.headers = {"authorization": "Bearer fake"}
        with (
            patch.object(server, "_verify_inbound_caller", return_value=CALLER),
            pytest.raises(ValueError, match=rf"^Namespace: {NS}\. Workflow failed \(RuntimeError\)\. ") as excinfo,
        ):
            await server.run_workflow(ctx, "do something", NS)
    assert "123456789012" not in str(excinfo.value)
    assert "read-only instruction" in str(excinfo.value)


@pytest.mark.asyncio
async def test_run_workflow_refuses_a_token_about_to_expire_before_the_run():

    expiring = Caller(email="a@example.com", groups=(), id_token="t", expires_at=time.time() + 30)
    with (
        patch.object(server, "_run_supervisor_sync") as run,
        patch.object(server, "_verify_inbound_caller", return_value=expiring),
        pytest.raises(ValueError, match="expires within a minute"),
    ):
        await server.run_workflow(MagicMock(), "do something", NS)
    run.assert_not_called()


@pytest.mark.asyncio
async def test_run_workflow_token_expiring_in_the_worker_keeps_its_reason():
    """A run queued until its token crosses the margin is refused in the worker; the caller still sees why."""

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=ValueError(server._TOKEN_EXPIRING_MESSAGE)),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        pytest.raises(ValueError, match=rf"^Namespace: {NS}\. Your COA token expires within a minute"),
    ):
        await server.run_workflow(MagicMock(), "do something", NS)


@pytest.mark.asyncio
async def test_run_workflow_other_value_error_from_the_worker_returns_only_the_class():

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=ValueError("arn:aws:iam::123456789012:role/x")),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
        pytest.raises(ValueError, match=rf"^Namespace: {NS}\. Workflow failed \(ValueError\)\. ") as excinfo,
    ):
        await server.run_workflow(MagicMock(), "do something", NS)
    assert "123456789012" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_run_workflow_denies_before_touching_the_supervisor_when_inbound_auth_fails():
    """Inbound verification must happen BEFORE any thread/supervisor work
    starts — an unauthenticated caller must never reach the (expensive)
    agent construction/Bedrock call path at all."""

    supervisor_called = {"n": 0}

    def count_calls(caller, namespace_id, instruction: str, confirmed_tools) -> str:
        supervisor_called["n"] += 1
        return "done"

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=count_calls),
        patch.object(server, "_verify_inbound_caller", side_effect=ValueError("Missing Authorization header")),
    ):
        ctx = MagicMock()
        ctx.request_context.request.headers = {}
        with pytest.raises(ValueError, match="Missing Authorization header"):
            await server.run_workflow(ctx, "do something", NS)

    assert supervisor_called["n"] == 0


@pytest.mark.asyncio
async def test_the_supervisor_is_built_fresh_for_each_run_workflow_call():
    """Each call builds its own supervisor with that call's ``confirm_mutations``; none is reused."""

    built_confirmed_sets: list[frozenset[str]] = []

    def fake_build_supervisor_agent(client, namespace_id, config, confirmed_tools, **kwargs):
        built_confirmed_sets.append(confirmed_tools)

        def fake_supervisor_call(instr: str) -> str:
            return f"confirmed={sorted(confirmed_tools)}"

        supervisor = MagicMock(side_effect=fake_supervisor_call)
        supervisor.event_loop_metrics.accumulated_usage = {}
        return SupervisorAgents(supervisor=supervisor, sub_agents=())

    with (
        patch.object(server, "_caller_client", return_value=MagicMock()),
        patch("coa_workflow_agents.server.build_supervisor_agent", side_effect=fake_build_supervisor_agent),
        patch.object(server, "_verify_inbound_caller", return_value=CALLER),
    ):
        ctx = MagicMock()
        ctx.request_context.request.headers = {"authorization": "Bearer fake"}

        result_unconfirmed = await server.run_workflow(ctx, "show me the sources", NS)
        result_confirmed = await server.run_workflow(ctx, "do everything", NS, confirm_mutations=["*"])

    assert built_confirmed_sets == [frozenset(), frozenset({"*"})]
    assert result_unconfirmed.endswith("\n\nconfirmed=[]")
    assert result_confirmed.endswith("\n\nconfirmed=['*']")
