# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever tool-registry factory (task 17).

Covers ``coa_serve.tier3.agentic.factory``:

- Endpoint-gated registration (Req 12.5-12.6): with the Neptune / OpenSearch
  endpoints unconfigured, the strategy / graph / vector tools are OMITTED and a
  ``structlog`` warning names each omitted tool and the missing endpoint(s); the
  Ontology_Lookup_Tool is ALWAYS present (Req 12.4).
- With both store endpoints configured, the full tool set — the seven
  ``strategy:<value>`` tools plus ``graph_traversal``, ``vector_search``, and
  ``ontology_lookup`` — is registered (Req 2.2, 2.3).
- Ontology source selection (Req 3.2, 12.7): the file source is selected when
  ``deep_reasoning_ontology_source="file"`` and the graph source when ``"graph"``.
- ``build_agentic_retriever`` composes the registry, controller, decomposer,
  synthesizer, and budget into an :class:`AgenticRetriever`.
- The Req 12.3 lazy-import invariant: importing ``factory`` does not import
  ``graphrag_toolkit``.

The "all tools present" test enumerates the real ``RetrieverStrategy`` enum and
builds the lexical adapter, which transitively imports ``graphrag_toolkit`` — but
at test-run time, never at module load, preserving the invariant the dedicated
subprocess test asserts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import structlog
from coa_serve.config import ServiceConfig
from coa_serve.tier3.agentic.factory import (
    build_agentic_retriever,
    build_budget_config,
    build_ontology_source,
    build_tool_registry,
)
from coa_serve.tier3.agentic.ontology import (
    OntologyFileSource,
    OntologyGraphSource,
)
from coa_serve.tier3.agentic.retriever import AgenticRetriever

ONTOLOGY_TOOL = "ontology_lookup"
GRAPH_TOOL = "graph_traversal"
CHUNK_TOOL = "chunk_window"
VECTOR_TOOL = "vector_search"


# ── test doubles ───────────────────────────────────────────────────


def make_config(**overrides) -> ServiceConfig:
    """Build a ServiceConfig with safe defaults, overriding fields per test."""
    base = dict(
        guardrail_id="",
        vkg_endpoint="http://vkg.local:8080",
        neptune_endpoint="",
        opensearch_endpoint="",
        bedrock_model_id="model",
        bedrock_region="us-east-1",
        data_sources_table="",
        metric_definitions_table="",
        memory_id="",
        session_metadata_table="",
    )
    base.update(overrides)
    return ServiceConfig(**base)


class FakeLLM:
    """Minimal embed/converse client standing in for ``clients.llm``."""

    async def embed(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0]

    async def converse(self, *args, **kwargs):
        return SimpleNamespace(text="{}", guardrail_blocked=False)


def make_clients() -> SimpleNamespace:
    """A ClientSet-shaped object exposing ``graph`` / ``vector`` / ``llm``."""
    return SimpleNamespace(graph=object(), vector=object(), llm=FakeLLM())


class FakeStepPlanner:
    """A no-op StepPlanner for composing the controller in the assembly test."""

    async def propose_steps(self, *, sub_question, context, tool_specs):
        return []

    async def check_sufficiency(self, *, sub_question, context):
        return False


def _omission_events(logs: list[dict]) -> dict[str, dict]:
    """Index ``agentic_tool_omitted`` warnings by the tool they name."""
    return {
        log["tool"]: log
        for log in logs
        if log.get("event") == "agentic_tool_omitted" and log.get("log_level") == "warning"
    }


# ── endpoint-gated omission (Req 12.5-12.6) ────────────────────────


