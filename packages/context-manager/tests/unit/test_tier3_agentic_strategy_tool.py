# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Retrieval_Strategy_Tool layer (task 6).

Covers ``coa_serve.tier3.agentic.tools.strategy_tool``:

- :func:`build_strategy_tools` registers exactly one Tool per
  ``RetrieverStrategy`` enum member, each named ``strategy:<value>`` (Req 2.2),
  and each Tool presents the four required interface fields (Req 11.1).
- :meth:`RetrievalStrategyTool.invoke` delegates to the wrapped
  ``BaselineLexicalRetriever`` and returns the native chunks/entities plus their
  normalized ``items`` (Req 5.1).
- An adapter error/timeout (the adapter degrades to an empty result with an
  error trace step, or — defensively — raises) surfaces as a degraded
  :class:`ToolResult`; the Tool NEVER raises (Req 9.1-9.2).
- Module load of ``strategy_tool`` does NOT import ``graphrag_toolkit``
  (Req 12.3).

The adapter is mocked with a lightweight fake exposing the same ``retrieve``
coroutine and a ``BaselineResult``-shaped return (``.chunks`` / ``.entities`` /
``.trace_steps``), so the invoke-path tests need no ``graphrag_toolkit`` access.
The one-tool-per-strategy test does enumerate the real ``RetrieverStrategy``
enum (which transitively imports the toolkit) — but at test-run time, never at
module load, preserving the invariant the dedicated subprocess test asserts.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.strategy_tool import (
    RetrievalStrategyTool,
    build_strategy_tools,
)

# The seven non-deprecated graphrag-toolkit strategies (mirrors
# ``RetrieverStrategy``); kept as plain strings so most tests need no enum/
# toolkit import.
_STRATEGY_VALUES = (
    "chunk_based",
    "chunk_based_semantic",
    "entity_based",
    "entity_context",
    "entity_network",
    "topic_based",
    "traversal",
    "topic_beam",
)


def _fake_chunk(chunk_id: str = "c1", text: str = "hello", source_doc: str = "doc-1"):
    """A ``ChunkResult``-shaped stand-in (attribute access only)."""
    return SimpleNamespace(chunk_id=chunk_id, text=text, source_doc=source_doc, relevance_score=0.9)


def _fake_entity(uri: str = "urn:e:amazon", label: str = "Amazon", type: str = "Company"):
    """A ``GraphEntity``-shaped stand-in (attribute access only)."""
    return SimpleNamespace(uri=uri, label=label, type=type)


def _baseline_result(*, chunks=(), entities=(), trace_steps=(), dropped_anonymous_chunks=0):
    """A ``BaselineResult``-shaped stand-in."""
    return SimpleNamespace(
        chunks=tuple(chunks),
        entities=tuple(entities),
        trace_steps=tuple(trace_steps),
        dropped_anonymous_chunks=dropped_anonymous_chunks,
    )


class FakeStrategy:
    """A ``RetrieverStrategy``-shaped stand-in exposing only ``.value``.

    ``RetrievalStrategyTool`` reads only ``strategy.value`` at construction (for
    the name and description), so a fake member is sufficient for the
    construction/interface tests and needs no toolkit import. The invoke-path
    tests, by contrast, use a REAL :class:`RetrieverStrategy` member (via
    :func:`_real_strategy`) because ``invoke`` resolves the strategy's
    ``StrategySpec`` from the real ``STRATEGY_REGISTRY``.
    """

    def __init__(self, value: str) -> None:
        self.value = value


def _real_strategy(value: str):
    """Return the real ``RetrieverStrategy`` member for ``value``.

    Lazily imports the enum (which transitively imports the toolkit) — at
    test-run time, never at this module's load — so the invoke path resolves a
    genuine ``StrategySpec`` exactly as in production.
    """
    from coa_serve.lexical.strategies import RetrieverStrategy

    return RetrieverStrategy(value)


class FakeLexical:
    """Mock of ``BaselineLexicalRetriever`` capturing the last ``retrieve`` call.

    ``retrieve`` either returns the configured ``BaselineResult`` stand-in or, when
    ``raises`` is set, raises it — letting a single fake exercise both the normal
    path and the defensive never-raise guard.
    """

    def __init__(self, result=None, *, raises: Exception | None = None) -> None:
        self._result = result if result is not None else _baseline_result()
        self._raises = raises
        self.calls: list[dict] = []

    async def retrieve(self, query, namespace, strategy=None):
        self.calls.append({"query": query, "namespace": namespace, "strategy": strategy})
        if self._raises is not None:
            raise self._raises
        return self._result


