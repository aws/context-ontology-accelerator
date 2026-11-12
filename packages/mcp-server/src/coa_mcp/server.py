# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""MCP Server for Context Ontology Accelerator.

Exposes 6 MCP tools (2 discovery + 4 execution) via Streamable HTTP
on AgentCore Runtime (port 8000, --protocol MCP).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Literal

import structlog
from coa_authorization.schema_visibility import (
    VIEW_ONTOLOGY_ACTION,
    VIEW_SOURCE_ACTION,
    SchemaVisibility,
    to_authorizer_flags,
)
from mcp.server.fastmcp import Context, FastMCP

from .audit import log_tool_invocation
from .auth.claims import CallerIdentity, ClaimsExtractionError, extract_caller_identity
from .auth.grant_resolver import TOOL_CEDAR_ACTION, GrantResolutionError, resolve_grant
from .clients.context_manager import ContextManagerClient
from .clients.lambda_client import LambdaClient
from .config import MCPConfig
from .tools import discovery, execution
from .tools.execution import ContextManagerError

logger = structlog.get_logger(__name__)

config = MCPConfig()

mcp = FastMCP(
    name="coa",
    host=config.mcp_host,
    stateless_http=True,
)

_lambda_client: LambdaClient | None = None
_cm_client: ContextManagerClient | None = None


