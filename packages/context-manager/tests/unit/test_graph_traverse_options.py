# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""GraphTraverse applies maxDepth, direction and relationshipFilter.

The generated SPARQL runs against a real rdflib dataset, so these tests check what
the options do to the result, not only the query text.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from coa_common.label_language import LabelPreference
from coa_serve.deadline import Deadline
from coa_serve.exceptions import DataSourceUnavailableError
from coa_serve.main import _handle_graph_traverse
from coa_serve.namespace_language import bind_label_preference
from coa_serve.tier3.graph_traverser import _GOVERNED_GRAPH_SEGMENT, GraphTraverser, _relationship_cap
from rdflib import OWL, RDF, RDFS, XSD, Dataset, Literal, URIRef
from structlog.testing import capture_logs

TEMPLATE = "https://ontology-workbench.local/{namespace}"
PREFIX = "https://ontology-workbench.local/demo/"
GRAPH = URIRef(f"{PREFIX}induced")
P = URIRef("urn:predicate:p")
Q = URIRef("urn:predicate:q")


def _e(name: str) -> URIRef:
    return URIRef(f"urn:entity:{name}")


class _RdflibGraph:
    """A ``GraphClient`` that runs each query on an rdflib dataset.

    A query without ``ORDER BY`` has no defined solution order in SPARQL, and
    Neptune does not promise one, so its ``LIMIT`` may keep any rows. This fake
    runs such a query without its ``LIMIT``, reverses the rows on every other
    such call, and only then applies the ``LIMIT``. A traversal that relies on the
    store's order then returns different results for the same request.
    """

    def __init__(self, dataset: Dataset):
        self._dataset = dataset
        self.queries: list[str] = []
        self._unordered_calls = 0

    def _rows(self, sparql: str) -> list[dict]:
        return [
            {str(name): str(value) for name, value in binding.items() if value is not None}
            for binding in self._dataset.query(sparql).bindings
        ]

    async def query(self, sparql: str, *, max_results: int = 10_000) -> list[dict]:
        self.queries.append(sparql)
        if "ORDER BY" in sparql:
            return self._rows(sparql)
        self._unordered_calls += 1
        limit = re.search(r"\bLIMIT\s+(\d+)\s*$", sparql)
        rows = self._rows(sparql[: limit.start()] if limit else sparql)
        if self._unordered_calls % 2 == 0:
            rows.reverse()
        return rows[: int(limit.group(1))] if limit else rows


def _dataset(*triples: tuple, graph: URIRef = GRAPH) -> Dataset:
    dataset = Dataset()
    named = dataset.graph(graph)
    named.add((URIRef(f"{graph}#onto"), RDF.type, OWL.Ontology))
    for triple in triples:
        named.add(triple)
    return dataset


def _labelled(*names: str) -> list[tuple]:
    return [(_e(name), RDFS.label, Literal(name.upper())) for name in names]


def _chain() -> Dataset:
    """a -p-> b -p-> c -p-> d -p-> e -p-> f."""
    names = ["a", "b", "c", "d", "e", "f"]
    edges = [(_e(src), P, _e(dst)) for src, dst in zip(names, names[1:], strict=False)]
    return _dataset(*edges, *_labelled(*names))


async def _traverse(dataset: Dataset, options: dict, graph: _RdflibGraph | None = None) -> dict:
    """Call the public graphTraverse handler with a real traverser over ``dataset``."""
    traverser = GraphTraverser(graph or _RdflibGraph(dataset), graph_uri_template=TEMPLATE)
    orchestrator = MagicMock()
    orchestrator._knowledge_retriever._graph = traverser
    with patch("coa_serve.main._orchestrator", orchestrator):
        response = await _handle_graph_traverse({"namespace": "demo", "options": options}, "request-1")
    assert response["statusCode"] == 200, response
    return response


def _uris(response: dict) -> list[str]:
    return [entity["uri"] for entity in response["entities"]]


def _edges(response: dict) -> set[tuple[str, str, str]]:
    return {(r["sourceUri"], r["predicateUri"], r["targetUri"]) for r in response["relationships"]}


