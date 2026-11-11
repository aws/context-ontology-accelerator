# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Discovery tools — list_metrics and describe_schema, delegated to backend Lambdas."""

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

    The backend does not apply the ``status`` filter, so the parameter is
    omitted rather than forwarded as a no-op that would make the response look
    filtered when it is not.

    ``max_results`` is capped at 200 (the ListMetrics page size).
    """
    query_params: dict[str, str] = {"maxResults": str(min(max_results, 200))}
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

    ``max_results`` is capped at 500. ``class_filter`` is a class URI or prefix.
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
    # Project to the Smithy DescribeSchema shape; the backend adds a namespace echo.
    return {
        "classes": raw.get("classes", []) if isinstance(raw, dict) else [],
        "ontologyVersion": raw.get("ontologyVersion") if isinstance(raw, dict) else None,
    }
