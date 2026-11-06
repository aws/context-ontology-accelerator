# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SPARQLFirewall F1–F5 — nothing unsafe reaches a graph endpoint."""

from __future__ import annotations

import time

import pytest
from coa_serve.tier2.sparql import firewall
from coa_serve.tier2.sparql.firewall import (
    FirewallRule,
    QueryForm,
    SafeQuery,
    SPARQLFirewall,
    UnsafeSPARQLError,
)
from rdflib.plugins.sparql import prepareQuery

pytestmark = pytest.mark.unit

_G = "https://example.test/ns/acme/data"
_EX = "PREFIX ex: <http://example.org/o#>\n"


def _check(sparql, **kwargs) -> SafeQuery:
    return SPARQLFirewall(default_max_results=1000).check(sparql, **kwargs)


def _rejected(sparql, rule, **kwargs) -> UnsafeSPARQLError:
    with pytest.raises(UnsafeSPARQLError) as exc:
        _check(sparql, **kwargs)
    assert exc.value.rule is rule
    return exc.value


def _dataset(sparql) -> set[tuple[str, str]]:
    clauses = prepareQuery(sparql).algebra["datasetClause"] or []
    return {("default", str(c["default"])) if "default" in c else ("named", str(c["named"])) for c in clauses}


def _top_limit(sparql) -> int | None:
    p = prepareQuery(sparql).algebra["p"]
    return int(p["length"]) if p is not None and p.name == "Slice" and "length" in p else None


class TestF1ReadOnly:
    @pytest.mark.parametrize(
        "update",
        [
            "INSERT DATA { <http://a> <http://b> <http://c> }",
            "DELETE DATA { <http://a> <http://b> <http://c> }",
            "DELETE WHERE { ?s ?p ?o }",
            "DELETE { ?s ?p ?o } INSERT { ?s ?p 1 } WHERE { ?s ?p ?o }",
            "WITH <http://g> DELETE { ?s ?p ?o } WHERE { ?s ?p ?o }",
            "DROP ALL",
            "DROP GRAPH <http://g>",
            "CREATE GRAPH <http://g>",
            "LOAD <http://evil/data.ttl>",
            "CLEAR DEFAULT",
            "COPY DEFAULT TO <http://g>",
            "MOVE DEFAULT TO <http://g>",
            "ADD DEFAULT TO <http://g>",
        ],
    )
    def test_every_update_form_is_rejected(self, update):
        _rejected(update, FirewallRule.READ_ONLY)

    @pytest.mark.parametrize(
        "smuggled",
        [
            "SELECT * WHERE { ?s ?p ?o } ; DROP ALL",
            "SELECT * WHERE { ?s ?p ?o }\nINSERT DATA { <http://a> <http://b> <http://c> }",
            "# harmless\nDROP ALL",
        ],
    )
    def test_an_update_smuggled_after_a_query_is_rejected(self, smuggled):
        _rejected(smuggled, FirewallRule.READ_ONLY)

    @pytest.mark.parametrize(
        "query",
        [
            _EX + 'SELECT ?t WHERE { ?t ex:title ?x FILTER(CONTAINS(LCASE(?x), "delete")) }',
            _EX + "SELECT ?t WHERE { ?t a <http://example.org/Load/Drop#Insert> }",
            _EX + "SELECT ?t WHERE { ?t a ex:Ticket } # then DROP ALL",
            _EX + "SELECT ?insert WHERE { ?insert a ex:Ticket }",
        ],
    )
    def test_a_keyword_inside_a_literal_iri_comment_or_variable_is_not_an_update(self, query):
        assert _check(query).form is QueryForm.SELECT

    @pytest.mark.parametrize(
        ("query", "form"),
        [
            ("SELECT ?s WHERE { ?s ?p ?o }", QueryForm.SELECT),
            ("ASK { ?s ?p ?o }", QueryForm.ASK),
            ("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }", QueryForm.CONSTRUCT),
        ],
    )
    def test_the_bounded_read_forms_are_admitted(self, query, form):
        assert _check(query).form is form


class TestProjectedVariables:
    """Serve orders a SELECT's columns by the variables the firewall reports."""

    def test_a_select_reports_its_projection_in_order(self):
        assert _check(f"{_EX}SELECT ?b ?a WHERE {{ ?a ex:p ?b }}").variables == ("b", "a")

    @pytest.mark.parametrize("query", ["ASK { ?s ?p ?o }", "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"])
    def test_the_other_read_forms_report_no_variables(self, query):
        assert _check(query).variables == ()


class TestF2GraphScope:
    @pytest.mark.parametrize(
        "query", ["SELECT * FROM <http://other> WHERE { ?s ?p ?o }", f"SELECT * FROM NAMED <{_G}> WHERE {{ ?s ?p ?o }}"]
    )
    def test_a_caller_written_dataset_clause_is_rejected(self, query):
        _rejected(query, FirewallRule.GRAPH_SCOPE, named_graph=_G)

    def test_graph_is_rejected_when_the_source_reads_its_default_graph(self):
        _rejected(f"SELECT * WHERE {{ GRAPH <{_G}> {{ ?s ?p ?o }} }}", FirewallRule.GRAPH_SCOPE)

    def test_a_graph_other_than_the_sources_is_rejected(self):
        _rejected("SELECT * WHERE { GRAPH <http://other> { ?s ?p ?o } }", FirewallRule.GRAPH_SCOPE, named_graph=_G)

    def test_a_variable_graph_is_rejected(self):
        _rejected("SELECT ?g WHERE { GRAPH ?g { ?s ?p ?o } }", FirewallRule.GRAPH_SCOPE, named_graph=_G)

    def test_the_sources_own_graph_is_allowed(self):
        safe = _check(f"SELECT ?s WHERE {{ GRAPH <{_G}> {{ ?s ?p ?o }} }}", named_graph=_G)
        assert _dataset(safe.sparql) == {("default", _G), ("named", _G)}

    def test_a_named_graph_with_a_trailing_newline_is_refused(self):
        _rejected("SELECT ?s WHERE { ?s ?p ?o }", FirewallRule.GRAPH_SCOPE, named_graph=_G + "\n")

    def test_no_named_graph_leaves_the_default_graph_unscoped(self):
        assert _dataset(_check("SELECT ?s WHERE { ?s ?p ?o }").sparql) == set()

    @pytest.mark.parametrize(
        "query",
        [
            _EX + "SELECT ?s WHERE { ?s a ex:Ticket }",
            _EX + "SELECT ?s { ?s a ex:Ticket }",
            _EX + "SELECT (CONCAT(?a, '{ WHERE') AS ?x) WHERE { ?s ex:a ?a }",
            "ASK { ?s ?p ?o }",
            "ASK WHERE { ?s ?p ?o }",
            "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }",
            "CONSTRUCT WHERE { ?s ?p ?o }",
            _EX + "SELECT ?s WHERE { ?s a ex:Ticket } VALUES ?s { ex:t1 }",
        ],
    )
    def test_every_form_is_confined_to_the_sources_graph(self, query):
        safe = _check(query, named_graph=_G)
        assert _dataset(safe.sparql) == {("default", _G), ("named", _G)}

    def test_an_unsafe_source_graph_iri_is_refused(self):
        _rejected("SELECT * WHERE { ?s ?p ?o }", FirewallRule.GRAPH_SCOPE, named_graph="https://x/> } DROP ALL #")


