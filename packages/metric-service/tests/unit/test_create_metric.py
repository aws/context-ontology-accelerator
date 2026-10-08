# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Create Metric handler."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import EVENT_SOURCE_PREFIX, GRAPH_BASE_URI
from coa_control_plane_server.models.create_metric_request_content import (
    CreateMetricRequestContent,
)
from coa_metrics.api.create_metric import _validate_soft, handler
from coa_metrics.source_status import DATA_SOURCE_ID_FORMAT_MESSAGE, PERMISSIVE_ENV
from coa_metrics.validator import ValidationResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _permissive_source_lookup(monkeypatch: pytest.MonkeyPatch):
    """Skip APPROVED-source enforcement in handler tests — the
    dedicated enforcement tests below opt out by clearing the env var."""
    monkeypatch.setenv(PERMISSIVE_ENV, "true")
    yield


# ── Test helpers ────────────────────────────────────────────────────────


def _make_event(body: dict | None = None, namespace: str = "test-ns") -> dict:
    """Build a minimal API Gateway proxy event."""
    return {
        "httpMethod": "POST",
        "path": f"/namespaces/{namespace}/metrics",
        "pathParameters": {"namespaceId": namespace},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {"claims": {"email": "test@example.com"}},
        },
    }


def _valid_body() -> dict:
    """Return a valid create metric request body."""
    return {
        "name": "monthly_revenue",
        "description": "Total revenue from all orders in a calendar month",
        "expression": {
            "dialects": [
                # Full SELECT — fragments are rejected at onboarding
                {"dialect": "TRINO", "expression": "SELECT SUM(total_amount) AS monthly_revenue FROM orders"}
            ]
        },
        "dataSourceId": "ds-abc123",
        "sourceTable": "orders",
        "defaultTimeGrain": "MONTH",
        "unit": "currency",
        "aiContext": {
            "synonyms": ["total sales"],
            "instructions": "Use with order_date.",
            "examples": ["What was revenue last month?"],
        },
        "ontologyConcepts": ["ind:Order"],
    }


# ── Validation tests ────────────────────────────────────────────────────


class TestValidation:
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_missing_body_returns_400(self, mock_neptune: MagicMock) -> None:
        event = _make_event(body=None)
        event["body"] = None
        resp = handler(event, None)
        assert resp["statusCode"] == 400
        assert "required" in json.loads(resp["body"])["message"]

    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_invalid_json_returns_400(self, mock_neptune: MagicMock) -> None:
        event = _make_event()
        event["body"] = "not json {"
        resp = handler(event, None)
        assert resp["statusCode"] == 400
        assert "Invalid JSON" in json.loads(resp["body"])["message"]

    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_missing_required_field_returns_400(self, mock_neptune: MagicMock) -> None:
        body = _valid_body()
        del body["name"]
        resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 400
        assert "required" in json.loads(resp["body"])["message"].lower()

    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_empty_expression_dialects_returns_400(self, mock_neptune: MagicMock) -> None:
        body = _valid_body()
        body["expression"] = {"dialects": []}
        resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 400
        msg = json.loads(resp["body"])["message"].lower()
        assert "at least 1 item" in msg or "validation" in msg

    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_invalid_dialect_entry_returns_400(self, mock_neptune: MagicMock) -> None:
        body = _valid_body()
        body["expression"] = [{"dialect": "TRINO"}]  # missing expression field, wrong structure
        mock_neptune.return_value.get_metric.return_value = None
        resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 400
        msg = json.loads(resp["body"])["message"].lower()
        assert "valid" in msg or "expression" in msg or "metricexpression" in msg

    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_athena_dialect_returns_400_with_trino_hint(self, mock_neptune: MagicMock) -> None:
        """ATHENA is not a valid SqlDialect and won't become one
        (Athena's engine roadmap may drop Trino) — the 400 must hint TRINO."""
        body = _valid_body()
        body["expression"] = {"dialects": [{"dialect": "ATHENA", "expression": "SUM(orders.total_amount)"}]}
        mock_neptune.return_value.get_metric.return_value = None
        resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 400
        msg = json.loads(resp["body"])["message"]
        assert "TRINO" in msg
        assert "Athena-backed sources" in msg

    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_flat_top_level_dialects_returns_400_with_shape_hint(self, mock_neptune: MagicMock) -> None:
        """A flat top-level 'dialects' (no 'expression' wrapper) is
        the reported mistake — the 400 message must hint at the correct nested
        shape, not just Pydantic's generic 'Field required'."""
        body = _valid_body()
        del body["expression"]
        body["dialects"] = [{"dialect": "TRINO", "expression": "SUM(orders.total_amount)"}]
        mock_neptune.return_value.get_metric.return_value = None
        resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 400
        msg = json.loads(resp["body"])["message"]
        assert "Field required" in msg
        assert '"dialects"' in msg
        assert "must be nested" in msg


