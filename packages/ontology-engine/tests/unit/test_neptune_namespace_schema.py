# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NeptuneDBGraphStore.get_namespace_schema: induced classes first.

Regression for describe_schema returning 0 induced classes when large
reference ontologies are loaded: the class page must order mapped
(coa:isMapped) classes first and then case-insensitively by label, push the
optional class prefix into the query (before LIMIT), and the property query
must be restricted to the domains of the returned classes.

The shape tests mock ``_sparql_query``; the behaviour tests run the generated
queries on an in-memory rdflib dataset, as test_catalog_label_language does.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from coa_common.constants import VOCAB_URI
from coa_ontology.stores import neptune_db_graph as ndb
from coa_ontology.stores.neptune_db_graph import NeptuneDBGraphStore
from rdflib import OWL, RDF, RDFS, XSD, Dataset, Literal, URIRef

pytestmark = pytest.mark.unit

NS = "ns-test"
_ISMAPPED_IRI = f"{VOCAB_URI}isMapped"
_URI_VARS = ("classUri", "domain", "propUri")
INDUCED = "http://example.org/induced#"
REF = "https://ref.example/"


def _b(**kw):
    return {k: {"type": "uri" if k in _URI_VARS else "literal", "value": v} for k, v in kw.items()}


def _store() -> NeptuneDBGraphStore:
    return NeptuneDBGraphStore(endpoint="https://ndb.example:8182", namespace=NS)


def _run(class_rows, prop_rows=None, **kwargs):
    responses = [{"results": {"bindings": class_rows}}, {"results": {"bindings": prop_rows or []}}]
    with patch.object(ndb, "_sparql_query", side_effect=responses) as q:
        result = _store().get_namespace_schema(NS, **kwargs)
    return result, [c.args[0] for c in q.call_args_list]


class TestNamespaceSchemaQueryShape:
    def test_class_page_orders_mapped_first_then_label(self):
        _, queries = _run([_b(classUri=INDUCED + "orders", classLabel="orders")])
        class_q = queries[0]
        assert f"OPTIONAL {{ ?classUri <{_ISMAPPED_IRI}> ?mapped }}" in class_q
        assert '(MIN(IF(COALESCE(STR(?mapped), "") = "true", 0, 1)) AS ?mappedRank)' in class_q
        # Inner page (before LIMIT) and outer rows are both mapped-first.
        assert class_q.count("ORDER BY ?mappedRank ?sortKey ?classUri") == 2
        inner = class_q.index("ORDER BY ?mappedRank ?sortKey ?classUri")
        assert inner < class_q.index("LIMIT 100")
        assert "STRSTARTS(STR(?classUri)" not in class_q

    def test_class_prefix_pushed_into_query_before_limit(self):
        _, queries = _run([], class_prefix=INDUCED)
        flt = f'FILTER(STRSTARTS(STR(?classUri), "{INDUCED}"))'
        assert flt in queries[0]
        assert queries[0].index(flt) < queries[0].index("LIMIT")

    def test_class_prefix_is_escaped(self):
        _, queries = _run([], class_prefix='http://x/"))} INSERT')
        assert 'STR(?classUri) = "http://x/\\"))} INSERT"' in queries[0]
        assert 'STRSTARTS(STR(?classUri), "http://x/\\"))} INSERT#")' in queries[0]

    def test_property_query_restricted_to_returned_class_domains(self):
        rows = [
            _b(classUri=INDUCED + "orders", classLabel="orders", mapped="true"),
            _b(classUri=REF + "Account", classLabel="Account"),
            _b(classUri="n934b37cblank"),
        ]
        props = [_b(propUri=INDUCED + "order_id", propLabel="order_id", domain=INDUCED + "orders")]
        result, queries = _run(rows, props)
        assert len(queries) == 2
        prop_q = queries[1]
        assert f"VALUES ?domain {{ <{INDUCED}orders> <{REF}Account> }}" in prop_q
        assert "?propUri rdfs:domain ?domain ." in prop_q
        assert "OPTIONAL { ?propUri rdfs:domain ?domain }" not in prop_q
        assert "n934b37cblank" not in prop_q
        # Backend row order is preserved (mapped-first comes from SPARQL ORDER BY).
        assert result["classes"][0]["uri"] == INDUCED + "orders"
        assert result["classes"][0]["properties"][0]["uri"] == INDUCED + "order_id"

    def test_no_property_query_without_classes(self):
        result, queries = _run([])
        assert len(queries) == 1
        assert result["classes"] == []

    def test_no_property_query_for_blank_node_classes_only(self):
        result, queries = _run([_b(classUri="n934b37cblank")])
        assert len(queries) == 1
        assert result["classes"][0]["properties"] == []