_GRAPH_IN_EXISTS = [
    "SELECT ?s WHERE {{ ?s ?p ?o FILTER EXISTS {{ GRAPH {g} {{ ?x ?y ?z }} }} }}",
    "SELECT ?s WHERE {{ ?s ?p ?o FILTER NOT EXISTS {{ GRAPH {g} {{ ?x ?y ?z }} }} }}",
    "SELECT ?s ?b WHERE {{ ?s ?p ?o BIND(EXISTS {{ GRAPH {g} {{ ?x ?y ?z }} }} AS ?b) }}",
]


class TestF2GraphInsideExists:
    """P1-2: rdflib keeps a GRAPH inside EXISTS as the parse-tree GraphGraphPattern."""

    @pytest.mark.parametrize("template", _GRAPH_IN_EXISTS)
    @pytest.mark.parametrize("graph", ["<http://example.org/graph/secret>", "?g"])
    def test_a_graph_inside_exists_is_rejected_when_the_source_reads_its_default_graph(self, template, graph):
        _rejected(template.format(g=graph), FirewallRule.GRAPH_SCOPE)

    @pytest.mark.parametrize("template", _GRAPH_IN_EXISTS)
    @pytest.mark.parametrize("graph", ["<http://example.org/graph/secret>", "?g"])
    def test_another_graph_inside_exists_is_rejected(self, template, graph):
        _rejected(template.format(g=graph), FirewallRule.GRAPH_SCOPE, named_graph=_G)

    @pytest.mark.parametrize("template", _GRAPH_IN_EXISTS)
    def test_the_sources_own_graph_inside_exists_is_allowed(self, template):
        safe = _check(template.format(g=f"<{_G}>"), named_graph=_G)
        assert _dataset(safe.sparql) == {("default", _G), ("named", _G)}


class TestTextAndTreeViews:
    """The query text and the parse tree are gated too, not only the algebra."""

    def test_graph_must_name_the_sources_graph_as_a_full_iri(self):
        _rejected(
            "PREFIX d: <https://example.test/ns/acme/>\nSELECT ?s WHERE { GRAPH d:data { ?s ?p ?o } }",
            FirewallRule.GRAPH_SCOPE,
            named_graph=_G,
        )

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT ?service ?graph WHERE { ?service ?graph ?o }",
            _EX + "SELECT ?s WHERE { ?s ex:SERVICE ?o . ?s ex:graph ?g }",
            _EX + "SELECT ?s WHERE { ?s ex:optional ?o . ?s ex:minus ?m . ?s ex:filter ?f . ?s ex:exists ?e }",
            # A keyword after a hyphen or a dot inside a name.
            _EX + "SELECT ?s WHERE { ?s ex:sub-graph ?g . ?s ex:no-filter ?f . ?s ex:a.exists ?e }",
            _EX + "SELECT ?s WHERE { ?s ex:pre-service ?v . ?s ex:not-optional ?o . ?s ex:x.minus ?m }",
            "PREFIX graph-x: <http://example.org/g#>\nPREFIX filter.v: <http://example.org/f#>\n"
            "SELECT ?s WHERE { ?s graph-x:p ?o . ?s filter.v:q ?r }",
        ],
    )
    def test_keywords_as_variables_or_local_names_are_not_patterns(self, query):
        assert _check(query, named_graph=_G).form is QueryForm.SELECT

    @pytest.mark.parametrize(
        "inner", ["?a ?b ?c.SERVICE <http://evil/sparql> { ?s ?q ?r }", "?a ?b ?c.GRAPH ?g { ?s ?q ?r }"]
    )
    def test_a_keyword_right_after_a_triple_dot_is_still_a_pattern(self, inner):
        nested = f"SELECT ?s WHERE {{ ?s ?p ?o FILTER EXISTS {{ ?s ?p ?o FILTER EXISTS {{ {inner} }} }} }}"
        for query in (f"SELECT ?s WHERE {{ {inner} }}", nested):
            with pytest.raises(UnsafeSPARQLError):
                _check(query, named_graph=_G)

    def test_a_pattern_the_parse_tree_lacks_fails_closed(self, monkeypatch):
        real = firewall._tree_of
        monkeypatch.setattr(firewall, "_tree_of", lambda text: real("SELECT ?s WHERE { ?s ?p ?o }"))
        _rejected("SELECT ?s WHERE { ?s ?p ?o OPTIONAL { ?s ?q ?r } }", FirewallRule.FAIL_CLOSED)


