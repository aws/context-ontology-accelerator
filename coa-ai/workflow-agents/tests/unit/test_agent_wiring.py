# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""How the agents are wired: each sub-agent's tools, gate and hooks, the supervisor's
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
    ONBOARDING_AGENT,
    ONBOARDING_SYSTEM_PROMPT,
    STILL_RUNNING_RULE,
    build_induction_agent,
    build_onboarding_agent,
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
from coa_workflow_agents.tools import induction, onboarding
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


def _onboarding_agent(coa_client, confirmed: frozenset[str] = frozenset()) -> Agent:
    gate = MutatingToolGate(mutating_tool_names=ALL_MUTATING_TOOL_NAMES, confirmed_tools=confirmed)
    return build_onboarding_agent(coa_client, NS, gate, CONFIG)


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
    agent_tools = set(_induction_agent(coa_client).tool_names) | set(_onboarding_agent(coa_client).tool_names)
    assert agent_tools >= ALL_MUTATING_TOOL_NAMES
    assert ALL_MUTATING_TOOL_NAMES == induction.MUTATING_TOOL_NAMES | onboarding.MUTATING_TOOL_NAMES


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


def test_the_supervisor_has_both_sub_agents_and_the_status_tool(coa_client):
    agents = build_supervisor_agent(coa_client, NS, CONFIG, frozenset())
    assert agents.supervisor.name == SUPERVISOR_NAME
    assert [a.name for a in agents.sub_agents] == [ONBOARDING_AGENT, INDUCTION_AGENT]
    assert set(agents.supervisor.tool_names) == {ONBOARDING_AGENT, INDUCTION_AGENT, "get_namespace_status"}


def test_the_supervisor_prompt_handles_still_running_and_names_no_absent_agent():
    assert "still_running" in SUPERVISOR_SYSTEM_PROMPT
    assert "read-only follow-up" in SUPERVISOR_SYSTEM_PROMPT
    for present in (ONBOARDING_AGENT, INDUCTION_AGENT):
        assert present in SUPERVISOR_SYSTEM_PROMPT
    for absent in ("AcceptanceAgent", "OntologyAgent", "MetricsAgent", "AdminAgent"):
        assert absent not in SUPERVISOR_SYSTEM_PROMPT


def test_the_supervisor_reports_progress_on_each_sub_agent_hand_off(coa_client):
    supervisor = build_supervisor_agent(coa_client, NS, CONFIG, frozenset()).supervisor
    messages: list[str] = []
    token = set_run(RunContext(deadline=time.monotonic() + 3600.0, progress=messages.append))
    try:
        for name in (ONBOARDING_AGENT, INDUCTION_AGENT, "get_namespace_status"):
            tool_use = {"name": name, "toolUseId": "t1", "input": {}}
            # Async: the supervisor's intervention (the gate) registers an async callback.
            asyncio.run(supervisor.hooks.invoke_callbacks_async(BeforeToolCallEvent(supervisor, None, tool_use, {})))
    finally:
        reset_run(token)
    # The supervisor's own tool is not a hand-off.
    assert messages == [f"calling {ONBOARDING_AGENT}", f"calling {INDUCTION_AGENT}"]


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


# --- the OnboardingAgent ---------------------------------------------------------


def test_the_onboarding_agent_has_exactly_the_onboarding_tools(coa_client):
    agent = _onboarding_agent(coa_client)
    assert agent.name == ONBOARDING_AGENT
    expected = {t.tool_name for t in onboarding.build_onboarding_tools(coa_client, NS)}
    assert set(agent.tool_names) == expected
    # Pinned by name so a write tool left out of MUTATING_TOOL_NAMES would be caught, not registered ungated.
    assert set(agent.tool_names) - onboarding.MUTATING_TOOL_NAMES == {
        "get_source_status",
        "list_sources",
        "get_scan_job_status",
        "list_source_scan_jobs",
    }


def test_the_onboarding_prompt_ends_with_the_still_running_rule(coa_client):
    prompt = _onboarding_agent(coa_client).system_prompt
    assert prompt is not None and prompt.endswith(STILL_RUNNING_RULE)


def test_the_onboarding_prompt_names_the_tools_it_uses_and_none_of_the_table_review_ones(coa_client):
    tools = set(_onboarding_agent(coa_client).tool_names)
    words = {w.strip(".,;:()") for w in ONBOARDING_SYSTEM_PROMPT.split()}
    for named in ("create_source", "create_document_source", "list_sources", "get_source_status", "approve_source"):
        assert named in tools and named in words
    # Table review arrives with its tools; until then the prompt must not send the agent to them.
    for absent in ("list_source_tables", "get_source_table", "review_table", "review_column", "update_table_keys"):
        assert absent not in words


@pytest.mark.parametrize(
    "sentence",
    [
        "set its confirm_discard_open_review only after the caller has said to discard the open re-scan review",
        "If it returns alreadyGone, tell the caller COA has no source with that id in this namespace",
        "a retry discards the previously approved scan, so ask the caller first",
    ],
)
def test_the_onboarding_prompt_gates_the_argument_level_risks_the_gate_cannot_see(sentence):
    """The gate decides on the tool name, so these rest on the prompt."""
    assert sentence in " ".join(ONBOARDING_SYSTEM_PROMPT.split())


def test_every_onboarding_write_is_named_in_the_prompts_never_without_intent_rule():
    rule = " ".join(ONBOARDING_SYSTEM_PROMPT.split()).split("Never call", 1)[1]
    assert all(name in rule for name in onboarding.MUTATING_TOOL_NAMES)


def test_the_onboarding_agent_denies_an_unconfirmed_approve_source_before_calling_coa():
    client = MagicMock()
    client.get_source.return_value = {"sourceId": "s1", "status": "PENDING_REVIEW"}
    agent = _onboarding_agent(client)
    result = agent.tool.approve_source(source_id="s1", record_direct_tool_call=False)
    assert result["status"] == "error"
    assert "confirm_mutations" in str(result["content"])
    client.approve_source.assert_not_called()


def test_the_onboarding_agent_runs_a_confirmed_approve_source():
    client = MagicMock()
    client.get_source.return_value = {"sourceId": "s1", "status": "APPROVED"}
    agent = _onboarding_agent(client, frozenset({"approve_source"}))
    result = agent.tool.approve_source(source_id="s1", record_direct_tool_call=False)
    assert result["status"] == "success", result


def test_confirming_one_onboarding_tool_does_not_confirm_another():
    client = MagicMock()
    agent = _onboarding_agent(client, frozenset({"approve_source"}))
    result = agent.tool.delete_source(source_id="s1", record_direct_tool_call=False)
    assert result["status"] == "error"
    client.delete_source.assert_not_called()
