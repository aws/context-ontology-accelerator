# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""How the agents are wired: the InductionAgent's tools, gate and hooks, the supervisor's
hand-off progress, and the ``StillRunningHook`` that turns a deadline-cut poll into a result."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from coa_workflow_agents.agents.sub_agents import (
    INDUCTION_AGENT,
    INDUCTION_SYSTEM_PROMPT,
    STILL_RUNNING_RULE,
    build_induction_agent,
)
from coa_workflow_agents.agents.supervisor import (
    ALL_MUTATING_TOOL_NAMES,
    SUPERVISOR_NAME,
    SUPERVISOR_SYSTEM_PROMPT,
    build_supervisor_agent,
)
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.intervention import MutatingToolGate
from coa_workflow_agents.observability import ToolCallObserver
from coa_workflow_agents.run_context import RunContext, reset_run, set_run
from coa_workflow_agents.still_running import STILL_RUNNING_MESSAGE, StillRunningHook
from coa_workflow_agents.tools import induction
from coa_workflow_agents.tools.polling import PollTimeoutError, RunDeadlineReachedError
from strands import Agent, tool
from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

NS = "11111111-1111-4111-8111-111111111111"
CONFIG = WorkflowAgentsConfig(bedrock_model_id="", aws_region="us-east-1")


def _induction_agent(coa_client, confirmed: frozenset[str] = frozenset()) -> Agent:
    gate = MutatingToolGate(mutating_tool_names=ALL_MUTATING_TOOL_NAMES, confirmed_tools=confirmed)
    return build_induction_agent(coa_client, NS, gate, CONFIG)


def _after_tool_call_owners(agent: Agent) -> list[type]:
    callbacks = agent.hooks._registered_callbacks.get(AfterToolCallEvent, [])
    return [type(getattr(entry.callback, "__self__", None)) for entry in callbacks]


# --- the InductionAgent ----------------------------------------------------------


def test_the_induction_agent_has_exactly_the_induction_tools(coa_client):
    agent = _induction_agent(coa_client)
    assert agent.name == INDUCTION_AGENT
    expected = {t.tool_name for t in induction.build_induction_tools(coa_client, NS)}
    assert set(agent.tool_names) == expected


def test_every_mutating_tool_is_one_an_agent_has(coa_client):
    assert set(_induction_agent(coa_client).tool_names) >= ALL_MUTATING_TOOL_NAMES


def test_the_induction_agent_denies_an_unconfirmed_start_induction_before_calling_coa():
    client = MagicMock()
    # Scripted to finish at once, so a missing gate fails the assertion instead of polling.
    client.start_induction.return_value = {"job_id": "j1"}
    client.get_induction_job.return_value = {"jobId": "j1", "status": "completed"}
    agent = _induction_agent(client)
    result = agent.tool.start_induction(datasource_ids=["s1"], record_direct_tool_call=False)
    assert result["status"] == "error"
    assert "confirm_mutations" in str(result["content"])
    client.start_induction.assert_not_called()


@pytest.mark.parametrize("confirmed", [frozenset({"start_induction"}), frozenset({"*"})], ids=["named", "all"])
def test_the_induction_agent_runs_a_confirmed_start_induction(confirmed):
    client = MagicMock()
    client.start_induction.return_value = {"job_id": "j1"}
    client.get_induction_job.return_value = {"jobId": "j1", "status": "completed"}
    agent = _induction_agent(client, confirmed)
    result = agent.tool.start_induction(datasource_ids=["s1"], record_direct_tool_call=False)
    assert result["status"] == "success", result
    client.start_induction.assert_called_once()


def test_the_induction_agent_prompt_ends_with_the_still_running_rule(coa_client):
    agent = _induction_agent(coa_client)
    assert agent.system_prompt is not None and agent.system_prompt.endswith(STILL_RUNNING_RULE)
    assert "still_running" in STILL_RUNNING_RULE


