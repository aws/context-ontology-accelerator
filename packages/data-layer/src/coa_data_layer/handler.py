# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Data Layer API Lambda handler — thin adapter between API Gateway and Context Manager.

Validates requests, invokes the Context Manager via AgentCore Runtime (HTTP POST
to the VPC endpoint with JWT bearer auth), and maps responses to Smithy output shapes.

Environment:
    AGENTCORE_RUNTIME_ARN: AgentCore Runtime ARN for the Context Manager.
    AWS_REGION: AWS region (used to construct the VPC endpoint hostname).
"""

from __future__ import annotations

import json
import logging
import os
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.constants import (
    DESCRIBE_SCHEMA_RESOURCE,
    LIST_METRICS_RESOURCE,
    METRIC_SERVICE_LAMBDA_ARN_ENV,
    ONTOLOGY_PROXY_LAMBDA_ARN_ENV,
)
from coa_common.smithy_shapes import normalize_dimensions

logger = logging.getLogger(__name__)

RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Ontology-api-proxy Lambda that fronts the ontology-engine's FastAPI. The MCP
# ``describe_schema`` tool reads the same route, so a UI/data-layer caller and
# an MCP agent see the same schema — one source of truth for the queryable
# ontology.
ONTOLOGY_PROXY_LAMBDA_ARN = os.environ.get(ONTOLOGY_PROXY_LAMBDA_ARN_ENV, "")

# Metric-service Lambda serving the governed-metric catalog. Data-layer's
# ServeListMetrics proxies straight to it (the CM never sees the request); the
# MCP ``discovery.list_metrics`` tool reads the same route, so both surfaces
# return the same catalog for the same input — one source of truth for governed
# metrics. Same rationale as DescribeSchema: pure read, no tier orchestration.
METRIC_SERVICE_LAMBDA_ARN = os.environ.get(METRIC_SERVICE_LAMBDA_ARN_ENV, "")

AGENTCORE_ENDPOINT = f"https://bedrock-agentcore.{REGION}.amazonaws.com"
ENCODED_ARN = quote(RUNTIME_ARN, safe="")
INVOKE_URL = f"{AGENTCORE_ENDPOINT}/runtimes/{ENCODED_ARN}/invocations?qualifier=DEFAULT"

# Lambda client for the ontology-api-proxy invocation. Created lazily so that
# the handler still loads in test environments that don't stub boto3.
_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda", region_name=REGION)
    return _lambda_client


_ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": _ALLOWED_ORIGIN,
}

REST_TIMEOUT_MS = 29_000


def _parse_first_sse_event(raw: str) -> dict:
    r"""Extract the first JSON payload from an SSE response.

    AgentCore's @app.entrypoint async generators produce text/event-stream
    responses where each yielded dict becomes: `data: <json>\\n\\n`
    Ref: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html

    Falls back to plain JSON parsing for local/test environments without SSE framing.
    """
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    # No SSE envelope (local testing or non-generator entrypoint)
    return json.loads(raw)


def _invoke_context_manager(payload: dict, token: str) -> dict:
    """Invoke the Context Manager via AgentCore Runtime HTTP endpoint."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        INVOKE_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    # Sync customer path behind API Gateway (30s cap): explicit 29s timeout and
    # no retry by design — fail fast and let the client decide when to retry.
    with urllib_request.urlopen(req, timeout=29) as resp:
        raw = resp.read().decode("utf-8")
    return _parse_first_sse_event(raw)


def _error_response(status_code: int, message: str, **extra) -> dict:
    body = {"message": message, **extra}
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body),
    }


def _success_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body),
    }


