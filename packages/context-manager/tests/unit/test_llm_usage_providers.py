# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the three text-LLM provider boundaries' usage recording.

Bedrock and GraphRAG run against faked boto clients only: the GraphRAG test goes
through the real toolkit ``LLMCache`` and the pinned LlamaIndex ``BedrockConverse``,
so it fails if either stops routing keyword generation through ``chat``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from coa_serve.clients.bedrock import BedrockLLMClient
from coa_serve.llm_usage import (
    LlmUsageSnapshot,
    LlmUsageTracker,
    bind_tracker,
    graphrag_retrieval,
    start_tracked_task,
)

from . import strands_fake
from .strands_fake import install_fake_strands, uninstall_fake_strands
from .test_sql_agent import _build, _generate_and_execute, _run

pytestmark = pytest.mark.unit

SONNET = "us.anthropic.claude-sonnet-5"


def _converse_response(input_tokens: int = 1200, output_tokens: int = 80, **extra) -> dict:
    return {
        "output": {"message": {"content": [{"text": "answer"}]}},
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
        },
        **extra,
    }


def _prompt(converse_kwargs: dict) -> str:
    return converse_kwargs["messages"][0]["content"][0]["text"]


def _client_error(code: str, operation: str = "Converse") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)


def _client(boto: MagicMock) -> BedrockLLMClient:
    client = BedrockLLMClient(model_id=SONNET, region="us-east-1")
    client._client = boto
    return client


async def _tracked(coro, tracker: LlmUsageTracker):
    return await start_tracked_task(coro, tracker)


# ── Direct Bedrock: converse ──────────────────────────────────────────────────


class TestConverseUsage:
    async def test_converse_records_model_reported_usage(self) -> None:
        boto = MagicMock()
        boto.converse.return_value = _converse_response(1200, 80)
        tracker = LlmUsageTracker()

        result = await _tracked(_client(boto).converse("q"), tracker)

        snapshot = tracker.snapshot()
        assert result.text == "answer"
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (1200, 80, True)
        assert snapshot.per_model[0].model_id == SONNET

    async def test_converse_records_the_per_call_model_override(self) -> None:
        boto = MagicMock()
        boto.converse.return_value = _converse_response(1000, 0)
        tracker = LlmUsageTracker()

        await _tracked(_client(boto).converse("q", model_id="global.anthropic.claude-sonnet-5"), tracker)

        assert tracker.snapshot().per_model[0].model_id == "global.anthropic.claude-sonnet-5"

    async def test_converse_without_a_tracker_is_unchanged(self) -> None:
        boto = MagicMock()
        boto.converse.return_value = _converse_response()

        assert (await _client(boto).converse("q")).text == "answer"

    async def test_converse_transport_failure_is_incomplete(self) -> None:
        boto = MagicMock()
        boto.converse.side_effect = _client_error("ThrottlingException")
        tracker = LlmUsageTracker()

        with pytest.raises(ClientError):
            await _tracked(_client(boto).converse("q"), tracker)

        assert tracker.snapshot().complete is False

    async def test_converse_usage_is_kept_when_the_response_then_errors(self) -> None:
        # A model refusal returns no text and raises — but the call was billed.
        boto = MagicMock()
        boto.converse.return_value = {**_converse_response(300, 5), "output": {"message": {"content": []}}}
        tracker = LlmUsageTracker()

        with pytest.raises(ValueError):
            await _tracked(_client(boto).converse("q"), tracker)

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.complete) == (300, True)

    async def test_converse_missing_usage_is_incomplete(self) -> None:
        boto = MagicMock()
        response = _converse_response()
        del response["usage"]
        boto.converse.return_value = response
        tracker = LlmUsageTracker()

        await _tracked(_client(boto).converse("q"), tracker)

        assert tracker.snapshot().complete is False

    async def test_cancelled_await_still_records_when_the_worker_finishes(self) -> None:
        # A deadline cancels the await; the worker keeps running and Bedrock bills it.
        release, finished = threading.Event(), threading.Event()

        def slow_converse(**kwargs):
            release.wait(5)
            finished.set()
            return _converse_response(700, 7)

        boto = MagicMock()
        boto.converse.side_effect = slow_converse
        tracker = LlmUsageTracker()
        task = start_tracked_task(_client(boto).converse("q"), tracker)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        release.set()
        await asyncio.to_thread(finished.wait, 5)
        await asyncio.sleep(0.05)  # let the worker return from the patched call

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.complete) == (700, True)

    async def test_snapshot_before_the_cancelled_worker_finishes_is_incomplete(self) -> None:
        release = threading.Event()

        def slow_converse(**kwargs):
            release.wait(5)
            return _converse_response(700, 7)

        boto = MagicMock()
        boto.converse.side_effect = slow_converse
        tracker = LlmUsageTracker()
        task = start_tracked_task(_client(boto).converse("q"), tracker)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        snapshot = tracker.snapshot()  # the response is assembled while Bedrock still runs
        release.set()

        assert snapshot.complete is False
        assert snapshot.input_tokens == 0

    async def test_concurrent_converse_requests_do_not_mix(self) -> None:
        boto = MagicMock()
        boto.converse.side_effect = lambda **kwargs: _converse_response(len(_prompt(kwargs)), 1)
        client = _client(boto)
        trackers = [LlmUsageTracker() for _ in range(5)]

        await asyncio.gather(*(_tracked(client.converse("x" * (i + 1) * 10), t) for i, t in enumerate(trackers)))

        assert [t.snapshot().input_tokens for t in trackers] == [10, 20, 30, 40, 50]


