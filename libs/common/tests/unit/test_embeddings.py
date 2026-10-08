# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared BedrockEmbedder.

Cover the two provider wire formats and the Cohere document/query asymmetry —
the correctness the whole "one embedding model everywhere" change hinges on —
plus the #784 item #2 batch path: v4-gated ≤96-text batching, subdivide-on-
timeout, per-text sanitisation, and the retry↔subdivide layering.
"""

from __future__ import annotations

import json
import multiprocessing
import pickle
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError
from coa_common import embeddings
from coa_common.embeddings import BedrockEmbedder, make_llama_index_embedding
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit


def _mock_bedrock(response: dict) -> MagicMock:
    client = MagicMock()
    client.invoke_model.return_value = {"body": MagicMock(read=MagicMock(return_value=json.dumps(response).encode()))}
    return client


def _sent_body(client: MagicMock) -> dict:
    return json.loads(client.invoke_model.call_args.kwargs["body"])


def _mock_bedrock_batch_echo(f=lambda t: [float(len(t))]) -> MagicMock:
    """side_effect echo: one vector per input text, in order (f injective over the test's inputs)."""
    client = MagicMock()

    def _echo(*, modelId, body):  # noqa: N803 (boto3 kwarg name)
        texts = json.loads(body)["texts"]
        payload = json.dumps({"embeddings": {"float": [f(t) for t in texts]}}).encode()
        return {"body": MagicMock(read=MagicMock(return_value=payload))}

    client.invoke_model.side_effect = _echo
    return client


def _sent_bodies(client: MagicMock) -> list[dict]:
    """Every request body in call order (chunk-boundary assertions)."""
    return [json.loads(c.kwargs["body"]) for c in client.invoke_model.call_args_list]


class TestCohere:
    def _embedder(self, client: MagicMock) -> BedrockEmbedder:
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)
        e._client = client
        return e

    def test_embed_document_uses_search_document(self):
        client = _mock_bedrock({"embeddings": {"float": [[0.1] * 1024]}})
        vec = self._embedder(client).embed_document("a claim doc")
        body = _sent_body(client)
        assert body["input_type"] == "search_document"
        assert body["texts"] == ["a claim doc"]
        assert body["output_dimension"] == 1024
        assert body["embedding_types"] == ["float"]
        assert len(vec) == 1024

    def test_embed_query_uses_search_query(self):
        client = _mock_bedrock({"embeddings": {"float": [[0.2] * 1024]}})
        self._embedder(client).embed_query("how many claims?")
        assert _sent_body(client)["input_type"] == "search_query"

    def test_parses_nested_float_response(self):
        client = _mock_bedrock({"embeddings": {"float": [[1.0, 2.0, 3.0]]}})
        assert self._embedder(client).embed_document("x") == [1.0, 2.0, 3.0]

    def test_raises_when_no_embedding(self):
        client = _mock_bedrock({"embeddings": {}})
        with pytest.raises(ValueError, match="Cohere"):
            self._embedder(client).embed_document("x")

    def test_inference_profile_id_detected_as_cohere(self):
        # The "us." inference-profile prefix must still route to the Cohere schema.
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0")
        client = _mock_bedrock({"embeddings": {"float": [[0.0] * 8]}})
        e._client = client
        e.embed_document("x")
        assert "texts" in _sent_body(client)


class TestTitan:
    def _embedder(self, client: MagicMock) -> BedrockEmbedder:
        e = BedrockEmbedder(model_id="amazon.titan-embed-text-v2:0", dimensions=1024)
        e._client = client
        return e

    def test_titan_request_and_response_schema(self):
        client = _mock_bedrock({"embedding": [0.5] * 1024})
        vec = self._embedder(client).embed_document("doc")
        body = _sent_body(client)
        assert body["inputText"] == "doc"
        assert body["dimensions"] == 1024
        assert body["normalize"] is True
        assert "input_type" not in body  # Titan is symmetric
        assert len(vec) == 1024

    def test_query_and_document_identical_for_titan(self):
        client = _mock_bedrock({"embedding": [0.5] * 4})
        e = self._embedder(client)
        e.embed_query("q")
        assert "inputText" in _sent_body(client)  # no query/document distinction

    def test_raises_when_no_embedding(self):
        client = _mock_bedrock({"nope": []})
        with pytest.raises(ValueError, match="Titan"):
            self._embedder(client).embed_document("x")


