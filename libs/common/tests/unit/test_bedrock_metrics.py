# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for per-induction-job Bedrock cost tracking and CloudWatch emission."""

from __future__ import annotations

import threading

import pytest
from coa_common import bedrock_metrics
from coa_common.bedrock_metrics import (
    METRICS_NAMESPACE,
    CostTracker,
    cost_for,
    emit_induction_job_metrics,
)

pytestmark = pytest.mark.unit

HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "us.anthropic.claude-sonnet-5"
COHERE_EMBED = "us.cohere.embed-v4:0"
TITAN_EMBED = "amazon.titan-embed-text-v2:0"
NOVA_LITE = "amazon.nova-lite-v1:0"
NOVA_PRO = "amazon.nova-pro-v1:0"
NOVA_PREMIER = "amazon.nova-premier-v1:0"


class FakeCloudWatch:
    """Captures put_metric_data calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_metric_data(self, **kwargs) -> None:
        self.calls.append(kwargs)


class RaisingCloudWatch:
    def put_metric_data(self, **kwargs) -> None:
        raise RuntimeError("boom")


class TestCostFor:
    def test_haiku_exact_math(self) -> None:
        # 1M input * $1/1M + 1M output * $5/1M = 6.00
        assert cost_for(HAIKU, 1_000_000, 1_000_000) == pytest.approx(6.00)

    def test_sonnet_exact_math(self) -> None:
        # 1M input * $2/1M + 1M output * $10/1M = 12.00 (promotional pricing)
        assert cost_for(SONNET, 1_000_000, 1_000_000) == pytest.approx(12.00)

    def test_input_and_output_priced_separately(self) -> None:
        # Haiku: 1000 input = $0.001, 0 output = $0
        assert cost_for(HAIKU, 1000, 0) == pytest.approx(0.001)
        # Haiku: 0 input, 1000 output = $0.005
        assert cost_for(HAIKU, 0, 1000) == pytest.approx(0.005)

    def test_unknown_sonnet_version_falls_back_to_sonnet_rate(self) -> None:
        # A model-version bump (id not in _PRICING) must not silently zero out.
        assert cost_for("us.anthropic.claude-sonnet-4-7", 1_000_000, 1_000_000) == pytest.approx(12.00)

    def test_unknown_haiku_version_falls_back_to_haiku_rate(self) -> None:
        assert cost_for("us.anthropic.claude-haiku-9-9-vNext", 1_000_000, 1_000_000) == pytest.approx(6.00)

    def test_unknown_embed_model_falls_back_to_cohere_embed_rate(self) -> None:
        # Cohere embed is the embed default; input-only pricing at $0.10/1M.
        assert cost_for("some-new.embed-model:0", 1_000_000, 999) == pytest.approx(0.10)

    def test_family_fallback_is_case_insensitive(self) -> None:
        assert cost_for("US.ANTHROPIC.CLAUDE-SONNET-XYZ", 1_000_000, 1_000_000) == pytest.approx(12.00)

    def test_truly_unknown_model_zero_cost_and_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings: list[tuple] = []
        monkeypatch.setattr(
            "coa_common.bedrock_metrics.logger.warning",
            lambda *args, **kwargs: warnings.append((args, kwargs)),
        )
        assert cost_for("gpt-foo", 1_000_000, 1_000_000) == 0.0
        assert warnings and warnings[0][0][0] == "bedrock_metrics_unknown_model_id"

    def test_cohere_embed_input_priced_output_free(self) -> None:
        # Cohere Embed v4: $0.10 / 1M input tokens, no output billing.
        assert cost_for(COHERE_EMBED, 1_000_000, 0) == pytest.approx(0.10)
        # Output tokens (should always be 0 for embeddings) never add cost.
        assert cost_for(COHERE_EMBED, 1_000_000, 999) == pytest.approx(0.10)

    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            # 1M in + 1M out at the AWS Price List on-demand rates (2026-07-29).
            (NOVA_LITE, 0.06 + 0.24),
            (NOVA_PRO, 0.80 + 3.20),
            (NOVA_PREMIER, 2.50 + 12.50),
        ],
    )
    def test_nova_models_are_priced_not_free(self, model_id: str, expected: float) -> None:
        cost = cost_for(model_id, 1_000_000, 1_000_000)
        assert cost > 0.0, f"{model_id} priced at $0.00 — cost tracking would silently under-report"
        assert cost == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            # The benchmark drives cross-region inference profiles ("us." prefixed),
            # which are not in _PRICING and must resolve via the per-variant family keys.
            ("us.amazon.nova-lite-v1:0", 0.06 + 0.24),
            ("us.amazon.nova-pro-v1:0", 0.80 + 3.20),
            ("us.amazon.nova-premier-v1:0", 2.50 + 12.50),
        ],
    )
    def test_nova_inference_profile_ids_price_per_variant(self, model_id: str, expected: float) -> None:
        assert cost_for(model_id, 1_000_000, 1_000_000) == pytest.approx(expected)

    def test_unlisted_nova_variant_is_not_silently_priced_as_another(self) -> None:
        # No bare "nova" family key: nova-micro must warn + return 0.0 rather than
        # borrowing Pro's rate (a 13x error).
        assert cost_for("us.amazon.nova-micro-v1:0", 1_000_000, 1_000_000) == 0.0

    def test_titan_embed_input_priced_output_free(self) -> None:
        # Titan Text Embeddings V2: $0.02 / 1M input tokens, no output billing.
        assert cost_for(TITAN_EMBED, 1_000_000, 0) == pytest.approx(0.02)
        assert cost_for(TITAN_EMBED, 1_000_000, 999) == pytest.approx(0.02)


class TestCostTracker:
    def test_starts_empty(self) -> None:
        tracker = CostTracker()
        assert tracker.total_cost_usd == 0.0
        assert tracker.input_tokens_by_stage == {}
        assert tracker.output_tokens_by_stage == {}
        assert tracker.rerank_invocations == 0
        assert tracker.rerank_latency_stats is None

    def test_record_accumulates_across_stages_and_calls(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 100, 50)
        tracker.record("generate", HAIKU, 200, 100)
        tracker.record("rerank", SONNET, 300, 150)

        assert tracker.input_tokens_by_stage == {"generate": 300, "rerank": 300}
        assert tracker.output_tokens_by_stage == {"generate": 150, "rerank": 150}

    def test_total_cost_sums_per_call_costs(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 1_000_000, 1_000_000)  # 6.00
        tracker.record("rerank", SONNET, 1_000_000, 1_000_000)  # 12.00
        assert tracker.total_cost_usd == pytest.approx(18.00)

    def test_embed_stage_accumulates_input_tokens_and_cost(self) -> None:
        tracker = CostTracker()
        tracker.record("embed", COHERE_EMBED, 1_000_000, 0)
        tracker.record("embed", COHERE_EMBED, 500_000, 0)

        # Input tokens accumulate under the "embed" stage; no output tokens.
        assert tracker.input_tokens_by_stage == {"embed": 1_500_000}
        assert tracker.output_tokens_by_stage == {"embed": 0}
        # 1.5M input tokens * $0.10/1M = $0.15
        assert tracker.total_cost_usd == pytest.approx(0.15)
        # Embed is not a rerank — no invocation/latency side effects.
        assert tracker.rerank_invocations == 0

    def test_embed_cost_sums_with_generate_and_rerank(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 1_000_000, 1_000_000)  # 6.00
        tracker.record("rerank", SONNET, 1_000_000, 1_000_000)  # 12.00
        tracker.record("embed", TITAN_EMBED, 1_000_000, 0)  # 0.02
        assert tracker.total_cost_usd == pytest.approx(18.02)

    def test_unknown_model_counts_tokens_but_no_cost(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", "unknown-model", 500, 250)

        assert tracker.total_cost_usd == 0.0
        assert tracker.input_tokens_by_stage == {"generate": 500}
        assert tracker.output_tokens_by_stage == {"generate": 250}

    def test_rerank_invocations_only_count_rerank(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 10, 5)
        tracker.record("rerank", HAIKU, 10, 5)
        tracker.record("rerank", HAIKU, 10, 5)
        assert tracker.rerank_invocations == 2

    def test_rerank_latency_stats(self) -> None:
        tracker = CostTracker()
        tracker.record("rerank", HAIKU, 10, 5, latency_ms=100.0)
        tracker.record("rerank", HAIKU, 10, 5, latency_ms=300.0)
        tracker.record("rerank", HAIKU, 10, 5, latency_ms=200.0)

        stats = tracker.rerank_latency_stats
        assert stats == {
            "SampleCount": 3.0,
            "Sum": 600.0,
            "Minimum": 100.0,
            "Maximum": 300.0,
        }

    def test_generate_latency_not_recorded_in_rerank_stats(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 10, 5, latency_ms=999.0)
        assert tracker.rerank_latency_stats is None

    def test_rerank_without_latency_still_counts_invocation(self) -> None:
        tracker = CostTracker()
        tracker.record("rerank", HAIKU, 10, 5)
        assert tracker.rerank_invocations == 1
        assert tracker.rerank_latency_stats is None

    def test_thread_safety_smoke(self) -> None:
        tracker = CostTracker()
        n_threads = 20
        per_thread = 100

        def worker() -> None:
            for _ in range(per_thread):
                tracker.record("generate", HAIKU, 1, 2)
                tracker.record("rerank", HAIKU, 3, 4, latency_ms=5.0)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = n_threads * per_thread
        assert tracker.input_tokens_by_stage == {"generate": total * 1, "rerank": total * 3}
        assert tracker.output_tokens_by_stage == {"generate": total * 2, "rerank": total * 4}
        assert tracker.rerank_invocations == total
        stats = tracker.rerank_latency_stats
        assert stats is not None
        assert stats["SampleCount"] == float(total)
        assert stats["Sum"] == pytest.approx(5.0 * total)


class TestRecordFromConverse:
    def test_extracts_usage_from_converse_response(self) -> None:
        tracker = CostTracker()
        resp = {"usage": {"inputTokens": 100, "outputTokens": 50}, "output": {}}
        tracker.record_from_converse("generate", HAIKU, resp)

        assert tracker.input_tokens_by_stage == {"generate": 100}
        assert tracker.output_tokens_by_stage == {"generate": 50}

    def test_missing_usage_applies_zero_defaults(self) -> None:
        tracker = CostTracker()
        tracker.record_from_converse("generate", HAIKU, {"output": {}})

        assert tracker.input_tokens_by_stage == {"generate": 0}
        assert tracker.output_tokens_by_stage == {"generate": 0}
        assert tracker.total_cost_usd == 0.0

    def test_none_response_is_noop_safe(self) -> None:
        tracker = CostTracker()
        tracker.record_from_converse("generate", HAIKU, None)  # type: ignore[arg-type]
        assert tracker.input_tokens_by_stage == {"generate": 0}

    def test_passes_latency_through_for_rerank(self) -> None:
        tracker = CostTracker()
        resp = {"usage": {"inputTokens": 200, "outputTokens": 80}}
        tracker.record_from_converse("rerank", SONNET, resp, latency_ms=42.0)

        assert tracker.rerank_invocations == 1
        assert tracker.rerank_latency_stats == {
            "SampleCount": 1.0,
            "Sum": 42.0,
            "Minimum": 42.0,
            "Maximum": 42.0,
        }


def _metric_by_name(call: dict, name: str) -> list[dict]:
    return [d for d in call["MetricData"] if d["MetricName"] == name]


class TestEmitInductionJobMetrics:
    def test_single_put_with_correct_namespace(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 100, 50)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)

        assert len(cw.calls) == 1
        assert cw.calls[0]["Namespace"] == METRICS_NAMESPACE == "COA/Ontology"

    def test_payload_names_units_dimensions_values(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 100, 50)
        tracker.record("rerank", SONNET, 200, 80, latency_ms=42.0)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        # RerankInvocations
        rerank = _metric_by_name(call, "RerankInvocations")[0]
        assert rerank["Unit"] == "Count"
        assert rerank["Value"] == 1.0
        assert rerank["Dimensions"] == [{"Name": "NamespaceId", "Value": "ns-1"}]

        # RerankLatencyMs (StatisticValues)
        latency = _metric_by_name(call, "RerankLatencyMs")[0]
        assert latency["Unit"] == "Milliseconds"
        assert latency["StatisticValues"] == {
            "SampleCount": 1.0,
            "Sum": 42.0,
            "Minimum": 42.0,
            "Maximum": 42.0,
        }

        # BedrockInputTokens — one per stage, carries Stage dimension
        in_gen = next(d for d in _metric_by_name(call, "BedrockInputTokens") if _stage_of(d) == "generate")
        assert in_gen["Unit"] == "Count"
        assert in_gen["Value"] == 100.0
        assert {"Name": "Stage", "Value": "generate"} in in_gen["Dimensions"]
        assert {"Name": "NamespaceId", "Value": "ns-1"} in in_gen["Dimensions"]
        in_rerank = next(d for d in _metric_by_name(call, "BedrockInputTokens") if _stage_of(d) == "rerank")
        assert in_rerank["Value"] == 200.0

        # BedrockOutputTokens — one per stage
        out_gen = next(d for d in _metric_by_name(call, "BedrockOutputTokens") if _stage_of(d) == "generate")
        assert out_gen["Value"] == 50.0
        out_rerank = next(d for d in _metric_by_name(call, "BedrockOutputTokens") if _stage_of(d) == "rerank")
        assert out_rerank["Value"] == 80.0

        # InductionJobCostUsd
        cost = _metric_by_name(call, "InductionJobCostUsd")[0]
        assert cost["Unit"] == "None"
        assert cost["Dimensions"] == [{"Name": "NamespaceId", "Value": "ns-1"}]
        assert cost["Value"] == pytest.approx(tracker.total_cost_usd)

    def test_embed_stage_emitted_as_input_tokens_datum(self) -> None:
        # Embed tokens flow through the same {NamespaceId, Stage} schema the
        # dashboard already searches — no new metric name, just Stage=embed.
        tracker = CostTracker()
        tracker.record("embed", COHERE_EMBED, 4200, 0)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        in_embed = next(d for d in _metric_by_name(call, "BedrockInputTokens") if _stage_of(d) == "embed")
        assert in_embed["Unit"] == "Count"
        assert in_embed["Value"] == 4200.0
        assert {"Name": "Stage", "Value": "embed"} in in_embed["Dimensions"]
        assert {"Name": "NamespaceId", "Value": "ns-1"} in in_embed["Dimensions"]
        # Zero output tokens for embeddings -> no output datum emitted.
        assert _metric_by_name(call, "BedrockOutputTokens") == []
        # Cost includes the embed spend: 4200 * $0.10/1M.
        cost = _metric_by_name(call, "InductionJobCostUsd")[0]
        assert cost["Value"] == pytest.approx(4200 * 0.10 / 1_000_000)

    def test_cost_always_emitted_even_at_zero(self) -> None:
        tracker = CostTracker()  # nothing recorded
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        cost = _metric_by_name(call, "InductionJobCostUsd")
        assert len(cost) == 1
        assert cost[0]["Value"] == 0.0
        # Nothing else should be present when nothing was recorded.
        assert _metric_by_name(call, "RerankInvocations") == []
        assert _metric_by_name(call, "RerankLatencyMs") == []
        assert _metric_by_name(call, "BedrockInputTokens") == []
        assert _metric_by_name(call, "BedrockOutputTokens") == []

    def test_latency_omitted_when_no_samples(self) -> None:
        tracker = CostTracker()
        tracker.record("rerank", HAIKU, 10, 5)  # invocation but no latency
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        assert _metric_by_name(call, "RerankLatencyMs") == []
        assert _metric_by_name(call, "RerankInvocations")[0]["Value"] == 1.0

    def test_zero_token_stage_datum_omitted(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 0, 0)  # zero tokens
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        assert _metric_by_name(call, "BedrockInputTokens") == []
        assert _metric_by_name(call, "BedrockOutputTokens") == []

    def test_client_error_is_swallowed(self) -> None:
        tracker = CostTracker()
        tracker.record("generate", HAIKU, 100, 50)

        # Must not raise.
        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=RaisingCloudWatch())


def _stage_of(datum: dict) -> str | None:
    for dim in datum["Dimensions"]:
        if dim["Name"] == "Stage":
            return dim["Value"]
    return None


class TestStageDurationAndVolumeTracking:
    def test_starts_empty(self) -> None:
        tracker = CostTracker()
        assert tracker.stage_durations_ms == {}
        assert tracker.job_duration_ms is None
        assert tracker.volume_counts == {}

    def test_record_stage_duration_is_last_writer(self) -> None:
        tracker = CostTracker()
        tracker.record_stage_duration("Induce", 100.0)
        tracker.record_stage_duration("Induce", 250.0)  # overwrite, not sum
        tracker.record_stage_duration("Store", 40.0)
        assert tracker.stage_durations_ms == {"Induce": 250.0, "Store": 40.0}

    def test_record_job_duration_is_last_writer(self) -> None:
        tracker = CostTracker()
        tracker.record_job_duration(1000.0)
        tracker.record_job_duration(2000.0)
        assert tracker.job_duration_ms == 2000.0

    def test_record_volume_is_last_writer(self) -> None:
        tracker = CostTracker()
        tracker.record_volume("TablesProcessed", 5)
        tracker.record_volume("TablesProcessed", 13)  # overwrite, not sum
        tracker.record_volume("InductionJobErrors", 1)
        assert tracker.volume_counts == {"TablesProcessed": 13, "InductionJobErrors": 1}

    def test_thread_safe_last_writer_smoke(self) -> None:
        tracker = CostTracker()

        def worker() -> None:
            for _ in range(200):
                tracker.record_stage_duration("Induce", 5.0)
                tracker.record_volume("TablesProcessed", 7)
                tracker.record_job_duration(9.0)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Single value per key regardless of concurrency (set, not accumulate).
        assert tracker.stage_durations_ms == {"Induce": 5.0}
        assert tracker.volume_counts == {"TablesProcessed": 7}
        assert tracker.job_duration_ms == 9.0


class TestEmitDurationAndVolumeDatums:
    def test_stage_durations_emitted_as_single_value_not_statistic_set(self) -> None:
        tracker = CostTracker()
        tracker.record_stage_duration("FetchMetadata", 12.0)
        tracker.record_stage_duration("Induce", 3456.0)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        fetch = _metric_by_name(call, "FetchMetadataDurationMs")[0]
        assert fetch["Unit"] == "Milliseconds"
        assert fetch["Value"] == 12.0
        assert "StatisticValues" not in fetch  # percentile-capable single Value
        assert fetch["Dimensions"] == [{"Name": "NamespaceId", "Value": "ns-1"}]

        induce = _metric_by_name(call, "InduceDurationMs")[0]
        assert induce["Value"] == 3456.0
        assert "StatisticValues" not in induce

    def test_job_duration_emitted_as_single_value(self) -> None:
        tracker = CostTracker()
        tracker.record_job_duration(98765.0)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        job = _metric_by_name(cw.calls[0], "InductionJobDurationMs")[0]
        assert job["Unit"] == "Milliseconds"
        assert job["Value"] == 98765.0
        assert "StatisticValues" not in job

    def test_volume_counts_emitted_as_count_values(self) -> None:
        tracker = CostTracker()
        tracker.record_volume("TablesProcessed", 13)
        tracker.record_volume("ColumnsProcessed", 65)
        tracker.record_volume("NovelClassesCreated", 4)
        tracker.record_volume("GroundedClassesMatched", 9)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        for name, expected in (
            ("TablesProcessed", 13.0),
            ("ColumnsProcessed", 65.0),
            ("NovelClassesCreated", 4.0),
            ("GroundedClassesMatched", 9.0),
        ):
            datum = _metric_by_name(call, name)[0]
            assert datum["Unit"] == "Count"
            assert datum["Value"] == expected
            assert datum["Dimensions"] == [{"Name": "NamespaceId", "Value": "ns-1"}]

    def test_recorded_zero_volume_still_emitted(self) -> None:
        # A recorded 0 (e.g. NovelClassesCreated=0 on a fully-grounded run) is a
        # genuine signal, distinct from "never recorded" — it IS emitted.
        tracker = CostTracker()
        tracker.record_volume("NovelClassesCreated", 0)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        novel = _metric_by_name(cw.calls[0], "NovelClassesCreated")
        assert len(novel) == 1
        assert novel[0]["Value"] == 0.0

    def test_unrecorded_duration_and_volume_datums_omitted(self) -> None:
        # Nothing recorded → none of the new datums appear (cost is still always
        # emitted, asserted elsewhere).
        tracker = CostTracker()
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="ns-1", cloudwatch_client=cw)
        call = cw.calls[0]

        assert _metric_by_name(call, "InductionJobDurationMs") == []
        assert _metric_by_name(call, "FetchMetadataDurationMs") == []
        assert _metric_by_name(call, "InduceDurationMs") == []
        assert _metric_by_name(call, "TablesProcessed") == []
        assert _metric_by_name(call, "InductionJobErrors") == []

    def test_error_volume_emitted_on_failure_shape(self) -> None:
        # Mirrors the failure path: only InductionJobErrors + job duration.
        tracker = CostTracker()
        tracker.record_volume("InductionJobErrors", 1)
        tracker.record_job_duration(500.0)
        cw = FakeCloudWatch()

        emit_induction_job_metrics(tracker, namespace_id="fail-ns", cloudwatch_client=cw)
        call = cw.calls[0]

        errors = _metric_by_name(call, "InductionJobErrors")[0]
        assert errors["Unit"] == "Count"
        assert errors["Value"] == 1.0
        assert _metric_by_name(call, "InductionJobDurationMs")[0]["Value"] == 500.0


class TestCloudwatchClientRegion:
    def test_explicit_region_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        monkeypatch.setattr(
            bedrock_metrics.boto3,
            "client",
            lambda service, region_name: captured.update(service=service, region=region_name),
        )
        # resolve_region must NOT be consulted when region is explicit.
        monkeypatch.setattr(bedrock_metrics, "resolve_region", lambda: "eu-west-1")
        bedrock_metrics._cloudwatch_client("ap-south-1")
        assert captured == {"service": "cloudwatch", "region": "ap-south-1"}

    def test_none_region_resolves_via_resolve_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        monkeypatch.setattr(
            bedrock_metrics.boto3,
            "client",
            lambda service, region_name: captured.update(service=service, region=region_name),
        )
        monkeypatch.setattr(bedrock_metrics, "resolve_region", lambda: "eu-central-1")
        bedrock_metrics._cloudwatch_client(None)
        assert captured["region"] == "eu-central-1"