class TestF3ResultBound:
    def test_a_missing_limit_is_injected(self):
        safe = _check("SELECT ?s WHERE { ?s ?p ?o } ORDER BY ?s")
        assert safe.limit == 1000
        assert _top_limit(safe.sparql) == 1000

    def test_a_larger_limit_is_lowered(self):
        safe = _check("SELECT ?s WHERE { ?s ?p ?o } LIMIT 50000")
        assert _top_limit(safe.sparql) == 1000

    def test_a_smaller_limit_is_kept(self):
        safe = _check("SELECT ?s WHERE { ?s ?p ?o } LIMIT 7")
        assert safe.limit == 7
        assert _top_limit(safe.sparql) == 7

    def test_the_callers_max_results_lowers_the_cap(self):
        assert _check("SELECT ?s WHERE { ?s ?p ?o }", max_results=25).limit == 25

    def test_the_callers_max_results_never_raises_the_cap(self):
        assert _check("SELECT ?s WHERE { ?s ?p ?o }", max_results=10**9).limit == 1000

    def test_only_the_top_level_limit_counts(self):
        """A bounded subquery does not bound the outer query."""
        query = "SELECT ?s WHERE { { SELECT ?s WHERE { ?s ?p ?o } LIMIT 5 } }"
        safe = _check(query)
        assert _top_limit(safe.sparql) == 1000
        assert "LIMIT 5" in safe.sparql

    def test_a_large_top_level_limit_is_lowered_without_touching_a_subquery(self):
        query = "SELECT ?s WHERE { { SELECT ?s WHERE { ?s ?p ?o } LIMIT 5 } } LIMIT 99999"
        safe = _check(query)
        assert _top_limit(safe.sparql) == 1000
        assert "LIMIT 5" in safe.sparql

    def test_limit_goes_before_a_trailing_values_clause(self):
        safe = _check(_EX + "SELECT ?s WHERE { ?s a ex:Ticket } VALUES ?s { ex:t1 }")
        assert _top_limit(safe.sparql) == 1000
        assert safe.sparql.rstrip().endswith("}")

    def test_a_limit_inside_a_literal_is_not_mistaken_for_the_bound(self):
        safe = _check(_EX + 'SELECT ?s WHERE { ?s ex:note "LIMIT 5" }')
        assert _top_limit(safe.sparql) == 1000

    def test_construct_is_bounded(self):
        assert _top_limit(_check("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }").sparql) == 1000

    @pytest.mark.parametrize(
        "query",
        [
            "DESCRIBE <http://example.org/t1>",
            "DESCRIBE ?s WHERE { ?s ?p ?o }",
            "DESCRIBE <http://example.org/t1> LIMIT 3",
        ],
    )
    def test_describe_is_rejected_because_a_limit_cannot_bound_its_triples(self, query):
        # A LIMIT bounds the described resources (and is ignored with no WHERE), never their triples.
        _rejected(query, FirewallRule.RESULT_BOUND, named_graph=_G)

    def test_a_limit_after_a_trailing_comment_still_bounds_the_query(self):
        safe = _check("SELECT ?s WHERE { ?s ?p ?o } # all of them")
        assert _top_limit(safe.sparql) == 1000

    def test_ask_is_not_given_a_limit(self):
        safe = _check("ASK { ?s ?p ?o }")
        assert safe.limit is None
        assert "LIMIT" not in safe.sparql


class TestF4FailClosed:
    @pytest.mark.parametrize("query", ["", "   ", "SELECT WHERE", "not sparql at all", "SELECT * WHERE { ?s ?p"])
    def test_anything_unparseable_is_rejected(self, query):
        with pytest.raises(UnsafeSPARQLError):
            _check(query)

    def test_trailing_semicolon_is_tolerated(self):
        assert _check("SELECT ?s WHERE { ?s ?p ?o };").form is QueryForm.SELECT

    def test_the_result_is_scoped_and_bounded_together(self):
        safe = _check(_EX + "SELECT ?t WHERE { ?t a ex:Ticket }", named_graph=_G, max_results=10)
        assert _dataset(safe.sparql) == {("default", _G), ("named", _G)}
        assert _top_limit(safe.sparql) == 10


class TestF4RewriteVerification:
    """P1-3: the re-parse of the rewritten text must catch a rewrite that went wrong."""

    _SCOPED = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> WHERE {{ ?s ?p ?o }}"

    def test_a_correctly_scoped_and_bounded_rewrite_passes(self):
        SPARQLFirewall._verify(f"{self._SCOPED}\nLIMIT 5", QueryForm.SELECT, _G, 5)

    def test_a_rewrite_scoped_to_another_graph_is_rejected(self):
        text = "SELECT ?s FROM <http://other> FROM NAMED <http://other> WHERE { ?s ?p ?o } LIMIT 5"
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify(text, QueryForm.SELECT, _G, 5)
        assert exc.value.rule is FirewallRule.FAIL_CLOSED

    def test_a_rewrite_that_lost_its_scope_is_rejected(self):
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify("SELECT ?s WHERE { ?s ?p ?o } LIMIT 5", QueryForm.SELECT, _G, 5)
        assert exc.value.rule is FirewallRule.FAIL_CLOSED

    def test_a_rewrite_that_changed_form_is_rejected(self):
        text = f"ASK FROM <{_G}> FROM NAMED <{_G}> {{ ?s ?p ?o }}"
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify(text, QueryForm.SELECT, _G, None)
        assert exc.value.rule is FirewallRule.FAIL_CLOSED

    @pytest.mark.parametrize("tail", ["", "\nLIMIT 50"])
    def test_a_rewrite_without_the_bound_is_rejected(self, tail):
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify(f"{self._SCOPED}{tail}", QueryForm.SELECT, _G, 5)
        assert exc.value.rule is FirewallRule.RESULT_BOUND

    def test_an_extra_graph_beside_the_sources_is_rejected(self):
        text = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> FROM NAMED <http://other> WHERE {{ ?s ?p ?o }} LIMIT 5"
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify(text, QueryForm.SELECT, _G, 5)
        assert exc.value.rule is FirewallRule.FAIL_CLOSED

    def test_a_limit_one_over_the_bound_is_rejected(self):
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify(f"{self._SCOPED}\nLIMIT 6", QueryForm.SELECT, _G, 5)
        assert exc.value.rule is FirewallRule.RESULT_BOUND

    def test_an_unparseable_rewrite_is_rejected(self):
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify("SELECT ?s WHERE {", QueryForm.SELECT, _G, 5)
        assert exc.value.rule is FirewallRule.FAIL_CLOSED

    def test_check_fails_closed_when_the_scoping_rewrite_breaks(self, monkeypatch):
        monkeypatch.setattr(SPARQLFirewall, "_scope_to_graph", staticmethod(lambda text, form, graph: text))
        _rejected("SELECT ?s WHERE { ?s ?p ?o }", FirewallRule.FAIL_CLOSED, named_graph=_G)

    def test_check_fails_closed_when_the_bounding_rewrite_breaks(self, monkeypatch):
        monkeypatch.setattr(SPARQLFirewall, "_bound", staticmethod(lambda text, form, cap: (text, cap)))
        _rejected("SELECT ?s WHERE { ?s ?p ?o }", FirewallRule.RESULT_BOUND)


