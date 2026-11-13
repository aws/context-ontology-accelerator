# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for embedding-failure handling in ``_accumulate_embeddings``."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace

EX = Namespace("http://example.org/ontology#")


def _build_simple_graph():
    """Build a minimal graph with one class for embedding tests."""
    g = Graph()
    g.add((EX.Customer, RDF.type, OWL.Class))
    g.add((EX.Customer, RDFS.label, Literal("Customer")))
    return g


@pytest.mark.unit
class TestAccumulateEmbeddingsErrorHandling:
    """Verify _accumulate_embeddings returns correct status on various failures."""

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_embedding_client_error_returns_error_status(self, mock_bedrock_cls):
        """When BedrockEmbeddingClient.embed_texts raises, result has status=error."""
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        mock_bedrock.embed_texts.side_effect = RuntimeError("AOSS index creation failed: HTTP 400")
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()

        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=_build_simple_graph(),
            ontology_id="http://example.org/ontology#",
            classes={EX.Customer},
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
        )

        assert result["status"] == "error"
        assert "AOSS index creation failed" in result["error"]
        assert "RuntimeError" in result["error"]

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_vector_store_error_returns_error_status(self, mock_bedrock_cls):
        """When vector_store.store_embeddings_batch raises, result has status=error."""
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()
        mock_vector_store.store_embeddings_batch.side_effect = ConnectionError("AOSS unreachable")

        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=_build_simple_graph(),
            ontology_id="http://example.org/ontology#",
            classes={EX.Customer},
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
        )

        assert result["status"] == "error"
        assert "AOSS unreachable" in result["error"]

    @patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient")
    def test_ok_status_on_success(self, mock_bedrock_cls):
        """Successful embedding returns status=ok."""
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_bedrock = MagicMock()
        mock_bedrock.model_id = "amazon.titan-embed-text-v2:0"
        mock_bedrock.embed_texts.return_value = [[0.1] * 1024]
        mock_bedrock_cls.return_value = mock_bedrock

        mock_vector_store = MagicMock()

        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=_build_simple_graph(),
            ontology_id="http://example.org/ontology#",
            classes={EX.Customer},
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
        )

        assert result["status"] == "ok"
        assert result["count"] == 1

    def test_skipped_status_when_no_entities(self):
        """When no classes/properties, status is 'skipped' — NOT an error."""
        from coa_ontology.catalog.ingest import _accumulate_embeddings

        mock_vector_store = MagicMock()
        g = Graph()

        result = _accumulate_embeddings(
            vector_store=mock_vector_store,
            graph=g,
            ontology_id="http://example.org/ontology#",
            classes=set(),
            obj_props=set(),
            dt_props=set(),
            namespace="test-ns",
        )

        assert result["status"] == "skipped"
        assert result["count"] == 0


@pytest.mark.unit
class TestIngestStoreErrorOnEmbeddingFailure:
    """Verify the ingest caller raises IngestStoreError when embedding fails."""

    def test_ingest_store_error_importable(self):
        """IngestStoreError is importable and is a subclass of IngestError."""
        from coa_ontology.catalog.ingest import IngestError, IngestStoreError

        assert issubclass(IngestStoreError, IngestError)
        assert issubclass(IngestStoreError, Exception)
