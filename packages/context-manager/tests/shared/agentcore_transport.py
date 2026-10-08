# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The one SSE transport every context-manager integ suite uses to call AgentCore.

Extracted here (outside ``tests/integ/``) so the unit suite can pin its retry
behaviour in environments where integ tests are stripped (the public mirror).

Each suite used to carry its own ``requests.post(...).iter_lines()`` loop, and they
had drifted: the conftest retried gateway 5xx before the response, but a read timeout
raised while CONSUMING the stream escaped it, and the session/streaming/metric suites
retried nothing at all. That is how one slow microVM failed tests as
``ConnectionError: Read timed out`` and one gateway blip as ``HTTPError: 504``.

Retry is whole-request: a failed attempt's partial frames are discarded and the POST
is re-issued, so callers never see a stitched stream. That is safe for what these
suites send — resolver queries and session actions. A re-issued streaming query may
persist its turn twice; the session tests assert ``>=`` turns, never an exact count.
4xx and application errors (carried in frames, not in the HTTP status) are never
retried — only the gateway or the connection failed, not the request.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator

import requests

RETRYABLE_STATUSES = frozenset({502, 503, 504})
MAX_ATTEMPTS = int(os.environ.get("INTEG_GATEWAY_MAX_ATTEMPTS", "4"))
BACKOFF_SECONDS = float(os.environ.get("INTEG_GATEWAY_BACKOFF_SECONDS", "5"))

# Raised mid-stream: requests wraps urllib3's ReadTimeoutError as ConnectionError from
# iter_lines(), and a connection dropped mid-chunk as ChunkedEncodingError.
_TRANSPORT_ERRORS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def runtime_session_id(label: str) -> str:
    """A runtime session id (>= 33 chars) unique to this pytest process.

    AgentCore pins a runtime session to the runtime version it first reached while the
    microVM stays warm, so an id reused across runs keeps answering from the PREVIOUS
    deploy (see the conftest's ``_RUNTIME_SESSION_ID``). Stable for the life of the
    process, so every call a test makes lands on the same warm VM.
    """
    return f"integ-test-{label}-{uuid.uuid4().hex}"


def iter_frames(response: requests.Response) -> Iterator[dict]:
    """Decode SSE ``data: {json}`` lines (or bare JSON lines) into dict frames."""
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        data = line[6:] if line.startswith("data: ") else line
        try:
            frame = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(frame, dict):
            yield frame


def post_sse(
    endpoint: str,
    headers: dict,
    payload: dict,
    *,
    is_terminal: Callable[[dict], bool],
    timeout: float,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_seconds: float = BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """POST ``payload`` and return its frames, up to and including the first terminal one.

    A stream that ends cleanly without a terminal frame returns what it sent; callers
    decide what that means. Raises ``requests.HTTPError`` for a non-retryable status or
    once retryable ones are exhausted, and the last transport error once those are.
    The response body is buffered before an HTTPError is raised, so ``e.response.text``
    stays readable.
    """
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(endpoint, headers=headers, json=payload, stream=True, timeout=timeout)
            try:
                if response.status_code >= 400:
                    _ = response.content
                response.raise_for_status()
                frames: list[dict] = []
                for frame in iter_frames(response):
                    frames.append(frame)
                    if is_terminal(frame):
                        break
                return frames
            finally:
                response.close()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status not in RETRYABLE_STATUSES or attempt == attempts:
                raise
            reason = f"HTTP {status}"
        except _TRANSPORT_ERRORS as e:
            if attempt == attempts:
                raise
            reason = f"{type(e).__name__}: {str(e)[:100]}"
        print(f"[integ transport] {reason} on attempt {attempt}/{attempts}, retrying in {backoff_seconds}s")
        sleep(backoff_seconds)
    raise AssertionError("unreachable: the final attempt returns or raises")
