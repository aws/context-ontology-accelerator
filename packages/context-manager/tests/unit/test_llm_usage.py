# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for request-scoped LLM usage accounting: tracker, contract, and handlers."""

from __future__ import annotations

import asyncio
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve import llm_usage
from coa_serve.llm_usage import (
    LlmUsageTracker,
    bind_tracker,
    current_tracker,
    graphrag_retrieval,
    is_untracked_graphrag_retrieval,
    note_unattributed_graphrag_call,
    record_usage,
    start_tracked_task,
)
from coa_serve.models import ConfidenceScore, InvokeRequest, InvokeResponse, LlmUsageMetrics, QueryResult, TraceStep
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SONNET = "us.anthropic.claude-sonnet-5"
HAS_AGENTCORE = importlib.util.find_spec("bedrock_agentcore") is not None


# ── Tracker ───────────────────────────────────────────────────────────────────


class TestLlmUsageTracker:
    def test_tracker_sums_completed_calls_into_a_complete_snapshot(self) -> None:
        tracker = LlmUsageTracker()
        for input_tokens, output_tokens in ((1000, 100), (2000, 300)):
            tracker.complete_call(tracker.register_call("converse", SONNET), input_tokens, output_tokens)

        snapshot = tracker.snapshot()

        assert (snapshot.input_tokens, snapshot.output_tokens) == (3000, 400)
        assert snapshot.per_model[0].model_id == SONNET
        assert (snapshot.per_model[0].input_tokens, snapshot.per_model[0].output_tokens) == (3000, 400)
        assert snapshot.per_model[0].max_input_tokens_per_call == 2000
        assert snapshot.complete is True

    def test_tracker_with_no_calls_is_a_complete_zero(self) -> None:
        snapshot = LlmUsageTracker().snapshot()

        assert (snapshot.input_tokens, snapshot.per_model, snapshot.complete) == (0, (), True)

    def test_tracker_pending_call_makes_the_snapshot_incomplete(self) -> None:
        tracker = LlmUsageTracker()
        tracker.complete_call(tracker.register_call("converse", SONNET), 10, 10)
        tracker.register_call("converse", SONNET)  # never finished

        snapshot = tracker.snapshot()

        assert snapshot.complete is False
        assert snapshot.input_tokens == 10  # known usage is kept, never discarded

    def test_tracker_keeps_unknown_model_for_downstream_pricing(self) -> None:
        tracker = LlmUsageTracker()
        tracker.complete_call(tracker.register_call("converse", "us.anthropic.claude-sonnet-9"), 500, 50)

        snapshot = tracker.snapshot()

        assert (snapshot.input_tokens, snapshot.output_tokens) == (500, 50)
        assert snapshot.per_model[0].model_id == "us.anthropic.claude-sonnet-9"
        assert snapshot.complete is True

    @pytest.mark.parametrize("bad", [-1, 1.5, True, None])
    def test_tracker_malformed_counts_fail_the_call(self, bad: object) -> None:
        tracker = LlmUsageTracker()
        tracker.complete_call(tracker.register_call("converse", SONNET), bad, 10)

        snapshot = tracker.snapshot()

        assert snapshot.complete is False
        assert snapshot.input_tokens == 0

    def test_tracker_failed_call_and_mark_incomplete(self) -> None:
        failed = LlmUsageTracker()
        failed.fail_call(failed.register_call("converse", SONNET), llm_usage.REASON_PROVIDER_ERROR)
        marked = LlmUsageTracker()
        marked.mark_incomplete(llm_usage.REASON_UNSUPPORTED_CALL)

        assert failed.snapshot().complete is False
        assert marked.snapshot().complete is False

    def test_tracker_double_completion_counts_once(self) -> None:
        tracker = LlmUsageTracker()
        handle = tracker.register_call("converse", SONNET)
        tracker.complete_call(handle, 100, 10)
        tracker.complete_call(handle, 100, 10)
        tracker.fail_call(handle, llm_usage.REASON_PROVIDER_ERROR)  # already closed: no effect

        snapshot = tracker.snapshot()

        assert (snapshot.input_tokens, snapshot.complete) == (100, True)

    def test_tracker_snapshot_is_final(self) -> None:
        tracker = LlmUsageTracker()
        handle = tracker.register_call("converse", SONNET)
        first = tracker.snapshot()
        tracker.complete_call(handle, 100, 10)  # a worker finishing after the response
        late = tracker.register_call("converse", SONNET)
        tracker.fail_call(late, llm_usage.REASON_PROVIDER_ERROR)
        tracker.mark_incomplete(llm_usage.REASON_UNSUPPORTED_CALL)

        assert first.complete is False
        assert tracker.snapshot().input_tokens == 0

    def test_tracker_ended_work_adds_no_tokens_and_stays_complete(self) -> None:
        tracker = LlmUsageTracker()
        work = tracker.begin_work("graphrag_retrieval")
        tracker.complete_call(tracker.register_call("graphrag_llm", SONNET), 100, 10)
        tracker.end_work(work)

        snapshot = tracker.snapshot()

        # Work has no model id; closing it must not create a model entry.
        assert (snapshot.input_tokens, snapshot.complete) == (100, True)
        assert [usage.model_id for usage in snapshot.per_model] == [SONNET]

    def test_tracker_open_work_is_incomplete_and_a_late_end_cannot_change_it(self) -> None:
        tracker = LlmUsageTracker()
        work = tracker.begin_work("graphrag_retrieval")

        snapshot = tracker.snapshot()
        tracker.end_work(work)  # the worker finishing after the response

        assert snapshot.complete is False
        assert tracker.snapshot().complete is False

    def test_tracker_records_safely_from_many_threads(self) -> None:
        tracker = LlmUsageTracker()

        def call(_: int) -> None:
            tracker.complete_call(tracker.register_call("converse", SONNET), 10, 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(call, range(400)))

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (4000, 400, True)


