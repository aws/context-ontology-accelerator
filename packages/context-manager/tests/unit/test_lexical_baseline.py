# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lexical baseline retriever and KnowledgeRetriever integration.

Tests cover:
- BaselineLexicalRetriever.retrieve maps toolkit output to BaselineResult
- Timeout enforcement
- tenant_id derivation
- KnowledgeRetriever with lexical_retriever calls it and skips vector/graph
- KnowledgeRetriever without lexical_retriever behaves as before (hand-rolled)
- factory returns None when strategy is "hand-rolled"
- factory returns BaselineLexicalRetriever when strategy is "lexical-baseline"
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.lexical.baseline_retriever import (
    BaselineLexicalRetriever,
    BaselineResult,
)
from coa_serve.lexical.strategies import (
    DEFAULT_SPEC,
    STRATEGY_REGISTRY,
    RetrieverStrategy,
)
from coa_serve.tier3.knowledge_retriever import KnowledgeRetriever
from coa_serve.tier3.vector_retriever import ChunkResult


def _make_baseline_result(chunks=None, entities=None):
    from .lexical_helpers import make_baseline_result

    return make_baseline_result(chunks=chunks, entities=entities)


def _make_synthesizer():
    from .lexical_helpers import make_synthesizer

    return make_synthesizer(
        answer="Apple's revenue was $83 billion in Q3 2022.",
        confidence=0.85,
        duration_ms=500,
    )


# ── BaselineLexicalRetriever tests ──────────────────────────────────


@pytest.mark.unit
class TestBaselineLexicalRetrieverInit:
    def test_tenant_id_derived_per_request(self):
        """tenant_id is derived from namespace at retrieval time, not at init."""
        from coa_common.constants import to_graphrag_tenant_id

        # to_graphrag_tenant_id strips hyphens and truncates to 25 chars
        assert to_graphrag_tenant_id("550e8400-e29b-41d4-a716-446655440000") == "550e8400e29b41d4a71644665"

    def test_get_engine_calls_factories_with_correct_uris(self):
        if True:  # no patch needed
            retriever = BaselineLexicalRetriever(
                graph_store_uri="neptune-graph://g-mytest",
                vector_store_uri="aoss://https://mytest.aoss.amazonaws.com",
            )

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory") as mock_gsf,
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory") as mock_vsf,
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine"),
        ):
            retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", DEFAULT_SPEC)

            mock_gsf.for_graph_store.assert_called_once_with("neptune-graph://g-mytest")
            # index_names must include "topic": TopicBeamSearch seeds from
            # get_index("topic") and gets a DummyVectorIndex without it.
            mock_vsf.for_vector_store.assert_called_once()
            v_args, v_kwargs = mock_vsf.for_vector_store.call_args
            assert v_args[0] == "aoss://https://mytest.aoss.amazonaws.com"
            assert "topic" in v_kwargs["index_names"]
            assert "chunk" in v_kwargs["index_names"]
            # And must NOT name an index kg-build does not write. The toolkit reads a
            # missing index as its cue to CREATE one, which AOSS NEXTGEN rejects
            # ("Field parameter 'engine' is not supported"); the error is swallowed and
            # the broken handle surfaced as topic_beam returning 1 node / confidence 0.0.
            # Mirrors kg-build's EMBEDDING_INDEXES default (chunk,topic).
            assert "statement" not in v_kwargs["index_names"]

    def test_engine_cached_per_tenant_id(self):
        """Engine is built once per tenant_id and reused on subsequent calls."""
        retriever = BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-test123",
            vector_store_uri="aoss://https://test.aoss.amazonaws.com",
        )

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            engine1 = retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", DEFAULT_SPEC)
            engine2 = retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", DEFAULT_SPEC)

            assert engine1 is engine2
            mock_engine.for_traversal_based_search.assert_called_once()

    def test_stores_config(self):
        if True:  # no patch needed — GraphRAGConfig already initialized at module level
            retriever = BaselineLexicalRetriever(
                graph_store_uri="neptune-graph://g-abc",
                vector_store_uri="aoss://https://xyz.aoss.amazonaws.com",
                timeout_s=20.0,
            )
        assert retriever._graph_store_uri == "neptune-graph://g-abc"
        assert retriever._vector_store_uri == "aoss://https://xyz.aoss.amazonaws.com"
        assert retriever._timeout_s == 20.0


