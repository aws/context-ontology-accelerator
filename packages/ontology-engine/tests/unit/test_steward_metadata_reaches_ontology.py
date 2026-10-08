# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Steward-reviewed metadata reaches the ontology and the NL→SQL context (#1167).

The review screen lets a steward approve a table's and its columns' description,
synonyms, glossary terms, tags, primary key and relationships. Before this change
glossary terms and tags were dropped when the catalog was projected for induction,
a foreign-key column's description never reached the ontology, and the NL→SQL
class text carried neither the class hierarchy nor the primary key that Ontop
already uses. These tests pin each hop:

* catalog → induction input (``_catalog_to_tables``)
* induction → ontology (``table_to_ontology`` and the RIGOR strategy)
* re-accept supersession (glossary terms and tags follow the #1118 rule)
* ontology → NL→SQL context (``_accumulate_embeddings`` class text)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.catalog.ingest import _SUPERSEDED_ANNOTATION_PREDICATES, _accumulate_embeddings
from coa_ontology.induce_catalog import _catalog_to_tables
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogConstraint, CatalogTable
from coa_ontology.inducer.strategies.base import GLOSSARY_TERM, TAG, add_glossary_and_tags, pascal_names_for
from coa_ontology.inducer.strategies.rigor_ontology import RigorOntologyStrategy, _format_schema_context
from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy, _join_comment
from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import SKOS

pytestmark = pytest.mark.unit

PREFIX = "http://example.org/onto/"
NS = Namespace(PREFIX)
SCL = Namespace(VOCAB_URI)


def _literals(g: Graph, subject, predicate) -> set[str]:
    return {str(o) for o in g.objects(subject, predicate)}


# ── catalog → induction input ────────────────────────────────────────────


def _catalog(table_bm: dict, column_bm: dict) -> dict:
    return {
        "databases": [
            {
                "name": "sales",
                "tables": [
                    {
                        "name": "orders",
                        "businessMetadata": table_bm,
                        "primaryKey": {"columns": ["order_id"]},
                        "columns": [
                            {"name": "order_id", "type": "int", "businessMetadata": {}},
                            {"name": "status", "type": "string", "businessMetadata": column_bm},
                        ],
                    }
                ],
            }
        ]
    }


def test_catalog_to_tables_carries_glossary_terms_and_tags():
    """The projection that previously kept only description + synonyms."""
    tables = _catalog_to_tables(
        _catalog(
            {"description": "Orders", "synonyms": ["purchases"], "glossaryTerms": ["Sales Order"], "tags": ["pii"]},
            {"glossaryTerms": ["Order Status"], "tags": ["enum"]},
        )
    )
    table = CatalogTable(**tables[0])
    assert table.glossaryTerms == ["Sales Order"]
    assert table.tags == ["pii"]
    status = next(c for c in table.columns if c.name == "status")
    assert status.glossaryTerms == ["Order Status"]
    assert status.tags == ["enum"]


def test_catalog_to_tables_tolerates_missing_or_malformed_lists():
    """Older catalog payloads (no keys, nulls, blanks) still parse to empty lists."""
    tables = _catalog_to_tables(_catalog({"glossaryTerms": None, "tags": "not-a-list"}, {"tags": ["", "  ", "ok"]}))
    table = CatalogTable(**tables[0])
    assert table.glossaryTerms == []
    assert table.tags == []
    assert next(c for c in table.columns if c.name == "status").tags == ["ok"]


# ── induction → ontology (table_to_ontology) ─────────────────────────────


def _orders_and_customers() -> list[CatalogTable]:
    customers = CatalogTable(
        id="ds1:customers",
        name="customers",
        fullyQualifiedName="sales.customers",
        datasourceId="ds1",
        columns=[CatalogColumn(name="customer_id", dataType="INT", constraint="PRIMARY_KEY")],
        tableConstraints=[CatalogConstraint(constraintType="PRIMARY_KEY", columns=["customer_id"])],
    )
    orders = CatalogTable(
        id="ds1:orders",
        name="orders",
        fullyQualifiedName="sales.orders",
        datasourceId="ds1",
        description="Customer orders",
        synonyms=["purchases"],
        glossaryTerms=["Sales Order"],
        tags=["finance"],
        columns=[
            CatalogColumn(name="order_id", dataType="INT", constraint="PRIMARY_KEY"),
            CatalogColumn(
                name="status",
                dataType="STRING",
                description="Fulfilment state",
                synonyms=["state"],
                glossaryTerms=["Order Status"],
                tags=["enum"],
            ),
            CatalogColumn(
                name="customer_id",
                dataType="INT",
                description="The buyer",
                glossaryTerms=["Buyer"],
                tags=["join-key"],
            ),
        ],
        tableConstraints=[
            CatalogConstraint(constraintType="PRIMARY_KEY", columns=["order_id"]),
            CatalogConstraint(
                constraintType="FOREIGN_KEY",
                columns=["customer_id"],
                referredColumns=["customers.customer_id"],
                relationshipType="DETERMINISTIC",
            ),
        ],
    )
    return [customers, orders]


def _class_by_label(g: Graph, label: str) -> URIRef:
    return next(s for s in g.subjects(RDF.type, OWL.Class) if (s, RDFS.label, Literal(label)) in g)


def _prop(g: Graph, domain: URIRef, label: str) -> URIRef:
    return next(p for p in g.subjects(RDFS.domain, domain) if (p, RDFS.label, Literal(label)) in g)


def test_table_to_ontology_emits_glossary_terms_and_tags_on_class_and_columns():
    onto, _ = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, _orders_and_customers(), [])
    orders = _class_by_label(onto, "orders")
    assert _literals(onto, orders, GLOSSARY_TERM) == {"Sales Order"}
    assert _literals(onto, orders, TAG) == {"finance"}
    status = _prop(onto, orders, "status")
    assert _literals(onto, status, GLOSSARY_TERM) == {"Order Status"}
    assert _literals(onto, status, TAG) == {"enum"}
    assert _literals(onto, status, SKOS.altLabel) == {"state"}


