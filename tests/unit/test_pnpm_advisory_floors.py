# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Regression test: the root pnpm lockfile resolves no follow-redirects below its advisory floor."""

from __future__ import annotations

import re
from pathlib import Path

_LOCKFILE = Path(__file__).resolve().parents[2] / "pnpm-lock.yaml"

# Cross-origin sensitiveHeaders leak on redirect (GHSA-mrwp-f3q8-3g9p), fixed in 1.16.2.
_FLOOR = (1, 16, 2)


def test_follow_redirects_is_at_or_above_the_advisory_floor() -> None:
    resolved = {
        tuple(int(part) for part in match.groups())
        for match in re.finditer(r"^  '?follow-redirects@(\d+)\.(\d+)\.(\d+)[^:]*:$", _LOCKFILE.read_text(), re.M)
    }

    assert resolved, "follow-redirects is not in pnpm-lock.yaml; the key format changed or the floor is stale"
    assert min(resolved) >= _FLOOR, f"pnpm-lock.yaml resolves follow-redirects {sorted(resolved)}"
