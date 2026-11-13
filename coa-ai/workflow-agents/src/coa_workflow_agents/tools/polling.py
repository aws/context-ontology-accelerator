# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Poll an async COA operation to a terminal state.

A tool calls ``poll_to_terminal`` once and gets one outcome: the success state,
``PollFailureError``, ``PollTimeoutError`` (or ``RunDeadlineReachedError`` when the
run deadline cut it short) or ``PollFetchError``. A transient failure of the poll
itself (network, 5xx) is retried with backoff, since the job may be fine; a 4xx
is raised at once.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import requests
import structlog

from coa_workflow_agents.clients.coa_api_client import CoaApiError, sanitized_route
from coa_workflow_agents.run_context import remaining_s, report_progress

logger = structlog.get_logger(__name__)

# Network failures a poll waits out. SSLError is a ConnectionError but permanent,
# so it is excluded where this is checked.
_TRANSIENT_FETCH_ERRORS: tuple[type[Exception], ...] = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
)


def read_job_id(response: dict[str, Any], *fallback_keys: str) -> str | None:
    """The first non-empty of ``job_id`` then ``fallback_keys``, or ``None``.

    ``job_id`` comes first: the induction and proposal endpoints return it snake_case
    although the Smithy model says camelCase.
    """
    for key in ("job_id", *fallback_keys):
        value = response.get(key)
        if value:
            return str(value)
    return None


# Consecutive transient fetch failures tolerated before ``PollFetchError``.
_MAX_CONSECUTIVE_FETCH_FAILURES = 10
_BACKOFF_BASE_S = 2.0
_BACKOFF_MAX_S = 60.0

# How long before the run deadline a poll gives up, leaving time to report it.
RUN_DEADLINE_MARGIN_S = 60.0


class PollTimeoutError(RuntimeError):
    """Raised when a resource never reached a terminal state in time."""

    def __init__(self, description: str, timeout_s: float, last_seen: dict[str, Any]) -> None:
        """Store the timed-out description, timeout, and last-observed state."""
        self.description = description
        self.last_seen = last_seen
        super().__init__(f"{description}: timed out after {timeout_s:.0f}s, last seen: {last_seen}")


class RunDeadlineReachedError(PollTimeoutError):
    """The run deadline, not the tool's own timeout, ended the poll; the operation still runs."""


class PollFailureError(RuntimeError):
    """Raised when a resource reached a known failure state."""

    def __init__(self, description: str, last_seen: dict[str, Any]) -> None:
        """Store the failure description and last-observed state."""
        self.last_seen = last_seen
        super().__init__(f"{description}: reached a failure state: {last_seen}")


class PollFetchError(RuntimeError):
    """``fetch`` kept failing transiently; the operation's own status is unknown."""

    def __init__(self, description: str, attempts: int, last_error: Exception) -> None:
        """Store the description, attempt count, and last underlying error."""
        self.last_error = last_error
        super().__init__(
            f"{description}: fetch failed {attempts} consecutive times, giving up "
            f"(last error: {type(last_error).__name__}: {last_error}). The underlying "
            "operation's actual status is unknown — it may still be running."
        )


def _is_retryable_fetch_error(exc: Exception) -> bool:
    """True for an int ``status_code >= 500`` or a network error (not SSL).

    A 4xx, an SSL error, a non-int ``status_code`` and any other exception (a bug in
    ``fetch``) are not retried.
    """
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return status_code >= 500
    if status_code is not None:
        logger.warning(
            "poll_fetch_error_has_non_int_status_code",
            exception_type=type(exc).__name__,
            status_code_type=type(status_code).__name__,
        )
        return False
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    return isinstance(exc, _TRANSIENT_FETCH_ERRORS)


def _poll_timeout(
    description: str, operation: str, timeout_s: float, last: dict[str, Any], *, run_deadline: bool = False
) -> PollTimeoutError:
    """Log ``poll_timeout`` (or ``run_deadline_reached``) and build the error.

    The log names ``operation``; ``description`` can carry ids, so it goes in the error only.
    """
    if run_deadline:
        logger.warning("run_deadline_reached", operation=operation, timeout_s=timeout_s)
        return RunDeadlineReachedError(description, timeout_s, last)
    logger.warning("poll_timeout", operation=operation, timeout_s=timeout_s)
    return PollTimeoutError(description, timeout_s, last)


