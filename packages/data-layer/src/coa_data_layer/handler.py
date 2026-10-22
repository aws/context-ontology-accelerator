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
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.constants import (
    DESCRIBE_SCHEMA_RESOURCE,
    ONTOLOGY_PROXY_LAMBDA_ARN_ENV,
    QUERY_STRATEGIES,
    validate_namespace_id,
    validate_query_text,
)
from coa_common.smithy_shapes import normalize_dimensions

from coa_data_layer import contract

logger = logging.getLogger(__name__)

RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Ontology-api-proxy Lambda that fronts the ontology-engine's FastAPI. The MCP
# ``describe_schema`` tool reads the same route, so a UI/data-layer caller and
# an MCP agent see the same schema — one source of truth for the queryable
# ontology.
ONTOLOGY_PROXY_LAMBDA_ARN = os.environ.get(ONTOLOGY_PROXY_LAMBDA_ARN_ENV, "")

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
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    # A JSON array or scalar is not a request structure; treating it as absent
    # yields the route's own 400 instead of an AttributeError surfacing as a 502.
    return parsed if isinstance(parsed, dict) else None


def _parse_query(body: dict) -> str:
    """Return a bounded, non-blank query, or raise ``ValueError`` explaining why not.

    The ``ValueError`` message reaches the client rather than being collapsed into
    one string, because there are four distinct client mistakes here and they call
    for different corrections: the field is absent (detected below), or
    ``validate_query_text`` rejects it as a non-string, blank after stripping, or
    longer than :data:`~coa_common.constants.MAX_QUERY_CODEPOINTS`. Reporting all
    of them as "Missing required field: query" tells a caller whose field IS
    present to look in the wrong place.

    The length bound is the one a non-Latin caller is most likely to hit, since a
    CJK query of the same character count says considerably more than a Latin one.
    All four messages describe the caller's own input, so propagating them
    discloses nothing about the service.
    """
    raw = body.get("query")
    if raw is None:
        raise ValueError("Missing required field: query")
    return validate_query_text(raw)


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


# Optional ``Query`` members that are forwarded verbatim once validated, by kind.
# A member listed here is read from the body by ``_parse_query_options``; the
# API conformance test resolves these tables when it checks which members are read.
_QUERY_INT_MEMBERS: dict[str, tuple[int, int]] = {
    "tierOverride": (contract.QUERY_TIER_OVERRIDE_MIN, contract.QUERY_TIER_OVERRIDE_MAX),
    "timeoutMs": (contract.QUERY_TIMEOUT_MS_MIN, contract.QUERY_TIMEOUT_MS_MAX),
    "maxResults": (contract.QUERY_MAX_RESULTS_MIN, contract.QUERY_MAX_RESULTS_MAX),
}
_QUERY_ENUM_MEMBERS: dict[str, frozenset[str]] = {
    "mode": contract.QUERY_MODES,
    "strategy": QUERY_STRATEGIES,
}
_QUERY_BOOL_MEMBERS: tuple[str, ...] = ("includeSupporting", "includeDebugInfo")