@pytest.fixture(autouse=True)
def _untagged_first():
    bind_label_preference(LabelPreference.untagged_first())


@pytest.mark.unit
@pytest.mark.asyncio
class TestProbesThroughThePublicHandler:
    """The behaviour the model documents, observed through ``_handle_graph_traverse``."""

    async def test_max_depth_two_and_five_reach_different_entities(self):
        start = {"startUri": str(_e("a")), "direction": "outgoing"}

        depth_two = await _traverse(_chain(), {**start, "maxDepth": 2})
        depth_five = await _traverse(_chain(), {**start, "maxDepth": 5})

        assert _uris(depth_two) == [str(_e("b")), str(_e("c"))]
        assert _uris(depth_five) == [str(_e(name)) for name in "bcdef"]

    async def test_direction_selects_the_side_that_is_followed(self):
        dataset = _dataset(
            (_e("hub"), P, _e("out")),
            (_e("in"), P, _e("hub")),
            *_labelled("hub", "out", "in"),
        )
        start = {"startUri": str(_e("hub")), "maxDepth": 1}

        outgoing = await _traverse(dataset, {**start, "direction": "outgoing"})
        incoming = await _traverse(dataset, {**start, "direction": "incoming"})
        both = await _traverse(dataset, {**start, "direction": "both"})

        assert _uris(outgoing) == [str(_e("out"))]
        assert _edges(outgoing) == {(str(_e("hub")), str(P), str(_e("out")))}
        assert _uris(incoming) == [str(_e("in"))]
        assert _edges(incoming) == {(str(_e("in")), str(P), str(_e("hub")))}
        assert sorted(_uris(both)) == sorted([str(_e("in")), str(_e("out"))])

    async def test_relationship_filter_follows_only_the_listed_predicates(self):
        dataset = _dataset(
            (_e("hub"), P, _e("via-p")),
            (_e("hub"), Q, _e("via-q")),
            *_labelled("hub", "via-p", "via-q"),
        )
        start = {"startUri": str(_e("hub")), "maxDepth": 1}

        filtered = await _traverse(dataset, {**start, "relationshipFilter": [str(P)]})
        unfiltered = await _traverse(dataset, start)

        assert _uris(filtered) == [str(_e("via-p"))]
        assert {predicate for _, predicate, _ in _edges(filtered)} == {str(P)}
        assert sorted(_uris(unfiltered)) == sorted([str(_e("via-p")), str(_e("via-q"))])

    async def test_the_same_request_returns_the_same_entities_every_time(self):
        """Without ORDER BY the store's row order picks which entities survive the cut."""
        neighbours = [f"n{index}" for index in range(6)]
        dataset = _dataset(*[(_e("hub"), P, _e(name)) for name in neighbours], *_labelled("hub", *neighbours))
        request = {"startUri": str(_e("hub")), "maxDepth": 1, "maxResults": 3}
        store = _RdflibGraph(dataset)

        first = await _traverse(dataset, request, store)
        second = await _traverse(dataset, request, store)

        assert _uris(first) == _uris(second)
        assert _uris(first) == [str(_e(name)) for name in neighbours[:3]]

    async def test_labels_follow_the_namespace_language(self):
        """Not the alphabetically smallest label: ``Apple`` sorts before ``りんご``."""
        dataset = _dataset(
            (_e("hub"), P, _e("fruit")),
            (_e("fruit"), RDFS.label, Literal("Apple", lang="en")),
            (_e("fruit"), RDFS.label, Literal("りんご", lang="ja")),
            *_labelled("hub"),
        )
        bind_label_preference(LabelPreference.for_namespace("ja"))

        response = await _traverse(dataset, {"startUri": str(_e("hub")), "maxDepth": 1})

        assert [entity["label"] for entity in response["entities"]] == ["りんご"]


def _traverser(dataset: Dataset) -> tuple[GraphTraverser, _RdflibGraph]:
    graph = _RdflibGraph(dataset)
    return GraphTraverser(graph, graph_uri_template=TEMPLATE), graph


