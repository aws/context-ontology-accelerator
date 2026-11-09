# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 applies the caller's ``allowedMetrics`` grant to governed metrics.

A caller whose grant declares ``allowedMetrics`` gets, in Tier 3, only the metric summaries the grant permits, and
graph traversal (the Tier 3 retrieval and the ``graphTraverse`` action) leaves the namespace's governed-metrics graph
out. The deep-reasoning node-type list, cached for every caller, never includes governed metrics.

The SPARQL is evaluated over an in-memory dataset (rdflib) with the same named-graph layout as Neptune: one graph per
ontology under ``{base}/{namespace}/``, with the governed metrics in the one named by
``quote(GOVERNED_METRICS_ONTOLOGY_ID)``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest
from coa_common import GOVERNED_METRICS_ONTOLOGY_ID
from coa_serve.tier3.graph_traverser import GraphTraverser
from rdflib import Dataset, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

pytestmark = pytest.mark.unit

BASE = "https://ontology-workbench.local"
NS = "sales"
DOMAIN_GRAPH = f"{BASE}/{NS}/{quote('http://ex.org/sales', safe='')}"
GOVERNED_GRAPH = f"{BASE}/{NS}/{quote(GOVERNED_METRICS_ONTOLOGY_ID, safe='')}"
CUSTOMER = "http://ex.org/sales#Customer"
MARGIN = f"urn:coa:{NS}:metric:margin_by_customer"
GOVERNED_METRIC = "urn:coa:vocab#GovernedMetric"


class _RdflibGraph:
    """A GraphClient that evaluates the SPARQL over an in-memory dataset."""

    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    async def query(self, sparql: str) -> list[dict[str, Any]]:
        result = self._dataset.query(sparql, initNs={"rdf": RDF, "rdfs": RDFS, "owl": OWL})
        return [{str(k): str(v) for k, v in row.asdict().items() if v is not None} for row in result]


@pytest.fixture
def graph() -> _RdflibGraph:
    ds = Dataset()
    domain = ds.graph(URIRef(DOMAIN_GRAPH))
    domain.add((URIRef(CUSTOMER), RDF.type, OWL.Class))
    domain.add((URIRef(CUSTOMER), RDFS.label, Literal("Customer")))
    governed = ds.graph(URIRef(GOVERNED_GRAPH))
    governed.add((URIRef(MARGIN), RDF.type, OWL.Class))
    governed.add((URIRef(MARGIN), RDFS.label, Literal("Margin by customer")))
    governed.add((URIRef(MARGIN), RDFS.comment, Literal("Gross margin per customer")))
    governed.add((URIRef(MARGIN), RDFS.subClassOf, URIRef(GOVERNED_METRIC)))
    governed.add((URIRef(GOVERNED_METRIC), RDF.type, OWL.Class))
    governed.add((URIRef(GOVERNED_METRIC), RDFS.label, Literal("Governed Metric")))
    return _RdflibGraph(ds)


def _uris(entities) -> set[str]:
    return {e.uri for e in entities}


class TestGraphTraversal:
    async def test_keyword_traversal_leaves_governed_metrics_out_when_hidden(self, graph):
        traverser = GraphTraverser(graph, graph_uri_template=f"{BASE}/{{namespace}}")

        assert _uris(await traverser.traverse("customer", NS, entities=["customer"])) == {CUSTOMER, MARGIN}
        hidden = await traverser.traverse("customer", NS, entities=["customer"], hide_governed=True)
        assert _uris(hidden) == {CUSTOMER}

    async def test_hop_traversal_leaves_governed_metrics_out_when_hidden(self, graph):
        traverser = GraphTraverser(graph, graph_uri_template=f"{BASE}/{{namespace}}")

        assert GOVERNED_METRIC in _uris(await traverser.traverse_from_uris([MARGIN], NS, max_hops=1))
        assert await traverser.traverse_from_uris([MARGIN], NS, max_hops=1, hide_governed=True) == []


