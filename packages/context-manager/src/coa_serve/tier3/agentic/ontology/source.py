# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology source abstraction for the Agentic Retriever.

The determined ontology (node types, edge types, and each edge type's source to
target node-type mapping) resolves from either of two backends implementing
:class:`OntologySource`. Both run the SAME SPARQL bodies and the shared
:func:`_build_ontology` row-shaping, so they return the same :class:`Ontology`:

- :class:`OntologyGraphSource` queries the namespace's RDF ontology graph(s)
  through the serve-layer :class:`GraphClient`.
- :class:`OntologyFileSource` parses a ``.ttl`` file with rdflib and runs the same
  SPARQL against the in-memory default graph.

Import discipline: ``rdflib`` is imported lazily inside
:meth:`OntologyFileSource._load_sync`, and nothing here imports ``graphrag_toolkit``.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from textwrap import indent
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import quote

import structlog
from coa_common.constants import GOVERNED_METRICS_ONTOLOGY_ID

from ....namespace_language import current_label_preference, row_label
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
_GOVERNED_GRAPH = quote(GOVERNED_METRICS_ONTOLOGY_ID, safe="")
"""The last segment of a namespace's governed-metrics graph URI."""

# Inner fast-fail cap on the owl:Ontology graph-IRI resolve (seconds), so a hung
# resolve returns ``None`` and leaves budget for the prefix-filter fallback queries
# instead of burning the whole outer ``_ontology_lookup_timeout_s``.
_GRAPH_IRI_RESOLVE_TIMEOUT_S = 2.0


def _ontology_lookup_timeout_s() -> float:
    """Fast-fail budget for a single graph-backed ontology lookup (seconds).

    The graph client already retries a hung SPARQL post, which can stack to ~16s
    per lookup; this caps the lookup end-to-end so a slow Neptune degrades the
    ontology to empty instead of starving the shared budget. Env-tunable; defaults
    to 6s and is floored at 1s so it can never be disabled.
    """
    try:
        return max(1.0, float(os.environ.get("DEEP_REASONING_ONTOLOGY_TIMEOUT_S", "6")))
    except (TypeError, ValueError):
        return 6.0


@dataclass(frozen=True)
class Ontology:
    """The determined ontology for a Namespace in one source-agnostic shape.

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
            ``(edge_type, source_node_type, target_node_type)`` triples,
            de-duplicated and sorted. ``source_node_type`` is the
            edge's domain class label; ``target_node_type`` is its range class
            label.
    """

    node_types: tuple[str, ...]
    edge_types: tuple[str, ...]
    edge_map: tuple[tuple[str, str, str], ...]


@runtime_checkable
class OntologySource(Protocol):
    """Backend that resolves an :class:`Ontology` for a Namespace.

    The configured source is selected per deployment. ``load`` returns
    the ontology in the uniform :class:`Ontology` shape; on a backend failure it
    raises, leaving the Ontology_Lookup_Tool to record the failure and the source
    in use.
    """

    async def load(self, namespace: str) -> Ontology:
        """Resolve and return the :class:`Ontology` for ``namespace``."""
        ...


# Shared SPARQL, run by BOTH sources.

_ONTOLOGY_PREFIXES = (
    "PREFIX owl: <http://www.w3.org/2002/07/owl#>\nPREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>"
)

# The determined node types — every ``owl:Class`` that carries a label. The class
# IRI and each label's tag are projected so a class with several labels becomes
# ONE node type, named by the namespace-language preference.
_NODE_TYPES_SELECT = "SELECT DISTINCT ?classUri ?classLabel (LANG(?classLabel) AS ?classLabelLang)"
_NODE_TYPES_BODY = "?classUri a owl:Class .\n?classUri rdfs:label ?classLabel ."
# The variables the node-type query binds, in projection order.
_NODE_TYPES_VARS: tuple[str, ...] = ("classUri", "classLabel", "classLabelLang")

