# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Deletion Pipeline Stage 2: graph_cleanup.py.

All external dependencies (GraphStoreFactory, VectorStoreFactory,
LexicalGraphIndex, DeleteSources) are mocked so tests run without
any AWS infrastructure.

graph_cleanup.py imports graphrag_toolkit inside main() (after applying the
graphrag monkey-patches), so we pre-stub the entire package in sys.modules before
importing the module under test. This is the same approach used for other heavy
deps that aren't installed in the dev venv.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# graphrag_toolkit stub — injected into sys.modules before graph_cleanup runs so
# the `from graphrag_toolkit...` lines inside main() resolve.
# ---------------------------------------------------------------------------


def _make_graphrag_stub() -> None:
    """Register minimal graphrag_toolkit stubs in sys.modules."""
    root = types.ModuleType("graphrag_toolkit")
    lexical = types.ModuleType("graphrag_toolkit.lexical_graph")
    storage = types.ModuleType("graphrag_toolkit.lexical_graph.storage")
    indexing = types.ModuleType("graphrag_toolkit.lexical_graph.indexing")
    build_mod = types.ModuleType("graphrag_toolkit.lexical_graph.indexing.build")
    delete_mod = types.ModuleType("graphrag_toolkit.lexical_graph.indexing.build.delete_sources")

    lexical.LexicalGraphIndex = MagicMock()
    storage.GraphStoreFactory = MagicMock()
    storage.VectorStoreFactory = MagicMock()
    delete_mod.DeleteSources = MagicMock()

    for name, mod in [
        ("graphrag_toolkit", root),
        ("graphrag_toolkit.lexical_graph", lexical),
        ("graphrag_toolkit.lexical_graph.storage", storage),
        ("graphrag_toolkit.lexical_graph.indexing", indexing),
        ("graphrag_toolkit.lexical_graph.indexing.build", build_mod),
        ("graphrag_toolkit.lexical_graph.indexing.build.delete_sources", delete_mod),
    ]:
        sys.modules.setdefault(name, mod)