# ── Conflict tests ──────────────────────────────────────────────────────


class TestDataSourceIdFormat:
    """dataSourceId is charset-checked before any source lookup or write."""

    @pytest.mark.parametrize("data_source_id", ["<script>alert(1)</script>", "ds 1", "ds/../x", "x" * 129])
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_malformed_data_source_id_returns_400(self, mock_neptune: MagicMock, data_source_id: str) -> None:
        body = {**_valid_body(), "dataSourceId": data_source_id}
        resp = handler(_make_event(body=body), None)
        assert resp["statusCode"] == 400
        if len(data_source_id) <= 128:
            assert json.loads(resp["body"])["message"] == DATA_SOURCE_ID_FORMAT_MESSAGE
        mock_neptune.return_value.create_metric.assert_not_called()

    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_pattern_failure_message_names_field(self, mock_neptune: MagicMock) -> None:
        body = {**_valid_body(), "dataSourceId": "<script>"}
        message = json.loads(handler(_make_event(body=body), None)["body"])["message"]
        assert message == DATA_SOURCE_ID_FORMAT_MESSAGE


class TestConflict:
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_existing_metric_returns_409(self, mock_neptune: MagicMock, mock_opensearch: MagicMock) -> None:
        mock_neptune.return_value.get_metric.return_value = MagicMock()  # exists
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 409
        assert "already exists" in json.loads(resp["body"])["message"]


# ── Success tests ───────────────────────────────────────────────────────