class TestIsCohereV4:
    @pytest.mark.parametrize(
        ("model_id", "expected"),
        [
            ("cohere.embed-v4:0", True),
            ("us.cohere.embed-v4:0", True),
            ("global.cohere.embed-v4:0", True),
            ("cohere.embed-english-v3", False),
            ("amazon.titan-embed-text-v2:0", False),
        ],
    )
    def test_v4_gate(self, model_id, expected):
        assert embeddings._is_cohere_v4(model_id) is expected


class TestBatch:
    def _embedder(self, client: MagicMock) -> BedrockEmbedder:
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)
        e._client = client
        return e

    def test_embed_documents_preserves_order(self):
        # Rewritten from the old single-text mock (invalidated by §3.2): v4 now
        # sends ONE InvokeModel with a populated texts[]. call_count==1 also
        # fails this if batching silently regresses to fan-out.
        client = _mock_bedrock_batch_echo(f=lambda t: [float(len(t))])
        out = self._embedder(client).embed_documents(["a", "bbb", "cc"])
        assert client.invoke_model.call_count == 1
        assert out == [[1.0], [3.0], [2.0]]
        assert _sent_bodies(client)[0]["texts"] == ["a", "bbb", "cc"]

    def test_batch_of_96_single_call(self):
        texts = [f"doc-{i}" for i in range(96)]
        client = _mock_bedrock_batch_echo(f=lambda t: [float(t.split("-")[1])])
        out = self._embedder(client).embed_documents(texts)
        assert client.invoke_model.call_count == 1
        assert out == [[float(i)] for i in range(96)]
        assert _sent_bodies(client)[0]["texts"] == texts

    def test_97_texts_split_into_two_chunks(self):
        texts = [f"doc-{i}" for i in range(97)]
        client = _mock_bedrock_batch_echo(f=lambda t: [float(t.split("-")[1])])
        out = self._embedder(client).embed_documents(texts)
        assert client.invoke_model.call_count == 2  # ceil(97/96)
        # The two chunks run concurrently on the thread pool, so call_args_list
        # order is nondeterministic — assert the multiset of chunk sizes, not
        # per-index. Result order is the real contract and is checked below.
        assert sorted(len(b["texts"]) for b in _sent_bodies(client)) == [1, 96]
        assert out == [[float(i)] for i in range(97)]  # order preserved across the boundary

    def test_duplicate_texts_not_deduped(self):
        client = _mock_bedrock_batch_echo()
        out = self._embedder(client).embed_documents(["dup", "dup", "dup"])
        assert client.invoke_model.call_count == 1
        assert len(out) == 3  # no dedup
        assert out == [[3.0], [3.0], [3.0]]

    def test_empty_and_whitespace_send_space_sentinel(self):
        client = _mock_bedrock_batch_echo()
        out = self._embedder(client).embed_documents(["a", "", "  ", "b"])
        assert len(out) == 4
        # Empty/whitespace slots send " " (not "", not raw whitespace).
        assert _sent_bodies(client)[0]["texts"] == ["a", " ", " ", "b"]

    def test_embed_document_empty_sends_space_sentinel(self):
        # Single-text path routes through _prep too — parity with the batch path.
        client = _mock_bedrock({"embeddings": {"float": [[0.0] * 4]}})
        self._embedder(client).embed_document("")
        assert _sent_body(client)["texts"] == [" "]


