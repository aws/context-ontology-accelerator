# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for na_graph.py and na_vector.py (thin wrappers over na_store).

Mocks the na_store module to verify delegation, namespace resolution,
and EmbeddingHit construction without requiring a live Neptune Analytics graph.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ── NeptuneAnalyticsVectorStore ─────────────────────────────────────────


class TestNeptuneAnalyticsVectorStore:
    @pytest.fixture(autouse=True)
    def _mock_na_store(self):
        with patch("coa_ontology.stores.na_vector.na_store") as m:
            m.VSS_DIMENSION = 1024
            m.store_embedding = MagicMock(return_value={"ok": True})
            m.store_embeddings_batch = MagicMock(return_value=[])
            m.get_embeddings_for_entity = MagicMock(return_value=[])
            m.list_embeddings_for_ontology = MagicMock(return_value=[])
            m.list_searchable_entity_uris = MagicMock(return_value=[])
            m.search_nearest = MagicMock(return_value=[])
            m.delete_embeddings_for_ontology = MagicMock(return_value={})
            m.delete_embeddings_for_entities = MagicMock(return_value=2)
            m.health_check = MagicMock(return_value={"status": "healthy"})
            self.mock = m
            yield

    def _make_store(self, namespace=None):
        from coa_ontology.stores.na_vector import NeptuneAnalyticsVectorStore

        return NeptuneAnalyticsVectorStore(namespace=namespace)

    def test_dimensions_from_na_store(self):
        store = self._make_store()
        assert store.dimensions == 1024

    def test_store_embedding_delegates(self):
        store = self._make_store(namespace="ns1")
        data = {"entity_uri": "http://ex.com/A", "vector": [0.1, 0.2]}
        store.store_embedding(data)
        self.mock.store_embedding.assert_called_once_with(data, namespace="ns1")

    def test_store_embeddings_batch_injects_namespace(self):
        store = self._make_store(namespace="ns1")
        items = [{"entity_uri": "http://ex.com/A"}, {"entity_uri": "http://ex.com/B", "namespace": "explicit"}]
        store.store_embeddings_batch(items)
        call_items = self.mock.store_embeddings_batch.call_args.args[0]
        assert call_items[0]["namespace"] == "ns1"
        assert call_items[1]["namespace"] == "explicit"

    def test_get_embeddings_for_entity_delegates(self):
        store = self._make_store(namespace="ns1")
        store.get_embeddings_for_entity("http://ex.com/A", entity_type="class")
        self.mock.get_embeddings_for_entity.assert_called_once_with(
            "http://ex.com/A", entity_type="class", embedding_type=None, namespace="ns1"
        )

    def test_namespace_override_wins(self):
        store = self._make_store(namespace="bound")
        store.get_embeddings_for_entity("http://ex.com/A", namespace="override")
        self.mock.get_embeddings_for_entity.assert_called_once_with(
            "http://ex.com/A", entity_type=None, embedding_type=None, namespace="override"
        )

    def test_list_searchable_entity_uris_delegates(self):
        store = self._make_store(namespace="ns1")
        self.mock.list_searchable_entity_uris.return_value = ["http://ex.com/A", "http://ex.com/B"]
        out = store.list_searchable_entity_uris("onto1")
        assert out == ["http://ex.com/A", "http://ex.com/B"]
        self.mock.list_searchable_entity_uris.assert_called_once_with("onto1", namespace="ns1")

    def test_search_nearest_builds_embedding_hits(self):
        self.mock.search_nearest.return_value = [
            {
                "entity_uri": "http://ex.com/A",
                "embedding_type": "lexical",
                "model_id": "titan-v2",
                "vector": [0.1],
                "ontology_id": "onto1",
                "entity_type": "class",
                "score": 0.95,
                "extra_field": "meta",
            }
        ]
        store = self._make_store(namespace="ns1")
        hits = store.search_nearest(vector=[0.1], embedding_type="lexical", model_id="titan-v2", top_k=5)
        assert len(hits) == 1
        h = hits[0]
        assert h.entity_uri == "http://ex.com/A"
        assert h.score == 0.95
        assert h.metadata == {"extra_field": "meta"}

    def test_delete_embeddings_delegates(self):
        store = self._make_store(namespace="ns1")
        store.delete_embeddings_for_ontology("onto1")
        self.mock.delete_embeddings_for_ontology.assert_called_once_with("onto1", namespace="ns1")

    def test_delete_embeddings_for_entities_scopes_by_ontology_id(self):
        # The whole point of the ontology_id filter: a shared IRI in a sibling
        # ontology under the same namespace must not have its embedding taken
        # down alongside the intended one. The wrapper must pass ontology_id
        # through to na_store so the openCypher can filter on `e.ontology_id`.
        store = self._make_store(namespace="ns1")
        n = store.delete_embeddings_for_entities(
            ["http://ex.com/A", "http://ex.com/B"],
            ontology_id="http://ex.com/onto1",
        )
        assert n == 2
        self.mock.delete_embeddings_for_entities.assert_called_once_with(
            ["http://ex.com/A", "http://ex.com/B"],
            ontology_id="http://ex.com/onto1",
            namespace="ns1",
        )

    def test_delete_embeddings_for_entities_without_ontology_id_is_noop(self):
        # Fail-closed: rather than issue an unscoped delete that could wipe a
        # sibling ontology's embedding, refuse and return 0. Verifies the
        # wrapper never reaches na_store when ontology_id is empty.
        store = self._make_store(namespace="ns1")
        assert store.delete_embeddings_for_entities(["http://ex.com/A"], ontology_id="") == 0
        self.mock.delete_embeddings_for_entities.assert_not_called()

    def test_delete_embeddings_for_entities_without_uris_is_noop(self):
        # Symmetric fail-closed for the other input.
        store = self._make_store(namespace="ns1")
        assert store.delete_embeddings_for_entities([], ontology_id="http://ex.com/onto1") == 0
        self.mock.delete_embeddings_for_entities.assert_not_called()

    def test_health_check_delegates(self):
        store = self._make_store()
        assert store.health_check() == {"status": "healthy"}


