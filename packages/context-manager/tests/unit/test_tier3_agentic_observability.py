# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Observability / tracing tests for the Agentic Retriever session (task 14).

These tests exercise the FULL trace across a complete reasoning session using the
*real* :class:`~coa_serve.tier3.agentic.retriever.AgenticRetriever`
and the *real*
:class:`~coa_serve.tier3.agentic.controller.ReasoningController`
(only the planner, tools, decomposer, synthesizer, and the wall clock are
deterministic doubles). Where the other agentic suites verify each component's
trace steps in isolation (``test_tier3_agentic_controller`` /
``test_tier3_agentic_retriever`` / ``test_tier3_agentic_decompose``), this suite
asserts that the complete Requirement-10 step set is recorded in the right
ORDER across the whole session, that the steps survive an early termination, and
that every step flows through the ``on_record`` progressive callback:

- The expected ordered trace (Req 10.1-10.4): ``agentic_session_start`` →
  ``decompose`` → ``subquestion_start`` → ``next_step_select`` (with the chosen
  tool + selection reason and ``tool_used``) → the per-tool invocation step (with
  ``tool_used``) → ``exploration_branch`` → the branch's
  ``subquestion_start`` / ``next_step_select`` → the terminal
  ``agentic_retrieval_stop`` (stop reason + elapsed ms) → ``llm_synthesize``.
- Steps survive early termination (Req 10.4, 10.6): when the wall-clock
  Time_Budget expires mid-session, every step recorded up to that point is still
  attached to the ``Tier3Result`` in invocation order, and unprocessed
  Sub_Questions never record a ``subquestion_start``.
- The trace flows through the ``on_record`` callback (Req 10 / progressive
  WebSocket emission): every recorded step fires the async callback exactly once,
  in order.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic import retriever as retriever_module
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.models import (
    PlannerDecision,
    StopReason,
    SubQuestion,
)
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.trace import TraceCollector

NS = "acme"


# ── deterministic test doubles ─────────────────────────────────────


