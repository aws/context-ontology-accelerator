# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MetricOpenSearchClient with mocked OpenSearch and Bedrock."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import URN_PREFIX
from coa_common.opensearch import DeleteIncompleteError
from coa_metrics.neptune_client import MetricAiContext, MetricDefinition, MetricDialect
from coa_metrics.opensearch_client import (
    MetricOpenSearchClient,
    _build_embedding_text,
)

pytestmark = pytest.mark.unit


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def sample_metric() -> MetricDefinition:
    return MetricDefinition(
        name="monthly_revenue",
        description="Total revenue from all orders",
        expression_dialects=[
            MetricDialect(dialect="trino", expression="SUM(orders.total_amount)"),
        ],
        data_source_id="ds-abc123",
        source_table="orders",
        ai_context=MetricAiContext(
            synonyms=["total sales", "revenue"],
            instructions="Use with order_date as time dimension.",
            examples=["What was revenue last month?"],
        ),
    )


@pytest.fixture
def metric_no_ai_context() -> MetricDefinition:
    return MetricDefinition(
        name="order_count",
        description="Number of orders",
        expression_dialects=[
            MetricDialect(dialect="trino", expression="COUNT(*)"),
        ],
        data_source_id="ds-abc123",
        source_table="orders",
    )


# ── Helper tests ────────────────────────────────────────────────────────


class TestHelpers:
    def test_build_embedding_text_with_ai_context(self, sample_metric: MetricDefinition) -> None:
        text = _build_embedding_text(sample_metric)
        assert "monthly_revenue" in text
        assert "Total revenue from all orders" in text
        assert "total sales revenue" in text
        assert "Use with order_date" in text
        assert "What was revenue last month?" in text

    def test_build_embedding_text_without_ai_context(self, metric_no_ai_context: MetricDefinition) -> None:
        text = _build_embedding_text(metric_no_ai_context)
        assert "order_count" in text
        assert "Number of orders" in text
        assert text == "order_count Number of orders"


# ── Embed metric tests ──────────────────────────────────────────────────
#
# MetricOpenSearchClient now delegates all AOSS I/O to the shared
# AossVectorClient (retry / auth / mapping / query-building are tested in
# coa_common's test_opensearch_client.py). Here we inject a mock
# shared client via ``client._client`` and assert the metric-specific shaping +
# delegation.


