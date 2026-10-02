# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Task 10 — end-to-end (E2E) tests across the Tier 3 lexical path.

These tests drive the *full* lexical path with only the graphrag-toolkit engine
faked — every shipping seam in between runs for real:

    InvokeRequest
      → Orchestrator.resolve / _run_tier3        (real; resolves strategy)
      → KnowledgeRetriever.resolve               (real; threads strategy)
      → _resolve_via_lexical                      (real; degradation boundary)
      → BaselineLexicalRetriever.retrieve         (real; timeout wrapper)
      → _get_engine / _retrieve_sync              (real; tenant + cache + build)
      → strategy.build_engine(...)                (real; toolkit factory FAKED here)
      → engine.retrieve(query) -> fake nodes      (FAKED)
      → _map_results                              (real; maps to Tier 3 model)
      → ResponseAssembler.assemble_tier3          (real)
      → InvokeResponse                            (real)

The only patched symbols are the toolkit's storage factories and
``LexicalGraphQueryEngine`` (so ``build_engine`` returns a fake engine), plus the
fake engine's ``retrieve`` (the network/graph call). Everything the serve layer
ships — URI handling, tenant derivation, per-(tenant, strategy) caching, the
bounded-timeout wrapper, result mapping, trace/metadata threading — executes.

This is deliberately broader than the co-located sibling suites, which assert
the same seams in isolation (engine/caching → 5.a, timeout → 6.a, mapping → 7.a,
parity → 8.a). Task 10 keeps the end-to-end framing only.

Covered:
  * Full lexical path: mapped results AND empties propagate to the response;
    resolved strategy surfaces in trace + response metadata.
  * Hand-rolled parity (lexical disabled): a request carrying ``retrieverStrategy``
    resolves identically via the vector + graph path (no regression) — Property 6.
  * Timeout degradation at the resolve boundary: a lexical timeout degrades to
    empties and the overall Tier 3 resolve still succeeds — Property 4.

_Requirements: 2.8, 2.9, 2.11, 3.1_
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever
from coa_serve.models import InvokeRequest, InvokeResponse
from coa_serve.orchestrator import Orchestrator
from coa_serve.tier1.metric_resolver import MetricMatch
from coa_serve.tier2.strategy import StrategyOption, StructuredQueryTier
from coa_serve.tier3.graph_traverser import GraphTraverser
from coa_serve.tier3.knowledge_retriever import KnowledgeRetriever
from coa_serve.tier3.vector_retriever import VectorRetriever

from .lexical_helpers import (
    AOSS_VECTOR_STORE_URI,
    NA_GRAPH_STORE_URI,
    entity_contexts,
    make_synthesizer,
    make_toolkit_node,
    source_metadata,
)

# ── Fake toolkit engine ─────────────────────────────────────────────


class _FakeEngine:
    """Stand-in for a toolkit ``LexicalGraphQueryEngine``.

    Only ``retrieve(query)`` is exercised by the adapter's ``_retrieve_sync``.
    ``nodes`` may be a list (returned as-is) or a zero-arg callable (so a test
    can make the call block to exercise the timeout path).
    """

    def __init__(self, nodes):
        self._nodes = nodes

    def retrieve(self, query: str):
        return self._nodes() if callable(self._nodes) else self._nodes


@contextmanager
def fake_toolkit_engine(nodes):
    """Patch the toolkit storage factories + engine so ``build_engine`` returns a
    :class:`_FakeEngine`. All adapter code between the factory and the engine
    runs for real; only the toolkit boundary is faked.

    Both engine-family entry points resolve through the same fake engine. All
    seven non-deprecated strategies build via ``for_traversal_based_search``, so
    only that entry point needs a return value; the deprecated semantic-guided
    entry point is no longer used by any strategy.
    """
    fake_engine = _FakeEngine(nodes)
    with (
        patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
        patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
        patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine_cls,
    ):
        mock_engine_cls.for_traversal_based_search.return_value = fake_engine
        # Yield the patched engine class so callers can assert how many times a
        # toolkit factory entry point was invoked (engine-build / cache checks).
        yield mock_engine_cls


# ── Stack builders ──────────────────────────────────────────────────


def _populated_nodes():
    """A realistic single toolkit node carrying the real nested metadata shape
    (``Source.sourceId`` + ``EntityContexts``) so ``_map_results`` produces a
    populated chunk + entity."""
    return [
        make_toolkit_node(
            node_id="chunk-1",
            text="Apple reported revenue of $83 billion in Q3 2022.",
            score=0.91,
            metadata={
                "source": source_metadata("AAPL-2022-Q3.pdf"),
                "entity_contexts": entity_contexts([("ent-apple", "Apple Inc.", "Company")]),
            },
        )
    ]