class TestDeepReasoningNodeTypes:
    @pytest.mark.parametrize("graph_iris", [None, [DOMAIN_GRAPH, GOVERNED_GRAPH]], ids=["prefix", "resolved"])
    async def test_node_types_never_include_governed_metrics(self, graph, graph_iris):
        from coa_serve.tier3.agentic.ontology.source import (
            _NODE_TYPES_BODY,
            _NODE_TYPES_SELECT,
            _graph_wrapped_query,
        )

        rows = await graph.query(
            _graph_wrapped_query(_NODE_TYPES_SELECT, _NODE_TYPES_BODY, f"{BASE}/{NS}/", graph_iris)
        )

        assert {r["classUri"] for r in rows} == {CUSTOMER}, "no governed-metrics class is a node type"


class TestTier3CatalogAndTraversal:
    _CATALOG = [
        {"metric_id": "m-1", "name": "revenue_total", "description": "Total revenue"},
        {"metric_id": "m-2", "name": "margin_by_customer", "description": "Gross margin per customer"},
    ]

    async def _resolve_kwargs(self, profile: dict | None) -> dict:
        from coa_serve.trace import TraceCollector

        from .test_orchestrator import _make_orchestrator

        orch, parts = _make_orchestrator(return_parts=True)
        await orch._run_tier3(
            "what metrics are there about customers?",
            NS,
            TraceCollector(),
            profile=profile,
            metric_definitions=list(self._CATALOG),
        )
        return parts["knowledge_retriever"].resolve.call_args.kwargs

    async def test_a_restricted_caller_gets_only_permitted_metrics_and_no_governed_graph(self):
        kwargs = await self._resolve_kwargs({"allowedMetrics": ["revenue_total"]})

        assert [m["name"] for m in kwargs["catalog_summary"]] == ["revenue_total"]
        assert kwargs["hide_governed"] is True

    async def test_an_empty_grant_permits_no_metric(self):
        kwargs = await self._resolve_kwargs({"allowedMetrics": []})

        assert kwargs["catalog_summary"] is None
        assert kwargs["hide_governed"] is True

    async def test_an_unrestricted_caller_is_unchanged(self):
        kwargs = await self._resolve_kwargs({"userId": "u"})

        assert kwargs["catalog_summary"] == self._CATALOG
        assert kwargs.get("hide_governed", False) is False

    async def test_deep_reasoning_gets_only_permitted_metrics(self):
        from coa_serve.trace import TraceCollector

        from .test_orchestrator import _deep_reasoning_orchestrator

        orch = _deep_reasoning_orchestrator()
        await orch._run_tier3(
            "what metrics are there about customers?",
            NS,
            TraceCollector(),
            profile={"allowedMetrics": ["revenue_total"]},
            metric_definitions=list(self._CATALOG),
            options={"mode": "deep-reasoning"},
        )

        catalog = orch._agentic_retriever.resolve.call_args.kwargs["catalog_summary"]
        assert [m["name"] for m in catalog] == ["revenue_total"]


class TestGraphTraverseAction:
    async def _traverse_kwargs(self, profile) -> dict:
        from coa_serve.main import _handle_graph_traverse

        orch = MagicMock()
        orch._knowledge_retriever._graph.traverse_from_uris = AsyncMock(return_value=[])
        with patch("coa_serve.main._orchestrator", orch):
            result = await _handle_graph_traverse({"namespace": NS, "options": {"startUri": CUSTOMER}}, "req", profile)
        assert result["statusCode"] == 200
        return orch._knowledge_retriever._graph.traverse_from_uris.call_args.kwargs

    async def test_a_restricted_caller_traverses_without_governed_metrics(self):
        from coa_serve.role_resolver import ResolvedProfile

        kwargs = await self._traverse_kwargs(ResolvedProfile(user_id="u", allowed_metrics=["revenue_total"]))
        assert kwargs["hide_governed"] is True

    async def test_an_unrestricted_caller_is_unchanged(self):
        from coa_serve.role_resolver import ResolvedProfile

        kwargs = await self._traverse_kwargs(ResolvedProfile(user_id="u"))
        assert kwargs.get("hide_governed", False) is False
