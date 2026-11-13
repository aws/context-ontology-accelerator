# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever reasoning controller with mocked planner, tools and clock."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections import deque

import pytest
from coa_serve.tier3.agentic import AgenticBudgetConfig, BudgetClock
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import (
    ReasoningController,
    validate_inputs,
)
from coa_serve.tier3.agentic.models import (
    PlannerDecision,
    StopReason,
    StopSignalConfig,
    SubQuestion,
)
from coa_serve.tier3.agentic.ontology import Ontology
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.trace import TraceCollector

NS = "acme"


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

    def __init__(self, chunk_id: str, text: str = "") -> None:
        self.chunk_id = chunk_id
        self.text = text
        self.uri = ""


def chunk_result(chunk_id: str, *, directions: tuple[str, ...] = (), detail: str = "ok") -> ToolResult:
    """A ToolResult carrying one (new, unless seen) chunk + optional directions."""
    return ToolResult(chunks=(_Chunk(chunk_id),), detail=detail, produced_directions=directions)


class FakeTool:
    """A mock Tool that records its invocations and is configurably degraded."""

    def __init__(
        self,
        name: str,
        *,
        input_schema: dict | None = None,
        result: ToolResult | None = None,
        raises: Exception | None = None,
        delay: float = 0.0,
        incrementing: bool = False,
    ) -> None:
        self.name = name
        self.description = f"fake tool {name}"
        self.input_schema = input_schema if input_schema is not None else {}
        self.output_schema = {}
        self._result = result
        self._raises = raises
        self._delay = delay
        self._incrementing = incrementing
        self._n = 0
        self.calls: list[dict] = []

    async def invoke(self, *, namespace: str, **kwargs) -> ToolResult:
        self.calls.append({"namespace": namespace, **kwargs})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        if self._incrementing:
            self._n += 1
            return chunk_result(f"{self.name}-{self._n}")
        return self._result if self._result is not None else chunk_result(f"{self.name}-c")


class ScriptedPlanner:
    """A mocked :class:`StepPlanner` driven by scripted candidate lists + booleans.

    ``candidates_script`` is consumed one list per ``propose_steps`` call; when it
    is exhausted, an empty list (no Tool selectable) is returned unless
    ``repeat_last`` keeps re-proposing the final scripted list. ``sufficiency`` is
    consumed one bool per ``check_sufficiency`` call, defaulting to
    ``default_sufficiency`` thereafter.
    """

    def __init__(
        self,
        candidates_script: list[list[PlannerDecision]] | None = None,
        *,
        repeat_last: bool = False,
        sufficiency: list[bool] | None = None,
        default_sufficiency: bool = False,
    ) -> None:
        self._script = deque(candidates_script or [])
        self._repeat_last = repeat_last
        self._last: list[PlannerDecision] | None = None
        self._suff = deque(sufficiency or [])
        self._default_suff = default_sufficiency
        self.propose_calls = 0
        self.sufficiency_calls = 0

    async def propose_steps(self, *, sub_question, context, tool_specs):
        self.propose_calls += 1
        if self._script:
            self._last = self._script.popleft()
            return self._last
        if self._repeat_last and self._last is not None:
            return self._last
        return []

    async def check_sufficiency(self, *, sub_question, context):
        self.sufficiency_calls += 1
        if self._suff:
            return self._suff.popleft()
        return self._default_suff


class AssessPlanner(ScriptedPlanner):
    """A :class:`ScriptedPlanner` that also implements ``assess`` with scripted
    working answers, so the controller's answer-stability path is exercised.

    ``working_answers`` is consumed one per step; sufficiency stays ``False`` so the
    loop does not stop on sufficiency and the stability signal is what ends it.
    """

    def __init__(self, candidates_script, *, working_answers: list[str], **kw) -> None:
        super().__init__(candidates_script, **kw)
        self._answers = deque(working_answers)

    async def assess(self, *, sub_question, context):
        self.sufficiency_calls += 1
        answer = self._answers.popleft() if self._answers else ""
        return False, answer