def _build_lexical_orchestrator(
    *,
    deployment_strategy: str = "chunk_based_semantic",
    timeout_s: float = 15.0,
):
    """Build a real Orchestrator wired to a real KnowledgeRetriever and a real
    BaselineLexicalRetriever (toolkit faked by the caller via
    ``fake_toolkit_engine``).

    Tier 1 misses and Tier 2 strategy returns None, so resolution falls
    through to Tier 3; callers also pass ``tierOverride=3`` to go straight there.

    Returns ``(orchestrator, lexical_retriever, synthesizer)``.
    """
    metric_resolver = AsyncMock()
    metric_resolver.match.return_value = MetricMatch(found=False)

    # Strategy that always fails (returns None) so Tier 2 falls through to Tier 3
    mock_strategy = MagicMock()
    mock_strategy.name = StrategyOption.ONTOP
    mock_strategy.resolve = AsyncMock(return_value=None)
    structured_query_tier = StructuredQueryTier(strategies=[mock_strategy])

    synthesizer = make_synthesizer(answer="An answer grounded in lexical context.", confidence=0.8)

    lexical_retriever = BaselineLexicalRetriever(
        graph_store_uri=NA_GRAPH_STORE_URI,
        vector_store_uri=AOSS_VECTOR_STORE_URI,
        timeout_s=timeout_s,
    )

    knowledge_retriever = KnowledgeRetriever(
        vector_retriever=None,
        graph_traverser=None,
        synthesizer=synthesizer,
        lexical_retriever=lexical_retriever,
    )

    orch = Orchestrator(
        metric_resolver=metric_resolver,
        knowledge_retriever=knowledge_retriever,
        structured_query_tier=structured_query_tier,
        query_executor=AsyncMock(),
        bedrock_client=None,  # skip embedding generation
        lexical_retriever_strategy=deployment_strategy,
    )
    return orch, lexical_retriever, synthesizer


def _build_hand_rolled_orchestrator():
    """Build a real Orchestrator wired to a real KnowledgeRetriever in
    hand-rolled mode (``lexical_retriever=None``) with real vector + graph
    retrievers (their clients mocked). Used for the parity E2E."""
    metric_resolver = AsyncMock()
    metric_resolver.match.return_value = MetricMatch(found=False)

    # Strategy that always fails (returns None) so Tier 2 falls through to Tier 3
    mock_strategy = MagicMock()
    mock_strategy.name = StrategyOption.ONTOP
    mock_strategy.resolve = AsyncMock(return_value=None)
    structured_query_tier = StructuredQueryTier(strategies=[mock_strategy])

    # Real VectorRetriever / GraphTraverser with mocked clients so the parallel
    # hand-rolled path runs end to end.
    vector_client = AsyncMock()
    vector_hit = MagicMock()
    vector_hit.id = "vec-c1"
    vector_hit.text = "Hand-rolled vector chunk."
    vector_hit.score = 0.77
    vector_hit.metadata = {"source_doc": "handrolled.pdf", "label": "", "uri": ""}
    vector_client.search.return_value = [vector_hit]
    vector_retriever = VectorRetriever(vector_client)

    graph_traverser = AsyncMock(spec=GraphTraverser)
    graph_traverser.traverse.return_value = []

    synthesizer = make_synthesizer(answer="Hand-rolled answer.", confidence=0.7)

    knowledge_retriever = KnowledgeRetriever(
        vector_retriever=vector_retriever,
        graph_traverser=graph_traverser,
        synthesizer=synthesizer,
        lexical_retriever=None,  # hand-rolled mode
    )

    bedrock_client = AsyncMock()
    bedrock_client.embed.return_value = [0.1] * 1024
    bedrock_client.model_id = "test-model"

    orch = Orchestrator(
        metric_resolver=metric_resolver,
        knowledge_retriever=knowledge_retriever,
        structured_query_tier=structured_query_tier,
        query_executor=AsyncMock(),
        bedrock_client=bedrock_client,
        lexical_retriever_strategy="chunk_based_semantic",
    )
    return orch, vector_client, graph_traverser, synthesizer


def _trace_steps(response: InvokeResponse) -> list:
    return list(response.result.trace)


# ── 1. Full lexical path end to end ─────────────────────────────────


