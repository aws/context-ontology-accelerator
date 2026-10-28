# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Vector_Search_Tool: k-NN chunk retrieval over the namespace vector index (Req 2.3).

:class:`VectorSearchTool` wraps the existing
:class:`~coa_serve.tier3.vector_retriever.VectorRetriever`, which runs
a k-NN search against the ``chunk_{namespace}`` OpenSearch index and returns
ranked :class:`ChunkResult` objects. It exposes that capability through the
uniform :class:`Tool` contract so the reasoning controller can invoke it the same
way as every other Tool.

A vector search needs a query embedding. The Tool accepts EITHER:

- a **precomputed embedding** (e.g. the routing embedding already computed for the
  request, reused to avoid a redundant Bedrock call), in which case it is used
  directly; or
- **sub-question text**, in which case the Tool embeds that text via the injected
  embed client (``async embed(text) -> list[float]``) before searching.

When both are supplied the precomputed embedding wins (no re-embedding).

Import discipline (Requirement 12.3): module load imports only the dependency-free
``.base`` contract and the sibling ``tier3.vector_retriever`` dataclasses
(``ChunkResult`` / ``VectorRetriever`` / ``DEFAULT_TOP_K``), none of which import
``graphrag_toolkit``. The embed client is duck-typed (referenced only under
``TYPE_CHECKING`` for the Protocol), so importing this module never pulls the
toolkit into ``sys.modules``.

Per-Tool failure tolerance (Req 9.1-9.2): :meth:`VectorSearchTool.invoke` never
raises for an expected failure. A missing embedding-and-query, an embed-client
error, or a backend search error all yield a (possibly empty) :class:`ToolResult`
whose ``detail`` records the degradation so the controller can record a degraded
source and continue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from ...vector_retriever import DEFAULT_TOP_K, ChunkResult, VectorRetriever
from .base import ToolResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import ToolRegistry

logger = structlog.get_logger(__name__)

TOOL_NAME = "vector_search"

_DESCRIPTION = (
    "Vector (k-NN) search over the namespace's document-chunk index. Embeds the "
    "sub-question and returns the most semantically similar chunk text, ranked by "
    "relevance. Best for open-ended natural-language questions where the answer is "
    "stated somewhere in the document text but you do not have a specific entity to "
    "anchor on. Supply 'query' (the sub-question text) to embed and search, or "
    "'embedding' (a precomputed query vector) to search directly."
)

_INPUT_SCHEMA: dict = {
    "query": "str (sub-question text to embed and search; used when no embedding is supplied)",
    "embedding": "list[float] (precomputed query embedding; used directly when supplied)",
    "top_k": "int (max chunks to return, default 20)",
}
_OUTPUT_SCHEMA: dict = {"chunks": "list", "items": "list"}


