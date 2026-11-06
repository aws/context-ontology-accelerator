# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deep-reasoning early-stop signals: wiring, telemetry, embed bounds, stop rules.

Covers the controller and retriever side of the stop signals (the planner side
is in ``test_tier3_agentic_planner.py`` and default equivalence in
``test_tier3_agentic_stop_signal_defaults.py``).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog.testing
from coa_serve.tier3.agentic import controller as controller_module
from coa_serve.tier3.agentic.budget import AgenticBudgetConfig, BudgetClock
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.models import (
    PlannerDecision,
    ReplySignals,
    StopReason,
    StopSignalConfig,
    SubQuestion,
)
from coa_serve.tier3.agentic.planner import BedrockStepPlanner
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.tier3.vector_retriever import ChunkResult
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit

NS = "acme-sales"


class _Clock:
    def __call__(self) -> float:
        return 1000.0


class _Decomposer:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    async def decompose(self, query, trace=None):
        return [SubQuestion(text=t) for t in self._texts]


class _Synth:
    async def synthesize(self, query, chunks, entities, **_):
        return SynthesisResult(answer="ok", confidence=0.8, duration_ms=1)


class _IndexRecordingController:
    """Records the telemetry indexes the retriever passes; the first loop opens one branch."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
        self.calls.append((sub_question.text, kw["sq_index"], kw["loop_index"]))
        if sub_question.text == "b":
            queue.append(SubQuestion(text="b-branch"))
        return StopReason.sufficiency_satisfied


class TestLoopIndexes:
    async def test_retriever_passes_sub_question_and_branch_indexes(self):
        controller = _IndexRecordingController()
        retriever = AgenticRetriever(
            object(),
            controller,
            _Decomposer(["a", "b"]),
            _Synth(),
            AgenticBudgetConfig(),
            clock_source=_Clock(),
            final_semantic_search=False,
        )
        await retriever.resolve("q", NS, trace=TraceCollector())
        assert sorted(controller.calls) == [("a", 0, 0), ("b", 1, 0), ("b-branch", 1, 1)]

    async def test_single_sub_question_is_index_zero(self):
        controller = _IndexRecordingController()
        retriever = AgenticRetriever(
            object(),
            controller,
            _Decomposer(["a"]),
            _Synth(),
            AgenticBudgetConfig(),
            clock_source=_Clock(),
            final_semantic_search=False,
        )
        await retriever.resolve("q", NS, trace=TraceCollector())
        assert controller.calls == [("a", 0, 0)]


# ── shared fakes ─────────────────────────────────────────────────────────


class _FakeClock:
    """A manually advanced time source for the BudgetClock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _chunk(chunk_id: str) -> ChunkResult:
    return ChunkResult(chunk_id=chunk_id, text=f"text {chunk_id}", source_doc=f"s3://b/{chunk_id}", relevance_score=0.9)


class _Tool:
    """A tool that returns a fresh chunk per call (``fresh=False``: always the same one)."""

    def __init__(self, name: str = "strategy:entity_based", *, fresh: bool = True) -> None:
        self.name = name
        self.description = f"fake {name}"
        self.input_schema = {"query": "str"}
        self.output_schema: dict = {}
        self._fresh = fresh
        self.calls = 0

    async def invoke(self, *, namespace: str, **_):
        self.calls += 1
        return ToolResult(chunks=(_chunk(f"{self.name}-{self.calls if self._fresh else 0}"),), detail="ok")


TOOL_STEP = [PlannerDecision(tool_name="strategy:entity_based", inputs={"query": "q"})]


def reply(answer: str = "", confidence: float | None = None, **kw) -> ReplySignals:
    return ReplySignals(confidence=confidence, working_answer=answer, reply_chars=len(answer) + 40, **kw)


class SignalPlanner:
    """Scripted planner that writes ``ctx.last_stop_signals`` like the real one.

    ``script`` holds ``(candidates, signals)`` pairs consumed per propose call; the
    last pair repeats. ``assessments`` holds ``(sufficient, answer, signals)``
    triples for the assess loop. ``per_question`` maps a sub-question text to its
    own script (for concurrent sub-questions).
    """

    def __init__(self, script=None, *, assessments=None, per_question=None) -> None:
        self._script = deque(script or [])
        self._assessments = deque(assessments or [])
        self._per_question = {k: deque(v) for k, v in (per_question or {}).items()}
        self.propose_calls = 0
        self.assess_calls = 0

    @staticmethod
    def _next(queue: deque):
        return queue.popleft() if len(queue) > 1 else queue[0]

    async def propose_steps(self, *, sub_question, context, tool_specs):
        self.propose_calls += 1
        queue = self._per_question.get(sub_question.text, self._script)
        candidates, signals = self._next(queue)
        if signals is not None:
            context.last_stop_signals = signals
        return candidates

    async def assess(self, *, sub_question, context):
        self.assess_calls += 1
        sufficient, answer, signals = self._next(self._assessments)
        if signals is not None:
            context.last_stop_signals = signals
        return sufficient, answer

    async def check_sufficiency(self, *, sub_question, context):
        return False


