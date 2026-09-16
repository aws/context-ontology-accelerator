# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Bedrock LLMClient."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestBedrockLLMClient:
    async def test_converse_returns_text(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "Generated SPARQL"}]}}}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        result = await client.converse("Generate SPARQL for: show claims")

        assert result.text == "Generated SPARQL"
        assert result.guardrail_blocked is False

    async def test_converse_passes_guardrail(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        await client.converse("test", guardrail_id="gr-123")

        call_kwargs = mock_client.converse.call_args[1]
        assert "guardrailConfig" in call_kwargs
        assert call_kwargs["guardrailConfig"]["guardrailIdentifier"] == "gr-123"

    async def test_converse_uses_configured_guardrail_version(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1", guardrail_version="3")
        client._client = mock_client
        await client.converse("test", guardrail_id="gr-123")

        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["guardrailConfig"]["guardrailVersion"] == "3"

    async def test_converse_passes_system_prompt(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        await client.converse("test", system="You are a SPARQL expert")

        call_kwargs = mock_client.converse.call_args[1]
        assert "system" in call_kwargs
        assert call_kwargs["system"][0]["text"] == "You are a SPARQL expert"

    async def test_embed_returns_vector(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        client = BedrockLLMClient(region="us-east-1")
        # embed() delegates to the shared BedrockEmbedder, embedding the query
        # with search_query semantics. Mock that boundary.
        client._embedder = MagicMock()
        client._embedder.embed_query.return_value = [0.1] * 1024

        result = await client.embed("test text")

        assert len(result) == 1024
        client._embedder.embed_query.assert_called_once_with("test text")

    async def test_converse_raises_on_throttle(self):
        from botocore.exceptions import ClientError
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "Converse",
        )

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        with pytest.raises(ClientError):
            await client.converse("test")

    async def test_converse_raises_on_empty_content(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": []}},
            "stopReason": "guardrail_intervened",
        }

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        with pytest.raises(ValueError, match="No text content"):
            await client.converse("test")

    async def test_health_check_returns_error_on_failure(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.invoke_model.side_effect = RuntimeError("network error")

        client = BedrockLLMClient(region="us-east-1")
        client._client = mock_client
        result = await client.health_check()
        assert result["status"] == "error"
        assert result["detail"] == "Health check failed"

    async def test_close_cleans_up_client(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        client = BedrockLLMClient(region="us-east-1")
        client._client = mock_client

        await client.close()
        mock_client.close.assert_called_once()
        assert client._client is None

    async def test_converse_guard_content_builds_correct_structure(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        await client.converse("context here", guardrail_id="gr-1", guard_content="Question: hello")

        call_kwargs = mock_client.converse.call_args[1]
        messages = call_kwargs["messages"]
        content_blocks = messages[0]["content"]
        assert len(content_blocks) == 2
        assert content_blocks[0] == {"text": "context here"}
        assert content_blocks[1] == {"guardContent": {"text": {"text": "Question: hello"}}}

    async def test_converse_guard_content_sent_without_guardrail(self):
        """The guarded text must still reach the model, as PLAIN text.

        tier2 callers use guardContent as the sole channel for the user's
        question, so dropping it would lose the question. Keeping the tag would
        lose the whole call: Converse rejects a guardContent block that arrives
        without a guardrailConfig ("The guardrail can't assess the content in the
        guardContent field"), and an unguarded deployment then fails EVERY
        generation — 135/135 questions, empty SQL, on the first benchmark run of
        SERVE_GUARDRAILS_DISABLED.
        """
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        await client.converse("context here", guardrail_id=None, guard_content="Question: hello")

        call_kwargs = mock_client.converse.call_args[1]
        content_blocks = call_kwargs["messages"][0]["content"]
        assert content_blocks == [
            {"text": "context here"},
            {"text": "Question: hello"},
        ]
        # And without a guardrail_id, no guardrailConfig on the request.
        assert "guardrailConfig" not in call_kwargs

    async def test_converse_guardrail_anonymize_not_blocked(self):
        """PII ANONYMIZE should NOT set guardrail_blocked=True."""
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Contact {EMAIL} for help"}]}},
            "stopReason": "guardrail_intervened",
            "trace": {
                "guardrail": {
                    "outputAssessment": {
                        "0": {
                            "sensitiveInformationPolicy": {
                                "piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED", "match": "test@example.com"}]
                            }
                        }
                    }
                }
            },
        }

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        result = await client.converse("test", guardrail_id="gr-1")

        assert result.guardrail_blocked is False
        assert result.text == "Contact {EMAIL} for help"

    async def test_converse_guardrail_blocked_on_content_policy(self):
        """Actual BLOCKED action should set guardrail_blocked=True."""
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Sorry, I can't help with that."}]}},
            "stopReason": "guardrail_intervened",
            "trace": {
                "guardrail": {
                    "outputAssessment": {
                        "0": {
                            "contentPolicy": {
                                "filters": [{"type": "VIOLENCE", "action": "BLOCKED", "confidence": "HIGH"}]
                            }
                        }
                    }
                }
            },
        }

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        result = await client.converse("test", guardrail_id="gr-1")

        assert result.guardrail_blocked is True


@pytest.mark.unit
class TestGuardrailDecisionMetrics:
    """#111 AC10/AC11 — every guarded Converse emits metrics plus one log line.

    Serve emits stdout EMF (no PutMetricData grant on the runtime role), so these
    assert on the shared emitter's arguments rather than on a boto call.
    """

    _BLOCK_TRACE = {
        "guardrail": {
            "outputAssessment": {"0": {"contentPolicy": {"filters": [{"type": "VIOLENCE", "action": "BLOCKED"}]}}}
        }
    }

    def _client(self, response: dict):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = response
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        return client

    async def test_block_emits_block_decision_via_emf(self):
        client = self._client(
            {
                "output": {"message": {"content": [{"text": "no"}]}},
                "stopReason": "guardrail_intervened",
                "trace": self._BLOCK_TRACE,
            }
        )

        with patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit:
            await client.converse("test", guardrail_id="gr-1")

        kwargs = emit.call_args.kwargs
        assert kwargs["blocked"] is True
        assert kwargs["component"] == "nl-to-sparql"
        assert kwargs["filter_type"] == "CONTENT"
        assert kwargs["transport"] == "emf"
        assert kwargs["latency_ms"] >= 0

    async def test_allow_also_emits(self):
        # A guardrail that stopped being called looks identical to one that
        # never blocks unless the allow path is counted too.
        client = self._client({"output": {"message": {"content": [{"text": "ok"}]}}})

        with patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit:
            await client.converse("test", guardrail_id="gr-1")

        kwargs = emit.call_args.kwargs
        assert kwargs["blocked"] is False
        assert kwargs["filter_type"] == "NONE"

    async def test_unguarded_call_emits_nothing(self):
        # Counting unguarded calls as ALLOW would dilute the block rate.
        client = self._client({"output": {"message": {"content": [{"text": "ok"}]}}})

        with patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit:
            await client.converse("test")

        emit.assert_not_called()

    async def test_block_with_no_text_still_emits_before_raising(self):
        # A hard block is both the decision most worth counting and the case
        # most likely to come back with no text blocks at all. The trace is
        # required for this to BE a confirmed block: without one the outcome is
        # UNKNOWN by design (see test_traceless_no_text_emits_unknown).
        client = self._client(
            {
                "output": {"message": {"content": []}},
                "stopReason": "guardrail_intervened",
                "trace": self._BLOCK_TRACE,
            }
        )

        with (
            patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit,
            pytest.raises(ValueError, match="No text content"),
        ):
            await client.converse("test", guardrail_id="gr-1")

        assert emit.call_args.kwargs["blocked"] is True

    async def test_traceless_no_text_emits_unknown_not_block(self):
        # The old code treated any traceless intervention as a confirmed block,
        # which is what inflated GuardrailBlocked and discarded 71 masked answers.
        client = self._client({"output": {"message": {"content": []}}, "stopReason": "guardrail_intervened"})

        with (
            patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit,
            pytest.raises(ValueError, match="No text content"),
        ):
            await client.converse("test", guardrail_id="gr-1")

        kwargs = emit.call_args.kwargs
        assert kwargs["decision"] == "UNKNOWN"
        assert kwargs["blocked"] is False

    async def test_decision_log_line_carries_required_keys(self):
        from coa_common import guardrail_metrics

        client = self._client(
            {
                "output": {"message": {"content": [{"text": "no"}]}},
                "stopReason": "guardrail_intervened",
                "trace": self._BLOCK_TRACE,
            }
        )

        with patch.object(guardrail_metrics, "logger") as mock_logger:
            await client.converse("test", guardrail_id="gr-1")

        decision_calls = [c for c in mock_logger.info.call_args_list if c[0][0] == "guardrail_decision"]
        assert len(decision_calls) == 1
        assert set(decision_calls[0].kwargs) == {"component", "decision", "filter_type", "latency_ms"}
        assert decision_calls[0].kwargs["decision"] == "BLOCK"

    async def test_metric_failure_does_not_break_converse(self):
        # Telemetry must never cost us the guardrail verdict.
        client = self._client(
            {
                "output": {"message": {"content": [{"text": "no"}]}},
                "stopReason": "guardrail_intervened",
                "trace": self._BLOCK_TRACE,
            }
        )

        with patch("coa_common.guardrail_metrics.emit_metric", side_effect=RuntimeError("boom")):
            result = await client.converse("test", guardrail_id="gr-1")

        assert result.guardrail_blocked is True


@pytest.mark.unit
class TestBedrockConverseStream:
    """Tests for converse_stream() method."""

    async def test_stream_yields_tokens(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        events = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"text": "Hello"}}},
            {"contentBlockDelta": {"delta": {"text": " world"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 5}}},
        ]
        mock_client = MagicMock()
        mock_client.converse_stream.return_value = {"stream": iter(events)}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        tokens = []
        async for token in client.converse_stream("test prompt"):
            tokens.append(token)

        assert tokens == ["Hello", " world"]

    async def test_stream_guardrail_anonymize_no_error(self):
        """PII ANONYMIZE in stream should NOT raise GuardrailBlockedError."""
        from coa_serve.clients.bedrock import BedrockLLMClient

        events = [
            {"contentBlockDelta": {"delta": {"text": "Contact "}}},
            {"contentBlockDelta": {"delta": {"text": "{EMAIL}"}}},
            {"messageStop": {"stopReason": "guardrail_intervened"}},
            {
                "metadata": {
                    "trace": {
                        "guardrail": {
                            "outputAssessment": {
                                "0": {
                                    "sensitiveInformationPolicy": {
                                        "piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED"}]
                                    }
                                }
                            }
                        }
                    }
                }
            },
        ]
        mock_client = MagicMock()
        mock_client.converse_stream.return_value = {"stream": iter(events)}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        tokens = []
        async for token in client.converse_stream("test", guardrail_id="gr-1"):
            tokens.append(token)

        assert tokens == ["Contact ", "{EMAIL}"]

    async def test_stream_guardrail_blocked_raises_error(self):
        """Actual BLOCKED action in stream should raise GuardrailBlockedError."""
        from coa_serve.clients.base import GuardrailBlockedError
        from coa_serve.clients.bedrock import BedrockLLMClient

        events = [
            {"contentBlockDelta": {"delta": {"text": "partial"}}},
            {"messageStop": {"stopReason": "guardrail_intervened"}},
            {
                "metadata": {
                    "trace": {
                        "guardrail": {
                            "outputAssessment": {
                                "0": {
                                    "contentPolicy": {
                                        "filters": [{"type": "HATE", "action": "BLOCKED", "confidence": "HIGH"}]
                                    }
                                }
                            }
                        }
                    }
                }
            },
        ]
        mock_client = MagicMock()
        mock_client.converse_stream.return_value = {"stream": iter(events)}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        with pytest.raises(GuardrailBlockedError):
            async for _ in client.converse_stream("test", guardrail_id="gr-1"):
                pass

    async def test_stream_thread_exception_propagates(self):
        """Exceptions from the thread should propagate to the caller."""
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse_stream.side_effect = RuntimeError("API failure")

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        with pytest.raises(RuntimeError, match="API failure"):
            async for _ in client.converse_stream("test"):
                pass

    async def test_stream_passes_guard_content(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        events = [
            {"contentBlockDelta": {"delta": {"text": "ok"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {}},
        ]
        mock_client = MagicMock()
        mock_client.converse_stream.return_value = {"stream": iter(events)}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        async for _ in client.converse_stream("ctx", guardrail_id="gr-1", guard_content="Question: hi"):
            pass

        call_kwargs = mock_client.converse_stream.call_args[1]
        content_blocks = call_kwargs["messages"][0]["content"]
        assert content_blocks[1] == {"guardContent": {"text": {"text": "Question: hi"}}}
        assert call_kwargs["guardrailConfig"]["streamProcessingMode"] == "sync"

    async def test_stream_untags_guard_content_without_a_guardrail(self):
        """The streamed path shares the kwargs builder, so it has the same rule.

        Tier-3 synthesis streams and also puts the user's query in guardContent, so
        an unguarded deployment would fail every synthesis for the same reason
        Tier-2 failed every generation.
        """
        from coa_serve.clients.bedrock import BedrockLLMClient

        events = [
            {"contentBlockDelta": {"delta": {"text": "ok"}}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {}},
        ]
        mock_client = MagicMock()
        mock_client.converse_stream.return_value = {"stream": iter(events)}

        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        async for _ in client.converse_stream("ctx", guardrail_id=None, guard_content="Question: hi"):
            pass

        call_kwargs = mock_client.converse_stream.call_args[1]
        assert call_kwargs["messages"][0]["content"] == [{"text": "ctx"}, {"text": "Question: hi"}]
        assert "guardrailConfig" not in call_kwargs

    async def test_converse_model_id_override(self):
        """Verify per-call model_id override reaches Bedrock API kwargs."""
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "result"}]}}}

        client = BedrockLLMClient(model_id="default-model", region="us-east-1")
        client._client = mock_client

        # Without override — uses default
        await client.converse("test prompt")
        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["modelId"] == "default-model"

        # With override — uses override
        await client.converse("test prompt", model_id="us.anthropic.claude-haiku-4-0")
        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["modelId"] == "us.anthropic.claude-haiku-4-0"

    async def test_converse_stream_model_id_override(self):
        """Verify per-call model_id override reaches converse_stream kwargs."""
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_stream_response = {
            "stream": iter(
                [
                    {"contentBlockDelta": {"delta": {"text": "hello"}}},
                    {"messageStop": {"stopReason": "end_turn"}},
                ]
            )
        }
        mock_client.converse_stream.return_value = mock_stream_response

        client = BedrockLLMClient(model_id="default-model", region="us-east-1")
        client._client = mock_client

        tokens = []
        async for token in client.converse_stream("test", model_id="us.anthropic.claude-opus-4-0"):
            tokens.append(token)

        call_kwargs = mock_client.converse_stream.call_args[1]
        assert call_kwargs["modelId"] == "us.anthropic.claude-opus-4-0"

    async def test_model_id_override_none_uses_default(self):
        """Verify that model_id=None falls back to configured default."""
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "result"}]}}}

        client = BedrockLLMClient(model_id="my-default", region="us-east-1")
        client._client = mock_client

        await client.converse("test", model_id=None)
        call_kwargs = mock_client.converse.call_args[1]
        assert call_kwargs["modelId"] == "my-default"

    async def test_converse_temperature_fallback_retries_without_temperature(self):
        """Verify temperature fallback retries on ValidationException mentioning temperature."""
        from botocore.exceptions import ClientError
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("new-model")
        mock_client = MagicMock()
        error_response = {"Error": {"Code": "ValidationException", "Message": "temperature is not supported"}}
        mock_client.converse.side_effect = [
            ClientError(error_response, "Converse"),
            {"output": {"message": {"content": [{"text": "ok"}]}}},
        ]

        client = BedrockLLMClient(model_id="new-model", region="us-east-1")
        client._client = mock_client

        result = await client.converse("test", temperature=0.5)
        assert result.text == "ok"
        # Second call should not have temperature
        second_call_kwargs = mock_client.converse.call_args_list[1][1]
        assert "temperature" not in second_call_kwargs.get("inferenceConfig", {})

    async def test_converse_non_temperature_error_propagates(self):
        """Verify non-temperature ValidationException is not retried."""
        from botocore.exceptions import ClientError
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        error_response = {"Error": {"Code": "ValidationException", "Message": "invalid model"}}
        mock_client.converse.side_effect = ClientError(error_response, "Converse")

        client = BedrockLLMClient(model_id="bad-model", region="us-east-1")
        client._client = mock_client

        with pytest.raises(ClientError):
            await client.converse("test")
        assert mock_client.converse.call_count == 1

    async def test_retry_loop_reraises_when_params_exhausted_no_fourth_call(self):
        """Bot finding (HIGH): the loop must not fall through to a silent 4th call.

        If every iteration raises a ValidationException that names a sampling
        param that has ALREADY been stripped (so nothing more can be shed), the
        underlying ClientError must propagate — the call must NOT be retried a
        4th time past the ``for _ in range(3)`` budget.
        """
        from botocore.exceptions import ClientError
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("stuck-model")
        bedrock_mod._MODELS_REJECTING_TOP_P.discard("stuck-model")
        # Names "temperature" every time, but after the first strip temperature is
        # gone from inferenceConfig, so the guard `"temperature" in inference` is
        # False and the handler re-raises rather than looping to a 4th call.
        err = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "temperature is not supported"}},
            "Converse",
        )
        mock_client = MagicMock()
        mock_client.converse.side_effect = [err, err, err, err]

        client = BedrockLLMClient(model_id="stuck-model", region="us-east-1")
        client._client = mock_client

        with pytest.raises(ClientError):
            await client.converse("test", temperature=0.5)
        # 1 initial + 1 retry after stripping temperature, then re-raise. NOT 4.
        assert mock_client.converse.call_count == 2

    async def test_temperature_rejection_is_remembered_per_model(self):
        """The doomed first call is paid ONCE per model, not on every call.

        Without memoization every Converse to a temperature-rejecting model
        burned a failed request plus a retry (68% of calls in a measured integ
        run), doubling LLM latency for the whole suite.
        """
        from botocore.exceptions import ClientError
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("memo-model")
        ok = {"output": {"message": {"content": [{"text": "ok"}]}}}
        err = ClientError({"Error": {"Code": "ValidationException", "Message": "temperature is not supported"}}, "C")

        mock_client = MagicMock()
        mock_client.converse.side_effect = [err, ok, ok]

        client = BedrockLLMClient(model_id="memo-model", region="us-east-1")
        client._client = mock_client

        first = await client.converse("test", temperature=0.5)
        second = await client.converse("test", temperature=0.5)

        assert (first.text, second.text) == ("ok", "ok")
        # 2 for the first converse (rejected + retry), 1 for the second — not 4.
        assert mock_client.converse.call_count == 3
        assert "temperature" not in mock_client.converse.call_args_list[2][1].get("inferenceConfig", {})

    async def test_temperature_memo_does_not_leak_across_models(self):
        """One model rejecting temperature must not disable it for others."""
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TEMPERATURE.add("rejecting-model")
        bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("accepting-model")

        mock_client = MagicMock()
        mock_client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        client = BedrockLLMClient(model_id="accepting-model", region="us-east-1")
        client._client = mock_client

        await client.converse("test", temperature=0.5)
        assert mock_client.converse.call_args[1]["inferenceConfig"]["temperature"] == 0.5


