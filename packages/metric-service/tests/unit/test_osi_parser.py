# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for OSI v1.0 YAML parser and serializer."""

from __future__ import annotations

from pathlib import Path

import pytest
from coa_metrics.osi_parser import (
    MAX_SQL_EXPRESSION_LENGTH,
    OsiAiContext,
    OsiCustomExtension,
    OsiDataset,
    OsiDialectExpression,
    OsiDocument,
    OsiMetric,
    _parse_ai_context,
    internal_dialect_to_osi,
    osi_dialect_to_internal,
    parse_osi_yaml,
    serialize_osi_yaml,
)

pytestmark = pytest.mark.unit


# ── Dialect Mapping Tests ───────────────────────────────────────────────


class TestDialectMapping:
    """Tests for OSI ↔ internal dialect mapping."""

    @pytest.mark.parametrize(
        "osi,internal",
        [
            ("ANSI_SQL", "POSTGRESQL"),
            ("ansi_sql", "POSTGRESQL"),
            ("SNOWFLAKE", "SNOWFLAKE"),
            ("Snowflake", "SNOWFLAKE"),
            ("DATABRICKS", "DATABRICKS"),
            # #140: a real dialect named directly is legitimate input and passes
            # through case-normalized, not collapsed to POSTGRESQL or lowercased.
            ("REDSHIFT", "REDSHIFT"),
            ("redshift", "REDSHIFT"),
            ("TRINO", "TRINO"),
            ("MySQL", "MYSQL"),
            ("POSTGRESQL", "POSTGRESQL"),
        ],
    )
    def test_osi_to_internal(self, osi: str, internal: str) -> None:
        assert osi_dialect_to_internal(osi) == internal

    def test_osi_to_internal_passthrough_is_valid_enum(self) -> None:
        # A passed-through dialect must be a real SqlDialect value.
        from coa_metrics.constants import VALID_SQL_DIALECTS

        assert osi_dialect_to_internal("REDSHIFT") in VALID_SQL_DIALECTS

    def test_osi_to_internal_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown OSI dialect"):
            osi_dialect_to_internal("ORACLE")

    @pytest.mark.parametrize(
        "internal,osi",
        [
            ("postgresql", "ANSI_SQL"),
            ("trino", "ANSI_SQL"),
            ("redshift", "ANSI_SQL"),
            ("snowflake", "SNOWFLAKE"),
            ("databricks", "DATABRICKS"),
        ],
    )
    def test_internal_to_osi(self, internal: str, osi: str) -> None:
        assert internal_dialect_to_osi(internal) == osi

    def test_internal_to_osi_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="No OSI mapping"):
            internal_dialect_to_osi("oracle")


# ── Parser Tests ────────────────────────────────────────────────────────

VALID_OSI_YAML = """\
osi_spec_version: "1.0"
datasets:
  - name: orders_db
    data_source_id: ds-abc123
    description: "Production orders database"
    synonyms:
      - order_system
      - sales_db
metrics:
  - name: monthly_revenue
    description: "Total revenue from all orders in a calendar month"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SUM(orders.total_amount)"
        - dialect: SNOWFLAKE
          expression: "SUM(orders.total_amount)"
    ai_context:
      synonyms:
        - "total sales"
        - "revenue"
      instructions: "Use with order_date."
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-abc123
          source_table: orders
          unit: currency
          return_type: "xsd:decimal"
          time_dimension: month
          ontology_concepts:
            - "ind:Order"
            - "ind:Customer"
"""

MINIMAL_OSI_YAML = """\
osi_spec_version: "1.0"
metrics:
  - name: simple_count
    description: "Count of rows"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "COUNT(*)"
"""


