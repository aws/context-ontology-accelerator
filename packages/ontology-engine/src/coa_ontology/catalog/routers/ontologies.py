# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology catalog CRUD router.

Routes go through the factory-built :class:`GraphStore` so the backend
selected by ``WORKBENCH_BACKEND`` (default ``opensearch_neptune``) is
honoured. Per-request ``namespace`` is threaded through by fetching a
namespace-bound store from :func:`build_stores` (cached).

Two listings exist:

  - ``list_ontologies`` (``GET /ontologies``) — authoritative: reads the
    per-namespace DynamoDB registry written by the ingest pipeline.
  - ``get_ontology`` (``GET /ontologies/{id}``) — prefers the registry
    row (which carries catalog metadata, graph_uri, and embedding
    bookkeeping) and falls back to the graph store for rows created
    before the registry existed.

``POST /ontologies/{ontology_id:path}/upload`` is the single file-ingest
entry point. It creates the ontology registry entry when it doesn't
exist and merges the uploaded content into it when it does (graph
triples accumulate via SPARQL set semantics; the registry row is
refreshed with the latest counts). The upload paths delegate to
:func:`coa_ontology.catalog.ingest.ingest_ontology_and_wait`
(ingest + embedding-readiness wait); the underlying ``ingest_ontology``
also powers ``POST /ontology/proposals/{id}/accept``.
"""

import contextlib
import hashlib
import ipaddress
import logging
import os
import re
import socket
import urllib.parse
from datetime import UTC, datetime
from threading import Thread

# Request/response bodies come from the Smithy-generated control-plane models
# (single source of truth for the wire contract) rather than hand-rolled
# duplicates — they already carry camelCase aliases + populate_by_name.
from coa_control_plane_server.models.get_ingest_status_response_content import (
    GetIngestStatusResponseContent,
)
from coa_control_plane_server.models.get_ontology_upload_url_request_content import (
    GetOntologyUploadUrlRequestContent,
)
from coa_control_plane_server.models.get_ontology_upload_url_response_content import (
    GetOntologyUploadUrlResponseContent,
)
from coa_control_plane_server.models.ingest_job import IngestJob
from coa_control_plane_server.models.ingest_job_state import IngestJobState
from coa_control_plane_server.models.ingest_ontology_from_s3_request_content import (
    IngestOntologyFromS3RequestContent,
)
from coa_control_plane_server.models.ingest_ontology_from_s3_response_content import (
    IngestOntologyFromS3ResponseContent,
)
from coa_control_plane_server.models.list_ontologies_response_content import (
    ListOntologiesResponseContent,
)
from coa_control_plane_server.models.ontology_record import (
    OntologyRecord as _OntologyRecord,
)
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from coa_ontology import dynamo_store
from coa_ontology.catalog.ingest import (
    ONTOLOGY_STATUS_INGESTING,
    IngestConflictError,
    IngestMetadata,
    IngestParseError,
    IngestStoreError,
    IngestValidationError,
    ingest_ontology_and_wait,
)
from coa_ontology.catalog.schemas import (
    OntologyCreate,
    OntologyFetchRequest,
    OntologyFetchResponse,
    OntologyResponse,
    OntologyUpdate,
    PropertyExtract,
    UnresolvedReference,
)
from coa_ontology.catalog.turtle_export import format_grouped_turtle
from coa_ontology.stores import build_stores as _build_stores  # noqa: F401 (re-exported)
from coa_ontology.stores.base import GraphStore, VectorStore

router = APIRouter()
log = logging.getLogger(__name__)


ONTOLOGY_STORAGE_PATH = os.getenv("ONTOLOGY_STORAGE_PATH", "./data/ontologies")


def _is_within_storage_root(candidate: str) -> bool:
    """True when *candidate* resolves to a path inside ``ONTOLOGY_STORAGE_PATH``.

    Compares path components via ``os.path.commonpath`` rather than a string
    prefix: ``startswith`` would accept a sibling directory whose name merely
    begins with the root (``/data/ontologies-evil`` for a root of
    ``/data/ontologies``), which a symlink inside the root can resolve to.
    """
    root = os.path.realpath(ONTOLOGY_STORAGE_PATH)
    real = os.path.realpath(candidate)
    if real == root:
        return True
    try:
        return os.path.commonpath([root, real]) == root
    except ValueError:
        # Mixed absolute/relative or different drives — cannot be inside.
        return False


def _stores(namespace: str | None) -> tuple[GraphStore, VectorStore]:
    # Lazy import so unit tests that patch
    # ``coa_ontology.stores.build_stores`` after this module
    # is already loaded still see the patched function.
    from coa_ontology.stores import build_stores

    return build_stores(namespace)


def _graph(namespace: str | None) -> GraphStore:
    """Return the namespace-bound graph store for the active backend."""
    return _stores(namespace)[0]


# Defaults for response fields not populated by every source.
_RESPONSE_DEFAULTS: dict = {
    "ontology_type": None,
    "title": "",
    "description": None,
    "version": None,
    "version_iri": None,
    "license": None,
    "license_tier": None,
    "source_registry": None,
    "source_url": None,
    "format": None,
    "class_count": None,
    "property_count": None,
    "axiom_count": None,
    "last_fetched": None,
    "last_modified_upstream": None,
    "parse_status": None,
    "domain_tags": [],
    "imports": [],
    "status": None,
    "delete_error": None,
    "created_at": None,
    "updated_at": None,
    "graph_uri": None,
    "embedding_index": None,
    "embedding_model_id": None,
    "embedding_count": None,
    "source": None,
}


def _to_response(ont: dict) -> OntologyResponse:
    merged = {**_RESPONSE_DEFAULTS, **{k: v for k, v in ont.items() if k not in ("PK", "SK")}}
    merged.setdefault("ontology_id", ont.get("ontology_id") or ont.get("id") or ont.get("uri", ""))
    if not merged.get("uri"):
        merged["uri"] = merged["ontology_id"]
    return OntologyResponse(**merged)


def _to_record(ont: dict) -> _OntologyRecord:
    """Adapt a registry row to the Smithy-declared ``OntologyRecord``.

    ``list_ontologies`` serialises the generated model (``by_alias``) so the wire
    matches the contract in ``models/src/main/smithy/ontology-graph.smithy``, and
    the generated TS client can actually deserialise it. Reuses ``_to_response``
    for the merge/defaulting, then drops down to the contract's field subset —
    the generated model ignores extras, so registry-only bookkeeping fields
    (``imports``, ``source``, ``embedding_index``, …) fall away here rather than
    leaking into a declared response shape that has no place for them.
    """
    return _OntologyRecord.model_validate(_to_response(ont).model_dump())


def _merge_registry_and_graph(registry_row: dict | None, graph_row: dict | None) -> dict | None:
    """Merge a Dynamo registry row with a graph-store row.

    Registry wins for bookkeeping fields (graph_uri, embedding_*, counts,
    timestamps); graph store wins for schema-derived edges (imports list)
    which the registry stores verbatim but the graph store keeps live.
    """
    if not registry_row and not graph_row:
        return None
    merged: dict = {}
    if graph_row:
        merged.update(graph_row)
    if registry_row:
        merged.update({k: v for k, v in registry_row.items() if v is not None and k not in ("PK", "SK")})
    return merged


@router.get("/", response_model=ListOntologiesResponseContent, response_model_by_alias=True)
def list_ontologies(
    ontology_type: str | None = None,
    uri: str | None = None,
    source_registry: str | None = None,
    license_tier: str | None = None,
    domain_tag: str | None = None,
    skip: int = 0,
    limit: int = 50,
    namespace: str = "default",
):
    """List ontologies from the DynamoDB namespace registry."""
    rows = dynamo_store.list_ontologies_registry(
        namespace=namespace,
        ontology_type=ontology_type,
        domain_tag=domain_tag,
        limit=(skip + limit) if limit else None,
    )
    # Additional filters that aren't natively supported by the DDB query
    if uri:
        rows = [r for r in rows if r.get("uri") == uri]
    if source_registry:
        rows = [r for r in rows if r.get("source_registry") == source_registry]
    if license_tier:
        rows = [r for r in rows if r.get("license_tier") == license_tier]
    return ListOntologiesResponseContent(ontologies=[_to_record(r) for r in rows[skip : skip + limit]])


_DOWNLOAD_URL_TTL_S = 3600


@router.get("/{ontology_id:path}/download")
def download_ontology_file(ontology_id: str, namespace: str = "default"):
    """Download the ontology serialized as Turtle (served via a presigned S3 URL).

    The graph store is the source of truth: we ask the backend for the
    contents of the ontology's named graph (``GET /sparql/gsp?graph=…``
    on Neptune DB) and return whatever it serializes — which reflects
    every triple currently in the graph, regardless of where it came
    from (induction, upload, manual SPARQL UPDATE).

    A local-file fallback covers two cases that the graph backend does
    not handle: ``Neptune Analytics`` (no native Turtle export) and
    legacy ontologies whose ingest predated the named-graph layout.
    """
    namespace_ok = True
    graph_turtle: str | None = None
    try:
        graph_turtle = _graph(namespace).get_graph_turtle(ontology_id)
    except Exception as e:  # graph backend unconfigured / unreachable
        namespace_ok = False
        log.warning(
            "graph fetch for %s in namespace %s failed: %s — falling back to local file",
            ontology_id,
            namespace,
            e,
        )

    if graph_turtle is not None and graph_turtle.strip():
        try:
            body = format_grouped_turtle(graph_turtle)
        except Exception as e:
            log.warning(
                "turtle reformat for %s in %s failed: %s — serving raw",
                ontology_id,
                namespace,
                e,
            )
            body = graph_turtle
        log.info(
            "download: offloading graph-backed turtle (%d bytes raw → %d bytes grouped) for %s in %s",
            len(graph_turtle),
            len(body),
            ontology_id,
            namespace,
        )
        download_url = dynamo_store.write_and_presign_ontology_download(namespace, ontology_id, body)
        return {"downloadUrl": download_url, "expiresInSeconds": _DOWNLOAD_URL_TTL_S}

    # Fallback: legacy local file under data/ontologies/. Used when the
    # graph backend does not support raw Turtle export, when the named
    # graph is empty, or when the graph backend is unreachable.
    reg = dynamo_store.get_ontology_registry(namespace, ontology_id)
    fp = (reg or {}).get("file_path")
    if not fp:
        ont = _graph(namespace).get_ontology(ontology_id) if namespace_ok else None
        if not ont:
            raise HTTPException(
                404,
                f"Ontology '{ontology_id}' has no content in namespace '{namespace}'. "
                "The graph contains no triples and no cached file is available.",
            )
        fp = ont.get("file_path")
    if not fp or not os.path.exists(fp):
        raise HTTPException(
            404,
            f"No content available for ontology {ontology_id}. "
            "The graph is empty for this ontology and no cached file exists; "
            "upload an ontology file or re-run induction.",
        )
    # SECURITY: ensure file path is within the allowed storage directory
    real_fp = os.path.realpath(fp)
    if not _is_within_storage_root(real_fp):
        raise HTTPException(403, "Access denied: file path outside storage boundary")
    log.info("download: offloading local-file fallback %s for %s in %s", fp, ontology_id, namespace)
    with open(real_fp, encoding="utf-8") as fh:
        body = fh.read()
    download_url = dynamo_store.write_and_presign_ontology_download(namespace, ontology_id, body)
    return {"downloadUrl": download_url, "expiresInSeconds": _DOWNLOAD_URL_TTL_S}


@router.get("/{ontology_id:path}/ingest-status/{job_id}")
def get_ingest_status(ontology_id: str, job_id: str, namespace: str = "default"):
    """Poll ingest job status.

    Declared BEFORE the greedy ``GET /{ontology_id:path}`` catch-all below:
    Starlette matches routes in declaration order and the ``:path`` converter
    spans slashes, so if the catch-all came first it would swallow
    ``/{id}/ingest-status/{job}`` and 404 on the registry lookup.
    """
    job = dynamo_store.get_ingest_job(job_id, namespace=namespace)
    if not job or job.get("ontology_id") != ontology_id:
        raise HTTPException(404, "Ingest job not found")
    # Build the typed IngestJob from the snake_case DDB item (populate_by_name)
    # and let FastAPI serialize it camelCase to match the Smithy wire contract.
    return GetIngestStatusResponseContent(result=IngestJob.model_validate(job))


@router.get("/{ontology_id:path}", response_model=OntologyResponse)
def get_ontology(ontology_id: str, namespace: str = "default"):
    """Fetch one ontology, merging its registry metadata and graph-store row.

    Args:
        ontology_id: Identifier (or URI) of the ontology to fetch.
        namespace: The namespace the ontology lives in.

    Returns:
        The merged ontology representation.

    Raises:
        HTTPException: 404 when the ontology exists in neither the registry nor the graph.
    """
    reg = dynamo_store.get_ontology_registry(namespace, ontology_id)
    graph_row = _graph(namespace).get_ontology(ontology_id)
    merged = _merge_registry_and_graph(reg, graph_row)
    if not merged:
        raise HTTPException(404, "Ontology not found")
    return _to_response(merged)


@router.post("/", response_model=OntologyResponse, status_code=201)
def create_ontology(body: OntologyCreate, namespace: str = "default"):
    """Register metadata only (no content). For full ingests use ``/upload``."""
    graph = _graph(namespace)
    if graph.get_ontology_by_uri(body.uri) or dynamo_store.get_ontology_registry(namespace, body.uri):
        raise HTTPException(409, f"Ontology with URI '{body.uri}' already exists")
    ont = graph.create_ontology(body.model_dump())
    # Seed a minimal registry row so ``list_ontologies`` sees it.
    reg = dynamo_store.put_ontology_registry(
        namespace,
        body.uri,
        {
            "uri": body.uri,
            "title": body.title,
            "description": body.description,
            "ontology_type": body.ontology_type,
            "version": body.version,
            "version_iri": body.version_iri,
            "license": body.license,
            "license_tier": body.license_tier,
            "source_registry": body.source_registry,
            "source_url": body.source_url,
            "format": body.format,
            "domain_tags": body.domain_tags or [],
            "imports": body.imports or [],
            "source": "metadata-only",
            "parse_status": "pending",
        },
    )
    return _to_response(_merge_registry_and_graph(reg, ont) or ont)


@router.patch("/{ontology_id:path}", response_model=OntologyResponse)
def update_ontology(ontology_id: str, body: OntologyUpdate, namespace: str = "default"):
    """Apply a partial update to an ontology's registry and graph-store rows.

    Args:
        ontology_id: Identifier (or URI) of the ontology to update.
        body: The fields to update (only set fields are applied).
        namespace: The namespace the ontology lives in.

    Returns:
        The merged, updated ontology representation.

    Raises:
        HTTPException: 404 when the ontology exists in neither store.
    """
    graph = _graph(namespace)
    existed_in_graph = bool(graph.get_ontology(ontology_id))
    existed_in_registry = bool(dynamo_store.get_ontology_registry(namespace, ontology_id))
    if not existed_in_graph and not existed_in_registry:
        raise HTTPException(404, "Ontology not found")
    updates = body.model_dump(exclude_unset=True)
    graph_row = graph.update_ontology(ontology_id, updates) if existed_in_graph else None
    reg_row = dynamo_store.update_ontology_registry(namespace, ontology_id, **updates) if existed_in_registry else None
    merged = _merge_registry_and_graph(reg_row, graph_row)
    if not merged:
        raise HTTPException(404, "Ontology not found after update")
    return _to_response(merged)


def _proposal_ids_to_delete_with_ontology(namespace: str, ontology_id: str, registry_row: dict | None) -> list[str]:
    """Collect proposals to delete when an ontology is deleted.

    Unions three sources so nothing is left dangling:
      1. The registry row's ``source_proposals`` list — the authoritative
         record of every proposal merged into this ontology's named graph.
      2. A ``list_proposals`` scan filtered by ``ontology_id`` + accepted
         status — catches proposals that fed the ontology but predate (or were
         missed by) the ``source_proposals`` bookkeeping.
      3. Proposals grounded AGAINST this ontology — any proposal whose
         ``metadata.grounding_ontologies_used`` lists ``ontology_id``. Once the
         ontology is gone their grounding references a target that no longer
         exists, so they're removed too.
    """
    ids: set[str] = set()
    for pid in (registry_row or {}).get("source_proposals", []) or []:
        if pid:
            ids.add(pid)
    try:
        for prop in dynamo_store.list_proposals(
            namespace=namespace, ontology_id=ontology_id, status="accepted", limit=500
        ):
            pid = prop.get("proposal_id")
            if pid:
                ids.add(pid)
    except Exception:
        # Scan failure means we fall back to the registry's source_proposals
        # list only — log so a partial collection (and any resulting orphaned
        # proposal rows) is visible to ops rather than silently swallowed.
        log.warning(
            "Proposal scan failed for ontology %s in %s — using registry source_proposals only",
            ontology_id,
            namespace,
            exc_info=True,
        )
    # (3) Proposals grounded against this ontology. list_proposals can't filter
    # on nested metadata, so scan ALL the namespace's proposals (limit=None so
    # a namespace with many proposals can't silently leave grounded ones behind)
    # and match on ``metadata.grounding_ontologies_used`` (a top-level metadata
    # list of the ontology ids a proposal grounded its matches against).
    try:
        for prop in dynamo_store.list_proposals(namespace=namespace, limit=None):
            grounded = ((prop.get("metadata") or {}).get("grounding_ontologies_used")) or []
            if ontology_id in grounded:
                pid = prop.get("proposal_id")
                if pid:
                    ids.add(pid)
    except Exception:
        log.warning(
            "Grounded-proposal scan failed for ontology %s in %s — grounded proposals may be left behind",
            ontology_id,
            namespace,
            exc_info=True,
        )
    return sorted(ids)


# Registry status set synchronously the moment a single-ontology delete starts.
# The row stays visible (GET /ontologies lists it with this status) until the
# async teardown finishes, at which point the row itself is removed.
ONTOLOGY_STATUS_DELETING = "deleting"

# ontology_type value for ontologies produced by induction (vs. user-loaded
# ``user_created`` / ``user_uploaded`` or curated ``foundational``).
_INDUCED_ONTOLOGY_TYPE = "induced"


def _induced_ontologies_present(namespace: str, exclude_ontology_id: str | None = None) -> list[dict]:
    """Induced ontology rows in the namespace (optionally excluding one id).

    Deleting a non-induced ontology while induced ontologies exist is refused:
    induced ontologies are derived from — and grounded against — the user-loaded
    and foundational ontologies in the namespace, so removing one of those out
    from under a live induced ontology would leave it dangling. ``exclude_ontology_id``
    lets the caller ignore the row being deleted (an induced ontology deleting
    itself is always allowed).
    """
    rows = dynamo_store.list_ontologies_registry(namespace, ontology_type=_INDUCED_ONTOLOGY_TYPE)
    if exclude_ontology_id:
        rows = [r for r in rows if r.get("ontology_id") != exclude_ontology_id]
    return rows


def _mark_delete_error(namespace: str, ontology_id: str, error: str) -> None:
    """Record why an async delete's teardown failed, on the registry row.

    Mirrors the accept flow's ``accept_error``: a row left in ``deleting`` is
    ambiguous (in-progress vs wedged), so on a hard-gate failure we stamp
    ``delete_error`` so the UI/ops can tell a stuck delete apart from one still
    running and offer a "retry delete". Best-effort + truncated: a large error
    (e.g. one embedding vectors in it) must not itself blow the 400 KB DDB item
    limit and fail this write. A cleared ``delete_error`` (see the route) means
    a fresh delete attempt is underway.
    """
    with contextlib.suppress(Exception):
        dynamo_store.update_ontology_registry(namespace, ontology_id, delete_error=str(error)[:1000])


def _delete_ontology_backend(namespace: str, ontology_id: str, proposal_ids: list[str] | None = None) -> None:
    """Tear down an ontology asynchronously, then remove its registry row.

    Runs in a daemon thread after the route has flipped the registry row to
    ``status="deleting"`` (the client-visible status — GET /ontologies keeps
    listing the ontology as "Delete in progress" while this runs). Order:
    Neptune named-graph DROP → AOSS embedding teardown (wait until the deleted
    vectors are no longer searchable) → proposal cleanup → **delete the
    registry row last**, so the ontology only disappears
    from the catalog once its graph + embeddings are actually gone. This also
    avoids the API Gateway 29s timeout (504) on large ontologies.

    Failure handling — the registry row is the recovery anchor. The graph DROP
    and embedding teardown are **hard gates**: if either raises (Neptune/AOSS
    throttling, IAM, connectivity), we log and RETURN WITHOUT deleting the
    registry row, so the ontology stays in ``status="deleting"`` rather than
    vanishing from the catalog while orphaned triples/vectors remain. A
    re-issued DELETE is idempotent and retries the teardown from the top.

    ECS restart caveat: this runs in a daemon thread, so a task restart (normal
    during a deploy) mid-teardown kills it before the registry row is removed —
    same recoverable outcome as a hard-gate failure: the row stays ``deleting``
    and a re-issued DELETE recovers it. (Per the accepted design; a durable
    job queue would remove even this window but isn't warranted here.)
    """
    graph_store, vector_store = _stores(namespace)

    # Hard gate 1: Neptune named-graph DROP. If this fails, abort so the
    # registry row is NOT removed — otherwise the ontology disappears from the
    # UI while its triples linger (silent data corruption / orphaned quads).
    try:
        graph_store.delete_ontology(ontology_id)
    except Exception as e:
        log.error(
            "Neptune graph delete failed for ontology %s in %s — leaving row in 'deleting' "
            "for retry (registry row NOT removed to avoid orphaned triples)",
            ontology_id,
            namespace,
            exc_info=True,
        )
        _mark_delete_error(namespace, ontology_id, f"Graph delete failed: {type(e).__name__}: {e}")
        return

    # Hard gate 2: AOSS embedding teardown. Same rationale — leaving embeddings
    # behind causes stale search results + storage/quota bleed, so don't remove
    # the registry row until they're gone. Capture the exact searchable URIs
    # BEFORE deleting so we can wait until every one of them is no longer
    # returned by the (eventually-consistent) vector index — otherwise a query
    # issued right after the delete could still surface the deleted vectors.
    try:
        from coa_ontology.catalog.ingest import _is_index_not_found, wait_for_embeddings_deleted

        # Snapshot what's searchable before delete so we can wait for exactly
        # those URIs to disappear. If the index doesn't exist, there's nothing
        # to delete or wait on — the embeddings are already gone.
        try:
            deleted_uris = vector_store.list_searchable_entity_uris(ontology_id, namespace=namespace)
        except Exception as probe_err:  # noqa: BLE001
            if _is_index_not_found(probe_err):
                deleted_uris = []
            else:
                raise
        vector_store.delete_embeddings_for_ontology(ontology_id, namespace=namespace)
        wait_for_embeddings_deleted(
            vector_store=vector_store,
            ontology_id=ontology_id,
            namespace=namespace,
            deleted_uris=deleted_uris,
        )
    except Exception as e:
        log.error(
            "AOSS embedding delete failed for ontology %s in %s — leaving row in 'deleting' "
            "for retry (registry row NOT removed to avoid orphaned embeddings)",
            ontology_id,
            namespace,
            exc_info=True,
        )
        _mark_delete_error(namespace, ontology_id, f"Embedding delete failed: {type(e).__name__}: {e}")
        return

    # Delete the proposals (DDB rows + S3 artifacts) tied to this ontology —
    # both the ones that produced it AND the ones grounded against it — so they
    # don't linger in the proposals catalog pointing at a graph that no longer
    # exists. Non-fatal: a leftover proposal row is a minor inconsistency (not
    # orphaned graph data), so log and continue to removing the registry row
    # rather than blocking the whole delete on it.
    for pid in proposal_ids or []:
        try:
            dynamo_store.delete_proposal(namespace, pid)
        except Exception:
            log.warning(
                "Proposal delete failed for %s (ontology %s in %s) — continuing",
                pid,
                ontology_id,
                namespace,
                exc_info=True,
            )

    # Graph + embeddings are gone — now drop the registry row so the ontology
    # disappears from the catalog. On failure the row stays in 'deleting'
    # (recoverable by re-issuing DELETE); log so it's visible to ops.
    try:
        dynamo_store.delete_ontology_registry(namespace, ontology_id)
    except Exception:
        log.error(
            "Registry row delete failed for ontology %s in %s — row stuck in 'deleting'; re-issue DELETE to recover",
            ontology_id,
            namespace,
            exc_info=True,
        )


@router.delete("/", status_code=200)
def delete_ontologies(namespace: str = "default", ontology_id: str | None = None):
    """Delete ontology data for a namespace.

    Handles two modes on the same ``DELETE /ontologies`` route: a single-ontology
    delete when ``ontology_id`` is supplied, and a whole-namespace wipe when it
    is omitted (hence the plural name).

    When ``ontology_id`` is supplied (query param), delete only that one ontology
    and return **202**: the registry row is flipped to ``status="deleting"``
    synchronously (so GET /ontologies keeps listing it as "Delete in progress" —
    the client-visible status), then the slower Neptune graph DROP + AOSS
    embedding teardown + accepted-proposal cleanup run in a background thread,
    which **removes the registry row last**. The ontology therefore stays
    visible until its graph + embeddings are actually clean. This mirrors the
    async accept flow and avoids the API Gateway 29s timeout (504) on large
    ontologies. The id is a full IRI carrying ``#``, so it comes in as a query
    param — API Gateway can't route an encoded ``#`` in a path segment. Clients
    poll GET /ontologies until the id is gone.

    Deleting an ontology also deletes the proposals tied to it — both those
    that fed into it and those grounded against it
    (their DynamoDB rows + S3 artifacts), so the proposals catalog doesn't keep
    entries pointing at a graph that no longer exists.

    When ``ontology_id`` is omitted, delete ALL ontology data for the namespace
    (named graphs, the AOSS vector index, and all DynamoDB records).
    """
    graph_store, vector_store = _stores(namespace)

    # Single-ontology delete (query param present): mark the row "deleting" now
    # (keeps it visible as "Delete in progress"), then finish graph + embeddings
    # + proposals async and drop the row last. Collect the source proposals here
    # since the row carries the authoritative ``source_proposals`` list.
    if ontology_id:
        registry_row = dynamo_store.get_ontology_registry(namespace, ontology_id)
        graph_exists = graph_store.get_ontology_by_uri(ontology_id) is not None
        if not registry_row and not graph_exists:
            raise HTTPException(404, "Ontology not found")
        # Guard: while any induced ontology exists, refuse to delete a NON-induced
        # (user-loaded / foundational) ontology — an induced ontology is derived
        # from and grounded against those, so removing one out from under it would
        # leave the induced ontology dangling. Deleting an induced ontology itself
        # stays allowed (it excludes itself from the check), otherwise induced
        # ontologies could never be removed. Enforces the rule for direct API
        # callers regardless of the UI.
        ontology_type = (registry_row or {}).get("ontology_type")
        if ontology_type != _INDUCED_ONTOLOGY_TYPE:
            induced = _induced_ontologies_present(namespace, exclude_ontology_id=ontology_id)
            if induced:
                induced_ids = sorted({r.get("ontology_id", "") for r in induced if r.get("ontology_id")})
                log.info(
                    "Refusing delete of ontology %s in %s — %d induced ontology(ies) present: %s",
                    ontology_id,
                    namespace,
                    len(induced),
                    induced_ids,
                )
                raise HTTPException(
                    409,
                    "Cannot delete this ontology while induced ontologies exist in the namespace. "
                    "Delete the induced ontologies first, then retry.",
                )
        proposal_ids = _proposal_ids_to_delete_with_ontology(namespace, ontology_id, registry_row)
        # Flip to "deleting" synchronously so the very next GET /ontologies
        # reflects the in-progress state. No-op if there's no registry row
        # (graph-only ontology) — the async teardown still runs. If the status
        # flip fails we abort with 500 rather than starting teardown: otherwise
        # the row would stay status=None and the ontology would vanish abruptly
        # at the end instead of showing "Delete in progress".
        if registry_row:
            try:
                # Clear any delete_error from a prior failed attempt so a
                # re-issued DELETE reads as "in progress", not "stuck".
                dynamo_store.update_ontology_registry(
                    namespace, ontology_id, status=ONTOLOGY_STATUS_DELETING, delete_error=""
                )
            except Exception as e:
                log.error(
                    "Failed to mark ontology %s in %s as deleting — aborting delete",
                    ontology_id,
                    namespace,
                    exc_info=True,
                )
                raise HTTPException(500, "Failed to mark ontology for deletion; please retry") from e
        Thread(
            target=_delete_ontology_backend,
            args=(namespace, ontology_id, proposal_ids),
            daemon=True,
        ).start()
        return Response(status_code=202)

    # 1. List all ontologies and drop their named graphs
    registry_rows = dynamo_store.list_ontologies_registry(namespace)
    graphs_dropped = 0
    for row in registry_rows:
        ontology_id = row.get("ontology_id", "")
        if ontology_id:
            with contextlib.suppress(Exception):
                if graph_store.delete_ontology(ontology_id):
                    graphs_dropped += 1

    # 2. Delete the entire AOSS vector index for the namespace.
    #
    # HARD GATE — deliberately NOT best-effort, unlike the graph drop above.
    # AOSS caps a collection at 1000 indexes and this store uses one index per
    # namespace, so a swallowed failure here does not degrade gracefully: the
    # caller (namespace deletion pipeline) goes on to delete the namespace
    # record, after which NOTHING can ever reclaim this index — its namespace
    # no longer exists, so no future teardown will ever target it again. Those
    # shells accumulate until every embedding write in the deployment fails
    # with `index_limit_breached`.
    #
    # Letting this raise turns an unrecoverable silent leak into a retryable
    # DELETE_FAILED, which is the same trade `_delete_ontology_backend` makes
    # for its AOSS teardown ("hard gate 2") and what finalize's docstring means
    # by DELETE_FAILED being preferable because it supports retry. delete_index
    # is idempotent (returns False when the index is already absent), so the
    # retry is safe.
    index_deleted = vector_store.delete_index(namespace=namespace)

    # 3. Purge all DDB items (registry + jobs + proposals)
    ddb_deleted = dynamo_store.delete_all_namespace_items(namespace)

    return {
        "namespace": namespace,
        "graphsDropped": graphs_dropped,
        "indexDeleted": index_deleted,
        "dynamoItemsDeleted": ddb_deleted,
    }


@router.delete("/{ontology_id:path}", status_code=204)
def delete_ontology(ontology_id: str, namespace: str = "default"):
    """Delete an ontology's registry row, named graph, and embeddings.

    Args:
        ontology_id: Identifier (or URI) of the ontology to delete.
        namespace: The namespace the ontology lives in.

    Raises:
        HTTPException: 404 when the ontology existed in neither the registry nor the graph.
    """
    graph_store, vector_store = _stores(namespace)
    removed_registry = dynamo_store.delete_ontology_registry(namespace, ontology_id)
    removed_graph = graph_store.delete_ontology(ontology_id)
    # Best-effort: clear embeddings so the shared namespace index doesn't
    # keep ghost vectors from deleted ontologies in the top-k pool.
    with contextlib.suppress(Exception):
        vector_store.delete_embeddings_for_ontology(ontology_id, namespace=namespace)
    if not removed_registry and not removed_graph:
        raise HTTPException(404, "Ontology not found")


@router.post("/{ontology_id:path}/upload", response_model=OntologyResponse)
def upload_ontology_file(
    ontology_id: str,
    file: UploadFile = File(...),  # noqa: B008
    namespace: str = Form("default"),
    title: str | None = Form(None),
    description: str | None = Form(None),
    ontology_type: str = Form("user_created"),
    version: str | None = Form(None),
    license: str | None = Form(None),
    license_tier: str | None = Form(None),
    source_registry: str | None = Form(None),
    source_url: str | None = Form(None),
    format: str | None = Form(None),
    domain_tags: str | None = Form(None),  # comma-separated
    validate_tier1: bool = Form(False),
):
    """Upload an ontology file into a namespace.

    One-shot ingest: registers the ontology in the Dynamo namespace
    registry, projects classes and properties into the per-ontology
    named graph, POSTs the raw Turtle (or auto-serialised Turtle for
    RDF-XML / JSON-LD) into that same graph, and indexes lexical
    embeddings into the shared per-namespace vector index.

    Single-upload semantics: attempting to upload the same
    ``ontology_id`` a second time returns ``409 Conflict``. To replace
    an ontology, ``DELETE /ontologies/{ontology_id}`` first and then
    re-upload.

    SECURITY — read before exposing this route. ``namespace`` is a ``Form``
    field, so it is read from the multipart **body** and is NOT bound to the
    Cedar-authorized ``{namespaceId}`` path parameter. This route is currently
    unreachable (absent from ``infra/bin/app.ts`` route wiring, from the
    authorizer's ``_PATH_MAPPING``, and from ``api_proxy_handler._PATH_MAP``),
    which is the only thing preventing a caller authorized on namespace A from
    writing into namespace B's storage dir, graph, and registry. Before wiring
    it, switch ``namespace`` to ``Query(...)`` — ``api_proxy_handler`` already
    overwrites ``query["namespace"]`` with the authorized ``namespaceId`` — or
    reject a body value that disagrees with the authorized one. The sibling
    ``/upload-url`` route takes ``namespace`` as a query param for exactly this
    reason.
    """
    # Fail fast on a duplicate upload before writing the file to disk,
    # so a repeat call doesn't leave an orphaned copy on the filesystem.
    if dynamo_store.get_ontology_registry(namespace, ontology_id):
        raise HTTPException(
            409,
            f"Ontology '{ontology_id}' already exists in namespace '{namespace}'. Delete it first to re-upload.",
        )

    content = file.file.read()
    file_dest = _persist_upload(
        namespace,
        ontology_id,
        content,
        format or "turtle",
    )

    md = IngestMetadata(
        ontology_id=ontology_id,
        title=title,
        description=description,
        ontology_type=ontology_type,
        version=version,
        license=license,
        license_tier=license_tier,
        source_registry=source_registry,
        source_url=source_url,
        format=format,
        domain_tags=[t.strip() for t in (domain_tags or "").split(",") if t.strip()] or None,
        file_path=file_dest,
        source="upload",
    )

    graph_store, vector_store = _stores(namespace)
    try:
        # Ingest + wait for the embeddings to be searchable in one call, so a
        # caller that immediately grounds/serves against this ontology doesn't
        # race the AOSS eventual-consistency window. This is a synchronous
        # handler bounded by API Gateway's 29s integration timeout; in practice
        # AOSS catches up in a few seconds for a small user-uploaded ontology,
        # and the wait is capped by INGEST_EMBEDDING_READY_TIMEOUT_S. Async /
        # large imports go through ingest-from-s3 instead.
        result = ingest_ontology_and_wait(
            graph_store=graph_store,
            vector_store=vector_store,
            content=content,
            namespace=namespace,
            metadata=md,
            validate=validate_tier1,
        )
    except IngestParseError as e:
        raise HTTPException(422, f"{e}") from e
    except IngestValidationError as e:
        raise HTTPException(422, {"message": str(e), "findings": [f.__dict__ for f in e.findings]}) from e
    except IngestConflictError as e:
        # Race between the pre-check above and the ingest call — the
        # registry row appeared in between. Same 409 response.
        raise HTTPException(409, f"{e}") from e
    except IngestStoreError as e:
        raise HTTPException(502, f"{e}") from e

    return _to_response(result["registry"])


_FETCH_ALLOWED_SCHEMES = {"https"}
_FETCH_MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # 64 MB cap on remote ontology size
_FETCH_TIMEOUT_SECS = 60


def _resolve_and_validate_url(url: str) -> tuple[str, str, str]:
    """Resolve hostname once, validate all IPs are public, return pinned URL and Host header.

    Defense-in-depth against SSRF including DNS rebinding: resolve the hostname
    once, verify every A/AAAA record points at a public IP, then return a URL
    that connects directly to the first validated IP. This eliminates the
    TOCTOU race where an attacker-controlled DNS server could serve different
    IPs for validation vs. the actual HTTP request.

    Returns:
        (pinned_url, original_hostname, port) — pinned_url uses the validated IP,
        original_hostname goes in the Host header, port for proper URL construction.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _FETCH_ALLOWED_SCHEMES:
        raise HTTPException(400, "source_url must use https://")
    if not parsed.hostname:
        raise HTTPException(400, "source_url is missing a hostname")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise HTTPException(400, f"Cannot resolve source_url host: {e}") from e
    if not infos:
        raise HTTPException(400, "source_url hostname resolved to no addresses")

    # Validate ALL returned IPs are globally routable unicast before using any.
    # `not is_global` rejects private, loopback, link-local (incl. IMDS
    # 169.254.169.254), CGNAT (100.64.0.0/10), unspecified (0.0.0.0), reserved,
    # and IPv4-mapped IPv6 variants (e.g. ::ffff:169.254.169.254) that an
    # explicit-flag enumeration can miss. `is_multicast` is checked separately
    # because multicast is NOT classified as private, so is_global is True for
    # it — we still must reject it.
    validated_ips: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast:
            raise HTTPException(400, f"source_url resolves to non-routable address {ip}")
        validated_ips.append(str(ip))

    # Use the first validated IP and reconstruct the URL to connect directly to it
    pinned_ip = validated_ips[0]
    port = parsed.port or 443
    # IPv6 addresses need bracket notation in URLs
    netloc = f"[{pinned_ip}]:{port}" if ":" in pinned_ip else f"{pinned_ip}:{port}"
    pinned_url = parsed._replace(netloc=netloc).geturl()

    return pinned_url, parsed.hostname, str(port)


@router.post("/{ontology_id:path}/fetch", response_model=OntologyFetchResponse)
def fetch_ontology_from_source(
    ontology_id: str,
    body: OntologyFetchRequest | None = None,
    namespace: str = "default",
):
    """Download the ontology content from its source URL and parse it.

    Behavior:
      - If body.source_url is provided, use it; else use the stored source_url
        on the ontology record.
      - httpx.get(source_url) with content negotiation (body.accept_formats
        or a sensible default list).
      - Parse with rdflib (format inferred from the stored ``format`` field
        or the response's Content-Type header).
      - Store classes/properties via the configured graph store so later
        induction runs can match against them.
      - Update the ontology record with class_count / property_count /
        parse_status / last_fetched.
      - Return a small summary (id, uri, parse_status, counts).

    Parse errors are swallowed — the ontology record still gets a
    parse_status='parse_error' so clients can see what happened.
    """
    graph = _graph(namespace)
    ont = graph.get_ontology(ontology_id)
    if not ont:
        raise HTTPException(404, "Ontology not found")

    # Resolve source URL: explicit override > stored record
    source_url = (body.source_url if body else None) or ont.get("source_url")
    if not source_url:
        raise HTTPException(400, "No source_url to fetch from (none provided, none stored)")

    # Content negotiation — caller can override, else use a turtle-preferring default
    accept_formats = (body.accept_formats if body else None) or [
        "text/turtle",
        "application/rdf+xml",
        "application/ld+json",
    ]
    headers = {"Accept": ", ".join(accept_formats)}

    import certifi
    import httpx as _httpx  # local to keep module import surface small
    from rdflib import OWL, RDF, RDFS, Graph

    # Resolve DNS once and pin to the validated IP to prevent DNS rebinding attacks
    pinned_url, original_hostname, port = _resolve_and_validate_url(source_url)
    # Add Host header so the target server knows which virtual host we're requesting
    headers = {**headers, "Host": original_hostname}

    try:
        # Stream the response so we can enforce a hard byte cap, and disable
        # redirects so a 3xx pointing back at the VPC can't bypass the
        # SSRF allowlist after the first hop. SSL verification stays ON against
        # the certifi trust store — all curated foundational sources (schema.org,
        # Dublin Core, FOAF, PROV-O, FIBO) present valid, certifi-trusted chains
        # (verified in-container), so the previous verify=False is unnecessary.
        with (
            _httpx.Client(follow_redirects=False, timeout=_FETCH_TIMEOUT_SECS, verify=certifi.where()) as client,
            # We connect to the pinned IP (pinned_url) to defeat DNS rebinding, but
            # TLS still needs the real hostname: sni_hostname drives both the SNI
            # sent in the handshake and the certificate hostname verification.
            # Without it, connecting to the bare IP fails the TLS handshake /
            # cert check against SNI-based hosts (the Host header alone is
            # HTTP-layer only and does NOT affect TLS).
            client.stream(
                "GET",
                pinned_url,
                headers=headers,
                extensions={"sni_hostname": original_hostname},
            ) as resp,
        ):
            if resp.status_code in (301, 302, 303, 307, 308):
                raise HTTPException(400, "Redirects are not allowed for source_url")
            resp.raise_for_status()
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > _FETCH_MAX_RESPONSE_BYTES:
                    raise HTTPException(
                        413,
                        f"source_url response exceeds {_FETCH_MAX_RESPONSE_BYTES} bytes",
                    )
            content = bytes(buf)
    except _httpx.HTTPError as e:
        log.warning("source_url_fetch_failed: url=%s error_type=%s", source_url, type(e).__name__)
        raise HTTPException(502, "Failed to fetch source_url: upstream request failed") from e

    # Cache for /download through the same namespace-scoped writer the upload
    # route uses. Writing to the shared root here (as this path used to) gave two
    # namespaces holding the same ontology id ONE file, so /download served
    # whichever fetched last. Format hint is the record's declared format — the
    # Content-Type sniff below runs after the write, and the extension is
    # cosmetic (``file_path`` is what /download reads).
    try:
        file_dest = _persist_upload(namespace, ontology_id, content, ont.get("format") or "turtle")
    except ValueError as e:
        # Which of the two inputs failed containment stays out of the response
        # (it is a traversal probe's only feedback channel) but goes to the log.
        log.warning("ontology_fetch_persist_rejected: namespace=%s id=%s reason=%s", namespace, ontology_id, e)
        raise HTTPException(400, "Invalid namespace or ontology_id") from e
    except OSError as e:
        # Disk full / permissions / read-only mount. Without this the bare
        # OSError becomes a 500 with no record of which namespace+id it was.
        log.error("ontology_fetch_persist_failed: namespace=%s id=%s error=%r", namespace, ontology_id, e)
        raise HTTPException(500, "Failed to persist fetched ontology content") from e

    updates: dict = {
        "file_path": file_dest,
        "last_fetched": datetime.now(UTC).isoformat(),
        "source_url": source_url,
    }

    # Pick parse format.
    # Prefer what the server actually sent (Content-Type header) over what the
    # stored ontology record claims — some servers do content negotiation
    # against our Accept header and return a different format than the one the
    # record was created with (FOAF returns application/ld+json when we ask
    # for it, even though its record says format=rdf-xml).
    rdflib_fmt = None
    ct = resp.headers.get("content-type", "").lower()
    if "ld+json" in ct or "json-ld" in ct:
        rdflib_fmt = "json-ld"
    elif "turtle" in ct:
        rdflib_fmt = "turtle"
    elif "rdf+xml" in ct or "owl+xml" in ct or "xml" in ct:
        rdflib_fmt = "xml"
    elif "n-triples" in ct or "ntriples" in ct:
        rdflib_fmt = "nt"

    if not rdflib_fmt:
        # Fall back to the stored format hint, else turtle as a last resort.
        fmt = (ont.get("format") or "").lower()
        fmt_map = {"rdf-xml": "xml", "owl-xml": "xml", "json-ld": "json-ld", "turtle": "turtle", "nt": "nt"}
        rdflib_fmt = fmt_map.get(fmt, "turtle")

    class_count = None
    property_count = None
    parse_status = "parse_error"

    g = Graph()
    try:
        g.parse(data=content, format=rdflib_fmt)

        # Classes: owl:Class OR rdfs:Class (Schema.org, Dublin Core, and other
        # pre-OWL / RDFS-only ontologies declare types via rdfs:Class).
        owl_classes = set(g.subjects(RDF.type, OWL.Class))
        rdfs_classes = set(g.subjects(RDF.type, RDFS.Class))
        classes = owl_classes | rdfs_classes

        # Properties: owl:ObjectProperty, owl:DatatypeProperty, rdf:Property,
        # owl:AnnotationProperty, owl:FunctionalProperty.
        prop_types = [
            OWL.DatatypeProperty,
            OWL.ObjectProperty,
            RDF.Property,
            OWL.AnnotationProperty,
            OWL.FunctionalProperty,
        ]
        props: set = set()
        for pt in prop_types:
            props |= set(g.subjects(RDF.type, pt))

        class_count = len(classes)
        property_count = len(props)
        parse_status = "ok"
        updates["class_count"] = class_count
        updates["property_count"] = property_count
        updates["axiom_count"] = len(g)

        # Optional: extract properties that reference the caller-supplied
        # class URIs via domain/range. This is the replacement for
        # sample_properties in config.yaml — the caller passes the class
        # URIs it already knows about, and the engine walks the rdflib
        # graph to find all properties linking to those classes.
        #
        # The original (pre-consolidation) setup.py did this client-side
        # in rdflib; moving it here keeps setup.py thin and lets us benefit
        # from the already-parsed graph in memory.
        if body is not None and (body.extract_properties_for_classes or body.extract_all_properties):
            from rdflib import URIRef

            # extract_all_properties wins over extract_properties_for_classes
            # (documented as mutually exclusive — this flag just turns the
            # domain/range filter off so every declared property is returned).
            extract_all = bool(body.extract_all_properties)
            target_uris = set() if extract_all else {URIRef(u) for u in (body.extract_properties_for_classes or [])}
            # Schema.org's property-domain/range vocabulary (not rdfs-based)
            schema_domain_preds = [
                URIRef("https://schema.org/domainIncludes"),
                URIRef("http://schema.org/domainIncludes"),
            ]
            schema_range_preds = [
                URIRef("https://schema.org/rangeIncludes"),
                URIRef("http://schema.org/rangeIncludes"),
            ]

            extracted: list[PropertyExtract] = []
            seen_uris: set = set()

            for prop_uri in props:
                if prop_uri in seen_uris:
                    continue
                # Collect domains/ranges from rdfs AND schema.org variants
                domains = set(g.objects(prop_uri, RDFS.domain))
                ranges = set(g.objects(prop_uri, RDFS.range))
                for pred in schema_domain_preds:
                    domains |= set(g.objects(prop_uri, pred))
                for pred in schema_range_preds:
                    ranges |= set(g.objects(prop_uri, pred))

                if extract_all:
                    # Keep every property; surface its full domain/range.
                    matched_domains = domains
                    matched_ranges = ranges
                else:
                    # Keep only if domain OR range intersects target class set
                    matched_domains = domains & target_uris
                    matched_ranges = ranges & target_uris
                    if not matched_domains and not matched_ranges:
                        continue

                seen_uris.add(prop_uri)
                extracted.append(
                    PropertyExtract(
                        property_uri=str(prop_uri),
                        labels=[str(o) for o in g.objects(prop_uri, RDFS.label)],
                        comments=[str(o) for o in g.objects(prop_uri, RDFS.comment)],
                        # When filtering by targets, only the matched subset.
                        # When extract_all, the full set (URIRef-only).
                        domain=[str(u) for u in matched_domains if isinstance(u, URIRef)],
                        range=[str(u) for u in matched_ranges if isinstance(u, URIRef)],
                    )
                )

            updates["extracted_property_count"] = len(extracted)

        # NOTE: we intentionally do NOT store every class/property in the graph
        # store here. /fetch is a lightweight catalog-metadata refresh — its
        # job is to populate class_count/property_count/axiom_count on the
        # ontology record and cache the serialized bytes locally. The classes
        # actually used for matching come from sample_classes in config.yaml
        # and are stored via the embeddings pipeline (Step 3/4 of setup.py).
        #
        # Persisting the full graph here would mean 2000+ serial SPARQL INSERT
        # calls for a large ontology like Schema.org (~800 classes + 1300
        # properties × ~300ms per INSERT), pushing /fetch to 10+ minutes and
        # blocking setup.py. If a caller needs the full serialized ontology,
        # it's on disk at file_path for rdflib to parse locally.

    except Exception:  # noqa: BLE001 — we want any parse failure recorded, not crashed
        # parse_status already 'parse_error'; leave counts as None
        pass

    updates["parse_status"] = parse_status
    graph.update_ontology(ontology_id, updates)

    # 'extracted' is defined above only when the caller requested extraction
    # (either extract_properties_for_classes or extract_all_properties) and
    # the parse succeeded. Otherwise return None so the schema stays
    # backwards-compatible for callers that don't care.
    extracted_response: list[PropertyExtract] | None = (
        locals().get("extracted")
        if body and (body.extract_properties_for_classes or body.extract_all_properties)
        else None
    )

    # ── Option E: unresolved-reference detection ─────────────────────
    # When the caller tells us which ontology URIs they've already loaded,
    # scan the parsed graph for rdfs:subClassOf / rdfs:domain / rdfs:range
    # targets whose namespace isn't covered by any of them. These are
    # "dangling" references — the fetched ontology points to external
    # classes that won't participate in grounding. Surface them so the
    # caller can warn the user.
    unresolved_response: list[UnresolvedReference] | None = None
    if parse_status == "ok" and body and body.known_ontology_uris:
        from collections import defaultdict

        from rdflib import URIRef

        # Normalize each known URI to a prefix we can match iri.startswith against.
        # Most ontology URIs end in '/' or '#'; tolerate both.
        known_prefixes = []
        for ku in body.known_ontology_uris:
            ku = ku.rstrip("/#")
            known_prefixes.append(ku)

        # Always treat the fetched ontology itself as "known"
        self_uri = ont.get("uri", ontology_id).rstrip("/#")
        known_prefixes.append(self_uri)

        # Also treat standard RDF/RDFS/OWL/XSD vocab as always-known — these
        # are baked into the semantics, not customer ontologies to load.
        known_prefixes.extend(
            [
                "http://www.w3.org/1999/02/22-rdf-syntax-ns",
                "http://www.w3.org/2000/01/rdf-schema",
                "http://www.w3.org/2002/07/owl",
                "http://www.w3.org/2001/XMLSchema",
                "http://www.w3.org/2004/02/skos/core",
            ]
        )

        def _is_known(iri: str) -> bool:
            return any(iri.startswith(p) for p in known_prefixes)

        def _namespace_of(iri: str) -> str:
            """'http://example.com/vocab#Claim' → 'http://example.com/vocab#'."""
            for sep in ("#", "/"):
                if sep in iri:
                    return iri.rsplit(sep, 1)[0] + sep
            return iri

        unresolved: dict[str, dict] = defaultdict(lambda: {"count": 0, "iris": set()})
        edge_preds = [RDFS.subClassOf, RDFS.domain, RDFS.range]
        for pred in edge_preds:
            for _s, _p, obj in g.triples((None, pred, None)):
                if not isinstance(obj, URIRef):
                    continue
                iri = str(obj)
                if _is_known(iri):
                    continue
                ns = _namespace_of(iri)
                unresolved[ns]["count"] += 1
                if len(unresolved[ns]["iris"]) < 5:
                    unresolved[ns]["iris"].add(iri)

        if unresolved:
            unresolved_response = [
                UnresolvedReference(
                    namespace=ns,
                    sample_iris=sorted(data["iris"]),
                    reference_count=data["count"],
                )
                for ns, data in sorted(unresolved.items())
            ]

    return OntologyFetchResponse(
        ontology_id=ontology_id,
        uri=ont.get("uri", ontology_id),
        parse_status=parse_status,
        class_count=class_count,
        property_count=property_count,
        axiom_count=updates.get("axiom_count"),
        last_fetched=updates.get("last_fetched"),
        file_path=file_dest,
        properties=extracted_response,
        unresolved_references=unresolved_response,
    )


# ── Helpers ─────────────────────────────────────────────────────────────


def _persist_upload(namespace: str, identifier: str, content: bytes, fmt: str) -> str:
    """Write the raw bytes to ``ONTOLOGY_STORAGE_PATH/{namespace}/...``.

    Returns the destination path. File name is a slugified version of the
    ontology id so the path stays filesystem-safe.
    """
    # SECURITY: reject path traversal characters in namespace. ``fullmatch`` (not
    # ``match`` + ``$``) because ``$`` also matches just before a trailing
    # newline, which would let ``"ns\n"`` through.
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", namespace):
        raise ValueError(f"Invalid namespace: {namespace!r}")
    root = os.path.realpath(ONTOLOGY_STORAGE_PATH)
    ns_dir = os.path.realpath(os.path.join(root, namespace))
    if not _is_within_storage_root(ns_dir):
        raise ValueError(f"Path traversal detected in namespace: {namespace!r}")
    os.makedirs(ns_dir, exist_ok=True)

    # Reduce the caller-supplied ontology id to a filesystem-safe slug. The
    # substitution maps every path separator to ``_``, and the resolved-dest
    # check below is the actual containment guarantee — so the FULL id is kept
    # rather than just its basename. Ontology ids are IRIs, and many differ only
    # before the last ``/`` (``https://schema.org/`` vs
    # ``http://purl.org/dc/terms/``); slugifying only the basename would collapse
    # them onto one filename and each upload would overwrite the last.
    full_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", identifier)
    if len(full_slug) > 200:
        # Truncation alone would merge any two ids sharing a 200-char prefix, so
        # the truncated form carries a digest of the full id. Only applied when
        # truncation actually happens, to keep the (overwhelmingly common) short
        # ids at their natural filenames.
        digest = hashlib.sha256(identifier.encode()).hexdigest()[:12]
        slug = f"{full_slug[:187]}-{digest}"
    else:
        slug = full_slug
    # A slug of only dots would combine with the extension into a name like
    # ``...ttl``; harmless, but meaningless as a filename.
    if slug.strip(".") == "":
        slug = "ontology"
    ext_map = {"turtle": "ttl", "rdf-xml": "xml", "owl-xml": "xml", "json-ld": "jsonld"}
    ext = ext_map.get((fmt or "turtle").lower(), "ttl")

    dest = os.path.realpath(os.path.join(ns_dir, f"{slug}.{ext}"))
    # Final belt-and-braces containment check on the resolved file path — this
    # is the value that actually reaches ``open()``.
    if os.path.dirname(dest) != ns_dir:
        raise ValueError(f"Path traversal detected in ontology id: {identifier!r}")
    with open(dest, "wb") as f:
        f.write(content)
    return dest


# ── Presigned URL upload ──────────────────────────────────────────────

_UPLOAD_EXPIRY_SECONDS = 900  # 15 min
_ONTOLOGY_BUCKET = dynamo_store.resolve_ontology_bucket()

_SUPPORTED_ONTOLOGY_TYPES = {
    "text/turtle",
    "application/rdf+xml",
    "application/ld+json",
    "application/owl+xml",
    "application/octet-stream",
}


@router.post("/upload-url")
def get_ontology_upload_url(body: GetOntologyUploadUrlRequestContent, namespace: str = "default"):
    """Generate a presigned S3 PUT URL for ontology file upload.

    After the client uploads the file to S3 via the presigned URL, the
    backend automatically ingests it (parses RDF, stores in graph+vector,
    updates registry). The client can poll GET /ontologies/{id} to check
    when ingestion is complete (embedding_count > 0).
    """
    import uuid as _uuid

    # The Smithy model leaves content_type optional; default to text/turtle.
    content_type = body.content_type or "text/turtle"
    if content_type not in _SUPPORTED_ONTOLOGY_TYPES:
        raise HTTPException(
            400,
            f"Unsupported content_type '{content_type}'. Supported: {', '.join(sorted(_SUPPORTED_ONTOLOGY_TYPES))}",
        )

    # Generate ontology ID if not provided
    ont_id = body.ontology_id or f"urn:ontology:upload-{_uuid.uuid4().hex[:8]}"
    upload_id = _uuid.uuid4().hex[:12]

    # S3 key: ontology-uploads/{namespace}/{upload_id}/{filename}
    safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", body.filename)[:200]
    if not safe_filename or safe_filename.strip(".") == "":
        raise HTTPException(400, "Invalid filename after sanitization.")
    s3_key = f"ontology-uploads/{namespace}/{upload_id}/{safe_filename}"

    try:
        # Reuse the s3v4-pinned client from dynamo_store — a no-Config client
        # falls back to the deprecated SigV2 presigner (still the default in
        # pre-2014 regions like us-east-1), and SigV2-only regions won't sign at
        # all. Only ``ContentType`` is signed: a signed ``ContentLength`` would
        # force the browser to PUT exactly that many bytes under SigV4 (403 for
        # anything else) — it never capped upload size for a presigned PUT.
        url = dynamo_store._get_s3().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": _ONTOLOGY_BUCKET,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=_UPLOAD_EXPIRY_SECONDS,
        )
    except Exception as e:
        log.error("Failed to generate presigned URL: %s", e)
        raise HTTPException(500, "Failed to generate upload URL") from e

    # The client then PUTs the file to S3 and calls
    # POST /ontologies/{id}/ingest-from-s3 with this s3_key to trigger ingest.
    return GetOntologyUploadUrlResponseContent(
        upload_url=url,
        s3_key=s3_key,
        ontology_id=ont_id,
    )


def _infer_rdf_format(filename: str) -> str:
    """Infer RDF format from file extension."""
    lower = filename.lower()
    if lower.endswith(".rdf") or lower.endswith(".owl"):
        return "xml"
    if lower.endswith(".jsonld") or lower.endswith(".json"):
        return "json-ld"
    if lower.endswith(".nt") or lower.endswith(".ntriples"):
        return "nt"
    if lower.endswith(".n3"):
        return "n3"
    return "turtle"


def _fail_ingest_stub(namespace: str, ontology_id: str, error: str) -> None:
    """Flip an "ingesting" stub row to failed when its background ingest errors.

    Only touches the row while it's still the stub we wrote at trigger time
    (status == ``ingesting``) — a real, previously-ingested row is never
    clobbered. Best-effort: a DDB hiccup here must not mask the original error.
    Without this, a failed async upload would leave the row spinning on
    "Ingesting" forever.
    """
    with contextlib.suppress(Exception):
        row = dynamo_store.get_ontology_registry(namespace, ontology_id)
        if row and row.get("status") == ONTOLOGY_STATUS_INGESTING:
            dynamo_store.update_ontology_registry(
                namespace, ontology_id, status="failed", parse_status="parse_error", ingest_error=error[:1000]
            )


def _run_ingest_from_s3(
    job_id: str, ontology_id: str, s3_key: str, fmt: str, title: str, ontology_type: str, namespace: str
):
    """Background worker for ontology ingestion.

    Status is persisted to DynamoDB (not an in-process dict) so the poll
    endpoint can read it even after an ECS task restart or across task
    instances.
    """
    import boto3 as _boto3

    dynamo_store.update_ingest_job(job_id, IngestJobState.RUNNING, namespace=namespace)
    try:
        s3 = _boto3.client("s3", region_name=os.getenv("AWS_REGION", "us-east-1"))
        resp = s3.get_object(Bucket=_ONTOLOGY_BUCKET, Key=s3_key)
        content_length = resp.get("ContentLength", 0)
        _max_ingest_bytes = 100 * 1024 * 1024  # 100 MB
        if content_length > _max_ingest_bytes:
            raise ValueError(f"File too large ({content_length} bytes, max {_max_ingest_bytes})")
        content = resp["Body"].read()

        graph_store, vector_store = _stores(namespace)
        # Ingest + wait for the embeddings to be searchable before marking the
        # job COMPLETED — a poller that sees COMPLETED and then grounds/serves
        # against this ontology must not race AOSS's eventual-consistency
        # window. The on_embeddings_sync hook moves the job from RUNNING to
        # EMBEDDINGS_SYNC for the duration of the wait, so a client shows
        # "Syncing embeddings…" instead of a stalled-looking RUNNING.
        result = ingest_ontology_and_wait(
            graph_store=graph_store,
            vector_store=vector_store,
            content=content.decode("utf-8", errors="replace"),
            namespace=namespace,
            metadata=IngestMetadata(
                ontology_id=ontology_id,
                title=title or ontology_id,
                ontology_type=ontology_type,
                format=fmt,
            ),
            validate=False,
            on_embeddings_sync=lambda: dynamo_store.update_ingest_job(
                job_id, IngestJobState.EMBEDDINGS_SYNC, namespace=namespace
            ),
            # Replace the "ingesting" stub row written at trigger time (rather
            # than 409-ing against it). The fresh-write path overwrites it with
            # the fully-ingested row — real counts, parse_status="ok", and the
            # transient "ingesting" status cleared (see put_ontology_registry).
            overwrite_stub=True,
        )

        ont = dynamo_store.get_ontology_registry(namespace, ontology_id) or result
        dynamo_store.update_ingest_job(
            job_id,
            IngestJobState.COMPLETED,
            namespace=namespace,
            result={
                "ontology_id": ontology_id,
                "class_count": ont.get("class_count"),
                "embedding_count": ont.get("embedding_count"),
            },
        )
    except IngestParseError as e:
        _fail_ingest_stub(namespace, ontology_id, f"Parse error: {e}")
        dynamo_store.update_ingest_job(job_id, IngestJobState.FAILED, namespace=namespace, error=f"Parse error: {e}")
    except IngestConflictError as e:
        # A genuine conflict (real row already existed) — do NOT touch that row;
        # only the job reflects the failure. The stub-overwrite path above means
        # this only fires for a real pre-existing ontology, which we must not clobber.
        dynamo_store.update_ingest_job(job_id, IngestJobState.FAILED, namespace=namespace, error=f"Conflict: {e}")
    except Exception as e:
        # Log the full error server-side but return only the exception class to
        # the client — str(e) on S3/boto failures can leak bucket names, key
        # paths, and account IDs (in ARNs). Mirrors proposals.py's validation path.
        log.exception("ingest-from-s3 job %s failed", job_id)
        _fail_ingest_stub(namespace, ontology_id, f"Ingest failed: {type(e).__name__}")
        dynamo_store.update_ingest_job(
            job_id, IngestJobState.FAILED, namespace=namespace, error=f"Ingest failed: {type(e).__name__}"
        )


@router.post("/{ontology_id:path}/ingest-from-s3", status_code=202)
def ingest_ontology_from_s3(
    ontology_id: str,
    body: IngestOntologyFromS3RequestContent,
    namespace: str = "default",
):
    """Ingest an ontology file from S3 (async).

    Returns 202 immediately with a job_id. Poll GET /ontologies/{id}/ingest-status/{job_id}
    for completion. Format is auto-detected from filename if not provided.
    """
    import uuid as _uuid
    from threading import Thread

    # SECURITY: the caller-supplied s3_key must point inside THIS namespace's
    # upload prefix. The artifacts bucket is multi-tenant — without this check a
    # user in namespace A could pass another tenant's key (e.g.
    # ``proposals/B/.../ontology.ttl``) and have its content read + ingested
    # into namespace A. The presigned-URL handler only ever issues keys under
    # ``ontology-uploads/{namespace}/{upload_id}/{file}``; enforce that shape
    # and reject path traversal.
    expected_prefix = f"ontology-uploads/{namespace}/"
    s3_key = body.s3_key
    if "\\" in s3_key or ".." in s3_key.split("/") or not s3_key.startswith(expected_prefix):
        raise HTTPException(403, "s3_key must be within this namespace's upload prefix")

    filename = s3_key.rsplit("/", 1)[-1] if "/" in s3_key else s3_key
    fmt = body.format or _infer_rdf_format(filename)

    job_id = str(_uuid.uuid4())
    dynamo_store.put_ingest_job(job_id, ontology_id, IngestJobState.PENDING, namespace=namespace)

    # Write an "ingesting" STUB registry row up-front so the ontology shows in
    # the ontologies list immediately — with a name + in-progress indicator —
    # instead of only a banner until the background parse+embed finishes (which
    # is how foundational Load and induction already behave). ``_run_ingest_from_s3``
    # passes ``overwrite_stub=True`` so the real ingest replaces this row. Only
    # write the stub when nothing is registered yet: a real existing row means a
    # genuine 409 the background ingest will surface, and we must not clobber it.
    if not dynamo_store.get_ontology_registry(namespace, ontology_id):
        dynamo_store.update_ontology_registry(
            namespace,
            ontology_id,
            uri=ontology_id,
            title=body.title or ontology_id,
            ontology_type=body.ontology_type or "user_created",
            format=fmt,
            status=ONTOLOGY_STATUS_INGESTING,
            parse_status="pending",
            class_count=0,
            property_count=0,
            axiom_count=0,
            embedding_count=0,
        )

    Thread(
        target=_run_ingest_from_s3,
        args=(job_id, ontology_id, s3_key, fmt, body.title or "", body.ontology_type or "user_created", namespace),
        daemon=True,
    ).start()

    # Return the typed IngestJob handle (camelCase on the wire via aliases).
    return IngestOntologyFromS3ResponseContent(
        result=IngestJob(job_id=job_id, ontology_id=ontology_id, status=IngestJobState.PENDING)
    )
