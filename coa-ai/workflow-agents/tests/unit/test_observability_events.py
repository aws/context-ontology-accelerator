# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The run's log events carry names, outcomes, durations and counts only, never caller data.

``run_workflow_completed`` and ``run_id`` (``server.run_workflow``), ``run_token_usage``
(``server._run_supervisor_sync``), ``tool_call_completed`` and ``sub_agent_call_completed``
(``observability.ToolCallObserver``) and ``coa_api_error`` (``CoaApiClient._request``).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import coa_workflow_agents.clients.coa_api_client as coa_api_client
import coa_workflow_agents.server as server
import pytest
import requests
import structlog
from coa_workflow_agents.agents.model import build_bedrock_model
from coa_workflow_agents.agents.sub_agents import _build_phase_agent
from coa_workflow_agents.agents.supervisor import (
    SUPERVISOR_NAME,
    SupervisorAgents,
    build_supervisor_agent,
)
from coa_workflow_agents.auth.caller import Caller
from coa_workflow_agents.clients.coa_api_client import CoaApiError
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.intervention import MutatingToolGate
from coa_workflow_agents.observability import ToolCallObserver
from coa_workflow_agents.run_context import report_progress
from coa_workflow_agents.tools.polling import RunDeadlineReachedError
from strands import Agent, tool
from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
# Caller data that must never reach a log event.
SENTINEL = "SENTINEL-caller-data-7f3a"
CONFIG = WorkflowAgentsConfig(bedrock_model_id="", bedrock_temperature=0.0, aws_region="us-east-1")


def _capture() -> Any:
    """capture_logs with the run's contextvars (run_id) merged, as the real logging setup does."""
    return capture_logs(processors=[structlog.contextvars.merge_contextvars])


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == name]


# --- run_workflow_completed and run_id ---------------------------------------


@pytest.mark.asyncio
async def test_run_workflow_success_emits_one_completed_event_with_success_outcome(caller):
    with (
        patch.object(server, "_run_supervisor_sync", return_value="done"),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        _capture() as logs,
    ):
        await server.run_workflow(MagicMock(), SENTINEL, NS)

    completed = _events(logs, "run_workflow_completed")
    assert len(completed) == 1
    entry = completed[0]
    assert set(entry) == {"event", "log_level", "outcome", "duration_s", "namespace_id", "run_id"}
    assert entry["outcome"] == "success"
    assert entry["namespace_id"] == NS
    assert isinstance(entry["duration_s"], float) and entry["duration_s"] >= 0
    assert SENTINEL not in json.dumps(logs)


@pytest.mark.asyncio
async def test_run_workflow_failure_emits_one_completed_event_with_failed_outcome(caller):
    with (
        patch.object(server, "_run_supervisor_sync", side_effect=RuntimeError("boom")),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        _capture() as logs,
        pytest.raises(ValueError, match="Workflow failed"),
    ):
        await server.run_workflow(MagicMock(), "x", NS)

    assert [entry["outcome"] for entry in _events(logs, "run_workflow_completed")] == ["failed"]
    assert len(_events(logs, "run_workflow_failed")) == 1


@pytest.mark.asyncio
async def test_run_workflow_backstop_timeout_emits_one_completed_event_with_timed_out_outcome(caller):
    def hangs(caller, namespace_id, instruction, confirmed_tools) -> str:
        time.sleep(1)
        return "late"

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=hangs),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        patch.object(server.config, "run_deadline_s", 0.05),
        patch.object(server, "BACKSTOP_GRACE_S", 0.0),
        _capture() as logs,
        pytest.raises(ValueError, match="timed out"),
    ):
        await server.run_workflow(MagicMock(), "x", NS)

    completed = _events(logs, "run_workflow_completed")
    assert [entry["outcome"] for entry in completed] == ["timed_out"]
    assert completed[0]["duration_s"] >= 0.05


