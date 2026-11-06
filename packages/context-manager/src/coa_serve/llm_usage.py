# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Request-scoped text-LLM usage accounting, opt-in per request (``options.includeLlmUsage``).

One :class:`LlmUsageTracker` belongs to one request. Every text-LLM provider
boundary (the serve Bedrock client, GraphRAG's LlamaIndex Bedrock LLM, the
Strands agent) follows the same lifecycle:

1. ``register_call`` on the request's context, BEFORE invoking the provider;
2. ``complete_call`` with the provider-reported input/output tokens, or
   ``fail_call`` when the provider returned no usable usage.

Workers never rely on implicit context propagation: direct Bedrock workers close
over the captured tracker, while GraphRAG explicitly re-binds it on the lexical
worker before provider code reads the context variable. ``run_in_executor`` does
not propagate context variables, and a worker keeps running after its awaiting
coroutine is cancelled. A snapshot taken while a call is still pending is
therefore incomplete, as is one with a failed call or a provider path that cannot
be accounted.

Nothing here stores prompts, responses, credentials, or provider request ids.
Model ids are retained because the benchmark must price each model's tokens at
report time; one request can use several differently priced models.
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import threading
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Bedrock reports prompt-cache tokens separately and bills them at their own rates,
# which the price catalogue does not carry. Serve does not enable prompt caching,
# so seeing either field means accounting would be wrong: report it incomplete.
_CACHE_TOKEN_FIELDS = ("cacheReadInputTokens", "cacheWriteInputTokens")

# Incomplete reasons (stable codes, logged on the incomplete snapshot).
REASON_CALL_IN_FLIGHT = "call_in_flight"  # a call or registered work still running
REASON_PROVIDER_ERROR = "provider_error"
REASON_MISSING_USAGE = "missing_usage"
REASON_MALFORMED_USAGE = "malformed_usage"
REASON_MISSING_MODEL_ID = "missing_model_id"
REASON_CACHE_TOKENS = "unpriced_cache_tokens"
REASON_STREAM_ABANDONED = "stream_abandoned"
REASON_AGENT_ABNORMAL_EXIT = "agent_abnormal_exit"
REASON_UNATTRIBUTED_CALL = "unattributed_llm_call"
REASON_UNSUPPORTED_CALL = "unsupported_llm_call"
REASON_USAGE_PATCH_INACTIVE = "usage_patch_inactive"


@dataclass(frozen=True)
class CallHandle:
    """One registered provider call."""

    call_id: int
    stage: str
    model_id: str


@dataclass(frozen=True)
class ModelUsageSnapshot:
    """Provider-reported tokens accumulated for one model."""

    model_id: str
    input_tokens: int
    output_tokens: int
    max_input_tokens_per_call: int


@dataclass(frozen=True)
class LlmUsageSnapshot:
    """The request's accumulated text-LLM usage, grouped for downstream pricing."""

    input_tokens: int
    output_tokens: int
    per_model: tuple[ModelUsageSnapshot, ...]
    complete: bool


class LlmUsageTracker:
    """Thread-safe accumulator for one request's text-LLM calls."""

    def __init__(self) -> None:
        """Start an empty, open accounting window."""
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, CallHandle] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._per_model: dict[str, list[int]] = {}
        self._reasons: set[str] = set()
        self._finalized = False

    def register_call(self, stage: str, model_id: str) -> CallHandle:
        """Record that a provider call is about to start; it stays pending until completed or failed."""
        with self._lock:
            handle = CallHandle(call_id=next(self._ids), stage=stage, model_id=model_id)
            if self._finalized:
                # A call started after the response was assembled is outside this request's
                # accounting window; it cannot change a snapshot that has already been returned.
                logger.debug("llm_usage_call_after_snapshot", stage=stage)
                return handle
            self._pending[handle.call_id] = handle
            return handle

    def complete_call(self, handle: CallHandle, input_tokens: object, output_tokens: object) -> None:
        """Close a pending call with its provider-reported token counts."""
        if (
            isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
        ):
            self.fail_call(handle, REASON_MALFORMED_USAGE)
            return
        with self._lock:
            if not self._close(handle):
                return
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            if not handle.model_id:
                self._reasons.add(REASON_MISSING_MODEL_ID)
                return
            totals = self._per_model.setdefault(handle.model_id, [0, 0, 0])
            totals[0] += input_tokens
            totals[1] += output_tokens
            totals[2] = max(totals[2], input_tokens)

    def fail_call(self, handle: CallHandle, reason: str) -> None:
        """Close a pending call whose usage is unknown; the request is incomplete."""
        with self._lock:
            if self._close(handle):
                self._reasons.add(reason)

    def begin_work(self, stage: str) -> CallHandle:
        """Register work that may make LLM calls the tracker cannot see until they start.

        Used where the calls happen later on another thread, e.g. a GraphRAG retrieval
        submitted to an executor: the handle is pending from submission, so a snapshot
        taken while the work is queued or running — or after its awaiting coroutine
        timed out — is incomplete even before any LLM call has registered.
        """
        return self.register_call(stage, model_id="")

    def end_work(self, handle: CallHandle) -> None:
        """Close a :meth:`begin_work` handle. It carries no tokens of its own."""
        with self._lock:
            self._close(handle)

    def mark_incomplete(self, reason: str) -> None:
        """Mark the request incomplete for spend that cannot be attributed to a call."""
        with self._lock:
            if not self._finalized:
                self._reasons.add(reason)

    def snapshot(self) -> LlmUsageSnapshot:
        """Finalize the request's accounting. Later calls cannot change it."""
        with self._lock:
            self._finalized = True
            reasons = set(self._reasons)
            if self._pending:
                reasons.add(REASON_CALL_IN_FLIGHT)
            snapshot = LlmUsageSnapshot(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                per_model=tuple(
                    ModelUsageSnapshot(
                        model_id=model_id,
                        input_tokens=totals[0],
                        output_tokens=totals[1],
                        max_input_tokens_per_call=totals[2],
                    )
                    for model_id, totals in sorted(self._per_model.items())
                ),
                complete=not reasons,
            )
            pending = len(self._pending)
        if reasons:
            logger.info("llm_usage_incomplete", reasons=sorted(reasons), pending_calls=pending)
        return snapshot

    def _close(self, handle: CallHandle) -> bool:
        """Remove ``handle`` from pending (lock held). False when late or already closed."""
        if self._finalized:
            logger.debug("llm_usage_recorded_after_snapshot", stage=handle.stage)
            return False
        return self._pending.pop(handle.call_id, None) is not None