class TestParseBatch:
    def _embedder(self) -> BedrockEmbedder:
        return BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)

    def test_parse_batch_returns_all_vectors(self):
        e = self._embedder()
        out = e._parse_batch({"embeddings": {"float": [[1.0, 2.0], [3.0, 4.0]]}}, 2)
        assert out == [[1.0, 2.0], [3.0, 4.0]]

    def test_parse_response_still_returns_single_vector(self):
        # Unchanged single-text parser (must NOT be mutated by the batch path).
        e = self._embedder()
        assert e._parse_response({"embeddings": {"float": [[9.0, 9.0]]}}) == [9.0, 9.0]

    def test_parse_batch_vector_count_mismatch_raises(self):
        # 3 inputs but a 2-vector response → loud ValueError naming both counts,
        # and it must NOT subdivide (ValueError ∉ _SUBDIVIDE_ON).
        client = MagicMock()
        payload = json.dumps({"embeddings": {"float": [[0.1], [0.2]]}}).encode()
        client.invoke_model.return_value = {"body": MagicMock(read=MagicMock(return_value=payload))}
        e = self._embedder()
        e._client = client
        with pytest.raises(ValueError, match=r"2 vectors for 3 inputs"):
            e.embed_documents(["a", "b", "c"])
        assert client.invoke_model.call_count == 1


class TestFanOutFallback:
    def test_embed_documents_titan_fans_out_per_text_no_batch_body(self):
        client = MagicMock()

        def _echo(*, modelId, body):  # noqa: N803
            text = json.loads(body)["inputText"]
            payload = json.dumps({"embedding": [float(len(text))]}).encode()
            return {"body": MagicMock(read=MagicMock(return_value=payload))}

        client.invoke_model.side_effect = _echo
        e = BedrockEmbedder(model_id="amazon.titan-embed-text-v2:0", dimensions=4)
        e._client = client
        out = e.embed_documents(["a", "bb", "ccc"])
        assert client.invoke_model.call_count == 3  # per-text, NOT 1
        for body in _sent_bodies(client):
            assert "inputText" in body
            assert "texts" not in body
        assert out == [[1.0], [2.0], [3.0]]  # order preserved

    def test_embed_documents_cohere_v3_fans_out_per_text(self):
        # Non-v4 Cohere: _is_cohere_v4 is False → per-text fan-out (Cohere body).
        client = _mock_bedrock_batch_echo()  # single-text bodies still have texts[]
        e = BedrockEmbedder(model_id="cohere.embed-english-v3", dimensions=4)
        e._client = client
        out = e.embed_documents(["a", "bb", "ccc"])
        assert client.invoke_model.call_count == 3
        assert out == [[1.0], [2.0], [3.0]]


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModel")


