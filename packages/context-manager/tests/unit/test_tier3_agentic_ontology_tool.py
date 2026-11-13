# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Ontology_Lookup_Tool layer (``coa_serve.tier3.agentic.tools.ontology_tool``)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from coa_serve.tier3.agentic.ontology import Ontology
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.ontology_tool import (
    OntologyLookupTool,
    build_ontology_tool,
    ontology_from_result,
)

# A populated ontology (node types, edge types, and directional source→target triples).
_POPULATED = Ontology(
    node_types=("Company", "FinancialReport", "Person"),
    edge_types=("COMPETES_WITH", "HAS_FINANCIALS", "WORKS_FOR"),
    edge_map=(
        ("COMPETES_WITH", "Company", "Company"),
        ("HAS_FINANCIALS", "Company", "FinancialReport"),
        ("WORKS_FOR", "Person", "Company"),
    ),
)

# A successful-but-empty ontology (no node types, no edge types).
_EMPTY = Ontology(node_types=(), edge_types=(), edge_map=())


class FakeOntologySource:
    """Mock :class:`OntologySource` counting ``load`` calls per namespace.

    ``load`` either returns the configured :class:`Ontology` or, when ``raises`` is
    set, raises it — letting a single fake exercise the success, empty, and
    load-failure paths. ``calls`` records the namespaces passed so the cache-reuse
    and namespace-scoping assertions are exact.
    """

    def __init__(self, ontology: Ontology | None = None, *, raises: Exception | None = None) -> None:
        self._ontology = ontology if ontology is not None else _POPULATED
        self._raises = raises
        self.calls: list[str] = []

    async def load(self, namespace: str) -> Ontology:
        self.calls.append(namespace)
        if self._raises is not None:
            raise self._raises
        return self._ontology


@pytest.mark.unit
class TestToolInterface:
    def test_is_registrable_as_a_tool(self):
        """An ontology tool satisfies the registry's four-field contract."""
        registry = ToolRegistry()
        registry.register(OntologyLookupTool(FakeOntologySource()))
        assert registry.names() == ["ontology_lookup"]

    def test_registry_specs_expose_the_four_fields(self):
        registry = ToolRegistry()
        registry.register(OntologyLookupTool(FakeOntologySource()))
        spec = registry.specs()[0]
        assert spec["name"] == "ontology_lookup"
        assert spec["description"].strip()
        assert isinstance(spec["input_schema"], dict)
        assert isinstance(spec["output_schema"], dict)


@pytest.mark.unit
class TestInvokeReturnsOntology:
    async def test_invoke_returns_node_edge_types_and_directional_edge_map(self):
        tool = OntologyLookupTool(FakeOntologySource(_POPULATED))

        out = await tool.invoke(namespace="acme")

        onto = ontology_from_result(out)
        assert onto is not None
        assert onto.node_types == _POPULATED.node_types
        assert onto.edge_types == _POPULATED.edge_types
        assert onto.edge_map == _POPULATED.edge_map

    async def test_detail_summarizes_a_populated_ontology(self):
        tool = OntologyLookupTool(FakeOntologySource(_POPULATED))
        out = await tool.invoke(namespace="acme")
        assert "3 node types" in out.detail
        assert "3 edge types" in out.detail
        assert "empty" not in out.detail
        assert "failed" not in out.detail

    async def test_invoke_scopes_load_to_request_namespace(self):
        source = FakeOntologySource(_POPULATED)
        tool = OntologyLookupTool(source)
        await tool.invoke(namespace="tenant-x")
        assert source.calls == ["tenant-x"]


@pytest.mark.unit
class TestPerRequestCaching:
    async def test_second_lookup_in_a_request_reuses_cache_single_load(self):
        """A repeat lookup for the same namespace performs a SINGLE source load."""
        source = FakeOntologySource(_POPULATED)
        tool = OntologyLookupTool(source)

        first = await tool.invoke(namespace="acme")
        second = await tool.invoke(namespace="acme")

        # The source was loaded exactly once across the two lookups.
        assert source.calls == ["acme"]
        # Both lookups return the same ontology shape...
        assert ontology_from_result(first) == ontology_from_result(second)
        # ...and the reuse is observable in the trace detail.
        assert "(cached)" in second.detail
        assert "(cached)" not in first.detail

    async def test_distinct_namespaces_load_independently(self):
        source = FakeOntologySource(_POPULATED)
        tool = OntologyLookupTool(source)

        await tool.invoke(namespace="ns-a")
        await tool.invoke(namespace="ns-b")
        await tool.invoke(namespace="ns-a")  # cached

        # One load per distinct namespace; the repeat of ns-a is served from cache.
        assert source.calls == ["ns-a", "ns-b"]

    async def test_empty_ontology_is_NOT_cached_and_reloads(self):
        """An EMPTY load is NOT cached, so an ontology accepted later still appears."""
        source = FakeOntologySource(_EMPTY)
        tool = OntologyLookupTool(source)

        first = await tool.invoke(namespace="acme")
        second = await tool.invoke(namespace="acme")

        # Re-loaded (not served from cache), and never marked cached.
        assert source.calls == ["acme", "acme"]
        assert "(cached)" not in first.detail
        assert "(cached)" not in second.detail

    async def test_empty_then_populated_self_heals(self):
        """An empty result followed by a populated one returns the populated ontology."""
        source = FakeOntologySource(_EMPTY)
        tool = OntologyLookupTool(source)
        empty = await tool.invoke(namespace="acme")
        assert ontology_from_result(empty) == _EMPTY

        # Ontology is now induced/accepted → source returns populated.
        source._ontology = _POPULATED
        healed = await tool.invoke(namespace="acme")
        onto = ontology_from_result(healed)
        assert onto is not None and onto.node_types  # populated, not the stale empty
        # And now it IS cached (populated loads are).
        cached = await tool.invoke(namespace="acme")
        assert "(cached)" in cached.detail