class TestNamespaceSchemaOnDataset:
    """Run the generated SPARQL on rdflib: a reference ontology that sorts first by label."""

    @staticmethod
    def _dataset():
        ds = Dataset()
        prefix = ndb._namespace_graph_prefix(NS)
        ref = ds.graph(URIRef(f"{prefix}/ref"))
        induced = ds.graph(URIRef(f"{prefix}/induced"))
        other_ns = ds.graph(URIRef(f"{ndb._namespace_graph_prefix('other-ns')}/induced"))
        # Capitalized and lowercase reference labels both sort before "policy" by label.
        for name in ("Account", "Agreement", "Contract", "agent", "Claimant"):
            ref.add((URIRef(REF + name), RDF.type, OWL.Class))
            ref.add((URIRef(REF + name), RDFS.label, Literal(name)))
        ref.add((URIRef(REF + "accountId"), RDF.type, OWL.DatatypeProperty))
        ref.add((URIRef(REF + "accountId"), RDFS.domain, URIRef(REF + "Account")))
        for name in ("policy", "claim"):
            cls = URIRef(INDUCED + name)
            induced.add((cls, RDF.type, OWL.Class))
            induced.add((cls, RDFS.label, Literal(name)))
            induced.add((cls, URIRef(_ISMAPPED_IRI), Literal("true", datatype=XSD.boolean)))
            prop = URIRef(f"{INDUCED}{name}_id")
            induced.add((prop, RDF.type, OWL.DatatypeProperty))
            induced.add((prop, RDFS.label, Literal(f"{name}_id")))
            induced.add((prop, RDFS.domain, cls))
            induced.add((prop, RDFS.range, XSD.string))
        # Mapped but unlabeled: still mapped-first, sorted by its IRI ("http://…" sorts
        # between "claim" and "policy"), as upstream falls back to the IRI.
        induced.add((URIRef(INDUCED + "zone"), RDF.type, OWL.Class))
        induced.add((URIRef(INDUCED + "zone"), URIRef(_ISMAPPED_IRI), Literal("true", datatype=XSD.boolean)))
        # An explicit ``false`` marker ranks as unmapped, not as mapped.
        ref.add((URIRef(REF + "Contract"), URIRef(_ISMAPPED_IRI), Literal("false", datatype=XSD.boolean)))
        # Another namespace's graph is never read.
        other_ns.add((URIRef("http://other.example/aaa"), RDF.type, OWL.Class))
        # A namespace whose name starts with this one's is a different namespace.
        look_alike_ns = ds.graph(URIRef(f"{ndb._namespace_graph_prefix(NS + 'other')}/induced"))
        look_alike_ns.add((URIRef("http://other.example/look-alike-namespace"), RDF.type, OWL.Class))
        return ds

    def _schema(self, **kwargs):
        ds = self._dataset()

        def run(query: str) -> dict:
            return json.loads(ds.query(query).serialize(format="json"))

        with patch.object(ndb, "_sparql_query", side_effect=run):
            return _store().get_namespace_schema(NS, **kwargs)

    def test_mapped_classes_fill_the_page_first(self):
        schema = self._schema(max_results=3)
        assert [c["uri"] for c in schema["classes"]] == [INDUCED + "claim", INDUCED + "zone", INDUCED + "policy"]
        props = {c["uri"]: [p["uri"] for p in c["properties"]] for c in schema["classes"]}
        assert props[INDUCED + "claim"] == [INDUCED + "claim_id"]
        assert props[INDUCED + "policy"] == [INDUCED + "policy_id"]

    def test_unmapped_classes_follow_case_insensitively(self):
        schema = self._schema(max_results=100)
        uris = [c["uri"] for c in schema["classes"]]
        assert uris[:3] == [INDUCED + "claim", INDUCED + "zone", INDUCED + "policy"]
        assert uris[3:] == [REF + n for n in ("Account", "agent", "Agreement", "Claimant", "Contract")]
        assert "http://other.example/aaa" not in uris
        assert "http://other.example/look-alike-namespace" not in uris
        account = next(c for c in schema["classes"] if c["uri"] == REF + "Account")
        assert [p["uri"] for p in account["properties"]] == [REF + "accountId"]

    def test_class_prefix_applies_before_limit(self):
        schema = self._schema(max_results=2, class_prefix=REF)
        assert [c["uri"] for c in schema["classes"]] == [REF + "Account", REF + "agent"]

    def test_class_prefix_does_not_page_in_look_alike_iris(self):
        assert self._schema(max_results=10, class_prefix=REF + "Acc")["classes"] == []
        exact = self._schema(max_results=10, class_prefix=REF + "Account")
        assert [c["uri"] for c in exact["classes"]] == [REF + "Account"]
        under = self._schema(max_results=10, class_prefix=INDUCED.rstrip("#"))
        assert [c["uri"] for c in under["classes"]] == [INDUCED + "claim", INDUCED + "zone", INDUCED + "policy"]

    def test_properties_only_for_returned_classes(self):
        schema = self._schema(max_results=1)
        assert [c["uri"] for c in schema["classes"]] == [INDUCED + "claim"]
        assert [p["uri"] for p in schema["classes"][0]["properties"]] == [INDUCED + "claim_id"]
