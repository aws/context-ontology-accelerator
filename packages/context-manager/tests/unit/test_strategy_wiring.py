# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for orchestrator and knowledge-retriever strategy-selection wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.lexical.strategies import RetrieverStrategy
from coa_serve.models import InvokeRequest, InvokeResponse
from coa_serve.orchestrator import Orchestrator
from coa_serve.tier2.strategy import StrategyOption, StructuredQueryTier
from coa_serve.tier3.graph_traverser import GraphTraverser
from coa_serve.tier3.knowledge_retriever import (
    KnowledgeRetriever,
    Tier3Result,
)
from coa_serve.tier3.vector_retriever import ChunkResult, VectorRetriever


def _tier3_result() -> Tier3Result:
    """A minimal, valid Tier3Result for the knowledge-retriever mock."""
    return Tier3Result(
        synthesized_answer="An answer grounded in lexical context.",
        supporting_content=(),
        graph_context=(),
        confidence=0.8,
        trace_steps=(
            {"step": "lexical_baseline_retrieve", "status": "success", "durationMs": 10},
            {"step": "t3.synthesize", "status": "success", "durationMs": 20},
        ),
    )


def _make_orchestrator(*, lexical_enabled: bool, deployment_strategy: str):
    """Build an Orchestrator with Tier 1/2 stubbed to miss and a mocked knowledge retriever."""
    metric_resolver = AsyncMock()
    metric_resolver.match.return_value = MagicMock(found=False, sql_template=None)

    # Strategy that always fails (returns None) so Tier 2 falls through to Tier 3
    mock_strategy = MagicMock()
    mock_strategy.name = StrategyOption.ONTOP
    mock_strategy.resolve = AsyncMock(return_value=None)
    structured_query_tier = StructuredQueryTier(strategies=[mock_strategy])

    # MagicMock (not AsyncMock) so ``lexical_enabled`` is a plain attribute and
    # only ``resolve`` is awaitable.
    knowledge_retriever = MagicMock()
    knowledge_retriever.lexical_enabled = lexical_enabled
    knowledge_retriever.resolve = AsyncMock(return_value=_tier3_result())

    orch = Orchestrator(
        metric_resolver=metric_resolver,
        knowledge_retriever=knowledge_retriever,
        structured_query_tier=structured_query_tier,
        query_executor=AsyncMock(),
        bedrock_client=None,  # skip embedding generation entirely
        lexical_retriever_strategy=deployment_strategy,
    )
    return orch, knowledge_retriever


@pytest.mark.unit
class TestRunTier3StrategyPrecedence:
    """resolve_strategy precedence (request > deployment > documented default)
    applied via ``_run_tier3`` when a lexical backend is configured."""

    async def test_request_override_beats_deployment_default(self):
        orch, km = _make_orchestrator(lexical_enabled=True, deployment_strategy="chunk_based_semantic")
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3, "retrieverStrategy": "traversal"},
        )

        await orch.resolve(request)

        assert km.resolve.await_count == 1
        passed = km.resolve.await_args.kwargs["retriever_strategy"]
        assert passed == RetrieverStrategy.TRAVERSAL

    async def test_deployment_default_beats_documented_default(self):
        orch, km = _make_orchestrator(lexical_enabled=True, deployment_strategy="entity_based")
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3},
        )

        await orch.resolve(request)

        passed = km.resolve.await_args.kwargs["retriever_strategy"]
        assert passed == RetrieverStrategy.ENTITY_BASED

    async def test_documented_default_when_deployment_invalid_and_no_override(self):
        orch, km = _make_orchestrator(lexical_enabled=True, deployment_strategy="not-a-real-strategy")
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3},
        )

        await orch.resolve(request)

        passed = km.resolve.await_args.kwargs["retriever_strategy"]
        assert passed == RetrieverStrategy.CHUNK_BASED_SEMANTIC


@pytest.mark.unit
class TestRunTier3OnDemandGraphrag:
    """A deployment where the lexical retriever is built but no deployment
    default is set (``lexical_retriever_strategy=None`` — e.g. TIER3_STRATEGY=
    hand-rolled). graphrag is engaged only when the request supplies
    ``retrieverStrategy``; otherwise no strategy resolves and Tier 3 runs the
    hand-rolled path."""

    async def test_no_request_strategy_resolves_none(self):
        orch, km = _make_orchestrator(lexical_enabled=True, deployment_strategy=None)
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3},
        )

        response = await orch.resolve(request)

        passed = km.resolve.await_args.kwargs["retriever_strategy"]
        assert passed is None
        assert "retrieverStrategy" not in (response.result.metadata or {})

    async def test_request_strategy_engages_graphrag_on_demand(self):
        orch, km = _make_orchestrator(lexical_enabled=True, deployment_strategy=None)
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3, "retrieverStrategy": "entity_network"},
        )

        response = await orch.resolve(request)

        passed = km.resolve.await_args.kwargs["retriever_strategy"]
        assert passed == RetrieverStrategy.ENTITY_NETWORK
        assert response.result.metadata["retrieverStrategy"] == "entity_network"


