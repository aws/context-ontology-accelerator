# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for guardrail decision observability."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_common.guardrail_metrics import (
    DECISION_ANONYMIZED,
    DECISION_UNKNOWN,
    METRICS_NAMESPACE,
    _client_for_region,
    _cloudwatch_client,
    assessments_from_trace,
    emit_guardrail_decision,
    filter_type_from_assessments,
    redacted_actions,
)

pytestmark = pytest.mark.unit

_BLOCK = {"contentPolicy": {"filters": [{"type": "PROMPT_ATTACK", "action": "BLOCKED"}]}}
_ANONYMIZE = {"sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED"}]}}
_TOPIC = {"topicPolicy": {"topics": [{"name": "medical-advice", "action": "BLOCKED"}]}}


def _metrics_by_name(client: MagicMock) -> dict[str, dict]:
    """Index the single put_metric_data call's MetricData by MetricName."""
    client.put_metric_data.assert_called_once()
    kwargs = client.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == METRICS_NAMESPACE
    return {m["MetricName"]: m for m in kwargs["MetricData"]}


def _emf_metric_names(stdout: str) -> set[str]:
    """Metric names from the EMF documents on stdout.

    Selects EMF documents by their ``_aws`` envelope rather than assuming every
    JSON line is one: a sibling package's tests configure structlog to write
    JSON logs to stdout, and those lines also start with ``{``. Asserts at
    least one document was found so a caller cannot pass vacuously when the
    emitter published nothing at all.
    """
    docs = []
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and "_aws" in doc:
            docs.append(doc)

    assert docs, f"no EMF documents on stdout: {stdout!r}"
    return {m["Name"] for doc in docs for family in doc["_aws"]["CloudWatchMetrics"] for m in family["Metrics"]}


class TestFilterTypeFromAssessments:
    """filter_type is the log field distinguishing WHY a guardrail fired."""

    def test_content_block_reports_content(self):
        assert filter_type_from_assessments([_BLOCK]) == "CONTENT"

    def test_topic_block_reports_topic(self):
        assert filter_type_from_assessments([_TOPIC]) == "TOPIC"

    def test_anonymize_reports_pii_not_none(self):
        # A non-blocking PII action still fired — reporting NONE would hide it.
        assert filter_type_from_assessments([_ANONYMIZE]) == "PII"

    def test_no_action_reports_none(self):
        assert filter_type_from_assessments([{"contentPolicy": {"filters": [{"action": "NONE"}]}}]) == "NONE"

    def test_empty_and_malformed_report_none(self):
        assert filter_type_from_assessments([]) == "NONE"
        assert filter_type_from_assessments(None) == "NONE"
        assert filter_type_from_assessments(["not-a-dict"]) == "NONE"

    def test_content_wins_over_pii_when_both_fire(self):
        assert filter_type_from_assessments([_ANONYMIZE, _BLOCK]) == "CONTENT"


def test_redacted_actions_skips_an_action_of_none_like_filter_type():
    assessment = {
        "contentPolicy": {
            "filters": [{"type": "HATE", "action": "NONE"}, {"type": "PROMPT_ATTACK", "action": "BLOCKED"}]
        }
    }
    assert redacted_actions([assessment]) == ["contentPolicy:PROMPT_ATTACK=BLOCKED"]


class TestAssessmentsFromTrace:
    """Converse traces nest assessments differently per key — all must flatten."""

    def test_flattens_input_assessment_dict_values(self):
        trace = {"inputAssessment": {"gr-1": _BLOCK}}
        assert assessments_from_trace(trace) == [_BLOCK]

    def test_flattens_output_assessments_list_values(self):
        trace = {"outputAssessments": {"gr-1": [_BLOCK, _ANONYMIZE]}}
        assert assessments_from_trace(trace) == [_BLOCK, _ANONYMIZE]

    def test_empty_and_malformed_return_empty(self):
        assert assessments_from_trace({}) == []
        assert assessments_from_trace(None) == []
        assert assessments_from_trace({"inputAssessment": "junk"}) == []