class VecEmbed:
    """Maps a working answer to a fixed vector: equal answers have cosine 1, different ones 0."""

    VECTORS = {"A": [1.0, 0.0, 0.0], "B": [0.0, 1.0, 0.0], "C": [0.0, 0.0, 1.0], "A2": [0.96, 0.28, 0.0]}

    def __init__(self, *, delay: float = 0.0, raises: Exception | None = None, fail_on: frozenset = frozenset()):
        self.calls: list[str] = []
        self.cancelled = 0
        self._delay = delay
        self._raises = raises
        self._fail_on = fail_on

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self._delay:
            try:
                await asyncio.sleep(self._delay)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if self._raises is not None or text in self._fail_on:
            raise self._raises or RuntimeError("embed down")
        return self.VECTORS.get(text, [0.5, 0.5, 0.5])


async def run(
    planner,
    *,
    config: StopSignalConfig,
    embed=None,
    tool=None,
    ctx: AccumulatedContext | None = None,
    single_call: bool = True,
    guards: bool = False,
    per_tool_timeout_s: float = 30.0,
    clock_source: _FakeClock | None = None,
    sq_index: int = 0,
    **budget,
):
    """Drive one controller loop; returns (stop, ctx, trace, tool)."""
    source = clock_source or _FakeClock()
    clock = BudgetClock(AgenticBudgetConfig(**{"max_steps": 10, "time_budget_s": 120.0, **budget}), clock=source)
    clock.started()
    tool = tool or _Tool()
    registry = ToolRegistry()
    registry.register(tool)
    ctx = ctx if ctx is not None else AccumulatedContext()
    ctx.single_call_steps = single_call
    ctx.sufficiency_guards = guards
    trace = TraceCollector()
    controller = ReasoningController(
        planner, per_tool_timeout_s=per_tool_timeout_s, embed_fn=embed, stop_signals=config
    )
    stop = await controller.run_one_loop(
        SubQuestion(text="q"), ctx, registry, clock, deque(), trace, namespace=NS, sq_index=sq_index
    )
    return stop, ctx, trace, tool


def steps(trace: TraceCollector, name: str) -> list:
    return [s for s in trace.steps if s.step == name]


def signal_details(trace: TraceCollector) -> list[dict]:
    return [s.detail for s in steps(trace, "stop_signals")]


TELEMETRY = StopSignalConfig(telemetry=True)

_FIELDS = {
    "phase",
    "sq",
    "loop",
    "step",
    "empty",
    "confidence",
    "cosine",
    "streak",
    "compared",
    "held_back",
    "repaired",
    "reply_chars",
    "propose_ms",
    "embed_ms",
    "elapsed_ms",
    "answer_sha256",
    "answer_chars",
}


# ── telemetry (AC-7) ────────────────────────────────────────────────────


class TestTelemetry:
    async def test_one_record_per_parsed_reply_with_every_field(self):
        planner = SignalPlanner([(TOOL_STEP, reply("A", 0.4)), ([], reply("SECRET ANSWER TEXT", 0.9))])
        with structlog.testing.capture_logs() as logs:
            stop, _, trace, _ = await run(planner, config=TELEMETRY, embed=VecEmbed(), sq_index=3)
        assert stop == StopReason.sufficiency_satisfied
        details = signal_details(trace)
        assert [d["step"] for d in details] == [1, 2]
        assert all(set(d) == _FIELDS for d in details)
        first, last = details
        assert (first["phase"], first["sq"], first["loop"], first["empty"]) == ("loop", 3, 0, False)
        assert (first["confidence"], last["confidence"], last["empty"]) == (0.4, 0.9, True)
        assert last["answer_sha256"] == hashlib.sha256(b"SECRET ANSWER TEXT").hexdigest()
        assert last["answer_chars"] == len("SECRET ANSWER TEXT")
        records = [r for r in logs if r["event"] == "deep_reasoning_stop_signals"]
        assert len(records) == 2
        assert all("request_id" in r and set(r) >= _FIELDS for r in records)

    async def test_no_answer_text_in_trace_or_log(self):
        planner = SignalPlanner([(TOOL_STEP, reply("A", 0.4)), ([], reply("SECRET ANSWER TEXT", 0.9))])
        with structlog.testing.capture_logs() as logs:
            _, _, trace, _ = await run(planner, config=TELEMETRY, embed=VecEmbed())
        assert "SECRET ANSWER TEXT" not in json.dumps(trace.steps_serializable, default=str)
        assert "SECRET ANSWER TEXT" not in json.dumps(logs, default=str)

    async def test_steps_serializable_round_trip_keeps_the_dict(self):
        planner = SignalPlanner([(TOOL_STEP, reply("A", 0.4)), (TOOL_STEP, reply("A", 0.5)), ([], reply("B", 1.0))])
        _, _, trace, _ = await run(planner, config=StopSignalConfig(similarity_threshold=0.5), embed=VecEmbed())
        payload = json.loads(json.dumps(trace.steps_serializable))
        recorded = [s["detail"] for s in payload if s["step"] == "stop_signals"]
        assert recorded == signal_details(trace)
        assert recorded[1]["cosine"] == 1.0 and recorded[1]["compared"] is True

    async def test_request_id_comes_from_the_bound_context(self):
        planner = SignalPlanner([([], reply("A", 0.9))])
        structlog.contextvars.bind_contextvars(request_id="req-123")
        try:
            with structlog.testing.capture_logs() as logs:
                await run(planner, config=TELEMETRY)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        assert [r["request_id"] for r in logs if r["event"] == "deep_reasoning_stop_signals"] == ["req-123"]

    async def test_defaults_record_nothing(self):
        planner = SignalPlanner([(TOOL_STEP, reply("A", 0.4)), ([], reply("B", 0.9))])
        with structlog.testing.capture_logs() as logs:
            _, _, trace, _ = await run(planner, config=StopSignalConfig(), embed=VecEmbed())
        assert not steps(trace, "stop_signals") and not steps(trace, "subquestion_stop")
        assert not [r for r in logs if r["event"] == "deep_reasoning_stop_signals"]

    async def test_failed_reply_is_not_recorded(self):
        planner = SignalPlanner([(None, None)])
        stop, _, trace, _ = await run(planner, config=TELEMETRY)
        assert stop is None
        assert not steps(trace, "stop_signals")


