# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""FastAPI router for submitting and polling asynchronous ontology-validation jobs."""

import logging
import uuid
from datetime import datetime
from threading import Thread
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from rdflib import Graph

from coa_ontology import dynamo_store
from coa_ontology.validation.schemas import (
    JobResponse,
    JobStatus,
    StructuralMetrics,
    ValidationReport,
    ValidationRequest,
    ValidationTier,
)
from coa_ontology.validation.validators import OntologyValidator
from coa_ontology.validation.validators.tier1 import (
    ConnectivityValidator,
    ConsistencyValidator,
    DatatypeTokenValidator,
    SHACLValidator,
    TaxonomyCycleValidator,
)
from coa_ontology.validation.validators.tier2 import StructuralMetricsValidator
from coa_ontology.validation.validators.tier3 import (
    AmbiguousMatchValidator,
    CompetencyQuestionValidator,
    LabelCompletenessValidator,
    OoPSValidator,
)

router = APIRouter()
_log = logging.getLogger(__name__)

_jobs: dict[str, JobResponse] = {}
_job_namespaces: dict[str, str] = {}  # job_id → namespace


def _persist(job: JobResponse, namespace: str) -> None:
    """Best-effort durable copy of an ontology-validation job in DynamoDB.

    The in-memory dict is lost on any ECS task restart, which 404s the poll for
    work that may still be running or already done. Persist so the poll resolves
    across restarts / instances. Failures are logged and swallowed (the caller's
    in-memory copy still serves the same-task poll).
    """
    try:
        item = {**job.model_dump(mode="json"), "proposal_id": None}
        if dynamo_store.get_async_job("ONTVALIDATE", job.job_id, namespace=namespace) is None:
            dynamo_store.put_async_job("ONTVALIDATE", job.job_id, item, namespace=namespace)
        else:
            dynamo_store.update_async_job(
                "ONTVALIDATE", job.job_id, {k: v for k, v in item.items() if k != "job_id"}, namespace=namespace
            )
    except Exception:  # noqa: BLE001 — durability is best-effort
        _log.warning("Failed to persist ontology-validation job %s", job.job_id, exc_info=True)


_VALIDATORS: dict[ValidationTier, list[OntologyValidator]] = {
    ValidationTier.tier1_blocking: [
        ConsistencyValidator(),
        TaxonomyCycleValidator(),
        ConnectivityValidator(),
        SHACLValidator(),
        DatatypeTokenValidator(),
    ],
    ValidationTier.tier2_scoring: [
        StructuralMetricsValidator(),
    ],
    ValidationTier.tier3_review: [
        OoPSValidator(),
        LabelCompletenessValidator(),
        AmbiguousMatchValidator(),
        CompetencyQuestionValidator(),
    ],
}


def _fetch_ontology_turtle(ontology_catalog_url: str, ontology_id: str, namespace: str = "default") -> str:
    encoded_id = quote(ontology_id, safe="")
    params = {"namespace": namespace}
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{ontology_catalog_url}/ontologies/{encoded_id}", params=params)
        resp.raise_for_status()
        data = resp.json()

        ont_type = data.get("ontology_type", "foundational")
        if ont_type == "foundational":
            raise ValueError(
                f"Ontology {ontology_id} is type '{ont_type}'. "
                "Validation is only supported for 'induced' or 'user_created' ontologies."
            )

        dl = client.get(f"{ontology_catalog_url}/ontologies/{encoded_id}/download", params=params)
        dl.raise_for_status()
        # /download no longer inlines the Turtle: it returns {"downloadUrl", ...}
        # pointing at a presigned S3 object, because a 6+ MB ontology would exceed
        # the API Gateway / Lambda response limit (#143). Follow the URL
        # out-of-band. The raw-body fallback keeps this working against an
        # ontology-engine that still inlines (mixed versions during a rollout).
        download_url = None
        try:
            payload = dl.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            download_url = payload.get("downloadUrl") or payload.get("download_url")
        if not download_url:
            return dl.text

        obj = client.get(download_url)
        obj.raise_for_status()
        return obj.text


