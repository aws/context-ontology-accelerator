# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for enrichment_metrics module."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from coa_common.bedrock import BedrockTruncationError
from coa_sources.database.pipeline.enrichment_metrics import (
    BedrockTokenUsageCounter,
    EnrichmentContext,
    EnrichmentMetricEmitter,
    _classify_bedrock_error,
    _resolve_engine,
)

# ── _resolve_engine ────────────────────────────────────────────────────────


class TestResolveEngine:
    def test_jdbc_source_extracts_engine_lowercase(self) -> None:
        source_item = {"configuration": json.dumps({"engine": "POSTGRESQL", "host": "db.example.com"})}
        assert _resolve_engine(source_item) == "postgresql"

    def test_jdbc_source_mysql(self) -> None:
        source_item = {"configuration": json.dumps({"engine": "MYSQL"})}
        assert _resolve_engine(source_item) == "mysql"

    def test_jdbc_source_snowflake(self) -> None:
        source_item = {"configuration": json.dumps({"engine": "SNOWFLAKE"})}
        assert _resolve_engine(source_item) == "snowflake"

    def test_glue_source_defaults_to_glue(self) -> None:
        source_item = {"configuration": json.dumps({"databaseName": "my_db", "catalogId": "123"})}
        assert _resolve_engine(source_item) == "glue"

    def test_missing_configuration_defaults_to_glue(self) -> None:
        source_item: dict[str, str] = {}
        assert _resolve_engine(source_item) == "glue"

    def test_configuration_already_dict(self) -> None:
        source_item = {"configuration": {"engine": "REDSHIFT"}}
        assert _resolve_engine(source_item) == "redshift"

    def test_empty_engine_field_defaults_to_glue(self) -> None:
        source_item = {"configuration": json.dumps({"engine": ""})}
        assert _resolve_engine(source_item) == "glue"

    def test_malformed_json_defaults_to_glue(self) -> None:
        source_item = {"configuration": "not valid json{{{"}
        assert _resolve_engine(source_item) == "glue"


# ── EnrichmentContext ──────────────────────────────────────────────────────


class TestEnrichmentContext:
    def test_derives_engine_from_jdbc_source(self) -> None:
        source_item = {"configuration": json.dumps({"engine": "ORACLE"})}
        ctx = EnrichmentContext(source_item=source_item, namespace_id="ns-abc")

        assert ctx.engine == "oracle"
        assert ctx.namespace_id == "ns-abc"

    def test_derives_glue_when_no_engine(self) -> None:
        source_item = {"configuration": json.dumps({"databaseName": "db"})}
        ctx = EnrichmentContext(source_item=source_item, namespace_id="ns-xyz")

        assert ctx.engine == "glue"


# ── BedrockTokenUsageCounter ──────────────────────────────────────────────


class TestBedrockTokenUsageCounter:
    def test_starts_at_zero(self) -> None:
        counter = BedrockTokenUsageCounter()
        assert counter.input_tokens == 0
        assert counter.output_tokens == 0
        assert counter.estimated_calls_cost_usd == 0.0

    def test_record_call_accumulates(self) -> None:
        counter = BedrockTokenUsageCounter()
        counter.record_call(100, 50)
        counter.record_call(200, 100)

        assert counter.input_tokens == 300
        assert counter.output_tokens == 150

    def test_estimated_cost_calculation(self) -> None:
        counter = BedrockTokenUsageCounter()
        # 1M input tokens = $1.00, 1M output tokens = $5.00
        counter.record_call(1_000_000, 1_000_000)

        assert counter.estimated_calls_cost_usd == pytest.approx(6.00)

    def test_estimated_cost_small_usage(self) -> None:
        counter = BedrockTokenUsageCounter()
        counter.record_call(1000, 500)

        # 1000 * 1.00/1M + 500 * 5.00/1M = 0.001 + 0.0025 = 0.0035
        assert counter.estimated_calls_cost_usd == pytest.approx(0.0035)


# ── _classify_bedrock_error ────────────────────────────────────────────────


class TestClassifyBedrockError:
    def test_truncation_error(self) -> None:
        exc = BedrockTruncationError(model_id="test-model", output_tokens=512, max_tokens=512)
        assert _classify_bedrock_error(exc) == "Truncated"

    def test_throttling_exception(self) -> None:
        exc = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "InvokeModel")
        assert _classify_bedrock_error(exc) == "Throttling"

    def test_too_many_requests(self) -> None:
        exc = ClientError({"Error": {"Code": "TooManyRequestsException", "Message": ""}}, "InvokeModel")
        assert _classify_bedrock_error(exc) == "Throttling"

    def test_validation_exception(self) -> None:
        exc = ClientError({"Error": {"Code": "ValidationException", "Message": "bad input"}}, "InvokeModel")
        assert _classify_bedrock_error(exc) == "Validation"

    def test_model_not_ready(self) -> None:
        exc = ClientError({"Error": {"Code": "ModelNotReadyException", "Message": ""}}, "InvokeModel")
        assert _classify_bedrock_error(exc) == "Validation"

    def test_access_denied(self) -> None:
        exc = ClientError({"Error": {"Code": "AccessDeniedException", "Message": ""}}, "InvokeModel")
        assert _classify_bedrock_error(exc) == "Guardrail"

    def test_model_error(self) -> None:
        exc = ClientError({"Error": {"Code": "ModelErrorException", "Message": ""}}, "InvokeModel")
        assert _classify_bedrock_error(exc) == "Guardrail"

    def test_unknown_client_error(self) -> None:
        exc = ClientError({"Error": {"Code": "InternalServerError", "Message": ""}}, "InvokeModel")
        assert _classify_bedrock_error(exc) == "Other"

    def test_non_client_error(self) -> None:
        exc = ValueError("something went wrong")
        assert _classify_bedrock_error(exc) == "Other"

    def test_runtime_error(self) -> None:
        exc = RuntimeError("timeout")
        assert _classify_bedrock_error(exc) == "Other"


