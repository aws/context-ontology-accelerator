# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Bedrock-backed reasoning planner (task 18 wiring).

Covers ``coa_serve.tier3.agentic.planner.BedrockStepPlanner``: the
concrete :class:`StepPlanner` that adapts a Bedrock :class:`LLMClient` into the
narrow planner Protocol the :class:`ReasoningController` consults. All tests
inject a mocked ``LLMClient`` whose ``converse`` returns scripted JSON, so the
planner decisions are fully deterministic without a live Bedrock call.

Asserted behavior:
- ``propose_steps`` parses the planner's ``{"candidates": [...]}`` JSON into
  :class:`PlannerDecision`s, tolerating fences/prose and coercing fields (Req 4.3).
- ``propose_steps`` returns ``[]`` (no tool selectable) when there are no tools,
  the Converse call raises, the guardrail blocks, or the response is unparseable
  (Req 4.7).
- ``check_sufficiency`` parses ``{"sufficient": <bool>}`` and degrades to ``False``
  on failure / guardrail block / unparseable response (Req 6.1-6.2).
- The planner sends the static guidance via ``system`` and guard-tags the
  sub-question via ``guard_content`` (guardrail not bypassed), at temperature 0.0.
- The lazy-import invariant (Req 12.3): importing the planner does not import
  ``graphrag_toolkit``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.models import PlannerDecision, SubQuestion
from coa_serve.tier3.agentic.planner import BedrockStepPlanner


class FakeLLM:
    """Mock LLMClient.converse returning scripted text / behavior."""

    def __init__(self, *, text="{}", guardrail_blocked=False, raises=None):
        self._text = text
        self._guardrail_blocked = guardrail_blocked
        self._raises = raises
        self.calls: list[dict] = []

    async def converse(
        self, *, prompt, system=None, guardrail_id=None, guard_content=None, temperature=None, max_tokens=4096
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "guardrail_id": guardrail_id,
                "guard_content": guard_content,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(text=self._text, guardrail_blocked=self._guardrail_blocked)


_SPECS = [
    {
        "name": "strategy:entity_based",
        "description": "Anchor an entity by name.",
        "input_schema": {"query": "str"},
        "output_schema": {"chunks": "list", "entities": "list"},
    },
    {
        "name": "ontology_lookup",
        "description": "Return the namespace ontology.",
        "input_schema": {},
        "output_schema": {"items": "list"},
    },
]


def _sq(text="Amazon competitors"):
    return SubQuestion(text=text)


# ── propose_steps parsing (Req 4.3) ─────────────────────────────────────


@pytest.mark.unit
class TestProposeSteps:
    @pytest.mark.asyncio
    async def test_parses_candidates_into_decisions(self):
        text = (
            '{"candidates": [{"tool_name": "strategy:entity_based", '
            '"inputs": {"query": "Amazon"}, "reasoning": "anchor entity", '
            '"is_ad_hoc": false}]}'
        )
        planner = BedrockStepPlanner(FakeLLM(text=text))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert len(decisions) == 1
        d = decisions[0]
        assert isinstance(d, PlannerDecision)
        assert d.tool_name == "strategy:entity_based"
        assert d.inputs == {"query": "Amazon"}
        assert d.reasoning == "anchor entity"
        assert d.is_ad_hoc is False

    @pytest.mark.asyncio
    async def test_parses_multiple_candidates(self):
        text = (
            '{"candidates": ['
            '{"tool_name": "ontology_lookup"},'
            '{"tool_name": "strategy:entity_based", "inputs": {"query": "Amazon"}}'
            "]}"
        )
        planner = BedrockStepPlanner(FakeLLM(text=text))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert [d.tool_name for d in decisions] == ["ontology_lookup", "strategy:entity_based"]
        # Missing inputs default to {} (Req 2.4 contract checked later in controller).
        assert decisions[0].inputs == {}

    @pytest.mark.asyncio
    async def test_tolerates_code_fences_and_prose(self):
        text = 'Here you go:\n```json\n{"candidates": [{"tool_name": "ontology_lookup"}]}\n```\nDone.'
        planner = BedrockStepPlanner(FakeLLM(text=text))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert [d.tool_name for d in decisions] == ["ontology_lookup"]

    @pytest.mark.asyncio
    async def test_marks_ad_hoc(self):
        text = '{"candidates": [{"tool_name": "graph_traversal", "is_ad_hoc": true}]}'
        planner = BedrockStepPlanner(FakeLLM(text=text))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert decisions[0].is_ad_hoc is True

    @pytest.mark.asyncio
    async def test_skips_entries_without_tool_name(self):
        text = '{"candidates": [{"inputs": {"query": "x"}}, {"tool_name": "ontology_lookup"}]}'
        planner = BedrockStepPlanner(FakeLLM(text=text))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert [d.tool_name for d in decisions] == ["ontology_lookup"]

    @pytest.mark.asyncio
    async def test_chunk_window_advertised_only_when_registered(self):
        """The chunk-window guidance is appended to the system prompt ONLY when the
        chunk_window tool is present in tool_specs — otherwise the planner would
        select an unregistered tool (unknown_tool:chunk_window) and waste a step."""
        # Absent from _SPECS → not advertised.
        llm = FakeLLM(text='{"candidates": []}')
        planner = BedrockStepPlanner(llm)
        await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert "chunk-window tool" not in llm.calls[0]["system"]

        # Present in specs → advertised.
        specs_with_cw = [
            *_SPECS,
            {
                "name": "chunk_window",
                "description": "adjacent chunks",
                "input_schema": {},
                "output_schema": {},
            },
        ]
        llm2 = FakeLLM(text='{"candidates": []}')
        planner2 = BedrockStepPlanner(llm2)
        await planner2.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=specs_with_cw)
        assert "chunk-window tool" in llm2.calls[0]["system"]


