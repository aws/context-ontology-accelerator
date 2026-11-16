# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the /fetch auto-extraction of properties.

These cover the rdflib filtering logic added in ontologies.py::fetch_ontology_from_source
when the caller passes ``extract_properties_for_classes``.
"""

from __future__ import annotations

import pytest
from rdflib import Graph

pytestmark = pytest.mark.unit


_TURTLE_RDFS = """
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix ex: <http://example.com/> .

ex:Person a owl:Class ; rdfs:label "Person" .
ex:Organization a owl:Class ; rdfs:label "Organization" .
ex:Address a owl:Class ; rdfs:label "Address" .

# Property whose domain is Person (in our target list) → should match
ex:hasName a owl:DatatypeProperty ;
  rdfs:label "has name" ;
  rdfs:comment "The name of a person" ;
  rdfs:domain ex:Person ;
  rdfs:range rdfs:Literal .

# Property whose range is Address (in our target list) → should match
ex:livesAt a owl:ObjectProperty ;
  rdfs:label "lives at" ;
  rdfs:domain ex:Person ;
  rdfs:range ex:Address .

# Property on neither → should NOT match
ex:unrelated a owl:DatatypeProperty ;
  rdfs:domain ex:SomethingElse ;
  rdfs:range rdfs:Literal .
"""

_TURTLE_SCHEMA_ORG = """
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix schema: <https://schema.org/> .

schema:Person a rdfs:Class ; rdfs:label "Person" .
schema:Organization a rdfs:Class ; rdfs:label "Organization" .

# Schema.org uses non-standard domainIncludes / rangeIncludes instead of rdfs:domain/range
schema:name a rdf:Property ;
  rdfs:label "name" ;
  schema:domainIncludes schema:Person, schema:Organization ;
  schema:rangeIncludes schema:Text .

schema:email a rdf:Property ;
  rdfs:label "email" ;
  schema:domainIncludes schema:Person ;
  schema:rangeIncludes schema:Text .
