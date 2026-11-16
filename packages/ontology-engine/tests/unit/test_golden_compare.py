# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for golden_compare scoring."""

from __future__ import annotations

import pytest
from coa_ontology.benchmark.golden_compare import compare_to_golden
from rdflib import Graph

pytestmark = pytest.mark.unit


GOLDEN = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix g: <http://example.com/golden/> .

g:Claim a owl:Class ; rdfs:label "Claim" .
g:Policy a owl:Class ; rdfs:label "Policy" .
g:PolicyHolder a owl:Class ; rdfs:label "Policy Holder" .

g:hasPolicy a owl:ObjectProperty ; rdfs:label "has policy" ; rdfs:domain g:Claim ; rdfs:range g:Policy .
g:claimNumber a owl:DatatypeProperty ; rdfs:label "claim number" ; rdfs:domain g:Claim .
"""


class TestExactMatch:
    """Induced ontology uses identical local names → exact matches."""

    def test_perfect_match(self):
        induced = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix i: <http://example.com/induced/> .

i:Claim a owl:Class ; rdfs:label "Claim" .
i:Policy a owl:Class ; rdfs:label "Policy" .
i:PolicyHolder a owl:Class ; rdfs:label "PolicyHolder" .
i:hasPolicy a owl:ObjectProperty ; rdfs:label "hasPolicy" .
i:claimNumber a owl:DatatypeProperty ; rdfs:label "claimNumber" .
"""
        score = compare_to_golden(induced, GOLDEN)
        assert score.class_recall == 1.0
        assert score.class_precision == 1.0
        assert score.class_f1 == 1.0
        assert score.property_recall == 1.0
        assert all(m.match_type == "exact" for m in score.class_matches)

    def test_partial_match(self):
        """Induced misses PolicyHolder → recall drops but precision stays high."""
        induced = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix i: <http://example.com/induced/> .

i:Claim a owl:Class .
i:Policy a owl:Class .
"""
        score = compare_to_golden(induced, GOLDEN)
        assert score.induced_classes_total == 2
        # 2 of 3 golden classes matched
        assert score.class_recall == pytest.approx(2 / 3)
        # 2 of 2 induced classes matched a golden → 100% precision
        assert score.class_precision == 1.0


class TestFuzzyMatch:
    """Induced uses variant names → aligned matches."""

    def test_camel_snake_swap(self):
        """claim_number ↔ claimNumber."""
        induced = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix i: <http://example.com/induced/> .

i:Claim a owl:Class .
i:claim_number a owl:DatatypeProperty .
"""
        score = compare_to_golden(induced, GOLDEN)
        # Property match should be aligned (underscore → camelCase equivalent)
        prop_match = next(
            m for m in score.property_matches if "claim" in m.golden_uri.lower() and "number" in m.golden_uri.lower()
        )
        assert prop_match.match_type in ("exact", "aligned")

    def test_substring_alignment(self):
        """Induced 'InsuranceClaim' should align with golden 'Claim'."""
        induced = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix i: <http://example.com/induced/> .

i:InsuranceClaim a owl:Class ; rdfs:label "Insurance Claim" .
"""
        score = compare_to_golden(induced, GOLDEN)
        claim_match = next(m for m in score.class_matches if m.golden_uri.endswith("/Claim"))
        assert claim_match.match_type == "aligned"
        assert claim_match.induced_uri.endswith("/InsuranceClaim")


class TestNovelDetection:
    """Induced classes/props not in golden should count as 'novel'."""

    def test_novel_classes_counted(self):
        induced = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix i: <http://example.com/induced/> .

i:Claim a owl:Class .
i:Policy a owl:Class .
i:PolicyHolder a owl:Class .
i:Catastrophe a owl:Class .
i:Adjuster a owl:Class .
"""
        score = compare_to_golden(induced, GOLDEN)
        # 3 matches (Claim, Policy, PolicyHolder), 2 novel (Catastrophe, Adjuster)
        assert score.induced_classes_total == 5
        assert score.induced_classes_novel == 2


class TestGroundingDetection:
    """Detect when induced class was grounded (subclass of foundational)."""

    def test_grounded_to_foundational(self):
        induced = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix schema: <https://schema.org/> .
@prefix fibo: <https://spec.edmcouncil.org/fibo/ontology/FND/Agreements/Contracts/> .
@prefix i: <http://example.com/induced/> .

i:Policy a owl:Class ;
  rdfs:subClassOf fibo:Contract .
i:PolicyHolder a owl:Class ;
  rdfs:subClassOf schema:Person .
"""
        score = compare_to_golden(induced, GOLDEN)
        # Both induced classes match golden AND are grounded to foundational
        assert score.grounded_classes == 2

    def test_ungrounded_is_not_grounded(self):
        """rdfs:subClassOf pointing within the same ontology namespace doesn't count."""
        induced = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix i: <http://example.com/induced/> .

i:Claim a owl:Class .
i:ClaimRecord a owl:Class ;
  rdfs:subClassOf i:Claim .
"""
        score = compare_to_golden(induced, GOLDEN)
        # i:ClaimRecord's subClassOf points to i:Claim (same ns) — not a grounding
        claim_match = next(m for m in score.class_matches if m.golden_uri.endswith("/Claim"))
        # Claim itself has no parent → not grounded (which is correct)
        assert claim_match.grounded_to is None
        assert score.grounded_classes == 0


class TestScoreSerialization:
    def test_as_dict_shape(self):
        score = compare_to_golden(GOLDEN, GOLDEN)  # compare golden to itself = 100%
        d = score.as_dict()
        assert set(d.keys()) == {"class", "property", "combined_f1"}
        assert d["class"]["f1"] == 1.0
        assert d["property"]["f1"] == 1.0
        assert d["combined_f1"] == 1.0

    def test_empty_induced(self):
        """Zero induced classes — scores should be 0, not crash."""
        empty = "@prefix owl: <http://www.w3.org/2002/07/owl#> ."
        score = compare_to_golden(empty, GOLDEN)
        assert score.class_precision == 0.0
        assert score.class_recall == 0.0
        assert score.class_f1 == 0.0
        assert score.combined_f1 == 0.0

    def test_graph_input(self):
        """Should accept rdflib Graph objects directly, not just strings."""
        golden_g = Graph()
        golden_g.parse(data=GOLDEN, format="turtle")
        induced_g = Graph()
        induced_g.parse(data=GOLDEN, format="turtle")
        score = compare_to_golden(induced_g, golden_g)
        assert score.class_f1 == 1.0
