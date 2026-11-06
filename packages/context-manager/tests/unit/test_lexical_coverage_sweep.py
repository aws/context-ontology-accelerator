# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Task 9 — cross-cutting / missing unit-test coverage + design-property sweep.

This module is the cross-cutting sweep for the ``lexical-baseline-retriever-fixes``
bugfix spec. It does two things:

1. **Records the design.md correctness-property → unit-test mapping**
   (coverage tracking) in :data:`PROPERTY_TEST_MAPPING` below, and asserts the
   referenced co-located test modules/classes actually exist so the mapping
   cannot silently rot.
2. **Fills the few genuine gaps at the seams between primitives** that are NOT
   already covered by the co-located suites (tasks 1.a–8.a). It deliberately
   does NOT duplicate those suites — each test here targets a seam/edge case
   with no existing unit-level assertion.

═══════════════════════════════════════════════════════════════════════════
 design.md Correctness Property  →  unit test(s)   (coverage tracking)
═══════════════════════════════════════════════════════════════════════════

Property 1 — NA backends produce a well-formed graph-store URI (BUG 1/7)
    test_lexical_baseline.py::TestGraphStoreUri (NDB/NA/wss/host/prefix shapes)
    test_lexical_baseline.py::TestBuildLexicalRetrieverStrategy
        ::test_retriever_uses_na_graph_store_uri / ::test_retriever_uses_ndb_graph_store_uri
    + seam gaps here: TestGraphStoreUriSeams (NA-detected-but-no-extractable-id
      fallback; trailing-slash NDB host normalization; bare NA graph-id).
    Validates: Requirements 2.1, 2.2, 2.3, 2.14

Property 2 — strategy resolution honors precedence and rejects unknowns (BUG 8)
    test_strategies.py::TestResolveStrategyPrecedence (request>deploy>default,
        unknown-request→absent, unknown-deploy→warn+fallback)
    test_models.py::TestRetrieverStrategyValidation (invalid request → 400/ValidationError)
    test_strategy_wiring.py::TestRunTier3StrategyPrecedence
    Validates: Requirements 2.15, 2.16, 2.17

Property 3 — tenant scoping flows through the real code path (BUG 6)
    test_lexical_baseline.py::TestGetEngineTenantAndCaching (tenant_id derivation,
        per-(tenant,strategy) caching, override non-collision, distinct tenants)
    Validates: Requirements 2.12, 2.13

Property 4 — timeout is bounded and degrades gracefully (BUG 3)
    test_lexical_baseline.py::TestBaselineLexicalRetrieverRetrieve
        ::test_timeout_degrades_to_empty / ::test_concurrency_bounded_by_dedicated_executor
    + seam gap here: TestResolveBoundaryDegradation (the Tier-3 resolve boundary
      ``_resolve_via_lexical`` absorbs an unexpected raise out of ``retrieve`` —
      the defensive try/except seam, distinct from the retriever-level timeout).
    Validates: Requirements 2.8, 2.9

Property 5 — tests exercise shipping code and real output (BUG 4, BUG 5)
    test_lexical_baseline.py::TestMapResults (real nested Source/EntityContexts
        shapes, defensive fallbacks, one-time warning)
    NOTE: the populated-graph confirmation is the integration task (11); unit
    level covers the mapping logic against the documented shape.
    Validates: Requirements 2.10, 2.11, 2.18, 2.19

Property 6 — the hand-rolled default path is unchanged (Preservation; 3.1, 3.7)
    test_strategy_wiring.py::TestRunTier3HandRolledParity,
        ::TestKnowledgeRetrieverHandRolledParity
    test_lexical_baseline.py::TestBuildLexicalRetriever::test_returns_none_when_hand_rolled
    Validates: Requirements 3.1, 3.7

Property 7 — non-targeted invariants hold (Preservation; 3.2–3.5)
    test_strategies.py::TestLazyImportInvariant + test_config.py
        ::test_load_config_does_not_import_graphrag_toolkit (lazy graphrag import)
    + seam gap here: TestVersionPinInvariant (3.3/3.4 — graphrag-lexical-graph==3.20.0
      pin asserted; previously no unit test).
    Validates: Requirements 3.2, 3.3, 3.4, 3.5

Property 8 — new lexical default is documented, not silent (3.6, 3.8)
    test_strategies.py::TestResolveStrategyPrecedence::test_default_is_chunk_based_semantic,
        ::TestStrategyRegistry::test_default_spec_is_spec_for_default_strategy
    test_config.py::TestLexicalRetrieverStrategyConfig::test_absent_var_yields_default
    Validates: Requirements 2.4, 2.5, 2.6, 2.7, 3.6, 3.8
