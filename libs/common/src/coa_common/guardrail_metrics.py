# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Guardrail allow/block observability — CloudWatch metrics + structured decision log.

One helper for every site that decides a guardrail outcome (issue #111, AC10/AC11).
Emits three metrics under ``COA/Guardrails``:

* ``GuardrailInvocations`` (Count) — every decision, whatever the outcome. Carries the
  ``Decision`` dimension, so ALLOW/ANONYMIZED/BLOCK/UNKNOWN/MODEL_FILTERED are separable.
* ``GuardrailBlocked`` (Count) — 1 only when the guardrail intervened with a **confirmed**
  block. Never incremented for an unconfirmed suppression: see ``GuardrailUnknown``.
* ``GuardrailUnknown`` (Count) — 1 when the guardrail intervened but the outcome could not
  be classified (no trace). Such a call IS suppressed, but suppression on missing evidence
  is not a policy block, and folding it into ``GuardrailBlocked`` would make both numbers
  unrecoverable. Should be zero in steady state — a non-zero value means guardrail traces
  stopped arriving, which previously destroyed answers silently.
* ``GuardrailLatency`` (Milliseconds) — wall-clock of the guarded call.

Block *rate* is deliberately NOT emitted: it is a CloudWatch math expression
over the two counters, so publishing it would be a third source of truth.

Two transports, because the callers run on different compute:

* ``transport="put"`` — explicit ``PutMetricData``, used by the ECS Fargate
  tasks (kg-build, enrichment, ontology). Not a constraint: ``awsLogs`` writes
  through ``PutLogEvents`` and CloudWatch Logs *does* extract Embedded Metric
  Format from those events. It is a choice — these containers already publish
  their other custom metrics (cost, stage durations) via ``PutMetricData``, and
  keeping the decision metric off the log pipeline means it does not depend on
  a JSON line surviving a stdout stream shared with human-readable logs, nor
  carry per-log-event ingestion cost for every guardrail call. Needs a
  ``cloudwatch:PutMetricData`` grant conditioned on the metric namespace.
* ``transport="emf"`` — stdout EMF via :func:`coa_common.metrics.emit_metric`.
  No IAM grant and no API latency on the request path, at the cost of log
  ingestion. Used by the serve path, which already emits its other metrics
  this way.

Nothing here may raise into a guardrail decision: a blocked-content path that
fails because telemetry broke would be a security regression. Every entry point
swallows its own exceptions.
"""

from __future__ import annotations

from contextlib import suppress
from functools import lru_cache
from typing import Any, Literal

import boto3
import structlog

from coa_common.config import resolve_region
from coa_common.metrics import emit_metric

logger = structlog.get_logger(__name__)

METRICS_NAMESPACE = "COA/Guardrails"

# Component dimension values — low cardinality by construction.
COMPONENT_KG_BUILD = "kg-build"
COMPONENT_NL_TO_SPARQL = "nl-to-sparql"
COMPONENT_ENRICHMENT = "enrichment"
# Serve's query-time screening of retrieved chunks. Distinct from
# ``nl-to-sparql`` (serve's Converse calls) even though both run in serve:
# folding them together would make retrieval blocks indistinguishable from
# generation blocks on the dashboard.
COMPONENT_SERVE_RETRIEVAL = "serve-retrieval"
# Ontology SHACL-shape NL generation (ontology ECS task).
COMPONENT_ONTOLOGY_SHAPES = "ontology-shapes"

DECISION_ALLOW = "ALLOW"
DECISION_BLOCK = "BLOCK"
# Masked, not blocked. Its own value because masking is invisible otherwise: an
# NTSB/TimeBank benchmark found 68% of one arm's answers silently carrying {NAME}
# placeholders, which no existing metric would have surfaced.
DECISION_ANONYMIZED = "ANONYMIZED"
# Guardrail intervened but no trace was returned, so the action is unknown. The call
# is suppressed (fail closed) yet deliberately NOT counted as a confirmed block.
DECISION_UNKNOWN = "UNKNOWN"
# The model filtered its own output (``stopReason=content_filtered``). Not a
# Bedrock Guardrail policy action at all — kept distinct so a native model refusal
# is never read as a guardrail block, nor (as before) as an ALLOW.
DECISION_MODEL_FILTERED = "MODEL_FILTERED"

# Guardrail policy key → (items key, reported filter_type). Mirrors
# ``guardrail_screener.GUARDRAIL_POLICY_PATHS``, deliberately re-declared rather
# than imported: the screener imports this module, so importing it back would
# be circular. Word filters are grouped under CONTENT — like content filters
# they reject the text outright, unlike PII which may merely anonymize.
_POLICY_FILTER_TYPES: list[tuple[str, str, str]] = [
    ("topicPolicy", "topics", "TOPIC"),
    ("contentPolicy", "filters", "CONTENT"),
    ("wordPolicy", "customWords", "CONTENT"),
    ("wordPolicy", "managedWordLists", "CONTENT"),
    ("sensitiveInformationPolicy", "piiEntities", "PII"),
    ("sensitiveInformationPolicy", "regexes", "PII"),
]

# Reported when several policies fire at once — most-severe wins.
_FILTER_TYPE_PRIORITY = ("CONTENT", "TOPIC", "PII")


@lru_cache(maxsize=4)
def _client_for_region(region: str) -> Any:
    """Cache one CloudWatch client per region — client construction is ~tens of ms.

    Keyed on the *resolved* region so a later environment change cannot be
    served a client built for the old one.
    """
    return boto3.client("cloudwatch", region_name=region)


def _cloudwatch_client(region: str | None) -> Any:
    """Return the memoized CloudWatch client for *region* (stub seam for tests)."""
    return _client_for_region(region or resolve_region())


def filter_type_from_assessments(assessments: list[dict[str, Any]] | None) -> str:
    """Summarise which guardrail policy family fired as CONTENT/PII/TOPIC/NONE.

    Considers any item whose action is set and not ``"NONE"`` (so a PII
    ANONYMIZE still reports ``PII``, not ``NONE``). When several families fire,
    the most severe wins per :data:`_FILTER_TYPE_PRIORITY`.

    Args:
        assessments: Guardrail assessment traces, in any API's shape.

    Returns:
        ``"CONTENT"``, ``"TOPIC"``, ``"PII"``, or ``"NONE"`` when nothing fired
        (also the answer for malformed input — this is a log field, not a gate).
    """
    found: set[str] = set()
    try:
        for assessment in assessments or []:
            for policy_key, items_key, filter_type in _POLICY_FILTER_TYPES:
                for item in assessment.get(policy_key, {}).get(items_key, []):
                    action = item.get("action")
                    if action and action != "NONE":
                        found.add(filter_type)
    except (TypeError, AttributeError):
        return "NONE"

    return next((t for t in _FILTER_TYPE_PRIORITY if t in found), "NONE")


def assessments_from_trace(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten a Converse guardrail trace into a flat list of assessments.

    Converse traces nest assessments under per-guardrail keys, and the value is
    either one assessment or a list of them depending on the key
    (``inputAssessment`` vs ``outputAssessment``/``outputAssessments``). Accepts
    all of those shapes so callers do not have to care which they got.

    Args:
        trace: The ``trace.guardrail`` sub-document from a Converse response.

    Returns:
        Every assessment found, or ``[]`` for an empty or malformed trace.
    """
    flat: list[dict[str, Any]] = []
    try:
        for value in (trace or {}).values():
            if not isinstance(value, dict):
                continue
            for entry in value.values():
                if isinstance(entry, list):
                    flat.extend(e for e in entry if isinstance(e, dict))
                elif isinstance(entry, dict):
                    flat.append(entry)
    except AttributeError:
        return []
    return flat


def redacted_actions(assessments: list[dict[str, Any]] | None) -> list[str]:
    """Summarise assessments as ``policy:name=action`` labels that are safe to log.

    An assessment echoes the text each policy matched (``{"type": "NAME", "match": "Jane Doe", ...}``),
    so it must never be logged as is. Only the policy, the configured name (a topic's or a regex's) or
    else the entity type, and the action are kept.

    Args:
        assessments: Guardrail assessments, as :func:`assessments_from_trace` returns them.

    Returns:
        One label per policy item that carries an action, e.g. ``"sensitiveInformationPolicy:NAME=BLOCKED"``;
        ``[]`` for empty or malformed input (this is a log field, not a gate).
    """
    out: list[str] = []
    for assessment in assessments or []:
        if not isinstance(assessment, dict):
            continue
        for policy_key, items_key, _ in _POLICY_FILTER_TYPES:
            policy = assessment.get(policy_key)
            items = policy.get(items_key) if isinstance(policy, dict) else None
            out.extend(_redacted_label(policy_key, item) for item in (items if isinstance(items, list) else []))
    return [label for label in out if label]


def _redacted_label(policy_key: str, item: Any) -> str:
    if not isinstance(item, dict) or not item.get("action"):
        return ""
    return f"{policy_key}:{item.get('name') or item.get('type') or '-'}={item['action']}"


def emit_guardrail_decision(
    *,
    component: str,
    blocked: bool,
    latency_ms: float,
    filter_type: str = "NONE",
    transport: Literal["put", "emf"] = "put",
    region: str | None = None,
    cloudwatch_client: Any = None,
    decision: str | None = None,
) -> None:
    """Record one guardrail decision as metrics plus a log line.

    Emits on EVERY outcome so the allow path is observable too (a guardrail
    that stopped being called looks identical to one that never blocks unless
    invocations are counted).

    Args:
        component: Dimension value, e.g. :data:`COMPONENT_KG_BUILD`.
        blocked: True only for a CONFIRMED block; drives ``GuardrailBlocked``.
            An unconfirmed suppression passes ``blocked=False`` with
            ``decision=DECISION_UNKNOWN`` so the two stay separable.
        latency_ms: Wall-clock of the guarded call, in milliseconds.
        filter_type: CONTENT/PII/TOPIC/NONE, from :func:`filter_type_from_assessments`.
        transport: ``"put"`` for ECS (PutMetricData), ``"emf"`` for Lambda-style stdout.
        region: Region override for the CloudWatch client (``"put"`` only).
        cloudwatch_client: Injected client for tests (``"put"`` only).
        decision: Explicit decision label. Omit to derive ALLOW/BLOCK from
            ``blocked``, which is what every pre-existing caller does.
    """
    if decision is None:
        decision = DECISION_BLOCK if blocked else DECISION_ALLOW

    # Best-effort, and separately suppressed from the metrics below: a logging
    # failure must not cost us the metric, nor vice versa.
    with suppress(Exception):
        logger.info(
            "guardrail_decision",
            component=component,
            decision=decision,
            filter_type=filter_type,
            latency_ms=round(latency_ms, 2),
        )

    try:
        if transport == "emf":
            _emit_emf(component, decision, blocked, latency_ms)
        else:
            _emit_put(component, decision, blocked, latency_ms, region, cloudwatch_client)
    except Exception:  # noqa: BLE001
        # ponytail: swallow-and-warn; observability must never fail a guardrail decision.
        # The warning is itself suppressed: a broken stdout (closed pipe, full log
        # buffer on ECS) would make it raise, and this emit sits BETWEEN the
        # block-determination and the ``raise GuardrailBlockedError`` in
        # bedrock.py — an escaping exception would skip the block entirely.
        with suppress(Exception):
            logger.warning("guardrail_metric_emit_failed", component=component, exc_info=True)


def _emit_emf(component: str, decision: str, blocked: bool, latency_ms: float) -> None:
    """Emit via stdout EMF (``emit_metric`` already swallows its own errors)."""
    emit_metric(METRICS_NAMESPACE, "GuardrailInvocations", 1, "Count", Component=component, Decision=decision)
    emit_metric(METRICS_NAMESPACE, "GuardrailLatency", latency_ms, "Milliseconds", Component=component)
    if blocked:
        emit_metric(METRICS_NAMESPACE, "GuardrailBlocked", 1, "Count", Component=component)
    if decision == DECISION_UNKNOWN:
        emit_metric(METRICS_NAMESPACE, "GuardrailUnknown", 1, "Count", Component=component)


def _emit_put(
    component: str,
    decision: str,
    blocked: bool,
    latency_ms: float,
    region: str | None,
    cloudwatch_client: Any,
) -> None:
    """Emit via a single PutMetricData call (the ECS transport — see module docstring)."""
    component_dim = [{"Name": "Component", "Value": component}]
    metric_data: list[dict[str, Any]] = [
        {
            "MetricName": "GuardrailInvocations",
            "Unit": "Count",
            "Dimensions": [*component_dim, {"Name": "Decision", "Value": decision}],
            "Value": 1.0,
        },
        {
            "MetricName": "GuardrailLatency",
            "Unit": "Milliseconds",
            "Dimensions": component_dim,
            "Value": float(latency_ms),
        },
    ]
    if blocked:
        metric_data.append(
            {
                "MetricName": "GuardrailBlocked",
                "Unit": "Count",
                "Dimensions": component_dim,
                "Value": 1.0,
            }
        )
    if decision == DECISION_UNKNOWN:
        metric_data.append(
            {
                "MetricName": "GuardrailUnknown",
                "Unit": "Count",
                "Dimensions": component_dim,
                "Value": 1.0,
            }
        )

    client = cloudwatch_client or _cloudwatch_client(region)
    client.put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=metric_data)
