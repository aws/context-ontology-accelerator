# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for table_enricher module."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError, ReadTimeoutError
from coa_common.bedrock import BedrockInvocationResult, GuardrailBlockedError
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    EnrichmentSource,
    ForeignKey,
    PrimaryKey,
    ReviewStatus,
    Table,
)
from coa_sources.database.enrichment.prompts import SYSTEM_PROMPT, build_user_prompt
from coa_sources.database.enrichment.table_enricher import (
    MAX_WORKERS,
    TABLE_TIMEOUT_SEC,
    _apply_column_metadata,
    _apply_foreign_keys,
    _apply_result,
    _apply_table_metadata,
    _has_pending,
    _prepare_table_context,
    _resolve_guardrail_id,
    _write_enriched_assets,
    run,
)


def _make_table(name: str = "orders", database: str = "sales", num_columns: int = 3) -> Table:
    columns = [Column(name=f"col_{i}", data_type="string") for i in range(num_columns)]
    return Table(name=name, database=database, columns=columns, data_source_id="ds-123")


def _mock_bedrock_response() -> dict:
    return {
        "table": {
            "description": "Customer orders table",
            "synonyms": ["purchases"],
            "glossary_terms": ["order management"],
            "tags": ["transactional"],
        },
        "primary_key": {"columns": ["col_0"], "confidence": 0.85},
        "columns": [
            {
                "name": "col_0",
                "description": "Primary identifier",
                "synonyms": ["id"],
                "glossary_terms": [],
                "tags": ["pk"],
                "confidence": 0.91,
            },
            {"name": "col_1", "description": "Order date", "synonyms": [], "glossary_terms": ["temporal"], "tags": []},
            {
                "name": "col_2",
                "description": "Amount",
                "synonyms": ["total"],
                "glossary_terms": ["financial"],
                "tags": ["numeric"],
            },
        ],
    }


def _mock_invocation_result(result: dict | None = None) -> BedrockInvocationResult:
    """Create a BedrockInvocationResult wrapping the standard mock response."""
    return BedrockInvocationResult(
        result=result or _mock_bedrock_response(),
        input_tokens=100,
        output_tokens=50,
        latency_ms=200.0,
    )


class TestApplyResult:
    def test_applies_table_metadata(self) -> None:
        table = _make_table()
        result = _mock_bedrock_response()
        _apply_result(table, result, table.columns)

        assert table.business_metadata.description == "Customer orders table"
        assert table.business_metadata.synonyms == ["purchases"]
        assert table.business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED
        assert table.business_metadata.review_status == ReviewStatus.PENDING_REVIEW

    def test_applies_primary_key(self) -> None:
        table = _make_table()
        result = _mock_bedrock_response()
        _apply_result(table, result, table.columns)

        assert table.primary_key.columns == ["col_0"]
        assert table.primary_key.confidence == 0.85
        assert table.primary_key.source == EnrichmentSource.AI_INFERRED

    def test_applies_column_metadata(self) -> None:
        table = _make_table()
        result = _mock_bedrock_response()
        _apply_result(table, result, table.columns)

        assert table.columns[0].business_metadata.description == "Primary identifier"
        assert table.columns[0].business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED
        assert table.columns[0].business_metadata.confidence == 0.91
        assert table.columns[2].business_metadata.tags == ["numeric"]

    def test_handles_missing_primary_key(self) -> None:
        table = _make_table()
        result = _mock_bedrock_response()
        result["primary_key"] = {"columns": [], "confidence": 0.0}
        _apply_result(table, result, table.columns)

        assert table.primary_key.columns == []

    def test_handles_missing_column_in_response(self) -> None:
        table = _make_table(num_columns=5)
        result = _mock_bedrock_response()  # Only has 3 columns
        _apply_result(table, result, table.columns)

        # Columns not in response keep default metadata
        assert table.columns[3].business_metadata.description == ""
        assert table.columns[4].business_metadata.description == ""


