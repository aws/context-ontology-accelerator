# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``poll_to_terminal``: one call, one outcome; plus ``read_job_id`` and ``ignore_stale_failure``."""

from __future__ import annotations

from unittest.mock import patch

import coa_workflow_agents.tools.polling as polling_module
import pytest
import requests
from coa_workflow_agents.clients.coa_api_client import CoaApiError
from coa_workflow_agents.tools.polling import (
    PollFailureError,
    PollFetchError,
    PollTimeoutError,
    _is_retryable_fetch_error,
    ignore_stale_failure,
    poll_to_terminal,
    read_job_id,
)
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit


def test_returns_immediately_when_first_fetch_is_terminal():
    calls = []

    def fetch():
        calls.append(1)
        return {"status": "APPROVED"}

    result = poll_to_terminal(
        "test",
        fetch,
        operation="test_op",
        is_terminal=lambda s: s["status"] == "APPROVED",
        is_failure=lambda s: s["status"] == "FAILED",
        timeout_s=5.0,
        interval_s=0.01,
    )

    assert result == {"status": "APPROVED"}
    assert len(calls) == 1


def test_polls_until_terminal():
    statuses = iter(["PENDING", "PENDING", "APPROVED"])

    def fetch():
        return {"status": next(statuses)}

    result = poll_to_terminal(
        "test",
        fetch,
        operation="test_op",
        is_terminal=lambda s: s["status"] == "APPROVED",
        is_failure=lambda s: s["status"] == "FAILED",
        timeout_s=5.0,
        interval_s=0.01,
    )

    assert result == {"status": "APPROVED"}


def test_raises_poll_failure_error_on_failure_state():
    def fetch():
        return {"status": "APPROVAL_FAILED"}

    with pytest.raises(PollFailureError) as exc_info:
        poll_to_terminal(
            "source abc approve",
            fetch,
            operation="test_op",
            is_terminal=lambda s: s["status"] == "APPROVED",
            is_failure=lambda s: s["status"] == "APPROVAL_FAILED",
            timeout_s=5.0,
            interval_s=0.01,
        )

    assert exc_info.value.last_seen == {"status": "APPROVAL_FAILED"}
    assert "source abc approve" in str(exc_info.value)


def test_raises_poll_timeout_error_when_never_terminal():
    def fetch():
        return {"status": "PENDING"}

    with pytest.raises(PollTimeoutError) as exc_info:
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: s["status"] == "APPROVED",
            is_failure=lambda s: s["status"] == "FAILED",
            timeout_s=0.05,
            interval_s=0.02,
        )

    assert exc_info.value.last_seen == {"status": "PENDING"}


def test_failure_check_wins_over_terminal_check_when_both_true():
    """is_failure must be checked before is_terminal (a state matching both
    is treated as a failure, never silently reported as success)."""

    def fetch():
        return {"status": "AMBIGUOUS"}

    with pytest.raises(PollFailureError):
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: s["status"] == "AMBIGUOUS",
            is_failure=lambda s: s["status"] == "AMBIGUOUS",
            timeout_s=5.0,
            interval_s=0.01,
        )


# ── Resilience: transient fetch failures vs. real terminal states ──────


class _FakeHttpError(Exception):
    """Stands in for CoaApiError — duck-typed on status_code, same as the real one."""

    def __init__(self, status_code: int | None):
        self.status_code = status_code
        super().__init__(f"fake HTTP error status={status_code}")


def test_recovers_from_a_few_transient_network_errors_then_succeeds():
    """A ConnectionError-like exception with no status_code (network down, not an
    HTTP response at all) must be retried, not treated as the job having failed."""
    attempts = {"n": 0}

    def fetch():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionError("simulated network blip")
        return {"status": "APPROVED"}

    result = poll_to_terminal(
        "test",
        fetch,
        operation="test_op",
        is_terminal=lambda s: s["status"] == "APPROVED",
        is_failure=lambda s: s["status"] == "FAILED",
        timeout_s=30.0,
        interval_s=0.01,
        _sleep=lambda _: None,
    )
    assert result == {"status": "APPROVED"}
    assert attempts["n"] == 3


