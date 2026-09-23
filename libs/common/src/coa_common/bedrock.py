# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Thin wrapper around Amazon Bedrock for LLM invocations."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

import boto3
from botocore.config import Config

from coa_common.config import resolve_region
from coa_common.guardrail_metrics import (
    COMPONENT_ENRICHMENT,
    assessments_from_trace,
    emit_guardrail_decision,
    filter_type_from_assessments,
)

logger = logging.getLogger(__name__)

# Chat/completion model used when a caller constructs BedrockClient without an
# explicit model_id (table enrichment, constraint inference). Resolved from the
# BEDROCK_CHAT_MODEL_ID environment variable so a deployment can point these
# paths at a region-appropriate model (#94) — the `us.` inference profile
# fallback is only invocable from US regions. Deliberately NOT BEDROCK_MODEL_ID:
# that name is already taken by the serve query LLM (context-manager) and the
# metric-service embed fallback chain, and reusing it here would cross-wire
# those models into this client. Must stay in step with the TypeScript
# DEFAULT_BEDROCK_CHAT_MODEL_ID (libs/ts-shared/src/constants.ts).
FALLBACK_CHAT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def resolve_chat_model_id(default: str = FALLBACK_CHAT_MODEL_ID) -> str:
    """Resolve the default chat/completion model id from the environment.

    Reads ``BEDROCK_CHAT_MODEL_ID``, falling back to ``default``. Accepts both
    geographic inference profiles (``us.``/``eu.``/``jp.``/``global.``) and bare
    in-region model ids, since some models publish geo profiles for only a
    subset of regions.
    """
    return os.environ.get("BEDROCK_CHAT_MODEL_ID") or default


DEFAULT_MODEL_ID = resolve_chat_model_id()
_DEFAULT_READ_TIMEOUT = 90
_DEFAULT_GUARDRAIL_VERSION = "DRAFT"

_DEFAULT_MAX_INPUT_CHARS = 200_000

_GUARDRAIL_POLICY_PATHS = [
    ("topicPolicy", "topics"),
    ("contentPolicy", "filters"),
    ("wordPolicy", "customWords"),
    ("wordPolicy", "managedWordLists"),
    ("sensitiveInformationPolicy", "piiEntities"),
    ("sensitiveInformationPolicy", "regexes"),
]


def _assessment_has_block(assessment: dict) -> bool:
    """Check if a guardrail assessment contains any BLOCKED action."""
    try:
        return any(
            item.get("action") == "BLOCKED"
            for policy_key, items_key in _GUARDRAIL_POLICY_PATHS
            for item in assessment.get(policy_key, {}).get(items_key, [])
        )
    except (TypeError, AttributeError):
        return False


def extract_text_blocks(content_blocks: list[dict]) -> str:
    """Join the text of every ``text`` ContentBlock in a Converse response.

    A Converse ``output.message.content`` is an array of ContentBlocks, and a
    reasoning model (e.g. Claude with extended thinking) emits a
    ``reasoningContent`` block — which has NO top-level ``text`` key — before
    the ``text`` block that carries the answer. Indexing ``content[0]["text"]``
    therefore raises ``KeyError`` whenever reasoning is present. Select blocks
    by the presence of a ``text`` key instead of by position, and concatenate
    them (there is normally one, but joining is safe if a model splits its
    answer across several).

    Raises:
        ValueError: if no ``text`` block is present (e.g. a response that is
            only ``reasoningContent`` / ``toolUse``) — a caller that expected a
            textual answer must fail loudly, never silently receive "".
    """
    texts = [b["text"] for b in content_blocks if isinstance(b, dict) and "text" in b]
    if not texts:
        raise ValueError("Bedrock response contained no text content block")
    return "\n".join(texts)


class GuardrailBlockedError(Exception):
    """Raised when a Bedrock Guardrail blocks the LLM response."""


