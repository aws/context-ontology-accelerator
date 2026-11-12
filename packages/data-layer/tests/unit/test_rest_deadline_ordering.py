# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The REST Context Manager call gives up before API Gateway's 29 s timeout."""

from __future__ import annotations

import http.client
import io
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest

# The handler module reads env vars at import time.
with patch.dict(
    "os.environ",
    {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/test",
        "AWS_REGION": "us-east-1",
        "ALLOWED_ORIGIN": "https://app.example.com",
        "ONTOLOGY_PROXY_LAMBDA_ARN": "arn:aws:lambda:us-east-1:123456789012:function:test-ontology-proxy",
    },
):
    from coa_data_layer import handler as handler_module
    from coa_data_layer.handler import handler

pytestmark = pytest.mark.unit


def _api_event(*, received_s_ago: float | None = None, authorizer_ms: int | None = None) -> dict:
    event = {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/query",
        "pathParameters": {"namespaceId": "22222222-2222-4222-8222-222222222222"},
        "body": json.dumps({"query": "hi"}),
        "headers": {"Authorization": "Bearer test-token"},
        "requestContext": {"authorizer": {"principalId": "user@example.com", "email": "user@example.com"}},
    }
    if received_s_ago is not None:
        event["requestContext"]["requestTimeEpoch"] = int((time.time() - received_s_ago) * 1000)
    if authorizer_ms is not None:
        event["requestContext"]["authorizer"]["integrationLatency"] = authorizer_ms
    return event


def _lambda_context(remaining_ms: int) -> SimpleNamespace:
    return SimpleNamespace(get_remaining_time_in_millis=lambda: remaining_ms)


# How far past the call's budget the handler may answer: scheduling slack on a loaded machine.
_DEADLINE_SLACK_S = 0.4


def _urlopen_answering(wait: threading.Event | None = None) -> MagicMock:
    chunks = [b"", json.dumps({"statusCode": 200, "result": {"answer": "42"}}).encode("utf-8")]

    def read(*_args):
        if wait is not None:
            wait.wait(5.0)
        return chunks.pop() if chunks else b""

    cm = MagicMock()
    cm.__enter__.return_value.read.side_effect = cm.__enter__.return_value.read1.side_effect = read
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def test_slow_context_manager_gets_rest_deadline_exceeded_within_budget():
    release = threading.Event()
    try:
        with patch.object(handler_module, "urllib_request") as mock_req:
            mock_req.urlopen = _urlopen_answering(release)
            start = time.monotonic()
            # 1.3 s of Lambda time left leaves the call 0.3 s.
            result = handler(_api_event(), _lambda_context(1_300))
            elapsed = time.monotonic() - start
    finally:
        release.set()

    assert result["statusCode"] == 504
    body = json.loads(result["body"])
    assert body["error"] == "rest_deadline_exceeded"
    assert body["limitMs"] == handler_module.REST_TIMEOUT_MS
    assert elapsed < 0.3 + _DEADLINE_SLACK_S


@pytest.mark.parametrize(
    ("received_s_ago", "context", "expected_s"),
    [
        (None, _lambda_context(30_000), 28.0),
        (None, _lambda_context(10_000), 9.0),
        (8.0, _lambda_context(30_000), 20.0),
        (-60.0, None, 28.0),
    ],
    ids=["unanchored", "lambda-time-left", "cold-start-counts", "future-epoch-capped"],
)
def test_context_manager_call_budget(received_s_ago, context, expected_s):
    """29 s from requestTimeEpoch (or handler entry) less 1 s, capped by the Lambda's time left less 1 s."""
    with patch.object(handler_module, "urllib_request") as mock_req:
        mock_req.urlopen = _urlopen_answering()
        handler(_api_event(received_s_ago=received_s_ago), context)

    assert expected_s - 0.5 < mock_req.urlopen.call_args.kwargs["timeout"] <= expected_s + 0.05


@pytest.mark.parametrize(
    ("authorizer_ms", "expected_s"),
    [(3_000, 23.0), (600_000, 28.0), (-5_000, 20.0), (0, 20.0), (True, 20.0), ("3000", 20.0)],
    ids=["authorizer-time-excluded", "never-past-handler-entry-budget", "negative", "zero", "bool", "string"],
)
def test_context_manager_call_budget_starts_after_the_authorizer(authorizer_ms, expected_s):
    """The gateway's 29 s integration timeout starts once the authorizer has returned; other latencies are ignored."""
    with patch.object(handler_module, "urllib_request") as mock_req:
        mock_req.urlopen = _urlopen_answering()
        handler(_api_event(received_s_ago=8.0, authorizer_ms=authorizer_ms), _lambda_context(30_000))

    assert expected_s - 0.5 < mock_req.urlopen.call_args.kwargs["timeout"] <= expected_s + 0.05


def test_gateway_budget_already_spent_returns_rest_deadline_exceeded_without_calling():
    with patch.object(handler_module, "urllib_request") as mock_req:
        result = handler(_api_event(received_s_ago=28.0), _lambda_context(30_000))

    mock_req.urlopen.assert_not_called()
    assert json.loads(result["body"])["error"] == "rest_deadline_exceeded"


