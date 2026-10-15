# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace-language label selection in the catalog graph and schema readers.

The routers read the namespace's immutable language server-side (LLD §2.2.5) and
the Neptune store names each resource with it: the tag COA asserts for that
language, then untagged, then any other literal in lexical order. Resources are
grouped by URI and never dropped for their language. The store queries are
mocked at ``_sparql_query``; bindings carry ``xml:lang`` as Neptune returns it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.label_language import LabelPreference
from coa_ontology.catalog import namespace_language as ns_language
from coa_ontology.main import app
from coa_ontology.stores import neptune_db_graph as ndb
from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore
from fastapi import HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

NS = "test-ns"
NS_ID = "11111111-2222-3333-4444-555555555555"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_OWL = "http://www.w3.org/2002/07/owl#"
EQUIPMENT = "https://example.org/onto#Equipment"
LEGACY = "https://example.org/onto#Legacy"
GERMAN = "https://example.org/onto#Anlage"

JA = LabelPreference.for_namespace("ja")
EN = LabelPreference.for_namespace("en")


def _uri(value: str) -> dict:
    return {"type": "uri", "value": value}


def _lit(value: str, lang: str | None = None) -> dict:
    binding = {"type": "literal", "value": value}
    if lang:
        binding["xml:lang"] = lang
    return binding


def _results(bindings: list[dict]) -> dict:
    return {"results": {"bindings": bindings}}


def _store() -> NeptuneDBGraphStore:
    return NeptuneDBGraphStore(endpoint="https://fake:8182", namespace=NS)


# ── store readers ──────────────────────────────────────────────────────────


class TestNamespaceSchema:
    ROWS = [
        {"classUri": _uri(EQUIPMENT), "classLabel": _lit("Equipment"), "classDescription": _lit("A device")},
        {"classUri": _uri(EQUIPMENT), "classLabel": _lit("設備", "ja"), "classDescription": _lit("機器", "ja")},
        {"classUri": _uri(LEGACY), "classLabel": _lit("Legacy")},
        {"classUri": _uri(GERMAN), "classLabel": _lit("Anlage", "de")},
    ]

    @pytest.mark.parametrize(
        ("preference", "label", "description"), [(JA, "設備", "機器"), (EN, "Equipment", "A device")]
    )
    def test_one_class_per_uri_named_by_the_namespace_language(self, preference, label, description) -> None:
        with patch.object(ndb, "_sparql_query", return_value=_results(self.ROWS)):
            schema = _store().get_namespace_schema(NS, include_properties=False, label_preference=preference)
        classes = {c["uri"]: c for c in schema["classes"]}
        assert list(classes) == [EQUIPMENT, LEGACY, GERMAN]
        assert classes[EQUIPMENT]["label"] == label
        assert classes[EQUIPMENT]["description"] == description
        # Legacy untagged-only and differently tagged classes stay visible.
        assert classes[LEGACY]["label"] == "Legacy"
        assert classes[GERMAN]["label"] == "Anlage"

    def test_limit_bounds_classes_not_label_rows(self) -> None:
        """``max_results`` pages classes, so every label of a paged class is read.

        Runs the generated query on an in-memory dataset. Limiting the joined
        rows would let ``"Equipment"`` fill the page and cut ``"設備"@ja`` off.
        """
        import json

        from rdflib import OWL, RDF, RDFS, Dataset, Literal, URIRef

        dataset = Dataset()
        graph = dataset.graph(URIRef(f"{ndb._namespace_graph_prefix(NS)}/onto"))
        equipment, zulu = URIRef(EQUIPMENT), URIRef("https://example.org/onto#Zulu")
        for cls in (equipment, zulu):
            graph.add((cls, RDF.type, OWL.Class))
        graph.add((equipment, RDFS.label, Literal("Equipment")))
        graph.add((equipment, RDFS.label, Literal("設備", lang="ja")))
        graph.add((equipment, RDFS.label, Literal("Gerät", lang="de")))
        graph.add((zulu, RDFS.label, Literal("Zulu")))

        def run(query: str) -> dict:
            return json.loads(dataset.query(query).serialize(format="json"))

        with patch.object(ndb, "_sparql_query", side_effect=run):
            schema = _store().get_namespace_schema(NS, max_results=1, include_properties=False, label_preference=JA)
        assert [(c["uri"], c["label"]) for c in schema["classes"]] == [(EQUIPMENT, "設備")]

    def test_properties_group_by_domain_and_uri(self) -> None:
        prop = "https://example.org/onto#serial"
        prop_rows = [
            {"propUri": _uri(prop), "propLabel": _lit("serial"), "domain": _uri(EQUIPMENT)},
            {"propUri": _uri(prop), "propLabel": _lit("製造番号", "ja"), "domain": _uri(EQUIPMENT)},
        ]
        responses = [_results(self.ROWS), _results(prop_rows)]
        with patch.object(ndb, "_sparql_query", side_effect=lambda _q: responses.pop(0)):
            schema = _store().get_namespace_schema(NS, label_preference=JA)
        equipment = next(c for c in schema["classes"] if c["uri"] == EQUIPMENT)
        assert [p["label"] for p in equipment["properties"]] == ["製造番号"]