@pytest.mark.unit
class TestEmptyVersusFailureAreDistinct:
    async def test_empty_ontology_is_a_successful_distinct_outcome(self):
        """Empty ontology: non-degraded result, extractable empty Ontology."""
        tool = OntologyLookupTool(FakeOntologySource(_EMPTY))

        out = await tool.invoke(namespace="acme")

        onto = ontology_from_result(out)
        # A successful empty load yields an Ontology object (not None) with empty sets.
        assert onto is not None
        assert onto.node_types == ()
        assert onto.edge_types == ()
        assert onto.edge_map == ()
        assert "empty" in out.detail
        assert "failed" not in out.detail

    async def test_load_failure_is_a_distinct_degraded_outcome(self):
        """Load failure: degraded result, no extractable Ontology, names the source."""
        source = FakeOntologySource(raises=RuntimeError("backend down"))
        tool = OntologyLookupTool(source, source_name="OntologyGraphSource")

        out = await tool.invoke(namespace="acme")  # must NOT raise

        # No ontology item → ontology_from_result signals failure with None.
        assert ontology_from_result(out) is None
        assert out.items == ()
        assert "failed" in out.detail
        assert "RuntimeError" in out.detail
        # The source in use is named in the trace detail.
        assert "OntologyGraphSource" in out.detail

    async def test_empty_and_failure_outcomes_differ(self):
        """The two outcomes are programmatically separable, not just textually."""
        empty_out = await OntologyLookupTool(FakeOntologySource(_EMPTY)).invoke(namespace="ns")
        failure_out = await OntologyLookupTool(FakeOntologySource(raises=ValueError("nope"))).invoke(namespace="ns")

        # Empty → an Ontology (truthy object); failure → None.
        assert ontology_from_result(empty_out) is not None
        assert ontology_from_result(failure_out) is None
        # Distinct detail markers.
        assert "empty" in empty_out.detail and "failed" not in empty_out.detail
        assert "failed" in failure_out.detail and "empty" not in failure_out.detail

    async def test_load_failure_is_not_cached_and_retries(self):
        """A failure is not cached, so a later lookup attempts the source again."""
        source = FakeOntologySource(raises=RuntimeError("transient"))
        tool = OntologyLookupTool(source)

        await tool.invoke(namespace="acme")
        await tool.invoke(namespace="acme")

        # Both attempts hit the source — the failure was not cached.
        assert source.calls == ["acme", "acme"]

    async def test_source_name_defaults_to_class_name(self):
        source = FakeOntologySource(raises=RuntimeError("x"))
        tool = OntologyLookupTool(source)  # no explicit source_name
        out = await tool.invoke(namespace="ns")
        assert "FakeOntologySource" in out.detail


@pytest.mark.unit
class TestBuildOntologyTool:
    def test_registers_single_ontology_lookup_tool(self):
        registry = ToolRegistry()

        name = build_ontology_tool(registry, FakeOntologySource())

        assert name == "ontology_lookup"
        assert registry.names() == ["ontology_lookup"]

    async def test_built_tool_invokes_source(self):
        registry = ToolRegistry()
        source = FakeOntologySource(_POPULATED)
        build_ontology_tool(registry, source, source_name="OntologyFileSource")

        tool = registry.get("ontology_lookup")
        out = await tool.invoke(namespace="ns")

        assert ontology_from_result(out) == _POPULATED
        assert source.calls == ["ns"]


@pytest.mark.unit
class TestOntologyFromResult:
    def test_returns_none_for_a_result_without_an_ontology_item(self):
        from coa_serve.tier3.agentic.tools.base import ToolResult

        assert ontology_from_result(ToolResult(detail="no ontology here")) is None


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_ontology_tool_does_not_import_graphrag(self):
        """``import ...tools.ontology_tool`` must NOT import ``graphrag_toolkit``.

        Runs in a fresh subprocess (current ``sys.path`` propagated) so a
        ``graphrag_toolkit`` already imported by a sibling test cannot mask a
        regression."""
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.tools.ontology_tool; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            "importing ontology_tool leaked graphrag_toolkit or failed.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
