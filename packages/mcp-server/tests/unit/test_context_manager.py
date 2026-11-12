# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Context Manager AgentCore HTTP client.

The client POSTs a payload to the AgentCore invocations endpoint via httpx-sse
and forwards the caller's JWT. These tests mock httpx_sse.aconnect_sse so no
real network calls happen, verifying URL/header construction and error mapping.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from coa_mcp.clients.context_manager import (
    ContextManagerClient,
    ContextManagerError,
)


def _make_client() -> ContextManagerClient:
    """Build a client; the SSE layer is mocked per test."""
    return ContextManagerClient(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123:runtime/cm-abc",
        region="us-east-1",
    )


def _mock_sse_response(status_code: int = 200, events: list[dict] | None = None, body: bytes = b""):
    """Create a mock for aconnect_sse that yields SSE events.

    Args:
        status_code: HTTP status code on the response.
        events: List of dicts to return as SSE event JSON payloads.
        body: Raw body bytes for error responses.
    """
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.aread = AsyncMock(return_value=body)

    mock_event_source = MagicMock()
    mock_event_source.response = mock_response

    async def _aiter_sse():
        for event_data in events or []:
            sse_event = MagicMock()
            sse_event.json.return_value = event_data
            yield sse_event

    mock_event_source.aiter_sse = _aiter_sse

    @asynccontextmanager
    async def _aconnect_sse(*args, **kwargs):
        _aconnect_sse.last_call_args = args
        _aconnect_sse.last_call_kwargs = kwargs
        yield mock_event_source

    return _aconnect_sse


@pytest.mark.unit
class TestContextManagerClientInit:
    def test_url_includes_encoded_arn_and_region(self):
        client = ContextManagerClient(
            runtime_arn="arn:aws:bedrock-agentcore:eu-west-1:1:runtime/cm",
            region="eu-west-1",
        )
        assert "bedrock-agentcore.eu-west-1.amazonaws.com" in client._url
        # The ARN colons/slashes must be percent-encoded into the path segment.
        assert "arn%3Aaws%3Abedrock-agentcore" in client._url
        assert client._url.endswith("/invocations?qualifier=DEFAULT")


@pytest.mark.unit
class TestContextManagerInvoke:
    @pytest.mark.asyncio
    async def test_returns_parsed_json_on_success(self):
        client = _make_client()
        mock_sse = _mock_sse_response(200, events=[{"result": {"tier": 1}}])

        with patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse):
            result = await client.invoke({"query": "hi"}, "my-jwt")

        assert result == {"result": {"tier": 1}}

    @pytest.mark.asyncio
    async def test_forwards_bearer_token_and_payload(self):
        client = _make_client()
        mock_sse = _mock_sse_response(200, events=[{}])

        with patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse):
            await client.invoke({"query": "revenue"}, "jwt-123")

        kwargs = mock_sse.last_call_kwargs
        assert kwargs["json"] == {"query": "revenue"}
        assert kwargs["headers"]["Authorization"] == "Bearer jwt-123"
        assert kwargs["headers"]["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_http_error_status_raises_context_manager_error(self):
        client = _make_client()
        mock_sse = _mock_sse_response(403, body=b"Forbidden")

        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse),
            pytest.raises(ContextManagerError, match="returned 403"),
        ):
            await client.invoke({}, "tok")

    @pytest.mark.asyncio
    async def test_timeout_raises_context_manager_error(self):
        client = _make_client()

        @asynccontextmanager
        async def _raise_timeout(*args, **kwargs):
            raise httpx.TimeoutException("slow")
            yield  # noqa: RUF027 — unreachable but needed for generator syntax

        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", _raise_timeout),
            pytest.raises(ContextManagerError, match="timed out"),
        ):
            await client.invoke({}, "tok")

    @pytest.mark.asyncio
    async def test_http_error_raises_context_manager_error(self):
        client = _make_client()

        @asynccontextmanager
        async def _raise_connect(*args, **kwargs):
            raise httpx.ConnectError("no route")
            yield  # noqa: RUF027 — unreachable but needed for generator syntax

        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", _raise_connect),
            pytest.raises(ContextManagerError, match="HTTP error"),
        ):
            await client.invoke({}, "tok")

    @pytest.mark.asyncio
    async def test_empty_stream_raises_context_manager_error(self):
        client = _make_client()
        mock_sse = _mock_sse_response(200, events=[])

        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse),
            pytest.raises(ContextManagerError, match="empty SSE stream"),
        ):
            await client.invoke({}, "tok")

    @pytest.mark.asyncio
    async def test_close_delegates_to_httpx_aclose(self):
        client = _make_client()
        client._client = MagicMock()
        client._client.aclose = AsyncMock()
        await client.close()
        client._client.aclose.assert_awaited_once()