def _parse_query_options(body: dict) -> dict[str, Any]:
    """Validate the optional ``Query`` members and return the CM ``options`` map.

    Each member is checked against its ``serve.smithy`` type and bound (see
    :mod:`coa_data_layer.contract`) and an out-of-contract value raises
    :class:`~coa_data_layer.contract.ContractViolation` — the request is rejected,
    never clamped, coerced or silently dropped. Nothing upstream validates the body
    against the model, so this is where the contract is enforced. ``null`` is
    treated as absent.

    ``strategy`` deserves a note: an unrecognised value forwarded blind would fall
    out of ``Orchestrator._EXPLICIT_STRATEGY_OPTIONS`` in serve and resolve to the
    default chain, leaving the caller believing it had pinned an engine.

    ``timeoutMs`` narrows the Context Manager's request-scoped deadline; the
    upper bound is this handler's own REST ceiling, so a caller cannot ask for a
    budget the transport could never honour.

    ``includeDebugInfo`` is forwarded as sent; the Context Manager honours it only
    for a caller entitled to see the generated SQL/SPARQL (it resolves the
    caller's namespace roles, which this handler does not have).

    Raises:
        ContractViolation: A member has the wrong type or is out of range.
    """
    options: dict[str, Any] = {}

    execute = body.get("execute")
    if execute is not None and contract.require_bool(execute, "execute") is False:
        options["execute"] = False
    for name, (lo, hi) in _QUERY_INT_MEMBERS.items():
        if (value := body.get(name)) is not None:
            options[name] = contract.require_int(value, name, lo, hi)
    for name, allowed in _QUERY_ENUM_MEMBERS.items():
        if (value := body.get(name)) is not None:
            options[name] = contract.require_enum(value, name, allowed)
    for name in _QUERY_BOOL_MEMBERS:
        if (value := body.get(name)) is not None:
            options[name] = contract.require_bool(value, name)
    dimensions = body.get("dimensions")
    if dimensions is not None:
        # Smithy DimensionFilterList arrives as ``[{name, value}, ...]``; CM's
        # Tier-1 ``substitute_dimensions`` calls ``.items()`` and expects a
        # mapping. Normalize here so a Tier-1 metric query with dimensions does
        # not 500 with ``AttributeError``. Bounds are checked first: the mapping
        # can no longer express a duplicate name or an operator.
        normalized = normalize_dimensions(contract.require_dimensions(dimensions))
        if normalized:
            options["dimensions"] = normalized
    return options


def _parse_kb_search_options(body: dict) -> dict[str, Any]:
    """Validate the optional ``KBSearch`` members and return the CM ``options`` map.

    ``topK`` defaults to :data:`~coa_data_layer.contract.KB_SEARCH_TOP_K_DEFAULT`
    when absent. ``sourceFilter`` and ``entityFilter`` are shape-checked and
    forwarded. Neither is supported yet: the Context Manager rejects a non-empty
    filter with 400 rather than return unfiltered chunks as if it had applied it.
    An empty list is accepted and restricts nothing.

    Raises:
        ContractViolation: A member has the wrong type or is out of range.
    """
    top_k = body.get("topK")
    options: dict[str, Any] = {
        "topK": contract.KB_SEARCH_TOP_K_DEFAULT
        if top_k is None
        else contract.require_int(top_k, "topK", contract.KB_SEARCH_TOP_K_MIN, contract.KB_SEARCH_TOP_K_MAX)
    }
    min_score = body.get("minScore")
    if min_score is not None:
        options["minScore"] = contract.require_number(
            min_score, "minScore", contract.KB_SEARCH_MIN_SCORE_MIN, contract.KB_SEARCH_MIN_SCORE_MAX
        )
    source_filter = body.get("sourceFilter")
    if source_filter is not None:
        options["sourceFilter"] = contract.require_filter_values(source_filter, "sourceFilter")
    entity_filter = body.get("entityFilter")
    if entity_filter is not None:
        options["entityFilter"] = contract.require_filter_values(entity_filter, "entityFilter")
    return options


def _parse_graph_traverse_options(body: dict) -> dict[str, Any]:
    """Validate the ``GraphTraverse`` members and return the CM ``options`` map.

    ``startUri`` is required; the other members take their documented defaults
    when absent. ``direction`` and ``relationshipFilter`` are validated and
    forwarded although the Context Manager does not apply them yet (the model
    documents this), so a malformed value is still a 400 rather than ignored.

    Raises:
        ValueError: ``startUri`` is absent.
        ContractViolation: A member has the wrong type or is out of range.
    """
    start_uri = body.get("startUri")
    if start_uri is None or start_uri == "":
        raise ValueError("Missing required field: startUri")
    max_depth = body.get("maxDepth")
    max_results = body.get("maxResults")
    direction = body.get("direction")
    options: dict[str, Any] = {
        "startUri": contract.require_start_uri(start_uri),
        "maxDepth": contract.GRAPH_TRAVERSE_MAX_DEPTH_DEFAULT
        if max_depth is None
        else contract.require_int(
            max_depth, "maxDepth", contract.GRAPH_TRAVERSE_MAX_DEPTH_MIN, contract.GRAPH_TRAVERSE_MAX_DEPTH_MAX
        ),
        "direction": contract.GRAPH_TRAVERSE_DIRECTION_DEFAULT
        if direction is None
        else contract.require_enum(direction, "direction", contract.GRAPH_TRAVERSE_DIRECTIONS),
        "maxResults": contract.GRAPH_TRAVERSE_MAX_RESULTS_DEFAULT
        if max_results is None
        else contract.require_int(
            max_results,
            "maxResults",
            contract.GRAPH_TRAVERSE_MAX_RESULTS_MIN,
            contract.GRAPH_TRAVERSE_MAX_RESULTS_MAX,
        ),
    }
    relationship_filter = body.get("relationshipFilter")
    if relationship_filter is not None:
        relationship_filter = contract.require_filter_values(relationship_filter, "relationshipFilter")
        if relationship_filter:
            options["relationshipFilter"] = relationship_filter
    return options


