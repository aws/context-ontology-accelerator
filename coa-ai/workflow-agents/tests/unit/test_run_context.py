# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``run_context``: the per-run deadline and progress sink, and how ``poll_to_terminal`` uses them."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from coa_workflow_agents.run_context import (
    RunContext,
    current_run,
    make_progress_sink,
    remaining_s,
    report_progress,
    reset_run,
    set_run,
)
from coa_workflow_agents.tools import polling
from coa_workflow_agents.tools.polling import PollTimeoutError, RunDeadlineReachedError, poll_to_terminal
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit


class _Run:
    """Set a RunContext for the duration of a ``with`` block, collecting progress messages."""

    def __init__(self, remaining: float) -> None:
        self.messages: list[str] = []
        self._remaining = remaining

    def __enter__(self) -> _Run:
        self._token = set_run(RunContext(deadline=time.monotonic() + self._remaining, progress=self.messages.append))
        return self

    def __exit__(self, *exc: object) -> None:
        reset_run(self._token)


# --- run_context -------------------------------------------------------------


def test_report_progress_without_a_run_is_a_noop():
    assert current_run() is None
    assert remaining_s() is None
    report_progress("nothing listens")  # must not raise


def test_report_progress_inside_a_run_reaches_the_sink():
    with _Run(100.0) as run:
        report_progress("hello")
        left = remaining_s()
    assert run.messages == ["hello"]
    assert left is not None and 99.0 < left <= 100.0
    assert current_run() is None


def test_progress_sink_schedules_send_on_the_loop_from_a_worker_thread():
    loop = asyncio.new_event_loop()
    sent: list[tuple[float, str]] = []
    done = threading.Event()

    async def send(progress: float, message: str) -> None:
        sent.append((progress, message))
        if len(sent) == 2:
            done.set()

    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    try:
        sink = make_progress_sink(loop, send)
        worker = threading.Thread(target=lambda: (sink("one"), sink("two")))
        worker.start()
        worker.join(timeout=2)
        assert done.wait(timeout=2)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()
    # The progress counter increases with each message, as MCP requires.
    assert sent == [(1.0, "one"), (2.0, "two")]


def test_progress_sink_values_stay_strictly_increasing_across_concurrent_threads():
    loop = asyncio.new_event_loop()
    sent: list[float] = []
    threads, per_thread = 8, 500
    done = threading.Event()

    async def send(progress: float, message: str) -> None:
        sent.append(progress)
        if len(sent) == threads * per_thread:
            done.set()

    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    # Switch threads as often as possible so an unlocked increment-then-schedule interleaves.
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        sink = make_progress_sink(loop, send)
        workers = [threading.Thread(target=lambda: [sink("tick") for _ in range(per_thread)]) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        assert done.wait(timeout=10)
    finally:
        sys.setswitchinterval(interval)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()
    assert sent == [float(n) for n in range(1, threads * per_thread + 1)]


def test_progress_sink_drops_messages_once_the_loop_is_closed():
    loop = asyncio.new_event_loop()
    loop.close()
    send = AsyncMock()
    sink = make_progress_sink(loop, send)
    with capture_logs() as logs:
        sink("too late")  # must not raise
        sink("later still")
    send.assert_not_called()
    # Logged once, not per dropped message, and without the messages.
    assert [e["event"] for e in logs] == ["progress_sink_loop_closed"]
    assert "too late" not in json.dumps(logs)


def test_progress_sink_closes_the_send_when_the_loop_closes_mid_schedule():
    """The loop can close between the sink's check and the schedule; the unsent coroutine is closed, not leaked."""

    async def real_send(progress: float, message: str) -> None:
        return None

    created: list[Any] = []

    def send(progress: float, message: str) -> Any:
        created.append(real_send(progress, message))
        return created[-1]

    loop = asyncio.new_event_loop()
    try:
        with patch.object(asyncio, "run_coroutine_threadsafe", side_effect=RuntimeError("Event loop is closed")):
            make_progress_sink(loop, send)("too late")  # must not raise
    finally:
        loop.close()
    assert [inspect.getcoroutinestate(coro) for coro in created] == [inspect.CORO_CLOSED]


def test_progress_sink_logs_a_failing_send_without_raising():
    async def send(progress: float, message: str) -> None:
        raise RuntimeError("client went away")

    async def main() -> None:
        sink = make_progress_sink(asyncio.get_running_loop(), send)
        await asyncio.to_thread(sink, "secret detail")
        await asyncio.sleep(0.05)

    with capture_logs() as logs:
        asyncio.run(main())  # never raised
    failures = [entry for entry in logs if entry["event"] == "progress_send_failed"]
    assert failures == [{"event": "progress_send_failed", "error_type": "RuntimeError", "log_level": "debug"}]
    assert "secret detail" not in json.dumps(logs)
    assert "client went away" not in json.dumps(logs)


# --- poll_to_terminal --------------------------------------------------------


def test_poll_tick_reports_progress_with_description_and_status():
    states = iter([{"status": "RUNNING"}, {"status": "DONE"}])
    with _Run(3600.0) as run:
        result = poll_to_terminal(
            "induction job j-1",
            lambda: next(states),
            operation="test_op",
            is_terminal=lambda s: s["status"] == "DONE",
            is_failure=lambda s: False,
            timeout_s=60,
            _sleep=lambda _: None,
        )
    assert result == {"status": "DONE"}
    assert run.messages == ["induction job j-1: RUNNING"]


def test_run_deadline_clamps_the_poll_and_raises_run_deadline_reached():
    # 60.05 s left before the run deadline and a 60 s margin: the poll may run
    # for ~0.05 s although the tool asked for 5 s. 5 s (not an hour) so a
    # broken clamp ends quickly as a plain PollTimeoutError and fails here.
    with _Run(polling.RUN_DEADLINE_MARGIN_S + 0.05), capture_logs() as logs:
        started = time.monotonic()
        with pytest.raises(RunDeadlineReachedError) as excinfo:
            poll_to_terminal(
                "induction job j-1",
                lambda: {"status": "RUNNING"},
                operation="test_op",
                is_terminal=lambda s: False,
                is_failure=lambda s: False,
                timeout_s=5,
                interval_s=0.01,
            )
    assert time.monotonic() - started < 2
    assert isinstance(excinfo.value, PollTimeoutError)
    assert excinfo.value.description == "induction job j-1"
    # The designed long-run outcome: its own event, not the alarmed poll_timeout.
    assert [entry["event"] for entry in logs if entry["event"] in ("poll_timeout", "run_deadline_reached")] == [
        "run_deadline_reached"
    ]
    (reached,) = [entry for entry in logs if entry["event"] == "run_deadline_reached"]
    assert reached["operation"] == "test_op"


def test_tool_timeout_shorter_than_the_run_deadline_stays_a_plain_poll_timeout():
    with _Run(3600.0), capture_logs() as logs, pytest.raises(PollTimeoutError) as excinfo:
        poll_to_terminal(
            "source s-1 approve",
            lambda: {"status": "APPROVING"},
            operation="test_op",
            is_terminal=lambda s: False,
            is_failure=lambda s: False,
            timeout_s=0.05,
            interval_s=0.01,
        )
    assert not isinstance(excinfo.value, RunDeadlineReachedError)
    assert [entry["event"] for entry in logs if entry["event"] in ("poll_timeout", "run_deadline_reached")] == [
        "poll_timeout"
    ]
