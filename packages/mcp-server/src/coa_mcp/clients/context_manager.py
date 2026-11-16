# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""HTTP client for invoking the Context Manager AgentCore Runtime.

The MCP server is a thin protocol adapter. Execution tools (query,
translate_sparql, rag_retrieval, graph_traversal) delegate to the Context
Manager via its AgentCore invocations endpoint. This avoids duplicating
DB/Neptune/OpenSearch/Bedrock access in the MCP runtime.

Runtime session affinity: AgentCore starts a new runtime session (fresh microVM, cold start plus the CM's
lazy initialization) for every invocation without ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``. The tool
layer passes a stable id per MCP session (``derive_runtime_session_id``), so consecutive tool calls reuse one
warm CM microVM. The id only selects the microVM; the payload, and so the request's semantics, is unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from urllib.parse import quote

import httpx
import structlog
from httpx_sse import aconnect_sse

logger = structlog.get_logger(__name__)

# AgentCore invocation timeout. No API Gateway in this path, so no 30s cap.
# TODO: 120s is BELOW serve's RESOLVE_TIMEOUT_S (170s), so this aborts while serve
# still works — agentic p50 is ~141s. Raising it needs an infra value + MCP retest.
_INVOKE_TIMEOUT_S = int(os.environ.get("CM_INVOKE_TIMEOUT_S", "120"))

RUNTIME_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
_RUNTIME_SESSION_ID_PREFIX = "coa-mcp-"

# AgentCore answers 409 RetryableConflictException ("Session operation in progress,
# please retry") while it provisions or tears down the session's microVM. With a
# shared runtime session id, the parallel tool calls an agent fires on a fresh MCP
# session can all land in that window. The request never reached the CM, so a short
# backoff retry is safe (no double execution). Attempts are bounded so a stuck
# session still fails fast relative to the invoke timeout.
_CONFLICT_RETRY_DELAYS_S = (0.25, 0.5, 1.0, 2.0)


def derive_runtime_session_id(user_id: str, *, mcp_session_id: str | None, namespace_id: str) -> str:
    """Derive the CM runtime session id for one tool call.

    Deterministic, so every call of one MCP session lands on the same warm CM
    microVM. Scoped by ``mcp_session_id`` when the caller resolved one, else by
    ``namespace_id``, so a session-less caller still reuses one microVM per
    namespace.

    ``user_id`` is ALWAYS part of the key: AgentCore treats a runtime session as
    the isolation boundary (one microVM per session) but does not bind sessions to
    users, so two callers must never share one. Hashing an injective JSON encoding
    of the parts keeps distinct (user, scope) pairs distinct, keeps the header
    within AgentCore's character set and length bounds whatever the inputs hold,
    and does not echo the MCP session id or user id onto another service's logs.
    """
    scope = ["mcp-session", mcp_session_id] if mcp_session_id else ["namespace", namespace_id]
    digest = hashlib.sha256(json.dumps([user_id, *scope]).encode()).hexdigest()
    # 8-char prefix + 64 hex chars = 72, inside AgentCore's 33..256 bounds (a shorter id is a 400).
    return f"{_RUNTIME_SESSION_ID_PREFIX}{digest}"


def _is_retryable_conflict(status_code: int, body: str) -> bool:
    """Whether a response is AgentCore's RetryableConflictException.

    It shares HTTP 409 with the non-retryable ConflictException; they differ only by
    message, so the body is inspected.
    """
    if status_code != 409:
        return False
    text = body.lower()
    return "retryableconflict" in text or "please retry" in text


