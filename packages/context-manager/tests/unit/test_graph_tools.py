# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for OntologyGraphTool — the Tier-2 ``explore_graph`` capability.

These pin the tool's own contract (seed resolution, hop bounds, node-type
filtering, truncation, caching, SPARQL-injection gating). Its exposure to the
agent as a tool lives in ``test_sql_agent.py``, and the flag that builds it in
``test_agentic_strategy.py``.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest
from coa_serve.tier2.tools import graph_tools
from coa_serve.tier2.tools.graph_tools import (
    MAX_HOPS,
    OntologyGraphTool,
    _bfs,
    _build_adjacency,
    _normalize_node_types,
)

NS = "11111111-1111-1111-1111-111111111111"
TEMPLATE = "https://ontology-workbench.local/{namespace}"
BASE = f"https://demo.coa/{NS}/ontology#"


def _iri(name: str) -> str:
    return BASE + name


# A tiny two-DB graph. DB "shop": orders -> customers, orders -> products.
# DB "hr": employees -> departments. Same-label collision: both DBs have a
# "notes" table (tests cross-DB seed disambiguation via IRI).
_FK_ROWS = [
    {
        "domain": _iri("Shop_Orders"),
        "domainLabel": "orders",
        "range": _iri("Shop_Customers"),
        "rangeLabel": "customers",
        "opLabel": "customerid",
        "comment": "Foreign key: orders.customerid references customers.id",
    },
    {
        "domain": _iri("Shop_Orders"),
        "domainLabel": "orders",
        "range": _iri("Shop_Products"),
        "rangeLabel": "products",
        "opLabel": "productid",
        "comment": "Foreign key: orders.productid references products.id",
    },
    {
        "domain": _iri("Hr_Employees"),
        "domainLabel": "employees",
        "range": _iri("Hr_Departments"),
        "rangeLabel": "departments",
        "opLabel": "deptid",
        "comment": "Foreign key: employees.deptid references departments.id",
    },
]

_COL_ROWS = {
    _iri("Shop_Customers"): [
        {"class": _iri("Shop_Customers"), "col": _iri("c_id"), "colLabel": "id", "range": "xsd#integer"},
        {"class": _iri("Shop_Customers"), "col": _iri("c_name"), "colLabel": "name", "range": "xsd#string"},
    ],
    _iri("Shop_Orders"): [
        {"class": _iri("Shop_Orders"), "col": _iri("o_id"), "colLabel": "id", "range": "xsd#integer"},
    ],
}


class _MockGraph:
    """Dispatches on the SPARQL shape, mirroring the tool's four queries.

    ``graph_rows`` is what the named-graph probe returns. Default ``None`` = the
    probe finds nothing, which is the prefix-filter fallback path most of these
    tests exercise; ``TestGraphScoping`` sets it to pin the bound form.
    """

    def __init__(self, fk_rows=None, col_rows=None, label_rows=None, graph_rows=None):
        self._fk = fk_rows if fk_rows is not None else _FK_ROWS
        self._cols = col_rows if col_rows is not None else _COL_ROWS
        self._labels = label_rows or {}
        self._graphs = graph_rows or []
        self.calls: list[str] = []

    async def query(self, sparql, *, max_results=10000):
        self.calls.append(sparql)
        if "?ont a owl:Ontology" in sparql:
            return [{"g": g} for g in self._graphs]
        if "?op a owl:ObjectProperty" in sparql:
            return self._fk
        if "?col a owl:DatatypeProperty" in sparql:
            body = sparql.split("VALUES ?class {", 1)[1].split("}", 1)[0]
            wanted = set(re.findall(r"<([^>]+)>", body))
            return [row for cls, rows in self._cols.items() if cls in wanted for row in rows]
        if "?class a owl:Class ; rdfs:label ?label" in sparql:
            key = sparql.split('LCASE(STR(?label)) = "')[1].split('"')[0]
            return [{"class": u} for u in self._labels.get(key, [])]
        return []

    async def ask(self, sparql):  # pragma: no cover - protocol completeness
        return False

    async def health_check(self):  # pragma: no cover - protocol completeness
        return {"status": "ok"}


def _catalog(iri_by_table: dict[str, str] | None = None):
    """A TableCatalog stand-in that resolves seed tables to class IRIs."""
    mapping = iri_by_table or {}

    def hits_for_tables(table_names):
        hits = []
        for name in table_names:
            iri = mapping.get(str(name).lower())
            if iri:
                hits.append(MagicMock(metadata={"uri": iri}))
        return hits, []

    catalog = MagicMock()
    catalog.hits_for_tables = hits_for_tables
    return catalog