@runtime_checkable
class EmbedClient(Protocol):
    """Minimal embed-client contract: an async ``embed(text) -> list[float]``.

    Matched structurally by the serve layer's ``BedrockLLMClient``; duck-typed at
    runtime so this module needs no concrete client import.
    """

    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``."""
        ...


def _chunk_item(chunk: ChunkResult) -> dict:
    """Normalize a retrieved ``ChunkResult`` to a plain dict for ``ToolResult.items``."""
    return {
        "type": "chunk",
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "source_doc": chunk.source_doc,
        "relevance_score": chunk.relevance_score,
        "label": chunk.label,
        "uri": chunk.uri,
    }


class VectorSearchTool:
    """k-NN chunk search via the existing ``VectorRetriever`` (Req 2.3, 2.4).

    The four uniform interface fields are static, graphrag-free data set at
    construction, so the Tool is registrable and describable to the planner
    without any toolkit access. ``invoke`` reuses a precomputed embedding when
    supplied, otherwise embeds the sub-question text via the injected embed
    client, then delegates to ``VectorRetriever.search``.
    """

    def __init__(self, vector_retriever: VectorRetriever, embed_client: EmbedClient) -> None:
        """Build the Tool over a ``VectorRetriever`` and an embed client.

        Args:
            vector_retriever: The shared :class:`VectorRetriever` that performs the
                k-NN search against the namespace chunk index.
            embed_client: A client exposing ``async embed(text) -> list[float]``
                (e.g. the serve layer's ``BedrockLLMClient``), used to embed
                sub-question text when no precomputed embedding is supplied.
        """
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._vector = vector_retriever
        self._embed = embed_client

    async def invoke(
        self,
        *,
        namespace: str,
        query: str = "",
        embedding: list[float] | None = None,
        top_k: int = DEFAULT_TOP_K,
        **_: Any,
    ) -> ToolResult:
        """Search the namespace chunk index and return a :class:`ToolResult`.

        Uses ``embedding`` directly when supplied; otherwise embeds ``query`` via
        the injected embed client. Never raises for an expected failure: a missing
        embedding-and-query, an embed-client error, or a backend search error all
        return a degraded :class:`ToolResult` whose ``detail`` records the reason
        (Req 9.1-9.2).

        Args:
            namespace: The originating request namespace; the search is scoped to
                it (Req 1.5).
            query: Sub-question text to embed when no ``embedding`` is supplied.
            embedding: A precomputed query embedding; used directly when supplied.
            top_k: Maximum number of chunks to return.

        Returns:
            A :class:`ToolResult` carrying the native ``chunks`` (passed through to
            synthesis), their normalized dict ``items``, and a ``detail`` line.
        """
        try:
            search_embedding = await self._resolve_embedding(embedding, query)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "vector_tool_embed_error",
                tool=self.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ToolResult(
                detail=f"{self.name} embed error: {type(exc).__name__}",
                degraded_reason="error",
            )

        if not search_embedding:
            return ToolResult(detail=f"{self.name}: no embedding or query text supplied")

        try:
            search_results = await self._vector.search(search_embedding, namespace, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "vector_tool_search_error",
                tool=self.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ToolResult(
                detail=f"{self.name} error: {type(exc).__name__}",
                degraded_reason="error",
            )

        dropped_anonymous_chunks = int(getattr(search_results, "dropped_anonymous_chunks", 0) or 0)
        chunks = tuple(search_results)
        items = tuple(_chunk_item(c) for c in chunks)
        if dropped_anonymous_chunks:
            detail = (
                f"{self.name}: {len(chunks)} chunks; "
                f"dropped {dropped_anonymous_chunks} anonymous or malformed document chunks"
            )
            degraded_reason = "missing_document_provenance"
        else:
            detail = f"{self.name}: {len(chunks)} chunks"
            degraded_reason = ""
        return ToolResult(
            chunks=chunks,
            items=items,
            detail=detail,
            degraded_reason=degraded_reason,
        )

    async def _resolve_embedding(self, embedding: list[float] | None, query: str) -> list[float]:
        """Return the embedding to search with: precomputed if supplied, else embed ``query``.

        A non-empty ``embedding`` is used directly (no re-embedding). Otherwise, if
        ``query`` text is present it is embedded via the injected embed client. When
        neither is available an empty list is returned so the caller can surface a
        non-degraded "nothing to search" result.
        """
        if embedding:
            return list(embedding)
        if query and query.strip():
            return await self._embed.embed(query)
        return []


def build_vector_tool(registry: ToolRegistry, vector_retriever: VectorRetriever, embed_client: EmbedClient) -> str:
    """Register the single :class:`VectorSearchTool` into ``registry``.

    Mirrors :func:`build_graph_tool`: a thin builder the factory (task 17) calls
    when the vector index endpoint and embed client are configured. Returns the
    registered Tool name.

    Args:
        registry: The :class:`ToolRegistry` to register the tool into.
        vector_retriever: The shared :class:`VectorRetriever` the tool wraps.
        embed_client: The embed client used to embed sub-question text.

    Returns:
        The registered Tool name (``"vector_search"``).
    """
    tool = VectorSearchTool(vector_retriever, embed_client)
    registry.register(tool)
    logger.info("vector_tool_registered", tool_name=tool.name)
    return tool.name