def _run_validation(job_id: str, body: ValidationRequest, namespace: str, config: dict):
    job = _jobs[job_id]
    job.status = JobStatus.RUNNING
    _persist(job, namespace)
    try:
        g = Graph()
        if body.ontology_turtle:
            g.parse(data=body.ontology_turtle, format="turtle")
        else:
            turtle = _fetch_ontology_turtle(config["ontology_catalog_url"], body.ontology_id, namespace=namespace)
            if turtle:
                g.parse(data=turtle, format="turtle")
            else:
                raise ValueError(f"No content available for ontology {body.ontology_id}.")

        all_findings = []
        metrics_out: dict[str, Any] = {}

        for tier in body.tiers:
            for v in _VALIDATORS.get(tier, []):
                all_findings.extend(
                    v.validate(
                        g,
                        shacl_shapes_turtle=body.shacl_shapes_turtle,
                        competency_questions=body.competency_questions or [],
                        _metrics_out=metrics_out,
                    )
                )

        passed = not any(f.severity == "error" for f in all_findings)

        job.report = ValidationReport(
            job_id=job_id,
            ontology_id=body.ontology_id,
            passed=passed,
            findings=all_findings,
            metrics=StructuralMetrics(**metrics_out) if metrics_out else None,
            tiers_run=body.tiers,
            created_at=datetime.utcnow(),
        )
        job.status = JobStatus.COMPLETED
    except Exception as e:
        job.status = JobStatus.FAILED
        job.error = str(e)
    _persist(job, namespace)


@router.post("/", response_model=JobResponse, status_code=202)
def start_validation(body: ValidationRequest, request: Request, namespace: str):
    """Create a pending validation job and run it on a background thread.

    Args:
        body: The validation request (ontology id/turtle, tiers, shapes, questions).
        request: The FastAPI request, used to read app config.
        namespace: The namespace the caller is authorized for; scopes the job.

    Returns:
        The newly created job in PENDING status (HTTP 202).
    """
    job_id = str(uuid.uuid4())
    _jobs[job_id] = JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=datetime.utcnow(),
    )
    _job_namespaces[job_id] = namespace
    _persist(_jobs[job_id], namespace)
    config = request.app.state.config
    Thread(target=_run_validation, args=(job_id, body, namespace, config), daemon=True).start()
    return _jobs[job_id]


@router.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, namespace: str):
    """Return a validation job by id, falling back to the durable DynamoDB copy.

    Args:
        job_id: Identifier of the validation job to fetch.
        namespace: The caller's namespace; jobs from other namespaces 404.

    Returns:
        The job response.

    Raises:
        HTTPException: 404 when the job is unknown or belongs to another namespace.
    """
    job = _jobs.get(job_id)
    if job:
        if _job_namespaces.get(job_id) != namespace:
            raise HTTPException(404, "Job not found")
        return job
    # Fall back to the durable copy so the poll resolves after a task restart /
    # on a different instance (a stale non-terminal row is swept to failed).
    # Best-effort: if DynamoDB is unreachable, fall through to a clean 404.
    try:
        item = dynamo_store.get_async_job("ONTVALIDATE", job_id, namespace=namespace)
    except Exception:  # noqa: BLE001
        _log.warning("Failed to read ontology-validation job %s from DynamoDB", job_id, exc_info=True)
        item = None
    if not item:
        raise HTTPException(404, "Job not found")
    return JobResponse.model_validate(item)


@router.get("/jobs", response_model=list[JobResponse])
def list_jobs(namespace: str):
    """List the in-memory validation jobs belonging to the given namespace.

    Args:
        namespace: The caller's namespace to filter jobs by.

    Returns:
        The validation jobs scoped to the namespace (in-memory only).
    """
    return [job for job_id, job in _jobs.items() if _job_namespaces.get(job_id) == namespace]
