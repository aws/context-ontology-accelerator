# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bedrock client — LLMClient implementation.

Provides Converse API (for NL-to-SPARQL and Tier 3 synthesis),
ConverseStream API (for Tier 3 token streaming), and
Bedrock embeddings (for query vectorization before k-NN search) — Cohere Embed
v4 by default (DEFAULT_EMBED_MODEL_ID), via the provider-aware BedrockEmbedder.

Error handling: Protocol methods raise on failure. Only health_check()
is exception-safe and returns a status dict.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from coa_common import BedrockEmbedder, resolve_region
from coa_common.constants import DEFAULT_EMBED_MODEL_ID
from coa_common.guardrail_metrics import (
    COMPONENT_NL_TO_SPARQL,
    DECISION_ALLOW,
    DECISION_ANONYMIZED,
    DECISION_BLOCK,
    DECISION_MODEL_FILTERED,
    DECISION_UNKNOWN,
    assessments_from_trace,
    emit_guardrail_decision,
    filter_type_from_assessments,
)

from ..llm_usage import (
    REASON_MISSING_USAGE,
    REASON_PROVIDER_ERROR,
    REASON_STREAM_ABANDONED,
    current_tracker,
    record_usage,
)
from ..schema_visibility import note_prompt_schema
from .base import (
    STOP_REASON_MAX_TOKENS,
    ContentFilteredError,
    ConverseResult,
    GuardrailBlockedError,
    GuardrailOutcome,
    instrumented,
)

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="bedrock")

# Stop reasons that mean the guardrail acted. Says nothing about WHAT it did —
# Bedrock uses the same value for masking and for blocking.
_GUARDRAIL_STOP_REASONS = ("guardrail", "guardrail_intervened")

# Guardrail policy paths covering all assessment types in guardrail traces.
_GUARDRAIL_POLICY_PATHS = [
    ("topicPolicy", "topics"),
    ("contentPolicy", "filters"),
    ("wordPolicy", "customWords"),
    ("wordPolicy", "managedWordLists"),
    ("sensitiveInformationPolicy", "piiEntities"),
    ("sensitiveInformationPolicy", "regexes"),
]

# Policy keys we can actually parse (derived, so the two can never drift apart).
_KNOWN_POLICY_KEYS = frozenset(policy for policy, _ in _GUARDRAIL_POLICY_PATHS)

# Actions we understand. Anything else — including a future Bedrock action, or
# ``contextualGroundingPolicy``, which we do not parse — must NOT be read as
# permission: an unrecognized shape could be a block we failed to see.
_KNOWN_ACTIONS = frozenset({"BLOCKED", "ANONYMIZED", "NONE"})

# Timeout for queue.get() to prevent deadlock if thread dies without signaling.
_STREAM_QUEUE_TIMEOUT_S = 300

# Model ids observed to reject `inferenceConfig.temperature` with a
# ValidationException (newer Anthropic models deprecated the parameter).
# Process-wide, never invalidated, no lock: CPython's `add`/`in` on a set of str
# is atomic under the GIL, and a lost race just costs one more rejected call.
_MODELS_REJECTING_TEMPERATURE: set[str] = set()

# Model ids observed to reject an effort level (``output_config.effort``); the
# effort option then degrades to the model's default.
_MODELS_REJECTING_EFFORT: set[str] = set()

# Model ids observed to reject `inferenceConfig.topP` with a ValidationException
# (the same models deprecated `top_p` alongside `temperature`).
_MODELS_REJECTING_TOP_P: set[str] = set()


def _assessment_has_action(assessment: dict, action: str) -> bool:
    """Whether any policy item in one assessment carries ``action``."""
    try:
        return any(
            item.get("action") == action
            for policy_key, items_key in _GUARDRAIL_POLICY_PATHS
            for item in assessment.get(policy_key, {}).get(items_key, [])
        )
    except (TypeError, AttributeError):
        logger.warning("guardrail_malformed_assessment", assessment_keys=list(assessment.keys()) if assessment else [])
        return False