# The determined edge types and their directional domain→range mapping. Domain
# and range are OPTIONAL so an edge type with no declared source/target class
# still surfaces in ``edge_types`` (it simply has no ``edge_map`` entry).
_EDGE_TYPES_SELECT = (
    "SELECT DISTINCT ?propUri ?propLabel (LANG(?propLabel) AS ?propLabelLang)"
    " ?domain ?domainLabel (LANG(?domainLabel) AS ?domainLabelLang)"
    " ?range ?rangeLabel (LANG(?rangeLabel) AS ?rangeLabelLang)"
)
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
_EDGE_TYPES_VARS: tuple[str, ...] = (
    "propUri",
    "propLabel",
    "propLabelLang",
    "domain",
    "domainLabel",
    "domainLabelLang",
    "range",
    "rangeLabel",
    "rangeLabelLang",
)


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
    many-namespace cluster.
    """
    from ....query_utils import graph_scoped_body

    # Governed metrics are owl:Class subjects labelled with their names; a node-type list has no use for them, and
    # this result is cached for every caller, so the governed-metrics graph is always left out.
    scoped = graph_scoped_body(indent(body, " " * 8), graph_uri_prefix, graph_iris, without_graph=_GOVERNED_GRAPH)
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

    ``node_rows`` bind ``classUri`` / ``classLabel``; ``edge_rows`` bind
    ``propUri`` / ``propLabel`` and optionally ``domain`` / ``domainLabel`` and
    ``range`` / ``rangeLabel``, each label with its ``…Lang`` tag. Rows are grouped
    by IRI and each resource is named once with the request's namespace-language
    preference, so a class carrying ``"設備"@ja`` and an untagged ``"Equipment"`` is
    one node type rather than two. A row without an IRI (older fixtures) is keyed
    by its label. An edge row contributes an ``edge_map`` triple only when BOTH a
    domain and a range label are present so every triple is fully directional.
    Shared verbatim by both sources.
    """
    preference = current_label_preference()

    def key(row: dict[str, Any], uri_var: str, label_var: str) -> str:
        return str(row.get(uri_var) or f"label:{row.get(label_var, '')}")

    class_labels: list[tuple[str, str, str]] = []
    for row in node_rows:
        if row.get("classLabel"):
            class_labels.append((key(row, "classUri", "classLabel"), *row_label(row, "classLabel")))
    node_types = {label for label in preference.pick_by_resource(class_labels).values() if label}

    prop_labels: list[tuple[str, str, str]] = []
    end_labels: list[tuple[str, str, str]] = []
    triples: set[tuple[str, str, str]] = set()
    for row in edge_rows:
        if not row.get("propLabel"):
            continue
        prop = key(row, "propUri", "propLabel")
        prop_labels.append((prop, *row_label(row, "propLabel")))
        domain = key(row, "domain", "domainLabel") if row.get("domainLabel") else ""
        rng = key(row, "range", "rangeLabel") if row.get("rangeLabel") else ""
        if domain:
            end_labels.append((domain, *row_label(row, "domainLabel")))
        if rng:
            end_labels.append((rng, *row_label(row, "rangeLabel")))
        if domain and rng:
            triples.add((prop, domain, rng))
    edge_label_of = preference.pick_by_resource(prop_labels)
    end_label_of = preference.pick_by_resource(end_labels)
    edge_types = {label for label in edge_label_of.values() if label}
    edge_map = {
        (edge_label_of[prop], end_label_of[domain], end_label_of[rng])
        for prop, domain, rng in triples
        if edge_label_of.get(prop) and end_label_of.get(domain) and end_label_of.get(rng)
    }

    return Ontology(
        node_types=tuple(sorted(node_types)),
        edge_types=tuple(sorted(edge_types)),
        edge_map=tuple(sorted(edge_map)),
    )


class OntologyGraphSource:
    """Live SPARQL/RDF backend for the Ontology_Lookup_Tool.

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
        genuinely empty ontology. Any other backend
        failure also propagates (the GraphClient ``query`` contract raises), leaving
        failure handling and trace recording to the Ontology_Lookup_Tool.
        """
        from ....query_utils import namespace_graph_prefix

        if not self._graph_uri_template:
            return _build_ontology([], [])
        # Validate before interpolating the namespace into the SPARQL prefix.
        validate_namespace(namespace)
        prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
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

        One coroutine so the caller's fast-fail timeout bounds the WHOLE sequence
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
        cluster-wide prefix filter (slow but correct). Bounded by
        ``_GRAPH_IRI_RESOLVE_TIMEOUT_S``; ``graph_scoped_body`` drops any unsafe IRI
        itself. Never raises: resolution is an optimisation, not a correctness
        dependency.
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
    """Resolve the ontology from a serialized ``.ttl`` file.

    Runs the shared SPARQL against the default graph of an in-memory rdflib graph.
    The ``namespace`` argument to :meth:`load` is accepted for interface parity
    with :class:`OntologyGraphSource` but not used: a file is a single ontology.

    rdflib builds a process-global pyparsing grammar lazily on first
    ``Graph.query``, and that construction is NOT thread-safe (concurrent
    first-use raises spurious ``TypeError: Param.postParse2() …`` errors). So the
    parse/query runs under a module-level lock, at most once per ``ttl_path``, and
    the query rows are cached.
    """

    # Query rows keyed by ttl_path; the lock guards rdflib and the cache.
    _cache: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
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

        Runs off the event loop. Raises on a missing or unparseable file.
        """
        return await asyncio.to_thread(self._load_locked)

    def _load_locked(self) -> Ontology:
        """Return the :class:`Ontology` for the path, running rdflib at most once.

        Double-checked against the cache: a fast pre-lock read avoids contending
        once the path is resolved, and the in-lock re-check ensures only the first
        of a racing cold batch performs the (non-thread-safe) rdflib parse/query
        while the rest reuse its result. The cache holds the query ROWS rather than
        the shaped ontology: label choice follows the calling request's namespace
        language, so shaping (cheap, no rdflib) runs on every load.
        """
        cached = self._cache.get(self._ttl_path)
        if cached is None:
            with self._lock:
                cached = self._cache.get(self._ttl_path)
                if cached is None:
                    cached = self._load_sync()
                    self._cache[self._ttl_path] = cached
        return _build_ontology(*cached)

    def _load_sync(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Parse the ``.ttl`` and run the shared, un-graphed SPARQL via rdflib.

        Runs the SAME node/edge SELECT bodies the graph source uses — without the
        ``GRAPH`` wrapper, since the parsed file lands in rdflib's default graph —
        through rdflib's in-memory SPARQL engine and returns the shaped rows for
        the shared :func:`_build_ontology`. No hand-walking of triples.
        """
        from rdflib import Graph  # lazy import

        graph = Graph()
        graph.parse(self._ttl_path, format="turtle")

        node_result = graph.query(_default_graph_query(_NODE_TYPES_SELECT, _NODE_TYPES_BODY))
        edge_result = graph.query(_default_graph_query(_EDGE_TYPES_SELECT, _EDGE_TYPES_BODY))

        return _result_rows(node_result, _NODE_TYPES_VARS), _result_rows(edge_result, _EDGE_TYPES_VARS)