def test_foreign_key_column_keeps_its_description_glossary_and_tags():
    """An FK column becomes an object property; its curated metadata used to be lost."""
    onto, _ = TableToOntologyStrategy()._build_proposal_ontology(PREFIX, _orders_and_customers(), [])
    orders = _class_by_label(onto, "orders")
    fk_prop = _prop(onto, orders, "customer_id")
    assert (fk_prop, RDF.type, OWL.ObjectProperty) in onto
    comments = _literals(onto, fk_prop, RDFS.comment)
    # ONE comment (readers take a single comment per property), description first.
    assert len(comments) == 1
    (comment,) = comments
    assert comment.startswith("The buyer (Foreign key: orders.customer_id references customers.")
    assert _literals(onto, fk_prop, GLOSSARY_TERM) == {"Buyer"}
    assert _literals(onto, fk_prop, TAG) == {"join-key"}


def test_join_comment_without_description_is_unchanged():
    """No approved description → the generated note exactly as before."""
    assert _join_comment("Foreign key: a.b references c.d", None) == "Foreign key: a.b references c.d"
    assert _join_comment("Foreign key: a.b references c.d", "  ") == "Foreign key: a.b references c.d"


def test_add_glossary_and_tags_skips_blank_values():
    g = Graph()
    add_glossary_and_tags(g, NS.X, ["", " term "], [None, "tag"])
    assert _literals(g, NS.X, GLOSSARY_TERM) == {"term"}
    assert _literals(g, NS.X, TAG) == {"tag"}


# ── induction → ontology (RIGOR) ─────────────────────────────────────────


def test_rigor_schema_context_shows_steward_terms_to_the_llm():
    _, orders = _orders_and_customers()
    text = _format_schema_context(orders)
    assert "Synonyms: purchases" in text
    assert "Glossary terms: Sales Order" in text
    assert "Tags: finance" in text
    assert "(glossary terms: Order Status)" in text
    assert "(tags: enum)" in text


def _rigor_graph() -> Graph:
    """What the RIGOR LLM might produce: classes/properties with its own comments, no key."""
    g = Graph()
    for cls, label in ((NS.Orders, "orders"), (NS.Customers, "customers")):
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(label)))
    g.add((NS.Orders, RDFS.comment, Literal("LLM wording for orders")))
    for prop, label, ptype in (
        (NS.orderId, "order_id", OWL.DatatypeProperty),
        (NS.status, "status", OWL.DatatypeProperty),
        (NS.customer, "customer_id", OWL.ObjectProperty),
    ):
        g.add((prop, RDF.type, ptype))
        g.add((prop, RDFS.domain, NS.Orders))
        g.add((prop, RDFS.label, Literal(label)))
    g.add((NS.status, RDFS.comment, Literal("LLM wording for status")))
    return g


