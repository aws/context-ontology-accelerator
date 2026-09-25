# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MetricNeptuneClient with mocked SPARQL transport."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_common.constants import GRAPH_BASE_URI, URN_PREFIX
from coa_metrics.neptune_client import (
    COA_VOCAB,
    OWL,
    RDFS,
    MetricAiContext,
    MetricDefinition,
    MetricDialect,
    MetricNeptuneClient,
    _esc,
    _iri,
    _metric_uri,
    _named_graph,
)

pytestmark = pytest.mark.unit


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> MetricNeptuneClient:
    return MetricNeptuneClient()


@pytest.fixture
def sample_metric() -> MetricDefinition:
    return MetricDefinition(
        name="monthly_revenue",
        description="Total revenue from all orders in a calendar month",
        expression_dialects=[
            MetricDialect(dialect="trino", expression="SUM(orders.total_amount)"),
            MetricDialect(dialect="postgresql", expression="SUM(orders.total_amount)"),
        ],
        data_source_id="ds-abc123",
        source_table="orders",
        default_time_grain="MONTH",
        unit="currency",
        return_type="decimal",
        ai_context=MetricAiContext(
            synonyms=["total sales", "revenue"],
            instructions="Use with order_date as time dimension.",
            examples=["What was revenue last month?"],
        ),
        ontology_concepts=[f"{GRAPH_BASE_URI}/namespace/test-ns/ontology/insurance/Order"],
        defined_by="steward@example.com",
        effective_from="2026-05-01",
    )


# ── Helper tests ────────────────────────────────────────────────────────


class TestHelpers:
    def test_iri_valid(self) -> None:
        assert _iri(f"urn:{URN_PREFIX}:test:metric/foo") == "<urn:coa:test:metric/foo>"

    def test_iri_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _iri("")

    def test_iri_rejects_injection(self) -> None:
        with pytest.raises(ValueError, match="forbidden"):
            _iri(f"urn:{URN_PREFIX}:test> ?s ?p ?o")

    def test_esc_special_chars(self) -> None:
        assert _esc('hello "world"\nnewline') == 'hello \\"world\\"\\nnewline'

    def test_named_graph_uses_ontology_engine_scheme(self) -> None:
        # Named graph should use {base}/{namespace}/{encoded ontology_id}
        graph = _named_graph("my-ns")
        assert "my-ns" in graph
        assert "GovernedMetrics" in graph
        # Should NOT be the old format
        assert graph != f"urn:{URN_PREFIX}:my-ns:published"

    def test_metric_uri(self) -> None:
        assert _metric_uri("my-ns", "revenue") == f"urn:{URN_PREFIX}:my-ns:metric:revenue"


# ── Create tests ────────────────────────────────────────────────────────


class TestCreateMetric:
    @patch("coa_metrics.neptune_client._sparql_query")
    @patch("coa_metrics.neptune_client._sparql_update")
    def test_create_builds_correct_sparql(
        self, mock_update: patch, mock_query: patch, client: MetricNeptuneClient, sample_metric: MetricDefinition
    ) -> None:
        mock_query.return_value = {"boolean": True}  # base vocabulary already seeded
        client.create_metric("test-ns", sample_metric)

        mock_update.assert_called_once()
        sparql = mock_update.call_args[0][0]

        # Verify graph target uses ontology-engine scheme
        assert "GRAPH" in sparql
        assert "test-ns" in sparql
        # Verify metric URI
        assert f"urn:{URN_PREFIX}:test-ns:metric:monthly_revenue" in sparql
        # Verify type declarations
        assert OWL + "Class" in sparql
        assert COA_VOCAB + "GovernedMetric" in sparql
        # Verify required fields
        assert "dataSourceId" in sparql
        assert "ds-abc123" in sparql
        assert "sourceTable" in sparql
        assert "orders" in sparql
        # Verify optional fields
        assert "defaultTimeGrain" in sparql
        assert "MONTH" in sparql
        assert "definedBy" in sparql
        assert "effectiveFrom" in sparql
        # Verify governedMetricFor as IRI object property (not string literal)
        assert "governedMetricFor" in sparql
        assert f"<{GRAPH_BASE_URI}/namespace/test-ns/ontology/insurance/Order>" in sparql

    @patch("coa_metrics.neptune_client._sparql_query")
    @patch("coa_metrics.neptune_client._sparql_update")
    def test_create_skips_non_iri_ontology_concepts(
        self, mock_update: patch, mock_query: patch, client: MetricNeptuneClient
    ) -> None:
        """ontology_concepts that aren't full IRIs are skipped (they should be resolved before write)."""
        mock_query.return_value = {"boolean": True}
        metric = MetricDefinition(
            name="test",
            description="desc",
            expression_dialects=[MetricDialect(dialect="trino", expression="COUNT(*)")],
            data_source_id="ds-1",
            source_table="t",
            ontology_concepts=["Order", "not-a-uri"],
        )
        client.create_metric("ns", metric)

        sparql = mock_update.call_args[0][0]
        # Non-IRI concepts should NOT appear as governedMetricFor triples
        assert "governedMetricFor" not in sparql


