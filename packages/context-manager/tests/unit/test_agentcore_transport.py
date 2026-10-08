# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pin the integ suites' AgentCore SSE transport retry contract (no network).

Every context-manager integ suite calls the runtime through
``tests/shared/agentcore_transport.post_sse``. These tests fake the HTTP layer with
real ``requests.Response`` objects so ``raise_for_status``/``iter_lines`` behave as
they do against AgentCore.
"""

from __future__ import annotations

import io
import json

import pytest
import requests

from tests.shared import agentcore_transport as mod

pytestmark = pytest.mark.unit

_DONE = {"type": "done", "payload": {"result": {"tier": 1}}}


class _DropsMidStream(io.RawIOBase):
    """A body that sends its first chunk, then fails the next read like a stalled VM."""

    def __init__(self, first: bytes, error: Exception) -> None:
        self._first, self._error = first, error

    def read(self, size: int = -1) -> bytes:
        if self._first:
            chunk, self._first = self._first, b""
            return chunk
        raise self._error


def _response(status: int, body: bytes | io.RawIOBase = b"") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://agentcore.test/invocations"
    response.encoding = "utf-8"
    response.raw = io.BytesIO(body) if isinstance(body, bytes) else body
    return response


def _sse(*frames: dict) -> bytes:
    return b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames)


def _is_terminal(frame: dict) -> bool:
    return frame.get("type") in ("done", "error")


@pytest.fixture
def post(monkeypatch):
    """Queue responses (or exceptions) for successive POSTs; records the call count."""
    queue: list = []
    calls: list[dict] = []

    def _post(*_args, **kwargs):
        calls.append(kwargs)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(mod.requests, "post", _post)
    return queue, calls


def _call(max_attempts: int = 4, sleeps: list | None = None, is_terminal=_is_terminal) -> list[dict]:
    return mod.post_sse(
        "https://agentcore.test/invocations",
        {},
        {"query": "q"},
        is_terminal=is_terminal,
        timeout=1,
        max_attempts=max_attempts,
        backoff_seconds=0,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
    )


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectionError("Read timed out."),
        requests.exceptions.ChunkedEncodingError("Connection broken"),
    ],
)
def test_post_sse_mid_stream_failure_reissues_request_and_discards_partial_frames(post, error) -> None:
    queue, calls = post
    partial = _DropsMidStream(_sse({"type": "step", "payload": {"n": "stale"}}), error)
    queue += [_response(200, partial), _response(200, _sse({"type": "step", "payload": {"n": 1}}, _DONE))]

    frames = _call()

    assert len(calls) == 2
    assert frames == [{"type": "step", "payload": {"n": 1}}, _DONE]


def test_post_sse_pre_response_gateway_5xx_is_retried(post) -> None:
    queue, calls = post
    queue += [_response(502, b"<html>502 Bad Gateway</html>"), _response(200, _sse(_DONE))]

    assert _call() == [_DONE]
    assert len(calls) == 2


def test_post_sse_4xx_is_not_retried_and_body_stays_readable(post) -> None:
    queue, calls = post
    queue += [_response(403, b"denied"), _response(200, _sse(_DONE))]
    sleeps: list = []

    with pytest.raises(requests.exceptions.HTTPError) as raised:
        _call(sleeps=sleeps)

    assert len(calls) == 1
    assert sleeps == []
    assert raised.value.response.text == "denied"


def test_post_sse_application_error_frame_is_not_retried(post) -> None:
    queue, calls = post
    error = {"type": "error", "payload": {"error": "NoResultError"}}
    queue += [_response(200, _sse(error)), _response(200, _sse(_DONE))]

    assert _call() == [error]
    assert len(calls) == 1


def test_post_sse_gives_up_after_max_attempts_on_persistent_504(post) -> None:
    queue, calls = post
    queue += [_response(504, b"Gateway Time-out") for _ in range(3)]
    sleeps: list = []

    with pytest.raises(requests.exceptions.HTTPError) as raised:
        _call(max_attempts=3, sleeps=sleeps)

    assert raised.value.response.status_code == 504
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_post_sse_gives_up_after_max_attempts_on_persistent_read_timeout(post) -> None:
    queue, calls = post
    queue += [requests.exceptions.ReadTimeout("Read timed out.") for _ in range(3)]

    with pytest.raises(requests.exceptions.ReadTimeout):
        _call(max_attempts=3)

    assert len(calls) == 3


def test_post_sse_gives_up_after_max_attempts_on_persistent_mid_stream_drop(post) -> None:
    queue, calls = post
    queue += [
        _response(200, _DropsMidStream(_sse({"type": "step"}), requests.exceptions.ChunkedEncodingError("broken")))
        for _ in range(3)
    ]

    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        _call(max_attempts=3)

    assert len(calls) == 3


def test_post_sse_clean_end_without_terminal_frame_returns_frames_unretried(post) -> None:
    queue, calls = post
    steps = [{"type": "step", "payload": {"n": 1}}, {"type": "step", "payload": {"n": 2}}]
    queue += [_response(200, _sse(*steps)), _response(200, _sse(_DONE))]

    assert _call() == steps
    assert len(calls) == 1


def test_post_sse_stops_reading_at_the_first_terminal_frame(post) -> None:
    queue, calls = post
    first = {"type": "step", "payload": {"n": 1}}
    queue.append(_response(200, _sse(first, {"type": "step", "payload": {"n": 2}}, _DONE)))

    assert _call(is_terminal=lambda _: True) == [first]
    assert len(calls) == 1


def test_post_sse_non_positive_max_attempts_still_makes_one_attempt(post) -> None:
    queue, calls = post
    queue.append(_response(200, _sse(_DONE)))

    assert _call(max_attempts=0) == [_DONE]
    assert len(calls) == 1


def test_runtime_session_id_is_unique_and_meets_agentcore_minimum_length() -> None:
    first, second = mod.runtime_session_id("x"), mod.runtime_session_id("x")

    assert first != second
    assert len(first) >= 33
