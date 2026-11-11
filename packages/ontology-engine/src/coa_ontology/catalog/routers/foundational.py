# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Foundational ontology loader — curated catalog of well-known public ontologies.

Covers Schema.org, FOAF, PROV-O, FIBO modules, and similar public ontologies
that users can load into a namespace with one click.

Two endpoints:

  GET  /ontology/foundational
        List the curated catalog. Each entry has a stable ``key``
        the client uses to load it, plus display metadata.

  POST /ontology/foundational/{key}/load?namespace=…
        Create the ontology record and trigger the existing
        ``fetch_ontology_from_source`` flow. Returns the same shape
        as ``POST /{ontology_id}/fetch`` so the UI can render a
        unified "loaded" confirmation.

The catalog is a hardcoded allowlist — no user-supplied URLs reach
the fetcher this way. Users who need a custom URL can call ``POST
/{ontology_id}/fetch`` directly with the SSRF-validated URL, or use
the file-upload endpoint. Adding a new foundational ontology means
adding an entry below; the SSRF allowlist in
``_validate_fetch_url`` still applies in defense-in-depth.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from threading import Thread

from coa_control_plane_server.models.ingest_job import IngestJob
from coa_control_plane_server.models.ingest_job_state import IngestJobState
from coa_control_plane_server.models.ingest_ontology_from_s3_response_content import (
    IngestOntologyFromS3ResponseContent,
)
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from coa_ontology import dynamo_store
from coa_ontology.catalog.routers.ontologies import (
    OntologyCreate,
    OntologyFetchRequest,
    create_ontology,
    fetch_ontology_from_source,
)

_log = logging.getLogger("foundational")

router = APIRouter()


class FoundationalOntology(BaseModel):
    """Curated foundational-ontology catalog entry.

    ``key`` is the stable identifier used in the load URL and in the
    UI dropdown. ``uri`` is the official ontology IRI (used as the
    ontology_id once loaded). ``source_url`` is the document URL the
    fetcher will GET.
    """

    key: str
    title: str
    description: str
    uri: str
    source_url: str
    format: str
    domain_tags: list[str]
    license: str | None = None


# Curated catalog — keep aligned with
# ``packages/ontology-engine/demo/config-example.yaml`` so demo and
# production share the same foundational set. Adding a new entry here
# requires that its ``source_url`` host is reachable from the ECS VPC
# (i.e. egresses through the NAT gateway to a public IP).
_CATALOG: list[FoundationalOntology] = [
    FoundationalOntology(
        key="schema-org",
        title="Schema.org",
        description=(
            "Vocabulary for structured data on the web — broad coverage of "
            "products, organizations, places, events, and more."
        ),
        uri="https://schema.org/",
        source_url="https://schema.org/version/latest/schemaorg-current-https.ttl",
        format="turtle",
        domain_tags=["general", "web", "commerce"],
        license="CC-BY-SA-3.0",
    ),
    FoundationalOntology(
        key="dublin-core",
        title="Dublin Core Terms",
        description=(
            "Core metadata vocabulary — title, creator, subject, date, etc. — "
            "used for documents and any cultural-heritage object."
        ),
        uri="http://purl.org/dc/terms/",
        source_url="https://www.dublincore.org/specifications/dublin-core/dcmi-terms/dublin_core_terms.ttl",
        format="turtle",
        domain_tags=["metadata", "documents"],
        license="CC-BY-4.0",
    ),
    FoundationalOntology(
        key="foaf",
        title="FOAF",
        description="Friend-of-a-Friend — vocabulary for describing people, their activities, and their relationships.",
        uri="http://xmlns.com/foaf/0.1/",
        source_url="https://xmlns.com/foaf/spec/index.rdf",
        format="rdf+xml",
        domain_tags=["people", "social"],
    ),
    FoundationalOntology(
        key="prov-o",
        title="PROV-O",
        description="W3C Provenance Ontology — entities, activities, and agents that produced or influenced data.",
        uri="http://www.w3.org/ns/prov#",
        source_url="https://www.w3.org/ns/prov.ttl",
        format="turtle",
        domain_tags=["provenance", "lineage"],
    ),
    FoundationalOntology(
        key="fibo-agreements",
        title="FIBO Agreements",
        description=(
            "Financial Industry Business Ontology — Foundations / Agreements module (obligor, obligee, beneficiary)."
        ),
        uri="https://spec.edmcouncil.org/fibo/ontology/FND/Agreements/Agreements/",
        source_url="https://spec.edmcouncil.org/fibo/ontology/master/latest/FND/Agreements/Agreements.ttl",
        format="turtle",
        domain_tags=["finance", "agreements", "insurance"],
    ),
    FoundationalOntology(
        key="fibo-contracts",
        title="FIBO Contracts",
        description="FIBO Foundations / Contracts module — formal contractual relationships between parties.",
        uri="https://spec.edmcouncil.org/fibo/ontology/FND/Agreements/Contracts/",
        source_url="https://spec.edmcouncil.org/fibo/ontology/master/latest/FND/Agreements/Contracts.ttl",
        format="turtle",
        domain_tags=["finance", "agreements", "insurance"],
    ),
    FoundationalOntology(
        key="fibo-clients-accounts",
        title="FIBO Clients & Accounts",
        description="FIBO Business / Products & Services — clients, accounts, account holders.",
        uri="https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/",
        source_url="https://spec.edmcouncil.org/fibo/ontology/master/latest/FBC/ProductsAndServices/ClientsAndAccounts.ttl",
        format="turtle",
        domain_tags=["finance", "accounts"],
    ),
    FoundationalOntology(
        key="fibo-financial-products",
        title="FIBO Financial Products and Services",
        description="FIBO Business / Products & Services — financial products and the services that surround them.",
        uri="https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/FinancialProductsAndServices/",
        source_url="https://spec.edmcouncil.org/fibo/ontology/master/latest/FBC/ProductsAndServices/FinancialProductsAndServices.ttl",
        format="turtle",
        domain_tags=["finance", "products"],
    ),
]