# ── NeptuneAnalyticsGraphStore ──────────────────────────────────────────


class TestNeptuneAnalyticsGraphStore:
    @pytest.fixture(autouse=True)
    def _mock_na_store(self):
        with patch("coa_ontology.stores.na_graph.na_store") as m:
            m.health_check = MagicMock(return_value={"status": "healthy"})
            m.create_ontology = MagicMock(return_value={"uri": "http://ex.com/onto"})
            m.get_ontology_by_uri = MagicMock(return_value=None)
            m.list_ontologies = MagicMock(return_value=[])
            m.store_class = MagicMock(return_value={})
            m.store_property = MagicMock(return_value={})
            m.update_ontology = MagicMock(return_value={})
            m.delete_ontology = MagicMock(return_value={})
            self.mock = m
            yield

    def _make_store(self, namespace=None):
        from coa_ontology.stores.na_graph import NeptuneAnalyticsGraphStore

        return NeptuneAnalyticsGraphStore(namespace=namespace)

    def test_health_check(self):
        store = self._make_store()
        assert store.health_check() == {"status": "healthy"}

    def test_create_ontology_delegates(self):
        store = self._make_store(namespace="ns1")
        store.create_ontology({"uri": "http://ex.com/onto", "title": "Test"})
        self.mock.create_ontology.assert_called_once()
        call_data = self.mock.create_ontology.call_args.args[0]
        assert call_data["uri"] == "http://ex.com/onto"

    def test_store_class_delegates(self):
        store = self._make_store(namespace="ns1")
        store.store_class(
            ontology_uri="http://ex.com/onto",
            class_uri="http://ex.com/onto#Foo",
            labels=["Foo"],
            comments=[],
            super_classes=[],
        )
        self.mock.store_class.assert_called_once()

    def test_store_property_delegates(self):
        store = self._make_store(namespace="ns1")
        store.store_property(
            ontology_uri="http://ex.com/onto",
            prop_uri="http://ex.com/onto#bar",
            label="bar",
            comment="",
            domains=[],
            ranges=[],
        )
        self.mock.store_property.assert_called_once()

    def test_load_turtle_skips_empty(self):
        store = self._make_store()
        result = store.load_turtle(ontology_uri="http://ex.com/onto", turtle="")
        assert result["status"] == "skipped"

    def test_delete_ontology_delegates(self):
        store = self._make_store(namespace="ns1")
        store.delete_ontology("http://ex.com/onto")
        self.mock.delete_ontology.assert_called_once()