def test_recovers_from_a_5xx_error_then_succeeds():
    attempts = {"n": 0}

    def fetch():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _FakeHttpError(status_code=503)
        return {"status": "APPROVED"}

    result = poll_to_terminal(
        "test",
        fetch,
        operation="test_op",
        is_terminal=lambda s: s["status"] == "APPROVED",
        is_failure=lambda s: s["status"] == "FAILED",
        timeout_s=30.0,
        interval_s=0.01,
        _sleep=lambda _: None,
    )
    assert result == {"status": "APPROVED"}


def test_a_4xx_error_is_not_retried_and_propagates_immediately():
    """A 4xx (bad namespaceId/sourceId) is a real, permanent error — waiting
    will not fix it, so it must propagate immediately, not be swallowed into
    a PollFetchError after burning the whole retry budget."""
    attempts = {"n": 0}

    def fetch():
        attempts["n"] += 1
        raise _FakeHttpError(status_code=404)

    with pytest.raises(_FakeHttpError):
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: s["status"] == "APPROVED",
            is_failure=lambda s: s["status"] == "FAILED",
            timeout_s=30.0,
            interval_s=0.01,
            _sleep=lambda _: None,
        )
    assert attempts["n"] == 1


def test_gives_up_with_poll_fetch_error_after_too_many_consecutive_transient_failures():
    def fetch():
        raise ConnectionError("still down")

    with pytest.raises(PollFetchError) as exc_info:
        poll_to_terminal(
            "test resource",
            fetch,
            operation="test_op",
            is_terminal=lambda s: s["status"] == "APPROVED",
            is_failure=lambda s: s["status"] == "FAILED",
            timeout_s=300.0,  # generous timeout — the fetch-failure cap should trip first
            interval_s=0.01,
            _sleep=lambda _: None,
        )
    assert "test resource" in str(exc_info.value)
    assert isinstance(exc_info.value.last_error, ConnectionError)


def test_a_single_success_resets_the_consecutive_failure_counter():
    """One good fetch between blips must reset the streak — this is what stops
    an intermittent (not sustained) network issue from ever hitting the cap."""
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        # Fail, succeed, fail, succeed, ... — never 10 in a row.
        if calls["n"] % 2 == 1:
            raise ConnectionError("intermittent blip")
        if calls["n"] >= 40:
            return {"status": "APPROVED"}
        return {"status": "PENDING"}

    result = poll_to_terminal(
        "test",
        fetch,
        operation="test_op",
        is_terminal=lambda s: s["status"] == "APPROVED",
        is_failure=lambda s: s["status"] == "FAILED",
        timeout_s=30.0,
        interval_s=0.001,
        _sleep=lambda _: None,
    )
    assert result == {"status": "APPROVED"}


def test_backoff_sleep_is_clamped_to_the_remaining_deadline():
    """A backoff near the 60 s cap never sleeps past what is left of ``timeout_s``."""
    sleeps: list[float] = []
    # Fake monotonic clock: starts at 0, advances only by what _sleep
    # records — so the deadline is approached realistically without an
    # actual wall-clock wait, and without the fetch-failure cap (10) tripping
    # before the deadline does (a real _sleep=lambda _: None no-op would
    # leave time.monotonic() frozen, so the 10-failure cap would win first
    # instead of exercising the deadline-clamping path this test targets).
    fake_now = {"t": 0.0}

    def fake_monotonic():
        return fake_now["t"]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        fake_now["t"] += seconds

    def fetch():
        raise ConnectionError("still down")

    # timeout_s is short enough that the deadline is hit on the FIRST
    # backoff, well before _BACKOFF_MAX_S (60s) or the fetch-failure cap.
    with (
        patch.object(polling_module.time, "monotonic", fake_monotonic),
        pytest.raises(PollTimeoutError),
    ):
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: False,
            is_failure=lambda s: False,
            timeout_s=0.01,
            interval_s=0.01,
            _sleep=fake_sleep,
        )

    assert sleeps, "expected at least one backoff sleep before the deadline/timeout check"
    assert all(s <= 0.01 + 1e-9 for s in sleeps), f"backoff sleep(s) overshot the remaining deadline: {sleeps}"


