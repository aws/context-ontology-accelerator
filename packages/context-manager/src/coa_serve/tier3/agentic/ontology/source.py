# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology source abstraction for the Agentic Retriever (Req 3.1, 3.6).

The determined ontology a request needs — the complete set of node types, the
complete set of edge types, and, for each edge type, the directional mapping of
its source node type to its target node type (Req 3.1) — can be resolved from
either of two backends. Both implement the :class:`OntologySource` Protocol and
return the SAME :class:`Ontology` shape so the Ontology_Lookup_Tool (and the
reasoning agent that consumes it) is agnostic to which backend resolved it
(Req 3.6). Crucially, both backends run the SAME SPARQL — the shared SELECT
bodies / triple patterns and the shared :func:`_build_ontology` row-shaping
defined in this module — so the file source is a faithful stand-in for the graph
source, not a parallel reimplementation.

- :class:`OntologyFileSource` — the CURRENT in-scope path the agentic retriever
  ships on. It is a deliberate STOPGAP: it materializes the WHOLE ontology by
  parsing a serialized ``.ttl`` Turtle file with rdflib into an in-memory graph
  and running the shared SPARQL against it (no ``GRAPH`` wrapper, since a parsed
  ``.ttl`` lands in the default graph). This is valid precisely while the
  ontology is small enough to copy wholesale alongside the request. Because it
  executes the same SPARQL as the graph source via rdflib's in-memory engine, it
  produces a byte-for-byte identical :class:`Ontology`.
