# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-induction-job Bedrock token/cost tracking and CloudWatch emission.

Induction runs on ECS Fargate with a plain ``awsLogs`` driver — there is no
Lambda log pipeline and no CloudWatch agent to auto-extract Embedded Metric
Format (EMF) from stdout. Metrics must therefore be published with an explicit
boto3 ``PutMetricData`` call. The EMF helper in ``metrics.py`` is Lambda-only
and is deliberately NOT reused here.

Usage:

    tracker = CostTracker()
    tracker.record("generate", model_id, in_tokens, out_tokens)
    tracker.record("rerank", model_id, in_tokens, out_tokens, latency_ms=42.0)
    emit_induction_job_metrics(tracker, namespace_id="ns-123")
"""

from __future__ import annotations

import resource
import threading

import boto3
import structlog

from coa_common.config import resolve_region

logger = structlog.get_logger(__name__)

METRICS_NAMESPACE = "COA/Ontology"

_STAGE_RERANK = "rerank"

# ponytail: hardcoded prices mirror enrichment_metrics.py; confirm vs AWS pricing page
# USD cost per single token, keyed by Bedrock model id (input/output priced separately).
_PRICING: dict[str, dict[str, float]] = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": 1.00 / 1_000_000,
        "output": 5.00 / 1_000_000,
    },
    "us.anthropic.claude-sonnet-4-6": {
        "input": 3.00 / 1_000_000,
        "output": 15.00 / 1_000_000,
    },
    "us.anthropic.claude-sonnet-5": {
        # Promotional pricing through 2026-08-31; standard is $3/$15 per 1M tokens.
        "input": 2.00 / 1_000_000,
        "output": 10.00 / 1_000_000,
    },
    # Embedding models bill on INPUT tokens only (no generated output).
    # ponytail: embed input-only pricing; confirm vs AWS pricing page
    "us.cohere.embed-v4:0": {
        "input": 0.10 / 1_000_000,
        "output": 0.0,
    },
    "amazon.titan-embed-text-v2:0": {
        "input": 0.02 / 1_000_000,
        "output": 0.0,
    },
    # Amazon Nova text models. Rates read from the AWS Price List API
    # (AmazonBedrock / us-east-1, on-demand "*-input-tokens" / "*-output-tokens"
    # usage types, price-list version 20260723233555) on 2026-07-29. Keyed by the
    # BARE model id; the cross-region inference-profile form ("us.amazon.nova-*")
    # resolves through the per-variant family entries below. Prices drift — re-check
    # against the pricing API periodically.
    "amazon.nova-lite-v1:0": {
        "input": 0.06 / 1_000_000,
        "output": 0.24 / 1_000_000,
    },
    "amazon.nova-pro-v1:0": {
        "input": 0.80 / 1_000_000,
        "output": 3.20 / 1_000_000,
    },
    "amazon.nova-premier-v1:0": {
        "input": 2.50 / 1_000_000,
        "output": 12.50 / 1_000_000,
    },
}

# ponytail: family-prefix fallback so a model-version bump doesn't silently zero out cost.
# Substring (matched case-insensitively against the model id) → pricing dict. Values reference
# the same dict objects as _PRICING so the rates are never duplicated. Cohere embed is the
# embed default because it is the primary embedding model in this stack.
_PRICING_FAMILY: dict[str, dict[str, float]] = {
    "sonnet": _PRICING["us.anthropic.claude-sonnet-5"],
    "haiku": _PRICING["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
    "embed": _PRICING["us.cohere.embed-v4:0"],
    # Nova families are per-variant (rates differ ~40x across the line, so a single
    # "nova" key would badly misprice). Premier is listed before Pro so the more
    # specific id wins regardless of substring subtleties. Deliberately no bare
    # "nova" key: an unlisted variant (micro, canvas, …) should log a warning
    # rather than silently borrow another variant's rate.
    "nova-lite": _PRICING["amazon.nova-lite-v1:0"],
    "nova-premier": _PRICING["amazon.nova-premier-v1:0"],
    "nova-pro": _PRICING["amazon.nova-pro-v1:0"],
}


def cost_for(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost for a single Bedrock call.

    Exact model ids are priced from :data:`_PRICING`. An unknown id falls back to
    a family-prefix match (sonnet/haiku/embed) so a model-version bump prices
    reasonably instead of silently zeroing out. Only a genuinely unrecognised id
    costs 0.0 (a warning is logged) — the caller should still accumulate the
    token counts so they are not silently lost.
    """
    pricing = _PRICING.get(model_id)
    if pricing is None:
        model_id_lower = model_id.lower()
        for family, family_pricing in _PRICING_FAMILY.items():
            if family in model_id_lower:
                pricing = family_pricing
                break
    if pricing is None:
        logger.warning("bedrock_metrics_unknown_model_id", model_id=model_id)
        return 0.0
    return (input_tokens * pricing["input"]) + (output_tokens * pricing["output"])