═══════════════════════════════════════════════════════════════════════════

_Requirements: 2.14, 2.15, 2.16, 2.17_
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock, patch

import pytest

from .lexical_helpers import (
    make_baseline_retriever,
    make_strategy_spec,
    make_synthesizer,
    make_toolkit_node,
)

# Machine-checkable mirror of the prose mapping above: each design.md property
# maps to the co-located module(s) (and optionally ``module::ClassName``) that
# provide its unit-level coverage. The test below asserts every referent exists.
PROPERTY_TEST_MAPPING: dict[str, list[str]] = {
    "Property 1": [
        "test_lexical_baseline::TestGraphStoreUri",
        "test_lexical_baseline::TestBuildLexicalRetrieverStrategy",
        "test_lexical_coverage_sweep::TestGraphStoreUriSeams",
    ],
    "Property 2": [
        "test_strategies::TestResolveStrategyPrecedence",
        "test_models::TestRetrieverStrategyValidation",
        "test_strategy_wiring::TestRunTier3StrategyPrecedence",
    ],
    "Property 3": [
        "test_lexical_baseline::TestGetEngineTenantAndCaching",
    ],
    "Property 4": [
        "test_lexical_baseline::TestBaselineLexicalRetrieverRetrieve",
        "test_lexical_coverage_sweep::TestResolveBoundaryDegradation",
    ],
    "Property 5": [
        "test_lexical_baseline::TestMapResults",
    ],
    "Property 6": [
        "test_strategy_wiring::TestRunTier3HandRolledParity",
        "test_strategy_wiring::TestKnowledgeRetrieverHandRolledParity",
        "test_lexical_baseline::TestBuildLexicalRetriever",
    ],
    "Property 7": [
        "test_strategies::TestLazyImportInvariant",
        "test_lexical_coverage_sweep::TestVersionPinInvariant",
    ],
    "Property 8": [
        "test_strategies::TestStrategyRegistry",
        "test_config::TestLexicalRetrieverStrategyConfig",
    ],
}


# ── Design-property sweep: mapping integrity ────────────────────────


@pytest.mark.unit
class TestDesignPropertyCoverageMapping:
    """Assert each design.md Property has at least one existing unit-test
    referent, and that every referent (``module`` or ``module::Class``) actually
    resolves. This converts the coverage-tracking mapping into a live check so a
    renamed/removed test surfaces as a failure rather than silent rot."""

    def test_all_eight_properties_present(self):
        assert set(PROPERTY_TEST_MAPPING) == {f"Property {i}" for i in range(1, 9)}

    @pytest.mark.parametrize("prop", [f"Property {i}" for i in range(1, 9)])
    def test_property_has_at_least_one_referent(self, prop):
        assert PROPERTY_TEST_MAPPING[prop], f"{prop} has no unit-test referent"

    def test_every_referent_resolves(self):
        package = __name__.rsplit(".", 1)[0]  # tests.unit
        missing: list[str] = []
        for prop, referents in PROPERTY_TEST_MAPPING.items():
            for referent in referents:
                module_name, _, class_name = referent.partition("::")
                try:
                    module = importlib.import_module(f"{package}.{module_name}")
                except ModuleNotFoundError:
                    missing.append(f"{prop}: module {module_name} not found")
                    continue
                if class_name and not hasattr(module, class_name):
                    missing.append(f"{prop}: {module_name} has no {class_name}")
        assert not missing, "Stale property→test mapping referents:\n" + "\n".join(missing)


# ── Property 1 seam gaps — graph-store URI fallbacks ────────────────