@pytest.mark.unit
class TestLexicalPathEndToEnd:
    """Drive the full lexical path with the toolkit engine faked, asserting
    mapped results and empties propagate and the resolved strategy surfaces in
    trace + response metadata (2.11, 2.18)."""

    async def test_mapped_results_propagate_to_response(self):
        orch, _lexical, synthesizer = _build_lexical_orchestrator()
        request = InvokeRequest(
            query="What was Apple's revenue?",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3},
        )

        with fake_toolkit_engine(_populated_nodes()):
            response = await orch.resolve(request)

        assert isinstance(response, InvokeResponse)
        assert response.result.tier == 3
        assert response.result.synthesized_answer == "An answer grounded in lexical context."

        # The toolkit node was mapped through _map_results all the way into the
        # assembled supporting_content (chunk) and graph_context (entity).
        supporting = response.result.supporting_content
        assert len(supporting) == 1
        assert supporting[0]["chunkId"] == "chunk-1"
        assert supporting[0]["sourceDoc"] == "AAPL-2022-Q3.pdf"
        assert supporting[0]["text"].startswith("Apple reported revenue")

        entities = response.result.graph_context["entities"]
        assert len(entities) == 1
        assert entities[0]["uri"] == "ent-apple"
        assert entities[0]["label"] == "Apple Inc."
        assert entities[0]["type"] == "Company"

        # The synthesizer received the mapped chunks + entities (lexical drove it).
        synthesizer.synthesize.assert_awaited_once()
        synth_chunks = synthesizer.synthesize.call_args.args[1]
        assert [c.chunk_id for c in synth_chunks] == ["chunk-1"]

    async def test_resolved_strategy_in_trace_and_metadata(self):
        # Request override picks a non-default strategy; it must surface in both
        # the response metadata (2.18) and the lexical trace step detail.
        orch, _lexical, _synth = _build_lexical_orchestrator(deployment_strategy="chunk_based_semantic")
        request = InvokeRequest(
            query="What was Apple's revenue?",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3, "retrieverStrategy": "traversal"},
        )

        with fake_toolkit_engine(_populated_nodes()):
            response = await orch.resolve(request)

        # Response metadata attributes the resolved strategy.
        assert response.result.metadata["retrieverStrategy"] == "traversal"
        assert response.result.metadata["namespace"] == "550e8400-e29b-41d4-a716-446655440000"

        # The lexical trace step detail records the same strategy.
        lexical_steps = [s for s in _trace_steps(response) if s.step == "lexical_baseline_retrieve"]
        assert len(lexical_steps) == 1
        assert lexical_steps[0].status == "success"
        assert lexical_steps[0].detail == "strategy=traversal; 1 nodes retrieved"

    async def test_deployment_default_strategy_surfaces_without_override(self):
        orch, _lexical, _synth = _build_lexical_orchestrator(deployment_strategy="entity_based")
        request = InvokeRequest(
            query="What was Apple's revenue?",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3},
        )

        with fake_toolkit_engine(_populated_nodes()):
            response = await orch.resolve(request)

        assert response.result.metadata["retrieverStrategy"] == "entity_based"
        lexical_steps = [s for s in _trace_steps(response) if s.step == "lexical_baseline_retrieve"]
        assert "strategy=entity_based" in lexical_steps[0].detail

    async def test_empties_propagate_when_engine_returns_no_nodes(self):
        # A populated graph that returns nothing for this query: empties must
        # propagate cleanly (success trace, empty supporting content) rather than
        # erroring anywhere along the path.
        orch, _lexical, synthesizer = _build_lexical_orchestrator()
        request = InvokeRequest(
            query="A query with no matches",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3},
        )

        with fake_toolkit_engine([]):  # engine returns zero nodes
            response = await orch.resolve(request)

        assert response.result.tier == 3
        assert response.result.supporting_content == []
        assert response.result.graph_context["entities"] == []
        # Lexical retrieval still SUCCEEDED (empty is not an error).
        lexical_steps = [s for s in _trace_steps(response) if s.step == "lexical_baseline_retrieve"]
        assert lexical_steps[0].status == "success"
        assert "0 nodes retrieved" in lexical_steps[0].detail
        # Synthesis still ran (with empty context).
        synthesizer.synthesize.assert_awaited_once()

    async def test_engine_built_once_per_tenant_strategy_across_requests(self):
        # Two requests for the same namespace + strategy reuse the cached engine
        # (the toolkit factory is invoked once), proving the per-(tenant, strategy)
        # cache survives across the full resolve path.
        orch, lexical, _synth = _build_lexical_orchestrator()
        request = InvokeRequest(
            query="q",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3},
        )

        with fake_toolkit_engine(_populated_nodes()) as mock_engine_cls:
            await orch.resolve(request)
            await orch.resolve(request)

            # chunk_based_semantic uses for_traversal_based_search; built once.
            assert mock_engine_cls.for_traversal_based_search.call_count == 1

        # One cache entry for (tenant, strategy).
        assert len(lexical._engines) == 1


# ── 2. Hand-rolled parity E2E ───────────────────────────────────────