def test_rigor_stamps_approved_metadata_and_replaces_the_llm_description():
    tables = _orders_and_customers()
    g = _rigor_graph()
    strategy = RigorOntologyStrategy()
    strategy._stamp_catalog_metadata(g, PREFIX, tables, pascal_names_for(tables))

    # Approved description wins over the LLM's (one description per term, #1118).
    assert _literals(g, NS.Orders, RDFS.comment) == {"Customer orders"}
    assert _literals(g, NS.status, RDFS.comment) == {"Fulfilment state"}
    assert _literals(g, NS.Orders, SKOS.altLabel) == {"purchases"}
    assert _literals(g, NS.Orders, GLOSSARY_TERM) == {"Sales Order"}
    assert _literals(g, NS.status, TAG) == {"enum"}
    assert _literals(g, NS.customer, GLOSSARY_TERM) == {"Buyer"}


def test_rigor_keeps_llm_description_when_none_was_approved():
    tables = _orders_and_customers()
    tables[1].columns[1].description = None
    g = _rigor_graph()
    RigorOntologyStrategy()._stamp_catalog_metadata(g, PREFIX, tables, pascal_names_for(tables))
    assert _literals(g, NS.status, RDFS.comment) == {"LLM wording for status"}


def test_rigor_declares_the_catalog_primary_key_when_the_llm_did_not():
    tables = _orders_and_customers()
    g = _rigor_graph()
    RigorOntologyStrategy()._stamp_catalog_metadata(g, PREFIX, tables, pascal_names_for(tables))
    key_list = g.value(NS.Orders, OWL.hasKey)
    assert key_list is not None
    assert list(Collection(g, key_list)) == [NS.orderId]


def test_rigor_catalog_key_replaces_an_llm_declared_key():
    """RIGOR's R2RML keys on the catalog key, so owl:hasKey must say the same."""
    tables = _orders_and_customers()
    g = _rigor_graph()
    llm_key = BNode()
    Collection(g, llm_key, [NS.status])
    g.add((NS.Orders, OWL.hasKey, llm_key))
    RigorOntologyStrategy()._stamp_catalog_metadata(g, PREFIX, tables, pascal_names_for(tables))
    keys = list(g.objects(NS.Orders, OWL.hasKey))
    assert len(keys) == 1
    assert list(Collection(g, keys[0])) == [NS.orderId]
    assert (llm_key, RDF.first, None) not in g  # the LLM's list is removed, not orphaned


def test_rigor_keeps_the_llm_key_when_the_catalog_key_does_not_resolve():
    """No partial key: an unresolvable catalog key leaves whatever the LLM declared."""
    tables = _orders_and_customers()
    g = _rigor_graph()
    llm_key = BNode()
    Collection(g, llm_key, [NS.customerName])
    g.add((NS.Customers, OWL.hasKey, llm_key))
    # customers' only key column has no generated property.
    RigorOntologyStrategy()._stamp_catalog_metadata(g, PREFIX, tables, pascal_names_for(tables))
    assert list(g.objects(NS.Customers, OWL.hasKey)) == [llm_key]


# ── re-accept supersession ───────────────────────────────────────────────


def test_glossary_terms_and_tags_are_superseded_on_re_accept():
    """Removing a term during review must remove it from the ontology (#1118 rule)."""
    assert _SUPERSEDED_ANNOTATION_PREDICATES[GLOSSARY_TERM] == SCL.supersededGlossaryTerm
    assert _SUPERSEDED_ANNOTATION_PREDICATES[TAG] == SCL.supersededTag


# ── ontology → NL→SQL context ────────────────────────────────────────────


def _context_text(g: Graph, classes: set, r2rml_mapped: dict[str, str]) -> dict[str, str]:
    """Run _accumulate_embeddings and return {class label: stored context_text}."""
    with patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient") as bedrock_cls:
        bedrock = MagicMock()
        bedrock.model_id = "amazon.titan-embed-text-v2:0"
        bedrock.embed_texts.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
        bedrock_cls.return_value = bedrock
        store = MagicMock()
        props = {p for p in g.subjects(RDFS.domain, None)}
        _accumulate_embeddings(
            vector_store=store,
            graph=g,
            ontology_id=PREFIX,
            classes=classes,
            obj_props={p for p in props if (p, RDF.type, OWL.ObjectProperty) in g},
            dt_props={p for p in props if (p, RDF.type, OWL.DatatypeProperty) in g},
            namespace="ns",
            class_to_datasource=r2rml_mapped,
        )
        items = store.store_embeddings_batch.call_args[0][0]
        embedded = bedrock.embed_texts.call_args[0][0]
    out = {}
    for i, it in enumerate(items):
        if it["entity_type"] == "class":
            label = str(g.value(URIRef(it["entity_uri"]), RDFS.label))
            out[label] = it["context_text"]
            out[f"{label}#embed"] = it.get("text") or embedded[i]
    return out


