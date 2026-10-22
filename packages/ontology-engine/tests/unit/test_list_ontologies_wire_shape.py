# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Wire-shape contract tests for ``GET /ontologies``.

These exist because the endpoint previously returned a bare snake_case array
while Smithy declared a ``{ontologies: [...]}`` envelope with camelCase members.
Nothing failed: the generated TS client silently deserialised
``out.ontologies`` to ``undefined``, the web app's ``?? []`` turned that into an
empty list, and every consumer degraded quietly. The frontend tests passed
because they mocked the hook with the *contract* shape rather than the wire.

So these assert the serialised JSON, not the Python model — the only level at
which that class of drift is visible.
"""

from unittest.mock import patch

import pytest
from coa_ontology.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

# A registry row as ``list_ontologies_registry`` yields it: snake_case, and
# carrying bookkeeping fields the Smithy contract does not declare.
SAMPLE_ROW = {
    "ontology_id": "http://ex.org/o#",
    "uri": "http://ex.org/o#",
    "title": "Claims Ontology",
    "ontology_type": "induced",
    "parse_status": "ok",
    "status": "deleting",
    "delete_error": "Neptune DROP timed out",
    "class_count": 5,
    "property_count": 3,
    "axiom_count": 1,
    "embedding_count": 10,
    "graph_uri": "https://wb.local/ns/o",
    "created_at": "2026-07-01T00:00:00Z",
    "updated_at": "2026-07-02T00:00:00Z",
    "domain_tags": ["insurance"],
    "format": "turtle",
    # Registry-only fields with no place in the declared contract.
    "imports": ["http://other/onto"],
    "source": "induction",
    "embedding_index": "idx-1",
}


@pytest.fixture
def _registry_rows():
    with patch(
        "coa_ontology.catalog.routers.ontologies.dynamo_store.list_ontologies_registry_page",
        return_value=([SAMPLE_ROW], None),
    ) as p:
        yield p


@pytest.mark.unit
class TestListOntologiesWireShape:
    def test_response_is_an_envelope_not_a_bare_array(self, _registry_rows):
        """The Smithy contract declares ``{ontologies: [...]}``."""
        body = client.get("/ontologies/", params={"namespace": "ns"}).json()
        assert isinstance(body, dict), "a bare array is what broke the generated client"
        assert "ontologies" in body
        assert len(body["ontologies"]) == 1

    def test_members_are_camel_case_on_the_wire(self, _registry_rows):
        """Serialised with ``by_alias`` so the generated client can read it."""
        row = client.get("/ontologies/", params={"namespace": "ns"}).json()["ontologies"][0]
        assert row["ontologyId"] == "http://ex.org/o#"
        assert row["ontologyType"] == "induced"
        assert row["classCount"] == 5
        assert row["embeddingCount"] == 10
        assert row["graphUri"] == "https://wb.local/ns/o"
        assert row["createdAt"] == "2026-07-01T00:00:00Z"
        assert row["domainTags"] == ["insurance"]
        # No snake_case leakage.
        assert "ontology_id" not in row
        assert "class_count" not in row

    def test_parse_status_and_delete_error_survive(self, _registry_rows):
        """The two fields the UI needs but the contract used to omit.

        ``parseStatus`` drives the Ingesting/Ready/Failed indicator; without it a
        failed ingest silently reads as Ready. ``deleteError`` is what lets the UI
        tell a stuck teardown from one still running.
        """
        row = client.get("/ontologies/", params={"namespace": "ns"}).json()["ontologies"][0]
        assert row["parseStatus"] == "ok"
        assert row["status"] == "deleting"
        assert row["deleteError"] == "Neptune DROP timed out"

    def test_registry_only_fields_are_dropped(self, _registry_rows):
        """Undeclared bookkeeping must not leak into a declared response shape."""
        row = client.get("/ontologies/", params={"namespace": "ns"}).json()["ontologies"][0]
        for undeclared in ("imports", "source", "embedding_index", "embeddingIndex"):
            assert undeclared not in row

    def test_empty_registry_still_returns_the_envelope(self):
        """An empty namespace yields ``{"ontologies": []}``, not ``[]`` or ``null``."""
        with patch(
            "coa_ontology.catalog.routers.ontologies.dynamo_store.list_ontologies_registry_page",
            return_value=([], None),
        ):
            body = client.get("/ontologies/", params={"namespace": "ns"}).json()
        assert body == {"ontologies": []}