class TestSubdivideOnTimeout:
    def _embedder(self, client: MagicMock) -> BedrockEmbedder:
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)
        e._client = client
        return e

    def test_model_timeout_subdivides_and_returns_all_vectors(self):
        client = MagicMock()

        def _echo(*, modelId, body):  # noqa: N803
            texts = json.loads(body)["texts"]
            if len(texts) > 2:  # timeout the big chunk, echo the halves
                raise _client_error("ModelTimeoutException")
            payload = json.dumps({"embeddings": {"float": [[float(t.split("-")[1])] for t in texts]}}).encode()
            return {"body": MagicMock(read=MagicMock(return_value=payload))}

        client.invoke_model.side_effect = _echo
        out = self._embedder(client).embed_documents([f"doc-{i}" for i in range(4)])
        assert out == [[0.0], [1.0], [2.0], [3.0]]
        # [4]→timeout→[2]+[2], each echoes → 1 + 2 = 3 invoke calls.
        assert client.invoke_model.call_count == 3

    def test_size_one_timeout_raises(self):
        client = MagicMock()
        client.invoke_model.side_effect = _client_error("ModelTimeoutException")
        with pytest.raises(ClientError):
            self._embedder(client).embed_documents(["only"])
        # A size-1 leaf IS app-retried (not subdivided), so the leaf is invoked
        # _EMBED_MAX_RETRIES + 1 times before re-raising.
        assert client.invoke_model.call_count == embeddings._EMBED_MAX_RETRIES + 1

    def test_read_timeout_subdivides_but_not_app_retried(self):
        # ReadTimeoutError is a BotoCoreError: _invoke's `except ClientError`
        # misses it (no double-retry stacking, §3.4.1), so a size-1 chunk that
        # read-times-out is invoked EXACTLY ONCE, then re-raised.
        client = MagicMock()
        client.invoke_model.side_effect = ReadTimeoutError(endpoint_url="https://bedrock")
        with pytest.raises(ReadTimeoutError):
            self._embedder(client).embed_documents(["only"])
        assert client.invoke_model.call_count == 1

    def test_is_retryable_false_for_read_timeout_no_attribute_error(self):
        # Guards .response on a BotoCoreError (no AttributeError).
        assert embeddings._is_retryable(ReadTimeoutError(endpoint_url="https://x")) is False
        assert embeddings._is_retryable(ConnectTimeoutError(endpoint_url="https://x")) is False

    @pytest.mark.parametrize("exc_cls", [ReadTimeoutError, ConnectTimeoutError])
    def test_embed_chunk_persistent_transport_timeout_call_count_bounded(self, monkeypatch, exc_cls):
        # A 96-chunk that transport-times-out at every size recurses no deeper
        # than ⌈log₂96⌉=7 (left-first short-circuit), and raising the app-retry
        # cap does NOT change the count — proving Read/ConnectTimeout stay out of
        # the retry layer entirely. Both branches of _SUBDIVIDE_ON are exercised.
        monkeypatch.setattr(embeddings, "_EMBED_MAX_RETRIES", 20)
        client = MagicMock()
        client.invoke_model.side_effect = exc_cls(endpoint_url="https://bedrock")
        with pytest.raises(exc_cls):
            self._embedder(client).embed_documents([f"doc-{i}" for i in range(96)])
        # left spine: 96,48,24,12,6,3,1 = 7 nodes
        assert client.invoke_model.call_count == 7


class TestValidationIsolation:
    def _embedder(self, client: MagicMock) -> BedrockEmbedder:
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)
        e._client = client
        return e

    def test_bad_text_isolated_and_raises_named_error(self):
        client = MagicMock()

        def _echo(*, modelId, body):  # noqa: N803
            texts = json.loads(body)["texts"]
            if "bad" in texts:  # atomic 400 over the whole chunk
                raise _client_error("ValidationException")
            payload = json.dumps({"embeddings": {"float": [[float(len(t))] for t in texts]}}).encode()
            return {"body": MagicMock(read=MagicMock(return_value=payload))}

        client.invoke_model.side_effect = _echo
        with pytest.raises(ValueError, match="bad"):
            self._embedder(client).embed_documents(["g0", "g1", "g2", "bad"])
        bodies = _sent_bodies(client)
        # The bad text was isolated to its own size-1 chunk.
        assert ["bad"] in [b["texts"] for b in bodies]
        # Healthy texts were still embedded (isolation, not whole-batch abort).
        assert ["g0", "g1"] in [b["texts"] for b in bodies]

    def test_size_one_validation_propagates_as_value_error(self):
        client = MagicMock()
        client.invoke_model.side_effect = _client_error("ValidationException")
        with pytest.raises(ValueError):
            self._embedder(client).embed_documents(["bad"])

    @pytest.mark.parametrize("code", ["ThrottlingException", "AccessDeniedException"])
    def test_throttle_and_access_denied_propagate_without_subdivide(self, code):
        client = MagicMock()
        client.invoke_model.side_effect = _client_error(code)
        with pytest.raises(ClientError):
            self._embedder(client).embed_documents(["a", "b", "c", "d"])
        assert client.invoke_model.call_count == 1  # no subdivide


