# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The two-signal (coa:isMapped + coa:queryBackend) T-Box gate, executed for real.

``TBoxContextBuilder.build`` runs against a GraphClient backed by an in-memory rdflib
dataset, so these tests assert which classes, parents and join paths each backend's
prompt actually receives — not the shape of the SPARQL that selects them. The fixture
is a mixed namespace whose cross-backend parent and edges are exactly the leaks the
gate exists to stop.
"""

from __future__ import annotations

import pytest
from coa_common.constants import VOCAB_URI, StructuredBackend
from coa_serve.tier2.ontop.tbox_context import TBoxContextBuilder
from coa_serve.tier2.ontop.types import VectorHit
from rdflib import OWL, RDF, RDFS, XSD, Dataset, Literal, Namespace, URIRef

pytestmark = pytest.mark.unit

_TEMPLATE = "https://ontology-workbench.local/{namespace}"
_NS = "ns-x"
_GRAPH = URIRef("https://ontology-workbench.local/ns-x/mixed")
EX = Namespace("http://example.org/o#")
COA = Namespace(VOCAB_URI)
_INIT_NS = {"owl": OWL, "rdfs": RDFS, "rdf": RDF, "xsd": XSD}


class _RdflibGraphClient:
    """GraphClient double that evaluates the builder's SPARQL against a dataset."""

    def __init__(self, ds: Dataset):
        self._ds = ds

    async def query(self, sparql: str) -> list[dict[str, str]]:
        result = self._ds.query(sparql, initNs=_INIT_NS)
        return [{str(v): str(row[v]) for v in result.vars if row[v] is not None} for row in result]

    async def ask(self, sparql: str) -> bool:
        return bool(self._ds.query(sparql, initNs=_INIT_NS).askAnswer)


def _cls(g, uri, label, *, mapped=False, graph=False, parent=None):
    g.add((uri, RDF.type, OWL.Class))
    g.add((uri, RDFS.label, Literal(label)))
    if mapped:
        g.add((uri, COA.isMapped, Literal(True)))
    if graph:
        g.add((uri, COA.queryBackend, Literal("RDF_SPARQL")))
    if parent is not None:
        g.add((uri, RDFS.subClassOf, parent))


def _edge(g, uri, domain, range_):
    g.add((uri, RDF.type, OWL.ObjectProperty))
    g.add((uri, RDFS.domain, domain))
    g.add((uri, RDFS.range, range_))
    g.add((uri, RDFS.label, Literal(str(uri).rsplit("#", 1)[1])))


def _dataset(*, sql_mapped=True, with_graph_classes=True) -> Dataset:
    """Order, Customer (SQL) + Ticket, Case (graph) + Doc (unmapped).

    Cross-backend links: Ticket ⊑ Order, Customer ⊑ Ticket, Ticket → Customer.
    Same-backend links: Case ⊑ Ticket, Case → Ticket, Order → Customer.
    """
    ds = Dataset()
    g = ds.graph(_GRAPH)
    g.add((URIRef("http://example.org/o"), RDF.type, OWL.Ontology))
    _cls(g, EX.Order, "Order", mapped=sql_mapped)
    _cls(g, EX.Doc, "Doc")
    if with_graph_classes:
        _cls(g, EX.Ticket, "Ticket", mapped=True, graph=True, parent=EX.Order)
        _cls(g, EX.Case, "Case", mapped=True, graph=True, parent=EX.Ticket)
        _cls(g, EX.Customer, "Customer", mapped=sql_mapped, parent=EX.Ticket)
        _edge(g, EX.raisedBy, EX.Ticket, EX.Customer)
        _edge(g, EX.about, EX.Case, EX.Ticket)
        g.add((EX.ticketId, RDF.type, OWL.DatatypeProperty))
        g.add((EX.ticketId, RDFS.domain, EX.Ticket))
        g.add((EX.ticketId, RDFS.range, XSD.string))
        g.add((EX.ticketId, RDFS.label, Literal("ticket id")))
    else:
        _cls(g, EX.Customer, "Customer", mapped=sql_mapped)
    _edge(g, EX.placedBy, EX.Order, EX.Customer)
    return ds


async def _build(ds: Dataset, backend: StructuredBackend):
    builder = TBoxContextBuilder(_RdflibGraphClient(ds), _TEMPLATE)
    return await builder.build([], _NS, query="tickets", backend=backend)


def _classes(ctx) -> dict[str, str | None]:
    return {c["uri"]: c.get("parent") for c in ctx.classes}


def _edges(ctx) -> set[str]:
    return {op["uri"] for op in ctx.object_properties}


