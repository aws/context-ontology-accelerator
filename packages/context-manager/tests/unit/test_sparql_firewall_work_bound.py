# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SPARQLFirewall F6: every query to a Neptune source carries the server-side timeout, and only ours."""

from __future__ import annotations

import pytest
from coa_serve.tier2.sparql.firewall import FirewallRule, SPARQLFirewall, UnsafeSPARQLError
from rdflib import Literal, URIRef
from rdflib.plugins.sparql import prepareQuery

pytestmark = pytest.mark.unit

_G = "https://example.test/ns/acme/data"
_HINTS = "http://aws.amazon.com/neptune/vocab/v01/QueryHints#"
_TIMEOUT = (URIRef(_HINTS + "Query"), URIRef(_HINTS + "queryTimeout"), Literal(7000))


def _check(sparql, **kwargs):
    return SPARQLFirewall(default_max_results=1000).check(sparql, **kwargs)


def _triples(node):
    if hasattr(node, "items"):
        for key, value in node.items():
            if key == "triples":
                yield from value
            else:
                yield from _triples(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _triples(item)


def _outer_triples(sparql):
    """The triples of the query's own WHERE group: not those of a subquery or a CONSTRUCT template."""
    algebra = prepareQuery(sparql).algebra
    pattern = algebra["p"]
    while pattern.name in ("Slice", "Project", "Distinct", "Reduced", "OrderBy", "Extend", "Filter", "Graph"):
        pattern = pattern["p"]
    return list(_triples(pattern))


class TestTheTimeoutIsSent:
    @pytest.mark.parametrize(
        "sparql",
        [
            "SELECT ?s WHERE { ?s ?p ?o }",
            "SELECT ?s { ?s ?p ?o }",
            "ASK { ?s ?p ?o }",
            "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
            "SELECT ?s WHERE { { SELECT ?s WHERE { ?s ?p ?o } LIMIT 999999 } }",
            "SELECT ?s WHERE { OPTIONAL { ?s ?p ?o } }",
            "SELECT (EXISTS { ?a ?b ?c } AS ?e) WHERE { ?s ?p ?o }",
        ],
    )
    def test_every_query_form_carries_the_timeout_in_its_where_group(self, sparql):
        safe = _check(sparql, named_graph=_G, query_timeout_ms=7000)
        assert _TIMEOUT in _outer_triples(safe.sparql)

    def test_the_timeout_does_not_change_the_dataset_or_the_bound(self):
        safe = _check("SELECT ?s WHERE { ?s ?p ?o }", named_graph=_G, query_timeout_ms=7000)
        assert safe.limit == 1000
        assert f"FROM <{_G}> FROM NAMED <{_G}>" in safe.sparql

    def test_a_construct_template_never_gets_the_timeout(self):
        safe = _check("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", query_timeout_ms=7000)
        template = prepareQuery(safe.sparql).algebra["template"]
        assert _TIMEOUT not in list(template)

    def test_the_short_construct_form_is_refused_because_its_pattern_is_its_template(self):
        with pytest.raises(UnsafeSPARQLError) as exc:
            _check("CONSTRUCT WHERE { ?s ?p ?o }", query_timeout_ms=7000)
        assert exc.value.rule is FirewallRule.WORK_BOUND

    def test_without_a_timeout_the_query_is_unchanged(self):
        assert "QueryHints" not in _check("SELECT ?s WHERE { ?s ?p ?o }").sparql


class TestOnlyOurTimeout:
    @pytest.mark.parametrize(
        "sparql",
        [
            f"PREFIX hint: <{_HINTS}> SELECT ?s WHERE {{ hint:Query hint:queryTimeout 999999 . ?s ?p ?o }}",
            f"SELECT ?s WHERE {{ <{_HINTS}Query> <{_HINTS}queryTimeout> 999999 . ?s ?p ?o }}",
            "PREFIX n: <http://aws.amazon.com/neptune/vocab/v01/> SELECT ?s WHERE { n:QueryHints\\#Query"
            " n:QueryHints\\#queryTimeout 999999 . ?s ?p ?o }",
            f"PREFIX hint: <{_HINTS}> SELECT ?s WHERE {{ ?s ?p ?o . hint:Group hint:joinOrder 'Ordered' }}",
            f"PREFIX HINT: <{_HINTS.upper()}> SELECT ?s WHERE {{ ?s ?p ?o }}",
        ],
    )
    def test_a_query_that_names_a_neptune_hint_is_refused(self, sparql):
        with pytest.raises(UnsafeSPARQLError) as exc:
            _check(sparql, query_timeout_ms=7000)
        assert exc.value.rule is FirewallRule.WORK_BOUND

    def test_a_hint_is_refused_even_when_no_timeout_is_asked_for(self):
        with pytest.raises(UnsafeSPARQLError):
            _check(f"SELECT ?s WHERE {{ <{_HINTS}Query> <{_HINTS}queryTimeout> 1 . ?s ?p ?o }}")
