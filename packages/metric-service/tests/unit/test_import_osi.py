# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for OSI import handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.api.import_osi import _osi_metric_to_definition, handler
from coa_metrics.osi_parser import (
    OsiAiContext,
    OsiCustomExtension,
    OsiDialectExpression,
    OsiDocument,
    OsiMetric,
)
from coa_metrics.source_status import PERMISSIVE_ENV

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _permissive_source_lookup(monkeypatch: pytest.MonkeyPatch):
    """Skip APPROVED-source enforcement (#564) in handler tests — mirrors
    test_create_metric. The dedicated enforcement tests below patch
    ``check_source_approved`` directly, which bypasses this env var."""
    monkeypatch.setenv(PERMISSIVE_ENV, "true")
    yield


# ── Fixtures ────────────────────────────────────────────────────────────

VALID_OSI_CONTENT = """\
osi_spec_version: "1.0"
datasets:
  - name: orders_db
    data_source_id: ds-abc123
metrics:
  - name: monthly_revenue
    description: "Total revenue"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SELECT SUM(total_amount) FROM orders"
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-abc123
          source_table: orders
          unit: currency
"""

MINIMAL_OSI_CONTENT = """\
osi_spec_version: "1.0"
metrics:
  - name: simple_count
    description: "Count of rows"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SELECT COUNT(*) FROM orders"
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-abc123
          source_table: events
"""


def _make_event(content: str, namespace: str = "test-ns") -> dict:
    return {
        "httpMethod": "POST",
        "pathParameters": {"namespaceId": namespace},
        "body": json.dumps({"content": content}),
        "requestContext": {"authorizer": {"email": "test@example.com"}},
    }


# ── Handler Tests ───────────────────────────────────────────────────────


class TestImportOsiHandler:
    """Tests for the import_osi handler function."""

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi._get_lookup")
    def test_successful_import_creates_metric(self, mock_lookup, mock_neptune, mock_opensearch) -> None:
        lookup = MagicMock()
        lookup.data_source_exists.return_value = True
        mock_lookup.return_value = lookup

        neptune = MagicMock()
        neptune.get_metric.return_value = None  # Metric doesn't exist yet
        mock_neptune.return_value = neptune

        opensearch = MagicMock()
        mock_opensearch.return_value = opensearch

        event = _make_event(VALID_OSI_CONTENT)
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["datasetsResolved"] == 1
        assert body["metricsCreated"] == 1
        assert body["metricsUpdated"] == 0

        neptune.create_metric.assert_called_once()

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi._get_lookup")
    def test_import_updates_existing_metric(self, mock_lookup, mock_neptune, mock_opensearch) -> None:
        lookup = MagicMock()
        lookup.data_source_exists.return_value = True
        mock_lookup.return_value = lookup

        neptune = MagicMock()
        neptune.get_metric.return_value = MagicMock()  # Metric exists
        mock_neptune.return_value = neptune

        opensearch = MagicMock()
        mock_opensearch.return_value = opensearch

        event = _make_event(MINIMAL_OSI_CONTENT)
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["metricsCreated"] == 0
        assert body["metricsUpdated"] == 1

        neptune.update_metric.assert_called_once()

    def test_invalid_json_body(self) -> None:
        event = {
            "httpMethod": "POST",
            "pathParameters": {"namespaceId": "test"},
            "body": "not json",
            "requestContext": {"authorizer": {}},
        }
        response = handler(event, None)
        assert response["statusCode"] == 400

    def test_missing_content_field(self) -> None:
        event = {
            "httpMethod": "POST",
            "pathParameters": {"namespaceId": "test"},
            "body": json.dumps({"other": "field"}),
            "requestContext": {"authorizer": {}},
        }
        response = handler(event, None)
        assert response["statusCode"] == 400
        assert "content" in json.loads(response["body"])["message"]

    def test_invalid_osi_yaml(self) -> None:
        event = _make_event("{{invalid yaml")
        response = handler(event, None)
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "parse errors" in body["message"]

    @patch("coa_metrics.api.import_osi._get_lookup")
    def test_unresolved_dataset_returns_400(self, mock_lookup) -> None:
        lookup = MagicMock()
        lookup.data_source_exists.return_value = False
        mock_lookup.return_value = lookup

        event = _make_event(VALID_OSI_CONTENT)
        response = handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "resolution failed" in body["message"]