_SMUGGLE_SERVICE = (
    'SELECT ((1<2)AS?v)#> WHERE { SERVICE <http://127.0.0.1:9/evil> { ?s ?p ?o } } VALUES ?x { """\n'
    "{ ?s ?p '\"\"\"' } # ' }"
)
_SMUGGLE_GRAPH = (
    'SELECT ((1<2)AS?v)#> WHERE { GRAPH <http://example.org/graph/secret> { ?s ?p ?o } } VALUES ?x { """\n'
    "{ ?s ?p '\"\"\"' } # ' }"
)


class TestFinalQueryIsTheCheckedQuery:
    """A rewrite must never turn a comment into live code.

    Every F1-F5 rule re-runs on the final string, which is the one executed; comment boundaries are the
    parser's, and any disagreement with the masker fails closed.
    """

    @pytest.mark.parametrize("named_graph", [_G, None])
    @pytest.mark.parametrize("query", [_SMUGGLE_SERVICE, _SMUGGLE_GRAPH], ids=["service", "graph"])
    def test_a_service_or_graph_hidden_by_a_comment_is_refused(self, query, named_graph):
        with pytest.raises(UnsafeSPARQLError):
            _check(query, named_graph=named_graph)

    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_a_values_clause_hidden_from_the_parser_cannot_come_alive(self, named_graph):
        with pytest.raises(UnsafeSPARQLError):
            _check("SELECT ?s WHERE { ?s ?p ?o } ORDER BY (?o<2)#> VALUES ?s { <urn:x> }", named_graph=named_graph)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT ?s WHERE { ?s ?p ?o FILTER(?o<2)#> }\n}",
            "SELECT ?s WHERE { ?s ?p ?o FILTER EXISTS { ?s ?q ?o FILTER(?o<2)#> } "
            "GRAPH <http://x/secret> { ?a ?b ?c } }\n} }",
            "SELECT ?s WHERE { ?s ?p ?o } ORDER BY (?o<2)#> LIMIT 999999",
        ],
        ids=["filter", "nested-exists-graph", "limit-in-comment"],
    )
    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_a_comment_the_masker_reads_as_an_iri_is_refused(self, query, named_graph):
        with pytest.raises(UnsafeSPARQLError):
            _check(query, named_graph=named_graph)

    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_an_escaped_hash_cannot_open_a_hidden_comment(self, named_graph):
        """rdflib expands \\u escapes before parsing, so \\u0023 is a '#' the masker never sees."""
        query = "SELECT ?s WHERE { ?s ?p ?o } \\u0023 SERVICE <http://127.0.0.1:9/evil> { ?a ?b ?c }"
        try:
            safe = _check(query, named_graph=named_graph)
        except UnsafeSPARQLError:
            return
        assert "Service" not in str(prepareQuery(safe.sparql).algebra)
        assert "SERVICE" not in safe.sparql

    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_a_plain_comment_is_dropped_from_the_executed_text(self, named_graph):
        safe = _check("SELECT ?s WHERE { ?s ?p ?o } # SERVICE <http://evil> { ?a ?b ?c }", named_graph=named_graph)
        assert "#" not in safe.sparql and "SERVICE" not in safe.sparql
        assert "Service" not in str(prepareQuery(safe.sparql).algebra)

    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_hashes_inside_iris_and_literals_are_kept(self, named_graph):
        query = (
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
            'SELECT ?s WHERE { ?s rdf:type <http://example.org/o#C> ; <urn:p> "a # b" , """x\n# y""" }'
        )
        safe = _check(query, named_graph=named_graph)
        assert "22-rdf-syntax-ns#" in safe.sparql and "o#C" in safe.sparql and "a # b" in safe.sparql

    def test_the_executed_text_has_exactly_the_checked_structure(self):
        safe = _check(f"SELECT ?s WHERE {{ ?s ?p ?o FILTER EXISTS {{ GRAPH <{_G}> {{ ?s ?q ?r }} }} }}", named_graph=_G)
        assert _dataset(safe.sparql) == {("default", _G), ("named", _G)}
        assert _top_limit(safe.sparql) == 1000


# Doubly escaped comment markers, and a carriage return ending a comment.
_ESCAPED_SERVICE = "SELECT ?s WHERE { ?s ?p ?o \\u005Cu0023\rSERVICE <http://127.0.0.1:9/> { ?a ?b ?c }\n}"
_ESCAPED_VALUES = "SELECT ?s WHERE { ?s ?p ?o } \\u005Cu0023 VALUES ?s { <urn:x> }"
_ESCAPED_GRAPH = (
    "SELECT ?v WHERE { ?s ?p ?o \\u005CU00000023\rGRAPH <http://example.org/graph/secret> { ?x <urn:k> ?v }\n}"
)