def _tool(graph=None, catalog=None, template=TEMPLATE):
    return OntologyGraphTool(graph or _MockGraph(), catalog, namespace=NS, graph_uri_template=template)


# ── pure helpers ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_normalize_node_types_defaults_to_all():
    assert _normalize_node_types(None) == ("table", "column")
    assert _normalize_node_types([]) == ("table", "column")
    assert _normalize_node_types(["bogus"]) == ("table", "column")  # unknown → all
    assert _normalize_node_types(["table"]) == ("table",)
    assert _normalize_node_types(["COLUMN"]) == ("column",)


@pytest.mark.unit
def test_build_adjacency_is_bidirectional():
    adj, labels = _build_adjacency(_FK_ROWS)
    outs = {(e["to"], e["direction"]) for e in adj[_iri("Shop_Orders")]}
    assert (_iri("Shop_Customers"), "outgoing") in outs
    # customers is reachable back to orders as an incoming edge
    assert any(e["to"] == _iri("Shop_Orders") and e["direction"] == "incoming" for e in adj[_iri("Shop_Customers")])
    assert labels[_iri("Shop_Orders")] == "orders"


@pytest.mark.unit
def test_build_adjacency_drops_self_loops_but_keeps_the_label():
    # A self-referential FK (employees.managerid -> employees) adds no reachable
    # neighbour — the node is already at hop 0 — so it stays out of the adjacency.
    rows = [
        {
            "domain": _iri("Hr_Employees"),
            "domainLabel": "employees",
            "range": _iri("Hr_Employees"),
            "rangeLabel": "employees",
            "opLabel": "managerid",
            "comment": "Foreign key: employees.managerid references employees.id",
        }
    ]
    adj, labels = _build_adjacency(rows)

    assert adj == {}
    assert labels[_iri("Hr_Employees")] == "employees"


@pytest.mark.unit
def test_bfs_respects_hop_bound():
    adj, _ = _build_adjacency(_FK_ROWS)
    # From customers: 1 hop reaches orders; 2 hops also reaches products (via orders).
    one = _bfs([_iri("Shop_Customers")], adj, max_hops=1)
    assert _iri("Shop_Orders") in one and _iri("Shop_Products") not in one
    two = _bfs([_iri("Shop_Customers")], adj, max_hops=2)
    assert _iri("Shop_Products") in two and two[_iri("Shop_Products")][0] == 2
    # DB boundary: hr tables are never reached from a shop seed.
    assert _iri("Hr_Departments") not in two