class TestParseOsiYaml:
    """Tests for parse_osi_yaml function."""

    def test_parse_valid_full_document(self) -> None:
        result = parse_osi_yaml(VALID_OSI_YAML)
        assert result.success
        assert result.document is not None

        doc = result.document
        assert doc.osi_spec_version == "1.0"

        # Datasets
        assert len(doc.datasets) == 1
        ds = doc.datasets[0]
        assert ds.name == "orders_db"
        assert ds.data_source_id == "ds-abc123"
        assert ds.description == "Production orders database"
        assert ds.synonyms == ["order_system", "sales_db"]

        # Metrics
        assert len(doc.metrics) == 1
        m = doc.metrics[0]
        assert m.name == "monthly_revenue"
        assert m.description == "Total revenue from all orders in a calendar month"
        assert len(m.expression) == 2
        assert m.expression[0].dialect == "ANSI_SQL"
        assert m.expression[0].expression == "SUM(orders.total_amount)"
        assert m.expression[1].dialect == "SNOWFLAKE"
        assert "total sales" in m.ai_context.synonyms

        # Custom extensions
        assert m.custom_extensions is not None
        ext = m.custom_extensions
        assert ext.data_source_id == "ds-abc123"
        assert ext.source_table == "orders"
        assert ext.unit == "currency"
        assert ext.return_type == "xsd:decimal"
        assert ext.time_dimension == "month"
        assert ext.ontology_concepts == ["ind:Order", "ind:Customer"]

    def test_parse_minimal_document(self) -> None:
        result = parse_osi_yaml(MINIMAL_OSI_YAML)
        assert result.success
        assert result.document is not None
        assert len(result.document.metrics) == 1
        assert result.document.metrics[0].name == "simple_count"
        assert result.document.metrics[0].custom_extensions is None

    def test_parse_invalid_yaml(self) -> None:
        result = parse_osi_yaml("{{invalid: yaml: [")
        assert not result.success
        assert len(result.errors) == 1
        assert "Invalid YAML" in result.errors[0].message

    def test_parse_not_a_mapping(self) -> None:
        result = parse_osi_yaml("- just a list")
        assert not result.success
        assert "must be a YAML mapping" in result.errors[0].message

    def test_parse_missing_spec_version(self) -> None:
        yaml_content = """\
metrics:
  - name: test
    description: "test"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "COUNT(*)"
"""
        result = parse_osi_yaml(yaml_content)
        assert not result.success
        assert any("osi_spec_version" in e.message for e in result.errors)

    def test_parse_wrong_spec_version(self) -> None:
        yaml_content = """\
osi_spec_version: "2.0"
metrics:
  - name: test
    description: "test"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "COUNT(*)"
"""
        result = parse_osi_yaml(yaml_content)
        assert not result.success
        assert any("Unsupported OSI spec version" in e.message for e in result.errors)

    def test_parse_missing_metric_name(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
metrics:
  - description: "no name"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "COUNT(*)"
"""
        result = parse_osi_yaml(yaml_content)
        assert not result.success
        assert any("'name' is required" in e.message for e in result.errors)

    def test_parse_missing_expression(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
metrics:
  - name: test
    description: "test"
"""
        result = parse_osi_yaml(yaml_content)
        assert not result.success
        assert any("dialect expression is required" in e.message for e in result.errors)

    def test_parse_rejects_expression_above_smithy_limit(self) -> None:
        expression = "SELECT 1 FROM orders WHERE " + ("x" * MAX_SQL_EXPRESSION_LENGTH)
        yaml_content = f"""\
osi_spec_version: "1.0"
metrics:
  - name: oversized
    description: "oversized expression"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "{expression}"
"""

        result = parse_osi_yaml(yaml_content)

        assert not result.success
        assert any(
            error.path == "$.metrics[0].expression.dialects[0].expression"
            and f"at most {MAX_SQL_EXPRESSION_LENGTH} characters" in error.message
            for error in result.errors
        )

    def test_parse_empty_metrics_list(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
metrics: []
"""
        result = parse_osi_yaml(yaml_content)
        assert not result.success
        assert any("At least one metric" in e.message for e in result.errors)

    def test_parse_no_metrics_key(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
datasets:
  - name: test_ds
"""
        result = parse_osi_yaml(yaml_content)
        assert not result.success
        assert any("At least one metric" in e.message for e in result.errors)

    def test_parse_dataset_missing_name(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
datasets:
  - description: "no name dataset"
metrics:
  - name: test
    description: "test"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "COUNT(*)"
"""
        result = parse_osi_yaml(yaml_content)
        assert not result.success
        assert any("Dataset 'name' is required" in e.message for e in result.errors)

    def test_parse_multiple_metrics(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
metrics:
  - name: metric_a
    description: "First metric"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SUM(a)"
  - name: metric_b
    description: "Second metric"
    expression:
      dialects:
        - dialect: SNOWFLAKE
          expression: "AVG(b)"
"""
        result = parse_osi_yaml(yaml_content)
        assert result.success
        assert len(result.document.metrics) == 2
        assert result.document.metrics[0].name == "metric_a"
        assert result.document.metrics[1].name == "metric_b"
        assert result.document.metrics[1].expression[0].dialect == "SNOWFLAKE"

    def test_parse_ignores_non_coa_extensions(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
metrics:
  - name: test
    description: "test"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "COUNT(*)"
    custom_extensions:
      - vendor_name: OTHER_VENDOR
        data:
          some_field: value
"""
        result = parse_osi_yaml(yaml_content)
        assert result.success
        assert result.document.metrics[0].custom_extensions is None


# ── Serializer Tests ────────────────────────────────────────────────────


class TestSerializeOsiYaml:
    """Tests for serialize_osi_yaml function."""

    def test_serialize_minimal(self) -> None:
        doc = OsiDocument(
            metrics=[
                OsiMetric(
                    name="simple_count",
                    description="Count of rows",
                    expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="COUNT(*)")],
                )
            ]
        )
        yaml_str = serialize_osi_yaml(doc)
        assert "osi_spec_version: '1.0'" in yaml_str
        assert "simple_count" in yaml_str
        assert "COUNT(*)" in yaml_str

    def test_serialize_with_datasets(self) -> None:
        doc = OsiDocument(
            datasets=[
                OsiDataset(
                    name="orders_db",
                    data_source_id="ds-123",
                    description="Orders",
                    synonyms=["sales"],
                )
            ],
            metrics=[
                OsiMetric(
                    name="revenue",
                    description="Total revenue",
                    expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SUM(amount)")],
                )
            ],
        )
        yaml_str = serialize_osi_yaml(doc)
        assert "orders_db" in yaml_str
        assert "ds-123" in yaml_str
        assert "sales" in yaml_str

    def test_serialize_with_custom_extensions(self) -> None:
        doc = OsiDocument(
            metrics=[
                OsiMetric(
                    name="revenue",
                    description="Revenue",
                    expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SUM(x)")],
                    custom_extensions=OsiCustomExtension(
                        data_source_id="ds-abc",
                        source_table="orders",
                        unit="currency",
                        ontology_concepts=["ind:Order"],
                    ),
                )
            ],
        )
        yaml_str = serialize_osi_yaml(doc)
        assert "vendor_name: COA" in yaml_str
        assert "ds-abc" in yaml_str
        assert "orders" in yaml_str
        assert "ind:Order" in yaml_str

    def test_roundtrip(self) -> None:
        """Parse → serialize → parse should produce equivalent documents."""
        result1 = parse_osi_yaml(VALID_OSI_YAML)
        assert result1.success

        yaml_str = serialize_osi_yaml(result1.document)
        result2 = parse_osi_yaml(yaml_str)
        assert result2.success

        doc1 = result1.document
        doc2 = result2.document

        assert doc1.osi_spec_version == doc2.osi_spec_version
        assert len(doc1.metrics) == len(doc2.metrics)
        assert doc1.metrics[0].name == doc2.metrics[0].name
        assert doc1.metrics[0].expression[0].expression == doc2.metrics[0].expression[0].expression
        assert len(doc1.datasets) == len(doc2.datasets)
        assert doc1.datasets[0].name == doc2.datasets[0].name


# ── AI Context Parsing Tests ────────────────────────────────────────────


class TestParseAiContext:
    """Tests for _parse_ai_context — structured OSI ai_context parsing."""

    def test_dict_with_all_fields(self) -> None:
        raw = {
            "synonyms": ["total sales", "revenue"],
            "instructions": "Use for revenue questions. Do not use for forecasting.",
            "examples": ["Q1 revenue", "monthly totals"],
        }
        result = _parse_ai_context(raw)
        assert result is not None
        assert result.synonyms == ["total sales", "revenue"]
        assert result.instructions == "Use for revenue questions. Do not use for forecasting."
        assert result.examples == ["Q1 revenue", "monthly totals"]

    def test_dict_with_only_synonyms(self) -> None:
        raw = {"synonyms": ["gross sales", "income"]}
        result = _parse_ai_context(raw)
        assert result is not None
        assert result.synonyms == ["gross sales", "income"]
        assert result.instructions == ""
        assert result.examples == []

    def test_dict_with_only_instructions(self) -> None:
        raw = {"instructions": "Use this metric carefully. It includes refunds."}
        result = _parse_ai_context(raw)
        assert result is not None
        assert result.instructions == "Use this metric carefully. It includes refunds."

    def test_legacy_string_stored_as_instructions(self) -> None:
        result = _parse_ai_context("Use this for revenue questions")
        assert result is not None
        assert result.instructions == "Use this for revenue questions"
        assert result.synonyms == []

    def test_none_returns_none(self) -> None:
        assert _parse_ai_context(None) is None

    def test_empty_dict_returns_none(self) -> None:
        assert _parse_ai_context({}) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_ai_context("") is None


# ── Dataset Source Field Tests ──────────────────────────────────────────


class TestDatasetSourceField:
    """Tests for OsiDataset source field per OSI spec."""

    def test_parse_dataset_with_source(self) -> None:
        yaml_content = """\
osi_spec_version: "1.0"
datasets:
  - name: store_sales
    source: tpcds.public.store_sales
    description: Fact table
metrics:
  - name: total
    description: Total
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "COUNT(*)"
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-123
          source_table: tpcds.public.store_sales
"""
        result = parse_osi_yaml(yaml_content)
        assert result.document is not None
        assert len(result.document.datasets) == 1
        ds = result.document.datasets[0]
        assert ds.name == "store_sales"
        assert ds.source == "tpcds.public.store_sales"

    def test_serialize_dataset_includes_source(self) -> None:
        doc = OsiDocument(
            datasets=[OsiDataset(name="orders", source="public.orders", data_source_id="ds-123")],
            metrics=[
                OsiMetric(
                    name="count",
                    description="Count",
                    expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="COUNT(*)")],
                )
            ],
        )
        yaml_str = serialize_osi_yaml(doc)
        assert "source: public.orders" in yaml_str
        assert "name: orders" in yaml_str

    def test_serialize_ai_context_as_structured_object(self) -> None:
        doc = OsiDocument(
            datasets=[],
            metrics=[
                OsiMetric(
                    name="revenue",
                    description="Total revenue",
                    expression=[OsiDialectExpression(dialect="ANSI_SQL", expression="SUM(amount)")],
                    ai_context=OsiAiContext(
                        synonyms=["total sales"],
                        instructions="Use for revenue. Do not use for forecasting.",
                    ),
                )
            ],
        )
        yaml_str = serialize_osi_yaml(doc)
        assert "ai_context:" in yaml_str
        assert "synonyms:" in yaml_str
        assert "- total sales" in yaml_str
        assert "instructions: Use for revenue. Do not use for forecasting." in yaml_str


# ── Vendor Extension Shape ──────────────────────────────────────────────

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


class TestShippedExamplesCarryCustomExtensions:
    """The shipped example YAMLs must use the spec-defined ``custom_extensions`` shape.

    They once carried COA metadata under ``x_coa:``, which nothing reads. Importing
    them parsed cleanly but dropped unit, return_type, time_dimension and
    ontology_concepts for every metric — and with no ontology_concepts, Check 6 had
    nothing to validate and raised nothing. These tests pin the samples to the
    shape the parser (and the exporter) actually speak.
    """

    @pytest.mark.parametrize("example", sorted(p.name for p in _EXAMPLES_DIR.glob("*.yaml")))
    def test_every_metric_has_populated_extension(self, example: str) -> None:
        result = parse_osi_yaml((_EXAMPLES_DIR / example).read_text())

        assert result.success, [f"{e.path}: {e.message}" for e in result.errors]
        assert result.warnings == [], [f"{w.path}: {w.message}" for w in result.warnings]
        assert result.document is not None
        assert result.document.metrics, f"{example} defines no metrics"

        for metric in result.document.metrics:
            ext = metric.custom_extensions
            assert ext is not None, f"{example}: metric '{metric.name}' has no COA custom_extensions"
            assert ext.data_source_id, f"{example}: metric '{metric.name}' lacks data_source_id"
            assert ext.source_table, f"{example}: metric '{metric.name}' lacks source_table"

    def test_examples_dir_is_not_empty(self) -> None:
        # Guard against the parametrized test above silently passing with zero cases.
        assert list(_EXAMPLES_DIR.glob("*.yaml"))

    def test_sample_import_keeps_ontology_binding(self) -> None:
        """The headline sample must round-trip the fields the x_coa bug dropped."""
        result = parse_osi_yaml((_EXAMPLES_DIR / "sample-osi-import.yaml").read_text())
        assert result.success
        assert result.document is not None
        by_name = {m.name: m for m in result.document.metrics}

        revenue = by_name["total_revenue"].custom_extensions
        assert revenue is not None
        assert revenue.unit == "USD"
        assert revenue.return_type == "decimal"
        assert revenue.time_dimension == "month"
        assert revenue.ontology_concepts == ["Revenue", "FinancialMetric"]


class TestVendorPrefixedKeysWarn:
    """``x_*`` keys are not OSI v1.0; the parser must say so instead of dropping them silently."""

    _LEGACY_X_COA = """\
osi_spec_version: "1.0"
metrics:
  - name: total_revenue
    description: "Revenue"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SUM(amount)"
    x_coa:
      data_source_id: ds-abc123
      source_table: orders
      unit: USD
      ontology_concepts:
        - Revenue
"""

    def test_x_coa_block_is_ignored_with_a_warning(self) -> None:
        result = parse_osi_yaml(self._LEGACY_X_COA)

        # Still a successful parse — warnings never block an import.
        assert result.success
        assert result.document is not None
        assert result.document.metrics[0].custom_extensions is None

        assert len(result.warnings) == 1
        warning = result.warnings[0]
        assert warning.path == "$.metrics[0].x_coa"
        assert "not part of OSI v1.0" in warning.message
        assert "custom_extensions" in warning.message
        assert "vendor_name: COA" in warning.message

    def test_any_x_prefixed_key_warns_case_insensitively(self) -> None:
        content = self._LEGACY_X_COA.replace("x_coa:", "X_Other_Vendor:")
        result = parse_osi_yaml(content)

        assert result.success
        assert [w.path for w in result.warnings] == ["$.metrics[0].X_Other_Vendor"]

    def test_spec_shape_produces_no_warnings(self) -> None:
        content = """\
osi_spec_version: "1.0"
metrics:
  - name: total_revenue
    description: "Revenue"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SUM(amount)"
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-abc123
          source_table: orders
"""
        result = parse_osi_yaml(content)
        assert result.success
        assert result.warnings == []

    def test_warnings_survive_alongside_errors(self) -> None:
        # A metric with an x_ key AND a missing description: the error wins (no
        # document), but the warning is still reported so both get fixed at once.
        content = self._LEGACY_X_COA.replace('    description: "Revenue"\n', "")
        result = parse_osi_yaml(content)

        assert not result.success
        assert any(e.path == "$.metrics[0].description" for e in result.errors)
        assert [w.path for w in result.warnings] == ["$.metrics[0].x_coa"]


class TestExplicitNullsAreTreatedAsAbsent:
    """YAML ``key: null`` must behave like a missing key — not crash, not become ``"None"``."""

    _NULLS_EVERYWHERE = """\
osi_spec_version: "1.0"
datasets:
  - name: orders
    source: null
    description: null
    synonyms: null
metrics:
  - name: total_revenue
    description: "Revenue"
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SUM(amount)"
    ai_context:
      synonyms: null
      instructions: null
      examples: null
    custom_extensions:
      - vendor_name: COA
        data:
          data_source_id: ds-abc123
          source_table: orders
          unit: null
          return_type: null
          time_dimension: null
          ontology_concepts: null
          defined_by: null
          effective_from: null
"""

    def test_null_lists_do_not_crash_and_null_strings_are_empty(self) -> None:
        result = parse_osi_yaml(self._NULLS_EVERYWHERE)

        assert result.success, [f"{e.path}: {e.message}" for e in result.errors]
        assert result.document is not None

        ds = result.document.datasets[0]
        assert ds.source == ""
        assert ds.description == ""
        assert ds.synonyms == []

        metric = result.document.metrics[0]
        assert metric.ai_context is not None
        assert metric.ai_context.synonyms == []
        assert metric.ai_context.instructions == ""
        assert metric.ai_context.examples == []

        ext = metric.custom_extensions
        assert ext is not None
        assert ext.data_source_id == "ds-abc123"
        assert ext.source_table == "orders"
        # Every nulled scalar is "" — never the string "None".
        assert (ext.unit, ext.return_type, ext.time_dimension, ext.defined_by, ext.effective_from) == ("",) * 5
        assert ext.ontology_concepts == []

    def test_scalar_where_list_expected_is_wrapped_not_iterated(self) -> None:
        content = self._NULLS_EVERYWHERE.replace("ontology_concepts: null", "ontology_concepts: Revenue")
        result = parse_osi_yaml(content)
        assert result.success
        assert result.document is not None
        ext = result.document.metrics[0].custom_extensions
        assert ext is not None
        assert ext.ontology_concepts == ["Revenue"]  # not ["R", "e", "v", ...]

    def test_null_entries_inside_a_list_are_dropped(self) -> None:
        content = self._NULLS_EVERYWHERE.replace(
            "ontology_concepts: null", "ontology_concepts:\n            - Revenue\n            - null\n            - ''"
        )
        result = parse_osi_yaml(content)
        assert result.success
        assert result.document is not None
        ext = result.document.metrics[0].custom_extensions
        assert ext is not None
        assert ext.ontology_concepts == ["Revenue"]
