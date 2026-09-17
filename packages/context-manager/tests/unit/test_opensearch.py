# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the serve OpenSearch VectorClient (async wrapper over the
shared AossVectorClient).

The serve client owns the async surface, ``VectorHit`` shaping, hybrid search,
and the proxy transport; the shared client owns the actual query building /
filtering / retry (tested in ``coa_common``'s
``test_opensearch_client.py``). So here we inject a mock shared client via
``client._aoss`` and assert delegation + hit shaping.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _client(**aoss_attrs):
    """Build a serve client with a mocked shared AossVectorClient."""
    from coa_serve.clients.opensearch import OpenSearchVectorClient

    client = OpenSearchVectorClient(endpoint="test.aoss.amazonaws.com")
    aoss = MagicMock()
    for k, v in aoss_attrs.items():
        getattr(aoss, k).return_value = v
    client._aoss = aoss
    return client, aoss


@pytest.mark.unit
class TestParseHits:
    def test_parse_hits(self):
        client, _ = _client()
        raw = {
            "hits": {
                "hits": [
                    {
                        "_id": "c1",
                        "_score": 0.92,
                        "_source": {
                            "text": "Claims SLA is 10 days",
                            "source_doc": "policy.pdf",
                            "label": "Policy.pdf",
                            "chunk_index": 3,
                            "type": "document",
                        },
                    }
                ]
            }
        }
        hits = client._parse_hits(raw)
        assert len(hits) == 1
        assert hits[0].id == "c1"
        assert hits[0].score == 0.92
        assert hits[0].text == "Claims SLA is 10 days"
        assert hits[0].metadata["label"] == "Policy.pdf"

    def test_parse_hits_empty_results(self):
        client, _ = _client()
        assert client._parse_hits({"hits": {"hits": []}}) == []

    def test_parse_hits_missing_fields(self):
        client, _ = _client()
        raw = {"hits": {"hits": [{"_id": "c2", "_score": 0.5, "_source": {"text": "some text"}}]}}
        hits = client._parse_hits(raw)
        assert hits[0].metadata["source_doc"] == ""
        assert hits[0].metadata["type"] == ""

    def test_parse_hits_keeps_missing_source_as_empty(self):
        client, _ = _client()
        raw = {
            "hits": {"hits": [{"_id": "c3", "_score": 0.8}, {"_id": "c4", "_score": 0.7, "_source": {"text": "valid"}}]}
        }
        hits = client._parse_hits(raw)
        assert len(hits) == 2
        assert hits[0].id == "c3" and hits[0].text == ""
        assert hits[1].id == "c4" and hits[1].text == "valid"


@pytest.mark.unit
class TestSourceDocumentIdentity:
    """Document identity/name resolution for GraphRAG chunk hits (#985).

    The nested fixture shape below is a real ``chunk_{tenant}`` document
    captured live from a deployed ap-northeast-1 environment, trimmed to the
    fields the ``_source`` projection requests.
    """

    _CHUNK_SOURCE = {
        "value": "外形図: 全高 98mm、レンズ部 φ120…",
        "metadata": {
            "source": {
                "sourceId": "aws:7ef12381488a4c3e9822c735d:8ca598df:2037",
                "metadata": {
                    # Staging artifact name — preprocessing converted the
                    # uploaded PDF to markdown, so this is NOT the name the
                    # user recognizes.
                    "filename": "nwcamera-NC-2600_dimensions.md",
                    "source_s3_key": "7ef12381/raw/320d3d58/nwcamera-NC-2600_dimensions.pdf",
                },
            },
        },
    }

    def test_chunk_hit_resolves_unique_id_and_display_name(self):
        """A retrieved chunk names its document: unique id + the UPLOADED
        document's name (raw-key basename), not the staging artifact's."""
        client, _ = _client()
        raw = {"hits": {"hits": [{"_id": "c1", "_score": 0.9, "_source": dict(self._CHUNK_SOURCE)}]}}

        hits = client._parse_hits(raw)

        assert hits[0].metadata["source_doc"] == "aws:7ef12381488a4c3e9822c735d:8ca598df:2037"
        assert hits[0].metadata["source_doc_name"] == "nwcamera-NC-2600_dimensions.pdf"
        assert hits[0].text == "外形図: 全高 98mm、レンズ部 φ120…"

    def test_staging_filename_is_only_the_fallback_name(self):
        """Without a raw key, the staging filename is better than nothing."""
        from coa_serve.clients.opensearch import _source_document_name

        source = {"metadata": {"source": {"sourceId": "aws:x:y:z", "metadata": {"filename": "report.md"}}}}
        assert _source_document_name(source) == "report.md"

    def test_same_path_in_two_sources_never_fuses_ids(self):
        """Regression: path-shaped metadata is NOT identity.

        Two document sources can each hold ``reports/annual.pdf``; their
        toolkit sourceIds differ, and the resolved ids must too. The shared
        path may only surface as the display name.
        """
        from coa_serve.clients.opensearch import _source_document_id, _source_document_name

        def _chunk(source_id: str) -> dict:
            return {
                "metadata": {
                    "source": {
                        "sourceId": source_id,
                        "metadata": {
                            "filename": "annual.pdf",
                            "source_s3_key": "reports/annual.pdf",
                        },
                    }
                }
            }

        chunk_a = _chunk("aws:tenant:1111:1")
        chunk_b = _chunk("aws:tenant:2222:9")

        assert _source_document_id(chunk_a) != _source_document_id(chunk_b)
        assert _source_document_id(chunk_a) == "aws:tenant:1111:1"
        assert _source_document_name(chunk_a) == _source_document_name(chunk_b) == "annual.pdf"

    def test_flat_source_doc_still_wins(self):
        from coa_serve.clients.opensearch import _source_document_id

        source = {"source_doc": "flat.pdf", **self._CHUNK_SOURCE}
        assert _source_document_id(source) == "flat.pdf"

    @pytest.mark.parametrize(
        "source",
        [
            {},
            {"metadata": None},
            {"metadata": "not-a-dict"},
            {"metadata": {"source": "bare-string"}},
            {"metadata": {"source": {"sourceId": 42, "metadata": {"filename": 7}}}},
        ],
    )
    def test_unknown_shapes_stay_empty(self, source):
        from coa_serve.clients.opensearch import _source_document_id, _source_document_name

        assert _source_document_id(source) == ""
        assert _source_document_name(source) == ""

    def test_projection_requests_the_nested_identity_fields(self):
        """The identity/name fields must be in the ``_source`` projection, or
        the resolvers never see them regardless of what the index holds."""
        from coa_serve.clients.opensearch import _SOURCE_FIELDS

        for path in (
            "metadata.source.sourceId",
            "metadata.source.metadata.source_s3_key",
            "metadata.source.metadata.filename",
        ):
            assert path in _SOURCE_FIELDS