_make_graphrag_stub()

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_ENV = {
    "NAMESPACE_ID": "890dd75f-649c-4e95-8981-f9dc33a518ea",
    "DOC_SOURCE_ID": "01KRPTQ3075C5C6NV825ZQ5WW4",
    "TENANT_ID": "890dd75f649c4e958981f9dc3",
    "NEPTUNE_ENDPOINT": "my-neptune.us-east-1.neptune.amazonaws.com",
    "OPENSEARCH_ENDPOINT": "abc123.us-east-1.aoss.amazonaws.com",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key, val in _BASE_ENV.items():
        monkeypatch.setenv(key, val)
    _evict_graph_cleanup()


def _evict_graph_cleanup():
    """Fully evict graph_cleanup so re-import re-executes module-level code."""
    sys.modules.pop("coa_sources.documents.kg_build.graph_cleanup", None)
    # Also remove the attribute from the parent package so re-import is fresh
    pkg = sys.modules.get("coa_sources.documents.kg_build")
    if pkg and hasattr(pkg, "graph_cleanup"):
        delattr(pkg, "graph_cleanup")


@pytest.fixture()
def mod():
    _evict_graph_cleanup()
    from coa_sources.documents.kg_build import graph_cleanup

    return graph_cleanup


def _make_store_mocks(mock_gsf, mock_vsf):
    """Wire up GraphStoreFactory / VectorStoreFactory context-manager mocks."""
    mock_gs = MagicMock()
    mock_vs = MagicMock()
    mock_gsf.for_graph_store.return_value.__enter__ = MagicMock(return_value=mock_gs)
    mock_gsf.for_graph_store.return_value.__exit__ = MagicMock(return_value=False)
    mock_vsf.for_vector_store.return_value.__enter__ = MagicMock(return_value=mock_vs)
    mock_vsf.for_vector_store.return_value.__exit__ = MagicMock(return_value=False)
    return mock_gs, mock_vs


def _make_graph_index(mock_idx_cls, sources):
    """Return a mock LexicalGraphIndex with pre-configured get_sources result."""
    mock_index = MagicMock()
    mock_index.get_sources.return_value = sources
    mock_idx_cls.return_value = mock_index
    return mock_index


# Patch targets — graph_cleanup imports these names INSIDE main(), after applying
# the graphrag monkey-patches (the AOSS NEXTGEN fix must land before
# LexicalGraphIndex/VectorStoreFactory bind their own references). Function-local
# imports resolve from the source module at call time, so patch them there rather
# than in the graph_cleanup namespace.
_P_GSF = "graphrag_toolkit.lexical_graph.storage.GraphStoreFactory"
_P_VSF = "graphrag_toolkit.lexical_graph.storage.VectorStoreFactory"
_P_IDX = "graphrag_toolkit.lexical_graph.LexicalGraphIndex"
_P_DS = "graphrag_toolkit.lexical_graph.indexing.build.delete_sources.DeleteSources"


# ---------------------------------------------------------------------------
# 1. Environment validation
# ---------------------------------------------------------------------------


class TestEnvValidation:
    @pytest.mark.parametrize(
        "missing_var",
        ["NAMESPACE_ID", "DOC_SOURCE_ID", "TENANT_ID", "NEPTUNE_ENDPOINT", "OPENSEARCH_ENDPOINT"],
    )
    def test_missing_env_var_raises(self, missing_var, monkeypatch, mod):
        monkeypatch.delenv(missing_var)
        _evict_graph_cleanup()
        from coa_sources.documents.kg_build import graph_cleanup

        with pytest.raises(RuntimeError, match="Missing required environment variables"):
            graph_cleanup.main()

    def test_all_env_vars_present_does_not_raise_on_validation(self, mod):
        """Validation itself should not raise when all vars are set."""
        missing = [
            name
            for name, val in [
                ("NAMESPACE_ID", mod.NAMESPACE_ID),
                ("DOC_SOURCE_ID", mod.DOC_SOURCE_ID),
                ("TENANT_ID", mod.TENANT_ID),
                ("NEPTUNE_ENDPOINT", mod.NEPTUNE_ENDPOINT),
                ("OPENSEARCH_ENDPOINT", mod.OPENSEARCH_ENDPOINT),
            ]
            if not val
        ]
        assert missing == []


# ---------------------------------------------------------------------------
# 2. Store URI construction
# ---------------------------------------------------------------------------


class TestStoreUris:
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_neptune_uri_uses_port_8182(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        mod.main()

        mock_gsf.for_graph_store.assert_called_once_with(f"neptune-db://{_BASE_ENV['NEPTUNE_ENDPOINT']}:8182")

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_opensearch_uri_uses_aoss_scheme(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        mod.main()

        mock_vsf.for_vector_store.assert_called_once_with(
            f"aoss://{_BASE_ENV['OPENSEARCH_ENDPOINT']}:443",
            index_names=mod.EMBEDDING_INDEXES,
        )

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_deletes_only_the_indexes_the_build_writes(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        """Cleanup must not look for an index the build path never created.

        Left to its default, the toolkit includes ``statement``, which
        EMBEDDING_INDEXES deliberately excludes — statements are built into the
        graph but never embedded. Deletion then waits 70 seconds per batch for
        documents that do not exist, and a 36-source delete ran 10 hours to a
        timeout. Asserting the two paths agree is what keeps them agreeing.
        """
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        mod.main()

        indexes = mock_vsf.for_vector_store.call_args.kwargs["index_names"]
        assert "statement" not in indexes, "statement is built into the graph but never embedded"
        assert indexes == mod.EMBEDDING_INDEXES, "must be the same set the build task writes"


# ---------------------------------------------------------------------------
# 3. LexicalGraphIndex construction
# ---------------------------------------------------------------------------


class TestGraphIndexConstruction:
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_index_created_with_tenant_id(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        mock_gs, mock_vs = _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        mod.main()

        mock_idx_cls.assert_called_once_with(mock_gs, mock_vs, tenant_id=_BASE_ENV["TENANT_ID"])

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_get_sources_filtered_by_doc_source_id(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        mock_index = _make_graph_index(mock_idx_cls, [])

        mod.main()

        mock_index.get_sources.assert_called_once_with(filter={"doc_source_id": _BASE_ENV["DOC_SOURCE_ID"]})


# ---------------------------------------------------------------------------
# 4. No-sources path
# ---------------------------------------------------------------------------


class TestNoSourcesFound:
    @patch(_P_DS)
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_no_sources_does_not_call_delete(self, mock_vsf, mock_gsf, mock_idx_cls, mock_ds_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        mod.main()

        mock_ds_cls.assert_not_called()

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_no_sources_completes_without_error(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        mod.main()  # should not raise


# ---------------------------------------------------------------------------
# 5. Happy path — sources found and deleted
# ---------------------------------------------------------------------------


class TestDeleteSources:
    @patch(_P_DS)
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_delete_called_with_source_ids(self, mock_vsf, mock_gsf, mock_idx_cls, mock_ds_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        sources = [{"sourceId": "src-001"}, {"sourceId": "src-002"}]
        _make_graph_index(mock_idx_cls, sources)
        mock_ds = MagicMock()
        mock_ds.delete_source_documents.return_value = ["src-001", "src-002"]
        mock_ds_cls.return_value = mock_ds

        mod.main()

        mock_ds.delete_source_documents.assert_called_once_with(["src-001", "src-002"])

    @patch(_P_DS)
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_delete_sources_uses_tenant_scoped_stores(self, mock_vsf, mock_gsf, mock_idx_cls, mock_ds_cls, mod):
        """DeleteSources must receive graph_index.graph_store / .vector_store
        (the tenant-scoped wrappers), not the raw stores."""
        _make_store_mocks(mock_gsf, mock_vsf)
        mock_index = _make_graph_index(mock_idx_cls, [{"sourceId": "src-001"}])
        mock_ds = MagicMock()
        mock_ds.delete_source_documents.return_value = ["src-001"]
        mock_ds_cls.return_value = mock_ds

        mod.main()

        mock_ds_cls.assert_called_once_with(
            graph_store=mock_index.graph_store,
            vector_store=mock_index.vector_store,
            batch_size=200,
            num_workers=1,
        )

    @patch(_P_DS)
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_batch_size_is_200(self, mock_vsf, mock_gsf, mock_idx_cls, mock_ds_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [{"sourceId": "src-001"}])
        mock_ds = MagicMock()
        mock_ds.delete_source_documents.return_value = ["src-001"]
        mock_ds_cls.return_value = mock_ds

        mod.main()

        _, kwargs = mock_ds_cls.call_args
        assert kwargs["batch_size"] == 200
        assert kwargs["num_workers"] == 1

    @patch(_P_DS)
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_single_source_deleted(self, mock_vsf, mock_gsf, mock_idx_cls, mock_ds_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [{"sourceId": "src-only"}])
        mock_ds = MagicMock()
        mock_ds.delete_source_documents.return_value = ["src-only"]
        mock_ds_cls.return_value = mock_ds

        mod.main()

        mock_ds.delete_source_documents.assert_called_once_with(["src-only"])

    @patch(_P_DS)
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_multiple_sources_all_passed_to_delete(self, mock_vsf, mock_gsf, mock_idx_cls, mock_ds_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        sources = [{"sourceId": f"src-{i}"} for i in range(5)]
        _make_graph_index(mock_idx_cls, sources)
        mock_ds = MagicMock()
        mock_ds.delete_source_documents.return_value = [s["sourceId"] for s in sources]
        mock_ds_cls.return_value = mock_ds

        mod.main()

        ids_passed = mock_ds.delete_source_documents.call_args[0][0]
        assert ids_passed == [f"src-{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# 6. Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @patch(_P_DS)
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_delete_failure_propagates(self, mock_vsf, mock_gsf, mock_idx_cls, mock_ds_cls, mod):
        """If DeleteSources raises, the exception should propagate (ECS task fails → Step Functions catches it)."""
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [{"sourceId": "src-001"}])
        mock_ds = MagicMock()
        mock_ds.delete_source_documents.side_effect = RuntimeError("Neptune OOM")
        mock_ds_cls.return_value = mock_ds

        with pytest.raises(RuntimeError, match="Neptune OOM"):
            mod.main()

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_get_sources_failure_propagates(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        mock_index = _make_graph_index(mock_idx_cls, [])
        mock_index.get_sources.side_effect = RuntimeError("Neptune connection refused")

        with pytest.raises(RuntimeError, match="Neptune connection refused"):
            mod.main()


# ---------------------------------------------------------------------------
# 7. graphrag monkey-patches applied on the deletion path
# ---------------------------------------------------------------------------


class TestGraphragPatchesApplied:
    """Regression: the deletion task must apply the same graphrag monkey-patches
    as the build task.

    Deletion reads the vector indexes (``delete_embeddings`` →
    ``_get_existing_doc_ids_for_ids`` → ``paginated_search``), so it hits the same
    AOSS failure modes. Unpatched, a ``429 circuit_breaking_exception`` from the
    NEXTGEN heap breaker was neither retried nor surfaced: the toolkit's handler is
    ``except Exception as ee: ... raise e`` with ``e`` unbound, so it raised
    ``UnboundLocalError``, masked the real 429, failed the ECS task, and left the
    namespace's embeddings orphaned half-deleted (observed 2026-07-27 on both
    SEC-10-Q namespaces; the defect is still present in the pinned 3.18.7).
    """

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_main_applies_all_three_patches(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        with (
            patch.object(mod, "_patch_graphrag_toolkit_for_aoss_nextgen") as p_aoss,
            patch.object(mod, "_patch_graphrag_paginated_search_retry") as p_page,
            patch.object(mod, "_patch_graphrag_bulk_ingest_retry") as p_bulk,
        ):
            mod.main()

        p_aoss.assert_called_once()
        p_page.assert_called_once()
        p_bulk.assert_called_once()

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_patches_applied_before_stores_are_built(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        """Ordering is load-bearing: the AOSS NEXTGEN patch must replace
        ``index_exists`` before ``VectorStoreFactory`` binds its own reference."""
        calls: list[str] = []
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])
        mock_vsf.for_vector_store.side_effect = lambda *a, **k: (
            calls.append("vector_store"),
            MagicMock(__enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)),
        )[1]

        with (
            patch.object(
                mod,
                "_patch_graphrag_toolkit_for_aoss_nextgen",
                side_effect=lambda **k: calls.append(("patch", k)),
            ) as mock_patch,
            patch.object(mod, "_patch_graphrag_paginated_search_retry"),
            patch.object(mod, "_patch_graphrag_bulk_ingest_retry"),
        ):
            mod.main()

        patch_idx = next(i for i, c in enumerate(calls) if c[0] == "patch" if isinstance(c, tuple))
        assert patch_idx < calls.index("vector_store"), f"patch must precede store build, got {calls}"
        # The deletion task must install the NON-creating variant, or cleaning an
        # already-empty tenant recreates chunk_*/topic_* as unreclaimable shells.
        mock_patch.assert_called_once_with(allow_create=False)


# ---------------------------------------------------------------------------
# Embedding-index existence filter
# ---------------------------------------------------------------------------


class TestExistingEmbeddingIndexFilter:
    """Deletion must never be handed an index name that no longer exists.

    graphrag's ``delete_embeddings`` reads "found no documents for these ids" as
    replication lag and sleeps in a 70-second retry loop PER BATCH before giving
    up. With a missing index the id map is always empty (the vector client is
    graphrag's dummy, so ``paginated_search`` yields nothing), so every batch
    burns the full 70s — the module docstring's "36-source deletion burned 10
    hours ... left the source DELETE_FAILED".

    A missing index is now normal: namespace teardown drops chunk/topic once the
    last source is deleted, and these ECS tasks can still be running then.
    """

    def _client(self, mod, exists_map):
        client = MagicMock()
        client.indices.exists.side_effect = lambda index: exists_map.get(index, False)
        avc = MagicMock()
        avc.return_value.raw_client.return_value = client
        return patch.object(mod, "AossVectorClient", avc), client

    def test_absent_index_is_filtered_out(self, mod):
        ctx, client = self._client(mod, {"chunk_t1": True, "topic_t1": False})
        with ctx:
            assert mod._existing_embedding_indexes(["chunk", "topic"], "t1") == ["chunk"]
        assert client.indices.exists.call_count == 2

    def test_all_absent_returns_empty(self, mod):
        ctx, _ = self._client(mod, {})
        with ctx:
            assert mod._existing_embedding_indexes(["chunk", "topic"], "t1") == []

    def test_all_present_keeps_everything(self, mod):
        ctx, _ = self._client(mod, {"chunk_t1": True, "topic_t1": True})
        with ctx:
            assert mod._existing_embedding_indexes(["chunk", "topic"], "t1") == ["chunk", "topic"]

    def test_probe_failure_keeps_the_prefix(self, mod):
        """Best-effort: a transient AOSS fault must not silently skip a delete
        that was actually needed — degrade to the old (slow) path instead."""
        client = MagicMock()
        client.indices.exists.side_effect = RuntimeError("AOSS 429")
        avc = MagicMock()
        avc.return_value.raw_client.return_value = client
        with patch.object(mod, "AossVectorClient", avc):
            assert mod._existing_embedding_indexes(["chunk"], "t1") == ["chunk"]

    def test_no_prefixes_short_circuits_without_a_client(self, mod):
        avc = MagicMock()
        with patch.object(mod, "AossVectorClient", avc):
            assert mod._existing_embedding_indexes([], "t1") == []
        avc.assert_not_called()


class TestMainPassesOnlyExistingIndexes:
    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_filtered_list_reaches_the_vector_store_factory(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        with patch.object(mod, "_existing_embedding_indexes", return_value=["chunk"]):
            mod.main()

        assert mock_vsf.for_vector_store.call_args.kwargs["index_names"] == ["chunk"]

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_no_existing_indexes_uses_dummy_store_never_empty_index_names(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        """When no AOSS index remains, fall back to the dummy vector store.

        Regression for the crash observed on doc-source deletion (exit 1):

            ValueError: Unrecognized vector store info: aoss://<host>:443

        graphrag's ``VectorStoreFactory.for_vector_store`` raises that when handed
        an EMPTY ``index_names`` — the ``aoss://`` factory's ``try_create``
        iterates zero names and returns ``[]``, so every factory is treated as a
        non-match and it falls through to the ValueError. The task then dies
        before the Neptune subgraph delete runs, leaving the source DELETE_FAILED.
        (The old assertion here expected ``index_names == []`` and only passed
        because VectorStoreFactory is mocked in this suite — the mock never
        reproduced the real failure.)

        The fix: when the filter finds nothing, point at ``dummy://`` with the
        configured (non-empty) names so a no-op VectorStore is built; deletion's
        ``get_index(...).delete_embeddings(...)`` no-ops while the Neptune subgraph
        delete proceeds.
        """
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])

        with patch.object(mod, "_existing_embedding_indexes", return_value=[]):
            mod.main()

        uri, kwargs = mock_vsf.for_vector_store.call_args.args, mock_vsf.for_vector_store.call_args.kwargs
        assert uri and uri[0] == "dummy://", "empty-index case must use the dummy vector store, not aoss://"
        assert kwargs["index_names"], "index_names must be NON-empty (empty triggers the ValueError)"
        assert kwargs["index_names"] == mod.EMBEDDING_INDEXES

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_no_existing_indexes_still_deletes_the_neptune_subgraph(self, mock_vsf, mock_gsf, mock_idx_cls, mod):
        """The Neptune subgraph delete is the part that still matters when the
        vector indexes are already gone — it must still run (not be skipped)."""
        _make_store_mocks(mock_gsf, mock_vsf)
        mock_index = _make_graph_index(mock_idx_cls, [{"sourceId": "src-001"}])

        with (
            patch.object(mod, "_existing_embedding_indexes", return_value=[]),
            patch(_P_DS) as mock_ds_cls,
        ):
            mock_ds = MagicMock()
            mock_ds.delete_source_documents.return_value = ["src-001"]
            mock_ds_cls.return_value = mock_ds
            mod.main()

        mock_ds.delete_source_documents.assert_called_once_with(["src-001"])
        assert mock_index.get_sources.called

    @patch(_P_IDX)
    @patch(_P_GSF)
    @patch(_P_VSF)
    def test_misconfigured_empty_embedding_indexes_still_never_passes_empty(
        self, mock_vsf, mock_gsf, mock_idx_cls, mod, monkeypatch
    ):
        """The dummy fallback must stay non-empty even if EMBEDDING_INDEXES is empty.

        ``EMBEDDING_INDEXES`` is env-overridable and parses ``EMBEDDING_INDEXES=""``
        (or whitespace) to ``[]``. Falling back to it directly would hand
        ``index_names=[]`` to ``for_vector_store("dummy://", [])`` — which raises the
        SAME ``ValueError`` this MR fixes, since an empty list produces zero indexes
        for every scheme. Guard: the fallback is coalesced to a fixed non-empty set.
        """
        _make_store_mocks(mock_gsf, mock_vsf)
        _make_graph_index(mock_idx_cls, [])
        monkeypatch.setattr(mod, "EMBEDDING_INDEXES", [])  # simulate EMBEDDING_INDEXES=""

        with patch.object(mod, "_existing_embedding_indexes", return_value=[]):
            mod.main()

        uri = mock_vsf.for_vector_store.call_args.args
        kwargs = mock_vsf.for_vector_store.call_args.kwargs
        assert uri and uri[0] == "dummy://"
        assert kwargs["index_names"], "index_names must be NON-empty even when EMBEDDING_INDEXES is empty"