# ── Conversion Tests ────────────────────────────────────────────────────


class TestOsiMetricToDefinition:
    """Tests for _osi_metric_to_definition helper."""

    def test_converts_ansi_sql_to_postgresql(self) -> None:
        osi_metric = OsiMetric(
            name="revenue",
            description="Total revenue",
            expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SELECT SUM(x) FROM t")],
            custom_extensions=OsiCustomExtension(data_source_id="ds-1", source_table="orders"),
        )
        doc = OsiDocument(metrics=[osi_metric])

        result = _osi_metric_to_definition(osi_metric, doc, "user@test.com")

        assert result.expression_dialects[0].dialect == "POSTGRESQL"
        assert result.data_source_id == "ds-1"
        assert result.source_table == "orders"
        assert result.defined_by == "user@test.com"

    def test_converts_snowflake_dialect(self) -> None:
        osi_metric = OsiMetric(
            name="count",
            description="Count",
            expression=[OsiDialectExpression(dialect="SNOWFLAKE", expression="SELECT COUNT(*) FROM t")],
            custom_extensions=OsiCustomExtension(data_source_id="ds-1", source_table="t"),
        )
        doc = OsiDocument(metrics=[osi_metric])

        result = _osi_metric_to_definition(osi_metric, doc, "user")

        assert result.expression_dialects[0].dialect == "SNOWFLAKE"

    def test_passes_through_real_dialect_redshift(self) -> None:
        # #140: a real dialect named directly is kept, not collapsed/lowercased.
        osi_metric = OsiMetric(
            name="rs",
            description="Redshift metric",
            expression=[OsiDialectExpression(dialect="REDSHIFT", expression="SELECT SUM(x) FROM t")],
            custom_extensions=OsiCustomExtension(data_source_id="ds-1", source_table="t"),
        )
        doc = OsiDocument(metrics=[osi_metric])

        result = _osi_metric_to_definition(osi_metric, doc, "user")

        assert result.expression_dialects[0].dialect == "REDSHIFT"

    def test_normalizes_lowercase_dialect(self) -> None:
        # #140: a mis-cased real dialect is normalized, never stored lowercase.
        osi_metric = OsiMetric(
            name="rs2",
            description="lower redshift",
            expression=[OsiDialectExpression(dialect="redshift", expression="SELECT 1 FROM t")],
            custom_extensions=OsiCustomExtension(data_source_id="ds-1", source_table="t"),
        )
        doc = OsiDocument(metrics=[osi_metric])

        result = _osi_metric_to_definition(osi_metric, doc, "user")

        assert result.expression_dialects[0].dialect == "REDSHIFT"

    def test_unknown_dialect_falls_back_lowercased(self) -> None:
        # An unknown dialect is still imported via the lenient try/except and used
        # as-is (lowercased). Real dialects (#140) never reach this path — they
        # pass through osi_dialect_to_internal — so this only covers genuinely
        # unknown values.
        osi_metric = OsiMetric(
            name="odd-dialect",
            description="unknown dialect",
            expression=[OsiDialectExpression(dialect="BigQuery", expression="SELECT 1 FROM t")],
            custom_extensions=OsiCustomExtension(data_source_id="ds-1", source_table="t"),
        )
        doc = OsiDocument(metrics=[osi_metric])

        result = _osi_metric_to_definition(osi_metric, doc, "user")

        assert result.expression_dialects[0].dialect == "bigquery"

    def test_raises_without_data_source_id(self) -> None:
        osi_metric = OsiMetric(
            name="bad",
            description="No source",
            expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SELECT 1")],
        )
        doc = OsiDocument(metrics=[osi_metric])

        with pytest.raises(ValueError, match="No data_source_id"):
            _osi_metric_to_definition(osi_metric, doc, "user")

    def test_maps_time_dimension(self) -> None:
        osi_metric = OsiMetric(
            name="monthly",
            description="Monthly",
            expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SELECT SUM(x) FROM t")],
            custom_extensions=OsiCustomExtension(data_source_id="ds-1", source_table="t", time_dimension="month"),
        )
        doc = OsiDocument(metrics=[osi_metric])

        result = _osi_metric_to_definition(osi_metric, doc, "user")

        assert result.default_time_grain == "MONTH"