class ScriptedEmbed:
    """A deterministic fake embed client: maps each distinct text to a fixed unit
    vector, so identical answers embed identically (cosine 1.0) and different ones
    are orthogonal (cosine 0.0). Lets a test drive 'stable' vs 'moving' answers."""

    _VECTORS = {
        "A": [1.0, 0.0, 0.0],
        "B": [0.0, 1.0, 0.0],
        "C": [0.0, 0.0, 1.0],
    }

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._VECTORS.get(text, [0.0, 0.0, 0.0])


def make_clock(**cfg) -> tuple[BudgetClock, FakeClockSource]:
    """Build a started BudgetClock over a fake time source."""
    source = FakeClockSource()
    clock = BudgetClock(AgenticBudgetConfig(**cfg), clock=source)
    clock.started()
    return clock, source


def make_registry(*tools: FakeTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


async def run_loop(
    planner: ScriptedPlanner,
    registry: ToolRegistry,
    *,
    clock: BudgetClock | None = None,
    ctx: AccumulatedContext | None = None,
    per_tool_timeout_s: float = 30.0,
    sub_question: SubQuestion | None = None,
    namespace: str = NS,
    ontology_edge_mode: str = "soft_prior",
    tool_context: dict | None = None,
):
    """Drive one controller loop and return (stop_reason, ctx, queue, trace)."""
    clock = clock or make_clock()[0]
    ctx = ctx if ctx is not None else AccumulatedContext()
    queue: deque = deque()
    trace = TraceCollector()
    controller = ReasoningController(
        planner, per_tool_timeout_s=per_tool_timeout_s, ontology_edge_mode=ontology_edge_mode
    )
    sub_question = sub_question or SubQuestion(text="why")
    stop = await controller.run_one_loop(
        sub_question, ctx, registry, clock, queue, trace, namespace=namespace, tool_context=tool_context
    )
    return stop, ctx, queue, trace


def trace_steps(trace: TraceCollector) -> list[str]:
    return [s.step for s in trace.steps]


def find_step(trace: TraceCollector, step: str):
    return [s for s in trace.steps if s.step == step]


@pytest.mark.unit
class TestValidateInputs:
    def test_conforming_inputs_have_no_violations(self):
        assert validate_inputs({"query": "amazon"}, {"query": "str"}) == []

    def test_unknown_field_is_a_violation(self):
        violations = validate_inputs({"nope": 1}, {"query": "str"})
        assert len(violations) == 1 and "nope" in violations[0]

    def test_wrong_scalar_type_is_a_violation(self):
        violations = validate_inputs({"query": 123}, {"query": "str"})
        assert violations and "query" in violations[0]

    def test_list_typed_field_accepts_a_list(self):
        assert validate_inputs({"edge_types": ["a", "b"]}, {"edge_types": "list[str]"}) == []

    def test_int_field_rejects_bool(self):
        violations = validate_inputs({"limit": True}, {"limit": "int"})
        assert violations and "bool" in violations[0]

    def test_empty_schema_rejects_any_supplied_field(self):
        assert validate_inputs({"x": 1}, {}) != []
        # ...but accepts an empty input set (e.g. the ontology tool).
        assert validate_inputs({}, {}) == []


@pytest.mark.unit
class TestPreferenceSelection:
    def _controller(self) -> ReasoningController:
        return ReasoningController(ScriptedPlanner())

    def test_strategy_beats_template_and_ad_hoc(self):
        c = self._controller()
        candidates = [
            PlannerDecision(tool_name="graph_traversal", is_ad_hoc=True),
            PlannerDecision(tool_name="graph_traversal"),  # template
            PlannerDecision(tool_name="strategy:entity_based"),
        ]
        assert c._select_decision(candidates).tool_name == "strategy:entity_based"

    def test_template_beats_ad_hoc(self):
        c = self._controller()
        candidates = [
            PlannerDecision(tool_name="graph_traversal", is_ad_hoc=True),
            PlannerDecision(tool_name="graph_traversal"),
        ]
        chosen = c._select_decision(candidates)
        assert chosen.tool_name == "graph_traversal" and chosen.is_ad_hoc is False

    def test_strategy_wins_ties_over_template(self):
        c = self._controller()
        # Template listed first; strategy must still win the tie.
        candidates = [
            PlannerDecision(tool_name="graph_traversal"),
            PlannerDecision(tool_name="strategy:traversal"),
        ]
        assert c._select_decision(candidates).tool_name == "strategy:traversal"

    def test_ad_hoc_selected_only_when_sole_candidate(self):
        c = self._controller()
        only = [PlannerDecision(tool_name="graph_traversal", is_ad_hoc=True)]
        chosen = c._select_decision(only)
        assert chosen.is_ad_hoc is True

    def test_no_candidates_returns_none(self):
        assert self._controller()._select_decision([]) is None

    async def test_loop_invokes_the_preferred_tool_not_the_others(self):
        """End-to-end: with mixed candidates the strategy tool is the one invoked."""
        strat = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        graph = FakeTool("graph_traversal", input_schema={"query": "str"})
        registry = make_registry(strat, graph)
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(tool_name="graph_traversal", inputs={"query": "q"}, is_ad_hoc=True),
                    PlannerDecision(tool_name="graph_traversal", inputs={"query": "q"}),
                    PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"}),
                ]
            ],
            sufficiency=[True],
        )

        stop, ctx, _queue, _trace = await run_loop(planner, registry)

        assert stop == StopReason.sufficiency_satisfied
        assert len(strat.calls) == 1
        assert graph.calls == []