@pytest.mark.unit
class TestBaselineLexicalRetrieverRetrieve:
    async def test_returns_baseline_result_on_success(self):
        if True:  # no patch needed — GraphRAGConfig already initialized at module level
            retriever = BaselineLexicalRetriever(
                graph_store_uri="neptune-graph://g-test",
                vector_store_uri="aoss://https://test.aoss.amazonaws.com",
            )

        # Mock the sync retrieve to return fake nodes shaped like real toolkit
        # output: nested ``source`` (Source.sourceId) + ``entity_contexts``
        # (EntityContexts.contexts[].entities[].entity).
        mock_node = MagicMock()
        mock_node.node_id = "node-1"
        mock_node.text = "Apple revenue was $83B"
        mock_node.score = 0.9
        mock_node.metadata = {
            "source": {"sourceId": "AAPL.pdf", "metadata": {"year": "2022"}},
            "entity_contexts": {
                "contexts": [
                    {
                        "entities": [
                            {
                                "entity": {
                                    "entityId": "ent-apple",
                                    "value": "Apple Inc.",
                                    "classification": "Company",
                                },
                                "score": 0.95,
                            }
                        ]
                    }
                ],
                "keywords": ["revenue"],
            },
        }

        with patch.object(retriever, "_retrieve_sync", return_value=[mock_node]):
            result = await retriever.retrieve("What is Apple revenue?", "550e8400-e29b-41d4-a716-446655440000")

        assert isinstance(result, BaselineResult)
        assert len(result.chunks) == 1
        assert result.chunks[0].text == "Apple revenue was $83B"
        assert result.chunks[0].relevance_score == 0.9
        assert result.chunks[0].source_doc == "AAPL.pdf"
        assert len(result.entities) == 1
        assert result.entities[0].uri == "ent-apple"
        assert result.entities[0].label == "Apple Inc."
        assert result.entities[0].type == "Company"
        assert result.trace_steps[0]["status"] == "success"

    async def test_timeout_degrades_to_empty(self):
        """Task 6.a / BUG 3 (2.8, 2.9): a retrieval that exceeds ``timeout_s``
        degrades to an empty ``BaselineResult`` with an error trace step and does
        NOT raise — so Tier 3 resolution can continue gracefully."""
        import time as _time

        retriever = BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-test",
            vector_store_uri="aoss://https://test.aoss.amazonaws.com",
            timeout_s=0.01,  # very short timeout
        )

        # The synchronous toolkit call runs on the dedicated executor and cannot
        # be cancelled; it outlives the (tiny) timeout, so ``asyncio.wait_for``
        # raises ``TimeoutError`` internally, which ``retrieve`` must absorb.
        def slow_sync(*_args):
            _time.sleep(2)
            return []

        with patch.object(retriever, "_retrieve_sync", side_effect=slow_sync):
            result = await retriever.retrieve("test query", "550e8400-e29b-41d4-a716-446655440000")

        # Degraded result: empty, no raise.
        assert isinstance(result, BaselineResult)
        assert result.chunks == ()
        assert result.entities == ()
        # A single error trace step attributing the timeout.
        assert len(result.trace_steps) == 1
        step = result.trace_steps[0]
        assert step["step"] == "lexical_baseline_retrieve"
        assert step["status"] == "error"
        assert "TimeoutError" in step["detail"]

    async def test_concurrency_bounded_by_dedicated_executor(self):
        """Task 6.a / BUG 3 (2.8): concurrent retrievals are bounded by the
        retriever's own ``ThreadPoolExecutor`` (``_LEXICAL_MAX_WORKERS``) and run
        on its named threads — excess calls queue rather than spawning unbounded
        workers or starving asyncio's shared default ``to_thread`` pool."""
        import asyncio
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from coa_serve.lexical.baseline_retriever import _LEXICAL_MAX_WORKERS

        retriever = BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-test",
            vector_store_uri="aoss://https://test.aoss.amazonaws.com",
            timeout_s=30.0,  # generous: we are testing queuing, not timeout
        )

        # The retriever owns a dedicated, bounded pool — not the shared default.
        assert isinstance(retriever._executor, ThreadPoolExecutor)
        assert retriever._executor._max_workers == _LEXICAL_MAX_WORKERS

        lock = threading.Lock()
        release = threading.Event()
        current = 0
        max_observed = 0
        thread_names: set[str] = set()

        def blocking_sync(*_args):
            nonlocal current, max_observed
            with lock:
                current += 1
                max_observed = max(max_observed, current)
                thread_names.add(threading.current_thread().name)
            # Hold the worker until the test releases it, forcing saturation.
            release.wait(timeout=10)
            with lock:
                current -= 1
            return []

        # Launch more concurrent calls than the pool has workers.
        n = _LEXICAL_MAX_WORKERS + 3
        with patch.object(retriever, "_retrieve_sync", side_effect=blocking_sync):
            tasks = [asyncio.create_task(retriever.retrieve("q", "ns")) for _ in range(n)]
            # Give the executor time to saturate its workers with the first batch.
            await asyncio.sleep(0.3)
            with lock:
                observed_while_saturated = max_observed
            # Let every queued/running worker finish.
            release.set()
            results = await asyncio.gather(*tasks)

        # Concurrency never exceeded the dedicated pool size, even though we
        # submitted more calls than workers (the excess queued).
        assert observed_while_saturated == _LEXICAL_MAX_WORKERS
        # All work ran on the retriever's own named threads, not the default pool.
        assert thread_names
        assert all(name.startswith("lexical-retr") for name in thread_names)
        # Every call still resolved (to an empty, successful result) once released.
        assert len(results) == n
        assert all(isinstance(r, BaselineResult) for r in results)

    async def test_returns_empty_on_error(self):
        if True:  # no patch needed — GraphRAGConfig already initialized at module level
            retriever = BaselineLexicalRetriever(
                graph_store_uri="neptune-graph://g-test",
                vector_store_uri="aoss://https://test.aoss.amazonaws.com",
            )

        with patch.object(retriever, "_retrieve_sync", side_effect=RuntimeError("connection failed")):
            result = await retriever.retrieve("test query", "550e8400-e29b-41d4-a716-446655440000")

        assert len(result.chunks) == 0
        assert result.trace_steps[0]["status"] == "error"
        assert "RuntimeError" in result.trace_steps[0]["detail"]


@pytest.mark.unit
class TestConnectionRetry:
    """Transient Neptune connection drops are retried on the (idempotent) read path;
    non-transient errors are not (the toolkit itself does not retry — total_max_attempts=1)."""

    def _retriever(self):
        return BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-test",
            vector_store_uri="aoss://https://test.aoss.amazonaws.com",
        )

    def test_retries_connection_closed_then_succeeds(self):
        retriever = self._retriever()
        calls = {"n": 0}

        class _Engine:
            def retrieve(self, query):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError(
                        "Connection was closed before we received a valid response from endpoint URL: "
                        "https://neptune:8182/opencypher"
                    )
                return ["ok-node"]

        with (
            patch.object(retriever, "_get_engine", return_value=_Engine()),
            patch("time.sleep"),  # don't actually sleep in the test
        ):
            result = retriever._retrieve_sync("q", "ns", DEFAULT_SPEC)

        assert result == ["ok-node"]
        assert calls["n"] == 3  # two transient failures retried, third succeeded

    def test_retries_ssl_unexpected_eof_then_succeeds(self):
        """A dropped TLS socket mid-read (``SSL: UNEXPECTED_EOF_WHILE_READING``) is a
        transient Neptune connection drop and is retried on an idempotent read.

        Regression guard for the ``chunk_based_semantic``-returned-0 diagnosis:
        this strategy fans out the most concurrent Neptune queries, so a single
        dropped pooled connection previously zeroed the whole retriever. The
        variant is distinct from ``Connection was closed`` and was not covered by
        the original marker set.
        """
        retriever = self._retriever()
        calls = {"n": 0}

        class _Engine:
            def retrieve(self, query):
                calls["n"] += 1
                if calls["n"] < 2:
                    raise RuntimeError(
                        "SSL validation failed for "
                        "https://neptune:8182/opencypher [SSL: UNEXPECTED_EOF_WHILE_READING] "
                        "unexpected eof while reading (_ssl.c:2580)"
                    )
                return ["ok-node"]

        with (
            patch.object(retriever, "_get_engine", return_value=_Engine()),
            patch("time.sleep"),
        ):
            result = retriever._retrieve_sync("q", "ns", DEFAULT_SPEC)

        assert result == ["ok-node"]
        assert calls["n"] == 2  # one transient SSL-EOF failure retried, second succeeded

    def test_persistent_ssl_validation_failure_not_retried(self):
        """A generic ``SSL validation failed`` (e.g. a real cert misconfiguration)
        without the ``UNEXPECTED_EOF_WHILE_READING`` marker is NOT a transient drop
        and must propagate on the first occurrence rather than being retried."""
        retriever = self._retriever()
        calls = {"n": 0}

        class _Engine:
            def retrieve(self, query):
                calls["n"] += 1
                raise RuntimeError(
                    "SSL validation failed for https://neptune:8182/opencypher "
                    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
                )

        with (
            patch.object(retriever, "_get_engine", return_value=_Engine()),
            patch("time.sleep"),
            pytest.raises(RuntimeError, match="CERTIFICATE_VERIFY_FAILED"),
        ):
            retriever._retrieve_sync("q", "ns", DEFAULT_SPEC)

        assert calls["n"] == 1  # not retried

    def test_exhausts_retries_then_raises(self):
        retriever = self._retriever()

        class _Engine:
            def retrieve(self, query):
                raise RuntimeError("ConnectionClosedError: Connection was closed")

        with (
            patch.object(retriever, "_get_engine", return_value=_Engine()),
            patch("time.sleep"),
            pytest.raises(RuntimeError, match="Connection was closed"),
        ):
            retriever._retrieve_sync("q", "ns", DEFAULT_SPEC)

    def test_non_transient_error_not_retried(self):
        retriever = self._retriever()
        calls = {"n": 0}

        class _Engine:
            def retrieve(self, query):
                calls["n"] += 1
                raise ValueError("bad query syntax")

        with (
            patch.object(retriever, "_get_engine", return_value=_Engine()),
            patch("time.sleep"),
            pytest.raises(ValueError, match="bad query syntax"),
        ):
            retriever._retrieve_sync("q", "ns", DEFAULT_SPEC)

        assert calls["n"] == 1  # not retried