class TestRun:
    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_run_enriches_tables(self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [_make_table("t1"), _make_table("t2")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_enriched"] == 2
        assert result["tables_failed"] == 0
        mock_write.assert_called_once()

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_run_handles_failures(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.side_effect = Exception("Bedrock error")
        mock_read.return_value = [_make_table("t1")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_enriched"] == 0
        assert result["tables_failed"] == 1
        # Only successfully enriched tables are written
        mock_write.assert_called_once_with([], "dom-789", "ns-456")

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_run_emits_throttle_metric_on_terminal_throttle(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        from botocore.exceptions import ClientError

        mock_client_cls.return_value.invoke.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "InvokeModel"
        )
        mock_read.return_value = [_make_table("t1")]
        mock_emitter = MagicMock()

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        assert result["tables_failed"] == 1
        mock_emitter.emit_bedrock_invocation_error.assert_called_once()
        call_kwargs = mock_emitter.emit_bedrock_invocation_error.call_args[1]
        assert call_kwargs["stage"] == "Pass1"

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_run_no_tables(self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock) -> None:
        mock_read.return_value = []

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result == {
            "tables_enriched": 0,
            "tables_failed": 0,
            "tables_skipped_unchanged": 0,
            "failed_table_ids": [],
        }
        mock_write.assert_not_called()


class TestDeterministicConstraintSkip:
    """Verify AI enrichment skips PK/FK when deterministic constraints exist."""

    def test_deterministic_pk_not_overwritten(self) -> None:
        table = _make_table()
        table.primary_key = PrimaryKey(columns=["col_0"], source=EnrichmentSource.DETERMINISTIC, confidence=1.0)

        result = _mock_bedrock_response()
        _apply_result(table, result, table.columns)

        assert table.primary_key.columns == ["col_0"]
        assert table.primary_key.source == EnrichmentSource.DETERMINISTIC
        assert table.primary_key.confidence == 1.0

    def test_pass1_does_not_apply_foreign_keys(self) -> None:
        table = _make_table()
        table.foreign_keys = [
            ForeignKey(
                column="col_1",
                target_table="customers",
                target_column="id",
                source=EnrichmentSource.DETERMINISTIC,
                confidence=1.0,
            )
        ]

        result = _mock_bedrock_response()
        result["foreign_keys"] = [
            {"column": "col_2", "target_table": "products", "target_column": "id", "confidence": 0.7}
        ]
        _apply_result(table, result, table.columns)

        # Pass 1 never applies FKs — existing FKs remain untouched
        assert len(table.foreign_keys) == 1
        assert table.foreign_keys[0].source == EnrichmentSource.DETERMINISTIC

    def test_ai_pk_applied_when_no_deterministic(self) -> None:
        table = _make_table()
        result = _mock_bedrock_response()
        _apply_result(table, result, table.columns)

        assert table.primary_key.columns == ["col_0"]
        assert table.primary_key.source == EnrichmentSource.AI_INFERRED


class TestPromptNoFK:
    """Verify system prompt no longer contains FK output schema."""

    def test_system_prompt_has_no_foreign_keys_in_output_schema(self) -> None:
        from coa_sources.database.enrichment.prompts import SYSTEM_PROMPT

        output_schema = SYSTEM_PROMPT.split("Rules:")[0]
        assert "foreign_keys" not in output_schema

    def test_system_prompt_instructs_no_fk_inference(self) -> None:
        from coa_sources.database.enrichment.prompts import SYSTEM_PROMPT

        assert "Do NOT infer foreign keys" in SYSTEM_PROMPT


class TestParallelChunkDispatch:
    """Verify large tables dispatch column batches in parallel via shared pool."""

    def test_max_workers_is_10(self) -> None:
        assert MAX_WORKERS == 10

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_large_table_submits_multiple_batches(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        table = _make_table("big", num_columns=120)
        mock_read.return_value = [table]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        # 3 Pass 1 batch calls + 1 Pass 2 FK inference call
        assert mock_client_cls.return_value.invoke.call_count == 4
        assert result["tables_enriched"] == 1

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_small_table_submits_single_call(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        table = _make_table("small", num_columns=10)
        mock_read.return_value = [table]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        # 1 Pass 1 call + 1 Pass 2 FK inference call
        assert mock_client_cls.return_value.invoke.call_count == 2
        assert result["tables_enriched"] == 1


class TestPerTableTimeout:
    """Verify per-table timeout surfaces as tables_failed."""

    def test_timeout_constant_defaults_to_90(self) -> None:
        assert TABLE_TIMEOUT_SEC == 90

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_read_timeout_surfaces_as_table_failed(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.side_effect = ReadTimeoutError(
            endpoint_url="https://bedrock.us-east-1.amazonaws.com"
        )
        mock_read.return_value = [_make_table("t1")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_enriched"] == 0
        assert result["tables_failed"] == 1

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_timeout_on_one_table_does_not_block_others(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        call_count = [0]

        def mock_invoke(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ReadTimeoutError(endpoint_url="https://bedrock.us-east-1.amazonaws.com")
            return _mock_invocation_result()

        mock_client_cls.return_value.invoke.side_effect = mock_invoke
        mock_read.return_value = [_make_table("t1"), _make_table("t2")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_enriched"] == 1
        assert result["tables_failed"] == 1


class TestRetryConfig:
    """Verify Bedrock retry configuration."""

    def test_bedrock_client_uses_adaptive_retry_with_3_attempts(self) -> None:
        with patch("boto3.client") as mock_boto:
            from coa_common.bedrock import BedrockClient as _BC

            _BC()
            config = mock_boto.call_args[1]["config"]
            assert config.retries["mode"] == "adaptive"
            assert config.retries["max_attempts"] == 3

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_5xx_error_surfaces_as_table_failed(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "500"}}, "InvokeModel"
        )
        mock_read.return_value = [_make_table("t1")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_enriched"] == 0
        assert result["tables_failed"] == 1


class TestExistingMetadataPassThrough:
    """Verify existing_pk and existing_fks are passed through to the prompt."""

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_existing_fks_passed_to_prompt(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        table = _make_table("orders")
        table.primary_key = PrimaryKey(columns=["col_0"], source=EnrichmentSource.DETERMINISTIC, confidence=1.0)
        table.foreign_keys = [
            ForeignKey(
                column="col_1",
                target_table="customers",
                target_column="id",
                source=EnrichmentSource.DETERMINISTIC,
                confidence=1.0,
            )
        ]
        mock_read.return_value = [table]

        run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        # First invoke call is Pass 1 (per-table); last is Pass 2 (FK inference)
        invoke_call = mock_client_cls.return_value.invoke.call_args_list[0]
        user_prompt = invoke_call[0][1]
        assert "foreign_keys" in user_prompt
        assert "customers" in user_prompt

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_existing_pk_passed_to_prompt(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        table = _make_table("orders")
        table.primary_key = PrimaryKey(columns=["order_id"], source=EnrichmentSource.DETERMINISTIC, confidence=1.0)
        mock_read.return_value = [table]

        run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        # First invoke call is Pass 1 (per-table); last is Pass 2 (FK inference)
        invoke_call = mock_client_cls.return_value.invoke.call_args_list[0]
        user_prompt = invoke_call[0][1]
        assert "primary_key" in user_prompt
        assert "order_id" in user_prompt


class TestMetricEmissionDuringEnrichment:
    """Verify the emitter is called correctly during the enrichment process."""

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_emitter_receives_pass1_invocation_metrics(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [_make_table("t1")]
        mock_emitter = MagicMock()

        run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        # Pass1 invocation for the single table
        mock_emitter.emit_bedrock_invocation_metrics.assert_called()
        call_kwargs = mock_emitter.emit_bedrock_invocation_metrics.call_args_list[0][1]
        assert call_kwargs["stage"] == "Pass1"
        assert call_kwargs["latency_ms"] == 200.0
        assert call_kwargs["input_tokens"] == 100
        assert call_kwargs["output_tokens"] == 50

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_emitter_receives_pass2_invocation_metrics(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [_make_table("t1"), _make_table("t2")]
        mock_emitter = MagicMock()

        run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        # Pass2 FK inference call should also emit metrics
        pass2_calls = [
            c for c in mock_emitter.emit_bedrock_invocation_metrics.call_args_list if c[1]["stage"] == "Pass2"
        ]
        assert len(pass2_calls) == 1

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_emitter_receives_error_on_bedrock_failure(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.side_effect = Exception("Bedrock down")
        mock_read.return_value = [_make_table("t1")]
        mock_emitter = MagicMock()

        run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        mock_emitter.emit_bedrock_invocation_error.assert_called_once()
        call_kwargs = mock_emitter.emit_bedrock_invocation_error.call_args[1]
        assert call_kwargs["stage"] == "Pass1"
        assert isinstance(call_kwargs["exc"], Exception)

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_multiple_tables_emit_multiple_pass1_metrics(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [_make_table("t1"), _make_table("t2"), _make_table("t3")]
        mock_emitter = MagicMock()

        run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        pass1_calls = [
            c for c in mock_emitter.emit_bedrock_invocation_metrics.call_args_list if c[1]["stage"] == "Pass1"
        ]
        # 3 tables = 3 Pass1 Bedrock calls
        assert len(pass1_calls) == 3

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_no_tables_no_emitter_calls(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_read.return_value = []
        mock_emitter = MagicMock()

        run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        mock_emitter.emit_bedrock_invocation_metrics.assert_not_called()
        mock_emitter.emit_bedrock_invocation_error.assert_not_called()


class TestResolveGuardrailId:
    """Verify SSM-based guardrail resolution paths."""

    @patch("coa_sources.database.enrichment.table_enricher.GUARDRAIL_SSM_PARAM", "")
    def test_returns_none_when_param_not_set(self) -> None:
        result = _resolve_guardrail_id()
        assert result is None

    @patch(
        "coa_sources.database.enrichment.table_enricher.GUARDRAIL_SSM_PARAM",
        "/coa/bedrock/guardrail-id",
    )
    @patch("coa_sources.database.enrichment.table_enricher.boto3")
    def test_returns_value_from_ssm(self, mock_boto3: MagicMock) -> None:
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "gr-abc123"}}
        mock_boto3.client.return_value = mock_ssm

        result = _resolve_guardrail_id()

        assert result == "gr-abc123"
        mock_boto3.client.assert_called_once()
        mock_ssm.get_parameter.assert_called_once_with(Name="/coa/bedrock/guardrail-id")

    @patch(
        "coa_sources.database.enrichment.table_enricher.GUARDRAIL_SSM_PARAM",
        "/coa/bedrock/guardrail-id",
    )
    @patch("coa_sources.database.enrichment.table_enricher.boto3")
    def test_returns_none_on_ssm_failure(self, mock_boto3: MagicMock) -> None:
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "not found"}}, "GetParameter"
        )
        mock_boto3.client.return_value = mock_ssm

        result = _resolve_guardrail_id()

        assert result is None


class TestGuardrailBlockedPath:
    """Verify GuardrailBlockedError surfaces as tables_failed."""

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_guardrail_blocked_marks_table_failed(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.side_effect = GuardrailBlockedError("content blocked")
        mock_read.return_value = [_make_table("t1")]
        mock_emitter = MagicMock()

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        assert result["tables_enriched"] == 0
        assert result["tables_failed"] == 1
        mock_emitter.emit_bedrock_invocation_error.assert_called_once()
        call_kwargs = mock_emitter.emit_bedrock_invocation_error.call_args[1]
        assert call_kwargs["stage"] == "Pass1"

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_guardrail_blocked_one_table_others_succeed(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        call_count = [0]

        def mock_invoke(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise GuardrailBlockedError("blocked")
            return _mock_invocation_result()

        mock_client_cls.return_value.invoke.side_effect = mock_invoke
        mock_read.return_value = [_make_table("t1"), _make_table("t2")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_enriched"] == 1
        assert result["tables_failed"] == 1

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_guardrail_blocked_returns_failed_table_ids(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        """The aggregate count is not enough — run must name the failed tables so
        the caller can record which ones came back blank."""
        mock_client_cls.return_value.invoke.side_effect = GuardrailBlockedError("content blocked")
        mock_read.return_value = [_make_table("t1", database="public")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_failed"] == 1
        # table_id == f"{database}.{name}"
        assert result["failed_table_ids"] == ["public.t1"]

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_all_succeed_returns_empty_failed_table_ids(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [_make_table("t1")]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_failed"] == 0
        assert result["failed_table_ids"] == []


class TestProtectedDescriptionPreserve:
    """Verify authoritative descriptions are not overwritten by AI."""

    def test_deterministic_description_preserved_at_table_level(self) -> None:
        table = _make_table()
        table.business_metadata = BusinessMetadata(
            description="Authoritative table desc",
            synonyms=[],
            glossary_terms=[],
            tags=[],
            enrichment_source=EnrichmentSource.DETERMINISTIC,
            review_status=ReviewStatus.APPROVED,
            confidence=1.0,
        )

        result = {
            "table": {
                "description": "AI wants to replace this",
                "synonyms": ["ai_synonym"],
                "glossary_terms": ["ai_term"],
                "tags": ["ai_tag"],
            }
        }
        _apply_table_metadata(table, result)

        assert table.business_metadata.description == "Authoritative table desc"
        assert table.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        assert table.business_metadata.synonyms == ["ai_synonym"]
        assert table.business_metadata.glossary_terms == ["ai_term"]
        assert table.business_metadata.tags == ["ai_tag"]

    def test_steward_edited_description_preserved(self) -> None:
        table = _make_table()
        table.business_metadata = BusinessMetadata(
            description="Steward wrote this",
            synonyms=["existing_syn"],
            glossary_terms=[],
            tags=[],
            enrichment_source=EnrichmentSource.STEWARD_EDITED,
            review_status=ReviewStatus.APPROVED,
        )

        result = {
            "table": {
                "description": "AI replacement",
                "synonyms": ["new_syn"],
                "glossary_terms": ["new_term"],
                "tags": ["new_tag"],
            }
        }
        _apply_table_metadata(table, result)

        assert table.business_metadata.description == "Steward wrote this"
        assert table.business_metadata.synonyms == ["existing_syn"]
        assert table.business_metadata.glossary_terms == ["new_term"]

    def test_catalog_existing_description_preserved(self) -> None:
        table = _make_table()
        table.business_metadata = BusinessMetadata(
            description="Catalog desc",
            enrichment_source=EnrichmentSource.CATALOG_EXISTING,
        )

        result = {"table": {"description": "AI replacement", "synonyms": [], "glossary_terms": [], "tags": []}}
        _apply_table_metadata(table, result)

        assert table.business_metadata.description == "Catalog desc"
        assert table.business_metadata.enrichment_source == EnrichmentSource.CATALOG_EXISTING

    def test_deterministic_table_receives_ai_synonyms_and_glossary(self) -> None:
        """Verify bug fix: DETERMINISTIC tables get AI-generated synonyms/glossary/tags."""
        table = _make_table()
        table.business_metadata = BusinessMetadata(
            description="Authoritative source system description",
            synonyms=[],
            glossary_terms=[],
            tags=[],
            enrichment_source=EnrichmentSource.DETERMINISTIC,
            review_status=ReviewStatus.PENDING_REVIEW,
            confidence=1.0,
        )

        result = {
            "table": {
                "description": "AI replacement (should be ignored)",
                "synonyms": ["order_tbl", "purchases"],
                "glossary_terms": ["order management", "sales"],
                "tags": ["transactional", "s3-backed"],
            }
        }
        _apply_table_metadata(table, result)

        # Description preserved from DETERMINISTIC source
        assert table.business_metadata.description == "Authoritative source system description"
        assert table.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        # Synonyms, glossary, tags populated from AI (bug fix #542)
        assert table.business_metadata.synonyms == ["order_tbl", "purchases"]
        assert table.business_metadata.glossary_terms == ["order management", "sales"]
        assert table.business_metadata.tags == ["transactional", "s3-backed"]


class TestProtectedColumnMetadata:
    """Verify protected column descriptions are preserved."""

    def test_deterministic_column_description_preserved(self) -> None:
        table = _make_table(num_columns=2)
        table.columns[0].business_metadata = BusinessMetadata(
            description="Source system comment",
            enrichment_source=EnrichmentSource.DETERMINISTIC,
            review_status=ReviewStatus.APPROVED,
            confidence=1.0,
        )

        column_results = [
            {
                "name": "col_0",
                "description": "AI replacement",
                "synonyms": ["ai_syn"],
                "glossary_terms": [],
                "tags": [],
            },
            {"name": "col_1", "description": "New AI desc", "synonyms": [], "glossary_terms": [], "tags": []},
        ]
        _apply_column_metadata(table, column_results)

        assert table.columns[0].business_metadata.description == "Source system comment"
        assert table.columns[0].business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        assert table.columns[0].business_metadata.synonyms == ["ai_syn"]
        assert table.columns[1].business_metadata.description == "New AI desc"
        assert table.columns[1].business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED

    def test_steward_specified_column_preserved(self) -> None:
        table = _make_table(num_columns=1)
        table.columns[0].business_metadata = BusinessMetadata(
            description="Steward column desc",
            synonyms=["existing"],
            enrichment_source=EnrichmentSource.STEWARD_SPECIFIED,
            review_status=ReviewStatus.APPROVED,
            confidence=1.0,
        )

        column_results = [
            {
                "name": "col_0",
                "description": "AI desc",
                "synonyms": ["new_syn"],
                "glossary_terms": ["g"],
                "tags": ["t"],
            },
        ]
        _apply_column_metadata(table, column_results)

        assert table.columns[0].business_metadata.description == "Steward column desc"
        assert table.columns[0].business_metadata.synonyms == ["existing"]
        assert table.columns[0].business_metadata.glossary_terms == ["g"]


class TestWriteEnrichedAssets:
    """Verify _write_enriched_assets logic."""

    @patch("coa_common.metadata_store.SMUSClient")
    def test_writes_enriched_tables(self, mock_smus_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_smus_cls.return_value = mock_client
        asset_mock = MagicMock()
        asset_mock.configure_mock(name="ds-123:sales.orders")
        asset_mock.asset_id = "asset-001"
        mock_client.search_assets.return_value = MagicMock(items=[asset_mock])

        table = _make_table("orders", database="sales")
        table.business_metadata.description = "Enriched desc"

        _write_enriched_assets([table], "dom-789", "proj-456")

        mock_client.create_asset_revision.assert_called_once()

    @patch("coa_common.metadata_store.SMUSClient")
    def test_skips_table_with_empty_data_source_id(self, mock_smus_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_smus_cls.return_value = mock_client

        table = Table(name="orphan", database="db", columns=[], data_source_id="")

        _write_enriched_assets([table], "dom-789", "proj-456")

        mock_client.search_assets.assert_not_called()

    @patch("coa_common.metadata_store.SMUSClient")
    def test_skips_when_asset_not_found(self, mock_smus_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_smus_cls.return_value = mock_client
        # find_asset_by_name returns None only for a genuinely absent asset;
        # a ranking miss no longer masquerades as one.
        mock_client.find_asset_by_name.return_value = None

        table = _make_table("orders")

        _write_enriched_assets([table], "dom-789", "proj-456")

        mock_client.create_asset_revision.assert_not_called()

    @patch("coa_common.metadata_store.SMUSClient")
    def test_handles_write_exception_gracefully(self, mock_smus_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_smus_cls.return_value = mock_client
        asset_mock = MagicMock()
        asset_mock.configure_mock(name="ds-123:sales.orders")
        asset_mock.asset_id = "asset-001"
        mock_client.find_asset_by_name.return_value = asset_mock
        mock_client.create_asset_revision.side_effect = Exception("DZ error")

        table = _make_table("orders", database="sales")

        _write_enriched_assets([table], "dom-789", "proj-456")


class TestIncompleteBatchDetection:
    """Verify partial batch failure marks table as failed."""

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_partial_batch_failure_marks_table_failed(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        call_count = [0]

        def mock_invoke(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Batch 2 failed")
            return _mock_invocation_result()

        mock_client_cls.return_value.invoke.side_effect = mock_invoke
        table = _make_table("big", num_columns=120)
        mock_read.return_value = [table]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_failed"] == 1
        assert result["tables_enriched"] == 0


class TestPass2FailureFallback:
    """Verify Pass 2 failure preserves Pass 1 results."""

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_pass2_failure_still_writes_pass1_results(
        self,
        mock_client_cls: MagicMock,
        mock_read: MagicMock,
        mock_write: MagicMock,
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [_make_table("t1"), _make_table("t2")]

        import coa_sources.database.enrichment.relationship_inferrer as ri_mod

        original_infer = ri_mod.infer_relationships
        ri_mod.infer_relationships = MagicMock(side_effect=Exception("Pass 2 exploded"))
        try:
            result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())
        finally:
            ri_mod.infer_relationships = original_infer

        assert result["tables_enriched"] == 2
        assert result["tables_failed"] == 0
        mock_write.assert_called_once()


class TestPrepareTableContext:
    """Verify _prepare_table_context extracts correct context."""

    def test_deterministic_description_used_as_extra_context_when_no_database_desc(self) -> None:
        table = _make_table()
        table.database_description = ""
        table.business_metadata = BusinessMetadata(
            description="Source system table comment",
            enrichment_source=EnrichmentSource.DETERMINISTIC,
        )

        ctx = _prepare_table_context(table)

        assert ctx["extra_context"] == "Source system table comment"

    def test_database_description_takes_precedence_over_deterministic(self) -> None:
        table = _make_table()
        table.database_description = "Database-level description"
        table.business_metadata = BusinessMetadata(
            description="Table-level desc",
            enrichment_source=EnrichmentSource.DETERMINISTIC,
        )

        ctx = _prepare_table_context(table)

        assert ctx["extra_context"] == "Database-level description"

    def test_ai_generated_description_not_used_as_extra_context(self) -> None:
        table = _make_table()
        table.database_description = ""
        table.business_metadata = BusinessMetadata(
            description="AI generated desc",
            enrichment_source=EnrichmentSource.AI_GENERATED,
        )

        ctx = _prepare_table_context(table)

        assert ctx["extra_context"] == ""


class TestApplyForeignKeys:
    """Verify _apply_foreign_keys applies AI-inferred FKs correctly."""

    def test_applies_foreign_keys_when_no_deterministic(self) -> None:
        table = _make_table()
        result = {
            "foreign_keys": [
                {"column": "col_0", "target_table": "customers", "target_column": "id", "confidence": 0.85},
                {"column": "col_1", "target_table": "products", "confidence": 0.7},
            ]
        }

        _apply_foreign_keys(table, result)

        assert len(table.foreign_keys) == 2
        assert table.foreign_keys[0].column == "col_0"
        assert table.foreign_keys[0].target_table == "customers"
        assert table.foreign_keys[0].source == EnrichmentSource.AI_INFERRED
        assert table.foreign_keys[1].target_column == ""

    def test_skips_when_deterministic_fks_exist(self) -> None:
        table = _make_table()
        table.foreign_keys = [
            ForeignKey(
                column="col_0",
                target_table="ref",
                target_column="id",
                source=EnrichmentSource.DETERMINISTIC,
                confidence=1.0,
            )
        ]
        result = {
            "foreign_keys": [
                {"column": "col_1", "target_table": "other", "target_column": "id", "confidence": 0.9},
            ]
        }

        _apply_foreign_keys(table, result)

        assert len(table.foreign_keys) == 1
        assert table.foreign_keys[0].source == EnrichmentSource.DETERMINISTIC

    def test_skips_incomplete_fk_entries(self) -> None:
        table = _make_table()
        result = {
            "foreign_keys": [
                {"column": "", "target_table": "customers", "target_column": "id"},
                {"column": "col_0", "target_table": ""},
                {"column": "col_1", "target_table": "products", "confidence": 0.8},
            ]
        }

        _apply_foreign_keys(table, result)

        assert len(table.foreign_keys) == 1
        assert table.foreign_keys[0].column == "col_1"


class TestCallBedrockMaxTokens:
    """Verify max_tokens logic in _call_bedrock based on extra_context."""

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_extra_context_with_hint_uses_16384_tokens(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        table = _make_table("orders")
        table.database_description = "Some database context"
        mock_read.return_value = [table]

        with patch("coa_sources.database.enrichment.table_enricher._prepare_table_context") as mock_ctx:
            mock_ctx.return_value = {
                "existing_description": "",
                "existing_pk": None,
                "existing_fks": None,
                "extra_context": "Evidence text",
                "extra_context_hint": "Use this for FK inference",
            }
            run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        invoke_call = mock_client_cls.return_value.invoke.call_args_list[0]
        assert invoke_call[1]["max_tokens"] == 16384


class TestEmptyDescriptionFallback:
    """Empty table descriptions stay empty, are logged, and are counted."""

    def test_empty_description_is_not_fabricated(self) -> None:
        table = _make_table("orders", database="sales")
        result = {
            "table": {
                "description": "",
                "synonyms": ["purchases"],
                "glossary_terms": ["order management"],
                "tags": ["transactional"],
            }
        }
        _apply_table_metadata(table, result)

        assert table.business_metadata.description == ""
        assert table.business_metadata.synonyms == ["purchases"]

    def test_missing_description_key_normalizes_to_empty_string(self) -> None:
        table = _make_table("users", database="auth")
        result = {"table": {"synonyms": ["accounts"], "glossary_terms": ["identity"], "tags": ["core"]}}
        _apply_table_metadata(table, result)

        assert table.business_metadata.description == ""
        assert table.business_metadata.tags == ["core"]

    def test_null_description_normalizes_to_empty_string(self) -> None:
        """A JSON null must not reach BusinessMetadata.description as None."""
        table = _make_table("lineitem", database="tpch")
        result = {"table": {"description": None, "synonyms": ["line_items"], "tags": ["detail"]}}
        _apply_table_metadata(table, result)

        assert table.business_metadata.description == ""
        assert table.business_metadata.synonyms == ["line_items"]

    def test_nonempty_description_used_as_is(self) -> None:
        table = _make_table("orders", database="sales")
        result = {
            "table": {
                "description": "Customer purchase records",
                "synonyms": ["purchases"],
                "glossary_terms": [],
                "tags": [],
            }
        }
        _apply_table_metadata(table, result)

        assert table.business_metadata.description == "Customer purchase records"
        assert table.business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED

    def test_missing_description_is_logged(self, caplog) -> None:
        table = _make_table("orders", database="sales")
        with caplog.at_level(logging.WARNING):
            _apply_table_metadata(table, {"table": {"tags": ["t"]}})

        assert "table_description_missing" in caplog.text

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_run_emits_tables_missing_description(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        no_desc = _mock_bedrock_response()
        no_desc["table"].pop("description")
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result(no_desc)
        mock_read.return_value = [_make_table("t1"), _make_table("t2")]
        mock_emitter = MagicMock()

        run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        calls = [c for c in mock_emitter.emit_metric.call_args_list if c[0][0] == "TablesMissingDescription"]
        assert len(calls) == 1
        assert calls[0][0][1] == 2

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_run_emits_zero_when_all_described(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [_make_table("t1")]
        mock_emitter = MagicMock()

        run("ds-123", "ns-456", "dom-789", "full", emitter=mock_emitter)

        calls = [c for c in mock_emitter.emit_metric.call_args_list if c[0][0] == "TablesMissingDescription"]
        assert calls[0][0][1] == 0


class TestPromptDescriptionContract:
    """The prompt must never suppress the table description."""

    def test_required_directive_added_when_no_existing_description(self) -> None:
        prompt = build_user_prompt("orders", "sales", [Column(name="c", data_type="int")])
        assert "MUST generate a non-empty table.description" in prompt

    def test_required_directive_absent_when_description_exists(self) -> None:
        prompt = build_user_prompt(
            "orders",
            "sales",
            [Column(name="c", data_type="int")],
            existing_description="Source comment",
        )
        assert "MUST generate a non-empty table.description" not in prompt
        assert "description: Source comment" in prompt

    def test_required_directive_survives_column_comments(self) -> None:
        """Regression: source column comments must not suppress table.description."""
        cols = [
            Column(
                name="c",
                data_type="int",
                business_metadata=BusinessMetadata(
                    description="Source comment",
                    enrichment_source=EnrichmentSource.DETERMINISTIC,
                ),
            )
        ]
        prompt = build_user_prompt("orders", "sales", cols)

        assert "columns_with_descriptions" in prompt
        assert "MUST generate a non-empty table.description" in prompt

    def test_existing_metadata_header_is_not_a_blanket_suppression(self) -> None:
        prompt = build_user_prompt("orders", "sales", [Column(name="c", data_type="int")], existing_pk=["c"])
        assert "DO NOT regenerate" not in prompt
        assert "generate everything else" in prompt

    def test_system_prompt_marks_table_description_required(self) -> None:
        assert '"table.description" is REQUIRED' in SYSTEM_PROMPT


class TestReScanPreservesReviewedItems:
    """On a re-scan, enrichment regenerates only PENDING items and leaves
    already-reviewed (APPROVED / REJECTED) items exactly as the steward left
    them — including AI-generated ones (which the protected-source rules do NOT
    cover)."""

    def test_approved_ai_column_not_regenerated(self) -> None:
        table = _make_table(num_columns=2)
        table.columns[0].business_metadata = BusinessMetadata(
            description="Approved AI description",
            synonyms=["kept"],
            enrichment_source=EnrichmentSource.AI_GENERATED,
            review_status=ReviewStatus.APPROVED,
            confidence=0.9,
        )
        column_results = [
            {
                "name": "col_0",
                "description": "AI wants to replace",
                "synonyms": ["x"],
                "glossary_terms": [],
                "tags": [],
            },
            {"name": "col_1", "description": "fresh", "synonyms": [], "glossary_terms": [], "tags": []},
        ]
        _apply_column_metadata(table, column_results)

        # Approved AI column preserved verbatim (content + status).
        assert table.columns[0].business_metadata.description == "Approved AI description"
        assert table.columns[0].business_metadata.synonyms == ["kept"]
        assert table.columns[0].business_metadata.review_status == ReviewStatus.APPROVED
        # A PENDING column is still regenerated.
        assert table.columns[1].business_metadata.description == "fresh"
        assert table.columns[1].business_metadata.review_status == ReviewStatus.PENDING_REVIEW

    def test_rejected_ai_column_not_resurrected(self) -> None:
        table = _make_table(num_columns=1)
        table.columns[0].business_metadata = BusinessMetadata(
            description="rejected text",
            enrichment_source=EnrichmentSource.AI_GENERATED,
            review_status=ReviewStatus.REJECTED,
        )
        _apply_column_metadata(
            table, [{"name": "col_0", "description": "AI", "synonyms": [], "glossary_terms": [], "tags": []}]
        )
        assert table.columns[0].business_metadata.review_status == ReviewStatus.REJECTED
        assert table.columns[0].business_metadata.description == "rejected text"

    def test_approved_ai_table_description_not_regenerated(self) -> None:
        table = _make_table()
        table.business_metadata = BusinessMetadata(
            description="Approved AI table desc",
            enrichment_source=EnrichmentSource.AI_GENERATED,
            review_status=ReviewStatus.APPROVED,
        )
        _apply_table_metadata(
            table, {"table": {"description": "regenerated", "synonyms": ["x"], "glossary_terms": [], "tags": []}}
        )
        assert table.business_metadata.description == "Approved AI table desc"
        assert table.business_metadata.review_status == ReviewStatus.APPROVED
        assert table.business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED

    def test_has_pending_true_for_freshly_discovered_table(self) -> None:
        # Default table + columns are PENDING_REVIEW (first-scan shape).
        assert _has_pending(_make_table()) is True

    def test_has_pending_false_when_table_and_all_columns_terminal(self) -> None:
        table = _make_table(num_columns=2)
        table.business_metadata = BusinessMetadata(description="d", review_status=ReviewStatus.APPROVED)
        for col in table.columns:
            col.business_metadata = BusinessMetadata(description="d", review_status=ReviewStatus.APPROVED)
        assert _has_pending(table) is False

    def test_has_pending_true_when_any_column_pending(self) -> None:
        table = _make_table(num_columns=2)
        table.business_metadata = BusinessMetadata(description="d", review_status=ReviewStatus.APPROVED)
        table.columns[0].business_metadata = BusinessMetadata(review_status=ReviewStatus.APPROVED)
        table.columns[1].business_metadata = BusinessMetadata(review_status=ReviewStatus.PENDING_REVIEW)
        assert _has_pending(table) is True

    @patch("coa_sources.database.enrichment.table_enricher._write_enriched_assets")
    @patch("coa_sources.database.enrichment.table_enricher.read_assets_for_datasource")
    @patch("coa_sources.database.enrichment.table_enricher.BedrockClient")
    def test_run_skips_fully_approved_table(
        self, mock_client_cls: MagicMock, mock_read: MagicMock, mock_write: MagicMock
    ) -> None:
        """A re-scan reads every asset, but a fully-approved (unchanged) table is
        left untouched — only the table with PENDING items is enriched."""
        approved = _make_table("approved", num_columns=1)
        approved.business_metadata = BusinessMetadata(
            description="steward-approved",
            enrichment_source=EnrichmentSource.AI_GENERATED,
            review_status=ReviewStatus.APPROVED,
        )
        approved.columns[0].business_metadata = BusinessMetadata(
            description="approved col",
            enrichment_source=EnrichmentSource.AI_GENERATED,
            review_status=ReviewStatus.APPROVED,
        )
        pending = _make_table("pending", num_columns=1)  # default: PENDING everywhere

        mock_client_cls.return_value.invoke.return_value = _mock_invocation_result()
        mock_read.return_value = [approved, pending]

        result = run("ds-123", "ns-456", "dom-789", "full", emitter=MagicMock())

        assert result["tables_enriched"] == 1
        assert result["tables_skipped_unchanged"] == 1
        # The approved table was never re-generated.
        assert approved.business_metadata.description == "steward-approved"
        assert approved.business_metadata.review_status == ReviewStatus.APPROVED
        assert approved.columns[0].business_metadata.review_status == ReviewStatus.APPROVED
        # Only the pending table is written back.
        written = mock_write.call_args[0][0]
        assert [t.name for t in written] == ["pending"]
