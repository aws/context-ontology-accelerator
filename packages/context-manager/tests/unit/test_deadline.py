# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the request-scoped Deadline (A0).

Covers budget derivation (transport default vs caller ``timeoutMs``), the
never-widen clamp, garbage/zero handling, and the monotonic remaining-time API.
"""

from __future__ import annotations

import time

import pytest
from coa_serve.deadline import Deadline

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestDeadlineFromBudget:
    def test_absent_timeout_uses_transport_budget(self):
        # The REST path passes ~29s; AgentCore passes ~170s. Absent caller
        # timeoutMs MUST fall back to the transport budget, never a hardcoded cap.
        assert Deadline.from_budget(29.0, None).budget_s == 29.0
        assert Deadline.from_budget(170.0, None).budget_s == 170.0

    def test_caller_can_narrow_budget(self):
        # timeoutMs is in MILLISECONDS; 5000ms narrows a 170s transport to 5s.
        assert Deadline.from_budget(170.0, 5000).budget_s == 5.0

    def test_caller_cannot_widen_past_transport(self):
        # A caller asking for 999s over a 29s REST transport is clamped to 29s —
        # the transport ceiling (API Gateway 29s) is hard and cannot be widened.
        assert Deadline.from_budget(29.0, 999_000).budget_s == 29.0

    def test_zero_or_negative_timeout_ignored(self):
        assert Deadline.from_budget(29.0, 0).budget_s == 29.0
        assert Deadline.from_budget(29.0, -100).budget_s == 29.0

    def test_non_numeric_timeout_ignored(self, caplog):
        # A malformed timeoutMs must not crash request handling. Cast hides the
        # deliberate type violation from the static checker; the runtime guard is
        # what's under test.
        from typing import cast

        with caplog.at_level("INFO"):
            assert Deadline.from_budget(29.0, cast(float, "bad")).budget_s == 29.0
        assert Deadline.from_budget(29.0, None).budget_s == 29.0
        # The invalid value is surfaced (observability), the None case is silent.
        assert "deadline_invalid_caller_timeout_ms" in caplog.text


@pytest.mark.unit
class TestDeadlineRemaining:
    def test_remaining_starts_near_full_budget(self):
        d = Deadline.from_budget(29.0, None)
        # Fresh deadline: remaining is essentially the full budget.
        assert 28.0 <= d.remaining_s() <= 29.0
        assert d.remaining_ms() > 0

    def test_remaining_never_negative(self):
        # Budget above the _MIN_BUDGET_S floor (1.0s) so the value under test is
        # the one we set; sleep past it and confirm remaining floors at 0.
        d = Deadline.from_budget(1.0, None)
        time.sleep(1.1)
        # Past the deadline, remaining floors at 0 (never negative).
        assert d.remaining_s() == 0.0
        assert d.remaining_ms() == 0

    def test_fits_true_when_budget_covers_cost(self):
        d = Deadline.from_budget(29.0, None)
        assert d.fits(10.0) is True

    def test_fits_false_when_cost_exceeds_remaining(self):
        d = Deadline.from_budget(5.0, None)
        assert d.fits(10.0) is False

    def test_expired_flag(self):
        # Budget at the _MIN_BUDGET_S floor (1.0s); sleep past it to expire.
        d = Deadline.from_budget(1.0, None)
        assert d.expired() is False
        time.sleep(1.1)
        assert d.expired() is True
