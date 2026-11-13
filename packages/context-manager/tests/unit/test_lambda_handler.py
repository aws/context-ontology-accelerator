# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the AOSS search proxy Lambda handler input validation."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_DIR = str(Path(__file__).resolve().parents[2] / "lambda" / "aoss-search-proxy")


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://test.us-west-2.aoss.amazonaws.com")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


@pytest.fixture
def handler():
    """Import handler with mocked OpenSearch client."""
    sys.modules.pop("index", None)
    with patch("boto3.Session") as mock_session:
        mock_creds = MagicMock()
        mock_session.return_value.get_credentials.return_value = mock_creds
        mock_creds.get_frozen_credentials.return_value = mock_creds
        sys.path.insert(0, _LAMBDA_DIR)
        import index

        index._client = None  # Reset singleton
        sys.path.pop(0)
        yield index.handler


@pytest.mark.unit
class TestLambdaValidation:
    def test_invalid_action_rejected(self, handler):
        result = handler({"action": "delete"}, None)
        assert result["statusCode"] == 400
        assert "Invalid action" in result["error"]

    def test_invalid_index_name_rejected(self, handler):
        result = handler({"action": "search", "index": "../../../etc", "body": {}}, None)
        assert result["statusCode"] == 400
        assert "Invalid index" in result["error"]

    def test_body_must_be_dict(self, handler):
        result = handler({"action": "search", "index": "test-index", "body": "not a dict"}, None)
        assert result["statusCode"] == 400
        assert "body must be a dict" in result["error"]

    def test_disallowed_query_shape_rejected(self, handler):
        result = handler(
            {"action": "search", "index": "test-index", "body": {"query": {"script": {"source": "malicious"}}}}, None
        )
        assert result["statusCode"] == 400
        assert "Only knn, bool, or match_all queries allowed" in result["error"]

    def test_knn_query_allowed(self, handler):
        result = handler(
            {
                "action": "search",
                "index": "test-index",
                "body": {"query": {"knn": {"embedding": {"vector": [0.1], "k": 5}}}, "size": 100},
            },
            None,
        )
        assert result["statusCode"] != 400

    def test_size_capped_at_50(self, handler):
        client = MagicMock()
        with patch.object(sys.modules[handler.__module__], "_get_client", return_value=client):
            handler(
                {"action": "search", "index": "test-index", "body": {"query": {"match_all": {}}, "size": 999}}, None
            )
        assert client.search.call_args.kwargs["body"]["size"] == 50

    def test_health_check_action(self, handler):
        result = handler({"action": "health_check"}, None)
        assert result["statusCode"] != 400

    def test_valid_index_patterns(self, handler):
        for idx in ["document-chunks", "chunk_abc123", "my_index"]:
            result = handler({"action": "search", "index": idx, "body": {"query": {"match_all": {}}}}, None)
            assert result["statusCode"] != 400, f"Index '{idx}' should be valid"
