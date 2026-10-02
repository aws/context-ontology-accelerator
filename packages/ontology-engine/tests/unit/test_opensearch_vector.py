# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for OpenSearchVectorStore — the thin VectorStore adapter over the
shared ``AossVectorClient``.

The store owns document shaping, per-namespace index naming, filter
construction, and ``EmbeddingHit`` mapping; the shared client owns the actual
AOSS I/O, retry, mapping, and query-body construction (tested in
``coa_common``'s ``test_opensearch_client.py``). So here we inject a
mock ``AossVectorClient`` and assert the store delegates correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_ontology.stores.base import EmbeddingHit
from coa_ontology.stores.opensearch_vector import OpenSearchVectorStore

pytestmark = pytest.mark.unit


def _store_with_client(client: MagicMock, index: str = "idx-ns1") -> OpenSearchVectorStore:
    store = OpenSearchVectorStore(namespace="ns1")
    store._client = client
    # ensure_index just echoes the resolved index name; bypass it deterministically.
    client.ensure_index.side_effect = lambda idx: idx
    store._ensure_index = lambda namespace=None: index
    # Reads resolve the index WITHOUT creating it (see _read_index docstring).
    store._read_index = lambda namespace=None: index
    return store


# ── store_embedding (doc shaping) ────────────────────────────────────────


class TestStoreEmbedding:
    def test_pads_short_vector_and_adds_optional_fields(self):
        client = MagicMock()
        store = _store_with_client(client)
        doc = store.store_embedding(
            {
                "entity_uri": "http://x/A",
                "embedding_type": "lexical",
                "model_id": "titan",
                "vector": [0.1, 0.2],
                "context_text": "ctx",
                "data_source_id": "ds1",
            }
        )
        assert len(doc["embedding"]) == store.dimensions
        assert doc["embedding"][:2] == [0.1, 0.2]
        assert doc["embedding"][2] == 0.0
        assert doc["context_text"] == "ctx"
        assert doc["data_source_id"] == "ds1"
        client.index_document.assert_called_once_with("idx-ns1", doc)

    def test_defaults_namespace_and_omits_absent_optionals(self):
        client = MagicMock()
        store = OpenSearchVectorStore(namespace=None)
        store._client = client
        store._ensure_index = lambda namespace=None: "idx-default"
        doc = store.store_embedding(
            {"entity_uri": "http://x/A", "embedding_type": "lexical", "model_id": "titan", "vector": [0.0] * 1024}
        )
        assert doc["namespace"] == "default"
        assert "context_text" not in doc
        assert "data_source_id" not in doc


# ── store_embeddings_batch (namespace grouping) ─────────────────────────


class TestStoreEmbeddingsBatch:
    def test_groups_by_namespace_and_bulk_indexes_each(self):
        client = MagicMock()
        store = OpenSearchVectorStore(namespace="bound-ns")
        store._client = client
        store._ensure_index = lambda namespace=None: f"idx-{namespace}"
        items = [
            {
                "entity_uri": "http://x/A",
                "embedding_type": "lexical",
                "model_id": "titan",
                "vector": [0.1],
                "namespace": "ns-a",
            },
            {
                "entity_uri": "http://x/B",
                "embedding_type": "lexical",
                "model_id": "titan",
                "vector": [0.2],
                "namespace": "ns-b",
            },
        ]
        out = store.store_embeddings_batch(items)
        assert len(out) == 2
        assert all(len(d["embedding"]) == store.dimensions for d in out)
        # One bulk_index call per distinct namespace.
        assert client.bulk_index.call_count == 2
        indices = {c.args[0] for c in client.bulk_index.call_args_list}
        assert indices == {"idx-ns-a", "idx-ns-b"}

    def test_empty_returns_empty_without_bulk(self):
        client = MagicMock()
        store = _store_with_client(client)
        assert store.store_embeddings_batch([]) == []
        client.bulk_index.assert_not_called()


# ── lookups (filter construction + delegation) ──────────────────────────


class TestLookups:
    def test_get_embeddings_for_entity_builds_filters(self):
        client = MagicMock()
        client.filter_search.return_value = [{"entity_uri": "http://x/A"}]
        store = _store_with_client(client)
        out = store.get_embeddings_for_entity("http://x/A", entity_type="class", embedding_type="lexical")
        assert out == [{"entity_uri": "http://x/A"}]
        idx, filters = client.filter_search.call_args.args[0], client.filter_search.call_args.args[1]
        assert idx == "idx-ns1"
        assert {"term": {"entity_uri": "http://x/A"}} in filters
        assert {"term": {"entity_type": "class"}} in filters
        assert {"term": {"embedding_type": "lexical"}} in filters

    def test_list_embeddings_for_ontology_delegates_with_filters(self):
        client = MagicMock()
        client.filter_search.return_value = [{"entity_uri": "http://x/A"}, {"entity_uri": "http://x/B"}]
        store = _store_with_client(client)
        out = store.list_embeddings_for_ontology("ont-1", embedding_type="lexical")
        assert [r["entity_uri"] for r in out] == ["http://x/A", "http://x/B"]
        filters = client.filter_search.call_args.args[1]
        assert {"term": {"ontology_id": "ont-1"}} in filters
        assert {"term": {"embedding_type": "lexical"}} in filters

    def test_list_searchable_entity_uris_projects_via_iter_source_field(self):
        client = MagicMock()
        client.iter_source_field.return_value = ["http://x/A", "http://x/B"]
        store = _store_with_client(client)
        assert store.list_searchable_entity_uris("ont-1") == ["http://x/A", "http://x/B"]
        args = client.iter_source_field.call_args.args
        assert args[0] == "idx-ns1"
        assert args[1] == [{"term": {"ontology_id": "ont-1"}}]
        assert args[2] == "entity_uri"


# ── search_nearest (filters + hit mapping) ──────────────────────────────


class TestSearchNearest:
    def test_builds_filters_and_maps_hits(self):
        client = MagicMock()
        client.knn_search.return_value = [
            {
                "_score": 0.87,
                "entity_uri": "http://x/A",
                "embedding_type": "lexical",
                "model_id": "titan",
                "embedding": [0.1, 0.2],
                "ontology_id": "ont-1",
                "entity_type": "class",
                "text": "A def",
            }
        ]
        store = _store_with_client(client)
        hits = store.search_nearest(
            vector=[0.1, 0.2],
            embedding_type="lexical",
            model_id="titan",
            entity_type="class",
            ontology_id="ont-1",
            top_k=5,
        )
        assert len(hits) == 1
        h = hits[0]
        assert isinstance(h, EmbeddingHit)
        assert h.entity_uri == "http://x/A"
        assert h.score == 0.87
        assert h.text == "A def"
        # Delegates to knn_search with the right index / top_k / filters.
        kwargs = client.knn_search.call_args.kwargs
        assert kwargs["top_k"] == 5
        filt = kwargs["filters"]
        assert {"term": {"embedding_type": "lexical"}} in filt
        assert {"term": {"model_id": "titan"}} in filt
        assert {"term": {"ontology_id": "ont-1"}} in filt
        assert {"term": {"entity_type": "class"}} in filt

    def test_no_optional_filters_when_omitted(self):
        client = MagicMock()
        client.knn_search.return_value = []
        store = _store_with_client(client)
        hits = store.search_nearest(vector=[0.5] * 1024, embedding_type="lexical", model_id="titan")
        assert hits == []
        filt = client.knn_search.call_args.kwargs["filters"]
        # Only the two mandatory filters — no entity_type / ontology_id.
        assert len(filt) == 2


# ── deletes + health (pure delegation) ──────────────────────────────────


class TestDeletesAndHealth:
    def test_delete_embeddings_delegates_to_delete_by_term(self):
        client = MagicMock()
        client.delete_by_term.return_value = 2
        store = _store_with_client(client)
        assert store.delete_embeddings_for_ontology("ont-1") == 2
        client.delete_by_term.assert_called_once_with("idx-ns1", "ontology_id", "ont-1")

    def test_delete_embeddings_for_entities_uses_one_terms_delete(self):
        # Append-mode ingest: all incoming subjects in ONE delete_by_term call
        # (list form -> a single `terms` query + one verify loop), deduped,
        # empties skipped, AND-scoped by ontology_id so a shared IRI in a
        # different ontology is not deleted.
        client = MagicMock()
        client.delete_by_term.return_value = 5
        store = _store_with_client(client)
        n = store.delete_embeddings_for_entities(
            ["http://x/A", "http://x/B", "http://x/A", ""],
            ontology_id="http://x/ont",
            namespace="ns1",
        )
        assert n == 5
        client.delete_by_term.assert_called_once_with(
            "idx-ns1",
            "entity_uri",
            ["http://x/A", "http://x/B"],
            and_filters=[{"term": {"ontology_id": "http://x/ont"}}],
        )

    def test_delete_embeddings_for_entities_empty_is_noop(self):
        client = MagicMock()
        store = _store_with_client(client)
        assert store.delete_embeddings_for_entities([], ontology_id="http://x/ont", namespace="ns1") == 0
        client.delete_by_term.assert_not_called()

    def test_delete_embeddings_for_entities_without_ontology_id_is_noop(self):
        """Cross-ontology safety: refuse to delete without an ontology scope.

        A single namespace index carries embeddings for multiple ontologies
        (foundational reloads, explicit-target merges), so an unscoped delete
        would silently take a sibling ontology's embedding with it. If a
        caller ever hands in an empty ``ontology_id``, delete nothing.
        """
        client = MagicMock()
        store = _store_with_client(client)
        assert store.delete_embeddings_for_entities(["http://x/A"], ontology_id="", namespace="ns1") == 0
        client.delete_by_term.assert_not_called()

    def test_delete_embeddings_for_entities_scopes_to_ontology_at_the_aoss_query(self):
        """Regression: without the ``ontology_id`` clause, the AOSS query was
        just ``terms entity_uri IN uris`` — a shared IRI in a different
        ontology (foundational reload, explicit-target merge) got its
        embedding deleted alongside the intended one and re-created under
        the wrong owner. The fix wraps the terms clause in a ``bool.must``
        with a ``term ontology_id`` filter. This test asserts the query
        shape carrying that filter.
        """
        client = MagicMock()
        client.delete_by_term.return_value = 3
        store = _store_with_client(client)
        store.delete_embeddings_for_entities(
            ["http://x/Shared"],
            ontology_id="http://x/ont-A",
            namespace="ns1",
        )
        # The client received the terms clause AND the ontology_id term as a
        # separate AND filter — not merged into the terms list, not implicit.
        _args, kwargs = client.delete_by_term.call_args
        assert kwargs.get("and_filters") == [{"term": {"ontology_id": "http://x/ont-A"}}], (
            f"delete_by_term was called without the ontology_id AND-filter "
            f"— a shared IRI would be deleted across ontologies. Got kwargs={kwargs}"
        )

    def test_delete_index_delegates(self):
        client = MagicMock()
        client.delete_index.return_value = True
        store = OpenSearchVectorStore(namespace="ns1")
        store._client = client
        assert store.delete_index() is True
        client.delete_index.assert_called_once()

    def test_health_check_delegates(self):
        client = MagicMock()
        client.health_check.return_value = {"status": "ok", "index": "idx-ns1"}
        store = OpenSearchVectorStore(namespace="ns1")
        store._client = client
        assert store.health_check() == {"status": "ok", "index": "idx-ns1"}


# ── read paths must not create indexes (AOSS 1000-index quota) ───────────


class TestReadsDoNotCreateIndex:
    """Reads must never ``ensure_index``.

    Creating an index on the read path resurrects an empty shell for a
    namespace whose index was already dropped by namespace teardown. Nothing
    reclaims those shells and AOSS caps a collection at 1000 indexes, so they
    accumulate until every write fails with ``index_limit_breached`` (observed
    live: 454 leaked empty indexes filled the dev collection to 1000/1000 and
    broke four integ suites).
    """

    def _store(self, client: MagicMock) -> OpenSearchVectorStore:
        # Deliberately do NOT stub _read_index — we want the real resolver, and
        # we assert that resolving for a read never touches ensure_index.
        store = OpenSearchVectorStore(namespace="ns1")
        store._client = client
        return store

    def test_search_nearest_does_not_ensure_index(self):
        client = MagicMock()
        client.knn_search.return_value = []
        store = self._store(client)
        store.search_nearest([0.1], "lexical", "titan")
        client.ensure_index.assert_not_called()

    def test_get_embeddings_for_entity_does_not_ensure_index(self):
        client = MagicMock()
        client.filter_search.return_value = []
        store = self._store(client)
        store.get_embeddings_for_entity("http://x/A")
        client.ensure_index.assert_not_called()

    def test_list_embeddings_for_ontology_does_not_ensure_index(self):
        client = MagicMock()
        client.filter_search.return_value = []
        store = self._store(client)
        store.list_embeddings_for_ontology("ont-1")
        client.ensure_index.assert_not_called()

    def test_list_searchable_entity_uris_does_not_ensure_index(self):
        client = MagicMock()
        client.iter_source_field.return_value = []
        store = self._store(client)
        store.list_searchable_entity_uris("ont-1")
        client.ensure_index.assert_not_called()

    def test_delete_embeddings_does_not_ensure_index(self):
        client = MagicMock()
        client.delete_by_term.return_value = 0
        store = self._store(client)
        store.delete_embeddings_for_ontology("ont-1")
        client.ensure_index.assert_not_called()

    def test_writes_still_ensure_index(self):
        """The write path is the ONLY place an index may be created."""
        client = MagicMock()
        client.ensure_index.side_effect = lambda idx: idx
        store = self._store(client)
        store.store_embedding(
            {"entity_uri": "http://x/A", "embedding_type": "lexical", "model_id": "titan", "vector": [0.1]}
        )
        client.ensure_index.assert_called_once()

    def test_read_index_matches_ensure_index_name(self):
        """Reads and writes must resolve the SAME index name, or reads go blind."""
        store = OpenSearchVectorStore(namespace="ns1")
        store._client = MagicMock()
        store._client.ensure_index.side_effect = lambda idx: idx
        assert store._read_index() == store._ensure_index()
        assert store._read_index("other-ns") == store._ensure_index("other-ns")
