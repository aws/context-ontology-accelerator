# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deep-reasoning ``sufficiencyGuards`` option (off by default).

Covers the partial-read guard in ``insufficiency_reason`` (a top name-matched
document read only in part), the bounded hold-back in the controller loop, the
retriever wiring (the decomposition call is unchanged), and the request validator.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest
from coa_serve.models import InvokeRequest
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.budget import BudgetClock
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController, insufficiency_reason
from coa_serve.tier3.agentic.models import PlannerDecision, StopReason, SubQuestion
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.tier3.vector_retriever import ChunkResult
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit

NS = "acme"


def _window(key: str, start: int, end: int, total: int) -> ChunkResult:
    return ChunkResult(
        chunk_id=f"doc:{key}@{start}",
        text=f"[Source document {key}: characters {start}-{end} of {total}]\nbody",
        source_doc=key,
        relevance_score=1.0,
    )


def _ctx(**kw) -> AccumulatedContext:
    ctx = AccumulatedContext()
    ctx.sufficiency_guards = True
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


class TestInsufficiencyReason:
    def test_off_without_the_option(self):
        ctx = AccumulatedContext(document_matches=[{"source_key": "docs/a.txt"}])
        ctx.chunks.append(_window("docs/a.txt", 0, 12000, 30000))
        assert insufficiency_reason(ctx) == ""

    def test_no_sql_rows_alone_does_not_hold_back(self):
        assert insufficiency_reason(_ctx()) == ""

    def test_partly_read_top_match_is_held_back(self):
        ctx = _ctx(document_matches=[{"source_key": "docs/a.txt"}])
        ctx.chunks.append(_window("docs/a.txt", 0, 12000, 30000))
        reason = insufficiency_reason(ctx)
        assert "docs/a.txt" in reason and "offset 12000" in reason

    def test_fully_read_or_unmatched_document_passes(self):
        ctx = _ctx(document_matches=[{"source_key": "docs/a.txt"}])
        ctx.chunks.extend([_window("docs/a.txt", 0, 12000, 20000), _window("docs/a.txt", 12000, 20000, 20000)])
        ctx.chunks.append(_window("docs/other.txt", 0, 100, 9000))
        assert insufficiency_reason(ctx) == ""

    def test_fork_keeps_the_flag(self):
        assert _ctx().fork().sufficiency_guards is True


# ── controller loop: bounded hold-back ─────────────────────────────


class _AlwaysSufficient:
    def __init__(self) -> None:
        self.checks = 0

    async def propose_steps(self, *, sub_question, context, tool_specs):
        return [PlannerDecision(tool_name="strategy:entity_based")]

    async def check_sufficiency(self, *, sub_question, context):
        self.checks += 1
        return True


class _Tool:
    """A tool that adds one new passage per call (progress, but never SQL rows)."""

    description = "tool"
    input_schema: dict = {}
    output_schema: dict = {}

    def __init__(self, name: str) -> None:
        self.name, self.calls = name, 0

    async def invoke(self, *, namespace, **_):
        self.calls += 1
        chunk = ChunkResult(chunk_id=f"{self.name}-{self.calls}", text="t", source_doc="", relevance_score=1)
        return ToolResult(chunks=(chunk,))


async def _run(ctx: AccumulatedContext, planner, trace: TraceCollector) -> StopReason:
    reg = ToolRegistry()
    reg.register(_Tool("nl_to_sql"))
    reg.register(_Tool("strategy:entity_based"))
    clock = BudgetClock(AgenticBudgetConfig(max_steps=6, time_budget_s=60))
    clock.started()
    return await ReasoningController(planner).run_one_loop(
        SubQuestion(text="q"), ctx, reg, clock, deque(), trace, namespace=NS
    )


def _partly_read_ctx(ctx: AccumulatedContext) -> AccumulatedContext:
    ctx.document_matches = [{"source_key": "docs/a.txt"}]
    ctx.chunks.append(_window("docs/a.txt", 0, 12000, 30000))
    return ctx


