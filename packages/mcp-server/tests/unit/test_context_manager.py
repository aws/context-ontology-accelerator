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
from coa_mcp.clients import context_manager
from coa_mcp.clients.context_manager import (
    RUNTIME_SESSION_HEADER,
    ContextManagerClient,
    ContextManagerError,
    derive_runtime_session_id,
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


def _mock_sse_sequence(responses: list[tuple[int, bytes, list[dict]]]):
    """aconnect_sse mock answering each call with the next (status, body, events).

    Returns ``(mock, calls)``; ``calls`` collects each call's kwargs.
    """
    calls: list[dict] = []

    @asynccontextmanager
    async def _aconnect_sse(*args, **kwargs):
        status_code, body, events = responses[len(calls)]
        calls.append(kwargs)
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.aread = AsyncMock(return_value=body)
        event_source = MagicMock()
        event_source.response = mock_response

        async def _aiter_sse():
            for event_data in events:
                sse_event = MagicMock()
                sse_event.json.return_value = event_data
                yield sse_event

        event_source.aiter_sse = _aiter_sse
        yield event_source

    return _aconnect_sse, calls


_RETRYABLE_CONFLICT_BODY = b'{"message":"Session operation in progress, please retry"}'


@pytest.mark.unit
class TestRuntimeSessionHeader:
    @pytest.mark.asyncio
    async def test_invoke_sends_runtime_session_header_when_given(self):
        client = _make_client()
        mock_sse = _mock_sse_response(200, events=[{}])
        session_id = derive_runtime_session_id("alice", mcp_session_id="mcp-1", namespace_id="sales")

        with patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse):
            await client.invoke({"query": "q"}, "tok", runtime_session_id=session_id)

        assert mock_sse.last_call_kwargs["headers"][RUNTIME_SESSION_HEADER] == session_id

    @pytest.mark.asyncio
    async def test_invoke_omits_runtime_session_header_by_default(self):
        client = _make_client()
        mock_sse = _mock_sse_response(200, events=[{}])

        with patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse):
            await client.invoke({"query": "q"}, "tok")

        assert RUNTIME_SESSION_HEADER not in mock_sse.last_call_kwargs["headers"]


@pytest.mark.unit
class TestDeriveRuntimeSessionId:
    def test_derive_runtime_session_id_deterministic_and_long_enough(self):
        first = derive_runtime_session_id("alice", mcp_session_id="mcp-1", namespace_id="sales")
        second = derive_runtime_session_id("alice", mcp_session_id="mcp-1", namespace_id="hr")
        assert first == second
        assert len(first) >= 33
        assert first.replace("-", "").isalnum()

    def test_derive_runtime_session_id_short_inputs_meet_agentcore_minimum(self):
        assert len(derive_runtime_session_id("a", mcp_session_id=None, namespace_id="")) >= 33

    def test_derive_runtime_session_id_distinct_users_and_scopes(self):
        ids = {
            derive_runtime_session_id("alice", mcp_session_id="mcp-1", namespace_id="sales"),
            derive_runtime_session_id("bob", mcp_session_id="mcp-1", namespace_id="sales"),
            derive_runtime_session_id("alice", mcp_session_id="mcp-2", namespace_id="sales"),
            derive_runtime_session_id("alice", mcp_session_id=None, namespace_id="sales"),
            derive_runtime_session_id("alice", mcp_session_id=None, namespace_id="hr"),
            # A namespace equal to an MCP session id must not collide with that session.
            derive_runtime_session_id("alice", mcp_session_id=None, namespace_id="mcp-1"),
        }
        assert len(ids) == 6


@pytest.mark.unit
class TestSessionConflictRetry:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(context_manager.asyncio, "sleep", AsyncMock())

    @pytest.mark.asyncio
    async def test_invoke_retries_retryable_conflict_then_succeeds(self):
        client = _make_client()
        mock_sse, calls = _mock_sse_sequence(
            [(409, _RETRYABLE_CONFLICT_BODY, []), (409, _RETRYABLE_CONFLICT_BODY, []), (200, b"", [{"ok": True}])]
        )

        with patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse):
            result = await client.invoke({"query": "q"}, "tok", runtime_session_id="s" * 40)

        assert result == {"ok": True}
        assert len(calls) == 3
        assert all(call["headers"][RUNTIME_SESSION_HEADER] == "s" * 40 for call in calls)

    @pytest.mark.asyncio
    async def test_invoke_retryable_conflict_exhausted_raises(self):
        client = _make_client()
        attempts = len(context_manager._CONFLICT_RETRY_DELAYS_S) + 1
        mock_sse, calls = _mock_sse_sequence([(409, _RETRYABLE_CONFLICT_BODY, [])] * attempts)

        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse),
            pytest.raises(ContextManagerError, match="409"),
        ):
            await client.invoke({}, "tok")

        assert len(calls) == attempts

    @pytest.mark.asyncio
    async def test_invoke_non_retryable_conflict_not_retried(self):
        client = _make_client()
        mock_sse, calls = _mock_sse_sequence([(409, b'{"message":"Resource already exists"}', [])])

        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", mock_sse),
            pytest.raises(ContextManagerError, match="returned 409"),
        ):
            await client.invoke({}, "tok")

        assert len(calls) == 1


@pytest.mark.unit
class TestContextManagerErrorStatus:
    def test_one_error_type_across_client_and_tool_layers(self):
        from coa_mcp.tools import execution

        assert execution.ContextManagerError is ContextManagerError

    @pytest.mark.asyncio
    async def test_http_error_carries_response_status(self):
        client = _make_client()
        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", _mock_sse_response(403, body=b"Forbidden")),
            pytest.raises(ContextManagerError) as info,
        ):
            await client.invoke({}, "tok")
        assert info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_invoke_non_utf8_error_body_keeps_response_status(self):
        client = _make_client()
        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", _mock_sse_response(502, body=b"\xff\xfe gateway")),
            pytest.raises(ContextManagerError) as info,
        ):
            await client.invoke({}, "tok")
        assert info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_timeout_carries_504_and_transport_error_502(self):
        client = _make_client()

        @asynccontextmanager
        async def _raise(exc):
            raise exc
            yield  # noqa: RUF027 — unreachable but needed for generator syntax

        for exc, code in ((httpx.ReadTimeout("slow"), 504), (httpx.ConnectError("no route"), 502)):
            with (
                patch("coa_mcp.clients.context_manager.aconnect_sse", lambda *a, _e=exc, **k: _raise(_e)),
                pytest.raises(ContextManagerError) as info,
            ):
                await client.invoke({}, "tok")
            assert info.value.status_code == code

    @pytest.mark.asyncio
    async def test_empty_stream_carries_502(self):
        client = _make_client()
        with (
            patch("coa_mcp.clients.context_manager.aconnect_sse", _mock_sse_response(200, events=[])),
            pytest.raises(ContextManagerError) as info,
        ):
            await client.invoke({}, "tok")
        assert info.value.status_code == 502