# ── propose_steps degradation (Req 4.7) ─────────────────────────────────


@pytest.mark.unit
class TestProposeStepsDegradation:
    @pytest.mark.asyncio
    async def test_empty_when_no_tools(self):
        planner = BedrockStepPlanner(FakeLLM(text='{"candidates": [{"tool_name": "x"}]}'))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=[])
        assert decisions == []

    @pytest.mark.asyncio
    async def test_empty_on_converse_error(self):
        planner = BedrockStepPlanner(FakeLLM(raises=RuntimeError("bedrock down")))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert decisions == []

    @pytest.mark.asyncio
    async def test_empty_on_guardrail_block(self):
        planner = BedrockStepPlanner(FakeLLM(text='{"candidates": []}', guardrail_blocked=True))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert decisions == []

    @pytest.mark.asyncio
    async def test_empty_on_unparseable(self):
        planner = BedrockStepPlanner(FakeLLM(text="not json at all"))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert decisions == []

    @pytest.mark.asyncio
    async def test_empty_when_candidates_not_list(self):
        planner = BedrockStepPlanner(FakeLLM(text='{"candidates": "nope"}'))
        decisions = await planner.propose_steps(sub_question=_sq(), context=AccumulatedContext(), tool_specs=_SPECS)
        assert decisions == []


# ── check_sufficiency (Req 6.1-6.2) ─────────────────────────────────────


@pytest.mark.unit
class TestCheckSufficiency:
    @pytest.mark.asyncio
    async def test_true(self):
        planner = BedrockStepPlanner(FakeLLM(text='{"sufficient": true}'))
        assert await planner.check_sufficiency(sub_question=_sq(), context=AccumulatedContext()) is True

    @pytest.mark.asyncio
    async def test_false(self):
        planner = BedrockStepPlanner(FakeLLM(text='{"sufficient": false}'))
        assert await planner.check_sufficiency(sub_question=_sq(), context=AccumulatedContext()) is False

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        planner = BedrockStepPlanner(FakeLLM(raises=RuntimeError("down")))
        assert await planner.check_sufficiency(sub_question=_sq(), context=AccumulatedContext()) is False

    @pytest.mark.asyncio
    async def test_false_on_guardrail_block(self):
        planner = BedrockStepPlanner(FakeLLM(text='{"sufficient": true}', guardrail_blocked=True))
        assert await planner.check_sufficiency(sub_question=_sq(), context=AccumulatedContext()) is False

    @pytest.mark.asyncio
    async def test_false_on_unparseable(self):
        planner = BedrockStepPlanner(FakeLLM(text="maybe?"))
        assert await planner.check_sufficiency(sub_question=_sq(), context=AccumulatedContext()) is False

    @pytest.mark.asyncio
    async def test_false_when_missing_key(self):
        planner = BedrockStepPlanner(FakeLLM(text='{"other": true}'))
        assert await planner.check_sufficiency(sub_question=_sq(), context=AccumulatedContext()) is False


# ── guardrail / determinism plumbing ────────────────────────────────────