def test_a_raising_is_failure_predicate_is_treated_as_a_permanent_failure_not_retried():
    """A raising predicate is a ``PollFailureError``, raised once, not retried."""
    attempts = {"n": 0}

    def fetch():
        attempts["n"] += 1
        return {"status": "weird"}  # not a dict shape the bad predicate expects

    def bad_is_failure(state):
        raise TypeError("predicate bug: expected a list, got a dict")

    with pytest.raises(PollFailureError, match="predicate TypeError"):
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: False,
            is_failure=bad_is_failure,
            timeout_s=5.0,
            interval_s=0.01,
            _sleep=lambda _: None,
        )
    assert attempts["n"] == 1  # not retried


def test_a_raising_is_terminal_predicate_is_treated_as_a_permanent_failure_not_retried():
    def fetch():
        return {"status": "weird"}

    def bad_is_terminal(state):
        raise TypeError("predicate bug")

    with pytest.raises(PollFailureError, match="predicate TypeError"):
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=bad_is_terminal,
            is_failure=lambda s: False,
            timeout_s=5.0,
            interval_s=0.01,
            _sleep=lambda _: None,
        )


def test_a_non_int_status_code_is_not_retried_and_is_logged():
    """A status_code attribute present but not an int is an unexpected shape:
    no TypeError out of the comparison, a warning, and no retry."""

    class _WeirdError(Exception):
        status_code = "500"  # string, not int — an unexpected shape

    with capture_logs() as logs:
        assert _is_retryable_fetch_error(_WeirdError("boom")) is False
    assert [e["event"] for e in logs] == ["poll_fetch_error_has_non_int_status_code"]


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (requests.exceptions.ConnectionError("down"), True),
        (requests.exceptions.ReadTimeout("slow"), True),
        (requests.exceptions.ChunkedEncodingError("cut off"), True),
        (ConnectionError("down"), True),
        (TimeoutError("slow"), True),
        (requests.exceptions.SSLError("bad certificate"), False),
        (KeyError("jobId"), False),
        (TypeError("NoneType is not subscriptable"), False),
        (RuntimeError("unexpected"), False),
    ],
)
def test_is_retryable_fetch_error_retries_only_network_failures_without_a_status(exc, retryable):
    assert _is_retryable_fetch_error(exc) is retryable


def test_a_programming_error_in_fetch_is_raised_at_once_not_retried():
    calls = []

    def fetch():
        calls.append(1)
        raise KeyError("status")

    with capture_logs() as logs, pytest.raises(KeyError):
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: True,
            is_failure=lambda s: False,
            timeout_s=5.0,
            interval_s=0.01,
            _sleep=lambda _: None,
        )
    assert len(calls) == 1
    assert not [e for e in logs if e["event"] == "poll_fetch_transient_failure"]


# ── read_job_id ──────────────────────────────────────────────────────────


def test_read_job_id_prefers_snake_case_job_id_over_fallbacks():
    assert read_job_id({"job_id": "J1", "jobId": "X"}, "jobId") == "J1"


def test_read_job_id_falls_back_in_order_when_snake_case_absent():
    assert read_job_id({"jobId": "J2"}, "jobId", "validationJobId") == "J2"
    assert read_job_id({"validationJobId": "J3"}, "jobId", "validationJobId") == "J3"


def test_read_job_id_treats_an_empty_string_id_as_absent():
    # a present-but-empty id is malformed, not a valid poll target
    assert read_job_id({"job_id": ""}, "jobId") is None
    # and the empty snake_case value falls through to a truthy fallback
    assert read_job_id({"job_id": "", "jobId": "J4"}, "jobId") == "J4"


