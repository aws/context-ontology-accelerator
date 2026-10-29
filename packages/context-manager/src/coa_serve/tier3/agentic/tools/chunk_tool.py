# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Chunk_Expansion_Tool: physical chunk-window expansion (improvement idea 6).

When the reasoning agent decides one or more retrieved chunks are particularly
relevant, this tool returns the chunks **physically adjacent to them in the source
document** — the chunks immediately before/after, up to a small number of hops.
The rationale (verified on SEC10Q): a figure and its period/label, or a claim and
its supporting number, frequently straddle a chunk boundary, so the chunk that
semantically matched the question is often one hop away from the chunk that holds
the specific value the answer needs.

It traverses the graphrag-toolkit lexical graph's chunk-sequence edges
(`__NEXT__` / `__PREVIOUS__`) over the toolkit **property graph** via openCypher,
through the toolkit ``GraphStore`` — the same machinery as the
``Graph_Traversal_Tool``, NOT SPARQL over the ontology graph.

Schema notes (verified 2026-06-29 on the dev graph): chunks carry the
tenant-scoped label ``__Chunk__``, are keyed by the Neptune node id (the
``chunkId`` property is null), the chunk text is the ``value`` property, and the
graph has dense ``__NEXT__``/``__PREVIOUS__`` coverage (3,115 of 3,125 chunks).
So seeds are matched and neighbors returned via ``id(...)``. Seed chunk ids come
from prior chunk/semantic retrieval results (``ChunkResult.chunk_id``), which the
planner sees listed in its context summary.

Import discipline (Requirement 12.3): module load imports only the dependency-free
``.base`` contract, the template strings, and the sibling ``ChunkResult``
dataclass — none of which import ``graphrag_toolkit``. The toolkit
``GraphStoreFactory`` and ``TenantId`` are imported LAZILY inside the first
invocation.

Per-Tool failure tolerance (Req 9.1-9.2): :meth:`ChunkExpansionTool.invoke` never
raises for an expected failure. A bad/empty parameter set, an out-of-bounds id
count, an unconfigured store, a backend error, or a timeout all yield a (possibly
empty) :class:`ToolResult` whose ``detail`` records the degradation.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog

from ...vector_retriever import ChunkResult
from ..templates import (
    CHUNK_WINDOW_EXPANSION,
    DEFAULT_CHUNK_WINDOW_HOPS,
    DEFAULT_CHUNK_WINDOW_LIMIT,
    MAX_CHUNK_IDS,
    MAX_CHUNK_WINDOW_HOPS,
    MIN_CHUNK_IDS,
)
from .base import ToolResult

logger = structlog.get_logger(__name__)

TOOL_NAME = "chunk_window"

_GRAPH_QUERY_TIMEOUT_S: float = float(os.environ.get("AGENTIC_GRAPH_QUERY_TIMEOUT_S", "30"))
_GRAPH_MAX_WORKERS: int = int(os.environ.get("AGENTIC_GRAPH_MAX_WORKERS", "4"))
_EXECUTOR = ThreadPoolExecutor(max_workers=_GRAPH_MAX_WORKERS, thread_name_prefix="agentic-chunkwin")

_DESCRIPTION = (
    "Chunk-window expansion over the source documents. Given one or more chunk ids "
    "you have already retrieved and judge particularly relevant, this returns the "
    "chunks immediately BEFORE/AFTER them in the original document (their physical "
    "neighbors), up to 'hops' hops. Use it when a relevant chunk seems to be missing "
    "an adjacent detail — e.g. a specific figure, period, or label that likely sits "
    "in the neighboring chunk. Pass 'chunk_ids' taken from prior chunk/semantic "
    "search results (the chunk ids listed in the gathered-context summary); ids that "
    "are not document chunks simply return nothing."
)

_INPUT_SCHEMA: dict = {
    "chunk_ids": "list[str] (1-50 chunk ids from prior chunk/semantic retrieval to expand around)",
    "hops": "int (how many chunks before/after to include, default 1, max 5)",
    "limit": "int (max neighbor chunks to return, default 50)",
}
_OUTPUT_SCHEMA: dict = {"chunks": "list", "items": "list"}