class TestSubquestionStop:
    @staticmethod
    def _stop(trace) -> dict:
        (record,) = steps(trace, "subquestion_stop")
        return record.detail

    async def test_sufficiency_exit(self):
        _, _, trace, _ = await run(SignalPlanner([(TOOL_STEP, reply("A")), ([], reply("A"))]), config=TELEMETRY)
        assert self._stop(trace) == {"sq": 0, "loop": 0, "reason": "sufficiency_satisfied", "steps": 2}

    async def test_none_exit(self):
        _, _, trace, _ = await run(SignalPlanner([(None, None)]), config=TELEMETRY)
        assert self._stop(trace) == {"sq": 0, "loop": 0, "reason": None, "steps": 0}

    async def test_max_steps_exit(self):
        _, _, trace, _ = await run(SignalPlanner([(TOOL_STEP, reply("A"))]), config=TELEMETRY, max_steps=2)
        assert self._stop(trace) == {"sq": 0, "loop": 0, "reason": "max_steps_reached", "steps": 2}

    async def test_time_budget_exit(self):
        source = _FakeClock()

        class _Slow(_Tool):
            async def invoke(self, *, namespace, **kw):
                source.t += 200.0  # past the 120 s budget
                return await super().invoke(namespace=namespace, **kw)

        _, _, trace, _ = await run(
            SignalPlanner([(TOOL_STEP, reply("A"))]), config=TELEMETRY, tool=_Slow(), clock_source=source
        )
        assert self._stop(trace) == {"sq": 0, "loop": 0, "reason": "time_budget_expired", "steps": 1}

    async def test_exception_exit_still_records(self):
        class _Boom(SignalPlanner):
            async def propose_steps(self, **kw):
                raise RuntimeError("planner exploded")

        clock = BudgetClock(AgenticBudgetConfig(), clock=_FakeClock())
        clock.started()
        trace = TraceCollector()
        controller = ReasoningController(_Boom(), stop_signals=TELEMETRY)
        with pytest.raises(RuntimeError):
            await controller.run_one_loop(
                SubQuestion(text="q"), AccumulatedContext(), ToolRegistry(), clock, deque(), trace, namespace=NS
            )
        assert self._stop(trace) == {"sq": 0, "loop": 0, "reason": None, "steps": 0}


class TestForkedSubQuestions:
    async def test_two_sub_questions_keep_their_own_signals(self):
        """Concurrent sub-questions on forks: each reply is recorded under its own index."""
        planner = SignalPlanner(
            per_question={
                "a": [(TOOL_STEP, reply("A", 0.2)), (TOOL_STEP, reply("A", 0.3)), ([], reply("A", 0.95))],
                "b": [(TOOL_STEP, reply("B", 0.6)), ([], reply("B", 0.4)), ([], reply("C", 0.9))],
            }
        )
        registry = ToolRegistry()
        registry.register(_Tool())
        controller = ReasoningController(
            planner,
            embed_fn=VecEmbed(),
            stop_signals=StopSignalConfig(min_confidence=0.7, similarity_threshold=0.9, stability_steps=5),
        )
        retriever = AgenticRetriever(
            registry,
            controller,
            _Decomposer(["a", "b"]),
            _Synth(),
            AgenticBudgetConfig(max_steps=20, time_budget_s=120.0),
            clock_source=_Clock(),
            final_semantic_search=False,
        )
        trace = TraceCollector()
        await retriever.resolve("q", NS, trace=trace, options={"singleCallSteps": True})
        by_sq: dict[int, list[dict]] = {}
        for d in signal_details(trace):
            by_sq.setdefault(d["sq"], []).append(d)
        assert [d["confidence"] for d in by_sq[0]] == [0.2, 0.3, 0.95]
        assert [d["confidence"] for d in by_sq[1]] == [0.6, 0.4, 0.9]
        # Sub-question b's empty reply at 0.4 < 0.7 was held back, on its own fork.
        assert [d["held_back"] for d in by_sq[1]] == [None, "confidence", None]
        assert [d["held_back"] for d in by_sq[0]] == [None, None, None]
        assert by_sq[0][1]["cosine"] == 1.0  # A vs A on fork a, never compared against b's answers
        stops = {s.detail["sq"]: s.detail["reason"] for s in steps(trace, "subquestion_stop")}
        assert stops == {0: "sufficiency_satisfied", 1: "sufficiency_satisfied"}


