# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Tier 3 knowledge retriever orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.clients.base import ConverseResult, VectorHit
from coa_serve.tier3.graph_traverser import GraphTraverser
from coa_serve.tier3.knowledge_retriever import (
    KnowledgeRetriever,
    Tier3Result,
)
from coa_serve.tier3.synthesizer import Synthesizer
from coa_serve.tier3.vector_retriever import VectorRetriever

# --- Fixtures ---


@pytest.fixture
def mock_bedrock():
    client = AsyncMock()
    client.embed.return_value = [0.1] * 1024
    client.converse.return_value = ConverseResult(
        text="The claims SLA is 10 business days per Policy v2.3.\n\n[CONFIDENCE: 0.85]"
    )
    return client


@pytest.fixture
def mock_opensearch():
    client = AsyncMock()
    client.search.return_value = [
        VectorHit(
            id="c1",
            text="Claims processing SLA is 10 business days",
            score=0.92,
            metadata={
                "source_doc": "policy.pdf",
                "label": "Policy v2.3",
                "type": "document",
            },
        ),
    ]
    return client


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = [
        {
            "entity": "http://ex.org/Claim",
            "label": "Claim",
            "type": "owl:Class",
            "predicate": "",
            "related": "",
            "relLabel": "",
        },
    ]
    return client


@pytest.fixture
def vector_retriever(mock_opensearch):
    return VectorRetriever(mock_opensearch)


@pytest.fixture
def graph_traverser(mock_neptune):
    return GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")


@pytest.fixture
def synthesizer(mock_bedrock):
    return Synthesizer(mock_bedrock, guardrail_id="gr-test-123")


# --- KnowledgeRetriever full flow ---