@pytest.mark.unit
class TestEndpointGatedOmission:
    def test_unconfigured_endpoints_omit_tools_with_warning(self):
        """No endpoints → only the ontology tool, with a warning per omitted tool."""
        config = make_config(neptune_endpoint="", opensearch_endpoint="")
        clients = make_clients()

        with structlog.testing.capture_logs() as logs:
            registry = build_tool_registry(config, clients)

        # Only the always-on ontology tool survives (Req 12.4).
        assert registry.names() == [ONTOLOGY_TOOL]
        assert GRAPH_TOOL not in registry.names()
        assert VECTOR_TOOL not in registry.names()
        assert not [n for n in registry.names() if n.startswith("strategy:")]

        # Each omitted tool is named with its missing endpoint(s) (Req 12.6).
        omissions = _omission_events(logs)
        assert GRAPH_TOOL in omissions
        assert omissions[GRAPH_TOOL]["missing_endpoints"] == ["neptune_endpoint"]
        assert VECTOR_TOOL in omissions
        assert "opensearch_endpoint" in omissions[VECTOR_TOOL]["missing_endpoints"]
        strategy_omission = next(t for t in omissions if t.startswith("strategy:"))
        assert set(omissions[strategy_omission]["missing_endpoints"]) == {
            "neptune_endpoint",
            "opensearch_endpoint",
        }

    def test_graph_tool_present_vector_omitted_when_only_neptune_set(self):
        """Neptune-only → graph tool present, vector + strategy tools omitted."""
        config = make_config(neptune_endpoint="my-neptune:8182", opensearch_endpoint="")
        clients = make_clients()

        with structlog.testing.capture_logs() as logs:
            registry = build_tool_registry(config, clients)

        assert GRAPH_TOOL in registry.names()
        assert VECTOR_TOOL not in registry.names()
        assert ONTOLOGY_TOOL in registry.names()
        # No strategy tools (they need opensearch too).
        assert not [n for n in registry.names() if n.startswith("strategy:")]

        omissions = _omission_events(logs)
        assert VECTOR_TOOL in omissions
        assert "opensearch_endpoint" in omissions[VECTOR_TOOL]["missing_endpoints"]

    def test_vector_omitted_when_embed_client_missing(self):
        """OpenSearch set but no embed client → vector tool omitted, names embed_client."""
        config = make_config(neptune_endpoint="", opensearch_endpoint="my-aoss")
        clients = SimpleNamespace(graph=object(), vector=object(), llm=None)

        with structlog.testing.capture_logs() as logs:
            registry = build_tool_registry(config, clients)

        assert VECTOR_TOOL not in registry.names()
        omissions = _omission_events(logs)
        assert "embed_client" in omissions[VECTOR_TOOL]["missing_endpoints"]

    def test_ontology_tool_always_present(self):
        """The ontology tool is registered regardless of endpoint configuration."""
        for neptune, opensearch in (("", ""), ("n:8182", ""), ("", "aoss")):
            config = make_config(neptune_endpoint=neptune, opensearch_endpoint=opensearch)
            registry = build_tool_registry(config, make_clients())
            assert ONTOLOGY_TOOL in registry.names()


# ── full registration when configured (Req 2.2, 2.3) ───────────────


@pytest.mark.unit
class TestFullRegistration:
    def test_all_tools_present_when_endpoints_configured(self):
        """Both store endpoints set → 7 strategy + graph + vector + ontology.

        chunk_window (idea 6) is DISABLED by default (opt-in via
        AGENTIC_ENABLE_CHUNK_WINDOW), so it is NOT in the default registry.
        """
        config = make_config(
            neptune_endpoint="my-neptune-host:8182",
            opensearch_endpoint="my-aoss-endpoint",
        )
        clients = make_clients()

        registry = build_tool_registry(config, clients)
        names = registry.names()

        strategy_names = [n for n in names if n.startswith("strategy:")]
        assert len(strategy_names) == 8, names  # 7 traversal + topic_beam (now always registered)
        assert "strategy:topic_beam" in names
        assert GRAPH_TOOL in names
        assert CHUNK_TOOL not in names  # disabled by default (idea 6 opt-in)
        assert VECTOR_TOOL in names
        assert ONTOLOGY_TOOL in names
        # 8 strategy + graph + vector + ontology.
        assert len(names) == 11  # nl_to_sql/nl_to_sparql need a StructuredQueryTier (not supplied here)

    def test_chunk_window_registered_when_enabled(self, monkeypatch):
        """AGENTIC_ENABLE_CHUNK_WINDOW=1 + neptune → chunk_window is registered (idea 6)."""
        monkeypatch.setenv("AGENTIC_ENABLE_CHUNK_WINDOW", "1")
        config = make_config(
            neptune_endpoint="my-neptune-host:8182",
            opensearch_endpoint="my-aoss-endpoint",
        )
        registry = build_tool_registry(config, make_clients())
        names = registry.names()
        assert CHUNK_TOOL in names
        # 8 strategy + graph + chunk_window + vector + ontology.
        assert len(names) == 12


# ── ontology source selection (Req 3.2, 12.7) ──────────────────────