# ── embed gating and deadline (AC-9, R-10) ──────────────────────────────


class TestEmbedBounds:
    @pytest.mark.parametrize("single_call", [True, False])
    async def test_no_embed_at_default_settings(self, single_call):
        embed = VecEmbed()
        planner = SignalPlanner(
            [(TOOL_STEP, reply("A"))], assessments=[(False, "A", None), (False, "A", None), (True, "A", None)]
        )
        await run(planner, config=StopSignalConfig(), embed=embed, single_call=single_call, max_steps=3)
        assert embed.calls == []

    async def test_no_embed_for_confidence_only(self):
        embed = VecEmbed()
        planner = SignalPlanner([(TOOL_STEP, reply("A", 0.9)), ([], reply("A", 0.9))])
        await run(planner, config=StopSignalConfig(min_confidence=0.5), embed=embed)
        assert embed.calls == []

    async def test_telemetry_alone_embeds_but_never_stops(self):
        embed = VecEmbed()
        planner = SignalPlanner([(TOOL_STEP, reply("A"))])
        stop, _, trace, _ = await run(planner, config=TELEMETRY, embed=embed, max_steps=4)
        assert stop == StopReason.max_steps_reached
        assert embed.calls == ["A"] * 4
        assert [d["cosine"] for d in signal_details(trace)] == [None, 1.0, 1.0, 1.0]
        assert [d["streak"] for d in signal_details(trace)] == [0, 0, 0, 0]

    async def test_slow_embed_is_cancelled_and_logged(self):
        embed = VecEmbed(delay=5.0)
        planner = SignalPlanner([(TOOL_STEP, reply("A")), ([], reply("A"))])
        with structlog.testing.capture_logs() as logs:
            stop, _, trace, _ = await run(
                planner, config=StopSignalConfig(similarity_threshold=0.9), embed=embed, per_tool_timeout_s=0.05
            )
        assert stop == StopReason.sufficiency_satisfied
        assert embed.cancelled == 2
        assert [r for r in logs if r["event"] == "answer_stability_embed_timeout"]
        assert all(d["compared"] is False for d in signal_details(trace))

    async def test_no_embed_when_the_budget_has_no_room(self):
        """Usable time (budget minus synthesis reserve) under the minimum skips the embed."""
        embed = VecEmbed()
        planner = SignalPlanner([(TOOL_STEP, reply("A")), ([], reply("A"))])
        stop, _, _, _ = await run(
            planner, config=StopSignalConfig(similarity_threshold=0.9), embed=embed, time_budget_s=9.0
        )
        assert stop == StopReason.sufficiency_satisfied
        assert embed.calls == []

    def test_cosine_is_pure_python(self):
        assert controller_module._cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
        assert controller_module._cosine_similarity([1.0, 0.0], [0.0, 2.0]) == 0.0
        assert controller_module._cosine_similarity([3.0, 4.0], [4.0, 3.0]) == pytest.approx(0.96)
        assert controller_module._cosine_similarity([], []) == 0.0
        assert controller_module._cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
        assert controller_module._cosine_similarity([1.0], [1.0, 0.0]) == 0.0

    def test_no_numpy_in_the_agentic_package(self):
        def imports_numpy(path: Path) -> bool:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "numpy" for a in node.names):
                    return True
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "numpy":
                    return True
            return False

        package = Path(controller_module.__file__).parent
        assert [p.name for p in package.rglob("*.py") if imports_numpy(p)] == []


# ── single-call confidence hold-back (AC-3) ─────────────────────────────


class _PendingSql:
    """A prefetch SQL still running, so the sufficiency guard has a reason to hold back."""

    def __init__(self) -> None:
        self.consumed = False
        self.task = asyncio.get_running_loop().create_future()


def _guarded_ctx() -> AccumulatedContext:
    ctx = AccumulatedContext()
    ctx.pending_sql = _PendingSql()
    return ctx


def held(trace) -> list:
    return [d["held_back"] for d in signal_details(trace)]


CONF = StopSignalConfig(min_confidence=0.7)