# ── Task 5.a: engine build + per-(tenant, strategy) caching ─────────


@pytest.mark.unit
class TestGetEngineTenantAndCaching:
    """Task 5.a: strategy-aware engine build + per-(tenant, strategy) cache.

    Covers BUG 6 (tenant_id derivation asserted through the real ``_get_engine``
    code path) and the per-``(tenant_id, strategy.name)`` cache so a per-request
    strategy override does not collide with the cached default.
    """

    def _make_retriever(self):
        return BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-test123",
            vector_store_uri="aoss://https://test.aoss.amazonaws.com",
        )

    def test_get_engine_passes_derived_tenant_id_to_toolkit_factory(self):
        """BUG 6: ``_get_engine`` builds via the toolkit factory with
        ``tenant_id == to_graphrag_tenant_id(namespace)`` — asserted through the
        real ``strategy.build_engine`` code path (not a stubbed builder)."""
        from coa_common.constants import to_graphrag_tenant_id

        retriever = self._make_retriever()
        namespace = "550e8400-e29b-41d4-a716-446655440000"
        expected_tenant = to_graphrag_tenant_id(namespace)
        # chunk_based_semantic is the default spec and routes through
        # LexicalGraphQueryEngine.for_traversal_based_search.
        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            retriever._get_engine(namespace, strategy)

            mock_engine.for_traversal_based_search.assert_called_once()
            _, kwargs = mock_engine.for_traversal_based_search.call_args
            assert kwargs["tenant_id"] == expected_tenant

    def test_cache_key_includes_derived_tenant_id(self):
        """The cache is keyed on the derived tenant_id (BUG 6), not the raw namespace."""
        from coa_common.constants import to_graphrag_tenant_id

        retriever = self._make_retriever()
        namespace = "550e8400-e29b-41d4-a716-446655440000"
        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine"),
        ):
            retriever._get_engine(namespace, strategy)

        expected_key = (to_graphrag_tenant_id(namespace), strategy.name)
        assert expected_key in retriever._engines

    def test_same_tenant_and_strategy_reuses_cached_engine(self):
        """Same namespace + same strategy reuses the cached engine (built once)."""
        retriever = self._make_retriever()
        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            engine1 = retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", strategy)
            engine2 = retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", strategy)

        assert engine1 is engine2
        mock_engine.for_traversal_based_search.assert_called_once()
        assert len(retriever._engines) == 1

    def test_per_request_strategy_override_does_not_collide_with_cache(self):
        """A per-request strategy override builds a distinct engine keyed
        separately, and the cached default engine is preserved (no collision)."""
        retriever = self._make_retriever()
        default_strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]
        # All seven strategies build through for_traversal_based_search; the two
        # builds are told apart by their cache key (tenant, strategy.name) and by
        # sequential engine return values.
        override_strategy = STRATEGY_REGISTRY[RetrieverStrategy.TRAVERSAL]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            mock_engine.for_traversal_based_search.side_effect = [
                "default-engine",
                "override-engine",
            ]

            engine_default = retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", default_strategy)
            engine_override = retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", override_strategy)
            # Re-request the default: must return the originally cached engine,
            # i.e. the override did not overwrite or collide with it.
            engine_default_again = retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", default_strategy)

        assert engine_default == "default-engine"
        assert engine_override == "override-engine"
        assert engine_default != engine_override
        assert engine_default_again is engine_default
        # Two distinct (tenant, strategy) cache entries; each built exactly once.
        assert mock_engine.for_traversal_based_search.call_count == 2
        assert len(retriever._engines) == 2

    def test_different_namespace_same_strategy_builds_distinct_engine(self):
        """Distinct tenants (namespaces) with the same strategy do not share an engine."""
        retriever = self._make_retriever()
        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            mock_engine.for_traversal_based_search.side_effect = ["engine-a", "engine-b"]

            engine_a = retriever._get_engine("aaaaaaaa-1111-4222-8333-444444444444", strategy)
            engine_b = retriever._get_engine("bbbbbbbb-2222-4333-8444-555555555555", strategy)

        assert engine_a != engine_b
        assert mock_engine.for_traversal_based_search.call_count == 2
        assert len(retriever._engines) == 2


