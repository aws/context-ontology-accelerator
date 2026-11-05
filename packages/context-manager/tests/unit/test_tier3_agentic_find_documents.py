# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for entity routing (``options.entityRouting``).

Covers the find_documents tool (graph + keyword channels, merged document
ranking, namespace scoping, degradation), the keyword search on the OpenSearch
client, the graph mention query, the context/controller plumbing (shared
matches, sufficiency deferral), anchoring a read on a mention chunk, term
extraction in the decomposition call, the retriever's opt-in wiring and the
planner guidance. Everything off by default.
"""

from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace

import pytest
from coa_serve.models import InvokeRequest
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.budget import BudgetClock
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.decompose import _SYSTEM_INSTRUCTION, DecompositionPlanner
from coa_serve.tier3.agentic.models import PlannerDecision, SubQuestion
from coa_serve.tier3.agentic.planner import BedrockStepPlanner
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools import graph_tool as gt
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.tier3.agentic.tools.document_tool import ReadDocumentTool, document_ref
from coa_serve.tier3.agentic.tools.find_documents_tool import FindDocumentsTool, build_find_documents_tool
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.synthesizer import SynthesisResult
from coa_serve.tier3.vector_retriever import ChunkResult
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit

NS = "acme"
BUCKET = "corp-docs"
SOURCES = [{"sourceType": "DOCUMENTS", "sourceBucketArn": f"arn:aws:s3:::{BUCKET}", "s3Prefixes": ["docs/"]}]


class FakeRegistry:
    def __init__(self, sources=SOURCES) -> None:
        self._sources = sources
        self.available = True

    async def document_sources(self, namespace):
        return self._sources


class FakeGraph:
    def __init__(self, rows=None, raises=None) -> None:
        self.rows, self.raises, self.calls = rows or [], raises, []

    async def find_entity_mentions(self, namespace, terms, *, entity_class=""):
        self.calls.append((namespace, list(terms), entity_class))
        if self.raises:
            raise self.raises
        return self.rows


class FakeVector:
    def __init__(self, hits=None, raises=None) -> None:
        self.hits, self.raises, self.calls = hits or [], raises, []

    async def phrase_search(self, phrases, *, index, size=50):
        self.calls.append((list(phrases), index, size))
        if self.raises:
            raise self.raises
        return self.hits


def _row(term, key, chunk, text="mention text", quality=2):
    return {
        "term": term,
        "quality": quality,
        "label": term,
        "class": "Ticket",
        "chunk": chunk,
        "text": text,
        "source": "aws:s",
        "key": key,
    }


def _hit(key, chunk, matched, text="raw chunk text", score=3.0):
    return {
        "chunk_id": chunk,
        "source_id": "aws:s",
        "source_s3_key": key,
        "text": text,
        "score": score,
        "matched": matched,
    }


def _tool(graph=None, vector=None, sources=SOURCES):
    return FindDocumentsTool(
        FakeRegistry(sources), graph_tool=graph, vector_client=vector, chunk_index_for=lambda ns: f"chunk_{ns}"
    )


# ── find_documents ─────────────────────────────────────────────────


class TestFindDocuments:
    @pytest.mark.asyncio
    async def test_ranks_by_distinct_terms_then_mentions(self):
        graph = FakeGraph(
            [
                _row("Dispute 17601", "docs/a.txt", "c1"),
                _row("Dispute 17601", "docs/b.txt", "c2"),
                _row("Dispute 17601", "docs/b.txt", "c3"),
            ]
        )
        vector = FakeVector([_hit("docs/a.txt", "c4", [1])])  # a.txt also matches term 1
        tool = _tool(graph, vector)
        result = await tool.invoke(namespace=NS, terms=["Dispute 17601", "chargeback form"])
        keys = [m["source_key"] for m in result.items]
        assert keys == ["docs/a.txt", "docs/b.txt"]  # 2 terms beats 2 mentions of 1 term
        top = result.items[0]
        assert top["matched_terms"] == ["Dispute 17601", "chargeback form"] and top["mentions"] == 2
        assert top["entity_match"] == "exact" and top["snippets"][0]["chunk_id"] in ("c1", "c4")
        assert graph.calls == [(NS, ["Dispute 17601", "chargeback form"], "")]
        assert vector.calls[0][1] == "chunk_acme"
        assert "1. docs/a.txt" in result.detail

    @pytest.mark.asyncio
    async def test_returns_raw_mentions_as_evidence_routable_to_the_document(self):
        tool = _tool(FakeGraph([_row("ACME-42", "docs/a.txt", "c1", text="Ticket ACME-42 was closed on Nov 30.")]))
        result = await tool.invoke(namespace=NS, terms=["ACME-42"])
        chunk = result.chunks[0]
        assert chunk.chunk_id == "c1" and chunk.text.startswith("Ticket ACME-42")
        assert chunk.uri == f"s3://{BUCKET}/docs/a.txt" and document_ref(chunk) == "docs/a.txt"
        assert "ACME-42" in result.items[0]["snippets"][0]["snippet"]

    @pytest.mark.asyncio
    async def test_drops_documents_outside_namespace_sources(self):
        tool = _tool(FakeGraph([_row("x", "other/secret.txt", "c1"), _row("x", "docs/ok.txt", "c2")]))
        result = await tool.invoke(namespace=NS, terms=["x1"])
        assert [m["source_key"] for m in result.items] == ["docs/ok.txt"]

    @pytest.mark.asyncio
    async def test_refuses_without_source_inventory(self):
        result = await _tool(FakeGraph([_row("x", "docs/a.txt", "c1")]), sources=None).invoke(
            namespace=NS, terms=["xy"]
        )
        assert not result.items and "refused" in result.detail

    @pytest.mark.asyncio
    async def test_channel_failure_degrades_to_other_channel(self):
        tool = _tool(FakeGraph(raises=TimeoutError()), FakeVector([_hit("docs/a.txt", "c1", [0])]))
        result = await tool.invoke(namespace=NS, terms=["alpha"])
        assert [m["source_key"] for m in result.items] == ["docs/a.txt"]
        assert "graph search failed" in result.detail

    @pytest.mark.asyncio
    async def test_terms_are_cleaned_and_bounded(self):
        graph = FakeGraph()
        tool = _tool(graph)
        result = await tool.invoke(namespace=NS, terms=["", "a", " 'Quoted' ", "quoted", *[f"t{i}" for i in range(20)]])
        assert graph.calls[0][1][0] == "Quoted" and len(graph.calls[0][1]) == 8
        assert not result.items and "no document mentions" in result.detail
        assert "no usable terms" in (await tool.invoke(namespace=NS, terms=[])).detail

    @pytest.mark.asyncio
    async def test_class_filter_and_top_k(self):
        rows = [_row("x", f"docs/{i}.txt", f"c{i}") for i in range(8)]
        graph = FakeGraph(rows)
        result = await _tool(graph).invoke(namespace=NS, terms=["xx"], entity_class="Ticket", top_k=3)
        assert graph.calls[0][2] == "Ticket" and len(result.items) == 3

    def test_builder_needs_a_channel(self):
        reg = ToolRegistry()
        assert build_find_documents_tool(reg, FakeRegistry()) is None and reg.names() == []
        assert build_find_documents_tool(reg, FakeRegistry(), vector_client=FakeVector()) == "find_documents"


class TestChannels:
    @pytest.mark.asyncio
    async def test_phrase_search_query_and_parsing(self, monkeypatch):
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        client = OpenSearchVectorClient.__new__(OpenSearchVectorClient)
        captured = {}

        def search_raw(index, body):
            captured.update(index=index, body=body)
            return {
                "hits": {
                    "hits": [
                        {
                            "_id": "os1",
                            "_score": 2.5,
                            "matched_queries": ["p1"],
                            "_source": {
                                "value": "raw text",
                                "metadata": {
                                    "chunk": {"chunkId": "aws:c1"},
                                    "source": {"sourceId": "aws:s1", "metadata": {"source_s3_key": "docs/a.txt"}},
                                },
                            },
                        }
                    ]
                }
            }

        async def run(fn, *a, **k):
            return fn(*a, **k)

        client._aoss = SimpleNamespace(search_raw=search_raw)
        monkeypatch.setattr(client, "_run", run)
        hits = await client.phrase_search(["alpha", "beta gamma", " "], index="chunk_x", size=50)
        should = captured["body"]["query"]["bool"]["should"]
        assert captured["index"] == "chunk_x" and len(should) == 2
        assert should[1] == {"match_phrase": {"value": {"query": "beta gamma", "_name": "p1"}}}
        assert hits == [
            {
                "chunk_id": "aws:c1",
                "source_id": "aws:s1",
                "source_s3_key": "docs/a.txt",
                "text": "raw text",
                "score": 2.5,
                "matched": [1],
            }
        ]
        assert await client.phrase_search([" "], index="chunk_x") == []

    @pytest.mark.asyncio
    async def test_graph_mention_query(self, monkeypatch):
        tool = gt.GraphTraversalTool("neptune-db://host:8182")
        captured = {}

        async def fake_run(cypher, params):
            captured.update(cypher=cypher, params=params)
            return [{"term": "dispute 17601", "quality": 2, "chunk": "c1", "key": "docs/a.txt"}]

        monkeypatch.setattr(tool, "_run_cypher", fake_run)
        monkeypatch.setattr(gt.GraphTraversalTool, "_format_label", staticmethod(lambda ns, label: f"`{label}{ns}`"))
        rows = await tool.find_entity_mentions(NS, ["Dispute 17601"], entity_class="Ticket")
        assert "`__Entity__acme`" in captured["cypher"] and "__EXTRACTED_FROM__" in captured["cypher"]
        assert captured["params"] == {"terms": ["dispute 17601"], "entity_class": "Ticket"}
        assert rows[0]["term"] == "Dispute 17601"  # mapped back to the caller's spelling
        captured.clear()
        assert await tool.find_entity_mentions(NS, ["", "  "]) == [] and captured == {}


# ── context + controller ───────────────────────────────────────────


def _match(key, rank, chunk="c1", text="mention"):
    return {
        "type": "document_match",
        "source_key": key,
        "matched_terms": ["t"],
        "mentions": 1,
        "snippets": [{"chunk_id": chunk, "snippet": text, "text": text}],
        "rank_key": rank,
    }


class TestContextMatches:
    def test_merge_keeps_best_and_sorts(self):
        ctx = AccumulatedContext()
        ctx.add_document_matches([_match("docs/a.txt", [1, 1, 0, 0]), _match("docs/b.txt", [2, 1, 0, 0])])
        ctx.add_document_matches([_match("docs/a.txt", [3, 1, 0, 0])])
        assert [m["source_key"] for m in ctx.document_matches] == ["docs/a.txt", "docs/b.txt"]
        fork = ctx.fork()
        assert fork.document_matches == ctx.document_matches and fork.chunks == []
        other = AccumulatedContext()
        other.add_document_matches([_match("docs/c.txt", [5, 1, 0, 0])])
        ctx.merge(other)
        assert ctx.document_matches[0]["source_key"] == "docs/c.txt"


class _Tool:
    def __init__(self, name, result) -> None:
        self.name, self.description, self.input_schema, self.output_schema = name, "d", {"terms": "list"}, {}
        self._result, self.calls = result, 0

    async def invoke(self, *, namespace, **_):
        self.calls += 1
        return self._result


class _Planner:
    def __init__(self, rounds) -> None:
        self._rounds = deque(rounds)

    async def propose_steps(self, **_):
        return self._rounds.popleft() if self._rounds else []

    async def check_sufficiency(self, **_):
        return True


def _loop(ctx, tools, rounds, steps=4):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    clock = BudgetClock(AgenticBudgetConfig(max_steps=steps, time_budget_s=60))
    clock.started()
    trace = TraceCollector()
    return ReasoningController(_Planner(rounds)).run_one_loop(
        SubQuestion(text="q"), ctx, reg, clock, deque(), trace, namespace=NS
    ), trace


class TestControllerRouting:
    @pytest.mark.asyncio
    async def test_stores_document_matches_from_results(self):
        ctx = AccumulatedContext()
        find = _Tool(
            "find_documents",
            ToolResult(
                items=(_match("docs/a.txt", [1, 1, 0, 0]),),
                chunks=(ChunkResult(chunk_id="c1", text="t", source_doc="", relevance_score=1),),
            ),
        )
        coro, _ = _loop(ctx, [find], [[PlannerDecision(tool_name="find_documents", inputs={"terms": ["x"]})]], steps=1)
        await coro
        assert ctx.document_matches[0]["source_key"] == "docs/a.txt"


# ── anchored read on a mention ─────────────────────────────────────


class _S3:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def get_object(self, *, Bucket, Key, Range=None):  # noqa: N803
        return {"Body": SimpleNamespace(read=lambda: self.body)}


class TestAnchorOnMention:
    @pytest.mark.asyncio
    async def test_anchor_chunk_id_from_shared_matches(self):
        mention = "Dispute 17601 was resolved in favour of the cardholder on November 30."
        doc = ("filler text " * 2000) + mention + (" more filler" * 1000)
        tool = ReadDocumentTool(FakeRegistry(), s3_client=_S3(doc.encode())).anchored_variant()
        ctx = AccumulatedContext()  # a fork: no chunks, only the shared matches
        ctx.add_document_matches([_match("docs/a.txt", [1, 1, 0, 0], chunk="aws:c9", text=mention)])
        result = await tool.invoke(
            namespace=NS, source_key="docs/a.txt", anchor_chunk_id="aws:c9", max_chars=3000, accumulated_context=ctx
        )
        item = result.items[0]
        assert item["anchor_source"] == "anchor_chunk_id" and item["anchor_method"] == "exact"
        assert item["offset"] < doc.index(mention) < item["end"]

    @pytest.mark.asyncio
    async def test_first_read_auto_anchors_on_matches(self):
        mention = "Thread subject: Quarterly limits review for account 553."
        doc = ("x y z " * 5000) + mention
        tool = ReadDocumentTool(FakeRegistry(), s3_client=_S3(doc.encode())).anchored_variant()
        ctx = AccumulatedContext()
        ctx.add_document_matches([_match("docs/a.txt", [1, 1, 0, 0], text=mention)])
        result = await tool.invoke(namespace=NS, source_key="docs/a.txt", max_chars=2000, accumulated_context=ctx)
        assert result.items[0]["anchor_source"] == "retrieved_chunks"
        assert result.items[0]["end"] >= doc.index(mention)


# ── decomposition term extraction ──────────────────────────────────


class _LLM:
    def __init__(self, payload) -> None:
        self.payload, self.calls = payload, []

    async def converse(self, *, prompt, system=None, **_):
        self.calls.append(system)
        return SimpleNamespace(text=json.dumps(self.payload), guardrail_blocked=False)


class TestDecomposeTerms:
    @pytest.mark.asyncio
    async def test_terms_parsed_in_the_same_call(self):
        llm = _LLM(
            {"sub_questions": [{"text": "q"}], "language": "en", "named_terms": ["Dispute 17601", 3, " ", "Ana"]}
        )
        sqs, lang, terms = await DecompositionPlanner(llm).decompose_with_terms("q")
        assert [s.text for s in sqs] == ["q"] and lang == "en" and terms == ["Dispute 17601", "Ana"]
        assert len(llm.calls) == 1 and "NAMED TERMS" in llm.calls[0]

    @pytest.mark.asyncio
    async def test_default_call_unchanged(self):
        llm = _LLM({"sub_questions": [{"text": "q"}], "language": "en"})
        sqs, lang = await DecompositionPlanner(llm).decompose_with_language("q")
        assert llm.calls == [_SYSTEM_INSTRUCTION] and lang == "en"


# ── retriever wiring ───────────────────────────────────────────────


class _Decomposer:
    def __init__(self, terms) -> None:
        self.terms, self.with_terms = terms, 0

    async def decompose_with_language(self, query, trace=None):
        return [SubQuestion(text=query)], "en"

    async def decompose_with_terms(self, query, trace=None):
        self.with_terms += 1
        return [SubQuestion(text=query)], "en", self.terms


class _Ctl:
    def __init__(self) -> None:
        self.seen = []

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
        self.seen.append((kw.get("excluded_tools"), list(ctx.document_matches)))
        return None


class _Synth:
    def __init__(self) -> None:
        self.chunks = []

    async def synthesize(self, query, chunks, entities, **_):
        self.chunks = list(chunks)
        return SynthesisResult(answer="ok", confidence=0.8, duration_ms=1)


def _retriever(tool, decomposer, ctl, synth):
    reg = ToolRegistry()
    reg.register(tool)
    return AgenticRetriever(
        reg, ctl, decomposer, synth, AgenticBudgetConfig(max_steps=3, time_budget_s=60), final_semantic_search=False
    )


class TestRetrieverWiring:
    @pytest.mark.asyncio
    async def test_default_off(self):
        graph = FakeGraph([_row("ACME-42", "docs/a.txt", "c1")])
        dec, ctl, trace = _Decomposer(["ACME-42"]), _Ctl(), TraceCollector()
        await _retriever(_tool(graph), dec, ctl, _Synth()).resolve("q", NS, trace=trace)
        excluded, matches = ctl.seen[0]
        assert "find_documents" in excluded and matches == []
        assert dec.with_terms == 0 and graph.calls == []
        assert not [s for s in trace.steps if s.step == "find_documents_preload"]

    @pytest.mark.asyncio
    async def test_on_routes_terms_up_front(self):
        graph = FakeGraph([_row("ACME-42", "docs/a.txt", "c1", text="Ticket ACME-42 closed")])
        dec, ctl, synth, trace = _Decomposer(["ACME-42"]), _Ctl(), _Synth(), TraceCollector()
        await _retriever(_tool(graph), dec, ctl, synth).resolve("q", NS, trace=trace, options={"entityRouting": True})
        excluded, matches = ctl.seen[0]
        assert "find_documents" not in excluded
        assert matches[0]["source_key"] == "docs/a.txt"
        assert graph.calls[0][1] == ["ACME-42"]
        assert any(c.chunk_id == "c1" for c in synth.chunks)  # the raw mention reaches synthesis
        assert [s.status for s in trace.steps if s.step == "find_documents_preload"] == ["success"]

    @pytest.mark.asyncio
    async def test_on_without_terms_skips_preload(self):
        graph = FakeGraph()
        trace = TraceCollector()
        await _retriever(_tool(graph), _Decomposer([]), _Ctl(), _Synth()).resolve(
            "q", NS, trace=trace, options={"entityRouting": True}
        )
        assert graph.calls == []
        assert [s.status for s in trace.steps if s.step == "find_documents_preload"] == ["skipped"]

    @pytest.mark.asyncio
    async def test_loop_fixes_drop_privacy_mask_placeholders(self):
        graph = FakeGraph([_row("ACME-42", "docs/a.txt", "c1")])
        retriever = _retriever(_tool(graph), _Decomposer(["{NAME}", "ACME-42"]), _Ctl(), _Synth())
        await retriever.resolve("q", NS, options={"entityRouting": True})
        routed_without = graph.calls[-1][1]
        await retriever.resolve("q", NS, options={"entityRouting": True, "loopFixes": True})
        assert "{NAME}" in routed_without
        assert graph.calls[-1][1] == ["ACME-42"]


# ── planner guidance ───────────────────────────────────────────────


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = []

    async def converse(self, *, prompt, system=None, **_):
        self.calls.append({"prompt": prompt, "system": system})
        return SimpleNamespace(text='{"candidates": []}', guardrail_blocked=False)


_BASE = [{"name": "strategy:entity_based", "description": "d", "input_schema": {}, "output_schema": {}}]
_FIND = {"name": "find_documents", "description": "d", "input_schema": {"terms": "list"}, "output_schema": {}}


class TestPlannerGuidance:
    @pytest.mark.asyncio
    async def test_only_when_offered(self):
        ctx = AccumulatedContext()
        ctx.add_document_matches([_match("docs/a.txt", [1, 1, 0, 0], chunk="aws:c1", text="Ticket ACME-42")])
        off, on = _FakeLLM(), _FakeLLM()
        await BedrockStepPlanner(off).propose_steps(sub_question=SubQuestion(text="q"), context=ctx, tool_specs=_BASE)
        await BedrockStepPlanner(on).propose_steps(
            sub_question=SubQuestion(text="q"), context=ctx, tool_specs=[*_BASE, _FIND]
        )
        assert "ROUTE NAMED THINGS" not in off.calls[0]["system"]
        assert "Documents matching the named terms" not in off.calls[0]["prompt"]
        assert "ROUTE NAMED THINGS" in on.calls[0]["system"]
        prompt = on.calls[0]["prompt"]
        assert "source_key=docs/a.txt" in prompt and "mention chunk id=aws:c1" in prompt and "not read yet" in prompt


def test_option_validation():
    assert InvokeRequest(query="q", namespace=NS, options={"entityRouting": True}).options
    with pytest.raises(ValueError):
        InvokeRequest(query="q", namespace=NS, options={"entityRouting": "yes"})
