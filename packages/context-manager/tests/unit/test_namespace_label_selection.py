# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace-language label selection across the serve readers (LLD §2.2.5).

Every reader groups rows by resource URI and picks one display label with the
request's namespace-language preference: the tag COA asserts for that language,
then untagged, then any other literal in lexical order. A language never hides a
resource, and Tier-2 physical identifiers never follow the namespace language.
"""

from __future__ import annotations

import pytest
from coa_common.label_language import LabelPreference
from coa_serve import namespace_language
from coa_serve.tier2.ontop.tbox_context import TBoxContextBuilder
from coa_serve.tier2.tools.graph_tools import OntologyGraphTool
from coa_serve.tier3.agentic.ontology.source import OntologyGraphSource, _build_ontology
from coa_serve.tier3.graph_traverser import GraphTraverser

pytestmark = pytest.mark.unit

NS = "11111111-1111-1111-1111-111111111111"
TEMPLATE = "https://ontology-workbench.local/{namespace}"
BASE = f"https://demo.coa/{NS}/ontology#"


def _iri(name: str) -> str:
    return BASE + name


@pytest.fixture
def bind_language():
    """Bind a namespace language for the test, as the entrypoint gate does."""

    def bind(language: str) -> None:
        namespace_language.bind_label_preference(LabelPreference.for_namespace(language))

    yield bind
    # An async test binds inside its own task; a sync one binds in this context.
    namespace_language.bind_label_preference(LabelPreference.untagged_first())


class _RowsGraph:
    """Graph client stub: returns rows chosen by a substring of the SPARQL."""

    def __init__(self, routes: dict[str, list[dict]]):
        self._routes = routes
        self.calls: list[str] = []

    async def query(self, sparql, *, max_results=10000):
        self.calls.append(sparql)
        for marker, rows in self._routes.items():
            if marker in sparql:
                return rows
        return []


# ── Tier 2 graph tools ─────────────────────────────────────────────────────


_FK_ROWS = [
    # "orders" carries an untagged physical label and a Japanese display label;
    # the FK row repeats once per label combination.
    {
        "op": _iri("orders_customer_id"),
        "domain": _iri("Orders"),
        "domainLabel": "orders",
        "range": _iri("Customers"),
        "rangeLabel": "customers",
        "comment": "Foreign key: orders.customer_id references customers.id",
    },
    {
        "op": _iri("orders_customer_id"),
        "domain": _iri("Orders"),
        "domainLabel": "注文",
        "domainLabelLang": "ja",
        "range": _iri("Customers"),
        "rangeLabel": "customers",
        "comment": "Foreign key: orders.customer_id references customers.id",
    },
    # A legacy table with only a differently tagged label stays reachable.
    {
        "op": _iri("customers_region_id"),
        "domain": _iri("Customers"),
        "domainLabel": "customers",
        "range": _iri("Regions"),
        "rangeLabel": "Regionen",
        "rangeLabelLang": "de",
    },
]

_COLUMN_ROWS = [
    {"class": _iri("Customers"), "col": _iri("customers_name"), "colLabel": "name", "range": "xsd#string"},
    {
        "class": _iri("Customers"),
        "col": _iri("customers_name"),
        "colLabel": "氏名",
        "colLabelLang": "ja",
        "range": "xsd#string",
        "colComment": "顧客の氏名",
        "colCommentLang": "ja",
    },
    {
        "class": _iri("Customers"),
        "col": _iri("customers_name"),
        "colLabel": "name",
        "range": "xsd#string",
        "colComment": "Customer name",
    },
]


def _graph_tool() -> OntologyGraphTool:
    graph = _RowsGraph(
        {
            "?op a owl:ObjectProperty": _FK_ROWS,
            "?col a owl:DatatypeProperty": _COLUMN_ROWS,
            "?class a owl:Class ; rdfs:label ?label": [{"class": _iri("Customers")}],
        }
    )
    return OntologyGraphTool(graph, None, namespace=NS, graph_uri_template=TEMPLATE)


class TestGraphTools:
    async def test_physical_identifiers_do_not_follow_the_namespace_language(self, bind_language) -> None:
        bind_language("ja")
        out = await _graph_tool().expand_from([_iri("Orders")], hops=2)
        by_iri = {node["iri"]: node for node in out}
        customers = by_iri[_iri("Customers")]
        assert customers["table"] == "customers"
        assert "table_label" not in customers
        # One column, named by its physical label, with the Japanese display label
        # and description as hints.
        assert customers["schema"] == "Table: customers | Columns: name:string [label: 氏名] (顧客の氏名)"

    async def test_differently_tagged_legacy_table_stays_visible(self, bind_language) -> None:
        bind_language("ja")
        out = await _graph_tool().expand_from([_iri("Orders")], hops=2)
        regions = next(node for node in out if node["iri"] == _iri("Regions"))
        assert regions["table"] == "Regionen"

    async def test_display_label_is_the_namespace_language(self, bind_language) -> None:
        bind_language("ja")
        result = await _graph_tool().explore_graph("customers", hops=1)
        tables = {node["table"]: node for node in result["tables"]}
        assert tables["orders"]["table_label"] == "注文"

    async def test_english_namespace_output_is_unchanged(self, bind_language) -> None:
        bind_language("en")
        out = await _graph_tool().expand_from([_iri("Orders")], hops=1)
        customers = next(node for node in out if node["iri"] == _iri("Customers"))
        assert "table_label" not in customers
        assert customers["schema"] == "Table: customers | Columns: name:string (Customer name)"


# ── Tier 2 T-Box context ───────────────────────────────────────────────────


def _tbox_rows() -> list[dict]:
    return [
        {"class": _iri("Equipment"), "label": "Equipment", "parentClass": None},
        {"class": _iri("Equipment"), "label": "設備", "labelLang": "ja", "parentClass": None},
        {"class": _iri("Legacy"), "label": "Legacy"},
        {"class": _iri("German"), "label": "Anlage", "labelLang": "de"},
        {"class": _iri("Equipment"), "property": _iri("serial"), "propLabel": "serial", "range": "xsd#string"},
        {
            "class": _iri("Equipment"),
            "property": _iri("serial"),
            "propLabel": "製造番号",
            "propLabelLang": "ja",
            "range": "xsd#string",
        },
    ]


class TestTBoxContext:
    @pytest.mark.parametrize(
        ("language", "equipment", "serial"),
        [("ja", "設備", "製造番号"), ("en", "Equipment", "serial"), ("zh-Hans", "Equipment", "serial")],
    )
    def test_selection_order(self, bind_language, language, equipment, serial) -> None:
        bind_language(language)
        builder = TBoxContextBuilder(graph_client=None, graph_uri_template=TEMPLATE)
        classes, properties = builder._parse_results(_tbox_rows())
        labels = {c["uri"]: c["label"] for c in classes}
        assert labels == {_iri("Equipment"): equipment, _iri("Legacy"): "Legacy", _iri("German"): "Anlage"}
        assert [p["label"] for p in properties] == [serial]

    def test_row_order_does_not_change_the_label(self, bind_language) -> None:
        bind_language("ja")
        builder = TBoxContextBuilder(graph_client=None, graph_uri_template=TEMPLATE)
        forward, _ = builder._parse_results(_tbox_rows())
        backward, _ = builder._parse_results(list(reversed(_tbox_rows())))
        assert {c["uri"]: c["label"] for c in forward} == {c["uri"]: c["label"] for c in backward}

    async def test_object_properties_are_one_edge_per_join_path(self, bind_language) -> None:
        bind_language("ja")
        graph = _RowsGraph({"?op a owl:ObjectProperty": _FK_ROWS})
        builder = TBoxContextBuilder(graph_client=graph, graph_uri_template=TEMPLATE)
        edges = await builder._fetch_object_properties(NS, mapped=False)
        assert [(e["domain_label"], e["range_label"]) for e in edges] == [
            ("注文", "customers"),
            ("customers", "Regionen"),
        ]
        assert "LANG(?domainLabel)" in graph.calls[-1]


# ── Tier 3 ontology source ─────────────────────────────────────────────────


class TestOntologySource:
    NODE_ROWS = [
        {"classUri": _iri("Equipment"), "classLabel": "Equipment"},
        {"classUri": _iri("Equipment"), "classLabel": "設備", "classLabelLang": "ja"},
        {"classUri": _iri("Site"), "classLabel": "Site"},
        {"classUri": _iri("Anlage"), "classLabel": "Anlage", "classLabelLang": "de"},
    ]
    EDGE_ROWS = [
        {
            "propUri": _iri("locatedAt"),
            "propLabel": "located at",
            "domain": _iri("Equipment"),
            "domainLabel": "Equipment",
            "range": _iri("Site"),
            "rangeLabel": "Site",
        },
        {
            "propUri": _iri("locatedAt"),
            "propLabel": "設置場所",
            "propLabelLang": "ja",
            "domain": _iri("Equipment"),
            "domainLabel": "設備",
            "domainLabelLang": "ja",
            "range": _iri("Site"),
            "rangeLabel": "Site",
        },
    ]

    def test_one_node_type_per_class_in_the_namespace_language(self, bind_language) -> None:
        bind_language("ja")
        ontology = _build_ontology(self.NODE_ROWS, self.EDGE_ROWS)
        assert ontology.node_types == ("Anlage", "Site", "設備")
        assert ontology.edge_types == ("設置場所",)
        assert ontology.edge_map == (("設置場所", "設備", "Site"),)

    def test_english_namespace_prefers_untagged(self, bind_language) -> None:
        bind_language("en")
        ontology = _build_ontology(self.NODE_ROWS, self.EDGE_ROWS)
        assert ontology.node_types == ("Anlage", "Equipment", "Site")
        assert ontology.edge_map == (("located at", "Equipment", "Site"),)

    async def test_graph_source_projects_uris_and_tags(self, bind_language) -> None:
        bind_language("ja")
        graph = _RowsGraph({"?classUri a owl:Class": self.NODE_ROWS, "?propUri a owl:ObjectProperty": self.EDGE_ROWS})
        ontology = await OntologyGraphSource(graph, graph_uri_template=TEMPLATE).load(NS)
        assert "設備" in ontology.node_types
        # The owl:Ontology graph-IRI resolve that runs first selects only ?g, so
        # only the class and edge type queries carry labels to project tags for.
        type_queries = [call for call in graph.calls if "a owl:Class" in call or "a owl:ObjectProperty" in call]
        assert len(type_queries) == 2
        assert all("LANG(" in call for call in type_queries)


# ── Tier 3 graph traverser ─────────────────────────────────────────────────


class TestGraphTraverser:
    # Rows carry the directed-provenance columns the query now selects
    # (source/predicate/target URIs + their language-tagged labels). The entity
    # arrives once per label value so the namespace-language preference can pick
    # the display label and each relationship-endpoint label independently of row
    # order.
    ROWS = [
        {
            "entity": _iri("Equipment"),
            "label": "Equipment",
            "type": "owl#Class",
            "sourceUri": _iri("Equipment"),
            "sourceLabel": "Equipment",
            "predicateUri": "rdfs#subClassOf",
            "predicateLabel": "subClassOf",
            "targetUri": _iri("Asset"),
            "targetLabel": "Asset",
        },
        {
            "entity": _iri("Equipment"),
            "label": "設備",
            "labelLang": "ja",
            "type": "owl#Class",
            "sourceUri": _iri("Equipment"),
            "sourceLabel": "設備",
            "sourceLabelLang": "ja",
            "predicateUri": "rdfs#subClassOf",
            "predicateLabel": "subClassOf",
            "targetUri": _iri("Asset"),
            "targetLabel": "資産",
            "targetLabelLang": "ja",
        },
        {"entity": _iri("Legacy"), "label": "Legacy", "type": "owl#Class"},
    ]

    @pytest.mark.parametrize(("language", "label", "target"), [("ja", "設備", "資産"), ("en", "Equipment", "Asset")])
    def test_one_entity_and_relationship_per_uri(self, bind_language, language, label, target) -> None:
        bind_language(language)
        entities = GraphTraverser(graph_client=None, graph_uri_template=TEMPLATE)._parse_results(self.ROWS)
        assert [e.uri for e in entities] == [_iri("Equipment"), _iri("Legacy")]
        assert entities[0].label == label
        assert [r["target_label"] for r in entities[0].relationships] == [target]
        assert [r["target_uri"] for r in entities[0].relationships] == [_iri("Asset")]
        assert entities[1].label == "Legacy"

    def test_hop_results_group_the_same_way(self, bind_language) -> None:
        bind_language("ja")
        entities = GraphTraverser(graph_client=None, graph_uri_template=TEMPLATE)._parse_hop_results(self.ROWS)
        assert entities[0].label == "設備"
        assert [r["target_label"] for r in entities[0].relationships] == ["資産"]


def test_unbound_preference_is_untagged_first() -> None:
    assert namespace_language.current_label_preference() == LabelPreference.untagged_first()


class TestAiContext:
    """``_parse_ai_context`` keeps each distinct aiContext literal once per node."""

    @staticmethod
    def _ctx(synonym: str) -> str:
        return '{"synonyms": ["' + synonym + '"]}'

    def test_distinct_literals_on_one_node_are_all_kept(self, bind_language) -> None:
        bind_language("en")
        rows = [
            {"s": _iri("Equipment"), "label": "Equipment", "aiContext": self._ctx("device")},
            {"s": _iri("Equipment"), "label": "Equipment", "aiContext": self._ctx("unit")},
        ]
        terms = TBoxContextBuilder._parse_ai_context(rows)
        assert sorted(t.synonyms[0] for t in terms) == ["device", "unit"]

    def test_same_literal_repeated_per_label_or_graph_is_one_term(self, bind_language) -> None:
        bind_language("ja")
        rows = [
            {"s": _iri("Equipment"), "label": "Equipment", "aiContext": self._ctx("device")},
            {"s": _iri("Equipment"), "label": "設備", "labelLang": "ja", "aiContext": self._ctx("device")},
            {"s": _iri("Equipment"), "label": "Equipment", "aiContext": self._ctx("device")},
        ]
        terms = TBoxContextBuilder._parse_ai_context(rows)
        assert [(t.label, t.synonyms) for t in terms] == [("設備", ["device"])]

    def test_unparseable_row_does_not_hide_a_later_valid_one(self, bind_language) -> None:
        bind_language("en")
        rows = [
            {"s": _iri("Equipment"), "label": "Equipment", "aiContext": "{not json"},
            {"s": _iri("Equipment"), "label": "Equipment", "aiContext": self._ctx("device")},
        ]
        terms = TBoxContextBuilder._parse_ai_context(rows)
        assert [t.synonyms for t in terms] == [["device"]]
