# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology foreign-key traversal — the Tier-2 ``explore_graph`` capability.

Where :class:`~coa_serve.tier2.tools.TableCatalog` finds tables by *semantic*
similarity, this walks the ontology's **foreign-key structure**: from a seed table
it expands ``k`` hops outward over the FK object-properties the ingest pipeline
induced (``owl:ObjectProperty`` with ``rdfs:domain``/``rdfs:range`` pointing at
table classes) and, optionally, returns the columns (``owl:DatatypeProperty``) on
the reachable tables.

The point is join discovery: a table two FKs away that a single vector search
would never surface. This returns graph *facts* only and makes no table-selection
decision — the consumer does, which keeps the choice AI-driven.

Two entry points onto the same walk, for the two consumers:
:meth:`OntologyGraphTool.explore_graph` is the agent *tool* (seeded by a table
name the agent chose, called when the agent decides to), while
:meth:`OntologyGraphTool.expand_from` is seeded by the classes a single
retrieval already returned and fires unconditionally — the flat NL→SQL path has
no agent to call a tool.

Structure of the deployed graph (verified against the reingested Spider 2.0 graph):

  * table  → ``:Db_Table a owl:Class ; rdfs:label "table"``
  * column → ``:db_table_col a owl:DatatypeProperty ; rdfs:domain :Db_Table``
  * FK     → ``:db_table_col a owl:ObjectProperty ;
                  rdfs:domain :Db_Table ; rdfs:range :Other_Table ;
                  ns1:fkProvenance "AI_INFERRED" ;
                  rdfs:comment "Foreign key: table.col references other.col"``

Traversal is BFS in Python over an adjacency loaded with one bounded SPARQL query
(the FK set is small — a few hundred edges — and cached for the life of the
request-scoped tool), so hop count, node-type filtering, ordering and truncation
are exact and cheap; recursive SPARQL over reified domain/range property nodes
would be both awkward and unbounded.

Every query here is bound to the namespace's named graphs, which a cheap probe
resolves once per request (:meth:`OntologyGraphTool._resolve_graph_iris`). The
scoping matters for cost, not just correctness — see
:func:`~coa_serve.query_utils.graph_scoped_body`.

Like the other tools here this is transport-agnostic: :class:`OntologyGraphTool`
is a plain async capability, so the in-process agent and any future MCP tool or
HTTP route call the same code.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

import structlog

from ...clients.base import GraphClient
from ...query_utils import (
    SPARQL_PREFIXES,
    get_graph_uri_template,
    graph_scoped_body,
    named_graphs_sparql,
    namespace_graph_prefix,
    object_properties_sparql,
    validate_namespace,
)
from ..nl_to_sql.sql_generator import hit_class_iri
from .table_tools import TableCatalog

logger = structlog.get_logger(__name__)

# Same IRI-ref safety gate the Tier-3 traverser uses: permits the '#' fragment
# separator (class IRIs are "https://.../ontology#Db_Table") while excluding the
# characters dangerous inside a SPARQL <...> IRI ref.
_SAFE_URI_RE = re.compile(r"^https?://[^\s<>\"{}|\\^`;]+$|^urn:[a-zA-Z0-9][a-zA-Z0-9._:-]*$")

# Gate on a seed passed as a bare table name (the fallback when no IRI is known).
# DENY-list, not an allow-list: an earlier ``^[a-z0-9][a-z0-9_ .-]{0,127}$`` allowed
# only Latin, so a table labelled 会員 or 회원 failed validation and traversal
# reported "seed table not found" — a silent no-op on any non-Latin schema. What
# actually has to be excluded is the characters that could break out of a SPARQL
# string literal, plus control characters; everything else is a legitimate label.
_UNSAFE_LABEL_CHARS = frozenset('"\\<>{}')
_MAX_LABEL_CHARS = 128


def _is_safe_label(label: str) -> bool:
    """True when a bare table label is safe to interpolate into a SPARQL literal."""
    if not label or len(label) > _MAX_LABEL_CHARS:
        return False
    return not any(c in _UNSAFE_LABEL_CHARS or ord(c) < 0x20 or ord(c) == 0x7F for c in label)