class TestSearchEntities:
    def test_hit_label_follows_the_namespace_language_not_row_order(self) -> None:
        rows = [
            {"s": _uri(EQUIPMENT), "label": _lit("Equipment"), "t": _uri(_OWL + "Class")},
            {"s": _uri(EQUIPMENT), "label": _lit("設備", "ja"), "t": _uri(_OWL + "Class")},
            {"s": _uri(LEGACY), "t": _uri(_OWL + "Class")},
        ]
        for ordered in (rows, list(reversed(rows))):
            with patch.object(ndb, "_sparql_query", return_value=_results(ordered)):
                hits = _store().search_entities("*", namespace=NS, label_preference=JA)
            labels = {h["uri"]: h["label"] for h in hits}
            assert labels == {EQUIPMENT: "設備", LEGACY: None}


class TestGetVertex:
    def test_labels_are_ordered_best_first_and_neighbors_named(self) -> None:
        graph = f"{ndb._namespace_graph_prefix(NS)}/x"
        outgoing = _results(
            [
                {"g": _uri(graph), "p": _uri(_RDFS + "label"), "o": _lit("Equipment")},
                {"g": _uri(graph), "p": _uri(_RDFS + "label"), "o": _lit("設備", "ja")},
                {"g": _uri(graph), "p": _uri(_RDFS + "subClassOf"), "o": _uri(LEGACY)},
            ]
        )
        neighbor_labels = _results(
            [
                {"o": _uri(LEGACY), "label": _lit("Legacy asset")},
                {"o": _uri(LEGACY), "label": _lit("旧資産", "ja")},
            ]
        )
        responses = [outgoing, _results([]), neighbor_labels, _results([])]
        with patch.object(ndb, "_sparql_query", side_effect=lambda _q: responses.pop(0)):
            vertex = _store().get_vertex(EQUIPMENT, namespace=NS, label_preference=JA)
        assert vertex["labels"] == ["設備", "Equipment"]
        assert vertex["edges"][0]["neighbor"]["label"] == "旧資産"


class TestOntologyOverview:
    def test_class_and_property_labels_follow_the_namespace_language(self) -> None:
        class_rows = _results(
            [
                {"c": _uri(EQUIPMENT), "label": _lit("Equipment")},
                {"c": _uri(EQUIPMENT), "label": _lit("設備", "ja"), "comment": _lit("機器", "ja")},
                {"c": _uri(LEGACY), "label": _lit("Legacy")},
            ]
        )
        prop = "https://example.org/onto#serial"
        prop_rows = _results(
            [
                {"p": _uri(prop), "type": _uri(_OWL + "DatatypeProperty"), "label": _lit("serial")},
                {"p": _uri(prop), "type": _uri(_OWL + "DatatypeProperty"), "label": _lit("製造番号", "ja")},
            ]
        )
        responses = [class_rows, prop_rows]
        with patch.object(ndb, "_sparql_query", side_effect=lambda _q: responses.pop(0)):
            overview = _store().get_ontology_overview("onto", namespace=NS, label_preference=JA)
        labels = {c["uri"]: (c["label"], c["comment"]) for c in overview["classes"]}
        assert labels == {EQUIPMENT: ("設備", "機器"), LEGACY: ("Legacy", None)}
        assert [p["label"] for p in overview["datatype_properties"]] == ["製造番号"]

    def test_sample_overview_uses_the_same_selection(self) -> None:
        class_rows = _results(
            [{"c": _uri(EQUIPMENT), "label": _lit("Equipment")}, {"c": _uri(EQUIPMENT), "label": _lit("設備", "ja")}]
        )
        with patch.object(ndb, "_sparql_query", return_value=class_rows):
            overview = _store().get_ontology_overview("onto", namespace=NS, sample=True, label_preference=JA)
        assert [c["label"] for c in overview["classes"]] == ["設備"]


