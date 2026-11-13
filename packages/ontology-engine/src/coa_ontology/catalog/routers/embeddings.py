# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Embedding CRUD + k-NN search router.

Routes go through the factory-built :class:`VectorStore` so the backend
selected by ``WORKBENCH_BACKEND`` (default ``opensearch_neptune``) is
honoured. Per-request ``namespace`` is threaded through both via a
namespace-bound store (:func:`build_stores`) and via per-call
``namespace`` kwargs (the vector protocol supports explicit per-call
namespace that overrides the bound one).
"""

from dataclasses import asdict

from fastapi import APIRouter

from coa_ontology.catalog.schemas import (
    EmbeddingBatchCreate,
    EmbeddingCreate,
    EmbeddingQuery,
    EmbeddingResponse,
    EmbeddingWithVectorResponse,
)
from coa_ontology.stores import build_stores
from coa_ontology.stores.base import VectorStore

router = APIRouter()


def _vec(namespace: str | None) -> VectorStore:
    """Return the namespace-bound vector store for the active backend."""
    return build_stores(namespace)[1]


def _emb_response(e: dict) -> EmbeddingResponse:
    return EmbeddingResponse(
        id=e.get("node_id") or e.get("id") or "",
        ontology_id=e.get("ontology_id", ""),
        entity_uri=e.get("entity_uri", ""),
        entity_type=e.get("entity_type", ""),
        embedding_type=e.get("embedding_type", ""),
        model_id=e.get("model_id", ""),
        dimensions=e.get("dimensions", len(e.get("vector") or e.get("embedding") or [])),
        text=e.get("text"),
        created_at=e.get("created_at", ""),
    )


def _matches_model(e: dict, model_id: str | None) -> bool:
    return model_id is None or e.get("model_id") == model_id


@router.post("/", response_model=EmbeddingResponse, status_code=201)
def store_embedding(body: EmbeddingCreate, namespace: str = "default"):
    """Store a single entity embedding in the namespace's vector store."""
    payload = {**body.model_dump(), "namespace": namespace}
    result = _vec(namespace).store_embedding(payload)
    # store_embedding returns the stored doc; preserve caller-provided dims
    result.setdefault("dimensions", body.dimensions)
    return _emb_response(result)


@router.post("/batch", response_model=list[EmbeddingResponse], status_code=201)
def store_embeddings_batch(body: EmbeddingBatchCreate, namespace: str = "default"):
    """Store a batch of entity embeddings in the namespace's vector store."""
    items = [{**e.model_dump(), "namespace": namespace} for e in body.embeddings]
    results = _vec(namespace).store_embeddings_batch(items)
    # Preserve caller-provided dimensions per item (stored doc may omit)
    for src, out in zip(body.embeddings, results, strict=False):
        out.setdefault("dimensions", src.dimensions)
    return [_emb_response(r) for r in results]


@router.get("/by-entity/{entity_uri:path}", response_model=list[EmbeddingWithVectorResponse])
def get_embeddings_for_entity(
    entity_uri: str,
    entity_type: str | None = None,
    embedding_type: str | None = None,
    model_id: str | None = None,
    namespace: str = "default",
):
    """Return all embeddings (with vectors) for a given entity URI."""
    rows = _vec(namespace).get_embeddings_for_entity(
        entity_uri=entity_uri,
        entity_type=entity_type,
        embedding_type=embedding_type,
        namespace=namespace,
    )
    return [
        EmbeddingWithVectorResponse(
            id=r.get("node_id") or r.get("id") or "",
            ontology_id=r.get("ontology_id", ""),
            entity_uri=r.get("entity_uri", entity_uri),
            entity_type=r.get("entity_type", ""),
            embedding_type=r.get("embedding_type", ""),
            model_id=r.get("model_id", ""),
            dimensions=r.get("dimensions", len(r.get("vector") or r.get("embedding") or [])),
            created_at=r.get("created_at", ""),
            vector=r.get("vector") or r.get("embedding") or [],
        )
        for r in rows
        if _matches_model(r, model_id)
    ]


@router.get("/by-ontology/{ontology_id:path}", response_model=list[EmbeddingResponse])
def list_embeddings_for_ontology(
    ontology_id: str,
    entity_type: str | None = None,
    embedding_type: str | None = None,
    model_id: str | None = None,
    skip: int = 0,
    limit: int = 100,
    namespace: str = "default",
):
    """List embeddings for an ontology, filtered and paginated."""
    rows = _vec(namespace).list_embeddings_for_ontology(
        ontology_id,
        entity_type=entity_type,
        embedding_type=embedding_type,
        namespace=namespace,
    )
    rows = [r for r in rows if _matches_model(r, model_id)]
    return [_emb_response(r) for r in rows[skip : skip + limit]]


@router.post("/search", response_model=list[EmbeddingWithVectorResponse])
def search_nearest(body: EmbeddingQuery, namespace: str = "default"):
    """Run a k-nearest-neighbor vector search over the namespace's embeddings."""
    hits = _vec(namespace).search_nearest(
        vector=body.vector,
        embedding_type=body.embedding_type,
        model_id=body.model_id,
        entity_type=body.entity_type,
        ontology_id=body.ontology_id,
        top_k=body.top_k,
        namespace=namespace,
    )
    out: list[EmbeddingWithVectorResponse] = []
    for h in hits:
        d = asdict(h)
        vec = d.get("vector") or []
        dims = len(vec) or len(body.vector)
        out.append(
            EmbeddingWithVectorResponse(
                id="",  # vector hits don't carry a stable external id
                ontology_id=d.get("ontology_id") or "",
                entity_uri=d.get("entity_uri") or "",
                entity_type=d.get("entity_type") or body.entity_type,
                embedding_type=d.get("embedding_type") or body.embedding_type,
                model_id=d.get("model_id") or body.model_id,
                dimensions=dims,
                created_at="",
                vector=vec[:dims],
            )
        )
    return out
