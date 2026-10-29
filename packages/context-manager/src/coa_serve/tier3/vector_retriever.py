# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Vector similarity search for document chunk retrieval.

Searches the ``chunk_{namespace_short_id}`` OpenSearch index using the
query embedding (already computed in routing).  Returns the top-k chunks
with text, source document attribution, and relevance scores.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from coa_common import graphrag_chunk_index_name

from ..clients.base import VectorClient

logger = structlog.get_logger(__name__)

DEFAULT_TOP_K = 20
# Default cap for the Tier-3 retrieval path. The isolated KBSearch action raises it
# to its contract bound (100) via ``max_top_k``: 100 chunks of indexed text are a
# few hundred KB, well inside the 6 MB synchronous Lambda response limit.
_MAX_TOP_K = 50


@dataclass(frozen=True)
class ChunkResult:
    """A single document chunk returned from vector retrieval."""

    chunk_id: str
    text: str
    source_doc: str
    relevance_score: float
    label: str = ""
    uri: str = ""
    # Human-readable document name (ingest's ``filename``). Display metadata,
    # not identity — distinct documents may share a name (#985).
    source_doc_name: str = ""


class ChunkSearchResults(list[ChunkResult]):
    """List-compatible vector result carrying mapping diagnostics."""

    def __init__(self, chunks: list[ChunkResult], *, dropped_anonymous_chunks: int = 0):
        """Store valid chunks plus the count rejected during hit mapping."""
        super().__init__(chunks)
        self.dropped_anonymous_chunks = dropped_anonymous_chunks


class VectorRetriever:
    """Retrieve document chunks via k-NN search against OpenSearch."""

    def __init__(self, vector_client: VectorClient):
        """Store the vector client used for k-NN chunk retrieval.

        Args:
            vector_client: Client issuing k-NN searches against OpenSearch.
        """
        self._vector = vector_client

    @property
    def vector_client(self) -> VectorClient:
        """Public access to underlying vector client for direct search (T-Box assembly)."""
        return self._vector

    async def search(
        self,
        embedding: list[float],
        namespace: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        max_top_k: int = _MAX_TOP_K,
    ) -> list[ChunkResult]:
        """Search for document chunks matching the query embedding.

        Args:
            embedding: Pre-computed query embedding vector.
            namespace: Namespace to scope the search.
            top_k: Maximum number of chunks to return.
            max_top_k: Ceiling applied to ``top_k``. Tier-3 retrieval keeps the
                default; the KBSearch API passes its own contract bound (100) so a
                caller's validated ``topK`` is not silently cut to the default.

        Returns:
            List of ChunkResult ordered by relevance score (descending).
        """
        top_k = min(top_k, max_top_k)
        index_name = graphrag_chunk_index_name(namespace)

        hits = await self._vector.search(
            embedding,
            namespace=namespace,
            top_k=top_k,
            index=index_name,
        )

        chunks: list[ChunkResult] = []
        dropped_anonymous_chunks = 0
        for hit in hits:
            metadata = hit.metadata if isinstance(hit.metadata, dict) else {}
            source_doc = metadata.get("source_doc", "")
            chunk_id = hit.id
            text = hit.text
            if (
                not isinstance(source_doc, str)
                or not source_doc.strip()
                or not isinstance(chunk_id, str)
                or not chunk_id.strip()
                or not isinstance(text, str)
                or not text.strip()
            ):
                dropped_anonymous_chunks += 1
                continue

            source_doc_name = metadata.get("source_doc_name", "")
            label = metadata.get("label", "")
            uri = metadata.get("uri", "")
            chunks.append(
                ChunkResult(
                    chunk_id=chunk_id,
                    text=text,
                    source_doc=source_doc.strip(),
                    source_doc_name=source_doc_name if isinstance(source_doc_name, str) else "",
                    relevance_score=hit.score,
                    label=label if isinstance(label, str) else "",
                    uri=uri if isinstance(uri, str) else "",
                )
            )

        if dropped_anonymous_chunks:
            logger.warning(
                "vector_chunks_dropped_missing_provenance",
                dropped_count=dropped_anonymous_chunks,
                namespace=namespace,
            )
        return ChunkSearchResults(chunks, dropped_anonymous_chunks=dropped_anonymous_chunks)
