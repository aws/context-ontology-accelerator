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
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from coa_common import BedrockEmbedder, resolve_region
from coa_common.constants import DEFAULT_EMBED_MODEL_ID

from .base import ConverseResult, GuardrailBlockedError, instrumented

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="bedrock")

# Guardrail policy paths covering all assessment types in outputAssessment traces.
_GUARDRAIL_POLICY_PATHS = [
    ("topicPolicy", "topics"),
    ("contentPolicy", "filters"),
    ("wordPolicy", "customWords"),
    ("wordPolicy", "managedWordLists"),
    ("sensitiveInformationPolicy", "piiEntities"),
    ("sensitiveInformationPolicy", "regexes"),
]

# Timeout for queue.get() to prevent deadlock if thread dies without signaling.
_STREAM_QUEUE_TIMEOUT_S = 300


def _assessment_has_block(assessment: dict) -> bool:
    """Check if a guardrail assessment contains any BLOCKED action."""
    try:
        return any(
            item.get("action") == "BLOCKED"
            for policy_key, items_key in _GUARDRAIL_POLICY_PATHS
            for item in assessment.get(policy_key, {}).get(items_key, [])
        )
    except (TypeError, AttributeError):
        logger.warning("guardrail_malformed_assessment", assessment_keys=list(assessment.keys()) if assessment else [])
        return False


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
                # Double-check after acquiring lock
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
        guard_content: str | None = None,
        model_id: str | None = None,
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
            guard_content: Text wrapped in a guardContent block for prompt-attack
                evaluation, leaving the main prompt unchecked.
            model_id: Per-call model override; defaults to the client's model.

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
            guard_content=guard_content,
            model_id=model_id,
        )

        client = self._get_client()
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            _EXECUTOR, lambda: self._call_with_temperature_fallback(client.converse, kwargs)
        )

        stop_reason = response.get("stopReason", "")
        content_blocks = response.get("output", {}).get("message", {}).get("content", [])
        text_blocks = [b["text"] for b in content_blocks if isinstance(b, dict) and "text" in b]
        if not text_blocks:
            raise ValueError(f"No text content in Bedrock response: stopReason={stop_reason}")

        # Join ALL text blocks. The Converse API may split a response across several
        # content blocks; taking only the first silently truncated the generation
        # (and for NL→SQL / NL→SPARQL that means a query cut off mid-statement).
        text = "".join(text_blocks)
        blocked = False

        # Parse guardrail trace to distinguish BLOCK from ANONYMIZE.
        # stop_reason alone is insufficient — it fires for both actions.
        if stop_reason in ("guardrail", "guardrail_intervened"):
            guardrail_trace = response.get("trace", {}).get("guardrail", {})
            if guardrail_trace:
                blocked = any(_assessment_has_block(a) for a in guardrail_trace.get("outputAssessment", {}).values())
                logger.info(
                    "guardrail_trace",
                    blocked=blocked,
                    stop_reason=stop_reason,
                    trace=guardrail_trace,
                )
            else:
                # No trace available — conservatively treat as blocked
                blocked = True

        return ConverseResult(text=text, guardrail_blocked=blocked)

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

        def _run_stream() -> None:
            """Run in thread — iterate EventStream, push text chunks to queue."""
            guardrail_triggered = False
            was_blocked = False
            try:
                client = self._get_client()
                response = self._call_with_temperature_fallback(client.converse_stream, kwargs)
                stream_holder["stream"] = response["stream"]
                if abandoned.is_set():
                    # Disconnected while the API call was in flight — nothing was
                    # closeable at that point, so honour the request here.
                    _close_stream_quietly(response["stream"])
                    return
                for event in response["stream"]:
                    if abandoned.is_set():
                        _close_stream_quietly(response["stream"])
                        return
                    if "contentBlockDelta" in event:
                        text = event["contentBlockDelta"].get("delta", {}).get("text", "")
                        if text:
                            loop.call_soon_threadsafe(queue.put_nowait, text)
                    elif "messageStop" in event:
                        stop_reason = event["messageStop"].get("stopReason", "")
                        if stop_reason in ("guardrail", "guardrail_intervened"):
                            guardrail_triggered = True
                    elif "metadata" in event:
                        trace = event["metadata"].get("trace", {}).get("guardrail", {})
                        if trace:
                            was_blocked = any(
                                _assessment_has_block(a) for a in trace.get("outputAssessment", {}).values()
                            )
                    # Safely ignore: messageStart, contentBlockStart, contentBlockStop
                # Signal guardrail result if triggered
                if guardrail_triggered:
                    loop.call_soon_threadsafe(
                        queue.put_nowait,
                        ("_guardrail_result", was_blocked),
                    )
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, e)

        task = loop.run_in_executor(_EXECUTOR, _run_stream)

        guardrail_intervened = False
        guardrail_was_blocked = False
        pending_exception: Exception | None = None
        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=_STREAM_QUEUE_TIMEOUT_S)
                if item is None:
                    break
                if isinstance(item, Exception):
                    pending_exception = item
                    break
                if isinstance(item, tuple) and item[0] == "_guardrail_result":
                    guardrail_intervened = True
                    guardrail_was_blocked = item[1]
                    continue
                yield item
        except TimeoutError:
            pending_exception = TimeoutError("Bedrock stream timed out waiting for next token")
        finally:
            # Do NOT await the worker here.
            #
            # The worker iterates boto3's SYNCHRONOUS EventStream in a thread-pool
            # thread and has no cancellation point, so awaiting it blocked for up to
            # BEDROCK_READ_TIMEOUT (120s) more — which defeated the 300s idle-timeout
            # guard above: it fired, then this line waited again.
            #
            # Worse, on early generator close (the SSE consumer in main.py cancelling
            # resolve_task when the client disconnects) `GeneratorExit` is thrown in
            # at the `yield`, and suspending on an await inside the resulting
            # `finally` makes CPython raise "async generator ignored GeneratorExit".
            #
            # Instead: signal the worker to stop, close the stream from this side to
            # unblock it, then detach it with a done-callback that surfaces any
            # error. The generator finalizes synchronously, and the worker cannot
            # keep consuming the Bedrock stream (and billing tokens) after the
            # client is gone. Setting the flag as well as closing covers the window
            # where the API call has not returned yet, so there is no stream to
            # close — see the comment on ``abandoned``.
            abandoned.set()
            _close_stream_quietly(stream_holder.get("stream"))
            task.add_done_callback(_log_detached_worker_result)

        if pending_exception:
            raise pending_exception

        # After stream completes, raise only if guardrail actually BLOCKED content.
        # PII ANONYMIZE (masking) is not a block — the tokens already contain masked values.
        if guardrail_intervened and guardrail_was_blocked:
            raise GuardrailBlockedError("guardrail_blocked")

    @staticmethod
    def _call_with_temperature_fallback(call_fn: Callable[..., Any], kwargs: dict[str, Any]) -> Any:
        """Invoke call_fn(**kwargs), retrying without temperature on ValidationException.

        Some newer models (e.g. Opus 4.8+) deprecated the temperature parameter.
        """
        try:
            return call_fn(**kwargs)
        except ClientError as e:
            if (
                e.response.get("Error", {}).get("Code") == "ValidationException"
                and "temperature" in str(e)
                and "inferenceConfig" in kwargs
            ):
                kwargs["inferenceConfig"].pop("temperature", None)
                logger.info("retry_without_temperature", model=kwargs.get("modelId"))
                return call_fn(**kwargs)
            raise

    def _build_converse_kwargs(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Build kwargs dict shared between converse() and converse_stream().

        Args:
            prompt: The main prompt text (context + question combined, or just context).
            system: System instruction text.
            guardrail_id: Bedrock guardrail identifier.
            max_tokens: Max output tokens.
            temperature: Sampling temperature; omitted from inferenceConfig when None.
            guard_content: If provided, this text is wrapped in a guardContent block
                and appended as a separate content item. The guardrail evaluates ONLY
                this block for prompt attack detection — the main prompt text is not
                checked. Per AWS best practices for RAG applications.
            model_id: Per-call model override. When None, uses self._model_id.
        """
        effective_model_id = model_id or self._model_id
        if model_id and model_id != self._model_id:
            logger.info("using_model_override", override=model_id, default=self._model_id)
        # Build message content blocks
        content: list[dict[str, Any]] = []
        if prompt:
            content.append({"text": prompt})
        if guard_content:
            # guardContent tag tells Bedrock guardrail to evaluate only this
            # block for prompt attacks, leaving retrieved context untouched.
            # Bedrock delivers the text to the model whether or not a
            # guardrailConfig is set — without one, the block is simply not
            # guardrail-scored. Callers that use guardContent as their SOLE
            # channel for untrusted user text (e.g. tier2 nl_to_sql /
            # nl_to_sparql, per the tier3 pattern) depend on this — with
            # the previous `and guardrail_id` gate, a deployment without a
            # guardrail would silently drop the user's question.
            content.append({"guardContent": {"text": {"text": guard_content}}})

        kwargs: dict[str, Any] = {
            "modelId": effective_model_id,
            "messages": [{"role": "user", "content": content}],
            "inferenceConfig": {
                "maxTokens": max_tokens,
                **({"temperature": temperature} if temperature is not None else {}),
            },
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if guardrail_id:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": guardrail_id,
                "guardrailVersion": self._guardrail_version,
            }
        return kwargs

    @instrumented("bedrock")
    async def embed(self, text: str) -> list[float]:
        """Embed query text into a vector using the shared search-query embedder.

        Args:
            text: The query text to embed.

        Returns:
            The embedding vector as a list of floats.
        """
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