# ── Direct Bedrock: converse_stream ───────────────────────────────────────────


def _stream(events: list[dict]) -> dict:
    return {"stream": iter(events)}


_TEXT = [{"contentBlockDelta": {"delta": {"text": "an"}}}, {"contentBlockDelta": {"delta": {"text": "swer"}}}]
_STOP = {"messageStop": {"stopReason": "end_turn"}}


def _metadata(input_tokens: int, output_tokens: int) -> dict:
    return {"metadata": {"usage": {"inputTokens": input_tokens, "outputTokens": output_tokens}}}


async def _drain(client: BedrockLLMClient) -> str:
    return "".join([token async for token in client.converse_stream("q")])


class TestConverseStreamUsage:
    async def test_stream_records_final_metadata_usage_once(self) -> None:
        boto = MagicMock()
        boto.converse_stream.return_value = _stream([*_TEXT, _STOP, _metadata(900, 40), _metadata(1, 1)])
        tracker = LlmUsageTracker()

        text = await _tracked(_drain(_client(boto)), tracker)

        snapshot = tracker.snapshot()
        assert text == "answer"
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (900, 40, True)

    async def test_stream_without_metadata_is_incomplete(self) -> None:
        boto = MagicMock()
        boto.converse_stream.return_value = _stream([*_TEXT, _STOP])
        tracker = LlmUsageTracker()

        await _tracked(_drain(_client(boto)), tracker)

        assert tracker.snapshot().complete is False

    async def test_guardrail_blocked_stream_keeps_its_usage(self) -> None:
        from coa_serve.clients.base import GuardrailBlockedError

        trace = {"guardrail": {"outputAssessments": {"g": [{"contentPolicy": {"filters": [{"action": "BLOCKED"}]}}]}}}
        boto = MagicMock()
        blocked_stop = {"messageStop": {"stopReason": "guardrail_intervened"}}
        usage_with_trace = {"metadata": {**_metadata(500, 20)["metadata"], "trace": trace}}
        boto.converse_stream.return_value = _stream([*_TEXT, blocked_stop, usage_with_trace])
        tracker = LlmUsageTracker()

        with pytest.raises(GuardrailBlockedError):
            await _tracked(_drain(_client(boto)), tracker)

        assert (tracker.snapshot().input_tokens, tracker.snapshot().complete) == (500, True)

    async def test_abandoned_stream_is_incomplete(self) -> None:
        # The client disconnects mid-generation, before the final usage event exists.
        resume = threading.Event()

        def events():
            yield _TEXT[0]
            resume.wait(5)
            yield _TEXT[1]
            yield _STOP
            yield _metadata(900, 40)

        boto = MagicMock()
        boto.converse_stream.return_value = {"stream": events()}
        tracker = LlmUsageTracker()

        async def read_one_token() -> None:
            stream = _client(boto).converse_stream("q")
            await stream.__anext__()
            await stream.aclose()  # the SSE client disconnected

        await _tracked(read_one_token(), tracker)
        resume.set()
        await asyncio.sleep(0.1)  # the detached worker notices and exits

        snapshot = tracker.snapshot()
        assert snapshot.complete is False
        assert snapshot.input_tokens == 0

    async def test_stream_drained_before_disconnect_keeps_its_usage(self) -> None:
        # If the worker already read the final usage, the spend is known and complete.
        boto = MagicMock()
        boto.converse_stream.return_value = _stream([*_TEXT, _STOP, _metadata(900, 40)])
        tracker = LlmUsageTracker()

        async def read_all_then_close() -> None:
            stream = _client(boto).converse_stream("q")
            await stream.__anext__()
            await asyncio.sleep(0.05)
            await stream.aclose()

        await _tracked(read_all_then_close(), tracker)

        assert (tracker.snapshot().input_tokens, tracker.snapshot().complete) == (900, True)

    async def test_stream_then_converse_fallback_counts_both_calls(self) -> None:
        boto = MagicMock()
        boto.converse_stream.side_effect = _client_error("ModelStreamErrorException", "ConverseStream")
        boto.converse.return_value = _converse_response(1000, 50)
        client = _client(boto)
        tracker = LlmUsageTracker()

        async def stream_then_fallback() -> None:
            try:
                await _drain(client)
            except ClientError:
                await client.converse("q")

        await _tracked(stream_then_fallback(), tracker)

        snapshot = tracker.snapshot()
        assert snapshot.input_tokens == 1000
        assert snapshot.complete is False  # the failed stream may have been billed without usage


