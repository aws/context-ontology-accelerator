# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Router for structured (table-to-ontology) induction jobs and their status.

``router`` is NOT mounted: ``main.py`` serves structured induction through
``induce_catalog.router``, so ``_run_induction`` and the endpoints below are a
retired reference that mints IRIs with ``InductionPipeline.build_ontology``,
whose names are not collision-safe (see its docstring). :func:`_build_pipeline`
is still live, because ``main.py`` wraps it and hands it to ``induce_catalog``,
so this module cannot simply be deleted.
"""

import uuid
from datetime import datetime
from threading import Thread

from coa_common.bedrock import FALLBACK_CHAT_MODEL_ID
from fastapi import APIRouter, HTTPException, Request

from coa_ontology.inducer.schemas import InductionRequest, JobResponse, JobStatus
from coa_ontology.inducer.services.data_catalog import DataCatalogClient
from coa_ontology.inducer.services.embedding_generator import EmbeddingGeneratorClient
from coa_ontology.inducer.services.ontology_catalog import OntologyCatalogClient
from coa_ontology.inducer.services.pipeline import InductionPipeline

router = APIRouter()

_jobs: dict[str, JobResponse] = {}


def _build_pipeline(config: dict) -> InductionPipeline:
    return InductionPipeline(
        data_catalog=DataCatalogClient(config["data_catalog_url"]),
        embedding_generator=EmbeddingGeneratorClient(config["embedding_generator_url"]),
        ontology_catalog=OntologyCatalogClient(config["ontology_catalog_url"]),
        llm_region=config.get("llm_region") or "us-east-1",
        grounding_llm_model_id=config.get("grounding_llm_model_id") or FALLBACK_CHAT_MODEL_ID,
    )


def _run_induction(job_id: str, body: InductionRequest, config: dict):
    job = _jobs[job_id]
    try:
        pipeline = _build_pipeline(config)

        job.status = JobStatus.FETCHING_METADATA
        tables = pipeline.fetch_tables(body.table_ids)

        job.status = JobStatus.GENERATING_EMBEDDINGS
        concepts = pipeline.extract_concepts(tables)
        concepts, model_id = pipeline.embed_concepts(concepts, backend=body.embedding_backend)

        job.status = JobStatus.MATCHING
        matches = pipeline.match_concepts(
            concepts,
            body.confidence_threshold,
            model_id=model_id,
            scoring_strategy=body.scoring_strategy,
            structural_weight=body.structural_weight,
        )

        job.status = JobStatus.BUILDING_ONTOLOGY
        graph = pipeline.build_ontology(body.ontology_uri_prefix, tables, concepts, matches)

        job.status = JobStatus.STORING
        stored = pipeline.store_induced_ontology(
            body.ontology_uri_prefix,
            body.label or "Induced Structured Ontology",
            graph,
            matches,
        )

        from coa_ontology.inducer.schemas import InductionReport

        novel_count = sum(1 for m in matches if m.match_type == "novel" and m.source_column == "")
        grounding = list(
            {
                m.matched_class_uri.rsplit("#", 1)[0]
                for m in matches
                if m.matched_class_uri and m.match_type in ("exact", "high_confidence")
            }
        )

        job.report = InductionReport(
            job_id=job_id,
            ontology_uri_prefix=body.ontology_uri_prefix,
            tables_processed=len(tables),
            columns_processed=sum(len(t.columns) for t in tables),
            matches=matches,
            novel_classes_created=novel_count,
            grounding_ontologies_used=grounding,
            induced_ontology_id=stored.get("id"),
            induced_ontology_uri=body.ontology_uri_prefix,
        )
        job.status = JobStatus.COMPLETED

    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)


@router.post("/", response_model=JobResponse, status_code=202)
def start_induction(body: InductionRequest, request: Request):
    """Start an induction job, dispatching to the strategy the request selects.

    For the ``unstructured_lexical_graph`` strategy the call is forwarded to the
    unstructured router; otherwise a structured table-to-ontology pipeline runs on a
    background thread and its progress is tracked in the in-memory job registry.

    Args:
        body: Induction parameters (tables, thresholds, strategy, URI prefix).
        request: Incoming request, used for app config and query parameters.

    Returns:
        The initial :class:`JobResponse` for the newly created (or forwarded) job.
    """
    # Dispatch to unstructured pipeline if strategy is unstructured_lexical_graph
    if body.strategy == "unstructured_lexical_graph":
        from coa_ontology.inducer.routers.induce_unstructured import (
            UnstructuredInductionRequest,
        )
        from coa_ontology.inducer.routers.induce_unstructured import (
            start_induction as start_unstructured,
        )

        # Use the user-provided graph_arn if present (must be a valid Neptune Analytics ARN),
        # otherwise use the sentinel value "neptune-db" which signals the router to resolve
        # the endpoint from server-side config (prevents SSRF)
        graph_arn = body.graph_arn or "neptune-db"

        namespace = request.query_params.get("namespace", "default")
        unstructured_body = UnstructuredInductionRequest(
            graph_arn=graph_arn,
            name=body.label or "unstructured-induction",
            ontology_uri_prefix=body.ontology_uri_prefix,
            confidence_threshold=body.confidence_threshold,
        )
        return start_unstructured(body=unstructured_body, request=request, namespace=namespace)

    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=datetime.utcnow(),
    )
    config = request.app.state.config
    Thread(target=_run_induction, args=(job_id, body, config), daemon=True).start()
    return _jobs[job_id]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str):
    """Return the status and report of a previously started induction job.

    Args:
        job_id: Identifier returned when the job was created.

    Returns:
        The tracked :class:`JobResponse` for the job.

    Raises:
        HTTPException: 404 if no job with the given id exists.
    """
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs():
    """Return every induction job currently tracked in memory."""
    return list(_jobs.values())