# ── explore_graph() ───────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestExploreGraph:
    async def test_tables_only_one_hop(self):
        tool = _tool(catalog=_catalog({"orders": _iri("Shop_Orders")}))
        r = await tool.explore_graph("orders", hops=1, node_types=["table"])

        assert {t["table"] for t in r["tables"]} == {"orders", "customers", "products"}
        assert r["columns"] == []
        seed = next(t for t in r["tables"] if t["table"] == "orders")
        assert seed["hop"] == 0 and seed["direction"] == "seed"
        customers = next(t for t in r["tables"] if t["table"] == "customers")
        assert customers["hop"] == 1 and "orders.customerid" in customers["via"]

    async def test_truncation_is_tables_first(self):
        tool = _tool(catalog=_catalog({"orders": _iri("Shop_Orders")}))
        r = await tool.explore_graph("orders", hops=1, limit=2)

        assert len(r["tables"]) == 2 and r["columns"] == []
        assert r["truncated"] is True
        assert r["returned_nodes"] == 2 and r["total_nodes_found"] > 2

    async def test_columns_follow_the_reached_tables(self):
        tool = _tool(catalog=_catalog({"orders": _iri("Shop_Orders")}))
        r = await tool.explore_graph("orders", hops=1, node_types=["column"], limit=50)

        assert r["tables"] == []
        got = {(c["table"], c["column"]) for c in r["columns"]}
        assert ("customers", "name") in got and ("orders", "id") in got

    async def test_catalog_iri_wins_over_the_label_lookup(self):
        # A retrieved table's exact IRI pins the seed, so a same-named table in
        # another database can't be walked by accident — and no label query runs.
        graph = _MockGraph(label_rows={"notes": [_iri("Shop_Notes"), _iri("Hr_Notes")]})
        tool = _tool(graph, catalog=_catalog({"orders": _iri("Shop_Orders")}))
        r = await tool.explore_graph("orders", hops=1, node_types=["table"])

        assert r["seed_matched_classes"] == 1
        assert not any("rdfs:label ?label" in c for c in graph.calls)

    async def test_label_fallback_when_the_table_was_not_retrieved(self):
        # "notes" exists in two DBs → label resolution returns both IRIs.
        graph = _MockGraph(label_rows={"notes": [_iri("Shop_Notes"), _iri("Hr_Notes")]})
        r = await _tool(graph, catalog=_catalog()).explore_graph("notes", hops=1, node_types=["table"])

        assert r["seed_matched_classes"] == 2

    async def test_works_without_a_catalog(self):
        graph = _MockGraph(label_rows={"orders": [_iri("Shop_Orders")]})
        r = await _tool(graph).explore_graph("orders", hops=1, node_types=["table"])

        assert {t["table"] for t in r["tables"]} == {"orders", "customers", "products"}

    async def test_unknown_seed_returns_an_error(self):
        r = await _tool(_MockGraph(label_rows={})).explore_graph("doesnotexist", hops=1)

        assert "error" in r and r["tables"] == []

    async def test_an_unsafe_seed_iri_never_reaches_sparql(self):
        # A catalog IRI that fails the safety gate falls back to the label lookup
        # (which misses here) → error, and no FK query is issued at all.
        graph = _MockGraph(label_rows={})
        catalog = _catalog({"orders": "http://x> } INSERT { ?s ?p ?o }"})
        r = await _tool(graph, catalog=catalog).explore_graph("orders", hops=1)

        assert "error" in r
        assert not any("owl:ObjectProperty" in c for c in graph.calls)

    async def test_an_unsafe_seed_label_never_reaches_sparql(self):
        graph = _MockGraph(label_rows={})
        r = await _tool(graph).explore_graph('bad" } INSERT { ?s ?p ?o }', hops=1)

        assert "error" in r
        assert graph.calls == []

    async def test_a_non_latin_seed_label_resolves(self):
        # Regression: an ASCII-only allow-list rejected non-Latin labels, so a CJK
        # (or accented, or Cyrillic) schema reported "seed table not found" and the
        # tool silently did nothing on every question. The literal is still escaped.
        for label in ("会員", "회원", "Kundenübersicht", "клиенты"):
            graph = _MockGraph(label_rows={label.lower(): [_iri("Shop_Customers")]})
            r = await _tool(graph).explore_graph(label, hops=1, node_types=["table"])

            assert "error" not in r, label
            assert any(f'= "{label.lower()}"' in c for c in graph.calls), label

    async def test_fk_edges_are_loaded_once_per_request(self):
        graph = _MockGraph()
        tool = _tool(graph, catalog=_catalog({"orders": _iri("Shop_Orders"), "customers": _iri("Shop_Customers")}))
        await tool.explore_graph("orders", hops=1, node_types=["table"])
        await tool.explore_graph("customers", hops=1, node_types=["table"])

        assert sum("owl:ObjectProperty" in c for c in graph.calls) == 1

    async def test_hops_and_limit_are_clamped(self):
        tool = _tool(catalog=_catalog({"orders": _iri("Shop_Orders")}))
        r = await tool.explore_graph("orders", hops=999, limit=99999)

        assert r["hops"] == MAX_HOPS

    async def test_unavailable_without_a_graph_uri_template(self):
        tool = _tool(catalog=_catalog({"orders": _iri("Shop_Orders")}), template="")

        assert tool.available is False
        assert "error" in await tool.explore_graph("orders")

    async def test_a_query_timeout_is_logged_as_such_and_propagates(self, monkeypatch):
        # The agent's tool wrapper catches this; the point is that the log names it
        # a timeout rather than leaving it indistinguishable from a bad query.
        async def _never(*_a, **_kw):
            raise TimeoutError

        monkeypatch.setattr(graph_tools.asyncio, "wait_for", _never)
        warnings: list[tuple] = []
        monkeypatch.setattr(
            graph_tools.logger, "warning", lambda event, **kw: warnings.append((event, kw)), raising=False
        )
        tool = _tool(catalog=_catalog({"orders": _iri("Shop_Orders")}))

        with pytest.raises(TimeoutError):
            await tool.explore_graph("orders", hops=1)

        assert warnings and warnings[0][0] == "graph_tool_timeout"
        assert warnings[0][1]["query"] == "fk_edges"

    async def test_every_query_is_still_scoped_when_the_probe_finds_nothing(self):
        # The fallback: no owl:Ontology anchor → prefix filter, i.e. exactly the
        # pre-fix behaviour. Slower, but never cross-namespace.
        graph = _MockGraph()
        tool = _tool(graph, catalog=_catalog({"orders": _iri("Shop_Orders")}))
        await tool.explore_graph("orders", hops=1)

        prefix = f"https://ontology-workbench.local/{NS}/"
        assert graph.calls
        for sparql in graph.calls:
            assert f'STRSTARTS(STR(?g), "{prefix}")' in sparql

    async def test_the_label_lookup_refuses_an_unsafe_label_on_its_own(self):
        # explore_graph gates the label before calling this, but the loader must not
        # depend on its caller for interpolation safety.
        tool = _tool()

        with pytest.raises(ValueError, match="unsafe table label"):
            await tool._resolve_label_to_iris("https://x/ns/", 'bad" } INSERT { ?s ?p ?o }')