class TestSuccess:
    @patch("coa_metrics.api.create_metric._get_eventbridge")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_successful_create_returns_201(
        self,
        mock_neptune: MagicMock,
        mock_opensearch: MagicMock,
        mock_eventbridge: MagicMock,
    ) -> None:
        mock_neptune.return_value.get_metric.return_value = None  # doesn't exist
        mock_neptune.return_value.create_metric.return_value = None
        mock_opensearch.return_value.embed_metric.return_value = None
        mock_eventbridge.return_value.put_events.return_value = {}

        resp = handler(_make_event(body=_valid_body()), None)

        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        assert body["metric"]["name"] == "monthly_revenue"
        assert body["metric"]["dataSourceId"] == "ds-abc123"
        assert body["metric"]["definedBy"] == "test@example.com"
        assert "effectiveFrom" in body["metric"]

    @patch("coa_metrics.api.create_metric._get_eventbridge")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_create_calls_neptune(
        self,
        mock_neptune: MagicMock,
        mock_opensearch: MagicMock,
        mock_eventbridge: MagicMock,
    ) -> None:
        mock_neptune.return_value.get_metric.return_value = None
        mock_eventbridge.return_value.put_events.return_value = {}

        handler(_make_event(body=_valid_body()), None)

        mock_neptune.return_value.create_metric.assert_called_once()
        call_args = mock_neptune.return_value.create_metric.call_args
        assert call_args[0][0] == "test-ns"  # namespace
        assert call_args[0][1].name == "monthly_revenue"  # metric

    @patch("coa_metrics.api.create_metric._get_eventbridge")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_create_calls_opensearch(
        self,
        mock_neptune: MagicMock,
        mock_opensearch: MagicMock,
        mock_eventbridge: MagicMock,
    ) -> None:
        mock_neptune.return_value.get_metric.return_value = None
        mock_eventbridge.return_value.put_events.return_value = {}

        handler(_make_event(body=_valid_body()), None)

        mock_opensearch.return_value.embed_metric.assert_called_once()

    @patch("coa_metrics.api.create_metric._get_eventbridge")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_create_emits_eventbridge_event(
        self,
        mock_neptune: MagicMock,
        mock_opensearch: MagicMock,
        mock_eventbridge: MagicMock,
    ) -> None:
        mock_neptune.return_value.get_metric.return_value = None
        mock_eventbridge.return_value.put_events.return_value = {}

        handler(_make_event(body=_valid_body()), None)

        mock_eventbridge.return_value.put_events.assert_called_once()
        entry = mock_eventbridge.return_value.put_events.call_args[1]["Entries"][0]
        assert entry["Source"] == f"{EVENT_SOURCE_PREFIX}.metric-service"
        assert entry["DetailType"] == f"{EVENT_SOURCE_PREFIX}.metric.published"
        detail = json.loads(entry["Detail"])
        assert detail["action"] == "CREATE"
        assert detail["namespace"] == "test-ns"
        assert detail["metricName"] == "monthly_revenue"

    @patch("coa_metrics.api.create_metric._get_eventbridge")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_opensearch_failure_does_not_fail_request(
        self,
        mock_neptune: MagicMock,
        mock_opensearch: MagicMock,
        mock_eventbridge: MagicMock,
    ) -> None:
        mock_neptune.return_value.get_metric.return_value = None
        mock_opensearch.return_value.embed_metric.side_effect = RuntimeError("OSS down")
        mock_eventbridge.return_value.put_events.return_value = {}

        resp = handler(_make_event(body=_valid_body()), None)

        # Should still succeed — OpenSearch is best-effort
        assert resp["statusCode"] == 201

    @patch("coa_metrics.api.create_metric._get_eventbridge")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_neptune_failure_returns_500(
        self,
        mock_neptune: MagicMock,
        mock_opensearch: MagicMock,
        mock_eventbridge: MagicMock,
    ) -> None:
        mock_neptune.return_value.get_metric.return_value = None
        mock_neptune.return_value.create_metric.side_effect = RuntimeError("Neptune down")

        resp = handler(_make_event(body=_valid_body()), None)

        assert resp["statusCode"] == 500


# ── Soft validation tests ───────────────────────────────────────────────


