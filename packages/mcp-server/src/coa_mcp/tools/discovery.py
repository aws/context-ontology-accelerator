# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery tools — list_metrics and describe_schema.

Delegated to backend Lambdas via direct invocation (bypassing API Gateway).
The MCP server is a thin protocol adapter; all DB/Bedrock access is in the
Lambdas.

Contract note: the endpoints and query-param names below match the Smithy
operations on the data-layer/metric-service services — ``ListMetrics`` and
``DescribeSchema``. Passing the same params to either surface yields the
same result.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.constants import (
    DESCRIBE_SCHEMA_RESOURCE,
    LIST_METRICS_RESOURCE,
    METRIC_SERVICE_LAMBDA_ARN_ENV,
    ONTOLOGY_PROXY_LAMBDA_ARN_ENV,
)

from ..clients.lambda_client import LambdaClient

logger = structlog.get_logger(__name__)

METRIC_SERVICE_LAMBDA = os.environ.get(METRIC_SERVICE_LAMBDA_ARN_ENV, "")
ONTOLOGY_PROXY_LAMBDA = os.environ.get(ONTOLOGY_PROXY_LAMBDA_ARN_ENV, "")


async def list_metrics(
    lambda_client: LambdaClient,
    namespace_id: str,
    bearer_token: str,
    *,
    max_results: int = 100,
    next_token: str | None = None,
    authorizer_context: dict | None = None,
) -> dict[str, Any]:
    """List governed metric definitions via Lambda invocation.

    The Smithy ``ListMetrics`` operation declares a ``status`` filter, but the
    metric-service backend does not yet apply it (see MR !939 review comment
    ``b6dea1de``). Rather than accept ``status`` here and silently forward it as
    a no-op — making the response look filtered when it is not — the parameter
    is intentionally omitted until the backend can honour it.

    Args:
        lambda_client: Lambda client for direct invocation.
        namespace_id: The namespace to query.
        bearer_token: Caller's JWT for identity propagation.
        max_results: Maximum metrics to return (default 100, max 1000).
        next_token: Continuation token from a prior response (``nextToken``).
        authorizer_context: Optional authorizer context for the synthetic event.

    Returns:
        Dict with ``metrics`` list and, when the backend paginates, a
        ``nextToken`` string to pass on the next call.
    """
    query_params: dict[str, str] = {"maxResults": str(min(max_results, 1000))}
    if next_token:
        query_params["nextToken"] = next_token

    return await lambda_client.invoke_api(
        function_name=METRIC_SERVICE_LAMBDA,
        method="GET",
        resource=LIST_METRICS_RESOURCE,
        path_params={"namespaceId": namespace_id},
        query_params=query_params,
        bearer_token=bearer_token,
        authorizer_context=authorizer_context,
    )


async def describe_schema(
    lambda_client: LambdaClient,
    namespace_id: str,
    bearer_token: str,
    *,
    max_results: int = 100,
    include_properties: bool = True,
    class_filter: str | None = None,
    authorizer_context: dict | None = None,
) -> dict[str, Any]:
    """Describe ontology classes and properties via Lambda invocation.

    Uses the ``/namespaces/{namespaceId}/schema`` Smithy path — the same URI
    data-layer's ``DescribeSchema`` operation targets — so the two surfaces
    stay on one endpoint. The path was previously ``/graph/schema`` on the
    ontology-engine service; the ontology-engine api-proxy accepts both, so
    the change is transparent server-side.

    Args:
        lambda_client: Lambda client for direct invocation.
        namespace_id: The namespace to query.
        bearer_token: Caller's JWT for identity propagation.
        max_results: Maximum classes to return (default 100, max 500).
        include_properties: Whether to include properties (default True).
        class_filter: Optional class URI or prefix filter (matches Smithy input).
        authorizer_context: Optional authorizer context for the synthetic event.

    Returns:
        Dict with ``classes`` and ``ontologyVersion``.
    """
    query_params: dict[str, str] = {
        "maxResults": str(min(max_results, 500)),
        "includeProperties": str(include_properties).lower(),
    }
    if class_filter:
        query_params["classFilter"] = class_filter

    raw = await lambda_client.invoke_api(
        function_name=ONTOLOGY_PROXY_LAMBDA,
        method="GET",
        resource=DESCRIBE_SCHEMA_RESOURCE,
        path_params={"namespaceId": namespace_id},
        query_params=query_params,
        bearer_token=bearer_token,
        authorizer_context=authorizer_context,
    )
    # Project to the Smithy ``DescribeSchema`` output shape so MCP and
    # data-layer return byte-identical bodies for the same input. The
    # ontology-engine backend includes an extra ``namespace`` echo that
    # data-layer's ``_handle_describe_schema`` strips; without this
    # projection MCP would leak that field on top of the Smithy contract.
    return {
        "classes": raw.get("classes", []) if isinstance(raw, dict) else [],
        "ontologyVersion": raw.get("ontologyVersion") if isinstance(raw, dict) else None,
    }