def test_context_manager_http_error_still_forwarded_from_bounded_call():
    upstream = json.dumps({"error": "NoResultError", "message": "No result for the requested tier"}).encode("utf-8")
    err = HTTPError("http://cm", 404, "Not Found", {}, io.BytesIO(upstream))
    with patch.object(handler_module, "urllib_request") as mock_req:
        mock_req.urlopen = MagicMock(side_effect=err)
        result = handler(_api_event(), _lambda_context(30_000))

    assert result["statusCode"] == 404
    assert json.loads(result["body"]) == {"message": "No result for the requested tier", "error": "NoResultError"}


def test_http_error_arriving_after_the_call_was_given_up_is_closed():
    release = threading.Event()
    error_fp = MagicMock()

    def answer_late(*_args, **_kwargs):
        release.wait(5.0)
        raise HTTPError("http://cm", 500, "late", {}, error_fp)

    try:
        with patch.object(handler_module, "urllib_request") as mock_req:
            mock_req.urlopen = MagicMock(side_effect=answer_late)
            result = handler(_api_event(), _lambda_context(1_300))
            release.set()
            deadline = time.monotonic() + 2.0
            while not error_fp.close.called and time.monotonic() < deadline:
                time.sleep(0.01)
    finally:
        release.set()

    assert json.loads(result["body"])["error"] == "rest_deadline_exceeded"
    error_fp.close.assert_called()


def test_abandoned_call_over_a_real_socket_stops_while_the_response_trickles():
    """A response that trickles keep-alives: the given-up call ends at its next read, not at EOF."""
    client_sock, server_sock = socket.socketpair()
    stop = threading.Event()
    server_sock.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")

    def trickle() -> None:
        piece = b": keep-alive\n"
        try:
            while not stop.wait(0.1):
                server_sock.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        except OSError:
            pass

    threading.Thread(target=trickle, daemon=True).start()
    resp = http.client.HTTPResponse(client_sock)
    resp.begin()
    try:
        with patch.object(handler_module, "urllib_request") as mock_req:
            mock_req.urlopen = MagicMock(return_value=resp)
            result = handler(_api_event(), _lambda_context(1_500))
            assert json.loads(result["body"])["error"] == "rest_deadline_exceeded"
            deadline = time.monotonic() + 1.5
            while not resp.isclosed() and time.monotonic() < deadline:
                time.sleep(0.02)
        assert resp.isclosed()
    finally:
        stop.set()
        server_sock.close()
        client_sock.close()


def _urlopen_raising_http_error_over(client_sock: socket.socket) -> MagicMock:
    """``urlopen`` that, like urllib, raises ``HTTPError`` over the live response once its headers are read."""

    def open_url(_req, timeout):
        client_sock.settimeout(timeout)
        resp = http.client.HTTPResponse(client_sock)
        resp.begin()
        raise HTTPError("http://cm", resp.status, resp.reason, resp.headers, resp)

    return MagicMock(side_effect=open_url)


def _error_head(body_len: int) -> bytes:
    return (
        f"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\nContent-Length: {body_len}\r\n\r\n"
    ).encode()


@pytest.mark.parametrize("stalled", [False, True], ids=["slow-error-body", "stalled-error-body"])
def test_http_error_body_is_read_within_the_budget(stalled):
    """An error response whose body trickles or stalls still answers rest_deadline_exceeded within the budget."""
    body = json.dumps({"error": "InternalError", "message": "x" * 8}).encode()
    client_sock, server_sock = socket.socketpair()
    stop = threading.Event()
    server_sock.sendall(_error_head(len(body)))

    def trickle() -> None:
        try:
            for byte in body:
                if stop.wait(0.05):
                    return
                server_sock.sendall(bytes([byte]))
        except OSError:
            pass

    if not stalled:
        threading.Thread(target=trickle, daemon=True).start()
    try:
        with patch.object(handler_module, "urllib_request") as mock_req:
            mock_req.urlopen = _urlopen_raising_http_error_over(client_sock)
            start = time.monotonic()
            # 1.3 s of Lambda time left leaves the call 0.3 s.
            result = handler(_api_event(), _lambda_context(1_300))
            elapsed = time.monotonic() - start
    finally:
        stop.set()
        server_sock.close()
        client_sock.close()

    assert result["statusCode"] == 504
    assert json.loads(result["body"])["error"] == "rest_deadline_exceeded"
    assert elapsed < 0.3 + _DEADLINE_SLACK_S


def test_http_error_body_read_in_full_is_still_forwarded_over_a_real_socket():
    upstream = json.dumps({"error": "NoResultError", "message": "No result for the requested tier"}).encode()
    client_sock, server_sock = socket.socketpair()
    server_sock.sendall(_error_head(len(upstream)) + upstream)
    try:
        with patch.object(handler_module, "urllib_request") as mock_req:
            mock_req.urlopen = _urlopen_raising_http_error_over(client_sock)
            result = handler(_api_event(), _lambda_context(30_000))
    finally:
        server_sock.close()
        client_sock.close()

    assert result["statusCode"] == 500
    assert json.loads(result["body"]) == {"message": "No result for the requested tier", "error": "NoResultError"}
