# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion pipeline step: delete every source in the namespace.

Invokes the real ``sources-api`` Lambda (cross-Lambda invoke, not a raw DDB
sweep) so each source's full teardown runs: S3 objects, OpenSearch/Neptune
embeddings, DataZone assets, and Glue/Athena federated resources for
database sources. Reusing the existing DELETE /namespaces/{ns}/sources/{id}
code path avoids duplicating (and risking drift from) that cleanup logic.

Lists sources via the same Lambda's LIST route, then deletes each one via
its DELETE route. Pages through all sources (the sources API caps each LIST
call at maxResults).
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import structlog

from coa_control_plane.namespace.cleanup import delete_graphrag_indexes

logger = structlog.get_logger(__name__)

_MAX_RESULTS = 100
_MAX_PAGES = 1000

_lambda_client: Any | None = None


def _get_lambda_client() -> Any:
    global _lambda_client  # noqa: PLW0603
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=os.environ["AWS_REGION"])
    return _lambda_client


def _invoke_sources_api(event: dict[str, Any]) -> dict[str, Any]:
    """Synchronously invoke the sources-api Lambda with an API-Gateway-shaped event."""
    function_name = os.environ["SOURCES_API_FN_NAME"]
    resp = _get_lambda_client().invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode("utf-8"),
    )
    payload = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        raise RuntimeError(f"sources-api invocation failed: {payload}")
    return payload


def _list_sources_page(namespace_id: str, next_token: str | None) -> dict[str, Any]:
    qs: dict[str, str] = {"maxResults": str(_MAX_RESULTS)}
    if next_token:
        qs["nextToken"] = next_token
    event = {
        "httpMethod": "GET",
        "resource": "/namespaces/{namespaceId}/sources",
        "pathParameters": {"namespaceId": namespace_id},
        "queryStringParameters": qs,
    }
    resp = _invoke_sources_api(event)
    if resp.get("statusCode") != 200:
        raise RuntimeError(f"Failed to list sources for {namespace_id}: {resp}")
    return json.loads(resp["body"])


def _delete_source(namespace_id: str, source_id: str) -> None:
    event = {
        "httpMethod": "DELETE",
        "resource": "/namespaces/{namespaceId}/sources/{sourceId}",
        "pathParameters": {"namespaceId": namespace_id, "sourceId": source_id},
    }
    resp = _invoke_sources_api(event)
    # 200/202/404 are all acceptable outcomes (404 = already gone, race-safe).
    if resp.get("statusCode") not in (200, 202, 404):
        raise RuntimeError(f"Failed to delete source {source_id} in {namespace_id}: {resp}")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task: delete every source for the namespace.

    Input: {"namespaceId": str}
    Output: {"sourcesDeleted": int, "graphragIndexesDeleted": list[str]}

    Raises on any failure so the state machine's retry/catch policy applies
    (see the pipeline construct — this step retries 3x, then the chain goes
    to MarkFailed and the namespace ends in DELETE_FAILED).

    After the last source is gone, drops the namespace's GraphRAG doc-KG vector
    indexes. Those are namespace-scoped (all doc sources share one GraphRAG
    tenant), so an individual source delete must not remove them — it only
    clears its own documents from the shared index. This step is where "no
    sources remain in this namespace" becomes true, so it is where they can be
    dropped. Nothing deleted them before, leaving one index set per deleted
    namespace against a collection AOSS caps at 1000 indexes.
    """
    namespace_id = event["namespaceId"]
    deleted = 0
    next_token: str | None = None

    for _ in range(_MAX_PAGES):
        page = _list_sources_page(namespace_id, next_token)
        for item in page.get("items", []):
            source_id = item["sourceId"]
            _delete_source(namespace_id, source_id)
            deleted += 1
        next_token = page.get("nextToken")
        if not next_token:
            break
    else:
        raise RuntimeError(f"Exceeded {_MAX_PAGES} pages listing sources for {namespace_id}")

    logger.info("namespace_sources_deleted", namespace_id=namespace_id, count=deleted)

    graphrag_removed = delete_graphrag_indexes(namespace_id)
    return {"sourcesDeleted": deleted, "graphragIndexesDeleted": graphrag_removed}