def test_the_observer_is_registered_before_the_still_running_hook(coa_client):
    # After-tool-call callbacks run in reverse order, so the observer sees the rewritten result.
    owners = _after_tool_call_owners(_induction_agent(coa_client))
    assert owners.index(ToolCallObserver) < owners.index(StillRunningHook)


# --- the supervisor --------------------------------------------------------------


def test_the_supervisor_has_the_induction_agent_and_the_status_tool(coa_client):
    agents = build_supervisor_agent(coa_client, NS, CONFIG, frozenset())
    assert agents.supervisor.name == SUPERVISOR_NAME
    (sub_agent,) = agents.sub_agents
    assert sub_agent.name == INDUCTION_AGENT
    assert set(agents.supervisor.tool_names) == {INDUCTION_AGENT, "get_namespace_status"}


def test_the_supervisor_prompt_handles_still_running_and_names_no_absent_agent():
    assert "still_running" in SUPERVISOR_SYSTEM_PROMPT
    assert "read-only follow-up" in SUPERVISOR_SYSTEM_PROMPT
    for absent in ("OnboardingAgent", "AcceptanceAgent", "OntologyAgent", "MetricsAgent", "AdminAgent"):
        assert absent not in SUPERVISOR_SYSTEM_PROMPT


def test_the_supervisor_reports_progress_on_each_sub_agent_hand_off(coa_client):
    supervisor = build_supervisor_agent(coa_client, NS, CONFIG, frozenset()).supervisor
    messages: list[str] = []
    token = set_run(RunContext(deadline=time.monotonic() + 3600.0, progress=messages.append))
    try:
        for name in (INDUCTION_AGENT, "get_namespace_status"):
            tool_use = {"name": name, "toolUseId": "t1", "input": {}}
            # Async: the supervisor's intervention (the gate) registers an async callback.
            asyncio.run(supervisor.hooks.invoke_callbacks_async(BeforeToolCallEvent(supervisor, None, tool_use, {})))
    finally:
        reset_run(token)
    # The supervisor's own tool is not a hand-off.
    assert messages == [f"calling {INDUCTION_AGENT}"]


# --- StillRunningHook ------------------------------------------------------------


@tool
def _slow_job() -> dict[str, Any]:
    """Raise as a poll cut short by the run deadline would."""
    raise RunDeadlineReachedError("induction job j-1", 10.0, {"status": "running"})


@tool
def _broken_job() -> dict[str, Any]:
    """Raise an ordinary poll timeout."""
    raise PollTimeoutError("induction job j-2", 10.0, {"status": "running"})


def test_still_running_hook_turns_run_deadline_reached_into_a_still_running_result():
    agent = Agent(tools=[_slow_job], hooks=[StillRunningHook()], callback_handler=None)
    with capture_logs() as logs:
        result = agent.tool._slow_job(record_direct_tool_call=False)
    assert result["status"] == "success"
    assert json.loads(result["content"][0]["text"]) == {
        "status": "still_running",
        "operation": "induction job j-1",
        "message": STILL_RUNNING_MESSAGE,
    }
    # The id stays in the result; the log names the tool only.
    assert [e for e in logs if e["event"] == "tool_still_running"] == [
        {"event": "tool_still_running", "tool_name": "_slow_job", "log_level": "warning"}
    ]


def test_still_running_hook_leaves_other_errors_alone():
    agent = Agent(tools=[_broken_job], hooks=[StillRunningHook()], callback_handler=None)
    result = agent.tool._broken_job(record_direct_tool_call=False)
    assert result["status"] == "error"
    assert "still_running" not in result["content"][0]["text"]


def test_the_induction_prompt_never_lets_the_agent_retry_a_failed_job_on_its_own():
    # Each start is a new billable job, and a confirmed start_induction stays allowed for the run.
    assert "do not call start_induction again in this request unless the caller asked you to retry" in " ".join(
        INDUCTION_SYSTEM_PROMPT.split()
    )
    assert "safe to retry" not in INDUCTION_SYSTEM_PROMPT
