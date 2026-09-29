# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SPARQLValidator named-graph scoping.

The validator's own queries are the LAST graph consumer on the Ontop route that
was still priced by the whole store: unlike the T-Box fetches, they run on EVERY
validate-and-retry attempt, and again in the second translation
``OntopStrategy._retry_vkg`` runs. These tests pin the query TEXT, not the
result, because both scoping forms select the same quads — the difference is only
what the planner can push into the index scan, and that is not observable through
a mocked client.

``test_sparql_validator_strstarts.py`` covers the same methods with no graphs
supplied, which is the fallback these tests assert stays reachable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.query_utils import _MAX_INLINED_GRAPHS
from coa_serve.tier2.ontop.sparql_validator import SPARQLValidator

pytestmark = pytest.mark.unit

_TEMPLATE = "https://ontology-workbench.local/{namespace}"
_G1 = "https://ontology-workbench.local/test-ns/ontology-a"
_G2 = "https://ontology-workbench.local/test-ns/ontology-b"

_SPARQL_WITH_URI = """
SELECT ?x WHERE {
    ?x a <http://example.org/ont#Claim> .
}
"""


@pytest.fixture
def mock_graph():
    client = AsyncMock()
    client.query = AsyncMock(return_value=[{"found": "5"}])
    client.ask = AsyncMock(return_value=True)
    return client


@pytest.fixture
def validator(mock_graph):
    return SPARQLValidator(graph_client=mock_graph, graph_uri_template=_TEMPLATE)


class TestUriExistenceCheckScoping:
    """The batch existence check is one query per attempt — the hottest one."""

    async def test_single_graph_uses_constant_graph_block(self, validator, mock_graph):
        result = await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=[_G1])

        assert result.valid is True
        query = mock_graph.query.call_args[0][0]
        assert f"GRAPH <{_G1}>" in query
        # The whole point: no cluster-wide match-then-filter.
        assert "GRAPH ?g" not in query
        assert "STRSTARTS" not in query
        # COUNT(DISTINCT ?s) is what makes the scoping form safe under UNION.
        assert "COUNT(DISTINCT ?s)" in query

    async def test_two_graphs_union_both(self, validator, mock_graph):
        await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=[_G1, _G2])

        query = mock_graph.query.call_args[0][0]
        assert f"GRAPH <{_G1}>" in query
        assert f"GRAPH <{_G2}>" in query
        assert "UNION" in query
        assert "STRSTARTS" not in query

    @pytest.mark.parametrize("graphs", [None, []])
    async def test_unresolved_graphs_keep_prefix_filter(self, validator, mock_graph, graphs):
        """No graphs resolved degrades to slow, never to no validation."""
        await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=graphs)

        query = mock_graph.query.call_args[0][0]
        assert "GRAPH ?g" in query
        assert 'STRSTARTS(STR(?g), "https://ontology-workbench.local/test-ns/")' in query

    async def test_above_inlining_cap_falls_back(self, validator, mock_graph):
        """Repeating the body 9+ times is the worse trade; query_utils warns."""
        many = [f"{_G1}-{i}" for i in range(_MAX_INLINED_GRAPHS + 1)]
        await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=many)

        query = mock_graph.query.call_args[0][0]
        assert "GRAPH ?g" in query
        assert "STRSTARTS" in query

    async def test_scoping_does_not_change_the_verdict(self, mock_graph):
        """A missing URI still fails, scoped or not — same count, same threshold."""
        mock_graph.query.return_value = [{"found": "0"}]
        validator = SPARQLValidator(graph_client=mock_graph, graph_uri_template=_TEMPLATE)

        scoped = await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=[_G1, _G2])
        unscoped = await validator.validate(_SPARQL_WITH_URI, "test-ns")

        assert scoped.valid is False
        assert unscoped.valid is False
        assert scoped.error == unscoped.error == "URI not found in ontology"

    async def test_a_scoped_shortfall_is_confirmed_unscoped_before_rejecting(self, mock_graph):
        """The resolved graphs are a SUBSET of the prefix, so a scoped miss is not a verdict.

        A URI published only in a graph the resolver did not mark (no
        ``owl:Ontology`` subject) is absent from the scoped count but present in the
        namespace. Rejecting on the scoped count alone would false-reject a valid
        query, so the shortfall is re-checked against the whole prefix.
        """
        mock_graph.query.side_effect = [[{"found": "0"}], [{"found": "1"}]]
        validator = SPARQLValidator(graph_client=mock_graph, graph_uri_template=_TEMPLATE)

        result = await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=[_G1])

        assert result.valid is True, "a URI outside the marked graphs must not be a rejection"
        first, second = (c[0][0] for c in mock_graph.query.call_args_list)
        assert f"GRAPH <{_G1}>" in first and "STRSTARTS" not in first  # fast path stays scoped
        assert "STRSTARTS" in second  # only the rejection path pays the second query

    async def test_a_genuine_miss_still_rejects_after_the_recheck(self, mock_graph):
        """The re-check confirms rather than excuses: an absent URI still fails."""
        mock_graph.query.side_effect = [[{"found": "0"}], [{"found": "0"}]]
        validator = SPARQLValidator(graph_client=mock_graph, graph_uri_template=_TEMPLATE)

        result = await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=[_G1])

        assert result.valid is False
        assert result.error == "URI not found in ontology"
        assert len(mock_graph.query.call_args_list) == 2

    async def test_a_passing_check_never_pays_the_second_query(self, validator, mock_graph):
        """The re-check is on the reject path only — the common case is one query."""
        await validator.validate(_SPARQL_WITH_URI, "test-ns", graph_iris=[_G1])

        assert len(mock_graph.query.call_args_list) == 1