class CostTracker:
    """Thread-safe accumulator for per-induction-job Bedrock usage.

    A single job may ground multiple tables concurrently, so ``record`` is
    guarded by a lock. Read accessors return snapshots taken under the lock.
    """

    def __init__(self) -> None:
        """Initialize empty per-stage token/cost/duration accumulators and their lock."""
        # ponytail: single global lock, fine for per-job accumulation
        self._lock = threading.Lock()
        self._input_tokens: dict[str, int] = {}
        self._output_tokens: dict[str, int] = {}
        self._total_cost_usd: float = 0.0
        self._rerank_invocations: int = 0
        self._rerank_latencies: list[float] = []
        # Single value per key per job (set, not summed) — CloudWatch computes
        # p50/p90 across JOBS from these one-value-per-job datums, which a summed
        # value would corrupt.
        self._stage_durations_ms: dict[str, float] = {}
        self._job_duration_ms: float | None = None
        self._volume_counts: dict[str, int] = {}

    def record(
        self,
        stage: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float | None = None,
    ) -> None:
        """Accumulate one Bedrock call. ``stage`` is "generate", "rerank", or "embed"."""
        # cost_for is pure (only reads the module pricing table) — compute it
        # outside the lock to keep the critical section minimal.
        cost = cost_for(model_id, input_tokens, output_tokens)
        with self._lock:
            self._input_tokens[stage] = self._input_tokens.get(stage, 0) + input_tokens
            self._output_tokens[stage] = self._output_tokens.get(stage, 0) + output_tokens
            self._total_cost_usd += cost
            if stage == _STAGE_RERANK:
                self._rerank_invocations += 1
                if latency_ms is not None:
                    self._rerank_latencies.append(latency_ms)

    def record_from_converse(
        self,
        stage: str,
        model_id: str,
        resp: dict,
        latency_ms: float | None = None,
    ) -> None:
        """Record a Bedrock Converse response's usage under ``stage``.

        No-op safe: reads ``resp['usage']`` with 0 defaults, so a missing or
        malformed usage block records zero tokens rather than raising. This is
        the single seam every Converse call site routes through.
        """
        usage = (resp or {}).get("usage", {}) or {}
        self.record(
            stage,
            model_id,
            usage.get("inputTokens", 0),
            usage.get("outputTokens", 0),
            latency_ms=latency_ms,
        )

    def record_stage_duration(self, stage: str, duration_ms: float) -> None:
        """Set the elapsed wall-clock (ms) for one induction stage.

        ``stage`` is one of FetchMetadata / GroundingLoad / Induce / Store.
        Last-writer-wins (a single value per stage per job), never summed —
        the dashboard emits this as one ``Value`` datum so CloudWatch can
        compute p50/p90 across jobs.
        """
        with self._lock:
            self._stage_durations_ms[stage] = float(duration_ms)

    def record_job_duration(self, duration_ms: float) -> None:
        """Set the whole-job wall-clock (ms). Last-writer-wins, never summed."""
        with self._lock:
            self._job_duration_ms = float(duration_ms)

    def record_volume(self, name: str, count: int) -> None:
        """Set a per-job volume count (e.g. TablesProcessed, InductionJobErrors).

        Last-writer-wins per name — a single value per job, not an accumulator.
        """
        with self._lock:
            self._volume_counts[name] = int(count)

    @property
    def total_cost_usd(self) -> float:
        """Total accumulated USD cost across all recorded calls."""
        with self._lock:
            return self._total_cost_usd

    @property
    def input_tokens_by_stage(self) -> dict[str, int]:
        """Snapshot of accumulated input tokens keyed by stage."""
        with self._lock:
            return dict(self._input_tokens)

    @property
    def output_tokens_by_stage(self) -> dict[str, int]:
        """Snapshot of accumulated output tokens keyed by stage."""
        with self._lock:
            return dict(self._output_tokens)

    @property
    def rerank_invocations(self) -> int:
        """Count of recorded rerank-stage calls."""
        with self._lock:
            return self._rerank_invocations

    @property
    def rerank_latency_stats(self) -> dict[str, float] | None:
        """CloudWatch StatisticSet for rerank latency, or None if no samples."""
        with self._lock:
            if not self._rerank_latencies:
                return None
            samples = list(self._rerank_latencies)
        return {
            "SampleCount": float(len(samples)),
            "Sum": float(sum(samples)),
            "Minimum": float(min(samples)),
            "Maximum": float(max(samples)),
        }

    @property
    def stage_durations_ms(self) -> dict[str, float]:
        """Snapshot of recorded wall-clock durations (ms) keyed by stage."""
        with self._lock:
            return dict(self._stage_durations_ms)

    @property
    def job_duration_ms(self) -> float | None:
        """Recorded whole-job wall-clock (ms), or None if never set."""
        with self._lock:
            return self._job_duration_ms

    @property
    def volume_counts(self) -> dict[str, int]:
        """Snapshot of recorded per-job volume counts keyed by name."""
        with self._lock:
            return dict(self._volume_counts)


