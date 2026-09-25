# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared guardrail content screener using Bedrock ApplyGuardrail API.

Provides both synchronous (Lambda/ingestion) and asynchronous (serve/query-time)
interfaces for evaluating text content against a configured Bedrock guardrail
without invoking a foundation model.

Used by:
- Ingestion pipeline (KG Build): screen chunks before indexing.
- Serve layer (Synthesizer): screen retrieved chunks before prompt assembly.

References:
- AWS ApplyGuardrail API: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html
- SDO-188: indirect prompt injection via unscreened RAG content.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from coa_common.config import resolve_region
from coa_common.guardrail_metrics import (
    COMPONENT_KG_BUILD,
    emit_guardrail_decision,
    filter_type_from_assessments,
)

logger = structlog.get_logger(__name__)

# Guardrail policy paths covering all assessment types.
# Consolidated from libs/common/bedrock.py and clients/bedrock.py (DRY).
GUARDRAIL_POLICY_PATHS: list[tuple[str, str]] = [
    ("topicPolicy", "topics"),
    ("contentPolicy", "filters"),
    ("wordPolicy", "customWords"),
    ("wordPolicy", "managedWordLists"),
    ("sensitiveInformationPolicy", "piiEntities"),
    ("sensitiveInformationPolicy", "regexes"),
]

# Retry configuration matching project patterns (opensearch_vector._oss_retry).
_MAX_RETRIES = 3
_INITIAL_BACKOFF_S = 0.2
_MAX_BACKOFF_S = 2.0

_EXECUTOR = ThreadPoolExecutor(max_workers=5, thread_name_prefix="guardrail-screen")


