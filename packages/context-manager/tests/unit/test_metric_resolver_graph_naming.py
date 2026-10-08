# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MetricResolver SPARQL graph-naming fix.

Verifies that _metric_list_sparql():
1. Uses STRSTARTS with the correct graph URI prefix
2. Returns the raw graph URI (?g); namespace is extracted client-side in
   _namespace_from_graph_uri, NOT with a per-row SPARQL REPLACE() regex (the
   regex was the Neptune OOM memory sink under concurrent refreshes).
3. Produces valid SPARQL (single braces)
4. Reflects custom graph URI templates
5. Extracts the namespace correctly even when the prefix contains regex
   metacharacters (the client-side split needs no escaping).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier1.metric_resolver import (
    MetricResolver,
    _metric_list_sparql,
    _namespace_from_graph_uri,
)


@pytest.mark.unit
class TestMetricResolverSparqlGraphNaming:
    """Verify metric list SPARQL targets the correct named graph."""

    def test_uses_strstarts(self):
        """SPARQL uses STRSTARTS with the template's static prefix."""
        sparql = _metric_list_sparql("https://ontology-workbench.local/{namespace}")

        assert "STRSTARTS(STR(?g)" in sparql
        assert "https://ontology-workbench.local/" in sparql
        assert "GRAPH ?g" in sparql
        assert "GRAPH <" not in sparql

    def test_namespace_extracted_client_side_not_via_replace(self):
        """SPARQL returns raw ?g; namespace is derived client-side, no REPLACE regex.

        The per-row REPLACE()/BIND regex was the Neptune OOM memory sink and was
        removed. The query now just selects ?g, and _namespace_from_graph_uri
        extracts the namespace from the graph URI after the rows arrive.
        """
        template = "https://ontology-workbench.local/{namespace}"
        sparql = _metric_list_sparql(template)

        # The OOM-prone per-row regex must be gone.
        assert "REPLACE(" not in sparql
        assert "BIND(" not in sparql
        assert "AS ?namespace)" not in sparql
        # The raw graph URI is returned for client-side extraction instead.
        assert "?g" in sparql

        # Client-side extraction yields the namespace (first segment after prefix).
        prefix = template.split("{namespace}", 1)[0]
        graph_uri = "https://ontology-workbench.local/insurance/onto-123"
        assert _namespace_from_graph_uri(graph_uri, prefix) == "insurance"

    def test_single_braces_in_output(self):
        """Generated SPARQL must have single braces (valid SPARQL)."""
        sparql = _metric_list_sparql("https://ontology-workbench.local/{namespace}")

        assert "{{" not in sparql
        assert "}}" not in sparql

    def test_custom_base_url(self):
        """Custom template is reflected in STRSTARTS prefix."""
        sparql = _metric_list_sparql("https://my-neptune.prod.internal/{namespace}")

        assert "https://my-neptune.prod.internal/" in sparql
        assert "ontology-workbench" not in sparql

    def test_default_template_fallback(self):
        """Empty template uses the default and still produces valid SPARQL."""
        sparql = _metric_list_sparql("")

        assert "STRSTARTS" in sparql
        assert "GRAPH ?g" in sparql
        assert "ontology-workbench.local" in sparql

    def test_namespace_extraction_handles_regex_metachar_prefix(self):
        """A prefix with regex metacharacters (dots) needs no escaping client-side.

        The old code escaped dots because the prefix was interpolated into a
        SPARQL REPLACE() regex pattern. With extraction done by a plain string
        split, a prefix like "ontology.example.com" is matched literally, so a
        dot can never act as a regex wildcard.
        """
        template = "https://ontology.example.com/{namespace}"
        sparql = _metric_list_sparql(template)

        # The literal prefix appears in the STRSTARTS filter, unescaped.
        assert "https://ontology.example.com/" in sparql
        # No regex construct that would need escaping.
        assert "REPLACE(" not in sparql

        prefix = template.split("{namespace}", 1)[0]
        graph_uri = "https://ontology.example.com/finance/onto-9"
        assert _namespace_from_graph_uri(graph_uri, prefix) == "finance"
        # A URI that only differs where a dot-as-wildcard would have matched is
        # correctly rejected (prefix is literal, not a pattern).
        assert _namespace_from_graph_uri("https://ontologyXexample.com/finance/x", prefix) == ""

    def test_resolver_uses_metric_list_sparql(self):
        """MetricResolver._refresh_from_neptune passes the correct SPARQL."""
        mock_neptune = AsyncMock()
        mock_neptune.query.return_value = []
        resolver = MetricResolver(neptune_client=mock_neptune)

        import asyncio

        asyncio.run(resolver._refresh_from_neptune())

        mock_neptune.query.assert_called_once()
        sparql_arg = mock_neptune.query.call_args[0][0]
        assert "STRSTARTS" in sparql_arg
        assert "GRAPH ?g" in sparql_arg
