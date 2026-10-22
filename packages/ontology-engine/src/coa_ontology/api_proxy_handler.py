# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lambda proxy handler — forwards API Gateway requests to the ontology-engine ECS service.

Translates API Gateway Lambda proxy events into HTTP requests to the
ontology-engine running on ECS (reachable via Service Connect at
ONTOLOGY_ENGINE_ENDPOINT). Returns the ECS response as a Lambda proxy response.

Environment:
    ONTOLOGY_ENGINE_ENDPOINT: Internal endpoint (e.g. http://ontology-engine:8001)
"""

from __future__ import annotations

import base64
import json
import logging
import os
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode

from coa_common import resolve_region
from coa_common.constants import validate_namespace_id
from coa_common.idempotency import idempotent_create
from coa_common.logging import setup_logging

# JSON logs for this module and for coa_common.idempotency's structlog events.
setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
_log = logging.getLogger(__name__)

ENDPOINT = os.environ["ONTOLOGY_ENGINE_ENDPOINT"].rstrip("/")

# Backend read timeout for ordinary routes: just under API Gateway's 29 s
# integration timeout and the 30 s budget of direct Lambda callers (the MCP
# server and the data layer invoke describe_schema directly), so a slow backend
# surfaces as this handler's 504 rather than the caller's own timeout.
_BACKEND_TIMEOUT_SECONDS = 28
# Tighter read timeout for the ``clientToken`` POSTs in ``_IDEMPOTENT_POSTS``:
# the handler must return its 504 — which lets the idempotency wrapper release
# its claim — with margin before this Lambda's own timeout, rather than being
# killed mid-request with the claim left IN_PROGRESS, locking the caller's
# clientToken out of retries.
_IDEMPOTENT_BACKEND_TIMEOUT_SECONDS = 25

# Fixed client-facing messages for transport failures; the detail is logged.
_MSG_BACKEND_UNREACHABLE = "The ontology service is unavailable. Retry the request."
_MSG_BACKEND_TIMEOUT = "The ontology service did not respond in time. Retry the request."
_MSG_BACKEND_ERROR = "The ontology service request failed."

_ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")

_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": _ALLOWED_ORIGIN,
}

# POST operations made retry-safe by an optional ``clientToken`` (Smithy
# @idempotencyToken), keyed resource -> operation name. Enforced here, not in the
# ECS service: only this Lambda sees the API Gateway authorizer context the
# idempotency key needs. Each starts a new job per call, so an unguarded retry
# would start a duplicate. The other mutating POSTs routed here are naturally
# idempotent; their Smithy docs say why.
_IDEMPOTENT_POSTS = {
    "/namespaces/{namespaceId}/induce": "StartInduction",
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-from-s3": "IngestOntologyFromS3",
    "/namespaces/{namespaceId}/ontology/foundational/{key}/load": "LoadFoundationalOntology",
    "/namespaces/{namespaceId}/proposals/{proposalId}/validate": "ValidateProposal",
    "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints": "InferConstraints",
}

# Map API Gateway paths to ontology-engine paths
_PATH_MAP = {
    "/namespaces/{namespaceId}/induce": "/induce/",
    "/namespaces/{namespaceId}/induce/jobs": "/induce/jobs",
    "/namespaces/{namespaceId}/induce/jobs/{jobId}": "/induce/jobs/{jobId}",
    "/namespaces/{namespaceId}/induce/datasources": "/induce/datasources",
    "/namespaces/{namespaceId}/induce/datasources/induced": "/induce/datasources/induced",
    "/namespaces/{namespaceId}/proposals": "/ontology/proposals/",
    "/namespaces/{namespaceId}/proposals/{proposalId}": "/ontology/proposals/{proposalId}",
    "/namespaces/{namespaceId}/proposals/{proposalId}/accept": "/ontology/proposals/{proposalId}/accept",
    "/namespaces/{namespaceId}/proposals/{proposalId}/cancel": "/ontology/proposals/{proposalId}/cancel",
    "/namespaces/{namespaceId}/proposals/{proposalId}/upload-url": "/ontology/proposals/{proposalId}/upload-url",
    "/namespaces/{namespaceId}/proposals/{proposalId}/validate": "/ontology/proposals/{proposalId}/validate",
    "/namespaces/{namespaceId}/proposals/{proposalId}/repair-datatypes": (
        "/ontology/proposals/{proposalId}/repair-datatypes"
    ),
    "/namespaces/{namespaceId}/proposals/{proposalId}/validate/jobs/{jobId}": (
        "/ontology/proposals/{proposalId}/validate/jobs/{jobId}"
    ),
    "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints": (
        "/ontology/proposals/{proposalId}/infer-constraints"
    ),
    "/namespaces/{namespaceId}/proposals/{proposalId}/infer-constraints/jobs/{jobId}": (
        "/ontology/proposals/{proposalId}/infer-constraints/jobs/{jobId}"
    ),
    "/namespaces/{namespaceId}/proposals/{proposalId}/compile-constraints": (
        "/ontology/proposals/{proposalId}/compile-constraints"
    ),
    "/namespaces/{namespaceId}/proposals/update": "/ontology/proposals/update",
    "/namespaces/{namespaceId}/proposals/reject": "/ontology/proposals/reject",
    "/namespaces/{namespaceId}/graph/search": "/graph/search",
    "/namespaces/{namespaceId}/graph/ontology-overview": "/graph/ontology-overview",
    # ``/namespaces/{namespaceId}/schema`` is the canonical Smithy DescribeSchema
    # URI; ``/graph/schema`` is the legacy alias kept for the ontology-engine
    # UI/tests. Both route to the same backend so MCP and data-layer converge
    # on one endpoint.
    "/namespaces/{namespaceId}/schema": "/graph/schema",
    "/namespaces/{namespaceId}/graph/schema": "/graph/schema",
    # Vertex lookups use ``?uri=`` query param (not path) so OWL IRIs with
    # slashes and ``#`` fragments don't need double URL-encoding.
    "/namespaces/{namespaceId}/graph/class": "/graph/class/",
    "/namespaces/{namespaceId}/graph/object-property": "/graph/object-property/",
    "/namespaces/{namespaceId}/graph/datatype-property": "/graph/datatype-property/",
    "/namespaces/{namespaceId}/ontologies": "/ontologies/",
    "/namespaces/{namespaceId}/ontologies/{ontologyId}": "/ontologies/{ontologyId}",
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/download": "/ontologies/{ontologyId}/download",
    "/namespaces/{namespaceId}/ontologies/upload-url": "/ontologies/upload-url",
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/fetch": "/ontologies/{ontologyId}/fetch",
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-from-s3": ("/ontologies/{ontologyId}/ingest-from-s3"),
    "/namespaces/{namespaceId}/ontologies/{ontologyId}/ingest-status/{jobId}": (
        "/ontologies/{ontologyId}/ingest-status/{jobId}"
    ),
    "/namespaces/{namespaceId}/embeddings": "/embeddings/",
    "/namespaces/{namespaceId}/embeddings/batch": "/embeddings/batch",
    "/namespaces/{namespaceId}/embeddings/search": "/embeddings/search",
    "/namespaces/{namespaceId}/embeddings/by-entity/{entityUri}": "/embeddings/by-entity/{entityUri}",
    "/namespaces/{namespaceId}/embeddings/by-ontology/{ontologyId}": "/embeddings/by-ontology/{ontologyId}",
    "/namespaces/{namespaceId}/validate": "/validate/",
    "/namespaces/{namespaceId}/validate/jobs": "/validate/jobs",
    "/namespaces/{namespaceId}/validate/jobs/{jobId}": "/validate/jobs/{jobId}",
    "/namespaces/{namespaceId}/ontology/foundational": "/ontology/foundational",
    "/namespaces/{namespaceId}/ontology/foundational/{key}/load": "/ontology/foundational/{key}/load",
}

# Query params a caller spells in camelCase, per backend path, and the snake_case
# name the ontology engine's FastAPI route actually binds.
#
# Without the rename the param survives the whole trip and is then dropped on the
# floor: this handler forwards any query string, and FastAPI ignores names it does
# not declare and falls back to its default. That is why MCP's
# ``describe_schema(max_results=500, include_properties=False)`` has been getting
# 100 classes with properties — the values were sent, never bound.
#
# Scoped per backend path rather than applied globally so a route that genuinely
# takes a camelCase param keeps getting it verbatim.
_QUERY_ALIASES: dict[str, dict[str, str]] = {
    "/graph/schema": {
        "maxResults": "max_results",
        "includeProperties": "include_properties",
        # ``classFilter`` matches the Smithy DescribeSchema input. FastAPI
        # binds ``class_filter`` on the ontology-engine route; without this
        # rename the param would survive the trip and be silently dropped.
        "classFilter": "class_filter",
    },
}


def _handle_system_health() -> dict:
    """Aggregate backend health: direct checks + ontology-engine /readiness."""
    import boto3

    region = resolve_region()
    results: dict = {}

    # DynamoDB
    sources_table = os.environ.get("SOURCES_TABLE", "")
    if sources_table:
        try:
            ddb = boto3.client("dynamodb", region_name=region)
            resp = ddb.describe_table(TableName=sources_table)
            status = resp["Table"]["TableStatus"]
            results["dynamodb"] = "ok" if status == "ACTIVE" else f"table_status: {status}"
        except Exception:
            _log.error("health_check_dynamodb_failed", exc_info=True)
            results["dynamodb"] = "error"
    else:
        results["dynamodb"] = "not_configured"

    # Neptune KG (GraphRAG)
    neptune_endpoint = os.environ.get("NEPTUNE_ENDPOINT", "")
    if neptune_endpoint:
        try:
            client = boto3.client(
                "neptunedata",
                region_name=region,
                endpoint_url=f"https://{neptune_endpoint}:8182",
            )
            client.execute_open_cypher_query(openCypherQuery="RETURN 1 AS health")
            results["neptuneKg"] = "ok"
        except Exception:
            _log.error("health_check_neptune_failed", exc_info=True)
            results["neptuneKg"] = "error"
    else:
        results["neptuneKg"] = "not_configured"

    # OpenSearch Serverless (AOSS)
    opensearch_endpoint = os.environ.get("OPENSEARCH_ENDPOINT", "")
    if opensearch_endpoint:
        try:
            client = boto3.client("opensearchserverless", region_name=region)
            host = opensearch_endpoint.replace("https://", "").split(".")[0]
            resp = client.batch_get_collection(ids=[host])
            details = resp.get("collectionDetails", [])
            if details and details[0].get("status") == "ACTIVE":
                results["opensearch"] = "ok"
            else:
                results["opensearch"] = f"collection_status: {details[0].get('status') if details else 'not_found'}"
        except Exception:
            _log.error("health_check_opensearch_failed", exc_info=True)
            results["opensearch"] = "error"
    else:
        results["opensearch"] = "not_configured"

    # Ontology Induction (via ontology-engine /readiness)
    try:
        url = f"{ENDPOINT}/readiness"
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results["ontologyInduction"] = {
            "graphStore": data.get("graph_store", "unknown"),
            "vectorStore": data.get("vector_store", "unknown"),
            "bedrockEmbeddings": data.get("bedrock_embeddings", "unknown"),
            "descriptionLlm": data.get("description_llm", "unknown"),
        }
    except Exception:
        _log.error("health_check_ontology_induction_failed", exc_info=True)
        results["ontologyInduction"] = {
            "graphStore": "error",
            "vectorStore": "error",
            "bedrockEmbeddings": "error",
            "descriptionLlm": "error",
        }

    all_statuses = [
        results.get("dynamodb", ""),
        results.get("neptuneKg", ""),
        results.get("opensearch", ""),
        results.get("ontologyInduction", {}).get("graphStore", ""),
        results.get("ontologyInduction", {}).get("vectorStore", ""),
        results.get("ontologyInduction", {}).get("bedrockEmbeddings", ""),
        results.get("ontologyInduction", {}).get("descriptionLlm", ""),
    ]
    overall = "ok" if all(s == "ok" for s in all_statuses) else "degraded"
    results["status"] = overall

    return {
        "statusCode": 200,
        "headers": _CORS_HEADERS,
        "body": json.dumps(results),
    }


def handler(event: dict, context) -> dict:
    """Forward an API Gateway proxy event to the ontology-engine ECS service.

    Maps the resource path to a backend path, substitutes path parameters,
    threads the namespace through as a query param, and forwards the request
    (preserving multipart content types and base64 bodies). ``/system-health``
    is answered directly rather than proxied.

    Args:
        event: API Gateway Lambda proxy event (method, resource, path/query
            params, headers, body).
        context: Lambda context object (unused).

    Returns:
        A Lambda proxy response dict with ``statusCode``, ``headers``, and
        ``body``; a 502 when the ECS service is unreachable, a 504 when it does
        not answer within ``_BACKEND_TIMEOUT_SECONDS``
        (``_IDEMPOTENT_BACKEND_TIMEOUT_SECONDS`` for ``_IDEMPOTENT_POSTS``).
    """
    resource = event.get("resource", "")

    # System health is handled directly (not proxied to ECS)
    if resource == "/system-health":
        return _handle_system_health()

    # namespaceId is a UUID v4 in the contract. Reject anything else here,
    # before it is forwarded (or an idempotency record is claimed for it): the
    # ontology engine's graph routes surfaced a malformed id as a 500.
    if "{namespaceId}" in resource:
        try:
            validate_namespace_id((event.get("pathParameters") or {}).get("namespaceId") or "")
        except ValueError as exc:
            return _error(400, str(exc))

    operation = _IDEMPOTENT_POSTS.get(resource)
    if operation and event.get("httpMethod") == "POST":
        return idempotent_create(
            event,
            lambda e: _forward(e, timeout=_IDEMPOTENT_BACKEND_TIMEOUT_SECONDS),
            operation=operation,
            headers=lambda: dict(_CORS_HEADERS),
        )
    return _forward(event, timeout=_BACKEND_TIMEOUT_SECONDS)


def _forward(event: dict, *, timeout: float) -> dict:
    """Proxy one API Gateway event to the ontology-engine and return its response.

    ``timeout`` is the backend read timeout in seconds; a timeout is a 504.
    """
    method = event["httpMethod"]
    resource = event.get("resource", "")
    path_params = event.get("pathParameters") or {}
    query: dict[str, str] = dict(event.get("queryStringParameters") or {})
    body = event.get("body", "")

    # Resolve backend path. API Gateway delivers path params still percent-
    # encoded, so normalize each to exactly ONE layer (unquote then re-quote):
    # re-encoding without the unquote double-encodes an IRI id, and the backend
    # then writes and reads the ontology under different named-graph URIs,
    # silently returning an empty graph. Keep '/' and ':' literal so the IRI
    # round-trips; encoding '?' '#' '&' stops a value from opening the query
    # early and overriding the authorizer-validated '?namespace=' set below.
    backend_path = _PATH_MAP.get(resource, resource)
    for key, val in path_params.items():
        backend_path = backend_path.replace(f"{{{key}}}", quote(unquote(val), safe="/:"))

    # Add namespace as query param (ontology-engine uses ?namespace=)
    namespace_id = path_params.get("namespaceId")
    if namespace_id:
        query["namespace"] = namespace_id

    # Build query string. ``urlencode`` escapes special chars (``#``, ``/``,
    # ``:``) so OWL IRI values like ``http://example.org/o#Cls`` don't break
    # the URL parser (the ``#`` would otherwise truncate the URL at the
    # fragment marker, dropping subsequent params like ``&namespace=``).
    aliases = _QUERY_ALIASES.get(backend_path, {})
    filtered = {aliases.get(k, k): v for k, v in query.items() if v is not None and v != ""}
    qs = urlencode(filtered) if filtered else ""
    url = f"{ENDPOINT}{backend_path}" + (f"?{qs}" if qs else "")

    # Forward request — preserve original content type for multipart uploads
    event_headers = event.get("headers") or {}
    content_type = event_headers.get("content-type") or event_headers.get("Content-Type") or "application/json"
    headers = {"Content-Type": content_type}
    data: bytes | None = None
    if event.get("isBase64Encoded") and body:
        data = base64.b64decode(body)
    elif body:
        data = body.encode("utf-8")
    req = urllib_request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            return {
                "statusCode": resp.status,
                "headers": _CORS_HEADERS,
                "body": resp_body,
            }
    except HTTPError as e:
        # The ontology engine's own error body (its FastAPI ``detail``) is this
        # service's response and passes through. Without one, answer a fixed
        # message — never one built from the exception text.
        resp_body = e.read().decode("utf-8") if e.fp else ""
        if not resp_body:
            _log.warning("ontology_engine_http_error status=%s resource=%s", e.code, resource)
            resp_body = json.dumps({"message": _MSG_BACKEND_ERROR})
        return {
            "statusCode": e.code,
            "headers": _CORS_HEADERS,
            "body": resp_body,
        }
    except URLError as e:
        if isinstance(e.reason, TimeoutError):
            _log.warning("ontology_engine_timeout resource=%s", resource)
            return _error(504, _MSG_BACKEND_TIMEOUT)
        _log.error("ontology_engine_unreachable resource=%s reason=%s", resource, e.reason)
        return _error(502, _MSG_BACKEND_UNREACHABLE)
    except TimeoutError:
        # A read timeout after the connection succeeded is raised bare.
        _log.warning("ontology_engine_timeout resource=%s", resource)
        return _error(504, _MSG_BACKEND_TIMEOUT)


def _error(status: int, message: str) -> dict:
    return {"statusCode": status, "headers": _CORS_HEADERS, "body": json.dumps({"message": message})}
