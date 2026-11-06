# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scripted deep-reasoning scenarios for the stop-signal default-equivalence fixtures (AC-2).

Each scenario drives the REAL :class:`BedrockStepPlanner` and
:class:`ReasoningController` (and, for prefetch, the real
:class:`AgenticRetriever`) over a scripted fake LLM client, with both
stop-signal thresholds at 0 and telemetry off. It returns every planner model
call (system and user prompt bytes plus call kwargs), the planner call count and
the stop reason.

The fixtures under ``fixtures/stop_signals_default/`` were captured with this
driver on ``main`` at ``f3f17f0ba``, before any stop-signal change. A test
asserts that :func:`run_scenario` still returns exactly the captured record, so
the default prompts, call count and stop reason stay byte-equal to main.

API::

    SCENARIOS                      # scenario name -> async builder
    await run_scenario(name, *, planner_kwargs=None, controller_kwargs=None) -> dict
    load_fixture(name) -> dict     # the captured record for ``name``
    FIXTURE_DIR                    # where the fixtures live

``planner_kwargs`` / ``controller_kwargs`` are passed to the planner and
controller constructors, so a caller can inject an explicit default
stop-signal configuration.

The fixtures are main's behaviour, not this branch's: never regenerate them
from a branch that changes the planner. To re-capture, check out the base
commit and write ``json.dumps(await run_scenario(name), indent=2,
sort_keys=True) + "\\n"`` to ``FIXTURE_DIR / f"{name}.json"`` for each name.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from coa_serve.tier3.agentic.budget import AgenticBudgetConfig, BudgetClock
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.models import SubQuestion
from coa_serve.tier3.agentic.planner import BedrockStepPlanner
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.tier3.vector_retriever import ChunkResult
from coa_serve.trace import TraceCollector

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stop_signals_default"

NAMESPACE = "acme-sales"
QUESTION = "What was ACME's 2024 revenue in the EMEA segment, and who owns that account?"

# The recommended per-request options that matter for the planner prompts
# (``defaults.py`` RECOMMENDED_OPTIONS subset): parse retry, guards, single call.
_SINGLE_CALL_OPTIONS = {"singleCallSteps": True, "sufficiencyGuards": True, "plannerParseRetry": True}


def _candidates(*tools: tuple[str, dict]) -> str:
    return json.dumps(
        {
            "candidates": [
                {"tool_name": name, "inputs": inputs, "reasoning": f"use {name}", "is_ad_hoc": False}
                for name, inputs in tools
            ]
        }
    )


_ENOUGH = '{"candidates": []}'
_UNPARSEABLE = '{"candidates": [{"tool_name": "strategy:entity_based", "inputs": {"query": "ACME'


def _assessment(sufficient: bool, answer: str) -> str:
    return json.dumps({"sufficient": sufficient, "working_answer": answer})