def _cloudwatch_client(region: str | None):
    """Create a boto3 CloudWatch client.

    An explicit ``region`` wins; otherwise fall back to the repo's shared
    ``resolve_region`` (AWS_REGION → AWS_DEFAULT_REGION → us-east-1) rather than
    a bespoke getenv — the ontology ECS task sets BEDROCK_REGION/LLM_REGION but
    not AWS_REGION, so a lone getenv would silently publish to us-east-1.
    """
    resolved = region or resolve_region()
    return boto3.client("cloudwatch", region_name=resolved)


def emit_induction_job_metrics(
    tracker: CostTracker,
    namespace_id: str,
    region: str | None = None,
    cloudwatch_client=None,
) -> None:
    """Publish one PutMetricData call summarising an induction job.

    Emits to the ``COA/Ontology`` namespace. Datums with a zero/empty value are
    omitted, EXCEPT ``InductionJobCostUsd`` which is always emitted (even 0.0).
    A single put stays far under the 1000-datum / 30-dimension PutMetricData
    caps, so no batching is needed. All failures are swallowed with a warning —
    metric emission must never fail an induction job.
    """
    ns_dim = [{"Name": "NamespaceId", "Value": namespace_id}]
    metric_data: list[dict] = []

    rerank_invocations = tracker.rerank_invocations
    if rerank_invocations:
        metric_data.append(
            {
                "MetricName": "RerankInvocations",
                "Unit": "Count",
                "Dimensions": ns_dim,
                "Value": float(rerank_invocations),
            }
        )

    latency_stats = tracker.rerank_latency_stats
    if latency_stats is not None:
        metric_data.append(
            {
                "MetricName": "RerankLatencyMs",
                "Unit": "Milliseconds",
                "Dimensions": ns_dim,
                "StatisticValues": latency_stats,
            }
        )

    for stage, tokens in tracker.input_tokens_by_stage.items():
        if tokens:
            metric_data.append(
                {
                    "MetricName": "BedrockInputTokens",
                    "Unit": "Count",
                    "Dimensions": [*ns_dim, {"Name": "Stage", "Value": stage}],
                    "Value": float(tokens),
                }
            )

    for stage, tokens in tracker.output_tokens_by_stage.items():
        if tokens:
            metric_data.append(
                {
                    "MetricName": "BedrockOutputTokens",
                    "Unit": "Count",
                    "Dimensions": [*ns_dim, {"Name": "Stage", "Value": stage}],
                    "Value": float(tokens),
                }
            )

    # Stage durations — one single-Value datum per recorded stage (NOT a
    # StatisticSet) so CloudWatch retains the per-job distribution and can
    # compute p50/p90 across jobs. Metric name is ``{Stage}DurationMs``.
    for stage, duration_ms in tracker.stage_durations_ms.items():
        metric_data.append(
            {
                "MetricName": f"{stage}DurationMs",
                "Unit": "Milliseconds",
                "Dimensions": ns_dim,
                "Value": float(duration_ms),
            }
        )

    # Job wall-clock — one single-Value datum per job (percentile-capable).
    job_duration_ms = tracker.job_duration_ms
    if job_duration_ms is not None:
        metric_data.append(
            {
                "MetricName": "InductionJobDurationMs",
                "Unit": "Milliseconds",
                "Dimensions": ns_dim,
                "Value": float(job_duration_ms),
            }
        )

    # Volume counts — TablesProcessed / ColumnsProcessed / GroundedClassesMatched
    # / NovelClassesCreated / InductionJobErrors. Omitted when never recorded
    # (None), but a recorded 0 is still emitted (a genuine 0-volume signal).
    for name, count in tracker.volume_counts.items():
        metric_data.append(
            {
                "MetricName": name,
                "Unit": "Count",
                "Dimensions": ns_dim,
                "Value": float(count),
            }
        )

    # Always emitted, even at 0.0 — a job that spent nothing is a meaningful signal.
    metric_data.append(
        {
            "MetricName": "InductionJobCostUsd",
            "Unit": "None",
            "Dimensions": ns_dim,
            "Value": tracker.total_cost_usd,
        }
    )

    try:
        client = cloudwatch_client or _cloudwatch_client(region)
        client.put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=metric_data)
    except Exception:  # noqa: BLE001
        # ponytail: metrics are best-effort; a PutMetricData failure must not fail induction
        logger.warning("emit_induction_job_metrics_failed", namespace_id=namespace_id, exc_info=True)


