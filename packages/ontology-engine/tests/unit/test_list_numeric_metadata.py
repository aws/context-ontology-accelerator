# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""List routes return DynamoDB numbers as JSON numbers, not strings.

boto3 reads every DynamoDB number back as ``Decimal``. ListProposals and
ListInducedDatasources declare a return annotation, so FastAPI serializes them
through Pydantic's JSON mode, which writes a ``Decimal`` as a string
(``"tables_processed": "2"``). The Smithy ``ProposalMetadata`` types these counts
``Integer``, GetProposal (no annotation) already returns numbers, and the web
console's Scope column only reads numbers. These tests seed items with
``Decimal`` values, as real DynamoDB reads produce them, and check the wire
format through the real app.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from coa_ontology import dynamo_store as ds
from coa_ontology import induce_catalog
from coa_ontology.main import app
from coa_ontology.pagination import json_native_numbers, page_body
from fastapi.testclient import TestClient
from pydantic import BaseModel

from .test_dynamo_store import _FakeDynamoTable

pytestmark = pytest.mark.unit

_METADATA = {
    "label": "orders",
    "tables_processed": Decimal("2"),
    "columns_processed": Decimal("6"),
    "novel_classes_created": Decimal("1"),
    "grounding_rate": Decimal("0.5"),
    "datasource_ids": ["ds-1"],
    "per_table": [{"table": "orders", "columns": Decimal("6")}],
}


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def table(monkeypatch):
    table = _FakeDynamoTable()
    table.put_item(
        Item={
            "PK": "ns#PROPOSAL#p1",
            "SK": "META",
            "proposal_id": "p1",
            "job_id": "job-p1",
            "namespace": "ns",
            "ontology_id": "https://example.com/onto",
            "proposal_type": "induction",
            "source_type": "STRUCTURED",
            "status": "pending",
            "created_at": "2026-10-01T00:00:00+00:00",
            "metadata": _METADATA,
        }
    )
    monkeypatch.setattr(ds, "_get_table", lambda: table)
    return table


class TestJsonNativeNumbers:
    def test_whole_decimal_becomes_int(self):
        out = json_native_numbers(Decimal("7"))
        assert out == 7 and type(out) is int

    def test_fractional_decimal_becomes_float(self):
        out = json_native_numbers(Decimal("0.25"))
        assert out == 0.25 and type(out) is float

    def test_nested_containers_are_walked(self):
        out = json_native_numbers({"a": [Decimal("1"), {"b": Decimal("2.5")}], "c": (Decimal("3"),)})
        assert out == {"a": [1, {"b": 2.5}], "c": [3]}

    def test_page_body_converts_items(self):
        body = page_body("proposals", [{"metadata": {"tables_processed": Decimal("2")}}], None, scope="proposals:ns")
        assert body == {"proposals": [{"metadata": {"tables_processed": 2}}]}
        assert type(body["proposals"][0]["metadata"]["tables_processed"]) is int

    def test_non_decimal_values_are_unchanged(self):
        value = {"s": "2", "b": True, "n": None, "i": 4, "f": 1.5}
        assert json_native_numbers(value) == value

    def test_page_body_passes_pydantic_models_through(self):
        # ListOntologies hands page_body Pydantic models and serializes them by
        # alias; the helper must return the same objects, not a dump of them.
        class _Record(BaseModel):
            name: str
            count: Decimal

        record = _Record(name="onto", count=Decimal("2"))
        body = page_body("ontologies", [record], None, scope="ontologies:ns")
        assert body["ontologies"][0] is record


class TestListProposalsNumbers:
    def test_metadata_counts_are_json_numbers(self, client, table):
        resp = client.get("/ontology/proposals/?namespace=ns")
        assert resp.status_code == 200
        meta = resp.json()["proposals"][0]["metadata"]
        assert meta["tables_processed"] == 2 and type(meta["tables_processed"]) is int
        assert meta["columns_processed"] == 6 and type(meta["columns_processed"]) is int
        assert meta["novel_classes_created"] == 1 and type(meta["novel_classes_created"]) is int
        assert meta["grounding_rate"] == 0.5 and type(meta["grounding_rate"]) is float
        assert meta["per_table"] == [{"table": "orders", "columns": 6}]

    def test_list_metadata_matches_get_proposal(self, client, table):
        listed = client.get("/ontology/proposals/?namespace=ns").json()["proposals"][0]["metadata"]
        with (
            patch.object(ds, "presign_proposal_artifact", return_value=None),
            patch.object(ds, "presign_proposal_matches", return_value=None),
            patch.object(ds, "presign_proposal_constraints", return_value=None),
        ):
            detail = client.get("/ontology/proposals/p1?namespace=ns")
        assert detail.status_code == 200
        detail_meta = detail.json()["metadata"]
        for key in ("tables_processed", "columns_processed", "novel_classes_created", "grounding_rate"):
            assert listed[key] == detail_meta[key]
            assert type(listed[key]) is type(detail_meta[key])

    def test_response_shape_is_unchanged(self, client, table):
        body = client.get("/ontology/proposals/?namespace=ns").json()
        assert set(body) == {"proposals"}
        assert body["proposals"][0]["proposal_id"] == "p1"
        assert body["proposals"][0]["metadata"]["datasource_ids"] == ["ds-1"]


class TestListInducedDatasourcesNumbers:
    def test_counts_are_json_numbers(self, client):
        accepted = [{"proposal_id": "p1", "ontology_id": "urn:o", "metadata": dict(_METADATA)}]
        with patch.object(induce_catalog.dynamo_store, "list_proposals", return_value=accepted):
            resp = client.get("/induce/datasources/induced?namespace=ns")
        assert resp.status_code == 200
        entry = resp.json()["datasources"][0]
        assert entry["datasourceId"] == "ds-1"
        for key, expected in (("tables_processed", 2), ("columns_processed", 6), ("novel_classes_created", 1)):
            assert entry[key] == expected and type(entry[key]) is int
