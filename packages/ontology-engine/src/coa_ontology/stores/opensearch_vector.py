# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenSearch Serverless vector store for the ontology workbench.

Thin :class:`VectorStore` adapter over the shared
:class:`coa_common.opensearch.AossVectorClient`. This module owns
only the ontology-workbench-specific concerns:

  - env configuration (endpoint / region / index prefix / dimensions);
  - per-namespace index naming (one index per namespace);
  - document shaping for the ``(entity_uri, embedding_type, model_id)`` schema;
  - ``EmbeddingHit`` construction from raw hits.

The AOSS client, SigV4 auth, transient-fault retry, canonical index mapping
(Faiss/HNSW, method-less, server-side ``knn.embedding.filter``), and all the
AOSS quirks (no client ``_id``, no ``_delete_by_query``, no scroll, 404 on
``/``) live in the shared library — see its package docstring.

Indexing strategy: one index per namespace; one document per
``(entity_uri, embedding_type, model_id)``. Vector field is ``embedding``
(Faiss/HNSW, 1024-d by default).
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any

from coa_common import AossVectorClient, ontology_vector_index_name

from coa_ontology.stores.base import EmbeddingHit, VectorStore

log = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────

OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "")  # e.g. https://xxx.us-east-1.aoss.amazonaws.com
OSS_REGION = os.getenv("OSS_REGION", os.getenv("AWS_REGION", "us-east-1"))
OSS_INDEX = os.getenv("OSS_INDEX", "ontology-workbench-embeddings")
OSS_DIMENSIONS = int(os.getenv("OSS_DIMENSIONS", "1024"))
DEFAULT_NAMESPACE = os.getenv("DYNAMODB_DEFAULT_NAMESPACE", "default")


def _index_for_namespace(namespace: str | None = None) -> str:
    """Return the OSS index name for a given namespace."""
    ns = namespace or DEFAULT_NAMESPACE
    return ontology_vector_index_name(OSS_INDEX, ns, default_namespace=DEFAULT_NAMESPACE)


# ── VectorStore impl ────────────────────────────────────────────────────