def record_usage(tracker: LlmUsageTracker, handle: CallHandle, usage: object) -> None:
    """Complete ``handle`` from a Bedrock Converse ``usage`` block, or fail it when unusable."""
    if not isinstance(usage, dict):
        tracker.fail_call(handle, REASON_MISSING_USAGE)
        return
    if any(usage.get(name) for name in _CACHE_TOKEN_FIELDS):
        tracker.fail_call(handle, REASON_CACHE_TOKENS)
        return
    if "inputTokens" not in usage or "outputTokens" not in usage:
        tracker.fail_call(handle, REASON_MISSING_USAGE)
        return
    tracker.complete_call(handle, usage["inputTokens"], usage["outputTokens"])


_ACTIVE: contextvars.ContextVar[LlmUsageTracker | None] = contextvars.ContextVar(
    "coa_serve_llm_usage_tracker", default=None
)
_UNTRACKED_GRAPHRAG_RETRIEVAL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "coa_serve_untracked_graphrag_retrieval", default=False
)


def current_tracker() -> LlmUsageTracker | None:
    """The tracker bound to the running request, or None when the caller did not opt in."""
    return _ACTIVE.get()


def is_untracked_graphrag_retrieval() -> bool:
    """Whether this context deliberately runs GraphRAG without usage accounting."""
    return _UNTRACKED_GRAPHRAG_RETRIEVAL.get()


@contextmanager
def bind_tracker(tracker: LlmUsageTracker | None) -> Iterator[None]:
    """Bind ``tracker`` for the duration of a block, e.g. inside a worker thread."""
    token = _ACTIVE.set(tracker)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def start_tracked_task[T](coro: Coroutine[Any, Any, T], tracker: LlmUsageTracker | None) -> asyncio.Task[T]:
    """Run ``coro`` as a task whose context carries ``tracker``.

    The caller's own context is left untouched, so an async-generator handler never
    sets or resets the variable across a ``yield``.
    """
    context = contextvars.copy_context()
    context.run(_ACTIVE.set, tracker)
    return asyncio.create_task(coro, context=context)


# ── GraphRAG work in flight ────────────────────────────────────────────────────
#
# GraphRAG runs its LlamaIndex LLM on serve's lexical worker, where the tracker is
# bound explicitly, but the toolkit may also call it from its own thread pools,
# where no tracker is bound. Deliberately untracked retrieval is marked separately:
# a text-LLM call seen with neither a tracker nor that marker while tracked GraphRAG
# retrieval is in flight cannot be attributed to one request, so every such request
# is marked incomplete rather than silently undercounted.
_graphrag_lock = threading.Lock()
_graphrag_inflight: dict[int, tuple[LlmUsageTracker, int]] = {}


@contextmanager
def graphrag_retrieval(tracker: LlmUsageTracker | None) -> Iterator[None]:
    """Bind ``tracker`` and register it as having GraphRAG retrieval in flight."""
    if tracker is None:
        marker_token = _UNTRACKED_GRAPHRAG_RETRIEVAL.set(True)
        try:
            with bind_tracker(None):
                yield
        finally:
            _UNTRACKED_GRAPHRAG_RETRIEVAL.reset(marker_token)
        return
    key = id(tracker)
    with _graphrag_lock:
        _, count = _graphrag_inflight.get(key, (tracker, 0))
        _graphrag_inflight[key] = (tracker, count + 1)
    try:
        with bind_tracker(tracker):
            yield
    finally:
        with _graphrag_lock:
            _, count = _graphrag_inflight[key]
            if count <= 1:
                del _graphrag_inflight[key]
            else:
                _graphrag_inflight[key] = (tracker, count - 1)


def note_unattributed_graphrag_call() -> None:
    """Mark every request with GraphRAG retrieval in flight incomplete."""
    with _graphrag_lock:
        trackers = [tracker for tracker, _ in _graphrag_inflight.values()]
    for tracker in trackers:
        tracker.mark_incomplete(REASON_UNATTRIBUTED_CALL)
    if trackers:
        logger.warning("llm_usage_unattributed_graphrag_call", affected_requests=len(trackers))
