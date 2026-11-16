# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Agentic Retriever budget/clock manager.

BudgetClock enforces the step cap, the fan-out cap and the wall-clock budget in
aggregate across branches. A manually advanced fake clock keeps time deterministic.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from coa_serve.tier3.agentic import AgenticBudgetConfig, BudgetClock
from coa_serve.tier3.agentic.budget import (
    AgenticBudgetConfig as DirectConfig,
)
from coa_serve.tier3.agentic.budget import BudgetClock as DirectClock


class FakeClock:
    """A manually advanced monotonic time source (seconds).

    Drop-in for ``time.monotonic``: calling it returns the current fake time;
    :meth:`advance` moves time forward deterministically.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.mark.unit
class TestAgenticBudgetConfig:
    def test_documented_defaults(self):
        """Defaults match the documented deployment defaults."""
        cfg = AgenticBudgetConfig()
        assert cfg.time_budget_s == 30.0
        assert cfg.max_steps == 10
        assert cfg.per_tool_timeout_s == 30.0
        assert cfg.max_fanout == 5
        assert cfg.synthesis_reserve_s == 8.0

    def test_is_frozen(self):
        cfg = AgenticBudgetConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.max_steps = 99  # type: ignore[misc]

    def test_reexported_symbols_are_the_same(self):
        """The package __init__ re-exports the module-level classes unchanged."""
        assert AgenticBudgetConfig is DirectConfig
        assert BudgetClock is DirectClock


@pytest.mark.unit
class TestStepCap:
    def test_allows_exactly_max_steps_then_refuses(self):
        """``try_consume_step`` returns True up to ``max_steps`` then False, and
        never exceeds the cap."""
        clock = BudgetClock(AgenticBudgetConfig(max_steps=3), clock=FakeClock())
        clock.started()

        assert [clock.try_consume_step() for _ in range(3)] == [True, True, True]
        assert clock.try_consume_step() is False
        assert clock.try_consume_step() is False
        assert clock.steps_consumed == 3

    def test_min_cap_of_one(self):
        clock = BudgetClock(AgenticBudgetConfig(max_steps=1), clock=FakeClock())
        clock.started()
        assert clock.try_consume_step() is True
        assert clock.try_consume_step() is False
        assert clock.steps_consumed == 1


@pytest.mark.unit
class TestFanoutCap:
    def test_allows_exactly_max_fanout_then_refuses(self):
        """``try_open_branch`` returns True up to ``max_fanout`` then False, and
        never exceeds the cap."""
        clock = BudgetClock(AgenticBudgetConfig(max_fanout=2), clock=FakeClock())
        clock.started()

        assert clock.try_open_branch() is True
        assert clock.try_open_branch() is True
        assert clock.try_open_branch() is False
        assert clock.branches_opened == 2


@pytest.mark.unit
class TestWallClockBudget:
    def test_expired_flips_exactly_at_the_boundary(self):
        """``expired()`` is False just below the budget and True exactly at it
        the boundary is ``elapsed >= time_budget_s``."""
        fake = FakeClock()
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30), clock=fake)
        clock.started()

        assert clock.expired() is False  # elapsed 0

        fake.advance(29.999)
        assert clock.expired() is False  # just below the boundary

        fake.advance(0.001)  # elapsed now exactly 30.0
        assert clock.expired() is True  # flips at the boundary

        fake.advance(5.0)
        assert clock.expired() is True  # stays expired past the boundary

    def test_remaining_s_counts_down_and_clamps_at_zero(self):
        fake = FakeClock()
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30), clock=fake)
        clock.started()

        assert clock.remaining_s() == 30.0
        fake.advance(10.0)
        assert clock.remaining_s() == pytest.approx(20.0)
        fake.advance(25.0)  # overshoot the budget
        assert clock.remaining_s() == 0.0  # clamped, never negative

    def test_elapsed_ms_tracks_fake_time(self):
        fake = FakeClock()
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30), clock=fake)
        clock.started()
        fake.advance(1.234)
        assert clock.elapsed_ms() == 1234

    def test_before_started_is_not_expired_and_full_remaining(self):
        """Before ``started()`` the clock reports no elapsed time, so it is not
        expired and the full budget remains."""
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30), clock=FakeClock())
        assert clock.expired() is False
        assert clock.remaining_s() == 30.0
        assert clock.elapsed_ms() == 0


@pytest.mark.unit
class TestToolDeadline:
    """Deadline-aware per-tool timeout: clamp the caller's cap to the
    wall-clock time left minus the synthesis reserve so a late tool cannot
    overrun the Time_Budget nor eat the synthesis slice."""

    def test_early_in_session_returns_the_caller_cap(self):
        """With plenty of budget left, the caller's per-tool cap is returned as-is."""
        fake = FakeClock()
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30, synthesis_reserve_s=8), clock=fake)
        clock.started()
        # remaining 30 - reserve 8 = 22 usable; min(cap 10, 22) == 10.
        assert clock.tool_deadline_s(10.0) == 10.0

    def test_clamps_to_remaining_minus_reserve_late_in_session(self):
        """Late in the session the cap is clamped to remaining - reserve."""
        fake = FakeClock()
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30, synthesis_reserve_s=8), clock=fake)
        clock.started()
        fake.advance(18)  # remaining 12; usable 12 - 8 = 4; min(cap 30, 4) == 4.
        assert clock.tool_deadline_s(30.0) == 4.0

    def test_reserve_is_protected_when_budget_nearly_spent(self):
        """Once remaining <= reserve, the deadline floors at the epsilon (a tool
        cannot consume the synthesis slice)."""
        fake = FakeClock()
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30, synthesis_reserve_s=8), clock=fake)
        clock.started()
        fake.advance(25)  # remaining 5 < reserve 8; usable negative -> floored.
        d = clock.tool_deadline_s(30.0)
        assert d > 0.0
        assert d < 1.0

    def test_honors_an_explicitly_small_caller_cap(self):
        """A small caller cap (e.g. a test/config override) is not overridden by
        the reserve math when budget is ample."""
        fake = FakeClock()
        clock = BudgetClock(AgenticBudgetConfig(time_budget_s=30, synthesis_reserve_s=8), clock=fake)
        clock.started()
        assert clock.tool_deadline_s(0.02) == 0.02


@pytest.mark.unit
class TestAggregateCapsAreSharedAcrossBranches:
    def test_step_cap_is_aggregate_across_simulated_branches(self):
        """A single shared clock instance enforces the step cap across multiple
        simulated branches combined, not per-branch."""
        clock = BudgetClock(AgenticBudgetConfig(max_steps=4), clock=FakeClock())
        clock.started()

        # Three simulated branches all draw from the same shared step budget.
        def branch_consume(n: int) -> int:
            return sum(1 for _ in range(n) if clock.try_consume_step())

        granted = branch_consume(2) + branch_consume(2) + branch_consume(2)
        assert granted == 4  # only the aggregate cap of 4 was granted total
        assert clock.steps_consumed == 4
        assert clock.try_consume_step() is False