@pytest.mark.unit
class TestOntologySourceSelection:
    def test_file_source_selected_when_configured(self):
        """``deep_reasoning_ontology_source="file"`` → OntologyFileSource over the file path."""
        config = make_config(deep_reasoning_ontology_source="file", deep_reasoning_ontology_file="/tmp/onto.ttl")
        source = build_ontology_source(config, make_clients())
        assert isinstance(source, OntologyFileSource)

    def test_file_source_is_what_the_registry_wires(self):
        """The registry's ontology tool is backed by the file source when configured."""
        config = make_config(deep_reasoning_ontology_source="file", deep_reasoning_ontology_file="/tmp/onto.ttl")
        registry = build_tool_registry(config, make_clients())
        tool = registry.get(ONTOLOGY_TOOL)
        assert isinstance(tool._source, OntologyFileSource)

    def test_graph_source_selected_when_configured(self):
        """``deep_reasoning_ontology_source="graph"`` → OntologyGraphSource over the graph client."""
        config = make_config(deep_reasoning_ontology_source="graph")
        source = build_ontology_source(config, make_clients())
        assert isinstance(source, OntologyGraphSource)

    def test_file_source_without_path_falls_back_to_graph(self):
        """Defense-in-depth: a 'file' source with an empty path would make rdflib
        parse the CWD (IsADirectoryError); the factory falls back to the graph
        source rather than build a source that fails every lookup."""
        config = make_config(deep_reasoning_ontology_source="file", deep_reasoning_ontology_file="")
        source = build_ontology_source(config, make_clients())
        assert isinstance(source, OntologyGraphSource)


# ── budget config mapping ──────────────────────────────────────────


@pytest.mark.unit
class TestBudgetConfig:
    def test_budget_built_from_config_fields(self):
        config = make_config(
            deep_reasoning_time_budget_s=45,
            deep_reasoning_max_steps=12,
            deep_reasoning_per_tool_timeout_s=20,
            deep_reasoning_max_fanout=7,
        )
        budget = build_budget_config(config)
        assert budget.time_budget_s == 45.0
        assert budget.max_steps == 12
        assert budget.per_tool_timeout_s == 20.0
        assert budget.max_fanout == 7


# ── full assembly ──────────────────────────────────────────────────


@pytest.mark.unit
class TestBuildAgenticRetriever:
    def test_composes_an_agentic_retriever(self, monkeypatch):
        """``build_agentic_retriever`` returns a composed retriever (no endpoints)."""
        monkeypatch.setenv("ENVIRONMENT", "local")
        config = make_config(neptune_endpoint="", opensearch_endpoint="")
        clients = make_clients()

        retriever = build_agentic_retriever(config, clients, FakeStepPlanner())

        assert isinstance(retriever, AgenticRetriever)
        # The composed registry still always carries the ontology tool.
        assert ONTOLOGY_TOOL in retriever._registry.names()

    def test_threads_guardrail_provider_into_synthesizer_and_decomposer(self, monkeypatch):
        """The injected provider must reach BOTH agentic-built consumers.

        Guards the silent-drift seam — a future refactor could drop the provider
        arg and the agentic path would silently revert to a frozen guardrail. We
        assert each composed consumer resolves the provider's LIVE value, and that
        flipping it is reflected without reconstruction.
        """
        monkeypatch.setenv("ENVIRONMENT", "local")
        config = make_config(neptune_endpoint="", opensearch_endpoint="", guardrail_id="gr-startup")
        clients = make_clients()

        current = {"gid": "gr-old"}
        retriever = build_agentic_retriever(
            config, clients, FakeStepPlanner(), guardrail_id_provider=lambda: current["gid"]
        )

        # Both agentic-path consumers must read through the provider, not the frozen id.
        assert retriever._synthesizer._effective_guardrail_id() == "gr-old"
        assert retriever._decomposer._effective_guardrail_id() == "gr-old"

        current["gid"] = "gr-new"
        assert retriever._synthesizer._effective_guardrail_id() == "gr-new"
        assert retriever._decomposer._effective_guardrail_id() == "gr-new"


# ── lazy-import invariant (Req 12.3) ───────────────────────────────


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_factory_does_not_import_graphrag(self):
        """``import ...agentic.factory`` must NOT import ``graphrag_toolkit``.

        Runs in a fresh subprocess (current ``sys.path`` propagated) so a
        ``graphrag_toolkit`` already imported by a sibling test cannot mask a
        regression (mirrors the task-1 invariant test)."""
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.factory; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            f"importing factory leaked graphrag_toolkit or failed.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