# ── Interface fields & naming ──────────────────────────────────────


@pytest.mark.unit
class TestToolInterface:
    def test_name_is_strategy_prefixed_value(self):
        tool = RetrievalStrategyTool(FakeStrategy("entity_network"), FakeLexical())
        assert tool.name == "strategy:entity_network"

    def test_description_is_non_empty_and_strategy_specific(self):
        chunk_tool = RetrievalStrategyTool(FakeStrategy("chunk_based"), FakeLexical())
        entity_tool = RetrievalStrategyTool(FakeStrategy("entity_network"), FakeLexical())
        assert chunk_tool.description.strip()
        assert entity_tool.description.strip()
        assert chunk_tool.description != entity_tool.description

    def test_unknown_strategy_value_still_gets_a_description(self):
        """An unmapped value gets a generic (still non-empty) description so the
        Tool remains registrable (Req 11.1)."""
        tool = RetrievalStrategyTool(FakeStrategy("some_future_strategy"), FakeLexical())
        assert "some_future_strategy" in tool.description

    def test_declares_dict_input_and_output_schemas(self):
        tool = RetrievalStrategyTool(FakeStrategy("chunk_based"), FakeLexical())
        assert isinstance(tool.input_schema, dict) and "query" in tool.input_schema
        assert isinstance(tool.output_schema, dict)

    def test_is_registrable_as_a_tool(self):
        """A strategy tool satisfies the registry's four-field contract (Req 11.1)."""
        registry = ToolRegistry()
        registry.register(RetrievalStrategyTool(FakeStrategy("chunk_based"), FakeLexical()))
        assert registry.names() == ["strategy:chunk_based"]


# ── invoke() returns chunks/entities ───────────────────────────────


@pytest.mark.unit
class TestInvokeReturnsResults:
    async def test_invoke_returns_chunks_and_entities(self):
        result = _baseline_result(
            chunks=(_fake_chunk("c1"), _fake_chunk("c2", text="world")),
            entities=(_fake_entity(),),
            trace_steps=({"step": "lexical_baseline_retrieve", "status": "success", "durationMs": 5},),
        )
        lexical = FakeLexical(result)
        tool = RetrievalStrategyTool(_real_strategy("entity_based"), lexical)

        out = await tool.invoke(namespace="ns-1", query="Amazon competitors")

        assert len(out.chunks) == 2
        assert len(out.entities) == 1
        # Native objects are passed through untouched for synthesis.
        assert out.chunks[0].chunk_id == "c1"
        assert out.entities[0].label == "Amazon"
        # items carries the normalized dict form (chunks then entities).
        assert len(out.items) == 3
        assert out.items[0]["type"] == "chunk"
        assert out.items[-1]["type"] == "entity"
        assert "2 chunks, 1 entities" in out.detail

    async def test_invoke_scopes_to_namespace_and_passes_query(self):
        lexical = FakeLexical(_baseline_result(chunks=(_fake_chunk(),)))
        tool = RetrievalStrategyTool(_real_strategy("chunk_based"), lexical)

        await tool.invoke(namespace="tenant-x", query="revenue 2023")

        assert len(lexical.calls) == 1
        call = lexical.calls[0]
        assert call["query"] == "revenue 2023"
        assert call["namespace"] == "tenant-x"
        # The tool resolves and passes the real StrategySpec for chunk_based.
        assert call["strategy"] is not None
        assert getattr(call["strategy"].name, "value", call["strategy"].name) == "chunk_based"

    async def test_invoke_empty_result_is_not_an_error(self):
        """A genuinely empty (non-degraded) result returns empty, no error detail."""
        lexical = FakeLexical(
            _baseline_result(trace_steps=({"step": "lexical_baseline_retrieve", "status": "success", "durationMs": 1},))
        )
        tool = RetrievalStrategyTool(_real_strategy("topic_based"), lexical)

        out = await tool.invoke(namespace="ns", query="q")

        assert out.chunks == () and out.entities == ()
        assert "degraded" not in out.detail
        assert "0 chunks, 0 entities" in out.detail


# ── Degraded / never-raises (Req 9.1-9.2) ──────────────────────────