@pytest.mark.unit
class TestRunTier3HandRolledParity:
    """When no lexical backend is configured, ``_run_tier3`` resolves no
    strategy (retriever_strategy stays ``None``) and adds no strategy metadata,
    so the hand-rolled path is untouched (3.1)."""

    async def test_no_strategy_resolved_when_lexical_disabled(self):
        orch, km = _make_orchestrator(lexical_enabled=False, deployment_strategy="chunk_based_semantic")
        # Even a request that carries retrieverStrategy must be ignored when no
        # lexical backend exists.
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3, "retrieverStrategy": "traversal"},
        )

        response = await orch.resolve(request)

        passed = km.resolve.await_args.kwargs["retriever_strategy"]
        assert passed is None
        assert "retrieverStrategy" not in (response.result.metadata or {})


@pytest.mark.unit
class TestRunTier3StrategyMetadata:
    """The resolved strategy name surfaces in the response metadata (2.18)."""

    async def test_resolved_strategy_in_response_metadata(self):
        orch, _km = _make_orchestrator(lexical_enabled=True, deployment_strategy="chunk_based_semantic")
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3, "retrieverStrategy": "traversal"},
        )

        response = await orch.resolve(request)

        assert isinstance(response, InvokeResponse)
        assert response.result.metadata["retrieverStrategy"] == "traversal"
        # The namespace metadata from the assembler is preserved (merge, not replace).
        assert response.result.metadata["namespace"] == "demo"

    async def test_deployment_default_strategy_in_response_metadata(self):
        orch, _km = _make_orchestrator(lexical_enabled=True, deployment_strategy="entity_based")
        request = InvokeRequest(
            query="explain the model",
            namespace="demo",
            options={"tierOverride": 3},
        )

        response = await orch.resolve(request)

        assert response.result.metadata["retrieverStrategy"] == "entity_based"


def _make_synthesizer():
    from .lexical_helpers import make_synthesizer

    return make_synthesizer(answer="synthesized.", confidence=0.77, duration_ms=42, guardrail_blocked=False)


@pytest.mark.unit
class TestKnowledgeRetrieverStrategyThreading:
    """``resolve(retriever_strategy=...)`` is threaded into the lexical
    retriever's ``retrieve(strategy=...)`` call when lexical is configured."""

    async def test_resolved_strategy_passed_to_lexical_retrieve(self):
        from coa_serve.lexical.baseline_retriever import BaselineResult
        from coa_serve.lexical.strategies import STRATEGY_REGISTRY

        mock_lexical = AsyncMock()
        mock_lexical.retrieve.return_value = BaselineResult(
            chunks=(),
            entities=(),
            trace_steps=({"step": "lexical_baseline_retrieve", "status": "success", "durationMs": 5},),
        )

        retriever = KnowledgeRetriever(
            vector_retriever=None,
            graph_traverser=None,
            synthesizer=_make_synthesizer(),
            lexical_retriever=mock_lexical,
        )

        await retriever.resolve("q", "ns", retriever_strategy=RetrieverStrategy.TRAVERSAL)

        # The resolved enum is mapped to its StrategySpec before being handed to
        # the adapter (which builds/caches engines from the spec, not the enum).
        mock_lexical.retrieve.assert_awaited_once_with(
            "q", "ns", strategy=STRATEGY_REGISTRY[RetrieverStrategy.TRAVERSAL]
        )


@pytest.mark.unit
class TestKnowledgeRetrieverHandRolledParity:
    """When ``lexical_retriever is None``, a non-None ``retriever_strategy`` is
    ignored entirely and the parallel vector + graph path runs unchanged."""

    async def test_retriever_strategy_ignored_when_lexical_none(self):
        mock_vector = AsyncMock(spec=VectorRetriever)
        mock_vector.search.return_value = [
            ChunkResult(chunk_id="c1", text="t", source_doc="d.pdf", relevance_score=0.8)
        ]
        mock_graph = AsyncMock(spec=GraphTraverser)
        mock_graph.traverse.return_value = []

        retriever = KnowledgeRetriever(
            vector_retriever=mock_vector,
            graph_traverser=mock_graph,
            synthesizer=_make_synthesizer(),
            lexical_retriever=None,  # hand-rolled mode
        )

        result = await retriever.resolve(
            "q",
            "ns",
            embedding=[0.1] * 8,
            retriever_strategy=RetrieverStrategy.TRAVERSAL,
        )

        mock_vector.search.assert_awaited_once()
        mock_graph.traverse.assert_awaited_once()
        step_names = [s["step"] for s in result.trace_steps]
        assert "lexical_baseline_retrieve" not in step_names
        assert "t3.vector_search" in step_names
        assert "t3.graph_traverse" in step_names


