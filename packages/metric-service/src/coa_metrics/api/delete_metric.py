# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Delete Metric Lambda handler.

DELETE /namespaces/{namespaceId}/metrics/{name} → 204 No Content

Removes metric from Neptune, OpenSearch, and emits EventBridge event.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import structlog
from coa_common.constants import EVENT_SOURCE_PREFIX
from coa_common.logging import setup_logging
from coa_common.response import api_response

from coa_metrics.api.contract import check_metric_name
from coa_metrics.neptune_client import MetricNeptuneClient
from coa_metrics.opensearch_client import MetricOpenSearchClient

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
    """API Gateway proxy-integration Lambda handler for DELETE /namespaces/{ns}/metrics/{name}."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    name = event.get("pathParameters", {}).get("name", "")
    logger.info("delete_metric", namespace=namespace, name=name)

    if not name:
        return api_response(400, {"message": "Metric name is required in path"})
    name_error = check_metric_name(name)
    if name_error:
        return api_response(400, {"message": name_error})

    # Delete from Neptune (returns False if not found)
    deleted = _get_neptune().delete_metric(namespace, name)
    if not deleted:
        return api_response(404, {"message": "Metric not found in namespace"})

    # Delete OpenSearch embedding (best-effort)
    try:
        _get_opensearch().delete_metric_embedding(namespace, name)
    except Exception as exc:
        logger.warning("opensearch_delete_failed", error=str(exc), name=name)

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
                            "action": "DELETE",
                            "namespace": namespace,
                            "metricName": name,
                        }
                    ),
                    "EventBusName": bus_name,
                }
            ]
        )
    except Exception as exc:
        logger.warning("eventbridge_emit_failed", error=str(exc), name=name)

    return api_response(204, {})