class TestStructuralRules:
    """No codepoint escape anywhere, nothing admitted that does not parse as submitted, and the
    final executed text passes every rule again."""

    @pytest.mark.parametrize("named_graph", [_G, None])
    @pytest.mark.parametrize(
        "query", [_ESCAPED_SERVICE, _ESCAPED_VALUES, _ESCAPED_GRAPH], ids=["service", "values", "graph"]
    )
    def test_doubly_escaped_comment_markers_are_refused(self, query, named_graph):
        with pytest.raises(UnsafeSPARQLError):
            _check(query, named_graph=named_graph)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT ?s WHERE { ?s ?p ?o } \\u0023 SERVICE <http://127.0.0.1:9/> { ?a ?b ?c }",
            "SELECT ?s WHERE { ?s ?p ?o } \\U00000023 SERVICE <http://127.0.0.1:9/> { ?a ?b ?c }",
            "SELECT ?s WHERE { ?s ?p ?o } \\u005c\\u0023",
            'SELECT ?s WHERE { ?s ?p "caf\\u00E9" }',
            "SELECT ?s WHERE { ?s ?p <http://example.org/\\u0041> }",
            "SELECT ?s WHERE { ?s \\u003Curn:p\\u003E ?o }",
        ],
        ids=["u-outside", "U-outside", "lower-hex-double", "in-literal", "in-iri", "escaped-iri-brackets"],
    )
    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_a_codepoint_escape_anywhere_is_refused(self, query, named_graph):
        with pytest.raises(UnsafeSPARQLError):
            _check(query, named_graph=named_graph)

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT ?s WHERE {",
            "SELECT ?s WHERE { ?s ?p ?o } }",
            "SELECT ?s WHERE { ?s ?p ?o } LIMIT",
            "SELECT ?s WHERE { ?s ?p ?o FILTER( }",
        ],
    )
    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_input_that_does_not_parse_as_submitted_is_refused(self, query, named_graph):
        with pytest.raises(UnsafeSPARQLError):
            _check(query, named_graph=named_graph)

    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_a_comment_ended_by_a_carriage_return_leaves_no_live_service(self, named_graph):
        """SPARQL 1.1 ends a comment at CR as well as LF; whatever is executed must have no comment or SERVICE."""
        query = "SELECT ?s WHERE { ?s ?p ?o # c\rSERVICE <http://127.0.0.1:9/> { ?a ?b ?c }\n}"
        try:
            safe = _check(query, named_graph=named_graph)
        except UnsafeSPARQLError:
            return
        assert "SERVICE" not in safe.sparql and "#" not in safe.sparql

    @pytest.mark.parametrize("named_graph", [_G, None])
    def test_many_hash_iris_are_admitted(self, named_graph):
        triples = " . ".join(f"?s <http://example.org/o#p{i}> ?o{i}" for i in range(70))
        _check(f"SELECT ?s WHERE {{ {triples} }}", named_graph=named_graph)

    def test_an_escaped_hash_in_a_local_name_is_refused_not_read_as_a_comment(self):
        # Escapes outside literals are refused; the masker must still not read ``\#`` as a comment start.
        _rejected("PREFIX ex: <http://ex.org/> SELECT ?s WHERE { ?s ex:a\\#b ?o }", FirewallRule.FAIL_CLOSED)

    def test_a_prefix_named_like_a_query_form_is_not_the_form(self):
        _check("PREFIX construct: <urn:c#> CONSTRUCT { ?s construct:p ?o } WHERE { ?s ?p ?o }", named_graph=_G)


