# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for client factory and ClientSet."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.clients.base import (
    ClientSet,
    GraphClient,
    LLMClient,
    QueryExecutor,
    VectorClient,
)


@pytest.mark.unit
class TestProtocolConformance:
    """Verify concrete implementations satisfy the protocol contracts."""

    def test_neptune_is_graph_client(self):
        from coa_serve.clients.neptune import NeptuneGraphClient

        client: GraphClient = NeptuneGraphClient(endpoint="test.neptune.amazonaws.com")
        assert isinstance(client, GraphClient)

    def test_opensearch_is_vector_client(self):
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        client: VectorClient = OpenSearchVectorClient(endpoint="test.aoss.amazonaws.com")
        assert isinstance(client, VectorClient)

    def test_bedrock_is_llm_client(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        client: LLMClient = BedrockLLMClient()
        assert isinstance(client, LLMClient)

    @patch("coa_serve.clients.source_db.boto3")
    def test_source_db_is_query_executor(self, mock_boto3):
        from coa_serve.clients.source_db import SourceDBQueryExecutor

        client: QueryExecutor = SourceDBQueryExecutor(data_sources_table="test-table")
        assert isinstance(client, QueryExecutor)


@pytest.mark.unit
class TestFactory:
    @patch("coa_serve.clients.factory._build_default_clients")
    def test_build_clients_calls_default_builder(self, mock_build):
        from coa_serve.clients.factory import build_clients

        mock_build.return_value = MagicMock(spec=ClientSet)
        result = build_clients()
        mock_build.assert_called_once()
        assert result is mock_build.return_value


@pytest.mark.unit
class TestClientSet:
    async def test_close_calls_client_close_methods(self):
        mock_graph = AsyncMock()
        mock_vector = AsyncMock()
        mock_llm = AsyncMock()
        mock_executor = AsyncMock()

        clients = ClientSet(
            graph=mock_graph,
            vector=mock_vector,
            llm=mock_llm,
            query_executor=mock_executor,
        )
        await clients.close()
        mock_graph.close.assert_called_once()
        mock_vector.close.assert_called_once()
        mock_llm.close.assert_called_once()
        mock_executor.close.assert_called_once()

    async def test_close_continues_on_error(self):
        mock_graph = AsyncMock()
        mock_graph.close = AsyncMock(side_effect=RuntimeError("graph close failed"))
        mock_vector = AsyncMock()
        mock_llm = AsyncMock()
        mock_executor = AsyncMock()

        clients = ClientSet(
            graph=mock_graph,
            vector=mock_vector,
            llm=mock_llm,
            query_executor=mock_executor,
        )
        await clients.close()
        mock_vector.close.assert_called_once()
        mock_llm.close.assert_called_once()
        mock_executor.close.assert_called_once()
