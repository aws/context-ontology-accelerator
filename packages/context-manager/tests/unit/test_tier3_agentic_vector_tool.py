# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Vector_Search_Tool layer (task 8).

Covers ``coa_serve.tier3.agentic.tools.vector_tool``:

- The four required interface fields are present and well-formed, and the Tool is
  registrable (Req 11.1).
- :meth:`VectorSearchTool.invoke` returns the native ``chunks`` from
  ``VectorRetriever.search`` plus their normalized ``items`` (Req 2.3).
- The Tool accepts EITHER a precomputed embedding OR sub-question text (Req 2.4):
  it embeds the sub-question text via the injected embed client when no embedding
  is supplied, and uses the precomputed embedding directly (no re-embedding) when
  one is supplied.
- An embed-client error or a backend search error surfaces as a degraded
  :class:`ToolResult`; the Tool NEVER raises (Req 9.1-9.2).
- :func:`build_vector_tool` registers exactly one ``vector_search`` Tool.
- Module load of ``vector_tool`` does NOT import ``graphrag_toolkit`` (Req 12.3).

Both the ``VectorRetriever`` and the embed client are mocked with lightweight
fakes exposing the same coroutines, so the tests need no ``graphrag_toolkit`` /
OpenSearch / Bedrock access.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from coa_serve.tier3.agentic.tools import ToolRegistry
from coa_serve.tier3.agentic.tools.vector_tool import (
    VectorSearchTool,
    build_vector_tool,
)
from coa_serve.tier3.vector_retriever import ChunkResult, ChunkSearchResults


def _chunk(chunk_id: str = "c1", text: str = "hello", source_doc: str = "doc-1", score: float = 0.9) -> ChunkResult:
    """A real ``ChunkResult`` for fidelity with the production passthrough."""
    return ChunkResult(
        chunk_id=chunk_id,
        text=text,
        source_doc=source_doc,
        relevance_score=score,
        label="Amazon",
        uri="urn:e:amazon",
    )


class FakeVectorRetriever:
    """Mock of ``VectorRetriever`` capturing the last ``search`` call.

    ``search`` either returns the configured chunk list or, when ``raises`` is
    set, raises it — letting a single fake exercise both the normal path and the
    defensive never-raise guard.
    """

    def __init__(
        self,
        chunks=None,
        *,
        raises: Exception | None = None,
        dropped_anonymous_chunks: int = 0,
    ) -> None:
        self._chunks = list(chunks) if chunks is not None else []
        self._raises = raises
        self._dropped_anonymous_chunks = dropped_anonymous_chunks
        self.calls: list[dict] = []

    async def search(self, embedding, namespace, *, top_k=10):
        self.calls.append({"embedding": embedding, "namespace": namespace, "top_k": top_k})
        if self._raises is not None:
            raise self._raises
        return ChunkSearchResults(
            list(self._chunks),
            dropped_anonymous_chunks=self._dropped_anonymous_chunks,
        )


class FakeEmbedClient:
    """Mock embed client capturing each ``embed`` call.

    Returns a fixed vector, or raises when ``raises`` is set.
    """

    def __init__(self, vector=None, *, raises: Exception | None = None) -> None:
        self._vector = list(vector) if vector is not None else [0.1, 0.2, 0.3]
        self._raises = raises
        self.calls: list[str] = []

    async def embed(self, text: str):
        self.calls.append(text)
        if self._raises is not None:
            raise self._raises
        return list(self._vector)


# ── Interface fields & naming ──────────────────────────────────────


@pytest.mark.unit
class TestToolInterface:
    def test_name_is_vector_search(self):
        tool = VectorSearchTool(FakeVectorRetriever(), FakeEmbedClient())
        assert tool.name == "vector_search"

    def test_description_is_non_empty(self):
        tool = VectorSearchTool(FakeVectorRetriever(), FakeEmbedClient())
        assert tool.description.strip()

    def test_declares_dict_input_and_output_schemas(self):
        tool = VectorSearchTool(FakeVectorRetriever(), FakeEmbedClient())
        assert isinstance(tool.input_schema, dict)
        assert "query" in tool.input_schema and "embedding" in tool.input_schema
        assert isinstance(tool.output_schema, dict)

    def test_is_registrable_as_a_tool(self):
        """A vector tool satisfies the registry's four-field contract (Req 11.1)."""
        registry = ToolRegistry()
        registry.register(VectorSearchTool(FakeVectorRetriever(), FakeEmbedClient()))
        assert registry.names() == ["vector_search"]


