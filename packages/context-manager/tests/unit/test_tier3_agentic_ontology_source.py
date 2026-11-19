# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ontology file and graph sources (``coa_serve.tier3.agentic.ontology.source``)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from coa_serve.tier3.agentic.ontology.source import (
    OntologyFileSource,
    OntologyGraphSource,
)

FIXTURE_TTL = str(Path(__file__).parent / "fixtures" / "ontology_sample.ttl")

# The ontology the fixture .ttl encodes, in the uniform Ontology shape.
EXPECTED_NODE_TYPES = ("Company", "FinancialReport", "Person")
EXPECTED_EDGE_TYPES = ("COMPETES_WITH", "HAS_FINANCIALS", "RELATED_TO", "WORKS_FOR")
EXPECTED_EDGE_MAP = (
    ("COMPETES_WITH", "Company", "Company"),
    ("HAS_FINANCIALS", "Company", "FinancialReport"),
    ("WORKS_FOR", "Person", "Company"),
)


class _FakeGraphClient:
    """Synchronous-bindings fake of the read-only SPARQL ``GraphClient``.

    Returns canned bindings keyed on which projection the query selects so the
    node-type and edge-type queries get their respective responses. Records the
    SPARQL it was asked to run so the test can assert the published named graph
    is targeted.
    """

    def __init__(self, node_rows, edge_rows, graph_iris=None):
        self._node_rows = node_rows
        self._edge_rows = edge_rows
        # Named-graph IRIs returned by the owl:Ontology resolve query. Default to a
        # single graph under the fixture prefix so the scoped (constant-GRAPH) path
        # is the one exercised; pass [] to simulate a resolve that finds nothing
        # (the source then falls back to the STRSTARTS prefix filter).
        self._graph_iris = (
            ["https://ontology-workbench.local/acme-namespace/onto-1"] if graph_iris is None else graph_iris
        )
        self.queries: list[str] = []

    async def query(self, sparql: str):
        self.queries.append(sparql)
        # The graph-IRI resolve query (binds ?g, anchored on owl:Ontology).
        if "owl:Ontology" in sparql:
            return [{"g": g} for g in self._graph_iris]
        if "?classLabel" in sparql:
            return list(self._node_rows)
        if "?propLabel" in sparql:
            return list(self._edge_rows)
        raise AssertionError(f"unexpected SPARQL: {sparql!r}")


def _graph_source_matching_fixture() -> tuple[OntologyGraphSource, _FakeGraphClient]:
    """An ``OntologyGraphSource`` over a fake client whose bindings mirror the .ttl.

    The bindings intentionally reproduce the fixture: three classes, three
    fully-directional object properties, and one object property (``RELATED_TO``)
    with no domain/range — so a successful parity assertion proves both backends
    produce the identical shape from equivalent source data.
    """
    node_rows = [{"classLabel": label} for label in ("Company", "FinancialReport", "Person")]
    edge_rows = [
        {"propLabel": "COMPETES_WITH", "domainLabel": "Company", "rangeLabel": "Company"},
        {"propLabel": "HAS_FINANCIALS", "domainLabel": "Company", "rangeLabel": "FinancialReport"},
        {"propLabel": "WORKS_FOR", "domainLabel": "Person", "rangeLabel": "Company"},
        {"propLabel": "RELATED_TO"},  # no domain/range → edge type only, no edge_map triple
    ]
    client = _FakeGraphClient(node_rows, edge_rows)
    # Per-ontology graphs live under a namespace prefix from this template — the
    # same shape the serve GraphTraverser + the ontology write side use.
    return OntologyGraphSource(client, graph_uri_template="https://ontology-workbench.local/{namespace}"), client


@pytest.mark.unit
class TestOntologyFileSource:
    async def test_parses_fixture_into_expected_shape(self):
        onto = await OntologyFileSource(FIXTURE_TTL).load("any-namespace")
        assert onto.node_types == EXPECTED_NODE_TYPES
        assert onto.edge_types == EXPECTED_EDGE_TYPES
        assert onto.edge_map == EXPECTED_EDGE_MAP

    async def test_missing_file_raises(self):
        # A missing/unreadable .ttl surfaces as an OSError (FileNotFoundError) at
        # load time, mirroring the graph source's raise-on-failure contract.
        with pytest.raises(OSError):
            await OntologyFileSource("/nonexistent/path/ontology.ttl").load("ns")


