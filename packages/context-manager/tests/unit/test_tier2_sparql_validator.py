# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Tier 2 SPARQL validator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator


def _make_graph_client(query_result=None):
    """Create a mock GraphClient."""
    client = AsyncMock()
    client.query.return_value = query_result or []
    return client


@pytest.mark.unit
class TestSPARQLValidatorSyntax:
    """Test syntax validation stage."""

    async def test_valid_select_query(self):
        client = _make_graph_client([{"found": "1"}])
        validator = SPARQLValidator(client)

        result = await validator.validate(
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 10",
            "demo",
        )
        assert result.valid is True

    async def test_valid_prefixed_query(self):
        client = _make_graph_client([{"found": "0"}])
        validator = SPARQLValidator(client)

        sparql = """
        PREFIX ex: <http://example.org/>
        SELECT ?name WHERE { ?s ex:hasName ?name }
        """
        result = await validator.validate(sparql, "demo")
        # Syntax is valid even if URIs don't exist
        assert result.error is None or result.error == "URI not found in ontology"

    async def test_invalid_syntax_returns_error(self):
        client = _make_graph_client()
        validator = SPARQLValidator(client)

        result = await validator.validate("SELECTT ?s WHERE { }", "demo")
        assert result.valid is False
        assert result.error == "SPARQL syntax error"

    async def test_empty_sparql_returns_error(self):
        client = _make_graph_client()
        validator = SPARQLValidator(client)

        result = await validator.validate("", "demo")
        assert result.valid is False
        assert result.error == "Empty SPARQL"

    async def test_whitespace_only_sparql(self):
        client = _make_graph_client()
        validator = SPARQLValidator(client)

        result = await validator.validate(" ", "demo")
        assert result.valid is False
        assert result.error == "Empty SPARQL"


@pytest.mark.unit
class TestSPARQLValidatorURIs:
    """Test URI existence validation stage."""

    async def test_all_uris_found_returns_valid(self):
        client = _make_graph_client([{"found": "2"}])
        validator = SPARQLValidator(client)

        sparql = """
        SELECT ?o WHERE {
            <http://example.org/Class1> <http://example.org/prop1> ?o
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    async def test_missing_uri_returns_invalid(self):
        client = _make_graph_client([{"found": "1"}])  # Only 1 of 2 found
        validator = SPARQLValidator(client)

        sparql = """
        SELECT ?o WHERE {
            <http://example.org/Class1> <http://example.org/prop1> ?o
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is False
        assert result.error == "URI not found in ontology"

    async def test_no_uris_in_query_passes(self):
        client = _make_graph_client()
        validator = SPARQLValidator(client)

        # Only variables, no explicit URIs
        result = await validator.validate(
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o } LIMIT 5",
            "demo",
        )
        assert result.valid is True
        client.query.assert_not_called()

    async def test_governed_metric_for_predicate_is_valid(self):
        # `coa:governedMetricFor` must validate as a legitimate
        # predicate. It is a urn: (not http) IRI without a `:metric:` segment,
        # so it is exempt from both the http existence check and the
        # GovernedMetric-URN shape check — locked here so a future tightening
        # of URI validation cannot silently false-reject it.
        client = _make_graph_client([{"found": "1"}])
        validator = SPARQLValidator(client)

        sparql = """
        SELECT ?m WHERE {
            ?m <urn:coa:vocab#governedMetricFor> <http://example.org/Class1>
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    async def test_prov_was_derived_from_predicate_is_valid(self):
        # `prov:wasDerivedFrom` (a W3C standard-vocab predicate) must
        # validate — standard w3.org prefixes are exempt from the
        # ontology-existence check by design.
        client = _make_graph_client([{"found": "1"}])
        validator = SPARQLValidator(client)

        sparql = """
        SELECT ?d WHERE {
            <http://example.org/Class1> <http://www.w3.org/ns/prov#wasDerivedFrom> ?d
        }
        """
        result = await validator.validate(sparql, "demo")
        assert result.valid is True

    async def test_invalid_uri_characters_rejected(self):
        client = _make_graph_client()
        validator = SPARQLValidator(client)

        # URI with spaces (invalid)
        sparql = "SELECT ?o WHERE { <http://example.org/bad uri> ?p ?o }"
        result = await validator.validate(sparql, "demo")
        # rdflib may catch this in syntax, or our URI check will
        assert result.valid is False

    async def test_neptune_error_returns_invalid(self):
        client = AsyncMock()
        client.query.side_effect = RuntimeError("Neptune unreachable")
        validator = SPARQLValidator(client)

        sparql = "SELECT ?o WHERE { <http://example.org/Class1> ?p ?o }"
        result = await validator.validate(sparql, "demo")
        assert result.valid is False
        assert result.error == "URI validation failed"

    async def test_invalid_namespace_raises(self):
        client = _make_graph_client()
        validator = SPARQLValidator(client)

        with pytest.raises(ValueError, match="Invalid namespace"):
            await validator.validate(
                "SELECT ?s WHERE { ?s ?p ?o }",
                "invalid namespace!",
            )