def test_read_job_id_returns_none_when_no_key_present():
    assert read_job_id({}, "jobId", "validationJobId") is None


def test_read_job_id_coerces_a_non_string_id_to_str():
    assert read_job_id({"job_id": 12345}) == "12345"


# ── ignore_stale_failure ─────────────────────────────────────────────────


def _failed(state: dict) -> bool:
    return state.get("status") == "FAILED"


def test_ignore_stale_failure_is_a_pass_through_when_not_started_in_failure():

    assert ignore_stale_failure(_failed, started_in_failure=False) is _failed


def test_ignore_stale_failure_skips_the_failure_until_the_state_changes_or_grace_passes():

    now = [0.0]
    check = ignore_stale_failure(_failed, started_in_failure=True, grace_s=10.0, _clock=lambda: now[0])

    assert check({"status": "FAILED"}) is False  # stale read inside the grace
    now[0] = 9.9
    assert check({"status": "FAILED"}) is False
    now[0] = 10.0
    assert check({"status": "FAILED"}) is True  # grace over: a real failure


def test_ignore_stale_failure_reports_a_failure_at_once_after_another_state_was_seen():

    check = ignore_stale_failure(_failed, started_in_failure=True, grace_s=10.0, _clock=lambda: 0.0)

    assert check({"status": "RUNNING"}) is False
    assert check({"status": "FAILED"}) is True


def test_timeout_logs_poll_timeout_event():
    """The alarm metric filter matches ``$.event = "poll_timeout"``."""
    with capture_logs() as logs, pytest.raises(PollTimeoutError):
        poll_to_terminal(
            "source s1 approve",
            lambda: {"status": "PENDING"},
            operation="test_op",
            is_terminal=lambda s: False,
            is_failure=lambda s: False,
            timeout_s=0.0,
            interval_s=0.0,
        )
    timeouts = [entry for entry in logs if entry["event"] == "poll_timeout"]
    assert timeouts == [{"event": "poll_timeout", "operation": "test_op", "timeout_s": 0.0, "log_level": "warning"}]


def test_poll_to_terminal_transient_coa_api_error_logs_class_status_and_route_only():
    body_sentinel = "BODY-SENTINEL-6f1c"
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise CoaApiError("GET", "/namespaces/n1/metrics/churn-rate", 503, f'{{"message":"{body_sentinel}"}}')
        return {"status": "APPROVED"}

    with capture_logs() as logs:
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: s["status"] == "APPROVED",
            is_failure=lambda s: s["status"] == "FAILED",
            timeout_s=30.0,
            interval_s=0.01,
            _sleep=lambda _: None,
        )
    event = next(e for e in logs if e["event"] == "poll_fetch_transient_failure")
    assert event["operation"] == "test_op"
    assert event["error_type"] == "CoaApiError"
    assert event["status_code"] == 503
    assert event["route"] == "/namespaces/{id}/metrics/{id}"
    assert "error" not in event
    assert body_sentinel not in repr(logs)
    assert "churn-rate" not in repr(logs)


def test_poll_to_terminal_transient_non_api_error_logs_class_only():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("host db.internal refused churn-rate")
        return {"status": "APPROVED"}

    with capture_logs() as logs:
        poll_to_terminal(
            "test",
            fetch,
            operation="test_op",
            is_terminal=lambda s: s["status"] == "APPROVED",
            is_failure=lambda s: s["status"] == "FAILED",
            timeout_s=30.0,
            interval_s=0.01,
            _sleep=lambda _: None,
        )
    event = next(e for e in logs if e["event"] == "poll_fetch_transient_failure")
    assert event["operation"] == "test_op"
    assert event["error_type"] == "ConnectionError"
    assert event["status_code"] is None
    assert event["route"] is None
    assert "churn-rate" not in repr(logs)