# ── GraphRAG (real LLMCache → pinned LlamaIndex BedrockConverse) ──────────────


@pytest.fixture
def graphrag_llm(monkeypatch):
    """Apply the usage patch to BedrockConverse for one test, restoring it after."""
    from coa_serve.lexical import graphrag_patches
    from llama_index.llms.bedrock_converse import BedrockConverse

    # Start from the true originals even if another test already patched the class
    # for the whole process; monkeypatch puts back whatever was there afterwards.
    for name in ("chat", *graphrag_patches._UNACCOUNTED_LLM_METHODS):
        method = getattr(BedrockConverse, name, None)
        if method is not None:
            monkeypatch.setattr(BedrockConverse, name, getattr(method, graphrag_patches._ORIGINAL_ATTR, method))
    monkeypatch.setattr(graphrag_patches, "_llm_usage_patched", False)
    graphrag_patches.patch_llm_usage_accounting()
    assert graphrag_patches.llm_usage_patch_active()

    boto = MagicMock()
    boto.converse.return_value = _converse_response(640, 12)
    llm = BedrockConverse(model=SONNET, region_name="us-east-1", client=boto)
    return SimpleNamespace(llm=llm, boto=boto, patches=graphrag_patches)


def _keyword_call(llm) -> str:
    """GraphRAG's keyword-provider call path: LLMCache.predict -> LLM.predict -> chat."""
    from graphrag_toolkit.lexical_graph.utils import LLMCache
    from llama_index.core.prompts import PromptTemplate

    return LLMCache(llm=llm, enable_cache=False).predict(PromptTemplate("keywords for: {q}"), q="ntsb")