# Bounds. hops is capped so a pathological k cannot fan out the whole graph;
# limit is capped so one call cannot return a wall of nodes into a prompt.
MAX_HOPS = 4
MAX_NODE_LIMIT = 200
DEFAULT_NODE_LIMIT = 50
_QUERY_TIMEOUT_S = 25.0
_EDGE_QUERY_LIMIT = 1000
# Resolving the namespace's named graphs gates every other query, so it gets a
# tighter budget: it matches one triple per graph and must be fast or useless.
_GRAPH_RESOLVE_TIMEOUT_S = 8.0
# A namespace holds one graph per published ontology; the cap is a backstop.
_MAX_GRAPHS = 25

VALID_NODE_TYPES = ("table", "column")

# Per-column description budget inside a walk-derived schema line (see
# :func:`_schema_text`). A whole walk can be appended to one prompt, so the
# descriptions are clipped rather than allowed to set the prompt's size.
_MAX_COL_DESC = 90


def _escape_literal(s: str) -> str:
    """Escape backslashes and double quotes for a SPARQL string literal."""
    return s.replace(chr(0x5C), chr(0x5C) + chr(0x5C)).replace(chr(0x22), chr(0x5C) + chr(0x22))


class OntologyGraphTool:
    """FK-graph traversal over one namespace's ontology graph."""

    def __init__(
        self,
        graph_client: GraphClient,
        catalog: TableCatalog | None = None,
        *,
        namespace: str,
        graph_uri_template: str | None = None,
    ):
        """Bind the tool to the graph client and the request's namespace.

        Args:
            graph_client: Read-only SPARQL client over the ontology graph.
            catalog: Optional table catalog. When given, a seed table already seen
                by ``search_tables`` is resolved to its exact class IRI, which
                avoids matching a same-named table in another database.
            namespace: Namespace to scope traversal to.
            graph_uri_template: Named-graph URI template override; defaults to the
                configured one.
        """
        self._graph = graph_client
        self._catalog = catalog
        self._namespace = namespace
        self._graph_uri_template = get_graph_uri_template(graph_uri_template)
        # Request-scoped caches: the edge set is loaded at most once per request.
        self._fk_edges: list[dict[str, str]] | None = None
        self._label_iris: dict[str, list[str]] = {}
        self._graph_iris: list[str] | None = None

    @property
    def available(self) -> bool:
        """True when a named-graph URI template is configured (else traversal is a no-op)."""
        return bool(self._graph_uri_template)

    # ── SPARQL loaders ──────────────────────────────────────────────────────

    async def _query(self, sparql: str, *, what: str) -> list[dict[str, str]]:
        """Run a bounded SPARQL query, logging a timeout distinctly from other failures.

        The caller's ``except Exception`` would swallow the distinction, and an
        exhausted graph is worth telling apart from a malformed query when reading
        logs — so name it here (the same ``*_timeout`` convention Tier-3 uses) and
        re-raise for the caller's error path.
        """
        try:
            return await asyncio.wait_for(self._graph.query(sparql), timeout=_QUERY_TIMEOUT_S)
        except TimeoutError:
            logger.warning(
                "graph_tool_timeout",
                query=what,
                namespace=self._namespace,
                timeout_s=_QUERY_TIMEOUT_S,
            )
            raise

    async def _resolve_graph_iris(self, prefix: str) -> list[str]:
        """Resolve the namespace's named-graph IRIs so the loaders below can bind ``?g``.

        Cheap (one triple per published graph) and it gates everything else, so it
        gets its own tighter timeout and is best-effort: any failure or miss returns
        ``[]``, which keeps the prefix-filter fallback in
        :func:`~coa_serve.query_utils.graph_scoped_body`. The result is cached
        either way, so a namespace whose graphs do not carry the ``owl:Ontology``
        anchor — or a probe that times out — costs this once per request, not once
        per query.
        """
        if self._graph_iris is not None:
            return self._graph_iris
        sparql = named_graphs_sparql(prefix, limit=_MAX_GRAPHS)
        try:
            rows = await asyncio.wait_for(self._graph.query(sparql), timeout=_GRAPH_RESOLVE_TIMEOUT_S)
        except Exception as e:
            logger.info(
                "fk_graph_resolve_failed",
                namespace=self._namespace,
                error=f"{type(e).__name__}: {str(e)[:120]}",
            )
            self._graph_iris = []
            return []
        iris = [r["g"] for r in rows if r.get("g") and _SAFE_URI_RE.match(r["g"])]
        self._graph_iris = iris
        logger.debug("fk_graphs_resolved", namespace=self._namespace, graphs=len(iris))
        return iris

    async def _load_fk_edges(self, prefix: str) -> list[dict[str, str]]:
        """Load every FK object-property (domain/range table + labels + comment)."""
        if self._fk_edges is not None:
            return self._fk_edges
        # SHARED with the Tier-2 T-Box context builder — same edges, one definition
        # (``query_utils.object_properties_sparql``). Differences from that caller are
        # arguments, not a different query: no mapped-gate (traversal may cross into a
        # class Ontop cannot resolve, since it returns facts rather than authoring
        # SQL), a higher limit, ``?comment`` for the FK provenance string, and the
        # resolved named graphs (the T-Box builder now resolves them too, so both
        # callers are on the scoped form). NOT anchored on ``domain_iris``: this
        # caller wants the whole FK graph to walk, so it is unaffected by the
        # one-sided ?domain narrowing described in ``object_properties_sparql``.
        graphs = await self._resolve_graph_iris(prefix)
        sparql = object_properties_sparql(prefix, limit=_EDGE_QUERY_LIMIT, with_comment=True, graph_iris=graphs)
        rows = await self._query(sparql, what="fk_edges")
        # Log the count so an empty traversal is attributable: "this namespace has
        # no induced FKs" reads differently from a query that failed or timed out.
        # ``graphs`` distinguishes a third case — the un-bound fallback, which is
        # what makes this query slow enough to time out on a busy cluster.
        logger.debug("fk_edges_loaded", namespace=self._namespace, graphs=len(graphs), edge_count=len(rows))
        self._fk_edges = rows
        return rows

    async def _load_columns(self, prefix: str, class_iris: list[str]) -> dict[str, list[dict[str, str]]]:
        """Load datatype-property columns for the given (already-safe) class IRIs.

        NOT shared with ``ontop/tbox_context._fetch_class_context``, which reads the
        same datatype properties: that one is a multi-branch UNION over class-driven
        and property-driven patterns and also pulls ``coa:distinctValues`` and parent
        classes, while this is a flat ``VALUES ?class`` lookup of column + label +
        range. A common builder would have to carry both shapes, so they are kept
        apart deliberately — unlike the FK query above, which really was a copy.
        """
        safe = [u for u in class_iris if _SAFE_URI_RE.match(u)]
        if not safe:
            return {}
        values = " ".join(f"<{u}>" for u in safe)
        graphs = await self._resolve_graph_iris(prefix)
        body = f"""            VALUES ?class {{ {values} }}
            ?col a owl:DatatypeProperty ; rdfs:domain ?class .
            OPTIONAL {{ ?col rdfs:label ?colLabel }}
            OPTIONAL {{ ?col rdfs:range ?range }}
            OPTIONAL {{ ?col rdfs:comment ?colComment }}"""
        sparql = f"""{SPARQL_PREFIXES}
        SELECT ?class ?col ?colLabel ?range ?colComment WHERE {{
{graph_scoped_body(body, prefix, graphs)}
        }}"""
        rows = await self._query(sparql, what="columns")
        by_class: dict[str, list[dict[str, str]]] = {}
        for r in rows:
            cls = r.get("class", "")
            if not cls:
                continue
            column = r.get("colLabel", "") or _localname(r.get("col", ""))
            by_class.setdefault(cls, []).append(
                {
                    "column": column,
                    "type": _localname(r.get("range", "")),
                    # The ingest pipeline's per-column description. Used only by
                    # :func:`_schema_text` when a walk has to rebuild a schema for a
                    # consumer with no retrieved text; ``explore_graph`` does not
                    # return it (the agent asked for structure, not prose).
                    "desc": (r.get("colComment", "") or "").strip(),
                }
            )
        return by_class

    async def _resolve_label_to_iris(self, prefix: str, label: str) -> list[str]:
        """Resolve a bare table label to its class IRI(s) (may be >1 across DBs).

        Raises:
            ValueError: If ``label`` is not a safe literal. Callers already gate with
                :func:`_is_safe_label`; re-checking here keeps the interpolation safe
                for any future caller too.
        """
        if not _is_safe_label(label):
            raise ValueError(f"unsafe table label: {label!r}")
        # Case-folded on both sides so a seed matches a differently-cased label.
        # ``str.lower()`` and SPARQL ``LCASE`` agree on ASCII and are both no-ops on
        # scripts without case (CJK), which is the common case here; the rare
        # locale-specific disagreement (Turkish dotted İ, German ß) can only cost a
        # match, never produce a wrong one.
        key = label.lower()
        if key in self._label_iris:
            return self._label_iris[key]
        graphs = await self._resolve_graph_iris(prefix)
        body = f"""            ?class a owl:Class ; rdfs:label ?label .
            FILTER(LCASE(STR(?label)) = "{_escape_literal(key)}")"""
        sparql = f"""{SPARQL_PREFIXES}
        SELECT ?class WHERE {{
{graph_scoped_body(body, prefix, graphs)}
        }}"""
        rows = await self._query(sparql, what="label_to_iris")
        iris = [r["class"] for r in rows if r.get("class") and _SAFE_URI_RE.match(r["class"])]
        self._label_iris[key] = iris
        return iris

    def _seed_iri_from_catalog(self, table_name: str) -> str:
        """Exact class IRI of a table the catalog already retrieved, or ""."""
        if self._catalog is None:
            return ""
        hits, _missing = self._catalog.hits_for_tables([table_name])
        for hit in hits:
            iri = hit_class_iri(hit)
            if iri:
                return iri
        return ""

    # ── traversal ────────────────────────────────────────────────────────────

    async def expand_from(
        self,
        seed_iris: list[str],
        *,
        hops: int = 1,
        max_tables: int = 8,
    ) -> list[dict[str, Any]]:
        """Tables an FK walk reaches from an ALREADY-CHOSEN set of classes.

        The flat-arm analogue of :meth:`explore_graph`. There is no agent to seed
        a walk on the flat NL→SQL path, so the seeds are the classes vector retrieval
        already returned, and the result is the ranked, *schema-bearing* set of
        tables one to ``hops`` FK hops beyond them. This is the only way that path
        can reach a table similarity did not rank in its top-k: it retrieves **once**
        and has no tools — its 2-shot ``correct()`` re-generates on an execution
        error but never re-retrieves — so a join table whose name and description
        look nothing like the question is otherwise unreachable.

        Seeds are class IRIs, never labels. 77 of the pooled Spider 2.0 namespace's
        412 classes share a bare label, so seeding by name walks out of the wrong
        database and returns a plausible schema for tables that cannot be joined. A
        seed IRI absent from the graph simply reaches nothing.

        The seeds themselves are excluded: the caller already holds their retrieved
        text, which is richer than what the graph can rebuild (it carries the sampled
        allowed-values block; see :func:`_schema_text`).

        Args:
            seed_iris: Class IRIs to walk out from; unsafe ones are dropped.
            hops: FK hops to expand outward; clamped to [1, :data:`MAX_HOPS`].
            max_tables: Cap on the tables returned; clamped to
                [0, :data:`MAX_NODE_LIMIT`].

        Returns:
            One dict per reached table (``iri``, ``table``, ``hop``, ``direction``,
            ``via``, ``schema``), nearest hop first then by label. ``[]`` for every
            degenerate case — no graph template, no safe seed, nothing reached — so a
            walk that finds nothing leaves the caller's prompt byte-identical to
            baseline. Load failures propagate: the caller decides whether a slow
            graph should degrade or fail the query.
        """
        validate_namespace(self._namespace)
        if not self._graph_uri_template:
            return []
        seeds = [s for s in (seed_iris or []) if s and _SAFE_URI_RE.match(s)]
        hops = max(1, min(int(hops), MAX_HOPS))
        max_tables = max(0, min(int(max_tables), MAX_NODE_LIMIT))
        if not seeds or not max_tables:
            return []

        prefix = namespace_graph_prefix(self._graph_uri_template, self._namespace)
        edges = await self._load_fk_edges(prefix)
        adjacency, label_of = _build_adjacency(edges)
        reached = _bfs(seeds, adjacency, hops)

        seed_set = set(seeds)
        # Same order explore_graph reports, and it matters more here: the caller
        # takes a fixed prefix of this list rather than showing an agent all of it,
        # so the ranking — not the hop bound — decides which tables reach the
        # writer's prompt.
        ordered = sorted(
            ((iri, meta) for iri, meta in reached.items() if iri not in seed_set),
            key=lambda kv: (kv[1][0], label_of.get(kv[0], kv[0])),
        )[:max_tables]
        if not ordered:
            return []

        cols_by_class = await self._load_columns(prefix, [iri for iri, _ in ordered])
        out: list[dict[str, Any]] = []
        for iri, (hop, via, direction) in ordered:
            label = label_of.get(iri, _localname(iri))
            out.append(
                {
                    "iri": iri,
                    "table": label,
                    "hop": hop,
                    "direction": direction,
                    "via": via,
                    # Keyed to the class the walk actually reached, so a homonym in
                    # another database cannot supply the schema.
                    "schema": _schema_text(label, cols_by_class.get(iri, [])),
                }
            )
        return out

    async def explore_graph(
        self,
        table_name: str,
        hops: int = 1,
        node_types: list[str] | None = None,
        limit: int = DEFAULT_NODE_LIMIT,
    ) -> dict[str, Any]:
        """BFS ``hops`` FK-hops out from a seed table; return tables (+optional columns).

        The seed is resolved to an exact class IRI through the catalog when the
        table was already retrieved (avoiding a cross-database label collision),
        otherwise by its label.

        Args:
            table_name: Seed table name, ideally one ``search_tables`` returned.
            hops: FK hops to expand outward; clamped to [1, :data:`MAX_HOPS`].
            node_types: Which nodes to return — any of ``"table"``, ``"column"``.
                Omit for all types.
            limit: Max nodes returned; clamped to [1, :data:`MAX_NODE_LIMIT`].
                Tables consume the budget before columns.

        Returns:
            The traversal result, or ``{"error": ...}`` when traversal is
            unavailable or the seed cannot be resolved.
        """
        validate_namespace(self._namespace)
        if not self._graph_uri_template:
            return {"error": "graph traversal unavailable (GRAPH_URI_TEMPLATE unset)"}

        seed = str(table_name).strip().lower()
        # Clamp into range: the caller's value wins, the bounds only stop a
        # pathological one (0, negative, 999). MAX_HOPS / MAX_NODE_LIMIT are the
        # ceilings; the 1s are floors, NOT defaults — the defaults are in the
        # signature (hops=1, limit=DEFAULT_NODE_LIMIT) and the caller may ask for
        # up to MAX_HOPS on any call.
        hops = max(1, min(int(hops), MAX_HOPS))
        limit = max(1, min(int(limit), MAX_NODE_LIMIT))
        types = _normalize_node_types(node_types)
        prefix = namespace_graph_prefix(self._graph_uri_template, self._namespace)

        seed_iri = self._seed_iri_from_catalog(seed)
        seed_iris: list[str] = []
        if seed_iri and _SAFE_URI_RE.match(seed_iri):
            seed_iris = [seed_iri]
        elif _is_safe_label(seed):
            seed_iris = await self._resolve_label_to_iris(prefix, seed)
        if not seed_iris:
            return {"error": f"seed table not found ({seed!r})", "tables": [], "columns": []}

        edges = await self._load_fk_edges(prefix)
        adjacency, label_of = _build_adjacency(edges)
        reached = _bfs(seed_iris, adjacency, hops)  # iri -> (hop, via, direction)
        ordered = sorted(reached.items(), key=lambda kv: (kv[1][0], label_of.get(kv[0], kv[0])))

        # Tables: nearest hop first, then label; the seed(s) at hop 0 are included
        # so the consumer sees where the walk started.
        table_nodes: list[dict[str, Any]] = []
        if "table" in types:
            table_nodes = [
                {"table": label_of.get(iri, _localname(iri)), "hop": hop, "direction": direction, "via": via}
                for iri, (hop, via, direction) in ordered
            ]

        column_nodes: list[dict[str, Any]] = []
        if "column" in types:
            cols_by_class = await self._load_columns(prefix, list(reached))
            for iri, _meta in ordered:
                table = label_of.get(iri, _localname(iri))
                for col in cols_by_class.get(iri, []):
                    column_nodes.append({"table": table, "column": col["column"], "type": col["type"]})

        # Global truncation: tables consume the budget first, then columns.
        total = len(table_nodes) + len(column_nodes)
        tables_out = table_nodes[:limit]
        columns_out = column_nodes[: max(0, limit - len(tables_out))]
        returned = len(tables_out) + len(columns_out)
        logger.debug(
            "explore_graph",
            namespace=self._namespace,
            seed=seed,
            seed_iri_resolved=bool(seed_iri),
            hops=hops,
            returned_nodes=returned,
        )
        return {
            "seed": seed,
            "seed_matched_classes": len(seed_iris),
            "hops": hops,
            "node_types": list(types),
            "tables": tables_out,
            "columns": columns_out,
            "total_nodes_found": total,
            "returned_nodes": returned,
            "truncated": total > returned,
        }


