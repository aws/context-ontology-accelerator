# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the thin HTTP/Bedrock service clients."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError
from coa_ontology.inducer.services.embedding_generator import EmbeddingGeneratorClient
from coa_ontology.inducer.services.grounding import _BEDROCK_MAX_POOL_CONNECTIONS
from coa_ontology.inducer.services.llm import LLMClient
from coa_ontology.inducer.services.ontology_catalog import OntologyCatalogClient

pytestmark = pytest.mark.unit


def _mock_httpx_client(response: MagicMock) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value = response
    client.get.return_value = response
    return client


def _resp(payload: object, status_code: int = 200) -> MagicMock:
    r = MagicMock()
    r.json.return_value = payload
    r.status_code = status_code
    r.raise_for_status = MagicMock()
    return r


# ── LLMClient ────────────────────────────────────────────────────────


class TestLLMClient:
    def test_generate_returns_text(self) -> None:
        client = LLMClient(model_id="m", region="us-east-1")
        bedrock = MagicMock()
        bedrock.converse.return_value = {"output": {"message": {"content": [{"text": "hello"}]}}}
        client._client = bedrock
        assert client.generate("prompt", system="sys", max_tokens=10) == "hello"
        # system list is populated when a system prompt is provided.
        assert bedrock.converse.call_args.kwargs["system"] == [{"text": "sys"}]

    def test_generate_empty_system_sends_empty_list(self) -> None:
        client = LLMClient()
        bedrock = MagicMock()
        bedrock.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}
        client._client = bedrock
        client.generate("prompt")
        assert bedrock.converse.call_args.kwargs["system"] == []

    def test_generate_wraps_client_error(self) -> None:
        client = LLMClient(model_id="m")
        bedrock = MagicMock()
        bedrock.converse.side_effect = ClientError({"Error": {"Code": "X", "Message": "y"}}, "Converse")
        client._client = bedrock
        with pytest.raises(RuntimeError, match="Bedrock converse\\(\\) failed"):
            client.generate("prompt")

    def test_generate_wraps_unexpected_response_shape(self) -> None:
        client = LLMClient()
        bedrock = MagicMock()
        bedrock.converse.return_value = {"unexpected": True}
        client._client = bedrock
        with pytest.raises(RuntimeError, match="Unexpected Bedrock response shape"):
            client.generate("prompt")

    def test_client_property_lazily_builds_boto_client(self) -> None:
        client = LLMClient(region="us-west-2")
        captured: dict[str, object] = {}

        def _fake_client(service_name: str, **kwargs: object) -> str:
            captured["service_name"] = service_name
            captured["config"] = kwargs.get("config")
            captured["region_name"] = kwargs.get("region_name")
            return "sentinel"

        with patch("boto3.client", side_effect=_fake_client) as mock_boto:
            assert client.client == "sentinel"
            # Cached — a second access does not re-create the client.
            assert client.client == "sentinel"
        mock_boto.assert_called_once()
        assert captured["service_name"] == "bedrock-runtime"
        assert captured["region_name"] == "us-west-2"
        assert captured["config"] is not None
        # Connection-pool cap shared with the grounding rerank client.
        assert captured["config"].max_pool_connections == _BEDROCK_MAX_POOL_CONNECTIONS
        # Async backoff config with a generous read budget for LLM generation.
        assert captured["config"].retries["max_attempts"] == 3
        assert captured["config"].read_timeout == 120

    def test_generate_records_usage_when_tracker_present(self) -> None:
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        client = LLMClient(model_id="m", cost_tracker=tracker)
        bedrock = MagicMock()
        bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "hello"}]}},
            "usage": {"inputTokens": 11, "outputTokens": 7},
        }
        client._client = bedrock
        assert client.generate("prompt") == "hello"
        # Token usage is recorded under the "generate" stage with the model id.
        assert tracker.input_tokens_by_stage == {"generate": 11}
        assert tracker.output_tokens_by_stage == {"generate": 7}
        # generate does not surface latency (only rerank does).
        assert tracker.rerank_invocations == 0

    def test_generate_without_tracker_is_unchanged(self) -> None:
        # No tracker → identical return value, no error even when usage present.
        client = LLMClient(model_id="m")
        bedrock = MagicMock()
        bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "hi"}]}},
            "usage": {"inputTokens": 3, "outputTokens": 2},
        }
        client._client = bedrock
        assert client.generate("prompt") == "hi"

    def test_generate_records_before_parse_failure(self) -> None:
        # Usage must be captured even when the body shape later fails to parse.
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        client = LLMClient(model_id="m", cost_tracker=tracker)
        bedrock = MagicMock()
        bedrock.converse.return_value = {"usage": {"inputTokens": 9, "outputTokens": 4}}
        client._client = bedrock
        with pytest.raises(RuntimeError, match="Unexpected Bedrock response shape"):
            client.generate("prompt")
        assert tracker.input_tokens_by_stage == {"generate": 9}
        assert tracker.output_tokens_by_stage == {"generate": 4}


# ── EmbeddingGeneratorClient ─────────────────────────────────────────