# ── invoke() returns chunks ────────────────────────────────────────


@pytest.mark.unit
class TestInvokeReturnsChunks:
    async def test_invoke_returns_chunks_and_normalized_items(self):
        vector = FakeVectorRetriever(chunks=[_chunk("c1"), _chunk("c2", text="world")])
        tool = VectorSearchTool(vector, FakeEmbedClient())

        out = await tool.invoke(namespace="ns-1", embedding=[0.5, 0.6])

        assert len(out.chunks) == 2
        # Native ChunkResult objects are passed through untouched for synthesis.
        assert out.chunks[0].chunk_id == "c1"
        assert out.chunks[1].text == "world"
        # items carries the normalized dict form.
        assert len(out.items) == 2
        assert out.items[0]["type"] == "chunk"
        assert out.items[0]["chunk_id"] == "c1"
        assert "2 chunks" in out.detail

    async def test_invoke_scopes_search_to_namespace_and_top_k(self):
        vector = FakeVectorRetriever(chunks=[_chunk()])
        tool = VectorSearchTool(vector, FakeEmbedClient())

        await tool.invoke(namespace="tenant-x", embedding=[0.1], top_k=3)

        assert len(vector.calls) == 1
        assert vector.calls[0]["namespace"] == "tenant-x"
        assert vector.calls[0]["top_k"] == 3

    async def test_invoke_empty_result_is_not_an_error(self):
        tool = VectorSearchTool(FakeVectorRetriever(chunks=[]), FakeEmbedClient())

        out = await tool.invoke(namespace="ns", embedding=[0.1])

        assert out.chunks == ()
        assert "error" not in out.detail
        assert "0 chunks" in out.detail

    async def test_invoke_preserves_dropped_anonymous_chunk_degradation(self):
        vector = FakeVectorRetriever(
            chunks=[_chunk()],
            dropped_anonymous_chunks=4,
        )
        tool = VectorSearchTool(vector, FakeEmbedClient())

        out = await tool.invoke(namespace="ns", embedding=[0.1])

        assert len(out.chunks) == 1
        assert out.degraded_reason == "missing_document_provenance"
        assert "dropped 4 anonymous or malformed document chunks" in out.detail

    async def test_index_where_no_hit_has_provenance_degrades_instead_of_succeeding_empty(self):
        # An index whose hits ALL lack document provenance must not look like an
        # ordinary "0 chunks" search: the evidence existed and was discarded.
        vector = FakeVectorRetriever(chunks=[], dropped_anonymous_chunks=3)
        tool = VectorSearchTool(vector, FakeEmbedClient())

        out = await tool.invoke(namespace="ns", embedding=[0.1])

        assert out.chunks == ()
        assert out.degraded_reason == "missing_document_provenance"
        assert "dropped 3 anonymous or malformed document chunks" in out.detail


# ── Embedding source selection (Req 2.4) ───────────────────────────