class OpenSearchVectorStore(VectorStore):
    """k-NN vector store backed by OpenSearch Serverless (VECTORSEARCH).

    Namespace handling: the store accepts a namespace at construction time
    (bound) OR per-call (explicit). Per-call namespace overrides the bound
    one. Each namespace maps to its own OpenSearch index (see
    :func:`_index_for_namespace`), so reads and writes are isolated.
    """

    def __init__(self, namespace: str | None = None):
        """Bind the store to a namespace and defer AOSS client creation.

        Args:
            namespace: Default namespace (and index) for reads/writes; a
                per-call namespace overrides it.
        """
        self.dimensions = OSS_DIMENSIONS
        self._namespace = namespace
        self._client: AossVectorClient | None = None

    def _aoss(self) -> AossVectorClient:
        """Lazily build the shared AOSS client.

        Defers the endpoint check so the store can be constructed under
        DI/tests without ``OSS_ENDPOINT`` set.
        """
        if self._client is None:
            if not OSS_ENDPOINT:
                raise RuntimeError("OSS_ENDPOINT not set — configure your OSS collection endpoint")
            self._client = AossVectorClient(
                endpoint=OSS_ENDPOINT,
                region=OSS_REGION,
                dimensions=OSS_DIMENSIONS,
                timeout=60,
            )
        return self._client

    def _resolve_ns(self, namespace: str | None) -> str | None:
        """Explicit call-site namespace wins; fall back to bound namespace."""
        return namespace if namespace is not None else self._namespace

    def _ensure_index(self, namespace: str | None = None) -> str:
        """Create the OSS index for the namespace if it doesn't exist.

        WRITE PATH ONLY. Reads must use :meth:`_read_index` — see its docstring.

        Returns the resolved index name.
        """
        return self._aoss().ensure_index(_index_for_namespace(self._resolve_ns(namespace)))

    def _read_index(self, namespace: str | None = None) -> str:
        """Resolve the index name for a read WITHOUT creating it.

        Reads deliberately do not ``ensure_index``: creating an index on the
        read path resurrects an empty shell for a namespace that was already
        torn down (namespace deletion drops the index, then any late read —
        a status poll, a search, a stale retry — recreated it). Nothing ever
        reclaims those shells, and AOSS caps a collection at 1000 indexes, so
        they accumulate until every write fails with ``index_limit_breached``.

        The shared ``AossVectorClient`` read helpers treat a missing index as
        "no data", so a read against a not-yet-created index returns empty
        instead of raising.
        """
        return _index_for_namespace(self._resolve_ns(namespace))

    def raw_client(self):
        """Return the retry-wrapped opensearch-py client for low-level access.

        Intended for debug/dev use (e.g. the live-induction dev script's ad-hoc
        ``.get`` / ``.count``). Prefer the typed store methods in app code.
        """
        return self._aoss().raw_client()

    # Writes -----------------------------------------------------------

    def _doc_from(self, item: dict, ns: str | None) -> dict:
        """Shape a store item into the index document schema."""
        vec = item["vector"]
        if len(vec) < self.dimensions:
            vec = list(vec) + [0.0] * (self.dimensions - len(vec))
        doc = {
            "entity_uri": item["entity_uri"],
            "ontology_id": item.get("ontology_id"),
            "entity_type": item.get("entity_type"),
            "embedding_type": item["embedding_type"],
            "model_id": item["model_id"],
            "namespace": ns or DEFAULT_NAMESPACE,
            "text": item.get("text", ""),
            "embedding": vec,
        }
        if item.get("context_text"):
            doc["context_text"] = item["context_text"]
        if item.get("data_source_id"):
            doc["data_source_id"] = item["data_source_id"]
        return doc

    def store_embedding(self, data: dict) -> dict:
        """Append one embedding document.

        AOSS auto-assigns ``_id`` and does not support client-supplied IDs or
        ``_delete_by_query`` on VECTORSEARCH collections. Re-accepting the same
        ontology therefore appends duplicate vectors rather than overwriting;
        callers that need idempotency should
        :meth:`delete_embeddings_for_ontology` first.
        """
        ns = self._resolve_ns(data.get("namespace"))
        idx = self._ensure_index(ns)
        doc = self._doc_from(data, ns)
        self._aoss().index_document(idx, doc)
        return doc

    def store_embeddings_batch(self, items: list[dict]) -> list[dict]:
        """Append a batch of embeddings (AOSS-compatible).

        See :meth:`store_embedding` for the idempotency caveat.
        """
        if not items:
            return []
        # Group by namespace to write to the correct indices. Each item's
        # namespace resolves through _resolve_ns so the bound namespace is
        # honoured when the item doesn't specify one.
        by_ns: dict[str | None, list[dict]] = defaultdict(list)
        for it in items:
            by_ns[self._resolve_ns(it.get("namespace"))].append(it)

        out: list[dict] = []
        for ns, ns_items in by_ns.items():
            idx = self._ensure_index(ns)
            docs = [self._doc_from(it, ns) for it in ns_items]
            self._aoss().bulk_index(idx, docs)
            out.extend(docs)
        return out

    # Lookups ----------------------------------------------------------

    def get_embeddings_for_entity(
        self,
        entity_uri,
        entity_type=None,
        embedding_type=None,
        namespace=None,
    ):
        """Return stored embeddings for one entity, optionally filtered.

        Args:
            entity_uri: URI of the entity whose embeddings to fetch.
            entity_type: Restrict to one entity type when provided.
            embedding_type: Restrict to one embedding variant when provided.
            namespace: Per-call namespace override; defaults to the bound one.

        Returns:
            List of matching embedding documents.
        """
        idx = self._read_index(namespace)
        filters: list[dict[str, Any]] = [{"term": {"entity_uri": entity_uri}}]
        if entity_type:
            filters.append({"term": {"entity_type": entity_type}})
        if embedding_type:
            filters.append({"term": {"embedding_type": embedding_type}})
        return self._aoss().filter_search(idx, filters, size=1000)

    def list_embeddings_for_ontology(
        self,
        ontology_id,
        entity_type=None,
        embedding_type=None,
        namespace=None,
    ):
        """Return all embeddings for an ontology, optionally filtered.

        Args:
            ontology_id: ID of the ontology whose embeddings to list.
            entity_type: Restrict to one entity type when provided.
            embedding_type: Restrict to one embedding variant when provided.
            namespace: Per-call namespace override; defaults to the bound one.

        Returns:
            List of matching embedding documents.
        """
        idx = self._read_index(namespace)
        filters: list[dict[str, Any]] = [{"term": {"ontology_id": ontology_id}}]
        if entity_type:
            filters.append({"term": {"entity_type": entity_type}})
        if embedding_type:
            filters.append({"term": {"embedding_type": embedding_type}})
        return self._aoss().filter_search(idx, filters, size=10_000)

    def list_searchable_entity_uris(self, ontology_id, namespace=None):
        """Return the entity URIs currently searchable for an ontology.

        Projects only ``entity_uri`` (excludes vectors/text) and pages with
        ``search_after`` so the readiness wait gets the complete URI set
        cheaply.

        Args:
            ontology_id: ID of the ontology to enumerate.
            namespace: Per-call namespace override; defaults to the bound one.

        Returns:
            List of ``entity_uri`` strings visible in the index.
        """
        # Project only entity_uri (exclude the knn_vector + text payload) and
        # page with search_after — the readiness wait needs the complete URI
        # set, never the vectors.
        idx = self._read_index(namespace)
        return self._aoss().iter_source_field(idx, [{"term": {"ontology_id": ontology_id}}], "entity_uri")

    def search_nearest(
        self,
        vector,
        embedding_type,
        model_id,
        entity_type=None,
        ontology_id=None,
        top_k=10,
        namespace=None,
    ):
        """k-NN search against stored vectors of the same model and type.

        Note: OpenSearch returns a ``cosinesimil`` ``_score`` in ``[0, 1]``;
        this backend does not yet convert it to raw cosine similarity, so
        ``EmbeddingHit.score`` is in ``[0, 1]`` here (see the inline note).

        Args:
            vector: Query embedding vector.
            embedding_type: Embedding variant to match against.
            model_id: Embedding model the vector was produced with.
            entity_type: Restrict to one entity type when provided.
            ontology_id: Restrict to a single ontology when provided.
            top_k: Maximum number of nearest hits to return.
            namespace: Per-call namespace override; defaults to the bound one.

        Returns:
            List of :class:`EmbeddingHit` nearest matches.
        """
        idx = self._read_index(namespace)
        filters: list[dict[str, Any]] = [
            {"term": {"embedding_type": embedding_type}},
            {"term": {"model_id": model_id}},
        ]
        if entity_type:
            filters.append({"term": {"entity_type": entity_type}})
        if ontology_id:
            filters.append({"term": {"ontology_id": ontology_id}})
        raw_hits = self._aoss().knn_search(idx, vector, top_k=top_k, filters=filters)
        hits = []
        for s in raw_hits:
            # NOTE: the ``VectorStore.search_nearest`` protocol expects raw cosine
            # similarity in [-1, 1] here. OpenSearch's k-NN ``cosinesimil`` _score
            # is ``(1 + cos)/2`` mapped to [0, 1], so the conversion
            # ``cos = 2 * _score - 1`` is needed to conform. That conversion is
            # deliberately NOT applied yet — see the OPTIONAL task in tasks.md
            # (Task 10A.1). Until then, callers using the OpenSearch backend must
            # be aware that ``score`` is in [0, 1] and the class normalizer's
            # thresholds (0.95 / 0.80) DO NOT behave as documented for OpenSearch.
            hits.append(
                EmbeddingHit(
                    entity_uri=s["entity_uri"],
                    embedding_type=s["embedding_type"],
                    model_id=s["model_id"],
                    vector=s.get("embedding", []),
                    ontology_id=s.get("ontology_id"),
                    entity_type=s.get("entity_type"),
                    score=s.get("_score"),
                    text=s.get("text"),
                )
            )
        return hits

    def delete_embeddings_for_ontology(self, ontology_id, namespace=None) -> int:
        """Delete all embeddings for an ontology, returning the deleted count.

        Uses per-doc deletes because AOSS has no ``_delete_by_query`` on
        VECTORSEARCH collections.
        """
        idx = self._read_index(namespace)
        return self._aoss().delete_by_term(idx, "ontology_id", ontology_id)

    def delete_embeddings_for_entities(
        self,
        entity_uris,
        ontology_id: str,
        namespace=None,
    ) -> int:
        """Delete the embeddings of the given entities within a single ontology.

        Compound filter: ``entity_uri IN uris AND ontology_id == ontology_id``.
        A namespace index can carry embeddings for multiple ontologies (shared
        IRIs from foundational reloads or explicit-target merges), so the
        ``ontology_id`` clause is required to avoid taking a sibling ontology's
        embedding down with the current one.

        One ``bool``-wrapped ``terms``+``term`` query + one verified delete loop
        for the whole set (see ``AossVectorClient.delete_by_term``) — NOT one
        round-trip per entity, so a re-accepted proposal with hundreds of
        classes/properties pays the AOSS refresh-lag wait once.
        """
        uris = [u for u in dict.fromkeys(entity_uris) if u]
        if not uris or not ontology_id:
            return 0
        idx = self._read_index(namespace)
        return self._aoss().delete_by_term(
            idx,
            "entity_uri",
            uris,
            and_filters=[{"term": {"ontology_id": ontology_id}}],
        )

    def delete_index(self, namespace: str | None = None) -> bool:
        """Delete the entire AOSS index for a namespace. Returns True if it existed."""
        idx = _index_for_namespace(self._resolve_ns(namespace))
        return self._aoss().delete_index(idx)

    # Health -----------------------------------------------------------

    def health_check(self) -> dict:
        """Probe OpenSearch Serverless by ensuring the configured index exists.

        AOSS 404s on ``/`` so ``client.info()`` is unusable; ensuring the
        index is the cheapest viable liveness probe.
        """
        return self._aoss().health_check(_index_for_namespace(self._namespace))