class TestEmbeddingGeneratorClient:
    def test_generate_lexical_returns_results_and_backend(self) -> None:
        client = EmbeddingGeneratorClient("http://eg:8001")
        resp = _resp({"results": [{"vector": [0.1]}], "backend": "titan"})
        with patch.object(httpx, "Client", return_value=_mock_httpx_client(resp)) as _mc:
            results, backend = client.generate_lexical("ont-1", [{"class_uri": "c"}], backend="titan")
        assert results == [{"vector": [0.1]}]
        assert backend == "titan"

    def test_generate_lexical_defaults_backend(self) -> None:
        client = EmbeddingGeneratorClient("http://eg:8001")
        resp = _resp({"results": []})  # no backend key
        mock_client = _mock_httpx_client(resp)
        with patch.object(httpx, "Client", return_value=mock_client):
            results, backend = client.generate_lexical("ont-1", [])
        assert results == []
        assert backend == "default"
        # No backend key added to payload when not provided.
        posted = mock_client.post.call_args.kwargs["json"]
        assert "backend" not in posted


# ── OntologyCatalogClient ────────────────────────────────────────────


class TestOntologyCatalogClient:
    def test_search_embeddings_threads_namespace_param(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        resp = _resp([{"entity_uri": "http://x"}])
        mock_client = _mock_httpx_client(resp)
        with patch.object(httpx, "Client", return_value=mock_client):
            out = client.search_embeddings([0.1], "lexical", "titan", namespace="ns1")
        assert out == [{"entity_uri": "http://x"}]
        assert mock_client.post.call_args.kwargs["params"] == {"namespace": "ns1"}

    def test_search_embeddings_omits_params_when_no_namespace(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp([]))
        with patch.object(httpx, "Client", return_value=mock_client):
            client.search_embeddings([0.1], "lexical", "titan", ontology_id="ont-1")
        assert mock_client.post.call_args.kwargs["params"] is None
        assert mock_client.post.call_args.kwargs["json"]["ontology_id"] == "ont-1"

    def test_get_embeddings_for_entity_passes_filters(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp([{"vector": [0.1]}]))
        with patch.object(httpx, "Client", return_value=mock_client):
            out = client.get_embeddings_for_entity("http://x", embedding_type="lexical", namespace="ns1")
        assert out == [{"vector": [0.1]}]
        assert mock_client.get.call_args.kwargs["params"] == {"embedding_type": "lexical", "namespace": "ns1"}

    def test_create_ontology_returns_json(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp({"id": "ont-1"}))
        with patch.object(httpx, "Client", return_value=mock_client):
            out = client.create_ontology({"uri": "http://x"})
        assert out == {"id": "ont-1"}

    def test_create_ontology_409_returns_existing(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        conflict = _resp({}, status_code=409)
        mock_client = _mock_httpx_client(conflict)
        with (
            patch.object(httpx, "Client", return_value=mock_client),
            patch.object(client, "get_ontology_by_uri", return_value={"id": "existing"}) as mock_get,
        ):
            out = client.create_ontology({"uri": "http://x"})
        assert out == {"id": "existing"}
        mock_get.assert_called_once_with("http://x")

    # ── transient retry on the legacy HTTP search path ──

    def test_search_embeddings_retries_transient_5xx_then_succeeds(self) -> None:
        """A transient 503 on recall is retried (reusing oss_retry's retry_call loop), not
        surfaced on the first blip. Two raise_for_status raises, then success."""

        client = OntologyCatalogClient("http://oc:8001")
        resp = _resp([{"entity_uri": "http://x"}])
        transient = httpx.HTTPStatusError(
            "503", request=httpx.Request("POST", "http://oc:8001"), response=httpx.Response(503)
        )
        resp.raise_for_status = MagicMock(side_effect=[transient, transient, None])
        mock_client = _mock_httpx_client(resp)
        with (
            patch.object(httpx, "Client", return_value=mock_client),
            patch("time.sleep", return_value=None),
        ):
            out = client.search_embeddings([0.1], "lexical", "titan", ontology_id="ont-1")
        assert out == [{"entity_uri": "http://x"}]
        assert resp.raise_for_status.call_count == 3

    def test_search_embeddings_terminal_4xx_raises_immediately(self) -> None:
        """A non-retryable 400 propagates on the first attempt (no retry)."""

        client = OntologyCatalogClient("http://oc:8001")
        resp = _resp([])
        terminal = httpx.HTTPStatusError(
            "400", request=httpx.Request("POST", "http://oc:8001"), response=httpx.Response(400)
        )
        resp.raise_for_status = MagicMock(side_effect=terminal)
        mock_client = _mock_httpx_client(resp)
        with (
            patch.object(httpx, "Client", return_value=mock_client),
            patch("time.sleep", return_value=None),
            pytest.raises(httpx.HTTPStatusError),
        ):
            client.search_embeddings([0.1], "lexical", "titan", ontology_id="ont-1")
        assert resp.raise_for_status.call_count == 1

    def test_search_embeddings_exhausts_retries_then_reraises(self) -> None:
        """Every attempt fails transiently → the error propagates after
        _HTTP_MAX_RETRIES + 1 tries (same exhaustion behavior as oss_retry)."""
        from coa_ontology.inducer.services import ontology_catalog as oc_mod

        client = OntologyCatalogClient("http://oc:8001")
        resp = _resp([])
        transient = httpx.HTTPStatusError(
            "503", request=httpx.Request("POST", "http://oc:8001"), response=httpx.Response(503)
        )
        resp.raise_for_status = MagicMock(side_effect=transient)
        mock_client = _mock_httpx_client(resp)
        with (
            patch.object(oc_mod, "_HTTP_MAX_RETRIES", 2),
            patch.object(httpx, "Client", return_value=mock_client),
            patch("time.sleep", return_value=None),
            pytest.raises(httpx.HTTPStatusError),
        ):
            client.search_embeddings([0.1], "lexical", "titan", ontology_id="ont-1")
        assert resp.raise_for_status.call_count == 3  # _HTTP_MAX_RETRIES (2) + 1

    @pytest.mark.parametrize("exc", [httpx.ConnectError("boom"), httpx.TimeoutException("slow")])
    def test_search_embeddings_retries_connect_and_timeout(self, exc: Exception) -> None:
        """Connect/timeout faults on the POST are retried, then success."""

        client = OntologyCatalogClient("http://oc:8001")
        resp = _resp([{"entity_uri": "http://x"}])
        mock_client = _mock_httpx_client(resp)
        mock_client.post.side_effect = [exc, exc, resp]
        with (
            patch.object(httpx, "Client", return_value=mock_client),
            patch("time.sleep", return_value=None),
        ):
            out = client.search_embeddings([0.1], "lexical", "titan", ontology_id="ont-1")
        assert out == [{"entity_uri": "http://x"}]
        assert mock_client.post.call_count == 3

    def test_search_embeddings_retries_transient_429_then_succeeds(self) -> None:
        """A 429 (throttle) on recall is retried like a 5xx, then success."""

        client = OntologyCatalogClient("http://oc:8001")
        resp = _resp([{"entity_uri": "http://x"}])
        transient = httpx.HTTPStatusError(
            "429", request=httpx.Request("POST", "http://oc:8001"), response=httpx.Response(429)
        )
        resp.raise_for_status = MagicMock(side_effect=[transient, None])
        mock_client = _mock_httpx_client(resp)
        with (
            patch.object(httpx, "Client", return_value=mock_client),
            patch("time.sleep", return_value=None),
        ):
            out = client.search_embeddings([0.1], "lexical", "titan", ontology_id="ont-1")
        assert out == [{"entity_uri": "http://x"}]
        assert resp.raise_for_status.call_count == 2

    def test_get_ontology_by_uri_raises_when_missing(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp({"ontologies": []}))
        with (
            patch.object(httpx, "Client", return_value=mock_client),
            pytest.raises(ValueError, match="not found"),
        ):
            client.get_ontology_by_uri("http://missing")

    def test_get_ontology_by_uri_returns_first(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp({"ontologies": [{"ontologyId": "a"}, {"ontologyId": "b"}]}))
        with patch.object(httpx, "Client", return_value=mock_client):
            assert client.get_ontology_by_uri("http://x") == {"ontologyId": "a"}

    def test_get_ontology_by_uri_unwraps_before_the_emptiness_check(self) -> None:
        """A populated envelope must not be indexed as if it were the list.

        ``{"ontologies": [...]}`` is truthy, so an implementation that checks
        emptiness first and then does ``results[0]`` raises ``KeyError: 0``
        rather than returning the record.
        """
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp({"ontologies": [{"ontologyId": "only"}]}))
        with patch.object(httpx, "Client", return_value=mock_client):
            assert client.get_ontology_by_uri("http://x") == {"ontologyId": "only"}

    def test_get_ontology_by_uri_accepts_a_legacy_bare_array(self) -> None:
        """Deployments predating the envelope answered with a bare array."""
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp([{"ontology_id": "legacy"}]))
        with patch.object(httpx, "Client", return_value=mock_client):
            assert client.get_ontology_by_uri("http://x") == {"ontology_id": "legacy"}

    def test_upload_ontology_file_posts_multipart(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp({"file_path": "s3://x"}))
        with patch.object(httpx, "Client", return_value=mock_client):
            out = client.upload_ontology_file("ont 1", b"@prefix x", filename="o.ttl")
        assert out == {"file_path": "s3://x"}
        # ontology_id is URL-encoded into the path (space → %20).
        assert "ont%201" in mock_client.post.call_args[0][0]
        assert "files" in mock_client.post.call_args.kwargs

    def test_store_embeddings_batch_wraps_payload(self) -> None:
        client = OntologyCatalogClient("http://oc:8001")
        mock_client = _mock_httpx_client(_resp([{"ok": True}]))
        with patch.object(httpx, "Client", return_value=mock_client):
            out = client.store_embeddings_batch([{"vector": [0.1]}])
        assert out == [{"ok": True}]
        assert mock_client.post.call_args.kwargs["json"] == {"embeddings": [{"vector": [0.1]}]}
