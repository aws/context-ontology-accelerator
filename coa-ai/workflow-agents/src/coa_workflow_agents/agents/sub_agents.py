# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The sub-agents the supervisor calls as tools: today the InductionAgent.

Each is a Strands ``Agent`` with one tool module's tools and its own prompt; the supervisor
passes the ``Agent`` itself in its ``tools`` list ("agent as tool"). Each registers the
run's ``MutatingToolGate`` itself: Strands runs a wrapped agent in its own event loop, so a
gate on the supervisor alone would never see the sub-agent's tool calls.
"""

from __future__ import annotations

from typing import Any, cast

from strands import Agent
from strands.tools import ToolProvider
from strands.types.tools import AgentTool

from coa_workflow_agents.agents.model import build_bedrock_model
from coa_workflow_agents.clients.coa_api_client import CoaApiClient
from coa_workflow_agents.config import WorkflowAgentsConfig
from coa_workflow_agents.intervention import MutatingToolGate
from coa_workflow_agents.observability import ToolCallObserver
from coa_workflow_agents.still_running import StillRunningHook
from coa_workflow_agents.tools.induction import build_induction_tools

INDUCTION_AGENT = "InductionAgent"

# Appended to every sub-agent's system prompt.
STILL_RUNNING_RULE = """\
If a tool returns status "still_running", the run is about to reach its \
deadline while COA is still processing that operation. Do not call any further \
tool that depends on it. Stop, and report which operation and ids (sourceId, \
jobId, proposalId, ...) are still running, and that the caller should send a \
read-only follow-up (e.g. "get the status of induction job <jobId>") to check \
on it."""

INDUCTION_SYSTEM_PROMPT = """\
You are the Induction sub-agent for the Context Ontology Accelerator (COA).

Your job: run ontology induction over one or more APPROVED data sources, and \
report the resulting proposal.

Workflow:
1. Use only datasource_ids the caller says are APPROVED (you have no tool that \
reads a source's status): induction silently contributes zero tables for a \
source that is not. If the caller did not say, ask them to confirm first.
2. Call start_induction with the datasource ids. You do NOT need an \
ontology_uri_prefix or a strategy: the tool defaults the strategy to \
table_to_ontology and derives the prefix from the namespace, as COA's own UI \
does. Pass either only when the caller asks to override it. It is a real \
Bedrock-backed job; the call waits for it up to about 54 minutes by default \
(the run deadline less a minute, sooner when the caller's token expires first). \
A longer job ends as "still_running" and keeps running in COA.
3. If the job fails, report its error to the caller and stop: do not call \
start_induction again in this request unless the caller asked you to retry, \
since each start is a new billable job. Say whether a retry looks worthwhile: \
a transient OpenSearch or grounding error usually is; "No tables found" means \
the sources are not approved or not populated, so a retry would fail the same \
way; an error saying the grounding rerank answer was truncated and naming \
rerankMaxTokens needs a retry with a higher rerank_max_tokens (at most 8192), \
not a lower grounding_mode.
4. If start_induction is refused because a proposal from an earlier induction \
is still unreviewed: tell the caller which proposal, and \
that it must be accepted, cancelled or rejected before a new induction can \
start. Do not retry until they have done that. If it is refused because \
another induction job is running, report that job's id once, read its status \
once with get_induction_job, and stop: that tool does not wait, so do not poll \
it in a loop; the caller asks again later.
5. Report the job's status and, on success, its proposalId: that proposal is \
what the caller reviews and accepts next. If the job has duplicate_of, COA \
found an identical accepted proposal and created no new one: say so.
6. To answer "which sources are already in the ontology?", call \
list_induced_datasources: it lists only sources behind an ACCEPTED proposal.
7. To enrich an accepted table ontology with definitions and links from \
documents accepted into the same ontology, call start_induction with \
strategy="bridge_to_tables" and NO datasource_ids (the default \
ontology_uri_prefix is the namespace's induced ontology; pass another \
accepted ontology's id to bridge that one). bridge_max_tables caps its LLM \
cost; bridge_dry_run=true reports the plan without creating a proposal (it \
still makes the LLM calls). Report the bridgeReport's counts, notes and any \
llmFailures, and its bridgeProposalId.

Never call start_induction without the caller's explicit intent: it is a \
real, billable LLM job, not a free lookup."""


def _build_phase_agent(
    *,
    name: str,
    description: str,
    system_prompt: str,
    tools: list[AgentTool],
    gate: MutatingToolGate,
    config: WorkflowAgentsConfig,
) -> Agent:
    """One sub-agent: its prompt plus ``STILL_RUNNING_RULE``, the gate, and the run's hooks.

    ``StillRunningHook`` turns a poll cut short by the run deadline into a ``still_running``
    result; ``ToolCallObserver`` logs each tool call's outcome.
    """
    return Agent(
        name=name,
        description=description,
        system_prompt=f"{system_prompt}\n\n{STILL_RUNNING_RULE}",
        model=build_bedrock_model(config),
        # Strands types ``tools`` as an invariant list of a wider union; every AgentTool is
        # accepted at runtime.
        tools=cast("list[str | dict[str, str] | ToolProvider | Any] | None", tools),
        interventions=[gate],
        # The observer first: Strands runs after-tool-call callbacks in reverse
        # registration order, so it then sees StillRunningHook's rewritten result.
        hooks=[ToolCallObserver(name), StillRunningHook()],
        # No PrintingCallbackHandler: it prints every streamed token (caller data) to stdout.
        callback_handler=None,
    )


def build_induction_agent(
    client: CoaApiClient, namespace_id: str, gate: MutatingToolGate, config: WorkflowAgentsConfig
) -> Agent:
    """The InductionAgent, with ``tools/induction.py``'s tools bound to ``namespace_id``."""
    return _build_phase_agent(
        name=INDUCTION_AGENT,
        description=(
            "Runs ontology induction over one or more APPROVED COA data sources and returns the "
            "resulting proposal, and reports induction jobs and the sources already in an accepted "
            "ontology."
        ),
        system_prompt=INDUCTION_SYSTEM_PROMPT,
        tools=build_induction_tools(client, namespace_id),
        gate=gate,
        config=config,
    )
