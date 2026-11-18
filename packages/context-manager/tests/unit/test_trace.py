# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for TraceCollector — progressive step emission and flush."""

from __future__ import annotations

import asyncio

import pytest
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit


class TestTraceCollector:
    async def test_record_appends_steps(self):
        trace = TraceCollector()
        trace.record("metric_match", "success", 42, detail="loss_ratio")
        trace.record("vector_search", "error", 100, detail="timeout")

        assert len(trace.steps) == 2
        assert trace.steps[0].step == "metric_match"
        assert trace.steps[0].status == "success"
        assert trace.steps[0].duration_ms == 42
        assert trace.steps[0].detail == "loss_ratio"
        assert trace.steps[1].step == "vector_search"
        assert trace.steps[1].status == "error"

    async def test_add_dicts_ingests_steps(self):
        trace = TraceCollector()
        trace.add_dicts(
            [
                {"step": "llm_translate", "status": "success", "durationMs": 200},
                {"step": "sparql_validation", "status": "skipped", "durationMs": 0, "detail": "no sparql"},
            ]
        )

        assert len(trace.steps) == 2
        assert trace.steps[0].step == "llm_translate"
        assert trace.steps[0].duration_ms == 200
        assert trace.steps[1].detail == "no sparql"

    async def test_record_carries_graph_seed_mode(self):
        """The graph-traversal seed mode serializes to the client as ``graphSeedMode``."""
        trace = TraceCollector()
        trace.record("t3.graph_traverse", "success", 50, tool_used="neptune", graph_seed_mode="uri_hop")
        assert trace.steps[0].graph_seed_mode == "uri_hop"
        assert trace.steps[0].model_dump(by_alias=True)["graphSeedMode"] == "uri_hop"

    async def test_add_dicts_carries_graph_seed_mode(self):
        trace = TraceCollector()
        trace.add_dicts([{"step": "t3.graph_traverse", "status": "success", "graphSeedMode": "keyword"}])
        assert trace.steps[0].graph_seed_mode == "keyword"

    async def test_add_dicts_skips_invalid_entries(self):
        trace = TraceCollector()
        trace.add_dicts(
            [
                {"step": "", "status": "success", "durationMs": 0},
                {"step": "valid", "status": "", "durationMs": 0},
                {"step": "good", "status": "success", "durationMs": 10},
            ]
        )

        assert len(trace.steps) == 1
        assert trace.steps[0].step == "good"

    async def test_on_record_callback_fires_for_each_step(self):
        received: list[str] = []

        async def on_record(ts):
            received.append(ts.step)

        trace = TraceCollector(on_record=on_record)
        trace.record("step_a", "success", 10)
        trace.record("step_b", "success", 20)

        await trace.flush()

        assert received == ["step_a", "step_b"]

    async def test_flush_awaits_all_pending_callbacks(self):
        order: list[str] = []

        async def slow_callback(ts):
            await asyncio.sleep(0.01)
            order.append(ts.step)

        trace = TraceCollector(on_record=slow_callback)
        trace.record("first", "success", 10)
        trace.record("second", "success", 20)
        trace.record("third", "success", 30)

        await trace.flush()

        assert set(order) == {"first", "second", "third"}

    async def test_flush_is_idempotent(self):
        call_count = 0

        async def counting_callback(ts):
            nonlocal call_count
            call_count += 1

        trace = TraceCollector(on_record=counting_callback)
        trace.record("step", "success", 10)

        await trace.flush()
        assert call_count == 1

        await trace.flush()
        assert call_count == 1

    async def test_callback_exception_does_not_block_flush(self):
        results: list[str] = []

        async def failing_callback(ts):
            if ts.step == "bad":
                raise RuntimeError("intentional failure")
            results.append(ts.step)

        trace = TraceCollector(on_record=failing_callback)
        trace.record("good1", "success", 10)
        trace.record("bad", "error", 0)
        trace.record("good2", "success", 20)

        await trace.flush()

        assert "good1" in results
        assert "good2" in results

    async def test_no_callback_flush_is_noop(self):
        trace = TraceCollector()
        trace.record("step", "success", 10)

        await trace.flush()

        assert len(trace.steps) == 1

    async def test_total_ms_tracks_elapsed_time(self):
        trace = TraceCollector()
        await asyncio.sleep(0.05)
        assert trace.total_ms >= 40  # At least ~50ms elapsed (allow some slack)

    async def test_steps_property_returns_copy(self):
        trace = TraceCollector()
        trace.record("a", "success", 10)

        steps = trace.steps
        steps.append(None)  # type: ignore

        assert len(trace.steps) == 1
