# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Validate Metric handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.api.validate_metric import handler
from coa_metrics.source_status import SourceValidationUnavailableError

pytestmark = pytest.mark.unit


# ── Test helpers ────────────────────────────────────────────────────────


def _make_event(body: dict | None = None, namespace: str = "test-ns") -> dict:
    """Build a minimal API Gateway proxy event for POST /namespaces/{ns}/metrics/validate."""
    path_params = {"namespaceId": namespace} if namespace else {}
    return {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/metrics/validate",
        "path": f"/namespaces/{namespace}/metrics/validate",
        "pathParameters": path_params,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"claims": {"email": "test@example.com"}}},
    }


def _valid_body() -> dict:
    """Return a valid validate-metric request body (no SQL syntax errors)."""
    return {
        "name": "monthly_revenue",
        "description": "Total revenue from all orders in a calendar month",
        "expression": {"dialects": [{"dialect": "TRINO", "expression": "SELECT SUM(total_amount) FROM orders"}]},
        "dataSourceId": "ds-abc123",
        "sourceTable": "orders",
        "ontologyConcepts": ["ind:Order"],
    }


# Patch lookups to None so only Check 1 (SQL syntax) runs — keeps tests hermetic.
_no_lookups = patch.multiple(
    "coa_metrics.api.validate_metric",
    _get_data_source_lookup=MagicMock(return_value=None),
    _get_ontology_lookup=MagicMock(return_value=None),
    check_source_approved=MagicMock(return_value=None),
    check_source_table_exists=MagicMock(return_value=None),
)


# ── Request validation tests ────────────────────────────────────────────


class TestRequestValidation:
    def test_missing_namespace_returns_400(self) -> None:
        resp = handler(_make_event(body=_valid_body(), namespace=""), None)
        assert resp["statusCode"] == 400
        assert "namespace" in json.loads(resp["body"])["message"]

    def test_missing_body_returns_400(self) -> None:
        event = _make_event(body=None)
        resp = handler(event, None)
        assert resp["statusCode"] == 400
        assert "required" in json.loads(resp["body"])["message"]

    def test_invalid_json_returns_400(self) -> None:
        event = _make_event()
        event["body"] = "not json {"
        resp = handler(event, None)
        assert resp["statusCode"] == 400
        assert "Invalid JSON" in json.loads(resp["body"])["message"]

    def test_missing_required_field_returns_400(self) -> None:
        body = _valid_body()
        del body["name"]
        resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 400


# ── Success tests ───────────────────────────────────────────────────────


class TestSuccess:
    def test_valid_metric_reports_unconfigured_catalog(self) -> None:
        with _no_lookups:
            resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        assert warnings == [
            {
                "field": "table_reference",
                "message": (
                    "Source table 'orders' was not verified because data source catalog metadata is not configured"
                ),
                "severity": "INFO",
            }
        ]

    def test_bad_sql_returns_200_with_error_severity(self) -> None:
        body = _valid_body()
        body["expression"] = {"dialects": [{"dialect": "TRINO", "expression": "SELECT FROM WHERE )("}]}
        with _no_lookups:
            resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        assert len(warnings) >= 1
        warning = warnings[0]
        assert warning["field"] == "sql_syntax"
        # #617/#1050: blocking checks surface as ERROR so validate predicts
        # the create/serve rejection (was misleadingly downgraded to WARNING).
        assert warning["severity"] == "ERROR"
        assert "message" in warning

    def test_response_shape_only_has_warnings(self) -> None:
        with _no_lookups:
            resp = handler(_make_event(body=_valid_body()), None)
        body = json.loads(resp["body"])
        assert list(body.keys()) == ["warnings"]

    def test_unknown_source_is_reported_as_error(self) -> None:
        source_message = "Data source 'ds-abc123' does not exist in namespace 'test-ns'"
        mock_table_check = MagicMock()
        with patch.multiple(
            "coa_metrics.api.validate_metric",
            _get_data_source_lookup=MagicMock(return_value=None),
            _get_ontology_lookup=MagicMock(return_value=None),
            check_source_approved=MagicMock(return_value=source_message),
            check_source_table_exists=mock_table_check,
        ):
            resp = handler(_make_event(body=_valid_body()), None)

        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        assert {"field": "dataSourceId", "message": source_message, "severity": "ERROR"} in warnings
        mock_table_check.assert_not_called()

    def test_missing_source_table_is_reported_as_error(self) -> None:
        table_message = "Source table 'orders' not found in data source 'ds-abc123'"
        with patch.multiple(
            "coa_metrics.api.validate_metric",
            _get_data_source_lookup=MagicMock(return_value=None),
            _get_ontology_lookup=MagicMock(return_value=None),
            check_source_approved=MagicMock(return_value=None),
            check_source_table_exists=MagicMock(return_value=table_message),
        ):
            resp = handler(_make_event(body=_valid_body()), None)

        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        assert {"field": "sourceTable", "message": table_message, "severity": "ERROR"} in warnings


# ── Error handling tests ────────────────────────────────────────────────