class ScriptedLLM:
    """Fake planner :class:`LLMClient`: returns scripted replies and records every call."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def converse(self, *, prompt: str, system: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"system": system, "prompt": prompt, **{k: kwargs[k] for k in sorted(kwargs)}})
        if not self._replies:
            raise AssertionError(f"unscripted planner call #{len(self.calls)}")
        return SimpleNamespace(text=self._replies.pop(0), guardrail_blocked=False)


class _Tool:
    """A deterministic tool returning fixed chunks and/or rows."""

    def __init__(self, name: str, *, chunks: tuple = (), rows: tuple = (), schema: dict | None = None) -> None:
        self.name = name
        self.description = f"{name} retrieval tool"
        self.input_schema = schema if schema is not None else {"query": "str"}
        self.output_schema: dict = {}
        self._chunks = chunks
        self._rows = rows

    async def invoke(self, *, namespace: str, **_: Any) -> ToolResult:
        return ToolResult(chunks=self._chunks, rows=self._rows, detail=f"{self.name}: {len(self._chunks)} chunk(s)")


def _chunk(cid: str, text: str) -> ChunkResult:
    return ChunkResult(chunk_id=cid, text=text, source_doc=f"s3://bucket/docs/{cid}.txt", relevance_score=0.9)


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        _Tool(
            "strategy:entity_based",
            chunks=(_chunk("c-1", "ACME EMEA segment revenue for fiscal 2024 was $41.2M, up 8% year over year."),),
        )
    )
    reg.register(
        _Tool(
            "strategy:chunk_based_semantic",
            chunks=(_chunk("c-2", "The ACME EMEA account is owned by Dana Whitfield, regional director."),),
        )
    )
    reg.register(_Tool("nl_to_sql", rows=({"segment": "EMEA", "year": 2024, "revenue_usd": 41200000},)))
    return reg


class _Clock:
    """A frozen clock, so no budget path depends on wall time."""

    def __call__(self) -> float:
        return 1000.0


def _budget() -> AgenticBudgetConfig:
    return AgenticBudgetConfig(max_steps=6, time_budget_s=120.0)


def _build(llm: ScriptedLLM, planner_kwargs: dict | None, controller_kwargs: dict | None) -> ReasoningController:
    planner = BedrockStepPlanner(llm, max_tokens=1024, **(planner_kwargs or {}))
    return ReasoningController(planner, per_tool_timeout_s=30.0, **(controller_kwargs or {}))


async def _run_loop(llm: ScriptedLLM, ctx: AccumulatedContext, controller: ReasoningController) -> str:
    clock = BudgetClock(_budget(), clock=_Clock())
    clock.started()
    stop = await controller.run_one_loop(
        SubQuestion(text=QUESTION),
        ctx,
        _registry(),
        clock,
        deque(),
        TraceCollector(),
        namespace=NAMESPACE,
        tool_context={"profile": {}, "options": dict(_SINGLE_CALL_OPTIONS), "model_id": None},
    )
    return str(stop)


async def _single_call(planner_kwargs: dict | None, controller_kwargs: dict | None) -> tuple[ScriptedLLM, str]:
    """Single-call path: unparseable reply + repair turn, a tool step, then ``[]`` (enough)."""
    llm = ScriptedLLM(
        [
            _UNPARSEABLE,
            _candidates(("strategy:entity_based", {"query": "ACME EMEA 2024 revenue"})),
            _candidates(("strategy:chunk_based_semantic", {"query": "ACME EMEA account owner"})),
            _ENOUGH,
        ]
    )
    ctx = AccumulatedContext()
    ctx.answer_language = "en"
    ctx.planner_parse_retry = True
    ctx.sufficiency_guards = True
    ctx.single_call_steps = True
    stop = await _run_loop(llm, ctx, _build(llm, planner_kwargs, controller_kwargs))
    return llm, stop


async def _assess_loop(planner_kwargs: dict | None, controller_kwargs: dict | None) -> tuple[ScriptedLLM, str]:
    """Assess loop (``single_call_steps=False``): propose + assess per step, sufficient on step 2."""
    llm = ScriptedLLM(
        [
            _candidates(("strategy:entity_based", {"query": "ACME EMEA 2024 revenue"})),
            _assessment(False, "EMEA 2024 revenue was $41.2M; the account owner is not yet known."),
            _candidates(("strategy:chunk_based_semantic", {"query": "ACME EMEA account owner"})),
            _assessment(True, "EMEA 2024 revenue was $41.2M; the account is owned by Dana Whitfield."),
        ]
    )
    ctx = AccumulatedContext()
    ctx.answer_language = "en"
    ctx.planner_parse_retry = True
    ctx.sufficiency_guards = True
    ctx.single_call_steps = False
    stop = await _run_loop(llm, ctx, _build(llm, planner_kwargs, controller_kwargs))
    return llm, stop


class _Decomposer:
    async def decompose_with_language(self, query: str, trace: Any = None) -> tuple[list[SubQuestion], str]:
        return [SubQuestion(text=query)], "en"


class _Synth:
    async def synthesize(self, query: str, chunks: Any, entities: Any, **_: Any) -> SynthesisResult:
        return SynthesisResult(answer="final answer", confidence=0.8, duration_ms=1)


async def _prefetch(
    replies: list[str], planner_kwargs: dict | None, controller_kwargs: dict | None
) -> tuple[ScriptedLLM, str]:
    llm = ScriptedLLM(replies)
    retriever = AgenticRetriever(
        _registry(),
        _build(llm, planner_kwargs, controller_kwargs),
        _Decomposer(),
        _Synth(),
        _budget(),
        clock_source=_Clock(),
        final_semantic_search=False,
    )
    trace = TraceCollector()
    await retriever.resolve(
        QUESTION,
        NAMESPACE,
        trace=trace,
        options={"prefetchMode": "assess", **_SINGLE_CALL_OPTIONS},
    )
    stops = [s.detail for s in trace.steps if s.step == "agentic_retrieval_stop"]
    return llm, stops[-1]


async def _prefetch_sufficient(planner_kwargs: dict | None, controller_kwargs: dict | None) -> tuple[ScriptedLLM, str]:
    """Prefetch assess says sufficient: the loop is skipped."""
    return await _prefetch(
        [_assessment(True, "EMEA 2024 revenue was $41.2M.")],
        planner_kwargs,
        controller_kwargs,
    )


async def _prefetch_then_loop(planner_kwargs: dict | None, controller_kwargs: dict | None) -> tuple[ScriptedLLM, str]:
    """Prefetch assess says not sufficient: the single-call loop runs one tool, then ``[]``."""
    return await _prefetch(
        [
            _assessment(False, "EMEA 2024 revenue was $41.2M; the account owner is not yet known."),
            _candidates(("strategy:chunk_based_semantic", {"query": "ACME EMEA account owner"})),
            _ENOUGH,
        ],
        planner_kwargs,
        controller_kwargs,
    )


SCENARIOS = {
    "single_call": _single_call,
    "assess_loop": _assess_loop,
    "prefetch_sufficient": _prefetch_sufficient,
    "prefetch_then_loop": _prefetch_then_loop,
}


async def run_scenario(
    name: str, *, planner_kwargs: dict | None = None, controller_kwargs: dict | None = None
) -> dict[str, Any]:
    """Run scenario ``name`` and return its record: every planner call, the call count and the stop reason."""
    llm, stop = await SCENARIOS[name](planner_kwargs, controller_kwargs)
    return {"scenario": name, "calls": llm.calls, "call_count": len(llm.calls), "stop_reason": stop}


def load_fixture(name: str) -> dict[str, Any]:
    """The record captured on main for scenario ``name``."""
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