@pytest.mark.unit
class TestInputContractRejection:
    async def test_non_conforming_inputs_not_invoked_then_loop_continues(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        registry = make_registry(tool)
        planner = ScriptedPlanner(
            [
                [PlannerDecision(tool_name="strategy:entity_based", inputs={"bogus": 1})],  # rejected
                [PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "ok"})],  # valid
            ],
            sufficiency=[True],
        )

        stop, ctx, _queue, trace = await run_loop(planner, registry)

        # Only the conforming invocation reached the tool.
        assert len(tool.calls) == 1
        assert tool.calls[0]["query"] == "ok"
        # The violation was traced as an error step on the tool, and the loop
        # continued to the sufficiency stop.
        violations = [s for s in trace.steps if s.status == "error" and "input_contract_violation" in str(s.detail)]
        assert len(violations) == 1
        assert stop == StopReason.sufficiency_satisfied


@pytest.mark.unit
class TestStopConditions:
    async def test_sufficiency_stop(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        registry = make_registry(tool)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        stop, _ctx, _queue, _trace = await run_loop(planner, registry)
        assert stop == StopReason.sufficiency_satisfied
        assert len(tool.calls) == 1

    async def test_no_new_information_stop(self):
        # The loop stops after max_no_progress_steps (default 3) consecutive no-progress steps.
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"}, result=ToolResult(detail="empty"))
        registry = make_registry(tool)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            repeat_last=True,
            default_sufficiency=False,
        )
        stop, _ctx, _queue, _trace = await run_loop(planner, registry)
        assert stop == StopReason.no_new_information
        assert len(tool.calls) == 3  # escalation tolerance (default 3) exhausted

    async def test_novel_fruitless_approaches_reset_the_streak(self):
        """Each fruitless step with a NOVEL (tool+inputs)
        approach resets the no-progress streak, so a planner that keeps diversifying
        is not stopped by the counter — only the aggregate step budget bounds it.
        Contrast test_no_new_information_stop, where REPEATS exhaust the streak at 3."""
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"}, result=ToolResult(detail="empty"))
        registry = make_registry(tool)
        clock, _src = make_clock(max_steps=6)
        # Six DISTINCT fruitless queries — each novel, so the streak keeps resetting
        # and no_new_information never fires; the max_steps cap (6) stops it instead.
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": f"q{i}"})] for i in range(6)],
            default_sufficiency=False,
        )
        stop, _ctx, _queue, _trace = await run_loop(planner, registry, clock=clock)
        assert stop == StopReason.max_steps_reached
        assert len(tool.calls) == 6  # all six novel steps ran; streak never exhausted

    async def test_max_steps_stop(self):
        # Each step adds new info and sufficiency is never satisfied, so the
        # aggregate step cap terminates the loop.
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"}, incrementing=True)
        registry = make_registry(tool)
        clock, _src = make_clock(max_steps=2)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            repeat_last=True,
            default_sufficiency=False,
        )
        stop, _ctx, _queue, _trace = await run_loop(planner, registry, clock=clock)
        assert stop == StopReason.max_steps_reached
        assert len(tool.calls) == 2  # exactly max_steps invocations

    async def test_no_tool_selectable_stop(self):
        registry = make_registry(FakeTool("strategy:entity_based", input_schema={"query": "str"}))
        planner = ScriptedPlanner([])  # proposes nothing
        stop, _ctx, _queue, trace = await run_loop(planner, registry)
        assert stop is None
        assert any("no_tool_selectable" in str(s.detail) for s in find_step(trace, "next_step_select"))

    async def test_time_budget_expiry_stops_before_a_step(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        registry = make_registry(tool)
        clock, src = make_clock(time_budget_s=30)
        src.advance(30)  # budget already spent
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            repeat_last=True,
        )
        stop, _ctx, _queue, _trace = await run_loop(planner, registry, clock=clock)
        assert stop == StopReason.time_budget_expired
        assert tool.calls == []  # no step initiated past the budget


