# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Import OSI Lambda handler.

POST /namespaces/{namespaceId}/import-osi → 200 OK (sync) or 202 Accepted (async)

Flow:
1. Read content from S3 (always — UI uploads to S3 first)
2. Parse OSI YAML, count metrics
3. If ≤ SYNC_THRESHOLD (0 — i.e. an empty import, or no queue configured):
   process synchronously, return 200
4. Otherwise: create import job, enqueue to SQS, return 202 with jobId

Every import carrying at least one metric therefore returns 202 + jobId; poll
GET /namespaces/{namespaceId}/import-jobs/{jobId} for completion. See the
SYNC_THRESHOLD comment for why this is not count-tunable.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
import structlog
from botocore.exceptions import ClientError
from coa_common.logging import setup_logging
from coa_common.response import api_response, get_caller_identity

from coa_metrics.api.import_s3_key import IMPORT_S3_KEY_SCOPE_ERROR, is_import_s3_key_for_namespace
from coa_metrics.data_source_lookup_factory import build_data_source_lookup
from coa_metrics.dataset_resolver import resolve_datasets
from coa_metrics.lookups import ColumnMetadata, DataSourceLookup
from coa_metrics.metadata_reconciler import (
    MetadataStore,
    reconcile_metadata,
)
from coa_metrics.neptune_client import (
    MetricAiContext,
    MetricDefinition,
    MetricDialect,
    MetricNeptuneClient,
)
from coa_metrics.opensearch_client import MetricOpenSearchClient
from coa_metrics.osi_parser import (
    OsiMetric,
    osi_dialect_to_internal,
    parse_osi_yaml,
)
from coa_metrics.source_status import (
    SourceValidationUnavailableError,
    check_source_approved,
    check_source_table_exists,
    permissive_lookup_enabled,
)
from coa_metrics.validator import check_data_modifying

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# Imports above this threshold are processed asynchronously via SQS.
#
# 0 == every non-empty import goes async. This is deliberate, not a tuning
# choice: the inline path embeds each metric, and the FIRST embed on a cold
# container costs ~25s (observed: metric #0 created→embedded 24.8s, metric #1
# 0.9s — AOSS IngestionRequestLatency averages ~200ms but tails to 15-51s).
# API Gateway's integration timeout is ~29s, so the fixed cost alone consumed
# almost the whole budget and a 3-metric import 504'd. Because the cost is
# per-container rather than per-metric, ANY non-zero threshold — even 1 — leaves
# the timeout reachable, so the count-based guard cannot bound it.
#
# Single-metric creates stay synchronous: they go through create_metric.py,
# which never consults this threshold.
SYNC_THRESHOLD = 0
_IMPORT_QUEUE_URL = os.environ.get("IMPORT_QUEUE_URL", "")
_sqs_client: Any = None


def _get_sqs() -> Any:
    global _sqs_client  # noqa: PLW0603
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _sqs_client


# ── Singleton clients ───────────────────────────────────────────────────

_neptune: MetricNeptuneClient | None = None
_opensearch: MetricOpenSearchClient | None = None
_lookups: dict[str, DataSourceLookup] = {}


def _get_neptune() -> MetricNeptuneClient:
    global _neptune  # noqa: PLW0603
    if _neptune is None:
        _neptune = MetricNeptuneClient()
    return _neptune


def _get_opensearch() -> MetricOpenSearchClient:
    global _opensearch  # noqa: PLW0603
    if _opensearch is None:
        _opensearch = MetricOpenSearchClient()
    return _opensearch