_BY_KEY: dict[str, FoundationalOntology] = {o.key: o for o in _CATALOG}


@router.get("/foundational")
def list_foundational_ontologies() -> dict[str, list[FoundationalOntology]]:
    """Return the curated foundational ontology catalog."""
    return {"items": _CATALOG}


def _ensure_registry_row(entry: FoundationalOntology, namespace: str) -> None:
    """Register the foundational ontology's registry row (idempotent).

    Both the async endpoint and the synchronous ``_load_foundational_and_wait``
    seed path MUST create the row BEFORE the load worker runs:
    ``_run_foundational_load`` -> ``fetch_ontology_from_source`` looks the row up
    first and raises 404 if it is absent, and a fresh namespace has no row yet.
    A pre-existing row is the idempotent-refresh case (409 -> log + continue);
    any other HTTPException is a real failure and re-raises.
    """
    try:
        create_ontology(
            OntologyCreate(
                ontology_id=entry.uri,
                title=entry.title,
                description=entry.description,
                uri=entry.uri,
                source_url=entry.source_url,
                format=entry.format,
                domain_tags=entry.domain_tags,
                ontology_type="foundational",
            ),
            namespace=namespace,
        )
    except HTTPException as e:
        if e.status_code != 409:
            raise
        _log.info("Foundational ontology %s already exists in %s, refreshing", entry.key, namespace)


def _run_foundational_load(job_id: str, entry: FoundationalOntology, namespace: str):
    """Background worker for foundational-ontology load (async).

    Drives a DynamoDB-backed :class:`IngestJob` (keyed by the ontology
    URI, same as ``ingest-from-s3``) through RUNNING → EMBEDDINGS_SYNC →
    COMPLETED/FAILED so the existing ``GET /ontologies/{id}/ingest-status/{job}``
    poll endpoint can report progress. Runs off the request thread because
    fetching + parsing + embedding a large ontology (FIBO, Schema.org) can
    exceed API Gateway's 29s integration timeout.

    Pipeline (matches what /upload does for user-supplied files):

      1. Fetch the ontology bytes from the curated source URL via the
         existing SSRF-validated fetcher.
      2. Run :func:`ingest_ontology_and_wait` over the fetched bytes —
         creates embeddings + projects classes/properties into the graph
         + flips the registry's ``parse_status`` to ``"ok"``, then blocks
         until those embeddings are searchable in the vector index. The
         ``on_embeddings_sync`` hook flips the job to EMBEDDINGS_SYNC for
         the duration of that wait.

    Idempotent: append mode means re-loading a foundational refreshes its
    embeddings without dropping the existing rows.
    """
    from coa_ontology.catalog.ingest import (
        IngestMetadata,
        ingest_ontology_and_wait,
    )
    from coa_ontology.catalog.routers.ontologies import _stores

    dynamo_store.update_ingest_job(job_id, IngestJobState.RUNNING, namespace=namespace)
    try:
        # ── 1. Fetch the bytes (SSRF-validated; populates file_path on disk) ──
        fetched = fetch_ontology_from_source(
            ontology_id=entry.uri,
            body=OntologyFetchRequest(source_url=entry.source_url),
            namespace=namespace,
        )
        fetched_parse_status = getattr(fetched, "parse_status", None)
        fetched_file_path = getattr(fetched, "file_path", None)

        if fetched_parse_status != "ok":
            # Fetch or parse failed — embeddings would fail with the same
            # root cause, so mark the job FAILED without attempting ingest.
            dynamo_store.update_ingest_job(
                job_id,
                IngestJobState.FAILED,
                namespace=namespace,
                error=f"Fetch/parse failed: parse_status={fetched_parse_status}",
            )
            return

        if not fetched_file_path:
            dynamo_store.update_ingest_job(
                job_id,
                IngestJobState.FAILED,
                namespace=namespace,
                error="Fetch reported ok but produced no file_path",
            )
            return

        with open(fetched_file_path, "rb") as f:
            content = f.read()

        # ── 2. Full ingest (parse + projection + embeddings + registry sync) ──
        graph_store, vector_store = _stores(namespace)
        ingest_result = ingest_ontology_and_wait(
            graph_store,
            vector_store,
            content,
            namespace=namespace,
            metadata=IngestMetadata(
                ontology_id=entry.uri,
                title=entry.title,
                description=entry.description,
                ontology_type="foundational",
                source_url=entry.source_url,
                format=entry.format,
                domain_tags=entry.domain_tags,
                file_path=fetched_file_path,
                source="foundational-load",
                platform_foundational=True,
            ),
            validate=False,
            allow_append=True,
            on_embeddings_sync=lambda: dynamo_store.update_ingest_job(
                job_id, IngestJobState.EMBEDDINGS_SYNC, namespace=namespace
            ),
        )

        dynamo_store.update_ingest_job(
            job_id,
            IngestJobState.COMPLETED,
            namespace=namespace,
            result={
                "ontology_id": entry.uri,
                "class_count": ingest_result.get("class_count"),
                "embedding_count": (ingest_result.get("embeddings") or {}).get("count"),
            },
        )
    except Exception as e:
        # Log the full error server-side but return only the exception class to
        # the client — str(e) on fetch/boto failures can leak URLs/paths.
        # Mirrors ingest-from-s3's failure path.
        _log.exception("foundational-load job %s failed for key=%s ns=%s", job_id, entry.key, namespace)
        dynamo_store.update_ingest_job(
            job_id, IngestJobState.FAILED, namespace=namespace, error=f"Load failed: {type(e).__name__}"
        )