# ── AI Context Tests ─────────────────────────────────────────────────────


class TestAiContextMapping:
    """Tests for structured ai_context mapping from OsiAiContext to MetricAiContext."""

    def test_structured_ai_context_maps_directly(self) -> None:
        osi_metric = OsiMetric(
            name="test_metric",
            description="Test",
            expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SELECT COUNT(*) FROM t")],
            ai_context=OsiAiContext(
                synonyms=["total sales", "revenue"],
                instructions="Use with order_date. Do not use for forecasting.",
                examples=["Q1 revenue", "monthly totals"],
            ),
            custom_extensions=OsiCustomExtension(data_source_id="ds-123", source_table="orders"),
        )
        doc = OsiDocument(datasets=[], metrics=[osi_metric])
        result = _osi_metric_to_definition(osi_metric, doc, "test@example.com")

        assert result.ai_context is not None
        assert result.ai_context.synonyms == ["total sales", "revenue"]
        assert result.ai_context.instructions == "Use with order_date. Do not use for forecasting."
        assert result.ai_context.examples == ["Q1 revenue", "monthly totals"]

    def test_none_ai_context_maps_to_none(self) -> None:
        osi_metric = OsiMetric(
            name="test_metric",
            description="Test",
            expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SELECT COUNT(*) FROM t")],
            ai_context=None,
            custom_extensions=OsiCustomExtension(data_source_id="ds-123", source_table="orders"),
        )
        doc = OsiDocument(datasets=[], metrics=[osi_metric])
        result = _osi_metric_to_definition(osi_metric, doc, "test@example.com")

        assert result.ai_context is None

    def test_ai_context_preserves_periods_in_instructions(self) -> None:
        """Regression test: periods in instructions must not cause data loss."""
        instructions = (
            "Use this metric when the user asks about revenue. Do not use for forecasting. Always filter by status."
        )
        osi_metric = OsiMetric(
            name="test_metric",
            description="Test",
            expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SELECT COUNT(*) FROM t")],
            ai_context=OsiAiContext(instructions=instructions),
            custom_extensions=OsiCustomExtension(data_source_id="ds-123", source_table="orders"),
        )
        doc = OsiDocument(datasets=[], metrics=[osi_metric])
        result = _osi_metric_to_definition(osi_metric, doc, "test@example.com")

        assert result.ai_context is not None
        assert result.ai_context.instructions == instructions


# ── SQL validation on import: soft/hard split (#161) ────────────────────


class TestImportSqlValidationSoftHardSplit:
    """#161: only DML/DDL is rejected per-metric at import time; a fragment
    imports successfully (its soft warning is surfaced by validation)."""

    def _doc_for(self, expression: str) -> tuple[OsiMetric, OsiDocument]:
        osi_metric = OsiMetric(
            name="m",
            description="Metric",
            expression=[OsiDialectExpression(dialect="ANSI_SQL", expression=expression)],
            custom_extensions=OsiCustomExtension(data_source_id="ds-1", source_table="t"),
        )
        return osi_metric, OsiDocument(metrics=[osi_metric])

    def test_fragment_expression_imports(self) -> None:
        """A fragment no longer aborts the import (was ValueError)."""
        osi_metric, doc = self._doc_for("COUNT(*)")
        result = _osi_metric_to_definition(osi_metric, doc, "user")
        assert result.expression_dialects[0].expression == "COUNT(*)"

    def test_unparseable_expression_imports(self) -> None:
        osi_metric, doc = self._doc_for("SELECT FROM WHERE (((")
        result = _osi_metric_to_definition(osi_metric, doc, "user")
        assert result.expression_dialects[0].expression == "SELECT FROM WHERE ((("

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; DROP TABLE x",
            "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
            "DROP TABLE orders",
        ],
    )
    def test_data_modifying_expression_raises(self, sql: str) -> None:
        osi_metric, doc = self._doc_for(sql)
        with pytest.raises(ValueError, match="data-modifying"):
            _osi_metric_to_definition(osi_metric, doc, "user")


