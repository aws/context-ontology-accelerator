# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request-scoped deadline propagation.

``Deadline`` is created once per request in ``main.py`` from the caller's budget
and threaded through ``StrategyContext`` so downstream steps (for example the
NL→SQL correction shot) can skip work that cannot finish in the time left.

- Monotonic clock only (``time.monotonic``): wall-clock jumps must not move a
  deadline.
- The budget is ``min(transport_budget, caller timeoutMs)``: a caller may ask for
  less time than the transport allows, never more.
- Absent ``timeoutMs`` yields the transport budget unchanged, so the AgentCore /
  Playground path keeps its full budget and is not clamped to the REST ceiling.
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

        A caller budget (``options.timeoutMs``, in milliseconds) can only narrow the
        transport budget.
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
