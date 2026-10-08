# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""FastAPI router for unstructured ontology induction.

Mirrors the structured ``induce_catalog`` router but trimmed for
unstructured needs: no R2RML mapping output, no SMUS catalog fetch.
The router accepts an :class:`UnstructuredInductionRequest`, persists
a job record to the existing ``OntologyProposals`` Dynamo table with
``source_type="UNSTRUCTURED"``, and spawns a daemon worker thread
that runs :class:`UnstructuredInductionPipeline` against the lexical
graph identified by ``body.graph_arn``.

Endpoints (mounted at ``/induce/unstructured`` in ``main.py``):

- ``POST /``                 — start a new unstructured induction job.
- ``GET  /jobs``             — list unstructured jobs in a namespace.
- ``GET  /jobs/{job_id}``    — fetch a single job item.

Source-type isolation (Requirement 5.7) is enforced at every read /
write callsite via ``source_type="UNSTRUCTURED"``: a pending
unstructured proposal does NOT block a new structured run, and vice
versa, and ``GET /jobs/{job_id}`` returns 404 (not 403) when the
caller passes a job_id that belongs to a different source type so
the response does not reveal cross-source-type job IDs.

References:
    - design.md § "Router" — reference implementation for this file.
    - Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 7.1, 7.2, 7.3,
      7.4 of
      ``.kiro/specs/unstructured-ontology-induction-api/requirements.md``.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import UTC, datetime
from threading import Event, Thread

from coa_control_plane_server.models.induction_job_status import (
    InductionJobStatus,
)
from coa_control_plane_server.models.induction_source_type import (
    InductionSourceType,
)
from fastapi import APIRouter, HTTPException, Query, Request

