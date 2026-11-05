# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Default equivalence (AC-2): at default stop-signal settings the planner behaves as on main.

Each scenario's planner prompts (system and user bytes), planner call count and
stop reason must equal the fixture captured on main (``f3f17f0ba``).
"""

from __future__ import annotations

import pytest
from coa_serve.tier3.agentic.models import StopSignalConfig

from .stop_signal_scenarios import SCENARIOS, load_fixture, run_scenario

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_default_prompts_calls_and_stop_match_main(name):
    record = await run_scenario(name)
    expected = load_fixture(name)
    assert record["call_count"] == expected["call_count"]
    assert record["stop_reason"] == expected["stop_reason"]
    for got, want in zip(record["calls"], expected["calls"], strict=True):
        assert got["system"] == want["system"]
        assert got["prompt"] == want["prompt"]
    assert record == expected


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_explicit_default_config_matches_main(name):
    """The injected default StopSignalConfig (0/0, telemetry off) is main's behaviour too."""
    record = await run_scenario(
        name,
        planner_kwargs={"stop_signals": StopSignalConfig()},
        controller_kwargs={"stop_signals": StopSignalConfig()},
    )
    assert record == load_fixture(name)


class _RecordingEmbed:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_wired_embed_backend_is_unused_at_defaults(name):
    """Production wires an embed backend into the controller; at defaults it is never called."""
    embed = _RecordingEmbed()
    record = await run_scenario(
        name,
        planner_kwargs={"stop_signals": StopSignalConfig()},
        controller_kwargs={"stop_signals": StopSignalConfig(), "embed_fn": embed},
    )
    assert record == load_fixture(name)
    assert embed.calls == []


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_enabled_signals_change_the_prompts(name):
    """Sanity check that the fixtures can fail: telemetry on changes at least one prompt."""
    record = await run_scenario(name, planner_kwargs={"stop_signals": StopSignalConfig(telemetry=True)})
    expected = load_fixture(name)
    assert [c["system"] for c in record["calls"]] != [c["system"] for c in expected["calls"]]


def test_fixtures_cover_every_call_site():
    calls = [c for name in SCENARIOS for c in load_fixture(name)["calls"]]
    assert any("STOP WHEN DONE" in c["system"] for c in calls)  # single-call propose
    assert any(c["prompt"].endswith("and nothing else.") for c in calls)  # propose repair turn
    assert any('"sufficient"' in c["system"] for c in load_fixture("prefetch_sufficient")["calls"])  # prefetch
    assert sum('"sufficient"' in c["system"] for c in load_fixture("assess_loop")["calls"]) == 2  # assess loop
