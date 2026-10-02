# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Tier 3 vector retriever."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from coa_serve.clients.base import VectorHit
from coa_serve.tier3.vector_retriever import VectorRetriever


@pytest.fixture
def mock_opensearch():
    client = AsyncMock()
    client.search.return_value = [
        VectorHit(
            id="c1",
            text="Claims processing SLA is 10 business days",
            score=0.92,
            metadata={
                "source_doc": "policy_v2.3.pdf",
                "label": "Policy v2.3",
                "uri": "s3://docs/policy.pdf",
            },
        ),
    ]
    return client


@pytest.fixture
def vector_retriever(mock_opensearch):
    return VectorRetriever(mock_opensearch)


@pytest.mark.unit
class TestVectorRetriever:
    async def test_search_returns_chunks(self, vector_retriever, mock_opensearch):
        embedding = [0.1] * 1024
        results = await vector_retriever.search(embedding, "550e8400-e29b-41d4-a716-446655440000")

        mock_opensearch.search.assert_awaited_once()
        assert len(results) == 1
        assert results[0].chunk_id == "c1"
        assert results[0].relevance_score == 0.92
        assert results[0].source_doc == "policy_v2.3.pdf"
        assert results[0].label == "Policy v2.3"

    async def test_search_uses_chunk_index(self, mock_opensearch):
        retriever = VectorRetriever(mock_opensearch)
        await retriever.search([0.1] * 10, "550e8400-e29b-41d4-a716-446655440000")

        call_kwargs = mock_opensearch.search.call_args
        # The index should be chunk_{sha256_prefix}
        assert call_kwargs.kwargs["index"].startswith("chunk_")

    async def test_search_empty_results(self):
        os_client = AsyncMock()
        os_client.search.return_value = []
        retriever = VectorRetriever(os_client)
        results = await retriever.search([0.1] * 10, "550e8400-e29b-41d4-a716-446655440000")
        assert results == []

    async def test_search_passes_top_k(self, mock_opensearch):
        retriever = VectorRetriever(mock_opensearch)
        await retriever.search([0.1] * 10, "550e8400-e29b-41d4-a716-446655440000", top_k=5)

        call_kwargs = mock_opensearch.search.call_args
        assert call_kwargs.kwargs["top_k"] == 5

    async def test_search_default_top_k(self, mock_opensearch):
        retriever = VectorRetriever(mock_opensearch)
        await retriever.search([0.1] * 10, "550e8400-e29b-41d4-a716-446655440000")

        call_kwargs = mock_opensearch.search.call_args
        assert call_kwargs.kwargs["top_k"] == 20

    async def test_search_handles_missing_metadata(self):
        os_client = AsyncMock()
        os_client.search.return_value = [
            VectorHit(
                id="c2",
                text="Some text",
                score=0.8,
                metadata={},
            ),
        ]
        retriever = VectorRetriever(os_client)
        results = await retriever.search([0.1] * 10, "550e8400-e29b-41d4-a716-446655440000")

        assert len(results) == 1
        assert results[0].source_doc == ""
        assert results[0].label == ""

    async def test_search_preserves_order(self):
        os_client = AsyncMock()
        os_client.search.return_value = [
            VectorHit(id=f"c{i}", text=f"Chunk {i}", score=0.9 - i * 0.01, metadata={}) for i in range(5)
        ]
        retriever = VectorRetriever(os_client)
        results = await retriever.search([0.1] * 10, "550e8400-e29b-41d4-a716-446655440000")

        assert len(results) == 5
        scores = [r.relevance_score for r in results]
        assert scores == sorted(scores, reverse=True)