# ── EnrichmentMetricEmitter ────────────────────────────────────────────────


class TestEnrichmentMetricEmitter:
    @staticmethod
    def _make_emitter(engine: str = "postgresql", namespace_id: str = "ns-123") -> EnrichmentMetricEmitter:
        source_item = {"configuration": json.dumps({"engine": engine.upper()})}
        ctx = EnrichmentContext(source_item=source_item, namespace_id=namespace_id)
        return EnrichmentMetricEmitter(ctx)

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_metric_includes_dimensions(self, mock_emit) -> None:
        emitter = self._make_emitter()
        emitter.emit_metric("TestMetric", 42.0, "Count")

        mock_emit.assert_called_once_with("TestMetric", 42.0, "Count", Engine="postgresql", NamespaceId="ns-123")

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_metric_passes_extra_dimensions(self, mock_emit) -> None:
        emitter = self._make_emitter()
        emitter.emit_metric("TestMetric", 1.0, "None", Stage="Pass1", Custom="val")

        mock_emit.assert_called_once_with(
            "TestMetric", 1.0, "None", Engine="postgresql", NamespaceId="ns-123", Stage="Pass1", Custom="val"
        )

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_bedrock_invocation_metrics_emits_three_metrics(self, mock_emit) -> None:
        emitter = self._make_emitter()
        emitter.emit_bedrock_invocation_metrics(stage="Pass1", latency_ms=150.0, input_tokens=500, output_tokens=200)

        assert mock_emit.call_count == 3
        calls = [c[0] for c in mock_emit.call_args_list]
        metric_names = [c[0] for c in calls]
        assert "BedrockInvocationLatencyMs" in metric_names
        assert "BedrockInputTokens" in metric_names
        assert "BedrockOutputTokens" in metric_names

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_bedrock_invocation_metrics_accumulates_tokens(self, mock_emit) -> None:
        emitter = self._make_emitter()
        emitter.emit_bedrock_invocation_metrics(stage="Pass1", latency_ms=100.0, input_tokens=500, output_tokens=200)
        emitter.emit_bedrock_invocation_metrics(stage="Pass2", latency_ms=80.0, input_tokens=300, output_tokens=100)

        assert emitter._token_counter.input_tokens == 800
        assert emitter._token_counter.output_tokens == 300

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_bedrock_invocation_error_throttling(self, mock_emit) -> None:
        emitter = self._make_emitter()
        exc = ClientError({"Error": {"Code": "ThrottlingException", "Message": ""}}, "InvokeModel")
        emitter.emit_bedrock_invocation_error(stage="Pass1", exc=exc)

        mock_emit.assert_called_once_with(
            "BedrockInvocationErrors",
            1,
            "Count",
            Engine="postgresql",
            NamespaceId="ns-123",
            Stage="Pass1",
            ErrorType="Throttling",
        )

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_bedrock_invocation_error_truncation(self, mock_emit) -> None:
        emitter = self._make_emitter()
        exc = BedrockTruncationError(model_id="test-model", output_tokens=512, max_tokens=512)

        emitter.emit_bedrock_invocation_error(stage="Pass2", exc=exc)

        mock_emit.assert_called_once_with(
            "BedrockInvocationErrors",
            1,
            "Count",
            Engine="postgresql",
            NamespaceId="ns-123",
            Stage="Pass2",
            ErrorType="Truncated",
        )

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_bedrock_invocation_error_other(self, mock_emit) -> None:
        emitter = self._make_emitter()
        exc = RuntimeError("unexpected")
        emitter.emit_bedrock_invocation_error(stage="Pass2", exc=exc)

        mock_emit.assert_called_once_with(
            "BedrockInvocationErrors",
            1,
            "Count",
            Engine="postgresql",
            NamespaceId="ns-123",
            Stage="Pass2",
            ErrorType="Other",
        )

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_enrichment_estimated_cost(self, mock_emit) -> None:
        emitter = self._make_emitter()
        emitter.emit_bedrock_invocation_metrics(stage="Pass1", latency_ms=100.0, input_tokens=1000, output_tokens=500)

        mock_emit.reset_mock()
        emitter.emit_enrichment_estimated_cost()

        # 1000 * 1.00/1M + 500 * 5.00/1M = 0.001 + 0.0025 = 0.0035
        mock_emit.assert_called_once()
        args = mock_emit.call_args[0]
        assert args[0] == "EnrichmentEstimatedCostUsd"
        assert args[1] == pytest.approx(0.0035)

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_emit_enrichment_estimated_cost_zero_when_no_invocations(self, mock_emit) -> None:
        emitter = self._make_emitter()
        emitter.emit_enrichment_estimated_cost()

        mock_emit.assert_called_once()
        args = mock_emit.call_args[0]
        assert args[0] == "EnrichmentEstimatedCostUsd"
        assert args[1] == 0.0

    @patch("coa_sources.database.pipeline.enrichment_metrics.emit_metric")
    def test_glue_engine_dimension(self, mock_emit) -> None:
        source_item = {"configuration": json.dumps({"databaseName": "my_db"})}
        ctx = EnrichmentContext(source_item=source_item, namespace_id="ns-glue")
        emitter = EnrichmentMetricEmitter(ctx)
        emitter.emit_metric("Test", 1.0, "Count")

        mock_emit.assert_called_once_with("Test", 1.0, "Count", Engine="glue", NamespaceId="ns-glue")