@pytest.mark.unit
class TestEmbeddingSourceSelection:
    async def test_embeds_subquestion_text_when_no_embedding_supplied(self):
        """No embedding → the Tool embeds the sub-question text, then searches with it."""
        vector = FakeVectorRetriever(chunks=[_chunk()])
        embed = FakeEmbedClient(vector=[0.7, 0.8, 0.9])
        tool = VectorSearchTool(vector, embed)

        out = await tool.invoke(namespace="ns", query="Amazon competitors")

        # The sub-question text was embedded exactly once...
        assert embed.calls == ["Amazon competitors"]
        # ...and the resulting vector was passed to the search.
        assert vector.calls[0]["embedding"] == [0.7, 0.8, 0.9]
        assert len(out.chunks) == 1

    async def test_uses_precomputed_embedding_directly_without_embedding(self):
        """A precomputed embedding is used directly; the embed client is NOT called."""
        vector = FakeVectorRetriever(chunks=[_chunk()])
        embed = FakeEmbedClient()
        tool = VectorSearchTool(vector, embed)

        await tool.invoke(namespace="ns", embedding=[0.11, 0.22])

        assert embed.calls == []  # no re-embedding
        assert vector.calls[0]["embedding"] == [0.11, 0.22]

    async def test_precomputed_embedding_wins_over_query(self):
        """When both are supplied the precomputed embedding wins (no re-embedding)."""
        vector = FakeVectorRetriever(chunks=[_chunk()])
        embed = FakeEmbedClient()
        tool = VectorSearchTool(vector, embed)

        await tool.invoke(namespace="ns", query="ignored", embedding=[1.0, 2.0])

        assert embed.calls == []
        assert vector.calls[0]["embedding"] == [1.0, 2.0]

    async def test_no_embedding_and_no_query_returns_empty_without_search(self):
        """Neither embedding nor query → nothing to search; no embed/search call, not an error."""
        vector = FakeVectorRetriever(chunks=[_chunk()])
        embed = FakeEmbedClient()
        tool = VectorSearchTool(vector, embed)

        out = await tool.invoke(namespace="ns")

        assert embed.calls == []
        assert vector.calls == []
        assert out.chunks == ()
        assert "no embedding or query" in out.detail


# ── Degraded / never-raises (Req 9.1-9.2) ──────────────────────────


@pytest.mark.unit
class TestDegradedNeverRaises:
    async def test_search_error_surfaces_as_degraded(self):
        vector = FakeVectorRetriever(raises=RuntimeError("boom"))
        tool = VectorSearchTool(vector, FakeEmbedClient())

        out = await tool.invoke(namespace="ns", embedding=[0.1])  # must not raise

        assert out.chunks == ()
        assert "error" in out.detail
        assert "RuntimeError" in out.detail
        assert out.degraded_reason == "error"

    async def test_embed_error_surfaces_as_degraded(self):
        embed = FakeEmbedClient(raises=ValueError("no embedding in response"))
        vector = FakeVectorRetriever(chunks=[_chunk()])
        tool = VectorSearchTool(vector, embed)

        out = await tool.invoke(namespace="ns", query="q")  # must not raise

        assert out.chunks == ()
        assert "embed error" in out.detail
        assert "ValueError" in out.detail
        assert out.degraded_reason == "error"
        # search must not have been attempted with a failed embedding.
        assert vector.calls == []


# ── Builder (Req 2.3) ──────────────────────────────────────────────


@pytest.mark.unit
class TestBuildVectorTool:
    def test_registers_single_vector_search_tool(self):
        registry = ToolRegistry()

        name = build_vector_tool(registry, FakeVectorRetriever(), FakeEmbedClient())

        assert name == "vector_search"
        assert registry.names() == ["vector_search"]

    async def test_built_tool_invokes_search(self):
        registry = ToolRegistry()
        vector = FakeVectorRetriever(chunks=[_chunk()])
        build_vector_tool(registry, vector, FakeEmbedClient())

        tool = registry.get("vector_search")
        out = await tool.invoke(namespace="ns", embedding=[0.1])

        assert len(out.chunks) == 1
        assert len(vector.calls) == 1


# ── Lazy-import invariant (Req 12.3) ───────────────────────────────


@pytest.mark.unit
class TestLazyImportInvariant:
    def test_importing_vector_tool_does_not_import_graphrag(self):
        """``import ...tools.vector_tool`` must NOT import ``graphrag_toolkit``.

        Runs in a fresh subprocess (current ``sys.path`` propagated) so a
        ``graphrag_toolkit`` already imported by a sibling test cannot mask a
        regression (mirrors the task-1/6/7 invariant tests)."""
        import os

        code = (
            "import sys; "
            "import coa_serve.tier3.agentic.tools.vector_tool; "
            "leaked = sorted(m for m in sys.modules if m == 'graphrag_toolkit' "
            "or m.startswith('graphrag_toolkit.')); "
            "assert not leaked, leaked; "
            "print('OK')"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
        assert result.returncode == 0, (
            f"importing vector_tool leaked graphrag_toolkit or failed.\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "OK" in result.stdout