class TestDomainRangeAskScoping:
    """Up to eight ASKs per checked property usage, ten usages, every attempt."""

    async def test_domain_asks_are_scoped(self, validator, mock_graph):
        # False then True: no compatible domain, but a domain IS declared, which is
        # the path that issues BOTH ASKs — and then, because that path ends in a
        # rejection, repeats them unscoped to confirm it.
        mock_graph.ask.side_effect = [False, True, False, True]

        verdict = await validator._is_domain_compatible(
            "http://example.org/ont#Order",
            "http://example.org/ont#hasQuantity",
            "test-ns",
            graph_iris=[_G1],
        )

        assert verdict is False
        asks = [call[0][0] for call in mock_graph.ask.call_args_list]
        assert len(asks) == 4
        for ask in asks[:2]:
            assert f"GRAPH <{_G1}>" in ask
            assert "STRSTARTS" not in ask
        for ask in asks[2:]:
            assert "STRSTARTS" in ask, "the confirming re-run must cover the whole prefix"

    async def test_a_scoped_domain_mismatch_is_confirmed_unscoped(self, validator, mock_graph):
        """A compatible domain in an unmarked graph must not read as a mismatch."""
        # Scoped: no compatible domain, but one is declared -> heading for a reject.
        # Unscoped: a compatible domain does exist -> compatible after all.
        mock_graph.ask.side_effect = [False, True, True]

        verdict = await validator._is_domain_compatible(
            "http://example.org/ont#Order",
            "http://example.org/ont#hasQuantity",
            "test-ns",
            graph_iris=[_G1],
        )

        assert verdict is True, "scoping must not manufacture a domain mismatch"
        assert "STRSTARTS" in mock_graph.ask.call_args_list[2][0][0]

    async def test_a_scoped_range_mismatch_is_confirmed_unscoped(self, validator, mock_graph):
        """Same for the range check, whose only reject is its final return."""
        # xsd:string is in no _XSD_SUBTYPES set, so there is no subtype probe: the
        # scoped pass is compat=False, has_range=True, then the unscoped re-run.
        mock_graph.ask.side_effect = [False, True, True]

        verdict = await validator._is_range_compatible(
            "http://example.org/ont#name",
            "http://www.w3.org/2001/XMLSchema#string",
            "test-ns",
            graph_iris=[_G1],
        )

        assert verdict is True
        assert "STRSTARTS" in mock_graph.ask.call_args_list[2][0][0]

    async def test_range_asks_are_scoped_including_xsd_subtype_probe(self, validator, mock_graph):
        # Not an exact range match, a range IS declared, and the declared range is
        # a supertype — the third ASK only runs on this path.
        mock_graph.ask.side_effect = [False, True, True]

        verdict = await validator._is_range_compatible(
            "http://example.org/ont#age",
            "http://www.w3.org/2001/XMLSchema#integer",
            "test-ns",
            graph_iris=[_G1, _G2],
        )

        assert verdict is True
        assert len(mock_graph.ask.call_args_list) == 3
        for call in mock_graph.ask.call_args_list:
            ask = call[0][0]
            assert f"GRAPH <{_G1}>" in ask
            assert f"GRAPH <{_G2}>" in ask
            assert "UNION" in ask
            assert "STRSTARTS" not in ask

    async def test_graphs_reach_the_asks_through_validate(self, mock_graph):
        """validate() must thread the graphs down two levels, not just use them itself."""
        mock_graph.query.return_value = [{"found": "5"}]
        mock_graph.ask.return_value = False  # no declared domain/range -> fail-open
        validator = SPARQLValidator(graph_client=mock_graph, graph_uri_template=_TEMPLATE)

        sparql = """
        PREFIX ont: <http://example.org/ont#>
        SELECT ?c WHERE {
            ?c a ont:Claim .
            ?c ont:claimAmount ?amount .
        }
        """
        result = await validator.validate(sparql, "test-ns", graph_iris=[_G1])

        assert result.valid is True
        assert mock_graph.ask.called, "domain/range stage did not run"
        for call in mock_graph.ask.call_args_list:
            assert f"GRAPH <{_G1}>" in call[0][0]
            assert "STRSTARTS" not in call[0][0]
