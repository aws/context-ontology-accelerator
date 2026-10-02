# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Neptune Analytics vector store — thin wrapper over ``app.catalog.na_store``.

Neptune Analytics hosts both the graph and a native vector index; the
``Embedding`` nodes created by :func:`na_store.store_embedding` carry a
``~embedding`` property indexed for k-NN search.

Namespace handling: the store accepts a namespace at construction time
(bound) OR per-call (explicit). Per-call namespace overrides the bound
one. Every read/write query filters by the ``namespace`` property on
the ``Embedding`` node.
"""

from __future__ import annotations

from coa_ontology.catalog import na_store
from coa_ontology.stores.base import EmbeddingHit, VectorStore


class NeptuneAnalyticsVectorStore(VectorStore):
    """Vector upsert + k-NN search against the NA native vector index."""

    def __init__(self, namespace: str | None = None):
        """Bind the store to a namespace and the NA vector dimension.

        Args:
            namespace: Default namespace for reads/writes; a per-call
                namespace overrides it.
        """
        self.dimensions = na_store.VSS_DIMENSION
        self._namespace = namespace

    def _resolve_ns(self, namespace: str | None) -> str | None:
        """Explicit call-site namespace wins; fall back to bound namespace."""
        return namespace if namespace is not None else self._namespace

    # Writes -----------------------------------------------------------
    def store_embedding(self, data):
        """Upsert a single embedding node into the NA vector index.

        Args:
            data: Embedding fields; a ``namespace`` key overrides the bound one.

        Returns:
            The na_store store result.
        """
        # Pull namespace from data, call-site, or bound (in that order)
        ns = data.get("namespace") or self._namespace
        return na_store.store_embedding(data, namespace=ns)

    def store_embeddings_batch(self, items):
        """Upsert a batch of embeddings, defaulting each to the bound namespace.

        Args:
            items: Embedding dicts to store; items lacking a ``namespace``
                inherit the bound one.

        Returns:
            The na_store batch-store result.
        """
        # Inject bound namespace into items that don't already specify one
        if self._namespace:
            items = [({**it, "namespace": it.get("namespace") or self._namespace}) for it in items]
        return na_store.store_embeddings_batch(items, namespace=self._namespace)

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
            List of embedding dicts for the entity.
        """
        return na_store.get_embeddings_for_entity(
            entity_uri,
            entity_type=entity_type,
            embedding_type=embedding_type,
            namespace=self._resolve_ns(namespace),
        )

    def list_embeddings_for_ontology(
        self,
        ontology_id,
        entity_type=None,
        embedding_type=None,
        namespace=None,
    ):
        """Return all embeddings attached to an ontology, optionally filtered.

        Args:
            ontology_id: ID of the ontology whose embeddings to list.
            entity_type: Restrict to one entity type when provided.
            embedding_type: Restrict to one embedding variant when provided.
            namespace: Per-call namespace override; defaults to the bound one.

        Returns:
            List of embedding dicts for the ontology.
        """
        return na_store.list_embeddings_for_ontology(
            ontology_id,
            entity_type=entity_type,
            embedding_type=embedding_type,
            namespace=self._resolve_ns(namespace),
        )

    def list_searchable_entity_uris(self, ontology_id, namespace=None):
        """Return the entity URIs currently searchable for an ontology.

        Args:
            ontology_id: ID of the ontology to enumerate.
            namespace: Per-call namespace override; defaults to the bound one.

        Returns:
            List of ``entity_uri`` strings visible in the vector index.
        """
        return na_store.list_searchable_entity_uris(
            ontology_id,
            namespace=self._resolve_ns(namespace),
        )

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
        """Forward to ``na_store.search_nearest``.

        Per the :meth:`~coa_ontology.stores.base.VectorStore.search_nearest`
        protocol contract, ``EmbeddingHit.score`` is raw cosine similarity
        in ``[-1, 1]``. ``na_store.search_nearest`` calls
        ``neptune.algo.vectors.topK.byEmbedding`` (the proper indexed
        Neptune Analytics k-NN primitive) and converts the raw L2-squared
        distance returned by NA to cosine similarity in-query via
        ``cos = 1 - L2² / 2``. This conversion is valid because input
        embeddings from ``BedrockEmbeddingClient`` are L2-normalized unit
        vectors.

        See ``na_store.search_nearest`` for the precise openCypher query
        and the rationale for using ``topK.byEmbedding`` over the now-
        deprecated ``distance.byEmbedding`` brute-force scan.
        """
        rows = na_store.search_nearest(
            vector=vector,
            embedding_type=embedding_type,
            model_id=model_id,
            entity_type=entity_type,
            ontology_id=ontology_id,
            top_k=top_k,
            namespace=self._resolve_ns(namespace),
        )
        hits: list[EmbeddingHit] = []
        for r in rows:
            hits.append(
                EmbeddingHit(
                    entity_uri=r.get("entity_uri", ""),
                    embedding_type=r.get("embedding_type", embedding_type),
                    model_id=r.get("model_id", model_id),
                    vector=r.get("vector", []),
                    ontology_id=r.get("ontology_id"),
                    entity_type=r.get("entity_type"),
                    score=r.get("score"),
                    metadata={
                        k: v
                        for k, v in r.items()
                        if k
                        not in {
                            "entity_uri",
                            "embedding_type",
                            "model_id",
                            "vector",
                            "ontology_id",
                            "entity_type",
                            "score",
                        }
                    },
                )
            )
        return hits

    def delete_embeddings_for_ontology(self, ontology_id, namespace=None):
        """Delete all embeddings for an ontology, returning the deleted count.

        Args:
            ontology_id: ID of the ontology whose embeddings to drop.
            namespace: Per-call namespace override; defaults to the bound one.

        Returns:
            The number of embeddings deleted.
        """
        return na_store.delete_embeddings_for_ontology(ontology_id, namespace=self._resolve_ns(namespace))

    def delete_embeddings_for_entities(
        self,
        entity_uris,
        ontology_id: str,
        namespace=None,
    ) -> int:
        """Delete the Embedding nodes of the given entities within a single ontology.

        Filters by ``ontology_id`` in addition to ``entity_uri`` so that shared
        IRIs across ontologies (foundational reloads, explicit-target merges)
        can't take a sibling ontology's embedding down with the current one.
        """
        uris = [u for u in dict.fromkeys(entity_uris) if u]
        if not uris or not ontology_id:
            return 0
        return na_store.delete_embeddings_for_entities(
            uris,
            ontology_id=ontology_id,
            namespace=self._resolve_ns(namespace),
        )

    def delete_index(self, namespace=None) -> bool:
        """Return False — NA has no separate vector index to delete.

        Args:
            namespace: Namespace whose index was requested (ignored).

        Returns:
            Always ``False``; NA embeddings are graph vertices dropped with
            the graph, not a standalone index.
        """
        # Neptune Analytics stores embeddings as graph vertices — they're
        # deleted alongside graph triples via DROP GRAPH. No separate index.
        return False

    def health_check(self):
        """Probe backend health via the underlying na_store client."""
        return na_store.health_check()
