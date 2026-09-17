# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request-scoped deadline propagation.

Serve historically had no request-scoped deadline: the only time limits lived at
leaf DB clients (fixed ``timeout_seconds=35`` in the NL→SQL executor, ``45.0`` in
the lexical retriever) plus a single outer ``asyncio.wait_for(timeout=
RESOLVE_TIMEOUT_S)`` wrapper in ``main.py``. Nothing carried "time remaining" from
the entrypoint into the strategies, so no inner step could decide *"I cannot fit
another attempt in the budget left — stop now"*.

That gap is why the NL→SQL two-shot self-correction loop overruns the REST
transport's hard ceiling: shot 1 is comfortably inside the budget, but shot 2
pushes total latency past the API Gateway integration timeout, and the request
504s server-side *after* having computed a usable answer.

``Deadline`` is that missing mechanism. It is created once per request in
``main.py`` from the caller's budget — ``options.timeoutMs`` when supplied,
otherwise the transport default already encoded in ``RESOLVE_TIMEOUT_S`` — and is
threaded through ``StrategyContext`` so downstream steps can be deadline-aware
without re-deriving or guessing the budget.

Design notes:
- Monotonic clock only (``time.monotonic``). Wall-clock jumps (NTP steps) must not
  move a deadline; only elapsed real time matters.
- The budget is a hard *upper* bound taken as ``min(transport_budget, caller
  timeoutMs)``. A caller may ask for LESS time than the transport allows, never
  more — a request cannot outlive the transport that carries it.
- Absent ``timeoutMs`` yields the transport budget unchanged, so the AgentCore /
  Playground path (RESOLVE_TIMEOUT_S ~= 170s) keeps its full budget and is NOT
  clamped down to the REST ceiling. This is the primary regression guard.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Smallest budget we will accept from a caller. Below this a request cannot do
# useful work (a single LLM generation alone exceeds it), so we floor it rather
# than let a ``timeoutMs: 1`` degenerate into "skip everything, return nothing".
_MIN_BUDGET_S = 1.0


@dataclass
class Deadline:
    """A monotonic, request-scoped time budget.

    Create with :meth:`from_budget` (clamps + records the start instant). Query
    :meth:`remaining_s` / :meth:`remaining_ms` at any inner step to decide whether
    the next unit of work fits.
    """

    budget_s: float
    _start: float = field(default_factory=time.monotonic)

    @classmethod
    def from_budget(
        cls,
        transport_budget_s: float,
        caller_timeout_ms: int | float | None = None,
    ) -> Deadline:
        """Build a deadline from the transport budget and an optional caller budget.

        Args:
            transport_budget_s: The budget the carrying transport allows (already
                encoded per-transport in ``RESOLVE_TIMEOUT_S``). Used as-is when the
                caller does not specify one.
            caller_timeout_ms: The caller's ``options.timeoutMs`` in milliseconds,
                or ``None`` when absent. When present, the effective budget is the
                MINIMUM of it and the transport budget — a caller may request less
                time, never more.

        Returns:
            A ``Deadline`` whose clock starts now.
        """
        budget_s = float(transport_budget_s)
        if caller_timeout_ms is not None:
            try:
                requested_s = float(caller_timeout_ms) / 1000.0
            except (TypeError, ValueError):
                # Distinguish "invalid timeout provided" from "no timeout provided"
                # in traces; the value is dropped, budget falls back to transport.
                logger.info("deadline_invalid_caller_timeout_ms raw=%r", caller_timeout_ms)
                requested_s = budget_s
            if requested_s > 0:
                budget_s = min(budget_s, requested_s)
        budget_s = max(_MIN_BUDGET_S, budget_s)
        return cls(budget_s=budget_s)

    def elapsed_s(self) -> float:
        """Seconds elapsed since this deadline started."""
        return time.monotonic() - self._start

    def remaining_s(self) -> float:
        """Seconds left in the budget. Never negative — clamped at 0."""
        return max(0.0, self.budget_s - self.elapsed_s())

    def remaining_ms(self) -> int:
        """Milliseconds left in the budget. Never negative."""
        return int(self.remaining_s() * 1000)

    def expired(self) -> bool:
        """True once the budget is exhausted."""
        return self.remaining_s() <= 0.0

    def fits(self, cost_s: float) -> bool:
        """True when a unit of work costing ``cost_s`` seconds fits the time left.

        Used by deadline-aware call sites (e.g. the NL→SQL correction shot) to skip
        work that cannot complete inside the budget rather than start it and 504.
        """
        return self.remaining_s() >= cost_s