@pytest.mark.unit
class TestPerToolFailureTolerance:
    async def test_returned_degradation_keeps_context_and_marks_trace(self):
        degraded = FakeTool(
            "vector_search",
            input_schema={"query": "str"},
            result=ToolResult(
                chunks=(_Chunk("valid"),),
                detail="1 chunk retained; dropped 2 anonymous chunks",
                degraded_reason="missing_document_provenance",
            ),
        )
        registry = make_registry(degraded)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="vector_search", inputs={"query": "q"})]],
            sufficiency=[True],
        )

        stop, ctx, _queue, trace = await run_loop(planner, registry)

        assert stop == StopReason.sufficiency_satisfied
        assert [chunk.chunk_id for chunk in ctx.chunks] == ["valid"]
        assert ctx.degraded_sources == [{"source": "vector_search", "reason": "missing_document_provenance"}]
        tool_step = next(step for step in trace.steps if step.step == "vector_search")
        assert tool_step.status == "degraded"
        assert ctx.attempt_history[0]["status"] == "degraded"

    async def test_timeout_is_degraded_and_loop_continues(self):
        slow = FakeTool("strategy:slow", input_schema={"query": "str"}, delay=1.0)
        registry = make_registry(slow)
        planner = ScriptedPlanner([[PlannerDecision(tool_name="strategy:slow", inputs={"query": "q"})]])
        stop, ctx, _queue, trace = await run_loop(planner, registry, per_tool_timeout_s=0.02)

        assert ctx.degraded_sources == [{"source": "strategy:slow", "reason": "timeout"}]
        assert any(s.status == "timeout" for s in trace.steps)
        # No usable step → the loop drains to no-tool-selectable.
        assert stop is None

    async def test_error_is_degraded_and_loop_continues(self):
        boom = FakeTool("strategy:boom", input_schema={"query": "str"}, raises=RuntimeError("x"))
        registry = make_registry(boom)
        planner = ScriptedPlanner([[PlannerDecision(tool_name="strategy:boom", inputs={"query": "q"})]])
        stop, ctx, _queue, trace = await run_loop(planner, registry)

        assert ctx.degraded_sources == [{"source": "strategy:boom", "reason": "error"}]
        assert any(s.status == "error" and s.step == "strategy:boom" for s in trace.steps)
        assert stop is None

    async def test_timeout_then_recovery_reaches_sufficiency(self):
        slow = FakeTool("strategy:slow", input_schema={"query": "str"}, delay=1.0)
        good = FakeTool("strategy:good", input_schema={"query": "str"})
        registry = make_registry(slow, good)
        planner = ScriptedPlanner(
            [
                [PlannerDecision(tool_name="strategy:slow", inputs={"query": "q"})],
                [PlannerDecision(tool_name="strategy:good", inputs={"query": "q"})],
            ],
            sufficiency=[True],
        )
        stop, ctx, _queue, _trace = await run_loop(planner, registry, per_tool_timeout_s=0.02)

        assert stop == StopReason.sufficiency_satisfied
        assert len(good.calls) == 1
        assert {"source": "strategy:slow", "reason": "timeout"} in ctx.degraded_sources


