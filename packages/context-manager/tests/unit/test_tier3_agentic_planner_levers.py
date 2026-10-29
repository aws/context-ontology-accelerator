# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deep-reasoning planner levers (all per-request, all off by default).

``tokenCaps`` (planner / synthesizer) and ``plannerParseRetry``: default calls are
unchanged; each option changes exactly its own piece.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from coa_serve.models import InvokeRequest
from coa_serve.request_caps import token_cap
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.models import SubQuestion
from coa_serve.tier3.agentic.planner import BedrockStepPlanner
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.synthesizer import SynthesisResult, Synthesizer

pytestmark = pytest.mark.unit

NS = "acme"


# ── planner token cap + parse retry ────────────────────────────────


class _LLM:
    def __init__(self, texts) -> None:
        self.texts, self.calls = list(texts), []

    async def converse(self, *, prompt, system=None, max_tokens=None, **_):
        self.calls.append({"prompt": prompt, "max_tokens": max_tokens})
        return SimpleNamespace(text=self.texts.pop(0), guardrail_blocked=False)


_SPECS = [{"name": "strategy:x", "description": "d", "input_schema": {}, "output_schema": {}}]
_GOOD = '{"candidates": [{"tool_name": "strategy:x", "inputs": {}, "reasoning": "r"}]}'


class TestPlanner:
    @pytest.mark.asyncio
    async def test_default_no_retry_and_default_cap(self):
        llm = _LLM(['{"candid'])
        out = await BedrockStepPlanner(llm).propose_steps(
            sub_question=SubQuestion(text="q"), context=AccumulatedContext(), tool_specs=_SPECS
        )
        assert out is None and len(llm.calls) == 1 and llm.calls[0]["max_tokens"] == 1024

    @pytest.mark.asyncio
    async def test_retry_once_and_cap_from_context(self):
        llm = _LLM(['{"candid', _GOOD])
        ctx = AccumulatedContext(planner_max_tokens=6000, planner_parse_retry=True)
        out = await BedrockStepPlanner(llm).propose_steps(
            sub_question=SubQuestion(text="q"), context=ctx, tool_specs=_SPECS
        )
        assert [d.tool_name for d in out] == ["strategy:x"]
        assert len(llm.calls) == 2 and "could not be parsed" in llm.calls[1]["prompt"]
        assert llm.calls[0]["max_tokens"] == 6000 and llm.calls[1]["max_tokens"] == 6000

    @pytest.mark.asyncio
    async def test_no_retry_for_a_parseable_empty_list(self):
        llm = _LLM(['{"candidates": []}'])
        ctx = AccumulatedContext(planner_parse_retry=True)
        await BedrockStepPlanner(llm).propose_steps(sub_question=SubQuestion(text="q"), context=ctx, tool_specs=_SPECS)
        assert len(llm.calls) == 1

    @pytest.mark.asyncio
    async def test_assess_uses_cap(self):
        llm = _LLM(['{"sufficient": true, "working_answer": "a"}'])
        await BedrockStepPlanner(llm).assess(
            sub_question=SubQuestion(text="q"), context=AccumulatedContext(planner_max_tokens=3000)
        )
        assert llm.calls[0]["max_tokens"] == 3000


# ── retriever wiring ───────────────────────────────────────────────


class _Ctl:
    def __init__(self) -> None:
        self.seen = []

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
        self.seen.append((ctx.planner_max_tokens, ctx.planner_parse_retry, dict(kw.get("tool_context") or {})))
        return None


class _Dec:
    async def decompose_with_language(self, query, trace=None):
        return [SubQuestion(text="decomposed sub-question")], "en"


class _Synth:
    def __init__(self) -> None:
        self.kwargs = []

    async def synthesize(self, query, chunks, entities, **kw):
        self.kwargs.append(kw)
        return SynthesisResult(answer="ok", confidence=0.8, duration_ms=1)


class TestRetrieverWiring:
    @pytest.mark.asyncio
    async def test_default_and_on(self):
        ctl, synth = _Ctl(), _Synth()
        r = AgenticRetriever(
            ToolRegistry(), ctl, _Dec(), synth, AgenticBudgetConfig(max_steps=2), final_semantic_search=False
        )
        await r.resolve("the original question", NS)
        await r.resolve(
            "the original question",
            NS,
            options={"tokenCaps": {"planner": 5000, "synthesizer": 12000}, "plannerParseRetry": True},
        )
        (p0, r0, _), (p1, r1, _) = ctl.seen
        assert (p0, r0) == (None, False)
        assert (p1, r1) == (5000, True)
        assert "max_tokens" not in synth.kwargs[0] and synth.kwargs[1]["max_tokens"] == 12000


class TestSynthesizerCap:
    @pytest.mark.asyncio
    async def test_cap(self):
        llm = AsyncMock()
        llm.converse.return_value = SimpleNamespace(text="answer [CONFIDENCE: 0.8]", guardrail_blocked=False)
        syn = Synthesizer(llm, "", max_prompt_chunks=None)
        await syn.synthesize("q", [], [])
        assert llm.converse.call_args.kwargs["max_tokens"] == 4096
        await syn.synthesize("q", [], [], max_tokens=9000)
        assert llm.converse.call_args.kwargs["max_tokens"] == 9000


def test_token_cap_and_validation():
    assert token_cap({}, "planner") is None
    assert token_cap({"tokenCaps": {"planner": 10}}, "planner") == 1024
    assert token_cap({"tokenCaps": {"planner": 99_999}}, "planner") == 16000
    assert token_cap({"tokenCaps": {"planner": True}}, "planner") is None
    ok = {"tokenCaps": {"planner": 4000, "synthesizer": 8000}, "plannerParseRetry": True}
    assert InvokeRequest(query="q", namespace=NS, options=ok).options == ok
    for bad in (
        {"tokenCaps": {"other": 1}},
        {"tokenCaps": {"planner": "1"}},
        {"tokenCaps": 5},
        {"plannerParseRetry": 1},
    ):
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options=bad)
