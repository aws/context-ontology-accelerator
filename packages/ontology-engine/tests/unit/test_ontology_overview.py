# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for NeptuneDBGraphStore.get_ontology_overview.

Mocks the SPARQL transport (`_sparql_query`) so no live Neptune is
needed. Verifies the two-query flow (classes + properties), per-subject
aggregation, and the object-vs-datatype property classification.

Run with: uv run pytest tests/unit/test_ontology_overview.py -v
"""

from unittest.mock import patch

import pytest
from coa_ontology.stores.neptune_db_graph import (
    OWL,
    RDF,
    NeptuneDBGraphStore,
    _ontology_graph_uri,
)

_XSD = "http://www.w3.org/2001/XMLSchema#"


def _rows(*bindings: dict) -> dict:
    return {"results": {"bindings": list(bindings)}}


def _uri(value: str) -> dict:
    return {"type": "uri", "value": value}


def _lit(value: str) -> dict:
    return {"type": "literal", "value": value}


CLASS_ROWS = _rows(
    {
        "c": _uri("https://ex.org/o#Claim"),
        "label": _lit("Claim"),
        "comment": _lit("A claim."),
        # Grounded to a previously-loaded class via coa:groundedTo + skos:exactMatch.
        "groundedTo": _uri("https://customer.example/onto#Claim"),
        "exact": _uri("https://customer.example/onto#Claim"),
        # Sub-class of a foundational class (not itself in this graph).
        "parent": _uri("https://customer.example/onto#Party"),
    },
    # Second label row for the same class — must collapse, first wins.
    {"c": _uri("https://ex.org/o#Claim"), "label": _lit("Claim alt")},
    {"c": _uri("https://ex.org/o#Policy")},
)

PROP_ROWS = _rows(
    # Object property: range is a class IRI.
    {
        "p": _uri("https://ex.org/o#hasPolicy"),
        "type": _uri(OWL + "ObjectProperty"),
        "label": _lit("has policy"),
        "domain": _uri("https://ex.org/o#Claim"),
        "range": _uri("https://ex.org/o#Policy"),
    },
    # Datatype property: explicit owl type.
    {
        "p": _uri("https://ex.org/o#amount"),
        "type": _uri(OWL + "DatatypeProperty"),
        "label": _lit("amount"),
        "domain": _uri("https://ex.org/o#Claim"),
        "range": _uri(_XSD + "decimal"),
    },
    # Generic rdf:Property with an xsd range → classified as datatype.
    {
        "p": _uri("https://ex.org/o#openDate"),
        "type": _uri(RDF + "Property"),
        "domain": _uri("https://ex.org/o#Claim"),
        "range": _uri(_XSD + "date"),
    },
    # Generic rdf:Property with a class range → classified as object (relationship).
    {
        "p": _uri("https://ex.org/o#relatedTo"),
        "type": _uri(RDF + "Property"),
        "range": _uri("https://ex.org/o#Policy"),
    },
)


@pytest.mark.unit
class TestGetOntologyOverview:
    def test_aggregates_and_classifies(self):
        store = NeptuneDBGraphStore(namespace="ns")
        with patch(
            "coa_ontology.stores.neptune_db_graph._sparql_query",
            side_effect=[CLASS_ROWS, PROP_ROWS],
        ):
            overview = store.get_ontology_overview("https://ex.org/o#", namespace="ns")

        assert overview is not None
        assert overview["ontology_id"] == "https://ex.org/o#"
        assert overview["graph_uri"] == _ontology_graph_uri("https://ex.org/o#", "ns")

        # Classes collapse per-URI; first label wins; missing comment → None.
        classes = {c["uri"]: c for c in overview["classes"]}
        assert set(classes) == {"https://ex.org/o#Claim", "https://ex.org/o#Policy"}
        assert classes["https://ex.org/o#Claim"]["label"] == "Claim"
        assert classes["https://ex.org/o#Claim"]["comment"] == "A claim."
        assert classes["https://ex.org/o#Policy"]["comment"] is None

        # Grounded class carries its target + match type; novel class does not.
        assert classes["https://ex.org/o#Claim"]["grounded_to"] == "https://customer.example/onto#Claim"
        assert classes["https://ex.org/o#Claim"]["match_type"] == "exact"
        assert classes["https://ex.org/o#Policy"]["grounded_to"] is None
        assert classes["https://ex.org/o#Policy"]["match_type"] is None

        # subClassOf parents are aggregated; a class with none gets [].
        assert classes["https://ex.org/o#Claim"]["sub_class_of"] == ["https://customer.example/onto#Party"]
        assert classes["https://ex.org/o#Policy"]["sub_class_of"] == []

        obj = {p["uri"] for p in overview["object_properties"]}
        dat = {p["uri"] for p in overview["datatype_properties"]}
        assert obj == {"https://ex.org/o#hasPolicy", "https://ex.org/o#relatedTo"}
        assert dat == {"https://ex.org/o#amount", "https://ex.org/o#openDate"}

    def test_empty_graph_returns_empty_lists(self):
        store = NeptuneDBGraphStore(namespace="ns")
        with patch(
            "coa_ontology.stores.neptune_db_graph._sparql_query",
            side_effect=[_rows(), _rows()],
        ):
            overview = store.get_ontology_overview("https://ex.org/o#", namespace="ns")
        assert overview is not None
        assert overview["classes"] == []
        assert overview["object_properties"] == []
        assert overview["datatype_properties"] == []

    def test_aggregates_multiple_subclass_parents_and_skips_bnode(self):
        # A class with two named super-classes (two rows) plus one blank-node
        # parent (an owl:Restriction cardinality axiom) — the bnode is dropped,
        # the two named IRIs are aggregated, de-duplicated, order-preserving.
        class_rows = _rows(
            {
                "c": _uri("https://ex.org/o#Order"),
                "label": _lit("Order"),
                "parent": _uri("https://ex.org/o#Document"),
            },
            {"c": _uri("https://ex.org/o#Order"), "parent": _uri("https://ex.org/o#Sellable")},
            # Duplicate parent row — must not appear twice.
            {"c": _uri("https://ex.org/o#Order"), "parent": _uri("https://ex.org/o#Document")},
            # Blank-node parent (owl:Restriction) — must be skipped.
            {"c": _uri("https://ex.org/o#Order"), "parent": {"type": "bnode", "value": "_:r0"}},
        )
        store = NeptuneDBGraphStore(namespace="ns")
        with patch(
            "coa_ontology.stores.neptune_db_graph._sparql_query",
            side_effect=[class_rows, _rows()],
        ):
            overview = store.get_ontology_overview("https://ex.org/o#", namespace="ns")
        order = overview["classes"][0]
        assert order["sub_class_of"] == ["https://ex.org/o#Document", "https://ex.org/o#Sellable"]

    def test_skips_blank_node_domain_and_range(self):
        store = NeptuneDBGraphStore(namespace="ns")
        prop_rows = _rows(
            {
                "p": _uri("https://ex.org/o#p"),
                "type": _uri(OWL + "ObjectProperty"),
                "domain": {"type": "bnode", "value": "_:b0"},
                "range": {"type": "bnode", "value": "_:b1"},
            }
        )
        with patch(
            "coa_ontology.stores.neptune_db_graph._sparql_query",
            side_effect=[_rows(), prop_rows],
        ):
            overview = store.get_ontology_overview("https://ex.org/o#", namespace="ns")
        prop = overview["object_properties"][0]
        assert prop["domain"] is None
        assert prop["range"] is None

    def test_annotation_property_excluded_from_both_buckets(self):
        # An ingested annotation property (e.g. the grounding
        # `matchConfidence` marker) carries BOTH owl:AnnotationProperty and a
        # generic rdf:Property stamp, with no rdfs:range. It must NOT be
        # classified as an object property (relationship) — it belongs in
        # neither the object nor the datatype bucket.
        prop_rows = _rows(
            {
                "p": _uri("https://ex.org/o#hasPolicy"),
                "type": _uri(OWL + "ObjectProperty"),
                "range": _uri("https://ex.org/o#Policy"),
            },
            # matchConfidence arrives as two rows (both types), no domain/range.
            {"p": _uri("https://ex.org/o#matchConfidence"), "type": _uri(OWL + "AnnotationProperty")},
            {"p": _uri("https://ex.org/o#matchConfidence"), "type": _uri(RDF + "Property")},
        )
        store = NeptuneDBGraphStore(namespace="ns")
        with patch(
            "coa_ontology.stores.neptune_db_graph._sparql_query",
            side_effect=[_rows(), prop_rows],
        ):
            overview = store.get_ontology_overview("https://ex.org/o#", namespace="ns")
        obj = {p["uri"] for p in overview["object_properties"]}
        dat = {p["uri"] for p in overview["datatype_properties"]}
        # Only the real relationship is counted; matchConfidence is dropped.
        assert obj == {"https://ex.org/o#hasPolicy"}
        assert "https://ex.org/o#matchConfidence" not in obj
        assert "https://ex.org/o#matchConfidence" not in dat

    def test_annotation_property_also_object_property_is_kept(self):
        # Guard the exclusion's boundary: a property explicitly declared an
        # owl:ObjectProperty is still a relationship even if it also carries an
        # owl:AnnotationProperty type — only annotation-ONLY props are dropped.
        prop_rows = _rows(
            {
                "p": _uri("https://ex.org/o#relatesTo"),
                "type": _uri(OWL + "ObjectProperty"),
                "range": _uri("https://ex.org/o#Policy"),
            },
            {"p": _uri("https://ex.org/o#relatesTo"), "type": _uri(OWL + "AnnotationProperty")},
        )
        store = NeptuneDBGraphStore(namespace="ns")
        with patch(
            "coa_ontology.stores.neptune_db_graph._sparql_query",
            side_effect=[_rows(), prop_rows],
        ):
            overview = store.get_ontology_overview("https://ex.org/o#", namespace="ns")
        obj = {p["uri"] for p in overview["object_properties"]}
        assert obj == {"https://ex.org/o#relatesTo"}


def _cls(uri: str, *parents: str) -> dict:
    row = {"c": _uri(uri)}
    if parents:
        # One row per parent (the real query emits a row per subClassOf); the
        # accumulator dedups. Use the first parent here and add extra rows below.
        row["parent"] = _uri(parents[0])
    return row


@pytest.mark.unit
class TestGetOntologyOverviewSample:
    """sample=True returns a bounded taxonomy seed: the largest-subtree roots +
    a capped BFS of their descendants, no properties, edges kept in-set."""

    def _run(self, class_rows: dict):
        store = NeptuneDBGraphStore(namespace="ns")
        # Sample mode issues exactly ONE SPARQL call (the class scan) — no
        # property query. side_effect has a single element to prove that.
        with patch(
            "coa_ontology.stores.neptune_db_graph._sparql_query",
            side_effect=[class_rows],
        ):
            return store.get_ontology_overview("https://ex.org/o#", namespace="ns", sample=True)

    def test_picks_largest_subtree_roots_and_omits_properties(self):
        # Root A has 2 descendants (B, C); root D has 0; root E has 1 (F).
        # With _SAMPLE_ROOTS=2 the seed is the two biggest: A and E (not D).
        rows = _rows(
            _cls("A"),
            _cls("B", "A"),
            _cls("C", "A"),
            _cls("D"),
            _cls("E"),
            _cls("F", "E"),
        )
        with (
            patch("coa_ontology.stores.neptune_db_graph._SAMPLE_ROOTS", 2),
            patch("coa_ontology.stores.neptune_db_graph._SAMPLE_MAX_DESCENDANTS", 50),
        ):
            overview = self._run(rows)
        uris = {c["uri"] for c in overview["classes"]}
        # A + its subtree {B, C}; E + its subtree {F}. D (childless root) dropped.
        assert uris == {"A", "B", "C", "E", "F"}
        assert overview["object_properties"] == []
        assert overview["datatype_properties"] == []

    def test_caps_descendants(self):
        # One root with 5 children; cap descendants at 2 → root + 2 children.
        rows = _rows(
            _cls("Root"),
            _cls("c1", "Root"),
            _cls("c2", "Root"),
            _cls("c3", "Root"),
            _cls("c4", "Root"),
            _cls("c5", "Root"),
        )
        with (
            patch("coa_ontology.stores.neptune_db_graph._SAMPLE_ROOTS", 3),
            patch("coa_ontology.stores.neptune_db_graph._SAMPLE_MAX_DESCENDANTS", 2),
        ):
            overview = self._run(rows)
        uris = {c["uri"] for c in overview["classes"]}
        assert "Root" in uris
        # Root + at most 2 descendants.
        assert len(uris) == 3

    def test_drops_edges_pointing_outside_the_sample(self):
        # A root grounded to a foundational parent NOT in this graph. The
        # subClassOf edge to that parent must be dropped so the root stays a root.
        rows = _rows(
            _cls("Claim", "https://foundational.example#Party"),
            _cls("Auto", "Claim"),
        )
        with (
            patch("coa_ontology.stores.neptune_db_graph._SAMPLE_ROOTS", 3),
            patch("coa_ontology.stores.neptune_db_graph._SAMPLE_MAX_DESCENDANTS", 50),
        ):
            overview = self._run(rows)
        by_uri = {c["uri"]: c for c in overview["classes"]}
        # Foundational parent is out of scope → not materialized, edge dropped.
        assert "https://foundational.example#Party" not in by_uri
        assert by_uri["Claim"]["sub_class_of"] == []
        # In-sample edge (Auto → Claim) is preserved.
        assert by_uri["Auto"]["sub_class_of"] == ["Claim"]

    def test_empty_graph_returns_empty(self):
        overview = self._run(_rows())
        assert overview["classes"] == []
        assert overview["object_properties"] == []
