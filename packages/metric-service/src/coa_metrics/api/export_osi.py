# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Export OSI Lambda handler.

GET /namespaces/{namespaceId}/export-osi → 200 OK

Flow:
1. List all metrics in the namespace from Neptune
2. Convert each internal MetricDefinition to OSI format
3. Map internal dialects to OSI dialects
4. Assemble OSI YAML document on-the-fly
5. Return content inline OR write to S3 and return pre-signed download URL
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import structlog
from coa_common import resolve_region
from coa_common.logging import setup_logging
from coa_common.response import api_response
from coa_common.s3 import regional_s3_client
from coa_control_plane_server.models.export_format import ExportFormat

from coa_metrics.api.contract import EXPORT_NAMES_MAX_ITEMS, check_metric_name_list
from coa_metrics.neptune_client import (
    MetricDefinition,
    MetricNeptuneClient,
)
from coa_metrics.osi_parser import (
    OsiAiContext,
    OsiCustomExtension,
    OsiDataset,
    OsiDialectExpression,
    OsiDocument,
    OsiMetric,
    internal_dialect_to_osi,
    serialize_osi_yaml,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)


# ── Singleton clients ───────────────────────────────────────────────────

_neptune: MetricNeptuneClient | None = None


def _get_neptune() -> MetricNeptuneClient:
    global _neptune  # noqa: PLW0603
    if _neptune is None:
        _neptune = MetricNeptuneClient()
    return _neptune


# ── Handler ─────────────────────────────────────────────────────────────


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler for GET /namespaces/{ns}/export-osi."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    query_params = event.get("queryStringParameters") or {}
    export_format = query_params.get("format", "inline")

    # API Gateway REST APIs deliver a repeated query param (?names=a&names=b)
    # via multiValueQueryStringParameters, not queryStringParameters (which
    # only keeps the last occurrence). Omitting `names` exports every metric
    # in the namespace — the pre-existing, unchanged behavior.
    multi_value_params = event.get("multiValueQueryStringParameters") or {}
    names = multi_value_params.get("names") or None

    logger.info("export_osi_start", namespace=namespace, format=export_format, names=names)

    if not namespace:
        return api_response(400, {"message": "namespaceId is required"})

    if names is not None:
        names_error = check_metric_name_list(names, "names", max_items=EXPORT_NAMES_MAX_ITEMS)
        if names_error:
            return api_response(400, {"message": names_error})

    if export_format not in (ExportFormat.INLINE, ExportFormat.S3):
        return api_response(
            400,
            {"message": f"Invalid format. Must be '{ExportFormat.INLINE}' or '{ExportFormat.S3}'."},
        )

    # Fetch metrics from Neptune. get_metrics_bulk() issues a single SPARQL
    # round-trip for every matching metric (vs. list_metrics()'s N+1 —  one
    # query per metric), which is what previously caused API Gateway 29s
    # timeouts on namespaces with 100+ metrics. Passing `names` scopes the
    # single query to just that subset; omitting it fetches everything.
    neptune = _get_neptune()
    try:
        metrics = neptune.get_metrics_bulk(namespace, names=names)
    except Exception as exc:
        logger.exception("neptune_list_failed", error=str(exc))
        return api_response(500, {"message": "Failed to retrieve metrics"})

    if not metrics:
        message = "No matching metrics found in namespace" if names else "No metrics found in namespace"
        return api_response(404, {"message": message})

    # Convert to OSI document
    osi_doc = _build_osi_document(metrics)

    # Serialize to YAML
    try:
        yaml_content = serialize_osi_yaml(osi_doc)
    except Exception as exc:
        logger.exception(
            "osi_serialization_failed",
            namespace=namespace,
            metrics_count=len(metrics),
            error=str(exc),
        )
        return api_response(500, {"message": "Failed to serialize OSI export"})

    logger.info(
        "export_osi_complete",
        namespace=namespace,
        metrics_exported=len(metrics),
        datasets_exported=len(osi_doc.datasets),
        format=export_format,
    )

    # Return based on format
    if export_format == ExportFormat.S3:
        result = _write_to_s3(namespace, yaml_content)
        if result is None:
            return api_response(500, {"message": "Failed to write export to S3"})
        return api_response(200, result)

    return api_response(200, {"content": yaml_content})


