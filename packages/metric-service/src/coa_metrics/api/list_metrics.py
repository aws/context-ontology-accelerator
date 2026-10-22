# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""List Metrics Lambda handler.

GET /namespaces/{namespaceId}/metrics → 200 OK

Returns paginated list of metrics with optional filters.
Query params: dataSourceId, sourceTable, maxResults, nextToken
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.logging import setup_logging
from coa_common.response import api_response

from coa_metrics.api.contract import (
    LIST_METRICS_MAX_RESULTS_MAX,
    LIST_METRICS_MAX_RESULTS_MIN,
    SOURCE_TABLE_MAX_LENGTH,
    SOURCE_TABLE_MIN_LENGTH,
    check_data_source_id,
    check_string,
)
from coa_metrics.api.create_metric import _metric_to_dict
from coa_metrics.neptune_client import MetricNeptuneClient

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = LIST_METRICS_MAX_RESULTS_MAX
# Largest offset a nextToken may carry. Far beyond any listing this service
# pages through, and it keeps a long digit string from reaching int() (which
# refuses more than 4300 digits) or the graph query's OFFSET clause.
_MAX_OFFSET = 2**31 - 1

_neptune: MetricNeptuneClient | None = None


def _get_neptune() -> MetricNeptuneClient:
    global _neptune  # noqa: PLW0603
    if _neptune is None:
        _neptune = MetricNeptuneClient()
    return _neptune


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler for GET /namespaces/{ns}/metrics."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    query_params = event.get("queryStringParameters") or {}
    logger.info("list_metrics", namespace=namespace, filters=query_params)

    # Parse pagination. maxResults above the page cap is clamped rather than
    # rejected: existing callers (the MCP list_metrics tool) pass larger values
    # and page with nextToken. Non-integer or < 1 values are contract violations.
    raw_limit = query_params.get("maxResults")
    if raw_limit is None:
        limit = DEFAULT_PAGE_SIZE
    else:
        try:
            limit = int(raw_limit)
        except (ValueError, TypeError):
            return api_response(400, {"message": "maxResults must be an integer"})
        if limit < LIST_METRICS_MAX_RESULTS_MIN:
            return api_response(400, {"message": f"maxResults must be at least {LIST_METRICS_MAX_RESULTS_MIN}"})
        limit = min(limit, MAX_PAGE_SIZE)

    # nextToken is the offset of the next page (simple offset-based pagination
    # for Neptune). A token this service did not mint is rejected rather than
    # silently restarting at the first page.
    offset = 0
    raw_token = query_params.get("nextToken")
    if raw_token is not None:
        if (
            len(raw_token) > len(str(_MAX_OFFSET))
            or not (raw_token.isascii() and raw_token.isdigit())
            or int(raw_token) > _MAX_OFFSET
        ):
            return api_response(400, {"message": "Invalid nextToken"})
        offset = int(raw_token)

    # Parse filters
    data_source_id = query_params.get("dataSourceId")
    source_table = query_params.get("sourceTable")
    filter_error = None
    if data_source_id is not None:
        filter_error = check_data_source_id(data_source_id)
    if filter_error is None and source_table is not None:
        filter_error = check_string(
            source_table, "sourceTable", min_length=SOURCE_TABLE_MIN_LENGTH, max_length=SOURCE_TABLE_MAX_LENGTH
        )
    if filter_error:
        return api_response(400, {"message": filter_error})

    # Query Neptune
    metrics = _get_neptune().list_metrics(
        namespace=namespace,
        data_source_id=data_source_id,
        source_table=source_table,
        limit=limit + 1,  # fetch one extra to determine if there's a next page
        offset=offset,
    )

    # Determine pagination
    has_next = len(metrics) > limit
    if has_next:
        metrics = metrics[:limit]
    next_token = str(offset + limit) if has_next else None

    response_body: dict[str, Any] = {
        "metrics": [_metric_to_dict(m) for m in metrics],
    }
    if next_token:
        response_body["nextToken"] = next_token

    return api_response(200, response_body)