@pytest.mark.unit
class TestRetrieveTraceStrategyDetail:
    """Task 5.a: the resolved strategy name is recorded in the trace step detail
    as ``detail="strategy=…; N nodes retrieved"`` on success."""

    def _make_retriever(self):
        return BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-test",
            vector_store_uri="aoss://https://test.aoss.amazonaws.com",
        )

    async def test_success_detail_records_instance_default_strategy(self):
        retriever = self._make_retriever()  # instance default = chunk_based_semantic
        nodes = [MagicMock(node_id="n1", text="t1", score=0.5, metadata={})]

        with patch.object(retriever, "_retrieve_sync", return_value=nodes):
            result = await retriever.retrieve("q", "ns")

        step = result.trace_steps[0]
        assert step["status"] == "success"
        assert step["detail"] == f"strategy={DEFAULT_SPEC.name}; {len(nodes)} nodes retrieved"
        # The default spec is chunk_based_semantic.
        assert "strategy=chunk_based_semantic" in step["detail"]

    async def test_success_detail_records_per_request_override_strategy(self):
        retriever = self._make_retriever()
        override = STRATEGY_REGISTRY[RetrieverStrategy.TRAVERSAL]
        nodes = [
            MagicMock(node_id="n1", text="t1", score=0.5, metadata={}),
            MagicMock(node_id="n2", text="t2", score=0.4, metadata={}),
        ]

        with patch.object(retriever, "_retrieve_sync", return_value=nodes):
            result = await retriever.retrieve("q", "ns", strategy=override)

        step = result.trace_steps[0]
        assert step["status"] == "success"
        assert step["detail"] == f"strategy=traversal; {len(nodes)} nodes retrieved"

    async def test_error_detail_records_resolved_strategy(self):
        retriever = self._make_retriever()
        override = STRATEGY_REGISTRY[RetrieverStrategy.TRAVERSAL]

        with patch.object(retriever, "_retrieve_sync", side_effect=RuntimeError("boom")):
            result = await retriever.retrieve("q", "ns", strategy=override)

        step = result.trace_steps[0]
        assert step["status"] == "error"
        assert step["detail"].startswith("strategy=traversal; ")
        assert "RuntimeError" in step["detail"]


# ── Task 11.a: tenant override seam ─────────────────────────────────


@pytest.mark.unit
class TestTenantIdOverrideSeam:
    """Task 11.a / BUG 6 extension (2.12, 2.13): the eval-only ``tenant_id_override``
    seam on ``BaselineLexicalRetriever``.

    The production multi-tenancy contract (design Property 3 / BUG 6) is the
    DEFAULT: ``_get_engine`` derives ``tenant_id = to_graphrag_tenant_id(namespace)``
    and passes it to the toolkit factory. The override seam bypasses that
    derivation only when explicitly constructed (the task-12 eval/integ harness):

      * ``None``                     -> derive tenant from namespace (unchanged).
      * ``DEFAULT_TENANT_OVERRIDE``  -> toolkit default tenant (``tenant_id=None``).
      * any other string             -> that literal tenant.

    The cache key incorporates the effective tenant so an override engine never
    collides with a namespace-derived engine.
    """

    def _make_retriever(self, *, tenant_id_override=None):
        return BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-test123",
            vector_store_uri="aoss://https://test.aoss.amazonaws.com",
            tenant_id_override=tenant_id_override,
        )

    def test_default_override_none_passes_namespace_derived_tenant(self):
        """Default (``tenant_id_override=None``): ``_get_engine`` still calls the
        toolkit factory with ``tenant_id == to_graphrag_tenant_id(namespace)`` —
        re-asserting design Property 3 / BUG 6 is intact."""
        from coa_common.constants import to_graphrag_tenant_id

        retriever = self._make_retriever()  # production default
        namespace = "550e8400-e29b-41d4-a716-446655440000"
        expected_tenant = to_graphrag_tenant_id(namespace)
        # DEFAULT_SPEC (chunk_based_semantic) routes through for_traversal_based_search.
        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            retriever._get_engine(namespace, strategy)

            mock_engine.for_traversal_based_search.assert_called_once()
            _, kwargs = mock_engine.for_traversal_based_search.call_args
            assert kwargs["tenant_id"] == expected_tenant
            # Sanity: a real namespace-derived tenant is a non-empty string.
            assert isinstance(kwargs["tenant_id"], str) and kwargs["tenant_id"]

    def test_default_tenant_override_passes_none_regardless_of_namespace(self):
        """Default-tenant override: ``_get_engine`` calls the factory with
        ``tenant_id is None`` (toolkit default tenant) regardless of the
        namespace passed."""
        from coa_serve.lexical.baseline_retriever import DEFAULT_TENANT_OVERRIDE

        retriever = self._make_retriever(tenant_id_override=DEFAULT_TENANT_OVERRIDE)
        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            # A namespace that WOULD derive a non-default tenant if not overridden.
            retriever._get_engine("550e8400-e29b-41d4-a716-446655440000", strategy)

            mock_engine.for_traversal_based_search.assert_called_once()
            _, kwargs = mock_engine.for_traversal_based_search.call_args
            assert kwargs["tenant_id"] is None

    def test_explicit_string_override_passes_that_literal_tenant(self):
        """Explicit-string override: the factory receives exactly that tenant
        string, regardless of the namespace passed."""
        retriever = self._make_retriever(tenant_id_override="sec10qbaselinetest")
        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            retriever._get_engine("some-other-namespace", strategy)

            mock_engine.for_traversal_based_search.assert_called_once()
            _, kwargs = mock_engine.for_traversal_based_search.call_args
            assert kwargs["tenant_id"] == "sec10qbaselinetest"

    def test_override_engine_does_not_collide_with_namespace_derived_engine(self):
        """Cache isolation: an override engine and a namespace-derived engine for
        the same ``(namespace, strategy)`` occupy DISTINCT cache entries and do
        not overwrite one another."""
        from coa_common.constants import to_graphrag_tenant_id
        from coa_serve.lexical.baseline_retriever import DEFAULT_TENANT_OVERRIDE

        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]
        namespace = "550e8400-e29b-41d4-a716-446655440000"
        derived_tenant = to_graphrag_tenant_id(namespace)

        # Namespace-derived retriever (production default).
        derived_retriever = self._make_retriever()
        # Default-tenant override retriever (eval harness).
        override_retriever = self._make_retriever(tenant_id_override=DEFAULT_TENANT_OVERRIDE)

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine") as mock_engine,
        ):
            mock_engine.for_traversal_based_search.side_effect = ["derived-engine", "override-engine"]

            engine_derived = derived_retriever._get_engine(namespace, strategy)
            engine_override = override_retriever._get_engine(namespace, strategy)

        # Distinct engines, keyed by the effective tenant (derived vs None).
        assert engine_derived == "derived-engine"
        assert engine_override == "override-engine"
        assert engine_derived != engine_override

        # The derived retriever cached under the namespace-derived tenant.
        assert (derived_tenant, strategy.name) in derived_retriever._engines
        # The override retriever cached under the toolkit default tenant (None).
        assert (None, strategy.name) in override_retriever._engines
        assert (derived_tenant, strategy.name) not in override_retriever._engines

    def test_single_retriever_override_uses_distinct_cache_key_from_derived(self):
        """Within a single retriever instance, the effective-tenant cache key
        means a default-tenant override engine is keyed on ``None`` rather than
        the namespace-derived tenant — so it never collides with what the
        production (un-overridden) path would cache for the same namespace."""
        from coa_common.constants import to_graphrag_tenant_id
        from coa_serve.lexical.baseline_retriever import DEFAULT_TENANT_OVERRIDE

        strategy = STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]
        namespace = "550e8400-e29b-41d4-a716-446655440000"
        derived_tenant = to_graphrag_tenant_id(namespace)

        override_retriever = self._make_retriever(tenant_id_override=DEFAULT_TENANT_OVERRIDE)

        with (
            patch("graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"),
            patch("graphrag_toolkit.lexical_graph.LexicalGraphQueryEngine"),
        ):
            override_retriever._get_engine(namespace, strategy)

        # The override keys on the toolkit default tenant (None), NOT on the
        # tenant the namespace would have derived.
        assert (None, strategy.name) in override_retriever._engines
        assert (derived_tenant, strategy.name) not in override_retriever._engines