_GRAPHS = [f"https://ontology-workbench.local/{NS}/g1", f"https://ontology-workbench.local/{NS}/g2"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestGraphScoping:
    """The named-graph binding: cost proportional to the namespace, not the cluster.

    ``GRAPH ?g`` + ``STRSTARTS`` matches in every graph on the cluster and filters
    afterwards. That is correct but priced by the whole store: at ~30 ingested
    namespaces the FK-edge query pinned Neptune at 99% CPU and stopped fitting in
    its 25 s timeout — and since a failed load is never cached, ``explore_graph``
    then errored on *every* call. Binding ``?g`` to the namespace's resolved graphs
    lets the quad index serve them directly.
    """

    async def test_resolved_graphs_are_named_instead_of_filtered(self):
        graph = _MockGraph(graph_rows=_GRAPHS)
        tool = _tool(graph, catalog=_catalog({"orders": _iri("Shop_Orders")}))
        r = await tool.explore_graph("orders", hops=1)

        assert "error" not in r
        walked = [c for c in graph.calls if "?ont a owl:Ontology" not in c]
        assert walked  # the FK + column queries
        for sparql in walked:
            # Constant ``GRAPH <iri>`` per graph, never a ``VALUES ?g`` join: the
            # planner can only push the graph into the index scan when it is a
            # constant. See ``query_utils.graph_scoped_body``.
            assert all(f"GRAPH <{g}> {{" in sparql for g in _GRAPHS)
            assert "VALUES ?g" not in sparql
            assert "STRSTARTS" not in sparql

    async def test_the_probe_runs_once_per_request(self):
        # It gates every other query, so re-probing would multiply the latency it
        # exists to remove. Cached across seeds and across query kinds.
        graph = _MockGraph(graph_rows=_GRAPHS, label_rows={"customers": [_iri("Shop_Customers")]})
        tool = _tool(graph, catalog=_catalog({"orders": _iri("Shop_Orders")}))
        await tool.explore_graph("orders", hops=1)
        await tool.explore_graph("customers", hops=1)

        assert sum("?ont a owl:Ontology" in c for c in graph.calls) == 1

    async def test_a_failed_probe_degrades_to_the_filter_and_is_not_retried(self, monkeypatch):
        # Degrade to slow, never to no traversal — and cache the failure, so a
        # namespace without the anchor does not pay the probe's timeout per query.
        calls: list[str] = []

        class _ProbeFails(_MockGraph):
            async def query(self, sparql, *, max_results=10000):
                calls.append(sparql)
                if "?ont a owl:Ontology" in sparql:
                    raise TimeoutError
                return await super().query(sparql, max_results=max_results)

        infos: list[tuple] = []
        monkeypatch.setattr(graph_tools.logger, "info", lambda event, **kw: infos.append((event, kw)), raising=False)
        tool = _tool(_ProbeFails(), catalog=_catalog({"orders": _iri("Shop_Orders")}))
        r = await tool.explore_graph("orders", hops=1)

        assert "error" not in r and r["tables"]  # the walk still happened
        assert sum("?ont a owl:Ontology" in c for c in calls) == 1
        assert any(e == "fk_graph_resolve_failed" for e, _ in infos)
        assert all("STRSTARTS" in c for c in calls if "?ont a owl:Ontology" not in c)

    async def test_an_unsafe_graph_iri_is_never_bound(self):
        # The probe reads IRIs out of the store; they go straight into <...> refs,
        # so they pass the same gate as a seed IRI.
        bad = _GRAPHS[0] + "> } INSERT { ?s ?p ?o "
        graph = _MockGraph(graph_rows=[bad, _GRAPHS[0]])
        tool = _tool(graph, catalog=_catalog({"orders": _iri("Shop_Orders")}))
        await tool.explore_graph("orders", hops=1)

        for sparql in graph.calls:
            assert "INSERT" not in sparql
