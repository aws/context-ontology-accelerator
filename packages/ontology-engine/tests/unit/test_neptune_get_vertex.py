# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for NeptuneDBGraphStore.get_vertex blank-node handling.

The vertex-detail endpoint (GET /graph/class?uri=...) crashed with HTTP 400
("Invalid vertex URI: ... got '': 'b35015439'") whenever the looked-up vertex
was the OBJECT of a triple whose SUBJECT is a blank node — e.g. an RDF list
cons-cell from ``owl:members ( ... )`` or an ``owl:Restriction`` class
expression. The incoming-edge loop added the bnode id to ``neighbor_uris``
unconditionally, then ``_iri()`` rejected it in the neighbor-label batch query.

These tests mock the module-level ``_sparql_query`` so no live Neptune is
needed, and assert that blank-node incoming subjects are skipped.
"""

import pytest
from coa_common.constants import GRAPH_BASE_URI, URN_PREFIX

pytestmark = pytest.mark.unit

from unittest.mock import patch

from coa_ontology.stores import neptune_db_graph as ndb
from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore

PUB = "https://parallax.aws/commercial/ontology/Publication"
NS = "test-ns"

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL = "http://www.w3.org/2002/07/owl#"


def _binding(**cols):
    """Build a SPARQL JSON binding row from kwargs of (value, type) tuples."""
    return {k: {"value": v[0], "type": v[1]} for k, v in cols.items()}


def _results(bindings):
    return {"results": {"bindings": bindings}}


class TestGetVertexBlankNodeIncoming:
    def test_bnode_incoming_subject_is_skipped_not_crash(self):
        """A blank-node subject pointing at the vertex must not 500 the lookup."""
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"

        # q1: outgoing triples — Publication is an owl:Class with a label.
        q1 = _results(
            [
                _binding(g=(graph_uri, "uri"), p=(_RDF + "type", "uri"), o=(_OWL + "Class", "uri")),
                _binding(g=(graph_uri, "uri"), p=(_RDFS + "label", "uri"), o=("Publication", "literal")),
            ]
        )
        # q1b: incoming triples — one URI subject + one BLANK-NODE subject
        # (the rdf:first cons-cell from owl:members that caused the crash).
        q1b = _results(
            [
                _binding(
                    g=(graph_uri, "uri"),
                    s=("https://parallax.aws/commercial/ontology/trial_has_publication", "uri"),
                    p=(_RDFS + "range", "uri"),
                ),
                _binding(
                    g=(graph_uri, "uri"),
                    s=("b35015439", "bnode"),  # ← the offending blank node
                    p=(_RDF + "first", "uri"),
                ),
            ]
        )
        # q2 (neighbor labels) and q3 (neighbor types) — empty is fine.
        empty = _results([])

        calls = [q1, q1b, empty, empty]

        def fake_sparql(_query):
            return calls.pop(0) if calls else empty

        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=fake_sparql):
            # Must NOT raise — previously raised ValueError from _iri("b35015439").
            record = store.get_vertex(PUB, namespace=NS)

        assert record is not None
        assert record["uri"] == PUB
        assert _OWL + "Class" in record["types"]
        assert "Publication" in record["labels"]

        # The URI incoming neighbor IS present as an edge.
        incoming = [e for e in record["edges"] if e.get("direction") == "incoming"]
        incoming_objs = {e["neighbor"]["uri"] for e in incoming}
        assert "https://parallax.aws/commercial/ontology/trial_has_publication" in incoming_objs

        # The blank-node subject is NOT present as an edge or neighbor.
        assert "b35015439" not in incoming_objs
        assert all("b35015439" not in str(e["neighbor"]["uri"]) for e in record["edges"])

    def test_uri_incoming_subject_still_included(self):
        """Sanity: normal URI incoming subjects are still surfaced as edges."""
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"
        q1 = _results([_binding(g=(graph_uri, "uri"), p=(_RDF + "type", "uri"), o=(_OWL + "Class", "uri"))])
        q1b = _results(
            [
                _binding(
                    g=(graph_uri, "uri"),
                    s=("http://example.org/Child", "uri"),
                    p=(_RDFS + "subClassOf", "uri"),
                ),
            ]
        )
        empty = _results([])
        calls = [q1, q1b, empty, empty]

        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=lambda q: calls.pop(0) if calls else empty):
            record = store.get_vertex(PUB, namespace=NS)

        incoming = [e for e in record["edges"] if e.get("direction") == "incoming"]
        assert any(e["neighbor"]["uri"] == "http://example.org/Child" for e in incoming)


class TestGetVertexEnrichment:
    """Exercise the full get_vertex path: outgoing + incoming + neighbor
    label/type enrichment queries (q2/q3) and the assembled response shape."""

    def test_neighbor_labels_and_kinds_resolved(self):
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"
        # q1 outgoing: type, label, comment, and a URI object neighbor (superclass)
        q1 = _results(
            [
                _binding(g=(graph_uri, "uri"), p=(_RDF + "type", "uri"), o=(_OWL + "Class", "uri")),
                _binding(g=(graph_uri, "uri"), p=(_RDFS + "label", "uri"), o=("Publication", "literal")),
                _binding(g=(graph_uri, "uri"), p=(_RDFS + "comment", "uri"), o=("A pub.", "literal")),
                _binding(
                    g=(graph_uri, "uri"),
                    p=(_RDFS + "subClassOf", "uri"),
                    o=("http://example.org/Document", "uri"),
                ),
            ]
        )
        # q1b incoming: a URI child class
        q1b = _results(
            [
                _binding(
                    g=(graph_uri, "uri"),
                    s=("http://example.org/Article", "uri"),
                    p=(_RDFS + "subClassOf", "uri"),
                ),
            ]
        )
        # q2 neighbor labels (for Document + Article)
        q2 = _results(
            [
                _binding(o=("http://example.org/Document", "uri"), label=("Document", "literal")),
                _binding(o=("http://example.org/Article", "uri"), label=("Article", "literal")),
            ]
        )
        # q3 neighbor types
        q3 = _results(
            [
                _binding(o=("http://example.org/Document", "uri"), t=(_OWL + "Class", "uri")),
                _binding(o=("http://example.org/Article", "uri"), t=(_OWL + "Class", "uri")),
            ]
        )
        calls = [q1, q1b, q2, q3]

        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=lambda q: calls.pop(0) if calls else _results([])):
            record = store.get_vertex(PUB, namespace=NS)

        assert record["uri"] == PUB
        assert record["labels"] == ["Publication"]
        assert record["comments"] == ["A pub."]
        assert _OWL + "Class" in record["types"]

        by_uri = {e["neighbor"]["uri"]: e["neighbor"] for e in record["edges"]}
        assert by_uri["http://example.org/Document"]["label"] == "Document"
        assert by_uri["http://example.org/Document"]["kind"] == "class"
        assert by_uri["http://example.org/Article"]["label"] == "Article"
        # Directions captured
        dirs = {e["direction"] for e in record["edges"]}
        assert dirs == {"outgoing", "incoming"}

    def test_no_triples_returns_none(self):
        """When neither outgoing nor incoming triples exist, return None."""
        empty = _results([])
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=lambda q: empty):
            record = store.get_vertex(PUB, namespace=NS)
        assert record is None

    def test_literal_object_edge_kind(self):
        """A non-rdfs:label literal object is surfaced as a literal-kind edge."""
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"
        q1 = _results(
            [
                _binding(g=(graph_uri, "uri"), p=(_RDF + "type", "uri"), o=(_OWL + "Class", "uri")),
                _binding(
                    g=(graph_uri, "uri"),
                    p=("https://parallax.aws/commercial/ontology/identifier", "uri"),
                    o=("C0043", "literal"),
                ),
            ]
        )
        calls = [q1, _results([]), _results([]), _results([])]
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=lambda q: calls.pop(0) if calls else _results([])):
            record = store.get_vertex(PUB, namespace=NS)
        lit_edges = [e for e in record["edges"] if e["neighbor"]["kind"] == "literal"]
        assert any(e["neighbor"]["uri"] == "C0043" for e in lit_edges)


_SCL = f"{GRAPH_BASE_URI}/vocab/coa#"


class TestGetVertexIsMapped:
    """get_vertex surfaces the coa:isMapped Tier-2 answerability marker as a
    first-class boolean (not buried in edges).

    A structured/R2RML-mapped class carries ``coa:isMapped "true"^^xsd:boolean``;
    unstructured-induced and foundational classes have NO such triple. The
    read path must reflect that so the E2E/UI can assert answerability."""

    def _run(self, extra_outgoing):
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"
        rows = [
            _binding(g=(graph_uri, "uri"), p=(_RDF + "type", "uri"), o=(_OWL + "Class", "uri")),
            _binding(g=(graph_uri, "uri"), p=(_RDFS + "label", "uri"), o=("Publication", "literal")),
        ]
        rows.extend(extra_outgoing)
        calls = [_results(rows), _results([]), _results([]), _results([])]
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=lambda q: calls.pop(0) if calls else _results([])):
            return store.get_vertex(PUB, namespace=NS)

    def test_mapped_class_is_mapped_true(self):
        """A class with the coa:isMapped 'true' triple reports is_mapped=True."""
        record = self._run(
            [
                _binding(
                    g=(f"{ndb._namespace_graph_prefix(NS)}/x", "uri"),
                    p=(_SCL + "isMapped", "uri"),
                    o=("true", "literal"),
                )
            ]
        )
        assert record["is_mapped"] is True
        # The marker must NOT also appear as a generic edge.
        assert all(e["predicate"] != _SCL + "isMapped" for e in record["edges"])

    def test_unmapped_class_is_mapped_false(self):
        """A class with no coa:isMapped triple reports is_mapped=False (absent == unmapped)."""
        record = self._run([])
        assert record["is_mapped"] is False

    def test_ismapped_false_literal_is_false(self):
        """A defensively-written coa:isMapped 'false' is treated as False, not truthy."""
        record = self._run(
            [
                _binding(
                    g=(f"{ndb._namespace_graph_prefix(NS)}/x", "uri"),
                    p=(_SCL + "isMapped", "uri"),
                    o=("false", "literal"),
                )
            ]
        )
        assert record["is_mapped"] is False


class TestSearchEntitiesStore:
    """Cover the search_entities SPARQL path on the Neptune store."""

    def test_search_dedup_and_kind(self):
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"
        bindings = _results(
            [
                _binding(
                    s=(PUB, "uri"),
                    label=("Publication", "literal"),
                    t=(_OWL + "Class", "uri"),
                    g=(graph_uri, "uri"),
                ),
                # duplicate row for same URI with a more specific/again class type
                _binding(
                    s=(PUB, "uri"),
                    label=("Publication", "literal"),
                    t=(_OWL + "Class", "uri"),
                    g=(graph_uri, "uri"),
                ),
            ]
        )
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", return_value=bindings):
            results = store.search_entities(query="publication", namespace=NS, limit=10)
        assert len(results) == 1
        assert results[0]["uri"] == PUB
        assert results[0]["label"] == "Publication"
        assert results[0]["kind"] == "class"

    def test_search_collects_all_source_graphs_for_shared_iri(self):
        """A class IRI asserted in more than one named graph collapses to ONE
        hit that carries ALL its source graphs in ``graph_uris`` — not just the
        first seen. (e.g. a shared IRI referenced by both schema.org and FIBO.)"""
        g1 = f"{ndb._namespace_graph_prefix(NS)}/schema"
        g2 = f"{ndb._namespace_graph_prefix(NS)}/fibo"
        bindings = _results(
            [
                _binding(s=(PUB, "uri"), label=("Publication", "literal"), t=(_OWL + "Class", "uri"), g=(g1, "uri")),
                _binding(s=(PUB, "uri"), label=("Publication", "literal"), t=(_OWL + "Class", "uri"), g=(g2, "uri")),
            ]
        )
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", return_value=bindings):
            results = store.search_entities(query="publication", namespace=NS, limit=10)
        assert len(results) == 1
        assert results[0]["uri"] == PUB
        # Both graphs present, order-stable, no scalar graph_uri key.
        assert results[0]["graph_uris"] == [g1, g2]
        assert "graph_uri" not in results[0]

    def test_search_with_kind_and_ontology_filter(self):
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(
                query="pub", namespace=NS, kind="class", ontology_id="http://example.org/myonto/", limit=5
            )
        # ontology_id filter pins ?g to the single ontology graph
        assert "myonto" in captured["query"]
        # kind filter adds a VALUES ?requiredType clause
        assert "requiredType" in captured["query"]

    def test_wildcard_query_drops_contains_filter(self):
        """``q=*`` (or empty) means match-all: no CONTAINS substring filter.

        The UI auto-loads a namespace's classes by passing ``q=*``. Treating
        ``*`` as a literal substring matches no IRI, so the filter must be
        dropped for the wildcard case.
        """
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        for wildcard in ("*", "", "  "):
            captured = {}

            def capture(q, _store_captured=captured):
                _store_captured["query"] = q
                return _results([])

            with patch.object(ndb, "_sparql_query", side_effect=capture):
                store.search_entities(query=wildcard, namespace=NS, kind="class", limit=5)
            assert "CONTAINS" not in captured["query"], wildcard

    def test_nonwildcard_query_keeps_contains_filter(self):
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(query="publication", namespace=NS, limit=5)
        assert "CONTAINS" in captured["query"]
        assert "publication" in captured["query"]

    def test_substring_matches_local_name_not_full_iri(self):
        """The query substring is matched against the entity's LOCAL NAME (after
        the last # or /), NOT the whole IRI — so a term appearing only in a path
        segment (e.g. ``.../ProductsAndServices/Broker``) does not spuriously
        match. Guards the fix for full-IRI path-noise matches."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(query="product", namespace=NS, limit=5)
        # The subject substring test must run against the REPLACE-stripped local
        # name, not the raw STR(?s).
        assert 'REPLACE(STR(?s), "^.*[#/]", "")' in captured["query"]
        assert "CONTAINS(LCASE(STR(?s))" not in captured["query"]
        # Label is still matched so human-readable names hit.
        assert "?label" in captured["query"]

    def test_exclude_ontology_id_adds_graph_filter(self):
        """exclude_ontology_id appends a FILTER(?g != ...) to the SPARQL query."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(
                query="Claim",
                namespace=NS,
                exclude_ontology_id=f"urn:{URN_PREFIX}:vocab#GovernedMetrics",
                limit=10,
            )
        assert "FILTER(?g !=" in captured["query"]
        assert "GovernedMetrics" in captured["query"]

    def test_exclude_ontology_id_special_chars_escaped(self):
        """Special characters in exclude_ontology_id are handled by _iri validation."""
        # _iri rejects URIs with forbidden characters (angle brackets, braces, etc.)
        # so a malicious input should raise ValueError, not produce broken SPARQL.
        with pytest.raises(ValueError):
            ndb._iri("'; DROP ALL;--")

        with pytest.raises(ValueError):
            ndb._iri("http://evil.com/><script>alert(1)</script>")

        with pytest.raises(ValueError):
            ndb._iri("urn:test\\backslash")

    def test_exclude_ontology_id_valid_urn_passes(self):
        """Valid URN-based ontology IDs pass _iri and produce correct SPARQL."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(
                query="test",
                namespace=NS,
                exclude_ontology_id="urn:example:ontology#Special-Name_v2",
                limit=10,
            )
        assert "FILTER(?g !=" in captured["query"]
        assert "Special-Name_v2" in captured["query"]

    def test_search_applies_limit_and_offset(self):
        """The paged query carries both LIMIT and OFFSET so the store returns a
        stable page window rather than the whole result set."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(query="pub", namespace=NS, limit=20, offset=40)
        assert "LIMIT 20" in captured["query"]
        assert "OFFSET 40" in captured["query"]
        # A deterministic ORDER BY is required for offset paging to be stable.
        assert "ORDER BY" in captured["query"]

    def test_search_negative_offset_clamped_to_zero(self):
        """A negative offset is clamped so we never emit ``OFFSET -N`` (invalid
        SPARQL) — the route already rejects it, this is defence in depth."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(query="pub", namespace=NS, limit=10, offset=-5)
        assert "OFFSET 0" in captured["query"]
        assert "OFFSET -" not in captured["query"]

    def test_search_limit_clamped_to_page_ceiling(self):
        """An oversized limit is capped so we never emit an unbounded LIMIT — the
        route rejects it with a 400, this is the store's own backstop for callers
        that bypass the router."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(query="pub", namespace=NS, limit=1_000_000_000)
        assert f"LIMIT {ndb._MAX_SEARCH_PAGE_SIZE}" in captured["query"]
        assert "LIMIT 1000000000" not in captured["query"]

    def test_search_nonpositive_limit_clamped_to_one(self):
        """``LIMIT 0`` / ``LIMIT -N`` is invalid or degenerate SPARQL; clamp to 1."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        for bad in (0, -5):
            with patch.object(ndb, "_sparql_query", side_effect=capture):
                store.search_entities(query="pub", namespace=NS, limit=bad)
            assert "LIMIT 1 " in captured["query"] or "LIMIT 1\n" in captured["query"]
            assert "LIMIT -" not in captured["query"]
            assert "LIMIT 0" not in captured["query"]

    def test_search_outer_query_is_namespace_scoped(self):
        """Regression: the outer query re-binds ?g, so it must ALSO carry the
        namespace prefix filter. Without it a subject shared across namespaces
        (e.g. foaf:Person) binds ?g to a foreign namespace's graph and leaks its
        graph_uri/labels — cross-namespace data isolation break."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(query="*", namespace=NS, limit=10)
        prefix = ndb._namespace_graph_prefix(NS).rstrip("/") + "/"
        # The namespace-prefix STRSTARTS filter must appear TWICE: once in the
        # inner subquery, once in the outer ?g re-binding.
        assert captured["query"].count(f'STRSTARTS(STR(?g), "{prefix}")') == 2

    def test_search_outer_query_scoped_to_single_ontology(self):
        """When ontology_id is given, the outer ?g must be pinned to that one
        ontology graph too (not just the inner subquery)."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.search_entities(query="*", namespace=NS, ontology_id="http://example.org/myonto/", limit=10)
        graph_uri = ndb._ontology_graph_uri("http://example.org/myonto/", NS)
        # FILTER(?g = <graph_uri>) appears in both the inner and outer scopes.
        assert captured["query"].count(f"FILTER(?g = <{graph_uri}>)") == 2