@pytest.mark.unit
class TestGraphStoreUriSeams:
    """Edge cases at the URI-derivation seam not covered by TestGraphStoreUri.

    TestGraphStoreUri already covers the happy NDB/NA/wss/host/prefix shapes;
    these target the *fallback* branches in ``_graph_store_uri`` /
    ``_is_neptune_analytics`` (the error-handling table's "NA endpoint not
    recognized → NDB form" row and host-normalization edges).
    """

    def test_na_prefix_without_extractable_graph_id_falls_back_to_cleaned(self):
        """A ``neptune-graph://`` URI whose tail is not a ``g-…`` id still yields
        a well-formed ``neptune-graph://<cleaned>`` rather than crashing — the
        defensive fallback branch in ``_graph_store_uri``."""
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("neptune-graph://my-custom-graph-name")
        assert uri == "neptune-graph://my-custom-graph-name"

    def test_unrecognized_endpoint_falls_back_to_ndb_form(self):
        """An endpoint matching no NA signal degrades to the NDB URI form
        (preserves prior behavior — error-handling table row)."""
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("some-internal-host.example.com")
        assert uri == "neptune-db://some-internal-host.example.com:8182"

    def test_ndb_host_trailing_slash_is_normalized(self):
        """A trailing slash on an NDB endpoint is stripped before the host:port
        is assembled (no empty path segment leaks into the URI)."""
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("https://cluster.us-east-1.neptune.amazonaws.com/")
        assert uri == "neptune-db://cluster.us-east-1.neptune.amazonaws.com:8182"

    def test_ndb_endpoint_with_port_8182_is_not_neptune_analytics(self):
        """An endpoint carrying an explicit ``:8182`` NDB port is treated as NDB
        even when a valid ``g-…`` graph-id substring is present — the port
        disambiguates it away from the NA branch."""
        from coa_serve.clients.factory import _is_neptune_analytics

        assert _is_neptune_analytics("g-abc1234567.host.example.com:8182") is False

    def test_bare_na_graph_id_detected(self):
        """A bare ``g-…`` id (no host, no scheme, no port) is detected as NA."""
        from coa_serve.clients.factory import _is_neptune_analytics

        assert _is_neptune_analytics("g-abc1234567") is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            "g-abc1234567.us-east-1.neptune-graph.amazonaws.com",
            "https://g-abc1234567.us-east-1.neptune-graph.amazonaws.com",
            "neptune-graph.amazonaws.com",
        ],
    )
    def test_real_na_hosts_detected(self, endpoint):
        from coa_serve.clients.factory import _is_neptune_analytics

        assert _is_neptune_analytics(endpoint) is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            # Suffix-extension look-alike: the real host is other.test.
            "https://neptune-graph.amazonaws.com.other.test/graph",
            # Service host present only in the path.
            "https://other.test/neptune-graph.amazonaws.com",
            # Label-boundary look-alike (not a ".neptune-graph…" subdomain).
            "https://evilneptune-graph.amazonaws.com/graph",
        ],
    )
    def test_na_host_match_is_anchored_to_the_host(self, endpoint):
        """A substring test would route these to the Neptune Analytics client.

        These carry no ``g-…`` graph-id, so with a host-anchored check they fall
        through to the NDB branch instead of selecting the wrong backend.
        """
        from coa_serve.clients.factory import _is_neptune_analytics

        assert _is_neptune_analytics(endpoint) is False


# ── Property 4 seam gap — Tier-3 resolve-boundary degradation ───────


@pytest.mark.unit
class TestResolveBoundaryDegradation:
    """The defensive ``try/except`` seam in ``_resolve_via_lexical``.

    The retriever-level timeout/degradation is covered in test_lexical_baseline.
    This covers the SECOND layer required by 2.9: even if the lexical retriever
    raises an *unhandled* exception (rather than returning an empty result), the
    Tier-3 resolve must still succeed with empties + an error trace step, never
    propagating the failure out of Tier 3.
    """

    async def test_unexpected_raise_from_retrieve_degrades_to_empties(self):
        from coa_serve.lexical.strategies import RetrieverStrategy
        from coa_serve.tier3.knowledge_retriever import (
            KnowledgeRetriever,
            Tier3Result,
        )

        mock_lexical = AsyncMock()
        mock_lexical.retrieve.side_effect = RuntimeError("toolkit blew up")

        retriever = KnowledgeRetriever(
            vector_retriever=None,
            graph_traverser=None,
            synthesizer=make_synthesizer(answer="degraded answer"),
            lexical_retriever=mock_lexical,
        )

        result = await retriever.resolve("q", "ns", retriever_strategy=RetrieverStrategy.CHUNK_BASED_SEMANTIC)

        # Tier 3 still resolved (did not raise) with empty supporting content.
        assert isinstance(result, Tier3Result)
        assert result.supporting_content == ()
        assert result.graph_context == ()
        assert result.synthesized_answer == "degraded answer"
        # An error trace step attributes the lexical failure.
        lexical_steps = [s for s in result.trace_steps if s["step"] == "lexical_baseline_retrieve"]
        assert lexical_steps
        assert lexical_steps[0]["status"] == "error"

    async def test_unexpected_raise_records_strategy_in_error_detail(self):
        """When a strategy was resolved, the resolve-boundary error trace detail
        attributes it (so logs/eval can still attribute the failed attempt)."""
        from coa_serve.lexical.strategies import RetrieverStrategy
        from coa_serve.tier3.knowledge_retriever import KnowledgeRetriever

        mock_lexical = AsyncMock()
        mock_lexical.retrieve.side_effect = RuntimeError("boom")

        retriever = KnowledgeRetriever(
            vector_retriever=None,
            graph_traverser=None,
            synthesizer=make_synthesizer(),
            lexical_retriever=mock_lexical,
        )

        result = await retriever.resolve("q", "ns", retriever_strategy=RetrieverStrategy.TRAVERSAL)

        lexical_steps = [s for s in result.trace_steps if s["step"] == "lexical_baseline_retrieve"]
        assert lexical_steps[0]["status"] == "error"
        assert "strategy=traversal" in lexical_steps[0]["detail"]


