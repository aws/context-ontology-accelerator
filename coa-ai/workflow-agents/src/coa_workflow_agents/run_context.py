# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-run context: the run deadline and the progress sink.

A run sets one ``RunContext`` in a ``ContextVar``; worker threads started with a
copy of the context (Strands does this for tools) read it through ``current_run``.
``poll_to_terminal`` clamps its timeout to ``deadline``, and ``report_progress``
forwards to ``progress``. Outside a run both are no-ops (``remaining_s`` is ``None``).
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

ProgressSink = Callable[[str], None]


@dataclass(frozen=True)
class RunContext:
    """A run's ``time.monotonic()`` deadline and thread-safe progress sink."""

    deadline: float
    progress: ProgressSink


_RUN: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("coa_workflow_run", default=None)


def current_run() -> RunContext | None:
    """The run context of the calling thread, or ``None`` outside a run."""
    return _RUN.get()


def set_run(run: RunContext | None) -> contextvars.Token[RunContext | None]:
    """Set the run context for the current context; returns the reset token."""
    return _RUN.set(run)


def reset_run(token: contextvars.Token[RunContext | None]) -> None:
    """Undo :func:`set_run`."""
    _RUN.reset(token)


def remaining_s() -> float | None:
    """Seconds left before the run deadline, or ``None`` when there is no run."""
    run = _RUN.get()
    return None if run is None else run.deadline - time.monotonic()


def report_progress(message: str) -> None:
    """Send ``message`` to the run's progress sink; a no-op outside a run."""
    run = _RUN.get()
    if run is not None:
        run.progress(message)


def make_progress_sink(
    loop: asyncio.AbstractEventLoop,
    send: Callable[[float, str], Coroutine[Any, Any, None]],
) -> ProgressSink:
    """A thread-safe sink scheduling ``send(counter, message)`` on ``loop``; never blocks or raises.

    The counter must increase (MCP), so increment and scheduling share one lock and sends
    arrive in order. A closed loop or failed send drops the message; the first drop on a
    closed loop logs ``progress_sink_loop_closed`` once.
    """
    counter = 0
    warned = False
    lock = threading.Lock()

    def dropped() -> None:
        nonlocal warned
        if not warned:
            warned = True
            logger.warning("progress_sink_loop_closed", sent=counter)

    def sink(message: str) -> None:
        nonlocal counter
        with lock:
            if loop.is_closed():
                dropped()
                return
            coro = send(float(counter + 1), message)
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                # The loop closed after the check; close the coroutine so it is not "never awaited".
                coro.close()
                dropped()
                return
            counter += 1
        future.add_done_callback(_log_send_failure)

    return sink


def _log_send_failure(future: Future[None]) -> None:
    """Log a failed progress send at debug level, without the message.

    Debug, not warning: a client that disconnects mid-run fails every later send.
    """
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
        logger.debug("progress_send_failed", error_type=type(exc).__name__)