class TestValidateSoft:
    def _request(self) -> CreateMetricRequestContent:
        return CreateMetricRequestContent.model_validate(_valid_body())

    def test_validator_import_failure_returns_empty(self) -> None:
        # Simulate sqlglot/validator import failure — must not block creation.
        with patch.dict(sys.modules, {"coa_metrics.validator": None}):
            assert _validate_soft(self._request(), "test-ns") == []

    @patch("coa_metrics.validator.validate_metric")
    @patch("coa_metrics.lookups.NeptuneOntologyLookup")
    @patch("coa_metrics.data_source_lookup_factory.build_data_source_lookup")
    def test_lookup_init_failure_falls_back_to_none(
        self,
        mock_build_lookup: MagicMock,
        mock_onto: MagicMock,
        mock_validate: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_build_lookup.side_effect = RuntimeError("DDB unavailable")
        mock_onto.side_effect = RuntimeError("Neptune unavailable")
        mock_validate.return_value = ValidationResult(valid=True, errors=[], warnings=[])

        result = _validate_soft(self._request(), "test-ns")

        assert result == []
        # Validation still ran, with both lookups degraded to None.
        kwargs = mock_validate.call_args.kwargs
        assert kwargs["data_sources_lookup"] is None
        assert kwargs["ontology_lookup"] is None

    @patch("coa_metrics.validator.validate_metric")
    def test_converts_errors_and_warnings(self, mock_validate: MagicMock) -> None:
        mock_validate.return_value = ValidationResult(
            valid=False,
            errors=[{"check": "sql_syntax", "message": "bad sql"}],
            warnings=[{"check": "table_reference", "message": "missing table"}],
        )
        result = _validate_soft(self._request(), "test-ns")

        by_field = {w["field"]: w for w in result}
        assert by_field["sql_syntax"]["severity"] == "ERROR"
        assert by_field["sql_syntax"]["message"] == "bad sql"
        assert by_field["table_reference"]["severity"] == "WARNING"

    @patch("coa_metrics.validator.validate_metric")
    def test_validation_exception_returns_empty(self, mock_validate: MagicMock) -> None:
        mock_validate.side_effect = RuntimeError("validator crashed")
        assert _validate_soft(self._request(), "test-ns") == []


# ── Ontology concept resolution tests ───────────────────────────────────


class TestOntologyConceptResolution:
    """Test that create_metric resolves concept names to URIs before writing."""

    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_resolves_concepts_before_neptune_write(
        self, mock_neptune_factory: MagicMock, mock_oss_factory: MagicMock
    ) -> None:
        mock_neptune = MagicMock()
        mock_neptune.get_metric.return_value = None  # no conflict
        mock_neptune.resolve_class_uris.return_value = [f"{GRAPH_BASE_URI}/namespace/test-ns/ontology/x/Policy"]
        mock_neptune_factory.return_value = mock_neptune

        mock_oss = MagicMock()
        mock_oss_factory.return_value = mock_oss

        body = _valid_body()
        body["ontologyConcepts"] = ["Policy"]
        event = _make_event(body)
        resp = handler(event, None)

        assert resp["statusCode"] == 201
        # Verify resolve_class_uris was called with the raw concept names
        mock_neptune.resolve_class_uris.assert_called_once_with("test-ns", ["Policy"])
        # Verify create_metric received the resolved URI
        create_call = mock_neptune.create_metric.call_args[0]
        metric_def = create_call[1]
        assert metric_def.ontology_concepts == [f"{GRAPH_BASE_URI}/namespace/test-ns/ontology/x/Policy"]

    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_resolution_failure_does_not_block_create(
        self, mock_neptune_factory: MagicMock, mock_oss_factory: MagicMock
    ) -> None:
        mock_neptune = MagicMock()
        mock_neptune.get_metric.return_value = None
        mock_neptune.resolve_class_uris.side_effect = Exception("Neptune timeout")
        mock_neptune_factory.return_value = mock_neptune

        mock_oss = MagicMock()
        mock_oss_factory.return_value = mock_oss

        body = _valid_body()
        body["ontologyConcepts"] = ["Policy"]
        event = _make_event(body)
        resp = handler(event, None)

        # Should still succeed — resolution failure is best-effort
        assert resp["statusCode"] == 201
        mock_neptune.create_metric.assert_called_once()


# ── SQL persistence validation (#617 / #1050) ──────────────────────────


class TestSqlPersistenceValidation:
    """#617/#1050: a non-SELECT fragment or unparseable SQL is a hard 400 at
    onboarding — the serve-time firewall enforces the same SELECT-only rule, so
    such a metric must not be persisted. (This supersedes #161, which had made
    fragments/parse errors soft warnings; only DML/DDL remains a separate 400.)"""

    def _publishing_neptune(self, mock_neptune_factory: MagicMock) -> MagicMock:
        mock_neptune = MagicMock()
        mock_neptune.get_metric.return_value = None  # no conflict
        mock_neptune_factory.return_value = mock_neptune
        return mock_neptune

    @staticmethod
    def _warning_messages(resp: dict) -> str:
        body = json.loads(resp["body"])
        return " ".join(w["message"] for w in body.get("warnings", []))

    # ── HARD: fragments and parse errors are rejected at onboarding ──────

    @pytest.mark.parametrize("fragment", ["COUNT(*)", "SUM(orders.total_amount)", "AVG(price)"])
    @patch("coa_metrics.api.create_metric._validate_soft")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_fragment_expression_returns_400(
        self,
        mock_neptune_factory: MagicMock,
        mock_oss: MagicMock,
        mock_validate_soft: MagicMock,
        fragment: str,
    ) -> None:
        """A fragment is rejected (#617/#1050): the serve firewall only executes
        full SELECTs, so a persisted fragment fails at query time. Must 400 and
        not persist."""
        mock_neptune = self._publishing_neptune(mock_neptune_factory)
        body = _valid_body()
        body["expression"] = {"dialects": [{"dialect": "TRINO", "expression": fragment}]}
        resp = handler(_make_event(body=body), None)

        assert resp["statusCode"] == 400
        payload = json.loads(resp["body"])
        assert "full SELECT statement" in payload["message"]
        mock_neptune.create_metric.assert_not_called()
        mock_validate_soft.assert_not_called()

    @pytest.mark.parametrize(
        "invalid_sql",
        [
            "SELECT *",
            "SELECT dblink_exec('conn', 'DELETE FROM orders')",
        ],
    )
    @patch("coa_metrics.api.create_metric._validate_soft")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_non_executable_or_forbidden_select_returns_400(
        self,
        mock_neptune_factory: MagicMock,
        mock_oss: MagicMock,
        mock_validate_soft: MagicMock,
        invalid_sql: str,
    ) -> None:
        mock_neptune = self._publishing_neptune(mock_neptune_factory)
        body = _valid_body()
        body["expression"] = {"dialects": [{"dialect": "POSTGRESQL", "expression": invalid_sql}]}

        resp = handler(_make_event(body=body), None)

        assert resp["statusCode"] == 400
        mock_neptune.create_metric.assert_not_called()
        mock_validate_soft.assert_not_called()

    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_unparseable_expression_returns_400(self, mock_neptune_factory: MagicMock, mock_oss: MagicMock) -> None:
        """A syntactically-broken expression is an ERROR check → hard 400
        (#617/#1050), not a soft warning."""
        mock_neptune = self._publishing_neptune(mock_neptune_factory)
        body = _valid_body()
        body["expression"] = {"dialects": [{"dialect": "TRINO", "expression": "SELECT FROM WHERE ((("}]}
        resp = handler(_make_event(body=body), None)

        assert resp["statusCode"] == 400
        payload = json.loads(resp["body"])
        assert "could not be parsed" in payload["message"]
        assert "TRINO" in payload["message"]
        mock_neptune.create_metric.assert_not_called()

    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_full_select_publishes_without_sql_warning(
        self, mock_neptune_factory: MagicMock, mock_oss: MagicMock
    ) -> None:
        self._publishing_neptune(mock_neptune_factory)
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 201
        assert "full SELECT statement" not in self._warning_messages(resp)

    # ── HARD: DML/DDL still 400 (security boundary) ──────────────────────

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; DROP TABLE x",
            "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
            "DROP TABLE orders",
            "DELETE FROM orders WHERE (((",  # unparseable DML — regex catches it
        ],
    )
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_data_modifying_expression_returns_400(self, mock_neptune: MagicMock, sql: str) -> None:
        body = _valid_body()
        body["expression"] = {"dialects": [{"dialect": "POSTGRESQL", "expression": sql}]}
        resp = handler(_make_event(body=body), None)

        assert resp["statusCode"] == 400
        assert "data-modifying" in json.loads(resp["body"])["message"]
        mock_neptune.return_value.create_metric.assert_not_called()


