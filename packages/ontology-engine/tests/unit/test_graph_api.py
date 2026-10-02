# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the /graph/* endpoints (vertex lookup + search).

All tests mock the graph store so no live Neptune/SPARQL is needed.
Run with: uv run pytest tests/unit/test_graph_api.py -v
"""

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import URN_PREFIX
from coa_ontology.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

OWL_CLASS = "http://www.w3.org/2002/07/owl#Class"
OWL_OBJECT_PROPERTY = "http://www.w3.org/2002/07/owl#ObjectProperty"
OWL_DATATYPE_PROPERTY = "http://www.w3.org/2002/07/owl#DatatypeProperty"
RDFS_CLASS = "http://www.w3.org/2000/01/rdf-schema#Class"

SAMPLE_CLASS_VERTEX = {
    "uri": "https://example.org/onto#Claim",
    "graph_uris": ["https://ontology-workbench.local/default/encoded-onto"],
    "types": [OWL_CLASS],
    "labels": ["Claim"],
    "comments": ["An insurance claim."],
    "edges": [
        {
            "predicate": "http://www.w3.org/2000/01/rdf-schema#subClassOf",
            "predicate_label": "subClassOf",
            "neighbor": {
                "uri": "https://schema.org/Event",
                "label": "Event",
                "kind": "class",
            },
        },
        {
            "predicate": "https://ontology-workbench.local/vocab#definedBy",
            "predicate_label": "definedBy",
            "neighbor": {
                "uri": "https://example.org/onto#",
                "label": "My Ontology",
                "kind": "ontology",
            },
        },
    ],
}

SAMPLE_OBJ_PROP_VERTEX = {
    "uri": "https://example.org/onto#claim_policyId",
    "graph_uris": ["https://ontology-workbench.local/default/encoded-onto"],
    "types": ["http://www.w3.org/1999/02/22-rdf-syntax-ns#Property", OWL_OBJECT_PROPERTY],
    "labels": ["Policy_ID"],
    "comments": [],
    "edges": [
        {
            "predicate": "http://www.w3.org/2000/01/rdf-schema#domain",
            "predicate_label": "domain",
            "neighbor": {
                "uri": "https://example.org/onto#Claim",
                "label": "Claim",
                "kind": "class",
            },
        },
        {
            "predicate": "http://www.w3.org/2000/01/rdf-schema#range",
            "predicate_label": "range",
            "neighbor": {
                "uri": "https://example.org/onto#Policy",
                "label": "Policy",
                "kind": "class",
            },
        },
    ],
}

SAMPLE_DATA_PROP_VERTEX = {
    "uri": "https://example.org/onto#claim_openDate",
    "graph_uris": ["https://ontology-workbench.local/default/encoded-onto"],
    "types": ["http://www.w3.org/1999/02/22-rdf-syntax-ns#Property", OWL_DATATYPE_PROPERTY],
    "labels": ["Open_Date"],
    "comments": [],
    "edges": [
        {
            "predicate": "http://www.w3.org/2000/01/rdf-schema#domain",
            "predicate_label": "domain",
            "neighbor": {
                "uri": "https://example.org/onto#Claim",
                "label": "Claim",
                "kind": "class",
            },
        },
        {
            "predicate": "http://www.w3.org/2000/01/rdf-schema#range",
            "predicate_label": "range",
            "neighbor": {
                "uri": "http://www.w3.org/2001/XMLSchema#string",
                "label": "string",
                "kind": "unknown",
            },
        },
    ],
}

SAMPLE_SEARCH_RESULTS = [
    {"uri": "https://example.org/onto#Claim", "label": "Claim", "kind": "class", "graph_uris": ["g1"]},
    {"uri": "https://example.org/onto#ClaimAmount", "label": "Claim_Amount", "kind": "class", "graph_uris": ["g1"]},
    {
        "uri": "https://example.org/onto#claim_claimId",
        "label": "Claim_ID",
        "kind": "object-property",
        "graph_uris": ["g1"],
    },
]


def _mock_graph_store(get_vertex_return=None, search_return=None):
    """Create a mock graph store with configurable return values."""
    mock = MagicMock()
    mock.get_vertex.return_value = get_vertex_return
    mock.search_entities.return_value = search_return or []
    # count_entities must return an int (Pydantic validates total_count); the
    # search route calls it alongside search_entities. Default to the number of
    # search hits so tests that only set search_return get a sensible total.
    mock.count_entities.return_value = len(search_return or [])
    return mock


@pytest.fixture(autouse=True)
def _patch_build_stores():
    """Patch build_stores globally so no real backend is needed."""
    mock_graph = _mock_graph_store()
    mock_vector = MagicMock()
    with patch(
        "coa_ontology.catalog.routers.graph.build_stores",
        return_value=(mock_graph, mock_vector),
    ) as p:
        # Expose the mock so tests can configure it
        p.mock_graph = mock_graph
        yield p


# ═══════════════════════════════════════════════════════════════════════
# GET /graph/class/{uri}
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestGetClass:
    def test_returns_class_vertex(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_CLASS_VERTEX
        resp = client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim", "namespace": "default"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["uri"] == "https://example.org/onto#Claim"
        assert body["kind"] == "class"
        assert body["labels"] == ["Claim"]
        assert body["comments"] == ["An insurance claim."]
        assert len(body["edges"]) == 2
        assert body["edges"][0]["predicate_label"] == "subClassOf"
        assert body["edges"][0]["neighbor"]["label"] == "Event"

    def test_is_mapped_defaults_false_when_absent(self, _patch_build_stores):
        """A store record without an is_mapped key (unmapped / legacy data)
        surfaces is_mapped=False through the API model — never omitted/None."""
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_CLASS_VERTEX
        resp = client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim", "namespace": "default"})
        assert resp.status_code == 200
        assert resp.json()["is_mapped"] is False

    def test_is_mapped_true_propagates(self, _patch_build_stores):
        """When the store reports a mapped class, the API surfaces is_mapped=True
        (the Tier-2 R2RML-answerability marker)."""
        _patch_build_stores.mock_graph.get_vertex.return_value = {**SAMPLE_CLASS_VERTEX, "is_mapped": True}
        resp = client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim", "namespace": "default"})
        assert resp.status_code == 200
        assert resp.json()["is_mapped"] is True

    def test_superseded_history_propagates(self, _patch_build_stores):
        """The store's `superseded` history buckets must reach the API model —
        the router maps fields explicitly, so a missing line here silently drops them
        (which is exactly what the live E2E caught)."""
        history = {"comments": ["AI: old text"], "altLabels": ["cust_master"]}
        _patch_build_stores.mock_graph.get_vertex.return_value = {**SAMPLE_CLASS_VERTEX, "superseded": history}
        resp = client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim", "namespace": "default"})
        assert resp.status_code == 200
        assert resp.json()["superseded"] == history

    def test_superseded_defaults_empty_when_absent(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_CLASS_VERTEX
        resp = client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim", "namespace": "default"})
        assert resp.json()["superseded"] == {}

    def test_404_when_vertex_not_found(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = None
        resp = client.get("/graph/class/", params={"uri": "https://example.org/onto#Missing", "namespace": "default"})
        assert resp.status_code == 404
        assert "No triples found" in resp.json()["detail"]

    def test_404_when_wrong_type(self, _patch_build_stores):
        # Vertex exists but is an ObjectProperty, not a Class
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_OBJ_PROP_VERTEX
        resp = client.get(
            "/graph/class/", params={"uri": "https://example.org/onto#claim_policyId", "namespace": "default"}
        )
        assert resp.status_code == 404
        assert "is not a class" in resp.json()["detail"]

    def test_accepts_rdfs_class(self, _patch_build_stores):
        vertex = {**SAMPLE_CLASS_VERTEX, "types": [RDFS_CLASS]}
        _patch_build_stores.mock_graph.get_vertex.return_value = vertex
        resp = client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim", "namespace": "default"})
        assert resp.status_code == 200

    def test_400_on_invalid_uri(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.side_effect = ValueError("URI scheme must be http/https/urn")
        resp = client.get("/graph/class/", params={"uri": "foobar", "namespace": "default"})
        assert resp.status_code == 400
        assert "Invalid vertex URI" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════
# GET /graph/object-property/{uri}
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestGetObjectProperty:
    def test_returns_object_property_vertex(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_OBJ_PROP_VERTEX
        resp = client.get(
            "/graph/object-property/",
            params={"uri": "https://example.org/onto#claim_policyId", "namespace": "default"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "object-property"
        assert body["labels"] == ["Policy_ID"]
        assert any(e["predicate_label"] == "domain" for e in body["edges"])
        assert any(e["predicate_label"] == "range" for e in body["edges"])

    def test_404_when_vertex_is_class(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_CLASS_VERTEX
        resp = client.get(
            "/graph/object-property/",
            params={"uri": "https://example.org/onto#Claim", "namespace": "default"},
        )
        assert resp.status_code == 404
        assert "is not a object property" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════
# GET /graph/datatype-property/{uri}
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestGetDatatypeProperty:
    def test_returns_datatype_property_vertex(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_DATA_PROP_VERTEX
        resp = client.get(
            "/graph/datatype-property/",
            params={"uri": "https://example.org/onto#claim_openDate", "namespace": "default"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "datatype-property"
        assert body["labels"] == ["Open_Date"]

    def test_404_when_vertex_is_object_property(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_OBJ_PROP_VERTEX
        resp = client.get(
            "/graph/datatype-property/",
            params={"uri": "https://example.org/onto#claim_policyId", "namespace": "default"},
        )
        assert resp.status_code == 404
        assert "is not a datatype property" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════
# GET /graph/search
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestSearchEntities:
    def test_returns_matching_hits(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = SAMPLE_SEARCH_RESULTS
        resp = client.get("/graph/search", params={"q": "Claim", "namespace": "default"})
        assert resp.status_code == 200
        body = resp.json()
        # Response is a {hits, total_count} envelope.
        assert len(body["hits"]) == 3
        assert body["hits"][0]["uri"] == "https://example.org/onto#Claim"
        assert body["hits"][0]["kind"] == "class"
        assert body["hits"][0]["label"] == "Claim"

    def test_empty_results(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        resp = client.get("/graph/search", params={"q": "NonExistent", "namespace": "default"})
        assert resp.status_code == 200
        assert resp.json() == {"hits": [], "total_count": 0}

    def test_total_count_reported_from_count_entities(self, _patch_build_stores):
        """total_count reflects count_entities (the full match count), which is
        independent of the paged hits actually returned."""
        _patch_build_stores.mock_graph.search_entities.return_value = [SAMPLE_SEARCH_RESULTS[0]]
        _patch_build_stores.mock_graph.count_entities.return_value = 137
        resp = client.get("/graph/search", params={"q": "Claim", "namespace": "default", "limit": "1"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["hits"]) == 1
        assert body["total_count"] == 137

    def test_offset_passed_to_store(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        resp = client.get("/graph/search", params={"q": "x", "namespace": "default", "offset": "40"})
        assert resp.status_code == 200
        call_kwargs = _patch_build_stores.mock_graph.search_entities.call_args[1]
        assert call_kwargs["offset"] == 40

    def test_offset_defaults_to_zero(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        resp = client.get("/graph/search", params={"q": "x", "namespace": "default"})
        assert resp.status_code == 200
        call_kwargs = _patch_build_stores.mock_graph.search_entities.call_args[1]
        assert call_kwargs["offset"] == 0

    def test_400_when_offset_negative(self, _patch_build_stores):
        resp = client.get("/graph/search", params={"q": "x", "namespace": "default", "offset": "-1"})
        assert resp.status_code == 400
        assert "offset" in resp.json()["detail"]

    @pytest.mark.parametrize("bad_limit", ["0", "-1", "501", "1000000000"])
    def test_400_when_limit_out_of_range(self, _patch_build_stores, bad_limit):
        """``limit`` lands in a SPARQL LIMIT clause, so an unbounded value is an
        unbounded scan and a non-positive one is invalid SPARQL (a 500). Both are
        rejected at the edge with a 400 and never reach the store."""
        resp = client.get("/graph/search", params={"q": "x", "namespace": "default", "limit": bad_limit})
        assert resp.status_code == 400
        assert "limit" in resp.json()["detail"]
        _patch_build_stores.mock_graph.search_entities.assert_not_called()

    @pytest.mark.parametrize("ok_limit", ["1", "50", "100", "500"])
    def test_limit_accepted_at_range_bounds(self, _patch_build_stores, ok_limit):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        resp = client.get("/graph/search", params={"q": "x", "namespace": "default", "limit": ok_limit})
        assert resp.status_code == 200
        assert _patch_build_stores.mock_graph.search_entities.call_args[1]["limit"] == int(ok_limit)

    def test_kind_filter_passed_to_store(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = [SAMPLE_SEARCH_RESULTS[0]]
        resp = client.get("/graph/search", params={"q": "Claim", "namespace": "default", "kind": "class"})
        assert resp.status_code == 200
        call_kwargs = _patch_build_stores.mock_graph.search_entities.call_args[1]
        assert call_kwargs["kind"] == "class"

    def test_ontology_id_filter_passed_to_store(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        resp = client.get(
            "/graph/search",
            params={"q": "x", "namespace": "default", "ontology_id": "https://example.org/onto#"},
        )
        assert resp.status_code == 200
        call_kwargs = _patch_build_stores.mock_graph.search_entities.call_args[1]
        assert call_kwargs["ontology_id"] == "https://example.org/onto#"

    def test_limit_passed_to_store(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        resp = client.get("/graph/search", params={"q": "x", "namespace": "default", "limit": "10"})
        assert resp.status_code == 200
        call_kwargs = _patch_build_stores.mock_graph.search_entities.call_args[1]
        assert call_kwargs["limit"] == 10

    def test_501_when_backend_unsupported(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.side_effect = NotImplementedError("nope")
        resp = client.get("/graph/search", params={"q": "x", "namespace": "default"})
        assert resp.status_code == 501
        assert "does not support" in resp.json()["detail"]

    def test_exclude_ontology_id_passed_to_store(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        resp = client.get(
            "/graph/search",
            params={
                "q": "Claim",
                "namespace": "default",
                "kind": "class",
                "exclude_ontology_id": f"urn:{URN_PREFIX}:vocab#GovernedMetrics",
            },
        )
        assert resp.status_code == 200
        call_kwargs = _patch_build_stores.mock_graph.search_entities.call_args[1]
        assert call_kwargs["exclude_ontology_id"] == f"urn:{URN_PREFIX}:vocab#GovernedMetrics"

    def test_400_when_both_ontology_id_and_exclude(self, _patch_build_stores):
        resp = client.get(
            "/graph/search",
            params={
                "q": "Claim",
                "namespace": "default",
                "ontology_id": "urn:a",
                "exclude_ontology_id": "urn:b",
            },
        )
        assert resp.status_code == 400
        assert "Cannot use both" in resp.json()["detail"]

    def test_400_when_exclude_ontology_id_empty(self, _patch_build_stores):
        resp = client.get(
            "/graph/search",
            params={
                "q": "Claim",
                "namespace": "default",
                "exclude_ontology_id": "   ",
            },
        )
        assert resp.status_code == 400
        assert "cannot be empty" in resp.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════
# Cross-cutting: namespace scoping
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.unit
class TestNamespaceScoping:
    def test_namespace_passed_to_get_vertex(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_CLASS_VERTEX
        client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim", "namespace": "insurance"})
        call_kwargs = _patch_build_stores.mock_graph.get_vertex.call_args[1]
        assert call_kwargs["namespace"] == "insurance"

    def test_namespace_passed_to_search(self, _patch_build_stores):
        _patch_build_stores.mock_graph.search_entities.return_value = []
        client.get("/graph/search", params={"q": "x", "namespace": "insurance"})
        call_kwargs = _patch_build_stores.mock_graph.search_entities.call_args[1]
        assert call_kwargs["namespace"] == "insurance"

    def test_default_namespace_when_omitted(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_vertex.return_value = SAMPLE_CLASS_VERTEX
        client.get("/graph/class/", params={"uri": "https://example.org/onto#Claim"})
        call_kwargs = _patch_build_stores.mock_graph.get_vertex.call_args[1]
        assert call_kwargs["namespace"] == "default"


@pytest.mark.unit
class TestHelpers:
    """Cover the helper functions in neptune_db_graph that are importable."""

    def test_local_name_with_hash(self):
        from coa_ontology.stores.neptune_db_graph import _local_name

        assert _local_name("http://example.org/onto#Claim") == "Claim"

    def test_local_name_with_slash(self):
        from coa_ontology.stores.neptune_db_graph import _local_name

        assert _local_name("http://example.org/onto/Claim") == "Claim"

    def test_local_name_no_separator(self):
        from coa_ontology.stores.neptune_db_graph import _local_name

        assert _local_name("Claim") == "Claim"

    def test_kind_from_type_class(self):
        from coa_ontology.stores.neptune_db_graph import _kind_from_type

        assert _kind_from_type("http://www.w3.org/2002/07/owl#Class") == "class"

    def test_kind_from_type_object_property(self):
        from coa_ontology.stores.neptune_db_graph import _kind_from_type

        assert _kind_from_type("http://www.w3.org/2002/07/owl#ObjectProperty") == "object-property"

    def test_kind_from_type_unknown(self):
        from coa_ontology.stores.neptune_db_graph import _kind_from_type

        assert _kind_from_type("http://example.org/Unknown") is None

    def test_kind_specificity(self):
        from coa_ontology.stores.neptune_db_graph import _kind_specificity

        assert _kind_specificity("class") > _kind_specificity("property")
        assert _kind_specificity("property") > _kind_specificity("unknown")

    def test_kind_to_sparql_types(self):
        from coa_ontology.stores.neptune_db_graph import _KIND_TO_SPARQL_TYPES

        assert "class" in _KIND_TO_SPARQL_TYPES
        assert len(_KIND_TO_SPARQL_TYPES["class"]) == 2


SAMPLE_OVERVIEW = {
    "ontology_id": "https://example.org/onto#",
    "graph_uri": "https://ontology-workbench.local/default/encoded-onto",
    "classes": [
        {"uri": "https://example.org/onto#Claim", "label": "Claim", "comment": "An insurance claim."},
        {"uri": "https://example.org/onto#Policy", "label": "Policy", "comment": None},
    ],
    "object_properties": [
        {
            "uri": "https://example.org/onto#hasPolicy",
            "label": "has policy",
            "domain": "https://example.org/onto#Claim",
            "range": "https://example.org/onto#Policy",
        }
    ],
    "datatype_properties": [
        {
            "uri": "https://example.org/onto#amount",
            "label": "amount",
            "domain": "https://example.org/onto#Claim",
            "range": "http://www.w3.org/2001/XMLSchema#decimal",
        }
    ],
}


@pytest.mark.unit
class TestOntologyOverview:
    def test_returns_overview(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_ontology_overview.return_value = SAMPLE_OVERVIEW
        resp = client.get(
            "/graph/ontology-overview",
            params={"ontology_id": "https://example.org/onto#", "namespace": "default"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["namespace"] == "default"
        assert len(body["classes"]) == 2
        assert len(body["objectProperties"]) == 1
        assert body["objectProperties"][0]["range"] == "https://example.org/onto#Policy"
        assert body["datatypeProperties"][0]["range"] == "http://www.w3.org/2001/XMLSchema#decimal"

    def test_501_when_backend_unsupported(self, _patch_build_stores):
        # Neptune Analytics returns None — overview not supported.
        _patch_build_stores.mock_graph.get_ontology_overview.return_value = None
        resp = client.get(
            "/graph/ontology-overview",
            params={"ontology_id": "https://example.org/onto#", "namespace": "default"},
        )
        assert resp.status_code == 501

    def test_empty_overview_is_200(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_ontology_overview.return_value = {
            "ontology_id": "https://example.org/onto#",
            "graph_uri": "g",
            "classes": [],
            "object_properties": [],
            "datatype_properties": [],
        }
        resp = client.get(
            "/graph/ontology-overview",
            params={"ontology_id": "https://example.org/onto#", "namespace": "default"},
        )
        assert resp.status_code == 200
        assert resp.json()["classes"] == []


# ═══════════════════════════════════════════════════════════════════════
# GET /graph/schema
# ═══════════════════════════════════════════════════════════════════════

SAMPLE_NAMESPACE_SCHEMA = {
    "classes": [
        {
            "uri": "https://example.org/onto#Claim",
            "label": "Claim",
            "description": "An insurance claim.",
            "parentClass": "https://schema.org/Event",
            "properties": [
                {"uri": "https://example.org/onto#claim_openDate", "label": "Open_Date", "range": "xsd:string"}
            ],
        },
        {
            "uri": "https://example.org/onto#Policy",
            "label": "Policy",
            "description": None,
            "parentClass": None,
            "properties": [],
        },
    ],
    "namespace": "default",
    "ontologyVersion": "",
}


@pytest.mark.unit
class TestNamespaceSchema:
    def test_returns_schema(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.return_value = SAMPLE_NAMESPACE_SCHEMA
        resp = client.get("/graph/schema", params={"namespace": "default"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["namespace"] == "default"
        assert len(body["classes"]) == 2
        assert body["classes"][0]["uri"] == "https://example.org/onto#Claim"

    def test_include_properties_defaults_true(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.return_value = SAMPLE_NAMESPACE_SCHEMA
        client.get("/graph/schema", params={"namespace": "default"})
        call_kwargs = _patch_build_stores.mock_graph.get_namespace_schema.call_args[1]
        assert call_kwargs["include_properties"] is True

    def test_include_properties_false(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.return_value = SAMPLE_NAMESPACE_SCHEMA
        client.get("/graph/schema", params={"namespace": "default", "include_properties": "false"})
        call_kwargs = _patch_build_stores.mock_graph.get_namespace_schema.call_args[1]
        assert call_kwargs["include_properties"] is False

    def test_max_results_capped_at_500(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.return_value = SAMPLE_NAMESPACE_SCHEMA
        client.get("/graph/schema", params={"namespace": "default", "max_results": "1000"})
        call_kwargs = _patch_build_stores.mock_graph.get_namespace_schema.call_args[1]
        assert call_kwargs["max_results"] == 500

    def test_max_results_below_cap_passed_through(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.return_value = SAMPLE_NAMESPACE_SCHEMA
        client.get("/graph/schema", params={"namespace": "default", "max_results": "50"})
        call_kwargs = _patch_build_stores.mock_graph.get_namespace_schema.call_args[1]
        assert call_kwargs["max_results"] == 50

    def test_501_when_backend_unsupported(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.side_effect = NotImplementedError("not supported")
        resp = client.get("/graph/schema", params={"namespace": "default"})
        assert resp.status_code == 501
        assert "does not support" in resp.json()["detail"]

    def test_500_on_unexpected_error(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.side_effect = RuntimeError("boom")
        resp = client.get("/graph/schema", params={"namespace": "default"})
        assert resp.status_code == 500
        assert "Internal error" in resp.json()["detail"]

    def test_namespace_passed_to_store(self, _patch_build_stores):
        _patch_build_stores.mock_graph.get_namespace_schema.return_value = SAMPLE_NAMESPACE_SCHEMA
        client.get("/graph/schema", params={"namespace": "insurance"})
        call_args = _patch_build_stores.mock_graph.get_namespace_schema.call_args
        assert call_args[0][0] == "insurance"
