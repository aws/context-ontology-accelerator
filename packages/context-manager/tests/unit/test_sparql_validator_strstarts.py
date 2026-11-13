# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SPARQLValidator named-graph queries (STRSTARTS pattern)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator


@pytest.fixture
def mock_graph():
    client = AsyncMock()
    client.query = AsyncMock(return_value=[{"found": "5"}])
    client.ask = AsyncMock(return_value=True)
    return client


@pytest.fixture
def validator(mock_graph):
    return SPARQLValidator(
        graph_client=mock_graph,
        graph_uri_template="https://ontology-workbench.local/{namespace}",
    )


@pytest.mark.unit
class TestValidatorUsesSTRSTARTS:
    """SPARQLValidator queries must use STRSTARTS for per-ontology sub-graphs."""

    async def test_uri_existence_uses_strstarts(self, validator, mock_graph):
        """URI existence check uses GRAPH ?g + STRSTARTS, not fixed GRAPH <uri>."""
        mock_graph.query.return_value = [{"found": "2"}]
        sparql = """
        SELECT ?x WHERE {
            <http://example.org/ont#Claim> a <http://example.org/ont#Policy>
        }
        """
        await validator.validate(sparql, "test-ns")

        # The query method should have been called
        assert mock_graph.query.called
        query_arg = mock_graph.query.call_args[0][0]

        # Must use GRAPH ?g, not GRAPH <...>
        assert "GRAPH ?g" in query_arg
        assert "GRAPH <" not in query_arg

        # Must contain STRSTARTS filter with correct prefix
        assert "STRSTARTS(STR(?g)" in query_arg
        assert "https://ontology-workbench.local/test-ns/" in query_arg

    async def test_domain_check_uses_strstarts(self, validator, mock_graph):
        """Domain compatibility check uses STRSTARTS pattern."""
        await validator._is_domain_compatible(
            "http://example.org/ont#Customer",
            "http://example.org/ont#hasName",
            "test-ns",
        )

        assert mock_graph.ask.called
        ask_arg = mock_graph.ask.call_args[0][0]

        assert "GRAPH ?g" in ask_arg
        assert "GRAPH <" not in ask_arg
        assert "STRSTARTS(STR(?g)" in ask_arg
        assert "https://ontology-workbench.local/test-ns/" in ask_arg

    async def test_range_check_uses_strstarts(self, validator, mock_graph):
        """Range compatibility check uses STRSTARTS pattern."""
        await validator._is_range_compatible(
            "http://example.org/ont#hasAge",
            "http://www.w3.org/2001/XMLSchema#integer",
            "test-ns",
        )

        assert mock_graph.ask.called
        ask_arg = mock_graph.ask.call_args[0][0]

        assert "GRAPH ?g" in ask_arg
        assert "GRAPH <" not in ask_arg
        assert "STRSTARTS(STR(?g)" in ask_arg
        assert "https://ontology-workbench.local/test-ns/" in ask_arg

    async def test_no_fixed_graph_uri_in_domain_compat(self, validator, mock_graph):
        """Both domain compatibility queries use STRSTARTS, not fixed GRAPH."""
        # First ask returns False (no compat domain), second returns True (has domain)
        mock_graph.ask.side_effect = [False, True]

        result = await validator._is_domain_compatible(
            "http://example.org/ont#Order",
            "http://example.org/ont#hasQuantity",
            "my-namespace",
        )
        assert result is False  # has domain but not compatible

        for call in mock_graph.ask.call_args_list:
            arg = call[0][0]
            assert "GRAPH ?g" in arg
            assert "STRSTARTS" in arg
            assert "https://ontology-workbench.local/my-namespace/" in arg

    async def test_range_xsd_subtype_uses_strstarts(self, validator, mock_graph):
        """XSD subtype range check also uses STRSTARTS."""
        # First ask: not exact match, Second: has range, Third: subtype check
        mock_graph.ask.side_effect = [False, True, True]

        result = await validator._is_range_compatible(
            "http://example.org/ont#age",
            "http://www.w3.org/2001/XMLSchema#integer",  # subtype of decimal
            "test-ns",
        )
        assert result is True

        for call in mock_graph.ask.call_args_list:
            arg = call[0][0]
            assert "GRAPH ?g" in arg
            assert "STRSTARTS" in arg


@pytest.mark.unit
class TestOpenSearchDataSourceId:
    """Verify data_source_id flows through VectorHit metadata."""

    def test_parse_hits_includes_data_source_id(self):
        """_parse_hits includes data_source_id from _source documents."""
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        client = OpenSearchVectorClient.__new__(OpenSearchVectorClient)

        mock_response = {
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_score": 0.95,
                        "_source": {
                            "text": "Table: customers",
                            "entity_type": "ontology_class",
                            "entity_uri": "http://ex.org/Customer",
                            "namespace": "test-ns",
                            "data_source_id": "rds-postgres-1",
                        },
                    },
                    {
                        "_id": "doc-2",
                        "_score": 0.88,
                        "_source": {
                            "text": "Table: orders",
                            "entity_type": "ontology_class",
                            "entity_uri": "http://ex.org/Order",
                            "namespace": "test-ns",
                            # No data_source_id — should default to ""
                        },
                    },
                ]
            }
        }

        hits = client._parse_hits(mock_response)

        assert len(hits) == 2
        assert hits[0].metadata["data_source_id"] == "rds-postgres-1"
        assert hits[1].metadata["data_source_id"] == ""

    def test_source_fields_includes_data_source_id(self):
        """_SOURCE_FIELDS list includes data_source_id for projection."""
        from coa_serve.clients.opensearch import _SOURCE_FIELDS

        assert "data_source_id" in _SOURCE_FIELDS
