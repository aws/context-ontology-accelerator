# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for OSI export handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.api.export_osi import _build_osi_document, _metric_to_osi, handler
from coa_metrics.neptune_client import MetricAiContext, MetricDefinition, MetricDialect

pytestmark = pytest.mark.unit


def _make_event(namespace: str = "test-ns", names: list[str] | None = None) -> dict:
    event: dict = {
        "httpMethod": "GET",
        "pathParameters": {"namespaceId": namespace},
        "requestContext": {"authorizer": {"email": "test@example.com"}},
    }
    if names is not None:
        event["multiValueQueryStringParameters"] = {"names": names}
    return event


def _sample_metric(
    name: str = "monthly_revenue",
    dialect: str = "postgresql",
) -> MetricDefinition:
    return MetricDefinition(
        name=name,
        description="Total revenue",
        expression_dialects=[MetricDialect(dialect=dialect, expression="SUM(orders.total_amount)")],
        data_source_id="ds-abc123",
        source_table="orders",
        default_time_grain="MONTH",
        unit="currency",
        return_type="xsd:decimal",
        ai_context=MetricAiContext(
            synonyms=["total sales", "revenue"],
            instructions="Use with order_date",
            examples=["Q1 revenue"],
        ),
        ontology_concepts=["ind:Order"],
        defined_by="steward@example.com",
        effective_from="2026-01-15",
    )