@pytest.mark.unit
class TestCountEntitiesStore:
    """Cover the count_entities SPARQL path on the Neptune store."""

    def test_count_returns_total(self):
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        bindings = _results([{"total": {"value": "137", "type": "literal"}}])
        with patch.object(ndb, "_sparql_query", return_value=bindings):
            total = store.count_entities(query="pub", namespace=NS)
        assert total == 137

    def test_count_empty_result_is_zero(self):
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", return_value=_results([])):
            assert store.count_entities(query="none", namespace=NS) == 0

    def test_count_uses_count_distinct_and_shares_filters(self):
        """The count query is COUNT(DISTINCT ?s) over the SAME filter body as the
        paged search — so total matches the pageable set. No LIMIT/OFFSET."""
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        captured = {}

        def capture(q):
            captured["query"] = q
            return _results([{"total": {"value": "0", "type": "literal"}}])

        with patch.object(ndb, "_sparql_query", side_effect=capture):
            store.count_entities(query="pub", namespace=NS, kind="class")
        assert "COUNT(DISTINCT ?s)" in captured["query"]
        assert "requiredType" in captured["query"]  # kind filter shared
        assert "CONTAINS" in captured["query"]  # text filter shared
        assert "LIMIT" not in captured["query"]
        assert "OFFSET" not in captured["query"]


