# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion pipeline step: delete every metric in the namespace.

Invokes the metric-api Lambda's bulk-delete-metrics route with
``deleteAll: true`` (cross-Lambda invoke). This authoritative,
namespace-scoped deletion does NOT depend on enumerating metric names from
DynamoDB — it directly purges all governed-metric triples from Neptune via
SPARQL DELETE WHERE. Idempotent and immune to DDB/Neptune drift.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import structlog

logger = structlog.get_logger(__name__)

_lambda_client: Any | None = None


def _get_lambda_client() -> Any:
    global _lambda_client  # noqa: PLW0603
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=os.environ["AWS_REGION"])
    return _lambda_client


def _invoke_metric_api(event: dict[str, Any]) -> dict[str, Any]:
    """Synchronously invoke the metric-api Lambda with an API-Gateway-shaped event."""
    function_name = os.environ["METRIC_API_FN_NAME"]
    resp = _get_lambda_client().invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8"),
    )
    payload = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"metric-api invocation failed: {payload}")
    return payload


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task: bulk-delete every metric for the namespace.

    Input: {"namespaceId": str}
    Output: {"metricsDeleted": int}

    Raises on failure so the state machine's retry/catch policy applies.
    """
    namespace_id = event["namespaceId"]

    invoke_event = {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/bulk-delete-metrics",
        "pathParameters": {"namespaceId": namespace_id},
        "body": json.dumps({"deleteAll": True}),
    }
    resp = _invoke_metric_api(invoke_event)
    if resp.get("statusCode") != 200:
        raise RuntimeError(f"Failed to bulk-delete metrics for {namespace_id}: {resp}")

    body = json.loads(resp["body"])
    deleted = body.get("deleted", 0)
    logger.info("namespace_metrics_deleted", namespace_id=namespace_id, count=deleted)
    return {"metricsDeleted": deleted}