class TestControllerHoldBack:
    @pytest.mark.asyncio
    async def test_held_back_at_most_twice_and_recorded(self):
        planner, trace = _AlwaysSufficient(), TraceCollector()
        ctx = _partly_read_ctx(_ctx())
        assert await _run(ctx, planner, trace) == StopReason.sufficiency_satisfied
        assert planner.checks == 3
        assert sum(s.step == "sufficiency_guard" for s in trace.steps) == 2
        held = [a for a in ctx.attempt_history if "held_back" in str(a)]
        assert len(held) == 2

    @pytest.mark.asyncio
    async def test_off_by_default_stops_on_first_check(self):
        planner, trace = _AlwaysSufficient(), TraceCollector()
        ctx = _partly_read_ctx(AccumulatedContext())
        assert await _run(ctx, planner, trace) == StopReason.sufficiency_satisfied
        assert planner.checks == 1
        assert not any(s.step == "sufficiency_guard" for s in trace.steps)


def test_option_validation():
    ok = {"sufficiencyGuards": True, "entityRouting": True}
    assert InvokeRequest(query="q", namespace=NS, options=ok).options == ok
    with pytest.raises(ValueError):
        InvokeRequest(query="q", namespace=NS, options={"sufficiencyGuards": "yes"})


# ── retriever wiring ───────────────────────────────────────────────


class _Decomposer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def decompose_with_language(self, query, trace=None):
        self.calls.append("language")
        return [SubQuestion(text=query)], "en"

    async def decompose_with_terms(self, query, trace=None):
        self.calls.append("terms")
        return [SubQuestion(text=query)], "en", []


class _Ctl:
    def __init__(self) -> None:
        self.seen: list[bool] = []

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
        self.seen.append(ctx.sufficiency_guards)
        return None


class _Synth:
    async def synthesize(self, query, chunks, entities, **_):
        return SynthesisResult(answer="ok", confidence=0.8, duration_ms=1)


class TestRetrieverWiring:
    @pytest.mark.asyncio
    async def test_guards_flag_set_and_decomposition_unchanged(self):
        dec, ctl = _Decomposer(), _Ctl()
        r = AgenticRetriever(
            ToolRegistry(), ctl, dec, _Synth(), AgenticBudgetConfig(max_steps=2), final_semantic_search=False
        )
        await r.resolve("q", NS)
        await r.resolve("q", NS, options={"entityRouting": True})
        await r.resolve("q", NS, options={"entityRouting": True, "sufficiencyGuards": True})
        assert dec.calls == ["language", "terms", "terms"]
        assert ctl.seen == [False, False, True]


# ── options.singleCallSteps ────────────────────────────────────────


class _StopAfterOne:
    """Proposes one step, then an empty list (the planner's "enough")."""

    def __init__(self) -> None:
        self.calls = 0
        self.checks = 0

    async def propose_steps(self, **_):
        self.calls += 1
        return [PlannerDecision(tool_name="strategy:entity_based", inputs={})] if self.calls == 1 else []

    async def check_sufficiency(self, **_):
        self.checks += 1
        return False