class TestGetVertexSupersededHistory:
    """coa:superseded* history literals are surfaced under ``superseded``,
    never as live ``comments``/``alt_labels`` and never as ``edges``."""

    def test_history_predicates_bucketed_not_live_not_edges(self):
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"
        q1 = _results(
            [
                _binding(g=(graph_uri, "uri"), p=(_RDF + "type", "uri"), o=(_OWL + "Class", "uri")),
                _binding(g=(graph_uri, "uri"), p=(_RDFS + "comment", "uri"), o=("Steward text.", "literal")),
                _binding(g=(graph_uri, "uri"), p=(_SCL + "supersededComment", "uri"), o=("AI text.", "literal")),
                _binding(g=(graph_uri, "uri"), p=(_SCL + "supersededAltLabel", "uri"), o=("cust_master", "literal")),
                _binding(g=(graph_uri, "uri"), p=(_SCL + "supersededAltLabel", "uri"), o=("cust_master", "literal")),
            ]
        )
        calls = [q1, _results([]), _results([]), _results([])]
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=lambda q: calls.pop(0) if calls else _results([])):
            record = store.get_vertex(PUB, namespace=NS)
        assert record["comments"] == ["Steward text."]
        assert record["alt_labels"] == []
        assert record["superseded"] == {"comments": ["AI text."], "altLabels": ["cust_master"]}  # deduped
        assert not any("superseded" in e["p"] for e in record["edges"])

    def test_no_history_gives_empty_dict(self):
        graph_uri = f"{ndb._namespace_graph_prefix(NS)}/x"
        q1 = _results([_binding(g=(graph_uri, "uri"), p=(_RDF + "type", "uri"), o=(_OWL + "Class", "uri"))])
        calls = [q1, _results([]), _results([]), _results([])]
        store = NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)
        with patch.object(ndb, "_sparql_query", side_effect=lambda q: calls.pop(0) if calls else _results([])):
            record = store.get_vertex(PUB, namespace=NS)
        assert record["superseded"] == {}
