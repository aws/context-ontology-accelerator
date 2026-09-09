# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Store-backed OntologyCatalogClient adapter.

The induction pipeline was written against :class:`OntologyCatalogClient`,
an HTTP client that talked to the stand-alone ontology-catalog service.
In the fused workbench we want it to go through the
:class:`~app.stores.base.GraphStore` + :class:`~app.stores.base.VectorStore`
abstractions instead, so either backend (``na_only`` or
``opensearch_neptune``) works transparently.

This adapter implements the subset of :class:`OntologyCatalogClient`
methods the pipeline actually uses:

  - :meth:`search_embeddings`
  - :meth:`get_embeddings_for_entity`
  - :meth:`create_ontology`
  - :meth:`get_ontology_by_uri`
  - :meth:`upload_ontology_file` (no-op unless backend supports it)
  - :meth:`store_embeddings_batch`

Anything else raises ``AttributeError`` — by design, so new call sites
are forced to go through the Protocols directly.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from coa_ontology.stores.base import GraphStore, VectorStore

log = logging.getLogger(__name__)


class StoreOntologyCatalogAdapter:
    """Implement the legacy OntologyCatalogClient interface over a store pair.

    Wraps a (GraphStore, VectorStore) pair so the induction pipeline can talk
    to either backend (``na_only`` or ``opensearch_neptune``) transparently.
    """

    def __init__(self, graph_store: GraphStore, vector_store: VectorStore, namespace: str | None = None):
        """Bind the adapter to a graph store, vector store, and namespace.

        Args:
            graph_store: Backend graph store for ontology/class metadata.
            vector_store: Backend vector store for embedding search and storage.
            namespace: Namespace this adapter is bound to; authoritative for
                all operations unless a per-call ``namespace`` overrides it.
        """
        self.graph = graph_store
        self.vec = vector_store
        self.namespace = namespace

    # Embedding ops ----------------------------------------------------

    def search_embeddings(
        self,
        vector,
        embedding_type,
        model_id,
        entity_type: str = "class",
        ontology_id: str | None = None,
        top_k: int = 5,
        namespace: str | None = None,
    ):
        """Search embeddings for the nearest entities to a query vector.

        Args:
            vector: Query embedding vector.
            embedding_type: Which embedding variant to search (e.g. label, comment).
            model_id: Embedding model identifier the vector was produced with.
            entity_type: Kind of entity to match (``"class"`` or ``"property"``).
            ontology_id: Restrict results to a single ontology when provided.
            top_k: Maximum number of nearest hits to return.
            namespace: Optional per-call namespace override; when None the
                adapter's bound namespace is used.

        Returns:
            List of hit dicts (nearest matches serialized from dataclasses).
        """
        # This adapter is namespace-bound at construction (self.namespace),
        # which is authoritative. Accept an optional per-call ``namespace``
        # for signature-compatibility with OntologyCatalogClient (so callers
        # like GroundingService._recall can pass it uniformly regardless of
        # which catalog implementation they hold); an explicit non-None value
        # overrides the bound one.
        ns = namespace if namespace is not None else self.namespace
        hits = self.vec.search_nearest(
            vector=vector,
            embedding_type=embedding_type,
            model_id=model_id,
            entity_type=entity_type,
            ontology_id=ontology_id,
            top_k=top_k,
            namespace=ns,
        )
        return [asdict(h) for h in hits]

    def list_embeddings_for_ontology(self, ontology_id, entity_type=None, embedding_type=None, namespace=None):
        """List an ontology's embedding docs (delegates to the vector store).

        Used by the grounding service's lexical (token-overlap) recall to
        enumerate a pool's class names portably across backends. Namespace
        resolution mirrors ``search_embeddings``: the bound namespace is
        authoritative unless a non-None per-call value overrides it.
        """
        ns = namespace if namespace is not None else self.namespace
        return self.vec.list_embeddings_for_ontology(
            ontology_id,
            entity_type=entity_type,
            embedding_type=embedding_type,
            namespace=ns,
        )

    def get_embeddings_for_entity(self, entity_uri, embedding_type=None, namespace=None):
        """Return stored embeddings for a single entity.

        Args:
            entity_uri: URI of the entity whose embeddings to fetch.
            embedding_type: Restrict to one embedding variant when provided.
            namespace: Optional per-call namespace override; defaults to the
                adapter's bound namespace.

        Returns:
            The vector store's embeddings for the entity.
        """
        ns = namespace if namespace is not None else self.namespace
        return self.vec.get_embeddings_for_entity(
            entity_uri=entity_uri,
            embedding_type=embedding_type,
            namespace=ns,
        )

    def store_embeddings_batch(self, embeddings):
        """Persist a batch of embeddings, tagging each with the bound namespace.

        Args:
            embeddings: Iterable of embedding dicts to store.

        Returns:
            The vector store's batch-store result.
        """
        # Inject namespace into every item so they land in the right index
        if self.namespace:
            embeddings = [{**e, "namespace": self.namespace} for e in embeddings]
        return self.vec.store_embeddings_batch(embeddings)

    # Class metadata ops ------------------------------------------------

    def get_class_definition(self, entity_uri: str) -> str:
        """Return the first comment/definition for a class, or empty string.

        Searches with the namespace first (foundationals loaded into this
        namespace's graphs), then falls back to a global search (foundationals
        stored under their own ontology URI as graph name).
        """
        vertex = self.graph.get_vertex(entity_uri, namespace=self.namespace)
        if vertex and vertex.get("comments"):
            return vertex["comments"][0]
        # Fallback: search without namespace restriction (for classes stored
        # under their ontology URI rather than the namespace prefix)
        vertex = self.graph.get_vertex(entity_uri, namespace=None)
        if vertex and vertex.get("comments"):
            return vertex["comments"][0]
        return ""

    # Ontology ops -----------------------------------------------------

    def create_ontology(self, data):
        """Create an ontology, returning the existing one if the URI is taken.

        Args:
            data: Ontology attributes; must include a ``uri`` key.

        Returns:
            The existing ontology when one already has that URI, otherwise the
            newly created ontology.
        """
        existing = self.graph.get_ontology_by_uri(data["uri"])
        if existing:
            return existing
        return self.graph.create_ontology(data)

    def get_ontology_by_uri(self, uri):
        """Fetch an ontology by URI, raising if it does not exist.

        Args:
            uri: The ontology URI to look up.

        Returns:
            The matching ontology.

        Raises:
            ValueError: If no ontology has that URI.
        """
        o = self.graph.get_ontology_by_uri(uri)
        if o is None:
            raise ValueError(f"Ontology with URI '{uri}' not found")
        return o

    def upload_ontology_file(self, ontology_id, content, filename="ontology.ttl"):
        """Accept an ontology file upload (no-op in the store-backed adapter).

        Args:
            ontology_id: ID of the ontology the file belongs to.
            content: Raw file bytes (logged by length, not persisted here).
            filename: Suggested filename; unused by this backend.

        Returns:
            A dict echoing the ontology id with a ``None`` file path.
        """
        # Neither backend exposes a file upload endpoint yet; proposals
        # are stored in DynamoDB and ingested via store_class/store_property.
        log.info("upload_ontology_file: no-op in store-backed adapter (%d bytes)", len(content))
        return {"id": ontology_id, "file_path": None}