def _parse_body(event: dict) -> dict | None:
    raw = event.get("body")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated string, stripping whitespace and filtering empty segments."""
    return [s.strip() for s in value.split(",") if s.strip()] if value else []


def _get_caller_profile(event: dict) -> dict:
    """Extract caller profile from authorizer context (set by JWT+Cedar authorizer).

    The authorizer writes groups/roles as comma-separated strings, not JSON.
    """
    context = event.get("requestContext", {}).get("authorizer", {})
    return {
        "userId": context.get("principalId", ""),
        "email": context.get("email", ""),
        "groups": _split_csv(context.get("groups", "")),
        "globalRoles": _split_csv(context.get("globalRoles", "")),
    }


def _get_bearer_token(event: dict) -> str:
    """Extract the raw Bearer token from the Authorization header."""
    headers = event.get("headers") or {}
    auth_header = headers.get("Authorization") or headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return auth_header


# ── Route handlers ───────────────────────────────────────────────────────────


def _handle_query(namespace: str, event: dict) -> dict:
    body = _parse_body(event)
    if not body or not body.get("query"):
        return _error_response(400, "Missing required field: query")
    query = body["query"].strip()
    if not query:
        return _error_response(400, "Missing required field: query")

    payload = {
        "query": query,
        "namespace": namespace,
        "profile": _get_caller_profile(event),
        "options": {},
    }

    if body.get("execute") is False:
        payload["options"]["execute"] = False
    if body.get("tierOverride") is not None:
        payload["options"]["tierOverride"] = body["tierOverride"]
    if body.get("mode") is not None:
        payload["options"]["mode"] = body["mode"]
    if body.get("dimensions"):
        # Smithy DimensionFilterList arrives as ``[{name, value}, ...]``; CM's
        # Tier-1 ``substitute_dimensions`` calls ``.items()`` and expects a
        # mapping. Normalize here so a Tier-1 metric query with dimensions does
        # not 500 with ``AttributeError``.
        normalized = normalize_dimensions(body["dimensions"])
        if normalized:
            payload["options"]["dimensions"] = normalized
    # ``timeoutMs`` is declared on the Smithy contract but the Context Manager
    # does not currently read ``options.timeoutMs`` — forwarding it would be a
    # silent no-op that hides "my deadline was ignored" behind a plausible
    # long-running response. The urllib timeout below (``REST_TIMEOUT_MS``)
    # is the actual, honoured deadline for this handler's own upstream call.
    # Track: MR !939 review comment ``b6dea1de``.
    if body.get("includeSupporting") is not None:
        payload["options"]["includeSupporting"] = body["includeSupporting"]
    if body.get("maxResults") is not None:
        payload["options"]["maxResults"] = body["maxResults"]

    result = _invoke_context_manager(payload, _get_bearer_token(event))

    status_code = result.get("statusCode", 200)
    if status_code >= 400:
        return _error_response(status_code, result.get("message", "Internal error"))

    return _success_response(
        200,
        {
            "result": result.get("result", result),
            "requestId": result.get("requestId"),
            "sessionId": result.get("sessionId"),
        },
    )


def _handle_translate(namespace: str, event: dict) -> dict:
    body = _parse_body(event)
    if not body or not body.get("query"):
        return _error_response(400, "Missing required field: query")
    query = body["query"].strip()
    if not query:
        return _error_response(400, "Missing required field: query")

    payload = {
        "action": "translate",
        "query": query,
        "namespace": namespace,
        "profile": _get_caller_profile(event),
    }

    result = _invoke_context_manager(payload, _get_bearer_token(event))

    status_code = result.get("statusCode", 200)
    if status_code >= 400:
        return _error_response(status_code, result.get("message", "Internal error"))

    return _success_response(
        200,
        {
            "sparqlQuery": result.get("sparqlQuery", ""),
            "confidence": result.get("confidence", {"score": 0.0}),
            "trace": result.get("trace", []),
            "ontologyVersion": result.get("ontologyVersion"),
        },
    )


def _handle_kb_search(namespace: str, event: dict) -> dict:
    body = _parse_body(event)
    if not body or not body.get("query"):
        return _error_response(400, "Missing required field: query")
    query = body["query"].strip()
    if not query:
        return _error_response(400, "Missing required field: query")

    payload = {
        "action": "kbSearch",
        "query": query,
        "namespace": namespace,
        "profile": _get_caller_profile(event),
        "options": {
            "topK": body.get("topK", 10),
        },
    }

    result = _invoke_context_manager(payload, _get_bearer_token(event))

    status_code = result.get("statusCode", 200)
    if status_code >= 400:
        return _error_response(status_code, result.get("message", "Internal error"))

    return _success_response(
        200,
        {
            "chunks": result.get("chunks", []),
            "trace": result.get("trace", []),
            "queryEmbeddingModel": result.get("queryEmbeddingModel"),
        },
    )


def _handle_graph_traverse(namespace: str, event: dict) -> dict:
    body = _parse_body(event)
    if not body or not body.get("startUri"):
        return _error_response(400, "Missing required field: startUri")

    options: dict = {
        "startUri": body["startUri"],
        "maxDepth": body.get("maxDepth", 2),
        "direction": body.get("direction", "both"),
        "maxResults": body.get("maxResults", 100),
    }
    if body.get("relationshipFilter"):
        options["relationshipFilter"] = body["relationshipFilter"]

    payload = {
        "action": "graphTraverse",
        "namespace": namespace,
        "profile": _get_caller_profile(event),
        "options": options,
    }

    result = _invoke_context_manager(payload, _get_bearer_token(event))

    status_code = result.get("statusCode", 200)
    if status_code >= 400:
        return _error_response(status_code, result.get("message", "Internal error"))

    return _success_response(
        200,
        {
            "entities": result.get("entities", []),
            "relationships": result.get("relationships", []),
            "trace": result.get("trace", []),
        },
    )


def _handle_describe_schema(namespace: str, event: dict) -> dict:
    """Describe the namespace's queryable schema.

    Proxies to the ontology-api-proxy Lambda's ``/graph/schema`` route — the
    same source MCP's ``describe_schema`` tool reads. This handler is the
    only place in the data layer that does not go through the Context
    Manager, because the schema query is pure Neptune SPARQL and doesn't
    involve any tiered orchestration.
    """
    if not ONTOLOGY_PROXY_LAMBDA_ARN:
        return _error_response(501, "DescribeSchema unavailable: ONTOLOGY_PROXY_LAMBDA_ARN not configured")

    query = event.get("queryStringParameters") or {}
    # camelCase (Smithy wire form) → forward as-is; the ontology-api-proxy's
    # per-path alias table renames them to snake_case for FastAPI binding.
    forwarded_query: dict[str, str] = {}
    if query.get("includeProperties") is not None:
        forwarded_query["includeProperties"] = str(query["includeProperties"]).lower()
    if query.get("maxResults") is not None:
        forwarded_query["maxResults"] = str(query["maxResults"])

    # Send the canonical Smithy path (``/namespaces/{namespaceId}/schema``); the
    # ontology-api-proxy's alias table renames it to the FastAPI ``/graph/schema``
    # route. Both this handler and MCP's ``discovery.describe_schema`` now use
    # the same path, so the legacy ``/graph/schema`` alias in the api-proxy can
    # be retired once no caller depends on it.
    proxy_event = {
        "httpMethod": "GET",
        "resource": DESCRIBE_SCHEMA_RESOURCE,
        "path": f"/namespaces/{namespace}/schema",
        "pathParameters": {"namespaceId": namespace},
        "queryStringParameters": forwarded_query or None,
        "headers": {"Authorization": f"Bearer {_get_bearer_token(event)}"},
        "body": None,
        "isBase64Encoded": False,
        "requestContext": {"stage": "data-layer"},
    }

    # boto3's Lambda invoke raises on throttling, permissions failures,
    # invalid function names, and connect/read timeouts. Left unhandled these
    # bubble up to the top-level ``except Exception`` in ``handler`` and come
    # back as "Context Manager invocation failed" — misleading, since CM was
    # never in the path. Map them to a 502 with a stable message; the full
    # error is logged so operators can still diagnose.
    try:
        response = _get_lambda_client().invoke(
            FunctionName=ONTOLOGY_PROXY_LAMBDA_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps(proxy_event).encode("utf-8"),
        )
    except (ClientError, BotoCoreError) as exc:
        logger.exception("describe_schema_invoke_failed", exc_info=exc)
        return _error_response(502, "Failed to invoke schema backend Lambda")

    if "FunctionError" in response:
        # The Lambda payload from a FunctionError contains a Python traceback
        # or a modelled error — either can carry ARNs, request IDs, or line
        # numbers that shouldn't reach the customer. Log the truncated
        # payload for operators and return a generic message.
        err_preview = response["Payload"].read().decode("utf-8", errors="replace")[:500]
        logger.warning("describe_schema_function_error err_preview=%s", err_preview)
        return _error_response(502, "Schema backend failed")

    # An invoke that succeeded at the Lambda control-plane level can still
    # return a body that isn't valid JSON (e.g. the proxy Lambda crashed
    # inside its handler wrapper). Catch that here so we produce a 502 with
    # a stable message rather than the router's generic "invocation failed".
    try:
        proxy_response = json.loads(response["Payload"].read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("describe_schema_invalid_envelope", exc_info=exc)
        return _error_response(502, "Schema backend returned invalid JSON")

    status_code = proxy_response.get("statusCode", 200)
    body_str = proxy_response.get("body", "{}")
    try:
        body = json.loads(body_str) if isinstance(body_str, str) else body_str
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("describe_schema_invalid_body body_preview=%s", str(body_str)[:200], exc_info=exc)
        body = {}

    if status_code >= 400:
        message = body.get("message") if isinstance(body, dict) else None
        return _error_response(status_code, message or f"Schema backend returned {status_code}")

    # Shape the response to match the Smithy DescribeSchema output. The
    # ontology-engine already returns ``classes`` and ``ontologyVersion`` in
    # the right shape; project them explicitly so a future backend change can't
    # leak internal fields into the customer-visible response.
    return _success_response(
        200,
        {
            "classes": body.get("classes", []) if isinstance(body, dict) else [],
            "ontologyVersion": body.get("ontologyVersion") if isinstance(body, dict) else None,
        },
    )


def _handle_list_metrics(namespace: str, event: dict) -> dict:
    """List the governed metrics available in the namespace.

    Proxies to the metric-service Lambda's ``/namespaces/{namespaceId}/metrics``
    route — the same source MCP's ``list_metrics`` tool reads. Like
    DescribeSchema, this handler bypasses the Context Manager because the metric
    registry is a pure Dynamo read with no tier orchestration to do, and both
    surfaces (UI/data-layer and MCP agent) must return the same catalog.
    """
    if not METRIC_SERVICE_LAMBDA_ARN:
        return _error_response(501, "ListMetrics unavailable: METRIC_SERVICE_LAMBDA_ARN not configured")

    query = event.get("queryStringParameters") or {}
    # camelCase (Smithy wire form) forwarded verbatim; the metric-service Lambda
    # accepts the same query-parameter names, so no renaming is needed here.
    # ``status`` is intentionally NOT forwarded: the Smithy contract declares it
    # but the metric-service backend does not yet filter on it, so forwarding
    # would make an unfiltered response look filtered. Track: MR !939 review
    # comment ``b6dea1de``.
    forwarded_query: dict[str, str] = {}
    if query.get("maxResults") is not None:
        forwarded_query["maxResults"] = str(query["maxResults"])
    if query.get("nextToken") is not None:
        forwarded_query["nextToken"] = str(query["nextToken"])

    proxy_event = {
        "httpMethod": "GET",
        "resource": LIST_METRICS_RESOURCE,
        "path": f"/namespaces/{namespace}/metrics",
        "pathParameters": {"namespaceId": namespace},
        "queryStringParameters": forwarded_query or None,
        "headers": {"Authorization": f"Bearer {_get_bearer_token(event)}"},
        "body": None,
        "isBase64Encoded": False,
        "requestContext": {"stage": "data-layer"},
    }

    # Same failure-mapping discipline as DescribeSchema: boto3 errors, function
    # errors, malformed envelopes, and >=400 backend responses all produce a
    # stable 502 with a schema-specific message rather than bubbling to the
    # router's generic "Context Manager invocation failed" (misleading — the CM
    # is not in this path).
    try:
        response = _get_lambda_client().invoke(
            FunctionName=METRIC_SERVICE_LAMBDA_ARN,
            InvocationType="RequestResponse",
            Payload=json.dumps(proxy_event).encode("utf-8"),
        )
    except (ClientError, BotoCoreError) as exc:
        logger.exception("list_metrics_invoke_failed", exc_info=exc)
        return _error_response(502, "Failed to invoke metric-service Lambda")

    if "FunctionError" in response:
        err_preview = response["Payload"].read().decode("utf-8", errors="replace")[:500]
        logger.warning("list_metrics_function_error err_preview=%s", err_preview)
        return _error_response(502, "Metric service backend failed")

    try:
        proxy_response = json.loads(response["Payload"].read().decode("utf-8"))
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        logger.warning("list_metrics_invalid_envelope", exc_info=exc)
        return _error_response(502, "Metric service returned invalid JSON")

    status_code = proxy_response.get("statusCode", 200)
    body_str = proxy_response.get("body", "{}")
    try:
        body = json.loads(body_str) if isinstance(body_str, str) else body_str
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("list_metrics_invalid_body body_preview=%s", str(body_str)[:200], exc_info=exc)
        body = {}

    if status_code >= 400:
        message = body.get("message") if isinstance(body, dict) else None
        return _error_response(status_code, message or f"Metric service returned {status_code}")

    # Project to the Smithy ServeListMetrics output shape so a future backend
    # change can't leak internal fields (e.g. an echoed ``namespace``) into the
    # customer-visible response.
    return _success_response(
        200,
        {
            "metrics": body.get("metrics", []) if isinstance(body, dict) else [],
            "nextToken": body.get("nextToken") if isinstance(body, dict) else None,
        },
    )


# ── Main handler ─────────────────────────────────────────────────────────────

_ROUTE_MAP = {
    ("POST", "/namespaces/{namespaceId}/query"): _handle_query,
    ("POST", "/namespaces/{namespaceId}/translate"): _handle_translate,
    ("POST", "/namespaces/{namespaceId}/kb/search"): _handle_kb_search,
    ("POST", "/namespaces/{namespaceId}/graph/traverse"): _handle_graph_traverse,
    ("GET", "/namespaces/{namespaceId}/metrics"): _handle_list_metrics,
    ("GET", "/namespaces/{namespaceId}/schema"): _handle_describe_schema,
}


def handler(event: dict, context) -> dict:
    """API Gateway Lambda proxy handler — routes to operation-specific handlers."""
    method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    path_params = event.get("pathParameters") or {}
    namespace = path_params.get("namespaceId", "")

    if not namespace:
        return _error_response(400, "Missing namespace path parameter")

    route_handler = _ROUTE_MAP.get((method, resource))
    if not route_handler:
        return _error_response(501, f"Not implemented: {method} {resource}")

    try:
        return route_handler(namespace, event)
    except HTTPError as e:
        resp_body = e.read().decode("utf-8") if e.fp else ""
        try:
            detail = json.loads(resp_body)
            msg = detail.get("message", resp_body[:200])
            error = detail.get("error")
        except (json.JSONDecodeError, TypeError):
            msg = resp_body[:200] or str(e)
            error = None
        return _error_response(e.code, msg, **({"error": error} if error else {}))
    except URLError as e:
        return _error_response(502, f"Cannot reach Context Manager: {e.reason}")
    except Exception as e:
        return _error_response(502, f"Context Manager invocation failed: {type(e).__name__}")
