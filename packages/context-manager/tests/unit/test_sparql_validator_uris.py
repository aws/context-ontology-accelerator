# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SPARQL validator URI extraction and filtering."""

from __future__ import annotations

import pytest
from coa_serve.tier2.ontop.sparql_validator import _SAFE_URI_RE, SPARQLValidator


@pytest.mark.unit
class TestSafeUriRegex:
    def test_rejects_a_trailing_newline(self):
        """`\\Z`, not `$`: `$` matches before a trailing newline, which would let a
        newline-bearing IRI reach SPARQL interpolation. A clean IRI still passes."""
        assert _SAFE_URI_RE.match("https://example.org/ontology")
        assert not _SAFE_URI_RE.match("https://example.org/ontology\n")
        assert not _SAFE_URI_RE.match("https://example.org/o\ninjected")


@pytest.fixture
def validator():
    """Validator with a mock graph client (no Neptune needed)."""
    from unittest.mock import AsyncMock

    mock_graph = AsyncMock()
    return SPARQLValidator(graph_client=mock_graph)


@pytest.mark.unit
class TestURIExtraction:
    def test_extracts_domain_uris(self, validator):
        sparql = """
        PREFIX : <http://example.org/insurance#>
        SELECT ?x WHERE { <http://example.org/insurance#Claim> a <http://example.org/insurance#Policy> }
        """
        uris = validator._extract_uris(sparql)
        assert "http://example.org/insurance#Claim" in uris
        assert "http://example.org/insurance#Policy" in uris

    def test_excludes_w3c_standard_uris(self, validator):
        sparql = """
        SELECT ?x WHERE {
            <http://example.org/insurance#Claim> a <http://www.w3.org/2002/07/owl#Class> .
            <http://example.org/insurance#claimId> <http://www.w3.org/2000/01/rdf-schema#range> <http://www.w3.org/2001/XMLSchema#string>
        }
        """
        uris = validator._extract_uris(sparql)
        assert "http://example.org/insurance#Claim" in uris
        assert "http://example.org/insurance#claimId" in uris
        # W3C URIs should be excluded
        assert all("www.w3.org" not in u for u in uris)

    def test_excludes_bare_namespace_uris(self, validator):
        sparql = "PREFIX : <http://example.org/insurance#> SELECT ?x WHERE { ?x a :Claim }"
        uris = validator._extract_uris(sparql)
        # The bare namespace URI (ending with #) should be excluded
        assert "http://example.org/insurance#" not in uris

    def test_excludes_slash_ending_namespaces(self, validator):
        sparql = "PREFIX ex: <http://example.org/> SELECT ?x WHERE { ex:thing a ex:Class }"
        uris = validator._extract_uris(sparql)
        assert "http://example.org/" not in uris

    def test_excludes_xmlns_and_purl(self, validator):
        sparql = """
        SELECT ?x WHERE {
            ?x <http://xmlns.com/foaf/0.1/name> "test" .
            ?x <http://purl.org/dc/elements/1.1/title> "title"
        }
        """
        uris = validator._extract_uris(sparql)
        assert len(uris) == 0

    def test_empty_sparql_returns_empty(self, validator):
        assert validator._extract_uris("") == []
        assert validator._extract_uris("SELECT ?x WHERE { ?x ?p ?o }") == []
