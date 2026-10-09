# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Create Metric Lambda handler.

POST /namespaces/{namespaceId}/metrics → 201 Created

Flow:
1. Parse and validate request body (via Smithy-generated Pydantic model)
2. Validate dialect values against SqlDialect enum
3. Write to Neptune as :GoverningMetric subclass
4. Embed in OpenSearch for Tier 0 routing
5. Emit EventBridge event (coa.metric.published, action=CREATE)
6. Return 201 with metric definition + any soft warnings
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
from coa_control_plane_server.models.create_metric_request_content import (
    CreateMetricRequestContent,
)
from pydantic import ValidationError

from coa_metrics.api.validation_errors import format_validation_error
from coa_metrics.constants import VALID_SQL_DIALECTS, invalid_dialect_message
from coa_metrics.lookups import LOOKUP_NOT_PROVIDED, LookupArgument
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

# ── Singleton clients (reused across warm invocations) ──────────────────

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


# ── Handler ─────────────────────────────────────────────────────────────


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler for POST /namespaces/{ns}/metrics."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    caller = get_caller_identity(event)
    logger.info("create_metric", namespace=namespace, caller=caller)

    # Parse request body
    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if body is None:
        return api_response(400, {"message": "Request body is required"})

    # Validate via Smithy-generated Pydantic model
    try:
        request = CreateMetricRequestContent.model_validate(body)
    except ValidationError as exc:
        return api_response(400, {"message": format_validation_error(exc)})

    # Build metric definition (includes SqlDialect validation)
    try:
        metric = _build_metric_definition(request, caller)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    # Enforce APPROVED source and an existing sourceTable — hard 400s, matching
    # the UI's filter. The UI only offers APPROVED/COMPLETED sources and catalog
    # tables; the API must not be a bypass.
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

    # Check for conflicts (metric already exists)
    existing = _get_neptune().get_metric(namespace, metric.name)
    if existing is not None:
        return api_response(409, {"message": f"Metric '{metric.name}' already exists in namespace '{namespace}'"})

    # Validation findings. Only the SQL syntax + shape checks (1/1b) gate
    # onboarding: the serve-time SQL firewall enforces the same SELECT-only rule,
    # so a metric that fails them would be persisted only to fail at query time
    # (#617/#1050). Block by check name (BLOCKING_CHECKS), not severity — the
    # metadata checks (2-6) can also be ERROR-severity (e.g. table_reference on
    # provable absence) but have their own dedicated gate (check_source_table_exists
    # above) and must stay non-blocking here (Kun's review, !1133). Note
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

    # Write to Neptune
    try:
        _get_neptune().create_metric(namespace, metric)
    except Exception as exc:
        logger.exception("neptune_write_failed", error=str(exc))
        return api_response(500, {"message": "Internal server error"})

    # Embed in OpenSearch (best-effort — don't fail the request)
    try:
        _get_opensearch().embed_metric(namespace, metric)
    except Exception as exc:
        logger.warning("opensearch_embed_failed", error=str(exc), name=metric.name)

    # Emit EventBridge event (best-effort)
    try:
        _emit_event(namespace, metric, action="CREATE")
    except Exception as exc:
        logger.warning("eventbridge_emit_failed", error=str(exc), name=metric.name)

    # Build response
    response_body: dict[str, Any] = {
        "metric": _metric_to_dict(metric),
    }
    if warnings:
        response_body["warnings"] = warnings

    return api_response(201, response_body)


# ── Validation ──────────────────────────────────────────────────────────


def _validate_soft(
    request: CreateMetricRequestContent,
    namespace: str,
    *,
    data_source_lookup: LookupArgument = LOOKUP_NOT_PROVIDED,
) -> list[dict[str, str]]:
    """Run soft validation — delegates to shared module."""
    from coa_metrics.validate_soft import validate_soft

    metric_body = {
        "expression": {
            "dialects": [{"dialect": d.dialect, "expression": d.expression} for d in request.expression.dialects],
        },
        "dataSourceId": request.data_source_id,
        "sourceTable": request.source_table,
        "ontologyConcepts": request.ontology_concepts or [],
    }
    if data_source_lookup is LOOKUP_NOT_PROVIDED:
        return validate_soft(metric_body, namespace)
    return validate_soft(metric_body, namespace, data_source_lookup=data_source_lookup)


# ── Helpers ─────────────────────────────────────────────────────────────


def _build_metric_definition(request: CreateMetricRequestContent, caller: str) -> MetricDefinition:
    """Convert validated Pydantic request model into a MetricDefinition dataclass.

    Performs additional SqlDialect validation that the generated model
    doesn't enforce (dialect is a constrained string, not an enum, in codegen).
    """
    dialects = []
    for d in request.expression.dialects:
        dialect_name = d.dialect.upper()
        if dialect_name not in VALID_SQL_DIALECTS:
            raise ValueError(invalid_dialect_message(d.dialect))
        # Persistence must not depend on best-effort advisory validation.
        # validate_soft includes catalog/ontology lookups and intentionally
        # fails open when those are unavailable; enforce the pure SQL
        # invariants here so an executable-shape error cannot be discarded
        # together with an unrelated lookup failure.
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
        name=request.name,
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
        effective_from=datetime.now(UTC).strftime("%Y-%m-%d"),
    )


def _metric_to_dict(metric: MetricDefinition) -> dict[str, Any]:
    """Convert MetricDefinition to API response dict (camelCase keys)."""
    result: dict[str, Any] = {
        "name": metric.name,
        "description": metric.description,
        "expression": {
            "dialects": [{"dialect": d.dialect, "expression": d.expression} for d in metric.expression_dialects],
        },
        "dataSourceId": metric.data_source_id,
        "sourceTable": metric.source_table,
    }
    if metric.default_time_grain:
        result["defaultTimeGrain"] = metric.default_time_grain
    if metric.unit:
        result["unit"] = metric.unit
    if metric.return_type:
        result["returnType"] = metric.return_type
    if metric.ai_context:
        result["aiContext"] = {
            "synonyms": metric.ai_context.synonyms,
            "instructions": metric.ai_context.instructions,
            "examples": metric.ai_context.examples,
        }
    if metric.ontology_concepts:
        result["ontologyConcepts"] = metric.ontology_concepts
    if metric.defined_by:
        result["definedBy"] = metric.defined_by
    if metric.effective_from:
        result["effectiveFrom"] = metric.effective_from
    return result


def _emit_event(namespace: str, metric: MetricDefinition, action: str) -> None:
    """Emit an EventBridge event for metric lifecycle changes."""
    bus_name = os.environ.get("EVENTBRIDGE_BUS_NAME", "default")
    _get_eventbridge().put_events(
        Entries=[
            {
                "Source": f"{EVENT_SOURCE_PREFIX}.metric-service",
                "DetailType": f"{EVENT_SOURCE_PREFIX}.metric.published",
                "Detail": json.dumps(
                    {
                        "action": action,
                        "namespace": namespace,
                        "metricName": metric.name,
                        "dataSourceId": metric.data_source_id,
                        "sourceTable": metric.source_table,
                    }
                ),
                "EventBusName": bus_name,
            }
        ]
    )
    logger.info("EventBridge event emitted", action=action, namespace=namespace, name=metric.name)
