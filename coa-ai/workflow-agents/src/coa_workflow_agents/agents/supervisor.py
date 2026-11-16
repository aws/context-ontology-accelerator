# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The supervisor agent that ``run_workflow`` drives.

Built fresh for every call: it carries that caller's client (their own token) and a
``MutatingToolGate`` scoped to that call's ``confirm_mutations``. Construction makes no
network call. Its only tool is the read-only ``get_namespace_status``.
"""

from __future__ import annotations

from typing import NamedTuple

from strands import Agent

from coa_workflow_agents.agents.model import build_bedrock_model
from coa_workflow_agents.clients.coa_api_client import CoaApiClient
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.intervention import MutatingToolGate
from coa_workflow_agents.observability import ToolCallObserver
from coa_workflow_agents.tools.status import build_status_tools

# Mutating tool names across all agents; empty while the supervisor's only tool is read-only.
ALL_MUTATING_TOOL_NAMES: frozenset[str] = frozenset()

SUPERVISOR_NAME = "CoaWorkflowSupervisor"

SUPERVISOR_SYSTEM_PROMPT = """\
You are the COA Workflow supervisor. You help a caller understand and drive the \
Context Ontology Accelerator (COA) in one namespace.

You have ONE tool:
- get_namespace_status: a read-only, end-to-end status overview of the namespace in a \
single call — every source and its status, every induction job, and every proposal, \
correlated by datasource. It does not cover metrics, the namespace's own status \
(ACTIVE/ARCHIVED) or its settings.

Answer status and overview questions with it, and report the concrete ids and statuses \
it returns. You cannot change anything yet: if the caller asks you to onboard, approve, \
induce, accept, delete or otherwise change COA, say that this server cannot do that yet, \
and change nothing."""


class SupervisorAgents(NamedTuple):
    """The supervisor built for one run, and the sub-agents it calls as tools (none today)."""

    supervisor: Agent
    sub_agents: tuple[Agent, ...]


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
    supervisor = Agent(
        name=SUPERVISOR_NAME,
        description="Reports the end-to-end status of a COA namespace.",
        system_prompt=SUPERVISOR_SYSTEM_PROMPT,
        model=build_bedrock_model(config),
        tools=[*build_status_tools(client, namespace_id)],
        interventions=[gate],
        hooks=[ToolCallObserver(SUPERVISOR_NAME)],
        # No PrintingCallbackHandler: it prints every streamed token (caller data) to stdout.
        callback_handler=None,
    )
    return SupervisorAgents(supervisor=supervisor, sub_agents=())