class TestSingleCallSteps:
    @pytest.mark.asyncio
    async def test_empty_proposal_ends_loop_without_a_check_and_gives_the_step_back(self):
        planner, trace = _StopAfterOne(), TraceCollector()
        ctx = AccumulatedContext()
        ctx.single_call_steps = True
        reg = ToolRegistry()
        reg.register(_Tool("strategy:entity_based"))
        clock = BudgetClock(AgenticBudgetConfig(max_steps=5, time_budget_s=60))
        clock.started()
        stop = await ReasoningController(planner).run_one_loop(
            SubQuestion(text="q"), ctx, reg, clock, deque(), trace, namespace=NS
        )
        assert stop == StopReason.sufficiency_satisfied
        assert planner.checks == 0 and planner.calls == 2
        assert clock.steps_consumed == 1

    @pytest.mark.asyncio
    async def test_failed_planner_call_is_not_enough(self):
        class _Failing(_StopAfterOne):
            async def propose_steps(self, **_):
                self.calls += 1
                return None  # what the planner returns when its call fails

        planner, trace = _Failing(), TraceCollector()
        ctx = AccumulatedContext()
        ctx.single_call_steps = True
        reg = ToolRegistry()
        reg.register(_Tool("strategy:entity_based"))
        clock = BudgetClock(AgenticBudgetConfig(max_steps=5, time_budget_s=60))
        clock.started()
        stop = await ReasoningController(planner).run_one_loop(
            SubQuestion(text="q"), ctx, reg, clock, deque(), trace, namespace=NS
        )
        assert stop != StopReason.sufficiency_satisfied
        assert not any(s.detail == "planner_says_enough" for s in trace.steps)

    @pytest.mark.asyncio
    async def test_guard_holds_back_an_empty_proposal_at_most_twice(self):
        planner, trace = _StopAfterOne(), TraceCollector()
        ctx = _partly_read_ctx(_ctx())
        ctx.single_call_steps = True
        assert await _run(ctx, planner, trace) == StopReason.sufficiency_satisfied
        assert sum(s.step == "sufficiency_guard" for s in trace.steps) == 2
        assert planner.calls == 4 and planner.checks == 0

    @pytest.mark.asyncio
    async def test_planner_prompt_gets_stop_instruction_and_content_only_with_option(self):
        from coa_serve.tier3.agentic.planner import BedrockStepPlanner

        seen: list[tuple[str, str]] = []

        class _Client:
            async def converse(self, *, prompt, system, **_):
                seen.append((prompt, system))
                return SimpleNamespace(text='{"candidates": []}', guardrail_blocked=False)

        planner = BedrockStepPlanner(_Client())
        spec = [{"name": "nl_to_sql", "description": "d", "input_schema": {"query": "str"}}]
        ctx = AccumulatedContext()
        await planner.propose_steps(sub_question=SubQuestion(text="q"), context=ctx, tool_specs=spec)
        ctx.single_call_steps = True
        await planner.propose_steps(sub_question=SubQuestion(text="q"), context=ctx, tool_specs=spec)
        (p0, s0), (p1, s1) = seen
        assert "STOP WHEN DONE" not in s0 and "Content gathered so far" not in p0
        assert "STOP WHEN DONE" in s1 and "Content gathered so far" in p1

    def test_fork_and_validation(self):
        ctx = AccumulatedContext()
        ctx.single_call_steps = True
        assert ctx.fork().single_call_steps is True
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options={"singleCallSteps": "yes"})


# ── options.keepSlowSql ────────────────────────────────────────────