class TestConfidenceHoldBack:
    async def test_low_confidence_enough_continues_then_high_confidence_stops(self):
        planner = SignalPlanner([([], reply("A", 0.42)), (TOOL_STEP, reply("A", 0.5)), ([], reply("A", 0.7))])
        stop, ctx, trace, tool = await run(planner, config=CONF)
        assert stop == StopReason.sufficiency_satisfied
        assert planner.propose_calls == 3 and tool.calls == 1
        assert held(trace) == ["confidence", None, None]
        (hold,) = steps(trace, "sufficiency_confidence_hold")
        assert hold.detail == "confidence 0.42 < 0.70: gather the part of the question still unanswered"
        assert any(a.get("status") == "held_back" for a in ctx.attempt_history)

    async def test_at_threshold_stops_at_once(self):
        planner = SignalPlanner([([], reply("A", 0.7))])
        stop, _, trace, _ = await run(planner, config=CONF)
        assert stop == StopReason.sufficiency_satisfied
        assert planner.propose_calls == 1 and not steps(trace, "sufficiency_confidence_hold")

    async def test_hold_back_is_bounded(self):
        planner = SignalPlanner([([], reply("A", 0.1))])
        stop, _, trace, _ = await run(planner, config=CONF)
        assert stop == StopReason.sufficiency_satisfied
        assert planner.propose_calls == 3
        assert held(trace) == ["confidence", "confidence", None]

    async def test_missing_confidence_counts_as_below(self):
        planner = SignalPlanner([([], reply("A", None)), ([], reply("A", 0.9))])
        stop, _, trace, _ = await run(planner, config=CONF)
        assert stop == StopReason.sufficiency_satisfied
        assert held(trace) == ["confidence", None]
        assert "confidence not reported < 0.70" in steps(trace, "sufficiency_confidence_hold")[0].detail

    async def test_hold_back_gives_the_step_back(self):
        planner = SignalPlanner([([], reply("A", 0.1))])
        stop, *_ = await run(planner, config=CONF, max_steps=1)
        assert stop == StopReason.sufficiency_satisfied  # three replies within one step

    async def test_guard_first_then_confidence(self):
        planner = SignalPlanner([([], reply("A", 0.1))])
        stop, _, trace, _ = await run(planner, config=CONF, ctx=_guarded_ctx(), guards=True)
        assert stop == StopReason.sufficiency_satisfied
        assert held(trace) == ["guard", "guard", "confidence", "confidence", None]
        assert len(steps(trace, "sufficiency_guard")) == 2
        assert len(steps(trace, "sufficiency_confidence_hold")) == 2

    async def test_threshold_zero_never_holds_back(self):
        planner = SignalPlanner([([], reply("A", 0.0))])
        stop, _, trace, _ = await run(planner, config=TELEMETRY)
        assert stop == StopReason.sufficiency_satisfied
        assert held(trace) == [None]

    async def test_no_tools_path_has_no_stale_carrier(self):
        """With no tools the planner answers [] without a model call; a stale carrier must not leak."""

        class _NoCallLLM:
            async def converse(self, **_):
                raise AssertionError("no model call expected")

        ctx = AccumulatedContext()
        ctx.single_call_steps = True
        ctx.last_stop_signals = ReplySignals(confidence=1.0, working_answer="stale")
        clock = BudgetClock(AgenticBudgetConfig(), clock=_FakeClock())
        clock.started()
        trace = TraceCollector()
        controller = ReasoningController(BedrockStepPlanner(_NoCallLLM(), stop_signals=CONF), stop_signals=CONF)
        stop = await controller.run_one_loop(
            SubQuestion(text="q"), ctx, ToolRegistry(), clock, deque(), trace, namespace=NS
        )
        assert stop == StopReason.sufficiency_satisfied
        assert len(steps(trace, "sufficiency_confidence_hold")) == 2
        assert all("not reported" in s.detail for s in steps(trace, "sufficiency_confidence_hold"))
        assert ctx.last_stop_signals is None


# ── single-call answer stability (AC-4) ─────────────────────────────────


def stable(window: int, threshold: float = 0.9) -> StopSignalConfig:
    return StopSignalConfig(similarity_threshold=threshold, stability_steps=window)


def scripted(*answers: str) -> SignalPlanner:
    """Every reply proposes a tool; the working answers follow ``answers`` (the last repeats)."""
    return SignalPlanner([(TOOL_STEP, reply(a)) for a in answers])


