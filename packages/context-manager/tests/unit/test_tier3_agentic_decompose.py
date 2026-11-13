# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever decomposition planner with a scripted planner LLM."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from coa_serve.clients.base import ConverseResult
from coa_serve.tier3.agentic.decompose import DecompositionPlanner
from coa_serve.trace import TraceCollector


class FakePlannerClient:
    """A mocked ``LLMClient`` whose ``converse`` returns a scripted decision and records calls."""

    def __init__(
        self,
        *,
        text: str | None = None,
        result: ConverseResult | None = None,
        guardrail_blocked: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self._result = result
        self._text = text
        self._guardrail_blocked = guardrail_blocked
        self._raises = raises
        self.calls: list[dict] = []

    async def converse(self, prompt, **kwargs) -> ConverseResult:
        self.calls.append({"prompt": prompt, **kwargs})
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return ConverseResult(text=self._text or "", guardrail_blocked=self._guardrail_blocked)


def planner_returning(sub_questions: list[dict]) -> FakePlannerClient:
    """Build a planner client that returns the given ``sub_questions`` as JSON."""
    return FakePlannerClient(text=json.dumps({"sub_questions": sub_questions}))


def decompose_steps(trace: TraceCollector) -> list:
    return [s for s in trace.steps if s.step == "decompose"]


@pytest.mark.unit
class TestSingleAsk:
    async def test_single_ask_yields_one_sub_question(self):
        planner = planner_returning([{"text": "What is Amazon's revenue?", "multi_entity": False}])
        sub_questions = await DecompositionPlanner(planner).decompose("What is Amazon's revenue?")

        assert len(sub_questions) == 1
        assert sub_questions[0].text == "What is Amazon's revenue?"
        assert sub_questions[0].multi_entity is False
        assert sub_questions[0].origin == "decomposition"

    async def test_unresolved_entity_set_stays_single_but_multi_entity(self):
        """The worked-example shape: 'competitors' is unresolved, so a single
        Sub_Question flagged multi_entity rather than a static split."""
        planner = planner_returning([{"text": "Amazon competitors' financial situations", "multi_entity": True}])
        sub_questions = await DecompositionPlanner(planner).decompose(
            "Tell me about Amazon's competitors' financial situations"
        )

        assert len(sub_questions) == 1
        assert sub_questions[0].multi_entity is True
        assert sub_questions[0].origin == "decomposition"


@pytest.mark.unit
class TestMultiEntity:
    async def test_multiple_distinct_entities_yield_multiple_sub_questions(self):
        planner = planner_returning(
            [
                {"text": "What is Amazon's revenue?", "multi_entity": False},
                {"text": "What is Walmart's revenue?", "multi_entity": False},
            ]
        )
        sub_questions = await DecompositionPlanner(planner).decompose("Compare Amazon and Walmart revenue")

        assert len(sub_questions) == 2
        assert [sq.text for sq in sub_questions] == [
            "What is Amazon's revenue?",
            "What is Walmart's revenue?",
        ]
        assert all(sq.origin == "decomposition" for sq in sub_questions)

    async def test_blank_entries_are_dropped_keeping_only_usable_sub_questions(self):
        planner = planner_returning(
            [
                {"text": "  ", "multi_entity": False},
                {"text": "What is Amazon's revenue?", "multi_entity": False},
            ]
        )
        sub_questions = await DecompositionPlanner(planner).decompose("q")
        assert len(sub_questions) == 1
        assert sub_questions[0].text == "What is Amazon's revenue?"

    async def test_plain_string_entries_are_accepted(self):
        planner = FakePlannerClient(text=json.dumps({"sub_questions": ["Amazon revenue", "Walmart revenue"]}))
        sub_questions = await DecompositionPlanner(planner).decompose("compare them")
        assert [sq.text for sq in sub_questions] == ["Amazon revenue", "Walmart revenue"]


@pytest.mark.unit
class TestDecompositionRecorded:
    async def test_decomposition_is_recorded_in_trace(self):
        planner = planner_returning(
            [
                {"text": "What is Amazon's revenue?", "multi_entity": False},
                {"text": "What is Walmart's revenue?", "multi_entity": False},
            ]
        )
        trace = TraceCollector()
        await DecompositionPlanner(planner).decompose("Compare Amazon and Walmart", trace)

        steps = decompose_steps(trace)
        assert len(steps) == 1
        assert steps[0].status == "success"
        assert "2 sub-questions" in str(steps[0].detail)

    async def test_single_ask_decomposition_recorded(self):
        planner = planner_returning([{"text": "q", "multi_entity": False}])
        trace = TraceCollector()
        await DecompositionPlanner(planner).decompose("q", trace)

        steps = decompose_steps(trace)
        assert len(steps) == 1
        assert steps[0].status == "success"
        assert "single self-contained" in str(steps[0].detail)


@pytest.mark.unit
class TestDegradation:
    async def test_planner_error_falls_back_to_single_sub_question(self):
        planner = FakePlannerClient(raises=RuntimeError("bedrock down"))
        trace = TraceCollector()
        sub_questions = await DecompositionPlanner(planner).decompose("anything", trace)

        assert len(sub_questions) == 1
        assert sub_questions[0].text == "anything"
        assert sub_questions[0].multi_entity is False
        steps = decompose_steps(trace)
        assert len(steps) == 1 and steps[0].status == "degraded"

    async def test_guardrail_block_falls_back_to_single_sub_question(self):
        planner = FakePlannerClient(text="", guardrail_blocked=True)
        trace = TraceCollector()
        sub_questions = await DecompositionPlanner(planner).decompose("blocked query", trace)

        assert len(sub_questions) == 1
        assert sub_questions[0].text == "blocked query"
        assert decompose_steps(trace)[0].status == "degraded"

    async def test_unparseable_response_falls_back(self):
        planner = FakePlannerClient(text="I cannot help with that.")
        trace = TraceCollector()
        sub_questions = await DecompositionPlanner(planner).decompose("orig", trace)

        assert len(sub_questions) == 1
        assert sub_questions[0].text == "orig"
        assert decompose_steps(trace)[0].status == "degraded"

    async def test_empty_sub_question_list_falls_back(self):
        planner = planner_returning([])
        sub_questions = await DecompositionPlanner(planner).decompose("orig")
        assert len(sub_questions) == 1
        assert sub_questions[0].text == "orig"

    async def test_missing_sub_questions_key_falls_back(self):
        planner = FakePlannerClient(text=json.dumps({"other": "value"}))
        sub_questions = await DecompositionPlanner(planner).decompose("orig")
        assert len(sub_questions) == 1
        assert sub_questions[0].text == "orig"


@pytest.mark.unit
class TestJsonExtraction:
    async def test_json_wrapped_in_markdown_fences_is_parsed(self):
        body = json.dumps({"sub_questions": [{"text": "Amazon revenue", "multi_entity": False}]})
        planner = FakePlannerClient(text=f"```json\n{body}\n```")
        sub_questions = await DecompositionPlanner(planner).decompose("q")
        assert len(sub_questions) == 1
        assert sub_questions[0].text == "Amazon revenue"

    async def test_json_embedded_in_prose_is_parsed(self):
        body = json.dumps({"sub_questions": [{"text": "Amazon revenue", "multi_entity": False}]})
        planner = FakePlannerClient(text=f"Sure! Here is the plan: {body} Hope that helps.")
        sub_questions = await DecompositionPlanner(planner).decompose("q")
        assert len(sub_questions) == 1
        assert sub_questions[0].text == "Amazon revenue"


@pytest.mark.unit
class TestPlannerCallScoping:
    async def test_converse_called_with_system_instruction_and_guard_content(self):
        planner = planner_returning([{"text": "q", "multi_entity": False}])
        await DecompositionPlanner(planner, guardrail_id="gr-1").decompose("my question")

        assert len(planner.calls) == 1
        call = planner.calls[0]
        assert call["prompt"] == "my question"
        assert "query planner" in call["system"]
        assert call["guard_content"] == "Question: my question"
        assert call["guardrail_id"] == "gr-1"

    async def test_query_is_truncated_to_max_chars(self):
        planner = planner_returning([{"text": "q", "multi_entity": False}])
        long_query = "x" * 5000
        await DecompositionPlanner(planner, max_query_chars=100).decompose(long_query)
        assert len(planner.calls[0]["prompt"]) == 100


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_decompose_does_not_import_graphrag(self):
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.decompose; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            f"importing decompose leaked graphrag_toolkit or failed.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
