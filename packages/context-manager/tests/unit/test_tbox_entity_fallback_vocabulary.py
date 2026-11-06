# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Entity-fallback class matching over skos:prefLabel and skos:altLabel.

Runs the generated SPARQL on an in-memory rdflib dataset, so property paths,
language-tagged literals and the mapped/backend gates are evaluated by a real
SPARQL engine instead of being matched as strings.
"""

from __future__ import annotations

from typing import Any

import pytest
from coa_common.constants import VOCAB_URI, StructuredBackend
from coa_common.label_language import LabelPreference
from coa_serve import namespace_language
from coa_serve.query_utils import QUERY_BACKEND_IRI
from coa_serve.tier2.ontop.tbox_context import TBoxContextBuilder
from rdflib import RDF, RDFS, Dataset, Literal, Namespace, URIRef
from rdflib.namespace import OWL, SKOS

pytestmark = pytest.mark.unit

EX = Namespace("https://example.org/ontology#")
IS_MAPPED = URIRef(f"{VOCAB_URI}isMapped")


class _RdfGraphClient:
    def __init__(self, dataset: Dataset):
        self._dataset = dataset

    async def query(self, sparql: str, max_results: int | None = None) -> list[dict[str, Any]]:
        rows = [{str(k): str(v) for k, v in row.asdict().items()} for row in self._dataset.query(sparql)]
        return rows[:max_results] if max_results is not None else rows


@pytest.fixture
def dataset() -> Dataset:
    return Dataset()


@pytest.fixture
def graph(dataset: Dataset):
    return dataset.graph(URIRef("https://test.local/ns/kg"))


@pytest.fixture
def builder(dataset: Dataset) -> TBoxContextBuilder:
    return TBoxContextBuilder(_RdfGraphClient(dataset), graph_uri_template="https://test.local/{namespace}")


@pytest.fixture
def ja_namespace():
    namespace_language.bind_label_preference(LabelPreference.for_namespace("ja"))
    yield
    namespace_language.bind_label_preference(LabelPreference.untagged_first())


def _mapped_class(graph, uri: URIRef, label: Literal) -> None:
    graph.add((uri, RDF.type, OWL.Class))
    graph.add((uri, IS_MAPPED, Literal(True)))
    graph.add((uri, RDFS.label, label))


def _uri_label_pairs(classes: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(c["uri"], c["label"]) for c in classes]


@pytest.mark.usefixtures("ja_namespace")
class TestSynonymsFindTheirClass:
    async def test_synonym_only_word_finds_its_class(self, graph, builder) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("解約", lang="ja"))
        graph.add((EX.Kaiyaku, SKOS.altLabel, Literal("顧客離脱", lang="ja")))

        classes, _ = await builder._fetch_by_entities("顧客離脱は何件ありますか", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Kaiyaku), "解約")]

    async def test_preferred_label_finds_its_class(self, graph, builder) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("kaiyaku"))
        graph.add((EX.Kaiyaku, SKOS.prefLabel, Literal("解約", lang="ja")))

        classes, _ = await builder._fetch_by_entities("解約の件数を教えて", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Kaiyaku), "kaiyaku")]

    async def test_synonym_in_another_language_finds_its_class(self, graph, builder) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("解約", lang="ja"))
        graph.add((EX.Kaiyaku, SKOS.altLabel, Literal("Customer attrition", lang="en")))

        classes, _ = await builder._fetch_by_entities("customer attrition の件数を教えて", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Kaiyaku), "解約")]

    async def test_any_of_many_synonyms_finds_its_class(self, graph, builder) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("解約", lang="ja"))
        for index in range(24):
            graph.add((EX.Kaiyaku, SKOS.altLabel, Literal(f"別名{index:02d}", lang="ja")))
        graph.add((EX.Kaiyaku, SKOS.altLabel, Literal("顧客離脱", lang="ja")))

        classes, _ = await builder._fetch_by_entities("顧客離脱は何件ありますか", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Kaiyaku), "解約")]

    async def test_class_with_labels_in_two_languages_is_returned_once_under_the_namespace_language(
        self, graph, builder
    ) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("解約", lang="ja"))
        graph.add((EX.Kaiyaku, RDFS.label, Literal("Churn", lang="en")))
        graph.add((EX.Kaiyaku, SKOS.altLabel, Literal("顧客離脱", lang="ja")))

        classes, _ = await builder._fetch_by_entities("顧客離脱は何件ありますか", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Kaiyaku), "解約")]

    async def test_long_synonym_is_matched_by_a_word_it_contains(self, graph, builder) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("解約", lang="ja"))
        graph.add((EX.Kaiyaku, SKOS.altLabel, Literal("契約期間の途中で発生した顧客離脱" + "あ" * 300, lang="ja")))

        classes, _ = await builder._fetch_by_entities("顧客離脱は何件ありますか", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Kaiyaku), "解約")]


@pytest.mark.usefixtures("ja_namespace")
class TestSynonymsDoNotWidenTheGates:
    async def test_one_character_synonym_does_not_match_every_question(self, graph, builder) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("解約", lang="ja"))
        graph.add((EX.Kaiyaku, SKOS.altLabel, Literal("解", lang="ja")))

        classes, _ = await builder._fetch_by_entities("解決済みの件数", "ns")

        assert classes == []

    async def test_unmapped_class_synonym_is_not_returned(self, graph, builder) -> None:
        graph.add((EX.Churn, RDF.type, OWL.Class))
        graph.add((EX.Churn, RDFS.label, Literal("解約", lang="ja")))
        graph.add((EX.Churn, SKOS.altLabel, Literal("顧客離脱", lang="ja")))

        classes, _ = await builder._fetch_by_entities("顧客離脱は何件ありますか", "ns")

        assert classes == []

    async def test_graph_backend_class_synonym_is_not_returned_for_sql(self, graph, builder) -> None:
        _mapped_class(graph, EX.Kaiyaku, Literal("解約", lang="ja"))
        graph.add((EX.Kaiyaku, SKOS.altLabel, Literal("顧客離脱", lang="ja")))
        graph.add((EX.Kaiyaku, URIRef(QUERY_BACKEND_IRI), Literal(StructuredBackend.RDF_SPARQL.value)))

        classes, _ = await builder._fetch_by_entities("顧客離脱は何件ありますか", "ns", backend=StructuredBackend.SQL)

        assert classes == []


class _RecordingGraphClient:
    """Returns ``class_rows`` for the class query, in that order, and records later queries."""

    def __init__(self, class_rows: list[dict[str, str]]):
        self._class_rows = class_rows
        self.queries: list[str] = []

    async def query(self, sparql: str, max_results: int | None = None) -> list[dict[str, Any]]:
        self.queries.append(sparql)
        return self._class_rows if len(self.queries) == 1 else []


@pytest.mark.usefixtures("ja_namespace")
class TestLabelMatchesKeepTheirColumns:
    async def test_label_match_takes_a_values_slot_before_synonym_only_matches(self) -> None:
        # Neptune returns the class rows unordered; here the 60 synonym-only matches come first.
        rows = [{"class": str(EX[f"Other{i:02d}"]), "label": f"その他{i:02d}", "labelLang": "ja"} for i in range(60)]
        rows.append({"class": str(EX.Churn), "label": "顧客離脱", "labelLang": "ja"})
        client = _RecordingGraphClient(rows)
        builder = TBoxContextBuilder(client, graph_uri_template="https://test.local/{namespace}")

        classes, _ = await builder._fetch_by_entities("顧客離脱は何件ありますか", "ns")

        assert len(classes) == 61
        property_query = client.queries[1]
        assert f"(<{EX.Churn}>)" in property_query
        assert property_query.count("(<https://example.org/ontology#Other") == 49


class TestLabelMatchesAreUnchanged:
    async def test_class_matched_by_label_and_synonym_is_returned_once(self, graph, builder) -> None:
        _mapped_class(graph, EX.Orders, Literal("orders"))
        graph.add((EX.Orders, SKOS.altLabel, Literal("purchase orders")))

        classes, _ = await builder._fetch_by_entities("how many purchase orders", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Orders), "orders")]

    async def test_label_only_class_is_still_found(self, graph, builder) -> None:
        _mapped_class(graph, EX.Orders, Literal("orders"))

        classes, _ = await builder._fetch_by_entities("how many orders", "ns")

        assert _uri_label_pairs(classes) == [(str(EX.Orders), "orders")]