def _chunk_item(chunk: ChunkResult) -> dict:
    """Normalize an adjacent ``ChunkResult`` to a dict for ``ToolResult.items``."""
    return {
        "type": "chunk",
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "source_doc": chunk.source_doc,
        "uri": chunk.uri,
        "label": chunk.label,
    }


class ChunkExpansionTool:
    """openCypher chunk-sequence (`__NEXT__`/`__PREVIOUS__`) expansion via the toolkit ``GraphStore``.

    The four uniform interface fields are static, graphrag-free data set at
    construction, so the tool is registrable/describable without toolkit access.
    The toolkit ``GraphStore`` is built lazily on first use and cached.
    """

    def __init__(self, graph_store_uri: str, *, query_timeout_s: float = _GRAPH_QUERY_TIMEOUT_S) -> None:
        """Build the tool over a graphrag-toolkit graph-store URI.

        Args:
            graph_store_uri: The same ``neptune-db://<host>:8182`` (or
                ``neptune-graph://g-…``) URI the strategy/graph tools use. The
                toolkit ``GraphStore`` is constructed from it lazily inside the
                first invocation (Req 12.3).
            query_timeout_s: Wall-clock cap applied around each graph query.
        """
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._graph_store_uri = graph_store_uri
        self._query_timeout_s = query_timeout_s
        self._store: Any = None

    def _get_store(self) -> Any:
        """Build (once) and return the toolkit ``GraphStore``; lazy toolkit import."""
        if self._store is None:
            from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory  # lazy

            self._store = GraphStoreFactory.for_graph_store(self._graph_store_uri)
            logger.info("agentic_chunk_store_built", uri=self._graph_store_uri)
        return self._store

    @staticmethod
    def _chunk_label(namespace: str) -> str:
        """Tenant-scoped, backtick-wrapped ``__Chunk__`` label for ``namespace``."""
        return ChunkExpansionTool._format_label(namespace, "__Chunk__")

    @staticmethod
    def _source_label(namespace: str) -> str:
        """Tenant-scoped, backtick-wrapped ``__Source__`` label for ``namespace``."""
        return ChunkExpansionTool._format_label(namespace, "__Source__")

    @staticmethod
    def _format_label(namespace: str, label: str) -> str:
        """Format a toolkit graph label for the namespace's tenant."""
        from coa_common.constants import to_graphrag_tenant_id
        from graphrag_toolkit.lexical_graph import TenantId  # lazy

        return TenantId(to_graphrag_tenant_id(namespace)).format_label(label)

    async def _run_cypher(self, cypher: str, params: dict) -> list[dict]:
        """Execute openCypher on the toolkit store under the query timeout."""
        store = self._get_store()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_EXECUTOR, lambda: store.execute_query_with_retry(cypher, params))
        rows = await asyncio.wait_for(asyncio.shield(fut), timeout=self._query_timeout_s)
        return list(rows or [])

    async def invoke(
        self,
        *,
        namespace: str,
        chunk_ids: list[str] | None = None,
        hops: int = DEFAULT_CHUNK_WINDOW_HOPS,
        limit: int = DEFAULT_CHUNK_WINDOW_LIMIT,
        **_: Any,
    ) -> ToolResult:
        """Return the chunks adjacent to ``chunk_ids`` in the source document.

        Validates the id count (1-50) and clamps ``hops`` to ``[1, 5]`` before
        building the query. Never raises for an expected failure: invalid params,
        an unconfigured store, a backend error, or a timeout all return a degraded
        :class:`ToolResult` whose ``detail`` records the reason (Req 9.1-9.2).
        """
        ids = [c for c in (chunk_ids or []) if isinstance(c, str) and c]
        if not (MIN_CHUNK_IDS <= len(ids) <= MAX_CHUNK_IDS):
            return ToolResult(
                detail=f"{self.name}: chunk_ids count {len(ids)} out of bounds [{MIN_CHUNK_IDS}, {MAX_CHUNK_IDS}]"
            )
        hops = max(1, min(int(hops), MAX_CHUNK_WINDOW_HOPS))

        cypher = CHUNK_WINDOW_EXPANSION.format(
            chunk_label=self._chunk_label(namespace),
            source_label=self._source_label(namespace),
            hops=hops,
            limit=int(limit),
        )
        try:
            rows = await self._run_cypher(cypher, {"chunk_ids": ids})
        except TimeoutError:
            logger.warning("chunk_tool_timeout", tool=self.name)
            return ToolResult(detail=f"{self.name} timeout")
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning("chunk_tool_invoke_error", tool=self.name, error=str(exc), error_type=type(exc).__name__)
            return ToolResult(detail=f"{self.name} error: {type(exc).__name__}")

        chunks: list[ChunkResult] = []
        dropped_chunks = 0
        dropped_without_source = 0
        for row in rows:
            if not isinstance(row, dict):
                dropped_chunks += 1
                continue
            cid = row.get("chunkId", "")
            text = row.get("text", "") or ""
            source_document_id = row.get("sourceDocumentId", "")
            has_source = isinstance(source_document_id, str) and bool(source_document_id.strip())
            if cid is None or not str(cid).strip() or not isinstance(text, str) or not text.strip() or not has_source:
                dropped_chunks += 1
                if not has_source:
                    dropped_without_source += 1
                continue
            source_name = row.get("sourceS3Key") or row.get("sourceFilename") or ""
            source_name = source_name.rsplit("/", 1)[-1] if isinstance(source_name, str) else ""
            chunks.append(
                ChunkResult(
                    chunk_id=str(cid),
                    text=text,
                    source_doc=source_document_id.strip(),
                    source_doc_name=source_name,
                    relevance_score=1.0,
                )
            )

        items = tuple(_chunk_item(c) for c in chunks)
        detail = f"{self.name}: expanded {len(ids)} chunk(s) -> {len(chunks)} adjacent chunk(s) (hops={hops})"
        degraded_reason = ""
        if dropped_chunks:
            detail = f"{detail}; dropped {dropped_chunks} anonymous or malformed document chunk(s)"
            degraded_reason = "missing_document_provenance"
            # Every adjacency row lacking a `__Source__` join is the schema-mismatch
            # case: the namespace's graph does not carry `__EXTRACTED_FROM__` (or
            # spells the source label differently), so chunk-window expansion can
            # never contribute document evidence for that namespace. Name it in the
            # detail, because otherwise it reads as an ordinary per-row data problem.
            # The ``degraded_reason`` vocabulary stays stable for downstream consumers.
            source_join_missing = dropped_without_source == len(rows)
            if source_join_missing:
                detail = (
                    f"{detail}; no adjacent chunk resolved a `__Source__` via "
                    "`__EXTRACTED_FROM__` — the namespace graph may be missing that shape"
                )
            logger.warning(
                "chunk_window_chunks_dropped",
                tool=self.name,
                seeds=len(ids),
                rows=len(rows),
                dropped=dropped_chunks,
                dropped_without_source=dropped_without_source,
                source_join_missing=source_join_missing,
                reason=degraded_reason,
            )
        return ToolResult(
            chunks=tuple(chunks),
            items=items,
            detail=detail,
            degraded_reason=degraded_reason,
        )


def build_chunk_expansion_tool(registry: Any, graph_store_uri: str) -> str:
    """Register the single :class:`ChunkExpansionTool` into ``registry``.

    Mirrors :func:`build_graph_tool`: a thin builder the factory calls when the
    Neptune graph endpoint is configured. Returns the registered Tool name.

    Args:
        registry: The :class:`ToolRegistry` to register the tool into.
        graph_store_uri: The graphrag-toolkit graph-store URI the tool queries.

    Returns:
        The registered Tool name (``"chunk_window"``).
    """
    tool = ChunkExpansionTool(graph_store_uri)
    registry.register(tool)
    logger.info("chunk_tool_registered", tool_name=tool.name)
    return tool.name
