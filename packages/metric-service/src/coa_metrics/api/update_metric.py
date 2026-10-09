# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Update Metric Lambda handler.

PUT /namespaces/{namespaceId}/metrics/{name} → 200 OK

Full replacement of metric definition (not partial update).
Flow: validate → check exists → delete old triples → write new → re-embed → emit event.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import boto3
import structlog
from coa_common.constants import EVENT_SOURCE_PREFIX
from coa_common.logging import setup_logging
from coa_common.response import api_response, get_caller_identity
from coa_control_plane_server.models.update_metric_request_content import (
    UpdateMetricRequestContent,
)
from pydantic import ValidationError

from coa_metrics.api.create_metric import _metric_to_dict, _validate_soft
from coa_metrics.api.validation_errors import format_validation_error
from coa_metrics.constants import VALID_SQL_DIALECTS, invalid_dialect_message
from coa_metrics.neptune_client import (
    MetricAiContext,
    MetricDefinition,
    MetricDialect,
    MetricNeptuneClient,
)
from coa_metrics.opensearch_client import MetricOpenSearchClient
from coa_metrics.source_status import (
    SourceValidationUnavailableError,
    build_validation_lookup,
    check_source_approved,
    check_source_table_exists,
)
from coa_metrics.validator import (
    BLOCKING_CHECKS,
    check_data_modifying,
    check_select_shape,
    check_tier1_execution_shape,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_neptune: MetricNeptuneClient | None = None
_opensearch: MetricOpenSearchClient | None = None
_eventbridge: Any | None = None


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


def _get_eventbridge() -> Any:
    global _eventbridge  # noqa: PLW0603
    if _eventbridge is None:
        _eventbridge = boto3.client("events", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _eventbridge


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler for PUT /namespaces/{ns}/metrics/{name}."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    name = event.get("pathParameters", {}).get("name", "")
    caller = get_caller_identity(event)
    logger.info("update_metric", namespace=namespace, name=name, caller=caller)

    if not name:
        return api_response(400, {"message": "Metric name is required in path"})

    # Parse request body
    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if body is None:
        return api_response(400, {"message": "Request body is required"})

    # Validate via Smithy-generated Pydantic model
    try:
        request = UpdateMetricRequestContent.model_validate(body)
    except ValidationError as exc:
        return api_response(400, {"message": format_validation_error(exc)})

    # Check metric exists
    existing = _get_neptune().get_metric(namespace, name)
    if existing is None:
        return api_response(404, {"message": f"Metric '{name}' not found in namespace '{namespace}'"})

    # Build updated metric definition (includes SqlDialect validation)
    try:
        metric = _build_updated_metric(request, name, caller, existing)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    # Enforce APPROVED source and an existing sourceTable — hard 400s, matching
    # the UI's filter.
    data_source_lookup = None
    try:
        source_error = check_source_approved(namespace, metric.data_source_id)
        if source_error is None:
            data_source_lookup = build_validation_lookup(namespace)
            source_error = check_source_table_exists(
                namespace,
                metric.data_source_id,
                metric.source_table,
                data_source_lookup=data_source_lookup,
            )
    except SourceValidationUnavailableError as exc:
        logger.error("source_validation_unavailable", namespace=namespace, error=str(exc))
        return api_response(503, {"message": "Data source validation is unavailable — try again later"})
    if source_error:
        return api_response(400, {"message": source_error})

    # Validation findings — block only on the SQL syntax + shape checks
    # (BLOCKING_CHECKS), matching create and the serve firewall (#617/#1050).
    # Block by check name, not severity, so an ERROR-severity metadata check
    # (e.g. table_reference on provable absence) does not 400 here — it has its
    # own dedicated gate (check_source_table_exists) (Kun's review, !1133).
    # _validate_soft surfaces the check name under the "field" key.
    findings = _validate_soft(request, namespace, data_source_lookup=data_source_lookup)
    blocking = [f for f in findings if f.get("field") in BLOCKING_CHECKS]
    if blocking:
        return api_response(400, {"message": blocking[0]["message"], "errors": blocking})
    warnings = [f for f in findings if f.get("field") not in BLOCKING_CHECKS]

    # Resolve ontology concept names to full URIs (best-effort)
    if metric.ontology_concepts:
        try:
            metric.ontology_concepts = _get_neptune().resolve_class_uris(namespace, metric.ontology_concepts)
        except Exception as exc:
            logger.warning("ontology_concept_resolution_failed", error=str(exc))

    # Atomic update in Neptune (delete + insert)
    try:
        _get_neptune().update_metric(namespace, name, metric)
    except Exception as exc:
        logger.exception("neptune_update_failed", error=str(exc))
        return api_response(500, {"message": "Internal server error"})

    # Re-embed in OpenSearch (best-effort)
    try:
        _get_opensearch().embed_metric(namespace, metric)
    except Exception as exc:
        logger.warning("opensearch_embed_failed", error=str(exc), name=name)

    # Emit EventBridge event (best-effort)
    try:
        bus_name = os.environ.get("EVENTBRIDGE_BUS_NAME", "default")
        _get_eventbridge().put_events(
            Entries=[
                {
                    "Source": f"{EVENT_SOURCE_PREFIX}.metric-service",
                    "DetailType": f"{EVENT_SOURCE_PREFIX}.metric.published",
                    "Detail": json.dumps(
                        {
                            "action": "UPDATE",
                            "namespace": namespace,
                            "metricName": name,
                            "dataSourceId": metric.data_source_id,
                            "sourceTable": metric.source_table,
                        }
                    ),
                    "EventBusName": bus_name,
                }
            ]
        )
    except Exception as exc:
        logger.warning("eventbridge_emit_failed", error=str(exc), name=name)

    response_body: dict[str, Any] = {"metric": _metric_to_dict(metric)}
    if warnings:
        response_body["warnings"] = warnings

    return api_response(200, response_body)


def _build_updated_metric(
    request: UpdateMetricRequestContent,
    name: str,
    caller: str,
    existing: MetricDefinition,
) -> MetricDefinition:
    """Build updated MetricDefinition from validated Pydantic model. Name comes from path."""
    dialects = []
    for d in request.expression.dialects:
        dialect_name = d.dialect.upper()
        if dialect_name not in VALID_SQL_DIALECTS:
            raise ValueError(invalid_dialect_message(d.dialect))
        # Keep the persistence invariant independent from best-effort
        # catalog/ontology validation; see create_metric._build_metric_definition.
        dml_error = check_data_modifying(d.expression, dialect_name)
        if dml_error:
            raise ValueError(dml_error)
        shape_error = check_select_shape(d.expression, dialect_name)
        if shape_error:
            raise ValueError(shape_error)
        dialects.append(MetricDialect(dialect=dialect_name, expression=d.expression))

    tier1_error = check_tier1_execution_shape([{"dialect": d.dialect, "expression": d.expression} for d in dialects])
    if tier1_error:
        raise ValueError(tier1_error)

    ai_context = None
    if request.ai_context:
        ai_context = MetricAiContext(
            synonyms=request.ai_context.synonyms or [],
            instructions=request.ai_context.instructions or "",
            examples=request.ai_context.examples or [],
        )

    return MetricDefinition(
        name=name,
        description=request.description,
        expression_dialects=dialects,
        data_source_id=request.data_source_id,
        source_table=request.source_table,
        default_time_grain=request.default_time_grain.value if request.default_time_grain else None,
        unit=request.unit,
        return_type=request.return_type,
        ai_context=ai_context,
        ontology_concepts=request.ontology_concepts or [],
        defined_by=caller,
        effective_from=existing.effective_from or datetime.now(UTC).strftime("%Y-%m-%d"),
    )