@pytest.mark.unit
class TestOntologyGraphSource:
    async def test_shapes_sparql_response_into_expected_shape(self):
        source, _ = _graph_source_matching_fixture()
        onto = await source.load("acme-namespace")
        assert onto.node_types == EXPECTED_NODE_TYPES
        assert onto.edge_types == EXPECTED_EDGE_TYPES
        assert onto.edge_map == EXPECTED_EDGE_MAP

    async def test_scopes_queries_to_resolved_graph_iris(self):
        """Scopes the type queries to the namespace's resolved graph IRIs with a
        constant GRAPH <iri> (pushed into the index scan), NOT a cluster-wide
        GRAPH ?g + STRSTARTS filter. One cheap owl:Ontology resolve runs first."""
        source, client = _graph_source_matching_fixture()
        await source.load("acme-namespace")
        # 3 queries: 1 graph-IRI resolve (owl:Ontology) + 2 scoped type queries.
        assert len(client.queries) == 3
        resolve = client.queries[0]
        assert "owl:Ontology" in resolve
        for sparql in client.queries[1:]:
            assert "GRAPH <https://ontology-workbench.local/acme-namespace/onto-1>" in sparql
            assert "GRAPH ?g" not in sparql  # scoped, not the cluster-wide form

    async def test_falls_back_to_prefix_filter_when_resolve_finds_no_graphs(self):
        """When the graph-IRI resolve yields nothing, degrade to the GRAPH ?g +
        STRSTARTS prefix filter (slow but correct) rather than failing the lookup."""
        node_rows = [{"classLabel": "Company"}]
        edge_rows = [{"propLabel": "WORKS_FOR", "domainLabel": "Person", "rangeLabel": "Company"}]
        client = _FakeGraphClient(node_rows, edge_rows, graph_iris=[])
        source = OntologyGraphSource(client, graph_uri_template="https://ontology-workbench.local/{namespace}")
        onto = await source.load("acme-namespace")
        assert onto.node_types == ("Company",)
        # resolve + 2 fallback type queries; the type queries use the prefix filter.
        for sparql in client.queries[1:]:
            assert 'STRSTARTS(STR(?g), "https://ontology-workbench.local/acme-namespace/")' in sparql

    async def test_empty_ontology_when_no_graph_uri_template(self):
        """With no graph-URI template the namespace has no queryable ontology graph,
        so load returns an empty Ontology WITHOUT issuing an unscoped query."""
        source, client = _graph_source_matching_fixture()
        source._graph_uri_template = ""  # simulate unconfigured template
        onto = await source.load("acme-namespace")
        assert onto.node_types == () and onto.edge_types == ()
        assert client.queries == []

    async def test_invalid_namespace_rejected(self):
        source, client = _graph_source_matching_fixture()
        with pytest.raises(ValueError):
            await source.load("bad namespace!")  # space + bang are invalid
        assert client.queries == []

    async def test_slow_lookup_times_out_fast_and_raises(self, monkeypatch):
        """A hung graph query must fail fast and RAISE, not report an empty ontology."""
        import asyncio as _asyncio

        monkeypatch.setenv("DEEP_REASONING_ONTOLOGY_TIMEOUT_S", "0.05")

        class _HangingGraphClient:
            async def query(self, sparql: str):
                await _asyncio.sleep(5)  # far longer than the 0.05s cap
                return []

        source = OntologyGraphSource(
            _HangingGraphClient(), graph_uri_template="https://ontology-workbench.local/{namespace}"
        )
        with pytest.raises(TimeoutError):
            await source.load("acme-namespace")

    async def test_resolve_times_out_and_falls_back_to_prefix_filter(self, monkeypatch):
        """A hung graph-IRI resolve degrades to the prefix filter WITHIN the budget."""
        import asyncio as _asyncio

        # Keep the OUTER lookup budget generous so this test proves the INNER resolve
        # timeout (not the outer one) is what triggers the fallback.
        monkeypatch.setenv("DEEP_REASONING_ONTOLOGY_TIMEOUT_S", "6")
        monkeypatch.setattr("coa_serve.tier3.agentic.ontology.source._GRAPH_IRI_RESOLVE_TIMEOUT_S", 0.05)

        class _HangingResolveClient:
            def __init__(self):
                self.queries: list[str] = []

            async def query(self, sparql: str):
                self.queries.append(sparql)
                if "owl:Ontology" in sparql:  # the resolve query — hang it
                    await _asyncio.sleep(5)
                    return []
                if "?classLabel" in sparql:
                    return [{"classLabel": "Company"}]
                if "?propLabel" in sparql:
                    return [{"propLabel": "WORKS_FOR", "domainLabel": "Person", "rangeLabel": "Company"}]
                raise AssertionError(f"unexpected SPARQL: {sparql!r}")

        client = _HangingResolveClient()
        source = OntologyGraphSource(client, graph_uri_template="https://ontology-workbench.local/{namespace}")
        onto = await source.load("acme-namespace")

        # Fallback ran: the type queries resolved a real ontology despite the hung resolve.
        assert "Company" in onto.node_types
        assert "WORKS_FOR" in onto.edge_types
        # The two type queries used the prefix filter (no constant GRAPH <iri> inlined).
        type_queries = [q for q in client.queries if "owl:Ontology" not in q]
        assert len(type_queries) == 2
        assert all("STRSTARTS" in q for q in type_queries)

    async def test_timeout_env_floored_at_one_second(self, monkeypatch):
        """The fast-fail timeout can be tuned but never disabled (floored at 1s)."""
        from coa_serve.tier3.agentic.ontology.source import _ontology_lookup_timeout_s

        monkeypatch.setenv("DEEP_REASONING_ONTOLOGY_TIMEOUT_S", "0")
        assert _ontology_lookup_timeout_s() == 1.0
        monkeypatch.setenv("DEEP_REASONING_ONTOLOGY_TIMEOUT_S", "not-a-number")
        assert _ontology_lookup_timeout_s() == 6.0
        monkeypatch.delenv("DEEP_REASONING_ONTOLOGY_TIMEOUT_S", raising=False)
        assert _ontology_lookup_timeout_s() == 6.0


@pytest.mark.unit
class TestSourceShapeParity:
    async def test_file_and_graph_sources_produce_identical_ontology(self):
        """Both backends return the SAME Ontology shape from equivalent data.

        Equality of the two frozen :class:`Ontology` instances covers node types,
        edge types, AND the directional source→target ``edge_map`` per edge type.
        """
        file_onto = await OntologyFileSource(FIXTURE_TTL).load("ns")
        graph_source, _ = _graph_source_matching_fixture()
        graph_onto = await graph_source.load("ns")

        assert file_onto == graph_onto


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_ontology_source_does_not_import_graphrag(self):
        """``import ...ontology.source`` must NOT import ``graphrag_toolkit``.

        Runs in a fresh subprocess (current ``sys.path`` propagated) so a
        ``graphrag_toolkit`` already imported by a sibling test cannot mask a
        regression."""
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.ontology.source; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            "importing ontology.source leaked graphrag_toolkit or failed.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
