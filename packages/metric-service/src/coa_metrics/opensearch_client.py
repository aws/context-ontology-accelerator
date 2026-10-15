# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenSearch Serverless client for metric vector embeddings.

Metrics are embedded alongside ontology classes and properties in the shared
per-namespace ontology vector index (same index the ontology-engine uses). This
module is a thin wrapper over the shared
:class:`coa_common.opensearch.AossVectorClient` — it owns only the
metric-specific concerns (embedding-text construction, the metric document
schema, and ``search_metrics`` result shaping). The AOSS client, SigV4 auth,
transient-fault retry, and the canonical index mapping live in the shared
library (see its package docstring).

Document schema (a subset of the shared canonical mapping):
  - entity_uri: the metric's IRI (urn:coa:{ns}:metric:{name})
  - ontology_id: GOVERNED_METRICS_ONTOLOGY_ID
  - entity_type: "metric"
  - embedding_type: "lexical"
  - model_id: Bedrock model ID
  - namespace: namespace name
  - text: embedding source text
  - embedding: 1024-d float vector

Embedding generation via Bedrock Cohere Embed v4 (DEFAULT_EMBED_MODEL_ID).
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common import (
    DEFAULT_EMBED_MODEL_ID,
    GOVERNED_METRICS_ONTOLOGY_ID,
    AossVectorClient,
    BedrockEmbedder,
    ontology_vector_index_name,
)
from coa_common.opensearch import DeleteIncompleteError

from coa_metrics.neptune_client import MetricDefinition, _metric_uri

logger = structlog.get_logger(__name__)

# ── Configuration ───────────────────────────────────────────────────────

OSS_ENDPOINT = os.getenv("OPENSEARCH_ENDPOINT", "")
OSS_REGION = os.getenv("AWS_REGION", "us-east-1")
OSS_INDEX_PREFIX = os.getenv("OSS_INDEX_PREFIX", "ontology-workbench-embeddings")
OSS_DIMENSIONS = int(os.getenv("OSS_DIMENSIONS", "1024"))
DEFAULT_NAMESPACE = os.getenv("DYNAMODB_DEFAULT_NAMESPACE", "default")

BEDROCK_MODEL_ID = os.getenv("BEDROCK_EMBED_MODEL_ID", os.getenv("BEDROCK_MODEL_ID", DEFAULT_EMBED_MODEL_ID))
BEDROCK_REGION = os.getenv("BEDROCK_REGION", os.getenv("AWS_REGION", "us-east-1"))


# ── Bedrock embedding ───────────────────────────────────────────────────


def _make_embedder() -> BedrockEmbedder:
    """Shared embedder — same model/dimensions as ontology + serve."""
    return BedrockEmbedder(model_id=BEDROCK_MODEL_ID, dimensions=OSS_DIMENSIONS, region=BEDROCK_REGION)


def _generate_embedding(text: str, embedder: BedrockEmbedder | None = None) -> list[float]:
    """Generate a metric embedding. Metric text is indexable content → document."""
    return (embedder or _make_embedder()).embed_document(text)


# ── Embedding text construction ─────────────────────────────────────────


def _build_embedding_text(metric: MetricDefinition) -> str:
    """Build embedding text matching ontology-engine style: label + description + synonyms."""
    parts = [metric.name, metric.description]
    if metric.ai_context:
        if metric.ai_context.synonyms:
            parts.append(" ".join(metric.ai_context.synonyms))
        if metric.ai_context.instructions:
            parts.append(metric.ai_context.instructions)
        if metric.ai_context.examples:
            parts.append(" ".join(metric.ai_context.examples))
    return " ".join(parts)


# ── MetricOpenSearchClient ──────────────────────────────────────────────


class MetricOpenSearchClient:
    """OpenSearch Serverless client for metric vector embeddings.

    Documents are stored in the shared ontology vector index with
    entity_type="metric", conforming to the ontology-engine's schema.
    """

    def __init__(self) -> None:
        """Initialize with lazily constructed AOSS client and embedder."""
        self._client: AossVectorClient | None = None
        self._embedder: BedrockEmbedder | None = None

    def _aoss(self) -> AossVectorClient:
        if self._client is None:
            if not OSS_ENDPOINT:
                raise RuntimeError("OPENSEARCH_ENDPOINT not set")
            self._client = AossVectorClient(
                endpoint=OSS_ENDPOINT,
                region=OSS_REGION,
                dimensions=OSS_DIMENSIONS,
                timeout=10,
            )
        return self._client

    def _embedder_client(self) -> BedrockEmbedder:
        if self._embedder is None:
            self._embedder = _make_embedder()
        return self._embedder

    def _index_name(self, namespace: str) -> str:
        """Return the shared ontology vector index name for a namespace."""
        return ontology_vector_index_name(OSS_INDEX_PREFIX, namespace, default_namespace=DEFAULT_NAMESPACE)

    def embed_metric(self, namespace: str, metric: MetricDefinition) -> None:
        """Generate embedding and upsert to the shared ontology vector index."""
        text = _build_embedding_text(metric)
        embedding = _generate_embedding(text, self._embedder_client())
        entity_uri = _metric_uri(namespace, metric.name)
        index = self._aoss().ensure_index(self._index_name(namespace))

        # Delete existing docs for this entity_uri (AOSS idempotent upsert).
        self._delete_by_entity_uri(index, entity_uri)

        # Index with the shared canonical schema (entity_type="metric").
        self._aoss().index_document(
            index,
            {
                "entity_uri": entity_uri,
                "ontology_id": GOVERNED_METRICS_ONTOLOGY_ID,
                "entity_type": "metric",
                "embedding_type": "lexical",
                "model_id": BEDROCK_MODEL_ID,
                "namespace": namespace,
                "text": text,
                "embedding": embedding,
            },
        )
        logger.info("Metric embedded", namespace=namespace, name=metric.name, text_length=len(text))

    def delete_metric_embedding(self, namespace: str, name: str) -> None:
        """Delete the embedding document for a metric."""
        entity_uri = _metric_uri(namespace, name)
        index = self._index_name(namespace)
        self._delete_by_entity_uri(index, entity_uri)
        logger.info("Metric embedding deleted", namespace=namespace, name=name)

    def search_metrics(
        self,
        namespace: str,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """k-NN search for metrics by vector similarity (server-side filtered)."""
        index = self._index_name(namespace)
        raw_hits = self._aoss().knn_search(
            index,
            query_embedding,
            top_k=top_k,
            filters=[{"term": {"entity_type": "metric"}}, {"term": {"namespace": namespace}}],
        )
        return [
            {
                "entity_uri": s.get("entity_uri"),
                "metric_name": (s.get("entity_uri") or "").rsplit(":", 1)[-1],
                "namespace": s.get("namespace"),
                "text": s.get("text"),
                "score": s.get("_score"),
            }
            for s in raw_hits
        ]

    def _delete_by_entity_uri(self, index: str, entity_uri: str) -> None:
        """Delete documents matching entity_uri (AOSS idempotent upsert pattern).

        Best-effort: the index may not exist yet (first metric in a namespace),
        and a delete that stops with the old document still matching is logged,
        not raised. The metric write that follows still lands, and the next
        upsert or delete of the same metric retries the leftover.
        """
        try:
            self._aoss().delete_by_term(index, "entity_uri", entity_uri, page=10)
        except DeleteIncompleteError as exc:
            logger.warning(
                "delete_by_entity_uri_incomplete", entity_uri=entity_uri, remaining=exc.remaining, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 — index-absent on first write is expected
            logger.debug("delete_by_entity_uri_skipped", entity_uri=entity_uri, error=str(exc))