class TestGraphragUsage:
    @staticmethod
    def _snapshot_after_untracked_call_while_tracked_retrieval_is_inflight(
        call: Callable[[], object],
    ) -> LlmUsageSnapshot:
        tracker = LlmUsageTracker()
        tracked_started = threading.Event()
        release_tracked = threading.Event()

        def tracked_worker() -> None:
            with graphrag_retrieval(tracker):
                tracked_started.set()
                assert release_tracked.wait(5)

        with ThreadPoolExecutor(max_workers=1) as pool:
            tracked_future = pool.submit(tracked_worker)
            assert tracked_started.wait(5)
            try:
                with graphrag_retrieval(None):
                    call()
            finally:
                release_tracked.set()
                tracked_future.result()

        return tracker.snapshot()

    def test_graphrag_keyword_call_records_bedrock_usage(self, graphrag_llm) -> None:
        tracker = LlmUsageTracker()

        with bind_tracker(tracker):
            assert _keyword_call(graphrag_llm.llm) == "answer"

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (640, 12, True)

    def test_graphrag_call_in_the_lexical_worker_is_attributed(self, graphrag_llm) -> None:
        tracker = LlmUsageTracker()

        def worker() -> str:  # what BaselineLexicalRetriever._retrieve_sync does
            with graphrag_retrieval(tracker):
                return _keyword_call(graphrag_llm.llm)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(worker).result()

        assert tracker.snapshot().input_tokens == 640

    def test_concurrent_graphrag_requests_do_not_mix(self, graphrag_llm) -> None:
        boto = graphrag_llm.boto
        boto.converse.side_effect = lambda **kwargs: _converse_response(int(_prompt(kwargs).split(": ")[1]), 0)
        trackers = [LlmUsageTracker() for _ in range(4)]

        def worker(index: int) -> None:
            from graphrag_toolkit.lexical_graph.utils import LLMCache
            from llama_index.core.prompts import PromptTemplate

            with graphrag_retrieval(trackers[index]):
                LLMCache(llm=graphrag_llm.llm).predict(PromptTemplate("keywords for: {q}"), q=str((index + 1) * 100))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))

        assert [t.snapshot().input_tokens for t in trackers] == [100, 200, 300, 400]

    def test_unbound_toolkit_thread_marks_inflight_requests_incomplete(self, graphrag_llm) -> None:
        tracker = LlmUsageTracker()

        with graphrag_retrieval(tracker), ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_keyword_call, graphrag_llm.llm).result()  # a toolkit-internal thread

        snapshot = tracker.snapshot()
        assert snapshot.input_tokens == 0
        assert snapshot.complete is False

    def test_untracked_chat_does_not_mark_concurrent_tracked_retrieval_incomplete(self, graphrag_llm) -> None:
        snapshot = self._snapshot_after_untracked_call_while_tracked_retrieval_is_inflight(
            lambda: _keyword_call(graphrag_llm.llm)
        )

        assert snapshot.complete is True

    def test_untracked_unsupported_call_does_not_mark_concurrent_tracked_retrieval_incomplete(
        self, graphrag_llm
    ) -> None:
        from llama_index.core.llms import ChatMessage

        graphrag_llm.boto.converse_stream.return_value = {"stream": iter([])}
        snapshot = self._snapshot_after_untracked_call_while_tracked_retrieval_is_inflight(
            lambda: list(graphrag_llm.llm.stream_chat([ChatMessage(role="user", content="q")]))
        )

        assert snapshot.complete is True

    def test_graphrag_llm_error_is_incomplete(self, graphrag_llm) -> None:
        from graphrag_toolkit.lexical_graph import ModelError

        graphrag_llm.boto.converse.side_effect = _client_error("ThrottlingException")
        tracker = LlmUsageTracker()

        with bind_tracker(tracker), pytest.raises(ModelError):
            _keyword_call(graphrag_llm.llm)

        assert tracker.snapshot().complete is False

    def test_unaccounted_graphrag_entry_point_is_reported(self, graphrag_llm) -> None:
        from llama_index.core.llms import ChatMessage

        graphrag_llm.boto.converse_stream.return_value = {"stream": iter([])}
        tracker = LlmUsageTracker()

        with bind_tracker(tracker):
            list(graphrag_llm.llm.stream_chat([ChatMessage(role="user", content="q")]))

        assert tracker.snapshot().complete is False

    def test_patch_is_idempotent(self, graphrag_llm) -> None:
        tracker = LlmUsageTracker()
        graphrag_llm.patches.patch_llm_usage_accounting()
        graphrag_llm.patches._llm_usage_patched = False  # even a forced re-apply must not double-wrap
        graphrag_llm.patches.patch_llm_usage_accounting()

        with bind_tracker(tracker):
            _keyword_call(graphrag_llm.llm)

        assert tracker.snapshot().input_tokens == 640  # recorded once, not twice

    def test_patch_stays_inactive_when_chat_is_missing(self, monkeypatch) -> None:
        from coa_serve.lexical import graphrag_patches
        from llama_index.llms.bedrock_converse import BedrockConverse

        monkeypatch.setattr(graphrag_patches, "_llm_usage_patched", False)
        monkeypatch.setattr(BedrockConverse, "chat", None)

        graphrag_patches.patch_llm_usage_accounting()

        assert graphrag_patches.llm_usage_patch_active() is False