"""


def _extract(turtle: str, target_uris: list[str]) -> list[dict]:
    """Minimal replica of the handler's extraction logic, driven directly
    by rdflib so we can test the filter semantics without spinning up
    FastAPI or AWS.
    """
    from rdflib import OWL, RDF, RDFS, URIRef

    g = Graph()
    g.parse(data=turtle, format="turtle")

    targets = {URIRef(u) for u in target_uris}
    schema_domain_preds = [
        URIRef("https://schema.org/domainIncludes"),
        URIRef("http://schema.org/domainIncludes"),
    ]
    schema_range_preds = [
        URIRef("https://schema.org/rangeIncludes"),
        URIRef("http://schema.org/rangeIncludes"),
    ]

    prop_types = [
        OWL.DatatypeProperty,
        OWL.ObjectProperty,
        RDF.Property,
        OWL.AnnotationProperty,
        OWL.FunctionalProperty,
    ]
    props: set = set()
    for pt in prop_types:
        props |= set(g.subjects(RDF.type, pt))

    results = []
    for prop_uri in props:
        domains = set(g.objects(prop_uri, RDFS.domain))
        ranges = set(g.objects(prop_uri, RDFS.range))
        for pred in schema_domain_preds:
            domains |= set(g.objects(prop_uri, pred))
        for pred in schema_range_preds:
            ranges |= set(g.objects(prop_uri, pred))

        matched_d = domains & targets
        matched_r = ranges & targets
        if not matched_d and not matched_r:
            continue

        results.append(
            {
                "property_uri": str(prop_uri),
                "labels": [str(o) for o in g.objects(prop_uri, RDFS.label)],
                "comments": [str(o) for o in g.objects(prop_uri, RDFS.comment)],
                "domain": sorted(str(u) for u in matched_d),
                "range": sorted(str(u) for u in matched_r),
            }
        )
    return results


class TestPropertyExtractionRdfs:
    """rdfs:domain / rdfs:range filtering."""

    def test_match_by_domain(self):
        results = _extract(_TURTLE_RDFS, ["http://example.com/Person"])
        uris = {r["property_uri"] for r in results}
        assert "http://example.com/hasName" in uris
        assert "http://example.com/livesAt" in uris
        assert "http://example.com/unrelated" not in uris

    def test_match_by_range(self):
        # Only 'livesAt' has Address in range; hasName's range is rdfs:Literal
        results = _extract(_TURTLE_RDFS, ["http://example.com/Address"])
        uris = {r["property_uri"] for r in results}
        assert "http://example.com/livesAt" in uris
        assert "http://example.com/hasName" not in uris

    def test_no_match_returns_empty(self):
        results = _extract(_TURTLE_RDFS, ["http://example.com/DoesNotExist"])
        assert results == []

    def test_returned_fields_populated(self):
        results = _extract(_TURTLE_RDFS, ["http://example.com/Person"])
        has_name = next(r for r in results if r["property_uri"] == "http://example.com/hasName")
        assert "has name" in has_name["labels"]
        assert "The name of a person" in has_name["comments"]
        assert has_name["domain"] == ["http://example.com/Person"]


class TestPropertyExtractionSchemaOrg:
    """schema:domainIncludes / schema:rangeIncludes — non-rdfs variants."""

    def test_schema_domain_includes_matches(self):
        """Schema.org properties use domainIncludes, not rdfs:domain. The
        extractor must treat schema:domainIncludes as equivalent for the
        matching step.
        """
        results = _extract(_TURTLE_SCHEMA_ORG, ["https://schema.org/Person"])
        uris = {r["property_uri"] for r in results}
        assert "https://schema.org/name" in uris
        assert "https://schema.org/email" in uris

    def test_schema_returns_only_matched_domain(self):
        """schema:name has domainIncludes Person AND Organization, but the
        request only asked for Person. The returned domain should be just
        the matched subset, not the full set.
        """
        results = _extract(_TURTLE_SCHEMA_ORG, ["https://schema.org/Person"])
        name = next(r for r in results if r["property_uri"] == "https://schema.org/name")
        assert name["domain"] == ["https://schema.org/Person"]
        # Organization is not in targets — should not appear even though
        # the source file lists it as a domain.
        assert "https://schema.org/Organization" not in name["domain"]


class TestSetupPropertyMerge:
    """Verify Step 4's merge of curated (config) + auto-extracted (/fetch) properties."""

    def _merge(self, curated: list[dict], auto: list[dict]) -> list[dict]:
        """Replica of setup.py::generate_property_embeddings merge logic."""
        by_uri: dict[str, dict] = {}
        for ap in auto:
            uri = ap.get("property_uri")
            if uri:
                by_uri[uri] = {
                    "property_uri": uri,
                    "labels": ap.get("labels", []),
                    "comments": ap.get("comments", []),
                    "domain": (ap.get("domain") or [None])[0],
                    "range": (ap.get("range") or [None])[0],
                }
        for cp in curated:
            uri = cp.get("property_uri")
            if uri:
                by_uri[uri] = cp
        return list(by_uri.values())

    def test_union_with_no_overlap(self):
        curated = [{"property_uri": "ex:a", "labels": ["a"]}]
        auto = [{"property_uri": "ex:b", "labels": ["b"], "domain": ["ex:D"], "range": ["ex:R"]}]
        merged = self._merge(curated, auto)
        uris = {m["property_uri"] for m in merged}
        assert uris == {"ex:a", "ex:b"}

    def test_curated_overrides_auto_on_conflict(self):
        """If the same URI appears in both, the curated config entry wins
        (hand-written overrides beat auto-extracted).
        """
        curated = [{"property_uri": "ex:a", "labels": ["curated-label"]}]
        auto = [{"property_uri": "ex:a", "labels": ["auto-label"], "domain": ["ex:D"]}]
        merged = self._merge(curated, auto)
        assert len(merged) == 1
        assert merged[0]["labels"] == ["curated-label"]

    def test_empty_inputs(self):
        assert self._merge([], []) == []

    def test_auto_only(self):
        auto = [
            {"property_uri": "ex:x", "labels": ["x"], "domain": ["ex:D"], "range": ["ex:R"]},
            {"property_uri": "ex:y", "labels": ["y"], "domain": [], "range": []},
        ]
        merged = self._merge([], auto)
        assert len(merged) == 2
        # domain/range flatten list → first element (or None)
        x = next(m for m in merged if m["property_uri"] == "ex:x")
        assert x["domain"] == "ex:D"
        assert x["range"] == "ex:R"
        y = next(m for m in merged if m["property_uri"] == "ex:y")
        assert y["domain"] is None