# ── sourceTable enforcement on import (#161 / #564) ─────────────────────

OSI_WITH_BAD_SOURCE_TABLE = """\
osi_spec_version: "1.0"
metrics:
  - name: bad_table_metric
    description: "References a table the catalog does not know"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SELECT COUNT(*) FROM orders"
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-abc123
          source_table: no_such_table
"""

# No source_table in custom_extensions → import defaults it to the metric NAME.
OSI_DEFAULTED_SOURCE_TABLE = """\
osi_spec_version: "1.0"
metrics:
  - name: no_such_table
    description: "source_table defaults to the metric name"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SELECT COUNT(*) FROM orders"
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-abc123
"""


class TestImportSourceTableEnforcement:
    """check_source_table_exists is wired into create + update but was NOT wired
    into import — so an imported metric could carry an unchecked sourceTable,
    re-opening the #564 UI-bypass class. Import keeps its per-metric semantics
    (skip-with-warning), it does not 400 the whole batch.
    """

    @staticmethod
    def _warnings(response: dict) -> str:
        return " ".join(json.loads(response["body"]).get("warnings", []))

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi.check_source_table_exists")
    def test_absent_source_table_skips_metric_with_warning(self, mock_check, mock_neptune, mock_opensearch) -> None:
        mock_check.return_value = "Source table 'no_such_table' not found in data source 'ds-abc123'"
        neptune = MagicMock()
        neptune.get_metric.return_value = None
        mock_neptune.return_value = neptune
        mock_opensearch.return_value = MagicMock()

        response = handler(_make_event(OSI_WITH_BAD_SOURCE_TABLE), None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["metricsCreated"] == 0
        neptune.create_metric.assert_not_called()
        assert "no_such_table" in self._warnings(response)

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi.check_source_table_exists")
    def test_defaulted_source_table_is_checked(self, mock_check, mock_neptune, mock_opensearch) -> None:
        """The metric-NAME default must be checked too — that is the exact path
        an unchecked sourceTable slipped through."""
        mock_check.return_value = "Source table 'no_such_table' not found in data source 'ds-abc123'"
        neptune = MagicMock()
        neptune.get_metric.return_value = None
        mock_neptune.return_value = neptune
        mock_opensearch.return_value = MagicMock()

        response = handler(_make_event(OSI_DEFAULTED_SOURCE_TABLE), None)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["metricsCreated"] == 0
        neptune.create_metric.assert_not_called()
        mock_check.assert_called_once_with("test-ns", "ds-abc123", "no_such_table")

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi.check_source_table_exists")
    def test_present_source_table_imports(self, mock_check, mock_neptune, mock_opensearch) -> None:
        mock_check.return_value = None
        neptune = MagicMock()
        neptune.get_metric.return_value = None
        mock_neptune.return_value = neptune
        mock_opensearch.return_value = MagicMock()

        response = handler(_make_event(OSI_WITH_BAD_SOURCE_TABLE), None)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["metricsCreated"] == 1

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi.check_source_table_exists")
    def test_validation_unavailable_returns_503_not_500(self, mock_check, mock_neptune, mock_opensearch) -> None:
        """A SourceValidationUnavailableError must not escape as a 500."""
        from coa_metrics.source_status import SourceValidationUnavailableError

        mock_check.side_effect = SourceValidationUnavailableError("catalog down")
        neptune = MagicMock()
        neptune.get_metric.return_value = None
        mock_neptune.return_value = neptune
        mock_opensearch.return_value = MagicMock()

        response = handler(_make_event(OSI_WITH_BAD_SOURCE_TABLE), None)

        assert response["statusCode"] == 503
        neptune.create_metric.assert_not_called()


class TestImportSourceApprovalEnforcement:
    """check_source_approved is wired into create + update but was NOT wired into
    import — so a metric bound to a PENDING/REJECTED/FAILED source could be
    imported, the same #564 bypass class as the unchecked sourceTable above.
    Approval is checked BEFORE the table so an unapproved source is rejected
    regardless of whether its table happens to exist.
    """

    @staticmethod
    def _warnings(response: dict) -> str:
        return " ".join(json.loads(response["body"]).get("warnings", []))

    @pytest.mark.parametrize("status", ["PENDING", "REJECTED", "FAILED"])
    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi.check_source_table_exists")
    @patch("coa_metrics.api.import_osi.check_source_approved")
    def test_unapproved_source_skips_metric_with_warning(
        self, mock_approved, mock_table, mock_neptune, mock_opensearch, status: str
    ) -> None:
        mock_approved.return_value = (
            f"Data source 'ds-abc123' has status '{status}' — metrics can only reference APPROVED or COMPLETED sources"
        )
        mock_table.return_value = None
        neptune = MagicMock()
        neptune.get_metric.return_value = None
        mock_neptune.return_value = neptune
        mock_opensearch.return_value = MagicMock()

        response = handler(_make_event(OSI_WITH_BAD_SOURCE_TABLE), None)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["metricsCreated"] == 0
        neptune.create_metric.assert_not_called()
        assert status in self._warnings(response)
        # Approval short-circuits — an unapproved source is never table-checked.
        mock_table.assert_not_called()

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi.check_source_table_exists")
    @patch("coa_metrics.api.import_osi.check_source_approved")
    def test_approved_source_imports(self, mock_approved, mock_table, mock_neptune, mock_opensearch) -> None:
        mock_approved.return_value = None
        mock_table.return_value = None
        neptune = MagicMock()
        neptune.get_metric.return_value = None
        mock_neptune.return_value = neptune
        mock_opensearch.return_value = MagicMock()

        response = handler(_make_event(OSI_WITH_BAD_SOURCE_TABLE), None)

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["metricsCreated"] == 1
        mock_approved.assert_called_once_with("test-ns", "ds-abc123")

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi.check_source_approved")
    def test_approval_unavailable_returns_503_not_500(self, mock_approved, mock_neptune, mock_opensearch) -> None:
        """An unreadable sources table is a batch-level 503, not a 500."""
        from coa_metrics.source_status import SourceValidationUnavailableError

        mock_approved.side_effect = SourceValidationUnavailableError("sources table read failed")
        neptune = MagicMock()
        neptune.get_metric.return_value = None
        mock_neptune.return_value = neptune
        mock_opensearch.return_value = MagicMock()

        response = handler(_make_event(OSI_WITH_BAD_SOURCE_TABLE), None)

        assert response["statusCode"] == 503
        neptune.create_metric.assert_not_called()


# ── Fail-closed data source lookup (#564) ───────────────────────────────


class TestGetLookupFailClosed:
    """_get_lookup must not silently degrade to the permissive fallback."""

    def _clear_cache(self) -> None:
        from coa_metrics.api import import_osi

        import_osi._lookups.clear()

    def test_lookup_unavailable_raises_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_metrics.api.import_osi import _get_lookup
        from coa_metrics.source_status import (
            PERMISSIVE_ENV,
            SourceValidationUnavailableError,
        )

        self._clear_cache()
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with (
            patch("coa_metrics.api.import_osi.build_data_source_lookup", return_value=None),
            pytest.raises(SourceValidationUnavailableError),
        ):
            _get_lookup("ns-fail-closed")

    def test_lookup_init_error_raises_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_metrics.api.import_osi import _get_lookup
        from coa_metrics.source_status import (
            PERMISSIVE_ENV,
            SourceValidationUnavailableError,
        )

        self._clear_cache()
        monkeypatch.delenv(PERMISSIVE_ENV, raising=False)
        with (
            patch(
                "coa_metrics.api.import_osi.build_data_source_lookup",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(SourceValidationUnavailableError),
        ):
            _get_lookup("ns-fail-closed-2")

    def test_permissive_env_enables_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coa_metrics.api.import_osi import _get_lookup, _PermissiveLookup
        from coa_metrics.source_status import PERMISSIVE_ENV

        self._clear_cache()
        monkeypatch.setenv(PERMISSIVE_ENV, "true")
        with patch("coa_metrics.api.import_osi.build_data_source_lookup", return_value=None):
            lookup = _get_lookup("ns-permissive")
        assert isinstance(lookup, _PermissiveLookup)
        assert lookup.data_source_exists("anything") is True