class TestEmitPutTransport:
    """ECS path: one PutMetricData call carrying the right metrics."""

    def test_block_emits_invocations_blocked_and_latency(self):
        client = MagicMock()
        emit_guardrail_decision(
            component="kg-build", blocked=True, latency_ms=42.5, filter_type="CONTENT", cloudwatch_client=client
        )

        metrics = _metrics_by_name(client)
        assert set(metrics) == {"GuardrailInvocations", "GuardrailBlocked", "GuardrailLatency"}
        assert metrics["GuardrailInvocations"]["Value"] == 1.0
        assert metrics["GuardrailBlocked"]["Value"] == 1.0
        assert metrics["GuardrailLatency"]["Value"] == 42.5
        assert metrics["GuardrailLatency"]["Unit"] == "Milliseconds"
        assert {"Name": "Decision", "Value": "BLOCK"} in metrics["GuardrailInvocations"]["Dimensions"]

    def test_allow_emits_invocations_and_latency_but_not_blocked(self):
        client = MagicMock()
        emit_guardrail_decision(component="kg-build", blocked=False, latency_ms=7.0, cloudwatch_client=client)

        metrics = _metrics_by_name(client)
        assert "GuardrailBlocked" not in metrics
        assert set(metrics) == {"GuardrailInvocations", "GuardrailLatency"}
        assert {"Name": "Decision", "Value": "ALLOW"} in metrics["GuardrailInvocations"]["Dimensions"]

    def test_block_rate_is_never_emitted(self):
        # Block rate is a CloudWatch math expression over the two counters —
        # publishing it would be a competing source of truth.
        client = MagicMock()
        emit_guardrail_decision(component="kg-build", blocked=True, latency_ms=1.0, cloudwatch_client=client)
        assert not [n for n in _metrics_by_name(client) if "Rate" in n]

    def test_component_dimension_is_carried(self):
        client = MagicMock()
        emit_guardrail_decision(component="enrichment", blocked=False, latency_ms=1.0, cloudwatch_client=client)
        for metric in _metrics_by_name(client).values():
            assert {"Name": "Component", "Value": "enrichment"} in metric["Dimensions"]

    def test_put_failure_does_not_raise_into_decision_path(self):
        # A guardrail BLOCK that failed to be enforced because telemetry broke
        # would be a security regression.
        client = MagicMock()
        client.put_metric_data.side_effect = RuntimeError("cloudwatch down")
        emit_guardrail_decision(component="kg-build", blocked=True, latency_ms=1.0, cloudwatch_client=client)

    def test_telemetry_double_fault_does_not_raise_into_decision_path(self):
        # The error handler is itself a failure point: on ECS a broken stdout
        # (closed pipe, full log buffer) makes the warning raise, and an
        # unprotected warning would let that escape into the caller — skipping
        # the ``raise GuardrailBlockedError`` that sits after this emit.
        client = MagicMock()
        client.put_metric_data.side_effect = RuntimeError("cloudwatch down")
        with patch("coa_common.guardrail_metrics.logger") as mock_logger:
            mock_logger.info.side_effect = RuntimeError("stdout closed")
            mock_logger.warning.side_effect = RuntimeError("stdout closed")
            emit_guardrail_decision(component="kg-build", blocked=True, latency_ms=1.0, cloudwatch_client=client)