@pytest.mark.unit
class TestConverseMultipleTextBlocks:
    """The Converse API may split a response across several content blocks."""

    async def test_all_text_blocks_are_joined(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {"text": "SELECT ?claim WHERE {"},
                        {"text": " ?claim a :Claim }"},
                    ]
                }
            }
        }
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        result = await client.converse("q")

        assert result.text == "SELECT ?claim WHERE { ?claim a :Claim }", (
            "blocks after the first were dropped, truncating the generation"
        )

    async def test_non_text_blocks_are_still_skipped(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {"text": "a"},
                        {"toolUse": {"name": "x"}},
                        {"text": "b"},
                    ]
                }
            }
        }
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        assert (await client.converse("q")).text == "ab"


@pytest.mark.unit
class TestConverseStreamCleanup:
    """Generator finalization must not block on the uncancellable worker thread.

    The worker iterates boto3's synchronous EventStream in a thread-pool thread, so
    there is no cancellation point: `await task` in `finally` waited out
    BEDROCK_READ_TIMEOUT, defeating the idle-timeout guard, and on early close it
    suspended inside GeneratorExit handling — which CPython rejects for async
    generators ("async generator ignored GeneratorExit").
    """

    class _BlockingStream:
        """Emits one chunk, then blocks like boto3's EventStream until closed."""

        def __init__(self, ticks: int = 200, tick_s: float = 0.05):
            self.closed = False
            self._ticks = ticks
            self._tick_s = tick_s

        def __iter__(self):
            import time

            yield {"contentBlockDelta": {"delta": {"text": "hello"}}}
            for _ in range(self._ticks):
                if self.closed:
                    raise RuntimeError("stream closed")
                time.sleep(self._tick_s)

        def close(self):
            self.closed = True

    def _client_over(self, stream):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse_stream.return_value = {"stream": stream}
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client
        return client

    async def test_early_close_returns_promptly(self):
        import time

        stream = self._BlockingStream()
        agen = self._client_over(stream).converse_stream("q")

        assert await agen.__anext__() == "hello"
        started = time.monotonic()
        await agen.aclose()
        elapsed = time.monotonic() - started

        assert elapsed < 2.0, f"aclose() blocked for {elapsed:.1f}s waiting on the worker thread"

    async def test_early_close_closes_the_upstream_stream(self):
        """Otherwise the worker keeps consuming Bedrock (and billing) after disconnect."""
        stream = self._BlockingStream()
        agen = self._client_over(stream).converse_stream("q")

        await agen.__anext__()
        await agen.aclose()

        assert stream.closed

    async def test_early_close_does_not_raise(self):
        stream = self._BlockingStream()
        agen = self._client_over(stream).converse_stream("q")

        await agen.__anext__()
        await agen.aclose()  # must not raise "async generator ignored GeneratorExit"

    async def test_normal_completion_still_yields_every_token(self):
        """The detached-worker change must not truncate a healthy stream."""
        events = [
            {"contentBlockDelta": {"delta": {"text": "a"}}},
            {"contentBlockDelta": {"delta": {"text": "b"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
        client = self._client_over(iter(events))

        assert [t async for t in client.converse_stream("q")] == ["a", "b"]

    async def test_abandon_during_the_api_call_still_releases_the_worker(self):
        """The window before the EventStream exists.

        `converse_stream` (the boto3 call) takes time; a client disconnecting during
        it finds nothing to close, so the worker would drain the entire response —
        holding an executor slot and billing tokens for a caller already gone. The
        `abandoned` flag covers that window.
        """
        import threading
        import time

        closed = threading.Event()
        drained_fully = threading.Event()

        class _Stream:
            def __iter__(self):
                for _ in range(40):
                    if closed.is_set():
                        raise RuntimeError("closed")
                    time.sleep(0.02)
                drained_fully.set()
                yield {"contentBlockDelta": {"delta": {"text": "late"}}}

            def close(self):
                closed.set()

        def _slow_api(**_kwargs):
            time.sleep(0.3)  # API in flight — nothing closeable yet
            return {"stream": _Stream()}

        from coa_serve.clients.bedrock import BedrockLLMClient

        mock_client = MagicMock()
        mock_client.converse_stream = _slow_api
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mock_client

        agen = client.converse_stream("q")
        pending = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.05)  # disconnect BEFORE the API returns
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        await agen.aclose()

        # The worker sees the flag right after the API call returns.
        for _ in range(50):
            if closed.is_set():
                break
            await asyncio.sleep(0.05)

        assert closed.is_set(), "worker kept the stream open after the caller left"
        assert not drained_fully.is_set(), "worker drained the whole response for a gone caller"

    async def test_normal_completion_is_not_treated_as_abandoned(self):
        """The flag must not fire on the healthy path and truncate the stream."""
        events = [
            {"contentBlockDelta": {"delta": {"text": "a"}}},
            {"contentBlockDelta": {"delta": {"text": "b"}}},
            {"contentBlockDelta": {"delta": {"text": "c"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]
        client = self._client_over(iter(events))

        assert [t async for t in client.converse_stream("q")] == ["a", "b", "c"]

    async def test_stream_without_close_method_is_tolerated(self):
        """A plain iterator has no close(); finalization must still not raise."""
        agen = self._client_over(iter([{"contentBlockDelta": {"delta": {"text": "x"}}}])).converse_stream("q")

        assert await agen.__anext__() == "x"
        await agen.aclose()


@pytest.mark.unit
class TestModelIdValidation:
    def test_validate_rejects_empty(self):
        from coa_serve.model_validation import validate_model_id

        with pytest.raises(ValueError, match="non-empty string"):
            validate_model_id("")

    def test_validate_rejects_whitespace(self):
        from coa_serve.model_validation import validate_model_id

        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model with spaces")

    def test_validate_rejects_too_long(self):
        from coa_serve.model_validation import validate_model_id

        with pytest.raises(ValueError, match="too long"):
            validate_model_id("x" * 300)

    def test_validate_rejects_control_characters(self):
        from coa_serve.model_validation import validate_model_id

        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model\tid")
        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model\r\ninjection")
        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model\x00null")

    def test_validate_rejects_special_characters(self):
        from coa_serve.model_validation import validate_model_id

        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model;drop table")
        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model$(cmd)")
        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model&id")

    def test_validate_accepts_valid_model(self):
        from coa_serve.model_validation import validate_model_id

        # Should not raise
        validate_model_id("us.anthropic.claude-sonnet-4-6")
        validate_model_id("us.anthropic.claude-haiku-4-0")
        validate_model_id("arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0")

    def test_validate_allowlist_rejects_unlisted(self):
        from coa_serve import model_validation

        orig = model_validation._ALLOWED_OVERRIDE_MODELS
        try:
            model_validation._ALLOWED_OVERRIDE_MODELS = {"model-a", "model-b"}
            # Should accept listed model
            model_validation.validate_model_id("model-a")
            # Should reject unlisted model
            with pytest.raises(ValueError, match="not permitted for override"):
                model_validation.validate_model_id("model-c")
        finally:
            model_validation._ALLOWED_OVERRIDE_MODELS = orig

    def test_validate_no_allowlist_accepts_all(self):
        from coa_serve import model_validation

        orig = model_validation._ALLOWED_OVERRIDE_MODELS
        try:
            model_validation._ALLOWED_OVERRIDE_MODELS = None
            # Should accept anything with valid format
            model_validation.validate_model_id("any.model.name-here")
        finally:
            model_validation._ALLOWED_OVERRIDE_MODELS = orig


def _trace(side: str, policy: str, items_key: str, items: list[dict]) -> dict:
    """Build a guardrail trace with one assessment on ``side``."""
    return {"guardrail": {side: {"0": {policy: {items_key: items}}}}}


_ANON_NAME = [{"type": "NAME", "action": "ANONYMIZED", "match": "Fidel Castro"}]
_BLOCK_VIOLENCE = [{"type": "VIOLENCE", "action": "BLOCKED", "confidence": "HIGH"}]


def _converse_client(response: dict):
    from coa_serve.clients.bedrock import BedrockLLMClient

    mock_client = MagicMock()
    mock_client.converse.return_value = response
    client = BedrockLLMClient(model_id="test-model", region="us-east-1")
    client._client = mock_client
    return client


def _intervened(trace: dict | None, text: str = "Eight U.S. presidents served during {NAME}'s rule.") -> dict:
    response: dict = {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": "guardrail_intervened",
    }
    if trace is not None:
        response["trace"] = trace
    return response


@pytest.mark.unit
class TestGuardrailOutcomeClassification:
    """A masked answer must survive; only a confirmed block may be suppressed.

    Serve discarded 71 masked answers because ``stopReason=guardrail_intervened``
    fires for masking as well as blocking, the trace that distinguishes them was
    never requested, and a missing trace was read as a block.
    """

    async def test_guardrail_config_requests_the_trace(self):
        # Without this the outcome is unclassifiable, so it is the regression guard
        # that matters most: every other assertion here is reachable only if the
        # request actually asks Bedrock for the trace.
        client = _converse_client({"output": {"message": {"content": [{"text": "ok"}]}}, "stopReason": "end_turn"})

        await client.converse("test", guardrail_id="gr-1")

        cfg = client._client.converse.call_args.kwargs["guardrailConfig"]
        assert cfg["trace"] == "enabled"

    async def test_no_guardrail_id_sends_no_guardrail_config(self):
        client = _converse_client({"output": {"message": {"content": [{"text": "ok"}]}}, "stopReason": "end_turn"})

        await client.converse("test")

        assert "guardrailConfig" not in client._client.converse.call_args.kwargs

    async def test_anonymized_output_keeps_text(self):
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(_trace("outputAssessment", "sensitiveInformationPolicy", "piiEntities", _ANON_NAME))
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.ANONYMIZED
        assert result.guardrail_blocked is False
        assert "{NAME}" in result.text

    async def test_anonymized_on_input_assessment_only_keeps_text(self):
        """The live-reproduced case: PII NAME is reported on the INPUT side.

        Reading only ``outputAssessment`` made this look like a block, which is why
        person-name questions came back empty.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(_trace("inputAssessment", "sensitiveInformationPolicy", "piiEntities", _ANON_NAME))
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.ANONYMIZED
        assert result.guardrail_blocked is False
        assert result.text.startswith("Eight U.S. presidents")

    async def test_blocked_on_output_assessment(self):
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                _trace("outputAssessment", "contentPolicy", "filters", _BLOCK_VIOLENCE),
                text="Response blocked by content filter.",
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.BLOCKED
        assert result.guardrail_blocked is True

    async def test_blocked_on_input_assessment_only(self):
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                _trace("inputAssessment", "contentPolicy", "filters", _BLOCK_VIOLENCE),
                text="Request blocked by content filter.",
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.BLOCKED

    async def test_block_wins_over_anonymize(self):
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "inputAssessment": {"0": {"sensitiveInformationPolicy": {"piiEntities": _ANON_NAME}}},
                        "outputAssessment": {"0": {"contentPolicy": {"filters": _BLOCK_VIOLENCE}}},
                    }
                }
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.BLOCKED

    async def test_missing_trace_is_unknown_and_empties_text(self):
        """The production path: intervened, no trace.

        Fails closed — but as UNKNOWN, not as a confirmed block — and the text is
        emptied because it may be real model output we could not classify.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(_intervened(None))

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.UNKNOWN
        assert result.guardrail_blocked is True
        assert result.text == ""

    async def test_empty_trace_is_unknown_not_none(self):
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(_intervened({"guardrail": {}}))

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.UNKNOWN

    async def test_anonymized_plus_unrecognized_policy_is_unknown(self):
        """MIXED trace: recognized masking alongside a shape we cannot read.

        The unreadable shape might have been a block, so reporting "just masked"
        would fail OPEN. An earlier version of the classifier ordered ANONYMIZED
        ahead of the unparsed-shape check and did exactly that.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "inputAssessment": {"0": {"sensitiveInformationPolicy": {"piiEntities": _ANON_NAME}}},
                        "outputAssessment": {
                            "0": {"contextualGroundingPolicy": {"filters": [{"action": "SOMETHING"}]}}
                        },
                    }
                }
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.UNKNOWN
        assert result.guardrail_blocked is True
        assert result.text == ""

    async def test_anonymized_beside_a_malformed_side_is_unknown(self):
        """A readable ANONYMIZED next to an UNREADABLE sibling must not pass.

        Regression: the unparsed-shape check originally walked
        ``assessments_from_trace``, which silently drops non-dict sides. The
        malformed side was therefore invisible and the response was classified as
        plain masking — failing OPEN on input that might have carried a block.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "inputAssessment": {"0": {"sensitiveInformationPolicy": {"piiEntities": _ANON_NAME}}},
                        "outputAssessment": "unparseable-string-that-might-have-been-a-block",
                    }
                }
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.UNKNOWN
        assert result.text == ""

    async def test_anonymized_beside_a_malformed_entry_is_unknown(self):
        """Same hole, one level deeper: a non-dict entry inside a valid side."""
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "inputAssessment": {"0": {"sensitiveInformationPolicy": {"piiEntities": _ANON_NAME}}},
                        "outputAssessment": {"0": "not-a-dict"},
                    }
                }
            )
        )

        assert (await client.converse("test", guardrail_id="gr-1")).outcome is GuardrailOutcome.UNKNOWN

    async def test_non_list_policy_items_is_unknown(self):
        """A policy whose items are not a list is unreadable, not empty."""
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {"guardrail": {"outputAssessment": {"0": {"sensitiveInformationPolicy": {"piiEntities": "x"}}}}}
            )
        )

        assert (await client.converse("test", guardrail_id="gr-1")).outcome is GuardrailOutcome.UNKNOWN

    async def test_non_assessment_metadata_does_not_force_unknown(self):
        """Trace metadata carries no action, so it must not suppress a good answer.

        Guards the opposite failure: over-triggering UNKNOWN would blank every
        guarded response.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "inputAssessment": {"0": {"sensitiveInformationPolicy": {"piiEntities": _ANON_NAME}}},
                        "actionReason": "PII masked",
                    }
                }
            )
        )

        assert (await client.converse("test", guardrail_id="gr-1")).outcome is GuardrailOutcome.ANONYMIZED

    async def test_anonymized_plus_unknown_action_value_is_unknown(self):
        """A known policy carrying an action outside the known set is unparsed."""
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "outputAssessment": {
                            "0": {
                                "sensitiveInformationPolicy": {
                                    "piiEntities": [
                                        {"type": "NAME", "action": "ANONYMIZED"},
                                        {"type": "EMAIL", "action": "FUTURE_ACTION"},
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        )

        assert (await client.converse("test", guardrail_id="gr-1")).outcome is GuardrailOutcome.UNKNOWN

    async def test_blocked_still_wins_over_an_unrecognized_shape(self):
        """An explicit block is definite, so it outranks the unparsed check."""
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "outputAssessment": {"0": {"contentPolicy": {"filters": _BLOCK_VIOLENCE}}},
                        "inputAssessment": {"0": {"contextualGroundingPolicy": {"filters": [{"action": "X"}]}}},
                    }
                }
            )
        )

        assert (await client.converse("test", guardrail_id="gr-1")).outcome is GuardrailOutcome.BLOCKED

    async def test_invocation_metrics_alone_is_not_unparsed(self):
        """``invocationMetrics`` is metadata, not a policy — it must not force UNKNOWN."""
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "outputAssessment": {
                            "0": {
                                "sensitiveInformationPolicy": {"piiEntities": _ANON_NAME},
                                "invocationMetrics": {"guardrailProcessingLatency": 42},
                            }
                        }
                    }
                }
            )
        )

        assert (await client.converse("test", guardrail_id="gr-1")).outcome is GuardrailOutcome.ANONYMIZED

    async def test_real_trace_with_applied_guardrail_details_is_anonymized(self):
        """Regression for the live shape: a real Bedrock trace attaches
        ``appliedGuardrailDetails`` and ``invocationMetrics`` to every assessment.

        The earlier name-allowlist only knew ``invocationMetrics``, so the
        unlisted ``appliedGuardrailDetails`` forced UNKNOWN — and benign name
        questions (e.g. "Fidel Castro") came back blocked and empty even though the
        guardrail had only masked. Neither key carries an action, so both must be
        ignored and the answer classified as ANONYMIZED.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "modelOutput": ["Eight U.S. presidents served during {NAME}'s rule."],
                        "inputAssessment": {
                            "gr-1": {
                                "sensitiveInformationPolicy": {
                                    "piiEntities": [
                                        {
                                            "match": "Fidel Castro",
                                            "type": "NAME",
                                            "action": "ANONYMIZED",
                                            "detected": True,
                                        }
                                    ]
                                },
                                "invocationMetrics": {"guardrailProcessingLatency": 87},
                                "appliedGuardrailDetails": {
                                    "guardrailId": "gr-1",
                                    "guardrailVersion": "1",
                                    "guardrailArn": "arn:aws:bedrock:us-east-1:123456789012:guardrail/gr-1",
                                },
                            }
                        },
                        "outputAssessments": None,
                        "actionReason": "Guardrail masked PII.",
                    }
                }
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.ANONYMIZED
        assert result.guardrail_blocked is False
        assert "{NAME}" in result.text

    async def test_plural_output_assessments_list_is_parsed(self):
        """``outputAssessments`` (plural) nests the assessment inside a LIST.

        The side value is ``{guardrailId: [assessment, ...]}`` rather than a single
        assessment dict. The unparsed-shape walk must descend into that list — an
        earlier version saw the list itself as a non-dict entry and forced UNKNOWN,
        blanking every response that carried a plural output side.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {
                    "guardrail": {
                        "outputAssessments": {
                            "gr-1": [
                                {
                                    "sensitiveInformationPolicy": {"piiEntities": _ANON_NAME},
                                    "appliedGuardrailDetails": {"guardrailId": "gr-1", "guardrailVersion": "1"},
                                }
                            ]
                        }
                    }
                }
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.ANONYMIZED
        assert "{NAME}" in result.text

    async def test_unrecognized_policy_shape_is_unknown_not_none(self):
        """A policy type absent from the known paths must not read as 'allowed'.

        ``_GUARDRAIL_POLICY_PATHS`` is a fixed list of six, so a new Bedrock policy
        would otherwise silently fail open.
        """
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client(
            _intervened(
                {"guardrail": {"outputAssessment": {"0": {"futurePolicy": {"widgets": [{"action": "BLOCKED"}]}}}}}
            )
        )

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.UNKNOWN

    async def test_no_intervention_is_none(self):
        from coa_serve.clients.base import GuardrailOutcome

        client = _converse_client({"output": {"message": {"content": [{"text": "ok"}]}}, "stopReason": "end_turn"})

        result = await client.converse("test", guardrail_id="gr-1")

        assert result.outcome is GuardrailOutcome.NONE
        assert result.guardrail_blocked is False
        assert result.text == "ok"

    async def test_content_filtered_raises_typed_error(self):
        from coa_serve.clients.base import ContentFilteredError

        client = _converse_client({"output": {"message": {"content": []}}, "stopReason": "content_filtered"})

        with pytest.raises(ContentFilteredError) as exc:
            await client.converse("test", guardrail_id="gr-1")

        assert exc.value.stop_reason == "content_filtered"
        # Still a ValueError, so anything catching the previous bare error keeps working.
        assert isinstance(exc.value, ValueError)

    async def test_content_filtered_is_not_reported_as_allow(self):
        from coa_serve.clients.base import ContentFilteredError

        client = _converse_client({"output": {"message": {"content": []}}, "stopReason": "content_filtered"})

        with (
            patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit,
            pytest.raises(ContentFilteredError),
        ):
            await client.converse("test", guardrail_id="gr-1")

        kwargs = emit.call_args.kwargs
        assert kwargs["decision"] == "MODEL_FILTERED"
        assert kwargs["blocked"] is False


@pytest.mark.unit
class TestGuardrailDecisionLabels:
    """Each outcome reports its own decision, and only a real block counts as one."""

    async def _decision_for(self, response: dict) -> dict:
        client = _converse_client(response)
        with patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit:
            await client.converse("test", guardrail_id="gr-1")
        return emit.call_args.kwargs

    async def test_anonymized_reports_anonymized_and_not_blocked(self):
        kwargs = await self._decision_for(
            _intervened(_trace("inputAssessment", "sensitiveInformationPolicy", "piiEntities", _ANON_NAME))
        )

        assert kwargs["decision"] == "ANONYMIZED"
        assert kwargs["blocked"] is False

    async def test_unknown_reports_unknown_and_not_blocked(self):
        # GuardrailBlocked must stay a count of CONFIRMED blocks; an unexplained
        # suppression gets its own decision so it can be alarmed on separately.
        kwargs = await self._decision_for(_intervened(None))

        assert kwargs["decision"] == "UNKNOWN"
        assert kwargs["blocked"] is False

    async def test_blocked_reports_block(self):
        kwargs = await self._decision_for(
            _intervened(_trace("outputAssessment", "contentPolicy", "filters", _BLOCK_VIOLENCE))
        )

        assert kwargs["decision"] == "BLOCK"
        assert kwargs["blocked"] is True

    async def test_no_intervention_reports_allow(self):
        kwargs = await self._decision_for(
            {"output": {"message": {"content": [{"text": "ok"}]}}, "stopReason": "end_turn"}
        )

        assert kwargs["decision"] == "ALLOW"
        assert kwargs["blocked"] is False


@pytest.mark.unit
class TestGuardrailTraceLogRedaction:
    """The trace carries raw matched PII, so it must never be logged verbatim."""

    async def test_log_omits_matched_values_and_raw_trace(self, capsys):
        client = _converse_client(
            _intervened(_trace("inputAssessment", "sensitiveInformationPolicy", "piiEntities", _ANON_NAME))
        )

        await client.converse("test", guardrail_id="gr-1")

        out = capsys.readouterr().out
        assert "guardrail_trace" in out
        # The matched value is a real person's name; it must not reach the log.
        assert "Fidel Castro" not in out
        # Derived, non-sensitive labels are what we keep.
        assert "NAME=ANONYMIZED" in out


@pytest.mark.unit
class TestConverseResultCompatibility:
    """Existing callers construct and read ``guardrail_blocked`` directly."""

    def test_legacy_blocked_kwarg_derives_outcome(self):
        from coa_serve.clients.base import ConverseResult, GuardrailOutcome

        result = ConverseResult(text="x", guardrail_blocked=True)

        assert result.outcome is GuardrailOutcome.BLOCKED
        assert result.guardrail_blocked is True

    def test_legacy_default_is_none_outcome(self):
        from coa_serve.clients.base import ConverseResult, GuardrailOutcome

        result = ConverseResult(text="x")

        assert result.outcome is GuardrailOutcome.NONE
        assert result.guardrail_blocked is False

    def test_outcome_drives_blocked_flag(self):
        from coa_serve.clients.base import ConverseResult, GuardrailOutcome

        assert ConverseResult(text="x", outcome=GuardrailOutcome.ANONYMIZED).guardrail_blocked is False
        assert ConverseResult(text="x", outcome=GuardrailOutcome.BLOCKED).guardrail_blocked is True
        # UNKNOWN is suppressed like a block even though it is not a confirmed one.
        assert ConverseResult(text="x", outcome=GuardrailOutcome.UNKNOWN).guardrail_blocked is True


def _stream_client(events: list[dict]):
    from coa_serve.clients.bedrock import BedrockLLMClient

    mock_client = MagicMock()
    mock_client.converse_stream.return_value = {"stream": iter(events)}
    client = BedrockLLMClient(model_id="test-model", region="us-east-1")
    client._client = mock_client
    return client


@pytest.mark.unit
class TestStreamGuardrailOutcome:
    """Streaming shares the classifier, and now reports and raises consistently."""

    async def test_stream_requests_the_trace(self):
        client = _stream_client([{"contentBlockDelta": {"delta": {"text": "hi"}}}])

        async for _ in client.converse_stream("test", guardrail_id="gr-1"):
            pass

        cfg = client._client.converse_stream.call_args.kwargs["guardrailConfig"]
        assert cfg["trace"] == "enabled"

    async def test_traceless_intervention_now_raises(self):
        """Previously this raised NOTHING, so a genuine block passed through.

        The old code required both ``guardrail_intervened`` AND a trace-derived
        block; since the trace was never requested, the second condition was never
        met and real blocks were silently allowed.
        """
        from coa_serve.clients.base import GuardrailBlockedError

        client = _stream_client(
            [
                {"contentBlockDelta": {"delta": {"text": "partial"}}},
                {"messageStop": {"stopReason": "guardrail_intervened"}},
            ]
        )

        with pytest.raises(GuardrailBlockedError, match="unknown"):
            async for _ in client.converse_stream("test", guardrail_id="gr-1"):
                pass

    async def test_anonymized_stream_does_not_raise(self):
        client = _stream_client(
            [
                {"contentBlockDelta": {"delta": {"text": "Eight presidents under {NAME}"}}},
                {"messageStop": {"stopReason": "guardrail_intervened"}},
                {
                    "metadata": {
                        "trace": {
                            "guardrail": {
                                "inputAssessment": {"0": {"sensitiveInformationPolicy": {"piiEntities": _ANON_NAME}}}
                            }
                        }
                    }
                },
            ]
        )

        chunks = [c async for c in client.converse_stream("test", guardrail_id="gr-1")]

        assert "".join(chunks) == "Eight presidents under {NAME}"

    async def test_stream_emits_one_decision_metric(self):
        # This path emitted no guardrail metrics at all before, so a streamed block
        # was invisible on the dashboard whose whole purpose is counting blocks.
        client = _stream_client(
            [
                {"contentBlockDelta": {"delta": {"text": "x"}}},
                {"messageStop": {"stopReason": "guardrail_intervened"}},
                {
                    "metadata": {
                        "trace": {
                            "guardrail": {"outputAssessment": {"0": {"contentPolicy": {"filters": _BLOCK_VIOLENCE}}}}
                        }
                    }
                },
            ]
        )

        from coa_serve.clients.base import GuardrailBlockedError

        with (
            patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit,
            pytest.raises(GuardrailBlockedError),
        ):
            async for _ in client.converse_stream("test", guardrail_id="gr-1"):
                pass

        assert emit.call_count == 1
        kwargs = emit.call_args.kwargs
        assert kwargs["decision"] == "BLOCK"
        assert kwargs["blocked"] is True

    async def test_stream_unknown_reports_unknown_not_block(self):
        from coa_serve.clients.base import GuardrailBlockedError

        client = _stream_client(
            [
                {"contentBlockDelta": {"delta": {"text": "x"}}},
                {"messageStop": {"stopReason": "guardrail_intervened"}},
            ]
        )

        with (
            patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit,
            pytest.raises(GuardrailBlockedError),
        ):
            async for _ in client.converse_stream("test", guardrail_id="gr-1"):
                pass

        kwargs = emit.call_args.kwargs
        assert kwargs["decision"] == "UNKNOWN"
        assert kwargs["blocked"] is False

    async def test_unguarded_stream_emits_no_metric(self):
        client = _stream_client([{"contentBlockDelta": {"delta": {"text": "x"}}}])

        with patch("coa_serve.clients.bedrock.emit_guardrail_decision") as emit:
            async for _ in client.converse_stream("test"):
                pass

        emit.assert_not_called()

    async def test_tokens_before_a_block_are_still_delivered(self):
        """Pins the KNOWN LIMITATION rather than pretending it does not exist.

        Tokens are yielded as they arrive, so by the time the guardrail outcome is
        known some content has already reached the caller and cannot be recalled.
        Pre-emptive suppression would require buffering and giving up streaming.
        Asserted so that changing it is a deliberate decision, not an accident.
        """
        from coa_serve.clients.base import GuardrailBlockedError

        client = _stream_client(
            [
                {"contentBlockDelta": {"delta": {"text": "leaked "}}},
                {"contentBlockDelta": {"delta": {"text": "prefix"}}},
                {"messageStop": {"stopReason": "guardrail_intervened"}},
                {
                    "metadata": {
                        "trace": {
                            "guardrail": {"outputAssessment": {"0": {"contentPolicy": {"filters": _BLOCK_VIOLENCE}}}}
                        }
                    }
                },
            ]
        )

        received: list[str] = []
        with pytest.raises(GuardrailBlockedError):
            async for chunk in client.converse_stream("test", guardrail_id="gr-1"):
                received.append(chunk)

        assert "".join(received) == "leaked prefix"


@pytest.mark.unit
class TestConverseTopPAndTemperatureObservability:
    """Step 0 (fashion-findings B1): a top_p determinism knob that survives a
    temperature drop, plus an observable signal when temperature is dropped so
    the drop stops being invisible."""

    def _mock_client(self):
        mc = MagicMock()
        mc.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        return mc

    async def test_top_p_threaded_into_inference_config(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mc = self._mock_client()
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mc
        await client.converse("test", temperature=0, top_p=0)

        cfg = mc.converse.call_args[1]["inferenceConfig"]
        assert cfg["topP"] == 0
        assert cfg["temperature"] == 0

    async def test_top_p_omitted_when_none(self):
        from coa_serve.clients.bedrock import BedrockLLMClient

        mc = self._mock_client()
        client = BedrockLLMClient(model_id="test-model", region="us-east-1")
        client._client = mc
        await client.converse("test")  # neither set

        cfg = mc.converse.call_args[1]["inferenceConfig"]
        assert "topP" not in cfg

    async def test_top_p_survives_temperature_drop(self):
        """On a temperature-rejecting model, temperature is popped but top_p
        remains — so a determinism-sensitive caller keeps a sampling constraint."""
        from botocore.exceptions import ClientError
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("drop-temp-model")
        mc = MagicMock()
        err = {"Error": {"Code": "ValidationException", "Message": "temperature is not supported"}}
        mc.converse.side_effect = [
            ClientError(err, "Converse"),
            {"output": {"message": {"content": [{"text": "ok"}]}}},
        ]
        client = BedrockLLMClient(model_id="drop-temp-model", region="us-east-1")
        client._client = mc
        result = await client.converse("test", temperature=0, top_p=0)

        assert result.text == "ok"
        retry_cfg = mc.converse.call_args_list[1][1]["inferenceConfig"]
        assert "temperature" not in retry_cfg  # dropped
        assert retry_cfg["topP"] == 0  # but top_p survives

    async def test_temperature_drop_is_logged_on_learned_path(self):
        """Once a model is known to reject temperature, every subsequent call
        drops it on the pre-learned path — and that drop must be observable
        (emits temperature_dropped_for_model at WARNING), not silent."""
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TEMPERATURE.add("learned-model")
        try:
            mc = self._mock_client()
            client = BedrockLLMClient(model_id="learned-model", region="us-east-1")
            client._client = mc
            with patch.object(bedrock_mod.logger, "warning") as mock_log:
                await client.converse("test", temperature=0, top_p=0)
            events = [c.args[0] for c in mock_log.call_args_list if c.args]
            assert "temperature_dropped_for_model" in events
            # temperature was actually dropped; top_p preserved
            cfg = mc.converse.call_args[1]["inferenceConfig"]
            assert "temperature" not in cfg
            assert cfg["topP"] == 0
        finally:
            bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("learned-model")

    async def test_top_p_dropped_on_top_p_validation_exception(self):
        """REGRESSION (live-found): claude-sonnet-5 deprecated top_p as well as
        temperature. Sending topP=0 for determinism throws ValidationException
        ('`top_p` is deprecated for this model'); the fallback must drop topP and
        retry, or the whole NL->SPARQL path fails closed on that model."""
        from botocore.exceptions import ClientError
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TOP_P.discard("drop-topp-model")
        mc = MagicMock()
        err = {"Error": {"Code": "ValidationException", "Message": "`top_p` is deprecated for this model."}}
        mc.converse.side_effect = [
            ClientError(err, "Converse"),
            {"output": {"message": {"content": [{"text": "ok"}]}}},
        ]
        client = BedrockLLMClient(model_id="drop-topp-model", region="us-east-1")
        client._client = mc
        result = await client.converse("test", top_p=0)

        assert result.text == "ok"
        retry_cfg = mc.converse.call_args_list[1][1]["inferenceConfig"]
        assert "topP" not in retry_cfg  # dropped on retry
        assert "drop-topp-model" in bedrock_mod._MODELS_REJECTING_TOP_P  # learned
        bedrock_mod._MODELS_REJECTING_TOP_P.discard("drop-topp-model")

    async def test_both_temperature_and_top_p_dropped_same_call(self):
        """A model that rejects BOTH params must shed both within one call: first
        ValidationException names temperature, second names top_p, third succeeds."""
        from botocore.exceptions import ClientError
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("drop-both-model")
        bedrock_mod._MODELS_REJECTING_TOP_P.discard("drop-both-model")
        mc = MagicMock()
        terr = {"Error": {"Code": "ValidationException", "Message": "`temperature` is deprecated for this model"}}
        perr = {"Error": {"Code": "ValidationException", "Message": "`top_p` is deprecated for this model."}}
        mc.converse.side_effect = [
            ClientError(terr, "Converse"),
            ClientError(perr, "Converse"),
            {"output": {"message": {"content": [{"text": "ok"}]}}},
        ]
        client = BedrockLLMClient(model_id="drop-both-model", region="us-east-1")
        client._client = mc
        result = await client.converse("test", temperature=0, top_p=0)

        assert result.text == "ok"
        assert mc.converse.call_count == 3
        final_cfg = mc.converse.call_args_list[2][1]["inferenceConfig"]
        assert "temperature" not in final_cfg
        assert "topP" not in final_cfg
        bedrock_mod._MODELS_REJECTING_TEMPERATURE.discard("drop-both-model")
        bedrock_mod._MODELS_REJECTING_TOP_P.discard("drop-both-model")

    async def test_top_p_dropped_on_learned_path(self):
        """Once top_p rejection is learned, subsequent calls pre-strip topP up
        front (no doomed request) and emit top_p_dropped_for_model at WARNING."""
        from coa_serve.clients import bedrock as bedrock_mod
        from coa_serve.clients.bedrock import BedrockLLMClient

        bedrock_mod._MODELS_REJECTING_TOP_P.add("learned-topp-model")
        try:
            mc = self._mock_client()
            client = BedrockLLMClient(model_id="learned-topp-model", region="us-east-1")
            client._client = mc
            with patch.object(bedrock_mod.logger, "warning") as mock_log:
                await client.converse("test", top_p=0)
            events = [c.args[0] for c in mock_log.call_args_list if c.args]
            assert "top_p_dropped_for_model" in events
            assert mc.converse.call_count == 1  # no doomed first request
            cfg = mc.converse.call_args[1]["inferenceConfig"]
            assert "topP" not in cfg
        finally:
            bedrock_mod._MODELS_REJECTING_TOP_P.discard("learned-topp-model")