class TestAnswerStability:
    @pytest.mark.parametrize("window", [1, 2, 3])
    async def test_stop_step_for_each_window(self, window):
        embed = VecEmbed()
        planner = scripted("A")
        stop, _, trace, tool = await run(planner, config=stable(window), embed=embed)
        assert stop == StopReason.answer_stable
        # Reply 1 sets the baseline; replies 2..W+1 are stable; the stop fires on
        # reply W+1, before its tool runs.
        assert planner.propose_calls == window + 1
        assert tool.calls == window
        assert len(embed.calls) == window + 1
        assert [d["streak"] for d in signal_details(trace)] == list(range(window + 1))
        assert steps(trace, "answer_stability")
        (sq_stop,) = steps(trace, "subquestion_stop")
        assert sq_stop.detail["reason"] == "answer_stable" and sq_stop.detail["steps"] == window + 1

    async def test_stop_gives_back_the_step(self):
        clock_source = _FakeClock()
        planner = scripted("A")
        stop, *_ = await run(planner, config=stable(1), embed=VecEmbed(), clock_source=clock_source, max_steps=2)
        assert stop == StopReason.answer_stable  # reply 2 consumed step 2, then gave it back

    async def test_streak_resets_when_the_answer_moves(self):
        planner = scripted("A", "A", "B", "B", "B")
        stop, _, trace, tool = await run(planner, config=stable(2), embed=VecEmbed())
        assert stop == StopReason.answer_stable
        assert [d["streak"] for d in signal_details(trace)] == [0, 1, 0, 1, 2]
        assert [d["cosine"] for d in signal_details(trace)] == [None, 1.0, 0.0, 1.0, 1.0]
        assert tool.calls == 4

    async def test_below_threshold_resets(self):
        planner = scripted("A", "A2", "A2", "A2")
        stop, _, trace, _ = await run(planner, config=stable(2, threshold=0.99), embed=VecEmbed())
        assert stop == StopReason.answer_stable
        assert [d["streak"] for d in signal_details(trace)] == [0, 0, 1, 2]

    async def test_failed_embed_skips_instead_of_resetting(self):
        embed = VecEmbed(fail_on=frozenset({"X"}))
        planner = scripted("A", "A", "X", "A")
        stop, _, trace, tool = await run(planner, config=stable(2), embed=embed)
        assert stop == StopReason.answer_stable
        details = signal_details(trace)
        assert [d["compared"] for d in details] == [True, True, False, True]
        assert [d["streak"] for d in details] == [0, 1, 1, 2]
        assert tool.calls == 3

    async def test_timed_out_embed_skips(self):
        class _SlowOnX(VecEmbed):
            async def embed(self, text):
                if text == "X":
                    await asyncio.sleep(5.0)
                return await super().embed(text)

        planner = scripted("A", "A", "X", "A")
        stop, _, trace, _ = await run(planner, config=stable(2), embed=_SlowOnX(), per_tool_timeout_s=0.05)
        assert stop == StopReason.answer_stable
        assert [d["streak"] for d in signal_details(trace)] == [0, 1, 1, 2]

    async def test_empty_answer_skips(self):
        embed = VecEmbed()
        planner = scripted("A", "A", "", "A")
        stop, _, trace, _ = await run(planner, config=stable(2), embed=embed)
        assert stop == StopReason.answer_stable
        assert [d["compared"] for d in signal_details(trace)] == [True, True, False, True]
        assert "" not in embed.calls

    async def test_reply_without_context_growth_is_skipped(self):
        """A tool step that adds nothing leaves the next reply uncompared (no embed, no change)."""
        embed = VecEmbed()
        planner = scripted("A")
        stop, _, trace, _ = await run(planner, config=stable(2), embed=embed, tool=_Tool(fresh=False))
        assert stop == StopReason.no_new_information  # the retrieval no-progress stop
        details = signal_details(trace)
        assert [d["compared"] for d in details] == [True, True, False, False]
        assert [d["streak"] for d in details] == [0, 1, 1, 1]
        assert len(embed.calls) == 2

    async def test_unknown_tool_reply_does_not_grow_the_context(self):
        bogus = [PlannerDecision(tool_name="nope", inputs={})]
        planner = SignalPlanner([(TOOL_STEP, reply("A")), (bogus, reply("A")), (TOOL_STEP, reply("A"))])
        _, _, trace, _ = await run(planner, config=stable(5), embed=VecEmbed(), max_steps=4)
        assert [d["compared"] for d in signal_details(trace)][:3] == [True, True, False]

    async def test_confidence_hold_back_is_not_progress(self):
        planner = SignalPlanner([(TOOL_STEP, reply("A", 0.9)), ([], reply("A", 0.1)), ([], reply("A", 0.9))])
        config = StopSignalConfig(min_confidence=0.5, similarity_threshold=0.9, stability_steps=3)
        _, _, trace, _ = await run(planner, config=config, embed=VecEmbed())
        assert [d["compared"] for d in signal_details(trace)] == [True, True, False]

    async def test_stability_stop_does_not_cancel_a_confidence_hold_back(self):
        """R18-m2: the streak reaches W on a held-back [] reply; the next (skipped) reply must not stop."""
        planner = SignalPlanner([(TOOL_STEP, reply("A", 0.5)), ([], reply("A", 0.3)), (TOOL_STEP, reply("A", 0.5))])
        config = StopSignalConfig(min_confidence=0.7, similarity_threshold=0.9, stability_steps=1)
        stop, _, trace, tool = await run(planner, config=config, embed=VecEmbed())
        assert stop == StopReason.answer_stable
        details = signal_details(trace)
        assert [d["compared"] for d in details] == [True, True, False, True]
        assert [d["streak"] for d in details] == [0, 1, 1, 2]
        assert held(trace) == [None, "confidence", None, None]
        # The tool proposed after the hold-back ran; the stop fired on the next compared reply.
        assert tool.calls == 2 and planner.propose_calls == 4

    async def test_no_embed_backend_never_stops_on_stability(self):
        stop, *_ = await run(scripted("A"), config=stable(1), embed=None, max_steps=3)
        assert stop == StopReason.max_steps_reached


