# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Graph_Traversal_Tool: entity/edge traversal over the Knowledge_Graph (Req 2.3).

:class:`GraphTraversalTool` traverses the graphrag-toolkit **property graph** (the
graph kg-build populates in Neptune Database) using **openCypher**, executed
through the toolkit ``GraphStore``. It is NOT SPARQL and does NOT use the serve
layer's ``GraphTraverser``/``NeptuneGraphClient`` (those target the RDF ontology
graph — a different dataset and query language; see the design's "Two graphs").

Three traversal modes are exposed through the uniform :class:`Tool` contract:

- ``keyword``    — find entities whose ``search_str`` contains terms extracted
  from a natural-language sub-question.
- ``uris``       — expand one hop over any ``__RELATION__`` edge from known entity
  ids.
- ``edge_typed`` — the prepared :data:`EDGE_TYPED_TRAVERSAL` Query_Template
  (Req 5.5): given 1-100 entity ids and 1-50 edge types, traverse exactly those
  edge types from those entities and return the connected nodes and their
  supporting statement text. This is the edge-type constraint the toolkit's own
  retrievers do not impose.

Ontology→property-graph edge-type mapping (Req 3.9): the requested edge types
(chosen by the planner from the ontology's edge-type set) are normalized with
:func:`normalize_edge_type` and matched against the same normalization of the
property graph's ``__RELATION__.value`` predicate strings, so the two independent
vocabularies reconcile despite casing/separator differences. A requested edge type
with no normalized match simply yields zero rows (an empty, non-degraded result).

Import discipline (Requirement 12.3): module load imports only the dependency-free
``.base`` contract, the template strings, and the sibling ``tier3`` result
dataclasses (``ChunkResult`` / ``GraphEntity``), none of which import
``graphrag_toolkit``. The toolkit ``GraphStoreFactory`` and ``TenantId`` are
imported **lazily** inside the first invocation, so importing this module never
pulls the toolkit into ``sys.modules``.

Per-Tool failure tolerance (Req 9.1-9.2): :meth:`GraphTraversalTool.invoke` never
raises for an expected failure. A bad/empty parameter set, an out-of-bounds
entity/edge count, an unconfigured graph store, a backend error, or a query
timeout all yield a (possibly empty) :class:`ToolResult` whose ``detail`` records
the degradation so the controller can record a degraded source and continue.
"""

from __future__ import annotations

import asyncio
import os
import re
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1, sha256
from typing import Any

import structlog

from ....graph_context import (
    MAX_SUPPORTING_STATEMENTS_BYTES,
    MAX_SUPPORTING_STATEMENTS_CHARS,
    MAX_SUPPORTING_STATEMENTS_PER_ENTITY,
    bound_supporting_statements,
    encode_supporting_statements,
    fair_bound_statement_groups,
)
from ...graph_traverser import GraphEntity
from ...vector_retriever import ChunkResult
from ..templates import (
    CONNECTED_TEXT_QUERY_LIMIT,
    CONNECTED_TEXT_RETRIEVAL,
    DEFAULT_EDGE_TYPED_LIMIT,
    EDGE_TYPED_TRAVERSAL,
    KEYWORD_ENTITY_SEARCH,
    KEYWORD_ENTITY_SEARCH_WITH_CONTAINERS,
    MAX_CONNECTED_TEXT_ROWS,
    MAX_EDGE_TYPES,
    MAX_ENTITIES,
    MAX_TRAVERSAL_LIMIT,
    MIN_EDGE_TYPES,
    MIN_ENTITIES,
    MIN_TRAVERSAL_LIMIT,
    URI_EXPANSION,
    normalize_edge_type,
)
from .base import ToolResult

logger = structlog.get_logger(__name__)

TOOL_NAME = "graph_traversal"

# Compatibility contract with graphrag-lexical-graph 3.18.7's persisted
# ``Entity.search_str``. Keep this tiny local copy instead of importing the
# toolkit's private graph_utils module at import time: that module eagerly pulls
# the toolkit and LlamaIndex graph stack, violating this tool's lazy-import
# boundary. A unit test compares this function with the installed helper.
_GRAPHRAG_SEARCH_STRING_PATTERN = re.compile(r"([^\s\w]|_)+")


def normalize_graphrag_search_text(value: str) -> str:
    """Normalize a query candidate exactly like toolkit 3.18.7 ``search_str``."""
    normalized = _GRAPHRAG_SEARCH_STRING_PATTERN.sub(" ", value)
    while "  " in normalized:
        normalized = normalized.replace("  ", " ")
    return normalized.lower().strip()


# ── synthetic predicate identity for label-only property-graph edges ────
#
# The toolkit's ``EntityRelationGraphBuilder`` writes only ``r.value`` — an
# LLM-extracted predicate STRING — on ``__RELATION__`` edges. No ingest path in
# this repository writes ``r.predicateUri`` (``grep -rn predicateUri
# packages/sources`` finds nothing), so on a real graph every relationship arrives
# label-only. The wire contract requires a ``predicateUri``, and dropping the edge
# there is worse than useless: the synthesizer still asserts the relationship in
# the answer from the label, so the user reads a claim whose evidence is missing
# from ``result.graphContext.relationships``.
#
# So a label-only edge is given a STABLE, TENANT-SCOPED SYNTHETIC identifier
# derived from the label. It is deliberately NOT the label verbatim and not an
# ontology IRI: the ``urn:coa:graph-predicate:`` scheme names it as minted by this
# service from a property-graph predicate string, so a consumer can tell it apart
# from an ontology predicate IRI. It is a pure function of (namespace, label), so
# the same predicate in the same tenant always resolves to the same IRI across
# requests. Both lossy display slugs carry collision-resistant digests, making
# accidental identity sharing computationally negligible without claiming
# mathematical uniqueness.
SYNTHETIC_PREDICATE_SCHEME = "urn:coa:graph-predicate"
_SLUG_UNSAFE = re.compile(r"[^a-z0-9_]+")
_MAX_SYNTHETIC_SLUG_CHARS = 64


def synthetic_predicate_uri(namespace: str, predicate_label: str) -> str:
    """Mint the stable tenant-scoped IRI for a label-only graph predicate.

    Args:
        namespace: The COA namespace owning the graph (the tenant scope).
        predicate_label: The property graph's ``__RELATION__.value`` string.

    Returns:
        A
        ``urn:coa:graph-predicate:<tenant>:<tenant-digest>:<slug>-<digest>`` IRI,
        or an empty string when ``predicate_label`` is blank (nothing to derive
        from).
    """
    if not predicate_label or not predicate_label.strip():
        return ""
    tenant_key = namespace.strip().lower() or "default"
    tenant = (_SLUG_UNSAFE.sub("-", tenant_key).strip("-") or "default")[:_MAX_SYNTHETIC_SLUG_CHARS]
    tenant_digest = sha256(tenant_key.encode("utf-8")).hexdigest()[:16]
    slug = (_SLUG_UNSAFE.sub("-", normalize_edge_type(predicate_label)).strip("-") or "predicate")[
        :_MAX_SYNTHETIC_SLUG_CHARS
    ]
    # The slug is lossy (case, punctuation, script), so a 128-bit digest of the
    # RAW label makes an accidental collision negligibly probable. It is still a
    # hash-derived identifier, not a proof that arbitrary distinct inputs can
    # never collide.
    predicate_digest = sha256(predicate_label.encode("utf-8")).hexdigest()[:32]
    return f"{SYNTHETIC_PREDICATE_SCHEME}:{tenant}:{tenant_digest}:{slug}-{predicate_digest}"


# Default wall-clock cap for a single graph query. The toolkit call is synchronous
# and cannot be cancelled once running, so it executes on a bounded worker pool and
# is awaited under this timeout; a timed-out call is abandoned (its worker drains)
# rather than cancelled, bounding thread usage. The controller also applies the
# configured per-tool timeout around invoke().
_GRAPH_QUERY_TIMEOUT_S: float = float(os.environ.get("AGENTIC_GRAPH_QUERY_TIMEOUT_S", "30"))

# Bounded executor for the synchronous toolkit GraphStore calls (mirrors the
# discipline in BaselineLexicalRetriever): excess concurrent queries queue rather
# than spawning unbounded threads, and a timed-out uncancellable call cannot starve
# unrelated to_thread work.
_GRAPH_MAX_WORKERS: int = int(os.environ.get("AGENTIC_GRAPH_MAX_WORKERS", "4"))
_EXECUTOR = ThreadPoolExecutor(max_workers=_GRAPH_MAX_WORKERS, thread_name_prefix="agentic-graph")

_DESCRIPTION = (
    "Graph traversal over the knowledge graph (property graph). Four modes: "
    "'keyword' finds entities whose labels match terms in the sub-question; 'uris' "
    "expands one hop outward from known entity ids; 'edge_types' discovers which "
    "edge-type predicates actually exist on given entity ids (use this to learn the "
    "real relationships available before an edge_typed traversal); 'edge_typed' "
    "traverses a specific set of edge types (1-50) from a specific set of entity ids "
    "(1-100) and returns the connected nodes and their text. Prefer 'edge_typed' "
    "when you know which edge type connects the concepts you need (e.g. follow "
    "HAS_SEGMENT from a company entity). Edge types SHOULD be drawn from the "
    "ontology when it lists a fitting one, but you MAY use a predicate discovered via "
    "'edge_types' that the ontology omits — the ontology is a pruned subset of the "
    "graph's real relationships. Entity ids come from a prior entity-anchoring retrieval."
)

_INPUT_SCHEMA: dict = {
    "mode": "str (one of: keyword, uris, edge_types, edge_typed)",
    "query": "str (keyword mode: the sub-question text)",
    "uris": "list[str] (uris mode: seed entity ids)",
    "entity_ids": "list[str] (edge_types/edge_typed mode: 1-100 source entity ids)",
    "edge_types": "list[str] (edge_typed mode: 1-50 edge-type labels)",
    "limit": "int (max rows, 1-100, default 100)",
}
_OUTPUT_SCHEMA: dict = {"entities": "list", "chunks": "list", "items": "list"}


def _entity_item(entity: GraphEntity) -> dict:
    """Normalize a ``GraphEntity`` to a plain dict for ``ToolResult.items``."""
    return {
        "type": "entity",
        "uri": entity.uri,
        "label": entity.label,
        "entity_type": entity.type,
        "relationships": list(entity.relationships),
        "properties": dict(entity.properties),
    }


def _chunk_item(chunk: ChunkResult) -> dict:
    """Normalize a connected-node ``ChunkResult`` to a dict for ``ToolResult.items``."""
    return {
        "type": "chunk",
        "chunk_id": chunk.chunk_id,
        "text": chunk.text,
        "source_doc": chunk.source_doc,
        "uri": chunk.uri,
        "label": chunk.label,
    }


class GraphTraversalTool:
    """openCypher traversal of the property graph via the toolkit ``GraphStore``.

    The four uniform interface fields are static, graphrag-free data set at
    construction, so the Tool is registrable and describable to the planner
    without any toolkit access. The toolkit ``GraphStore`` is built lazily on first
    use and cached for the tool's lifetime.
    """

    def __init__(self, graph_store_uri: str, *, query_timeout_s: float = _GRAPH_QUERY_TIMEOUT_S) -> None:
        """Build the Tool over a graphrag-toolkit graph-store URI.

        Args:
            graph_store_uri: The same ``neptune-db://<host>:8182`` (or
                ``neptune-graph://g-…``) URI the strategy tools/kg-build use. The
                toolkit ``GraphStore`` is constructed from it lazily inside the
                first invocation (Req 12.3).
            query_timeout_s: Wall-clock cap applied around each graph query.
        """
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._graph_store_uri = graph_store_uri
        self._query_timeout_s = query_timeout_s
        self._store: Any = None

    # ── lazy toolkit wiring (Req 12.3) ─────────────────────────────────

    def _get_store(self) -> Any:
        """Build (once) and return the toolkit ``GraphStore``; lazy toolkit import."""
        if self._store is None:
            from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory  # lazy

            self._store = GraphStoreFactory.for_graph_store(self._graph_store_uri)
            logger.info("agentic_graph_store_built", uri=self._graph_store_uri)
        return self._store

    @staticmethod
    def _entity_label(namespace: str) -> str:
        """Tenant-scoped, backtick-wrapped ``__Entity__`` label for ``namespace``."""
        return GraphTraversalTool._format_label(namespace, "__Entity__")

    @staticmethod
    def _statement_label(namespace: str) -> str:
        """Tenant-scoped, backtick-wrapped ``__Statement__`` label for ``namespace``."""
        return GraphTraversalTool._format_label(namespace, "__Statement__")

    @staticmethod
    def _format_label(namespace: str, label: str) -> str:
        """Format ``label`` for the namespace's graphrag tenant (authoritative).

        Derives the tenant exactly as kg-build/`BaselineLexicalRetriever` do
        (``to_graphrag_tenant_id``) and uses the toolkit ``TenantId.format_label``
        so the label string matches what is stored in the graph. Both imports are
        lazy (the toolkit one for Req 12.3; ``to_graphrag_tenant_id`` is toolkit-free
        but kept local for symmetry).
        """
        from coa_common.constants import to_graphrag_tenant_id
        from graphrag_toolkit.lexical_graph import TenantId  # lazy

        return TenantId(to_graphrag_tenant_id(namespace)).format_label(label)

    async def _run_cypher(self, cypher: str, params: dict) -> list[dict]:
        """Execute openCypher on the toolkit store under the query timeout.

        The toolkit call is synchronous; it runs on a bounded worker pool and is
        awaited under :attr:`_query_timeout_s`. ``asyncio.shield`` ensures a
        timeout abandons (rather than cancels) the in-flight worker, so the pool
        is never left with a dangling cancellation.
        """
        store = self._get_store()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(_EXECUTOR, lambda: store.execute_query_with_retry(cypher, params))
        rows = await asyncio.wait_for(asyncio.shield(fut), timeout=self._query_timeout_s)
        return list(rows or [])

    @staticmethod
    def _validated_limit(mode: str, limit: object) -> tuple[int | None, ToolResult | None]:
        """Validate one caller-supplied row limit before formatting query text."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            return None, ToolResult(detail=f"{TOOL_NAME}[{mode}]: limit must be an integer")
        if not (MIN_TRAVERSAL_LIMIT <= limit <= MAX_TRAVERSAL_LIMIT):
            return None, ToolResult(
                detail=(
                    f"{TOOL_NAME}[{mode}]: limit {limit} out of bounds [{MIN_TRAVERSAL_LIMIT}, {MAX_TRAVERSAL_LIMIT}]"
                )
            )
        return limit, None

    # ── dispatch ───────────────────────────────────────────────────────

    async def invoke(self, *, namespace: str, mode: str = "keyword", **kwargs: Any) -> ToolResult:
        """Run a traversal scoped to ``namespace`` and return a :class:`ToolResult`.

        Dispatches on ``mode`` (``keyword`` / ``uris`` / ``edge_typed``). Never
        raises for an expected failure: an unknown mode, invalid parameters, an
        out-of-bounds entity/edge count, an unconfigured store, a backend error,
        or a timeout all return a degraded :class:`ToolResult` whose ``detail``
        records the reason (Req 9.1-9.2).
        """
        try:
            if mode == "keyword":
                return await self._invoke_keyword(namespace, **kwargs)
            if mode == "uris":
                return await self._invoke_uris(namespace, **kwargs)
            if mode == "edge_typed":
                return await self._invoke_edge_typed(namespace, **kwargs)
            if mode == "edge_types":
                return await self._invoke_edge_types(namespace, **kwargs)
            return ToolResult(detail=f"{self.name}: unknown mode {mode!r}")
        except TimeoutError:
            logger.warning("graph_tool_timeout", tool=self.name, mode=mode)
            return ToolResult(
                detail=f"{self.name} timeout in mode {mode!r}",
                degraded_reason="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "graph_tool_invoke_error",
                tool=self.name,
                mode=mode,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ToolResult(
                detail=f"{self.name} error: {type(exc).__name__}",
                degraded_reason="error",
            )

    # ── keyword mode ───────────────────────────────────────────────────

    async def _invoke_keyword(
        self, namespace: str, *, query: str = "", limit: int = DEFAULT_EDGE_TYPED_LIMIT, **_: Any
    ) -> ToolResult:
        validated_limit, error = self._validated_limit("keyword", limit)
        if error is not None:
            return error
        assert validated_limit is not None
        # Build one bounded raw plan, then apply the property graph's persisted
        # ``search_str`` contract at this sink only. RDF consumers intentionally
        # keep their LCASE-compatible spelling instead.
        from ....query_utils import build_query_search_plan

        search_plan = build_query_search_plan(query or "", max_count=5)
        terms = list(
            dict.fromkeys(
                normalized
                for candidate in search_plan.terms
                if (normalized := normalize_graphrag_search_text(candidate))
            )
        )
        containers = list(
            dict.fromkeys(
                normalized
                for candidate in search_plan.containers
                if (normalized := normalize_graphrag_search_text(candidate))
            )
        )
        probes = list(dict.fromkeys(terms + containers))
        if not probes:
            return ToolResult(detail=f"{self.name}[keyword]: no usable terms in query")
        template = KEYWORD_ENTITY_SEARCH_WITH_CONTAINERS if containers else KEYWORD_ENTITY_SEARCH
        cypher = template.format(entity_label=self._entity_label(namespace), limit=validated_limit)
        rows = await self._run_cypher(cypher, {"probes": probes, "terms": terms, "containers": containers})
        entities = [
            GraphEntity(uri=row.get("uri", ""), label=row.get("label", ""), type=row.get("class", ""))
            for row in rows
            if row.get("uri")
        ]
        return self._entities_result(entities, f"{self.name}[keyword]: {len(entities)} entities")

    # ── uris mode ──────────────────────────────────────────────────────

    async def _invoke_uris(
        self, namespace: str, *, uris: list[str] | None = None, limit: int = DEFAULT_EDGE_TYPED_LIMIT, **_: Any
    ) -> ToolResult:
        validated_limit, error = self._validated_limit("uris", limit)
        if error is not None:
            return error
        assert validated_limit is not None
        entity_ids = [u for u in (uris or []) if u]
        if not (MIN_ENTITIES <= len(entity_ids) <= MAX_ENTITIES):
            return ToolResult(
                detail=(
                    f"{self.name}[uris]: entity count {len(entity_ids)} out of bounds [{MIN_ENTITIES}, {MAX_ENTITIES}]"
                )
            )
        cypher = URI_EXPANSION.format(entity_label=self._entity_label(namespace), limit=validated_limit)
        rows = await self._run_cypher(cypher, {"entity_ids": entity_ids})
        entities = self._connected_entities(rows, namespace)
        return self._entities_result(entities, f"{self.name}[uris]: {len(entities)} connected entities")

    # ── edge_types mode (predicate discovery for the soft ontology prior) ──

    async def _invoke_edge_types(
        self, namespace: str, *, entity_ids: list[str] | None = None, limit: int = 50, **_: Any
    ) -> ToolResult:
        """Discover the edge-type predicates actually present on the given entities.

        Runs :data:`ENTITY_EDGE_TYPES` and returns the raw ``r.value`` predicates
        (with occurrence counts) as ``edge_type`` items so the planner can propose
        real graph predicates that the pruned ontology omits (the soft-prior half of
        idea 5). No chunks/entities are gathered — this is a metadata probe — so it
        does not itself count as retrieval progress; the discovered predicates are
        surfaced to the planner via the result ``detail`` and cached on the context.
        """
        from ..templates import ENTITY_EDGE_TYPES

        validated_limit, error = self._validated_limit("edge_types", limit)
        if error is not None:
            return error
        assert validated_limit is not None
        ids = [e for e in (entity_ids or []) if e]
        if not (MIN_ENTITIES <= len(ids) <= MAX_ENTITIES):
            return ToolResult(
                detail=(
                    f"{self.name}[edge_types]: entity count {len(ids)} out of bounds [{MIN_ENTITIES}, {MAX_ENTITIES}]"
                )
            )
        cypher = ENTITY_EDGE_TYPES.format(entity_label=self._entity_label(namespace), limit=validated_limit)
        rows = await self._run_cypher(cypher, {"entity_ids": ids})
        items = tuple(
            {"type": "edge_type", "edge": row.get("edge", ""), "freq": row.get("freq", 0)}
            for row in rows
            if row.get("edge")
        )
        labels = ", ".join(f"{i['edge']}({i['freq']})" for i in items[:20]) or "(none)"
        return ToolResult(items=items, detail=f"{self.name}[edge_types]: {len(items)} present predicates: {labels}")

    # ── edge_typed mode (prepared Query_Template, Req 5.5) ─────────────

    async def _invoke_edge_typed(
        self,
        namespace: str,
        *,
        entity_ids: list[str] | None = None,
        edge_types: list[str] | None = None,
        limit: int = DEFAULT_EDGE_TYPED_LIMIT,
        **_: Any,
    ) -> ToolResult:
        """Fill and execute the edge-typed traversal template (Req 5.5).

        Validates the entity count (1-100) and edge-type count (1-50) before
        building the query, then runs the openCypher template with entity ids and
        normalized edge types passed as query parameters. Returns the connected
        entities plus, where present, their supporting statement text as chunks.
        """
        entity_ids = [e for e in (entity_ids or []) if e]
        edge_types = [e for e in (edge_types or []) if e]
        validated_limit, error = self._validated_limit("edge_typed", limit)
        if error is not None:
            return error
        assert validated_limit is not None

        if not (MIN_ENTITIES <= len(entity_ids) <= MAX_ENTITIES):
            return ToolResult(
                detail=(
                    f"{self.name}[edge_typed]: entity count {len(entity_ids)} out of "
                    f"bounds [{MIN_ENTITIES}, {MAX_ENTITIES}]"
                )
            )
        if not (MIN_EDGE_TYPES <= len(edge_types) <= MAX_EDGE_TYPES):
            return ToolResult(
                detail=(
                    f"{self.name}[edge_typed]: edge-type count {len(edge_types)} out of "
                    f"bounds [{MIN_EDGE_TYPES}, {MAX_EDGE_TYPES}]"
                )
            )

        # Normalize requested edge types to match stored r.value (Req 3.9 mapping).
        edge_types_norm = sorted({normalize_edge_type(e) for e in edge_types})

        cypher = EDGE_TYPED_TRAVERSAL.format(
            entity_label=self._entity_label(namespace),
            limit=validated_limit,
        )
        relationship_rows = await self._run_cypher(
            cypher,
            {"entity_ids": entity_ids, "edge_types_norm": edge_types_norm},
        )
        connected_ids = list(
            dict.fromkeys(
                connected
                for row in relationship_rows
                if isinstance(row, dict) and isinstance((connected := row.get("connectedUri")), str) and connected
            )
        )

        text_rows: list[dict] = []
        text_degraded_reason = ""
        if connected_ids:
            text_cypher = CONNECTED_TEXT_RETRIEVAL.format(
                entity_label=self._entity_label(namespace),
                statement_label=self._statement_label(namespace),
                limit=CONNECTED_TEXT_QUERY_LIMIT,
            )
            try:
                text_rows = await self._run_cypher(text_cypher, {"connected_ids": connected_ids})
            except TimeoutError:
                logger.warning(
                    "graph_tool_connected_text_timeout",
                    tool=self.name,
                    namespace=namespace,
                    connected_count=len(connected_ids),
                )
                text_degraded_reason = "connected_text_retrieval_timeout"
            except Exception as exc:  # noqa: BLE001 - preserve valid relationship evidence
                logger.warning(
                    "graph_tool_connected_text_error",
                    tool=self.name,
                    namespace=namespace,
                    connected_count=len(connected_ids),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                text_degraded_reason = "connected_text_retrieval_error"

        return self._parse_edge_typed(
            relationship_rows,
            text_rows,
            namespace,
            text_degraded_reason=text_degraded_reason,
        )

    # ── result assembly ────────────────────────────────────────────────

    @staticmethod
    def _connected_entities(
        rows: list[dict],
        namespace: str = "",
        statements: dict[str, list[str]] | None = None,
    ) -> list[GraphEntity]:
        """Group explicitly directed relationship rows by connected entity.

        A row whose ``predicateUri`` is null (the shape the real property graph
        always returns, see :func:`synthetic_predicate_uri`) gets a stable
        tenant-scoped synthetic predicate IRI minted from its ``predicateLabel``, so
        the edge survives to the wire instead of being silently discarded
        downstream. The human-readable label is still carried separately and is
        never itself promoted to the identifier slot.
        """
        by_connected: dict[str, GraphEntity] = {}
        rels: dict[str, list[dict]] = {}
        bounded_statements = statements or {}
        for row in rows:
            connected = row.get("connectedUri", "")
            if not connected:
                continue
            if connected not in by_connected:
                by_connected[connected] = GraphEntity(
                    uri=connected,
                    label=row.get("connectedLabel", "") or "",
                    type=row.get("connectedClass", "") or "",
                )
                rels[connected] = []

            source_uri = row.get("sourceUri", "")
            target_uri = row.get("targetUri", "")
            predicate_uri = row.get("predicateUri", "")
            predicate_label = row.get("predicateLabel", "")
            if not predicate_uri and predicate_label:
                predicate_uri = synthetic_predicate_uri(namespace, predicate_label)
            if source_uri and target_uri and (predicate_uri or predicate_label):
                relationship = {
                    "source_uri": source_uri,
                    "source_label": row.get("sourceLabel", "") or "",
                    "target_uri": target_uri,
                    "target_label": row.get("targetLabel", "") or "",
                }
                if predicate_uri:
                    relationship["predicate_uri"] = predicate_uri
                if predicate_label:
                    relationship["predicate_label"] = predicate_label
                rels[connected].append(relationship)

        return [
            GraphEntity(
                uri=e.uri,
                label=e.label,
                type=e.type,
                relationships=tuple(rels[e.uri]),
                properties=(
                    {"supportingStatements": encode_supporting_statements(bounded_statements.get(e.uri, []))}
                    if bounded_statements.get(e.uri)
                    else {}
                ),
            )
            for e in by_connected.values()
        ]

    def _entities_result(self, entities: list[GraphEntity], detail: str) -> ToolResult:
        items = tuple(_entity_item(e) for e in entities)
        directions = tuple(e.label for e in entities if e.label)
        return ToolResult(entities=tuple(entities), items=items, detail=detail, produced_directions=directions)

    @staticmethod
    def _bounded_connected_texts(text_rows: list[dict]) -> tuple[dict[str, list[str]], bool]:
        """Group graph statements within query, count, character, and byte bounds."""
        query_truncated = len(text_rows) > MAX_CONNECTED_TEXT_ROWS
        accepted_rows = text_rows[:MAX_CONNECTED_TEXT_ROWS]
        grouped: dict[str, list[str]] = {}
        seen: dict[str, set[str]] = {}
        for row in accepted_rows:
            connected = row.get("connectedUri")
            text = row.get("connectedText")
            if not isinstance(connected, str) or not connected or not isinstance(text, str) or not text.strip():
                continue
            connected_seen = seen.setdefault(connected, set())
            if text in connected_seen:
                continue
            connected_seen.add(text)
            grouped.setdefault(connected, []).append(text)

        uris = list(grouped)
        fair_groups = fair_bound_statement_groups([grouped[uri] for uri in uris])
        bounded = {uri: statements for uri, statements in zip(uris, fair_groups, strict=True) if statements}
        budget_truncated = False
        for uri, input_statements in grouped.items():
            output_statements = bounded.get(uri, [])
            if len(output_statements) != len(input_statements):
                budget_truncated = True
                break
            if any(before != after for before, after in zip(input_statements, output_statements, strict=True)):
                budget_truncated = True
                break
            if len(output_statements) > MAX_SUPPORTING_STATEMENTS_PER_ENTITY:
                budget_truncated = True
                break
            if bound_supporting_statements(input_statements) != output_statements:
                budget_truncated = True
                break

        return bounded, query_truncated or budget_truncated

    def _parse_edge_typed(
        self,
        relationship_rows: list[dict],
        text_rows: list[dict],
        namespace: str = "",
        *,
        text_degraded_reason: str = "",
    ) -> ToolResult:
        """Parse edge-typed rows into connected entities + their statement text.

        Relationship rows bind the actual directed ``sourceUri``/
        ``predicateUri``/``targetUri`` triple and connected entity. Text rows are
        independently query-bounded, then bounded again by the shared graph
        statement count/character/byte contract before any chunks enter the
        accumulated context or synthesis prompt.
        """
        statements, text_truncated = self._bounded_connected_texts(text_rows)
        entities = self._connected_entities(relationship_rows, namespace, statements)

        chunks: list[ChunkResult] = []
        labels = {entity.uri: entity.label for entity in entities}
        for connected, connected_statements in statements.items():
            label = labels.get(connected, "")
            for text in connected_statements:
                chunks.append(
                    ChunkResult(
                        chunk_id=f"{connected}#stmt-{sha1(text.encode('utf-8')).hexdigest()[:12]}",
                        text=text,
                        source_doc="",
                        relevance_score=1.0,
                        label=label,
                        uri=connected,
                    )
                )

        items = tuple(_chunk_item(c) for c in chunks) + tuple(_entity_item(e) for e in entities)
        directions = tuple(e.label for e in entities if e.label)
        detail = f"{self.name}[edge_typed]: {len(entities)} connected nodes, {len(chunks)} bounded statements"
        if text_degraded_reason:
            detail += f"; supporting text unavailable ({text_degraded_reason})"
        elif text_truncated:
            detail += (
                "; supporting statements truncated to "
                f"{MAX_CONNECTED_TEXT_ROWS} rows / {MAX_SUPPORTING_STATEMENTS_CHARS} chars / "
                f"{MAX_SUPPORTING_STATEMENTS_BYTES} UTF-8 bytes"
            )
        return ToolResult(
            chunks=tuple(chunks),
            entities=tuple(entities),
            items=items,
            detail=detail,
            degraded_reason=text_degraded_reason or ("connected_text_cap_reached" if text_truncated else ""),
            produced_directions=directions,
        )


def build_graph_tool(registry: Any, graph_store_uri: str) -> str:
    """Register the single :class:`GraphTraversalTool` into ``registry``.

    Mirrors :func:`build_strategy_tools`: a thin builder the factory (task 17)
    calls when the Neptune graph endpoint is configured. Returns the registered
    Tool name.

    Args:
        registry: The :class:`ToolRegistry` to register the tool into.
        graph_store_uri: The graphrag-toolkit graph-store URI the tool queries.

    Returns:
        The registered Tool name (``"graph_traversal"``).
    """
    tool = GraphTraversalTool(graph_store_uri)
    registry.register(tool)
    logger.info("graph_tool_registered", tool_name=tool.name)
    return tool.name
