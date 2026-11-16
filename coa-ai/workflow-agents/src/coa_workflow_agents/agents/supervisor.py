# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The supervisor agent that ``run_workflow`` drives.

Built fresh for every call: it carries that caller's client (their own token) and a
``MutatingToolGate`` scoped to that call's ``confirm_mutations``, which every sub-agent
registers too. Construction makes no network call. Its tools are the read-only
``get_namespace_status`` and the InductionAgent (``sub_agents.py``).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from strands import Agent
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

from coa_workflow_agents.agents.model import build_bedrock_model
from coa_workflow_agents.agents.sub_agents import build_induction_agent
from coa_workflow_agents.clients.coa_api_client import CoaApiClient
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.intervention import MutatingToolGate
from coa_workflow_agents.observability import ToolCallObserver
from coa_workflow_agents.run_context import report_progress
from coa_workflow_agents.tools import induction
from coa_workflow_agents.tools.status import build_status_tools

# Mutating tool names across all agents: what confirm_mutations may name.
ALL_MUTATING_TOOL_NAMES = frozenset(induction.MUTATING_TOOL_NAMES)

SUPERVISOR_NAME = "CoaWorkflowSupervisor"

SUPERVISOR_SYSTEM_PROMPT = """\
You are the COA Workflow supervisor. You help a caller understand and drive the \
Context Ontology Accelerator (COA) in one namespace.

You have one specialist sub-agent as a tool:
- InductionAgent: run ontology induction over approved sources (a real, billable \
job; the call waits about 54 minutes at most, then reports it as still \
running), report induction jobs, and list the sources \
already in an accepted ontology.

You also have ONE tool of your own:
- get_namespace_status: a read-only, end-to-end status overview of the namespace in a \
single call — every source and its status, every induction job, and every proposal, \
correlated by datasource. For a status or overview question, call it directly instead \
of delegating. It does not cover metrics, the namespace's own status \
(ACTIVE/ARCHIVED) or its settings.

Induction needs APPROVED sources: check with get_namespace_status first when you are \
not sure. You cannot onboard or approve a source, or review, accept, reject or cancel \
a proposal: if the caller asks for any of these, or any other change, say that this \
server cannot do that yet, and change nothing.

Tell the caller what you are doing before you delegate, and report the concrete result \
(sourceId, jobId, proposalId, final status) after it — do not just say "done".

start_induction is DENIED at the code level unless the caller authorized it for this \
run (the run_workflow 'confirm_mutations' argument). If the InductionAgent reports a \
denial for this reason, do not retry: tell the caller that they must re-issue the \
request naming start_induction in 'confirm_mutations'.

If the InductionAgent reports that an operation is "still_running", the run reached \
its deadline while COA is still processing it. Do not delegate any further step that \
depends on it. Tell the caller which operation and ids are still running, and that \
they should send a read-only follow-up to check its status."""


class SupervisorAgents(NamedTuple):
    """The supervisor built for one run, and the sub-agents it calls as tools."""

    supervisor: Agent
    sub_agents: tuple[Agent, ...]


class HandoffProgressHook(HookProvider):
    """Sends ``calling <sub-agent>`` progress each time the supervisor hands off.

    So the caller sees which phase is running between heartbeats. Sent before the call, so
    it says "calling", not that the phase ran. A no-op outside a run.
    """

    def __init__(self, sub_agent_names: frozenset[str]) -> None:
        """Report hand-offs to the tools named ``sub_agent_names``."""
        self._sub_agent_names = sub_agent_names

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register the before-tool-call callback."""
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Report the hand-off when the called tool is a sub-agent."""
        name = event.tool_use.get("name", "")
        if name in self._sub_agent_names:
            report_progress(f"calling {name}")


def build_supervisor_agent(
    client: CoaApiClient,
    namespace_id: str,
    config: WorkflowAgentsConfig,
    confirmed_tools: frozenset[str],
) -> SupervisorAgents:
    """Build the supervisor for one call, bound to ``namespace_id`` and the caller's ``client``.

    ``confirmed_tools`` is the normalized ``confirm_mutations`` set
    (``intervention.normalize_confirm_mutations``). The namespace is bound here, not passed
    per tool call, so the model cannot switch it.
    """
    gate = MutatingToolGate(mutating_tool_names=ALL_MUTATING_TOOL_NAMES, confirmed_tools=confirmed_tools)
    sub_agents = (build_induction_agent(client, namespace_id, gate, config),)
    sub_agent_names = frozenset(agent.name for agent in sub_agents)
    supervisor = Agent(
        name=SUPERVISOR_NAME,
        description="Reports the status of a COA namespace and runs ontology induction in it.",
        system_prompt=SUPERVISOR_SYSTEM_PROMPT,
        model=build_bedrock_model(config),
        tools=[*sub_agents, *build_status_tools(client, namespace_id)],
        interventions=[gate],
        hooks=[HandoffProgressHook(sub_agent_names), ToolCallObserver(SUPERVISOR_NAME, sub_agent_names)],
        # No PrintingCallbackHandler: it prints every streamed token (caller data) to stdout.
        callback_handler=None,
    )
    return SupervisorAgents(supervisor=supervisor, sub_agents=sub_agents)