class BedrockTruncationError(ValueError):
    """Raised when Bedrock exhausts the requested output-token budget."""

    def __init__(self, *, model_id: str, output_tokens: int, max_tokens: int) -> None:
        """Capture the safe diagnostics needed to identify budget exhaustion."""
        self.model_id = model_id
        self.stop_reason = "max_tokens"
        self.output_tokens = output_tokens
        self.max_tokens = max_tokens
        super().__init__(
            "Bedrock response truncated: "
            f"model={model_id} stop_reason={self.stop_reason} "
            f"output_tokens={output_tokens} requested_max_tokens={max_tokens}"
        )


class InputTooLargeError(ValueError):
    """Raised when the combined prompt exceeds the configured input-size cap."""


@dataclass(frozen=True)
class BedrockInvocationResult:
    """Result of a Bedrock invocation including parsed content and usage."""

    result: dict | list
    input_tokens: int
    output_tokens: int
    latency_ms: float


class BedrockClient:
    """Invokes Bedrock models for metadata enrichment via the Converse API.

    Args:
        region: AWS region for Bedrock client (defaults to AWS_REGION env var or us-east-1).
        model_id: Bedrock model identifier (defaults to Claude Haiku 4.5).
        read_timeout: HTTP read timeout in seconds (default 90).
        guardrail_id: Optional Bedrock Guardrail identifier.
        guardrail_version: Guardrail version (default DRAFT).
        max_input_chars: Maximum combined prompt length in characters (default 200,000).
            Requests exceeding this raise InputTooLargeError.
        component: ``Component`` dimension for the guardrail decision metrics
            (#111 AC10). Both consumers of this client run as ECS Fargate tasks,
            which publish their custom metrics via PutMetricData, so decisions
            go out the same way rather than as stdout EMF.
    """

    def __init__(
        self,
        region: str | None = None,
        model_id: str = DEFAULT_MODEL_ID,
        read_timeout: int = _DEFAULT_READ_TIMEOUT,
        guardrail_id: str | None = None,
        guardrail_version: str | None = None,
        max_input_chars: int = _DEFAULT_MAX_INPUT_CHARS,
        component: str = COMPONENT_ENRICHMENT,
    ) -> None:
        """Build the Bedrock runtime client with adaptive retries (see class Args)."""
        # resolve_region (AWS_REGION → AWS_DEFAULT_REGION → us-east-1) rather than a
        # bespoke getenv: this region reaches both the Bedrock client and the
        # guardrail decision metrics, and no ECS task definition sets AWS_REGION —
        # so a lone getenv silently files metrics in us-east-1. See config.resolve_region.
        self._region = region or resolve_region()
        self._model_id = model_id
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version or _DEFAULT_GUARDRAIL_VERSION
        self._max_input_chars = max_input_chars
        self._component = component
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=self._region,
            config=Config(
                retries={"mode": "adaptive", "max_attempts": 3},
                read_timeout=read_timeout,
            ),
        )

    @staticmethod
    def _check_guardrail_blocked(guardrail_trace: dict) -> bool:
        """Determine if a guardrail trace indicates BLOCK (not just ANONYMIZE)."""
        if not guardrail_trace:
            return True

        for assessment in guardrail_trace.get("inputAssessment", {}).values():
            if _assessment_has_block(assessment):
                return True

        for assessments in guardrail_trace.get("outputAssessments", {}).values():
            for assessment in assessments:
                if _assessment_has_block(assessment):
                    return True

        return False

    def invoke(self, system_prompt: str, user_prompt: str, max_tokens: int = 4096) -> BedrockInvocationResult:
        """Call Bedrock via Converse API, parse JSON response, and return result with token usage + latency."""
        input_chars = len(system_prompt) + len(user_prompt)
        if input_chars > self._max_input_chars:
            logger.warning("Bedrock input too large: %d chars (limit %d)", input_chars, self._max_input_chars)
            raise InputTooLargeError(
                f"Combined prompt is {input_chars} chars, exceeds limit of {self._max_input_chars}"
            )
        start = time.monotonic()
        kwargs = {
            "modelId": self._model_id,
            "messages": [{"role": "user", "content": [{"text": user_prompt}]}],
            "system": [{"text": system_prompt}],
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if self._guardrail_id:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": self._guardrail_id,
                "guardrailVersion": self._guardrail_version,
                "trace": "enabled",
            }

        try:
            response = self._client.converse(**kwargs)
        except self._client.exceptions.ClientError as e:
            logger.error("Bedrock API call failed: %s", e)
            raise
        except Exception as e:
            logger.error("Unexpected error invoking Bedrock: %s", e)
            raise

        latency_ms = (time.monotonic() - start) * 1000

        try:
            stop_reason = response.get("stopReason")
            usage = response.get("usage", {})
            input_tokens = usage.get("inputTokens", 0)
            output_tokens = usage.get("outputTokens", 0)

            # ``max_tokens`` means any text/content envelope is expected to be
            # incomplete. Classify it before indexing the envelope or parsing
            # JSON so even a response that ends between content blocks retains
            # the actionable truncation contract.
            if stop_reason == "max_tokens":
                logger.warning(
                    "Bedrock response truncated: model=%s stop_reason=%s output_tokens=%s requested_max_tokens=%s",
                    self._model_id,
                    stop_reason,
                    output_tokens,
                    max_tokens,
                )
                raise BedrockTruncationError(
                    model_id=self._model_id,
                    output_tokens=output_tokens,
                    max_tokens=max_tokens,
                )

            output_message = response["output"]["message"]
            content_blocks = output_message.get("content", [])
            logger.info(
                "Bedrock response: model=%s stop_reason=%s usage=%s content_blocks=%d",
                self._model_id,
                stop_reason,
                usage,
                len(content_blocks),
            )

            intervened = stop_reason in ("guardrail_intervened", "guardrail")
            guardrail_trace = response.get("trace", {}).get("guardrail", {}) if intervened else {}
            blocked = self._check_guardrail_blocked(guardrail_trace) if intervened else False
            # Flattened per-category assessment (empty when no guardrail/trace). Kept
            # in a local so a BLOCK can name the category that actually fired instead
            # of collapsing to an un-diagnosable "CONTENT" filter_type.
            assessments = assessments_from_trace(guardrail_trace)

            # Emit the decision only when a guardrail was actually applied —
            # counting unguarded invocations as ALLOW would make the block rate
            # meaningless. Emitted for allow AND block (#111 AC10/AC11).
            if self._guardrail_id:
                emit_guardrail_decision(
                    component=self._component,
                    blocked=blocked,
                    latency_ms=latency_ms,
                    filter_type=filter_type_from_assessments(assessments),
                    # Both consumers are ECS tasks that publish metrics via PutMetricData.
                    transport="put",
                    region=self._region,
                )

            if blocked:
                logger.warning(
                    "Guardrail blocked response: model=%s guardrail=%s filter_type=%s assessments=%s",
                    self._model_id,
                    self._guardrail_id,
                    filter_type_from_assessments(assessments),
                    assessments,
                )
                raise GuardrailBlockedError(f"Guardrail {self._guardrail_id} blocked the response")

                # Intervened without blocking — the response is still usable but
                # matched values were substituted with placeholders (e.g.
                # "{NAME}"). Note this masks values, it does not drop fields, so
                # it cannot by itself explain a wholly missing output field.
                logger.warning(
                    "Guardrail intervened without blocking (values masked): model=%s guardrail=%s",
                    self._model_id,
                    self._guardrail_id,
                )

            if not content_blocks:
                logger.error("Bedrock returned no content blocks")
                raise ValueError("Bedrock returned no content blocks")
            # Select the text block(s) by key, not position: a reasoning model
            # returns a reasoningContent block (no "text" key) ahead of the
            # answer, so content[0]["text"] would KeyError. extract_text_blocks
            # raises ValueError if there is no text block at all.
            content = extract_text_blocks(content_blocks)
            if not content.strip():
                logger.error("Bedrock returned empty content")
                raise ValueError("Bedrock returned empty response")
            text = content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            result = json.loads(text)
            return BedrockInvocationResult(
                result=result,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            )
        except (GuardrailBlockedError, BedrockTruncationError):
            raise
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error("Failed to parse Bedrock response: %s", e)
            raise ValueError(f"Invalid Bedrock response format: {e}") from e