@pytest.mark.asyncio
async def test_run_workflow_refused_expiring_token_emits_no_completed_event(make_caller):
    expiring = make_caller(expires_at=time.time() + 30)
    with (
        patch.object(server, "_verify_inbound_caller", return_value=expiring),
        _capture() as logs,
        pytest.raises(ValueError, match="expires within a minute"),
    ):
        await server.run_workflow(MagicMock(), "x", NS)
    assert _events(logs, "run_workflow_completed") == []
    assert _events(logs, "run_workflow_started") == []


@pytest.mark.asyncio
async def test_run_workflow_invalid_namespace_emits_no_completed_event(caller):
    with (
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        _capture() as logs,
        pytest.raises(ValueError, match="namespace_id"),
    ):
        await server.run_workflow(MagicMock(), "x", "not-a-uuid")
    assert _events(logs, "run_workflow_completed") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verify", "args", "reason", "error"),
    [
        (ValueError(f"Token audience mismatch {SENTINEL}"), ("x", NS), "auth", "ValueError"),
        (30.0, ("x", NS), "token_expiring", "ValueError"),
        (None, ("x", NS, [SENTINEL]), "confirm_mutations", "ValueError"),
        (None, ("x", SENTINEL), "namespace_id", "ValueError"),
    ],
    ids=["auth", "token_expiring", "confirm_mutations", "namespace_id"],
)
async def test_run_workflow_rejected_names_the_failed_check_without_caller_data(
    make_caller, verify, args, reason, error
):
    """``verify`` is the verification's error, the seconds the token has left, or ``None`` for a valid caller."""
    if isinstance(verify, Exception):
        kwargs: dict[str, Any] = {"side_effect": verify}
    else:
        built = make_caller() if verify is None else make_caller(expires_at=time.time() + verify)
        kwargs = {"return_value": built}
    with (
        patch.object(server, "_verify_inbound_caller", **kwargs),
        _capture() as logs,
        pytest.raises(ValueError),
    ):
        await server.run_workflow(MagicMock(), *args)
    (rejected,) = _events(logs, "run_workflow_rejected")
    assert {k: rejected[k] for k in ("reason", "error")} == {"reason": reason, "error": error}
    assert SENTINEL not in json.dumps(logs)


@tool
def _probe_tool(note: str) -> str:
    """Return its input, as a tool result carrying caller data would."""
    return f"echo {note}"


@pytest.mark.asyncio
async def test_run_workflow_run_id_reaches_worker_and_strands_thread_events(caller):
    def worker(caller, namespace_id, instruction, confirmed_tools) -> str:
        server.logger.info("worker_probe")
        # A direct tool call runs on a Strands thread of its own (strands._async.run_async).
        agent = Agent(tools=[_probe_tool], hooks=[ToolCallObserver("ProbeAgent")], callback_handler=None)
        agent.tool._probe_tool(note=SENTINEL, record_direct_tool_call=False)
        return "done"

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=worker),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        _capture() as logs,
    ):
        await server.run_workflow(MagicMock(), "x", NS)

    run_ids = {
        entry.get("run_id")
        for entry in logs
        if entry["event"] in ("run_workflow_started", "worker_probe", "tool_call_completed", "run_workflow_completed")
    }
    assert len(run_ids) == 1
    (run_id,) = run_ids
    assert isinstance(run_id, str) and len(run_id) == 32
    assert len(_events(logs, "tool_call_completed")) == 1
    assert SENTINEL not in json.dumps(logs)


@pytest.mark.asyncio
async def test_run_workflow_after_the_call_run_id_is_cleared(caller):
    with (
        patch.object(server, "_run_supervisor_sync", side_effect=RuntimeError("boom")),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        pytest.raises(ValueError),
    ):
        await server.run_workflow(MagicMock(), "x", NS)
    assert "run_id" not in structlog.contextvars.get_contextvars()


