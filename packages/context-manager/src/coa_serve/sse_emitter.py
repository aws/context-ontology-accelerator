# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

r"""SSE event emitter for HTTP streaming responses.

Produces typed event dicts that the @app.entrypoint yields to AgentCore.
AgentCore handles SSE framing automatically (wraps each yielded item as
a `data: <json>\n\n` line in the text/event-stream response).

The event envelope structure maintains wire-level compatibility with the
frontend playground reducer:
  {type, requestId, timestamp, payload, sessionId?}

AgentCore HTTP protocol contract:
- yield dict → AgentCore serializes to JSON, sends as `data: {json}\n\n`
- yield str → AgentCore sends as `data: {string}\n\n`

Reference:
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from .models import QueryResult, TraceStep
from .serialization import json_safe as _json_safe

logger = structlog.get_logger(__name__)


class SSEEmitter:
    """Produces typed event dicts for AgentCore streaming via yield.

    Orchestrator callbacks (on_step, on_token) push events to an internal
    asyncio.Queue. The ``stream()`` async generator drains the queue and
    yields event dicts for the @app.entrypoint to yield.
    """

    def __init__(self, request_id: str, session_id: str | None = None) -> None:
        """Initialize the emitter with correlation ids and an empty event queue.

        Args:
            request_id: Correlation ID included in every event envelope.
            session_id: Optional AgentCore session ID, included when active.
        """
        self._request_id = request_id
        self._session_id = session_id
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """Return whether the emitter has been closed and stops accepting events."""
        return self._closed

    def _make_envelope(self, event_type: str, payload: dict) -> dict:
        """Build a typed event envelope dict."""
        envelope: dict = {
            "type": event_type,
            "requestId": self._request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": _json_safe(payload),
        }
        if self._session_id:
            envelope["sessionId"] = self._session_id
        return envelope

    def queue_step(self, step: TraceStep) -> None:
        """Queue a step event from a TraceCollector on_record callback."""
        if self._closed:
            return
        serialized = step.model_dump(by_alias=True, exclude_none=True)
        # The frontend reducer expects "stepName", not "step"
        if "step" in serialized:
            serialized["stepName"] = serialized.pop("step")
        self._queue.put_nowait(self._make_envelope("step", serialized))

    def queue_token(self, text: str) -> None:
        """Queue a token event from the Tier 3 on_token callback."""
        if self._closed:
            return
        self._queue.put_nowait(self._make_envelope("token", {"text": text}))

    def format_done(self, result: QueryResult) -> dict:
        """Build the final done event with the full QueryResult payload."""
        payload = result.model_dump(by_alias=True, exclude_none=True, mode="json")
        return self._make_envelope("done", {"result": payload})

    def format_error(self, error: str, message: str, status_code: int, *, llm_usage: dict | None = None) -> dict:
        """Build an error event for mid-stream failures."""
        payload = {"error": error, "message": message, "statusCode": status_code}
        if llm_usage is not None:
            payload["llmUsage"] = llm_usage
        return self._make_envelope(
            "error",
            payload,
        )

    async def stream(self):
        """Async generator that drains queued events until sentinel None.

        Yields event dicts ready for the @app.entrypoint to yield.
        AgentCore serializes them to JSON and sends as SSE `data:` lines.
        """
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    def done(self) -> None:
        """Signal that no more events will be queued (sends sentinel)."""
        self._queue.put_nowait(None)

    def close(self) -> None:
        """Mark emitter as closed and push sentinel to unblock stream()."""
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)
