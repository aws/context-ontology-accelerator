# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SSEEmitter (sse_emitter.py)."""

from __future__ import annotations

import asyncio

import pytest
from coa_serve.models import ConfidenceScore, QueryResult, TraceStep
from coa_serve.sse_emitter import SSEEmitter

pytestmark = pytest.mark.unit


class TestSSEEmitterEnvelope:
    def test_envelope_has_required_fields(self):
        emitter = SSEEmitter("req-1", session_id="sess-1")
        envelope = emitter._make_envelope("step", {"stepName": "t1.resolve"})

        assert envelope["type"] == "step"
        assert envelope["requestId"] == "req-1"
        assert envelope["sessionId"] == "sess-1"
        assert "timestamp" in envelope
        assert envelope["payload"] == {"stepName": "t1.resolve"}

    def test_envelope_omits_session_id_when_none(self):
        emitter = SSEEmitter("req-2", session_id=None)
        envelope = emitter._make_envelope("token", {"text": "hi"})

        assert "sessionId" not in envelope
        assert envelope["requestId"] == "req-2"


class TestSSEEmitterQueueStep:
    def test_queue_step_renames_step_to_stepName(self):
        emitter = SSEEmitter("req-1")
        step = TraceStep(step="t1.resolve", status="active", durationMs=42)
        emitter.queue_step(step)

        event = emitter._queue.get_nowait()
        assert event["type"] == "step"
        assert event["payload"]["stepName"] == "t1.resolve"
        assert "step" not in event["payload"]
        assert event["payload"]["status"] == "active"
        assert event["payload"]["durationMs"] == 42

    def test_queue_step_noop_when_closed(self):
        emitter = SSEEmitter("req-1")
        emitter.close()
        step = TraceStep(step="t1.resolve", status="active", durationMs=0)
        emitter.queue_step(step)

        assert emitter._queue.qsize() == 1  # just the sentinel


class TestSSEEmitterQueueToken:
    def test_queue_token_produces_correct_envelope(self):
        emitter = SSEEmitter("req-1", session_id="sess-1")
        emitter.queue_token("Hello")

        event = emitter._queue.get_nowait()
        assert event["type"] == "token"
        assert event["payload"]["text"] == "Hello"
        assert event["sessionId"] == "sess-1"

    def test_queue_token_noop_when_closed(self):
        emitter = SSEEmitter("req-1")
        emitter.close()
        emitter.queue_token("ignored")

        assert emitter._queue.qsize() == 1


class TestSSEEmitterFormatDone:
    def test_format_done_wraps_query_result(self):
        emitter = SSEEmitter("req-1", session_id="sess-1")
        result = QueryResult(
            tier=2,
            confidence=ConfidenceScore(score=0.9, rationale="high"),
            trace=[TraceStep(step="t2.sql", status="done", durationMs=100)],
            result_rows=[{"col": "val"}],
        )
        event = emitter.format_done(result)

        assert event["type"] == "done"
        assert event["requestId"] == "req-1"
        assert event["sessionId"] == "sess-1"
        assert event["payload"]["result"]["tier"] == 2
        assert event["payload"]["result"]["confidence"]["score"] == 0.9
        assert len(event["payload"]["result"]["resultRows"]) == 1


class TestSSEEmitterFormatError:
    def test_format_error_produces_typed_event(self):
        emitter = SSEEmitter("req-1")
        event = emitter.format_error("TimeoutError", "Query timed out", 504)

        assert event["type"] == "error"
        assert event["requestId"] == "req-1"
        assert event["payload"]["error"] == "TimeoutError"
        assert event["payload"]["message"] == "Query timed out"
        assert event["payload"]["statusCode"] == 504

    def test_format_error_can_include_llm_usage(self):
        emitter = SSEEmitter("req-1")
        usage = {"inputTokens": 10, "outputTokens": 1, "perModel": [], "complete": False}

        event = emitter.format_error("TimeoutError", "Query timed out", 504, llm_usage=usage)

        assert event["payload"]["llmUsage"] == usage


class TestSSEEmitterStream:
    @pytest.mark.asyncio
    async def test_stream_yields_queued_events_then_stops(self):
        emitter = SSEEmitter("req-1")
        emitter.queue_token("a")
        emitter.queue_token("b")
        emitter.done()

        events = []
        async for event in emitter.stream():
            events.append(event)

        assert len(events) == 2
        assert events[0]["payload"]["text"] == "a"
        assert events[1]["payload"]["text"] == "b"

    @pytest.mark.asyncio
    async def test_stream_stops_on_close(self):
        emitter = SSEEmitter("req-1")
        emitter.queue_token("x")
        emitter.close()

        events = []
        async for event in emitter.stream():
            events.append(event)

        assert len(events) == 1
        assert events[0]["payload"]["text"] == "x"

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        emitter = SSEEmitter("req-1")
        emitter.close()
        emitter.close()  # should not push another sentinel

        assert emitter.is_closed
        assert emitter._queue.qsize() == 1


class TestSSEEmitterDone:
    @pytest.mark.asyncio
    async def test_done_sends_sentinel_stopping_stream(self):
        emitter = SSEEmitter("req-1")

        async def producer():
            await asyncio.sleep(0.01)
            emitter.done()

        asyncio.create_task(producer())

        events = []
        async for event in emitter.stream():
            events.append(event)

        assert events == []