@pytest.mark.unit
class TestAdHocHandling:
    async def test_ad_hoc_generation_is_traced(self):
        tool = FakeTool("graph_traversal", input_schema={"query": "str"})
        registry = make_registry(tool)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="graph_traversal", inputs={"query": "q"}, is_ad_hoc=True)]],
            sufficiency=[True],
        )
        stop, _ctx, _queue, trace = await run_loop(planner, registry)

        assert stop == StopReason.sufficiency_satisfied
        ad_hoc = find_step(trace, "ad_hoc_query")
        assert len(ad_hoc) == 1
        assert "no Retrieval_Strategy_Tool" in str(ad_hoc[0].detail)

    async def test_ad_hoc_execution_failure_retains_no_results(self):
        boom = FakeTool("graph_traversal", input_schema={"query": "str"}, raises=RuntimeError("bad cypher"))
        registry = make_registry(boom)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="graph_traversal", inputs={"query": "q"}, is_ad_hoc=True)]]
        )
        stop, ctx, _queue, trace = await run_loop(planner, registry)

        assert ctx.degraded_sources == [{"source": "graph_traversal", "reason": "error"}]
        assert ctx.chunks == []  # no partial results retained
        failure = [s for s in trace.steps if "ad-hoc query execution" in str(s.detail)]
        assert len(failure) == 1
        assert stop is None


@pytest.mark.unit
class TestOntologyEdgeFiltering:
    async def test_absent_edge_types_excluded_before_invocation(self):
        graph = FakeTool(
            "graph_traversal",
            input_schema={
                "mode": "str",
                "entity_ids": "list[str]",
                "edge_types": "list[str]",
            },
        )
        registry = make_registry(graph)
        ctx = AccumulatedContext()
        ctx.ontology = Ontology(
            node_types=("Company",),
            edge_types=("COMPETES_WITH", "HAS_FINANCIALS"),
            edge_map=(),
        )
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="graph_traversal",
                        inputs={
                            "mode": "edge_typed",
                            "entity_ids": ["e1"],
                            "edge_types": ["COMPETES_WITH", "NOT_IN_ONTOLOGY", "HAS_FINANCIALS"],
                        },
                    )
                ]
            ],
            sufficiency=[True],
        )

        stop, _ctx, _queue, trace = await run_loop(planner, registry, ctx=ctx, ontology_edge_mode="strict")

        assert stop == StopReason.sufficiency_satisfied
        # strict mode: the absent edge type was excluded; only ontology edges passed.
        assert graph.calls[0]["edge_types"] == ["COMPETES_WITH", "HAS_FINANCIALS"]
        filt = find_step(trace, "ontology_edge_filter")
        assert len(filt) == 1 and "NOT_IN_ONTOLOGY" in str(filt[0].detail)

    async def test_soft_prior_keeps_off_ontology_edges_reordered(self):
        """Default soft_prior: NOTHING is dropped — ontology edges are ordered
        first, off-ontology edges (the majority of the graph's real predicates) are
        retained so the traversal isn't starved."""
        graph = FakeTool(
            "graph_traversal",
            input_schema={"mode": "str", "entity_ids": "list[str]", "edge_types": "list[str]"},
        )
        registry = make_registry(graph)
        ctx = AccumulatedContext()
        ctx.ontology = Ontology(node_types=("Company",), edge_types=("COMPETES_WITH", "HAS_FINANCIALS"), edge_map=())
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="graph_traversal",
                        inputs={
                            "mode": "edge_typed",
                            "entity_ids": ["e1"],
                            # off-ontology 'HAS_SEGMENT' first, then two ontology edges.
                            "edge_types": ["HAS_SEGMENT", "COMPETES_WITH", "HAS_FINANCIALS"],
                        },
                    )
                ]
            ],
            sufficiency=[True],
        )

        stop, _ctx, _queue, trace = await run_loop(planner, registry, ctx=ctx)  # default soft_prior

        assert stop == StopReason.sufficiency_satisfied
        # All three retained; ontology edges reordered to the front, off-ontology kept.
        assert graph.calls[0]["edge_types"] == ["COMPETES_WITH", "HAS_FINANCIALS", "HAS_SEGMENT"]
        filt = find_step(trace, "ontology_edge_filter")
        assert len(filt) == 1 and "HAS_SEGMENT" in str(filt[0].detail)

    async def test_no_filtering_when_all_edge_types_in_ontology(self):
        graph = FakeTool(
            "graph_traversal",
            input_schema={"mode": "str", "entity_ids": "list[str]", "edge_types": "list[str]"},
        )
        registry = make_registry(graph)
        ctx = AccumulatedContext()
        ctx.ontology = Ontology(node_types=(), edge_types=("COMPETES_WITH",), edge_map=())
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="graph_traversal",
                        inputs={"mode": "edge_typed", "entity_ids": ["e1"], "edge_types": ["COMPETES_WITH"]},
                    )
                ]
            ],
            sufficiency=[True],
        )
        _stop, _ctx, _queue, trace = await run_loop(planner, registry, ctx=ctx)
        assert graph.calls[0]["edge_types"] == ["COMPETES_WITH"]
        assert find_step(trace, "ontology_edge_filter") == []

    async def test_no_filtering_when_ontology_not_yet_resolved(self):
        graph = FakeTool(
            "graph_traversal",
            input_schema={"mode": "str", "entity_ids": "list[str]", "edge_types": "list[str]"},
        )
        registry = make_registry(graph)
        ctx = AccumulatedContext()  # ontology is None
        planner = ScriptedPlanner(
            [
                [
                    PlannerDecision(
                        tool_name="graph_traversal",
                        inputs={"mode": "edge_typed", "entity_ids": ["e1"], "edge_types": ["ANYTHING"]},
                    )
                ]
            ],
            sufficiency=[True],
        )
        _stop, _ctx, _queue, _trace = await run_loop(planner, registry, ctx=ctx)
        # With no ontology resolved the controller does not filter.
        assert graph.calls[0]["edge_types"] == ["ANYTHING"]


