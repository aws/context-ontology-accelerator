# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.datazone_forms."""

from __future__ import annotations

import json

import pytest
from coa_common.datazone_forms import (
    ASSET_TYPE_NAME,
    FORM_TYPE_NAME,
    _parse_json_list,
    build_forms_input,
    deserialize_form,
    serialize_form,
)
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    ForeignKey,
    PrimaryKey,
    Table,
    TechnicalMetadata,
)

pytestmark = pytest.mark.unit


def _rich_table() -> Table:
    return Table(
        name="orders",
        database="sales",
        data_source_id="ds-1",
        namespace_id="ns-1",
        database_description="sales schema",
        technical_metadata=TechnicalMetadata(
            column_count=2,
            partition_keys=["dt"],
            format="parquet",
            location="s3://bucket/orders",
        ),
        business_metadata=BusinessMetadata(
            description="Customer orders",
            synonyms=["purchases"],
            glossary_terms=["Order"],
            tags=["sales"],
            enrichment_source="AI_GENERATED",
            review_status="APPROVED",
        ),
        primary_key=PrimaryKey(columns=["id"], source="DETERMINISTIC", confidence=0.9),
        foreign_keys=[
            ForeignKey(
                column="cust_id",
                target_table="customers",
                target_column="id",
                source="AI_INFERRED",
                confidence=0.7,
            )
        ],
        columns=[
            Column(name="id", data_type="int", nullable=False),
            Column(
                name="cust_id",
                data_type="int",
                business_metadata=BusinessMetadata(description="customer ref"),
                distinct_values=["1", "2"],
            ),
        ],
    )


class TestSerializeForm:
    def test_keys_match_smithy_structure(self):
        form = serialize_form(_rich_table())
        assert form["tableName"] == "orders"
        assert form["databaseName"] == "sales"
        assert form["dataSourceId"] == "ds-1"
        assert form["namespaceId"] == "ns-1"
        assert form["columnCount"] == 2

    def test_complex_fields_json_serialized(self):
        form = serialize_form(_rich_table())
        assert json.loads(form["partitionKeys"]) == ["dt"]
        assert json.loads(form["synonyms"]) == ["purchases"]
        assert json.loads(form["primaryKeyColumns"]) == ["id"]
        assert json.loads(form["foreignKeys"])[0]["target_table"] == "customers"
        assert len(json.loads(form["columns"])) == 2

    def test_column_count_falls_back_to_len_columns(self):
        table = _rich_table()
        table.technical_metadata.column_count = 0
        form = serialize_form(table)
        assert form["columnCount"] == 2

    def test_no_primary_key_emits_defaults(self):
        table = Table(name="t", database="d", primary_key=None)  # type: ignore[arg-type]
        form = serialize_form(table)
        assert form["primaryKeyColumns"] == "[]"
        assert form["primaryKeySource"] == ""
        assert form["primaryKeyConfidence"] == 0.0


class TestBuildFormsInput:
    def test_builds_forms_output_list(self):
        forms = build_forms_input(_rich_table())
        assert len(forms) == 1
        entry = forms[0]
        assert entry["formName"] == FORM_TYPE_NAME
        assert entry["typeIdentifier"] == FORM_TYPE_NAME
        content = json.loads(entry["content"])
        assert content["tableName"] == "orders"


class TestDeserializeForm:
    def test_roundtrip_preserves_core_fields(self):
        table = _rich_table()
        restored = deserialize_form(serialize_form(table))
        assert restored.name == "orders"
        assert restored.database == "sales"
        assert restored.namespace_id == "ns-1"
        assert restored.business_metadata.description == "Customer orders"
        assert restored.business_metadata.synonyms == ["purchases"]
        assert restored.primary_key.columns == ["id"]
        assert restored.primary_key.confidence == 0.9
        assert restored.foreign_keys[0].target_table == "customers"
        assert restored.technical_metadata.format == "parquet"

    def test_roundtrip_preserves_fk_governance_fields(self):
        # #1088: inferred relationships carry review_status, target_datasource_id
        # (cross-source), and provenance through serialize -> deserialize.
        table = Table(
            name="orders",
            database="sales",
            foreign_keys=[
                ForeignKey(
                    column="cust_id",
                    target_table="customers",
                    target_column="id",
                    source="AI_INFERRED",
                    confidence=0.7,
                    review_status="PENDING_REVIEW",
                    target_datasource_id="ds-2",
                    provenance="column-name match: cust_id -> customers.id",
                )
            ],
        )
        fk = deserialize_form(serialize_form(table)).foreign_keys[0]
        assert fk.review_status == "PENDING_REVIEW"
        assert fk.target_datasource_id == "ds-2"
        assert fk.provenance == "column-name match: cust_id -> customers.id"

    def test_legacy_fk_without_governance_fields_defaults_empty(self):
        # An FK persisted before these fields existed has none of the keys; it
        # must deserialize to "" (grandfathered), not raise.
        payload = {"foreignKeys": json.dumps([{"column": "c", "target_table": "t", "source": "AI_INFERRED"}])}
        fk = deserialize_form(payload).foreign_keys[0]
        assert fk.review_status == ""
        assert fk.target_datasource_id == ""
        assert fk.provenance == ""

    def test_columns_deserialized_with_business_metadata(self):
        restored = deserialize_form(serialize_form(_rich_table()))
        cust = next(c for c in restored.columns if c.name == "cust_id")
        assert cust.business_metadata.description == "customer ref"
        assert cust.distinct_values == ["1", "2"]

    def test_data_source_id_override_takes_precedence(self):
        payload = serialize_form(_rich_table())
        restored = deserialize_form(payload, data_source_id="override-ds")
        assert restored.data_source_id == "override-ds"

    def test_empty_payload_produces_empty_table(self):
        table = deserialize_form({})
        assert table.name == ""
        assert table.columns == []
        assert table.foreign_keys == []

    def test_skips_columns_without_name(self):
        payload = {"columns": json.dumps([{"data_type": "int"}, {"name": "ok", "data_type": "int"}])}
        table = deserialize_form(payload)
        assert [c.name for c in table.columns] == ["ok"]

    def test_unknown_business_metadata_keys_filtered(self):
        payload = {
            "columns": json.dumps(
                [{"name": "c", "data_type": "int", "business_metadata": {"description": "d", "bogus": "x"}}]
            )
        }
        table = deserialize_form(payload)
        assert table.columns[0].business_metadata.description == "d"


class TestParseJsonList:
    def test_returns_list_unchanged(self):
        assert _parse_json_list(["a", "b"]) == ["a", "b"]

    def test_empty_string_returns_empty_list(self):
        assert _parse_json_list("") == []

    def test_valid_json_list(self):
        assert _parse_json_list('["x", "y"]') == ["x", "y"]

    def test_non_list_json_returns_empty(self):
        assert _parse_json_list('{"a": 1}') == []

    def test_invalid_json_returns_empty(self):
        assert _parse_json_list("{not json") == []


def test_asset_type_name_constant():
    assert ASSET_TYPE_NAME == "CoaRelationalTable"