@pytest.mark.unit
class TestModeRequestToggle:
    """``options.mode`` is the request-side execution toggle.

    Mode is orthogonal to the tier cascade — the request field is ``mode``. The
    deployment default is ``standard`` (serve-stack TIER3_STRATEGY=hand-rolled), so
    a caller opts IN to the multi-step loop with ``mode="deep-reasoning"``. Selecting
    deep reasoning hands the whole request to the loop, bypassing the T1->T2 cascade.
    """

    async def test_deep_reasoning_mode_bypasses_the_tier_cascade(self):
        """Selecting deep reasoning routes straight to the loop — mode is not a tier.

        Without this, a question T1/T2 can answer returns before Tier 3, so the
        toggle appears to do nothing on any namespace with a structured source.
        """
        orch, _ = _make_orchestrator(lexical_enabled=True, deployment_strategy="chunk_based_semantic")
        orch._agentic_retriever = MagicMock()
        ran: list[str] = []

        async def _t1(*a, **k):
            ran.append("tier1")
            return MagicMock()  # T1 WOULD answer

        async def _t3(*a, **k):
            ran.append("tier3_deep_reasoning")
            return MagicMock(result=MagicMock())

        orch._run_tier1 = _t1
        orch._run_tier3 = _t3
        orch._resolve_source_gating = AsyncMock(
            return_value=MagicMock(skip_tier1=False, skip_tier3_vector_search=False)
        )
        orch._embed_query = AsyncMock(return_value=[0.1])

        await orch.resolve(InvokeRequest(query="q", namespace="ns", options={"mode": "deep-reasoning"}))
        assert ran == ["tier3_deep_reasoning"], f"deep reasoning must bypass the cascade, ran={ran}"

    def test_explicit_tier_override_wins_over_deep_reasoning_mode(self):
        """A tierOverride is a direct instruction and is honored over mode routing."""
        orch, _ = _make_orchestrator(lexical_enabled=True, deployment_strategy="chunk_based_semantic")
        orch._agentic_retriever = MagicMock()
        # The short-circuit guard is `tier_override is None and
        # _is_deep_reasoning_engaged`, so a tierOverride disables the bypass
        # regardless of mode.
        assert orch._is_deep_reasoning_engaged({"mode": "deep-reasoning"}) is True  # engaged flag unchanged

    def test_validator_accepts_standard_and_deep_reasoning(self):
        for mode in ("deep-reasoning", "standard"):
            req = InvokeRequest(query="q", namespace="ns", options={"mode": mode})
            assert req.options["mode"] == mode

    def test_validator_still_accepts_deprecated_agentic_spelling(self):
        """The pre-rename wire value must not start 400-ing on upgrade."""
        req = InvokeRequest(query="q", namespace="ns", options={"mode": "agentic"})
        assert req.options["mode"] == "agentic"

    def test_validator_rejects_unknown_mode(self):
        with pytest.raises(ValueError, match="must be 'deep-reasoning' or 'standard'"):
            InvokeRequest(query="q", namespace="ns", options={"mode": "turbo"})

    def test_standard_opts_out_even_when_deployment_default_is_deep_reasoning(self):
        """The whole point of the toggle: 'standard' must win over the default."""
        orch, _ = _make_orchestrator(lexical_enabled=True, deployment_strategy="chunk_based_semantic")
        orch._agentic_retriever = MagicMock()  # a retriever IS available
        orch._tier3_deep_reasoning_default = True  # deployment default says deep reasoning

        assert orch._is_deep_reasoning_engaged({"mode": "standard"}) is False
        assert orch._is_deep_reasoning_engaged({"mode": "deep-reasoning"}) is True
        assert orch._is_deep_reasoning_engaged({}) is True  # absent → deployment default

    def test_no_retriever_means_never_deep_reasoning(self):
        """Even an explicit mode cannot engage a retriever that was not built."""
        orch, _ = _make_orchestrator(lexical_enabled=True, deployment_strategy="chunk_based_semantic")
        orch._agentic_retriever = None
        orch._tier3_deep_reasoning_default = True

        assert orch._is_deep_reasoning_engaged({"mode": "deep-reasoning"}) is False
        assert orch._is_deep_reasoning_engaged({}) is False