@pytest.mark.unit
class TestConstruction:
    def test_missing_endpoint_raises(self):
        from coa_serve.clients.opensearch import OpenSearchVectorClient

        with pytest.raises(ValueError, match="OpenSearch endpoint must be provided"):
            OpenSearchVectorClient(endpoint="")


@pytest.mark.unit
class TestSearchDelegation:
    async def test_entity_type_becomes_server_side_filter_and_shapes_hits(self):
        client, aoss = _client(
            knn_search=[
                {"_id": "cls1", "_score": 0.9, "text": "Claim", "entity_type": "class"},
                {"_id": "cls2", "_score": 0.85, "text": "Policy", "entity_type": "class"},
            ]
        )
        hits = await client.search([0.1] * 8, top_k=2, index="ont-idx", entity_type="class")

        # Delegates to the shared client's knn_search with the term filter + top_k.
        kwargs = aoss.knn_search.call_args.kwargs
        assert kwargs["top_k"] == 2
        assert kwargs["filters"] == [{"term": {"entity_type": "class"}}]
        assert aoss.knn_search.call_args.args[0] == "ont-idx"
        assert [h.id for h in hits] == ["cls1", "cls2"]
        assert hits[0].metadata["entity_type"] == "class"

    async def test_require_mapped_adds_exists_filter(self):
        client, aoss = _client(knn_search=[])
        await client.search([0.1] * 8, top_k=5, entity_type="class", require_mapped=True)
        assert aoss.knn_search.call_args.kwargs["filters"] == [
            {"term": {"entity_type": "class"}},
            {"exists": {"field": "data_source_id"}},
        ]

    async def test_no_filters_passes_none(self):
        client, aoss = _client(knn_search=[])
        await client.search([0.1] * 8, top_k=7)
        assert aoss.knn_search.call_args.kwargs["filters"] is None
        assert aoss.knn_search.call_args.kwargs["top_k"] == 7

    async def test_count_documents_delegates_with_filters(self):
        client, aoss = _client(count=3)
        n = await client.count_documents(index="ont-idx", entity_type="class", require_mapped=True)
        assert n == 3
        assert aoss.count.call_args.args[0] == "ont-idx"
        assert aoss.count.call_args.kwargs["filters"] == [
            {"term": {"entity_type": "class"}},
            {"exists": {"field": "data_source_id"}},
        ]


@pytest.mark.unit
class TestHybridAndHealth:
    async def test_hybrid_search_uses_bool_should_via_search_raw(self):
        client, aoss = _client(search_raw={"hits": {"hits": [{"_id": "h1", "_score": 0.6, "_source": {"text": "t"}}]}})
        hits = await client.hybrid_search("query", [0.1] * 8, top_k=4, index="chunks")
        body = aoss.search_raw.call_args.args[1]
        should = body["query"]["bool"]["should"]
        assert any("match" in clause for clause in should)
        assert any("knn" in clause for clause in should)
        assert [h.id for h in hits] == ["h1"]

    async def test_health_check_ok_maps_to_healthy(self):
        client, aoss = _client(health_check={"status": "ok", "index": "document-chunks"})
        result = await client.health_check()
        assert result["status"] == "healthy"

    async def test_health_check_error_maps_to_unhealthy(self):
        client, aoss = _client(health_check={"status": "error", "error": "boom"})
        result = await client.health_check()
        assert result["status"] == "unhealthy"

    async def test_close_is_noop(self):
        client, _ = _client()
        await client.close()  # must not raise