def _normalize_node_types(node_types: list[str] | None) -> tuple[str, ...]:
    """None/empty → all types; else the valid subset (unknown values dropped)."""
    if not node_types:
        return VALID_NODE_TYPES
    wanted = {str(x).strip().lower() for x in node_types}
    return tuple(t for t in VALID_NODE_TYPES if t in wanted) or VALID_NODE_TYPES


def _schema_text(label: str, cols: list[dict[str, str]]) -> str:
    """One table as the ``"Table: … | Columns: …"`` line the SQL writer reads.

    Deliberately the same shape as the class text the ingest pipeline stores in the
    vector index, so a table a walk found can be handed to the writer through the
    existing context builder instead of a second code path. The ontology supplies
    the column name, its XSD type and the ingest-time description; what it cannot
    supply is the sampled allowed-values block, so a walk-derived schema is thinner
    than a retrieved one — which is why the caller never overwrites a retrieved one.
    """
    if not cols:
        return f"Table: {label}"
    parts = []
    for col in cols:
        name = col.get("column") or ""
        if not name:
            continue
        text = f"{name}:{col.get('type') or 'unknown'}"
        desc = (col.get("desc") or "")[:_MAX_COL_DESC]
        parts.append(f"{text} ({desc})" if desc else text)
    if not parts:
        return f"Table: {label}"
    return f"Table: {label} | Columns: {', '.join(parts)}"