class TestStabilityVeto:
    async def test_guard_vetoes_the_stability_stop_twice(self):
        planner = scripted("A")
        stop, _, trace, tool = await run(planner, config=stable(1), embed=VecEmbed(), ctx=_guarded_ctx(), guards=True)
        assert stop == StopReason.answer_stable
        assert held(trace) == [None, "stability_veto", "stability_veto", None]
        assert tool.calls == 3  # the two vetoed replies still ran their tools

    async def test_vetoes_leave_the_guard_deferrals_for_enough(self):
        """Two stability vetoes, then [] with the guard reason still set: still held back."""
        planner = SignalPlanner([(TOOL_STEP, reply("A"))] * 3 + [([], reply("A"))])
        stop, _, trace, _ = await run(planner, config=stable(1), embed=VecEmbed(), ctx=_guarded_ctx(), guards=True)
        assert stop == StopReason.sufficiency_satisfied
        assert held(trace) == [None, "stability_veto", "stability_veto", "guard", "guard", None]


# ── prefetch confidence gate (AC-3b) ────────────────────────────────────


class _ScriptedLLM:
    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[str] = []

    async def converse(self, *, prompt, system, **_):
        self.calls.append(system)
        return SimpleNamespace(text=self._replies.pop(0), guardrail_blocked=False)


def _assessment(sufficient: bool, confidence=None) -> str:
    body = {"sufficient": sufficient, "working_answer": "EMEA revenue was $41.2M."}
    if confidence is not None:
        body["confidence"] = confidence
    return json.dumps(body)


_ENOUGH = json.dumps({"candidates": [], "confidence": 0.95, "working_answer": "done"})


async def _prefetch_run(config: StopSignalConfig, *replies: str, controller=None):
    llm = _ScriptedLLM(*replies)
    registry = ToolRegistry()
    registry.register(_Tool())
    planner = BedrockStepPlanner(llm, stop_signals=config)
    retriever = AgenticRetriever(
        registry,
        controller or ReasoningController(planner, stop_signals=config),
        _Decomposer(["q"]),
        _Synth(),
        AgenticBudgetConfig(max_steps=6, time_budget_s=120.0),
        clock_source=_Clock(),
        final_semantic_search=False,
    )
    trace = TraceCollector()
    await retriever.resolve("q", NS, trace=trace, options={"prefetchMode": "assess", "singleCallSteps": True})
    return llm, trace


def _prefetch_done(trace) -> str:
    (done,) = steps(trace, "prefetch_done")
    return done.detail


class TestPrefetchGate:
    async def test_low_confidence_runs_the_loop(self):
        llm, trace = await _prefetch_run(CONF, _assessment(True, 0.4), _ENOUGH)
        assert len(llm.calls) == 2  # the prefetch assess, then one loop reply
        assert _prefetch_done(trace) == "not sufficient; running the loop"
        prefetch = [d for d in signal_details(trace) if d["phase"] == "prefetch"]
        assert prefetch == [
            {
                **prefetch[0],
                "sq": None,
                "loop": None,
                "step": 0,
                "empty": True,
                "confidence": 0.4,
                "held_back": "confidence",
            }
        ]
        assert steps(trace, "sufficiency_confidence_hold")

    async def test_missing_confidence_runs_the_loop(self):
        llm, trace = await _prefetch_run(CONF, _assessment(True), _ENOUGH)
        assert len(llm.calls) == 2
        assert _prefetch_done(trace) == "not sufficient; running the loop"

    async def test_confidence_at_the_bar_skips_the_loop(self):
        llm, trace = await _prefetch_run(CONF, _assessment(True, 0.7))
        assert len(llm.calls) == 1
        assert _prefetch_done(trace) == "sufficient; loop skipped"

    async def test_threshold_zero_skips_the_loop_as_on_main(self):
        llm, trace = await _prefetch_run(TELEMETRY, _assessment(True, 0.1))
        assert len(llm.calls) == 1
        assert _prefetch_done(trace) == "sufficient; loop skipped"
        assert [d["held_back"] for d in signal_details(trace) if d["phase"] == "prefetch"] == [None]

    async def test_not_sufficient_is_recorded_and_unchanged(self):
        llm, trace = await _prefetch_run(CONF, _assessment(False, 0.9), _ENOUGH)
        assert len(llm.calls) == 2
        assert [d["held_back"] for d in signal_details(trace) if d["phase"] == "prefetch"] == [None]

    async def test_guard_veto_keeps_the_vote_and_shows_as_guard(self, monkeypatch):
        """R18-m3: as on the loop paths, ``empty`` is the check's own vote and a guard veto is ``"guard"``."""
        from coa_serve.tier3.agentic import retriever as retriever_module

        monkeypatch.setattr(retriever_module, "insufficiency_reason", lambda ctx: "read the rest of the document")
        llm, trace = await _prefetch_run(CONF, _assessment(True, 0.9), _ENOUGH)
        assert len(llm.calls) == 2
        assert _prefetch_done(trace) == "not sufficient; running the loop"
        (prefetch,) = [d for d in signal_details(trace) if d["phase"] == "prefetch"]
        assert (prefetch["empty"], prefetch["held_back"], prefetch["confidence"]) == (True, "guard", 0.9)
        assert not steps(trace, "sufficiency_confidence_hold")

    async def test_defaults_record_no_prefetch_signals(self):
        llm, trace = await _prefetch_run(StopSignalConfig(), _assessment(True, 0.1))
        assert len(llm.calls) == 1
        assert not signal_details(trace)

    async def test_controller_without_the_gate_keeps_the_decision(self):
        class _NoGate:
            async def assess(self, sub_question, ctx):
                return True, "answer"

            async def run_one_loop(self, *a, **kw):
                raise AssertionError("the loop must be skipped")

        llm, trace = await _prefetch_run(CONF, controller=_NoGate())
        assert _prefetch_done(trace) == "sufficient; loop skipped"


