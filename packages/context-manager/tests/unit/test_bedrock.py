# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Bedrock LLMClient."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import MagicMock

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
        """guardContent must be delivered to the model even without a
        guardrailConfig — tier2 callers use guardContent as the sole
        channel for untrusted user text, so dropping it in unconfigured
        deployments would silently lose the user's question.
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
            {"guardContent": {"text": {"text": "Question: hello"}}},
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
        from coa_serve.clients.bedrock import BedrockLLMClient

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