def _carries_guardrail_action(value: object) -> bool:
    """Whether a sub-tree carries an ``action`` field anywhere within it.

    Only guardrail *policies* carry actions; inert metadata such as
    ``invocationMetrics`` and ``appliedGuardrailDetails`` does not. This is the
    line between an unparsed *policy* — which could hide a block we never saw —
    and a benign metadata key we can safely ignore.
    """
    if isinstance(value, dict):
        return "action" in value or any(_carries_guardrail_action(v) for v in value.values())
    if isinstance(value, list):
        return any(_carries_guardrail_action(v) for v in value)
    return False


def _assessment_is_unparsed(assessment: dict) -> bool:
    """Whether one assessment holds a shape we cannot classify.

    Fails closed on:

    * a known policy whose items are not a list, whose item is not a dict, or
      whose action is outside :data:`_KNOWN_ACTIONS`;
    * an *unknown* key that carries a guardrail action — a policy Bedrock added
      that we do not parse (e.g. ``contextualGroundingPolicy``), which could be a
      block we never saw.

    Unknown keys that carry NO action are benign metadata and are ignored:
    Bedrock always attaches ``appliedGuardrailDetails`` (and
    ``invocationMetrics``) to each assessment, and keying off the presence of an
    action survives Bedrock adding new metadata without failing open.
    """
    for policy_key, items_key in _GUARDRAIL_POLICY_PATHS:
        try:
            items = assessment.get(policy_key, {}).get(items_key, []) or []
        except (TypeError, AttributeError):
            return True
        if not isinstance(items, list):
            return True
        for item in items:
            if not isinstance(item, dict) or item.get("action") not in _KNOWN_ACTIONS:
                return True
    for key, value in assessment.items():
        if key in _KNOWN_POLICY_KEYS:
            continue
        if _carries_guardrail_action(value):
            return True
    return False


def _trace_has_unparsed_shape(guardrail_trace: dict) -> bool:
    """Whether the trace contains anything this code cannot interpret.

    Presence of an unparsed shape means we may have MISSED a block, so it must
    downgrade the verdict to UNKNOWN rather than be ignored.

    Deliberately walks the RAW trace rather than :func:`assessments_from_trace`,
    which silently drops non-dict sides and entries, so a readable ANONYMIZED
    beside an unreadable sibling is not reported as plain masking.

    Only assessment *sides* are inspected. Other top-level trace keys are metadata
    that carry no action, and treating them as unparsed would suppress every
    guarded response. Both nesting shapes Bedrock uses are handled: ``inputAssessment``
    maps a guardrail id to a single assessment, while ``outputAssessments`` (plural)
    maps it to a LIST of them.
    """
    if not isinstance(guardrail_trace, dict):
        return True
    for side_key, side in guardrail_trace.items():
        if "assessment" not in side_key.lower():
            continue
        if side is None:
            # A null side (Bedrock leaves ``outputAssessments`` null when the
            # output was not assessed) carries no action — nothing to interpret.
            continue
        if isinstance(side, dict):
            entries = list(side.values())
        elif isinstance(side, list):
            entries = list(side)
        else:
            return True
        for entry in entries:
            # A side value is one assessment, or (outputAssessments) a list of them.
            assessments = entry if isinstance(entry, list) else [entry]
            for assessment in assessments:
                if not isinstance(assessment, dict) or _assessment_is_unparsed(assessment):
                    return True
    return False