def _load_foundational_and_wait(entry: FoundationalOntology, namespace: str) -> bool:
    """Synchronously load a curated foundational ontology, blocking until embeddings are searchable.

    Returns True on COMPLETED, False otherwise.

    This is the in-process variant of :func:`load_foundational_ontology` (which
    returns 202 and loads on a background thread for the HTTP client). Callers
    that must ground AGAINST the ontology immediately — notably
    ``induce_catalog.resolve_grounding_ontology_ids`` — cannot use the async
    endpoint: it returns before embeddings exist, so grounding recall would run
    against an empty index and silently match nothing. Runs the SAME
    ``_run_foundational_load`` worker body inline (which itself blocks on
    ``ingest_ontology_and_wait`` until the vector index is searchable), then
    reads the terminal job state.
    """
    import uuid as _uuid

    job_id = str(_uuid.uuid4())
    _ensure_registry_row(entry, namespace)  # row must exist before fetch -> avoids 404
    dynamo_store.put_ingest_job(job_id, entry.uri, IngestJobState.PENDING, namespace=namespace)
    _run_foundational_load(job_id, entry, namespace)  # inline, blocks
    job = dynamo_store.get_ingest_job(job_id, namespace=namespace)
    status = (job or {}).get("status")
    if status != IngestJobState.COMPLETED:
        _log.warning(
            "sync foundational load did not complete: key=%s ns=%s status=%s",
            entry.key,
            namespace,
            status,
        )
        return False
    return True


@router.post("/foundational/{key}/load", status_code=202)
def load_foundational_ontology(key: str, namespace: str = "default") -> IngestOntologyFromS3ResponseContent:
    """Load a curated foundational ontology into a namespace (async).

    Returns 202 + an :class:`IngestJob` handle immediately and runs the
    fetch + parse + embedding on a background thread. Poll the shared
    ``GET /ontologies/{id}/ingest-status/{job_id}`` endpoint — keyed by the
    ontology URI (``result.ontology_id``) — for completion. Async because
    fetching + embedding a large ontology (FIBO, Schema.org) can exceed API
    Gateway's 29s integration timeout.

    Idempotent: the registry row is created here if absent (409 → refresh);
    the background load appends embeddings so re-loading refreshes without
    dropping existing rows.
    """
    entry = _BY_KEY.get(key)
    if entry is None:
        raise HTTPException(404, f"Unknown foundational ontology key: {key}")

    # ── Registry row (skip when it already exists) ──
    # Done on the request thread so the row exists before the client polls;
    # a pre-existing row is the idempotent-refresh case (409 → keep going).
    _ensure_registry_row(entry, namespace)

    # Key the IngestJob by the ontology URI so the SAME
    # GET /ontologies/{ontology_id:path}/ingest-status/{job_id} poll endpoint
    # works — the client polls with entry.uri (returned as result.ontology_id).
    job_id = str(_uuid.uuid4())
    dynamo_store.put_ingest_job(job_id, entry.uri, IngestJobState.PENDING, namespace=namespace)

    Thread(
        target=_run_foundational_load,
        args=(job_id, entry, namespace),
        daemon=True,
    ).start()

    return IngestOntologyFromS3ResponseContent(
        result=IngestJob(job_id=job_id, ontology_id=entry.uri, status=IngestJobState.PENDING)
    )