# ── Get tests ───────────────────────────────────────────────────────────


class TestGetMetric:
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_get_returns_metric_with_iri_concepts(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {
            "results": {
                "bindings": [
                    {"p": {"value": RDFS + "comment"}, "o": {"value": "A test metric"}},
                    {"p": {"value": COA_VOCAB + "dataSourceId"}, "o": {"value": "ds-123"}},
                    {"p": {"value": COA_VOCAB + "sourceTable"}, "o": {"value": "orders"}},
                    {
                        "p": {"value": COA_VOCAB + "expressionDialects"},
                        "o": {"value": '[{"dialect": "trino", "expression": "SUM(x)"}]'},
                    },
                    {"p": {"value": COA_VOCAB + "unit"}, "o": {"value": "count"}},
                    {
                        "p": {"value": COA_VOCAB + "governedMetricFor"},
                        "o": {"value": f"{GRAPH_BASE_URI}/namespace/ns/ontology/x/Order"},
                    },
                ]
            }
        }

        metric = client.get_metric("test-ns", "test_metric")

        assert metric is not None
        assert metric.name == "test_metric"
        assert metric.description == "A test metric"
        assert metric.data_source_id == "ds-123"
        assert metric.source_table == "orders"
        assert len(metric.expression_dialects) == 1
        assert metric.unit == "count"
        assert metric.ontology_concepts == [f"{GRAPH_BASE_URI}/namespace/ns/ontology/x/Order"]

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_get_returns_none_when_not_found(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {"results": {"bindings": []}}
        assert client.get_metric("test-ns", "nonexistent") is None


# ── List tests ──────────────────────────────────────────────────────────


class TestListMetrics:
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_list_with_no_filters(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.side_effect = [
            {"results": {"bindings": [{"s": {"value": f"urn:{URN_PREFIX}:test-ns:metric:revenue"}}]}},
            {
                "results": {
                    "bindings": [
                        {"p": {"value": RDFS + "comment"}, "o": {"value": "Revenue"}},
                        {"p": {"value": COA_VOCAB + "dataSourceId"}, "o": {"value": "ds-1"}},
                        {"p": {"value": COA_VOCAB + "sourceTable"}, "o": {"value": "orders"}},
                        {
                            "p": {"value": COA_VOCAB + "expressionDialects"},
                            "o": {"value": '[{"dialect": "trino", "expression": "SUM(x)"}]'},
                        },
                    ]
                }
            },
        ]

        metrics = client.list_metrics("test-ns")
        assert len(metrics) == 1
        assert metrics[0].name == "revenue"


# ── Bulk get tests ──────────────────────────────────────────────────────


class TestGetMetricsBulk:
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_bulk_issues_single_query_for_multiple_metrics(
        self, mock_query: patch, client: MetricNeptuneClient
    ) -> None:
        """Single SPARQL round-trip fetches + groups triples for N metrics."""
        mock_query.return_value = {
            "results": {
                "bindings": [
                    {
                        "s": {"value": f"urn:{URN_PREFIX}:test-ns:metric:revenue"},
                        "p": {"value": RDFS + "comment"},
                        "o": {"value": "Revenue"},
                    },
                    {
                        "s": {"value": f"urn:{URN_PREFIX}:test-ns:metric:revenue"},
                        "p": {"value": COA_VOCAB + "dataSourceId"},
                        "o": {"value": "ds-1"},
                    },
                    {
                        "s": {"value": f"urn:{URN_PREFIX}:test-ns:metric:revenue"},
                        "p": {"value": COA_VOCAB + "expressionDialects"},
                        "o": {"value": '[{"dialect": "trino", "expression": "SUM(x)"}]'},
                    },
                    {
                        "s": {"value": f"urn:{URN_PREFIX}:test-ns:metric:orders_count"},
                        "p": {"value": RDFS + "comment"},
                        "o": {"value": "Order count"},
                    },
                    {
                        "s": {"value": f"urn:{URN_PREFIX}:test-ns:metric:orders_count"},
                        "p": {"value": COA_VOCAB + "dataSourceId"},
                        "o": {"value": "ds-1"},
                    },
                    {
                        "s": {"value": f"urn:{URN_PREFIX}:test-ns:metric:orders_count"},
                        "p": {"value": COA_VOCAB + "expressionDialects"},
                        "o": {"value": '[{"dialect": "trino", "expression": "COUNT(*)"}]'},
                    },
                ]
            }
        }

        metrics = client.get_metrics_bulk("test-ns")

        # Exactly one round-trip regardless of metric count.
        mock_query.assert_called_once()
        names = {m.name for m in metrics}
        assert names == {"revenue", "orders_count"}
        by_name = {m.name: m for m in metrics}
        assert by_name["revenue"].description == "Revenue"
        assert by_name["orders_count"].description == "Order count"

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_bulk_with_names_filter_adds_values_clause(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {"results": {"bindings": []}}

        client.get_metrics_bulk("test-ns", names=["revenue", "orders_count"])

        sparql = mock_query.call_args[0][0]
        assert "VALUES ?s" in sparql
        assert f"urn:{URN_PREFIX}:test-ns:metric:revenue" in sparql
        assert f"urn:{URN_PREFIX}:test-ns:metric:orders_count" in sparql

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_bulk_without_names_omits_values_clause(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {"results": {"bindings": []}}

        client.get_metrics_bulk("test-ns")

        sparql = mock_query.call_args[0][0]
        assert "VALUES ?s" not in sparql

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_bulk_returns_empty_list_when_no_matches(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {"results": {"bindings": []}}
        assert client.get_metrics_bulk("test-ns") == []


# ── Update tests ────────────────────────────────────────────────────────


class TestUpdateMetric:
    @patch("coa_metrics.neptune_client._sparql_update")
    def test_update_is_atomic_delete_insert(
        self, mock_update: patch, client: MetricNeptuneClient, sample_metric: MetricDefinition
    ) -> None:
        client.update_metric("test-ns", "monthly_revenue", sample_metric)

        mock_update.assert_called_once()
        sparql = mock_update.call_args[0][0]

        assert "WITH" in sparql
        assert "DELETE" in sparql
        assert "INSERT" in sparql
        assert "WHERE" in sparql


# ── Delete tests ────────────────────────────────────────────────────────


class TestDeleteMetric:
    @patch("coa_metrics.neptune_client._sparql_update")
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_delete_existing_metric(self, mock_query: patch, mock_update: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {
            "results": {
                "bindings": [
                    {"p": {"value": RDFS + "comment"}, "o": {"value": "test"}},
                    {"p": {"value": COA_VOCAB + "dataSourceId"}, "o": {"value": "ds-1"}},
                    {"p": {"value": COA_VOCAB + "sourceTable"}, "o": {"value": "t"}},
                    {
                        "p": {"value": COA_VOCAB + "expressionDialects"},
                        "o": {"value": '[{"dialect": "x", "expression": "y"}]'},
                    },
                ]
            }
        }

        result = client.delete_metric("test-ns", "to_delete")
        assert result is True
        mock_update.assert_called_once()
        sparql = mock_update.call_args[0][0]
        assert "DELETE" in sparql
        assert f"urn:{URN_PREFIX}:test-ns:metric:to_delete" in sparql

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_delete_nonexistent_returns_false(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {"results": {"bindings": []}}
        assert client.delete_metric("test-ns", "nonexistent") is False


class TestDeleteAllMetricsForNamespace:
    @patch("coa_metrics.neptune_client._sparql_update")
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_deletes_all_metrics_without_enumeration(
        self, mock_query: patch, mock_update: patch, client: MetricNeptuneClient
    ) -> None:
        """Namespace-scoped bulk delete does not call get_metric per metric."""
        mock_query.return_value = {"results": {"bindings": [{"count": {"value": "3"}}]}}

        deleted = client.delete_all_metrics_for_namespace("test-ns")

        assert deleted == 3
        # First query counts metrics
        mock_query.assert_called_once()
        count_query = mock_query.call_args[0][0]
        assert "COUNT(DISTINCT ?s)" in count_query
        assert "GovernedMetric" in count_query

        # Second call is the DELETE
        mock_update.assert_called_once()
        delete_sparql = mock_update.call_args[0][0]
        assert "DELETE" in delete_sparql
        assert "GovernedMetric" in delete_sparql
        # Should delete all triples for all metrics in one SPARQL update
        assert "?s ?p ?o" in delete_sparql

    @patch("coa_metrics.neptune_client._sparql_update")
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_returns_zero_when_no_metrics_exist(
        self, mock_query: patch, mock_update: patch, client: MetricNeptuneClient
    ) -> None:
        """Idempotent: safe to call on empty namespace."""
        mock_query.return_value = {"results": {"bindings": [{"count": {"value": "0"}}]}}

        deleted = client.delete_all_metrics_for_namespace("empty-ns")

        assert deleted == 0
        mock_query.assert_called_once()
        # Should NOT issue DELETE when count is 0
        mock_update.assert_not_called()

    @patch("coa_metrics.neptune_client._sparql_update")
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_uses_correct_namespace_graph(
        self, mock_query: patch, mock_update: patch, client: MetricNeptuneClient
    ) -> None:
        """Deletion is scoped to the namespace's metric graph."""
        mock_query.return_value = {"results": {"bindings": [{"count": {"value": "1"}}]}}

        client.delete_all_metrics_for_namespace("my-namespace")

        # Verify both queries reference the namespace in the graph URI
        count_query = mock_query.call_args[0][0]
        assert "my-namespace" in count_query
        assert "GovernedMetrics" in count_query

        delete_sparql = mock_update.call_args[0][0]
        assert "my-namespace" in delete_sparql
        assert "GovernedMetrics" in delete_sparql


# ── Resolve class URIs tests ────────────────────────────────────────────


class TestResolveClassUris:
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_resolve_passes_full_iris_through(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        result = client.resolve_class_uris("ns", ["http://example.org/Class", f"urn:{URN_PREFIX}:ns:class:X"])
        mock_query.assert_not_called()
        assert result == ["http://example.org/Class", f"urn:{URN_PREFIX}:ns:class:X"]

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_resolve_looks_up_labels_in_batch(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {
            "results": {
                "bindings": [
                    {
                        "label": {"value": "Order"},
                        "cls": {"type": "uri", "value": f"{GRAPH_BASE_URI}/namespace/ns/ontology/x/Order"},
                    },
                ]
            }
        }
        result = client.resolve_class_uris("ns", ["Order"])
        mock_query.assert_called_once()
        assert result == [f"{GRAPH_BASE_URI}/namespace/ns/ontology/x/Order"]

    @pytest.mark.parametrize(
        "class_binding",
        [
            {"type": "uri", "value": "not-an-absolute-iri"},
            {"type": "uri", "value": "ftp://example.org/Order"},
            {"type": "literal", "value": "http://example.org/Order"},
            {"type": "uri", "value": 123},
            {"type": "uri", "value": "http://example.org/not a valid iri"},
        ],
        ids=["relative", "unsupported-scheme", "wrong-binding-type", "non-string", "unsafe"],
    )
    @patch("coa_metrics.neptune_client._sparql_query")
    def test_resolve_rejects_malformed_neptune_uri_as_protocol_error(
        self,
        mock_query: patch,
        client: MetricNeptuneClient,
        class_binding: dict[str, object],
    ) -> None:
        mock_query.return_value = {
            "results": {
                "bindings": [
                    {
                        "label": {"value": "Order"},
                        "cls": class_binding,
                    },
                ]
            }
        }

        with pytest.raises(ValueError, match="ontology resolver") as raised:
            client.resolve_class_uris("ns", ["Order"])

        assert raised.value.__class__ is ValueError

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_resolve_drops_unresolvable(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {"results": {"bindings": []}}
        result = client.resolve_class_uris("ns", ["NonExistentClass"])
        assert result == []

    @patch("coa_metrics.neptune_client._sparql_query")
    def test_resolve_mixes_iris_and_labels(self, mock_query: patch, client: MetricNeptuneClient) -> None:
        mock_query.return_value = {
            "results": {
                "bindings": [{"label": {"value": "Order"}, "cls": {"type": "uri", "value": "http://example.org/Order"}}]
            }
        }
        result = client.resolve_class_uris("ns", ["http://full.uri/X", "Order"])
        assert result == ["http://full.uri/X", "http://example.org/Order"]