def _classify_guardrail(stop_reason: str, guardrail_trace: dict) -> GuardrailOutcome:
    """Classify what the guardrail did, failing closed on anything unreadable.

    Precedence — an explicit block wins; then anything we could not parse; only
    then may we conclude the intervention was harmless masking:

    1. ``BLOCKED`` in either assessment            -> BLOCKED
    2. any policy/action we cannot interpret       -> UNKNOWN
    3. ``ANONYMIZED`` in either assessment         -> ANONYMIZED
    4. the stop reason says the guardrail acted    -> UNKNOWN
    5. otherwise                                   -> NONE

    Rules 2 and 4 are the safety property. Rule 2 specifically covers a MIXED
    trace: a recognized ``ANONYMIZED`` alongside an unrecognized policy shape must
    not be reported as mere masking, because the shape we could not read might
    have been a block. Ordering anonymize ahead of that check would fail OPEN.

    A missing trace, an empty trace, and a malformed trace all land on UNKNOWN via
    rule 4, so NONE requires a non-intervened stop reason and can never be reached
    by failing to parse.
    """
    assessments = assessments_from_trace(guardrail_trace)
    if any(_assessment_has_action(a, "BLOCKED") for a in assessments):
        return GuardrailOutcome.BLOCKED
    if _trace_has_unparsed_shape(guardrail_trace):
        return GuardrailOutcome.UNKNOWN
    if any(_assessment_has_action(a, "ANONYMIZED") for a in assessments):
        return GuardrailOutcome.ANONYMIZED
    if stop_reason in _GUARDRAIL_STOP_REASONS:
        return GuardrailOutcome.UNKNOWN
    return GuardrailOutcome.NONE


def _redacted_assessment_actions(guardrail_trace: dict) -> list[str]:
    """Summarize a trace as ``policy:type=action`` strings, dropping matched values.

    Guardrail traces carry the RAW matched text (``{"type": "NAME", "action":
    "ANONYMIZED", "match": "<a real person's name>"}``). Logging the trace object
    would therefore write plaintext PII to CloudWatch.
    """
    out: list[str] = []
    for assessment in assessments_from_trace(guardrail_trace):
        for policy_key, items_key in _GUARDRAIL_POLICY_PATHS:
            try:
                items = assessment.get(policy_key, {}).get(items_key, []) or []
            except (TypeError, AttributeError):
                continue
            for item in items:
                if isinstance(item, dict):
                    out.append(f"{policy_key}:{item.get('type')}={item.get('action')}")
    return out


def _decision_for(outcome: GuardrailOutcome) -> str:
    """Map an outcome onto its metrics ``Decision`` label."""
    return {
        GuardrailOutcome.NONE: DECISION_ALLOW,
        GuardrailOutcome.ANONYMIZED: DECISION_ANONYMIZED,
        GuardrailOutcome.BLOCKED: DECISION_BLOCK,
        GuardrailOutcome.UNKNOWN: DECISION_UNKNOWN,
    }[outcome]


def _close_stream_quietly(stream: Any) -> None:
    """Close a boto3 EventStream, ignoring absence and close errors.

    Closing the underlying HTTP response is the only way to unblock a worker thread
    parked in ``for event in stream``: a thread-pool thread has no cancellation
    point. Best-effort by design — this runs during generator finalization, where
    raising would mask the original outcome.
    """
    if stream is None:
        return
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - finalization must not raise
        logger.debug("bedrock_stream_close_failed", error=type(exc).__name__, error_msg=str(exc))


def _log_detached_worker_result(task: Any) -> None:
    """Surface a detached stream worker's failure instead of discarding it.

    Attached as a done-callback once the generator stops awaiting the worker, so an
    exception raised after detachment is still visible rather than swallowed by the
    unretrieved-result warning.
    """
    try:
        task.result()
    except asyncio.CancelledError:  # pragma: no cover - normal on shutdown
        pass
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        logger.debug("bedrock_stream_worker_after_detach", error=type(exc).__name__, error_msg=str(exc))