def _localname(iri: str) -> str:
    """Last path/fragment segment of an IRI (or the input if it is not an IRI)."""
    if not iri:
        return ""
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _build_adjacency(edges: list[dict[str, str]]) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    """Build a bidirectional FK adjacency + an IRI→label map from FK edge rows.

    Each edge (domain --FK--> range) yields an *outgoing* neighbor from the domain
    and an *incoming* neighbor to the range, so BFS follows joins in both
    directions (a query may join parent→child or child→parent).
    """
    adjacency: dict[str, list[dict[str, str]]] = {}
    label_of: dict[str, str] = {}
    for e in edges:
        dom, rng = e.get("domain", ""), e.get("range", "")
        if not dom or not rng:
            continue
        label_of[dom] = e.get("domainLabel", "") or _localname(dom)
        label_of[rng] = e.get("rangeLabel", "") or _localname(rng)
        # A self-referential FK (employee.manager_id → employee) adds two adjacency
        # entries that BFS can never use: the node is already reached at hop 0 by
        # the time its own neighbours are scanned. Keep its label, drop the edges.
        if dom == rng:
            continue
        via = e.get("comment", "") or e.get("opLabel", "") or _localname(e.get("op", ""))
        adjacency.setdefault(dom, []).append({"to": rng, "via": via, "direction": "outgoing"})
        adjacency.setdefault(rng, []).append({"to": dom, "via": via, "direction": "incoming"})
    return adjacency, label_of


def _bfs(
    seeds: list[str], adjacency: dict[str, list[dict[str, str]]], max_hops: int
) -> dict[str, tuple[int, str, str]]:
    """Breadth-first expansion. Returns iri → (hop, via, direction); seeds at hop 0."""
    reached: dict[str, tuple[int, str, str]] = {}
    queue: deque[tuple[str, int]] = deque()
    for s in seeds:
        if s not in reached:
            reached[s] = (0, "", "seed")
            queue.append((s, 0))
    while queue:
        iri, hop = queue.popleft()
        if hop >= max_hops:
            continue
        for edge in adjacency.get(iri, []):
            neighbor = edge["to"]
            if neighbor not in reached:
                reached[neighbor] = (hop + 1, edge["via"], edge["direction"])
                queue.append((neighbor, hop + 1))
    return reached
