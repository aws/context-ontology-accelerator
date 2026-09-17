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
    ) -> list[ChunkResult]:
        """Search for document chunks matching the query embedding.

        Args:
            embedding: Pre-computed query embedding vector.
            namespace: Namespace to scope the search.
            top_k: Maximum number of chunks to return.

        Returns:
            List of ChunkResult ordered by relevance score (descending).
        """
        top_k = min(top_k, _MAX_TOP_K)
        index_name = graphrag_chunk_index_name(namespace)

        hits = await self._vector.search(
            embedding,
            namespace=namespace,
            top_k=top_k,
            index=index_name,
        )

        return [
            ChunkResult(
                chunk_id=hit.id,
                text=hit.text,
                source_doc=hit.metadata.get("source_doc", ""),
                source_doc_name=hit.metadata.get("source_doc_name", ""),
                relevance_score=hit.score,
                label=hit.metadata.get("label", ""),
                uri=hit.metadata.get("uri", ""),
            )
            for hit in hits
        ]
