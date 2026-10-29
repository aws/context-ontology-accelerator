# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""NL->SQL class text names tables and columns by their R2RML physical names (#1287).

When an accepted ontology's ``rdfs:label`` differs from the database identifier
(a curated ontology, or a steward relabelling a class in Japanese), the NL->SQL
prompt used to say ``Table: 受注 | Columns: 合計金額:double`` and the generated SQL
queried a table that does not exist. The R2RML already says which table and column
each class and property map to; these tests pin that the class text uses those
names and keeps the label as a business name.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.catalog.ingest import (
    _accumulate_embeddings,
    _physical_names_from_r2rml,
    _unquote_sql_identifier,
)
from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace
from rdflib.collection import Collection

pytestmark = pytest.mark.unit

NS = Namespace("http://example.org/onto/")
TM = "http://example.org/map/"

R2RML = f"""
@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix ex: <{NS}> .
<{TM}Orders> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"orders\\"" ] ;
    rr:subjectMap [ rr:class ex:Orders ; rr:template "o/{{order_id}}" ] ;
    rr:predicateObjectMap [ rr:predicate ex:totalAmount ; rr:objectMap [ rr:column "\\"total_amount\\"" ] ] ;
    rr:predicateObjectMap [ rr:predicate ex:orderId ; rr:objectMap [ rr:column "\\"order_id\\"" ] ] ;
    rr:predicateObjectMap [ rr:predicate ex:placedBy ;
        rr:objectMap [ rr:parentTriplesMap <{TM}Customers> ;
                       rr:joinCondition [ rr:child "\\"customer_id\\"" ; rr:parent "\\"id\\"" ] ] ] .
<{TM}Customers> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"sales\\".\\"customers\\"" ] ;
    rr:subjectMap [ rr:class ex:Customers ; rr:template "c/{{id}}" ] ;
    rr:predicateObjectMap [ rr:predicateMap [ rr:constant ex:customerName ] ;
                            rr:objectMap [ rr:column "\\"name\\"" ] ] .
<{TM}Derived> a rr:TriplesMap ;
    rr:logicalTable [ rr:sqlQuery "SELECT 1 AS x" ] ;
    rr:subjectMap [ rr:class ex:Derived ; rr:template "d/{{x}}" ] .
"""


def test_unquote_sql_identifier():
    assert _unquote_sql_identifier('"orders"') == "orders"
    assert _unquote_sql_identifier('"sales"."orders"') == "orders"
    assert _unquote_sql_identifier("public.lot") == "lot"
    assert _unquote_sql_identifier('"a.b"') == "a.b"
    assert _unquote_sql_identifier('"say ""hi"""') == 'say "hi"'
    assert _unquote_sql_identifier("受注") == "受注"


def test_physical_names_from_r2rml():
    tables, columns = _physical_names_from_r2rml(R2RML)
    assert tables == {str(NS.Orders): "orders", str(NS.Customers): "customers"}
    assert columns == {
        str(NS.totalAmount): "total_amount",
        str(NS.orderId): "order_id",
        str(NS.customerName): "name",  # rr:predicateMap / rr:constant form
    }
    # rr:sqlQuery is a derived relation, not a table NL->SQL can name.
    assert str(NS.Derived) not in tables


def test_physical_names_skip_ambiguous_mappings():
    two_tables = (
        R2RML
        + f"""
<{TM}Orders2> a rr:TriplesMap ;
    rr:logicalTable [ rr:tableName "\\"orders_archive\\"" ] ;
    rr:subjectMap [ rr:class ex:Orders ] ;
    rr:predicateObjectMap [ rr:predicate ex:totalAmount ; rr:objectMap [ rr:column "\\"amount\\"" ] ] .
"""
    )
    tables, columns = _physical_names_from_r2rml(two_tables)
    assert str(NS.Orders) not in tables
    assert str(NS.totalAmount) not in columns
    assert columns[str(NS.orderId)] == "order_id"


@pytest.mark.parametrize("bad", [None, "", "   ", "this is not turtle {"])
def test_physical_names_fail_soft(bad):
    assert _physical_names_from_r2rml(bad) == ({}, {})


