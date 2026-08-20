# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Execution tools — query, translate_sparql, rag_retrieval, graph_traversal.

These tools delegate to the Context Manager via its AgentCore HTTP invocation
endpoint. The MCP server is a thin protocol adapter: it handles MCP framing,
auth extraction, and tool dispatch, but all orchestration (SQL generation,
Cedar authorization, DB access, LLM calls) happens in the Context Manager.

Identity propagation: the caller's JWT is forwarded to the CM, which validates
it independently and resolves roles from the same Cognito pool.

Contract note: the payload keys sent to CM and the wrapper keys these helpers
return are the same Smithy shape data-layer uses (``query``, ``startUri``,
``topK``, ``maxDepth`` etc. on input; ``{result, requestId, sessionId}`` on
output where applicable). A single SDK generated from the Smithy models can
target either surface without a translation layer.
"""

from __future__ import annotations

from typing import Any

import structlog
from coa_common.smithy_shapes import normalize_dimensions

from ..clients.context_manager import ContextManagerClient

logger = structlog.get_logger(__name__)


class ContextManagerError(Exception):
    """Raised when the Context Manager returns a >=400 status.

    Carries the status code so the tool layer can map it back to a matching
    MCP error surface instead of collapsing every failure to a generic 500.
    """

    def __init__(self, status_code: int, message: str) -> None:
        """Record ``status_code`` alongside the human-readable ``message``."""
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _build_cm_profile(profile: dict) -> dict:
    """Build the profile dict the CM entrypoint expects.

    The CM strips globalRoles/resourceRoles (security) but reads userId
    and groups to resolve roles authoritatively from its own RRM table.
    We pass the original IdP groups (not the already-resolved globalRoles)
    so the CM can query Group::<idp_group> in the RRM table.
    """
    return {
        "userId": profile.get("userId", ""),
        "groups": profile.get("idpGroups") or profile.get("globalRoles", []),
        "namespace": profile.get("namespace", ""),
    }


def _raise_for_cm_error(response: dict) -> None:
    """Raise ContextManagerError if CM signalled a >=400 status.

    Data-layer's handler forwards CM's ``statusCode`` verbatim; the MCP tool
    layer must not silently downgrade every failure to a 500. Callers that
    catch ``ContextManagerError`` can preserve the code.
    """
    status_code = response.get("statusCode")
    if isinstance(status_code, int) and status_code >= 400:
        message = response.get("message") or response.get("error") or "Context Manager error"
        raise ContextManagerError(status_code, str(message))


async def execute_query(
    cm_client: ContextManagerClient,
    query: str,
    namespace_id: str,
    profile: dict,
    bearer_token: str,
    *,
    execute: bool | None = None,
    tier_override: int | None = None,
    mode: str | None = None,
    dimensions: list[dict] | None = None,
    include_supporting: bool = True,
    max_results: int = 1000,
) -> dict[str, Any]:
    """End-to-end NL query with tiered resolution.

    Mirrors the Smithy ``Query`` operation on the data-layer service — the
    payload keys and response envelope match one-for-one so a client can call
    either surface identically. Options mapped from Smithy input fields:
    ``execute``, ``tierOverride``, ``mode``, ``dimensions``, ``includeSupporting``,
    ``maxResults``.

    The Smithy contract also declares ``timeoutMs``, but the Context Manager
    does not currently honour it (no reader in ``coa_serve``). Rather than
    forward a phantom deadline that gets silently ignored, this signature omits
    it until CM plumbs it into the resolve pipeline. Track: MR !939 review
    comment ``b6dea1de``.

    Returns the ``{result, requestId, sessionId}`` envelope; the caller should
    forward it verbatim so downstream identifiers stay observable in traces.
    Raises :class:`ContextManagerError` on CM >=400 with the code preserved.
    """
    payload: dict[str, Any] = {
        "query": query,
        "namespace": namespace_id,
        "profile": _build_cm_profile(profile),
        "options": {"maxResults": min(max_results, 10000)},
    }
    if execute is False:
        payload["options"]["execute"] = False
    if tier_override is not None:
        payload["options"]["tierOverride"] = tier_override
    if mode is not None:
        payload["options"]["mode"] = mode
    if dimensions:
        # Smithy DimensionFilterList arrives as ``[{name, value}, ...]``; CM's
        # Tier-1 ``substitute_dimensions`` calls ``.items()`` and expects a
        # mapping. Normalize here so a Tier-1 metric query with dimensions does
        # not 500 with ``AttributeError``.
        normalized = normalize_dimensions(dimensions)
        if normalized:
            payload["options"]["dimensions"] = normalized
    if not include_supporting:
        payload["options"]["includeSupporting"] = False

    response = await cm_client.invoke(payload, bearer_token)
    _raise_for_cm_error(response)
    return {
        "result": response.get("result", _strip_envelope(response)),
        "requestId": response.get("requestId"),
        "sessionId": response.get("sessionId"),
    }


async def execute_translate_sparql(
    cm_client: ContextManagerClient,
    query: str,
    namespace_id: str,
    profile: dict,
    bearer_token: str,
) -> dict[str, Any]:
    """Translate NL to SPARQL using the published ontology.

    Response shape matches the Smithy ``TranslateSPARQL`` output:
    ``{sparqlQuery, confidence, trace, ontologyVersion}``.
    """
    payload: dict[str, Any] = {
        "action": "translate",
        "query": query,
        "namespace": namespace_id,
        "profile": _build_cm_profile(profile),
    }

    response = await cm_client.invoke(payload, bearer_token)
    _raise_for_cm_error(response)
    unwrapped = _strip_envelope(response)

    sparql = unwrapped.get("sparqlQuery") or unwrapped.get("sparql_generated") or unwrapped.get("queryUsed", "")
    return {
        "sparqlQuery": sparql,
        "confidence": unwrapped.get("confidence", {"score": 0.0, "rationale": ""}),
        "trace": unwrapped.get("trace", []),
        "ontologyVersion": unwrapped.get("ontologyVersion"),
    }


async def execute_rag_retrieval(
    cm_client: ContextManagerClient,
    query: str,
    namespace_id: str,
    profile: dict,
    bearer_token: str,
    *,
    top_k: int = 10,
    min_score: float | None = None,
    source_filter: list[str] | None = None,
    entity_filter: list[str] | None = None,
) -> dict[str, Any]:
    """Retrieve semantically similar document chunks from the knowledge base.

    Response shape matches the Smithy ``KBSearch`` output:
    ``{chunks, trace, queryEmbeddingModel}``.
    """
    payload: dict[str, Any] = {
        "action": "kbSearch",
        "query": query,
        "namespace": namespace_id,
        "profile": _build_cm_profile(profile),
        "options": {"topK": min(top_k, 100)},
    }
    if min_score is not None:
        payload["options"]["minScore"] = min_score
    if source_filter:
        payload["options"]["sourceFilter"] = source_filter
    if entity_filter:
        payload["options"]["entityFilter"] = entity_filter

    response = await cm_client.invoke(payload, bearer_token)
    _raise_for_cm_error(response)
    unwrapped = _strip_envelope(response)

    return {
        "chunks": unwrapped.get("chunks") or unwrapped.get("supportingContent") or [],
        "trace": unwrapped.get("trace", []),
        "queryEmbeddingModel": unwrapped.get("queryEmbeddingModel"),
    }


async def execute_graph_traversal(
    cm_client: ContextManagerClient,
    start_uri: str,
    namespace_id: str,
    profile: dict,
    bearer_token: str,
    *,
    max_depth: int = 2,
    direction: str = "both",
    max_results: int = 100,
    relationship_filter: list[str] | None = None,
) -> dict[str, Any]:
    """Traverse the semantic graph for entity relationships and context.

    Response shape matches the Smithy ``GraphTraverse`` output:
    ``{entities, relationships, trace}``.
    """
    _VALID_DIRECTIONS = ("outgoing", "incoming", "both")
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"Invalid direction '{direction}': must be one of {_VALID_DIRECTIONS}")

    options: dict[str, Any] = {
        "startUri": start_uri,
        "maxDepth": min(max_depth, 5),
        "direction": direction,
        "maxResults": min(max_results, 1000),
    }
    if relationship_filter:
        options["relationshipFilter"] = relationship_filter

    payload: dict[str, Any] = {
        "action": "graphTraverse",
        "namespace": namespace_id,
        "profile": _build_cm_profile(profile),
        "options": options,
    }

    response = await cm_client.invoke(payload, bearer_token)
    _raise_for_cm_error(response)
    unwrapped = _strip_envelope(response)

    graph_context = unwrapped.get("graphContext") or unwrapped.get("graph_context") or {}
    return {
        "entities": graph_context.get("entities") or unwrapped.get("entities", []),
        "relationships": graph_context.get("relationships") or unwrapped.get("relationships", []),
        "trace": unwrapped.get("trace", []),
    }


def _strip_envelope(response: dict) -> dict[str, Any]:
    """Return the inner CM result body, whether or not it was wrapped.

    The CM ``@app.entrypoint`` sometimes returns ``{"result": {...}, ...}``
    and sometimes returns the operation body flat; the operation-specific
    helpers above tolerate both.
    """
    if isinstance(response.get("result"), dict):
        return response["result"]
    return response