def _worker_rss_bytes() -> int:
    """Return this process's peak resident-set size in bytes.

    ``ru_maxrss`` is a high-water mark (peak RSS since process start), which is
    exactly what OOM observability wants — the pre-kill ramp, not the momentary
    value. On Linux it is reported in kibibytes, so scale by 1024 for bytes.
    """
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def emit_induction_heartbeat_metrics(
    namespace_id: str,
    region: str | None = None,
    cloudwatch_client=None,
) -> None:
    """Publish a lightweight per-tick liveness + memory sample for one induction job.

    Called from the 45s watchdog so a run SIGKILLed by the OOM-killer (exit 137)
    — which runs neither ``except`` nor ``finally``, so the terminal
    ``emit_induction_job_metrics`` never fires — still leaves a partial trail:

    - ``InductionHeartbeat`` (Count=1) proves the job was alive at each tick;
      its absence-after-presence with no terminal metric == a crash.
    - ``InductionWorkerMemoryBytes`` is the in-process peak RSS, sampled every
      45s independently of the 1-min ECS ``MemoryUtilization`` metric, so the
      pre-OOM memory ramp is captured even between the coarser ECS samples.

    Best-effort: all failures are swallowed with a warning — a PutMetricData
    error in a tick must never kill the heartbeat.
    """
    ns_dim = [{"Name": "NamespaceId", "Value": namespace_id}]
    metric_data = [
        {"MetricName": "InductionHeartbeat", "Unit": "Count", "Dimensions": ns_dim, "Value": 1.0},
        {
            "MetricName": "InductionWorkerMemoryBytes",
            "Unit": "Bytes",
            "Dimensions": ns_dim,
            "Value": float(_worker_rss_bytes()),
        },
    ]
    try:
        client = cloudwatch_client or _cloudwatch_client(region)
        client.put_metric_data(Namespace=METRICS_NAMESPACE, MetricData=metric_data)
    except Exception:  # noqa: BLE001
        # ponytail: best-effort per-tick emit; a failure must not stop the heartbeat
        logger.warning("emit_induction_heartbeat_metrics_failed", namespace_id=namespace_id, exc_info=True)
