# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Get Metric Lambda handler.

GET /namespaces/{namespaceId}/metrics/{name} → 200 OK

Returns the full metric definition from Neptune.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.logging import setup_logging
from coa_common.response import api_response

from coa_metrics.api.contract import check_metric_name
from coa_metrics.api.create_metric import _metric_to_dict
from coa_metrics.neptune_client import MetricNeptuneClient

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_neptune: MetricNeptuneClient | None = None


def _get_neptune() -> MetricNeptuneClient:
    global _neptune  # noqa: PLW0603
    if _neptune is None:
        _neptune = MetricNeptuneClient()
    return _neptune


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler for GET /namespaces/{ns}/metrics/{name}."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    name = event.get("pathParameters", {}).get("name", "")
    logger.info("get_metric", namespace=namespace, name=name)

    if not name:
        return api_response(400, {"message": "Metric name is required"})
    name_error = check_metric_name(name)
    if name_error:
        return api_response(400, {"message": name_error})

    metric = _get_neptune().get_metric(namespace, name)
    if metric is None:
        return api_response(404, {"message": "Metric not found in namespace"})

    return api_response(200, {"metric": _metric_to_dict(metric)})