class TestLexicalRetrieverBinding:
    async def test_retriever_hands_the_request_tracker_to_its_worker(self, graphrag_llm) -> None:
        from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever

        retriever = BaselineLexicalRetriever("neptune-db://x", "aoss://y")
        seen: list = []

        def fake_retrieve(query: str):
            from coa_serve.llm_usage import current_tracker

            seen.append(current_tracker())
            return []

        engine = MagicMock()
        engine.retrieve.side_effect = fake_retrieve
        retriever._engines[(None, "x")] = engine
        retriever._get_engine = lambda namespace, strategy: engine
        tracker = LlmUsageTracker()

        await start_tracked_task(retriever.retrieve("q", "ns"), tracker)

        assert seen == [tracker]
        assert tracker.snapshot().complete is True

    async def test_timed_out_retrieval_still_running_at_snapshot_is_incomplete(self, graphrag_llm) -> None:
        # The retriever gives up at its timeout but the worker keeps going; its keyword
        # LLM call has not even started when the response is assembled.
        from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever

        retriever = BaselineLexicalRetriever("neptune-db://x", "aoss://y", timeout_s=0.05)
        reach_llm, worker_done = threading.Event(), threading.Event()

        def slow_retrieve(query: str):
            reach_llm.wait(5)  # e.g. still in a Neptune query
            _keyword_call(graphrag_llm.llm)  # the LLM call happens after the snapshot
            worker_done.set()
            return []

        engine = MagicMock()
        engine.retrieve.side_effect = slow_retrieve
        retriever._get_engine = lambda namespace, strategy: engine
        tracker = LlmUsageTracker()

        result = await start_tracked_task(retriever.retrieve("q", "ns"), tracker)
        snapshot = tracker.snapshot()  # the response is assembled now
        reach_llm.set()
        await asyncio.to_thread(worker_done.wait, 5)

        assert result.chunks == ()  # the retriever degraded on timeout
        assert snapshot.complete is False
        assert tracker.snapshot() == snapshot  # the late LLM call cannot change it

    async def test_retrieval_queued_behind_a_busy_executor_is_incomplete(self, graphrag_llm) -> None:
        # The worker never started before the snapshot: nothing inside it could register.
        from concurrent.futures import ThreadPoolExecutor

        from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever

        retriever = BaselineLexicalRetriever("neptune-db://x", "aoss://y", timeout_s=0.05)
        retriever._executor = ThreadPoolExecutor(max_workers=1)
        release = threading.Event()
        retriever._executor.submit(release.wait, 5)  # another request holds the only worker
        engine = MagicMock()
        engine.retrieve.return_value = []
        retriever._get_engine = lambda namespace, strategy: engine
        tracker = LlmUsageTracker()

        await start_tracked_task(retriever.retrieve("q", "ns"), tracker)
        snapshot = tracker.snapshot()
        release.set()
        retriever._executor.shutdown(wait=True)

        assert snapshot.complete is False

    async def test_tracked_retrieval_without_the_patch_is_incomplete(self, monkeypatch) -> None:
        from coa_serve.lexical import graphrag_patches
        from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever

        monkeypatch.setattr(graphrag_patches, "_llm_usage_patched", False)
        monkeypatch.setattr(graphrag_patches, "patch_llm_usage_accounting", lambda: None)
        retriever = BaselineLexicalRetriever("neptune-db://x", "aoss://y")
        engine = MagicMock()
        engine.retrieve.return_value = []
        retriever._get_engine = lambda namespace, strategy: engine
        tracker = LlmUsageTracker()

        await start_tracked_task(retriever.retrieve("q", "ns"), tracker)

        assert tracker.snapshot().complete is False