# ── assess path: the same rules with single_call_steps=False (AC-5) ─────


def assessed(*rows) -> SignalPlanner:
    """A planner that always proposes a tool; each assess reply follows ``rows`` (the last repeats).

    A row is ``(sufficient, answer, confidence)``; the assess signals carry the confidence.
    """
    return SignalPlanner(
        [(TOOL_STEP, None)],
        assessments=[(suff, answer, reply(answer, conf)) for suff, answer, conf in rows],
    )


class TestAssessPath:
    async def test_low_confidence_sufficient_continues(self):
        planner = assessed((True, "A", 0.3), (True, "B", 0.8))
        stop, _, trace, tool = await run(planner, config=CONF, single_call=False)
        assert stop == StopReason.sufficiency_satisfied
        assert tool.calls == 2 and planner.assess_calls == 2
        assert held(trace) == ["confidence", None]
        assert steps(trace, "sufficiency_confidence_hold")

    async def test_threshold_zero_keeps_mains_stop(self):
        planner = assessed((True, "A", 0.1))
        stop, _, trace, tool = await run(planner, config=TELEMETRY, single_call=False)
        assert stop == StopReason.sufficiency_satisfied and tool.calls == 1

    async def test_guard_then_confidence(self):
        planner = assessed((True, "A", 0.1))
        stop, _, trace, _ = await run(
            planner, config=CONF, single_call=False, ctx=_guarded_ctx(), guards=True, max_steps=4
        )
        assert stop == StopReason.max_steps_reached
        assert held(trace) == ["guard", "guard", "confidence", "confidence"]

    @pytest.mark.parametrize("window", [1, 2, 3])
    async def test_stability_stop_keeps_no_new_information(self, window):
        embed = VecEmbed()
        planner = assessed((False, "A", None))
        stop, _, trace, tool = await run(planner, config=stable(window), embed=embed, single_call=False)
        assert stop == StopReason.no_new_information
        assert tool.calls == window + 1 and len(embed.calls) == window + 1
        assert steps(trace, "answer_stability")

    async def test_stability_runs_only_when_not_sufficient(self):
        embed = VecEmbed()
        planner = assessed((False, "A", None), (True, "A", None))
        stop, *_ = await run(planner, config=stable(1), embed=embed, single_call=False)
        assert stop == StopReason.sufficiency_satisfied
        assert embed.calls == ["A"]

    async def test_held_back_sufficient_is_compared(self):
        embed = VecEmbed()
        planner = assessed((False, "A", 0.9), (True, "A", 0.1))
        config = StopSignalConfig(min_confidence=0.5, similarity_threshold=0.9, stability_steps=1)
        stop, _, trace, _ = await run(planner, config=config, embed=embed, single_call=False)
        assert stop == StopReason.no_new_information
        assert held(trace) == [None, "confidence"]

    async def test_failed_embed_and_empty_answer_skip(self):
        embed = VecEmbed(fail_on=frozenset({"X"}))
        planner = assessed((False, "A", None), (False, "X", None), (False, "", None), (False, "A", None))
        stop, _, trace, _ = await run(planner, config=stable(1), embed=embed, single_call=False)
        assert stop == StopReason.no_new_information
        assert [d["compared"] for d in signal_details(trace)] == [True, False, False, True]
        assert [d["streak"] for d in signal_details(trace)] == [0, 0, 0, 1]

    async def test_assess_error_leaves_no_carrier(self):
        """A failing assess call reads as "no signal", even after an earlier reply wrote one."""

        class _Llm:
            def __init__(self) -> None:
                self.n = 0

            async def converse(self, *, prompt, system, **_):
                self.n += 1
                if "sufficient" in system:
                    if self.n == 2:
                        return SimpleNamespace(
                            text='{"sufficient": false, "confidence": 0.9, "working_answer": "A"}',
                            guardrail_blocked=False,
                        )
                    raise RuntimeError("bedrock down")
                return SimpleNamespace(
                    text=json.dumps({"candidates": [{"tool_name": "strategy:entity_based", "inputs": {"query": "q"}}]}),
                    guardrail_blocked=False,
                )

        planner = BedrockStepPlanner(_Llm(), stop_signals=CONF)
        stop, ctx, trace, _ = await run(planner, config=CONF, single_call=False, max_steps=2)
        assert stop == StopReason.max_steps_reached
        assert [d["confidence"] for d in signal_details(trace)] == [0.9, None]
        assert ctx.last_stop_signals is None