@pytest.mark.unit
class TestPlannerCallShape:
    @pytest.mark.asyncio
    async def test_guard_content_and_system_and_temperature(self):
        """Static guidance goes via ``system``; the sub-question is guard-tagged;
        the call is deterministic (temperature 0.0) and carries the guardrail id."""
        fake = FakeLLM(text='{"candidates": []}')
        planner = BedrockStepPlanner(fake, guardrail_id="gr-123")
        await planner.propose_steps(
            sub_question=_sq("Amazon competitors"), context=AccumulatedContext(), tool_specs=_SPECS
        )
        call = fake.calls[-1]
        assert call["system"] is not None and "planner" in call["system"].lower()
        assert call["guardrail_id"] == "gr-123"
        assert call["guard_content"] == "Question: Amazon competitors"
        assert call["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_context_summary_includes_ontology_edges(self):
        """When the ontology is resolved, its edge types appear in the prompt so
        the planner can choose meaningful traversals."""
        fake = FakeLLM(text='{"candidates": []}')
        planner = BedrockStepPlanner(fake)
        ctx = AccumulatedContext()
        ctx.ontology = SimpleNamespace(edge_types=("COMPETES_WITH", "HAS_FINANCIALS"))
        await planner.propose_steps(sub_question=_sq(), context=ctx, tool_specs=_SPECS)
        prompt = fake.calls[-1]["prompt"]
        assert "COMPETES_WITH" in prompt
        assert "HAS_FINANCIALS" in prompt

    @pytest.mark.asyncio
    async def test_system_prompt_carries_graph_exploration_workflow(self):
        """The standing graph-exploration workflow (resolve ontology → anchor
        entities → follow relationships) is in the SYSTEM instruction, applies even
        to a bare question, and tells the planner the semantic search is automatic
        at the end so it focuses on graph exploration."""
        fake = FakeLLM(text='{"candidates": []}')
        planner = BedrockStepPlanner(fake)
        await planner.propose_steps(
            sub_question=_sq("What are NVIDIA's market segments?"),
            context=AccumulatedContext(),
            tool_specs=_SPECS,
        )
        system = fake.calls[-1]["system"].lower()
        # The three graph-exploration steps.
        assert "ontology" in system and "first" in system
        assert "anchor" in system and "entit" in system
        assert "relationship" in system or "travers" in system
        # Applies even without the user asking.
        assert "even when" in system or "standing strategy" in system
        # Tells the planner a final semantic search is automatic, so it should
        # focus on graph exploration (not spend a step on a plain semantic search).
        assert "semantic search" in system and ("end of the session" in system or "automatically" in system)

    @pytest.mark.asyncio
    async def test_assess_system_carries_language_constraint(self):
        """Work item #901: the assess prompt (which produces working_answer, later injected
        into synthesis as <agent_preliminary_findings>) must instruct the model to write in
        the sub-question's language, so it cannot seed a foreign language into synthesis."""
        fake = FakeLLM(text='{"sufficient": true, "working_answer": "ok"}')
        planner = BedrockStepPlanner(fake)
        await planner.assess(sub_question=_sq("How is permission enforced?"), context=AccumulatedContext())
        system = fake.calls[-1]["system"]
        assert "LANGUAGE:" in system
        assert "SAME language as the sub-question" in system
        # Explicitly tells it to ignore the (possibly foreign-language) gathered context.
        assert "ignore their language" in system.lower()


# ── lazy-import invariant (Req 12.3) ────────────────────────────────────


@pytest.mark.unit
class TestStructuredRowsVisibility:
    """Rows gathered by structured tools must be visible to BOTH planner prompts.

    Before this, only chunks/entities were rendered: a sub-question fully
    answered by NL->SQL rows was assessed as "no data", the working answer
    denied having data, and tool selection wandered into unrelated tools."""

    @pytest.mark.asyncio
    async def test_propose_prompt_reports_rows(self):
        fake = FakeLLM(text='{"candidates": []}')
        planner = BedrockStepPlanner(fake)
        ctx = AccumulatedContext()
        ctx.rows.extend([{"base_dt": "2026-06-01", "au": 63044}, {"base_dt": "2026-06-02", "au": 61890}])
        await planner.propose_steps(sub_question=_sq(), context=ctx, tool_specs=_SPECS)
        prompt = fake.calls[-1]["prompt"]
        assert "2 structured data row(s)" in prompt
        assert "2026-06-01" in prompt

    @pytest.mark.asyncio
    async def test_assess_prompt_carries_row_content(self):
        fake = FakeLLM(text='{"sufficient": true, "answer": "ok"}')
        planner = BedrockStepPlanner(fake)
        ctx = AccumulatedContext()
        ctx.rows.append({"country": "US", "revenue": 34317184})
        await planner.assess(sub_question=_sq(), context=ctx)
        prompt = fake.calls[-1]["prompt"]
        assert "Structured data rows gathered" in prompt
        assert "34317184" in prompt

    @pytest.mark.asyncio
    async def test_assess_without_rows_unchanged(self):
        fake = FakeLLM(text='{"sufficient": false, "answer": ""}')
        planner = BedrockStepPlanner(fake)
        await planner.assess(sub_question=_sq(), context=AccumulatedContext())
        assert "Structured data rows gathered" not in fake.calls[-1]["prompt"]


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_planner_does_not_import_graphrag(self):
        """``import ...agentic.planner`` must NOT import ``graphrag_toolkit``."""
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.planner; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            f"importing planner leaked graphrag_toolkit or failed.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
