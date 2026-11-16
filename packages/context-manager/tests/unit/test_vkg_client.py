# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for VKG Service HTTP client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from coa_serve.clients.vkg import VKGClient, VKGResult, VKGTranslationError


def _mock_response(status_code=200, json_data=None, text=""):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


@pytest.mark.unit
class TestVKGClientInit:
    def test_requires_base_url(self):
        with pytest.raises(ValueError, match="VKG Service URL must be provided"):
            VKGClient(base_url="")

    def test_accepts_base_url(self):
        client = VKGClient(base_url="http://vkg.local:8080")
        assert client._base_url == "http://vkg.local:8080"

    def test_strips_trailing_slash(self):
        client = VKGClient(base_url="http://vkg.local:8080/")
        assert client._base_url == "http://vkg.local:8080"

    def test_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("VKG_SERVICE_URL", "http://from-env:9090")
        client = VKGClient()
        assert client._base_url == "http://from-env:9090"


@pytest.mark.unit
class TestVKGClientTranslate:
    async def test_successful_translation(self):
        client = VKGClient(base_url="http://vkg.local:8080")

        mock_http = AsyncMock()
        mock_http.post.return_value = _mock_response(
            200,
            json_data={
                "sql": "SELECT id, name FROM orders",
                "dialect": "postgresql",
                "ontologyVersion": "v2.1",
                "sourceTableRefs": ["catalog.public.orders"],
            },
        )
        client._http = mock_http

        result = await client.translate("SELECT ?o WHERE { ?o a :Order }", namespace="demo")

        assert isinstance(result, VKGResult)
        assert result.sql == "SELECT id, name FROM orders"
        assert result.dialect == "postgresql"
        assert result.ontology_version == "v2.1"
        assert result.source_table_refs == ["catalog.public.orders"]

    @patch("retryhttp.retry", lambda **kwargs: lambda f: f)
    async def test_503_retry(self):
        client = VKGClient(base_url="http://vkg.local:8080", max_retries=3)

        mock_http = AsyncMock()
        # retryhttp handles the retry; we test that 503 raises HTTPStatusError
        # which translate() wraps in VKGTranslationError
        mock_http.post.return_value = _mock_response(503, text="Service Unavailable")
        client._http = mock_http

        with pytest.raises(VKGTranslationError, match="VKG returned 503"):
            await client.translate("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

    @patch("retryhttp.retry", lambda **kwargs: lambda f: f)
    async def test_non_200_raises(self):
        client = VKGClient(base_url="http://vkg.local:8080", max_retries=1)

        mock_http = AsyncMock()
        mock_http.post.return_value = _mock_response(400, text="Bad Request: invalid SPARQL")
        client._http = mock_http

        with pytest.raises(VKGTranslationError, match="VKG returned 400"):
            await client.translate("INVALID", namespace="demo")

    async def test_missing_sql_field_raises(self):
        client = VKGClient(base_url="http://vkg.local:8080")

        mock_http = AsyncMock()
        mock_http.post.return_value = _mock_response(200, json_data={"dialect": "postgresql"})
        client._http = mock_http

        with pytest.raises(VKGTranslationError, match="parse error"):
            await client.translate("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

    async def test_default_dialect_when_missing(self):
        client = VKGClient(base_url="http://vkg.local:8080")

        mock_http = AsyncMock()
        mock_http.post.return_value = _mock_response(
            200,
            json_data={"sql": "SELECT 1", "ontologyVersion": "v1"},
        )
        client._http = mock_http

        result = await client.translate("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")
        assert result.dialect == "postgresql"

    async def test_health_check_ok(self):
        client = VKGClient(base_url="http://vkg.local:8080")

        mock_http = AsyncMock()
        mock_http.get.return_value = _mock_response(200)
        client._http = mock_http

        status = await client.health_check()
        assert status["status"] == "ok"

    async def test_health_check_degraded(self):
        client = VKGClient(base_url="http://vkg.local:8080")

        mock_http = AsyncMock()
        mock_http.get.return_value = _mock_response(503, text="Unavailable")
        client._http = mock_http

        status = await client.health_check()
        assert status["status"] == "degraded"

    async def test_health_check_error(self):
        client = VKGClient(base_url="http://vkg.local:8080")

        mock_http = AsyncMock()
        mock_http.get.side_effect = RuntimeError("Connection refused")
        client._http = mock_http

        status = await client.health_check()
        assert status["status"] == "error"

    @patch("retryhttp.retry", lambda **kwargs: lambda f: f)
    async def test_connect_error_raises(self):
        client = VKGClient(base_url="http://vkg.local:8080", max_retries=3)

        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.ConnectError("Connection refused")
        client._http = mock_http

        with pytest.raises(VKGTranslationError, match="ConnectError"):
            await client.translate("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

    @patch("retryhttp.retry", lambda **kwargs: lambda f: f)
    async def test_connect_timeout_raises(self):
        client = VKGClient(base_url="http://vkg.local:8080", max_retries=3)

        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.ConnectTimeout("Timed out")
        client._http = mock_http

        with pytest.raises(VKGTranslationError, match="ConnectTimeout"):
            await client.translate("SELECT ?s WHERE { ?s ?p ?o }", namespace="demo")

    @patch("retryhttp.retry", lambda **kwargs: lambda f: f)
    async def test_remote_protocol_error_raises(self):
        client = VKGClient(base_url="http://vkg.local:8080", max_retries=3)

        mock_http = AsyncMock()
        mock_http.post.side_effect = httpx.RemoteProtocolError("Server disconnected")
        client._http = mock_http

        with pytest.raises(VKGTranslationError, match="RemoteProtocolError"):
            await client.translate("SELECT ?c WHERE { ?c a :Customer }", namespace="demo")
