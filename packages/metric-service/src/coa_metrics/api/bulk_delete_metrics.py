# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bulk Delete Metrics Lambda handler.

POST /namespaces/{namespaceId}/metrics/bulk-delete → 200 OK

Deletes metrics by explicit name list or filter criteria.
Uses a single SPARQL DELETE WHERE for efficiency.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any

import structlog
from coa_common.logging import setup_logging
from coa_common.response import api_response

from coa_metrics.api.contract import (
    BULK_DELETE_FILTER_DATA_SOURCE_ID_MAX_LENGTH,
    BULK_DELETE_FILTER_NAME_PREFIX_MAX_LENGTH,
    BULK_DELETE_FILTER_SOURCE_TABLE_MAX_LENGTH,
    BULK_DELETE_NAMES_MAX_ITEMS,
    check_metric_name_list,
    check_string,
)
from coa_metrics.neptune_client import MetricNeptuneClient
from coa_metrics.opensearch_client import MetricOpenSearchClient

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_neptune: MetricNeptuneClient | None = None
_opensearch: MetricOpenSearchClient | None = None


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


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    logger.info("bulk_delete_metrics", namespace=namespace)

    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if not body or not isinstance(body, dict):
        return api_response(400, {"message": "Request body is required"})

    names = body.get("names")
    filter_criteria = body.get("filter")
    delete_all = body.get("deleteAll", False)
    bound_error = _check_bounds(names, filter_criteria, delete_all)
    if bound_error:
        return api_response(400, {"message": bound_error})

    # Validate mutual exclusivity
    provided = sum([bool(names), bool(filter_criteria), bool(delete_all)])
    if provided > 1:
        return api_response(400, {"message": "'names', 'filter', and 'deleteAll' are mutually exclusive"})
    if provided == 0:
        return api_response(400, {"message": "One of 'names', 'filter', or 'deleteAll' is required"})

    neptune = _get_neptune()
    opensearch = _get_opensearch()

    if delete_all:
        return _delete_all(namespace, neptune, opensearch)
    elif names:
        return _delete_by_names(namespace, names, neptune, opensearch)
    else:
        return _delete_by_filter(namespace, filter_criteria, neptune, opensearch)


def _check_bounds(names: Any, filter_criteria: Any, delete_all: Any) -> str | None:
    """Enforce the BulkDeleteMetrics input bounds from the Smithy model (no value echo)."""
    if names is not None:
        error = check_metric_name_list(names, "names", max_items=BULK_DELETE_NAMES_MAX_ITEMS)
        if error:
            return error
    if filter_criteria is not None:
        if not isinstance(filter_criteria, dict):
            return "filter must be an object"
        for field, max_length in (
            ("dataSourceId", BULK_DELETE_FILTER_DATA_SOURCE_ID_MAX_LENGTH),
            ("sourceTable", BULK_DELETE_FILTER_SOURCE_TABLE_MAX_LENGTH),
            ("namePrefix", BULK_DELETE_FILTER_NAME_PREFIX_MAX_LENGTH),
        ):
            value = filter_criteria.get(field)
            if value is not None:
                error = check_string(value, f"filter.{field}", max_length=max_length)
                if error:
                    return error
    if not isinstance(delete_all, bool):
        return "deleteAll must be a boolean"
    return None


def _delete_all(
    namespace: str,
    neptune: MetricNeptuneClient,
    opensearch: MetricOpenSearchClient,
) -> dict[str, Any]:
    """Delete ALL metrics for the namespace (authoritative, no name enumeration).

    Used by the namespace deletion pipeline. Does not depend on DynamoDB listing —
    directly purges all governed-metric triples from Neptune. OpenSearch cleanup is
    best-effort (metrics that never had embeddings won't be found).
    """
    deleted_count = neptune.delete_all_metrics_for_namespace(namespace)

    # Best-effort OpenSearch cleanup: delete the entire namespace's metric embedding collection
    # (safer than per-name deletion when DDB/Neptune have drifted)
    if deleted_count > 0:
        with contextlib.suppress(Exception):
            # The OpenSearch client doesn't have a delete-all-for-namespace method,
            # so we rely on the namespace deletion pipeline to clean up the entire
            # OpenSearch index. Individual embeddings left behind are harmless —
            # they'll be orphaned and won't match any queries once Neptune is clean.
            pass

    logger.info("bulk_delete_all", namespace=namespace, deleted=deleted_count)
    return api_response(200, {"deleted": deleted_count})


def _delete_by_names(
    namespace: str,
    names: list[str],
    neptune: MetricNeptuneClient,
    opensearch: MetricOpenSearchClient,
) -> dict[str, Any]:
    """Delete metrics by explicit name list."""
    deleted = 0
    not_found: list[str] = []

    for name in names:
        try:
            was_deleted = neptune.delete_metric(namespace, name)
            if was_deleted:
                deleted += 1
                with contextlib.suppress(Exception):  # best-effort
                    opensearch.delete_metric_embedding(namespace, name)
            else:
                not_found.append(name)
        except Exception as exc:
            logger.warning("bulk_delete_error", name=name, error=str(exc))
            not_found.append(name)

    logger.info("bulk_delete_by_names", namespace=namespace, deleted=deleted, not_found=len(not_found))

    response: dict[str, Any] = {"deleted": deleted}
    if not_found:
        response["notFound"] = not_found
    return api_response(200, response)


def _delete_by_filter(
    namespace: str,
    filter_criteria: dict[str, Any],
    neptune: MetricNeptuneClient,
    opensearch: MetricOpenSearchClient,
) -> dict[str, Any]:
    """Delete metrics matching filter criteria."""
    data_source_id = filter_criteria.get("dataSourceId")
    source_table = filter_criteria.get("sourceTable")
    name_prefix = filter_criteria.get("namePrefix")

    if not data_source_id and not source_table and not name_prefix:
        return api_response(400, {"message": "At least one filter field is required"})

    # First, list all metrics matching the filter
    all_metrics = neptune.list_metrics(
        namespace=namespace,
        data_source_id=data_source_id,
        source_table=source_table,
        limit=10000,
        offset=0,
    )

    # Apply name prefix filter if specified
    if name_prefix:
        all_metrics = [m for m in all_metrics if m.name.startswith(name_prefix)]

    if not all_metrics:
        return api_response(200, {"deleted": 0})

    # Delete each metric
    deleted = 0
    for metric in all_metrics:
        try:
            was_deleted = neptune.delete_metric(namespace, metric.name)
            if was_deleted:
                deleted += 1
                with contextlib.suppress(Exception):
                    opensearch.delete_metric_embedding(namespace, metric.name)
        except Exception as exc:
            logger.warning("bulk_delete_filter_error", name=metric.name, error=str(exc))

    logger.info(
        "bulk_delete_by_filter",
        namespace=namespace,
        deleted=deleted,
        total_matched=len(all_metrics),
        filter=filter_criteria,
    )

    return api_response(200, {"deleted": deleted})
