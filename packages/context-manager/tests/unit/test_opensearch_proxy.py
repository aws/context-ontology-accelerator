# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for OpenSearch client Lambda proxy path."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_serve.clients.opensearch import OpenSearchVectorClient


@pytest.fixture
def proxy_client(monkeypatch):
    """Client configured to use Lambda proxy."""
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://test.us-west-2.aoss.amazonaws.com")
    monkeypatch.setenv("OPENSEARCH_PROXY_LAMBDA_ARN", "arn:aws:lambda:us-west-2:123:function:proxy")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    with patch("boto3.client") as mock_boto:
        mock_lambda = MagicMock()
        mock_boto.return_value = mock_lambda
        client = OpenSearchVectorClient()
        client._lambda_client = mock_lambda
        yield client, mock_lambda


@pytest.fixture
def direct_client(monkeypatch):
    """Client configured for direct AOSS access (no proxy)."""
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://test.us-west-2.aoss.amazonaws.com")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("OPENSEARCH_PROXY_LAMBDA_ARN", raising=False)
    return OpenSearchVectorClient()


@pytest.mark.unit
class TestProxyPath:
    async def test_invoke_proxy_success(self, proxy_client):
        client, mock_lambda = proxy_client
        mock_response = {
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(
                        {
                            "statusCode": 200,
                            "body": {
                                "hits": {
                                    "hits": [{"_id": "1", "_score": 0.9, "_source": {"text": "hello", "label": "test"}}]
                                }
                            },
                        }
                    ).encode()
                )
            ),
            "StatusCode": 200,
        }
        mock_lambda.invoke.return_value = mock_response

        results = await client.search([0.1] * 1024, namespace="test", top_k=5, index="chunk_abc")
        assert len(results) == 1
        assert results[0].text == "hello"
        assert results[0].score == 0.9
        assert results[0].metadata["label"] == "test"

    async def test_invoke_proxy_function_error(self, proxy_client):
        client, mock_lambda = proxy_client
        mock_response = {
            "Payload": MagicMock(read=MagicMock(return_value=b'{"errorMessage":"timeout"}')),
            "FunctionError": "Unhandled",
        }
        mock_lambda.invoke.return_value = mock_response

        with pytest.raises(RuntimeError, match="Vector search proxy returned an error"):
            await client.search([0.1] * 1024, namespace="test")

    async def test_invoke_proxy_500_response(self, proxy_client):
        client, mock_lambda = proxy_client
        mock_response = {
            "Payload": MagicMock(
                read=MagicMock(return_value=json.dumps({"statusCode": 500, "error": "Internal search error"}).encode())
            ),
        }
        mock_lambda.invoke.return_value = mock_response

        with pytest.raises(RuntimeError, match="Vector search proxy returned an error"):
            await client.search([0.1] * 1024, namespace="test")

    async def test_invoke_proxy_empty_on_missing_index(self, proxy_client):
        """Proxy returns 200 with empty hits when index doesn't exist."""
        client, mock_lambda = proxy_client
        mock_response = {
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps(
                        {"statusCode": 200, "body": {"hits": {"total": {"value": 0}, "hits": []}}}
                    ).encode()
                )
            ),
            "StatusCode": 200,
        }
        mock_lambda.invoke.return_value = mock_response

        results = await client.search([0.1] * 1024, namespace="test", index="chunk_nonexistent")
        assert results == []

    async def test_health_check_via_proxy(self, proxy_client):
        client, mock_lambda = proxy_client
        mock_response = {
            "Payload": MagicMock(
                read=MagicMock(
                    return_value=json.dumps({"statusCode": 200, "body": {"version": {"number": "2.11"}}}).encode()
                )
            ),
        }
        mock_lambda.invoke.return_value = mock_response

        result = await client.health_check()
        assert result["status"] == "healthy"
        assert result["via"] == "lambda_proxy"


@pytest.mark.unit
class TestDirectPath:
    def test_no_proxy_arn_means_direct(self, direct_client):
        assert direct_client._proxy_lambda_arn == ""
        assert direct_client._lambda_client is None


@pytest.mark.unit
class TestParseHits:
    def test_parse_empty_results(self, direct_client):
        results = direct_client._parse_hits({"hits": {"hits": []}})
        assert results == []

    def test_parse_hits_extracts_metadata(self, direct_client):
        raw = {
            "hits": {
                "hits": [
                    {
                        "_id": "doc1",
                        "_score": 0.85,
                        "_source": {
                            "text": "Insurance deductibles...",
                            "source_doc": "policy.pdf",
                            "label": "Deductibles",
                            "uri": "s3://docs/policy.pdf",
                            "type": "document",
                            "namespace": "insurance",
                            "chunk_index": 3,
                            "description": "About deductibles",
                        },
                    }
                ]
            }
        }
        results = direct_client._parse_hits(raw)
        assert len(results) == 1
        hit = results[0]
        assert hit.id == "doc1"
        assert hit.text == "Insurance deductibles..."
        assert hit.score == 0.85
        assert hit.metadata["source_doc"] == "policy.pdf"
        assert hit.metadata["label"] == "Deductibles"
        assert hit.metadata["chunk_index"] == 3
