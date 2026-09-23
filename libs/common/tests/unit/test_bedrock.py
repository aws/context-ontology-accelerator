# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the common BedrockClient."""

from __future__ import annotations

from contextlib import suppress
from unittest.mock import MagicMock, patch

import pytest
from coa_common.bedrock import (
    DEFAULT_MODEL_ID,
    FALLBACK_CHAT_MODEL_ID,
    BedrockClient,
    BedrockInvocationResult,
    BedrockTruncationError,
    GuardrailBlockedError,
    InputTooLargeError,
    extract_text_blocks,
    resolve_chat_model_id,
)

pytestmark = pytest.mark.unit


class TestDefaultModelIdResolution:
    """The chat model default is resolved from the environment (#94).

    Table enrichment and constraint inference construct ``BedrockClient``
    without a ``model_id``, so before this the module constant was the only
    input and an infrastructure change could not reach those paths — they
    stayed on a ``us.`` inference profile that a non-US region cannot invoke.

    Tests target ``resolve_chat_model_id`` rather than reloading the module:
    ``DEFAULT_MODEL_ID`` is read once at import (matching a container's static
    environment), and reloading swaps the module object out from under the rest
    of this file's patches.
    """

    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_CHAT_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0")
        assert resolve_chat_model_id() == "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_accepts_a_bare_in_region_model_id(self, monkeypatch):
        # Some models publish geo profiles for only a subset of regions, so a
        # bare in-region id must be usable too.
        monkeypatch.setenv("BEDROCK_CHAT_MODEL_ID", "anthropic.claude-haiku-4-5")
        assert resolve_chat_model_id() == "anthropic.claude-haiku-4-5"

    def test_falls_back_to_the_shared_default(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_CHAT_MODEL_ID", raising=False)
        assert resolve_chat_model_id() == FALLBACK_CHAT_MODEL_ID
        assert FALLBACK_CHAT_MODEL_ID == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_empty_env_var_falls_back(self, monkeypatch):
        # An empty value is a misconfiguration, not a request for an empty model
        # id — fall back rather than sending "" to Bedrock.
        monkeypatch.setenv("BEDROCK_CHAT_MODEL_ID", "")
        assert resolve_chat_model_id() == FALLBACK_CHAT_MODEL_ID

    def test_module_default_uses_the_resolver(self):
        # DEFAULT_MODEL_ID is what the BedrockClient constructor defaults to.
        assert resolve_chat_model_id() == DEFAULT_MODEL_ID


class TestExtractTextBlocks:
    """Select the answer by block type, not position (reasoning-model safe)."""

    def test_single_text_block(self):
        assert extract_text_blocks([{"text": "hello"}]) == "hello"

    def test_skips_reasoning_content_block(self):
        # A reasoning model emits reasoningContent (no "text" key) BEFORE the
        # answer's text block — the exact shape that made content[0]["text"] raise.
        blocks = [
            {"reasoningContent": {"reasoningText": {"text": "let me think", "signature": "sig"}}},
            {"text": '{"choice": "Claim"}'},
        ]
        assert extract_text_blocks(blocks) == '{"choice": "Claim"}'

    def test_joins_multiple_text_blocks(self):
        assert extract_text_blocks([{"text": "a"}, {"text": "b"}]) == "a\nb"

    def test_raises_when_no_text_block(self):
        # Only reasoning / non-text content → no answer. Must raise, never "".
        with pytest.raises(ValueError, match="no text content block"):
            extract_text_blocks([{"reasoningContent": {"reasoningText": {"text": "x", "signature": "s"}}}])

    def test_raises_on_empty_list(self):
        with pytest.raises(ValueError, match="no text content block"):
            extract_text_blocks([])


class TestBedrockClient:
    @patch("coa_common.bedrock.boto3")
    def test_invoke_parses_json_response(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"key": "value"}'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1")
        result = client.invoke("system", "user")

        assert isinstance(result, BedrockInvocationResult)
        assert result.result == {"key": "value"}
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.latency_ms > 0
        mock_client.converse.assert_called_once_with(
            modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            messages=[{"role": "user", "content": [{"text": "user"}]}],
            system=[{"text": "system"}],
            inferenceConfig={"maxTokens": 4096},
        )

    @patch("coa_common.bedrock.boto3")
    def test_invoke_parses_reasoning_model_response(self, mock_boto3):
        # A reasoning model returns reasoningContent at content[0] and the answer
        # at content[1]. The old content[0]["text"] indexing raised KeyError here;
        # invoke() must skip the reasoning block and parse the answer.
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "deliberating", "signature": "sig"}}},
                        {"text": '{"key": "value"}'},
                    ]
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1")
        result = client.invoke("system", "user")

        assert result.result == {"key": "value"}

    @patch("coa_common.bedrock.boto3")
    def test_invoke_strips_markdown_fences(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '```json\n{"key": "value"}\n```'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1")
        result = client.invoke("system", "user")

        assert result.result == {"key": "value"}

    @patch("coa_common.bedrock.boto3")
    def test_invoke_raises_on_invalid_json(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "not json"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1")
        with pytest.raises(ValueError, match="Invalid Bedrock response format"):
            client.invoke("system", "user")

    @patch("coa_common.bedrock.boto3")
    def test_invoke_reports_max_tokens_as_truncation_before_json_parsing(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"tables": [{"name": "orders"'}]}},
            "stopReason": "max_tokens",
            "usage": {"inputTokens": 100, "outputTokens": 512},
        }

        client = BedrockClient(region="us-east-1", model_id="test-model")
        with pytest.raises(BedrockTruncationError) as raised:
            client.invoke("system", "user", max_tokens=512)

        exc = raised.value
        assert exc.model_id == "test-model"
        assert exc.stop_reason == "max_tokens"
        assert exc.output_tokens == 512
        assert exc.max_tokens == 512
        assert "Invalid Bedrock response format" not in str(exc)
        assert "requested_max_tokens=512" in str(exc)

    @patch("coa_common.bedrock.boto3")
    def test_invoke_reports_max_tokens_even_when_output_envelope_is_incomplete(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.converse.return_value = {
            "stopReason": "max_tokens",
            "usage": {"inputTokens": 100, "outputTokens": 512},
        }

        client = BedrockClient(region="us-east-1", model_id="test-model")
        with pytest.raises(BedrockTruncationError, match="stop_reason=max_tokens"):
            client.invoke("system", "user", max_tokens=512)

    @patch("coa_common.bedrock.boto3")
    def test_invoke_raises_on_empty_content(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "   "}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1")
        with pytest.raises(ValueError, match="Bedrock returned empty response"):
            client.invoke("system", "user")

    @patch("coa_common.bedrock.boto3")
    def test_invoke_raises_on_client_error(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        error_response = {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}
        mock_client.exceptions.ClientError = type("ClientError", (Exception,), {})
        mock_client.converse.side_effect = mock_client.exceptions.ClientError(error_response, "Converse")

        client = BedrockClient(region="us-east-1")
        with pytest.raises(mock_client.exceptions.ClientError):
            client.invoke("system", "user")

    @patch("coa_common.bedrock.boto3")
    def test_custom_model_id(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"ok": true}'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-west-2", model_id="custom-model")
        client.invoke("sys", "usr")

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["modelId"] == "custom-model"

    @patch("coa_common.bedrock.boto3")
    def test_custom_max_tokens(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"result": "ok"}'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1")
        client.invoke("sys", "usr", max_tokens=8192)

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["inferenceConfig"] == {"maxTokens": 8192}

    @patch("coa_common.bedrock.boto3")
    def test_invoke_rejects_oversized_input(self, mock_boto3):
        """Prompts exceeding max_input_chars are rejected before any Bedrock call."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        client = BedrockClient(region="us-east-1", max_input_chars=100)
        with pytest.raises(InputTooLargeError):
            client.invoke("system", "x" * 200)

        mock_client.converse.assert_not_called()


class TestGuardrailIntegration:
    @patch("coa_common.bedrock.boto3")
    def test_guardrail_config_passed_when_id_set(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"ok": true}'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1", guardrail_id="gr-123")
        client.invoke("sys", "usr")

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["guardrailConfig"] == {
            "guardrailIdentifier": "gr-123",
            "guardrailVersion": "DRAFT",
            "trace": "enabled",
        }

    @patch("coa_common.bedrock.boto3")
    def test_guardrail_config_not_passed_when_id_absent(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"ok": true}'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1")
        client.invoke("sys", "usr")

        call_kwargs = mock_client.converse.call_args[1]
        assert "guardrailConfig" not in call_kwargs

    @patch("coa_common.bedrock.boto3")
    def test_guardrail_blocked_raises_error(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "I cannot help with that."}]}},
            "stopReason": "guardrail_intervened",
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "trace": {
                "guardrail": {
                    "outputAssessments": {
                        "gr-123": [
                            {"contentPolicy": {"filters": [{"action": "BLOCKED", "type": "VIOLENCE"}]}},
                        ]
                    }
                }
            },
        }

        client = BedrockClient(region="us-east-1", guardrail_id="gr-123")
        with pytest.raises(GuardrailBlockedError, match="gr-123"):
            client.invoke("sys", "usr")

    @patch("coa_common.bedrock.boto3")
    def test_guardrail_blocked_when_no_trace(self, mock_boto3):
        """Conservatively treat as blocked when trace is absent."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "blocked"}]}},
            "stopReason": "guardrail_intervened",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1", guardrail_id="gr-123")
        with pytest.raises(GuardrailBlockedError):
            client.invoke("sys", "usr")

    @patch("coa_common.bedrock.boto3")
    def test_guardrail_anonymize_does_not_raise(self, mock_boto3):
        """PII ANONYMIZE is not a block — response is usable with masked values."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"description": "Contact {EMAIL} for info"}'}]}},
            "stopReason": "guardrail_intervened",
            "usage": {"inputTokens": 10, "outputTokens": 5},
            "trace": {
                "guardrail": {
                    "outputAssessments": {
                        "gr-123": [
                            {
                                "sensitiveInformationPolicy": {
                                    "piiEntities": [{"action": "ANONYMIZED", "type": "EMAIL"}]
                                }
                            },
                        ]
                    }
                }
            },
        }

        client = BedrockClient(region="us-east-1", guardrail_id="gr-123")
        result = client.invoke("sys", "usr")
        assert result.result == {"description": "Contact {EMAIL} for info"}

    @patch("coa_common.bedrock.boto3")
    def test_custom_guardrail_version(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": '{"ok": true}'}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }

        client = BedrockClient(region="us-east-1", guardrail_id="gr-456", guardrail_version="3")
        client.invoke("sys", "usr")

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["guardrailConfig"]["guardrailVersion"] == "3"


class TestGuardrailDecisionMetrics:
    """#111 AC10/AC11 — Converse guardrail decisions are counted and logged."""

    _ALLOW_RESPONSE = {
        "output": {"message": {"content": [{"text": '{"ok": true}'}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }
    _BLOCK_RESPONSE = {
        "output": {"message": {"content": [{"text": "I cannot help."}]}},
        "stopReason": "guardrail_intervened",
        "usage": {"inputTokens": 10, "outputTokens": 5},
        "trace": {
            "guardrail": {
                "outputAssessments": {
                    "gr-123": [{"contentPolicy": {"filters": [{"action": "BLOCKED", "type": "VIOLENCE"}]}}]
                }
            }
        },
    }

    @staticmethod
    def _invoke(mock_boto3, response, **client_kwargs):
        """Invoke with a canned Converse response, returning the CloudWatch mock."""
        mock_client = MagicMock()
        mock_client.converse.return_value = response
        mock_boto3.client.return_value = mock_client
        cw = MagicMock()
        client = BedrockClient(region="us-east-1", **client_kwargs)
        with (
            patch("coa_common.guardrail_metrics._cloudwatch_client", return_value=cw),
            suppress(GuardrailBlockedError),
        ):
            client.invoke("sys", "usr")
        return cw

    @staticmethod
    def _metrics(cw):
        return {m["MetricName"]: m for m in cw.put_metric_data.call_args.kwargs["MetricData"]}

    @patch("coa_common.bedrock.boto3")
    def test_block_emits_invocations_blocked_and_latency(self, mock_boto3):
        cw = self._invoke(mock_boto3, self._BLOCK_RESPONSE, guardrail_id="gr-123")
        metrics = self._metrics(cw)
        assert set(metrics) == {"GuardrailInvocations", "GuardrailBlocked", "GuardrailLatency"}
        assert {"Name": "Decision", "Value": "BLOCK"} in metrics["GuardrailInvocations"]["Dimensions"]

    @patch("coa_common.bedrock.boto3")
    def test_allow_emits_invocations_without_blocked(self, mock_boto3):
        cw = self._invoke(mock_boto3, self._ALLOW_RESPONSE, guardrail_id="gr-123")
        metrics = self._metrics(cw)
        assert "GuardrailBlocked" not in metrics
        assert {"Name": "Decision", "Value": "ALLOW"} in metrics["GuardrailInvocations"]["Dimensions"]

    @patch("coa_common.bedrock.boto3")
    def test_no_metrics_when_unguarded(self, mock_boto3):
        """Counting unguarded calls as ALLOW would make the block rate meaningless."""
        cw = self._invoke(mock_boto3, self._ALLOW_RESPONSE)
        cw.put_metric_data.assert_not_called()

    @patch("coa_common.bedrock.boto3")
    def test_component_dimension_defaults_to_enrichment(self, mock_boto3):
        cw = self._invoke(mock_boto3, self._ALLOW_RESPONSE, guardrail_id="gr-123")
        for metric in self._metrics(cw).values():
            assert {"Name": "Component", "Value": "enrichment"} in metric["Dimensions"]

    @patch("coa_common.bedrock.boto3")
    def test_component_dimension_is_overridable(self, mock_boto3):
        cw = self._invoke(mock_boto3, self._ALLOW_RESPONSE, guardrail_id="gr-123", component="ontology-shapes")
        assert {"Name": "Component", "Value": "ontology-shapes"} in self._metrics(cw)["GuardrailInvocations"][
            "Dimensions"
        ]

    @patch("coa_common.bedrock.boto3")
    def test_decision_log_line_has_required_keys(self, mock_boto3):
        mock_client = MagicMock()
        mock_client.converse.return_value = self._BLOCK_RESPONSE
        mock_boto3.client.return_value = mock_client

        client = BedrockClient(region="us-east-1", guardrail_id="gr-123")
        with (
            patch("coa_common.guardrail_metrics.logger") as mock_logger,
            pytest.raises(GuardrailBlockedError),
        ):
            client.invoke("sys", "usr")

        assert mock_logger.info.call_args[0][0] == "guardrail_decision"
        kwargs = mock_logger.info.call_args.kwargs
        assert kwargs["component"] == "enrichment"
        assert kwargs["decision"] == "BLOCK"
        assert kwargs["filter_type"] == "CONTENT"
        assert kwargs["latency_ms"] >= 0

    @patch("coa_common.bedrock.boto3")
    def test_metric_failure_still_raises_on_block(self, mock_boto3):
        """A broken emitter must not let blocked content through."""
        mock_client = MagicMock()
        mock_client.converse.return_value = self._BLOCK_RESPONSE
        mock_boto3.client.return_value = mock_client
        cw = MagicMock()
        cw.put_metric_data.side_effect = RuntimeError("cloudwatch down")

        client = BedrockClient(region="us-east-1", guardrail_id="gr-123")
        with (
            patch("coa_common.guardrail_metrics._cloudwatch_client", return_value=cw),
            pytest.raises(GuardrailBlockedError),
        ):
            client.invoke("sys", "usr")

    @patch("coa_common.bedrock.boto3")
    def test_telemetry_double_fault_still_raises_on_block(self, mock_boto3):
        """The block must survive BOTH the metric put and the fallback log failing.

        The emit sits between the block-determination and the raise, so a
        telemetry exception escaping it would return blocked content to the
        caller as if the guardrail had allowed it.
        """
        mock_client = MagicMock()
        mock_client.converse.return_value = self._BLOCK_RESPONSE
        mock_boto3.client.return_value = mock_client
        cw = MagicMock()
        cw.put_metric_data.side_effect = RuntimeError("cloudwatch down")

        client = BedrockClient(region="us-east-1", guardrail_id="gr-123")
        with (
            patch("coa_common.guardrail_metrics._cloudwatch_client", return_value=cw),
            patch("coa_common.guardrail_metrics.logger") as mock_logger,
            pytest.raises(GuardrailBlockedError),
        ):
            mock_logger.info.side_effect = RuntimeError("stdout closed")
            mock_logger.warning.side_effect = RuntimeError("stdout closed")
            client.invoke("sys", "usr")