@pytest.mark.unit
class TestHandRolledParityEndToEnd:
    """With ``lexical_retriever is None``, a request carrying ``retrieverStrategy``
    resolves identically to today via the parallel vector + graph path — no
    regression (Property 6 / Requirement 3.1)."""

    async def test_request_with_strategy_runs_vector_graph_path(self):
        orch, vector_client, graph_traverser, synthesizer = _build_hand_rolled_orchestrator()
        # The request carries a retrieverStrategy that must be entirely ignored
        # because no lexical backend is configured.
        request = InvokeRequest(
            query="explain claims",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3, "retrieverStrategy": "traversal"},
        )

        response = await orch.resolve(request)

        assert isinstance(response, InvokeResponse)
        assert response.result.tier == 3
        assert response.result.synthesized_answer == "Hand-rolled answer."

        # The parallel vector + graph path executed (the hand-rolled retrieval).
        vector_client.search.assert_awaited_once()
        graph_traverser.traverse.assert_awaited_once()

        # No lexical trace step on the hand-rolled path; vector + graph appear.
        step_names = [s.step for s in _trace_steps(response)]
        assert "lexical_baseline_retrieve" not in step_names
        assert "t3.vector_search" in step_names
        assert "t3.graph_traverse" in step_names

        # No strategy metadata threaded onto the response (lexical disabled).
        assert "retrieverStrategy" not in (response.result.metadata or {})

        # The hand-rolled vector chunk propagated to supporting_content.
        assert len(response.result.supporting_content) == 1
        assert response.result.supporting_content[0]["chunkId"] == "vec-c1"

    async def test_parity_with_and_without_strategy_is_identical(self):
        # Resolving with a retrieverStrategy and without it produce the same
        # shape on the hand-rolled path (the option has zero effect).
        orch_a, va, ga, _sa = _build_hand_rolled_orchestrator()
        orch_b, vb, gb, _sb = _build_hand_rolled_orchestrator()

        req_with = InvokeRequest(
            query="explain claims",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3, "retrieverStrategy": "traversal"},
        )
        req_without = InvokeRequest(
            query="explain claims",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3},
        )

        resp_with = await orch_a.resolve(req_with)
        resp_without = await orch_b.resolve(req_without)

        # Same tier, same supporting content, same trace step names, no strategy
        # metadata in either case.
        assert resp_with.result.tier == resp_without.result.tier == 3
        assert resp_with.result.supporting_content == resp_without.result.supporting_content
        assert [s.step for s in resp_with.result.trace] == [s.step for s in resp_without.result.trace]
        assert "retrieverStrategy" not in (resp_with.result.metadata or {})
        assert "retrieverStrategy" not in (resp_without.result.metadata or {})


# ── 3. Timeout-degradation E2E at the resolve boundary ──────────────


@pytest.mark.unit
class TestTimeoutDegradationEndToEnd:
    """A lexical timeout degrades to empties and the overall Tier 3 resolve still
    succeeds — it does not fail the whole resolve (Property 4 / 2.8, 2.9)."""

    async def test_lexical_timeout_degrades_resolve_to_empties(self):
        # Tiny timeout; the faked engine blocks longer than the timeout. The
        # synchronous toolkit call cannot be cancelled, so the adapter's
        # asyncio.wait_for raises TimeoutError internally, which the adapter
        # absorbs into an empty result with an error trace step.
        orch, _lexical, synthesizer = _build_lexical_orchestrator(timeout_s=0.05)
        request = InvokeRequest(
            query="slow query",
            namespace="550e8400-e29b-41d4-a716-446655440000",
            options={"tierOverride": 3},
        )

        def _blocking_retrieve():
            time.sleep(1.0)
            return _populated_nodes()

        with fake_toolkit_engine(_blocking_retrieve):
            response = await orch.resolve(request)

        # The overall resolve SUCCEEDED (Tier 3 response produced), not an error.
        assert isinstance(response, InvokeResponse)
        assert response.result.tier == 3
        assert response.result.synthesized_answer == "An answer grounded in lexical context."

        # Lexical degraded to empties.
        assert response.result.supporting_content == []
        assert response.result.graph_context["entities"] == []

        # The lexical trace step records the timeout as an error, but synthesis
        # still ran (the resolve continued past the lexical failure).
        steps = _trace_steps(response)
        lexical_steps = [s for s in steps if s.step == "lexical_baseline_retrieve"]
        assert len(lexical_steps) == 1
        assert lexical_steps[0].status == "error"
        assert "TimeoutError" in lexical_steps[0].detail

        synth_steps = [s for s in steps if s.step == "t3.synthesize"]
        assert len(synth_steps) == 1
        assert synth_steps[0].status == "success"
        synthesizer.synthesize.assert_awaited_once()
