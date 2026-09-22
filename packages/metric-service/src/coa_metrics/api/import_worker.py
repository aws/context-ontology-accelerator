# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQS worker Lambda for async metric import.

Processes a chunk of metrics from an OSI file stored in S3.
If more metrics remain, sends a continuation message to the same queue.

SQS message format:
{
    "namespaceId": "ns-1",
    "jobId": "uuid",
    "s3Key": "imports/uuid/file.yaml",
    "offset": 0,
    "chunkSize": 200
}
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import structlog
from coa_common.logging import setup_logging

from coa_metrics.api.import_job_store import (
    complete_job,
    get_job,
    update_job_progress,
)
from coa_metrics.api.import_osi import _get_lookup, _osi_metric_to_definition
from coa_metrics.dataset_resolver import resolve_datasets
from coa_metrics.neptune_client import MetricNeptuneClient
from coa_metrics.osi_parser import parse_osi_yaml
from coa_metrics.source_status import (
    SourceValidationUnavailableError,
    check_source_approved,
    check_source_table_exists,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

DEFAULT_CHUNK_SIZE = 50
_QUEUE_URL = os.environ.get("IMPORT_QUEUE_URL", "")
_BUCKET = os.environ.get("OSI_BUCKET_NAME", "")

_neptune: MetricNeptuneClient | None = None
_s3 = None
_sqs = None


def _get_neptune() -> MetricNeptuneClient:
    global _neptune  # noqa: PLW0603
    if _neptune is None:
        _neptune = MetricNeptuneClient()
    return _neptune


def _get_s3():
    global _s3  # noqa: PLW0603
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _s3


def _get_sqs():
    global _sqs  # noqa: PLW0603
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _sqs


def handler(event: dict[str, Any], context: Any) -> None:
    """SQS Lambda handler — processes one message (one chunk)."""
    for record in event.get("Records", []):
        body = json.loads(record["body"])
        _process_chunk(body)


def _process_chunk(msg: dict[str, Any]) -> None:
    """Process a single chunk of metrics from the import job."""
    namespace_id = msg["namespaceId"]
    job_id = msg["jobId"]
    s3_key = msg["s3Key"]
    offset = msg.get("offset", 0)
    chunk_size = msg.get("chunkSize", DEFAULT_CHUNK_SIZE)

    logger.info(
        "import_chunk_start",
        job_id=job_id,
        namespace=namespace_id,
        offset=offset,
        chunk_size=chunk_size,
    )

    # Defense-in-depth: the s3_key must live under this namespace's import prefix
    if not s3_key.startswith(f"{namespace_id}/imports/"):
        logger.error("invalid_s3_key_for_namespace", job_id=job_id, s3_key=s3_key, namespace=namespace_id)
        complete_job(namespace_id, job_id, status="FAILED")
        return

    # Fetch the job to check it hasn't been cancelled
    job = get_job(namespace_id, job_id)
    if not job or job.get("status") != "IN_PROGRESS":
        logger.warning("import_job_not_active", job_id=job_id, status=job.get("status") if job else "NOT_FOUND")
        return

    # Read the OSI file from S3
    try:
        response = _get_s3().get_object(Bucket=_BUCKET, Key=s3_key)
        content = response["Body"].read().decode("utf-8")
    except Exception as exc:
        logger.error("s3_read_failed", job_id=job_id, s3_key=s3_key, error=str(exc))
        complete_job(namespace_id, job_id, status="FAILED")
        return

    # Parse all metrics from the file
    try:
        parse_result = parse_osi_yaml(content)
        if not parse_result.success:
            logger.error("osi_parse_failed", job_id=job_id, errors=str(parse_result.errors))
            complete_job(namespace_id, job_id, status="FAILED")
            return
        all_metrics = parse_result.document.metrics if parse_result.document else []
    except Exception as exc:
        logger.error("osi_parse_failed", job_id=job_id, error=str(exc))
        complete_job(namespace_id, job_id, status="FAILED")
        return

    # Resolve datasets once (first chunk) — must succeed before any metric is written,
    # matching the synchronous import path (import_osi.py).
    if offset == 0 and parse_result.document and parse_result.document.datasets:
        try:
            lookup = _get_lookup(namespace_id)
        except SourceValidationUnavailableError as exc:
            # Fail-closed: no permissive fallback — fail the job with a
            # clear reason instead of importing unvalidated source references.
            logger.error("source_validation_unavailable", job_id=job_id, error=str(exc))
            complete_job(namespace_id, job_id, status="FAILED")
            return
        resolution = resolve_datasets(parse_result.document.datasets, lookup)
        if not resolution.success:
            logger.error("dataset_resolution_failed", job_id=job_id, errors=str(resolution.errors))
            complete_job(namespace_id, job_id, status="FAILED")
            return

    # Slice the chunk
    chunk = all_metrics[offset : offset + chunk_size]
    if not chunk:
        # No more metrics to process — mark complete
        complete_job(namespace_id, job_id, status="COMPLETED")
        return

    # Process each metric in the chunk
    neptune = _get_neptune()
    created = 0
    updated = 0
    errors: list[str] = []
    warnings: list[str] = []
    source_approval_results: dict[str, str | None] = {}
    source_table_results: dict[tuple[str, str], str | None] = {}

    for processed, osi_metric in enumerate(chunk, start=1):
        try:
            metric_def = _osi_metric_to_definition(osi_metric, parse_result.document, "import-worker")
            # Every non-empty import is dispatched to this worker
            # (import_osi.SYNC_THRESHOLD == 0), so the worker must enforce the
            # same source contract as create/update and the dormant synchronous
            # import path. Otherwise OSI import can persist a metric for a
            # missing or unapproved source.
            data_source_id = metric_def.data_source_id
            if data_source_id not in source_approval_results:
                source_approval_results[data_source_id] = check_source_approved(namespace_id, data_source_id)
            source_error = source_approval_results[data_source_id]
            if source_error is None:
                source_table_key = (data_source_id, metric_def.source_table.lower())
                if source_table_key not in source_table_results:
                    source_table_results[source_table_key] = check_source_table_exists(
                        namespace_id,
                        data_source_id,
                        metric_def.source_table,
                    )
                source_error = source_table_results[source_table_key]
            if source_error:
                raise ValueError(source_error)
            if metric_def.ontology_concepts:
                metric_def.ontology_concepts = neptune.resolve_class_uris(namespace_id, metric_def.ontology_concepts)
            existing = neptune.get_metric(namespace_id, metric_def.name)
            if existing:
                neptune.update_metric(namespace_id, metric_def.name, metric_def)
                updated += 1
            else:
                neptune.create_metric(namespace_id, metric_def)
                created += 1
        except SourceValidationUnavailableError as exc:
            metric_name = getattr(osi_metric, "name", "unknown")
            errors.append(f"{metric_name}: {exc}")
            logger.error(
                "source_validation_unavailable",
                job_id=job_id,
                name=metric_name,
                error=str(exc),
            )
            # Preserve the counts and errors from any metrics already handled in
            # this chunk before failing the job. Returning without this update
            # would leave the durable job record inconsistent with Neptune.
            update_job_progress(
                namespace_id,
                job_id,
                metrics_processed=processed,
                metrics_created=created,
                metrics_updated=updated,
                errors=errors,
                warnings=warnings or None,
            )
            complete_job(namespace_id, job_id, status="FAILED")
            return
        except Exception as exc:
            metric_name = getattr(osi_metric, "name", "unknown")
            errors.append(f"{metric_name}: {exc}")
            logger.warning("metric_import_error", name=metric_name, error=str(exc))

    # Update job progress — pass per-chunk deltas; the store increments atomically.
    update_job_progress(
        namespace_id,
        job_id,
        metrics_processed=len(chunk),
        metrics_created=created,
        metrics_updated=updated,
        errors=errors or None,
        warnings=warnings or None,
    )

    logger.info(
        "import_chunk_done",
        job_id=job_id,
        offset=offset,
        processed=len(chunk),
        created=created,
        updated=updated,
        errors_count=len(errors),
    )

    # If more metrics remain, send continuation message
    next_offset = offset + chunk_size
    if next_offset < len(all_metrics):
        try:
            _get_sqs().send_message(
                QueueUrl=_QUEUE_URL,
                MessageBody=json.dumps(
                    {
                        "namespaceId": namespace_id,
                        "jobId": job_id,
                        "s3Key": s3_key,
                        "offset": next_offset,
                        "chunkSize": chunk_size,
                    }
                ),
            )
        except Exception as exc:
            # Without a continuation, remaining chunks would never run — fail loudly.
            logger.exception("import_continuation_failed", job_id=job_id, next_offset=next_offset, error=str(exc))
            complete_job(namespace_id, job_id, status="FAILED")
            return
        logger.info("import_continuation_sent", job_id=job_id, next_offset=next_offset)
    else:
        # All chunks processed — mark complete
        complete_job(namespace_id, job_id, status="COMPLETED")