class TestExportOsiHandler:
    """Tests for the export_osi handler function."""

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_successful_export(self, mock_neptune) -> None:
        neptune = MagicMock()
        neptune.get_metrics_bulk.return_value = [_sample_metric()]
        mock_neptune.return_value = neptune

        response = handler(_make_event(), None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "content" in body
        assert "osi_spec_version" in body["content"]
        assert "monthly_revenue" in body["content"]
        assert "ANSI_SQL" in body["content"]
        # Export must use the single-round-trip bulk fetch, not the N+1 list_metrics path.
        neptune.get_metrics_bulk.assert_called_once_with("test-ns", names=None)
        neptune.list_metrics.assert_not_called()

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_names_query_param_scopes_export(self, mock_neptune) -> None:
        """A repeated `names` query param exports only that subset."""
        neptune = MagicMock()
        neptune.get_metrics_bulk.return_value = [_sample_metric("metric_a")]
        mock_neptune.return_value = neptune

        response = handler(_make_event(names=["metric_a", "metric_b"]), None)

        assert response["statusCode"] == 200
        neptune.get_metrics_bulk.assert_called_once_with("test-ns", names=["metric_a", "metric_b"])

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_no_matching_names_returns_404_with_subset_message(self, mock_neptune) -> None:
        neptune = MagicMock()
        neptune.get_metrics_bulk.return_value = []
        mock_neptune.return_value = neptune

        response = handler(_make_event(names=["does_not_exist"]), None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert "No matching metrics found" in body["message"]

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_no_metrics_returns_404(self, mock_neptune) -> None:
        neptune = MagicMock()
        neptune.get_metrics_bulk.return_value = []
        mock_neptune.return_value = neptune

        response = handler(_make_event(), None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert "No metrics found" in body["message"]

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_neptune_failure_returns_500(self, mock_neptune) -> None:
        neptune = MagicMock()
        neptune.get_metrics_bulk.side_effect = RuntimeError("Connection failed")
        mock_neptune.return_value = neptune

        response = handler(_make_event(), None)

        assert response["statusCode"] == 500

    @patch("coa_metrics.api.export_osi.serialize_osi_yaml")
    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_serialization_failure_returns_500(self, mock_neptune, mock_serialize) -> None:
        """A serialize_osi_yaml failure must not propagate as an unhandled 500
        with no context — it should be caught, logged, and return a clean
        api_response(500, ...)."""
        neptune = MagicMock()
        neptune.get_metrics_bulk.return_value = [_sample_metric()]
        mock_neptune.return_value = neptune
        mock_serialize.side_effect = ValueError("bad yaml document")

        response = handler(_make_event(), None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "Failed to serialize OSI export" in body["message"]

    def test_missing_namespace_returns_400(self) -> None:
        event = {
            "httpMethod": "GET",
            "pathParameters": {"namespaceId": ""},
            "requestContext": {"authorizer": {}},
        }
        response = handler(event, None)
        assert response["statusCode"] == 400

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_multiple_metrics_exported(self, mock_neptune) -> None:
        neptune = MagicMock()
        neptune.get_metrics_bulk.return_value = [
            _sample_metric("metric_a"),
            _sample_metric("metric_b"),
        ]
        mock_neptune.return_value = neptune

        response = handler(_make_event(), None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "metric_a" in body["content"]
        assert "metric_b" in body["content"]


class TestMetricToOsi:
    """Tests for _metric_to_osi helper."""

    def test_maps_postgresql_to_ansi_sql(self) -> None:
        metric = _sample_metric(dialect="postgresql")
        osi = _metric_to_osi(metric)

        assert osi.expression[0].dialect == "ANSI_SQL"

    def test_maps_snowflake(self) -> None:
        metric = _sample_metric(dialect="snowflake")
        osi = _metric_to_osi(metric)

        assert osi.expression[0].dialect == "SNOWFLAKE"

    def test_maps_databricks(self) -> None:
        metric = _sample_metric(dialect="databricks")
        osi = _metric_to_osi(metric)

        assert osi.expression[0].dialect == "DATABRICKS"

    def test_custom_extensions_populated(self) -> None:
        metric = _sample_metric()
        osi = _metric_to_osi(metric)

        assert osi.custom_extensions is not None
        assert osi.custom_extensions.data_source_id == "ds-abc123"
        assert osi.custom_extensions.source_table == "orders"
        assert osi.custom_extensions.unit == "currency"
        assert osi.custom_extensions.time_dimension == "month"
        assert osi.custom_extensions.ontology_concepts == ["ind:Order"]

    def test_no_ai_context_produces_none(self) -> None:
        metric = MetricDefinition(
            name="simple",
            description="Simple",
            expression_dialects=[MetricDialect(dialect="postgresql", expression="COUNT(*)")],
            data_source_id="ds-1",
            source_table="t",
        )
        osi = _metric_to_osi(metric)

        assert osi.ai_context is None


class TestBuildOsiDocument:
    """Tests for _build_osi_document helper."""

    def test_deduplicates_datasets(self) -> None:
        metrics = [
            _sample_metric("metric_a"),
            _sample_metric("metric_b"),
        ]
        # Both use ds-abc123
        doc = _build_osi_document(metrics)

        assert len(doc.datasets) == 1
        assert doc.datasets[0].data_source_id == "ds-abc123"

    def test_multiple_data_sources_create_multiple_datasets(self) -> None:
        m1 = _sample_metric("metric_a")
        m2 = MetricDefinition(
            name="metric_b",
            description="B",
            expression_dialects=[MetricDialect(dialect="snowflake", expression="AVG(x)")],
            data_source_id="ds-xyz789",
            source_table="events",
        )
        doc = _build_osi_document([m1, m2])

        assert len(doc.datasets) == 2
        ds_ids = {d.data_source_id for d in doc.datasets}
        assert ds_ids == {"ds-abc123", "ds-xyz789"}

    def test_spec_version_set(self) -> None:
        doc = _build_osi_document([_sample_metric()])
        assert doc.osi_spec_version == "1.0"


class TestExportDatasetMapping:
    """Tests for dataset name/source mapping per OSI spec."""

    def test_dataset_uses_source_table_as_source(self) -> None:
        """source field should be the fully qualified table name."""
        metric = _sample_metric()
        metric.source_table = "public.orders"
        doc = _build_osi_document([metric])

        assert len(doc.datasets) == 1
        ds = doc.datasets[0]
        assert ds.source == "public.orders"

    def test_dataset_name_is_short_logical_name(self) -> None:
        """name field should be the last segment of the qualified table name."""
        metric = _sample_metric()
        metric.source_table = "tpcds.public.store_sales"
        doc = _build_osi_document([metric])

        assert len(doc.datasets) == 1
        ds = doc.datasets[0]
        assert ds.name == "store_sales"
        assert ds.source == "tpcds.public.store_sales"

    def test_dataset_name_unqualified_table(self) -> None:
        """If source_table has no dots, name and source are the same."""
        metric = _sample_metric()
        metric.source_table = "orders"
        doc = _build_osi_document([metric])

        ds = doc.datasets[0]
        assert ds.name == "orders"
        assert ds.source == "orders"


class TestExportAiContext:
    """Tests for structured ai_context export per OSI spec."""

    def test_ai_context_exported_as_structured_object(self) -> None:
        metric = _sample_metric()
        osi_metric = _metric_to_osi(metric)

        assert osi_metric.ai_context is not None
        assert osi_metric.ai_context.synonyms == ["total sales", "revenue"]
        assert osi_metric.ai_context.instructions == "Use with order_date"
        assert osi_metric.ai_context.examples == ["Q1 revenue"]

    def test_ai_context_with_periods_preserved(self) -> None:
        """Regression: periods in instructions must not be corrupted."""
        metric = _sample_metric()
        metric.ai_context = MetricAiContext(
            instructions="Use this metric carefully. Do not use for forecasting. Always filter by date.",
        )
        osi_metric = _metric_to_osi(metric)

        assert osi_metric.ai_context is not None
        expected = "Use this metric carefully. Do not use for forecasting. Always filter by date."
        assert osi_metric.ai_context.instructions == expected