def _get_lambda_client() -> LambdaClient:
    """Lazy-init the Lambda client for discovery tool invocations."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = LambdaClient(region=config.aws_region)
    return _lambda_client


def _get_cm_client() -> ContextManagerClient:
    """Lazy-init the Context Manager HTTP client for execution tools.

    Resolves the CM runtime ARN from the CM_RUNTIME_ARN env var (set by CDK
    from SSM at deploy time).
    """
    global _cm_client
    if _cm_client is None:
        cm_arn = os.environ.get("CM_RUNTIME_ARN", "")
        if not cm_arn:
            raise RuntimeError(
                "CM_RUNTIME_ARN env var not set. The MCP stack must pass the Context Manager runtime ARN."
            )
        _cm_client = ContextManagerClient(runtime_arn=cm_arn, region=config.aws_region)
    return _cm_client


async def _resolve_caller_and_profile(
    namespace_id: str,
    ctx: Any = None,
    *,
    cedar_action: str | None = None,
) -> tuple[CallerIdentity, dict]:
    """Extract caller identity and resolve their data-access profile."""
    auth_header = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
            if request is not None and hasattr(request, "headers"):
                auth_header = request.headers.get("authorization")
        except (AttributeError, ValueError):
            pass

    if not config.auth_enabled:
        return CallerIdentity(
            user_id="local-dev",
            agent_id=None,
            namespaces=[],
            roles=["admin"],
            raw_claims={},
        ), {"namespace": namespace_id}

    caller = extract_caller_identity(auth_header)
    grant = await resolve_grant(caller, namespace_id, cedar_action=cedar_action)
    profile = grant.to_profile_dict()
    profile["idpGroups"] = caller.roles
    if caller.raw_claims.get("email"):
        profile["userId"] = caller.raw_claims["email"]
    return caller, profile


def _schema_right(profile: dict, action: str) -> bool:
    """A schema right (``viewSource`` / ``viewOntology``) decided by ``resolve_grant``.

    Read from the decisions cached on the resolved profile, so no policy is
    reloaded. Absent means withhold (fail closed).
    """
    return (profile.get("schemaVisibility") or {}).get(action) is True


def _caller_can_view_ontology(profile: dict) -> bool:
    """Whether the caller may see ontology schema (``viewOntology``).

    Output-suppression gate for ``translate_sparql`` / ``graph_traversal``,
    which run for a schema-blind ``data-analyst`` (authorized on ``query`` /
    ``traverseGraph``). Defense in depth: serve already withholds these fields.
    """
    return _schema_right(profile, VIEW_ONTOLOGY_ACTION)


# Response keys withheld from a caller lacking ``viewOntology``. Each names an
# all-or-nothing schema-bearing field: the whole field is dropped, with no
# per-value redaction, so no ontology identifier leaks to a schema-blind caller.
# ``trace`` is not listed: serve strips its step details upstream. If serve stops
# doing that, route ``trace`` through ``_withhold_schema_fields`` too.
_TRANSLATE_SPARQL_SCHEMA_KEYS = ("sparqlQuery",)
_GRAPH_TRAVERSAL_SCHEMA_KEYS = ("entities", "relationships")


def _withhold_schema_fields(result: dict, keys: tuple[str, ...]) -> dict:
    """Blank the named schema-bearing keys in a tool result, in place.

    Blanks to ``[]`` or ``""`` instead of deleting so the Smithy output shape is kept.
    """
    for key in keys:
        if key in result:
            result[key] = [] if isinstance(result[key], list) else ""
    return result


def _suppress_schema_unless_viewer(
    result: dict, profile: dict, namespace_id: str, keys: tuple[str, ...], tool: str
) -> dict:
    """Withhold ``keys`` unless the caller holds ``viewOntology``.

    The decision was made with the tool action in ``resolve_grant`` (one policy
    load per call). Each withhold is logged so an operator can tell "withheld by
    policy" from "the tool found nothing".
    """
    if _caller_can_view_ontology(profile):
        return result
    logger.info("schema_fields_withheld", tool=tool, namespace=namespace_id, fields=list(keys))
    return _withhold_schema_fields(result, keys)


def _extract_bearer_token(ctx: Any) -> str:
    """Extract the raw Bearer token from the request context for forwarding.

    Returns empty string if auth is disabled (local dev mode).
    """
    if not config.auth_enabled:
        return ""
    if ctx is None:
        raise ValueError("Bearer token required but request context is None")
    try:
        request = ctx.request_context.request
        if request is not None and hasattr(request, "headers"):
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                return auth_header[7:]
            if auth_header:
                return auth_header
    except (AttributeError, ValueError):
        pass
    raise ValueError("Bearer token extraction failed: no Authorization header found in request")


def _build_authorizer_context(caller: CallerIdentity, profile: dict) -> dict:
    """Build a synthetic API GW authorizer context for direct Lambda invocations."""
    return {
        "principalId": caller.user_id,
        "userId": caller.user_id,
        "sub": caller.user_id,
        "email": caller.raw_claims.get("email", caller.user_id),
        "groups": ",".join(caller.roles),
        "globalRoles": ",".join(profile.get("globalRoles", [])),
    }


def _metric_schema_flags(profile: dict) -> dict[str, str]:
    """Schema-visibility flags (``canViewSource`` / ``canViewOntology``) for the metric service.

    The metric service withholds metric SQL, source tables and ontology concepts
    unless these are ``"true"``. This mirrors what the API Gateway authorizer adds
    on ``readMetric`` requests; this path invokes the metric Lambda directly, so
    MCP supplies the same flags, from the decisions cached by ``resolve_grant``.
    """
    return to_authorizer_flags(
        SchemaVisibility(
            source=_schema_right(profile, VIEW_SOURCE_ACTION),
            ontology=_schema_right(profile, VIEW_ONTOLOGY_ACTION),
        )
    )


@mcp.tool()
async def list_metrics(
    ctx: Context,
    namespaceId: str,
    maxResults: int = 100,
    nextToken: str | None = None,
) -> str:
    """List governed metric definitions in the namespace.

    Returns metric summaries including name, description, dimensions, and synonyms.
    Use this tool FIRST to discover what metrics are available before querying.
    A metric's ``label`` is for display only; queries should use the metric ``name`` or its synonyms.

    This is a read-only, lightweight lookup — no LLM calls, no SQL execution.

    The Smithy ``ListMetrics`` operation declares a ``status`` filter, but the
    metric-service backend does not yet honour it. Rather than accept ``status``
    here and silently return an unfiltered list (which the caller would read as
    filtered), the parameter is intentionally not exposed until the backend can
    apply it.

    Args:
        ctx: MCP request context carrying the caller's identity and bearer token.
        namespaceId: The namespace to list metrics for.
        maxResults: Maximum number of metrics to return (default 100, max 200).
        nextToken: Continuation token from a prior response. When present the
            call resumes where that response left off; matches the Smithy
            ``ListMetrics`` input shape.

    Returns:
        JSON with a ``metrics`` array; ``nextToken`` when the backend
        paginated. Same envelope as ``GET /namespaces/{id}/metrics`` on the
        metric-service surface, so the two paths are drop-in interchangeable.
    """
    start = time.perf_counter()
    caller = None
    try:
        caller, profile = await _resolve_caller_and_profile(
            namespaceId, ctx, cedar_action=TOOL_CEDAR_ACTION["list_metrics"]
        )
        token = _extract_bearer_token(ctx)
        lc = _get_lambda_client()
        # Local dev: no authorizer context, so metric-service sees neither flag and
        # returns full definitions (its deliberate missing-flag rule).
        authz_ctx = None
        if caller.user_id != "local-dev":
            authz_ctx = _build_authorizer_context(caller, profile)
            authz_ctx.update(_metric_schema_flags(profile))
        result = await discovery.list_metrics(
            lc,
            namespaceId,
            token,
            max_results=maxResults,
            next_token=nextToken,
            authorizer_context=authz_ctx,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        if caller:
            log_tool_invocation(caller, "list_metrics", namespaceId, duration_ms=duration_ms)
        return json.dumps(result, indent=2)

    except (ClaimsExtractionError, GrantResolutionError) as e:
        if caller:
            log_tool_invocation(caller, "list_metrics", namespaceId, success=False, error=str(e))
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.error("list_metrics_failed", namespace=namespaceId, error=str(e))
        if caller:
            log_tool_invocation(caller, "list_metrics", namespaceId, success=False, error=str(e))
        raise


@mcp.tool()
async def describe_schema(
    ctx: Context,
    namespaceId: str,
    maxResults: int = 100,
    includeProperties: bool = True,
    classFilter: str | None = None,
) -> str:
    """Describe available ontology classes, properties, and data source tables.

    Returns the schema structure of the namespace — classes, their properties,
    and connected data sources. Use this tool to understand what data is
    available and how entities are related before querying. Classes mapped to
    tables (queryable through SQL) are listed first, then the rest by label;
    classFilter (an exact class IRI, or an IRI namespace ending in # or /) is
    applied before maxResults, so matching classes are not crowded out by the page limit.

    This is a read-only, lightweight lookup — no LLM calls, no SQL execution.

    Args:
        ctx: MCP request context carrying the caller's identity and bearer token.
        namespaceId: The namespace to describe.
        maxResults: Maximum number of classes to return (default 100, max 500).
        includeProperties: Whether to include properties for each class (default True).
        classFilter: Optional class URI or prefix to restrict the response to a
            single class or subtree. Matches the Smithy ``DescribeSchema`` input.

    Returns:
        JSON with ``classes`` array and ``ontologyVersion``. Same envelope as
        ``GET /namespaces/{namespaceId}/schema`` on the data-layer surface.
    """
    start = time.perf_counter()
    caller = None
    try:
        caller, profile = await _resolve_caller_and_profile(
            namespaceId, ctx, cedar_action=TOOL_CEDAR_ACTION["describe_schema"]
        )
        token = _extract_bearer_token(ctx)
        lc = _get_lambda_client()
        authz_ctx = _build_authorizer_context(caller, profile) if caller.user_id != "local-dev" else None
        result = await discovery.describe_schema(
            lc,
            namespaceId,
            token,
            max_results=maxResults,
            include_properties=includeProperties,
            class_filter=classFilter,
            authorizer_context=authz_ctx,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        if caller:
            log_tool_invocation(caller, "describe_schema", namespaceId, duration_ms=duration_ms)
        return json.dumps(result, indent=2)

    except (ClaimsExtractionError, GrantResolutionError) as e:
        if caller:
            log_tool_invocation(caller, "describe_schema", namespaceId, success=False, error=str(e))
        raise ValueError(str(e)) from e
    except Exception as e:
        logger.error("describe_schema_failed", namespace=namespaceId, error=str(e))
        if caller:
            log_tool_invocation(caller, "describe_schema", namespaceId, success=False, error=str(e))
        raise


# A Literal (not ``str``) so FastMCP publishes the accepted values in the tool schema.
# Serve silently falls back to DEFAULT_STRATEGY on an unknown value, so an
# unvalidated typo would run the wrong engine. Mirrors the Smithy ``QueryStrategy`` enum.
QueryStrategyLiteral = Literal[
    "best",
    "ontop",
    "nl_to_sql",
    "ontop_first",
    "nl_to_sql_first",
    "deep-reasoning",
]


@mcp.tool()
async def query(
    ctx: Context,
    query: str,
    namespaceId: str,
    execute: bool | None = None,
    tierOverride: int | None = None,
    mode: str | None = None,
    strategy: QueryStrategyLiteral | None = None,
    dimensions: list[dict] | None = None,
    includeSupporting: bool = True,
    maxResults: int = 1000,
    timeoutMs: int | None = None,
) -> str:
    """End-to-end natural language query with tiered resolution.

    Resolves the query through the full pipeline:
    - Tier 1: Deterministic metric lookup (confidence=1.0, no LLM)
    - Tier 2: Ontology-guided SPARQL→SQL translation
    - Tier 3: Agentic fallback with parallel retrieval and LLM synthesis

    Args:
        ctx: MCP request context carrying the caller's identity and bearer token.
        query: Natural-language question (e.g., "What is monthly revenue by region?").
        namespaceId: The namespace to query against.
        execute: When ``False``, translate only — return the generated query
            without executing it. Absent = default (execute).
        tierOverride: Pin resolution to a specific tier (1, 2, or 3), bypassing
            automatic routing. Omit for auto.
        mode: Execution mode — ``standard`` (single-shot) or ``deep-reasoning``
            (multi-step reasoning loop; higher recall, much slower). Absent = the
            serve deployment default.
        strategy: Which Tier-2 engine answers a structured query — ``best``,
            ``ontop``, ``nl_to_sql``, ``ontop_first``, ``nl_to_sql_first`` or
            ``deep-reasoning``. Absent = the serve default (``nl_to_sql_first``).
            A DIFFERENT axis from ``mode``: ``mode`` decides whether the whole
            T1→T2→T3 cascade is replaced by the Tier-3 reasoning loop, this decides
            which engine answers within Tier 2. Ignored when the resolved tier is
            not 2. An explicit value is never overridden by automatic tier gating.
        dimensions: Dimension filters constraining the query. Each entry is a
            ``{name, value}`` object matching ``DimensionFilter``.
        includeSupporting: Whether to include supporting document chunks (default True).
        maxResults: Maximum result rows (default 1000, max 10000).
        timeoutMs: Optional caller budget in milliseconds. When supplied, the
            Context Manager clamps the request deadline to ``min(transport,
            timeoutMs)`` so a caller can ask for a shorter, graceful return
            instead of the full transport budget. Must be an integer from 1 to
            the MCP transport timeout in milliseconds (120000 by default, set
            by ``CM_INVOKE_TIMEOUT_S``); any other value is rejected with a
            tool error.

    Returns:
        JSON envelope ``{result, requestId, sessionId}`` matching the Smithy
        ``Query`` output — same shape data-layer returns, so identifiers stay
        observable in traces.
    """
    start = time.perf_counter()
    caller = None
    try:
        caller, profile = await _resolve_caller_and_profile(namespaceId, ctx, cedar_action=TOOL_CEDAR_ACTION["query"])
        token = _extract_bearer_token(ctx)
        cm = _get_cm_client()
        result = await execution.execute_query(
            cm,
            query,
            namespaceId,
            profile,
            token,
            execute=execute,
            tier_override=tierOverride,
            mode=mode,
            strategy=strategy,
            dimensions=dimensions,
            include_supporting=includeSupporting,
            max_results=maxResults,
            timeout_ms=timeoutMs,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        if caller:
            log_tool_invocation(caller, "query", namespaceId, duration_ms=duration_ms)
        return json.dumps(result, indent=2, default=str)

    except (ClaimsExtractionError, GrantResolutionError) as e:
        if caller:
            log_tool_invocation(caller, "query", namespaceId, success=False, error=str(e))
        raise ValueError(str(e)) from e
    except ContextManagerError as e:
        if caller:
            log_tool_invocation(caller, "query", namespaceId, success=False, error=str(e))
        raise ValueError(f"CM {e.status_code}: {e.message}") from e
    except Exception as e:
        logger.error("query_failed", namespace=namespaceId, error=str(e))
        if caller:
            log_tool_invocation(caller, "query", namespaceId, success=False, error=str(e))
        raise


@mcp.tool()
async def translate_sparql(
    ctx: Context,
    query: str,
    namespaceId: str,
) -> str:
    """Translate a natural language query to SPARQL using the published ontology.

    Returns the generated SPARQL query and a confidence score.
    Useful for debugging, previewing what the system will execute, and
    for agents that want to inspect the query plan before execution.

    Args:
        ctx: MCP request context carrying the caller's identity and bearer token.
        query: Natural-language question to translate.
        namespaceId: The namespace (determines which ontology is used).

    Returns:
        JSON ``{sparqlQuery, confidence, trace, ontologyVersion}`` matching
        the Smithy ``TranslateSPARQL`` output.
    """
    start = time.perf_counter()
    caller = None
    try:
        caller, profile = await _resolve_caller_and_profile(
            namespaceId, ctx, cedar_action=TOOL_CEDAR_ACTION["translate_sparql"]
        )
        token = _extract_bearer_token(ctx)
        cm = _get_cm_client()
        result = await execution.execute_translate_sparql(cm, query, namespaceId, profile, token)
        if config.auth_enabled:
            result = _suppress_schema_unless_viewer(
                result, profile, namespaceId, _TRANSLATE_SPARQL_SCHEMA_KEYS, "translate_sparql"
            )
        duration_ms = int((time.perf_counter() - start) * 1000)
        if caller:
            log_tool_invocation(caller, "translate_sparql", namespaceId, duration_ms=duration_ms)
        return json.dumps(result, indent=2, default=str)

    except (ClaimsExtractionError, GrantResolutionError) as e:
        if caller:
            log_tool_invocation(caller, "translate_sparql", namespaceId, success=False, error=str(e))
        raise ValueError(str(e)) from e
    except ContextManagerError as e:
        if caller:
            log_tool_invocation(caller, "translate_sparql", namespaceId, success=False, error=str(e))
        raise ValueError(f"CM {e.status_code}: {e.message}") from e
    except Exception as e:
        logger.error("translate_sparql_failed", namespace=namespaceId, error=str(e))
        if caller:
            log_tool_invocation(caller, "translate_sparql", namespaceId, success=False, error=str(e))
        raise


@mcp.tool()
async def rag_retrieval(
    ctx: Context,
    query: str,
    namespaceId: str,
    topK: int = 10,
    minScore: float | None = None,
    sourceFilter: list[str] | None = None,
    entityFilter: list[str] | None = None,
) -> str:
    """Retrieve semantically similar document chunks from the knowledge base.

    Performs vector similarity search against indexed documents.
    Returns chunks with source attribution and relevance scores.

    Args:
        ctx: MCP request context carrying the caller's identity and bearer token.
        query: Natural-language search query.
        namespaceId: The namespace to search within.
        topK: Maximum number of chunks to return (default 10, max 100).
        minScore: Minimum similarity score threshold (0.0 to 1.0).
        sourceFilter: Not supported yet — leave unset. A non-empty value is
            rejected with an error rather than silently ignored.
        entityFilter: Not supported yet — leave unset. A non-empty value is
            rejected with an error rather than silently ignored.

    Returns:
        JSON ``{chunks, trace, queryEmbeddingModel}`` matching the Smithy
        ``KBSearch`` output.
    """
    start = time.perf_counter()
    caller = None
    try:
        caller, profile = await _resolve_caller_and_profile(
            namespaceId, ctx, cedar_action=TOOL_CEDAR_ACTION["rag_retrieval"]
        )
        token = _extract_bearer_token(ctx)
        cm = _get_cm_client()
        result = await execution.execute_rag_retrieval(
            cm,
            query,
            namespaceId,
            profile,
            token,
            top_k=topK,
            min_score=minScore,
            source_filter=sourceFilter,
            entity_filter=entityFilter,
        )
        duration_ms = int((time.perf_counter() - start) * 1000)
        if caller:
            log_tool_invocation(caller, "rag_retrieval", namespaceId, duration_ms=duration_ms)
        return json.dumps(result, indent=2, default=str)

    except (ClaimsExtractionError, GrantResolutionError) as e:
        if caller:
            log_tool_invocation(caller, "rag_retrieval", namespaceId, success=False, error=str(e))
        raise ValueError(str(e)) from e
    except ContextManagerError as e:
        if caller:
            log_tool_invocation(caller, "rag_retrieval", namespaceId, success=False, error=str(e))
        raise ValueError(f"CM {e.status_code}: {e.message}") from e
    except Exception as e:
        logger.error("rag_retrieval_failed", namespace=namespaceId, error=str(e))
        if caller:
            log_tool_invocation(caller, "rag_retrieval", namespaceId, success=False, error=str(e))
        raise


@mcp.tool()
async def graph_traversal(
    ctx: Context,
    startUri: str,
    namespaceId: str,
    maxDepth: int = 2,
    direction: str = "both",
    maxResults: int = 100,
    relationshipFilter: list[str] | None = None,
) -> str:
    """Traverse the semantic graph for entity relationships and context.

    Starting from a given entity URI, explores relationships up to the
    specified depth. Returns entities and their relationships.

    Args:
        ctx: MCP request context carrying the caller's identity and bearer token.
        startUri: IRI of the entity to start traversal from.
        namespaceId: The namespace to traverse within.
        maxDepth: Maximum traversal depth (default 2, max 5).
        direction: Traversal direction — ``outgoing``, ``incoming``, or ``both`` (default).
        maxResults: Maximum entities to return (default 100, max 1000).
        relationshipFilter: Restrict traversal to these relationship (predicate) IRIs.

    Returns:
        JSON ``{entities, relationships, trace}`` matching the Smithy
        ``GraphTraverse`` output.
    """
    start = time.perf_counter()
    caller = None
    try:
        caller, profile = await _resolve_caller_and_profile(
            namespaceId, ctx, cedar_action=TOOL_CEDAR_ACTION["graph_traversal"]
        )
        token = _extract_bearer_token(ctx)
        cm = _get_cm_client()
        result = await execution.execute_graph_traversal(
            cm,
            startUri,
            namespaceId,
            profile,
            token,
            max_depth=maxDepth,
            direction=direction,
            max_results=maxResults,
            relationship_filter=relationshipFilter,
        )
        if config.auth_enabled:
            result = _suppress_schema_unless_viewer(
                result, profile, namespaceId, _GRAPH_TRAVERSAL_SCHEMA_KEYS, "graph_traversal"
            )
        duration_ms = int((time.perf_counter() - start) * 1000)
        if caller:
            log_tool_invocation(caller, "graph_traversal", namespaceId, duration_ms=duration_ms)
        return json.dumps(result, indent=2, default=str)

    except (ClaimsExtractionError, GrantResolutionError) as e:
        if caller:
            log_tool_invocation(caller, "graph_traversal", namespaceId, success=False, error=str(e))
        raise ValueError(str(e)) from e
    except ContextManagerError as e:
        if caller:
            log_tool_invocation(caller, "graph_traversal", namespaceId, success=False, error=str(e))
        raise ValueError(f"CM {e.status_code}: {e.message}") from e
    except Exception as e:
        logger.error("graph_traversal_failed", namespace=namespaceId, error=str(e))
        if caller:
            log_tool_invocation(caller, "graph_traversal", namespaceId, success=False, error=str(e))
        raise


if __name__ == "__main__":
    from coa_common.logging import setup_logging

    setup_logging(config.log_level)
    logger.info(
        "mcp_server_starting",
        host=config.mcp_host,
        port=config.mcp_port,
        auth_enabled=config.auth_enabled,
        tools=["list_metrics", "describe_schema", "query", "translate_sparql", "rag_retrieval", "graph_traversal"],
    )

    if os.environ.get("SCL_ALLOW_CORS", "false").lower() == "true":
        from .dev_support import add_cors_middleware

        starlette_app = mcp.streamable_http_app()
        add_cors_middleware(starlette_app)
        import uvicorn

        uvicorn.run(starlette_app, host=config.mcp_host, port=config.mcp_port)
    else:
        mcp.run(transport="streamable-http")