# ── APPROVED-source enforcement ──────────────────────────────────


class TestSourceApprovalEnforcement:
    """The API must not bypass the UI's APPROVED/COMPLETED source filter."""

    @patch("coa_metrics.api.create_metric.check_source_approved")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_non_approved_source_returns_400(
        self, mock_neptune: MagicMock, mock_check: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        mock_check.return_value = (
            "Data source 'ds-abc123' has status 'PENDING_REVIEW' — "
            "metrics can only reference APPROVED or COMPLETED sources"
        )
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 400
        assert "PENDING_REVIEW" in json.loads(resp["body"])["message"]
        mock_neptune.return_value.create_metric.assert_not_called()

    @patch("coa_metrics.api.create_metric.check_source_approved")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_validation_unavailable_returns_503(
        self, mock_neptune: MagicMock, mock_check: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from coa_metrics.source_status import SourceValidationUnavailableError

        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        mock_check.side_effect = SourceValidationUnavailableError("table read failed")
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 503
        mock_neptune.return_value.create_metric.assert_not_called()

    @patch("coa_metrics.api.create_metric.check_source_approved")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_approved_source_passes(
        self, mock_neptune_factory: MagicMock, mock_oss: MagicMock, mock_check: MagicMock
    ) -> None:
        mock_neptune = MagicMock()
        mock_neptune.get_metric.return_value = None
        mock_neptune_factory.return_value = mock_neptune
        mock_check.return_value = None
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 201
        mock_neptune.create_metric.assert_called_once()


# ── sourceTable enforcement (#161) ──────────────────────────────────────


class TestSourceTableEnforcement:
    """#161: sourceTable is a hard block, but only on provable absence."""

    @patch("coa_metrics.api.create_metric.check_source_table_exists")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_absent_source_table_returns_400(self, mock_neptune: MagicMock, mock_check: MagicMock) -> None:
        mock_check.return_value = "Source table 'orders' not found in data source 'ds-abc123'"
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 400
        assert "not found" in json.loads(resp["body"])["message"]
        mock_neptune.return_value.create_metric.assert_not_called()

    @patch("coa_metrics.api.create_metric.check_source_table_exists")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_table_validation_unavailable_returns_503(self, mock_neptune: MagicMock, mock_check: MagicMock) -> None:
        from coa_metrics.source_status import SourceValidationUnavailableError

        mock_check.side_effect = SourceValidationUnavailableError("catalog unreachable")
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 503
        mock_neptune.return_value.create_metric.assert_not_called()

    @patch("coa_metrics.api.create_metric.check_source_table_exists")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_unprovable_absence_still_publishes(
        self, mock_neptune_factory: MagicMock, mock_oss: MagicMock, mock_check: MagicMock
    ) -> None:
        """Returning None (can't prove absence) must publish — the soft
        table_reference warning is the only signal in that case."""
        mock_neptune = MagicMock()
        mock_neptune.get_metric.return_value = None
        mock_neptune_factory.return_value = mock_neptune
        mock_check.return_value = None
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 201
        mock_neptune.create_metric.assert_called_once()

    @patch("coa_metrics.api.create_metric._validate_soft")
    @patch("coa_metrics.api.create_metric.check_source_table_exists")
    @patch("coa_metrics.api.create_metric._get_opensearch")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_error_severity_table_reference_still_publishes(
        self,
        mock_neptune_factory: MagicMock,
        mock_oss: MagicMock,
        mock_check: MagicMock,
        mock_validate_soft: MagicMock,
    ) -> None:
        """!1133 (Kun's review): the validator can tag table_reference ERROR on
        provable absence, but that metadata check has its own dedicated gate
        (check_source_table_exists, using a different lookup). When the dedicated
        gate says publish (None), an ERROR-severity table_reference finding must
        NOT independently 400 — onboarding blocks only on sql_syntax/sql_shape
        (BLOCKING_CHECKS). Regression guard for the old severity-based filter."""
        mock_neptune = MagicMock()
        mock_neptune.get_metric.return_value = None
        mock_neptune_factory.return_value = mock_neptune
        mock_check.return_value = None  # dedicated gate can't prove absence → publish
        mock_validate_soft.return_value = [
            {"field": "table_reference", "message": "Source table 'orders' not found", "severity": "ERROR"}
        ]
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 201, resp
        mock_neptune.create_metric.assert_called_once()
        # the ERROR-severity metadata finding is still surfaced (non-blocking)
        body = json.loads(resp["body"])
        assert any(w["field"] == "table_reference" for w in body.get("warnings", []))

    @patch("coa_metrics.api.create_metric._validate_soft")
    @patch("coa_metrics.api.create_metric.check_source_table_exists")
    @patch("coa_metrics.api.create_metric._get_neptune")
    def test_sql_shape_error_still_blocks(
        self,
        mock_neptune_factory: MagicMock,
        mock_check: MagicMock,
        mock_validate_soft: MagicMock,
    ) -> None:
        """Complement to the above: a sql_shape ERROR (a BLOCKING_CHECK) must
        still 400 and not persist, so the narrower filter did not disable the
        onboarding gate #617/#1050 added."""
        mock_neptune = MagicMock()
        mock_neptune.get_metric.return_value = None
        mock_neptune_factory.return_value = mock_neptune
        mock_check.return_value = None
        mock_validate_soft.return_value = [
            {"field": "sql_shape", "message": "expression must be a full SELECT statement", "severity": "ERROR"}
        ]
        resp = handler(_make_event(body=_valid_body()), None)
        assert resp["statusCode"] == 400, resp
        mock_neptune.create_metric.assert_not_called()