class TestCloudWatchClientRegion:
    """Metrics must land in the region the component actually runs in.

    No ECS task definition sets AWS_REGION (the stacks set NDB_REGION /
    BEDROCK_REGION / … instead), so a client built from the env default lands in
    us-east-1 while the deploy targets somewhere else — metrics filed to a region
    nobody watches.
    """

    @pytest.fixture(autouse=True)
    def _cold_client_cache(self):
        # _client_for_region is memoized; a warm entry would skip the boto3 call
        # these tests assert on.
        _client_for_region.cache_clear()
        yield
        _client_for_region.cache_clear()

    def test_passed_region_wins_over_env_default(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        with patch("coa_common.guardrail_metrics.boto3") as mock_boto3:
            _cloudwatch_client("us-west-2")
        assert mock_boto3.client.call_args.kwargs["region_name"] == "us-west-2"

    def test_falls_back_to_resolve_region_not_bespoke_getenv(self, monkeypatch):
        # AWS_DEFAULT_REGION alone must still be honoured — a lone
        # os.getenv("AWS_REGION", "us-east-1") would miss it.
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
        with patch("coa_common.guardrail_metrics.boto3") as mock_boto3:
            _cloudwatch_client(None)
        assert mock_boto3.client.call_args.kwargs["region_name"] == "eu-west-1"

    def test_repeat_calls_reuse_one_client_per_region(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        west, east = MagicMock(name="west"), MagicMock(name="east")
        with patch("coa_common.guardrail_metrics.boto3") as mock_boto3:
            mock_boto3.client.side_effect = [west, east]
            first = _cloudwatch_client("us-west-2")
            second = _cloudwatch_client("us-west-2")
            other = _cloudwatch_client("eu-west-1")
        assert first is second is west
        assert other is east
        # Two constructions for three calls: the repeat was served from cache.
        assert mock_boto3.client.call_count == 2

    def test_emit_threads_region_through_to_the_client(self, _no_real_cloudwatch):
        # _no_real_cloudwatch is the autouse patch of the client factory; asserting
        # on it proves the region reaches the factory rather than being dropped.
        emit_guardrail_decision(component="kg-build", blocked=True, latency_ms=1.0, region="us-west-2")
        assert _no_real_cloudwatch.call_args[0][0] == "us-west-2"


class TestEmitEmfTransport:
    """Lambda/AgentCore path: EMF on stdout, no API call."""

    def test_emf_writes_three_metrics_on_block(self, capsys):
        emit_guardrail_decision(
            component="nl-to-sparql", blocked=True, latency_ms=12.0, filter_type="TOPIC", transport="emf"
        )

        names = _emf_metric_names(capsys.readouterr().out)
        assert names == {"GuardrailInvocations", "GuardrailBlocked", "GuardrailLatency"}

    def test_emf_omits_blocked_on_allow(self, capsys):
        emit_guardrail_decision(component="nl-to-sparql", blocked=False, latency_ms=3.0, transport="emf")

        # Assert on the EMF metric names, not a substring of the whole stdout
        # blob: a bare `not in` check would also pass if nothing were emitted.
        names = _emf_metric_names(capsys.readouterr().out)
        assert names == {"GuardrailInvocations", "GuardrailLatency"}

    def test_emf_makes_no_cloudwatch_api_call(self):
        with patch("coa_common.guardrail_metrics._cloudwatch_client") as mock_boto:
            emit_guardrail_decision(component="nl-to-sparql", blocked=True, latency_ms=1.0, transport="emf")
        mock_boto.assert_not_called()


class TestDecisionLogLine:
    """One structured decision line, on allow AND block."""

    @pytest.mark.parametrize(
        ("blocked", "expected_decision"),
        [(True, "BLOCK"), (False, "ALLOW")],
    )
    def test_logs_decision_with_required_keys(self, blocked, expected_decision):
        with patch("coa_common.guardrail_metrics.logger") as mock_logger:
            emit_guardrail_decision(
                component="kg-build",
                blocked=blocked,
                latency_ms=15.678,
                filter_type="CONTENT",
                cloudwatch_client=MagicMock(),
            )

        event, kwargs = mock_logger.info.call_args[0][0], mock_logger.info.call_args.kwargs
        assert event == "guardrail_decision"
        assert kwargs == {
            "component": "kg-build",
            "decision": expected_decision,
            "filter_type": "CONTENT",
            "latency_ms": 15.68,
        }


@pytest.mark.unit
class TestUnknownDecision:
    """An unexplained suppression is not a confirmed block.

    ``GuardrailBlocked`` is documented as "1 only when the guardrail intervened with
    a block". Folding an unclassifiable intervention into it would make both numbers
    unrecoverable, so ``UNKNOWN`` gets its own counter.
    """

    def test_explicit_decision_overrides_the_derived_one(self, capsys):
        emit_guardrail_decision(
            component="serve",
            blocked=False,
            latency_ms=1.0,
            transport="emf",
            decision=DECISION_UNKNOWN,
        )

        out = capsys.readouterr().out
        assert "UNKNOWN" in out
        assert "GuardrailUnknown" in out
        # Not a confirmed block, so the block counter stays untouched.
        assert "GuardrailBlocked" not in out

    def test_omitted_decision_still_derives_from_blocked(self, capsys):
        """Back-compat: every pre-existing call site omits ``decision``."""
        emit_guardrail_decision(component="serve", blocked=True, latency_ms=1.0, transport="emf")

        out = capsys.readouterr().out
        assert "BLOCK" in out
        assert "GuardrailBlocked" in out
        assert "GuardrailUnknown" not in out

    def test_anonymized_decision_is_not_a_block(self, capsys):
        emit_guardrail_decision(
            component="serve",
            blocked=False,
            latency_ms=1.0,
            transport="emf",
            decision=DECISION_ANONYMIZED,
        )

        out = capsys.readouterr().out
        assert "ANONYMIZED" in out
        assert "GuardrailBlocked" not in out
        assert "GuardrailUnknown" not in out

    def test_put_transport_emits_unknown_metric(self):
        cw = MagicMock()
        emit_guardrail_decision(
            component="kg-build",
            blocked=False,
            latency_ms=2.0,
            transport="put",
            decision=DECISION_UNKNOWN,
            cloudwatch_client=cw,
        )

        metric_names = {m["MetricName"] for m in cw.put_metric_data.call_args.kwargs["MetricData"]}
        assert "GuardrailUnknown" in metric_names
        assert "GuardrailBlocked" not in metric_names