# ── Strands ───────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_strands():
    yield
    uninstall_fake_strands()


def _driver_with_usage(usage: dict | None, *, raise_exc: BaseException | None = None):
    async def driver(tools):
        await _generate_and_execute(tools)
        if usage is not None:
            strands_fake.last_agent.event_loop_metrics = SimpleNamespace(accumulated_usage=usage)
        if raise_exc is not None:
            raise raise_exc

    return driver


_STRANDS_USAGE = {"inputTokens": 5000, "outputTokens": 400}


class TestStrandsUsage:
    async def test_strands_turns_are_recorded_after_a_normal_return(self, fake_strands) -> None:
        agent, _ = _build(model_id=SONNET)
        install_fake_strands(_driver_with_usage({"inputTokens": 5000, "outputTokens": 400, "totalTokens": 5400}))
        tracker = LlmUsageTracker()

        await start_tracked_task(_run(agent), tracker)

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (5000, 400, True)

    async def test_strands_error_keeps_turns_but_is_incomplete(self, fake_strands) -> None:
        agent, _ = _build(model_id=SONNET)
        install_fake_strands(_driver_with_usage(_STRANDS_USAGE, raise_exc=RuntimeError("x")))
        tracker = LlmUsageTracker()

        await start_tracked_task(_run(agent), tracker)

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (5000, 400, False)

    async def test_strands_cancellation_is_incomplete(self, fake_strands) -> None:
        agent, _ = _build(model_id=SONNET)
        install_fake_strands(_driver_with_usage(_STRANDS_USAGE, raise_exc=asyncio.CancelledError()))
        tracker = LlmUsageTracker()

        with pytest.raises(asyncio.CancelledError):
            await start_tracked_task(_run(agent), tracker)

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (5000, 400, False)

    async def test_strands_access_denial_is_incomplete_and_still_raises(self, fake_strands) -> None:
        from coa_serve.exceptions import AccessDeniedError

        agent, _ = _build(model_id=SONNET)
        install_fake_strands(_driver_with_usage(_STRANDS_USAGE, raise_exc=AccessDeniedError("no")))
        tracker = LlmUsageTracker()

        with pytest.raises(AccessDeniedError):
            await start_tracked_task(_run(agent), tracker)

        snapshot = tracker.snapshot()
        assert (snapshot.input_tokens, snapshot.output_tokens, snapshot.complete) == (5000, 400, False)

    async def test_strands_without_exposed_usage_is_incomplete(self, fake_strands) -> None:
        agent, _ = _build(model_id=SONNET)
        install_fake_strands(_driver_with_usage(None))
        tracker = LlmUsageTracker()

        await start_tracked_task(_run(agent), tracker)

        assert tracker.snapshot().complete is False

    async def test_strands_untracked_run_is_unchanged(self, fake_strands) -> None:
        agent, _ = _build(model_id=SONNET)
        install_fake_strands(_driver_with_usage({"inputTokens": 1, "outputTokens": 1}))

        outcome = await _run(agent)

        assert outcome.executed_sql

    def test_strands_model_id_reads_the_real_bedrock_model_default(self) -> None:
        # With no request model, Strands runs ITS default, not BEDROCK_MODEL_ID: read it from the agent.
        from coa_serve.agents.sql_agent import _strands_model_id

        strands_models = pytest.importorskip("strands.models")
        model = strands_models.BedrockModel(region_name="us-east-1")
        agent = SimpleNamespace(model=model)

        assert _strands_model_id(agent, "fallback") == model.config["model_id"]
        assert _strands_model_id(SimpleNamespace(model=None), "fallback") == "fallback"
