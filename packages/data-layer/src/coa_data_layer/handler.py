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

import io
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, TypeGuard
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
from coa_common.sparql_config import graph_sources_enabled

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

# Kept back from the gateway's limit (which starts when the gateway calls this Lambda, after the
# authorizer, cold start included) so that rest_deadline_exceeded reaches the client before the
# gateway's own 504.
_GATEWAY_RESPONSE_MARGIN_S = 1.0
# Budget from handler entry when the event has no requestTimeEpoch (direct invocation).
_UNANCHORED_BUDGET_S = REST_TIMEOUT_MS / 1000 - _GATEWAY_RESPONSE_MARGIN_S
# Time kept back from the Lambda's own timeout for the same.
_LAMBDA_EXIT_MARGIN_S = 1.0
_READ_CHUNK_BYTES = 64 * 1024
# Deadline (time.monotonic()) for the current request's Context Manager call.
_cm_call_deadline: ContextVar[float | None] = ContextVar("_cm_call_deadline", default=None)


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _cm_call_budget_s(event: dict, context) -> float:
    """Seconds the Context Manager call may take: within the gateway's clock and the Lambda's time left."""
    budget = _UNANCHORED_BUDGET_S
    request_context = event.get("requestContext") or {}
    received_ms = request_context.get("requestTimeEpoch")
    if _is_number(received_ms):
        # The integration timeout starts once the Lambda authorizer has returned.
        authorizer_ms = (request_context.get("authorizer") or {}).get("integrationLatency")
        if _is_number(authorizer_ms) and authorizer_ms > 0:
            received_ms += authorizer_ms
        # A start ahead of this clock (skew, crafted event) must not extend the budget.
        anchored = received_ms / 1000 + REST_TIMEOUT_MS / 1000 - time.time() - _GATEWAY_RESPONSE_MARGIN_S
        budget = min(budget, anchored)
    remaining_ms = getattr(context, "get_remaining_time_in_millis", None)
    if remaining_ms is not None:
        budget = min(budget, remaining_ms() / 1000 - _LAMBDA_EXIT_MARGIN_S)
    return budget


def _close_late_http_error(outcome: queue.Queue) -> None:
    """Close an HTTP error the abandoned call queued around the time it was given up on."""
    try:
        _, error = outcome.get_nowait()
    except queue.Empty:
        return
    if isinstance(error, HTTPError):
        error.close()


def _read_body(resp, abandoned: threading.Event) -> bytes:
    """Read ``resp`` to the end, stopping early once the call is given up on.

    ``read1`` (one socket read), not ``read(n)``, so a trickling response cannot hold an abandoned call.
    """
    chunks: list[bytes] = []
    while not abandoned.is_set() and (chunk := resp.read1(_READ_CHUNK_BYTES)):
        chunks.append(chunk)
    return b"".join(chunks)


def _buffered_http_error(error: HTTPError, abandoned: threading.Event) -> HTTPError:
    """Read ``error``'s body to the end and close it, returning a copy that holds the body in memory."""
    if error.fp is None:
        return error
    try:
        body = _read_body(error.fp, abandoned)
    finally:
        error.close()
    return HTTPError(error.filename, error.code, error.msg, error.hdrs, io.BytesIO(body))


def _post(req: urllib_request.Request, timeout_s: float, abandoned: threading.Event) -> bytes:
    """POST ``req`` and return the body; an HTTP error's body is read here too, not by whoever catches it."""
    try:
        with urllib_request.urlopen(req, timeout=timeout_s) as resp:
            return _read_body(resp, abandoned)
    except HTTPError as exc:
        raise _buffered_http_error(exc, abandoned) from None