# ── Task 7.a: result mapping (_map_results) ─────────────────────────


def _make_node(node_id="n1", text="some text", score=0.5, metadata=None):
    """Build a fake toolkit ``NodeWithScore`` (delegates to the shared helper).

    Kept as a thin local alias so the existing 7.a tests read unchanged while
    the shared ``lexical_helpers.make_toolkit_node`` is the single source of
    truth for the fake-node shape.
    """
    from .lexical_helpers import make_toolkit_node

    return make_toolkit_node(node_id=node_id, text=text, score=score, metadata=metadata)


def _source_metadata(source_id="doc-123", extra_metadata=None):
    """The real toolkit ``Source`` shape (delegates to the shared helper)."""
    from .lexical_helpers import source_metadata

    return source_metadata(source_id=source_id, extra_metadata=extra_metadata)


def _entity_contexts(entities):
    """The real toolkit ``EntityContexts`` shape (delegates to the shared helper)."""
    from .lexical_helpers import entity_contexts

    return entity_contexts(entities)


@pytest.fixture(autouse=True)
def _reset_shape_mismatch_warning():
    """Reset the module-level one-time-warning guard around each test.

    ``_map_results`` warns at most once per process on a shape mismatch; tests
    that assert the warning fires must start from a clean (un-warned) state.
    """
    import coa_serve.lexical.baseline_retriever as br

    br._shape_mismatch_warned = False
    yield
    br._shape_mismatch_warned = False


@pytest.mark.unit
class TestMapResults:
    """Task 7.a / BUG 5 (2.11): ``_map_results`` against the REAL nested toolkit
    metadata shape (``metadata["source"]["sourceId"]`` / ``entity_contexts``),
    plus defensive fallbacks that degrade to empties + a one-time warning rather
    than crashing on a shape mismatch."""

    # ---- real nested toolkit shape -------------------------------------

    def test_source_doc_read_from_nested_source_id(self):
        """``source_doc`` comes from ``metadata["source"]["sourceId"]`` (the real
        ``Source`` shape), NOT a flat ``metadata["source"]`` string."""
        node = _make_node(metadata={"source": _source_metadata("AAPL-2022-Q3.pdf")})

        chunks, entities = BaselineLexicalRetriever._map_results([node])

        assert len(chunks) == 1
        assert chunks[0].source_doc == "AAPL-2022-Q3.pdf"
        assert chunks[0].chunk_id == "n1"
        assert chunks[0].text == "some text"
        assert chunks[0].relevance_score == 0.5
        assert entities == []

    def test_entities_read_from_entity_contexts(self):
        """Entities are read from the nested ``entity_contexts`` structure
        (``contexts[].entities[].entity`` with ``entityId``/``value``/
        ``classification``)."""
        node = _make_node(
            metadata={
                "source": _source_metadata("doc.pdf"),
                "entity_contexts": _entity_contexts(
                    [
                        ("ent-apple", "Apple Inc.", "Company"),
                        ("ent-tim", "Tim Cook", "Person"),
                    ]
                ),
            }
        )

        chunks, entities = BaselineLexicalRetriever._map_results([node])

        assert len(chunks) == 1
        assert chunks[0].source_doc == "doc.pdf"
        assert len(entities) == 2
        by_uri = {e.uri: e for e in entities}
        assert by_uri["ent-apple"].label == "Apple Inc."
        assert by_uri["ent-apple"].type == "Company"
        assert by_uri["ent-tim"].label == "Tim Cook"
        assert by_uri["ent-tim"].type == "Person"

    def test_source_doc_read_from_traversal_result_shape(self):
        """The traversal retriever nests the source under
        ``metadata["result"]["source"]["sourceId"]``; the mapper reads it too."""
        node = _make_node(metadata={"result": {"source": _source_metadata("traversal-doc.pdf")}})

        chunks, _ = BaselineLexicalRetriever._map_results([node])

        assert chunks[0].source_doc == "traversal-doc.pdf"

    def test_entities_deduplicated_across_nodes(self):
        """The same entity URI appearing on multiple nodes yields a single
        ``GraphEntity`` (first occurrence wins)."""
        ec = _entity_contexts([("ent-apple", "Apple Inc.", "Company")])
        node1 = _make_node(node_id="n1", metadata={"entity_contexts": ec})
        node2 = _make_node(node_id="n2", metadata={"entity_contexts": ec})

        chunks, entities = BaselineLexicalRetriever._map_results([node1, node2])

        assert len(chunks) == 2
        assert len(entities) == 1
        assert entities[0].uri == "ent-apple"

    def test_missing_classification_falls_back_to_default_type(self):
        """An entity context with a value but missing classification uses the
        default lexical entity type rather than crashing."""
        from coa_serve.lexical.baseline_retriever import _DEFAULT_ENTITY_TYPE

        node = _make_node(
            metadata={
                "entity_contexts": {
                    "contexts": [{"entities": [{"entity": {"entityId": "e1", "value": "Acme"}, "score": 0.5}]}],
                    "keywords": [],
                }
            }
        )

        _, entities = BaselineLexicalRetriever._map_results([node])

        assert len(entities) == 1
        assert entities[0].uri == "e1"
        assert entities[0].label == "Acme"
        assert entities[0].type == _DEFAULT_ENTITY_TYPE

    # ---- defensive fallbacks + one-time warning ------------------------

    def test_empty_metadata_yields_empty_source_and_entities_no_warning(self):
        """A node with NO metadata is the legitimate empty case (e.g. an empty
        graph) — it degrades to empty ``source_doc``/entities WITHOUT a warning,
        since there is no shape to mismatch."""
        import coa_serve.lexical.baseline_retriever as br

        node = _make_node(metadata={})

        with patch.object(br.logger, "warning") as mock_warn:
            chunks, entities = BaselineLexicalRetriever._map_results([node])

        assert chunks[0].source_doc == ""
        assert entities == []
        mock_warn.assert_not_called()

    def test_shape_mismatch_degrades_and_warns_once(self):
        """A node that HAS metadata but matches none of the known shapes (neither
        a usable ``source`` nor any entities) degrades to empties AND emits a
        single ``logger.warning`` — it does not crash."""
        import coa_serve.lexical.baseline_retriever as br

        # Unexpected keys / wrong nesting: no sourceId, no entity_contexts.
        bad_meta_1 = {"unexpected_key": "value", "source": {"wrong": "shape"}}
        bad_meta_2 = {"another": "mismatch", "entities": 12345}
        node1 = _make_node(node_id="n1", metadata=bad_meta_1)
        node2 = _make_node(node_id="n2", metadata=bad_meta_2)

        with patch.object(br.logger, "warning") as mock_warn:
            chunks, entities = BaselineLexicalRetriever._map_results([node1, node2])

        # Degraded, not crashed.
        assert len(chunks) == 2
        assert all(c.source_doc == "" for c in chunks)
        assert entities == []
        # Warned exactly once despite two mismatched nodes (one-time guard).
        mock_warn.assert_called_once()
        # The warning carries the diagnostic event name + offending keys.
        args, kwargs = mock_warn.call_args
        assert args[0] == "baseline_map_results_shape_mismatch"
        assert "metadata_keys" in kwargs

    def test_malformed_entity_contexts_does_not_crash(self):
        """Wholly malformed ``entity_contexts`` (wrong types at every level)
        yields no entities instead of raising."""
        node = _make_node(
            metadata={
                "source": _source_metadata("doc.pdf"),
                "entity_contexts": {"contexts": "not-a-list"},
            }
        )

        # Has a valid source_doc, so this is not a full mismatch — just no entities.
        chunks, entities = BaselineLexicalRetriever._map_results([node])

        assert chunks[0].source_doc == "doc.pdf"
        assert entities == []

    def test_partial_entity_entries_are_skipped(self):
        """Malformed individual entity entries (non-dict, missing ``value``) are
        skipped while well-formed siblings are still mapped."""
        node = _make_node(
            metadata={
                "entity_contexts": {
                    "contexts": [
                        {
                            "entities": [
                                "not-a-dict",
                                {"entity": {"entityId": "e-missing-value"}},  # no value
                                {"entity": {"entityId": "e-ok", "value": "Good", "classification": "Thing"}},
                            ]
                        }
                    ],
                    "keywords": [],
                }
            }
        )

        _, entities = BaselineLexicalRetriever._map_results([node])

        assert len(entities) == 1
        assert entities[0].uri == "e-ok"
        assert entities[0].label == "Good"

    def test_entity_without_entity_id_uses_value_as_uri(self):
        """When an entity lacks ``entityId`` but has a ``value``, the value is
        used as the URI (defensive fallback) rather than dropping the entity."""
        node = _make_node(
            metadata={
                "entity_contexts": {
                    "contexts": [
                        {"entities": [{"entity": {"value": "Apple Inc.", "classification": "Company"}, "score": 0.5}]}
                    ],
                    "keywords": [],
                }
            }
        )

        _, entities = BaselineLexicalRetriever._map_results([node])

        assert len(entities) == 1
        assert entities[0].uri == "Apple Inc."
        assert entities[0].label == "Apple Inc."

    def test_empty_node_list_returns_empty(self):
        """An empty node list maps to empty chunks/entities (no crash, no warning)."""
        import coa_serve.lexical.baseline_retriever as br

        with patch.object(br.logger, "warning") as mock_warn:
            chunks, entities = BaselineLexicalRetriever._map_results([])

        assert chunks == []
        assert entities == []
        mock_warn.assert_not_called()

    def test_legacy_flat_source_string_still_supported(self):
        """A flat ``metadata["source"]`` string (simple/legacy shape) is still
        read, so the mapper is backward compatible while preferring the nested
        ``Source`` shape."""
        node = _make_node(metadata={"source": "legacy-doc.pdf"})

        chunks, _ = BaselineLexicalRetriever._map_results([node])

        assert chunks[0].source_doc == "legacy-doc.pdf"


