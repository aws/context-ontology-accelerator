# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the deep-reasoning bounds that keep a large namespace from blowing the answer prompt.

- a planner-supplied row limit is clamped (``bounded_limit``) in the chunk-window tool (the graph
  traversal validates its own limits and bounds its statements on ``main``);
- the answer prompt's document context is fitted to a character budget: unchanged under it,
  documents kept before graph statements over it, the trim traced and logged;
- the question splitter keeps at most 6 sub-questions.
"""

from __future__ import annotations

import json

import pytest
from coa_serve.clients.base import ConverseResult
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.decompose import DecompositionPlanner
from coa_serve.tier3.agentic.models import StopReason, SubQuestion
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.templates import bounded_limit
from coa_serve.tier3.agentic.tools.chunk_tool import ChunkExpansionTool
from coa_serve.tier3.synthesizer import (
    SynthesisResult,
    _prompt_chars,
    fit_chunks_to_budget,
    synth_max_context_chars,
)
from coa_serve.tier3.vector_retriever import ChunkResult
from coa_serve.trace import TraceCollector


class _FakeStore:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    def execute_query_with_retry(self, cypher, params, **_kw):
        self.calls.append((cypher, params))
        return self.rows


_NS = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"


def _chunk(chunk_id: str, text: str = "word " * 10) -> ChunkResult:
    return ChunkResult(chunk_id=chunk_id, text=text, source_doc="doc.txt", relevance_score=0.9, label="L")


# ── planner-supplied row limits ────────────────────────────────────


@pytest.mark.unit
class TestBoundedLimit:
    def test_in_range_is_kept(self):
        assert bounded_limit(40, default=100, ceiling=200) == 40

    def test_above_ceiling_is_clamped(self):
        assert bounded_limit(5000, default=100, ceiling=200) == 200

    def test_below_one_is_raised_to_one(self):
        assert bounded_limit(0, default=100, ceiling=200) == 1

    def test_not_an_int_falls_back_to_default(self):
        assert bounded_limit("lots", default=100, ceiling=200) == 100
        assert bounded_limit(None, default=100, ceiling=200) == 100


@pytest.mark.unit
class TestToolLimitsClamped:
    async def test_chunk_window_limit_clamped(self):
        tool = ChunkExpansionTool("neptune-db://fake-host:8182")
        store = _FakeStore([])
        tool._store = store
        await tool.invoke(namespace=_NS, chunk_ids=["c1"], limit=999)
        assert "LIMIT 100" in store.calls[0][0]


# ── answer-prompt document-context budget ──────────────────────────


@pytest.mark.unit
class TestFitChunksToBudget:
    def test_under_budget_is_unchanged(self):
        chunks = [_chunk("a"), _chunk("b")]
        kept, left_out = fit_chunks_to_budget(chunks, 10_000)
        assert kept is chunks and left_out == 0

    def test_prompt_chars_counts_datamark_markers(self):
        c = ChunkResult(chunk_id="x", text="a b c", source_doc="", relevance_score=1.0, label="")
        # three 1-char words joined by two 8-char markers, plus the label line allowance
        assert _prompt_chars(c) == 3 + 2 * 8 + 20

    def test_documents_kept_before_statements(self):
        doc = _chunk("doc1", "word " * 100)
        stmts = [_chunk(f"e:n#stmt{i}", "fact " * 100) for i in range(5)]
        budget = _prompt_chars(doc) + 2 * _prompt_chars(stmts[0])
        kept, left_out = fit_chunks_to_budget([*stmts, doc], budget)
        assert doc in kept
        assert left_out == 3
        # gathered order is preserved among the kept chunks
        assert kept == [stmts[0], stmts[1], doc]

    def test_oversized_chunk_skipped_smaller_after_it_kept(self):
        small1, big, small2 = _chunk("s1"), _chunk("big", "word " * 5000), _chunk("s2")
        budget = _prompt_chars(small1) + _prompt_chars(small2) + 10
        kept, left_out = fit_chunks_to_budget([small1, big, small2], budget)
        assert kept == [small1, small2] and left_out == 1

    def test_budget_env_override_and_clamp(self, monkeypatch):
        monkeypatch.delenv("TIER3_SYNTH_MAX_CONTEXT_CHARS", raising=False)
        assert synth_max_context_chars() == 600_000
        monkeypatch.setenv("TIER3_SYNTH_MAX_CONTEXT_CHARS", "300000")
        assert synth_max_context_chars() == 300_000
        monkeypatch.setenv("TIER3_SYNTH_MAX_CONTEXT_CHARS", "5")
        assert synth_max_context_chars() == 20_000
        monkeypatch.setenv("TIER3_SYNTH_MAX_CONTEXT_CHARS", "not-a-number")
        assert synth_max_context_chars() == 600_000


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _Decomposer:
    async def decompose(self, query, trace=None):
        return [SubQuestion(text=query)]


class _Controller:
    def __init__(self, chunks):
        self._chunks = chunks

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **_):
        ctx.chunks.extend(self._chunks)
        return StopReason.sufficiency_satisfied


class _Synthesizer:
    def __init__(self):
        self.chunks: list[ChunkResult] = []

    async def synthesize(self, query, chunks, entities, **_kw):
        self.chunks = list(chunks)
        return SynthesisResult(answer="ok", confidence=0.8, duration_ms=1)


def _retriever(chunks, synthesizer):
    return AgenticRetriever(
        registry=object(),
        controller=_Controller(chunks),
        decomposer=_Decomposer(),
        synthesizer=synthesizer,
        budget=AgenticBudgetConfig(),
        clock_source=_Clock(),
        final_semantic_search=False,
    )


@pytest.mark.unit
class TestRetrieverContextBudget:
    async def test_under_budget_no_trim_no_trace(self):
        chunks = [_chunk(f"c{i}") for i in range(5)]
        synth = _Synthesizer()
        trace = TraceCollector()
        result = await _retriever(chunks, synth).resolve("q", "ns", trace=trace)
        assert synth.chunks == chunks
        assert len(result.supporting_content) == 5
        assert not [s for s in trace.steps if s.step == "synthesis_context_capped"]

    async def test_over_budget_trimmed_traced_and_reported(self, monkeypatch):
        monkeypatch.setenv("TIER3_SYNTH_MAX_CONTEXT_CHARS", "20000")
        doc = _chunk("doc", "word " * 1000)
        stmts = [_chunk(f"e:hub#stmt{i}", "fact " * 50) for i in range(500)]
        synth = _Synthesizer()
        trace = TraceCollector()
        result = await _retriever([*stmts, doc], synth).resolve("q", "ns", trace=trace)
        assert doc in synth.chunks
        assert sum(_prompt_chars(c) for c in synth.chunks) <= 20_000
        assert len(result.supporting_content) == len(synth.chunks)
        capped = [s for s in trace.steps if s.step == "synthesis_context_capped"]
        assert len(capped) == 1 and capped[0].status == "capped"
        assert f"of {len(stmts) + 1} chunks" in capped[0].detail


# ── sub-question fan-out ───────────────────────────────────────────


class _PlannerClient:
    def __init__(self, n):
        self._text = json.dumps({"sub_questions": [{"text": f"part {i}"} for i in range(n)]})

    async def converse(self, prompt, **kwargs):
        return ConverseResult(text=self._text, guardrail_blocked=False)


@pytest.mark.unit
class TestSubQuestionCap:
    async def test_four_kept(self):
        subs = await DecompositionPlanner(_PlannerClient(4)).decompose("q")
        assert len(subs) == 4

    async def test_more_than_six_capped_and_traced(self):
        trace = TraceCollector()
        subs = await DecompositionPlanner(_PlannerClient(10)).decompose("q", trace)
        assert [s.text for s in subs] == [f"part {i}" for i in range(6)]
        step = [s for s in trace.steps if s.step == "decompose"][0]
        assert "4 more left out, cap 6" in step.detail