def _parse_describe_schema_query(query: dict) -> dict[str, str]:
    """Validate the ``DescribeSchema`` query string and return it in canonical form.

    Query-string values always arrive as strings, so the booleans and integers
    are parsed here. The camelCase (Smithy wire) names are kept: the
    ontology-api-proxy's per-path alias table renames them for FastAPI.

    Raises:
        ContractViolation: A parameter is malformed or out of range.
    """
    forwarded: dict[str, str] = {}
    include_properties = query.get("includeProperties")
    if include_properties is not None:
        forwarded["includeProperties"] = str(
            contract.require_query_bool(include_properties, "includeProperties")
        ).lower()
    max_results = query.get("maxResults")
    if max_results is not None:
        forwarded["maxResults"] = str(
            contract.require_query_int(
                max_results,
                "maxResults",
                contract.DESCRIBE_SCHEMA_MAX_RESULTS_MIN,
                contract.DESCRIBE_SCHEMA_MAX_RESULTS_MAX,
            )
        )
    return forwarded


# ── Route handlers ───────────────────────────────────────────────────────────


def _handle_query(namespace: str, event: dict) -> dict:
    body = _parse_body(event) or {}
    try:
        query = _parse_query(body)
    except ValueError as exc:
        return _error_response(400, str(exc))

    payload: dict[str, Any] = {
        "query": query,
        "namespace": namespace,
        "profile": _get_caller_profile(event),
        "options": {},
    }

    try:
        payload["options"].update(_parse_query_options(body))
    except contract.ContractViolation as exc:
        return _error_response(400, str(exc))

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
    body = _parse_body(event) or {}
    try:
        query = _parse_query(body)
    except ValueError as exc:
        return _error_response(400, str(exc))

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
    body = _parse_body(event) or {}
    try:
        query = _parse_query(body)
    except ValueError as exc:
        return _error_response(400, str(exc))

    payload = {
        "action": "kbSearch",
        "query": query,
        "namespace": namespace,
        "profile": _get_caller_profile(event),
    }
    try:
        payload["options"] = _parse_kb_search_options(body)
    except contract.ContractViolation as exc:
        return _error_response(400, str(exc))

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
    body = _parse_body(event) or {}
    try:
        options = _parse_graph_traverse_options(body)
    except ValueError as exc:  # includes ContractViolation
        return _error_response(400, str(exc))

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
    try:
        forwarded_query = _parse_describe_schema_query(query)
    except contract.ContractViolation as exc:
        return _error_response(400, str(exc))

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
        logger.warning(
            "describe_schema_function_error function_error=%s request_id=%s err_preview=%s",
            response.get("FunctionError"),
            response.get("ResponseMetadata", {}).get("RequestId"),
            err_preview,
        )
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


# ── Main handler ─────────────────────────────────────────────────────────────