@pytest.mark.asyncio
async def test_run_workflow_cancelled_emits_cancelled_completed_event_and_clears_run_id(caller):
    entered = threading.Event()
    release = threading.Event()

    def hangs(caller, namespace_id, instruction, confirmed_tools) -> str:
        entered.set()
        release.wait(5)
        return "late"

    after: dict[str, Any] = {}

    async def runner() -> None:
        # Runs in the task's own context, so it sees whether the run reset run_id there.
        try:
            await server.run_workflow(MagicMock(), "x", NS)
        finally:
            after.update(structlog.contextvars.get_contextvars())

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=hangs),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        _capture() as logs,
    ):
        task = asyncio.create_task(runner())
        try:
            # The worker is running, so the run is past its setup and run_id is bound.
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    # A client disconnect is not a failed run: kept out of the success rate.
    assert [entry["outcome"] for entry in _events(logs, "run_workflow_completed")] == ["cancelled"]
    assert _events(logs, "run_workflow_failed") == []
    assert "run_id" not in after


@pytest.mark.asyncio
async def test_run_workflow_first_progress_notification_is_the_run_id_and_reply_unchanged(caller):
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()

    def reports(caller, namespace_id, instruction, confirmed_tools) -> str:
        # A sub-agent's progress, as fast as it can come: right as the worker starts.
        report_progress("calling Worker")
        return f"Namespace: {NS}\n\ndone"

    with (
        patch.object(server, "_run_supervisor_sync", side_effect=reports),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        _capture() as logs,
    ):
        reply = await server.run_workflow(ctx, "x", NS)
        for _ in range(3):
            await asyncio.sleep(0)

    assert reply == f"Namespace: {NS}\n\ndone"
    (started,) = _events(logs, "run_workflow_started")
    messages = [call.args[2] for call in ctx.report_progress.await_args_list]
    assert messages[:2] == [f"run {started['run_id']}", "calling Worker"]
    assert ctx.report_progress.await_args_list[0].args == (1.0, None, f"run {started['run_id']}")


@pytest.mark.asyncio
async def test_run_workflow_two_runs_get_different_run_ids(caller):
    with (
        patch.object(server, "_run_supervisor_sync", return_value="done"),
        patch.object(server, "_verify_inbound_caller", return_value=caller),
        _capture() as logs,
    ):
        await asyncio.gather(server.run_workflow(MagicMock(), "a", NS), server.run_workflow(MagicMock(), "b", NS))
    assert len({entry["run_id"] for entry in _events(logs, "run_workflow_completed")}) == 2


# --- run_token_usage ---------------------------------------------------------


def _fake_agent(usage: dict[str, int], result: Any = "answer") -> MagicMock:
    agent = MagicMock(side_effect=result if isinstance(result, BaseException) else None, return_value=result)
    agent.event_loop_metrics.accumulated_usage = usage
    return agent


def _run_sync_with(caller: Caller, agents: SupervisorAgents) -> str:
    with (
        patch.object(server, "_caller_client", return_value=MagicMock()),
        patch.object(server, "build_supervisor_agent", return_value=agents),
    ):
        return server._run_supervisor_sync(caller, NS, SENTINEL, frozenset())