class TestBatchSizeConfig:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            (None, 96),
            ("96", 96),
            ("1", 1),
            ("200", 96),
            ("0", 1),
            ("-5", 1),
            ("abc", 96),
        ],
    )
    def test_env_batch_size_clamps(self, monkeypatch, env, expected):
        if env is None:
            monkeypatch.delenv("BEDROCK_EMBED_BATCH_SIZE", raising=False)
        else:
            monkeypatch.setenv("BEDROCK_EMBED_BATCH_SIZE", env)
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0")
        assert e._batch_size == expected

    def test_kill_switch_one_call_per_text(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_EMBED_BATCH_SIZE", "1")
        client = _mock_bedrock_batch_echo()
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0")
        e._client = client
        out = e.embed_documents([f"t{i}" for i in range(100)])
        assert client.invoke_model.call_count == 100  # restores pre-fix one-call-per-text
        assert len(out) == 100


class TestReadTimeoutConfig:
    def test_default_read_timeout(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_EMBED_READ_TIMEOUT", raising=False)
        assert BedrockEmbedder(model_id="us.cohere.embed-v4:0")._read_timeout == 60

    def test_env_read_timeout(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_EMBED_READ_TIMEOUT", "120")
        assert BedrockEmbedder(model_id="us.cohere.embed-v4:0")._read_timeout == 120

    def test_explicit_read_timeout_overrides_env(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_EMBED_READ_TIMEOUT", "120")
        assert BedrockEmbedder(model_id="us.cohere.embed-v4:0", read_timeout=30)._read_timeout == 30


class TestLlamaIndexAdapter:
    """The graphrag adapter must route text→search_document, query→search_query
    and request the configured dimension (so Cohere v4 returns 1024, not 1536)."""

    def test_text_and_query_route_to_correct_input_type(self):
        # Build our own embedder+client so we can inspect the request body.
        embedder = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)
        client = _mock_bedrock({"embeddings": {"float": [[0.0] * 1024]}})
        embedder._client = client

        from llama_index.core.base.embeddings.base import BaseEmbedding

        class _Adapter(BaseEmbedding):
            def _get_query_embedding(self, query: str) -> list[float]:
                return embedder.embed_query(query)

            async def _aget_query_embedding(self, query: str) -> list[float]:
                return embedder.embed_query(query)

            def _get_text_embedding(self, text: str) -> list[float]:
                return embedder.embed_document(text)

            async def _aget_text_embedding(self, text: str) -> list[float]:
                return embedder.embed_document(text)

        adapter = _Adapter(model_name="us.cohere.embed-v4:0", embed_batch_size=10)

        adapter._get_text_embedding("doc")
        assert _sent_body(client)["input_type"] == "search_document"
        assert _sent_body(client)["output_dimension"] == 1024

        adapter._get_query_embedding("q")
        assert _sent_body(client)["input_type"] == "search_query"

    def test_adapter_is_picklable(self):
        # graphrag's build pipeline pickles embed_model across a
        # ProcessPoolExecutor; a closure/local class silently breaks ingestion.
        import pickle

        adapter = make_llama_index_embedding(model_id="us.cohere.embed-v4:0", dimensions=1024)
        restored = pickle.loads(pickle.dumps(adapter))
        assert restored.model_name == "us.cohere.embed-v4:0"
        assert restored.embed_dimensions == 1024

    def test_factory_returns_base_embedding_instance(self):
        # to_embedding_model returns BaseEmbedding instances unchanged, so the
        # factory must return an isinstance of it.
        from llama_index.core.base.embeddings.base import BaseEmbedding

        adapter = make_llama_index_embedding(model_id="us.cohere.embed-v4:0", dimensions=1024)
        assert isinstance(adapter, BaseEmbedding)
        assert adapter.model_name == "us.cohere.embed-v4:0"


class TestLlamaIndexAdapterAcrossProcesses:
    """graphrag's KG build unpickles the adapter in workers started with the
    ``spawn`` method. Those workers import ``coa_common.embeddings`` fresh and
    never call the factory, so the class must resolve by name on its own."""

    def test_module_getattr_fresh_interpreter_resolves_adapter_class(self):
        code = (
            "import coa_common.embeddings as m\n"
            "cls = m.BedrockEmbedderLlamaIndex\n"
            "assert cls.__module__ == 'coa_common.embeddings', cls.__module__\n"
            "print(cls.__qualname__)\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "BedrockEmbedderLlamaIndex"

    @pytest.mark.parametrize("start_method", ["spawn", "forkserver"])
    def test_adapter_unpickles_in_fresh_worker_process(self, start_method):
        if start_method not in multiprocessing.get_all_start_methods():
            pytest.skip(f"{start_method} start method not available on this platform")
        adapter = make_llama_index_embedding(model_id="us.cohere.embed-v4:0", dimensions=1024)
        payload = pickle.dumps(adapter)

        # The child runs pickle.loads on the payload (the same step a graphrag
        # worker runs) and sends the restored instance back.
        ctx = multiprocessing.get_context(start_method)
        with ctx.Pool(1) as pool:
            restored = pool.apply(pickle.loads, (payload,))

        assert type(restored).__qualname__ == "BedrockEmbedderLlamaIndex"
        assert restored.embed_model_id == "us.cohere.embed-v4:0"
        assert restored.embed_dimensions == 1024

    def test_module_getattr_returns_cached_class(self):
        assert embeddings.BedrockEmbedderLlamaIndex is embeddings.BedrockEmbedderLlamaIndex
        adapter = make_llama_index_embedding(model_id="us.cohere.embed-v4:0", dimensions=1024)
        assert type(adapter) is embeddings.BedrockEmbedderLlamaIndex

    def test_module_getattr_unknown_name_raises_attribute_error(self):
        missing = "NoSuchThing"
        with pytest.raises(AttributeError, match="has no attribute 'NoSuchThing'"):
            getattr(embeddings, missing)
        assert not hasattr(embeddings, missing)


class TestCostTracking:
    """The embedder records input-token usage on the shared per-job CostTracker.

    Titan responses carry ``inputTextTokenCount``; Cohere embed-v4 does not, so
    the count is estimated from the (char-capped) input text at ~4 chars/token.
    Embeddings bill on input only, so output tokens are always recorded as 0.
    """

    def test_titan_records_response_token_count(self):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        client = _mock_bedrock({"embedding": [0.5] * 8, "inputTextTokenCount": 42})
        e = BedrockEmbedder(model_id="amazon.titan-embed-text-v2:0", dimensions=8, cost_tracker=tracker)
        e._client = client

        vec = e.embed_document("doc")
        assert len(vec) == 8  # vector still returned unchanged
        assert tracker.input_tokens_by_stage == {"embed": 42}
        assert tracker.output_tokens_by_stage == {"embed": 0}
        # 42 input tokens * $0.02/1M
        assert tracker.total_cost_usd == pytest.approx(42 * 0.02 / 1_000_000)

    def test_cohere_records_char_over_four_estimate(self):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        client = _mock_bedrock({"embeddings": {"float": [[0.1] * 8]}})  # no token count
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=8, cost_tracker=tracker)
        e._client = client

        # 20 chars -> ceil(20/4) == 5 estimated input tokens.
        e.embed_document("x" * 20)
        assert tracker.input_tokens_by_stage == {"embed": 5}
        assert tracker.output_tokens_by_stage == {"embed": 0}

    def test_cohere_estimate_is_at_least_one_token(self):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        client = _mock_bedrock({"embeddings": {"float": [[0.1] * 8]}})
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=8, cost_tracker=tracker)
        e._client = client

        e.embed_document("")  # empty text still counts as 1 token, never 0
        assert tracker.input_tokens_by_stage == {"embed": 1}

    def test_batch_records_per_document(self):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        # v4 embed_documents now sends ONE batch call, so the mock must echo one
        # vector per text (the single-vector _mock_bedrock would trip the
        # _parse_batch count guard). No inputTextTokenCount -> Cohere char-estimate.
        client = _mock_bedrock_batch_echo(f=lambda t: [0.1] * 8)
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=8, cost_tracker=tracker)
        e._client = client

        # Three docs of 8 chars -> ceil(8/4)=2 tokens each -> 6 total.
        e.embed_documents(["abcdefgh", "abcdefgh", "abcdefgh"])
        assert tracker.input_tokens_by_stage == {"embed": 6}

    def test_no_tracker_leaves_behavior_unchanged(self):
        # Without a tracker, nothing is recorded and the vector is returned as-is.
        client = _mock_bedrock({"embeddings": {"float": [[1.0, 2.0, 3.0]]}})
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0")  # no cost_tracker
        e._client = client
        assert e.embed_document("x") == [1.0, 2.0, 3.0]

    def test_set_cost_tracker_attaches_late(self):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        client = _mock_bedrock({"embedding": [0.5] * 8, "inputTextTokenCount": 7})
        e = BedrockEmbedder(model_id="amazon.titan-embed-text-v2:0", dimensions=8)  # built without tracker
        e._client = client

        e.set_cost_tracker(tracker)  # attach per-job tracker after construction
        e.embed_document("doc")
        assert tracker.input_tokens_by_stage == {"embed": 7}

    def test_float_token_count_coerced_to_int(self):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        # A float count (e.g. 12.0) must be honoured, not rejected as non-int.
        client = _mock_bedrock({"embedding": [0.5] * 8, "inputTextTokenCount": 12.0})
        e = BedrockEmbedder(model_id="amazon.titan-embed-text-v2:0", dimensions=8, cost_tracker=tracker)
        e._client = client

        e.embed_document("doc")
        assert tracker.input_tokens_by_stage == {"embed": 12}

    def test_bool_token_count_rejected_falls_to_estimate(self):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        # bool is an int subclass; True must NOT be treated as a token count.
        client = _mock_bedrock({"embedding": [0.5] * 8, "inputTextTokenCount": True})
        e = BedrockEmbedder(model_id="amazon.titan-embed-text-v2:0", dimensions=8, cost_tracker=tracker)
        e._client = client

        e.embed_document("x" * 20)  # falls back to ceil(20/4) == 5
        assert tracker.input_tokens_by_stage == {"embed": 5}


class TestTruncation:
    def test_input_truncated_to_bound(self):
        client = _mock_bedrock({"embeddings": {"float": [[0.0]]}})
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0")
        e._client = client
        e.embed_document("x" * 12000)
        # Bounded to _MAX_INPUT_CHARS (8000) — parity with the prior direct-invoke code.
        assert len(_sent_body(client)["texts"][0]) == 8000


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(embeddings.time, "sleep", lambda *_a, **_k: None)


def _cohere_ok(dims: int = 1024) -> dict:
    payload = json.dumps({"embeddings": {"float": [[0.1] * dims]}}).encode()
    return {"body": MagicMock(read=MagicMock(return_value=payload))}


class TestEmbedRetry:
    """408/ModelTimeoutException is not in any botocore retryable set;
    _embed retries it at the application level, ONLY this code."""

    def _embedder(self, client: MagicMock) -> BedrockEmbedder:
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)
        e._client = client
        return e

    def test_retries_model_timeout_then_succeeds(self):
        client = MagicMock()
        client.invoke_model.side_effect = [
            _client_error("ModelTimeoutException"),
            _client_error("ModelTimeoutException"),
            _cohere_ok(),
        ]
        vec = self._embedder(client).embed_document("x")
        assert client.invoke_model.call_count == 3
        assert len(vec) == 1024

    @pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
    def test_non_retryable_client_error_propagates_immediately(self, code):
        client = MagicMock()
        client.invoke_model.side_effect = _client_error(code)
        with pytest.raises(ClientError):
            self._embedder(client).embed_document("x")
        assert client.invoke_model.call_count == 1

    def test_model_timeout_exhausts_and_reraises(self):
        client = MagicMock()
        client.invoke_model.side_effect = _client_error("ModelTimeoutException")
        with pytest.raises(ClientError):
            self._embedder(client).embed_document("x")
        assert client.invoke_model.call_count == embeddings._EMBED_MAX_RETRIES + 1

    def test_backoff_slept_between_retries(self, monkeypatch):
        mock_sleep = MagicMock()
        monkeypatch.setattr(embeddings.time, "sleep", mock_sleep)
        client = MagicMock()
        client.invoke_model.side_effect = [
            _client_error("ModelTimeoutException"),
            _client_error("ModelTimeoutException"),
            _cohere_ok(),
        ]
        self._embedder(client).embed_document("x")
        assert mock_sleep.call_count == 2