# ── Property 7 seam gap — version-pin invariant (3.4) ───────────────


@pytest.mark.unit
class TestVersionPinInvariant:
    """Requirement 3.3 / 3.4 / Property 7: ``graphrag-lexical-graph`` stays pinned
    to ``==3.20.0`` (in sync with kg-build's requirements.txt, the root
    ``uv.lock``, and context-manager's own pin). No prior unit test asserted the
    pin; a silent bump would break reader/builder version parity."""

    def test_graphrag_lexical_graph_pinned_to_expected_version(self):
        from importlib.metadata import version

        assert version("graphrag-lexical-graph") == "3.20.0"


# ── Cross-primitive seam: registry build_engine ↔ adapter cache ─────


@pytest.mark.unit
class TestStrategyBuildEngineSeam:
    """The seam between the strategy registry's ``build_engine`` callable and the
    adapter's ``_get_engine`` cache: a custom spec's ``build_engine`` is invoked
    with the derived ``tenant_id`` and its return value is what gets cached.

    test_lexical_baseline covers this for the *registry* specs (via the toolkit
    factory mocks); this covers it for an arbitrary ``StrategySpec`` through the
    shared ``make_strategy_spec`` factory, proving the adapter is agnostic to how
    a spec builds its engine (no toolkit import needed)."""

    def test_get_engine_invokes_spec_build_engine_with_tenant_id(self):
        from coa_common.constants import to_graphrag_tenant_id

        calls: list[dict] = []

        def fake_build_engine(graph_store, vector_store, *, tenant_id):
            calls.append(
                {
                    "graph_store": graph_store,
                    "vector_store": vector_store,
                    "tenant_id": tenant_id,
                }
            )
            return f"engine-for-{tenant_id}"

        spec = make_strategy_spec(build_engine=fake_build_engine)
        retriever = make_baseline_retriever(strategy=spec)
        namespace = "550e8400-e29b-41d4-a716-446655440000"

        # Patch only the storage factories (lazily imported inside _get_engine);
        # the spec's build_engine is our stub, so no graphrag engine is built.
        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
        ):
            engine = retriever._get_engine(namespace, spec)

        expected_tenant = to_graphrag_tenant_id(namespace)
        assert engine == f"engine-for-{expected_tenant}"
        assert len(calls) == 1
        assert calls[0]["tenant_id"] == expected_tenant
        # Cached under (tenant_id, strategy.name).
        assert (expected_tenant, spec.name) in retriever._engines

    def test_make_strategy_spec_defaults_to_registry_spec(self):
        """Sanity: the shared factory returns the real registry spec when not
        overridden (so suites reusing it exercise the shipping spec)."""
        from coa_serve.lexical.strategies import (
            DEFAULT_SPEC,
            RetrieverStrategy,
        )

        assert make_strategy_spec() is DEFAULT_SPEC
        assert make_strategy_spec(RetrieverStrategy.TRAVERSAL).name is RetrieverStrategy.TRAVERSAL


# ── Shared-helper integrity (consolidation guard) ───────────────────


@pytest.mark.unit
class TestSharedHelperIntegrity:
    """Guard the consolidated helpers so the co-located suites can rely on them
    as one source of truth (task 9 deliverable 1)."""

    def test_toolkit_node_builder_shape(self):
        node = make_toolkit_node(node_id="x1", text="t", score=0.7, metadata={"k": "v"})
        assert node.node_id == "x1"
        assert node.text == "t"
        assert node.score == 0.7
        assert node.metadata == {"k": "v"}
        # Plain object: attribute access does NOT auto-create like a MagicMock.
        assert not hasattr(node, "definitely_absent_attr")

    def test_endpoint_constants_match_derived_uris(self):
        """The NDB/NA endpoint fixtures agree with the factory's actual output,
        so suites asserting against the constants stay truthful."""
        from coa_serve.clients.factory import _graph_store_uri

        from .lexical_helpers import (
            NA_ENDPOINT,
            NA_GRAPH_STORE_URI,
            NDB_ENDPOINT,
            NDB_GRAPH_STORE_URI,
        )

        assert _graph_store_uri(NDB_ENDPOINT) == NDB_GRAPH_STORE_URI
        assert _graph_store_uri(NA_ENDPOINT) == NA_GRAPH_STORE_URI