class ContextManagerClient:
    """Async HTTP client for invoking the Context Manager via AgentCore."""

    def __init__(self, runtime_arn: str, region: str):
        """Initialize the client and build the AgentCore invocation URL."""
        encoded_arn = quote(runtime_arn, safe="")
        self._url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        )
        # Fast connect timeout, long read budget (the CM does full tiered
        # resolution). No retry here: the CM retries its own dependencies, so
        # retrying the whole invocation would stack retries across layers.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=2.0,
                read=_INVOKE_TIMEOUT_S,
                write=_INVOKE_TIMEOUT_S,
                pool=_INVOKE_TIMEOUT_S,
            )
        )
        logger.info(
            "cm_client_initialized",
            runtime_arn=runtime_arn,
            region=region,
        )

    async def invoke(self, payload: dict, bearer_token: str, *, runtime_session_id: str | None = None) -> dict:
        """Invoke the Context Manager with the given payload.

        The Context Manager's @app.entrypoint is an async generator. AgentCore
        returns responses as text/event-stream (SSE). We use httpx-sse to parse
        the stream per the SSE spec and return the first event's JSON payload.

        ``runtime_session_id`` (``derive_runtime_session_id``) routes the call to that AgentCore runtime
        session; ``None`` omits the header, so AgentCore starts a new session for this call. Raises
        ``ContextManagerError`` with the HTTP status, 504 on a timeout or 502 on a transport error.
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        }
        if runtime_session_id:
            headers[RUNTIME_SESSION_HEADER] = runtime_session_id
        # One attempt per delay, plus a final attempt after the last delay (None: no retry left).
        for delay_s in (*_CONFLICT_RETRY_DELAYS_S, None):
            try:
                return await self._invoke_once(payload, headers)
            except _RetryableConflictError as exc:
                if delay_s is None:
                    logger.warning("cm_invoke_session_conflict_exhausted", attempts=len(_CONFLICT_RETRY_DELAYS_S) + 1)
                    raise ContextManagerError(409, str(exc)) from exc
                logger.info("cm_invoke_session_conflict_retry", delay_s=delay_s)
                await asyncio.sleep(delay_s)
        raise AssertionError("unreachable: the final attempt returns or raises")

    async def _invoke_once(self, payload: dict, headers: dict[str, str]) -> dict:
        """One invocation attempt; raises ``_RetryableConflictError`` on a session conflict."""
        try:
            async with aconnect_sse(self._client, "POST", self._url, json=payload, headers=headers) as event_source:
                status_code = event_source.response.status_code
                if status_code >= 400:
                    # Lenient decode: a non-UTF-8 error body (e.g. from a proxy) must not mask the HTTP status.
                    full_body = (await event_source.response.aread()).decode(errors="replace")
                    body = full_body[:200]
                    if _is_retryable_conflict(status_code, full_body):
                        raise _RetryableConflictError(f"Context Manager returned 409: {body}")
                    logger.warning("cm_invoke_error", status=status_code, body=body)
                    raise ContextManagerError(status_code, f"Context Manager returned {status_code}: {body}")

                async for sse in event_source.aiter_sse():
                    return sse.json()

                raise ContextManagerError(502, "Context Manager returned empty SSE stream")

        except httpx.TimeoutException as exc:
            logger.error("cm_invoke_timeout", timeout_s=_INVOKE_TIMEOUT_S)
            raise ContextManagerError(504, f"Context Manager request timed out ({_INVOKE_TIMEOUT_S}s)") from exc
        except httpx.HTTPError as exc:
            logger.error("cm_invoke_http_error", error=str(exc))
            raise ContextManagerError(502, f"HTTP error invoking Context Manager: {exc}") from exc

    async def close(self) -> None:
        """Close the underlying HTTP client, releasing pooled connections."""
        await self._client.aclose()


class ContextManagerError(Exception):
    """Raised when a Context Manager invocation fails, at any layer.

    One type for transport failures (raised here) and CM-signalled errors (raised
    by ``tools.execution`` from a ``statusCode >= 400`` in the response), so the
    tool layer maps every CM failure to the same ``CM <code>: <message>`` error.
    ``status_code`` is the HTTP status when AgentCore/the CM returned one;
    otherwise the gateway-style code for the failure: 504 for a timeout, 502 for
    a transport error or an empty response stream.
    """

    def __init__(self, status_code: int, message: str) -> None:
        """Record ``status_code`` alongside the human-readable ``message``."""
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _RetryableConflictError(Exception):
    """AgentCore is provisioning or tearing down the runtime session; retry shortly."""