def _serve_graph() -> Graph:
    g = Graph()
    for cls, label in (
        (NS.Policy, "policy"),
        (NS.CommercialPolicy, "commercial_policy"),
        (NS.FiboContract, "Contract"),
    ):
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal(label)))
    # Mapped parent (Policy) and a foundational, unmapped one (FiboContract).
    g.add((NS.CommercialPolicy, RDFS.subClassOf, NS.Policy))
    g.add((NS.CommercialPolicy, RDFS.subClassOf, NS.FiboContract))
    g.add((NS.CommercialPolicy, GLOSSARY_TERM, Literal("Business Policy")))
    g.add((NS.CommercialPolicy, TAG, Literal("underwriting")))
    for prop, label in ((NS.policyNo, "policy_no"), (NS.premium, "premium")):
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.domain, NS.CommercialPolicy))
        g.add((prop, RDFS.label, Literal(label)))
        g.add((prop, RDFS.range, XSD.decimal))
    g.add((NS.premium, SKOS.altLabel, Literal("price")))
    g.add((NS.premium, GLOSSARY_TERM, Literal("Written Premium")))
    key_list = BNode()
    Collection(g, key_list, [NS.policyNo])
    g.add((NS.CommercialPolicy, OWL.hasKey, key_list))
    return g


def test_nl_to_sql_context_carries_hierarchy_key_glossary_and_tags():
    g = _serve_graph()
    mapped = {str(NS.CommercialPolicy): "ds1", str(NS.Policy): "ds1"}
    text = _context_text(g, {NS.CommercialPolicy}, mapped)["commercial_policy"]
    assert "Kind of: policy" in text
    assert "Contract" not in text  # foundational parent has no table: never named in SQL context
    assert "Primary key: policy_no" in text
    assert "Glossary terms: Business Policy" in text
    assert "Tags: underwriting" in text
    assert "premium:decimal [synonyms: price; glossary: Written Premium]" in text


def test_nl_to_sql_context_unchanged_without_steward_terms():
    """A class with no hierarchy, key, glossary or tags renders exactly as before."""
    g = Graph()
    g.add((NS.Plain, RDF.type, OWL.Class))
    g.add((NS.Plain, RDFS.label, Literal("plain")))
    g.add((NS.col, RDF.type, OWL.DatatypeProperty))
    g.add((NS.col, RDFS.domain, NS.Plain))
    g.add((NS.col, RDFS.label, Literal("col")))
    g.add((NS.col, RDFS.range, XSD.string))
    text = _context_text(g, {NS.Plain}, {str(NS.Plain): "ds1"})["plain"]
    assert text == "Table: plain | Columns: col:string"


def test_nl_to_sql_column_term_hints_are_capped_per_kind():
    """A column with many steward terms shows five of each kind, like the Ontop context."""
    g = _serve_graph()
    for i in range(12):
        g.add((NS.premium, GLOSSARY_TERM, Literal(f"term{i:02d}")))
    mapped = {str(NS.CommercialPolicy): "ds1", str(NS.Policy): "ds1"}
    text = _context_text(g, {NS.CommercialPolicy}, mapped)["commercial_policy"]
    hint = text.split("premium:decimal [", 1)[1].split("]", 1)[0]
    glossary = hint.split("glossary: ", 1)[1].split(";")[0].split(", ")
    assert len(glossary) == 5
    assert "synonyms: price" in hint


def test_nl_to_sql_embedding_text_keeps_its_shape_plus_table_glossary_terms():
    """The vector input gains only the table's glossary terms (review of #1167).

    Enrichment fills synonyms, glossary terms and tags for nearly every column, so
    per-column hints, tags, the key and the parent stay in the stored context text
    only: in the embedding they would push a wide table's last columns past the
    8000-character input cut and make tables look alike.
    """
    g = _serve_graph()
    mapped = {str(NS.CommercialPolicy): "ds1", str(NS.Policy): "ds1"}
    texts = _context_text(g, {NS.CommercialPolicy}, mapped)
    embed = texts["commercial_policy#embed"]
    assert "Glossary terms: Business Policy" in embed
    for absent in ("Tags:", "underwriting", "Kind of:", "Primary key:", "Written Premium", "[synonyms"):
        assert absent not in embed, absent
    head, cols = embed.rsplit(" | Columns: ", 1)
    assert head == "Table: commercial_policy | Glossary terms: Business Policy"
    assert set(cols.split(", ")) == {"policy_no", "premium"}