@pytest.mark.unit
class TestPresentEdgeDiscovery:
    async def test_discovery_caches_edges_and_counts_as_progress(self):
        """An edge_types-mode probe gathers no chunks (new_count==0) but learning
        present predicates is PROGRESS: it caches them on ctx and does NOT trip the
        no-progress stop after a single step."""
        graph = FakeTool(
            "graph_traversal",
            input_schema={"mode": "str", "entity_ids": "list[str]"},
            result=ToolResult(
                items=(
                    {"type": "edge_type", "edge": "HAS SEGMENT", "freq": 9},
                    {"type": "edge_type", "edge": "SELLS", "freq": 4},
                ),
                detail="graph_traversal[edge_types]: 2 present predicates",
            ),
        )
        registry = make_registry(graph)
        # max_no_progress_steps=1 would stop immediately on a true no-progress step;
        # discovery must NOT be treated as one. The scripted planner proposes the
        # probe once, then nothing (empty) → loop ends with no_tool_selectable, not
        # no_new_information.
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="graph_traversal", inputs={"mode": "edge_types", "entity_ids": ["e1"]})], []],
            sufficiency=[False],
        )
        ctx = AccumulatedContext()

        stop, out_ctx, _queue, trace = await run_loop(planner, registry, ctx=ctx)

        # Discovered predicates cached for the planner's next step...
        assert out_ctx.present_edge_types == ["HAS SEGMENT", "SELLS"]
        # ...and the step was recorded as progress (a discovery trace step exists,
        # and the loop did NOT stop with no_new_information).
        assert find_step(trace, "present_edges_discovered")
        assert stop != StopReason.no_new_information