class TestMetricEmission:
    """EMF counters (§3.6) must fire with the right NAME + model_id dimension,
    and input_type where the counter carries it — proves observability wiring,
    not just the happy path."""

    def _embedder(self, client: MagicMock) -> BedrockEmbedder:
        e = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=1024)
        e._client = client
        return e

    def test_emf_counters_fire_with_name_and_dimensions(self, monkeypatch):
        emit = MagicMock()
        monkeypatch.setattr(embeddings, "emit_metric", emit)

        def _names() -> list[str]:
            return [c.args[1] for c in emit.call_args_list]  # emit_metric(namespace, name, value, ...)

        def _kwargs_for(name: str):
            return next(c.kwargs for c in emit.call_args_list if c.args[1] == name)

        # 1) >1-chunk ModelTimeout subdivide → EmbedBatchSizeUsed + EmbedChunkSubdivisions
        def _subdivide(*, modelId, body):  # noqa: N803
            texts = json.loads(body)["texts"]
            if len(texts) > 2:  # timeout the big chunk, echo the halves
                raise _client_error("ModelTimeoutException")
            payload = json.dumps({"embeddings": {"float": [[0.0] for _ in texts]}}).encode()
            return {"body": MagicMock(read=MagicMock(return_value=payload))}

        client = MagicMock()
        client.invoke_model.side_effect = _subdivide
        self._embedder(client).embed_documents([f"doc-{i}" for i in range(4)])
        assert "EmbedBatchSizeUsed" in _names()
        assert "EmbedChunkSubdivisions" in _names()
        assert _kwargs_for("EmbedBatchSizeUsed")["model_id"] == "us.cohere.embed-v4:0"
        assert _kwargs_for("EmbedBatchSizeUsed")["input_type"] == "document"
        assert _kwargs_for("EmbedChunkSubdivisions")["input_type"] == "document"

        # 2) size-1 ReadTimeout → EmbedReadTimeouts
        emit.reset_mock()
        client = MagicMock()
        client.invoke_model.side_effect = ReadTimeoutError(endpoint_url="https://bedrock")
        with pytest.raises(ReadTimeoutError):
            self._embedder(client).embed_documents(["only"])
        assert "EmbedReadTimeouts" in _names()
        assert _kwargs_for("EmbedReadTimeouts")["model_id"] == "us.cohere.embed-v4:0"
        assert _kwargs_for("EmbedReadTimeouts")["input_type"] == "document"

        # 3) single-text ModelTimeout retry via embed_document → EmbedRetries
        emit.reset_mock()
        client = MagicMock()
        client.invoke_model.side_effect = [_client_error("ModelTimeoutException"), _cohere_ok()]
        self._embedder(client).embed_document("x")
        assert "EmbedRetries" in _names()
        assert _kwargs_for("EmbedRetries")["model_id"] == "us.cohere.embed-v4:0"


def test_prep_logs_when_input_exceeds_the_char_cap():
    """A text past the 8000-char cap loses its tail from the vector; the cut is logged."""
    with capture_logs() as logs:
        assert len(embeddings._prep("x" * (embeddings._MAX_INPUT_CHARS + 5))) == embeddings._MAX_INPUT_CHARS
        embeddings._prep("short")
    cuts = [e for e in logs if e["event"] == "embedding_input_truncated"]
    assert len(cuts) == 1
    assert cuts[0]["chars"] == embeddings._MAX_INPUT_CHARS + 5