class TestErrorHandling:
    @patch("coa_metrics.api.validate_metric.check_source_approved")
    def test_source_validation_unavailable_returns_503(self, mock_source: MagicMock) -> None:
        mock_source.side_effect = SourceValidationUnavailableError("DynamoDB unavailable")

        resp = handler(_make_event(body=_valid_body()), None)

        assert resp["statusCode"] == 503
        assert "validation is unavailable" in json.loads(resp["body"])["message"]

    @patch("coa_metrics.api.validate_metric.check_source_approved")
    def test_sql_error_precedes_source_validation_outage(self, mock_source: MagicMock) -> None:
        mock_source.side_effect = SourceValidationUnavailableError("DynamoDB unavailable")
        body = _valid_body()
        body["expression"]["dialects"][0]["expression"] = "SELECT FROM WHERE )("

        resp = handler(_make_event(body=body), None)

        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        assert any(warning["field"] == "sql_syntax" and warning["severity"] == "ERROR" for warning in warnings)
        mock_source.assert_not_called()

    @patch("coa_metrics.api.validate_metric.check_source_approved")
    def test_sql_shape_error_precedes_source_validation_outage(self, mock_source: MagicMock) -> None:
        mock_source.side_effect = SourceValidationUnavailableError("DynamoDB unavailable")
        body = _valid_body()
        body["expression"]["dialects"][0]["expression"] = "COUNT(*)"

        resp = handler(_make_event(body=body), None)

        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        assert any(warning["field"] == "sql_shape" and warning["severity"] == "ERROR" for warning in warnings)
        mock_source.assert_not_called()

    @patch("coa_metrics.api.validate_metric.validate_metric")
    def test_validation_exception_returns_500(self, mock_validate: MagicMock) -> None:
        mock_validate.side_effect = RuntimeError("boom")
        with _no_lookups:
            resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 500
        assert "Internal error" in json.loads(resp["body"])["message"]

    @patch("coa_metrics.api.validate_metric.validate_metric")
    def test_blocking_maps_to_error_soft_to_info_severity(self, mock_validate: MagicMock) -> None:
        mock_validate.return_value = MagicMock(
            valid=False,
            errors=[{"check": "sql_syntax", "message": "bad sql"}],
            warnings=[{"check": "ontology_linkage", "message": "class not found"}],
        )
        with _no_lookups:
            resp = handler(_make_event(body=_valid_body()), None)
        warnings = json.loads(resp["body"])["warnings"]
        by_field = {w["field"]: w["severity"] for w in warnings}
        # #617/#1050: blocking checks (result.errors) surface as ERROR; soft
        # findings (result.warnings) stay INFO.
        assert by_field["sql_syntax"] == "ERROR"
        assert by_field["ontology_linkage"] == "INFO"


# ── Shape severity (#617/#1050) ─────────────────────────────────────────


class TestShapeSeverity:
    """A non-SELECT fragment must surface as an ERROR-severity finding so a
    'validate before create' truthfully predicts the create/serve rejection."""

    def test_fragment_surfaces_error_severity(self) -> None:
        body = _valid_body()
        body["expression"] = {"dialects": [{"dialect": "TRINO", "expression": "COUNT(*)"}]}
        with _no_lookups:
            resp = handler(_make_event(body=body), None)

        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        shape = [w for w in warnings if w["field"] == "sql_shape"]
        assert len(shape) == 1
        assert shape[0]["severity"] == "ERROR"
        assert "full SELECT statement" in shape[0]["message"]


# ── Declared sourceTable: one verdict, one wording ──────────────────────


class TestKnownButRejectedSourceTable:
    """The table is in the catalog but ``table_exists`` rejects it.

    The source gate and the validator's ``table_reference`` check both run on the
    real SMUS lookup here, with only the DataZone reads patched, so the response
    shows what a caller of validate-before-create actually gets.
    """

    @staticmethod
    def _asset(status: str):
        from coa_common.domain_models import BusinessMetadata, Column, ReviewStatus, Table

        return Table(
            name="orders",
            database="public",
            business_metadata=BusinessMetadata(review_status=ReviewStatus(status)),
            columns=[Column(name="total_amount", data_type="decimal")],
        )

    @pytest.mark.parametrize(
        ("asset_status", "expected_tail"),
        [
            ("PENDING_REVIEW", "but is not approved — approve the table on the data source, then retry"),
            (None, "but its catalog metadata is missing or could not be read — re-scan the data source, then retry"),
        ],
    )
    def test_no_finding_says_not_found(self, monkeypatch: pytest.MonkeyPatch, asset_status, expected_tail) -> None:
        from coa_metrics.lookups import SmusCatalogDataSourceLookup
        from coa_metrics.source_status import PERMISSIVE_ENV

        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        lookup = SmusCatalogDataSourceLookup(
            domain_id="dzd-1", project_id="prj-1", namespace_id="test-ns", datasources_table="sources-tbl"
        )
        asset = None if asset_status is None else self._asset(asset_status)
        with (
            patch("coa_metrics.lookups.read_asset_names_for_datasource", return_value={"orders": "asset-orders"}),
            patch("coa_metrics.lookups.read_table_for_asset", return_value=asset),
            patch("coa_metrics.data_source_lookup_factory.build_data_source_lookup", return_value=lookup),
            patch.multiple(
                "coa_metrics.api.validate_metric",
                _get_data_source_lookup=MagicMock(return_value=lookup),
                _get_ontology_lookup=MagicMock(return_value=None),
                check_source_approved=MagicMock(return_value=None),
            ),
        ):
            resp = handler(_make_event(body=_valid_body()), None)

        assert resp["statusCode"] == 200
        warnings = json.loads(resp["body"])["warnings"]
        expected = f"Source table 'orders' exists in data source 'ds-abc123' {expected_tail}"
        assert {"field": "sourceTable", "message": expected, "severity": "ERROR"} in warnings
        assert {"field": "table_reference", "message": expected, "severity": "INFO"} in warnings
        assert not [w for w in warnings if "not found" in w["message"]]