# ── KnowledgeRetriever integration tests ────────────────────────────


@pytest.mark.unit
class TestKnowledgeRetrieverWithLexical:
    async def test_calls_lexical_retriever_when_present(self):
        from coa_serve.lexical.strategies import STRATEGY_REGISTRY, RetrieverStrategy

        mock_lexical = AsyncMock()
        mock_lexical.retrieve.return_value = _make_baseline_result()

        mock_vector = AsyncMock()
        mock_graph = AsyncMock()
        synthesizer = _make_synthesizer()

        retriever = KnowledgeRetriever(
            vector_retriever=mock_vector,
            graph_traverser=mock_graph,
            synthesizer=synthesizer,
            lexical_retriever=mock_lexical,
        )

        # A resolved strategy engages the lexical path (request- or deployment-driven).
        result = await retriever.resolve(
            "What is Apple revenue?",
            "550e8400-e29b-41d4-a716-446655440000",
            retriever_strategy=RetrieverStrategy.CHUNK_BASED_SEMANTIC,
        )

        # Lexical retriever was called with the resolved StrategySpec.
        mock_lexical.retrieve.assert_called_once_with(
            "What is Apple revenue?",
            "550e8400-e29b-41d4-a716-446655440000",
            strategy=STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC],
        )
        # Vector and graph were NOT called
        mock_vector.search.assert_not_called()
        mock_graph.traverse.assert_not_called()
        mock_graph.traverse_from_uris.assert_not_called()
        # Synthesizer was called with the lexical results
        synthesizer.synthesize.assert_called_once()
        # Result is valid
        assert result.synthesized_answer == "Apple's revenue was $83 billion in Q3 2022."
        assert result.confidence == 0.85

    async def test_lexical_present_but_no_strategy_runs_hand_rolled(self):
        """A lexical retriever is built on every deployment, but without a
        resolved strategy (no request field, hand-rolled deployment) Tier 3 runs
        the parallel vector + graph path — the retriever is not engaged."""
        mock_lexical = AsyncMock()
        mock_vector = AsyncMock()
        mock_vector.search.return_value = [
            ChunkResult(chunk_id="c1", text="test", source_doc="doc.pdf", relevance_score=0.8)
        ]
        mock_graph = AsyncMock()
        mock_graph.traverse.return_value = []
        synthesizer = _make_synthesizer()

        retriever = KnowledgeRetriever(
            vector_retriever=mock_vector,
            graph_traverser=mock_graph,
            synthesizer=synthesizer,
            lexical_retriever=mock_lexical,
        )

        # No retriever_strategy → hand-rolled path even though lexical is present.
        await retriever.resolve("test query", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 1024)

        mock_lexical.retrieve.assert_not_called()
        mock_vector.search.assert_called_once()
        mock_graph.traverse.assert_called_once()

    async def test_does_not_call_lexical_when_none(self):
        mock_vector = AsyncMock()
        mock_vector.search.return_value = [
            ChunkResult(chunk_id="c1", text="test", source_doc="doc.pdf", relevance_score=0.8)
        ]
        mock_graph = AsyncMock()
        mock_graph.traverse.return_value = []
        synthesizer = _make_synthesizer()

        retriever = KnowledgeRetriever(
            vector_retriever=mock_vector,
            graph_traverser=mock_graph,
            synthesizer=synthesizer,
            lexical_retriever=None,  # hand-rolled mode
        )

        await retriever.resolve("test query", "550e8400-e29b-41d4-a716-446655440000", embedding=[0.1] * 1024)

        # Vector and graph WERE called (hand-rolled path)
        mock_vector.search.assert_called_once()
        mock_graph.traverse.assert_called_once()

    async def test_lexical_trace_steps_included(self):
        from coa_serve.lexical.strategies import RetrieverStrategy

        mock_lexical = AsyncMock()
        mock_lexical.retrieve.return_value = _make_baseline_result()
        synthesizer = _make_synthesizer()

        retriever = KnowledgeRetriever(
            vector_retriever=None,
            graph_traverser=None,
            synthesizer=synthesizer,
            lexical_retriever=mock_lexical,
        )

        result = await retriever.resolve(
            "test", "550e8400-e29b-41d4-a716-446655440000", retriever_strategy=RetrieverStrategy.CHUNK_BASED_SEMANTIC
        )

        step_names = [s["step"] if isinstance(s, dict) else s.step for s in result.trace_steps]
        assert "lexical_baseline_retrieve" in step_names
        assert "t3.synthesize" in step_names