def _read_within(req: urllib_request.Request, timeout_s: float) -> bytes:
    """POST ``req`` and return the body, raising ``TimeoutError`` after ``timeout_s`` in total.

    ``urlopen``'s timeout is per socket operation, so the call runs on a thread waited on for ``timeout_s``.
    """
    outcome: queue.Queue[tuple[bytes | None, BaseException | None]] = queue.Queue(maxsize=1)
    abandoned = threading.Event()

    def call() -> None:
        try:
            body = _post(req, timeout_s, abandoned)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            outcome.put_nowait((None, exc))
            if abandoned.is_set():
                _close_late_http_error(outcome)
            return
        outcome.put_nowait((body, None))

    threading.Thread(target=call, name="context-manager-call", daemon=True).start()
    try:
        body, error = outcome.get(timeout=timeout_s)
    except queue.Empty:
        abandoned.set()
        _close_late_http_error(outcome)
        raise TimeoutError(f"Context Manager call exceeded {timeout_s:.1f}s") from None
    if error is not None:
        raise error
    return body or b""


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
    # Sync customer path behind API Gateway: bounded by the request's budget and
    # no retry by design — fail fast and let the client decide when to retry.
    deadline = _cm_call_deadline.get()
    remaining_s = _UNANCHORED_BUDGET_S if deadline is None else deadline - time.monotonic()
    if remaining_s <= 0:
        raise TimeoutError("REST budget spent before the Context Manager call")
    raw = _read_within(req, remaining_s).decode("utf-8")
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
    """Return a bounded, non-blank query, or raise ``ValueError`` with a client-safe reason."""
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
_QUERY_BOOL_MEMBERS: tuple[str, ...] = ("includeSupporting", "includeDebugInfo", "includeLlmUsage")


def _enum_values(name: str, allowed: frozenset[str]) -> frozenset[str]:
    """The values ``name`` accepts; ``nl_to_sparql`` is not a valid strategy when graph sources are disabled."""
    if name == "strategy" and not graph_sources_enabled():
        return allowed - {"nl_to_sparql"}
    return allowed


def _parse_query_options(body: dict) -> dict[str, Any]:
    """Validate the optional ``Query`` members and return the CM ``options`` map.

    An out-of-contract value raises :class:`~coa_data_layer.contract.ContractViolation`;
    it is never clamped, coerced or dropped. ``null`` is treated as absent.

    ``strategy`` is validated because an unrecognised value would silently fall back to
    the default chain. ``timeoutMs`` is capped at this handler's REST ceiling.
    ``includeDebugInfo`` is forwarded as sent; the Context Manager decides who may see it.
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
            options[name] = contract.require_enum(value, name, _enum_values(name, allowed))
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
    try:
        forwarded_query = _parse_describe_schema_query(query)
    except contract.ContractViolation as exc:
        return _error_response(400, str(exc))

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

    # Map invoke failures to a 502 naming the schema backend, not the Context Manager.
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
        # The payload can carry ARNs and tracebacks: log a preview, return a generic message.
        err_preview = response["Payload"].read().decode("utf-8", errors="replace")[:500]
        logger.warning(
            "describe_schema_function_error function_error=%s request_id=%s err_preview=%s",
            response.get("FunctionError"),
            response.get("ResponseMetadata", {}).get("RequestId"),
            err_preview,
        )
        return _error_response(502, "Schema backend failed")

    # A successful invoke can still return a non-JSON payload.
    try:
        proxy_response = json.loads(response["Payload"].read().decode("utf-8"))
    except ValueError as exc:
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

    # Project only the modelled fields so backend changes cannot leak internal ones.
    return _success_response(
        200,
        {
            "classes": body.get("classes", []) if isinstance(body, dict) else [],
            "ontologyVersion": body.get("ontologyVersion") if isinstance(body, dict) else None,
        },
    )


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
    token = _cm_call_deadline.set(time.monotonic() + _cm_call_budget_s(event, context))
    try:
        return _dispatch(route_handler, namespace, event)
    finally:
        _cm_call_deadline.reset(token)


def _dispatch(route_handler: Callable[[str, dict], dict], namespace: str, event: dict) -> dict:
    """Run ``route_handler`` and map Context Manager transport errors to client-safe responses."""
    try:
        return route_handler(namespace, event)
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        logger.warning("context_manager_http_error status=%s body_preview=%s", e.code, resp_body[:500])
        msg, error = _client_safe_upstream_error(resp_body, e.code)
        return _error_response(e.code, msg, **({"error": error} if error else {}))
    except URLError as e:
        # A urlopen timeout means the REST budget was exceeded; it is not a connectivity failure.
        if isinstance(e.reason, TimeoutError):
            logger.warning("context_manager_rest_deadline_exceeded cause=socket timeout (%s)", e.reason)
            return _rest_deadline_exceeded()
        # ``reason`` names the endpoint host and the socket error; log it, don't return it.
        logger.warning("context_manager_unreachable reason=%s", e.reason)
        return _error_response(502, "Cannot reach Context Manager")
    except TimeoutError as e:
        # Budget spent before the call, wall-clock wait expired, or a read timed out (bare TimeoutError).
        logger.warning("context_manager_rest_deadline_exceeded cause=%s", e)
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