@pytest.mark.unit
@pytest.mark.asyncio
class TestTraverseGraph:
    @pytest.mark.parametrize("depth", [1, 2, 3, 4, 5])
    async def test_each_depth_reaches_exactly_that_far(self, depth):
        traverser, _ = _traverser(_chain())

        entities = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=depth, direction="outgoing")

        assert [entity.uri for entity in entities] == [str(_e(name)) for name in "bcdef"[:depth]]

    @pytest.mark.parametrize(
        ("direction", "expected"),
        [("outgoing", ["d"]), ("incoming", ["b"]), ("both", ["b", "d"])],
    )
    async def test_each_direction_from_the_middle_of_a_chain(self, direction, expected):
        traverser, _ = _traverser(_chain())

        entities = await traverser.traverse_graph(str(_e("c")), "demo", max_hops=1, direction=direction)

        assert [entity.uri for entity in entities] == [str(_e(name)) for name in expected]

    async def test_direction_holds_on_every_hop_not_only_the_first(self):
        """``incoming`` from ``c`` at depth 2 must not turn around at ``b``."""
        traverser, _ = _traverser(_chain())

        entities = await traverser.traverse_graph(str(_e("c")), "demo", max_hops=2, direction="incoming")

        assert [entity.uri for entity in entities] == [str(_e("b")), str(_e("a"))]

    async def test_the_filter_applies_on_every_hop(self):
        dataset = _dataset(
            (_e("a"), P, _e("b")),
            (_e("b"), Q, _e("c")),
            (_e("b"), P, _e("d")),
            *_labelled("a", "b", "c", "d"),
        )
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(
            str(_e("a")), "demo", max_hops=2, direction="outgoing", relationship_filter=(str(P),)
        )

        assert [entity.uri for entity in entities] == [str(_e("b")), str(_e("d"))]

    async def test_without_a_filter_type_and_label_are_not_followed(self):
        klass = URIRef("urn:class:Thing")
        dataset = _dataset((_e("a"), RDF.type, klass), (_e("a"), P, _e("b")), *_labelled("a", "b"))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1)

        assert [entity.uri for entity in entities] == [str(_e("b"))]
        assert entities[0].type == ""

    async def test_a_filter_with_no_usable_iri_follows_nothing_and_queries_nothing(self):
        """``FilterValueList`` accepts any bounded string; a prefixed name cannot be interpolated."""
        traverser, graph = _traverser(_chain())

        entities = await traverser.traverse_graph(
            str(_e("a")), "demo", max_hops=2, relationship_filter=("rdfs:label", "x> } UNION { ?s ?p ?o")
        )

        assert entities == []
        assert graph.queries == []

    async def test_only_usable_filter_values_reach_the_query(self):
        traverser, graph = _traverser(_chain())

        await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1, relationship_filter=("rdfs:label", str(P)))

        edge_queries = [query for query in graph.queries if "?neighbour" in query]
        assert edge_queries
        assert all(f"FILTER(?predicate IN (<{P}>))" in query for query in edge_queries)
        assert all("rdfs:label>" not in query for query in edge_queries)

    async def test_max_results_bounds_the_discovered_entities_in_a_stable_order(self):
        neighbours = [f"n{index}" for index in range(5)]
        dataset = _dataset(*[(_e("hub"), P, _e(name)) for name in reversed(neighbours)], *_labelled(*neighbours))
        traverser, _ = _traverser(dataset)

        runs = [await traverser.traverse_graph(str(_e("hub")), "demo", max_hops=2, max_results=2) for _ in range(3)]

        assert all([entity.uri for entity in run] == [str(_e("n0")), str(_e("n1"))] for run in runs)

    async def test_every_limited_query_is_ordered_first(self):
        traverser, graph = _traverser(_chain())

        await traverser.traverse_graph(str(_e("a")), "demo", max_hops=3)

        for query in graph.queries[1:]:
            assert query.index("ORDER BY") < query.index("LIMIT")

    async def test_relationships_carry_directed_provenance_and_predicate_labels(self):
        dataset = _dataset(
            (_e("a"), P, _e("b")),
            (P, RDFS.label, Literal("links to")),
            *_labelled("a", "b"),
        )
        traverser, _ = _traverser(dataset)

        (entity,) = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1)

        assert entity.label == "B"
        assert entity.relationships == (
            {
                "source_uri": str(_e("a")),
                "source_label": "A",
                "predicate_uri": str(P),
                "predicate_label": "links to",
                "target_uri": str(_e("b")),
                "target_label": "B",
            },
        )

    async def test_no_query_after_resolution_is_unbound_by_graph(self):
        """The ``GRAPH ?g`` + ``STRSTARTS`` form pinned Neptune's CPU at 99% (~30 namespaces)."""
        graphs = [URIRef(f"{PREFIX}graph-{index:02d}") for index in range(9)]
        dataset = Dataset()
        for graph_iri in graphs:
            dataset.graph(graph_iri).add((URIRef(f"{graph_iri}#onto"), RDF.type, OWL.Ontology))
        dataset.graph(graphs[-1]).add((_e("a"), P, _e("b")))
        traverser, graph = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1)

        # The edge lives in the ninth graph, past graph_scoped_body's inlining limit.
        assert [entity.uri for entity in entities] == [str(_e("b"))]
        for query in graph.queries[1:]:
            # The vocabulary filter uses STRSTARTS on ?neighbour; only a graph-scoped one is the cluster-wide form.
            assert not re.search(r"STRSTARTS\(\s*STR\(\s*\?g\b", query)
            assert "GRAPH ?" not in query

    async def test_another_namespace_is_never_read(self):
        dataset = _chain()
        other = dataset.graph(URIRef("https://ontology-workbench.local/demo-two/induced"))
        other.add((URIRef("https://ontology-workbench.local/demo-two/induced#onto"), RDF.type, OWL.Ontology))
        other.add((_e("a"), P, _e("leak")))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1, direction="outgoing")

        assert [entity.uri for entity in entities] == [str(_e("b"))]

    async def test_the_governed_metrics_graph_is_read_unless_hidden(self):
        """The metric service writes that graph without an ``owl:Ontology`` subject."""
        dataset = _chain()
        dataset.graph(URIRef(f"{PREFIX}{_GOVERNED_GRAPH_SEGMENT}")).add((_e("a"), P, _e("metric")))
        traverser, _ = _traverser(dataset)

        shown = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1, direction="outgoing")
        hidden = await traverser.traverse_graph(
            str(_e("a")), "demo", max_hops=1, direction="outgoing", hide_governed=True
        )

        assert [entity.uri for entity in shown] == [str(_e("b")), str(_e("metric"))]
        assert [entity.uri for entity in hidden] == [str(_e("b"))]

    async def test_a_namespace_with_no_published_graph_returns_nothing(self):
        traverser, graph = _traverser(Dataset())

        assert await traverser.traverse_graph(str(_e("a")), "demo", hide_governed=True) == []
        assert len(graph.queries) == 1

    async def test_direction_is_not_widened_on_a_later_hop(self):
        """``outgoing`` from ``a``: ``x -> b`` points into ``b`` and must not be followed."""
        dataset = _dataset((_e("a"), P, _e("b")), (_e("x"), P, _e("b")), *_labelled("a", "b", "x"))
        traverser, _ = _traverser(dataset)

        outgoing = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=2, direction="outgoing")
        incoming = await traverser.traverse_graph(str(_e("b")), "demo", max_hops=2, direction="incoming")
        both = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=2, direction="both")

        assert [entity.uri for entity in outgoing] == [str(_e("b"))]
        # Incoming from b reaches a and x; neither has an incoming edge, and b's
        # outgoing side (none here) is never looked at.
        assert [entity.uri for entity in incoming] == [str(_e("a")), str(_e("x"))]
        assert [entity.uri for entity in both] == [str(_e("b")), str(_e("x"))]

    async def test_incoming_does_not_follow_an_outgoing_edge_on_a_later_hop(self):
        dataset = _dataset((_e("a"), P, _e("b")), (_e("a"), P, _e("y")), *_labelled("a", "b", "y"))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("b")), "demo", max_hops=2, direction="incoming")

        assert [entity.uri for entity in entities] == [str(_e("a"))]

    async def test_order_decides_which_entities_survive_a_limit(self):
        """60 neighbours and ``maxResults: 1`` put the edge query's LIMIT at 50.

        Without ORDER BY the fake store returns the reversed rows on the second
        unordered query, and the LIMIT keeps n59..n10.
        """
        neighbours = [f"n{index:02d}" for index in range(60)]
        dataset = _dataset(*[(_e("hub"), P, _e(name)) for name in neighbours], *_labelled(*neighbours))
        traverser, _ = _traverser(dataset)

        runs = [await traverser.traverse_graph(str(_e("hub")), "demo", max_hops=1, max_results=1) for _ in range(2)]

        assert [[entity.uri for entity in run] for run in runs] == [[str(_e("n00"))], [str(_e("n00"))]]

    async def test_a_cycle_closing_edge_is_reported_under_the_last_entity(self):
        dataset = _dataset(
            (_e("a"), P, _e("b")), (_e("b"), P, _e("c")), (_e("c"), P, _e("a")), *_labelled("a", "b", "c")
        )
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=3, direction="outgoing")

        held = {entity.uri: [(r["source_uri"], r["target_uri"]) for r in entity.relationships] for entity in entities}
        assert held == {
            str(_e("b")): [(str(_e("a")), str(_e("b")))],
            str(_e("c")): [(str(_e("b")), str(_e("c"))), (str(_e("c")), str(_e("a")))],
        }

    async def test_edges_to_entities_cut_by_max_results_are_not_returned(self):
        neighbours = [f"n{index}" for index in range(5)]
        dataset = _dataset(*[(_e("hub"), P, _e(name)) for name in neighbours], *_labelled(*neighbours))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("hub")), "demo", max_hops=2, max_results=2)

        returned = {entity.uri for entity in entities}
        assert returned == {str(_e("n0")), str(_e("n1"))}
        for entity in entities:
            for relationship in entity.relationships:
                assert {relationship["source_uri"], relationship["target_uri"]} <= returned | {str(_e("hub"))}

    @pytest.mark.parametrize(("max_results", "cap"), [(1, 50), (2, 50), (3, 60), (250, 5000), (1000, 5000)])
    async def test_the_relationship_cap_is_twenty_per_entity_between_50_and_5000(self, max_results, cap):
        assert _relationship_cap(max_results) == cap

    async def test_the_relationship_floor_stops_the_walk_at_50(self):
        predicates = [URIRef(f"urn:predicate:p{index:02d}") for index in range(60)]
        dataset = _dataset(*[(_e("hub"), predicate, _e("n0")) for predicate in predicates], (_e("n0"), P, _e("n1")))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("hub")), "demo", max_hops=2, max_results=1)

        assert [(entity.uri, len(entity.relationships)) for entity in entities] == [(str(_e("n0")), 50)]

    async def test_the_relationship_ceiling_stops_the_walk_at_5000(self):
        predicates = [URIRef(f"urn:predicate:p{index:04d}") for index in range(5001)]
        dataset = _dataset(*[(_e("hub"), predicate, _e("n0")) for predicate in predicates], (_e("n0"), P, _e("n1")))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("hub")), "demo", max_hops=2, max_results=1000)

        assert [(entity.uri, len(entity.relationships)) for entity in entities] == [(str(_e("n0")), 5000)]

    async def test_an_entity_reached_only_after_the_relationship_cap_is_not_returned(self):
        """Each graph group has its own LIMIT, so rows beyond the cap can still arrive.

        n0's 50 edges, from the first group, fill the cap for ``max_results: 2``; n1's
        edge, from the ninth graph, comes after it, so n1 has no edge and is not returned.
        """
        graphs = [URIRef(f"{PREFIX}graph-{index:02d}") for index in range(9)]
        dataset = Dataset()
        for graph_iri in graphs:
            dataset.graph(graph_iri).add((URIRef(f"{graph_iri}#onto"), RDF.type, OWL.Ontology))
        for index in range(50):
            dataset.graph(graphs[0]).add((_e("hub"), URIRef(f"urn:predicate:p{index:02d}"), _e("n0")))
        dataset.graph(graphs[-1]).add((_e("hub"), P, _e("n1")))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("hub")), "demo", max_hops=1, max_results=2, hide_governed=True)

        assert [(entity.uri, len(entity.relationships)) for entity in entities] == [(str(_e("n0")), 50)]

    async def test_edges_and_labels_from_different_graph_groups_are_merged(self):
        graphs = [URIRef(f"{PREFIX}graph-{index:02d}") for index in range(9)]
        dataset = Dataset()
        for graph_iri in graphs:
            dataset.graph(graph_iri).add((URIRef(f"{graph_iri}#onto"), RDF.type, OWL.Ontology))
        dataset.graph(graphs[0]).add((_e("a"), P, _e("b")))
        dataset.graph(graphs[-1]).add((_e("a"), Q, _e("c")))
        dataset.graph(graphs[-1]).add((_e("b"), RDFS.label, Literal("B from the ninth graph")))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1, hide_governed=True)

        assert [(entity.uri, entity.label) for entity in entities] == [
            (str(_e("b")), "B from the ninth graph"),
            (str(_e("c")), ""),
        ]

    async def test_a_filter_value_with_a_control_character_matches_nothing(self):
        traverser, graph = _traverser(_chain())

        nothing = await traverser.traverse_graph(str(_e("a")), "demo", relationship_filter=("urn:predicate:p\x01",))
        only_p = await traverser.traverse_graph(
            str(_e("a")), "demo", max_hops=1, relationship_filter=("urn:predicate:p\x01", str(P))
        )

        assert nothing == []
        assert [entity.uri for entity in only_p] == [str(_e("b"))]
        assert all("\x01" not in query for query in graph.queries)

    async def test_a_stored_predicate_that_is_not_a_safe_iri_is_not_interpolated(self):
        odd = URIRef("urn:p>x")
        dataset = _dataset((_e("a"), odd, _e("b")), *_labelled("a", "b"))
        traverser, graph = _traverser(dataset)

        (entity,) = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1)

        assert entity.relationships[0]["predicate_uri"] == str(odd)
        assert entity.relationships[0]["predicate_label"] == ""
        assert all("<urn:p>x>" not in query for query in graph.queries)

    async def test_a_budget_that_runs_out_mid_traversal_is_a_timeout(self):
        """Depths 1 and 2 finish, depth 3 has no budget left: no partial answer."""
        traverser, _ = _traverser(_chain())

        with (
            patch.object(Deadline, "remaining_s", side_effect=[20.0, 20.0, 20.0, 0.5]),
            pytest.raises(TimeoutError, match="edge query at depth 3"),
        ):
            await traverser.traverse_graph(str(_e("a")), "demo", max_hops=5, direction="outgoing")

    async def test_an_exhausted_budget_is_a_timeout_not_a_partial_answer(self):
        traverser, _ = _traverser(_chain())

        with (
            patch.object(Deadline, "remaining_s", return_value=0.5),
            pytest.raises(TimeoutError, match="budget exhausted"),
        ):
            await traverser.traverse_graph(str(_e("a")), "demo", max_hops=5)

    async def test_the_handler_reports_an_exhausted_budget_as_unavailable(self):
        with (
            patch.object(Deadline, "remaining_s", return_value=0.5),
            pytest.raises(DataSourceUnavailableError, match="timed out"),
        ):
            await _traverse(_chain(), {"startUri": str(_e("a")), "maxDepth": 5})

    async def test_a_datatype_is_not_a_hub_between_unrelated_tables(self):
        """The inducer's shape: every column has ``rdfs:range xsd:string``.

        From a class with ``both``, depth 3 must not reach the string columns of
        another table through ``xsd:string``.
        """
        customer, order = URIRef("urn:class:Customer"), URIRef("urn:class:Order")
        name, email, status = URIRef("urn:col:name"), URIRef("urn:col:email"), URIRef("urn:col:status")
        dataset = _dataset(
            (customer, RDF.type, OWL.Class),
            (order, RDF.type, OWL.Class),
            (name, RDFS.domain, customer),
            (email, RDFS.domain, customer),
            (status, RDFS.domain, order),
            *[(column, RDFS.range, XSD.string) for column in (name, email, status)],
        )
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(str(customer), "demo", max_hops=3, direction="both")

        assert [entity.uri for entity in entities] == [str(email), str(name)]

    async def test_a_vocabulary_term_is_not_visited_even_through_a_listed_predicate(self):
        klass = URIRef("urn:class:Thing")
        dataset = _dataset((klass, RDFS.subClassOf, OWL.Thing), (klass, RDFS.subClassOf, _e("parent")))
        traverser, _ = _traverser(dataset)

        entities = await traverser.traverse_graph(
            str(klass), "demo", max_hops=2, direction="outgoing", relationship_filter=(str(RDFS.subClassOf),)
        )

        assert [entity.uri for entity in entities] == [str(_e("parent"))]

    async def test_a_frontier_larger_than_one_query_is_expanded_in_full(self):
        """60 entities at depth 1, each with one child: all 60 children are reached at depth 2."""
        hubs = [f"n{index:02d}" for index in range(60)]
        dataset = _dataset(
            *[(_e("root"), P, _e(hub)) for hub in hubs],
            *[(_e(hub), P, _e(f"{hub}-child")) for hub in hubs],
        )
        traverser, graph = _traverser(dataset)

        entities = await traverser.traverse_graph(
            str(_e("root")), "demo", max_hops=2, direction="outgoing", max_results=1000
        )

        uris = [entity.uri for entity in entities]
        assert uris[:60] == [str(_e(hub)) for hub in hubs]
        assert sorted(uris[60:]) == sorted(str(_e(f"{hub}-child")) for hub in hubs)
        edge_queries = [query for query in graph.queries if "?frontier" in query and "VALUES ?frontier" in query]
        assert len(edge_queries) == 3

    @pytest.mark.parametrize("bad", ["urn:entity:a\ud800", "https://example.com/\udfff"])
    async def test_a_lone_surrogate_matches_nothing_instead_of_failing(self, bad):
        traverser, graph = _traverser(_chain())

        assert await traverser.traverse_graph(bad, "demo", max_hops=1) == []
        entities = await traverser.traverse_graph(
            str(_e("a")), "demo", max_hops=1, direction="outgoing", relationship_filter=(bad, str(P))
        )
        assert [entity.uri for entity in entities] == [str(_e("b"))]
        assert all(bad not in query for query in graph.queries)

    async def test_a_namespace_with_more_than_50_graphs_is_refused(self):
        dataset = Dataset()
        for index in range(51):
            graph_iri = URIRef(f"{PREFIX}graph-{index:02d}")
            dataset.graph(graph_iri).add((URIRef(f"{graph_iri}#onto"), RDF.type, OWL.Ontology))
        traverser, graph = _traverser(dataset)

        with pytest.raises(RuntimeError, match="more than 50 ontology graphs"):
            await traverser.traverse_graph(str(_e("a")), "demo", max_hops=1)
        assert len(graph.queries) == 1
        with pytest.raises(DataSourceUnavailableError, match="Graph traversal failed"):
            await _traverse(dataset, {"startUri": str(_e("a")), "maxDepth": 1})

    async def test_labels_past_the_metadata_row_cap_are_empty_and_logged(self):
        traverser, _ = _traverser(_chain())

        with patch("coa_serve.tier3.graph_traverser._MAX_METADATA_ROWS", 2), capture_logs() as logs:
            entities = await traverser.traverse_graph(str(_e("a")), "demo", max_hops=2, direction="outgoing")

        assert [(entity.uri, entity.label) for entity in entities] == [(str(_e("b")), "B"), (str(_e("c")), "")]
        assert [log["limit"] for log in logs if log["event"] == "graph_traverse_label_rows_capped"] == [2]