def test_run_supervisor_sync_success_logs_token_usage_summed_across_agents(caller):
    supervisor = _fake_agent(
        {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12, "cacheReadInputTokens": 5, "cacheWriteInputTokens": 1}
    )
    subs = (
        _fake_agent({"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}),
        _fake_agent({"inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "cacheReadInputTokens": 7}),
    )
    with capture_logs() as logs:
        assert _run_sync_with(caller, SupervisorAgents(supervisor, subs)) == f"Namespace: {NS}\n\nanswer"

    assert _events(logs, "run_token_usage") == [
        {
            "event": "run_token_usage",
            "log_level": "info",
            "input_tokens": 110,
            "output_tokens": 22,
            "total_tokens": 132,
            "cache_read_input_tokens": 12,
            "cache_write_input_tokens": 1,
            "agents": 3,
        }
    ]


def test_run_supervisor_sync_supervisor_raises_still_logs_token_usage(caller):
    supervisor = _fake_agent({"inputTokens": 4, "outputTokens": 1, "totalTokens": 5}, result=RuntimeError("boom"))
    sub = _fake_agent({"inputTokens": 6, "outputTokens": 3, "totalTokens": 9})
    with capture_logs() as logs, pytest.raises(RuntimeError, match="boom"):
        _run_sync_with(caller, SupervisorAgents(supervisor, (sub,)))

    (usage,) = _events(logs, "run_token_usage")
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"], usage["agents"]) == (10, 4, 14, 2)
    assert usage["cache_read_input_tokens"] == 0 and usage["cache_write_input_tokens"] == 0


def test_run_supervisor_sync_build_raises_logs_no_token_usage(caller):
    with (
        patch.object(server, "_caller_client", return_value=MagicMock()),
        patch.object(server, "build_supervisor_agent", side_effect=RuntimeError("bad build")),
        capture_logs() as logs,
        pytest.raises(RuntimeError, match="bad build"),
    ):
        server._run_supervisor_sync(caller, NS, "x", frozenset())
    assert _events(logs, "run_token_usage") == []


# --- tool_call_completed / sub_agent_call_completed ---------------------------


@tool
def _ok_tool(note: str) -> str:
    """Succeed, echoing caller data into the result."""
    return f"ok {note}"


@tool
def _failing_tool(note: str) -> str:
    """Fail with caller data in the message."""
    raise RuntimeError(f"bad {note}")


@tool
def _mutating_tool(note: str) -> str:
    """A mutating tool the gate protects."""
    return f"mutated {note}"


@tool
def _deadline_tool(note: str) -> str:
    """Raise as a poll cut short by the run deadline would."""
    raise RunDeadlineReachedError(f"induction job {note}", 10.0, {"status": "running"})


def _gated_agent() -> Agent:
    """A sub-agent built by the real ``_build_phase_agent`` wiring, with test tools."""
    gate = MutatingToolGate(mutating_tool_names=frozenset({"_mutating_tool"}), confirmed_tools=frozenset())
    return _build_phase_agent(
        name="TestAgent",
        description="test",
        system_prompt="test",
        tools=[_ok_tool, _failing_tool, _mutating_tool, _deadline_tool],
        gate=gate,
        config=CONFIG,
    )


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("_ok_tool", "success"),
        ("_failing_tool", "error"),
        ("_mutating_tool", "denied"),
        ("_deadline_tool", "still_running"),
    ],
)
def test_tool_call_observer_tool_call_logs_outcome_without_caller_data(tool_name, expected):
    agent = _gated_agent()
    with capture_logs() as logs:
        getattr(agent.tool, tool_name)(note=SENTINEL, record_direct_tool_call=False)

    (entry,) = _events(logs, "tool_call_completed")
    assert set(entry) == {"event", "log_level", "agent", "tool", "outcome", "duration_s"}
    assert entry["agent"] == "TestAgent"
    assert entry["tool"] == tool_name
    assert entry["outcome"] == expected
    assert isinstance(entry["duration_s"], float) and entry["duration_s"] >= 0
    assert len(_events(logs, "tool_still_running")) == (1 if expected == "still_running" else 0)
    assert SENTINEL not in json.dumps(logs)


def _supervisor_call(supervisor: Agent, name: str, *, status: str, exception: Exception | None = None) -> None:
    tool_use = {"name": name, "toolUseId": f"id-{name}", "input": {"input": SENTINEL}}
    # Async: the supervisor's intervention (the gate) registers an async callback.
    asyncio.run(supervisor.hooks.invoke_callbacks_async(BeforeToolCallEvent(supervisor, None, tool_use, {})))
    result = {"toolUseId": f"id-{name}", "status": status, "content": [{"text": SENTINEL}]}
    after = AfterToolCallEvent(supervisor, None, tool_use, {}, result, exception=exception)
    asyncio.run(supervisor.hooks.invoke_callbacks_async(after))


def test_tool_call_observer_sub_agent_call_logs_sub_agent_call_completed():
    def sub(name: str) -> Agent:
        return Agent(name=name, description=name, model=build_bedrock_model(CONFIG), callback_handler=None)

    names = frozenset({"InductionAgent", "AcceptanceAgent"})
    supervisor = Agent(
        name=SUPERVISOR_NAME,
        model=build_bedrock_model(CONFIG),
        tools=[sub(n) for n in sorted(names)],
        hooks=[ToolCallObserver(SUPERVISOR_NAME, names)],
        callback_handler=None,
    )
    with capture_logs() as logs:
        _supervisor_call(supervisor, "InductionAgent", status="success")
        _supervisor_call(supervisor, "AcceptanceAgent", status="error", exception=RuntimeError(SENTINEL))

    assert [
        {key: entry[key] for key in ("sub_agent", "outcome")} for entry in _events(logs, "sub_agent_call_completed")
    ] == [
        {"sub_agent": "InductionAgent", "outcome": "success"},
        {"sub_agent": "AcceptanceAgent", "outcome": "error"},
    ]
    assert all(
        set(entry) == {"event", "log_level", "sub_agent", "outcome", "duration_s"}
        for entry in _events(logs, "sub_agent_call_completed")
    )
    assert _events(logs, "tool_call_completed") == []
    assert SENTINEL not in json.dumps(logs)


def test_tool_call_observer_supervisor_own_tool_logs_tool_call_completed(coa_client):
    supervisor = build_supervisor_agent(coa_client, NS, CONFIG, frozenset()).supervisor
    with capture_logs() as logs:
        _supervisor_call(supervisor, "get_namespace_status", status="success")

    assert _events(logs, "sub_agent_call_completed") == []
    (entry,) = _events(logs, "tool_call_completed")
    assert (entry["agent"], entry["tool"], entry["outcome"]) == (SUPERVISOR_NAME, "get_namespace_status", "success")


def test_tool_call_observer_unknown_tool_name_logs_unknown(coa_client):
    supervisor = build_supervisor_agent(coa_client, NS, CONFIG, frozenset()).supervisor
    with capture_logs() as logs:
        _supervisor_call(supervisor, SENTINEL, status="error", exception=Exception(f"Unknown tool: {SENTINEL}"))

    (entry,) = _events(logs, "tool_call_completed")
    assert (entry["agent"], entry["tool"], entry["outcome"]) == (SUPERVISOR_NAME, "unknown", "error")
    assert SENTINEL not in json.dumps(logs)


def _after(tool_use_id: str) -> MagicMock:
    event = MagicMock(spec=AfterToolCallEvent)
    event.tool_use = {"name": "get_source", "toolUseId": tool_use_id}
    event.cancel_message = None
    event.exception = None
    event.result = {"status": "success"}
    return event


def test_tool_call_observer_concurrent_same_tool_calls_pair_by_tool_use_id():
    observer = ToolCallObserver("A")
    before_a, before_b = MagicMock(spec=BeforeToolCallEvent), MagicMock(spec=BeforeToolCallEvent)
    before_a.tool_use = {"name": "get_source", "toolUseId": "use-a"}
    before_b.tool_use = {"name": "get_source", "toolUseId": "use-b"}
    with (
        patch("coa_workflow_agents.observability.time.monotonic", side_effect=[100.0, 101.0, 104.0, 110.0]),
        capture_logs() as logs,
    ):
        observer.before_tool_call(before_a)  # t=100
        observer.before_tool_call(before_b)  # t=101
        observer.after_tool_call(_after("use-b"))  # t=104 -> 3 s
        observer.after_tool_call(_after("use-a"))  # t=110 -> 10 s

    assert [entry["duration_s"] for entry in _events(logs, "tool_call_completed")] == [3.0, 10.0]


def test_tool_call_observer_after_without_before_logs_zero_duration():
    observer = ToolCallObserver("A")
    event = MagicMock(spec=AfterToolCallEvent)
    event.tool_use = {"name": "t", "toolUseId": "never-started"}
    event.cancel_message = None
    event.exception = None
    event.result = {"status": "success"}
    with capture_logs() as logs:
        observer.after_tool_call(event)
    assert _events(logs, "tool_call_completed")[0]["duration_s"] == 0.0


# --- coa_api_error -----------------------------------------------------------


def _fake_response(status_code: int, text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = text.encode()
    resp.text = text
    resp.headers = {}
    return resp


@pytest.mark.parametrize("status", [400, 404, 409, 500, 503])
def test_request_error_status_logs_coa_api_error_and_raises(status, coa_client):
    client = coa_client
    path = f"/namespaces/{NS}/sources/{SENTINEL}"
    with (
        patch.object(client._session, "request", return_value=_fake_response(status, f"body {SENTINEL}")),
        capture_logs() as logs,
        pytest.raises(CoaApiError) as excinfo,
    ):
        client.get(path)

    assert excinfo.value.status_code == status
    assert _events(logs, "coa_api_error") == [
        {
            "event": "coa_api_error",
            "log_level": "warning",
            "method": "GET",
            "route": "/namespaces/{id}/sources/{id}",
            "status_code": status,
            "error_type": "",
        }
    ]
    assert SENTINEL not in json.dumps(logs)
    assert NS not in json.dumps(logs)


@pytest.mark.parametrize(
    "exc",
    [
        requests.Timeout("t"),
        requests.ConnectionError("c"),
        requests.exceptions.SSLError(),
        requests.exceptions.ChunkedEncodingError("cut off"),
    ],
)
def test_request_transport_error_logs_coa_api_error_and_reraises_unchanged(exc, coa_client):
    client = coa_client
    with (
        patch.object(client._session, "request", side_effect=exc),
        capture_logs() as logs,
        pytest.raises(type(exc)) as excinfo,
    ):
        client.post(f"/namespaces/{NS}/sources/{SENTINEL}/approve", {"note": SENTINEL})

    assert excinfo.value is exc
    assert _events(logs, "coa_api_error") == [
        {
            "event": "coa_api_error",
            "log_level": "warning",
            "method": "POST",
            "route": "/namespaces/{id}/sources/{id}/approve",
            "status_code": 0,
            "error_type": type(exc).__name__,
        }
    ]
    assert SENTINEL not in json.dumps(logs)


def test_request_success_logs_no_coa_api_error(coa_client):
    client = coa_client
    resp = _fake_response(200, "{}")
    resp.json.return_value = {}
    with patch.object(client._session, "request", return_value=resp), capture_logs() as logs:
        client.get("/namespaces")
    assert _events(logs, "coa_api_error") == []


def test_post_idempotent_logs_one_coa_api_error_per_attempt_and_a_sanitized_retry(coa_client):
    client = coa_client
    ok = _fake_response(200, "{}")
    ok.json.return_value = {}
    path = f"/namespaces/{NS}/metrics"
    with (
        patch.object(client._session, "request", side_effect=[_fake_response(503, "busy"), ok]),
        patch.object(coa_api_client.time, "sleep"),
        capture_logs() as logs,
    ):
        client.post_idempotent(path, {"name": SENTINEL}, "tok")

    errors = _events(logs, "coa_api_error")
    assert [e["status_code"] for e in errors] == [503]
    (retry,) = _events(logs, "coa_api_client.idempotent_post_retry")
    assert retry["route"] == "/namespaces/{id}/metrics"
    assert "path" not in retry
    assert SENTINEL not in json.dumps(logs)
    assert NS not in json.dumps(logs)