class TestPendingSql:
    @pytest.mark.asyncio
    async def test_pending_sql_holds_the_check_then_is_taken_when_done(self):
        import asyncio
        import time

        from coa_serve.tier3.agentic.controller import PendingSql, take_pending_sql

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        ctx = _ctx()
        ctx.pending_sql = PendingSql(fut, "q", time.perf_counter())
        assert "still running" in insufficiency_reason(ctx)
        assert ctx.fork().pending_sql is ctx.pending_sql
        trace = TraceCollector()
        assert await take_pending_sql(ctx, trace) is False
        fut.set_result(ToolResult(rows=({"n": 1},), detail="1 row"))
        assert await take_pending_sql(ctx, trace) is True
        assert ctx.rows and ctx.pending_sql.consumed
        assert insufficiency_reason(ctx) == ""
        assert [s.step for s in trace.steps] == ["prefetch_sql_late"]

    @pytest.mark.asyncio
    async def test_late_access_denied_propagates(self):
        import asyncio
        import time

        from coa_serve.exceptions import AccessDeniedError
        from coa_serve.tier3.agentic.controller import PendingSql, take_pending_sql

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        fut.set_exception(AccessDeniedError("denied"))
        ctx = AccumulatedContext()
        ctx.pending_sql = PendingSql(fut, "q", time.perf_counter())
        with pytest.raises(AccessDeniedError):
            await take_pending_sql(ctx, TraceCollector())

    @pytest.mark.asyncio
    async def test_planner_sql_request_waits_for_the_pending_call(self):
        import asyncio
        import time

        from coa_serve.tier3.agentic.controller import PendingSql

        class _SqlThenStop:
            def __init__(self) -> None:
                self.calls = 0

            async def propose_steps(self, **_):
                self.calls += 1
                return [PlannerDecision(tool_name="nl_to_sql", inputs={})] if self.calls == 1 else []

            async def check_sufficiency(self, **_):
                return True

        async def _late():
            await asyncio.sleep(0.01)
            return ToolResult(rows=({"n": 2},), detail="1 row")

        ctx = AccumulatedContext()
        ctx.pending_sql = PendingSql(asyncio.ensure_future(_late()), "q", time.perf_counter())
        reg = ToolRegistry()
        sql = _Tool("nl_to_sql")
        reg.register(sql)
        clock = BudgetClock(AgenticBudgetConfig(max_steps=4, time_budget_s=60))
        clock.started()
        trace = TraceCollector()
        await ReasoningController(_SqlThenStop()).run_one_loop(
            SubQuestion(text="q"), ctx, reg, clock, deque(), trace, namespace=NS
        )
        assert sql.calls == 0 and ctx.rows == [{"n": 2}]
        assert [s.detail for s in trace.steps if s.step == "pending_sql_wait"] == ["used the prefetch SQL"]


class _SlowSql:
    name = "nl_to_sql"
    description = "sql"
    input_schema = {"query": "str"}
    output_schema: dict = {}

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.cancelled = False
        self.calls = 0

    async def invoke(self, *, namespace, query="", **_):
        import asyncio

        self.calls += 1
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult(rows=({"n": 7},), detail="1 row")


class _NotSufficient:
    async def assess(self, sub_question, ctx):
        return False, ""

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
        import asyncio

        await asyncio.sleep(0.05)
        return None


class TestKeepSlowSqlPrefetch:
    @staticmethod
    def _retriever(sql) -> AgenticRetriever:
        reg = ToolRegistry()
        reg.register(sql)
        return AgenticRetriever(
            reg,
            _NotSufficient(),
            _Decomposer(),
            _Synth(),
            AgenticBudgetConfig(max_steps=2, time_budget_s=60, per_tool_timeout_s=0.01),
            final_semantic_search=False,
        )

    @pytest.mark.asyncio
    async def test_late_prefetch_sql_is_kept_and_used(self):
        sql, trace = _SlowSql(0.03), TraceCollector()
        opts = {"prefetchMode": "assess", "keepSlowSql": True}
        result = await self._retriever(sql).resolve("q", NS, options=opts, trace=trace)
        assert [s.status for s in trace.steps if s.step == "prefetch_sql"] == ["pending"]
        assert [s.status for s in trace.steps if s.step == "prefetch_sql_late"] == ["success"]
        assert result is not None and sql.calls == 1 and not sql.cancelled

    @pytest.mark.asyncio
    async def test_unfinished_prefetch_sql_cancelled_before_the_answer(self):
        import asyncio

        sql, trace = _SlowSql(5.0), TraceCollector()
        opts = {"prefetchMode": "assess", "keepSlowSql": True}
        await self._retriever(sql).resolve("q", NS, options=opts, trace=trace)
        await asyncio.sleep(0)
        assert sql.cancelled
        assert not [s for s in trace.steps if s.step == "prefetch_sql_late"]

    @pytest.mark.asyncio
    async def test_without_the_option_a_slow_call_times_out(self):
        sql, trace = _SlowSql(0.03), TraceCollector()
        await self._retriever(sql).resolve("q", NS, options={"prefetchMode": "assess"}, trace=trace)
        assert [s.status for s in trace.steps if s.step == "prefetch_sql"] == ["error"]

    def test_option_validated(self):
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options={"keepSlowSql": "yes"})
