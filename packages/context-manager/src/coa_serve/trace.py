# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Processing trace collection — passed through the pipeline.

Supports an optional async callback (``on_record``) that fires for each
recorded step, enabling progressive SSE emission of trace events.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence

import structlog

from .models import TraceStep

logger = structlog.get_logger(__name__)

OnRecordCallback = Callable[[TraceStep], Awaitable[None]]


class TraceCollector:
    """Collects TraceStep entries as the pipeline progresses.

    Args:
        on_record: Optional async callback invoked for each recorded step.
            Used by the SSE emitter to emit progressive ``step`` events.
            Callbacks are scheduled as tasks and can be awaited via ``flush()``
            to guarantee delivery before emitting subsequent events (e.g. done).
    """

    def __init__(self, on_record: OnRecordCallback | None = None) -> None:
        """Initialize an empty collector and start the wall-clock timer.

        Args:
            on_record: Optional async callback invoked for each recorded step,
                used by the SSE emitter for progressive delivery.
        """
        self._steps: list[TraceStep] = []
        self._start_time = time.perf_counter()
        self._on_record = on_record
        self._pending_tasks: list[asyncio.Task] = []

    def record(
        self,
        step: str,
        status: str,
        duration_ms: int,
        detail: str | dict | None = None,
        tool_used: str | None = None,
        parallel_group: str | None = None,
        wall_ms: int | None = None,
        graph_seed_mode: str | None = None,
    ) -> None:
        """Append a trace step and fire the on_record callback if configured.

        Args:
            step: Step identifier (see StepId).
            status: Step outcome (e.g. ``ok``, ``error``, ``skipped``).
            duration_ms: Step duration in milliseconds.
            detail: Optional human-readable detail or structured payload.
            tool_used: Optional name of the upstream tool exercised by the step.
            parallel_group: Optional group label for concurrent child steps.
            wall_ms: Optional wall-clock elapsed time for a parallel group.
            graph_seed_mode: Optional label for how the graph_traverse step
                obtained its seeds ('uri_hop' or 'keyword').
        """
        ts = TraceStep(
            step=step,
            status=status,
            duration_ms=duration_ms,
            detail=detail,
            tool_used=tool_used,
            parallel_group=parallel_group,
            wall_ms=wall_ms,
            graph_seed_mode=graph_seed_mode,
        )
        self._steps.append(ts)
        self._fire_callback(ts)

    def add_dicts(self, steps: Sequence[dict]) -> None:
        """Ingest trace steps from downstream modules that return dicts or tuples of dicts."""
        for s in steps:
            step = s.get("step", "unknown")
            status = s.get("status", "unknown")
            if not step or not status:
                continue
            ts = TraceStep(
                step=step,
                status=status,
                duration_ms=s.get("durationMs", 0),
                detail=s.get("detail"),
                tool_used=s.get("toolUsed"),
                parallel_group=s.get("parallelGroup"),
                wall_ms=s.get("wallMs"),
                graph_seed_mode=s.get("graphSeedMode"),
            )
            self._steps.append(ts)
            self._fire_callback(ts)

    async def flush(self) -> None:
        """Await all pending on_record callback tasks.

        Call this before emitting a terminal event (e.g. done) to guarantee
        all step events have been sent to the client.
        """
        if not self._pending_tasks:
            return
        results = await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        self._pending_tasks.clear()
        for r in results:
            if isinstance(r, Exception):
                logger.warning("trace_emit_failed", error=type(r).__name__, message=str(r))

    def discard(self) -> None:
        """Cancel pending on_record callback tasks without delivering them.

        Use on error paths where the trace steps recorded so far MUST NOT
        reach the client — chiefly, authorization denials (``AccessDeniedError``)
        where a T1_FIREWALL step's detail names the restricted tables in
        the deny reason. Calling ``flush()`` on that path emits those step
        events to the SSE stream before the 403 error event and lets any
        authenticated caller enumerate restricted table names.

        Best-effort: a task already past ``create_task`` scheduling and
        actually executing may still complete. Under a single-threaded
        event loop, tasks scheduled via ``loop.create_task`` do not run
        until the current coroutine yields, so the common case (record
        step → raise AccessDeniedError → discard) cancels before delivery.
        (See the trace-flush-on-denial hardening in ``main.py::_run_resolve``.)
        """
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()
        self._pending_tasks.clear()

    def _fire_callback(self, ts: TraceStep) -> None:
        """Fire the on_record callback as a tracked background task."""
        if not self._on_record:
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._safe_callback(ts))
            self._pending_tasks.append(task)
        except RuntimeError:
            # No running event loop — skip (e.g., called from sync test context)
            pass

    async def _safe_callback(self, ts: TraceStep) -> None:
        """Execute callback with exception safety."""
        try:
            await self._on_record(ts)  # type: ignore[misc]
        except Exception:
            logger.debug("trace_on_record_callback_failed", step=ts.step)

    @property
    def steps(self) -> list[TraceStep]:
        """Return a copy of the recorded steps (untruncated, for logging)."""
        return list(self._steps)

    @property
    def steps_serializable(self) -> list[dict]:
        """Return trace steps with detail values truncated for client responses.

        Use this when serializing traces into API responses. Logger calls
        should use ``.steps`` (untruncated) for CloudWatch.
        Truncation is handled by TraceStep's field_serializer (triggered by model_dump).
        """
        return [s.model_dump(by_alias=True, exclude_none=True) for s in self._steps]

    @property
    def total_ms(self) -> int:
        """Return elapsed milliseconds since the collector was created."""
        return int((time.perf_counter() - self._start_time) * 1000)