class TestFinalRecheckEachRule:
    """Each re-check on the final text has a test that fails without it."""

    _CHECKED = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> WHERE {{ ?s ?p ?o }}\nLIMIT 5"

    @staticmethod
    def _rejected_by_verify(text, checked_text, rule):
        checked = prepareQuery(checked_text).algebra
        with pytest.raises(UnsafeSPARQLError) as exc:
            SPARQLFirewall._verify(text, QueryForm.SELECT, _G, 5, checked)
        assert exc.value.rule is rule

    def test_a_service_in_the_final_text_is_refused_as_f5(self):
        text = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> WHERE {{ SERVICE <http://x/> {{ ?s ?p ?o }} }}\nLIMIT 5"
        self._rejected_by_verify(text, self._CHECKED, FirewallRule.FEDERATED)

    def test_another_graph_in_the_final_text_is_refused_as_f2(self):
        text = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> WHERE {{ GRAPH <http://x/secret> {{ ?s ?p ?o }} }}\nLIMIT 5"
        self._rejected_by_verify(text, self._CHECKED, FirewallRule.GRAPH_SCOPE)

    def test_a_values_clause_the_checked_query_lacks_is_refused(self):
        text = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> WHERE {{ ?s ?p ?o }}\nLIMIT 5\nVALUES ?s {{ <urn:x> }}"
        self._rejected_by_verify(text, self._CHECKED, FirewallRule.FAIL_CLOSED)

    def test_a_comment_in_the_final_text_is_refused(self):
        self._rejected_by_verify(f"{self._CHECKED} # x", self._CHECKED, FirewallRule.FAIL_CLOSED)

    def test_a_different_operator_with_the_same_shape_is_refused(self):
        checked = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> WHERE {{ {{ ?s ?p ?o }} UNION {{ ?s ?q ?o }} }}\nLIMIT 5"
        text = f"SELECT ?s FROM <{_G}> FROM NAMED <{_G}> WHERE {{ {{ ?s ?p ?o }} MINUS {{ ?s ?q ?o }} }}\nLIMIT 5"
        self._rejected_by_verify(text, checked, FirewallRule.FAIL_CLOSED)


class TestF5Federated:
    @pytest.mark.parametrize(
        "query",
        [
            "SELECT * WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } }",
            "SELECT * WHERE { SERVICE SILENT <http://evil/sparql> { ?s ?p ?o } }",
            "SELECT * WHERE { ?x <urn:p> ?ep . SERVICE ?ep { ?s ?p ?o } }",
            "select * where { service <http://evil/sparql> { ?s ?p ?o } }",
            "SELECT * WHERE { ?s ?p ?o OPTIONAL { SERVICE <http://evil/sparql> { ?s ?q ?z } } }",
            "SELECT * WHERE { { ?s ?p ?o } UNION { SERVICE <http://evil/sparql> { ?s ?q ?z } } }",
            "SELECT * WHERE { ?s ?p ?o MINUS { SERVICE <http://evil/sparql> { ?s ?q ?z } } }",
            "SELECT * WHERE { ?s ?p ?o FILTER EXISTS { SERVICE <http://evil/sparql> { ?s ?q ?z } } }",
            "SELECT * WHERE { { SELECT ?s WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } } } }",
            "ASK { SERVICE <http://evil/sparql> { ?s ?p ?o } }",
            "CONSTRUCT { ?s ?p ?o } WHERE { SERVICE <http://evil/sparql> { ?s ?p ?o } }",
        ],
    )
    def test_service_is_rejected_wherever_it_appears(self, query):
        _rejected(query, FirewallRule.FEDERATED)
        _rejected(query, FirewallRule.FEDERATED, named_graph=_G)

    def test_service_is_reported_as_f5_even_when_f2_would_also_reject(self):
        _rejected(
            f"SELECT * FROM <{_G}> WHERE {{ SERVICE <http://evil/sparql> {{ ?s ?p ?o }} }}",
            FirewallRule.FEDERATED,
            named_graph=_G,
        )

    def test_the_word_service_inside_a_literal_is_not_federation(self):
        assert _check('SELECT ?s WHERE { ?s ?p "SERVICE <http://evil/sparql>" }').form is QueryForm.SELECT


class TestDatasetClauseOutsideTheProjection:
    """On a named-graph source the dataset clause goes before the WHERE clause, never inside the projection.

    A brace in the SELECT projection (an EXISTS expression) used to receive ``FROM <g> FROM NAMED <g>``, so a valid
    query was refused as "Rewritten query does not parse"; without a named graph the same query was accepted.
    """

    @pytest.mark.parametrize(
        "query",
        [
            "SELECT (EXISTS { ?s ?p ?o } AS ?x) WHERE { ?s ?p ?o }",
            "SELECT (NOT EXISTS { ?s ?p 1 } AS ?x) ?s { ?s ?p ?o }",
        ],
    )
    def test_a_brace_in_the_projection_keeps_the_query(self, query):
        safe = _check(query, named_graph=_G)
        projection, _, rest = safe.sparql.partition(f"FROM <{_G}> FROM NAMED <{_G}>")
        assert "EXISTS {" in projection
        assert rest.lstrip().startswith(("WHERE", "{"))


_TITLE = "http://example.org/o#title"


class TestDeniedPredicates:
    """With predicates denied to the caller, a query must neither name one nor read an unknown predicate."""

    @pytest.mark.parametrize(
        "sparql",
        [
            _EX + "SELECT ?t ?v WHERE { ?t ex:title ?v }",
            "SELECT ?t ?v WHERE { ?t <http://example.org/o#title> ?v }",
            _EX + "SELECT ?t WHERE { ?t a ex:Ticket . FILTER EXISTS { ?t ex:title ?v } }",
            _EX + "SELECT ?t ?v WHERE { ?v ^ex:title ?t }",
            _EX + "SELECT ?t ?v WHERE { ?t ex:owner/ex:title ?v }",
            _EX + "SELECT ?t ?v WHERE { ?t (ex:name|ex:title) ?v }",
            _EX + "SELECT ?t ?v WHERE { ?t !ex:name ?v }",
            _EX + "SELECT ?t ?p ?v WHERE { ?t a ex:Ticket ; ?p ?v }",
            _EX + "SELECT ?t WHERE { ?t a ex:Ticket ; ex:owner* ?o . ?o ?p ?v }",
            # A ';' list inside EXISTS arrives as one flat list of triples.
            _EX + "SELECT ?t WHERE { ?t a ex:Ticket . FILTER EXISTS { ?t a ex:Ticket ; ?p ?v } }",
            _EX + "SELECT ?t WHERE { ?t a ex:Ticket . FILTER NOT EXISTS { ?t a ex:Ticket ; ex:name ?n ; ?p ?v } }",
            _EX + "ASK { ?t a ex:Ticket . FILTER EXISTS { ?t a ex:Ticket , ex:Case ; ?p ?v } }",
        ],
        ids=[
            "prefixed",
            "full-iri",
            "filter-exists",
            "inverse",
            "sequence",
            "alternative",
            "negated",
            "var",
            "var-2",
            "exists-list",
            "not-exists-list",
            "ask-exists-list",
        ],
    )
    def test_a_query_that_may_read_a_denied_predicate_is_refused(self, sparql):
        _rejected(sparql, FirewallRule.GRANT, denied_predicates=frozenset({_TITLE}))

    def test_a_query_on_other_predicates_passes(self):
        safe = _check(_EX + "SELECT ?t ?n WHERE { ?t a ex:Ticket ; ex:name ?n }", denied_predicates=frozenset({_TITLE}))
        assert safe.variables == ("t", "n")

    def test_an_iri_is_matched_exactly(self):
        _check(_EX + "SELECT ?t ?v WHERE { ?t ex:Title ?v }", denied_predicates=frozenset({_TITLE}))

    def test_without_denied_predicates_a_variable_predicate_still_passes(self):
        _check(_EX + "SELECT ?t ?p ?v WHERE { ?t a ex:Ticket ; ?p ?v }")


class TestEscapesOutsideLiterals:
    """A backslash escape outside a string literal is refused: the IRI it spells is not the one the checks see."""

    @pytest.mark.parametrize(
        "sparql",
        [
            "PREFIX ex: <http://example.org/o> SELECT ?t ?v WHERE { ?t ex:\\#title ?v }",
            "PREFIX ex: <http://example.org/o#> SELECT ?t ?v WHERE { ?t ex:ti\\-tle ?v }",
            "PREFIX hint: <http://aws.amazon.com/neptune/vocab/v01/QueryHints\\#> SELECT ?s WHERE { ?s ?p ?o }",
        ],
        ids=["escaped-hash", "escaped-dash", "escaped-hint-namespace"],
    )
    def test_an_escaped_local_name_is_refused(self, sparql):
        with pytest.raises(UnsafeSPARQLError):
            SPARQLFirewall().check(sparql, denied_predicates=frozenset({_TITLE}))
        with pytest.raises(UnsafeSPARQLError):
            SPARQLFirewall().check(sparql)

    def test_an_escape_inside_a_string_literal_still_passes(self):
        _check(_EX + 'SELECT ?t WHERE { ?t ex:name ?n . FILTER(?n = "a\\"b") }')


class TestNestedPatternsInsideExists:
    """rdflib keeps a translated EXISTS group as an attribute; patterns nested in it are checked too."""

    _N = _EX + "SELECT ?t WHERE { ?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket %s } }"

    @pytest.mark.parametrize(
        "inner",
        [
            "FILTER EXISTS { ?t ex:title ?v }",
            "FILTER NOT EXISTS { ?t ex:title ?v }",
            "FILTER EXISTS { ?t ?p ?v }",
            "FILTER EXISTS { ?t !ex:name ?v }",
            'FILTER EXISTS { ?t ex:title ?v FILTER(STRSTARTS(?v, "A")) }',
            "MINUS { ?t ex:title ?v }",
            "OPTIONAL { ?t ex:title ?v }",
            "FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS { ?t ex:title ?v } }",
        ],
        ids=["exists", "not-exists", "var", "negated", "oracle", "minus", "optional", "doubly-nested"],
    )
    def test_a_denied_predicate_nested_inside_exists_is_refused(self, inner):
        _rejected(self._N % inner, FirewallRule.GRANT, denied_predicates=frozenset({_TITLE}))

    def test_a_nested_exists_on_other_predicates_still_passes(self):
        _check(self._N % "FILTER EXISTS { ?t ex:name ?n }", denied_predicates=frozenset({_TITLE}))

    def test_a_query_hint_nested_inside_exists_is_refused(self):
        hint = (
            "PREFIX ex: <http://example.org/o#> PREFIX hint: <http://aws.amazon.com/neptune/vocab/v01/QueryHints#> "
            "SELECT ?t WHERE { ?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket "
            "FILTER EXISTS { hint:Query hint:queryTimeout 3600000 . ?t ex:name ?n } } }"
        )
        _rejected(hint, FirewallRule.WORK_BOUND)

    def test_an_unknown_node_in_the_algebra_fails_closed(self, monkeypatch):
        class _Unknown:
            pass

        real = firewall._grant_children

        def with_unknown(node):
            children = real(node)
            return [*children, _Unknown()] if isinstance(node, firewall.CompValue) and node.name == "BGP" else children

        monkeypatch.setattr(firewall, "_grant_children", with_unknown)
        _rejected(
            _EX + "SELECT ?t WHERE { ?t ex:name ?n }", FirewallRule.FAIL_CLOSED, denied_predicates=frozenset({_TITLE})
        )


class TestRelativeIrisAndBase:
    """A relative IRI resolves against a base the checks don't see, so BASE and scheme-less IRIs are refused."""

    @pytest.mark.parametrize(
        "sparql",
        [
            "BASE <urn:ex:o> SELECT ?t ?v WHERE { ?t <#title> ?v }",
            "BASE <http://example.org/o/> SELECT ?t ?v WHERE { ?t <./ti:tle> ?v }",
            "SELECT ?t ?v WHERE { ?t <#title> ?v }",
            "SELECT ?t ?v WHERE { ?t <title> ?v }",
        ],
        ids=["urn-base", "http-base", "fragment-only", "relative"],
    )
    def test_base_and_relative_iris_are_refused(self, sparql):
        _rejected(sparql, FirewallRule.FAIL_CLOSED, denied_predicates=frozenset({"urn:ex:o#title"}))
        _rejected(sparql, FirewallRule.FAIL_CLOSED)

    def test_absolute_iris_and_a_less_than_comparison_still_pass(self):
        _check("SELECT ?t WHERE { ?t <http://example.org/o#n> ?n . FILTER(?n < 5) }")


class TestRepeatedPrefixLabels:
    """A label declared twice has two readings: rdflib keeps the last, an engine may keep the first."""

    @pytest.mark.parametrize(
        "sparql",
        [
            "PREFIX ex: <http://example.org/o#> PREFIX ex: <http://example.org/benign#> "
            "SELECT ?t ?v WHERE { ?t ex:title ?v }",
            "PREFIX ex: <http://example.org/o#> PREFIX ex: <http://example.org/o#> "
            "SELECT ?t ?v WHERE { ?t ex:title ?v }",
            "PREFIX : <http://example.org/o#> PREFIX : <http://example.org/benign#> "
            "SELECT ?t ?v WHERE { ?t :title ?v }",
        ],
        ids=["redefined", "same-iri", "empty-label"],
    )
    def test_a_repeated_prefix_label_is_refused(self, sparql):
        _rejected(sparql, FirewallRule.FAIL_CLOSED, denied_predicates=frozenset({_TITLE}))
        _rejected(sparql, FirewallRule.FAIL_CLOSED)

    def test_distinct_labels_still_pass(self):
        _check(
            "PREFIX ex: <http://example.org/o#> PREFIX ex2: <http://example.org/benign#> "
            "SELECT ?t ?v WHERE { ?t ex2:title ?v }",
            denied_predicates=frozenset({_TITLE}),
        )


_OTHER_GRAPH = "https://example.test/ns/other/data"


class TestForeignGraphNestedInExists:
    """Walking the EXISTS attribute views keeps graph scoping intact: a foreign GRAPH is refused at any depth."""

    @pytest.mark.parametrize(
        "where",
        [
            f"?s a ex:T FILTER EXISTS {{ GRAPH <{_OTHER_GRAPH}> {{ ?s ?p ?o }} }}",
            f"?s a ex:T FILTER NOT EXISTS {{ GRAPH <{_OTHER_GRAPH}> {{ ?s ?p ?o }} }}",
            f"?s a ex:T FILTER EXISTS {{ ?s a ex:T FILTER EXISTS {{ GRAPH <{_OTHER_GRAPH}> {{ ?s ?p ?o }} }} }}",
            f"?s a ex:T MINUS {{ GRAPH <{_OTHER_GRAPH}> {{ ?s ?p ?o }} }}",
            "?s a ex:T FILTER EXISTS { GRAPH ?g { ?s ?p ?o } }",
            f"{{ ?s a ex:T }} UNION {{ ?s a ex:U FILTER EXISTS {{ GRAPH <{_OTHER_GRAPH}> {{ ?s ?p ?o }} }} }}",
            f"{{ SELECT ?s WHERE {{ ?s a ex:T FILTER EXISTS {{ GRAPH <{_OTHER_GRAPH}> {{ ?s ?p ?o }} }} }} }}",
        ],
        ids=["exists", "not-exists", "doubly-nested", "minus", "graph-variable", "under-union", "in-subquery"],
    )
    def test_a_foreign_graph_nested_in_exists_is_refused(self, where):
        _rejected(_EX + f"SELECT ?s WHERE {{ {where} }}", FirewallRule.GRAPH_SCOPE, named_graph=_G)

    def test_the_sources_own_graph_nested_in_exists_is_admitted(self):
        _check(_EX + f"SELECT ?s WHERE {{ ?s a ex:T FILTER EXISTS {{ GRAPH <{_G}> {{ ?s ?p ?o }} }} }}", named_graph=_G)


def _sub(inner: str) -> str:
    return "{ SELECT ?t WHERE { ?t a ex:Ticket " + inner + " } }"


_ORACLE = 'FILTER EXISTS { ?t ex:title ?v FILTER(STRSTARTS(?v, "A")) }'


class TestPatternsTheAlgebraLoses:
    """rdflib translates a nested EXISTS twice and can lose a subquery's patterns; the parse tree is checked too."""

    @pytest.mark.parametrize(
        "where",
        [
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS " + _sub(_ORACLE) + " }",
            "?t a ex:Ticket FILTER EXISTS { FILTER EXISTS " + _sub(_ORACLE) + " }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS "
            + _sub("FILTER NOT EXISTS { ?t ex:title ?v }")
            + " }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER NOT EXISTS " + _sub(_ORACLE) + " }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS "
            + _sub(_ORACLE)
            + " } }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS "
            + _sub("FILTER EXISTS { ?t !ex:x ?v }")
            + " }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS "
            + _sub("FILTER EXISTS { ?t ex:a/ex:title ?v }")
            + " }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS " + _sub("FILTER EXISTS { ?t ?p ?v }") + " }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS { SELECT ?t WHERE { "
            + _sub(_ORACLE)
            + " } } }",
            "?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS "
            + _sub("FILTER(EXISTS { ?t ex:title ?v } && true)")
            + " }",
        ],
        ids=[
            "e-e-sub-e",
            "e-e-sub-e-bare",
            "e-e-sub-not-exists",
            "e-not-exists-sub-e",
            "three-deep",
            "negated",
            "path",
            "var",
            "sub-in-sub",
            "exists-in-expression",
        ],
    )
    @pytest.mark.parametrize("named_graph", [None, _G], ids=["no-graph", "graph"])
    def test_a_denied_predicate_the_algebra_loses_is_refused(self, where, named_graph):
        query = _EX + "SELECT ?t WHERE { " + where + " }"
        _rejected(query, FirewallRule.GRANT, denied_predicates=frozenset({_TITLE}), named_graph=named_graph)

    def test_the_bind_and_ask_forms_are_refused(self):
        bind = (
            _EX
            + "SELECT ?t ?b WHERE { ?t a ex:Ticket BIND(EXISTS { ?t a ex:Ticket FILTER EXISTS "
            + _sub(_ORACLE)
            + " } AS ?b) }"
        )
        ask = _EX + "ASK { ?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS " + _sub(_ORACLE) + " } }"
        for query in (bind, ask):
            _rejected(query, FirewallRule.GRANT, denied_predicates=frozenset({_TITLE}))

    def test_the_same_shape_on_other_predicates_passes(self):
        inner = _sub('FILTER EXISTS { ?t ex:name ?v FILTER(STRSTARTS(?v, "A")) }')
        _check(
            _EX + "SELECT ?t WHERE { ?t a ex:Ticket FILTER EXISTS { ?t a ex:Ticket FILTER EXISTS " + inner + " } }",
            denied_predicates=frozenset({_TITLE}),
        )


class TestNoFalseRefusalsFromTheIriChecks:
    """The BASE and relative-IRI checks read IRIs, not words or comparisons."""

    @pytest.mark.parametrize(
        "sparql",
        [
            "PREFIX base: <http://example.org/b#> SELECT ?s ?n WHERE { ?s base:name ?n }",
            _EX + "SELECT ?s WHERE { ?s ex:knowledge-base ?k }",
            _EX + "SELECT ?s WHERE { ?s ex:a.base ?k }",
            _EX + "SELECT ?s WHERE { ?s ex:n ?n ; ex:m ?m FILTER(?n<5&&?m>3) }",
            _EX + 'SELECT ?s WHERE { ?s ex:n ?n FILTER(?n = "<relative>") }',
        ],
        ids=["base-prefix", "knowledge-base", "a-dot-base", "tight-comparison", "iri-in-a-literal"],
    )
    def test_a_legitimate_query_is_not_refused(self, sparql):
        _check(sparql, denied_predicates=frozenset({_TITLE}))

    @pytest.mark.parametrize(
        "iri",
        ["http://example.org/x/../o#title", "http://example.org/./o#title", "http://example.org/o/.."],
        ids=["dot-dot", "dot", "trailing-dot-dot"],
    )
    def test_an_iri_with_dot_segments_is_refused(self, iri):
        _rejected(f"SELECT ?t ?v WHERE {{ ?t <{iri}> ?v }}", FirewallRule.FAIL_CLOSED)


def _nested_subqueries(depth: int) -> str:
    inner = "?s ex:name ?n"
    for _ in range(depth):
        inner = "{ SELECT ?s WHERE { " + inner + " } }"
    return _EX + "SELECT ?s WHERE { ?s ex:name ?m FILTER EXISTS " + inner + " }"


def _seconds(call) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


class TestDeepNestingStaysLinear:
    """Each walk visits a node once, so nesting depth can't make a check take exponential time."""

    _DEPTH = 18
    _BOUND_S = 1.0

    def test_a_deeply_nested_query_is_checked_quickly(self):
        query = _nested_subqueries(self._DEPTH)
        assert _seconds(lambda: _check(query, denied_predicates=frozenset({_TITLE}))) < self._BOUND_S

    @pytest.mark.parametrize("view", ["algebra", "parse-tree"])
    def test_every_walk_is_quick_on_both_views(self, view):
        query = _nested_subqueries(self._DEPTH)
        tree = firewall._tree_of(query)
        node = SPARQLFirewall._parse(query) if view == "algebra" else firewall._expanded_tree(tree)
        walks = {
            "walk": lambda: list(firewall._walk(node)),
            "iris": lambda: list(firewall._iris(node)),
            "grant": lambda: firewall._check_denied_predicates(node, frozenset({_TITLE})),
        }
        slow = {name: round(s, 2) for name, walk in walks.items() if (s := _seconds(walk)) >= self._BOUND_S}
        assert not slow

    def test_a_denied_predicate_deep_inside_is_still_refused(self):
        query = _nested_subqueries(self._DEPTH).replace("?s ex:name ?n", "?s ex:title ?n")
        _rejected(query, FirewallRule.GRANT, denied_predicates=frozenset({_TITLE}))


class TestAQuoteInsideAnIriCandidate:
    """An apostrophe can't be part of an IRI the masker blanks: the parser may read it as a string literal."""

    @pytest.mark.parametrize(
        "pattern",
        [r"?t ex:\#title ?v", "?t <#title> ?v", "?t ex:title ?v"],
        ids=["escaped-local-name", "relative-iri", "plain-denied"],
    )
    def test_a_comparison_with_a_quoted_bracket_is_refused(self, pattern):
        query = (
            "PREFIX ex: <http://example.org/o#>\nPREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
            "SELECT ?v WHERE { ?t ex:name ?x . FILTER(?x<xsd:string('>')||true) " + pattern + " . }"
        )
        with pytest.raises(UnsafeSPARQLError):
            _check(query, denied_predicates=frozenset({_TITLE}))

    def test_an_iri_with_an_apostrophe_is_refused(self):
        _rejected("SELECT ?s WHERE { ?s <http://example.org/o#it's> ?v }", FirewallRule.FAIL_CLOSED)