def _get_lookup(namespace: str) -> DataSourceLookup:
    """Get or build the data source lookup for a namespace.

    Per-namespace cache prevents cross-namespace data leakage.

    Fail-closed: if the SMUS-backed lookup cannot be built, the import
    is rejected instead of silently accepting every data source reference.
    The permissive fallback only applies when ``ALLOW_PERMISSIVE_SOURCE_LOOKUP``
    is explicitly enabled (dev/testing), and logs a WARNING when active.

    Raises:
        SourceValidationUnavailableError: Lookup unavailable and permissive
            mode not enabled — callers map this to a 5xx / failed job.
    """
    if namespace not in _lookups:
        init_error = "lookup not configured"
        try:
            lookup = build_data_source_lookup(namespace)
            if lookup:
                _lookups[namespace] = lookup
                return _lookups[namespace]
        except Exception as exc:
            init_error = str(exc)
            logger.warning("data_source_lookup_init_failed", error=init_error)
        if not permissive_lookup_enabled():
            raise SourceValidationUnavailableError(
                f"data source lookup unavailable for namespace '{namespace}': {init_error}"
            )
        logger.warning(
            "permissive_source_lookup_active",
            namespace=namespace,
            detail="data source references NOT validated — dev/testing mode",
        )
        _lookups[namespace] = _PermissiveLookup()
    return _lookups[namespace]


class _PermissiveLookup(DataSourceLookup):
    """Fallback lookup that accepts all data sources (dev/testing only)."""

    def data_source_exists(self, data_source_id: str) -> bool:
        return True

    def table_exists(self, data_source_id: str, table_name: str) -> bool:
        return True

    def get_table_columns(self, data_source_id: str, table_name: str) -> list[ColumnMetadata] | None:
        return None


# ── S3 helpers ──────────────────────────────────────────────────────────

_s3_client: Any = None


def _get_s3_client() -> Any:
    global _s3_client  # noqa: PLW0603
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _s3_client


def _get_bucket() -> str:
    bucket_name = os.environ.get("OSI_BUCKET_NAME", "")
    if not bucket_name:
        raise RuntimeError("OSI_BUCKET_NAME not configured")
    return bucket_name


def _read_from_s3(s3_key: str) -> str:
    """Read OSI YAML content from S3. Raises on failure."""
    response = _get_s3_client().get_object(Bucket=_get_bucket(), Key=s3_key)
    content = response["Body"].read().decode("utf-8")
    logger.info("s3_content_read", s3_key=s3_key, size_bytes=len(content))
    return content


@dataclass(frozen=True)
class StagedImportSource:
    """A version-pinned source object for an asynchronous import job."""

    key: str
    version_id: str