def assessment_has_block(assessment: dict) -> bool:
    """Check if a guardrail assessment contains any BLOCKED action.

    Shared utility, consolidating duplicated logic from bedrock.py modules.
    """
    try:
        return any(
            item.get("action") == "BLOCKED"
            for policy_key, items_key in GUARDRAIL_POLICY_PATHS
            for item in assessment.get(policy_key, {}).get(items_key, [])
        )
    except (TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class ScreeningResult:
    """Result of screening a single text content block.

    Attributes:
        text_index: Position of this text in the input batch (0-based).
        passed: True if content is safe to use, False if intervened.
        action: Raw guardrail action ("NONE" or "GUARDRAIL_INTERVENED").
        intervention_reason: Human-readable reason when intervened.
        assessment: Full assessment trace when outputScope=FULL (for debugging/quarantine).
    """

    text_index: int
    passed: bool
    action: str
    intervention_reason: str | None = None
    assessment: dict[str, Any] | None = None


class GuardrailScreenerError(Exception):
    """Raised when the screener is permanently unavailable after retries."""


class GuardrailScreener:
    """Screens text content via Bedrock ApplyGuardrail API.

    Provides both sync and async interfaces for use in Lambda (ingestion)
    and async serve (query-time) contexts respectively.

    Args:
        guardrail_id: Bedrock guardrail identifier (retrieval guardrail).
        guardrail_version: Guardrail version string.
        region: AWS region. Defaults to AWS_REGION env var.
        component: ``Component`` dimension for the decision metrics (#111 AC10).
        metrics_transport: ``"put"`` (PutMetricData) or ``"emf"`` (stdout EMF).
            Defaults to ``"put"`` because the primary caller is the kg-build ECS
            Fargate task, which publishes its other custom metrics the same way.
        guardrail_id_provider: Optional callable returning the guardrail id LIVE
            on each screening call. When supplied it takes precedence over
            ``guardrail_id`` so an operator's SSM retrieval-guardrail change
            reaches a long-lived (serve) screener without a redeploy.
            Short-lived callers (ingestion Fargate tasks) omit it and keep the
            static ``guardrail_id``.
        guardrail_version_provider: Optional callable returning the guardrail
            version LIVE; paired with ``guardrail_id_provider``.
    """

    def __init__(
        self,
        guardrail_id: str,
        guardrail_version: str | None = None,
        region: str | None = None,
        component: str = COMPONENT_KG_BUILD,
        metrics_transport: Literal["put", "emf"] = "put",
        *,
        guardrail_id_provider: Callable[[], str] | None = None,
        guardrail_version_provider: Callable[[], str] | None = None,
    ) -> None:
        """Store guardrail coordinates and defer client creation until first use (see class Args)."""
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version or os.environ.get("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
        self._guardrail_id_provider = guardrail_id_provider
        self._guardrail_version_provider = guardrail_version_provider
        # resolve_region (AWS_REGION → AWS_DEFAULT_REGION → us-east-1) rather than a
        # bespoke getenv: this region reaches both ApplyGuardrail and the guardrail
        # decision metrics, so a wrong value screens against — and files metrics in —
        # a region nobody watches. See config.resolve_region.
        self._region = region or resolve_region()
        self._component = component
        self._metrics_transport = metrics_transport
        self._client: Any = None

    def _effective_guardrail_id(self) -> str:
        """Resolve the guardrail id LIVE via the provider when injected.

        Falls back to the construction-time ``guardrail_id`` when no provider is
        wired (the ingestion path). An empty live value falls back to the static
        id rather than calling ApplyGuardrail with an empty identifier.
        """
        if self._guardrail_id_provider is not None:
            return self._guardrail_id_provider() or self._guardrail_id
        return self._guardrail_id

    def _effective_guardrail_version(self) -> str:
        """Resolve the guardrail version LIVE via the provider when injected."""
        if self._guardrail_version_provider is not None:
            return self._guardrail_version_provider() or self._guardrail_version
        return self._guardrail_version

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self._region,
                config=BotoConfig(
                    read_timeout=10,
                    connect_timeout=5,
                    retries={"max_attempts": 1},
                ),
            )
        return self._client

    def screen_texts(self, texts: list[str], *, source: str = "INPUT") -> list[ScreeningResult]:
        """Evaluate texts against the guardrail (synchronous, for Lambda).

        NOTE: ApplyGuardrail evaluates ALL content blocks as a single input.
        Multiple texts in one call are concatenated, not evaluated independently.
        For per-document screening, call with one text at a time.
        When called with multiple texts, the single overall result is applied
        to all texts (conservative: if any triggers, all are marked as failed).

        Args:
            texts: List of text strings to evaluate.
            source: "INPUT" for content entering the system, "OUTPUT" for responses.

        Returns:
            ScreeningResult per text in the same order as input.

        Raises:
            GuardrailScreenerError: After exhausting retries on transient errors.
        """
        if not texts:
            return []

        content = [{"text": {"text": t}} for t in texts]
        # Spans _call_with_retry, so GuardrailLatency includes retry backoff —
        # deliberately, it is what the caller waited, not the server's own time.
        start = time.monotonic()
        response = self._call_with_retry(content, source)
        latency_ms = (time.monotonic() - start) * 1000
        return self._parse_response(response, len(texts), latency_ms=latency_ms)

    async def ascreen_texts(self, texts: list[str], *, source: str = "INPUT") -> list[ScreeningResult]:
        """Evaluate texts against the guardrail (async, for serve layer).

        Runs the synchronous API call in a thread pool executor.

        Args:
            texts: List of text strings to evaluate (max 10 per call).
            source: "INPUT" or "OUTPUT".

        Returns:
            ScreeningResult per text in the same order as input.

        Raises:
            GuardrailScreenerError: After exhausting retries on transient errors.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self.screen_texts, texts)

    def _call_with_retry(self, content: list[dict], source: str) -> dict:
        """Call ApplyGuardrail with exponential backoff on transient errors."""
        attempt = 0
        while True:
            try:
                client = self._get_client()
                return client.apply_guardrail(
                    guardrailIdentifier=self._effective_guardrail_id(),
                    guardrailVersion=self._effective_guardrail_version(),
                    source=source,
                    content=content,
                    outputScope="FULL",
                )
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                status_code = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)

                is_transient = error_code in ("ThrottlingException", "TooManyRequestsException") or status_code >= 500

                if not is_transient or attempt >= _MAX_RETRIES:
                    logger.error(
                        "guardrail_screener_failed",
                        error_code=error_code,
                        status_code=status_code,
                        attempts=attempt + 1,
                    )
                    raise GuardrailScreenerError(
                        f"ApplyGuardrail failed after {attempt + 1} attempts: {error_code}"
                    ) from e

                backoff = min(_MAX_BACKOFF_S, _INITIAL_BACKOFF_S * (2**attempt)) + random.uniform(0, 0.1)
                logger.warning(
                    "guardrail_screener_retry",
                    error_code=error_code,
                    attempt=attempt + 1,
                    backoff_s=round(backoff, 3),
                )
                time.sleep(backoff)
                attempt += 1
            except Exception as e:
                logger.error("guardrail_screener_unexpected_error", error=str(e))
                raise GuardrailScreenerError(f"Unexpected error calling ApplyGuardrail: {e}") from e

    def _emit_decision(self, *, blocked: bool, latency_ms: float, assessments: list[dict]) -> None:
        """Emit the allow/block decision metrics + structured log line (#111 AC10/AC11)."""
        emit_guardrail_decision(
            component=self._component,
            blocked=blocked,
            latency_ms=latency_ms,
            filter_type=filter_type_from_assessments(assessments),
            transport=self._metrics_transport,
            region=self._region,
        )

    def _parse_response(self, response: dict, expected_count: int, *, latency_ms: float = 0.0) -> list[ScreeningResult]:
        """Parse ApplyGuardrail response into ScreeningResult list.

        ApplyGuardrail evaluates all content blocks as one input and returns
        a single overall action + assessment. We apply the overall result to
        all texts uniformly. For per-text granularity, callers should invoke
        screen_texts() with one text at a time.

        Emits one guardrail decision (metrics + log) per parsed response,
        covering BOTH the allow and block outcomes.
        """
        action = response.get("action", "NONE")
        assessments = response.get("assessments", [])

        # Single overall action applies to all texts in the batch.
        if action == "NONE":
            self._emit_decision(blocked=False, latency_ms=latency_ms, assessments=assessments)
            return [ScreeningResult(text_index=i, passed=True, action="NONE") for i in range(expected_count)]

        # GUARDRAIL_INTERVENED covers two very different outcomes:
        #   1. A genuine security BLOCK (content/topic/word policy) — quarantine.
        #   2. Benign PII ANONYMIZATION (masking a name/email/phone) — must NOT
        #      quarantine. Dropping every document that merely mentions a person
        #      guts knowledge-graph / ontology construction, whose primary payload
        #      IS named entities and their relationships.
        # Only a real block should fail screening; use the shared assessment_has_block
        # predicate (already used by the Converse-based paths) across all assessments.
        assessment = assessments[0] if assessments else {}
        reason = self._extract_intervention_reason(assessment) if assessment else "unknown"
        # Quarantine only on a real block. Fail CLOSED if there is no assessment
        # to inspect (anomalous with outputScope=FULL): we cannot prove the
        # intervention was benign anonymization, so treat it as a block.
        blocked = (not assessments) or any(assessment_has_block(a) for a in assessments)
        self._emit_decision(blocked=blocked, latency_ms=latency_ms, assessments=assessments)

        if not blocked:
            # Anonymize-only (or no blocking policy fired): safe to use. The
            # intervention_reason still records what was masked for observability.
            return [
                ScreeningResult(
                    text_index=i,
                    passed=True,
                    action=action,
                    intervention_reason=reason,
                    assessment=assessment,
                )
                for i in range(expected_count)
            ]

        return [
            ScreeningResult(
                text_index=i,
                passed=False,
                action="GUARDRAIL_INTERVENED",
                intervention_reason=reason,
                assessment=assessment,
            )
            for i in range(expected_count)
        ]

    @staticmethod
    def _extract_intervention_reason(assessment: dict) -> str:
        """Extract a human-readable reason from an assessment trace.

        Reports every policy item with a non-NONE action, including PII
        ANONYMIZED entries — not just BLOCKED ones. A guardrail that only
        anonymizes PII previously logged the unhelpful "unknown" reason
        because no item was BLOCKED.
        """
        reasons: list[str] = []
        for policy_key, items_key in GUARDRAIL_POLICY_PATHS:
            items = assessment.get(policy_key, {}).get(items_key, [])
            for item in items:
                item_action = item.get("action")
                if item_action and item_action != "NONE":
                    item_type = item.get("type", item.get("name", "unknown"))
                    confidence = item.get("confidence", "")
                    suffix = f"({confidence})" if confidence else ""
                    reasons.append(f"{policy_key}.{items_key}:{item_type}:{item_action}{suffix}")
        return "; ".join(reasons) if reasons else "unknown"