class TestRecordUsage:
    def test_record_usage_reads_a_converse_usage_block(self) -> None:
        tracker = LlmUsageTracker()
        record_usage(tracker, tracker.register_call("converse", SONNET), {"inputTokens": 7, "outputTokens": 3})

        assert tracker.snapshot().input_tokens == 7

    @pytest.mark.parametrize(
        "usage",
        [
            None,
            {},
            {"inputTokens": 7},
            {"inputTokens": 7, "outputTokens": 3, "cacheReadInputTokens": 5},
            {"inputTokens": 7, "outputTokens": 3, "cacheWriteInputTokens": 5},
            "7 tokens",
        ],
    )
    def test_record_usage_unusable_block_is_incomplete(self, usage: object) -> None:
        tracker = LlmUsageTracker()
        record_usage(tracker, tracker.register_call("converse", SONNET), usage)

        assert tracker.snapshot().complete is False

    def test_record_usage_zero_cache_fields_are_ignored(self) -> None:
        tracker = LlmUsageTracker()
        usage = {"inputTokens": 7, "outputTokens": 3, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
        record_usage(tracker, tracker.register_call("converse", SONNET), usage)

        assert tracker.snapshot().complete is True


# ── Context binding ───────────────────────────────────────────────────────────


class TestContextBinding:
    async def test_tracked_tasks_see_only_their_own_tracker(self) -> None:
        trackers = (LlmUsageTracker(), LlmUsageTracker())
        both_started = asyncio.Barrier(2)

        async def request(tokens: int) -> None:
            await both_started.wait()  # interleave the two requests
            tracker = current_tracker()
            assert tracker is not None
            tracker.complete_call(tracker.register_call("converse", SONNET), tokens, 0)

        await asyncio.gather(
            start_tracked_task(request(100), trackers[0]), start_tracked_task(request(900), trackers[1])
        )

        assert [t.snapshot().input_tokens for t in trackers] == [100, 900]
        assert current_tracker() is None  # the caller's context was never touched

    async def test_cancelling_one_request_keeps_the_other_tracker(self) -> None:
        kept, cancelled = LlmUsageTracker(), LlmUsageTracker()
        release = asyncio.Event()

        async def request() -> LlmUsageTracker | None:
            await release.wait()
            return current_tracker()

        doomed = start_tracked_task(request(), cancelled)
        survivor = start_tracked_task(request(), kept)
        await asyncio.sleep(0)
        doomed.cancel()
        release.set()

        assert await survivor is kept
        with pytest.raises(asyncio.CancelledError):
            await doomed

    def test_executor_thread_needs_an_explicit_binding(self) -> None:
        tracker = LlmUsageTracker()
        with bind_tracker(tracker), ThreadPoolExecutor(max_workers=1) as pool:
            unbound = pool.submit(current_tracker).result()

            def bound() -> LlmUsageTracker | None:
                with bind_tracker(tracker):
                    return current_tracker()

            assert pool.submit(bound).result() is tracker
        assert unbound is None  # run_in_executor-style workers do not inherit the context


class TestGraphragInflightGuard:
    def test_unattributed_call_marks_in_flight_requests_incomplete(self) -> None:
        inflight, finished, untracked_ok = LlmUsageTracker(), LlmUsageTracker(), LlmUsageTracker()
        with graphrag_retrieval(finished):
            pass
        with graphrag_retrieval(inflight):
            seen_in_worker: list[LlmUsageTracker | None] = []

            def toolkit_thread() -> None:
                seen_in_worker.append(current_tracker())
                note_unattributed_graphrag_call()

            thread = threading.Thread(target=toolkit_thread)
            thread.start()
            thread.join()

        assert seen_in_worker == [None]
        assert inflight.snapshot().complete is False
        assert finished.snapshot().complete is True
        assert untracked_ok.snapshot().complete is True

    def test_guard_counts_nested_retrievals(self) -> None:
        tracker = LlmUsageTracker()
        with graphrag_retrieval(tracker):
            with graphrag_retrieval(tracker):
                pass
            assert id(tracker) in llm_usage._graphrag_inflight
        assert id(tracker) not in llm_usage._graphrag_inflight

    def test_untracked_retrieval_registers_nothing(self) -> None:
        assert is_untracked_graphrag_retrieval() is False
        with graphrag_retrieval(None):
            assert current_tracker() is None
            assert is_untracked_graphrag_retrieval() is True
            note_unattributed_graphrag_call()  # no in-flight trackers: a no-op
        assert is_untracked_graphrag_retrieval() is False
        assert llm_usage._graphrag_inflight == {}


# ── Contract ──────────────────────────────────────────────────────────────────


def _result(**kwargs) -> QueryResult:
    return QueryResult(
        tier=3,
        confidence=ConfidenceScore(score=0.5, rationale="r"),
        trace=[TraceStep(step="s", status="success", duration_ms=0)],
        **kwargs,
    )


class TestContract:
    @pytest.mark.parametrize("value", [True, False])
    def test_include_llm_usage_accepts_booleans(self, value: bool) -> None:
        assert (
            InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": value}).options["includeLlmUsage"]
            is value
        )

    @pytest.mark.parametrize("value", ["false", "true", 1, 0, None, {}])
    def test_include_llm_usage_rejects_non_booleans(self, value: object) -> None:
        with pytest.raises(ValidationError, match="includeLlmUsage"):
            InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": value})

    def test_query_result_omits_llm_usage_by_default(self) -> None:
        assert "llmUsage" not in _result().model_dump(by_alias=True, exclude_none=True, mode="json")

    def test_query_result_serializes_llm_usage_in_camel_case(self) -> None:
        tracker = LlmUsageTracker()
        tracker.complete_call(tracker.register_call("converse", SONNET), 1000, 100)

        dumped = _result(llm_usage=LlmUsageMetrics.from_snapshot(tracker.snapshot())).model_dump(
            by_alias=True, exclude_none=True, mode="json"
        )

        assert dumped["llmUsage"] == {
            "inputTokens": 1000,
            "outputTokens": 100,
            "perModel": [
                {
                    "modelId": SONNET,
                    "inputTokens": 1000,
                    "outputTokens": 100,
                    "maxInputTokensPerCall": 1000,
                }
            ],
            "complete": True,
        }

    @pytest.mark.parametrize(
        "field",
        [
            {"inputTokens": -1},
            {"inputTokens": True},
            {"perModel": "not-a-list"},
            {"perModel": [{"modelId": "", "inputTokens": 1, "outputTokens": 1, "maxInputTokensPerCall": 1}]},
            {"perModel": [{"modelId": SONNET, "inputTokens": True, "outputTokens": 1, "maxInputTokensPerCall": 1}]},
            {"complete": "yes"},
        ],
    )
    def test_llm_usage_metrics_rejects_malformed_values(self, field: dict) -> None:
        base = {
            "inputTokens": 1,
            "outputTokens": 1,
            "perModel": [{"modelId": SONNET, "inputTokens": 1, "outputTokens": 1, "maxInputTokensPerCall": 1}],
            "complete": True,
        }
        with pytest.raises(ValidationError):
            LlmUsageMetrics.model_validate({**base, **field})


# ── Handlers ──────────────────────────────────────────────────────────────────


def _orchestrator_spending(tokens: int, *, raise_exc: Exception | None = None, hang: bool = False) -> MagicMock:
    """A fake orchestrator whose resolve() spends ``tokens`` through the request tracker."""

    async def resolve(request, **kwargs):
        tracker = current_tracker()
        if tracker is not None:
            tracker.complete_call(tracker.register_call("converse", SONNET), tokens, 10)
            if hang:
                tracker.register_call("converse", SONNET)  # still running at the deadline
        if hang:
            await asyncio.sleep(3600)
        if raise_exc is not None:
            raise raise_exc
        return InvokeResponse(result=_result(metadata={}))

    orchestrator = MagicMock()
    orchestrator.resolve = AsyncMock(side_effect=resolve)
    return orchestrator


async def _blocking(request: InvokeRequest) -> list[dict]:
    from coa_serve.main import _handle_blocking_query

    return [event async for event in _handle_blocking_query(request, "req-1")]


class TestBlockingHandler:
    @pytest.fixture(autouse=True)
    def _orchestrator(self):
        import coa_serve.main as main_mod

        saved = main_mod._orchestrator
        yield
        main_mod._orchestrator = saved

    async def test_blocking_opt_in_attaches_complete_usage(self) -> None:
        import coa_serve.main as main_mod

        main_mod._orchestrator = _orchestrator_spending(1000)
        [event] = await _blocking(InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": True}))

        usage = event["result"]["llmUsage"]
        assert (usage["inputTokens"], usage["outputTokens"], usage["complete"]) == (1000, 10, True)
        assert usage["perModel"] == [
            {
                "modelId": SONNET,
                "inputTokens": 1000,
                "outputTokens": 10,
                "maxInputTokensPerCall": 1000,
            }
        ]

    async def test_blocking_without_opt_in_has_no_usage_and_no_tracker(self) -> None:
        import coa_serve.main as main_mod

        main_mod._orchestrator = _orchestrator_spending(1000)
        [event] = await _blocking(InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": False}))

        assert "llmUsage" not in event["result"]

    async def test_blocking_error_keeps_the_known_spend(self) -> None:
        import coa_serve.main as main_mod

        main_mod._orchestrator = _orchestrator_spending(400, raise_exc=RuntimeError("boom"))
        [event] = await _blocking(InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": True}))

        assert event["statusCode"] == 500
        assert event["llmUsage"]["inputTokens"] == 400
        assert event["llmUsage"]["complete"] is True

    async def test_blocking_timeout_with_a_call_in_flight_is_incomplete(self) -> None:
        import coa_serve.main as main_mod

        main_mod._orchestrator = _orchestrator_spending(400, hang=True)
        with patch.object(main_mod, "RESOLVE_TIMEOUT_S", 0.05):
            [event] = await _blocking(InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": True}))

        assert event["statusCode"] == 504
        assert event["llmUsage"]["inputTokens"] == 400
        assert event["llmUsage"]["complete"] is False

    async def test_concurrent_blocking_requests_do_not_mix_usage(self) -> None:
        import coa_serve.main as main_mod

        async def resolve(request, **kwargs):
            tracker = current_tracker()
            await asyncio.sleep(0.01)
            tracker.complete_call(tracker.register_call("converse", SONNET), len(request.query), 0)
            return InvokeResponse(result=_result(metadata={}))

        main_mod._orchestrator = MagicMock(resolve=AsyncMock(side_effect=resolve))
        events = await asyncio.gather(
            *(
                _blocking(InvokeRequest(query="q" * n, namespace="ns", options={"includeLlmUsage": True}))
                for n in (3, 30, 300)
            )
        )

        assert [e[0]["result"]["llmUsage"]["inputTokens"] for e in events] == [3, 30, 300]


@pytest.mark.skipif(not HAS_AGENTCORE, reason="bedrock_agentcore SDK not installed")
class TestStreamingHandler:
    @pytest.fixture(autouse=True)
    def _orchestrator(self):
        import coa_serve.main as main_mod

        saved = main_mod._orchestrator
        yield
        main_mod._orchestrator = saved

    async def test_streaming_done_event_carries_usage(self) -> None:
        import coa_serve.main as main_mod
        from coa_serve.main import _handle_streaming_query

        main_mod._orchestrator = _orchestrator_spending(250)
        request = InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": True})
        with patch.object(main_mod, "_session_manager", None):
            events = [event async for event in _handle_streaming_query({}, request, "req-1", "")]

        done = [e for e in events if isinstance(e, dict) and e.get("type") == "done"]
        assert done, events
        assert done[0]["payload"]["result"]["llmUsage"]["inputTokens"] == 250

    async def test_streaming_error_event_keeps_known_usage(self) -> None:
        import coa_serve.main as main_mod
        from coa_serve.main import _handle_streaming_query

        main_mod._orchestrator = _orchestrator_spending(250, raise_exc=RuntimeError("boom"))
        request = InvokeRequest(query="q", namespace="ns", options={"includeLlmUsage": True})
        with patch.object(main_mod, "_session_manager", None):
            events = [event async for event in _handle_streaming_query({}, request, "req-1", "")]

        errors = [event for event in events if isinstance(event, dict) and event.get("type") == "error"]
        assert errors, events
        usage = errors[0]["payload"]["llmUsage"]
        assert (usage["inputTokens"], usage["complete"]) == (250, True)