class TestEmbedMetric:
    @staticmethod
    def _fake_embedder(vector: list[float]) -> MagicMock:
        """A BedrockEmbedder stand-in whose embed_document returns `vector`."""
        embedder = MagicMock()
        embedder.embed_document.return_value = vector
        return embedder

    @staticmethod
    def _client_with(embedder: MagicMock) -> tuple[MetricOpenSearchClient, MagicMock]:
        client = MetricOpenSearchClient()
        client._embedder = embedder
        aoss = MagicMock()
        aoss.ensure_index.side_effect = lambda idx: idx  # echo the index name
        client._client = aoss
        return client, aoss

    def test_embed_metric_calls_bedrock_and_indexes(self, sample_metric: MetricDefinition) -> None:
        client, aoss = self._client_with(self._fake_embedder([0.1, 0.2, 0.3]))
        client.embed_metric("test-ns", sample_metric)

        # The shared embedder was invoked on the metric text (as a document).
        client._embedder.embed_document.assert_called_once()
        assert "monthly_revenue" in client._embedder.embed_document.call_args[0][0]

        # Indexed with the canonical schema (entity_type="metric").
        aoss.index_document.assert_called_once()
        index_used, indexed_doc = aoss.index_document.call_args.args
        assert index_used == "ontology-workbench-embeddings-test-ns"
        assert indexed_doc["entity_type"] == "metric"
        assert indexed_doc["entity_uri"] == f"urn:{URN_PREFIX}:test-ns:metric:monthly_revenue"
        assert indexed_doc["ontology_id"] == f"urn:{URN_PREFIX}:vocab#GovernedMetrics"
        assert indexed_doc["embedding_type"] == "lexical"
        assert indexed_doc["namespace"] == "test-ns"
        assert indexed_doc["embedding"] == [0.1, 0.2, 0.3]

    def test_embed_metric_deletes_existing_before_indexing(self, sample_metric: MetricDefinition) -> None:
        client, aoss = self._client_with(self._fake_embedder([0.1]))
        client.embed_metric("test-ns", sample_metric)

        # Idempotent upsert: delete-by-entity_uri, then index.
        aoss.delete_by_term.assert_called_once_with(
            "ontology-workbench-embeddings-test-ns",
            "entity_uri",
            f"urn:{URN_PREFIX}:test-ns:metric:monthly_revenue",
            page=10,
        )
        aoss.index_document.assert_called_once()

    def test_embed_metric_incomplete_delete_logs_and_still_indexes(self, sample_metric: MetricDefinition) -> None:
        # The delete-before-write is best-effort: a delete that stops with the old
        # doc still matching is logged at warning, and the new doc still lands.
        client, aoss = self._client_with(self._fake_embedder([0.1]))
        aoss.delete_by_term.side_effect = DeleteIncompleteError(
            "1 matching document(s) left", deleted=0, remaining=1, failed=2
        )
        with patch("coa_metrics.opensearch_client.logger") as log:
            client.embed_metric("test-ns", sample_metric)
        aoss.index_document.assert_called_once()
        warned = [c for c in log.warning.call_args_list if c.args and c.args[0] == "delete_by_entity_uri_incomplete"]
        assert len(warned) == 1
        assert warned[0].kwargs["remaining"] == 1


# ── Delete embedding tests ──────────────────────────────────────────────


class TestDeleteMetricEmbedding:
    def test_delete_by_entity_uri_delegates(self) -> None:
        client = MetricOpenSearchClient()
        aoss = MagicMock()
        client._client = aoss
        client.delete_metric_embedding("test-ns", "revenue")
        aoss.delete_by_term.assert_called_once_with(
            "ontology-workbench-embeddings-test-ns", "entity_uri", f"urn:{URN_PREFIX}:test-ns:metric:revenue", page=10
        )

    def test_delete_handles_not_found_gracefully(self) -> None:
        client = MetricOpenSearchClient()
        aoss = MagicMock()
        aoss.delete_by_term.side_effect = Exception("index_not_found")
        client._client = aoss
        # Best-effort: a missing index (first metric in a namespace) is swallowed.
        client.delete_metric_embedding("test-ns", "nonexistent")


# ── Search tests ────────────────────────────────────────────────────────


class TestSearchMetrics:
    def test_search_returns_results_with_entity_uri(self) -> None:
        client = MetricOpenSearchClient()
        aoss = MagicMock()
        aoss.knn_search.return_value = [
            {
                "entity_uri": f"urn:{URN_PREFIX}:test-ns:metric:revenue",
                "namespace": "test-ns",
                "text": "revenue Total revenue",
                "_score": 0.95,
            },
            {
                "entity_uri": f"urn:{URN_PREFIX}:test-ns:metric:cost",
                "namespace": "test-ns",
                "text": "cost Total cost",
                "_score": 0.82,
            },
        ]
        client._client = aoss
        results = client.search_metrics("test-ns", [0.1, 0.2, 0.3], top_k=5)

        assert len(results) == 2
        assert results[0]["metric_name"] == "revenue"
        assert results[0]["entity_uri"] == f"urn:{URN_PREFIX}:test-ns:metric:revenue"
        assert results[0]["score"] == 0.95

        # Delegates with the metric + namespace server-side filters.
        kwargs = aoss.knn_search.call_args.kwargs
        assert kwargs["top_k"] == 5
        assert {"term": {"entity_type": "metric"}} in kwargs["filters"]
        assert {"term": {"namespace": "test-ns"}} in kwargs["filters"]