def _effective_timeout(timeout_s: float) -> tuple[float, bool]:
    """``(timeout, set_by_run_deadline)``: ``timeout_s`` clamped to the run deadline minus the margin."""
    left = remaining_s()
    if left is None:
        return timeout_s, False
    run_bound = max(0.0, left - RUN_DEADLINE_MARGIN_S)
    if run_bound < timeout_s:
        return run_bound, True
    return timeout_s, False


def poll_to_terminal[T: dict[str, Any]](
    description: str,
    fetch: Callable[[], T],
    *,
    is_terminal: Callable[[T], bool],
    is_failure: Callable[[T], bool],
    timeout_s: float,
    operation: str,
    interval_s: float = 5.0,
    _sleep: Callable[[float], None] | None = None,
) -> T:
    """Poll ``fetch`` until ``is_terminal`` or ``is_failure``; return the terminal state.

    ``description`` labels progress and errors and may carry ids, so it is never logged;
    ``operation`` is the literal logged instead. ``timeout_s`` is wall-clock, retries
    included, and is clamped to the run deadline. ``fetch`` must release anything it
    opens. ``_sleep`` is a test hook. A raising predicate is a ``PollFailureError``; a
    non-retryable ``fetch`` error is re-raised as-is.
    """
    if _sleep is None:
        _sleep = time.sleep
    timeout_s, run_bound = _effective_timeout(timeout_s)
    deadline = time.monotonic() + timeout_s
    last: T = {}  # type: ignore[assignment]
    consecutive_fetch_failures = 0
    while True:
        try:
            last = fetch()
            consecutive_fetch_failures = 0
        except Exception as e:
            if not _is_retryable_fetch_error(e):
                raise
            consecutive_fetch_failures += 1
            # Never the message: it carries the raw path and part of the response body.
            api_error = e if isinstance(e, CoaApiError) else None
            logger.warning(
                "poll_fetch_transient_failure",
                operation=operation,
                attempt=consecutive_fetch_failures,
                error_type=type(e).__name__,
                status_code=api_error.status_code if api_error else None,
                route=sanitized_route(api_error.path) if api_error else None,
            )
            if consecutive_fetch_failures >= _MAX_CONSECUTIVE_FETCH_FAILURES:
                raise PollFetchError(description, consecutive_fetch_failures, e) from e
            if time.monotonic() >= deadline:
                raise _poll_timeout(description, operation, timeout_s, last, run_deadline=run_bound) from e
            backoff = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * (2**consecutive_fetch_failures))
            backoff = backoff * (0.5 + random.random() / 2)  # noqa: S311 — jitter, not security-sensitive
            # Never sleep past the budget.
            remaining = max(0.0, deadline - time.monotonic())
            _sleep(min(backoff, remaining))
            continue

        try:
            failed = is_failure(last)
            terminal = not failed and is_terminal(last)
        except Exception as e:
            # Not retried: a predicate that raises on this state will keep raising.
            raise PollFailureError(f"{description} (predicate {type(e).__name__}: {e})", last) from e
        if failed:
            raise PollFailureError(description, last)
        if terminal:
            return last
        report_progress(f"{description}: {last.get('status', 'in progress')}")
        if time.monotonic() >= deadline:
            raise _poll_timeout(description, operation, timeout_s, last, run_deadline=run_bound)
        _sleep(interval_s)


# How long a retry that starts in its own failure state ignores that state: COA's
# status reads are eventually consistent, so the first poll can still show it.
STALE_FAILURE_GRACE_S = 10.0


def ignore_stale_failure[T: dict[str, Any]](
    is_failure: Callable[[T], bool],
    *,
    started_in_failure: bool,
    grace_s: float | None = None,
    _clock: Callable[[], float] | None = None,
) -> Callable[[T], bool]:
    """Wrap ``is_failure`` so a stale read of the old failure does not count.

    With ``started_in_failure``, a failure counts only after a non-failure state was seen
    or ``grace_s`` (default ``STALE_FAILURE_GRACE_S``) passed. Build it right before polling.
    """
    if not started_in_failure:
        return is_failure
    grace = STALE_FAILURE_GRACE_S if grace_s is None else grace_s
    clock = time.monotonic if _clock is None else _clock
    started_at = clock()
    left_failure = False

    def check(state: T) -> bool:
        nonlocal left_failure
        if not is_failure(state):
            left_failure = True
            return False
        return left_failure or clock() - started_at >= grace

    return check
