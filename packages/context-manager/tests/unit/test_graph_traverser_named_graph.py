# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for graph traverser named-graph scoping via STRSTARTS."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.tier3.graph_traverser import GraphTraverser

GRAPH_URI_TEMPLATE = "https://ontology-workbench.local/{namespace}"


@pytest.fixture
def mock_neptune():
    client = AsyncMock()
    client.query.return_value = []
    return client


@pytest.mark.unit
class TestGraphTraverserNamedGraphPattern:
    async def test_label_query_uses_strstarts(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("claims", "insurance", entities=["claim"])

        query = mock_neptune.query.call_args[0][0]
        assert "GRAPH ?g" in query
        assert "STRSTARTS(STR(?g)" in query
        assert "https://ontology-workbench.local/insurance/" in query
        assert "GRAPH <https://ontology-workbench.local/insurance>" not in query

    async def test_hop_query_2hop_uses_strstarts(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse_from_uris(["http://ex.org/Claim"], "my-namespace", max_hops=2)

        query = mock_neptune.query.call_args[0][0]
        assert "GRAPH ?g" in query
        assert "STRSTARTS(STR(?g)" in query
        assert "https://ontology-workbench.local/my-namespace/" in query
        assert "GRAPH <https://ontology-workbench.local/my-namespace>" not in query

    async def test_hop_query_1hop_uses_strstarts(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse_from_uris(["http://ex.org/Claim"], "test-ns", max_hops=1)

        query = mock_neptune.query.call_args[0][0]
        assert "GRAPH ?g" in query
        assert "STRSTARTS(STR(?g)" in query
        assert "https://ontology-workbench.local/test-ns/" in query
        assert "UNION" in query  # 1-hop uses UNION pattern

    async def test_graph_uri_prefix_trailing_slash(self, mock_neptune):
        """Prefix should always end with / for proper STRSTARTS matching."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        prefix = traverser._graph_uri_prefix("my-ns")
        assert prefix == "https://ontology-workbench.local/my-ns/"

    async def test_graph_uri_prefix_deduplicates_slash(self, mock_neptune):
        """If template already produces trailing slash, don't double up."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://example.com/{namespace}/")
        prefix = traverser._graph_uri_prefix("test")
        assert prefix == "https://example.com/test/"
        assert "//" not in prefix.replace("https://", "")


@pytest.mark.unit
class TestGraphTraverserEmptyTemplate:
    async def test_traverse_returns_empty_no_template(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template="")
        results = await traverser.traverse("claims", "insurance", entities=["claim"])
        assert results == []
        mock_neptune.query.assert_not_awaited()

    async def test_traverse_from_uris_returns_empty_no_template(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template="")
        results = await traverser.traverse_from_uris(["http://ex.org/Claim"], "insurance", max_hops=2)
        assert results == []
        mock_neptune.query.assert_not_awaited()


@pytest.mark.unit
class TestGraphTraverserQueryStructure:
    async def test_label_query_multiple_entities(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("claims and policies", "ns", entities=["claim", "policy"])

        query = mock_neptune.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "claim")' in query
        assert 'CONTAINS(LCASE(?label), "policy")' in query
        assert "||" in query

    async def test_label_query_escapes_quotes(self, mock_neptune):
        """A quote reaches the query escaped by ``_build_label_query``.

        The sanitizer only rejects what escaping cannot fix (line terminators, control characters).
        """
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("test", "ns", entities=['foo"bar'])

        query = mock_neptune.query.call_args[0][0]
        assert r'CONTAINS(LCASE(?label), "foo\"bar")' in query
        # The literal is not broken out of: no unescaped quote survives.
        assert 'foo"bar' not in query

    async def test_label_query_rejects_a_line_terminator(self, mock_neptune):
        """The one case escaping cannot cover, so the sanitizer must.

        A SPARQL short string literal may not contain a raw line terminator, so a
        newline would end the literal and put the rest of the token into query
        position. Escaping backslash and quote does not help.
        """
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("test", "ns", entities=["foo\nbar"])
        mock_neptune.query.assert_not_awaited()

    async def test_nonsemantic_entities_are_rejected_without_querying(self, mock_neptune):
        """Safe punctuation is still useless and must not consume query budget."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("test", "ns", entities=["___", "---", "ーー", "😺"])

        mock_neptune.query.assert_not_awaited()

    async def test_mixed_external_entities_keep_only_semantic_terms(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("test", "ns", entities=["___", "Policy", "ーー"])

        query = mock_neptune.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "policy")' in query
        assert "___" not in query
        assert "ーー" not in query

    @pytest.mark.parametrize(
        "entity",
        ["設置場所", "メーカー", "제조사", "wärmepumpe", "órgão", "เครื่องปรับอากาศ", "कर्मचारी"],
    )
    async def test_a_non_ascii_keyword_reaches_the_query(self, mock_neptune, entity):
        """The safety gate is defined by danger and semantic content, not ASCII."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("test", "ns", entities=[entity])

        query = mock_neptune.query.call_args[0][0]
        assert f'CONTAINS(LCASE(?label), "{entity}")' in query

    async def test_extracted_combining_script_terms_reach_lcase_filters(self, mock_neptune):
        """The natural-language path must preserve graphemes before SPARQL generation."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("İstanbul कर्मचारी เครื่องปรับอากาศ", "ns")

        query = mock_neptune.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "i̇stanbul")' in query
        assert 'CONTAINS(LCASE(?label), "कर्मचारी")' in query
        assert 'CONTAINS(LCASE(?label), "เครื่องปรับอากาศ")' in query

    async def test_pure_han_query_uses_bounded_reverse_containment(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("空調設備型番", "ns")

        query = mock_neptune.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "空調設備型番")' in query
        assert 'CONTAINS("空調設備型番", LCASE(STR(?label)))' in query
        assert 'CONTAINS(LCASE(?label), "空調")' not in query

    async def test_realistic_japanese_query_keeps_late_concept_filters(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse(
            "この施設に設置されているエレベーターのメーカーと型番を教えて",
            "ns",
        )

        query = mock_neptune.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "エレベーター")' in query
        assert 'CONTAINS(LCASE(?label), "メーカー")' in query
        assert 'CONTAINS(LCASE(?label), "型番")' in query
        assert "この施設に設置されているエレベーターのメーカーと型番を教えて" in query

    async def test_no_space_thai_query_uses_reverse_containment(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("เครื่องปรับอากาศรุ่นอะไร", "ns")

        query = mock_neptune.query.call_args[0][0]
        assert 'CONTAINS("เครื่องปรับอากาศรุ่นอะไร", LCASE(STR(?label)))' in query

    async def test_a_long_no_space_thai_query_still_reaches_the_store(self, mock_neptune):
        # The per-term cost bound drops the over-length run from forward terms; the
        # reverse container is what keeps the query answerable at all.
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        thai = "ขอทราบรายละเอียดของเครื่องปรับอากาศทั้งหมดในอาคารนี้รวมถึงผู้ผลิตรุ่นและวันที่ติดตั้ง"
        await traverser.traverse(thai, "ns")

        mock_neptune.query.assert_awaited_once()
        query = mock_neptune.query.call_args[0][0]
        assert f'CONTAINS("{thai}", LCASE(STR(?label)))' in query

    async def test_korean_particle_suffixed_query_reaches_the_store_as_a_container(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("이 시설에 설치된 엘리베이터의 제조사와 모델명을 알려줘", "ns")

        query = mock_neptune.query.call_args[0][0]
        # The stored label 제조사 is found inside the particle-suffixed word.
        assert 'CONTAINS("제조사와", LCASE(STR(?label)))' in query
        assert 'CONTAINS("엘리베이터의", LCASE(STR(?label)))' in query

    async def test_label_query_truncation_is_deterministic_without_inverting_relevance(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("空調設備型番", "ns")

        query = mock_neptune.query.call_args[0][0]
        assert "ORDER BY ?entity" in query
        assert query.index("ORDER BY") < query.index("LIMIT")
        # A label-length sort demotes the exact forward hit, so it must not be used.
        assert "STRLEN(STR(?label))) ?entity" not in query

    async def test_an_eszett_keyword_is_not_casefolded_away_from_lcase(self, mock_neptune):
        """The sanitizer's fold must be the one ``LCASE`` applies on the label side.

        ``casefold`` maps ``ß``→``ss``, so the query would search for ``strasse``
        while ``LCASE(?label)`` yields ``straße`` — a comparison that can never be
        true. ``lower()`` leaves ``ß`` alone, matching ``LCASE``'s behaviour.
        """
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("test", "ns", entities=["Straße"])

        query = mock_neptune.query.call_args[0][0]
        assert 'CONTAINS(LCASE(?label), "straße")' in query
        assert "strasse" not in query

    async def test_label_query_has_optional_relationships(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("claims", "ns", entities=["claim"])

        query = mock_neptune.query.call_args[0][0]
        assert "OPTIONAL" in query
        assert "?sourceUri" in query
        assert "?predicateUri" in query
        assert "?targetUri" in query
        assert "?predicateLabel" in query

    async def test_hop_query_2hop_has_values_clause(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse_from_uris(["http://ex.org/A", "http://ex.org/B"], "ns", max_hops=2)

        query = mock_neptune.query.call_args[0][0]
        assert "VALUES ?seed" in query
        assert "<http://ex.org/A>" in query
        assert "<http://ex.org/B>" in query

    async def test_hop_query_2hop_has_hop2_variables(self, mock_neptune):
        """2-hop query surfaces class↔class edges via the induced property domain/range arm."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse_from_uris(["http://ex.org/Claim"], "ns", max_hops=2)

        query = mock_neptune.query.call_args[0][0]
        assert "(rdfs:domain|rdfs:range)" in query
        assert "?sourceUri" in query
        assert "?predicateUri" in query
        assert "?targetUri" in query

    async def test_hop_query_1hop_bidirectional(self, mock_neptune):
        """Query traverses both direct directions (outgoing and incoming) plus
        the induced property-node arm."""
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse_from_uris(["http://ex.org/Claim"], "ns", max_hops=1)

        query = mock_neptune.query.call_args[0][0]
        # Direct arms (non-induced ontologies): both edge directions present.
        assert "?seed ?predicateUri ?entity" in query
        assert "?entity ?predicateUri ?seed" in query
        # Induced arm: property node bridged off the seed class.
        assert "?entity (rdfs:domain|rdfs:range) ?seed" in query

    async def test_hop_query_limits_to_10_uris(self, mock_neptune):
        """traverse_from_uris caps at 10 URIs to prevent bloated queries."""
        uris = [f"http://ex.org/Entity{i}" for i in range(20)]
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse_from_uris(uris, "ns", max_hops=2)

        query = mock_neptune.query.call_args[0][0]
        # Only first 10 should appear
        assert "<http://ex.org/Entity9>" in query
        assert "<http://ex.org/Entity10>" not in query

    async def test_strstarts_filter_outside_graph_block(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        await traverser.traverse("claims", "ns", entities=["claim"])

        query = mock_neptune.query.call_args[0][0]
        graph_open = query.index("{", query.index("GRAPH ?g"))
        depth = 0
        for graph_close in range(graph_open, len(query)):
            depth += {"{": 1, "}": -1}.get(query[graph_close], 0)
            if depth == 0:
                break
        assert query.index("STRSTARTS") > graph_close


@pytest.mark.unit
class TestGraphTraverserNamespaceInPrefix:
    async def test_namespace_with_uuid(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        prefix = traverser._graph_uri_prefix("385e5e21-ac50-48a1-b59b-bbba19f26a24")
        assert prefix == "https://ontology-workbench.local/385e5e21-ac50-48a1-b59b-bbba19f26a24/"

    async def test_namespace_with_simple_name(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template=GRAPH_URI_TEMPLATE)
        prefix = traverser._graph_uri_prefix("bird-benchmark")
        assert prefix == "https://ontology-workbench.local/bird-benchmark/"

    async def test_different_template_base(self, mock_neptune):
        traverser = GraphTraverser(mock_neptune, graph_uri_template="https://custom.domain.com/graphs/{namespace}")
        prefix = traverser._graph_uri_prefix("my-ns")
        assert prefix == "https://custom.domain.com/graphs/my-ns/"


@pytest.mark.unit
class TestGraphTraverserLabelQueryExecutesAgainstATripleStore:
    """Execute the generated SPARQL, rather than asserting on its text.

    Every other SPARQL assertion here is a substring check, which cannot catch a
    query that parses and runs but matches nothing. The reverse-containment arm has
    exactly that failure mode: ``CONTAINS(container, LCASE(?label))`` raises a type
    error for a language-tagged label, and ``FILTER`` converts a type error to
    false — so a Japanese or Thai namespace whose labels carry ``@ja``/``@th``
    silently returns no rows. ``LCASE(STR(?label))`` is what makes the arm see them.
    """

    GRAPH_URI = "https://ontology-workbench.local/ns/o"

    def _store(self):
        from rdflib import RDF, RDFS, Dataset, Literal, URIRef
        from rdflib.namespace import OWL, XSD

        dataset = Dataset()
        graph = dataset.graph(URIRef(self.GRAPH_URI))
        # The same label spelling under the three typings an ontology may carry.
        for name, label in (
            ("Tagged", Literal("空調設備", lang="ja")),
            ("Plain", Literal("空調設備")),
            ("Typed", Literal("空調設備", datatype=XSD.string)),
            # A one-character label the STRLEN guard must exclude.
            ("Short", Literal("空", lang="ja")),
        ):
            subject = URIRef(f"https://example.org/o#{name}")
            graph.add((subject, RDFS.label, label))
            graph.add((subject, RDF.type, OWL.Class))
        return dataset

    def _run(self, sparql: str) -> set[str]:
        dataset = self._store()
        prologue = "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        return {str(row.entity).rsplit("#", 1)[-1] for row in dataset.query(prologue + sparql)}

    def _traverser(self):
        return GraphTraverser(AsyncMock(), graph_uri_template="https://ontology-workbench.local/{namespace}")

    def test_reverse_containment_matches_a_language_tagged_label(self):
        sparql = self._traverser()._build_label_query("ns", [], ["空調設備型番を教えて"])

        assert self._run(sparql) == {"Tagged", "Plain", "Typed"}

    def test_reverse_containment_excludes_a_one_character_label(self):
        sparql = self._traverser()._build_label_query("ns", [], ["空調設備型番を教えて"])

        assert "Short" not in self._run(sparql)

    def test_forward_containment_matches_every_label_typing(self):
        sparql = self._traverser()._build_label_query("ns", ["空調設備"], [])

        assert self._run(sparql) == {"Tagged", "Plain", "Typed"}

    def test_an_unrelated_container_matches_nothing(self):
        sparql = self._traverser()._build_label_query("ns", [], ["エレベーターの点検記録"])

        assert self._run(sparql) == set()