@pytest.mark.unit
class TestBranchEnqueue:
    async def test_branches_enqueued_up_to_the_fanout_cap(self):
        tool = FakeTool(
            "graph_traversal",
            input_schema={"query": "str"},
            result=chunk_result("c1", directions=("Walmart", "Target", "Costco")),
        )
        registry = make_registry(tool)
        clock, _src = make_clock(max_fanout=2)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="graph_traversal", inputs={"query": "q"})]],
            sufficiency=[True],
        )

        stop, _ctx, queue, trace = await run_loop(planner, registry, clock=clock)

        assert stop == StopReason.sufficiency_satisfied
        # Only fan-out-cap (2) branches opened despite 3 surfaced directions.
        assert len(queue) == 2
        assert clock.branches_opened == 2
        seeds = [sq.text for sq in queue]
        assert seeds == ["Walmart", "Target"]
        # Each enqueued item is a branch-origin SubQuestion.
        for sq in queue:
            assert isinstance(sq, SubQuestion)
            assert sq.origin == "branch"
        # The cap-reached case is traced as a skipped branch.
        assert any(s.step == "exploration_branch" and s.status == "skipped" for s in trace.steps)

    async def test_no_branches_when_no_directions_surfaced(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"}, result=chunk_result("c1"))
        registry = make_registry(tool)
        clock, _src = make_clock(max_fanout=5)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        _stop, _ctx, queue, _trace = await run_loop(planner, registry, clock=clock)
        assert len(queue) == 0
        assert clock.branches_opened == 0


@pytest.mark.unit
class TestNamespaceScoping:
    async def test_tool_invoked_with_request_namespace(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        registry = make_registry(tool)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            sufficiency=[True],
        )
        await run_loop(planner, registry, namespace="tenant-z")
        assert tool.calls[0]["namespace"] == "tenant-z"


@pytest.mark.unit
class TestUnknownTool:
    async def test_unknown_tool_is_skipped_and_loop_continues(self):
        good = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        registry = make_registry(good)
        planner = ScriptedPlanner(
            [
                [PlannerDecision(tool_name="strategy:does_not_exist", inputs={"query": "q"})],
                [PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})],
            ],
            sufficiency=[True],
        )
        stop, _ctx, _queue, trace = await run_loop(planner, registry)
        assert stop == StopReason.sufficiency_satisfied
        assert len(good.calls) == 1
        assert any("unknown_tool" in str(s.detail) for s in trace.steps)


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_controller_does_not_import_graphrag(self):
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.controller; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            f"importing controller leaked graphrag_toolkit or failed.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout


FIXES = {"options": {"loopFixes": True}}


@pytest.mark.unit
class TestLoopFixes:
    async def test_unknown_fields_rejected_without_the_option(self):
        tool = FakeTool("nl_to_sql", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="nl_to_sql", inputs={"query": "q", "top_k": 5})]], sufficiency=[True]
        )
        await run_loop(planner, make_registry(tool))
        assert tool.calls == []

    async def test_unknown_fields_dropped_with_the_option(self):
        tool = FakeTool("nl_to_sql", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="nl_to_sql", inputs={"query": "q", "top_k": 5, "entity_class": "X"})]],
            sufficiency=[True],
        )
        stop, _ctx, _q, trace = await run_loop(planner, make_registry(tool), tool_context=FIXES)
        assert len(tool.calls) == 1 and "top_k" not in tool.calls[0] and tool.calls[0]["query"] == "q"
        assert "entity_class" in str(find_step(trace, "unknown_fields_dropped")[0].detail)
        assert stop == StopReason.sufficiency_satisfied

    async def test_type_mismatch_still_rejected_with_the_option(self):
        tool = FakeTool("nl_to_sql", input_schema={"query": "str"})
        planner = ScriptedPlanner([[PlannerDecision(tool_name="nl_to_sql", inputs={"query": 3, "x": 1})]])
        await run_loop(planner, make_registry(tool), tool_context=FIXES)
        assert tool.calls == []

    async def test_tool_not_started_when_budget_spent(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        clock, src = make_clock(time_budget_s=60, synthesis_reserve_s=5)
        src.advance(54)  # 6 s left, 1 s usable after the reserve
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]], repeat_last=True
        )
        stop, _ctx, _q, trace = await run_loop(planner, make_registry(tool), clock=clock, tool_context=FIXES)
        assert tool.calls == [] and stop == StopReason.time_budget_expired
        assert find_step(trace, "budget_spent")
        # No planner call is made either, and the step is given back.
        assert planner.propose_calls == 0 and clock.steps_consumed == 0

    async def test_planner_called_without_the_option_when_budget_spent(self):
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        clock, src = make_clock(time_budget_s=60, synthesis_reserve_s=5)
        src.advance(54)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]], sufficiency=[True]
        )
        await run_loop(planner, make_registry(tool), clock=clock)
        assert planner.propose_calls == 1

    async def test_small_per_tool_cap_does_not_stop_the_loop(self):
        # A per-tool cap below the 2 s floor must not read as "no time left".
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"})
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]], sufficiency=[True]
        )
        stop, _ctx, _q, trace = await run_loop(planner, make_registry(tool), per_tool_timeout_s=1.0, tool_context=FIXES)
        assert len(tool.calls) == 1 and not find_step(trace, "budget_spent")
        assert stop == StopReason.sufficiency_satisfied

    async def test_repeated_read_gets_its_step_back(self):
        tool = FakeTool("read_document", input_schema={"source_key": "str"}, result=chunk_result("doc:a@0"))
        clock, _src = make_clock(max_steps=3)
        planner = ScriptedPlanner(
            [[PlannerDecision(tool_name="read_document", inputs={"source_key": "a"})]], repeat_last=True
        )
        stop, ctx, _q, _trace = await run_loop(planner, make_registry(tool), clock=clock, tool_context=FIXES)
        # The first read is new; the next two repeats get their steps back (the
        # refund cap), the third does not; the no-progress streak then ends the loop.
        assert len(tool.calls) == 4
        assert clock.steps_consumed == 2
        assert any(a.get("status") == "already_read" for a in ctx.attempt_history)
        assert stop == StopReason.no_new_information