@pytest.mark.unit
class TestKnowledgeRetrieverFullFlow:
    async def test_full_flow_with_embedding(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve(
            "What is the claims processing SLA?",
            "550e8400-e29b-41d4-a716-446655440000",
            embedding=[0.1] * 1024,
        )

        assert isinstance(result, Tier3Result)
        assert "claims SLA is 10 business days" in result.synthesized_answer
        assert result.confidence == 0.85
        assert len(result.supporting_content) == 1
        assert result.supporting_content[0]["chunkId"] == "c1"
        assert len(result.graph_context) == 1

    async def test_trace_steps_present(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("SLA?", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        step_names = [s["step"] for s in result.trace_steps]
        assert "t3.vector_search" in step_names
        assert "t3.graph_traverse" in step_names
        assert "t3.synthesize" in step_names

    async def test_guardrail_step_emitted_on_success_when_guardrail_configured(
        self, vector_retriever, graph_traverser, synthesizer
    ):
        """A t3.guardrail step is emitted so the UI can render 'passed' — not 'n/a'."""
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("SLA?", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        guardrail_step = next(s for s in result.trace_steps if s["step"] == "t3.guardrail")
        assert guardrail_step["status"] == "success"

    async def test_guardrail_step_skipped_when_no_guardrail_id(self, vector_retriever, graph_traverser, mock_bedrock):
        """No guardrail configured → no t3.guardrail step (UI correctly renders 'n/a')."""
        import os

        # Bypass the Synthesizer constructor's non-empty guardrail_id assertion for local envs.
        os.environ["ALLOW_NO_GUARDRAIL"] = "true"
        try:
            synthesizer_no_guard = Synthesizer(mock_bedrock, guardrail_id="")
        finally:
            os.environ.pop("ALLOW_NO_GUARDRAIL", None)

        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer_no_guard)
        result = await retriever.resolve("SLA?", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        assert not any(s["step"] == "t3.guardrail" for s in result.trace_steps)

    async def test_guardrail_step_denied_when_blocked(self, vector_retriever, graph_traverser, mock_bedrock):
        """A blocked synthesis emits a t3.guardrail step with status=denied."""
        mock_bedrock.converse.return_value = ConverseResult(text="Blocked", guardrail_blocked=True)
        synthesizer_blocked = Synthesizer(mock_bedrock, guardrail_id="gr-test-123")
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer_blocked)

        result = await retriever.resolve("SLA?", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        guardrail_step = next(s for s in result.trace_steps if s["step"] == "t3.guardrail")
        assert guardrail_step["status"] == "denied"

    async def test_guardrail_step_emitted_on_lexical_path(self, synthesizer):
        """The lexical resolve path emits the t3.guardrail step too, not just the graph path."""
        from coa_serve.lexical.baseline_retriever import BaselineResult
        from coa_serve.lexical.strategies import RetrieverStrategy

        lexical = AsyncMock()
        lexical.retrieve.return_value = BaselineResult(chunks=(), entities=(), trace_steps=())
        retriever = KnowledgeRetriever(None, None, synthesizer, lexical_retriever=lexical)

        result = await retriever.resolve(
            "SLA?",
            "550e8400-e29b-41d4-a716-446655440000",
            retriever_strategy=RetrieverStrategy.CHUNK_BASED_SEMANTIC,
        )

        guardrail_step = next(s for s in result.trace_steps if s["step"] == "t3.guardrail")
        assert guardrail_step["status"] == "success"

    async def test_per_leg_timing(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("SLA?", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        vec_step = next(s for s in result.trace_steps if s["step"] == "t3.vector_search")
        graph_step = next(s for s in result.trace_steps if s["step"] == "t3.graph_traverse")
        assert "durationMs" in vec_step
        assert "durationMs" in graph_step


@pytest.mark.unit
class TestKnowledgeRetrieverGraphBranch:
    """Change 1 (Cause A) regression guard: resolve() must seed graph traversal
    via the language-agnostic URI-hop branch when entity_uris are supplied, and
    fall back to the ASCII keyword branch otherwise. The graph_traverse trace
    self-documents which branch ran (graphSeedMode=="uri_hop"/"keyword")."""

    async def test_uri_hop_branch_when_entity_uris_present(self, vector_retriever, graph_traverser, synthesizer):
        graph_traverser.traverse = AsyncMock(return_value=[])
        graph_traverser.traverse_from_uris = AsyncMock(return_value=[])
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)

        result = await retriever.resolve(
            "契約と請求の関係は？",
            "550e8400-e29b-41d4-a716-446655440000",
            embedding=[0.1] * 10,
            entity_uris=["http://ex.org/Claim", "http://ex.org/Policy"],
        )

        graph_traverser.traverse_from_uris.assert_awaited_once()
        graph_traverser.traverse.assert_not_awaited()
        graph_step = next(s for s in result.trace_steps if s["step"] == "t3.graph_traverse")
        assert graph_step["graphSeedMode"] == "uri_hop"

    async def test_keyword_branch_when_no_entity_uris(self, vector_retriever, graph_traverser, synthesizer):
        graph_traverser.traverse = AsyncMock(return_value=[])
        graph_traverser.traverse_from_uris = AsyncMock(return_value=[])
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)

        result = await retriever.resolve(
            "What is the SLA?",
            "550e8400-e29b-41d4-a716-446655440000",
            embedding=[0.1] * 10,
        )

        graph_traverser.traverse.assert_awaited_once()
        graph_traverser.traverse_from_uris.assert_not_awaited()
        graph_step = next(s for s in result.trace_steps if s["step"] == "t3.graph_traverse")
        assert graph_step["graphSeedMode"] == "keyword"


@pytest.mark.unit
class TestKnowledgeRetrieverTraversalStrategy:
    async def test_keyword_strategy_without_uris(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve(
            "What about claims?",
            "550e8400-e29b-41d4-a716-446655440000",
            embedding=[0.1] * 10,
        )
        graph_step = next(s for s in result.trace_steps if s["step"] == "t3.graph_traverse")
        assert graph_step["graphSeedMode"] == "keyword"

    async def test_uri_hop_strategy_with_uris(self, vector_retriever, graph_traverser, synthesizer, mock_neptune):
        mock_neptune.query.return_value = []
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve(
            "What about claims?",
            "550e8400-e29b-41d4-a716-446655440000",
            embedding=[0.1] * 10,
            entity_uris=["http://ex.org/Claim"],
        )
        graph_step = next(s for s in result.trace_steps if s["step"] == "t3.graph_traverse")
        assert graph_step["graphSeedMode"] == "uri_hop"


@pytest.mark.unit
class TestKnowledgeRetrieverNoEmbedding:
    async def test_skips_vector_search_without_embedding(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("test query", "550e8400-e29b-41d4-a716-446655440000")

        vec_step = next(s for s in result.trace_steps if s["step"] == "t3.vector_search")
        assert vec_step["status"] == "skipped"
        assert vec_step["detail"] == "no_embedding"
        # Should still have graph and synthesis
        assert any(s["step"] == "t3.graph_traverse" for s in result.trace_steps)
        assert any(s["step"] == "t3.synthesize" for s in result.trace_steps)

    async def test_no_embedding_still_synthesizes(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000")
        assert result.synthesized_answer is not None


@pytest.mark.unit
class TestKnowledgeRetrieverCatalogSummary:
    async def test_catalog_summary_traced(self, vector_retriever, graph_traverser, synthesizer):
        catalog = [
            {"name": "revenue", "description": "Total revenue", "dimensions": ["month"]},
            {"name": "orders", "description": "Order count", "dimensions": ["region"]},
        ]
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve(
            "What is revenue?",
            "finance",
            embedding=[0.1] * 10,
            catalog_summary=catalog,
        )

        catalog_step = next(s for s in result.trace_steps if s["step"] == "t3.catalog_context")
        assert catalog_step["status"] == "success"
        assert "2 metrics" in catalog_step["detail"]

    async def test_no_catalog_summary_no_trace(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        step_names = [s["step"] for s in result.trace_steps]
        assert "t3.catalog_context" not in step_names


@pytest.mark.unit
class TestKnowledgeRetrieverPartialFailure:
    async def test_vector_failure_still_synthesizes(self, graph_traverser, synthesizer, mock_bedrock):
        failing_os = AsyncMock()
        failing_os.search.side_effect = Exception("OpenSearch unavailable")
        vector_retriever = VectorRetriever(failing_os)

        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("test query", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        assert result.synthesized_answer is not None
        vec_step = next(s for s in result.trace_steps if s["step"] == "t3.vector_search")
        assert vec_step["status"] == "error"
        assert vec_step["detail"] == "retrieval_error"
        assert any(s["step"] == "t3.graph_traverse" and s["status"] == "success" for s in result.trace_steps)

    async def test_graph_failure_still_synthesizes(self, vector_retriever, synthesizer, mock_bedrock):
        failing_neptune = AsyncMock()
        failing_neptune.query.side_effect = Exception("Neptune timeout")
        graph_traverser = GraphTraverser(
            failing_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}"
        )

        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("test query", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        assert result.synthesized_answer is not None
        graph_step = next(s for s in result.trace_steps if s["step"] == "t3.graph_traverse")
        assert graph_step["status"] == "error"
        assert graph_step["detail"] == "retrieval_error"
        assert any(s["step"] == "t3.vector_search" and s["status"] == "success" for s in result.trace_steps)

    async def test_both_retrieval_fail_still_synthesizes(self, synthesizer, mock_bedrock):
        failing_os = AsyncMock()
        failing_os.search.side_effect = Exception("OS down")
        vector_retriever = VectorRetriever(failing_os)

        failing_neptune = AsyncMock()
        failing_neptune.query.side_effect = Exception("Neptune down")
        graph_traverser = GraphTraverser(
            failing_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}"
        )

        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        assert result.synthesized_answer is not None
        assert result.supporting_content == ()
        assert result.graph_context == ()

    async def test_synthesis_failure_propagates(self, vector_retriever, graph_traverser, mock_bedrock):
        failing_bedrock = AsyncMock()
        failing_bedrock.converse.side_effect = Exception("Bedrock throttled")
        synth = Synthesizer(failing_bedrock, guardrail_id="gr-123")

        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synth)
        with pytest.raises(Exception, match="Bedrock throttled"):
            await retriever.resolve("test query", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)


@pytest.mark.unit
class TestKnowledgeRetrieverDegradedSources:
    """degraded_sources field + per-source timeout."""

    async def test_no_degradation_when_all_succeed(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("SLA?", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)
        assert result.degraded_sources == ()

    async def test_vector_error_recorded_as_degraded(self, graph_traverser, synthesizer):
        failing_os = AsyncMock()
        failing_os.search.side_effect = Exception("OpenSearch unavailable")
        retriever = KnowledgeRetriever(VectorRetriever(failing_os), graph_traverser, synthesizer)
        result = await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        assert len(result.degraded_sources) == 1
        deg = result.degraded_sources[0]
        assert deg["source"] == "t3.vector_search"
        assert deg["reason"] == "error"

    async def test_source_timeout_recorded_as_timeout(self, graph_traverser, synthesizer, monkeypatch):
        import coa_serve.tier3.knowledge_retriever as kr

        # Force a tiny per-source timeout so a slow source trips it deterministically.
        # (Module-level constants are read at import; patch the live values.)
        monkeypatch.setattr(kr, "VECTOR_SEARCH_TIMEOUT_S", 0.01)
        monkeypatch.setattr(kr, "GRAPH_TRAVERSE_TIMEOUT_S", 0.01)

        async def _slow_search(*args, **kwargs):
            import asyncio

            await asyncio.sleep(1)
            return []

        slow_os = AsyncMock()
        slow_os.search.side_effect = _slow_search
        retriever = KnowledgeRetriever(VectorRetriever(slow_os), graph_traverser, synthesizer)
        result = await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        assert result.synthesized_answer is not None  # still synthesizes
        timeouts = [d for d in result.degraded_sources if d["reason"] == "timeout"]
        assert len(timeouts) == 1
        assert timeouts[0]["source"] == "t3.vector_search"
        vec_step = next(s for s in result.trace_steps if s["step"] == "t3.vector_search")
        assert vec_step["status"] == "timeout"

    async def test_per_source_env_override_vector(self, graph_traverser, synthesizer, monkeypatch):
        # the per-source override applies to vector_search only — graph
        # keeps its default. Constants are env-derived at import, so patch the
        # live module values (the _float_env contract itself is locked below).
        import coa_serve.tier3.knowledge_retriever as kr

        monkeypatch.setattr(kr, "VECTOR_SEARCH_TIMEOUT_S", 0.01)

        async def _slow_search(*args, **kwargs):
            import asyncio

            await asyncio.sleep(1)
            return []

        slow_os = AsyncMock()
        slow_os.search.side_effect = _slow_search
        retriever = KnowledgeRetriever(VectorRetriever(slow_os), graph_traverser, synthesizer)
        result = await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)

        timeouts = [d for d in result.degraded_sources if d["reason"] == "timeout"]
        assert [t["source"] for t in timeouts] == ["t3.vector_search"]

    def test_float_env_contract(self, monkeypatch):
        # Env contract: per-source var > single-knob default > 10.0; malformed
        # values fall back instead of crashing import.
        from coa_serve.tier3.knowledge_retriever import _float_env

        monkeypatch.setenv("TIER3_VECTOR_TIMEOUT", "3.5")
        assert _float_env("TIER3_VECTOR_TIMEOUT", 10.0) == 3.5
        monkeypatch.setenv("TIER3_VECTOR_TIMEOUT", "not-a-number")
        assert _float_env("TIER3_VECTOR_TIMEOUT", 10.0) == 10.0
        monkeypatch.delenv("TIER3_VECTOR_TIMEOUT")
        assert _float_env("TIER3_VECTOR_TIMEOUT", 7.0) == 7.0


@pytest.mark.unit
class TestKnowledgeRetrieverTruncation:
    async def test_supporting_content_truncated_flag(self, mock_neptune, mock_bedrock):
        os_client = AsyncMock()
        os_client.search.return_value = [
            VectorHit(
                id=f"c{i}",
                text=f"Chunk {i}",
                score=0.9 - i * 0.01,
                metadata={"source_doc": f"d{i}", "label": f"Doc {i}", "type": "document"},
            )
            for i in range(8)
        ]
        vec = VectorRetriever(os_client)
        graph = GraphTraverser(mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}")
        synth = Synthesizer(mock_bedrock, guardrail_id="gr-123")
        retriever = KnowledgeRetriever(vec, graph, synth)

        result = await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)
        assert result.supporting_content_truncated is True
        assert len(result.supporting_content) == 5

    async def test_supporting_content_not_truncated(self, vector_retriever, graph_traverser, synthesizer):
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synthesizer)
        result = await retriever.resolve("claims", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10)
        assert result.supporting_content_truncated is False
        assert len(result.supporting_content) == 1


@pytest.mark.unit
class TestKnowledgeRetrieverContextPassthrough:
    async def test_passes_catalog_and_structured_data(self, vector_retriever, graph_traverser, mock_bedrock):
        mock_bedrock.converse.return_value = ConverseResult(text="Answer with metrics. [CONFIDENCE: 0.92]")
        synth = Synthesizer(mock_bedrock, guardrail_id="gr-123")
        retriever = KnowledgeRetriever(vector_retriever, graph_traverser, synth)

        result = await retriever.resolve(
            "What is monthly revenue?",
            "finance",
            embedding=[0.1] * 10,
            structured_data=[{"month": "Jan", "revenue": 1000}],
            catalog_summary=[{"name": "monthly_revenue", "description": "Total", "dimensions": ["month"]}],
            conversation_history=[{"role": "user", "content": "Show revenue"}],
        )

        assert result.confidence == 0.92
        prompt_arg = mock_bedrock.converse.call_args.kwargs["prompt"]
        assert "<structured_data>" in prompt_arg or "scl_structured_" in prompt_arg
        assert "<catalog_summary>" in prompt_arg
        assert "<conversation_history>" in prompt_arg


@pytest.mark.unit
class TestTier3ToolUsed:
    """the retriever's progressive trace records carry stable
    toolUsed ids (vector_search=opensearch, graph_traverse=neptune,
    llm_synthesize=bedrock)."""

    async def test_record_sites_carry_tool_used(self, mock_neptune, mock_bedrock):
        os_client = AsyncMock()
        os_client.search.return_value = []
        retriever = KnowledgeRetriever(
            VectorRetriever(os_client),
            GraphTraverser(
                graph_client=mock_neptune, graph_uri_template="https://ontology-workbench.local/{namespace}"
            ),
            Synthesizer(bedrock_client=mock_bedrock, guardrail_id=""),
        )
        from coa_serve.trace import TraceCollector

        trace = TraceCollector()
        await retriever.resolve("test", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 10, trace=trace)

        by_step = {ts.step: ts for ts in trace.steps}
        assert by_step["t3.vector_search"].tool_used == "opensearch"
        assert by_step["t3.graph_traverse"].tool_used == "neptune"
        assert by_step["t3.synthesize"].tool_used == "bedrock"
