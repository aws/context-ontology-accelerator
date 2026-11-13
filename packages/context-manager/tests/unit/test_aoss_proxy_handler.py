# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the AOSS search proxy Lambda handler."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_DIR = str(Path(__file__).parents[2] / "lambda" / "aoss-search-proxy")
sys.path.insert(0, _LAMBDA_DIR)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://test.us-west-2.aoss.amazonaws.com")
    monkeypatch.setenv("AWS_REGION", "us-west-2")


@pytest.fixture
def _mock_client():
    with patch("index._get_client") as mock_get:
        mock_os = MagicMock()
        mock_get.return_value = mock_os
        yield mock_os


@pytest.mark.unit
class TestAossProxyHandler:
    def test_search_success(self, _mock_client):
        import index

        _mock_client.search.return_value = {"hits": {"total": {"value": 5}, "hits": [{"_id": "1", "_score": 0.9}]}}
        result = index.handler({"action": "search", "index": "chunk_abc", "body": {"query": {"match_all": {}}}}, None)
        assert result["statusCode"] == 200
        assert result["body"]["hits"]["total"]["value"] == 5

    def test_missing_index_returns_empty_not_500(self, _mock_client):
        import index
        from opensearchpy import NotFoundError

        _mock_client.search.side_effect = NotFoundError(404, "index_not_found_exception", {})
        result = index.handler(
            {"action": "search", "index": "chunk_missing", "body": {"query": {"match_all": {}}}}, None
        )
        assert result["statusCode"] == 200
        assert result["body"]["hits"]["total"]["value"] == 0
        assert result["body"]["hits"]["hits"] == []

    def test_other_error_returns_500(self, _mock_client):
        import index

        _mock_client.search.side_effect = RuntimeError("connection failed")
        result = index.handler({"action": "search", "index": "chunk_abc", "body": {"query": {"match_all": {}}}}, None)
        assert result["statusCode"] == 500
        # Error detail is surfaced for debuggability (AOSS 400s carry the real cause).
        assert result["error"].startswith("Internal search error")
        assert "connection failed" in result["error"]

    def test_invalid_action_returns_400(self, _mock_client):
        import index

        result = index.handler({"action": "delete"}, None)
        assert result["statusCode"] == 400
        assert "Invalid action" in result["error"]

    def test_invalid_index_name_returns_400(self, _mock_client):
        import index

        result = index.handler({"action": "search", "index": "../../etc/passwd"}, None)
        assert result["statusCode"] == 400
        assert "Invalid index name" in result["error"]

    def test_caps_result_size(self, _mock_client):
        import index

        _mock_client.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        index.handler(
            {"action": "search", "index": "chunk_abc", "body": {"query": {"match_all": {}}, "size": 9999}}, None
        )
        call_body = _mock_client.search.call_args[1]["body"]
        assert call_body["size"] <= 50