- :class:`OntologyGraphSource` — the live SPARQL/RDF backend. It queries the RDF
  **ontology graph(s)** for the namespace through the existing serve-layer
  :class:`GraphClient` (the same client ``GraphTraverser`` uses), wrapping the
  shared SPARQL bodies in a ``GRAPH ?g { ... }`` block and filtering
  ``STRSTARTS(STR(?g), <prefix>)`` — where the prefix comes from
  ``get_graph_uri_template()`` (default ``urn:coa:{namespace}:published``), the
  same helper ``GraphTraverser`` uses — so it unions every per-ontology named
  graph in the namespace. Its live use is DEFERRED to follow-up task 21, which
  will issue targeted, query-shaped lookups against the ontology graph instead of
  copying it wholesale; the shape it returns is already identical to the file
  source's so that switch is a drop-in. This is the SPARQL/RDF side — a different
  dataset and query language from the graphrag-toolkit property graph the
  strategy/traversal tools use (see the design's "Two graphs").

Import discipline (Requirement 12.3): this module imports only the dependency-
free serve-layer helpers, ``structlog``, ``asyncio``, and ``textwrap`` at load.
``rdflib`` is imported lazily inside :meth:`OntologyFileSource._load_sync`, and
nothing here imports ``graphrag_toolkit``.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from textwrap import indent
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from ....query_utils import validate_namespace

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from ....clients.base import GraphClient

logger = structlog.get_logger(__name__)

# Backstop LIMIT on the cheap owl:Ontology graph-IRI resolve. A namespace holds
# one graph per published ontology, so this is far above the real count; it only
# bounds a pathological store. graph_scoped_body applies its own (lower) inlining
# cap and falls back to the prefix filter above it, so this value never forces a
# slow query — it only caps how many IRIs we fetch.
_MAX_ONTOLOGY_GRAPHS = 50

# Inner fast-fail cap on JUST the owl:Ontology graph-IRI resolve (seconds). The
# resolve is a cheap optimisation that lets the type queries use a constant
# ``GRAPH <iri>`` instead of a cluster-wide ``STRSTARTS`` scan; it must never
# itself eat the whole lookup budget. Without this, a single hung resolve burns
# the full outer ``_ontology_lookup_timeout_s`` and the lookup fails with NOTHING,
# even though the prefix-filter fallback would have answered within budget. Giving
# the resolve its own short timeout (so on hang it returns ``None`` and the type
# queries run with the prefix filter) is exactly what Tier-2's ``graph_tools`` and
# ``tbox_context`` resolvers do. Kept well under the outer cap so the fallback
# queries still have budget left to run.
_GRAPH_IRI_RESOLVE_TIMEOUT_S = 2.0


def _ontology_lookup_timeout_s() -> float:
    """Fast-fail budget for a single graph-backed ontology lookup (seconds).

    The ontology lookup is ONE preparatory step inside the deep-reasoning loop,
    not the whole answer, so it must fail fast rather than burn a large slice of
    the shared time budget. The underlying graph client already retries a hung
    SPARQL post (NEPTUNE_TIMEOUT read x retryhttp attempts), which can stack to
    ~16s per lookup — long enough to swallow a full reasoning step. This caps the
    lookup end-to-end so a slow/hung Neptune degrades the ontology to empty (the
    soft-prior loop still runs) instead of starving the budget. Env-tunable;
    defaults to a tight 6s and is floored at 1s so it can never be disabled.
    """
    try:
        return max(1.0, float(os.environ.get("DEEP_REASONING_ONTOLOGY_TIMEOUT_S", "6")))
    except (TypeError, ValueError):
        return 6.0


@dataclass(frozen=True)
class Ontology:
    """The determined ontology for a Namespace in one source-agnostic shape (Req 3.1, 3.6).

    Every :class:`OntologySource` returns this exact shape regardless of backend,
    so the Ontology_Lookup_Tool and the reasoning agent treat the graph and file
    sources interchangeably.

    Attributes:
        node_types: The complete set of determined node-type labels (from the
            ontology's ``owl:Class`` declarations), de-duplicated and sorted.
        edge_types: The complete set of determined edge-type labels (from the
            ontology's ``owl:ObjectProperty`` declarations), de-duplicated and
            sorted. Every entry in ``edge_map`` references an edge type present
            here; an edge type with no resolvable domain/range still appears here
            but has no ``edge_map`` entry.
        edge_map: The directional source→target mapping per edge type as
            ``(edge_type, source_node_type, target_node_type)`` triples
            (Req 3.1), de-duplicated and sorted. ``source_node_type`` is the
            edge's domain class label; ``target_node_type`` is its range class
            label.
    """

    node_types: tuple[str, ...]
    edge_types: tuple[str, ...]
    edge_map: tuple[tuple[str, str, str], ...]


@runtime_checkable
class OntologySource(Protocol):
    """Backend that resolves an :class:`Ontology` for a Namespace (Req 3.2).

    The configured source is selected per deployment (Req 3.3). ``load`` returns
    the ontology in the uniform :class:`Ontology` shape; on a backend failure it
    raises, leaving the Ontology_Lookup_Tool to record the failure and the source
    in use (Req 3.11).
    """

    async def load(self, namespace: str) -> Ontology:
        """Resolve and return the :class:`Ontology` for ``namespace``."""
        ...


# ── Shared SPARQL, run by BOTH sources ─────────────────────────────
#
# The SELECT projections and the WHERE *bodies* (triple patterns) below are the
# single source of truth for the ontology queries. The only per-source difference
# is the execution target and how the body is wrapped:
#   - the NDB graph source wraps each body in ``GRAPH ?g { ... }`` and filters
#     ``STRSTARTS(STR(?g), <prefix>)`` (prefix from ``get_graph_uri_template()``,
#     default ``urn:coa:{ns}:published``) to union the namespace's per-ontology
#     graphs, then ships it to the serve-layer GraphClient;
#   - the file source runs each body unchanged against the default graph of an
#     in-memory rdflib graph parsed from a ``.ttl``.
# Both feed the resulting binding rows into the shared :func:`_build_ontology`.

_ONTOLOGY_PREFIXES = (
    "PREFIX owl: <http://www.w3.org/2002/07/owl#>\nPREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>"
)

# The determined node types — every ``owl:Class`` that carries a label.
_NODE_TYPES_SELECT = "SELECT DISTINCT ?classLabel"
_NODE_TYPES_BODY = "?classUri a owl:Class .\n?classUri rdfs:label ?classLabel ."
# The variables the node-type query binds, in projection order.
_NODE_TYPES_VARS: tuple[str, ...] = ("classLabel",)

# The determined edge types and their directional domain→range mapping. Domain
# and range are OPTIONAL so an edge type with no declared source/target class
# still surfaces in ``edge_types`` (it simply has no ``edge_map`` entry).
_EDGE_TYPES_SELECT = "SELECT DISTINCT ?propLabel ?domainLabel ?rangeLabel"
_EDGE_TYPES_BODY = (
    "?propUri a owl:ObjectProperty .\n"
    "?propUri rdfs:label ?propLabel .\n"
    "OPTIONAL {\n"
    "    ?propUri rdfs:domain ?domain .\n"
    "    ?domain rdfs:label ?domainLabel .\n"
    "}\n"
    "OPTIONAL {\n"
    "    ?propUri rdfs:range ?range .\n"
    "    ?range rdfs:label ?rangeLabel .\n"
    "}"
)
# The variables the edge-type query binds, in projection order.
_EDGE_TYPES_VARS: tuple[str, ...] = ("propLabel", "domainLabel", "rangeLabel")


def _graph_wrapped_query(select: str, body: str, graph_uri_prefix: str, graph_iris: list[str] | None = None) -> str:
    """Build the NDB query: run ``body`` over the namespace's per-ontology graphs.

    Induced ontologies are loaded into ONE named graph PER ontology under a
    namespace prefix (``{base}/{namespace}/{encoded-ontology-id}``), NOT a single
    ``:published`` graph. When the namespace's graph IRIs are resolved
    (``graph_iris``), the body is scoped to a constant ``GRAPH <iri>`` per graph —
    pushed into the index scan — via :func:`graph_scoped_body`. When they are not
    (resolution failed/empty, or more graphs than the inlining cap), it falls back
    to ``GRAPH ?g { ... } FILTER(STRSTARTS(STR(?g), <prefix>))``, which matches in
    EVERY graph on the cluster and filters afterwards — correct but cluster-wide,
    so its cost scales with the whole store, not the namespace. The scoped form is
    the same fix Tier-2's FK-edge query uses to stop pinning Neptune's CPU on a
    many-namespace cluster. (The earlier exact-match on a hardcoded
    ``urn:orion:{ns}:published`` graph matched nothing, so the ontology always
    resolved empty even after a successful accept.)
    """
    from ....query_utils import graph_scoped_body

    scoped = graph_scoped_body(indent(body, " " * 8), graph_uri_prefix, graph_iris)
    return f"{_ONTOLOGY_PREFIXES}\n\n{select}\nWHERE {{\n{scoped}\n}}\n"


def _default_graph_query(select: str, body: str) -> str:
    """Build the file query: run ``body`` against the default graph.

    Used by :class:`OntologyFileSource`: a single ``.ttl`` parsed with
    ``Graph().parse(...)`` lands in rdflib's default graph, so the shared patterns
    must run WITHOUT a ``GRAPH`` wrapper.
    """
    return f"{_ONTOLOGY_PREFIXES}\n\n{select}\nWHERE {{\n{indent(body, ' ' * 4)}\n}}\n"


def _result_rows(result: Iterable[Any], var_names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Shape rdflib ``Graph.query`` result rows into the dict rows ``_build_ontology`` consumes.

    Maps each ``rdflib`` solution into ``{var: str(value)}`` keyed by the SELECT
    variable names (``classLabel`` / ``propLabel`` / ``domainLabel`` /
    ``rangeLabel``), stringifying bound values and OMITTING unbound (``None``)
    OPTIONAL variables — exactly the dict shape the serve-layer GraphClient yields
    and that :func:`_build_ontology` expects, so the file source reuses the SAME
    shaping path as the graph source.
    """
    rows: list[dict[str, Any]] = []
    for row in result:
        shaped: dict[str, Any] = {}
        for var in var_names:
            value = row.get(var)
            if value is not None:
                shaped[var] = str(value)
        rows.append(shaped)
    return rows


def _build_ontology(node_rows: list[dict[str, Any]], edge_rows: list[dict[str, Any]]) -> Ontology:
    """Shape SPARQL binding rows into a sorted, de-duplicated :class:`Ontology`.

    ``node_rows`` bind ``classLabel``; ``edge_rows`` bind ``propLabel`` and
    optionally ``domainLabel`` / ``rangeLabel``. An edge row contributes an
    ``edge_map`` triple only when BOTH a domain and a range label are present so
    every triple is fully directional (Req 3.1). Shared verbatim by both sources.
    """
    node_types = {str(row["classLabel"]) for row in node_rows if row.get("classLabel")}

    edge_types: set[str] = set()
    edge_map: set[tuple[str, str, str]] = set()
    for row in edge_rows:
        edge = row.get("propLabel")
        if not edge:
            continue
        edge = str(edge)
        edge_types.add(edge)
        domain = row.get("domainLabel")
        rng = row.get("rangeLabel")
        if domain and rng:
            edge_map.add((edge, str(domain), str(rng)))

    return Ontology(
        node_types=tuple(sorted(node_types)),
        edge_types=tuple(sorted(edge_types)),
        edge_map=tuple(sorted(edge_map)),
    )


class OntologyGraphSource:
    """Live SPARQL/RDF backend for the Ontology_Lookup_Tool (Req 3.2, 3.4).

    Reuses the read-only :class:`GraphClient` already used by ``GraphTraverser``
    to run the shared ontology SPARQL over the namespace's per-ontology named
    graphs. Node types are the ``owl:Class`` labels; edge types are the
    ``owl:ObjectProperty`` labels; the directional ``edge_map`` is read from each
    property's ``rdfs:domain`` (source node type) and ``rdfs:range`` (target node
    type), resolved to their class labels.

    Graph scoping: accepted ontologies are loaded into ONE named graph PER ontology
    under a namespace prefix (``{base}/{namespace}/{encoded-ontology-id}``), so this
    matches ``GRAPH ?g`` filtered by that prefix — identical to how
    ``GraphTraverser`` scopes its property-graph queries — via the SAME shared
    ``get_graph_uri_template`` / ``namespace_graph_prefix`` helpers, so read scoping
    can never drift from the rest of serve or the ontology write side.
    """

    def __init__(self, graph_client: GraphClient, *, graph_uri_template: str | None = None) -> None:
        """Build the source over a serve-layer graph client.

        Args:
            graph_client: The read-only SPARQL :class:`GraphClient` (e.g.
                ``NeptuneGraphClient``) to issue the ontology queries through.
            graph_uri_template: The ``{namespace}``-placeholder template for the
                namespace graph prefix. Defaults to the shared serve template
                (``get_graph_uri_template()``), the same one ``GraphTraverser`` uses.
        """
        from ....query_utils import get_graph_uri_template

        self._graph = graph_client
        self._graph_uri_template = get_graph_uri_template(graph_uri_template)

    async def load(self, namespace: str) -> Ontology:
        """Resolve the ontology for ``namespace`` from its per-ontology RDF graphs.

        Issues the node-type and edge-type SPARQL queries (the shared bodies,
        prefix-filtered to the namespace's ontology graphs) concurrently and shapes
        their bindings into the uniform :class:`Ontology` via the shared
        :func:`_build_ontology`. When no graph-URI template is configured the
        namespace has no queryable ontology graph, so this returns an empty
        Ontology rather than issuing an unscoped query. A lookup that exceeds the
        fast-fail timeout (see :func:`_ontology_lookup_timeout_s`) RAISES
        ``TimeoutError`` — a timeout is a lookup FAILURE, kept distinct from a
        genuinely empty ontology (Req 3.10 empty vs 3.11 failed). Any other backend
        failure also propagates (the GraphClient ``query`` contract raises), leaving
        failure handling and trace recording to the Ontology_Lookup_Tool (Req 3.11).
        """
        from ....query_utils import namespace_graph_prefix

        if not self._graph_uri_template:
            return _build_ontology([], [])
        # Validate before interpolating the namespace into the SPARQL prefix.
        validate_namespace(namespace)
        prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        # Fast-fail: a single ontology lookup must not consume a large slice of the
        # shared reasoning budget if Neptune is slow/hung (the graph client's own
        # read-timeout x retry can stack to ~16s). Cap the lookup end-to-end. On
        # timeout we RAISE rather than return an empty Ontology: a timeout is a
        # lookup FAILURE, not a namespace that genuinely has no ontology, and the
        # two are distinct outcomes (Req 3.10 empty vs 3.11 failed). Returning empty
        # here would mislead the planner ("this namespace has no ontology") and hide
        # the timeout from a trace. OntologyLookupTool.invoke catches every exception
        # and records a "failed (source=...)" result WITHOUT re-raising, so the
        # fast-fail intent is preserved — the soft-prior traversal still runs — while
        # the outcome is labelled correctly.
        timeout_s = _ontology_lookup_timeout_s()
        try:
            node_rows, edge_rows = await asyncio.wait_for(
                self._load_scoped(prefix),
                timeout=timeout_s,
            )
        except TimeoutError:
            logger.warning("ontology_graph_lookup_timeout", namespace=namespace, timeout_s=timeout_s)
            raise
        return _build_ontology(node_rows, edge_rows)

    async def _load_scoped(self, prefix: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Resolve the namespace's graph IRIs, then run the scoped type queries.

        Resolve the named-graph IRIs first (a cheap owl:Ontology scan), then scope
        the type queries to them with a constant ``GRAPH <iri>`` per graph instead
        of a cluster-wide ``STRSTARTS`` prefix filter — the same fix Tier-2 uses to
        stop the ontology scan pinning Neptune CPU as the cluster grows. A resolve
        that fails/empties/exceeds the inlining cap degrades inside
        :func:`_graph_wrapped_query` to the prefix filter, so this never fails the
        lookup — it only makes the common (few-graph) case cheap. Kept as one
        coroutine so the caller's fast-fail timeout bounds the WHOLE sequence
        (resolve + both type queries), not just the type queries.
        """
        graph_iris = await self._resolve_graph_iris(prefix)
        return await asyncio.gather(
            self._graph.query(_graph_wrapped_query(_NODE_TYPES_SELECT, _NODE_TYPES_BODY, prefix, graph_iris)),
            self._graph.query(_graph_wrapped_query(_EDGE_TYPES_SELECT, _EDGE_TYPES_BODY, prefix, graph_iris)),
        )

    async def _resolve_graph_iris(self, prefix: str) -> list[str] | None:
        """Resolve the namespace's named-graph IRIs for constant-GRAPH scoping.

        Returns the resolved IRIs, or ``None`` when resolution fails, times out, or
        yields nothing — in which case :func:`_graph_wrapped_query` degrades to the
        cluster-wide prefix filter (slow but correct). The resolve gets its OWN
        short timeout (``_GRAPH_IRI_RESOLVE_TIMEOUT_S``, well under the outer
        lookup cap), so a hung resolve cannot eat the whole lookup budget and leave
        no time for the prefix-filter fallback — the same reason Tier-2's resolvers
        bound their resolve. ``graph_scoped_body`` drops any unsafe IRI itself, so
        no pre-filtering is needed here. Never raises: resolution is a cheap
        optimisation, not a correctness dependency.
        """
        from ....query_utils import named_graphs_sparql

        try:
            rows = await asyncio.wait_for(
                self._graph.query(named_graphs_sparql(prefix, limit=_MAX_ONTOLOGY_GRAPHS)),
                timeout=_GRAPH_IRI_RESOLVE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning("ontology_graph_iri_resolve_timeout", timeout_s=_GRAPH_IRI_RESOLVE_TIMEOUT_S)
            return None
        except Exception as exc:  # noqa: BLE001 - resolution is best-effort; fall back to the prefix filter
            logger.warning("ontology_graph_iri_resolve_failed", error=str(exc), error_type=type(exc).__name__)
            return None
        iris = [r["g"] for r in rows if r.get("g")]
        return iris or None


class OntologyFileSource:
    """Current shipping STOPGAP: resolve the ontology from a serialized ``.ttl`` (Req 3.5).

    This is the in-scope path the agentic retriever ships on today. It is a
    deliberate stopgap that materializes the WHOLE ontology by parsing a Turtle
    file with rdflib and running the SAME shared SPARQL the graph source runs —
    only against the default graph of the in-memory rdflib graph (no ``GRAPH``
    wrapper, since a parsed ``.ttl`` has no named graph) and via rdflib's
    in-memory SPARQL engine instead of Neptune. The resulting binding rows go
    through the SAME :func:`_build_ontology` shaping, so the :class:`Ontology` it
    returns is identical to the graph source's. This is valid while the ontology
    is small enough to copy wholesale; task 21 introduces the live graph path.

    The ``namespace`` argument to :meth:`load` is accepted for interface parity
    with :class:`OntologyGraphSource` but not used — a file represents a single
    ontology.

    Thread-safety + caching. rdflib's SPARQL parser builds a process-global
    pyparsing grammar lazily on first ``Graph.query``, and that construction is
    NOT thread-safe: concurrent first-use from multiple threads (e.g. the
    ``asyncio.to_thread`` workers behind concurrent requests) races on the shared
    grammar and raises spurious ``TypeError: Param.postParse2() …`` /
    ``<lambda>() missing … argument`` errors. Because a ``.ttl`` ontology is
    static, :meth:`load` parses + queries at most ONCE per ``ttl_path`` and caches
    the resulting :class:`Ontology`; the parse/query is guarded by a module-level
    lock so the first (cold) concurrent batch is serialized rather than racing,
    and every subsequent call returns the cached value without touching rdflib.
    """

    # Process-wide cache of resolved ontologies keyed by ttl_path, plus the lock
    # that guards both the rdflib parse/query (not thread-safe) and the cache. The
    # ontology file is immutable for the process lifetime, so a single resolve per
    # path is correct; the lock makes the first concurrent batch safe.
    _cache: dict[str, Ontology] = {}
    _lock = threading.Lock()

    def __init__(self, ttl_path: str) -> None:
        """Build the source over a path to a serialized Turtle ontology file.

        Args:
            ttl_path: Filesystem path to the ``.ttl`` ontology file. It is read
                lazily on :meth:`load`, so an unreadable/missing path surfaces as
                a load failure at invoke time rather than at construction.
        """
        self._ttl_path = ttl_path

    async def load(self, namespace: str) -> Ontology:
        """Parse the configured ``.ttl`` and run the shared SPARQL over it.

        ``rdflib`` is imported lazily inside :meth:`_load_sync` (Req 12.3
        discipline) and the parse + query — a fast, in-memory operation — runs off
        the event loop. The result is cached per ``ttl_path`` and the rdflib work
        is serialized by a module-level lock (see the class docstring) so that
        concurrent cold first-use cannot race rdflib's non-thread-safe SPARQL
        grammar construction. Raises on a missing/unparseable file, mirroring the
        graph source's raise-on-failure contract so the Ontology_Lookup_Tool
        records it uniformly (Req 3.11).
        """
        return await asyncio.to_thread(self._load_locked)

    def _load_locked(self) -> Ontology:
        """Return the cached :class:`Ontology` for the path, resolving it once under the lock.

        Double-checked against the cache: a fast pre-lock read avoids contending
        once the path is resolved, and the in-lock re-check ensures only the first
        of a racing cold batch performs the (non-thread-safe) rdflib parse/query
        while the rest reuse its result.
        """
        cached = self._cache.get(self._ttl_path)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(self._ttl_path)
            if cached is not None:
                return cached
            ontology = self._load_sync()
            self._cache[self._ttl_path] = ontology
            return ontology

    def _load_sync(self) -> Ontology:
        """Parse the ``.ttl`` and run the shared, un-graphed SPARQL via rdflib.

        Runs the SAME node/edge SELECT bodies the graph source uses — without the
        ``GRAPH`` wrapper, since the parsed file lands in rdflib's default graph —
        through rdflib's in-memory SPARQL engine, then feeds the rows into the
        shared :func:`_build_ontology`. No hand-walking of triples.
        """
        from rdflib import Graph  # lazy import (Req 12.3 discipline)

        graph = Graph()
        graph.parse(self._ttl_path, format="turtle")

        node_result = graph.query(_default_graph_query(_NODE_TYPES_SELECT, _NODE_TYPES_BODY))
        edge_result = graph.query(_default_graph_query(_EDGE_TYPES_SELECT, _EDGE_TYPES_BODY))

        node_rows = _result_rows(node_result, _NODE_TYPES_VARS)
        edge_rows = _result_rows(edge_result, _EDGE_TYPES_VARS)
        return _build_ontology(node_rows, edge_rows)