class TestMixedNamespace:
    async def test_sql_build_sees_only_sql_classes(self):
        ctx = await _build(_dataset(), StructuredBackend.SQL)
        assert set(_classes(ctx)) == {str(EX.Order), str(EX.Customer)}

    async def test_sql_build_drops_a_graph_parent(self):
        ctx = await _build(_dataset(), StructuredBackend.SQL)
        assert _classes(ctx)[str(EX.Customer)] is None

    async def test_sql_build_keeps_only_sql_to_sql_edges(self):
        ctx = await _build(_dataset(), StructuredBackend.SQL)
        assert _edges(ctx) == {str(EX.placedBy)}

    async def test_sparql_build_sees_only_graph_classes(self):
        ctx = await _build(_dataset(), StructuredBackend.RDF_SPARQL)
        assert set(_classes(ctx)) == {str(EX.Ticket), str(EX.Case)}

    async def test_sparql_build_keeps_a_graph_parent_and_drops_a_sql_parent(self):
        classes = _classes(await _build(_dataset(), StructuredBackend.RDF_SPARQL))
        assert classes[str(EX.Case)] == str(EX.Ticket)
        assert classes[str(EX.Ticket)] is None  # Order is a SQL class

    async def test_sparql_build_keeps_only_graph_to_graph_edges(self):
        ctx = await _build(_dataset(), StructuredBackend.RDF_SPARQL)
        assert _edges(ctx) == {str(EX.about)}

    async def test_sparql_build_carries_graph_class_columns(self):
        ctx = await _build(_dataset(), StructuredBackend.RDF_SPARQL)
        assert {p["uri"] for p in ctx.properties} == {str(EX.ticketId)}


class TestUnmarkedNamespace:
    async def test_a_graph_import_exposes_no_unmarked_sql_class(self):
        """Unmarked SQL classes stay hidden even when the graph classes carry markers,
        and the graph classes never reach the SQL prompt."""
        ctx = await _build(_dataset(sql_mapped=False), StructuredBackend.SQL)
        assert ctx.classes == []

    async def test_an_unmarked_sql_namespace_stays_dark(self):
        ctx = await _build(_dataset(sql_mapped=False, with_graph_classes=False), StructuredBackend.SQL)
        assert ctx.classes == []

    async def test_sparql_build_is_never_ungated(self):
        """A namespace with no markers at all must not expose its classes to SPARQL —
        that is the misconfigured (ontology never imported) case, which fails loud."""
        ctx = await _build(_dataset(sql_mapped=False, with_graph_classes=False), StructuredBackend.RDF_SPARQL)
        assert ctx.classes == []

    async def test_sparql_build_on_a_sql_only_namespace_is_empty(self):
        ctx = await _build(_dataset(with_graph_classes=False), StructuredBackend.RDF_SPARQL)
        assert ctx.classes == []


class TestDefaultBackend:
    async def test_build_defaults_to_sql(self):
        builder = TBoxContextBuilder(_RdflibGraphClient(_dataset()), _TEMPLATE)
        ctx = await builder.build([], _NS, query="tickets")
        assert set(_classes(ctx)) == {str(EX.Order), str(EX.Customer)}


def _hit(uri, hit_type: str = "ontology_class") -> VectorHit:
    return VectorHit(type=hit_type, score=0.9, entity_id=str(uri), uri=str(uri), metadata={"entity_uri": str(uri)})


def _sql_only(classes, properties) -> None:
    """The SQL view of the mixed fixture: Order and Customer, no graph parent, no graph column."""
    assert {c["uri"]: c.get("parent") for c in classes} == {str(EX.Order): None, str(EX.Customer): None}
    assert str(EX.ticketId) not in {p["uri"] for p in properties}


class TestEveryFetchIsBackendGated:
    """The fetches build() reaches only past the full-namespace threshold apply the gate too."""

    async def test_the_vector_hit_fetch_keeps_graph_classes_out_of_sql(self):
        builder = TBoxContextBuilder(_RdflibGraphClient(_dataset()), _TEMPLATE)
        hits = [
            _hit(EX.Order),
            _hit(EX.Ticket),
            _hit(EX.Case),
            _hit(EX.Customer),
            _hit(EX.ticketId, "ontology_property"),
        ]
        _sql_only(*await builder._fetch_ontology_context(hits, _NS, backend=StructuredBackend.SQL))

    async def test_the_entity_fetch_keeps_graph_classes_out_of_sql(self):
        builder = TBoxContextBuilder(_RdflibGraphClient(_dataset()), _TEMPLATE)
        _sql_only(*await builder._fetch_by_entities("order ticket case customer", _NS, backend=StructuredBackend.SQL))