# ── namespace language resolution ──────────────────────────────────────────


@pytest.fixture
def namespaces_table(monkeypatch):
    """A mocked namespaces table behind ``NAMESPACES_TABLE``; cache cleared."""
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    ns_language._language_cache.clear()
    table = MagicMock()
    with (
        patch.object(ns_language, "_namespaces_table", return_value=table),
        patch.object(ns_language, "resolve_namespace_id", return_value=NS_ID),
    ):
        yield table
    ns_language._language_cache.clear()


class TestNamespaceLanguage:
    def test_reads_the_stored_language(self, namespaces_table) -> None:
        namespaces_table.get_item.return_value = {"Item": {"language": "ja"}}
        assert ns_language.label_preference_for(NS) == JA
        namespaces_table.get_item.assert_called_once_with(Key={"PK": f"NS#{NS_ID}", "SK": "METADATA"})

    def test_legacy_item_reads_as_english(self, namespaces_table) -> None:
        namespaces_table.get_item.return_value = {"Item": {"name": NS}}
        assert ns_language.namespace_language(NS) == "en"

    def test_language_is_cached(self, namespaces_table) -> None:
        namespaces_table.get_item.return_value = {"Item": {"language": "ja"}}
        ns_language.namespace_language(NS)
        ns_language.namespace_language(NS)
        assert namespaces_table.get_item.call_count == 1

    def test_cache_is_keyed_by_the_resolved_uuid(self, namespaces_table) -> None:
        namespaces_table.get_item.return_value = {"Item": {"language": "ja"}}
        ns_language.namespace_language(NS)
        assert list(ns_language._language_cache) == [NS_ID]
        # The same name resolving to a different namespace is read afresh.
        other = "99999999-8888-7777-6666-555555555555"
        namespaces_table.get_item.return_value = {"Item": {"language": "en"}}
        with patch.object(ns_language, "resolve_namespace_id", return_value=other):
            assert ns_language.namespace_language(NS) == "en"

    def test_missing_item_is_404_not_english(self, namespaces_table) -> None:
        namespaces_table.get_item.return_value = {}
        with pytest.raises(HTTPException) as raised:
            ns_language.namespace_language(NS)
        assert raised.value.status_code == 404

    def test_unresolvable_namespace_is_404(self, namespaces_table) -> None:
        with (
            patch.object(ns_language, "resolve_namespace_id", side_effect=ValueError("nope")),
            pytest.raises(HTTPException) as raised,
        ):
            ns_language.namespace_language(NS)
        assert raised.value.status_code == 404

    def test_read_error_is_503_not_english(self, namespaces_table) -> None:
        namespaces_table.get_item.side_effect = RuntimeError("throttled")
        with pytest.raises(HTTPException) as raised:
            ns_language.namespace_language(NS)
        assert raised.value.status_code == 503

    def test_no_table_configured_is_untagged_first(self, monkeypatch) -> None:
        monkeypatch.delenv("NAMESPACES_TABLE", raising=False)
        ns_language._language_cache.clear()
        assert ns_language.label_preference_for(NS) == LabelPreference.untagged_first()


class TestRouters:
    def test_schema_route_passes_the_namespace_preference(self, namespaces_table) -> None:
        namespaces_table.get_item.return_value = {"Item": {"language": "ja"}}
        graph = MagicMock()
        graph.get_namespace_schema.return_value = {"classes": [], "namespace": NS, "ontologyVersion": ""}
        with patch("coa_ontology.catalog.routers.graph.build_stores", return_value=(graph, MagicMock())):
            resp = TestClient(app).get("/graph/schema", params={"namespace": NS})
        assert resp.status_code == 200
        assert graph.get_namespace_schema.call_args.kwargs["label_preference"] == JA

    def test_missing_namespace_fails_closed(self, namespaces_table) -> None:
        namespaces_table.get_item.return_value = {}
        graph = MagicMock()
        with patch("coa_ontology.catalog.routers.graph.build_stores", return_value=(graph, MagicMock())):
            resp = TestClient(app).get("/graph/search", params={"namespace": NS, "q": "x"})
        assert resp.status_code == 404
        graph.search_entities.assert_not_called()