@pytest.mark.unit
class TestAnswerStability:
    async def _run(self, *, working_answers, threshold, stability_steps=2, max_steps=10):
        """Drive a loop where every step makes RETRIEVAL progress (a new chunk) but
        the working answer follows ``working_answers``. Returns (stop, trace, embed)."""
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"}, incrementing=True)
        registry = make_registry(tool)
        planner = AssessPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            repeat_last=True,
            working_answers=working_answers,
        )
        embed = ScriptedEmbed()
        controller = ReasoningController(
            planner,
            embed_fn=embed,
            stop_signals=StopSignalConfig(similarity_threshold=threshold, stability_steps=stability_steps),
        )
        clock = make_clock(max_steps=max_steps)[0]
        trace = TraceCollector()
        stop = await controller.run_one_loop(
            SubQuestion(text="why"), AccumulatedContext(), registry, clock, deque(), trace, namespace=NS
        )
        return stop, trace, embed

    async def test_stable_answer_counts_as_no_progress_and_stops(self):
        """When the working answer stays the SAME across steps (cosine 1.0 >= 0.9),
        the loop stops with no_new_information even though each step gathered a new
        chunk — the real redundant-retrieval failure the new_count==0 guard misses."""
        stop, trace, embed = await self._run(working_answers=["A", "A", "A", "A"], threshold=0.9)
        assert stop == StopReason.no_new_information
        # Window 2 (stability_steps): baseline on step 1, stable on steps 2 and 3.
        assert embed.calls == ["A", "A", "A"]
        # The stability signal was observed and recorded.
        assert find_step(trace, "answer_stability")

    async def test_moving_answer_does_not_trip_stability(self):
        """A working answer that changes each step (cosine 0.0 < 0.9) never counts
        as no-progress, so the loop runs to the step ceiling instead of stopping."""
        stop, _trace, _ = await self._run(working_answers=["A", "B", "C", "A", "B", "C"], threshold=0.9, max_steps=4)
        assert stop == StopReason.max_steps_reached

    async def test_threshold_zero_disables_the_signal(self):
        """With threshold 0.0 (default) and telemetry off, a perfectly stable answer
        does NOT stop the loop, and no embed runs at all."""
        stop, trace, embed = await self._run(working_answers=["A", "A", "A", "A"], threshold=0.0, max_steps=4)
        assert stop == StopReason.max_steps_reached
        assert not find_step(trace, "answer_stability")
        assert embed.calls == []

    async def test_no_embed_fn_means_no_stability_calls(self):
        """Without an embed function the stability path is entirely skipped."""
        tool = FakeTool("strategy:entity_based", input_schema={"query": "str"}, incrementing=True)
        registry = make_registry(tool)
        planner = AssessPlanner(
            [[PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]],
            repeat_last=True,
            working_answers=["A", "A", "A"],
        )
        controller = ReasoningController(
            planner, embed_fn=None, stop_signals=StopSignalConfig(similarity_threshold=0.9)
        )
        clock = make_clock(max_steps=3)[0]
        trace = TraceCollector()
        stop = await controller.run_one_loop(
            SubQuestion(text="why"), AccumulatedContext(), registry, clock, deque(), trace, namespace=NS
        )
        assert stop == StopReason.max_steps_reached
        assert not find_step(trace, "answer_stability")