class BedrockLLMClient:
    """Bedrock Converse API + embeddings client (Cohere Embed v4 by default)."""

    def __init__(
        self,
        model_id: str | None = None,
        embed_model_id: str | None = None,
        region: str | None = None,
        guardrail_version: str | None = None,
    ):
        """Configure the client's model, embedder, region, and guardrail version.

        Args:
            model_id: Converse model id. Defaults to the ``BEDROCK_MODEL_ID`` env
                var, then a built-in Claude Sonnet default.
            embed_model_id: Embedding model id. Defaults to ``BEDROCK_EMBED_MODEL_ID``
                then ``DEFAULT_EMBED_MODEL_ID``.
            region: AWS region. Defaults to ``BEDROCK_REGION`` then the resolved region.
            guardrail_version: Guardrail version to apply. Defaults to
                ``BEDROCK_GUARDRAIL_VERSION`` then ``"DRAFT"``.
        """
        self._model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5")
        self._embed_model_id = embed_model_id or os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID)
        self._region = region or os.environ.get("BEDROCK_REGION", resolve_region())
        # Query embeddings go through the shared embedder so serve uses the SAME
        # model (and Cohere search_query input_type) as ingestion.
        self._embedder = BedrockEmbedder(model_id=self._embed_model_id, region=self._region)
        self._guardrail_version = guardrail_version or os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
        self._client: Any = None
        self._client_lock = threading.Lock()
        logger.info(
            "bedrock_client_configured",
            model_id=self._model_id,
            embed_model_id=self._embed_model_id,
            region=self._region,
        )

    @property
    def model_id(self) -> str:
        """Return the default Converse model id for this client."""
        return self._model_id

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = boto3.client(
                        "bedrock-runtime",
                        region_name=self._region,
                        config=BotoConfig(
                            read_timeout=int(os.environ.get("BEDROCK_READ_TIMEOUT", "120")),
                            connect_timeout=int(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "10")),
                            retries={"max_attempts": 2},
                        ),
                    )
        return self._client

    async def close(self) -> None:
        """Close the underlying boto3 client and release its connection pool."""
        if self._client is not None:
            self._client.close()
            self._client = None

    @instrumented("bedrock")
    async def converse(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        top_p: float | None = None,
        guard_content: str | None = None,
        model_id: str | None = None,
        effort: str | None = None,
    ) -> ConverseResult:
        """Send a single-turn prompt via the Bedrock Converse API.

        Parses the guardrail trace to distinguish a true content BLOCK from a
        PII anonymize/mask action, reflecting the outcome in the result.

        Args:
            prompt: The main prompt text (context plus question, or just context).
            system: Optional system instruction text.
            guardrail_id: Optional Bedrock guardrail identifier to apply.
            max_tokens: Maximum output tokens.
            temperature: Sampling temperature; omitted when None.
            top_p: Nucleus-sampling cutoff; omitted when None. Determinism-sensitive
                callers pass top_p=0 alongside temperature=0 so a surviving
                sampling constraint remains even if temperature is dropped for the
                model (see _call_with_temperature_fallback).
            guard_content: Text wrapped in a guardContent block for prompt-attack
                evaluation, leaving the main prompt unchecked.
            model_id: Per-call model override; defaults to the client's model.
            effort: Model effort level, sent as ``output_config.effort`` in
                ``additionalModelRequestFields``; omitted when None.

        Returns:
            A ConverseResult with the generated text and whether a guardrail blocked it.

        Raises:
            ValueError: If the response contains no text content.
        """
        kwargs = self._build_converse_kwargs(
            prompt,
            system=system,
            guardrail_id=guardrail_id,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            guard_content=guard_content,
            model_id=model_id,
        )
        if effort and (model_id or self._model_id) not in _MODELS_REJECTING_EFFORT:
            kwargs["additionalModelRequestFields"] = {"output_config": {"effort": effort}}

        client = self._get_client()
        loop = asyncio.get_running_loop()
        # Opt-in LLM usage accounting: register on the request's context, record in the
        # worker through the captured tracker — the worker finishes (and Bedrock bills)
        # even when this await is cancelled by a deadline.
        tracker = current_tracker()
        handle = tracker.register_call("converse", kwargs["modelId"]) if tracker is not None else None

        def _call() -> Any:
            try:
                response = self._call_with_temperature_fallback(client.converse, kwargs)
            except BaseException:
                if handle is not None:
                    tracker.fail_call(handle, REASON_PROVIDER_ERROR)
                raise
            if handle is not None:
                record_usage(tracker, handle, response.get("usage"))
            return response

        start = time.monotonic()
        response = await loop.run_in_executor(_EXECUTOR, _call)
        latency_ms = (time.monotonic() - start) * 1000

        stop_reason = response.get("stopReason", "")
        guardrail_trace: dict[str, Any] = {}

        # Classify what the guardrail DID. stop_reason alone is insufficient — it
        # fires for masking and blocking alike — so the trace is requested (see
        # _build_converse_kwargs) and read across BOTH assessments.
        if stop_reason in _GUARDRAIL_STOP_REASONS:
            guardrail_trace = response.get("trace", {}).get("guardrail", {})
        outcome = _classify_guardrail(stop_reason, guardrail_trace)
        blocked = outcome is GuardrailOutcome.BLOCKED

        if outcome is not GuardrailOutcome.NONE:
            # NEVER log the trace object (see _redacted_assessment_actions).
            logger.info(
                "guardrail_trace",
                outcome=outcome.value,
                blocked=blocked,
                stop_reason=stop_reason,
                actions=_redacted_assessment_actions(guardrail_trace),
            )

        # Emitted before the no-text check below: a hard block is the decision we
        # most need counted, and it is also the case most likely to return no
        # text. Only when a guardrail was actually applied — counting unguarded
        # calls as ALLOW would make the block rate meaningless.
        #
        # ``blocked`` drives GuardrailBlocked and is therefore True only for a
        # CONFIRMED block; UNKNOWN is suppressed but reported under its own
        # decision so an unexplained suppression can be alarmed on separately.
        if guardrail_id:
            model_filtered = stop_reason == "content_filtered"
            emit_guardrail_decision(
                component=COMPONENT_NL_TO_SPARQL,
                blocked=blocked,
                latency_ms=latency_ms,
                filter_type=filter_type_from_assessments(assessments_from_trace(guardrail_trace)),
                # Serve already emits its other custom metrics as stdout EMF —
                # same transport here, so no PutMetricData grant is needed.
                transport="emf",
                decision=DECISION_MODEL_FILTERED if model_filtered else _decision_for(outcome),
            )

        content_blocks = response.get("output", {}).get("message", {}).get("content", [])
        text_blocks = [b["text"] for b in content_blocks if isinstance(b, dict) and "text" in b]
        if not text_blocks:
            if stop_reason == "content_filtered":
                # The MODEL refused, which is not a guardrail policy action. Typed so
                # callers and dashboards can tell the two apart; a ValueError subclass
                # so anything that caught the old bare ValueError still does.
                raise ContentFilteredError(
                    f"Model filtered its own output: stopReason={stop_reason}",
                    stop_reason=stop_reason,
                )
            raise ValueError(f"No text content in Bedrock response: stopReason={stop_reason}")

        # Join ALL text blocks: the Converse API may split a response across several
        # content blocks.
        text = "".join(text_blocks)

        # UNKNOWN means we could not classify the intervention, so the text may be
        # real model output that a policy would have blocked. Suppressing the flag
        # while returning the content would fail OPEN in the text channel — callers
        # such as tier2's sql_generator read .text without consulting the flag. On a
        # confirmed BLOCK no suppression is needed: Bedrock has already substituted
        # its blockedOutputsMessaging, so .text is boilerplate, not model content.
        if outcome is GuardrailOutcome.UNKNOWN:
            text = ""

        # Truncation is a 200 with partial content, so nothing downstream fails
        # loudly — log it here, once, where the cap that caused it is in scope.
        # Reasoning models spend the SAME budget on their thinking blocks, so a
        # cap that looks generous for the answer alone can still cut it off.
        if stop_reason == STOP_REASON_MAX_TOKENS:
            logger.warning(
                "bedrock_output_truncated",
                model=kwargs["modelId"],
                max_tokens=max_tokens,
                text_chars=len(text),
            )

        return ConverseResult(text=text, outcome=outcome, stop_reason=stop_reason)

    async def converse_stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text tokens from Bedrock ConverseStream API.

        Yields text fragments as they arrive. Raises GuardrailBlockedError
        if the guardrail intervenes mid-stream with a BLOCK action.

        Uses a thread pool executor to iterate boto3's synchronous EventStream,
        bridging to async via loop.call_soon_threadsafe + asyncio.Queue.
        """
        kwargs = self._build_converse_kwargs(
            prompt,
            system=system,
            guardrail_id=guardrail_id,
            max_tokens=max_tokens,
            guard_content=guard_content,
            model_id=model_id,
        )

        # Enable sync guardrail processing so intervention is immediate (not end-of-stream)
        if guardrail_id and "guardrailConfig" in kwargs:
            kwargs["guardrailConfig"]["streamProcessingMode"] = "sync"

        queue: asyncio.Queue[str | None | Exception | tuple] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        # Shared state between the event loop and the worker thread.
        #
        # ``stream`` is published by the worker as soon as the EventStream exists so
        # this side can close it — closing is what unblocks the worker's synchronous
        # `for event in ...`, since a thread-pool thread has no cancellation point.
        #
        # ``abandoned`` covers the window BEFORE that: the ConverseStream API call
        # itself takes time, and a client that disconnects during it would find
        # nothing to close, leaving the worker to drain the whole response (holding
        # an executor slot and billing tokens) for a caller that is already gone.
        # The worker checks the flag right after the call returns, and again on each
        # event, so it exits at the first opportunity either way.
        stream_holder: dict[str, Any] = {}
        abandoned = threading.Event()
        # Opt-in LLM usage accounting (see converse()). The final usage arrives in the
        # stream's metadata event, so the worker records it; a stream closed early or
        # ending without usage leaves the request incomplete, never zero-cost.
        tracker = current_tracker()
        handle = tracker.register_call("converse_stream", kwargs["modelId"]) if tracker is not None else None
        usage_seen = False

        def _fail_usage(reason: str) -> None:
            if handle is not None and not usage_seen:
                tracker.fail_call(handle, reason)

        def _run_stream() -> None:
            """Run in thread — iterate EventStream, push text chunks to queue."""
            nonlocal usage_seen
            guardrail_triggered = False
            guardrail_trace: dict[str, Any] = {}
            try:
                client = self._get_client()
                response = self._call_with_temperature_fallback(client.converse_stream, kwargs)
                stream_holder["stream"] = response["stream"]
                if abandoned.is_set():
                    # Disconnected while the API call was in flight — nothing was
                    # closeable at that point, so honour the request here.
                    _close_stream_quietly(response["stream"])
                    _fail_usage(REASON_STREAM_ABANDONED)
                    return
                for event in response["stream"]:
                    if abandoned.is_set():
                        _close_stream_quietly(response["stream"])
                        _fail_usage(REASON_STREAM_ABANDONED)
                        return
                    if "contentBlockDelta" in event:
                        text = event["contentBlockDelta"].get("delta", {}).get("text", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, text)
                    elif "messageStop" in event:
                        stop_reason = event["messageStop"].get("stopReason", "")
                        if stop_reason in _GUARDRAIL_STOP_REASONS:
                            guardrail_triggered = True
                        elif stop_reason == STOP_REASON_MAX_TOKENS:
                            # The consumer sees a stream that simply ends, so the cap
                            # has to be reported here or not at all.
                            logger.warning(
                                "bedrock_output_truncated",
                                model=kwargs["modelId"],
                                max_tokens=max_tokens,
                                streaming=True,
                            )
                    elif "metadata" in event:
                        trace = event["metadata"].get("trace", {}).get("guardrail", {})
                        if trace:
                            guardrail_trace = trace
                        if handle is not None and not usage_seen:
                            usage_seen = True
                            record_usage(tracker, handle, event["metadata"].get("usage"))
                    # Safely ignore: messageStart, contentBlockStart, contentBlockStop
                # Classified rather than a bare bool so a traceless intervention is
                # UNKNOWN, not allowed.
                if guardrail_triggered:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        (
                            "_guardrail_result",
                            _classify_guardrail("guardrail_intervened", guardrail_trace),
                        ),
                    )
                _fail_usage(REASON_MISSING_USAGE)
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as e:
                _fail_usage(REASON_PROVIDER_ERROR)
                loop.call_soon_threadsafe(queue.put_nowait, e)

        task = loop.run_in_executor(_EXECUTOR, _run_stream)

        guardrail_outcome = GuardrailOutcome.NONE
        pending_exception: Exception | None = None
        stream_start = time.monotonic()
        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=_STREAM_QUEUE_TIMEOUT_S)
                if item is None:
                    break
                if isinstance(item, Exception):
                    pending_exception = item
                    break
                if isinstance(item, tuple) and item[0] == "_guardrail_result":
                    guardrail_outcome = item[1]
                    continue
                yield item
        except TimeoutError:
            pending_exception = TimeoutError("Bedrock stream timed out waiting for next token")
        finally:
            # Do NOT await the worker here: it iterates boto3's synchronous
            # EventStream in a thread with no cancellation point, and on early
            # generator close (GeneratorExit thrown at the `yield`) awaiting in this
            # `finally` makes CPython raise "async generator ignored GeneratorExit".
            # Signal the worker to stop, close the stream to unblock it, and detach
            # it with a done-callback that surfaces any error (see ``abandoned``).
            abandoned.set()
            _close_stream_quietly(stream_holder.get("stream"))
            task.add_done_callback(_log_detached_worker_result)

        if pending_exception:
            raise pending_exception

        if guardrail_id:
            emit_guardrail_decision(
                component=COMPONENT_NL_TO_SPARQL,
                blocked=guardrail_outcome is GuardrailOutcome.BLOCKED,
                latency_ms=(time.monotonic() - stream_start) * 1000,
                transport="emf",
                decision=_decision_for(guardrail_outcome),
            )

        # Known limitation: tokens are yielded as they arrive, so raising here cannot
        # recall content already sent. ANONYMIZE is not a block (the tokens already
        # carry the masked values), so only BLOCKED and UNKNOWN raise; an
        # intervention we cannot explain must not be treated as permission.
        if guardrail_outcome in (GuardrailOutcome.BLOCKED, GuardrailOutcome.UNKNOWN):
            raise GuardrailBlockedError(f"guardrail_{guardrail_outcome.value}")

    @staticmethod
    def _call_with_temperature_fallback(call_fn: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        """Invoke call_fn(**kwargs), retrying without a sampling param on ValidationException.

        Some newer models (e.g. claude-sonnet-5, Opus 4.8+) deprecated BOTH
        ``temperature`` and ``top_p`` and reject either with a ValidationException.
        Each rejection is remembered per model id (``_MODELS_REJECTING_TEMPERATURE``
        / ``_MODELS_REJECTING_TOP_P``; an effort level the model rejects, likewise in
        ``_MODELS_REJECTING_EFFORT``), so only the FIRST call to such a model pays
        the failed request + retry; later calls omit the offending param up front.

        NL→SPARQL sets ``topP=0`` for determinism, so dropping only ``temperature``
        is insufficient on these models — the retry would still carry ``topP`` and
        fail again, taking the whole SPARQL path down. Both params are handled
        symmetrically, and a single call can shed both (one learned up front, the
        other on the retry).
        """
        model_id = kwargs.get("modelId")
        inference = kwargs.get("inferenceConfig", {})

        # Pre-strip any params this model is already known to reject, so a warm
        # model pays neither a doomed request nor a retry. Emit observability on
        # the pre-learned path too (not one-per-process) so dashboards see every
        # affected call — a determinism-sensitive caller running UNCONSTRAINED
        # (temperature=0 / topP=0 requested, never sent) is otherwise invisible.
        if "inferenceConfig" in kwargs:
            if model_id in _MODELS_REJECTING_TEMPERATURE:
                dropped = inference.pop("temperature", None)
                if dropped is not None:
                    logger.warning(
                        "temperature_dropped_for_model",
                        model=model_id,
                        had_top_p="topP" in inference,
                        learned=True,
                    )
            if model_id in _MODELS_REJECTING_TOP_P:
                dropped = inference.pop("topP", None)
                if dropped is not None:
                    logger.warning(
                        "top_p_dropped_for_model",
                        model=model_id,
                        learned=True,
                    )

        # Retry loop: at most three learnings (effort, temperature, topP). Each
        # ValidationException naming a deprecated param is
        # learned, the param stripped, and the call retried; any other error, or
        # running out of strippable params, re-raises.
        for _ in range(4):
            try:
                return call_fn(**kwargs)
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") != "ValidationException":
                    raise
                msg = str(e)
                extra = kwargs.get("additionalModelRequestFields") or {}
                if "output_config" in extra and ("effort" in msg or "output_config" in msg):
                    kwargs.pop("additionalModelRequestFields", None)
                    if model_id is not None:
                        _MODELS_REJECTING_EFFORT.add(model_id)
                    logger.warning("retry_without_effort", model=model_id)
                    continue
                if "inferenceConfig" not in kwargs:
                    raise
                if "temperature" in msg and "temperature" in inference:
                    inference.pop("temperature", None)
                    if model_id is not None:
                        _MODELS_REJECTING_TEMPERATURE.add(model_id)
                    logger.warning(
                        "retry_without_temperature",
                        model=model_id,
                        had_top_p="topP" in inference,
                    )
                    continue
                if "top_p" in msg and "topP" in inference:
                    inference.pop("topP", None)
                    if model_id is not None:
                        _MODELS_REJECTING_TOP_P.add(model_id)
                    logger.warning(
                        "retry_without_top_p",
                        model=model_id,
                        had_temperature="temperature" in inference,
                    )
                    continue
                raise
        # Unreachable: at most three params can be shed, so the loop returns or re-raises.
        raise RuntimeError("converse retry loop exhausted without returning or raising")

    def _build_converse_kwargs(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        top_p: float | None = None,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Build kwargs dict shared between converse() and converse_stream()."""
        effective_model_id = model_id or self._model_id
        if model_id and model_id != self._model_id:
            logger.info("using_model_override", override=model_id, default=self._model_id)
        # Every prompt the model sees passes here; for a caller without viewSource
        # the schema names in it are recorded so the response scrub can redact them
        # where the model could have copied them into a constant (no-op otherwise).
        note_prompt_schema(prompt, system, guard_content)
        content: list[dict[str, Any]] = []
        if prompt:
            content.append({"text": prompt})
        if guard_content:
            # guardContent tells the guardrail to evaluate only this block for
            # prompt attacks. Callers that use it as their sole channel for untrusted
            # user text depend on the text reaching the model either way, so with no
            # guardrail it is sent as an ordinary text block: Converse rejects a
            # guardContent block without a guardrailConfig ("The guardrail
            # configuration is missing.", ValidationException).
            if guardrail_id:
                content.append({"guardContent": {"text": {"text": guard_content}}})
            else:
                content.append({"text": guard_content})

        kwargs: dict[str, Any] = {
            "modelId": effective_model_id,
            "messages": [{"role": "user", "content": content}],
            "inferenceConfig": {
                "maxTokens": max_tokens,
                **({"temperature": temperature} if temperature is not None else {}),
                **({"topP": top_p} if top_p is not None else {}),
            },
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if guardrail_id:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": guardrail_id,
                "guardrailVersion": self._guardrail_version,
                # Required, not diagnostic: Bedrock returns the guardrail trace only
                # when asked, and without it an intervention is unclassifiable.
                # Matches libs/common/src/coa_common/bedrock.py.
                "trace": "enabled",
            }
        return kwargs

    @instrumented("bedrock")
    async def embed(self, text: str) -> list[float]:
        """Embed query text into a vector using the shared search-query embedder."""
        # Serve embeds search QUERIES — use search_query input_type (Cohere) via
        # the shared embedder so vectors match the search_document embeddings
        # written at ingestion time.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self._embedder.embed_query, text)

    async def health_check(self) -> dict[str, Any]:
        """Probe Bedrock reachability via a test embedding call.

        Returns:
            A status dict: ``{"status": "ok", ...}`` on success, otherwise
            ``{"status": "error", ...}``. This method never raises.
        """
        try:
            await self.embed("health")
            return {"status": "ok", "model": self._model_id}
        except Exception:
            return {"status": "error", "detail": "Health check failed"}