from coa_ontology import dynamo_store
from coa_ontology.bedrock_embeddings import BedrockEmbeddingClient
from coa_ontology.inducer.schemas import JobResponse
from coa_ontology.inducer.services.embedding_generator import EmbeddingGeneratorClient
from coa_ontology.inducer.services.grounding import GroundingService
from coa_ontology.inducer.services.llm import LLMClient
from coa_ontology.inducer.unstructured.schemas import (
    UnstructuredInductionConfig,
    UnstructuredInductionRequest,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import (
    InMemoryIRIResolver,
)
from coa_ontology.inducer.unstructured.services.pipeline import (
    UnstructuredInductionPipeline,
)
from coa_ontology.inducer.unstructured.stores.na_lexical import (
    NeptuneAnalyticsLexicalStore,
)
from coa_ontology.inducer.unstructured.stores.ndb_lexical import (
    NeptuneDatabaseLexicalStore,
)

log = logging.getLogger(__name__)
router = APIRouter()

# Cached literal "UNSTRUCTURED" — the value passed to dynamo_store so
# every read / write on this router is scoped to unstructured items.
SOURCE_TYPE: str = InductionSourceType.UNSTRUCTURED.value


@router.post("/", status_code=202, response_model=JobResponse)
def start_induction(body: UnstructuredInductionRequest, request: Request, namespace: str) -> JobResponse:
    """Start an unstructured induction job.

    ``namespace`` is an EXPLICIT route parameter bound from the
    ``?namespace=`` query param — it is NOT read from the request body
    and is NOT defaulted to ``"default"``, mirroring the structured
    router's ``WorkbenchInductionRequest`` convention so a caller
    cannot pass a namespace other than the one their token authorized
    (Requirement 5.2).

    Enforces one in-flight unstructured proposal per namespace
    (Requirement 5.3). A pending STRUCTURED proposal does NOT block a
    new unstructured run and vice versa — the in-flight check is
    explicitly scoped to ``source_type="UNSTRUCTURED"``. "In flight" is the
    shared ``PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION`` set, the same one the
    structured router uses: unreviewed work plus an accept that is merging.

    Returns HTTP 202 with a :class:`JobResponse`. The pipeline runs
    asynchronously in a daemon worker thread; clients poll
    ``GET /jobs/{job_id}`` to track status.
    """
    job_id = str(uuid.uuid4())

    # In-flight lock (see the structured router for the full two-layer
    # rationale): this conditional lock is what actually blocks a SECOND
    # trigger while this run is still executing (no proposal row exists during
    # the in-flight window). The block-until-reviewed proposal check runs
    # AFTER it is acquired — see below.
    #
    # Unstructured proposals go through the same accept pipeline as structured
    # ones (accepting -> embeddings_sync -> accepted | accept_failed), so they
    # block on the same status set, defined once in proposals.py.
    from coa_ontology.proposals import PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

    try:
        dynamo_store.acquire_induction_lock(namespace, job_id)
    except dynamo_store.InductionLockHeldError as e:
        raise HTTPException(
            409,
            {
                "message": (
                    "An induction is already in progress for this namespace. "
                    "Wait for it to finish before starting a new one."
                ),
                "job_id": e.job_id,
            },
        ) from e

    # Everything after the lock is acquired must release it on failure — the
    # worker's finally only runs if the worker THREAD actually starts, so a
    # failure in create_proposal_stub / put_job / Thread.start() here would
    # otherwise leak the lock and wedge the namespace until the stale timeout.
    # The same handler releases it when the block-until-reviewed 409 fires.
    try:
        # Block-until-reviewed: checked UNDER the lock, not before it. Checking
        # first left a check-then-act gap — a trigger could see no proposal,
        # the running worker could then write its pending proposal and release
        # the lock, and the trigger would acquire it and start a duplicate run
        # beside an unreviewed proposal. The worker writes its proposal before
        # releasing, so once we hold the lock this read is authoritative.
        pending: list[dict] = []
        for _status in PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION:
            pending = dynamo_store.list_proposals(namespace=namespace, status=_status, source_type=SOURCE_TYPE)
            if pending:
                break
        if pending:
            raise HTTPException(
                409,
                {
                    "message": (
                        "An unstructured proposal is already in flight for this namespace. "
                        "Accept or reject it before starting a new induction."
                    ),
                    "proposal_id": pending[0]["proposal_id"],
                },
            )

        # In-progress PROPOSAL stub FIRST for immediate + refresh-safe
        # visibility (the worker upserts it into the real proposal at the end).
        # Ordered before put_job so a job row never exists without its stub.
        dynamo_store.create_proposal_stub(
            proposal_id=job_id,
            job_id=job_id,
            namespace=namespace,
            ontology_id=body.ontology_uri_prefix,
            source_type=SOURCE_TYPE,
            label=body.label or "Induced Unstructured Ontology",
        )

        dynamo_store.put_job(
            job_id=job_id,
            status=InductionJobStatus.PENDING.value,
            request_body=body.model_dump(),
            datasource_details=[],
            namespace=namespace,
            source_type=SOURCE_TYPE,
        )

        Thread(
            target=_run_unstructured_induction,
            args=(job_id, body, namespace, request.app.state.config),
            daemon=True,
        ).start()
    except Exception:
        with contextlib.suppress(Exception):
            dynamo_store.release_induction_lock(namespace)
        raise

    return JobResponse(
        job_id=job_id,
        status=InductionJobStatus.PENDING,
        created_at=datetime.now(UTC),
    )


@router.get("/jobs", response_model=list[dict])
def list_jobs(
    namespace: str,
    status: str | None = Query(default=None),
) -> list[dict]:
    """List unstructured induction jobs in a namespace.

    ``namespace`` is an EXPLICIT required route parameter (bound from
    ``?namespace=``), mirroring the structured router — it is never
    defaulted to ``"default"``. Always filters on
    ``source_type="UNSTRUCTURED"`` so structured jobs are never
    returned through this endpoint (Requirement 5.8).
    """
    return dynamo_store.list_jobs(
        namespace=namespace,
        status=status,
        source_type=SOURCE_TYPE,
    )


@router.get("/jobs/{job_id}", response_model=dict)
def get_job(job_id: str, namespace: str) -> dict:
    """Fetch a single unstructured induction job.

    ``namespace`` is an EXPLICIT required route parameter (bound from
    ``?namespace=``), mirroring the structured router — it is never
    defaulted to ``"default"``. Returns 404 (not 403) when the job
    exists but its ``source_type`` is not ``"UNSTRUCTURED"`` so the
    response does not reveal that a job with the same ID exists under
    a different source type (Requirement 5.8).
    """
    job = dynamo_store.get_job(job_id=job_id, namespace=namespace)
    if job is None or job.get("source_type") != SOURCE_TYPE:
        raise HTTPException(404, "Job not found")
    return job


# ── Worker thread ───────────────────────────────────────────────────────


def _build_lexical_store(graph_arn: str, region: str, app_config: dict, namespace: str = ""):
    """Build the appropriate lexical store for the given graph endpoint.

    Routes based on the graph_arn format:
      - NA ARN or bare graph ID → NeptuneAnalyticsLexicalStore
      - NDB path → NeptuneDatabaseLexicalStore (endpoint from server config only)

    Security: graph_arn is never used as an endpoint URL for Neptune Database.
    The Neptune DB endpoint is ALWAYS resolved from server-side config
    (app_config["neptune_endpoint"]) to prevent SSRF attacks where a malicious
    graph_arn could cause boto3 to send SigV4-signed requests (containing IAM
    credentials) to an attacker-controlled server.
    """
    from coa_common import to_graphrag_tenant_id

    tenant_id = to_graphrag_tenant_id(namespace) if namespace else ""

    # Strict ARN validation — only accept canonical Neptune Analytics ARN format
    _VALID_ARN_PREFIX = "arn:aws:neptune-graph:"
    if graph_arn.startswith(_VALID_ARN_PREFIX):
        graph_id = _parse_graph_id(graph_arn)
        return NeptuneAnalyticsLexicalStore(graph_id=graph_id, region=region)

    # Neptune DB path: resolve endpoint entirely from server-side config.
    # NEVER use the caller-supplied graph_arn as an endpoint URL.
    configured_endpoint = app_config.get("neptune_endpoint", "")
    if not configured_endpoint:
        msg = "Neptune DB endpoint is not configured server-side"
        raise ValueError(msg)

    endpoint = configured_endpoint.replace("https://", "").replace("wss://", "").rstrip("/")
    if ":" in endpoint:
        endpoint = endpoint.split(":")[0]
    return NeptuneDatabaseLexicalStore(endpoint=endpoint, region=region, tenant_id=tenant_id)


def _run_unstructured_induction(
    job_id: str,
    body: UnstructuredInductionRequest,
    namespace: str,
    app_config: dict,
) -> None:
    """Background worker: build clients, run the pipeline, persist the proposal.

    ``namespace`` is passed explicitly by the route handler (bound from
    the ``?namespace=`` query param) since it is no longer carried on
    the request body.

    On each pipeline stage transition the worker writes the new status
    to the job item. On success it persists the induced ontology Turtle
    as a proposal record (``ontology_turtle=report.turtle``) tagged
    with ``source_type="UNSTRUCTURED"``, and marks the job
    ``completed``. ``r2rml_turtle`` is set to the empty string because
    R2RML mappings apply only to structured (table → ontology) sources;
    the existing accept handler in ``proposals.py`` tolerates empty
    R2RML and skips writing R2RML artifacts to S3 when the value is
    empty. On any exception the worker marks the job ``failed`` with a
    truncated error message and does NOT create a proposal record
    (Requirement 5.6). The failure-status write relies on the
    reserved-keyword aliasing in ``dynamo_store.update_job_status`` so
    the ``error`` attribute (a DynamoDB reserved keyword) persists.

    Args:
        job_id: The orchestrator-issued job ID. Re-used as the
            ``proposal_id`` for the proposal created on success
            (1:1 mapping mirrors the structured side).
        body: The validated request body.
        namespace: The explicit namespace bound from the route's
            ``?namespace=`` query param.
        app_config: ``request.app.state.config`` snapshot. Used to pick
            up the Bedrock + LLM model defaults configured at app
            startup.
    """
    ns = namespace

    def on_status(stage: InductionJobStatus) -> None:
        dynamo_store.update_job_status(
            job_id=job_id,
            status=stage.value.lower(),
            namespace=ns,
            source_type=SOURCE_TYPE,
        )

    # Liveness watchdog — declared here so ``finally`` can always stop it, even
    # if the run raises before it is started. Unstructured jobs write
    # ``updated_at`` only on stage transitions, so a live-but-slow stage that
    # exceeds the stale window would otherwise be false-swept to failed by
    # ``count_active_jobs`` (it scans source_type=None → unstructured rows too),
    # under-counting active jobs and letting namespace deletion proceed while
    # this worker is still writing. The heartbeat keeps ``updated_at`` fresh.
    # Reuses the structured watchdog helpers (DRY); imported lazily to avoid the
    # induce_catalog ⇄ induce_unstructured import cycle (mirrors the lazy
    # ``resolve_grounding_ontology_ids`` import below).
    from coa_ontology.induce_catalog import _join_watchdog, _start_watchdog

    watchdog_stop = Event()
    watchdog: Thread | None = None

    try:
        _parse_graph_id(body.graph_arn)  # validates ARN format
        region = _parse_region(body.graph_arn) or app_config.get("bedrock_region", "us-east-1")

        lexical_store = _build_lexical_store(body.graph_arn, region, app_config, namespace=ns)
        from coa_common import DEFAULT_EMBED_MODEL_ID

        embedding_client = BedrockEmbeddingClient(
            region=app_config.get("bedrock_region", "us-east-1"),
            model_id=app_config.get("bedrock_embed_model_id", DEFAULT_EMBED_MODEL_ID),
            dimensions=app_config.get("bedrock_embed_dimensions", 1024),
        )
        description_llm = LLMClient(
            model_id=app_config.get("description_llm_model_id", "us.anthropic.claude-sonnet-4-6"),
            region=app_config.get("llm_region", "us-east-1"),
        )
        iri_resolver = InMemoryIRIResolver(
            namespace=ns,
            ontology_name=body.name,
        )

        # Resolve grounding selections to loaded-ontology URIs the SAME way
        # the structured path does (curated key -> URI, plus lazy load+embed
        # of any not-yet-loaded ontology) via the shared resolver. Passing the
        # raw FE selections straight through would silently mis-ground when the
        # FE sends a curated key rather than a URI, or when the selected
        # ontology isn't embedded yet. Build the GroundingService only when at
        # least one selection RESOLVES, so the pipeline skips grounding (no
        # wasted client construction) otherwise.
        grounding_service: GroundingService | None = None
        resolved_grounding_ids: list[str] = []
        if body.grounding_ontology_ids:
            from coa_ontology.induce_catalog import resolve_grounding_ontology_ids

            def _grounding_progress(loaded: int, total: int) -> None:
                dynamo_store.update_job_status(
                    job_id=job_id,
                    status=InductionJobStatus.LOADING_GROUNDING_ONTOLOGIES.value.lower(),
                    namespace=ns,
                    source_type=SOURCE_TYPE,
                    grounding_loaded=loaded,
                    grounding_total=total,
                )

            resolved_grounding_ids = resolve_grounding_ontology_ids(
                grounding_keys=body.grounding_ontology_ids,
                namespace=ns,
                on_progress=_grounding_progress,
            )

        # The grounding pool is EXACTLY the caller's resolved selections — the
        # namespace's accepted induced ontology is no longer force-included. The
        # UI pre-checks it as a default selection (so it arrives in
        # ``grounding_ontology_ids`` and resolves like any other pick), but the
        # user can deselect it. Mirrors the structured worker
        # (induce_catalog._run_induction).

        if resolved_grounding_ids:
            # Use a NAMESPACE-BOUND in-process catalog (build_stores(ns) +
            # StoreOntologyCatalogAdapter) exactly like the structured path
            # (induce_catalog._run_induction). This is the reason structured
            # grounding works on multi-namespace deployments: the adapter
            # queries this namespace's OpenSearch index directly. The plain
            # HTTP OntologyCatalogClient defaults to the "default" (bare)
            # index, which is empty here → 0 recall hits → nothing grounded.
            from coa_ontology.stores import build_stores
            from coa_ontology.stores.adapters import StoreOntologyCatalogAdapter

            ns_graph, ns_vec = build_stores(namespace=ns)
            grounding_service = GroundingService(
                ontology_catalog=StoreOntologyCatalogAdapter(ns_graph, ns_vec, namespace=ns),
                embedding_generator=EmbeddingGeneratorClient(app_config["embedding_generator_url"]),
                llm_region=app_config.get("llm_region", "us-east-1"),
                llm_model_id=app_config.get(
                    "grounding_llm_model_id",
                    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                ),
                namespace=ns,
            )

        config = UnstructuredInductionConfig(
            namespace=ns,
            name=body.name,
            ontology_uri_prefix=body.ontology_uri_prefix,
            graph_arn=body.graph_arn,
            confidence_threshold=body.confidence_threshold,
            sampling=body.sampling,
            description_mode=body.description_mode,
            include_individuals=body.include_individuals,
            entailment_enabled=body.entailment_enabled,
            # Pass the resolved pool AS-IS, including an empty list — an empty
            # pool ([] or None) means "no grounding scope" and recall returns no
            # candidates instead of falling back to a namespace-wide search
            # (which would ground against a loaded-but-unselected ontology).
            # Grounding is always scoped to exactly this pool; there is no
            # unscoped/global recall. Mirrors the structured worker.
            grounding_ontology_ids=resolved_grounding_ids,
            grounding_mode=body.grounding_mode,
            rerank_max_tokens=body.rerank_max_tokens,
            lexical_store=lexical_store,
            embedding_client=embedding_client,
            description_llm=description_llm,
            iri_resolver=iri_resolver,
            grounding_service=grounding_service,
        )

        pipeline = UnstructuredInductionPipeline()

        # Start the liveness heartbeat before the long-running blocking run so a
        # slow-but-alive stage is never age-swept to failed. Stopped in
        # ``finally``.
        watchdog = _start_watchdog(job_id, ns, watchdog_stop, region=app_config.get("llm_region"))

        report = pipeline.run(config, on_status=on_status)

        # Stage: STORING — owned by the worker thread (the orchestrator
        # only emits stages that correspond to in-pipeline work).
        dynamo_store.update_job_status(
            job_id=job_id,
            status=InductionJobStatus.STORING.value.lower(),
            namespace=ns,
            source_type=SOURCE_TYPE,
        )

        # 1:1 proposal_id ↔ job_id, mirroring the structured side.
        # The induced ontology Turtle is persisted as ``ontology_turtle``
        # — this is what the steward review queue and the accept
        # handler consume. ``r2rml_turtle`` is intentionally empty:
        # R2RML mappings exist only for structured sources, and the
        # accept handler skips writing the R2RML artifact to S3 when
        # the value is empty.
        proposal_id = job_id
        dynamo_store.create_proposal(
            proposal_id=proposal_id,
            job_id=job_id,
            namespace=ns,
            ontology_id=body.ontology_uri_prefix,
            proposal_type="induction",
            ontology_turtle=report.turtle,
            r2rml_turtle="",  # unstructured sources have no R2RML
            source_type=SOURCE_TYPE,
            metadata={
                "label": body.label or "Induced Unstructured Ontology",
                "source_type": SOURCE_TYPE,
                "datasource_ids": body.datasource_ids,
                "graph_arn": body.graph_arn,
                "description_mode": body.description_mode,
                "class_count": report.tboxClassCount,
                "object_property_count": report.objectPropertyCount,
                "datatype_property_count": report.datatypePropertyCount,
                "concept_count": report.conceptCount,
                "individual_count": report.individualCount,
                "close_match_count": report.closeMatchAxiomCount,
                "foundational_grounded_count": report.foundationalGroundedCount,
                # Per-class grounding ConceptMatch records. create_proposal
                # offloads these to matches.json (S3) — the review UI hydrates
                # them into metadata.matches to show the grounding-candidate
                # count and drive the manual grounding-OVERRIDE editor, exactly
                # like structured proposals. Only emit the key when non-empty
                # so no empty matches.json is written for ungrounded runs.
                **({"matches": report.groundingMatches} if report.groundingMatches else {}),
                "warnings": report.warnings,
            },
        )

        dynamo_store.update_job_status(
            job_id=job_id,
            status=InductionJobStatus.COMPLETED.value.lower(),
            namespace=ns,
            source_type=SOURCE_TYPE,
            proposal_id=proposal_id,
            class_count=report.tboxClassCount,
            object_property_count=report.objectPropertyCount,
            datatype_property_count=report.datatypePropertyCount,
            concept_count=report.conceptCount,
        )

    except Exception as e:
        log.exception("unstructured induction job %s failed", job_id)
        try:
            dynamo_store.update_job_status(
                job_id=job_id,
                status=InductionJobStatus.FAILED.value.lower(),
                namespace=ns,
                source_type=SOURCE_TYPE,
                error=f"{type(e).__name__}: {str(e)[:1024]}",
            )
        except Exception:  # noqa: BLE001 — best-effort error persist
            log.exception("failed to persist failure status for job %s", job_id)
        # Flip the in-progress proposal stub to failed (proposal_id == job_id).
        try:
            dynamo_store.update_proposal(job_id, namespace=ns, status="failed", accept_error=str(e)[:1024])
        except Exception:  # noqa: BLE001 — best-effort
            log.exception("failed to mark proposal stub failed for job %s", job_id)
    finally:
        # Stop the liveness heartbeat and join it BEFORE releasing the lock, so
        # no tick can land after the run is done.
        _join_watchdog(watchdog, watchdog_stop, job_id, log)
        # Release the in-flight lock whether the run succeeded or failed — it
        # only guards the ACTIVE run. Block-until-reviewed is enforced by
        # start_induction's blocking-proposal check (the disjoint
        # completed-but-unreviewed window). Best-effort; stale-takeover backstops.
        try:
            dynamo_store.release_induction_lock(ns)
        except Exception:  # noqa: BLE001 — best-effort
            log.exception("failed to release induction lock for job %s", job_id)


# ── ARN parsing helpers ─────────────────────────────────────────────────


def _parse_graph_id(graph_arn: str) -> str:
    """Extract the graph identifier from a Neptune Analytics graph ARN.

    Accepted forms:
      - ``arn:aws:neptune-graph:<region>:<account>:graph/<graph_id>``
      - ``<graph_id>`` (bare ID, used in tests; falls back to the app
        config region in :func:`_parse_region`)

    Raises:
        ValueError: if the input starts with ``arn:`` but lacks the
            ``graph/`` segment (an obviously malformed ARN).
    """
    if graph_arn.startswith("arn:"):
        try:
            return graph_arn.split("graph/", 1)[1]
        except IndexError as e:
            raise ValueError(f"malformed Neptune Analytics graph ARN: {graph_arn!r}") from e
    return graph_arn


def _parse_region(graph_arn: str) -> str | None:
    """Extract the region from a Neptune Analytics graph ARN.

    Returns None when the input is a bare graph ID (no ``arn:``
    prefix); callers fall back to the app config's region in that
    case.
    """
    if not graph_arn.startswith("arn:"):
        return None
    parts = graph_arn.split(":")
    return parts[3] if len(parts) >= 4 else None