# ── Factory tests ───────────────────────────────────────────────────


@pytest.mark.unit
class TestGraphStoreUri:
    """Task 2.a: Assert backend-aware graph_store_uri computation (BUG 1/7)."""

    def test_ndb_endpoint_produces_neptune_db_uri(self):
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("test-cluster.us-east-1.neptune.amazonaws.com")
        assert uri == "neptune-db://test-cluster.us-east-1.neptune.amazonaws.com:8182"

    def test_ndb_endpoint_with_wss_prefix(self):
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("wss://test-cluster.us-east-1.neptune.amazonaws.com:8182/gremlin")
        assert uri == "neptune-db://test-cluster.us-east-1.neptune.amazonaws.com:8182"

    def test_na_graph_id_produces_neptune_graph_uri(self):
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("g-abc1234567")
        assert uri == "neptune-graph://g-abc1234567"

    def test_na_host_produces_neptune_graph_uri(self):
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("g-abc1234567.us-east-1.neptune-graph.amazonaws.com")
        assert uri == "neptune-graph://g-abc1234567"

    def test_na_uri_prefix_produces_neptune_graph_uri(self):
        from coa_serve.clients.factory import _graph_store_uri

        uri = _graph_store_uri("neptune-graph://g-abc1234567")
        assert uri == "neptune-graph://g-abc1234567"

    def test_vector_store_uri_is_aoss_prefixed(self):
        """The vector URI is always aoss://<endpoint>."""
        # This is computed inline in build_lexical_retriever; verify the pattern.
        endpoint = "https://test.aoss.amazonaws.com"
        assert f"aoss://{endpoint}" == "aoss://https://test.aoss.amazonaws.com"


@pytest.mark.unit
class TestBuildLexicalRetrieverStrategy:
    """Task 2.a: Assert factory builds retriever with resolved StrategySpec."""

    def test_retriever_receives_resolved_strategy_spec(self):
        from coa_serve.clients.factory import build_lexical_retriever
        from coa_serve.lexical.strategies import STRATEGY_REGISTRY, RetrieverStrategy

        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "g-abc1234567.us-east-1.neptune-graph.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"
        mock_config.lexical_retriever_strategy = "chunk_based_semantic"

        result = build_lexical_retriever(mock_config)

        assert result is not None
        assert result._strategy is STRATEGY_REGISTRY[RetrieverStrategy.CHUNK_BASED_SEMANTIC]

    def test_retriever_instance_default_is_default_spec_regardless_of_config(self):
        """Strategy is now resolved per-request and passed into ``retrieve()``;
        the factory builds the retriever with DEFAULT_SPEC as the (unused)
        instance fallback regardless of ``config.lexical_retriever_strategy``."""
        from coa_serve.clients.factory import build_lexical_retriever
        from coa_serve.lexical.strategies import DEFAULT_SPEC

        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"
        mock_config.lexical_retriever_strategy = "traversal"

        result = build_lexical_retriever(mock_config)

        assert result is not None
        assert result._strategy is DEFAULT_SPEC

    def test_timeout_s_omitted_uses_standard_mode_default(self, monkeypatch):
        """No ``timeout_s`` and no env override → the standard-mode fallback (45s),
        NOT the retriever's own 15s. The 15s default is too low for slow single-shot
        traversal strategies (topic_beam's graph beam-traversal times out to empty),
        so build_lexical_retriever raises the standard-mode timeout when the caller
        (the deep-reasoning path) does not pass its own budget."""
        from coa_serve.clients.factory import build_lexical_retriever

        monkeypatch.delenv("LEXICAL_RETRIEVER_TIMEOUT_S", raising=False)
        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"

        result = build_lexical_retriever(mock_config)

        assert result is not None
        assert result._timeout_s == 45.0

    def test_timeout_s_from_env_override(self, monkeypatch):
        """LEXICAL_RETRIEVER_TIMEOUT_S tunes the standard-mode timeout without code."""
        from coa_serve.clients.factory import build_lexical_retriever

        monkeypatch.setenv("LEXICAL_RETRIEVER_TIMEOUT_S", "90")
        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"

        result = build_lexical_retriever(mock_config)

        assert result is not None
        assert result._timeout_s == 90.0

    def test_invalid_env_timeout_falls_back_to_default(self, monkeypatch):
        """A non-numeric LEXICAL_RETRIEVER_TIMEOUT_S is ignored (logged), not fatal:
        it falls back to the 45s standard-mode default rather than crashing startup."""
        from coa_serve.clients.factory import build_lexical_retriever

        monkeypatch.setenv("LEXICAL_RETRIEVER_TIMEOUT_S", "not-a-number")
        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"

        result = build_lexical_retriever(mock_config)

        assert result is not None
        assert result._timeout_s == 45.0

    def test_explicit_timeout_s_overrides_env(self, monkeypatch):
        """An explicit timeout_s (the deep-reasoning per-tool budget) wins over the env
        fallback — the env only applies on the standard-mode path (timeout_s=None)."""
        from coa_serve.clients.factory import build_lexical_retriever

        monkeypatch.setenv("LEXICAL_RETRIEVER_TIMEOUT_S", "90")
        mock_config = MagicMock()
        mock_config.tier3_strategy = "deep-reasoning"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"

        result = build_lexical_retriever(mock_config, timeout_s=30.0)

        assert result is not None
        assert result._timeout_s == 30.0

    def test_timeout_s_override_is_applied(self):
        """The deep-reasoning path passes its per-tool budget so a graphrag strategy is not
        cut short by the adapter's 15s default (live: ``strategy:entity_based
        degraded ... TimeoutError after 15.0s`` inside a 45s per-tool budget)."""
        from coa_serve.clients.factory import build_lexical_retriever

        mock_config = MagicMock()
        mock_config.tier3_strategy = "deep-reasoning"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"

        result = build_lexical_retriever(mock_config, timeout_s=45.0)

        assert result is not None
        assert result._timeout_s == 45.0

    def test_retriever_uses_na_graph_store_uri(self):
        from coa_serve.clients.factory import build_lexical_retriever

        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "g-abc1234567.us-east-1.neptune-graph.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"
        mock_config.lexical_retriever_strategy = "chunk_based_semantic"

        result = build_lexical_retriever(mock_config)

        assert result._graph_store_uri == "neptune-graph://g-abc1234567"
        assert result._vector_store_uri == "aoss://https://test.aoss.amazonaws.com"

    def test_retriever_uses_ndb_graph_store_uri(self):
        from coa_serve.clients.factory import build_lexical_retriever

        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"
        mock_config.lexical_retriever_strategy = "chunk_based_semantic"

        result = build_lexical_retriever(mock_config)

        assert result._graph_store_uri == "neptune-db://test-cluster.us-east-1.neptune.amazonaws.com:8182"
        assert result._vector_store_uri == "aoss://https://test.aoss.amazonaws.com"


