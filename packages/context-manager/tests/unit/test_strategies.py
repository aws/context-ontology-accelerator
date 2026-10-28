# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the selectable retriever-strategy registry.

Tests cover the strategy registry implemented in
``coa_serve.lexical.strategies``:

- ``resolve_strategy`` precedence (request > deployment > default), default of
  ``chunk_based_semantic``, unrecognized-request fall-through, and
  unrecognized-deployment warn-and-fall-back behavior.
- The exposed set is EXACTLY the seven non-deprecated graphrag-toolkit
  retrievers — no extras, no deprecated names (this enforces the task-16
  cleanup). Each maps to its retriever class and the ``traversal`` engine
  family; the default is ``chunk_based_semantic`` -> ``ChunkBasedSemanticSearch``.
- No builder uses the deprecated semantic-guided engine entry point.
- ``StrategySpec`` immutability (frozen dataclass).
- The lazy-import invariant: importing the strategies module does NOT trigger
  the ``graphrag_toolkit`` import chain (verified in an isolated subprocess so
  the package ``__init__``'s eager ``baseline_retriever`` import does not mask
  the module's own laziness).
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import structlog
from coa_serve.lexical.strategies import (
    DEFAULT_SPEC,
    DEFAULT_STRATEGY,
    STRATEGY_REGISTRY,
    RetrieverStrategy,
    resolve_strategy,
)

# Absolute path to strategies.py, derived from this test file's location:
#   packages/context-manager/tests/unit/test_strategies.py
#   packages/context-manager/src/coa_serve/lexical/strategies.py
_STRATEGIES_PATH = Path(__file__).resolve().parents[2] / "src" / "coa_serve" / "lexical" / "strategies.py"

# The canonical seven non-deprecated retrievers (task 16): strategy name -> the
# exact ordered list of graphrag-toolkit retriever class names each builder
# wires through ``for_traversal_based_search``. This is the single source of
# truth the cleanup is enforced against.
EXPECTED_RETRIEVERS: dict[RetrieverStrategy, list[str]] = {
    RetrieverStrategy.CHUNK_BASED: ["ChunkBasedSearch"],
    RetrieverStrategy.CHUNK_BASED_SEMANTIC: ["ChunkBasedSemanticSearch"],
    RetrieverStrategy.ENTITY_BASED: ["EntityBasedSearch"],
    RetrieverStrategy.ENTITY_CONTEXT: ["EntityContextSearch"],
    RetrieverStrategy.ENTITY_NETWORK: ["EntityNetworkSearch"],
    RetrieverStrategy.TOPIC_BASED: ["TopicBasedSearch"],
    RetrieverStrategy.TRAVERSAL: [
        "ChunkBasedSearch",
        "EntityNetworkSearch",
        "TopicBasedSearch",
    ],
}

# The traversal-family strategies (everything except topic_beam, which is
# constructed directly as the engine retriever). The engine-builder invariants below are
# specific to the traversal factory, so they parametrize over this set; topic_beam
# has its own coverage in TestTopicBeamStrategy.
_TRAVERSAL_STRATEGIES = [s for s in RetrieverStrategy if s is not RetrieverStrategy.TOPIC_BEAM]


# ── resolve_strategy precedence ─────────────────────────────────────


@pytest.mark.unit
class TestResolveStrategyPrecedence:
    def test_request_value_wins_over_deployment_default(self):
        """A valid request value takes precedence over the deployment default."""
        resolved = resolve_strategy(
            request_value="traversal",
            config_default="chunk_based_semantic",
        )
        assert resolved is RetrieverStrategy.TRAVERSAL

    def test_deployment_default_used_when_request_absent(self):
        """With no request value, the deployment default is used."""
        resolved = resolve_strategy(
            request_value=None,
            config_default="entity_based",
        )
        assert resolved is RetrieverStrategy.ENTITY_BASED

    def test_no_request_and_no_deployment_default_returns_none(self):
        """No request value and no deployment default (None) → None: no graphrag
        strategy is engaged, so the caller runs the hand-rolled path. This is the
        default hand-rolled deployment with no per-request retrieverStrategy."""
        resolved = resolve_strategy(request_value=None, config_default=None)
        assert resolved is None

    def test_request_value_engages_strategy_without_deployment_default(self):
        """A per-request value engages graphrag on-demand even when the
        deployment default is None (hand-rolled deployment)."""
        resolved = resolve_strategy(request_value="traversal", config_default=None)
        assert resolved is RetrieverStrategy.TRAVERSAL

    def test_unrecognized_request_with_no_default_returns_none(self):
        """An unrecognized request value is treated as absent; with no deployment
        default that resolves to None (hand-rolled)."""
        resolved = resolve_strategy(request_value="bogus", config_default=None)
        assert resolved is None

    def test_none_none_falls_through_to_default(self):
        """No request value and an empty deployment default → documented default."""
        resolved = resolve_strategy(request_value=None, config_default="")
        assert resolved is DEFAULT_STRATEGY
        assert resolved is RetrieverStrategy.CHUNK_BASED_SEMANTIC

    def test_unrecognized_request_value_treated_as_absent(self):
        """An unrecognized request value falls through to the deployment default."""
        resolved = resolve_strategy(
            request_value="not-a-real-strategy",
            config_default="traversal",
        )
        assert resolved is RetrieverStrategy.TRAVERSAL

    def test_unrecognized_request_logs_warning_then_falls_through(self):
        """The unrecognized request value warns and falls through to deployment."""
        with structlog.testing.capture_logs() as logs:
            resolved = resolve_strategy(
                request_value="bogus",
                config_default="chunk_based_semantic",
            )
        assert resolved is RetrieverStrategy.CHUNK_BASED_SEMANTIC
        events = [entry.get("event") for entry in logs]
        assert "unrecognized_request_retriever_strategy" in events

    def test_unrecognized_deployment_default_warns_and_falls_back(self):
        """An unrecognized deployment default warns and falls back to DEFAULT_STRATEGY."""
        with structlog.testing.capture_logs() as logs:
            resolved = resolve_strategy(
                request_value=None,
                config_default="garbage-strategy",
            )
        assert resolved is DEFAULT_STRATEGY
        warn_entries = [entry for entry in logs if entry.get("event") == "invalid_lexical_retriever_strategy"]
        assert warn_entries, "expected an invalid_lexical_retriever_strategy warning"
        assert warn_entries[0]["config_default"] == "garbage-strategy"

    def test_default_is_chunk_based_semantic(self):
        """The documented default strategy is chunk_based_semantic."""
        assert DEFAULT_STRATEGY is RetrieverStrategy.CHUNK_BASED_SEMANTIC
        assert DEFAULT_STRATEGY.value == "chunk_based_semantic"


# ── Exactly the seven non-deprecated retrievers (cleanup enforcement) ─


@pytest.mark.unit
class TestExactlyExpectedStrategies:
    """The enum and registry contain EXACTLY the expected retrievers — the seven
    traversal retrievers plus ``topic_beam`` (TopicGraphRAG, graphrag 3.18.7). No
    other extras, no deprecated dashed aliases. Guards against accidental additions."""

    EXPECTED_NAMES = {
        "chunk_based",
        "chunk_based_semantic",
        "entity_based",
        "entity_context",
        "entity_network",
        "topic_based",
        "traversal",
        "topic_beam",
    }

    # Dashed/aliased spellings that must NOT survive: the benchmark uses
    # "topic-beam-chunk_only" (a CLI string), but our enum value is the plain
    # snake_case "topic_beam" — the dashed forms and the raw factory name must not leak in.
    FORBIDDEN_NAMES = {
        "topic-beam-chunk_only",
        "topic_beam_chunk_only",
        "TOPIC_BEAM_CHUNK_ONLY",
        "semantic_guided",
    }

    def test_enum_has_exactly_the_expected_values(self):
        assert {s.value for s in RetrieverStrategy} == self.EXPECTED_NAMES

    def test_enum_has_exactly_eight_members(self):
        assert len(list(RetrieverStrategy)) == 8

    def test_registry_has_exactly_the_expected_keys(self):
        assert {s.value for s in STRATEGY_REGISTRY} == self.EXPECTED_NAMES
        assert len(STRATEGY_REGISTRY) == 8

    def test_no_deprecated_names_in_enum(self):
        values = {s.value for s in RetrieverStrategy}
        names = {s.name for s in RetrieverStrategy}
        for forbidden in self.FORBIDDEN_NAMES:
            assert forbidden not in values
            assert forbidden not in names

    def test_registry_keys_match_enum_members_exactly(self):
        assert set(STRATEGY_REGISTRY.keys()) == set(RetrieverStrategy)


# ── STRATEGY_REGISTRY structure ─────────────────────────────────────


@pytest.mark.unit
class TestStrategyRegistry:
    def test_traversal_strategies_map_to_traversal_family(self):
        """The seven traversal retrievers build through ``for_traversal_based_search``.

        ``topic_beam`` (TopicGraphRAG) is the sole exception — it subclasses
        ``TraversalBasedBaseRetriever`` and is constructed as the engine's retriever
        directly (engine_family="direct"), matching the graphrag-toolkit benchmark.
        Building it through ``for_semantic_guided_search`` returned the "No relevant
        context" sentinel on a healthy graph."""
        for strategy, spec in STRATEGY_REGISTRY.items():
            if strategy is RetrieverStrategy.TOPIC_BEAM:
                assert spec.engine_family == "direct", strategy
            else:
                assert spec.engine_family == "traversal", strategy

    def test_registry_covers_all_strategy_members(self):
        """Every RetrieverStrategy member has a registry entry (and vice versa)."""
        assert set(STRATEGY_REGISTRY.keys()) == set(RetrieverStrategy)

    def test_registry_spec_name_matches_key(self):
        """Each spec's name matches the key it is registered under."""
        for strategy, spec in STRATEGY_REGISTRY.items():
            assert spec.name is strategy

    def test_build_engine_is_callable(self):
        for spec in STRATEGY_REGISTRY.values():
            assert callable(spec.build_engine)

    def test_default_spec_is_spec_for_default_strategy(self):
        assert DEFAULT_SPEC is STRATEGY_REGISTRY[DEFAULT_STRATEGY]
        assert DEFAULT_SPEC.name is RetrieverStrategy.CHUNK_BASED_SEMANTIC


# ── StrategySpec immutability ───────────────────────────────────────


@pytest.mark.unit
class TestStrategySpecFrozen:
    def test_mutation_raises_frozen_instance_error(self):
        spec = STRATEGY_REGISTRY[RetrieverStrategy.TRAVERSAL]
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.engine_family = "traversal"  # type: ignore[misc]

    def test_name_mutation_raises_frozen_instance_error(self):
        spec = DEFAULT_SPEC
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = RetrieverStrategy.TRAVERSAL  # type: ignore[misc]


# ── Engine-builder construction ─────────────────────────────────────


@pytest.mark.unit
class TestEngineBuilderConstruction:
    """Every one of the seven builders constructs a toolkit engine via the
    non-deprecated ``for_traversal_based_search`` entry point, wiring its
    canonical retriever class(es) and ``post_processors=[]`` /
    ``context_format="raw"`` (no ``BedrockContextFormat``, which raises
    ``KeyError: 'source'`` on the nested traversal node shape; the adapter's
    ``_map_results`` parses the raw nodes directly).

    Builders import ``graphrag_toolkit`` lazily, so only
    ``LexicalGraphQueryEngine`` is patched; the retriever classes /
    ``WeightedTraversalBasedRetriever`` (a side-effect-free dataclass) import for
    real, letting us assert the concrete retriever identity by name.
    """

    def _build(self, strategy: RetrieverStrategy):
        """Invoke the registry builder for ``strategy`` with the toolkit factory
        patched; return the patched ``LexicalGraphQueryEngine`` mock."""
        spec = STRATEGY_REGISTRY[strategy]
        with patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine:
            spec.build_engine(MagicMock(), MagicMock(), tenant_id="t1")
        return mock_engine

    @staticmethod
    def _retriever_names(retrievers) -> list[str]:
        """Class names of the retrievers in a builder's ``retrievers=`` kwarg.

        Each entry is either a retriever class (``ChunkBasedSearch``) or a
        ``WeightedTraversalBasedRetriever`` wrapping one; unwrap the latter.
        """
        names: list[str] = []
        for r in retrievers:
            wrapped = getattr(r, "retriever", r)  # unwrap WeightedTraversalBasedRetriever
            names.append(getattr(wrapped, "__name__", type(wrapped).__name__))
        return names

    @pytest.mark.parametrize("strategy", _TRAVERSAL_STRATEGIES)
    def test_builder_uses_traversal_entry_point_with_canonical_retrievers(self, strategy):
        """Each strategy builds via ``for_traversal_based_search`` (never the
        semantic-guided entry point) and wires exactly its canonical retriever
        class(es)."""
        mock_engine = self._build(strategy)

        mock_engine.for_traversal_based_search.assert_called_once()
        # The deprecated semantic-guided entry point is never used.
        mock_engine.for_semantic_guided_search.assert_not_called()

        _, kwargs = mock_engine.for_traversal_based_search.call_args
        assert self._retriever_names(kwargs["retrievers"]) == EXPECTED_RETRIEVERS[strategy]

    def test_default_builder_uses_chunk_based_semantic_search(self):
        """The default (``chunk_based_semantic``) builds with the
        ``ChunkBasedSemanticSearch`` retriever (the 11.b ``ChunkBasedSearch``
        workaround is reverted)."""
        mock_engine = self._build(RetrieverStrategy.CHUNK_BASED_SEMANTIC)

        _, kwargs = mock_engine.for_traversal_based_search.call_args
        names = self._retriever_names(kwargs["retrievers"])
        assert names == ["ChunkBasedSemanticSearch"]

    @pytest.mark.parametrize("strategy", _TRAVERSAL_STRATEGIES)
    def test_builder_drops_bedrock_context_format(self, strategy):
        """No builder attaches ``BedrockContextFormat``: every one passes an
        empty ``post_processors`` and ``context_format="raw"``."""
        mock_engine = self._build(strategy)

        _, kwargs = mock_engine.for_traversal_based_search.call_args
        assert kwargs["post_processors"] == [], f"{strategy} attached post_processors"
        assert kwargs["context_format"] == "raw", f"{strategy} did not use raw context_format"

    @pytest.mark.parametrize("strategy", _TRAVERSAL_STRATEGIES)
    def test_builder_passes_tenant_id_through(self, strategy):
        mock_engine = self._build(strategy)
        _, kwargs = mock_engine.for_traversal_based_search.call_args
        assert kwargs["tenant_id"] == "t1"

    @pytest.mark.parametrize("strategy", _TRAVERSAL_STRATEGIES)
    def test_builder_pins_benchmarked_processor_args(self, strategy):
        """Every builder forwards the pinned benchmarked ProcessorArgs so retrieval
        matches the toolkit team's benchmark config and cannot silently drift on a
        toolkit version bump (reproducibility). ``for_traversal_based_search``
        forwards these kwargs into ``ProcessorArgs``."""
        mock_engine = self._build(strategy)
        _, kwargs = mock_engine.for_traversal_based_search.call_args
        assert kwargs["reranker"] == "tfidf", f"{strategy} did not pin reranker=tfidf"
        assert kwargs["vss_top_k"] == 20, f"{strategy} did not pin vss_top_k=20"
        assert kwargs["max_search_results"] == 10, f"{strategy} did not pin max_search_results=10"
        assert kwargs["max_statements"] == 200, f"{strategy} did not pin max_statements=200"
        assert kwargs["derive_subqueries"] is False, f"{strategy} did not pin derive_subqueries=False"

    def test_no_builder_references_semantic_guided_entry_point(self):
        """Belt-and-suspenders: invoking any builder never calls the deprecated
        ``for_semantic_guided_search`` factory entry point."""
        for strategy in _TRAVERSAL_STRATEGIES:
            spec = STRATEGY_REGISTRY[strategy]
            with patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine:
                spec.build_engine(MagicMock(), MagicMock(), tenant_id="t1")
            mock_engine.for_semantic_guided_search.assert_not_called()
            mock_engine.for_traversal_based_search.assert_called_once()


# ── topic_beam (TopicGraphRAG) builder ──────────────────────────────


@pytest.mark.unit
class TestTopicBeamStrategy:
    """topic_beam is constructed as the engine's retriever directly, with the
    3.18.7-valid subset of the benchmark's topic-beam config."""

    def _build(self):
        """Build the topic_beam engine with the toolkit boundary mocked.

        The store WRAPPERS must be patched too: the builder wraps the stores itself
        (MultiTenant + ReadOnly) because it passes the retriever as an INSTANCE, and
        those wrappers are real pydantic models that reject a MagicMock store with a
        ValidationError. Patched at their DEFINITION modules, which is where the
        builder imports them from.
        """
        spec = STRATEGY_REGISTRY[RetrieverStrategy.TOPIC_BEAM]
        with (
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
            patch("graphrag_toolkit.lexical_graph.retrieval.retrievers.TopicBeamSearch") as mock_tb,
            patch(
                "graphrag_toolkit.lexical_graph.storage.graph.multi_tenant_graph_store.MultiTenantGraphStore"
            ) as mock_mtg,
            patch(
                "graphrag_toolkit.lexical_graph.storage.vector.multi_tenant_vector_store.MultiTenantVectorStore"
            ) as mock_mtv,
            patch(
                "graphrag_toolkit.lexical_graph.storage.vector.read_only_vector_store.ReadOnlyVectorStore"
            ) as mock_ro,
        ):
            spec.build_engine(MagicMock(), MagicMock(), tenant_id="t1")
        return mock_engine, mock_tb, mock_mtg, mock_mtv, mock_ro

    def test_constructs_engine_directly_with_retriever(self):
        """NOT via a factory. TopicBeamSearch subclasses TraversalBasedBaseRetriever, and
        for_semantic_guided_search ignored that contract — the engine returned the
        "No relevant context" sentinel (1 node, confidence 0.0) on a healthy graph where
        chunk_based_semantic and topic_based both answered."""
        mock_engine, mock_tb, _mtg, _mtv, _ro = self._build()
        mock_engine.for_semantic_guided_search.assert_not_called()
        mock_engine.for_traversal_based_search.assert_not_called()
        mock_engine.assert_called_once()
        _, kwargs = mock_engine.call_args
        assert kwargs["tenant_id"] == "t1"
        assert kwargs["retriever"] is mock_tb.return_value

    def test_topic_beam_params_match_benchmark_base_config(self):
        """All three neighbour strategies ON (the benchmark harness base config); the
        base beam config is width=100/depth=6/topk=50/path_weighted.

        The narrow chunk_only variant starved the beam on our graph: 10 good topic
        seeds still yielded only 1 node."""
        _, mock_tb, _mtg, _mtv, _ro = self._build()
        mock_tb.assert_called_once()
        _, kw = mock_tb.call_args
        assert kw["beam_width"] == 100
        assert kw["max_depth"] == 6
        assert kw["top_k"] == 50
        assert kw["scoring_mode"] == "path_weighted"
        assert kw["use_same_chunk_neighbors"] is True
        assert kw["use_entity_neighbors"] is True
        assert kw["use_adjacent_chunk_neighbors"] is True

    def test_engine_family_is_direct(self):
        assert STRATEGY_REGISTRY[RetrieverStrategy.TOPIC_BEAM].engine_family == "direct"

    def test_statement_cap_applied_once_in_processor_args(self):
        """Cap in ProcessorArgs (10/topic), not also on the retriever — capping in both
        places truncates twice and starves the beam."""
        _, mock_tb, _mtg, _mtv, _ro = self._build()
        _, kw = mock_tb.call_args
        assert kw["max_statements_per_topic"] == 9999
        assert kw["processor_args"].max_statements_per_topic == 10

    def test_processor_args_request_chunk_details(self):
        """The retriever's own ProcessorArgs must ask for chunk details.

        Without ``include_chunk_details`` the toolkit's statements query returns each
        chunk as ``{chunkId, value: NULL, metadata: {}}``: no source node carries any
        text, so every one is dropped as anonymous and topic_beam answers with no
        citations, where main returned them for the same query."""
        _, mock_tb, _mtg, _mtv, _ro = self._build()
        _, kw = mock_tb.call_args
        assert kw["processor_args"].include_chunk_details is True

    def test_stores_are_wrapped_before_the_retriever_is_constructed(self):
        """The builder must wrap the stores ITSELF (MultiTenant + ReadOnly).

        ``LexicalGraphQueryEngine`` only threads its own wrapped stores into a
        retriever it INSTANTIATES; an instance is taken as-is. Passing raw stores
        therefore (a) queried the untenanted ``topic`` index instead of
        ``topic_<tenant>`` and (b) left ``writeable`` True, so the toolkit tried to
        CREATE the index — which AOSS NEXTGEN rejects. Both symptoms surfaced as
        topic_beam returning ~1 node at confidence 0.0.
        """
        _, mock_tb, mock_mtg, mock_mtv, mock_ro = self._build()

        mock_mtg.wrap.assert_called_once()
        mock_mtv.wrap.assert_called_once()
        mock_ro.wrap.assert_called_once()

        # to_tenant_id must be applied BEFORE wrap: wrap compares against the
        # default-tenant sentinel and needs a real TenantId, not the raw string.
        _, tenant_arg = mock_mtg.wrap.call_args[0]
        assert tenant_arg is not None
        assert tenant_arg != "t1", "raw tenant string reached wrap() — to_tenant_id was not applied"

        # The retriever receives the WRAPPED stores, not the originals.
        _, kw = mock_tb.call_args
        assert kw["graph_store"] is mock_mtg.wrap.return_value
        assert kw["vector_store"] is mock_ro.wrap.return_value


# ── Source-level cleanup audit ──────────────────────────────────────


@pytest.mark.unit
class TestSourceLevelCleanup:
    """Audit the strategies source itself: NO strategy calls the deprecated
    semantic-guided factory, and no deprecated builder function survives."""

    def test_no_strategy_uses_semantic_guided_entry_point(self):
        import coa_serve.lexical.strategies as strat_mod

        # No leftover deprecated builder function object.
        assert not hasattr(strat_mod, "_build_topic_beam_chunk_only_engine")

        # Zero active calls to for_semantic_guided_search. topic_beam used to be the
        # one caller; that was the bug (it is a traversal retriever, and the factory
        # made it return the "No relevant context" sentinel). Ignore comment prose.
        source = _STRATEGIES_PATH.read_text()
        call_lines = [
            line
            for line in source.splitlines()
            if "LexicalGraphQueryEngine.for_semantic_guided_search(" in line and not line.lstrip().startswith("#")
        ]
        assert call_lines == [], f"no strategy may use the semantic-guided factory, got {call_lines}"


# ── Lazy-import invariant ───────────────────────────────────────────


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_strategies_does_not_import_graphrag(self):
        """Importing the strategies module must NOT import graphrag_toolkit.

        The parent package ``lexical/__init__`` eagerly imports graphrag via
        ``baseline_retriever``, so to test the strategies module's OWN laziness
        we load the module file directly (by path, under a standalone name) in
        a fresh subprocess so the package ``__init__``'s eager chain is never
        triggered, then assert ``graphrag_toolkit`` is absent from
        ``sys.modules`` afterwards.
        """
        code = (
            "import sys, importlib.util; "
            f"spec = importlib.util.spec_from_file_location('strategies_isolated', {str(_STRATEGIES_PATH)!r}); "
            "mod = importlib.util.module_from_spec(spec); "
            "sys.modules['strategies_isolated'] = mod; "
            "spec.loader.exec_module(mod); "
            "leaked = sorted(m for m in sys.modules if 'graphrag' in m); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"strategies module import leaked graphrag_toolkit or failed.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