def _graph(labels: dict) -> Graph:
    g = Graph()
    for cls in (NS.Orders, NS.Customers):
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, labels[cls]))
    for prop, rng in ((NS.totalAmount, XSD.double), (NS.orderId, XSD.integer)):
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.domain, NS.Orders))
        g.add((prop, RDFS.range, rng))
        g.add((prop, RDFS.label, labels[prop]))
    g.add((NS.placedBy, RDF.type, OWL.ObjectProperty))
    g.add((NS.placedBy, RDFS.domain, NS.Orders))
    g.add((NS.placedBy, RDFS.range, NS.Customers))
    g.add((NS.placedBy, RDFS.label, Literal("placed by")))
    key = BNode()
    Collection(g, key, [NS.orderId])
    g.add((NS.Orders, OWL.hasKey, key))
    return g


PHYSICAL = {
    NS.Orders: Literal("orders"),
    NS.Customers: Literal("customers"),
    NS.totalAmount: Literal("total_amount"),
    NS.orderId: Literal("order_id"),
}
JAPANESE = {
    NS.Orders: Literal("受注", lang="ja"),
    NS.Customers: Literal("顧客", lang="ja"),
    NS.totalAmount: Literal("合計金額", lang="ja"),
    NS.orderId: Literal("受注番号", lang="ja"),
}


def _texts(g: Graph, physical_names) -> tuple[str, str]:
    """Run _accumulate_embeddings; return the Orders class's (context_text, embedded text)."""
    with patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient") as bedrock_cls:
        bedrock = MagicMock()
        bedrock.model_id = "amazon.titan-embed-text-v2:0"
        bedrock.embed_texts.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
        bedrock_cls.return_value = bedrock
        store = MagicMock()
        _accumulate_embeddings(
            vector_store=store,
            graph=g,
            ontology_id=str(NS),
            classes={NS.Orders, NS.Customers},
            obj_props={NS.placedBy},
            dt_props={NS.totalAmount, NS.orderId},
            namespace="ns",
            class_to_datasource={str(NS.Orders): "ds1", str(NS.Customers): "ds1"},
            physical_names=physical_names,
        )
        items = store.store_embeddings_batch.call_args[0][0]
        embedded = bedrock.embed_texts.call_args[0][0]
    for i, it in enumerate(items):
        if it["entity_uri"] == str(NS.Orders):
            return it["context_text"], it.get("text") or embedded[i]
    raise AssertionError("Orders not indexed")


def test_relabelled_class_text_uses_physical_names():
    context, embed = _texts(_graph(JAPANESE), _physical_names_from_r2rml(R2RML))
    head = context.split(" | ")
    assert head[0] == "Table: orders"
    assert head[1] == "Business name: 受注"
    assert "Primary key: order_id" in context
    assert "placed by->customers" in context
    cols = context.split(" | Columns: ", 1)[1]
    assert "total_amount:double (business name: 合計金額)" in cols
    assert "order_id:integer (business name: 受注番号)" in cols
    assert "合計金額:" not in context and "受注番号:" not in context
    # The embedded text names the same table, so serve's hit map and its context
    # agree on the key, and keeps the business names for retrieval.
    assert embed.startswith("Table: orders | Business name: 受注")
    assert "total_amount (business name: 合計金額)" in embed


def test_labels_equal_to_physical_names_render_as_before():
    """An induced ontology (labels are the physical names) gets byte-identical text."""
    g = _graph(PHYSICAL)
    assert _texts(g, _physical_names_from_r2rml(R2RML)) == _texts(g, None)
    context, _ = _texts(g, None)
    assert "Business name" not in context and "business name" not in context


def test_without_r2rml_the_label_is_used():
    context, _ = _texts(_graph(JAPANESE), None)
    assert context.startswith("Table: 受注 | ")
    assert "合計金額:double" in context


def test_unresolved_entries_fall_back_to_the_label():
    tables, columns = _physical_names_from_r2rml(R2RML)
    columns.pop(str(NS.orderId))
    context, _ = _texts(_graph(JAPANESE), (tables, columns))
    assert "受注番号:integer" in context
    assert "total_amount:double (business name: 合計金額)" in context


def test_table_name_parse_matches_serve_side():
    """Serve reads the name after ``Table: `` up to the first `` | `` (sql_generator)."""
    context, embed = _texts(_graph(JAPANESE), _physical_names_from_r2rml(R2RML))
    for text in (context, embed):
        assert text.split(" | ")[0].replace("Table: ", "").strip().lower() == "orders"