# Error types the Context Manager itself emits (``coa_serve.exceptions`` plus the
# entrypoint's own validation/internal errors). An HTTP error body carrying one of
# these is our service's structured, client-safe error; anything else came from
# the transport in between (the runtime's own JSON errors can name ARNs and roles).
_CONTEXT_MANAGER_ERROR_TYPES = frozenset(
    {
        "AccessDeniedError",
        "AmbiguousReferenceError",
        "DataSourceUnavailableError",
        "InternalError",
        "NamespaceNotFoundError",
        "NoResultError",
        "QueryTranslationError",
        "TimeoutError",
        "ValidationError",
    }
)
_UPSTREAM_MESSAGE_MAX_CHARS = 500


def _client_safe_upstream_error(resp_body: str, status_code: int) -> tuple[str, str | None]:
    """Return ``(message, error_type)`` that may be shown to the caller for an upstream HTTP error.

    Only a Context Manager structured error (a JSON object whose ``error`` is one
    of :data:`_CONTEXT_MANAGER_ERROR_TYPES` and whose ``message`` is a string) is
    forwarded, and then only those two fields. Any other body — the runtime's own
    error JSON, an HTML gateway page, plain text — is replaced by a fixed message.
    """
    try:
        detail = json.loads(resp_body)
    except (json.JSONDecodeError, TypeError):
        detail = None
    if isinstance(detail, dict):
        error = detail.get("error")
        message = detail.get("message")
        if error in _CONTEXT_MANAGER_ERROR_TYPES and isinstance(message, str) and message:
            return message[:_UPSTREAM_MESSAGE_MAX_CHARS], error
    return f"Context Manager request failed (HTTP {status_code})", None


_ROUTE_MAP = {
    ("POST", "/namespaces/{namespaceId}/query"): _handle_query,
    ("POST", "/namespaces/{namespaceId}/translate"): _handle_translate,
    ("POST", "/namespaces/{namespaceId}/kb/search"): _handle_kb_search,
    ("POST", "/namespaces/{namespaceId}/graph/traverse"): _handle_graph_traverse,
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
    # namespaceId is a UUID v4 in the contract. Reject anything else here rather
    # than forwarding it, where a malformed id surfaced as a backend 500.
    try:
        validate_namespace_id(namespace)
    except ValueError as exc:
        return _error_response(400, str(exc))

    route_handler = _ROUTE_MAP.get((method, resource))
    if not route_handler:
        return _error_response(501, f"Not implemented: {method} {resource}")

    try:
        return route_handler(namespace, event)
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.warning("context_manager_http_error status=%s body_preview=%s", e.code, resp_body[:500])
        msg, error = _client_safe_upstream_error(resp_body, e.code)
        return _error_response(e.code, msg, **({"error": error} if error else {}))
    except URLError as e:
        # A urlopen timeout (the 29s REST budget in _invoke_context_manager) surfaces
        # as URLError whose .reason is a socket timeout. Distinguish it from a genuine
        # connectivity failure: the request did reach the Context Manager and is likely
        # still running server-side (e.g. Tier-3 synthesis routinely needs 20-90s),
        # it just cannot fit the hard 29s API Gateway ceiling. Return a diagnosable 504
        # naming the streaming path rather than a misleading 502 "cannot reach", so a
        # builder does not chase a phantom connectivity/timeout bug.
        if isinstance(e.reason, TimeoutError):
            return _rest_deadline_exceeded()
        # ``reason`` names the endpoint host and the socket error; log it, don't return it.
        logger.warning("context_manager_unreachable reason=%s", e.reason)
        return _error_response(502, "Cannot reach Context Manager")
    except TimeoutError:
        # A read timeout (the response did not arrive within the budget) is raised by
        # urlopen as a bare TimeoutError, not wrapped in URLError like a connect timeout.
        return _rest_deadline_exceeded()
    except Exception as e:
        logger.exception("context_manager_invocation_failed")
        return _error_response(502, f"Context Manager invocation failed: {type(e).__name__}")


def _rest_deadline_exceeded() -> dict:
    return _error_response(
        504,
        (
            "Query exceeded the 29s REST timeout. The request may still be "
            "completing server-side; long-running queries (e.g. Tier-3 "
            "synthesis) should use the streaming query endpoint, which has a "
            "larger budget. See the serve docs: 'REST query timeout'."
        ),
        error="rest_deadline_exceeded",
        limitMs=REST_TIMEOUT_MS,
    )