class FakeClockSource:
    """A manually advanced monotonic time source (seconds) for the BudgetClock."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


class _Chunk:
    """Minimal chunk stand-in keyed by ``chunk_id`` (matches AccumulatedContext)."""

    def __init__(self, chunk_id: str) -> None:
        self.chunk_id = chunk_id
        self.text = f"text-{chunk_id}"
        self.source_doc = "doc.pdf"
        self.label = "L"
        self.relevance_score = 0.9
        self.uri = ""


class FakeTool:
    """A mock Tool returning a configured ToolResult; records its invocations.

    Implements the four-field uniform contract so it registers cleanly. An
    optional ``on_invoke`` hook fires inside ``invoke`` (used to advance the fake
    clock so a real-controller loop can be driven past its Time_Budget).
    """

    def __init__(
        self,
        name: str,
        *,
        input_schema: dict | None = None,
        result: ToolResult | None = None,
        on_invoke=None,
    ) -> None:
        self.name = name
        self.description = f"fake tool {name}"
        self.input_schema = input_schema if input_schema is not None else {}
        self.output_schema = {}
        self._result = result
        self._on_invoke = on_invoke
        self.calls: list[dict] = []

    async def invoke(self, *, namespace: str, **kwargs) -> ToolResult:
        self.calls.append({"namespace": namespace, **kwargs})
        if self._on_invoke is not None:
            self._on_invoke()
        if self._result is not None:
            return self._result
        return ToolResult(chunks=(_Chunk(f"{self.name}-c"),), detail="ok")


class ScriptedPlanner:
    """A :class:`StepPlanner` double driven by scripted candidate lists + booleans.

    ``candidates_script`` is consumed one list per ``propose_steps`` call; once
    exhausted an empty list (no Tool selectable, Req 4.7) is returned.
    ``sufficiency`` is consumed one bool per ``check_sufficiency`` call, defaulting
    to ``default_sufficiency`` thereafter.
    """

    def __init__(
        self,
        candidates_script: list[list[PlannerDecision]] | None = None,
        *,
        sufficiency: list[bool] | None = None,
        default_sufficiency: bool = False,
    ) -> None:
        from collections import deque

        self._script = deque(candidates_script or [])
        self._suff = deque(sufficiency or [])
        self._default_suff = default_sufficiency

    async def propose_steps(self, *, sub_question, context, tool_specs):
        if self._script:
            return self._script.popleft()
        return []

    async def check_sufficiency(self, *, sub_question, context):
        if self._suff:
            return self._suff.popleft()
        return self._default_suff


class FakeDecomposer:
    """A DecompositionPlanner double returning scripted Sub_Questions.

    Records the ``decompose`` trace step itself (as the real planner does,
    Req 13.10) so the session-level ordered trace includes it.
    """

    def __init__(self, sub_questions: list[SubQuestion]) -> None:
        self._sub_questions = sub_questions

    async def decompose(self, query, trace=None):
        if trace is not None:
            trace.record("decompose", "success", 0, detail=f"{len(self._sub_questions)} sub-questions")
        return list(self._sub_questions)


class FakeSynthesizer:
    """A Synthesizer double; ``synthesize`` returns a scripted result."""

    def __init__(self, result: SynthesisResult | None = None) -> None:
        self._result = result or SynthesisResult(answer="final answer", confidence=0.8, duration_ms=7)
        self.calls = 0

    async def synthesize(
        self,
        query,
        chunks,
        entities,
        *,
        structured_data=None,
        catalog_summary=None,
        conversation_history=None,
        working_answer=None,
    ):
        self.calls += 1
        return self._result


def make_retriever(
    *,
    decomposer: FakeDecomposer,
    planner: ScriptedPlanner,
    tools: list[FakeTool],
    synthesizer: FakeSynthesizer,
    clock_source: FakeClockSource,
    budget: AgenticBudgetConfig | None = None,
    per_tool_timeout_s: float = 30.0,
) -> AgenticRetriever:
    """Assemble a real AgenticRetriever over a real controller + real registry."""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    controller = ReasoningController(planner, per_tool_timeout_s=per_tool_timeout_s)
    return AgenticRetriever(
        registry=registry,
        controller=controller,
        decomposer=decomposer,
        synthesizer=synthesizer,
        budget=budget or AgenticBudgetConfig(),
        clock_source=clock_source,
        # These tests assert the PLANNER-DRIVEN trace shape step-for-step; the
        # guaranteed final semantic search is a separate post-loop step verified in
        # test_tier3_agentic_retriever.TestFinalSemanticSearch, so keep it off here.
        final_semantic_search=False,
    )


def step_names(steps) -> list[str]:
    return [s.step for s in steps]


# ── expected ordered trace (Req 10.1-10.4) ─────────────────────────


@pytest.mark.unit
class TestOrderedTrace:
    async def test_scripted_session_produces_expected_ordered_trace(self):
        # One sub-question whose single tool step surfaces one Exploration_Branch;
        # the branch then finds no selectable tool and the queue drains. This
        # single scripted session exercises EVERY Requirement-10 step type.
        tool = FakeTool(
            "strategy:entity_based",
            input_schema={"query": "str"},
            result=ToolResult(chunks=(_Chunk("c1"),), detail="found 1 chunk", produced_directions=("Walmart",)),
        )
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="strategy:entity_based",
                        inputs={"query": "anchor"},
                        reasoning="best entity-anchored strategy for the ask",
                    )
                ],
                # Second propose_steps call (the branch loop) → nothing selectable.
            ],
            sufficiency=[True],  # root sub-question is satisfied after its one step
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root question")]),
            planner=planner,
            tools=[tool],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        result = await retriever.resolve("root question", NS, trace=trace)

        # The exact ordered step sequence the session emits.
        assert step_names(trace.steps) == [
            "agentic_session_start",
            "decompose",
            "answer_language",  # resolved once, before the loop
            "subquestion_start",  # root
            "next_step_select",  # root: chose the strategy tool
            "strategy:entity_based",  # the per-tool invocation
            "exploration_branch",  # surfaced "Walmart"
            "subquestion_start",  # the branch
            "next_step_select",  # branch: no tool selectable
            "agentic_retrieval_stop",  # terminal
            "llm_synthesize",  # reused final synthesis
        ]

        # The result carries the SAME ordered steps (Req 10.4): attached in
        # invocation order.
        assert [d["step"] for d in result.trace_steps] == step_names(trace.steps)

    async def test_tool_invocation_step_carries_tool_used_and_duration(self):
        # Req 10.1: a Tool invocation step records the tool used, a status, and a
        # duration in milliseconds.
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[tool],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        await retriever.resolve("root", NS, trace=trace)

        tool_steps = [s for s in trace.steps if s.step == "strategy:entity_based"]
        assert len(tool_steps) == 1
        assert tool_steps[0].tool_used == "strategy:entity_based"
        assert tool_steps[0].status == "succeeded"
        assert isinstance(tool_steps[0].duration_ms, int)

    async def test_next_step_select_records_chosen_tool_and_reason(self):
        # Req 10.2: the next-step selection records the selected tool and the
        # reason for the selection.
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="strategy:entity_based",
                        inputs={"query": "q"},
                        reasoning="entity anchoring fits the question",
                    )
                ]
            ],
            sufficiency=[True],
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[tool],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        await retriever.resolve("root", NS, trace=trace)

        selects = [s for s in trace.steps if s.step == "next_step_select" and s.tool_used]
        assert len(selects) == 1
        assert selects[0].tool_used == "strategy:entity_based"
        assert "entity anchoring fits the question" in str(selects[0].detail)

    async def test_terminal_stop_step_records_reason_and_elapsed_ms(self):
        # Req 10.3: the terminal step records the stop reason and total elapsed ms.
        src = FakeClockSource()
        tool = FakeTool(
            "strategy:entity_based",
            input_schema={"query": "str"},
            on_invoke=lambda: src.advance(2.0),  # 2s of "work"
        )
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[tool],
            synthesizer=FakeSynthesizer(),
            clock_source=src,
        )
        trace = TraceCollector()

        await retriever.resolve("root", NS, trace=trace)

        stops = [s for s in trace.steps if s.step == "agentic_retrieval_stop"]
        assert len(stops) == 1
        assert str(StopReason.sufficiency_satisfied) in str(stops[0].detail)
        # Elapsed time recorded in ms; the 2s advance during the tool invoke shows.
        assert stops[0].duration_ms == 2000

    async def test_terminal_stop_precedes_synthesis_which_is_last(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[tool],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector()

        await retriever.resolve("root", NS, trace=trace)

        names = step_names(trace.steps)
        assert names[-1] == "llm_synthesize"
        assert names[-2] == "agentic_retrieval_stop"


# ── steps survive early termination (Req 10.4, 10.6) ───────────────


@pytest.mark.unit
class TestStepsSurviveEarlyTermination:
    async def test_time_budget_expiry_attaches_all_prior_steps_ordered(self):
        # The first sub-question's tool invocation blows the 30s Time_Budget; the
        # session terminates before the second sub-question is ever started, yet
        # every step recorded up to termination must be attached to the result,
        # in invocation order (Req 10.6 / 10.4).
        src = FakeClockSource()
        tool = FakeTool(
            "strategy:entity_based",
            input_schema={"query": "str"},
            on_invoke=lambda: src.advance(99.0),  # blow the 30s budget mid-loop
        )
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            default_sufficiency=False,  # force the loop back to the expiry check
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="first"), SubQuestion(text="second")]),
            planner=planner,
            tools=[tool],
            synthesizer=FakeSynthesizer(),
            clock_source=src,
            budget=AgenticBudgetConfig(time_budget_s=30),
        )
        trace = TraceCollector()

        result = await retriever.resolve("q", NS, trace=trace)

        names = step_names(trace.steps)
        # The early-termination path still ran the first sub-question's steps...
        assert names[:6] == [
            "agentic_session_start",
            "decompose",
            "answer_language",
            "subquestion_start",
            "next_step_select",
            "strategy:entity_based",
        ]
        # ...recorded the terminal stop with the time-budget reason...
        stops = [s for s in trace.steps if s.step == "agentic_retrieval_stop"]
        assert len(stops) == 1
        assert str(StopReason.time_budget_expired) in str(stops[0].detail)
        # ...and never started the second (unprocessed) sub-question.
        assert names.count("subquestion_start") == 1
        # The partial result attaches exactly those steps, in the same order.
        assert [d["step"] for d in result.trace_steps] == names
        assert result.partial is True

    async def test_synthesis_failure_retains_all_prior_steps(self):
        # An early-ish terminal path (synthesis failure) must still retain every
        # previously recorded trace step on the result (Req 10.5/10.6 retention).
        class _BoomSynth:
            async def synthesize(self, *a, **k):
                raise RuntimeError("bedrock down")

        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        registry = ToolRegistry()
        registry.register(tool)
        retriever = AgenticRetriever(
            registry=registry,
            controller=ReasoningController(planner),
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            synthesizer=_BoomSynth(),
            budget=AgenticBudgetConfig(),
            clock_source=FakeClockSource(),
            final_semantic_search=False,
        )
        trace = TraceCollector()

        result = await retriever.resolve("root", NS, trace=trace)

        names = [d["step"] for d in result.trace_steps]
        # The tool/select steps recorded before the failed synthesis survive.
        assert "subquestion_start" in names
        assert "next_step_select" in names
        assert "strategy:entity_based" in names
        assert "agentic_retrieval_stop" in names
        # The synthesis failure is itself recorded as an error step (Req 8.6).
        synth = [s for s in trace.steps if s.step == "llm_synthesize"]
        assert len(synth) == 1 and synth[0].status == "error"
        assert result.partial is True

    async def test_synthesis_timeout_falls_back_to_working_answer(self):
        """A synthesis call that overruns the wall-clock must degrade to the loop's
        grounded working answer, not an empty string.

        Synthesis used to be a bare ``await`` — ``synthesis_reserve_s`` only shrank
        TOOL timeouts, so a slow LLM ran unbounded past the Time_Budget and pushed
        total wall time into the AgentCore invocation ceiling (~131s), where the
        endpoint returns HTTP 200 with an empty envelope and the answer is lost.
        Measured on the SEC-10-Q eval before the fix: p50 96.9s / p90 117.2s /
        max 131.8s, with the 131.8s query returning empty.
        """

        class _HangingSynth:
            async def synthesize(self, *a, **k):
                await asyncio.sleep(60)  # never completes inside the bounded window

        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        registry = ToolRegistry()
        registry.register(tool)
        retriever = AgenticRetriever(
            registry=registry,
            controller=ReasoningController(planner),
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            synthesizer=_HangingSynth(),
            budget=AgenticBudgetConfig(),
            clock_source=FakeClockSource(),
            final_semantic_search=False,
        )
        # Shrink the bound so the test does not wait on the real 20s floor.
        with patch.object(retriever_module, "_MIN_SYNTHESIS_TIMEOUT_S", 0.05):
            trace = TraceCollector()
            result = await retriever.resolve("root", NS, trace=trace)

        synth = [s for s in trace.steps if s.step == "llm_synthesize"]
        assert len(synth) == 1 and synth[0].status == "error"
        assert synth[0].detail["error"] == "TimeoutError"
        assert result.partial is True
        # Prior steps are retained even on the timeout path.
        assert "strategy:entity_based" in [d["step"] for d in result.trace_steps]


# ── trace flows through the on_record callback (Req 10) ────────────


@pytest.mark.unit
class TestOnRecordCallback:
    async def test_every_session_step_fires_the_on_record_callback_in_order(self):
        received: list[str] = []

        async def on_record(ts):
            received.append(ts.step)

        tool = FakeTool(
            "strategy:entity_based",
            input_schema={"query": "str"},
            result=ToolResult(chunks=(_Chunk("c1"),), detail="ok", produced_directions=("Walmart",)),
        )
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        retriever = make_retriever(
            decomposer=FakeDecomposer([SubQuestion(text="root")]),
            planner=planner,
            tools=[tool],
            synthesizer=FakeSynthesizer(),
            clock_source=FakeClockSource(),
        )
        trace = TraceCollector(on_record=on_record)

        await retriever.resolve("root", NS, trace=trace)
        # Drain the progressive-emission callback tasks (as the WebSocket handler
        # does before a terminal event).
        await trace.flush()

        # The callback fired once per recorded step, in invocation order — the
        # exact progressive stream a WebSocket client would receive.
        assert received == step_names(trace.steps)
        # And the full Requirement-10 step set was delivered.
        assert "agentic_session_start" in received
        assert "decompose" in received
        assert "subquestion_start" in received
        assert "next_step_select" in received
        assert "strategy:entity_based" in received
        assert "exploration_branch" in received
        assert "agentic_retrieval_stop" in received
        assert "llm_synthesize" in received