# ── Helpers ─────────────────────────────────────────────────────────────


def _build_osi_document(metrics: list[MetricDefinition]) -> OsiDocument:
    """Build an OSI document from internal metric definitions."""
    osi_metrics: list[OsiMetric] = []
    datasets_seen: dict[str, OsiDataset] = {}

    for metric in metrics:
        # Collect unique datasets — in OSI, a dataset represents a table/view
        # name = short logical name, source = fully qualified table reference
        dataset_key = metric.source_table or metric.data_source_id
        if dataset_key and dataset_key not in datasets_seen:
            # Derive short name from the last segment of the qualified table name
            short_name = dataset_key.rsplit(".", 1)[-1] if "." in dataset_key else dataset_key
            datasets_seen[dataset_key] = OsiDataset(
                name=short_name,
                source=dataset_key,
                data_source_id=metric.data_source_id,
            )

        # Convert metric
        osi_metric = _metric_to_osi(metric)
        osi_metrics.append(osi_metric)

    return OsiDocument(
        datasets=list(datasets_seen.values()),
        metrics=osi_metrics,
    )


def _metric_to_osi(metric: MetricDefinition) -> OsiMetric:
    """Convert an internal MetricDefinition to OSI format."""
    # Map dialects to OSI
    osi_dialects: list[OsiDialectExpression] = []
    for d in metric.expression_dialects:
        try:
            osi_dialect = internal_dialect_to_osi(d.dialect)
        except ValueError:
            # Unknown dialect — use uppercase as-is
            osi_dialect = d.dialect.upper()
        osi_dialects.append(OsiDialectExpression(dialect=osi_dialect, expression=d.expression))

    # Build ai_context as structured OSI object
    osi_ai_context: OsiAiContext | None = None
    if metric.ai_context and (
        metric.ai_context.synonyms or metric.ai_context.instructions or metric.ai_context.examples
    ):
        osi_ai_context = OsiAiContext(
            synonyms=metric.ai_context.synonyms,
            instructions=metric.ai_context.instructions,
            examples=metric.ai_context.examples,
        )

    # Build custom extensions
    time_dimension = ""
    if metric.default_time_grain:
        time_dimension = metric.default_time_grain.lower()

    custom_ext = OsiCustomExtension(
        data_source_id=metric.data_source_id,
        source_table=metric.source_table,
        unit=metric.unit or "",
        return_type=metric.return_type or "",
        time_dimension=time_dimension,
        ontology_concepts=metric.ontology_concepts,
        defined_by=metric.defined_by or "",
        effective_from=metric.effective_from or "",
        label=metric.label,
    )

    return OsiMetric(
        name=metric.name,
        description=metric.description,
        expression=osi_dialects,
        ai_context=osi_ai_context,
        custom_extensions=custom_ext,
    )


# ── S3 helpers ──────────────────────────────────────────────────────────

PRESIGN_EXPIRY_SECONDS = 900  # 15 minutes

_s3_client: Any = None


def _get_s3_client() -> Any:
    global _s3_client  # noqa: PLW0603
    if _s3_client is None:
        _s3_client = regional_s3_client(resolve_region())
    return _s3_client


def _write_to_s3(namespace: str, yaml_content: str) -> dict[str, Any] | None:
    """Write YAML content to S3 and return a pre-signed download URL."""
    bucket_name = os.environ.get("OSI_BUCKET_NAME", "")
    if not bucket_name:
        logger.error("OSI_BUCKET_NAME not configured")
        return None

    export_id = str(uuid.uuid4())
    s3_key = f"{namespace}/exports/{export_id}/metrics.osi.yaml"

    try:
        s3_client = _get_s3_client()
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=yaml_content.encode("utf-8"),
            ContentType="application/x-yaml",
        )

        download_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": s3_key},
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
        )
    except Exception as exc:
        logger.exception("s3_export_failed", error=str(exc))
        return None

    logger.info("export_written_to_s3", s3_key=s3_key, expires_in=PRESIGN_EXPIRY_SECONDS)

    return {
        "downloadUrl": download_url,
        "expiresIn": PRESIGN_EXPIRY_SECONDS,
    }
