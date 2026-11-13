# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Aggregate budget and clock manager for the Agentic Retriever.

A single :class:`BudgetClock` is created per reasoning session and shared across
every Sub_Question and Exploration_Branch so that the wall-clock Time_Budget, the
maximum number of Reasoning_Steps, and the fan-out cap are all enforced **in
aggregate** for the whole request rather than per-branch.

The clock is driven by an **injectable** monotonic time source (a callable
returning seconds, defaulting to :func:`time.monotonic`). Injecting the clock lets
tests drive time deterministically without sleeping.

Import discipline: this module is dependency-free and never
imports ``graphrag_toolkit``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

# Smallest timeout ``tool_deadline_s`` ever hands to ``asyncio.wait_for``. Kept
# tiny (not a full second) so an explicitly-small per-tool cap in tests/config is
# honored, and so a budget-exhausted session times a tool out effectively at once
# rather than granting it a spurious extra second.
_MIN_TOOL_TIMEOUT_S = 0.001


@dataclass(frozen=True)
class AgenticBudgetConfig:
    """The four budgets that bound a single Agentic Retriever reasoning session.

    This is a plain value object; range validation and env-var parsing live in
    the service configuration loader. Here the values are simply held
    and read by the :class:`BudgetClock`. The defaults match the documented
    deployment defaults. Config load enforces the ranges: ``time_budget_s`` 1-300,
    ``max_steps`` 1-50, ``per_tool_timeout_s`` 1-120, ``max_fanout`` 1-20.
    ``synthesis_reserve_s`` is the wall-clock slice reserved at the END of the
    session for the final synthesis call; tool invocations are clamped so they
    cannot consume it (see :meth:`BudgetClock.tool_deadline_s`).
    """

    time_budget_s: float = 30.0
    max_steps: int = 10
    per_tool_timeout_s: float = 30.0
    max_fanout: int = 5
    synthesis_reserve_s: float = 8.0


class BudgetClock:
    """Aggregate wall-clock + step + fan-out enforcement for one session.

    A single instance is shared across all Sub_Questions and Exploration_Branches
    of a request, so the step counter and fan-out counter are **global** to the
    session — not per-branch — which is what makes the caps aggregate.

    Time is read from an injectable monotonic source so tests can advance time
    deterministically. Call :meth:`started` once at the beginning of the session
    to anchor the wall clock before consuming the budget.
    """

    def __init__(
        self,
        config: AgenticBudgetConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the budget config and (injectable) monotonic clock."""
        self._config = config
        self._now = clock
        self._start: float | None = None
        self._steps: int = 0
        self._branches: int = 0

    def started(self) -> None:
        """Anchor the wall clock at the current monotonic time.

        Idempotent in effect for callers that re-anchor, but normally called
        exactly once at session start before any budget is consumed.
        """
        self._start = self._now()

    def _elapsed_s(self) -> float:
        """Seconds elapsed since :meth:`started`; 0.0 if not yet started."""
        if self._start is None:
            return 0.0
        return self._now() - self._start

    def remaining_s(self) -> float:
        """Seconds remaining in the Time_Budget, clamped at 0.0 (never negative)."""
        return max(0.0, self._config.time_budget_s - self._elapsed_s())

    def tool_deadline_s(self, per_tool_timeout_s: float) -> float:
        """Effective timeout for the next tool invocation (deadline-aware).

        Clamps the caller's per-tool cap to the wall-clock time left minus the
        synthesis reserve, floored at ``_MIN_TOOL_TIMEOUT_S`` so ``asyncio.wait_for``
        always receives a positive timeout.
        """
        usable = self.remaining_s() - self._config.synthesis_reserve_s
        return max(_MIN_TOOL_TIMEOUT_S, min(per_tool_timeout_s, usable))

    def expired(self) -> bool:
        """True once the elapsed time reaches the Time_Budget boundary.

        Flips to True exactly when ``elapsed >= time_budget_s``.
        """
        return self._elapsed_s() >= self._config.time_budget_s

    def elapsed_ms(self) -> int:
        """Whole milliseconds elapsed since :meth:`started` (for trace steps)."""
        return int(self._elapsed_s() * 1000)

    def try_consume_step(self) -> bool:
        """Reserve one Reasoning_Step against the aggregate step cap.

        Returns True and increments the shared step counter while fewer than
        ``max_steps`` steps have been consumed; returns False once the cap is
        reached without ever exceeding it.
        """
        if self._steps >= self._config.max_steps:
            return False
        self._steps += 1
        return True

    def refund_step(self) -> None:
        """Return one consumed Reasoning_Step (a step that did no work, e.g. a repeated read)."""
        if self._steps > 0:
            self._steps -= 1

    def try_open_branch(self) -> bool:
        """Reserve one Sub_Question/Exploration_Branch against the fan-out cap.

        Returns True and increments the shared fan-out counter while fewer than
        ``max_fanout`` branches have been opened; returns False once the cap is
        reached without ever exceeding it. The counter is global
        to the session, so Sub_Questions and Exploration_Branches share one
        aggregate budget.
        """
        if self._branches >= self._config.max_fanout:
            return False
        self._branches += 1
        return True

    @property
    def steps_consumed(self) -> int:
        """Number of Reasoning_Steps reserved so far this session."""
        return self._steps

    @property
    def branches_opened(self) -> int:
        """Number of Sub_Questions/Exploration_Branches opened so far this session."""
        return self._branches