def _write_to_s3(content: str, namespace: str) -> StagedImportSource:
    """Stage exact async input under a unique versioned key."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"{namespace}/imports/jobs/{timestamp}-{uuid.uuid4()}.yaml"
    result = _get_s3_client().put_object(Bucket=_get_bucket(), Key=s3_key, Body=content.encode("utf-8"))
    version_id = result.get("VersionId") if isinstance(result, dict) else None
    if not isinstance(version_id, str) or not version_id.strip():
        raise RuntimeError("versioned S3 staging write did not return VersionId")
    logger.info("s3_inline_written", s3_key=s3_key, version_id=version_id, size_bytes=len(content))
    return StagedImportSource(key=s3_key, version_id=version_id)


# ── Handler ─────────────────────────────────────────────────────────────


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler for POST /namespaces/{ns}/import-osi."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    caller = get_caller_identity(event)
    logger.info("import_osi_start", namespace=namespace, caller=caller)

    # Parse request body
    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if body is None or not isinstance(body, dict):
        return api_response(400, {"message": "Request body is required"})

    # Resolve content: either inline or from S3
    content = body.get("content", "")
    raw_s3_key: object = body.get("s3Key", "")
    has_s3_key = "s3Key" in body and raw_s3_key != ""

    if content and has_s3_key:
        return api_response(400, {"message": "'content' and 's3Key' are mutually exclusive"})

    if has_s3_key:
        if not is_import_s3_key_for_namespace(raw_s3_key, namespace):
            return api_response(400, {"message": IMPORT_S3_KEY_SCOPE_ERROR})
        s3_key = raw_s3_key
        # Read content from S3
        try:
            content = _read_from_s3(s3_key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return api_response(404, {"message": f"OSI YAML not found at S3 key: {s3_key}"})
            logger.exception("s3_read_failed", s3_key=s3_key)
            return api_response(503, {"message": "Failed to read from S3 — service unavailable, retry later"})
        except Exception:
            logger.exception("s3_read_failed", s3_key=s3_key)
            return api_response(500, {"message": "Failed to read OSI YAML from S3"})
    elif content:
        s3_key = ""
        # Enforce inline size limit (5MB)
        max_inline_bytes = 5 * 1024 * 1024
        if len(content.encode("utf-8")) > max_inline_bytes:
            return api_response(
                413,
                {"message": "Inline content exceeds 5MB limit. Use GetOsiUploadUrl for large files."},
            )
    else:
        return api_response(400, {"message": "Either 'content' or 's3Key' is required"})

    # Step 1: Parse OSI YAML
    parse_result = parse_osi_yaml(content)
    if not parse_result.success:
        error_messages = [f"{e.path}: {e.message}" for e in parse_result.errors]
        return api_response(400, {"message": "OSI YAML parse errors", "errors": error_messages})

    document = parse_result.document
    if document is None:
        return api_response(400, {"message": "Failed to parse OSI document"})

    # Async decision: if metric count exceeds threshold, process via SQS worker
    metric_count = len(document.metrics) if document.metrics else 0
    if metric_count > SYNC_THRESHOLD and _IMPORT_QUEUE_URL:
        # Re-stage the exact bytes under a unique key even when the request used
        # S3. This binds every new job to a version returned by the bucket and
        # prevents a caller from changing the source between chunks or retries.
        try:
            staged_source = _write_to_s3(content, namespace)
        except Exception:
            logger.exception("s3_write_for_async_failed", namespace=namespace)
            return api_response(500, {"message": "Failed to stage content for async import"})
        s3_key = staged_source.key
        from coa_metrics.api.import_job_store import complete_job, create_job

        job = create_job(
            namespace_id=namespace,
            s3_key=s3_key,
            metrics_total=metric_count,
            source_version_id=staged_source.version_id,
        )
        try:
            _get_sqs().send_message(
                QueueUrl=_IMPORT_QUEUE_URL,
                MessageBody=json.dumps(
                    {
                        "namespaceId": namespace,
                        "jobId": job["jobId"],
                        "s3Key": s3_key,
                        "offset": 0,
                        "chunkSize": 50,
                    }
                ),
                MessageAttributes={
                    "namespaceId": {"DataType": "String", "StringValue": namespace},
                    "jobId": {"DataType": "String", "StringValue": job["jobId"]},
                },
            )
        except Exception:
            logger.exception("import_enqueue_failed", namespace=namespace, job_id=job["jobId"])
            complete_job(namespace, job["jobId"], status="FAILED")
            return api_response(500, {"message": "Failed to enqueue import job"})
        logger.info(
            "import_async_enqueued",
            namespace=namespace,
            job_id=job["jobId"],
            metric_count=metric_count,
        )
        return api_response(
            202,
            {
                "datasetsResolved": 0,
                "metricsCreated": 0,
                "metricsUpdated": 0,
                "jobId": job["jobId"],
                "status": "IN_PROGRESS",
            },
        )

    warnings: list[str] = []

    # Step 2: Resolve datasets (fail-closed when the lookup is unavailable, #564).
    # The lookup is only needed when a datasets block is present — documents
    # relying solely on per-metric custom_extensions skip resolution entirely.
    if document.datasets:
        try:
            lookup = _get_lookup(namespace)
        except SourceValidationUnavailableError as exc:
            logger.error("source_validation_unavailable", namespace=namespace, error=str(exc))
            return api_response(503, {"message": "Data source validation is unavailable — try again later"})
        resolution = resolve_datasets(document.datasets, lookup)
        if not resolution.success:
            error_messages = [f"Dataset '{e.dataset_name}': {e.reason}" for e in resolution.errors]
            return api_response(
                400,
                {"message": "Dataset resolution failed — all datasets must be onboarded", "errors": error_messages},
            )
        datasets_resolved = len(resolution.resolved)

        # Step 3: Reconcile metadata (best-effort — don't fail import)
        try:
            metadata_store = _get_metadata_store()
            if metadata_store:
                reconciliation = reconcile_metadata(resolution.resolved, metadata_store)
                warnings.extend(reconciliation.warnings)
        except Exception as exc:
            logger.warning("metadata_reconciliation_failed", error=str(exc))
            warnings.append(f"Metadata reconciliation skipped: {exc}")
    else:
        datasets_resolved = 0

    # Step 4: Create/update metrics
    metrics_created = 0
    metrics_updated = 0
    neptune = _get_neptune()
    opensearch = _get_opensearch()

    for osi_metric in document.metrics:
        try:
            metric_def = _osi_metric_to_definition(osi_metric, document, caller)
        except ValueError as exc:
            warnings.append(f"Metric '{osi_metric.name}': skipped — {exc}")
            continue

        # Enforce an APPROVED source (#564) and the declared sourceTable (#161)
        # — create/update enforce both, so import must too or it is a bypass.
        # Approval is checked first, matching create_metric: a metric bound to a
        # PENDING/REJECTED/FAILED source must not land regardless of whether its
        # table happens to exist. Note import defaults source_table to the
        # metric NAME when custom_extensions omit it, which is exactly the path
        # that carried unchecked tables. Per-metric skip-with-warning matches how
        # the DML block above is handled; a validation outage is batch-level
        # (503) since it affects every metric.
        try:
            source_error = check_source_approved(namespace, metric_def.data_source_id)
            if source_error is None:
                source_error = check_source_table_exists(namespace, metric_def.data_source_id, metric_def.source_table)
        except SourceValidationUnavailableError as exc:
            logger.error("source_validation_unavailable", namespace=namespace, error=str(exc))
            return api_response(503, {"message": "Data source validation is unavailable — try again later"})
        if source_error:
            warnings.append(f"Metric '{metric_def.name}': skipped — {source_error}")
            continue

        # Resolve ontology concept names to full URIs
        if metric_def.ontology_concepts:
            try:
                metric_def.ontology_concepts = neptune.resolve_class_uris(namespace, metric_def.ontology_concepts)
            except Exception as exc:
                logger.warning("ontology_concept_resolution_failed", name=metric_def.name, error=str(exc))

        # Check if metric already exists
        try:
            existing = neptune.get_metric(namespace, metric_def.name)
        except Exception as exc:
            logger.warning("metric_lookup_failed", name=metric_def.name, error=str(exc))
            warnings.append(f"Metric '{metric_def.name}': lookup failed — {exc}")
            continue

        try:
            if existing is not None:
                neptune.update_metric(namespace, metric_def.name, metric_def)
                metrics_updated += 1
                author = getattr(existing, "defined_by", "")
                author_detail = f" authored by {author.strip()}" if isinstance(author, str) and author.strip() else ""
                warnings.append(f"Metric '{metric_def.name}' overwritten (had existing metadata{author_detail})")
                logger.info("metric_updated_via_import", name=metric_def.name, namespace=namespace)
            else:
                neptune.create_metric(namespace, metric_def)
                metrics_created += 1
                logger.info("metric_created_via_import", name=metric_def.name, namespace=namespace)
        except Exception as exc:
            logger.exception("metric_write_failed", name=osi_metric.name, error=str(exc))
            warnings.append(f"Metric '{osi_metric.name}': write failed — {exc}")
            continue

        # Embed in OpenSearch (best-effort)
        try:
            opensearch.embed_metric(namespace, metric_def)
        except Exception as exc:
            logger.warning("opensearch_embed_failed", name=metric_def.name, error=str(exc))
            warnings.append(f"Metric '{metric_def.name}': search indexing delayed — {exc}")

    # Build response
    response_body: dict[str, Any] = {
        "datasetsResolved": datasets_resolved,
        "metricsCreated": metrics_created,
        "metricsUpdated": metrics_updated,
        "status": "COMPLETED",
    }
    if warnings:
        response_body["warnings"] = warnings

    logger.info(
        "import_osi_complete",
        namespace=namespace,
        datasets_resolved=datasets_resolved,
        metrics_created=metrics_created,
        metrics_updated=metrics_updated,
        warnings_count=len(warnings),
    )

    return api_response(200, response_body)


# ── Helpers ─────────────────────────────────────────────────────────────


def _get_metadata_store() -> MetadataStore | None:
    """Get metadata store if configured. Returns None if not available."""
    # TODO: Implement DynamoDB-backed MetadataStore when catalog integration is ready
    return None


def _osi_metric_to_definition(
    osi_metric: OsiMetric,
    document: Any,
    caller: str,
) -> MetricDefinition:
    """Convert an OSI metric to an internal MetricDefinition.

    Maps OSI dialects to internal dialects and extracts custom extension data.
    """
    # Map dialects
    dialects: list[MetricDialect] = []
    for expr in osi_metric.expression:
        try:
            internal_dialect = osi_dialect_to_internal(expr.dialect)
        except ValueError:
            # Unknown dialect — use as-is (lowercase)
            internal_dialect = expr.dialect.lower()
        # Safety check: DML/DDL is the only hard block on import.
        # Fragments and unparseable SQL import with a soft warning.
        dml_error = check_data_modifying(expr.expression, internal_dialect)
        if dml_error:
            raise ValueError(dml_error)
        dialects.append(MetricDialect(dialect=internal_dialect, expression=expr.expression))

    if not dialects:
        raise ValueError("No valid dialect expressions")

    # Extract data source and table from custom extensions or dataset resolution
    data_source_id = ""
    source_table = ""
    unit = ""
    return_type = ""
    time_grain = ""
    ontology_concepts: list[str] = []

    if osi_metric.custom_extensions:
        ext = osi_metric.custom_extensions
        data_source_id = ext.data_source_id
        source_table = ext.source_table
        unit = ext.unit
        return_type = ext.return_type
        time_grain = ext.time_dimension
        ontology_concepts = ext.ontology_concepts

    # If no data_source_id from extensions, try to resolve from datasets
    if not data_source_id and document.datasets and len(document.datasets) == 1:
        data_source_id = document.datasets[0].data_source_id or document.datasets[0].name

    if not data_source_id:
        raise ValueError("No data_source_id in custom_extensions and cannot infer from datasets")

    if not source_table:
        # Default source_table to metric name if not specified
        source_table = osi_metric.name

    # Map ai_context directly — OSI ai_context is a structured object matching our MetricAiContext
    ai_context = None
    if osi_metric.ai_context:
        ai_context = MetricAiContext(
            synonyms=osi_metric.ai_context.synonyms,
            instructions=osi_metric.ai_context.instructions,
            examples=osi_metric.ai_context.examples,
        )

    # Map time_dimension to TimeGrain enum values
    default_time_grain = _map_time_dimension(time_grain) if time_grain else None

    return MetricDefinition(
        name=osi_metric.name,
        description=osi_metric.description,
        expression_dialects=dialects,
        data_source_id=data_source_id,
        source_table=source_table,
        default_time_grain=default_time_grain,
        unit=unit or None,
        return_type=return_type or None,
        ai_context=ai_context,
        ontology_concepts=ontology_concepts,
        defined_by=caller,
        effective_from=datetime.now(UTC).strftime("%Y-%m-%d"),
    )


def _map_time_dimension(time_dim: str) -> str | None:
    """Map OSI time_dimension to internal TimeGrain enum value."""
    mapping = {
        "day": "DAY",
        "week": "WEEK",
        "month": "MONTH",
        "quarter": "QUARTER",
        "year": "YEAR",
    }
    return mapping.get(time_dim.lower())