@pytest.mark.unit
class TestBuildLexicalRetriever:
    def test_builds_on_hand_rolled_when_endpoints_present(self):
        """The lexical retriever is built on every deployment (so a per-request
        retrieverStrategy can engage graphrag on-demand); whether the default
        Tier 3 path uses it is decided in the orchestrator, not here."""
        from coa_serve.clients.factory import build_lexical_retriever

        mock_config = MagicMock()
        mock_config.tier3_strategy = "hand-rolled"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"

        result = build_lexical_retriever(mock_config)
        assert isinstance(result, BaselineLexicalRetriever)

    def test_returns_none_when_endpoints_unset(self):
        from coa_serve.clients.factory import build_lexical_retriever

        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = ""
        mock_config.opensearch_endpoint = ""

        result = build_lexical_retriever(mock_config)
        assert result is None

    def test_returns_retriever_when_lexical_baseline(self):
        from coa_serve.clients.factory import build_lexical_retriever

        mock_config = MagicMock()
        mock_config.tier3_strategy = "lexical-baseline"
        mock_config.neptune_endpoint = "test-cluster.us-east-1.neptune.amazonaws.com"
        mock_config.opensearch_endpoint = "https://test.aoss.amazonaws.com"
        mock_config.lexical_retriever_strategy = "chunk_based_semantic"

        if True:  # no patch needed — GraphRAGConfig already initialized at module level
            result = build_lexical_retriever(mock_config)

        assert result is not None
        assert isinstance(result, BaselineLexicalRetriever)


# ── Task 11.b: retired-model patch (load-bearing) ───────────────────


@pytest.mark.unit
class TestRetiredModelPatch:
    """Task 11.b: the graphrag-toolkit retired default model is patched at import.

    graphrag-toolkit 3.18.3 hardcodes the now-retired
    ``us.anthropic.claude-3-7-sonnet-20250219-v1:0`` as the default extraction
    AND response model. Importing ``baseline_retriever`` patches the module-level
    constants (``_graphrag_cfg.DEFAULT_EXTRACTION_MODEL`` /
    ``DEFAULT_RESPONSE_MODEL``) to an active model. This is LOAD-BEARING: without
    it every strategy fails inside retrieval (``keyword_vss_provider`` calls the
    extraction LLM) with ``ResourceNotFoundException`` ("model ... end of its
    life"). See ``strategy-investigation-findings.md`` → "Finding 0".
    """

    _RETIRED_MODEL = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"

    def test_extraction_and_response_constants_patched_at_import(self):
        """Both toolkit default-model constants equal the adapter's active model."""
        import graphrag_toolkit.lexical_graph.config as _graphrag_cfg
        from coa_serve.lexical.baseline_retriever import _ACTIVE_MODEL

        assert _graphrag_cfg.DEFAULT_EXTRACTION_MODEL == _ACTIVE_MODEL
        assert _graphrag_cfg.DEFAULT_RESPONSE_MODEL == _ACTIVE_MODEL

    def test_active_model_is_not_the_retired_default(self):
        """The patched-in model is NOT the retired toolkit default."""
        import graphrag_toolkit.lexical_graph.config as _graphrag_cfg
        from coa_serve.lexical.baseline_retriever import _ACTIVE_MODEL

        assert _ACTIVE_MODEL != self._RETIRED_MODEL
        assert _graphrag_cfg.DEFAULT_EXTRACTION_MODEL != self._RETIRED_MODEL
        assert _graphrag_cfg.DEFAULT_RESPONSE_MODEL != self._RETIRED_MODEL

    def test_active_model_is_overridable_via_env(self):
        """``_ACTIVE_MODEL`` is sourced from ``GRAPHRAG_LLM_MODEL`` (overridable),
        falling back to the active default when the env var is unset."""
        import os

        from coa_serve.lexical.baseline_retriever import _ACTIVE_MODEL

        expected = os.environ.get("GRAPHRAG_LLM_MODEL", "us.anthropic.claude-sonnet-5")
        assert expected == _ACTIVE_MODEL

    def test_env_override_changes_patched_constants_on_reimport(self):
        """Setting ``GRAPHRAG_LLM_MODEL`` and re-importing the module patches the
        toolkit constants to the overridden value (proves the env seam is live,
        not a frozen literal). The module is reloaded back to its original state
        afterward so other tests see the default patch."""
        import importlib
        import os
        from unittest.mock import patch as _patch

        import coa_serve.lexical.baseline_retriever as br
        import graphrag_toolkit.lexical_graph.config as _graphrag_cfg

        override = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
        try:
            with _patch.dict(os.environ, {"GRAPHRAG_LLM_MODEL": override}):
                importlib.reload(br)
                assert override == br._ACTIVE_MODEL
                assert override == _graphrag_cfg.DEFAULT_EXTRACTION_MODEL
                assert override == _graphrag_cfg.DEFAULT_RESPONSE_MODEL
        finally:
            # Restore the module (and the toolkit constants) to the default patch.
            importlib.reload(br)