@pytest.mark.unit
class TestDegradedNeverRaises:
    async def test_adapter_error_trace_step_surfaces_as_degraded(self):
        """The adapter returns an empty result + error trace step on timeout/error;
        the Tool surfaces a degraded ToolResult and does not raise (Req 9.1-9.2)."""
        lexical = FakeLexical(
            _baseline_result(
                trace_steps=(
                    {
                        "step": "lexical_baseline_retrieve",
                        "status": "error",
                        "durationMs": 15000,
                        "detail": "strategy=entity_based; TimeoutError after 15.0s",
                    },
                ),
            )
        )
        tool = RetrievalStrategyTool(_real_strategy("entity_based"), lexical)

        out = await tool.invoke(namespace="ns", query="q")

        assert out.chunks == () and out.entities == ()
        assert "degraded" in out.detail
        assert "TimeoutError" in out.detail
        assert out.degraded_reason == "timeout"

    async def test_adapter_degraded_trace_step_surfaces_provenance_drop(self):
        lexical = FakeLexical(
            _baseline_result(
                chunks=(_fake_chunk(),),
                dropped_anonymous_chunks=3,
                trace_steps=(
                    {
                        "step": "lexical_baseline_retrieve",
                        "status": "degraded",
                        "durationMs": 10,
                        "detail": "1 node retained; dropped 3 anonymous chunks",
                    },
                ),
            )
        )
        tool = RetrievalStrategyTool(_real_strategy("entity_based"), lexical)

        out = await tool.invoke(namespace="ns", query="q")

        assert len(out.chunks) == 1
        assert out.degraded_reason == "missing_document_provenance"
        assert "dropped 3 anonymous chunks" in out.detail

    async def test_adapter_raising_is_caught_and_degraded(self):
        """Even if the adapter unexpectedly raises, the Tool never propagates it."""
        lexical = FakeLexical(raises=RuntimeError("boom"))
        tool = RetrievalStrategyTool(_real_strategy("traversal"), lexical)

        out = await tool.invoke(namespace="ns", query="q")  # must not raise

        assert out.chunks == () and out.entities == ()
        assert "error" in out.detail
        assert "RuntimeError" in out.detail
        assert out.degraded_reason == "error"


# ── Builder: one tool per strategy (Req 2.2) ───────────────────────


@pytest.mark.unit
class TestBuildStrategyTools:
    def test_registers_one_tool_per_strategy_named_strategy_value(self):
        """Exactly one Tool per RetrieverStrategy member, named ``strategy:<value>``.

        Enumerates the real enum (transitively imports the toolkit at run time —
        never at module load, per the dedicated subprocess test below)."""
        registry = ToolRegistry()
        lexical = FakeLexical()

        names = build_strategy_tools(registry, lexical)

        expected = [f"strategy:{v}" for v in _STRATEGY_VALUES]
        assert sorted(names) == sorted(expected)
        assert sorted(registry.names()) == sorted(expected)
        assert len(registry.names()) == 8

    def test_each_registered_tool_has_required_interface_fields(self):
        registry = ToolRegistry()
        build_strategy_tools(registry, FakeLexical())
        for spec in registry.specs():
            assert spec["name"].startswith("strategy:")
            assert spec["description"].strip()
            assert isinstance(spec["input_schema"], dict)
            assert isinstance(spec["output_schema"], dict)

    async def test_built_tool_invoke_uses_real_registry_spec(self):
        """A built tool resolves its StrategySpec from STRATEGY_REGISTRY and passes
        it to the adapter (the lazy-import path inside invoke)."""
        registry = ToolRegistry()
        lexical = FakeLexical(_baseline_result(chunks=(_fake_chunk(),)))
        build_strategy_tools(registry, lexical)

        tool = registry.get("strategy:chunk_based")
        out = await tool.invoke(namespace="ns", query="q")

        assert len(out.chunks) == 1
        # The adapter received a real StrategySpec for chunk_based (not None).
        passed = lexical.calls[-1]["strategy"]
        assert passed is not None
        assert getattr(passed.name, "value", passed.name) == "chunk_based"


# ── Lazy-import invariant (Req 12.3) ───────────────────────────────


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_strategy_tool_does_not_import_graphrag(self):
        """``import ...tools.strategy_tool`` must NOT import ``graphrag_toolkit``.

        Runs in a fresh subprocess (current ``sys.path`` propagated) so a
        ``graphrag_toolkit`` already imported by a sibling test cannot mask a
        regression (mirrors the task-1 invariant test)."""
        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.tools.strategy_tool; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        import os

        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            "importing strategy_tool leaked graphrag_toolkit or failed.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
