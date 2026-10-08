# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assemble query-relevant T-Box context from Neptune published graph.

The T-Box context provides the ontology schema (classes, properties, metrics)
needed by the NL-to-SPARQL translation prompt. Context is assembled from
vector search hits produced during routing.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from coa_common.constants import URN_PREFIX, VOCAB_URI

from ...clients.base import GraphClient
from ...query_utils import (
    DEFAULT_GRAPH_URI_TEMPLATE,
    build_query_search_plan,
    escape_sparql_string_literal,
    get_graph_uri_template,
    graph_scoped_body,
    named_graphs_sparql,
    namespace_graph_prefix,
    normalize_label_match_text,
    object_properties_sparql,
    validate_namespace,
)
from .types import VectorHit

logger = structlog.get_logger(__name__)

# Full IRI for coa:distinctValues — the sampled distinct enum values a datatype
# property may carry (emitted at induction from low-cardinality categorical
# columns). Used as a full IRI (not a prefix) so the SPARQL needs no PREFIX decl.
_SCL_DISTINCT_VALUES = f"{VOCAB_URI}distinctValues"
# Max sampled values rendered per property in the prompt (keeps the context lean;
# a handful of literals is enough for the LLM to anchor WHERE-clause values).
_MAX_DISTINCT_VALUES_IN_PROMPT = 12

# Steward-reviewed annotations carried into the prompt (#1167), so the NL→SPARQL
# writer sees the same approved metadata the NL→SQL writer gets from the class
# text: description (rdfs:comment), synonyms (skos:altLabel), glossary terms and
# tags. Full IRIs because the graph client sends no PREFIX prolog and only
# rdfs:/owl: are Neptune defaults.
_RDFS_COMMENT = "http://www.w3.org/2000/01/rdf-schema#comment"
_SKOS_ALT_LABEL = "http://www.w3.org/2004/02/skos/core#altLabel"
_SCL_GLOSSARY_TERM = f"{VOCAB_URI}glossaryTerm"
_SCL_TAG = f"{VOCAB_URI}tag"
_ANNOTATION_KEYS = {
    _RDFS_COMMENT: "description",
    _SKOS_ALT_LABEL: "synonyms",
    _SCL_GLOSSARY_TERM: "glossary_terms",
    _SCL_TAG: "tags",
}
# Subjects per annotation query. The query asks for the annotations of exactly
# the terms in the prompt (classes, then columns, then join paths), so rows per
# subject are bounded: one description plus at most a few synonyms, glossary terms
# and tags each (enrichment caps those at 5), i.e. <= ~16. 50 subjects therefore
# stays far below _SPARQL_RESULT_LIMIT, and matches the VALUES cap.
_ANNOTATION_SUBJECT_CHUNK = 50
# Annotation queries in flight at once per build. The fetch is on the Tier-2
# critical path and this builder has driven Neptune to timeouts before, so a large
# prompt is fetched in a few waves rather than all at once.
_ANNOTATION_MAX_CONCURRENCY = 4
# Share of max_tokens kept free for annotations when the context has to be
# truncated. Without it truncation fills the budget and large namespaces, where
# business wording helps most, would get no annotations at all.
_ANNOTATION_BUDGET_SHARE = 0.15
# Prompt caps. Column descriptions match the NL→SQL context's 100-character cut;
# class descriptions are capped only to bound a pathological catalog entry.
_MAX_CLASS_DESCRIPTION_CHARS = 500
_MAX_PROPERTY_DESCRIPTION_CHARS = 100
_MAX_TERMS_IN_PROMPT = 5
# ~4 characters per token, the same rough rate the per-element estimates assume.
_CHARS_PER_TOKEN = 4

# Maximum URIs per SPARQL VALUES clause (Neptune query complexity limit)
_MAX_SPARQL_VALUES_URIS = 50
# Cap for the join-path query's ?domain anchor (``domain_iris`` in
# object_properties_sparql). Deliberately NOT _MAX_SPARQL_VALUES_URIS: there the
# VALUES set is the result set being asked about, so truncating returns fewer rows
# of the same kind, whereas here it is a FILTER over classes already chosen for the
# prompt — truncating silently deletes the join paths of every class past the cut.
# Sized to never bind (the full-context path caps at 200 classes, the vector path
# yields ~10); it exists only to bound the query string.
_MAX_DOMAIN_ANCHOR_URIS = 250
# Maximum results per SPARQL query
_SPARQL_RESULT_LIMIT = 2000
# Cap on FK join paths folded into the prompt. Lower than _SPARQL_RESULT_LIMIT
# because these are rendered as prose for the LLM, not walked programmatically.
_OBJECT_PROPERTY_LIMIT = 200
# Namespace class count threshold for full-context fetch (bypass vector search)
_FULL_CONTEXT_CLASS_THRESHOLD = 200

# Tier-2 answerability gate. A class is included in the structured-query T-Box
# context ONLY if it carries the ``coa:isMapped true`` marker written at ingest
# (i.e. it is the rr:class of an R2RML TriplesMap → SQL-backed → answerable by
# Ontop / NL->SQL). Unmapped classes (unstructured-induced, foundational) are
# excluded so the LLM never authors a query over a class Ontop can't resolve.
# This is a REQUIRED triple pattern (absent marker == hidden), not OPTIONAL.
# The full IRI is inlined because ``coa:`` is not a Neptune default prefix and
# the graph client injects no PREFIX prolog. The SPARQL keyword ``true`` is
# exactly the typed literal ``"true"^^xsd:boolean`` — the same term store_class
# writes — so it matches by term equality.
_IS_MAPPED_IRI = f"{VOCAB_URI}isMapped"
_IS_MAPPED_PATTERN = f"?class <{_IS_MAPPED_IRI}> true ."
# ─────────────────────────────────────────────────────────────────────────────
# LEGACY-NAMESPACE BRIDGE (temporary compatibility shim)
#
# The ``coa:isMapped`` marker is written only by the post-marker ingest path
# (NDB backend). A namespace ingested BEFORE the marker existed — or via a
# backend that never writes it — carries ZERO isMapped triples, so the required
# ``_IS_MAPPED_PATTERN`` gate would hide EVERY class and silently break Tier-2
# for that namespace (structured queries fall through to Tier-3). To avoid
# regressing existing users, ``build`` probes once per request whether the
# namespace has ANY mapped marker (``_namespace_has_mapped_markers``); when it
# has none it drops the gate entirely (``mapped=False``), restoring the
# pre-filter behavior (all classes exposed) for that namespace only. A namespace
# with even ONE mapped class keeps the strict gate. The gate is selected
# per-request via ``_class_gate(mapped)`` / ``_parent_pattern(mapped)`` so both
# behaviors coexist across namespaces.
#
# ── HOW TO REMOVE THIS BRIDGE (revert to the strict pre-bridge behavior) ──
# Added 2026-07-16; TEMPORARY. Revisit by ~2026-10 (≈3 months). If you are here
# after that and the bridge still exists, it has likely outlived its purpose:
# verify all live namespaces carry coa:isMapped markers, then remove it.
# The end state is the original strict "absent marker == hidden" behavior —
# adopt it once all live namespaces have been re-inducted (so they carry
# coa:isMapped markers) and users have been given notice. Removal is a pure
# deletion, no logic to re-derive: every bridge site below is tagged
# ``# BRIDGE:`` and carries the exact PRE-BRIDGE line to restore. To remove:
# 1. Delete ``_namespace_has_mapped_markers`` and the ``mapped = await …``
# probe call + fallback log in ``build``.
# 2. Delete ``_NO_GATE``, ``_UNGATED_PARENT_PATTERN``, ``_class_gate``,
# ``_parent_pattern``, and the ``mapped`` parameter on the four fetch
# methods.
# 3. At each ``# BRIDGE:`` site, replace the ``_class_gate(mapped)`` /
# ``_parent_pattern(mapped)`` / conditional-gate call with the PRE-BRIDGE
# line quoted in its comment (the module constants ``_IS_MAPPED_PATTERN`` /
# ``_MAPPED_PARENT_PATTERN`` used unconditionally).
# 4. Delete the bridge tests (class ``TestIsMappedLegacyBridge`` in
# test_tbox_context_strstarts.py) AND revert the two ancillary test edits:
# the ``graph_client.ask = AsyncMock(...)`` line + bridge comment added to
# TestTBoxGlossaryMappedGate (same file), and the reworded ``call_count``
# comment in test_tier2_tbox_context.py.
# 5. Restore the prose comments that were reworded to mention the bridge (this
# block, the dark-namespace canary above ``if not context.classes``, and the
# count/fetch-all/object-property explanatory comments) to their pre-bridge
# wording — these are comment-only and must be hand-reverted.
_NO_GATE = ""  # empty triple pattern → no isMapped restriction (legacy fallback)
# Parent (rdfs:subClassOf) is surfaced to the prompt as ``subClassOf: <parent>``,
# so it must be gated the same way as the class itself and as object-property
# ends: bind ?parentClass ONLY when the parent is itself a MAPPED class. Without
# the inner isMapped guard, a grounded class (``ind:Orders rdfs:subClassOf
# fibo:PurchaseOrder``) leaks the unmapped foundational parent IRI into the
# structured prompt — the exact "unmapped class name reaches the LLM" failure the
# object-property gate prevents. The parent's isMapped marker lives in the same
# named graph as the subClassOf triple (store_class writes all of a class's
# triples into one graph), so an inner ``?parentClass <isMapped> true`` matches a
# mapped sibling parent and correctly excludes any foundational/unmapped parent.
_MAPPED_PARENT_PATTERN = f"OPTIONAL {{ ?class rdfs:subClassOf ?parentClass . ?parentClass <{_IS_MAPPED_IRI}> true . }}"
# Legacy-fallback parent pattern: surface the subClassOf parent WITHOUT the
# parent-is-mapped guard. Used only when the namespace has zero isMapped markers,
# so there is no mapped/unmapped distinction to enforce — the pre-filter behavior.
_UNGATED_PARENT_PATTERN = "OPTIONAL { ?class rdfs:subClassOf ?parentClass . }"


# BRIDGE: pre-bridge, callers used the module constant ``_IS_MAPPED_PATTERN``
# directly (always gated). This helper adds the ``mapped=False`` fallback.
def _class_gate(mapped: bool) -> str:
    """The ``?class <isMapped> true`` gate, or empty for the legacy fallback.

    ``mapped=True`` (the namespace has ≥1 isMapped marker) → the required gate,
    so unmapped classes stay hidden. ``mapped=False`` (namespace predates the
    marker / backend never writes it) → empty, exposing all classes.
    """
    return _IS_MAPPED_PATTERN if mapped else _NO_GATE


# BRIDGE: pre-bridge, callers used ``_MAPPED_PARENT_PATTERN`` directly.
def _parent_pattern(mapped: bool) -> str:
    """subClassOf-parent OPTIONAL — parent-mapped-gated, or ungated for fallback."""
    return _MAPPED_PARENT_PATTERN if mapped else _UNGATED_PARENT_PATTERN


# Per-class property OPTIONAL. Restricted to owl:DatatypeProperty (columns) ON
# PURPOSE: a datatype property's rdfs:range is an xsd:* term (xsd:integer, …),
# never a class IRI, so surfacing it in the prompt's "Properties" section can
# never leak a class name. An OBJECT property's range IS a class IRI — and when
# that target class is unmapped (unstructured / foundational), an un-gated
# ``?property rdfs:range ?range`` would reintroduce an UNANSWERABLE class name
# into the structured prompt via the property's range (the object-property
# equivalent of the subClassOf-parent leak). Object-property join paths are
# surfaced separately by ``_fetch_object_properties``, mapped-gated on BOTH ends;
# excluding them here means the only object-property edges the LLM ever sees are
# mapped<->mapped. (``owl:`` / ``rdfs:`` resolve as Neptune default prefixes, as
# elsewhere in these queries.)
_DATATYPE_PROPS_OPTIONAL = f"""OPTIONAL {{
              ?property a owl:DatatypeProperty .
              ?property rdfs:domain ?class .
              ?property rdfs:label ?propLabel .
              ?property rdfs:range ?range .
              OPTIONAL {{ ?property <{_SCL_DISTINCT_VALUES}> ?distinctValue . }}
            }}"""
# Approximate tokens per ontology element (for prompt budget estimation)
_TOKENS_PER_CLASS = 20
_TOKENS_PER_PROPERTY = 30
_TOKENS_PER_OBJECT_PROPERTY = 25
_TOKENS_PER_METRIC = 40

# ── Named-graph scoping (query cost) ─────────────────────────────────────────
# Resolve the namespace's graph IRIs once per build and thread them into every
# fetch, so each query is priced by the namespace instead of by the whole cluster.
# Failure returns ``[]`` and the prefix-filter fallback keeps working — slow, not
# broken. See ``query_utils.graph_scoped_body`` for the query forms and why the
# constant-``GRAPH <iri>`` one is load-bearing on multi-graph namespaces.
_MAX_GRAPHS = 50
# 10s, not 5s: this is the FIRST Neptune query of a build, so on a cold container it
# pays connection setup and credential signing too, and 5s lost that race half the
# time (BIRD-Interact resolved 5 of 10 builds to ``graphs=0``, 5 to ``graphs=2``, on
# an unchanged namespace). Warm, it returns in ~10ms. Losing is asymmetric: the
# fallback is the cluster-wide scan this exists to avoid.
_GRAPH_RESOLVE_TIMEOUT_S = 10.0
# The builder is process-lived, so unlike the request-scoped traversal tool the
# cache must expire or a namespace that publishes a new graph is queried against a
# stale IRI list for the life of the process.
_GRAPH_IRI_CACHE_TTL_S = 300.0


_SAFE_URI_RE = re.compile(r"^https?://[^\s<>\"{}|\\^`]+\Z")  # \Z, not $, so a trailing newline is rejected


def _is_safe_sparql_uri(uri: str) -> bool:
    """Return True if uri is safe for SPARQL angle-bracket interpolation."""
    return bool(_SAFE_URI_RE.match(uri)) and ">" not in uri


__all__ = ["TBoxContext", "TBoxContextBuilder", "DEFAULT_GRAPH_URI_TEMPLATE"]


def _render_annotations(item: dict[str, Any], max_description: int) -> str:
    """Render a term's steward-reviewed annotations as the prompt-line suffix (#1167).

    The same fields the NL→SQL context carries, so both writers resolve business
    wording the same way. Shared by ``format_for_prompt`` and the annotation budget
    so the budget charges exactly what the prompt shows.
    """
    parts: list[str] = []
    description = (item.get("description") or "").strip()
    if description:
        parts.append(f"description: {description[:max_description]}")
    for key, title in (("synonyms", "synonyms"), ("glossary_terms", "glossary terms"), ("tags", "tags")):
        values = item.get(key) or []
        if values:
            parts.append(f"{title}: {', '.join(values[:_MAX_TERMS_IN_PROMPT])}")
    return f" | {' | '.join(parts)}" if parts else ""


@dataclass
class MetricContext:
    """A metric's context for the NL-to-SPARQL prompt."""

    name: str
    description: str = ""
    formula: str = ""
    dimensions: list[str] = field(default_factory=list)


@dataclass
class AiContextTerm:
    """A term parsed from a node's OSI-spec ``:aiContext`` literal.

    Sourced from the ``:aiContext`` JSON literal on an ontology/metric node:
    business synonyms and free-text instructions that ground the LLM's
    translation (e.g. "churn" → the ontology URI; "use net amount, not gross").
    """

    label: str
    synonyms: list[str] = field(default_factory=list)
    instructions: str = ""


@dataclass
class TBoxContext:
    """Ontology T-Box schema subset for LLM prompt inclusion."""

    # Each class, property and object property may also carry the steward-reviewed
    # annotations attached by ``_attach_annotations``: ``description`` (str),
    # ``synonyms``, ``glossary_terms``, ``tags`` (list[str]).
    classes: list[dict[str, Any]]  # [{uri, label, parent, ...annotations}]
    properties: list[dict[str, Any]]  # [{uri, label, domain, range, ...annotations}]
    object_properties: list[dict[str, Any]] = field(default_factory=list)  # [{uri, label, domain, range_class, ...}]
    metrics: list[MetricContext] = field(default_factory=list)
    glossary: list[AiContextTerm] = field(default_factory=list)  # aiContext terms
    token_estimate: int = 0
    # Not prompt content — nothing formats this. Carried out of the build so
    # ``SPARQLValidator``, which runs afterwards on the same namespace, can scope its
    # own queries without re-resolving. Empty means resolution failed or was skipped,
    # which ``graph_scoped_body`` reads as "use the prefix filter".
    graph_iris: list[str] = field(default_factory=list)


class TBoxContextBuilder:
    """Builds T-Box context from routing's vector search hits.

    Partitions hits by type, fetches full definitions from Neptune for
    ontology hits, and formats metric context from metric hits.
    """

    def __init__(self, graph_client: GraphClient, graph_uri_template: str | None = None, metric_resolver=None):
        """Bind the graph client, URI template, and optional metric resolver.

        Args:
            graph_client: Graph client used to fetch full ontology definitions.
            graph_uri_template: Optional named-graph URI template; a default is
                resolved when None.
            metric_resolver: Optional Tier-1 resolver that enriches metric hits
                with full Neptune metric definitions.
        """
        self._graph = graph_client
        self._graph_uri_template = get_graph_uri_template(graph_uri_template)
        # optional Tier-1 MetricResolver — when present, metric vector
        # hits are enriched with the FULL Neptune-loaded :GovernedMetric
        # definition (description/dimensions/formula) instead of only the sparse
        # vector-hit metadata. Optional + duck-typed to avoid a hard import cycle.
        self._metric_resolver = metric_resolver
        # namespace -> (resolved_at_monotonic, graph IRIs). See _MAX_GRAPHS above.
        self._graph_iri_cache: dict[str, tuple[float, list[str]]] = {}

    async def _resolve_graph_iris(self, namespace: str, graph_uri_prefix: str) -> list[str]:
        """Resolve the namespace's named-graph IRIs so the fetches below can bind ``?g``.

        Best-effort by design: a failure, a timeout, or a namespace whose graphs
        carry no ``owl:Ontology`` anchor all return ``[]``, which leaves
        :func:`~coa_serve.query_utils.graph_scoped_body` on its prefix-filter
        form. Cached per namespace with a TTL (see ``_GRAPH_IRI_CACHE_TTL_S``)
        because this builder outlives the request.

        ONLY A SUCCESSFUL RESOLUTION IS CACHED, including a successful empty one.
        Caching a *failure* would turn one transient timeout into 300s of cluster-wide
        scans for every request this container serves — and those scans are what
        overload the graph in the first place, a feedback loop that was observed
        answering a whole BIRD-Interact cell with no ontology at all. Re-querying
        costs one cheap query; the loop costs the run.
        """
        cached = self._graph_iri_cache.get(namespace)
        if cached and (time.monotonic() - cached[0]) < _GRAPH_IRI_CACHE_TTL_S:
            return cached[1]
        try:
            rows = await asyncio.wait_for(
                self._graph.query(named_graphs_sparql(graph_uri_prefix, limit=_MAX_GRAPHS)),
                timeout=_GRAPH_RESOLVE_TIMEOUT_S,
            )
            iris = [r["g"] for r in rows if r.get("g") and _is_safe_sparql_uri(r["g"])]
        except Exception as e:
            # warning, not info: every query in this build now runs the cluster-wide
            # scan this resolution exists to remove.
            logger.warning(
                "tbox_graph_resolve_failed",
                namespace=namespace,
                error=f"{type(e).__name__}: {str(e)[:120]}",
                detail=(
                    "falling back to the prefix filter (cluster-wide scan) for THIS request only — "
                    "deliberately not cached, see the docstring"
                ),
            )
            return []
        self._graph_iri_cache[namespace] = (time.monotonic(), iris)
        # Bound the cache: drop entries whose TTL has lapsed, so it holds only the
        # namespaces seen within one TTL window rather than every namespace this
        # process ever served. ponytail: still O(namespaces-active-within-TTL), no
        # hard max-size cap — swap in an LRU if a single container ever fans out to
        # thousands of live namespaces inside 300s.
        now = time.monotonic()
        expired = [ns for ns, (at, _) in self._graph_iri_cache.items() if now - at >= _GRAPH_IRI_CACHE_TTL_S]
        for ns in expired:
            del self._graph_iri_cache[ns]
        # info, not debug: the only signal that the graphs were bound rather than
        # fallen back — a silent fallback surfaces only as a 16s ReadTimeout later.
        logger.info("tbox_graphs_resolved", namespace=namespace, graphs=len(iris))
        return iris

    async def build(
        self,
        vector_hits: list[VectorHit],
        namespace: str,
        query: str = "",
        max_tokens: int = 20_000,
    ) -> TBoxContext:
        """Build T-Box context from vector search hits.

        Args:
            vector_hits: Typed vector search results from routing.
            namespace: Ontology namespace.
            query: Original NL query (used for entity-based fallback if no hits).
            max_tokens: Token budget for the returned context.

        Returns:
            TBoxContext with classes, properties, and metric summaries.
        """
        # Partition hits by type
        ontology_hits = [h for h in vector_hits if h.type in ("ontology_class", "ontology_property", "ontology")]
        metric_hits = [h for h in vector_hits if h.type == "metric"]

        # Build metric context from metric hits
        metrics = self._build_metric_context(metric_hits)

        # Bind the namespace's named graphs ONCE for every Neptune query below
        # (see the _MAX_GRAPHS block at module top). Resolved here rather than in
        # each fetch so the cost is one query per build, and passed explicitly so
        # a fetch called directly (tests, future callers) still scopes itself via
        # the prefix-filter fallback.
        graph_iris: list[str] = []
        if self._graph_uri_template:
            prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
            if _is_safe_sparql_uri(prefix):
                graph_iris = await self._resolve_graph_iris(namespace, prefix)

        # BRIDGE: probe ONCE whether this namespace has any coa:isMapped marker
        # (see the LEGACY-NAMESPACE BRIDGE block at module top). ``mapped`` then
        # selects the strict gate (has markers) or the pre-filter fallback (none).
        # Pre-bridge, this probe did not exist and every fetch below was
        # unconditionally gated — to remove, delete this call and pass nothing.
        mapped = await self._namespace_has_mapped_markers(namespace, graph_iris=graph_iris)
        if not mapped:
            logger.info(
                "tbox_ismapped_bridge_fallback",
                namespace=namespace,
                detail=(
                    "namespace has 0 coa:isMapped markers — dropping the mapped-class gate "
                    "and exposing all classes (legacy/pre-marker fallback so Tier-2 keeps working)"
                ),
            )

        # Fetch ontology definitions from Neptune
        classes: list[dict[str, Any]] = []
        properties: list[dict[str, Any]] = []

        # Strategy: For small namespaces (< threshold classes), fetch ALL classes
        # and properties to ensure complete context. This avoids vector-search
        # misses that cause InternalError on valid queries.
        full_context = await self._try_full_namespace_context(namespace, mapped, graph_iris=graph_iris)
        if full_context is not None:
            classes, properties = full_context
        elif ontology_hits:
            classes, properties = await self._fetch_ontology_context(
                ontology_hits, namespace, mapped, graph_iris=graph_iris
            )
        elif query:
            # Fallback: use query entities to fetch context
            classes, properties = await self._fetch_by_entities(query, namespace, mapped, graph_iris=graph_iris)

        # Fetch ObjectProperties (FK join paths) only when multiple classes are
        # present — single-class queries don't need join paths, saving a Neptune roundtrip.
        object_properties: list[dict[str, Any]] = []
        if len(classes) > 1:
            object_properties = await self._fetch_object_properties(
                namespace, mapped, graph_iris=graph_iris, class_uris=[c["uri"] for c in classes if c.get("uri")]
            )

        # fetch :aiContext glossary (synonyms/instructions) for the hit
        # nodes so business terms map to ontology URIs. Best-effort — never blocks
        # translation; an empty/failed fetch simply yields no glossary section.
        #
        # GATED to the mapped context: a glossary entry grounds a business term to
        # an ontology node ("churn" → <...#Churn>), so it must only reference nodes
        # that actually entered the structured context — the mapped classes /
        # datatype properties fetched above, plus metric hits (metrics are
        # answerable via the Tier-1 metric path). Feeding an UNMAPPED class's
        # aiContext would ground a business term to a class the LLM cannot query —
        # the glossary form of the unmapped-name leak the class / subClassOf-parent
        # / object-property gates already prevent. Intersect the raw hits with the
        # mapped URIs (no extra Neptune round-trip — the sets are already in hand).
        mapped_uris = {c["uri"] for c in classes} | {p["uri"] for p in properties}
        glossary_hits = [h for h in vector_hits if getattr(h, "uri", "") in mapped_uris or h.type == "metric"]
        glossary = await self._fetch_ai_context(glossary_hits, namespace, graph_iris=graph_iris)

        context = TBoxContext(
            classes=classes,
            properties=properties,
            object_properties=object_properties,
            metrics=metrics,
            glossary=glossary,
            token_estimate=self._estimate_tokens(classes, properties, metrics, object_properties),
            graph_iris=graph_iris,
        )

        if context.token_estimate > max_tokens:
            # Leave room for the steward annotations attached below; they are spent
            # classes-first, so table-level wording survives in large namespaces.
            context = self._truncate(context, int(max_tokens * (1 - _ANNOTATION_BUDGET_SHARE)))

        # Annotations are fetched for what survived truncation only, so the query
        # is bounded by the prompt rather than by the namespace.
        await self._attach_annotations(context, namespace, max_tokens=max_tokens, graph_iris=graph_iris)

        logger.info(
            "tbox_context_built",
            namespace=namespace,
            classes=len(context.classes),
            properties=len(context.properties),
            metrics=len(context.metrics),
            tokens=context.token_estimate,
        )
        # Dark-namespace canary: the structured-query context resolved to zero
        # classes. Routing still sent a structured question here, so Tier-2
        # will yield nothing and the router falls back to Tier-3 silently. With the
        # bridge above, a pre-marker namespace no longer trips this (its gate is
        # dropped), so reaching here with ``mapped=True`` means a genuinely empty
        # mapped schema (ontology with no R2RML-mapped classes) and with
        # ``mapped=False`` means the namespace has no classes at all. Surfaced so
        # the silent degradation is at least observable in logs.
        if not context.classes:
            # Message branches on ``mapped``: with the bridge, a zero-marker
            # (legacy/NA) namespace already had its gate dropped, so an empty
            # result there means the namespace has no classes at all —
            # re-induction guidance would be wrong. Only the gated (mapped=True)
            # case is a "no R2RML-mapped classes / may predate the marker" state.
            if mapped:
                detail = (
                    "0 coa:isMapped classes reachable — Tier-2 structured queries will "
                    "return no context (ontology has no R2RML-mapped classes; a namespace "
                    "predating the marker would instead take the legacy fallback)"
                )
            else:
                detail = (
                    "namespace has no coa:isMapped markers (legacy fallback active, gate "
                    "already dropped) yet resolved zero classes — it appears to have no "
                    "ontology classes at all; re-induction will not change this"
                )
            logger.warning("tbox_context_no_mapped_classes", namespace=namespace, detail=detail)
        return context

    async def _namespace_has_mapped_markers(self, namespace: str, graph_iris: list[str] | None = None) -> bool:
        """Probe whether the namespace has ANY ``coa:isMapped true`` marker.

        The legacy-namespace bridge: returns True when at least one mapped class
        exists (keep the strict gate), False when none do (drop the gate and
        expose all classes — the pre-marker fallback).

        Fail-safe default is **True** (keep the strict gate) on any error or when
        no graph template is configured: an unfiltered fallback exposes MORE (all
        classes, incl. unmapped/foundational) to the LLM prompt, so defaulting to
        the gate on uncertainty is the conservative choice — it never leaks
        unmapped class names on a transient probe failure. A genuinely-legacy
        namespace whose probe fails simply keeps returning no context (the prior
        behavior), which the dark-namespace canary already logs.
        """
        validate_namespace(namespace)
        if not self._graph_uri_template:
            return True

        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        if not _is_safe_sparql_uri(graph_uri_prefix):
            return True

        ask_sparql = f"""
        ASK {{
{graph_scoped_body(f"            ?c <{_IS_MAPPED_IRI}> true .", graph_uri_prefix, graph_iris)}
        }}
        """
        try:
            # GraphClient.ask returns a plain bool for SPARQL ASK.
            return bool(await self._graph.ask(ask_sparql))
        except Exception as e:
            logger.warning("tbox_ismapped_probe_failed", namespace=namespace, error=str(e))
            return True  # conservative: keep the gate on probe failure

    async def _try_full_namespace_context(
        self,
        namespace: str,
        mapped: bool = True,
        graph_iris: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        """For small namespaces, fetch ALL classes and properties.

        Returns None if namespace is too large (> _FULL_CONTEXT_CLASS_THRESHOLD)
        or if the fetch fails. This ensures complete ontology context for the LLM,
        eliminating vector-search misses that cause InternalError on valid queries.
        """
        validate_namespace(namespace)

        if not self._graph_uri_template:
            return None

        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        if not _is_safe_sparql_uri(graph_uri_prefix):
            return None

        # First: count classes to check threshold. When ``mapped`` (the namespace
        # has isMapped markers), counts only MAPPED classes — the ones we'll
        # actually expose — so the threshold reflects the size of the structured
        # schema the LLM sees. In the legacy fallback (``mapped`` False) the gate
        # is dropped and ALL classes count, matching the pre-filter behavior.
        # BRIDGE: this ``gate`` local is used in the count/classes/props queries
        # below. PRE-BRIDGE those queries inlined ``_IS_MAPPED_PATTERN`` directly;
        # to remove, drop this line and substitute ``_IS_MAPPED_PATTERN`` for every
        # ``{gate}`` and ``_MAPPED_PARENT_PATTERN`` for every ``{_parent_pattern(mapped)}``.
        gate = _class_gate(mapped)
        count_body = f"""            ?class a owl:Class .
            {gate}"""
        count_sparql = f"""
        SELECT (COUNT(DISTINCT ?class) AS ?cnt)
        WHERE {{
{graph_scoped_body(count_body, graph_uri_prefix, graph_iris)}
        }}
        """
        try:
            count_result = await self._graph.query(count_sparql)
            if not count_result:
                return None
            # Neptune client returns flat binding dicts: {"cnt": "75"}
            cnt_val = count_result[0].get("cnt", "0")
            class_count = int(cnt_val) if isinstance(cnt_val, (int, float)) else int(str(cnt_val))
            if class_count == 0 or class_count > _FULL_CONTEXT_CLASS_THRESHOLD:
                logger.info(
                    "full_context_skipped",
                    namespace=namespace,
                    class_count=class_count,
                    threshold=_FULL_CONTEXT_CLASS_THRESHOLD,
                )
                return None
        except Exception as e:
            logger.warning("full_context_count_failed", namespace=namespace, error=str(e))
            return None

        # Fetch ALL classes and their properties for this namespace. When
        # ``mapped``, ``gate`` (= _IS_MAPPED_PATTERN) is required (absent marker ==
        # class hidden) so unmapped (unstructured / foundational) classes never
        # enter the structured-query prompt; in the legacy fallback ``gate`` is
        # empty and all classes are fetched (pre-filter behavior).
        #
        # TWO queries, NOT one joined query. A single ``?class … OPTIONAL{{?property}}``
        # SELECT emits one row per (class, property) pair; with up to
        # _FULL_CONTEXT_CLASS_THRESHOLD(200) classes each averaging >10 datatype
        # properties, the joined cardinality exceeds LIMIT _SPARQL_RESULT_LIMIT
        # (2000) and — with no ORDER BY — whichever classes' rows land past the cut
        # VANISH ENTIRELY from the parsed result (a mapped class silently missing
        # from the prompt). Splitting guarantees the class list is complete
        # (bounded by the ≤200 threshold, so it can never hit the row cap); only
        # the secondary property list can still truncate, which merely drops some
        # column hints (the class is still present + answerable) and is logged.
        classes_body = f"""            ?class a owl:Class .
            {gate}
            ?class rdfs:label ?label .
            {_parent_pattern(mapped)}"""
        classes_sparql = f"""
        SELECT DISTINCT ?class ?label ?parentClass
        WHERE {{
{graph_scoped_body(classes_body, graph_uri_prefix, graph_iris)}
        }} LIMIT {_SPARQL_RESULT_LIMIT}
        """
        # Datatype properties of mapped classes. ``?class`` is gated on isMapped
        # here too: without it, a property whose domain is an UNMAPPED class would
        # make ``_parse_results`` add that unmapped class (with an empty label) to
        # the class dict — reintroducing the exact unmapped-class leak the feature
        # prevents. Restricted to owl:DatatypeProperty (range is xsd:*, never a
        # class IRI); object-property join paths are surfaced by
        # ``_fetch_object_properties`` (mapped-gated on both ends). The optional
        # ``?distinctValue`` carries per-column allowed-values (categorical enum
        # samples) into NL→SQL/SPARQL generation.
        props_body = f"""            ?class a owl:Class .
            {gate}
            ?property a owl:DatatypeProperty .
            ?property rdfs:domain ?class .
            ?property rdfs:label ?propLabel .
            ?property rdfs:range ?range .
            OPTIONAL {{ ?property <{_SCL_DISTINCT_VALUES}> ?distinctValue . }}"""
        props_sparql = f"""
        SELECT ?class ?property ?range ?propLabel ?distinctValue
        WHERE {{
{graph_scoped_body(props_body, graph_uri_prefix, graph_iris)}
        }} LIMIT {_SPARQL_RESULT_LIMIT}
        """

        try:
            class_results = await self._graph.query(classes_sparql)
            if not class_results:
                return None
            prop_results = await self._graph.query(props_sparql)
            if len(prop_results) >= _SPARQL_RESULT_LIMIT:
                logger.warning(
                    "full_context_properties_truncated",
                    namespace=namespace,
                    limit=_SPARQL_RESULT_LIMIT,
                    detail="datatype-property list hit the row cap; some column hints omitted (classes unaffected)",
                )
            # Class rows FIRST so every mapped class is registered with its label
            # and parent before property rows (which carry no label) are folded in;
            # _parse_results dedups classes by URI, so property rows only append
            # properties and never overwrite a class entry.
            classes, properties = self._parse_results(class_results + prop_results)
            logger.info(
                "full_namespace_context_loaded",
                namespace=namespace,
                classes=len(classes),
                properties=len(properties),
            )
            return classes, properties
        except Exception as e:
            logger.warning("full_context_fetch_failed", namespace=namespace, error=str(e))
            return None

    async def _fetch_object_properties(
        self,
        namespace: str,
        mapped: bool = True,
        graph_iris: list[str] | None = None,
        class_uris: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch ObjectProperties (FK join paths) for the namespace.

        ``class_uris`` are the classes already selected into this T-Box. Anchoring
        the query to them is both the performance fix (see ``domain_iris`` in
        ``object_properties_sparql``) and the relevant scope: a join path whose
        domain class never reaches the prompt names a class the writer cannot
        reference, so it spends LIMIT budget without adding a usable join.
        """
        if not self._graph_uri_template:
            return []

        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        if not _is_safe_sparql_uri(graph_uri_prefix):
            return []

        # When ``mapped``: only surface object-property edges where BOTH ends are
        # MAPPED classes (both required patterns). Tier-2 turns these into SQL
        # joins; an edge to an unmapped class can't be joined and would
        # reintroduce an unmapped class name into the prompt. Edges between
        # unmapped classes (e.g. unstructured<->unstructured) stay in the graph
        # for Tier-3 / grounding-traversal — just not structured-query
        # material. In the legacy fallback (``mapped`` False, no markers exist)
        # both end-gates are dropped so all join paths surface, matching the
        # pre-filter behavior.
        # BRIDGE: passing ``mapped_gate_iri=None`` is the legacy fallback described
        # above. To remove the bridge, pass ``_IS_MAPPED_IRI`` unconditionally —
        # that yields the pre-bridge query (both ends mapped-gated).
        #
        # The query text itself lives in ``query_utils.object_properties_sparql``
        # because the Tier-2 FK-traversal tool reads the same edges; keeping one
        # definition means a fix there (label handling, scoping, performance)
        # reaches both callers instead of one.
        #
        # ``graph_iris`` is the whole reason Spider-2-sized namespaces produced no
        # join paths: unbound, this is the most expensive query the builder issues
        # (two label lookups per edge, cluster-wide) and it was the first to
        # ReadTimeout. See the _MAX_GRAPHS block at module top.
        sparql = object_properties_sparql(
            graph_uri_prefix,
            limit=_OBJECT_PROPERTY_LIMIT,
            mapped_gate_iri=_IS_MAPPED_IRI if mapped else None,
            graph_iris=graph_iris,
            domain_iris=[u for u in (class_uris or []) if _is_safe_sparql_uri(u)][:_MAX_DOMAIN_ANCHOR_URIS],
        )
        try:
            results = await self._graph.query(sparql)
            obj_props = []
            for row in results:
                if row.get("op") and row.get("domain") and row.get("range"):
                    obj_props.append(
                        {
                            "uri": row["op"],
                            "label": row.get("opLabel", ""),
                            "domain": row["domain"],
                            "domain_label": row.get("domainLabel", ""),
                            "range_class": row["range"],
                            "range_label": row.get("rangeLabel", ""),
                        }
                    )
            return obj_props
        except Exception as e:
            logger.warning("object_property_fetch_failed", namespace=namespace, error=str(e))
            return []

    async def _fetch_ontology_context(
        self,
        ontology_hits: list[VectorHit],
        namespace: str,
        mapped: bool = True,
        graph_iris: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch full class/property definitions from Neptune for ontology vector hits."""
        validate_namespace(namespace)

        if not self._graph_uri_template:
            logger.warning("tbox_missing_graph_uri_template")
            return [], []

        class_uris = [h.uri for h in ontology_hits if h.uri and h.type == "ontology_class"]
        prop_uris = [h.uri for h in ontology_hits if h.uri and h.type == "ontology_property"]

        if not class_uris and not prop_uris:
            return [], []

        all_uris = (class_uris + prop_uris)[:_MAX_SPARQL_VALUES_URIS]

        if len(class_uris) + len(prop_uris) > _MAX_SPARQL_VALUES_URIS:
            logger.warning(
                "tbox_uri_list_truncated",
                total=len(class_uris) + len(prop_uris),
                limit=_MAX_SPARQL_VALUES_URIS,
                namespace=namespace,
            )

        safe_uris = [uri for uri in all_uris if _is_safe_sparql_uri(uri)]
        if not safe_uris:
            logger.warning("tbox_all_uris_rejected", namespace=namespace, count=len(all_uris))
            return [], []

        class_uri_set = set(class_uris)
        prop_uri_set = set(prop_uris)
        safe_class_uris = [u for u in safe_uris if u in class_uri_set]
        safe_prop_uris = [u for u in safe_uris if u in prop_uri_set]

        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        if not _is_safe_sparql_uri(graph_uri_prefix):
            logger.warning("tbox_unsafe_graph_uri_prefix", prefix=graph_uri_prefix, namespace=namespace)
            return [], []

        class_values = " ".join(f"(<{uri}>)" for uri in safe_class_uris) if safe_class_uris else ""
        prop_values = " ".join(f"(<{uri}>)" for uri in safe_prop_uris) if safe_prop_uris else ""

        # BRIDGE: PRE-BRIDGE the UNION clauses below inlined ``_IS_MAPPED_PATTERN``
        # and ``_MAPPED_PARENT_PATTERN`` directly; to remove, drop this local and
        # substitute those constants for ``{gate}`` / ``{_parent_pattern(mapped)}``.
        gate = _class_gate(mapped)
        union_clauses: list[str] = []
        if class_values:
            union_clauses.append(f"""
            {{
              VALUES (?class) {{ {class_values} }}
              ?class a owl:Class .
              {gate}
              ?class rdfs:label ?label .
              {_parent_pattern(mapped)}
              {_DATATYPE_PROPS_OPTIONAL}
            }}""")
        if prop_values:
            # Restricted to owl:DatatypeProperty for the same reason as the
            # class-driven OPTIONAL: a datatype property's range is an xsd:* term
            # (safe + useful column-type signal), while an object property's range
            # is a class IRI that, if unmapped, would leak an unanswerable class
            # name into the prompt. Object-property edges are surfaced separately
            # by ``_fetch_object_properties`` (mapped-gated on both ends).
            union_clauses.append(f"""
            {{
              VALUES (?property) {{ {prop_values} }}
              ?property a owl:DatatypeProperty .
              ?property rdfs:domain ?class .
              ?property rdfs:label ?propLabel .
              OPTIONAL {{ ?property rdfs:range ?range . }}
              OPTIONAL {{ ?property <{_SCL_DISTINCT_VALUES}> ?distinctValue . }}
              ?class a owl:Class .
              {gate}
              ?class rdfs:label ?label .
              {_parent_pattern(mapped)}
            }}""")

        sparql = f"""
        SELECT ?class ?property ?range ?label ?propLabel ?parentClass ?distinctValue
        WHERE {{
{graph_scoped_body(" UNION ".join(union_clauses), graph_uri_prefix, graph_iris)}
        }} LIMIT {_SPARQL_RESULT_LIMIT}
        """

        try:
            results = await self._graph.query(sparql)
        except Exception as e:
            logger.error("tbox_fetch_failed", namespace=namespace, error=str(e))
            return [], []

        return self._parse_results(results)

    async def _fetch_by_entities(
        self,
        query: str,
        namespace: str,
        mapped: bool = True,
        graph_iris: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Fallback: fetch ontology context by matching NL query entities against class labels."""
        validate_namespace(namespace)
        search_plan = build_query_search_plan(query, max_count=5)

        if not search_plan.terms and not search_plan.containers:
            logger.info("tbox_entity_fetch_no_search_candidates", namespace=namespace, query_length=len(query))
            return [], []

        if not self._graph_uri_template:
            logger.warning("tbox_missing_graph_uri_template")
            return [], []

        forward_filters = [
            f'CONTAINS(LCASE(?label), "{escape_sparql_string_literal(normalize_label_match_text(term))}")'
            for term in search_plan.terms
        ]
        # ``STR()`` is required on the reverse arm and not on the forward one:
        # SPARQL argument compatibility accepts CONTAINS(lang-tagged, simple) but
        # rejects CONTAINS(simple, lang-tagged) as a type error, which FILTER then
        # swallows as false. Without STR() every ``@ja``/``@th`` label would be
        # invisible to reverse containment. STRLEN keeps one-character labels from
        # matching any query that happens to contain that character.
        reverse_filters = [
            "(STRLEN(STR(?label)) >= 2 && "
            f'CONTAINS("{escape_sparql_string_literal(normalize_label_match_text(container))}", '
            "LCASE(STR(?label))))"
            for container in search_plan.containers
        ]
        filters = " || ".join(forward_filters + reverse_filters)
        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        if not _is_safe_sparql_uri(graph_uri_prefix):
            logger.warning("tbox_unsafe_graph_uri_prefix", prefix=graph_uri_prefix)
            return [], []

        # BRIDGE: PRE-BRIDGE, ``{_class_gate(mapped)}`` was ``{_IS_MAPPED_PATTERN}``
        # and ``{_parent_pattern(mapped)}`` was ``{_MAPPED_PARENT_PATTERN}`` (both
        # unconditional).
        #
        # Split into a class query and a property query for the same reason
        # ``_try_full_namespace_context`` is split: joined against the datatype
        # OPTIONAL, one class fans out to (properties x distinct values x parents x
        # labels) rows, and with no ORDER BY a class whose rows all land past
        # ``_SPARQL_RESULT_LIMIT`` vanishes entirely from the parsed result — a
        # matched class silently missing from the structured prompt. Reverse
        # containment made that reachable: it matches far more classes than a
        # handful of short forward terms did, and a multilingual namespace carries
        # one label per language per class. DISTINCT plus the bounded class list
        # keeps the class query one row per (class, label, parent).
        entity_classes_body = f"""            ?class a owl:Class .
            {_class_gate(mapped)}
            ?class rdfs:label ?label .
            FILTER({filters})
            {_parent_pattern(mapped)}"""
        classes_sparql = f"""
        SELECT DISTINCT ?class ?label ?parentClass
        WHERE {{
{graph_scoped_body(entity_classes_body, graph_uri_prefix, graph_iris)}
        }} LIMIT {_SPARQL_RESULT_LIMIT}
        """

        try:
            class_results = await self._graph.query(classes_sparql)
        except Exception as e:
            logger.error("tbox_entity_fetch_failed", namespace=namespace, error=str(e))
            return [], []

        if not class_results:
            return [], []

        if len(class_results) >= _SPARQL_RESULT_LIMIT:
            # The class query is one row per (class, label, parent) and is not
            # ordered, so past the cap it is Neptune's row order that decides which
            # matched classes reach the prompt. Reachable on a large multilingual
            # namespace, and silent until now.
            logger.warning(
                "tbox_entity_classes_truncated",
                namespace=namespace,
                limit=_SPARQL_RESULT_LIMIT,
                detail="matched-class rows hit the row cap; some matched classes may be missing from the prompt",
            )

        # De-duplicate before the VALUES cap. The class query is DISTINCT over
        # (?class, ?label, ?parentClass), so a class carrying several language
        # labels — exactly the namespaces this MR serves — contributes one row per
        # label. Capping the raw rows let 50 VALUES entries cover far fewer distinct
        # classes and left the rest with no column hints, and the duplicates
        # multiplied the property rows as well.
        matched_uris = list(
            dict.fromkeys(
                row["class"] for row in class_results if row.get("class") and _is_safe_sparql_uri(row["class"])
            )
        )
        if not matched_uris:
            return self._parse_results(class_results)

        if len(matched_uris) > _MAX_SPARQL_VALUES_URIS:
            logger.info(
                "tbox_entity_property_uris_truncated",
                namespace=namespace,
                matched=len(matched_uris),
                limit=_MAX_SPARQL_VALUES_URIS,
                detail="column hints fetched for the first N matched classes; all matched classes still surface",
            )

        # Properties are fetched for the classes the label match already selected,
        # so this query cannot introduce a class the gate above rejected. Bounded
        # by the same VALUES cap the vector-hit path uses.
        class_values = " ".join(f"(<{uri}>)" for uri in matched_uris[:_MAX_SPARQL_VALUES_URIS])
        entity_props_body = f"""            VALUES (?class) {{ {class_values} }}
            ?property a owl:DatatypeProperty .
            ?property rdfs:domain ?class .
            ?property rdfs:label ?propLabel .
            ?property rdfs:range ?range .
            OPTIONAL {{ ?property <{_SCL_DISTINCT_VALUES}> ?distinctValue . }}"""
        props_sparql = f"""
        SELECT ?class ?property ?range ?propLabel ?distinctValue
        WHERE {{
{graph_scoped_body(entity_props_body, graph_uri_prefix, graph_iris)}
        }} LIMIT {_SPARQL_RESULT_LIMIT}
        """

        try:
            prop_results = await self._graph.query(props_sparql)
        except Exception as e:
            # A property-list failure only costs column hints; the matched classes
            # are already answerable, so degrade rather than drop them.
            logger.warning("tbox_entity_property_fetch_failed", namespace=namespace, error=str(e))
            prop_results = []

        if len(prop_results) >= _SPARQL_RESULT_LIMIT:
            logger.warning(
                "tbox_entity_properties_truncated",
                namespace=namespace,
                limit=_SPARQL_RESULT_LIMIT,
                detail="datatype-property list hit the row cap; some column hints omitted (classes unaffected)",
            )

        # Class rows FIRST so every matched class is registered with its label and
        # parent before property rows (which carry no label) are folded in.
        return self._parse_results(class_results + prop_results)

    async def _fetch_ai_context(
        self, vector_hits: list[VectorHit], namespace: str, graph_iris: list[str] | None = None
    ) -> list[AiContextTerm]:
        """Fetch :aiContext JSON literals from Neptune for the hit nodes.

        ``:aiContext`` carries business synonyms + instructions authored per
        ontology/metric node. We batch-query the literal for the hit URIs and
        parse synonyms/instructions into AiContextTerm entries.

        Fail-open: any error, missing template, or no safe URIs returns [] so
        translation proceeds without a glossary (never raises).
        """
        uris = [h.uri for h in vector_hits if getattr(h, "uri", "") and _is_safe_sparql_uri(h.uri)]
        if not uris or not self._graph_uri_template:
            return []
        uris = uris[:_MAX_SPARQL_VALUES_URIS]
        try:
            validate_namespace(namespace)
        except Exception:
            return []

        # Scope to the namespace's graph(s) so we never read another namespace's
        # :aiContext (matches the _fetch_by_entities graph-prefix filter pattern).
        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        if not _is_safe_sparql_uri(graph_uri_prefix):
            logger.warning("aicontext_unsafe_graph_uri_prefix", prefix=graph_uri_prefix)
            return []

        values_clause = " ".join(f"(<{u}>)" for u in uris)
        aicontext_body = f"""            VALUES (?s) {{ {values_clause} }}
            ?s <urn:{URN_PREFIX}:vocab#aiContext> ?aiContext .
            OPTIONAL {{ ?s rdfs:label ?label . }}"""
        sparql = f"""
        SELECT ?s ?label ?aiContext
        WHERE {{
{graph_scoped_body(aicontext_body, graph_uri_prefix, graph_iris)}
        }} LIMIT {_SPARQL_RESULT_LIMIT}
        """
        try:
            results = await self._graph.query(sparql)
        except Exception as e:
            logger.warning("aicontext_fetch_failed", namespace=namespace, error=str(e))
            return []

        return self._parse_ai_context(results)

    @staticmethod
    def _parse_ai_context(results: list[dict[str, Any]]) -> list[AiContextTerm]:
        """Parse :aiContext JSON literals into AiContextTerm entries."""
        import json as _json

        terms: list[AiContextTerm] = []
        for row in results:
            raw = row.get("aiContext", "")
            if not raw:
                continue
            try:
                ctx = _json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(ctx, dict):
                continue
            synonyms = ctx.get("synonyms", [])
            if not isinstance(synonyms, list):
                synonyms = []
            instructions = ctx.get("instructions", "")
            if not isinstance(instructions, str):
                instructions = ""
            label = row.get("label", "") or ctx.get("label", "")
            if synonyms or instructions:
                terms.append(
                    AiContextTerm(
                        label=str(label),
                        synonyms=[str(s) for s in synonyms],
                        instructions=instructions,
                    )
                )
        return terms

    def _build_metric_context(self, metric_hits: list[VectorHit]) -> list[MetricContext]:
        """Build metric context for the prompt from metric vector hits.

        when a Tier-1 MetricResolver is available, enrich each hit with
        the FULL Neptune-loaded :GovernedMetric definition (matched by metric_id
        or name); fall back to the sparse vector-hit metadata when the resolver
        has no entry (e.g. a hit that isn't a governed metric, or resolver not
        yet loaded). Never raises — enrichment is best-effort.
        """
        metrics = []
        for hit in metric_hits:
            meta = hit.metadata
            name = meta.get("name", hit.entity_id)
            description = meta.get("description", "")
            formula = meta.get("formula", "")
            dimensions = meta.get("dimensions", [])

            defn = self._resolve_full_metric(hit, name)
            if defn is not None:
                # Prefer the authoritative Neptune definition; keep hit metadata
                # only where the resolver leaves a field empty.
                name = defn.name or name
                description = defn.description or description
                formula = defn.sql_template or formula
                dimensions = defn.dimensions or dimensions

            metrics.append(MetricContext(name=name, description=description, formula=formula, dimensions=dimensions))
        return metrics

    def _resolve_full_metric(self, hit: VectorHit, name: str):
        """Look up the full metric definition for a hit (by id then name). Best-effort."""
        if self._metric_resolver is None:
            return None
        try:
            entity_id = hit.metadata.get("metric_id") or hit.entity_id
            defn = self._metric_resolver.lookup(entity_id) if entity_id else None
            if defn is None and name:
                # Fall back to name-keyed lookup via the resolver's snapshot.
                defn = self._metric_resolver._snapshot.by_name.get(name.lower())
            return defn
        except Exception:
            return None

    async def _fetch_annotations(
        self,
        subject_uris: list[str],
        namespace: str,
        graph_iris: list[str] | None = None,
    ) -> dict[str, dict[str, list[str]]]:
        """Fetch the steward-reviewed annotations of exactly ``subject_uris``.

        ``subject_uris`` are the terms in the prompt, classes first, so if anything
        is lost it is the least useful part. One query per
        :data:`_ANNOTATION_SUBJECT_CHUNK` subjects, at most
        :data:`_ANNOTATION_MAX_CONCURRENCY` in flight. Best-effort, like the
        glossary fetch: a failed chunk only means those terms reach the prompt
        without annotations.

        Returns:
            ``{subject_uri: {key: [values]}}`` with keys from :data:`_ANNOTATION_KEYS`.
        """
        if not self._graph_uri_template:
            return {}
        graph_uri_prefix = namespace_graph_prefix(self._graph_uri_template, namespace)
        if not _is_safe_sparql_uri(graph_uri_prefix):
            return {}
        safe = [u for u in dict.fromkeys(subject_uris) if _is_safe_sparql_uri(u)]
        if not safe:
            return {}
        predicate_values = " ".join(f"<{p}>" for p in _ANNOTATION_KEYS)
        semaphore = asyncio.Semaphore(_ANNOTATION_MAX_CONCURRENCY)

        async def _chunk(uris: list[str]) -> list[dict[str, Any]]:
            values = " ".join(f"<{u}>" for u in uris)
            body = f"""            VALUES ?s {{ {values} }}
            VALUES ?p {{ {predicate_values} }}
            ?s ?p ?o ."""
            sparql = f"""
        SELECT ?s ?p ?o
        WHERE {{
{graph_scoped_body(body, graph_uri_prefix, graph_iris)}
        }} LIMIT {_SPARQL_RESULT_LIMIT}
        """
            async with semaphore:
                try:
                    rows = await self._graph.query(sparql)
                except Exception as e:
                    logger.warning("tbox_annotations_fetch_failed", namespace=namespace, error=str(e))
                    return []
            if len(rows) >= _SPARQL_RESULT_LIMIT:
                logger.warning(
                    "tbox_annotations_truncated",
                    namespace=namespace,
                    subjects=len(uris),
                    limit=_SPARQL_RESULT_LIMIT,
                    detail="annotation rows hit the cap; some descriptions/synonyms omitted",
                )
            return rows

        started = time.perf_counter()
        chunks = [safe[i : i + _ANNOTATION_SUBJECT_CHUNK] for i in range(0, len(safe), _ANNOTATION_SUBJECT_CHUNK)]
        results = await asyncio.gather(*(_chunk(c) for c in chunks))
        logger.info(
            "tbox_annotations_fetched",
            namespace=namespace,
            subjects=len(safe),
            queries=len(chunks),
            rows=sum(len(r) for r in results),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )

        annotations: dict[str, dict[str, list[str]]] = {}
        for rows in results:
            for row in rows:
                subject, key, value = row.get("s"), _ANNOTATION_KEYS.get(row.get("p", "")), row.get("o")
                # Collapse whitespace: the prompt is one term per line, so a
                # newline inside a description would start a fake schema line.
                text = " ".join(str(value).split()) if value is not None else ""
                if not subject or not key or not text:
                    continue
                bucket = annotations.setdefault(subject, {}).setdefault(key, [])
                if text not in bucket:
                    bucket.append(text)
        return annotations

    async def _attach_annotations(
        self,
        context: TBoxContext,
        namespace: str,
        max_tokens: int,
        graph_iris: list[str] | None = None,
    ) -> None:
        """Copy the fetched annotations onto the context's terms, within the token budget.

        Runs after ``_truncate``, so it may only spend what the truncated context left
        of ``max_tokens``. Spent in the truncation's own priority: classes, then
        datatype properties, then join paths. A term whose annotations do not fit is
        left as it was (still queryable, just without them). Values are sorted so the
        prompt is stable across builds (Neptune returns rows unordered).
        """
        subjects = [
            item["uri"]
            for items in (context.classes, context.properties, context.object_properties)
            for item in items
            if item.get("uri")
        ]
        if not subjects:
            return
        annotations = await self._fetch_annotations(subjects, namespace, graph_iris=graph_iris)
        if not annotations:
            return
        remaining = (max_tokens - context.token_estimate) * _CHARS_PER_TOKEN
        groups = (
            (context.classes, _MAX_CLASS_DESCRIPTION_CHARS),
            (context.properties, _MAX_PROPERTY_DESCRIPTION_CHARS),
            (context.object_properties, _MAX_PROPERTY_DESCRIPTION_CHARS),
        )
        added_chars = skipped = 0
        for items, max_description in groups:
            for item in items:
                found = annotations.get(item.get("uri", ""))
                if not found:
                    continue
                candidate: dict[str, Any] = {}
                descriptions = sorted(found.get("description", []))
                if descriptions:
                    candidate["description"] = " ".join(descriptions)
                for key in ("synonyms", "glossary_terms", "tags"):
                    values = sorted(found.get(key, []))
                    if values:
                        candidate[key] = values
                cost = len(_render_annotations(candidate, max_description))
                if cost > remaining:
                    skipped += 1
                    continue
                item.update(candidate)
                remaining -= cost
                added_chars += cost
        context.token_estimate += added_chars // _CHARS_PER_TOKEN
        if skipped:
            logger.warning(
                "tbox_annotations_over_budget",
                namespace=namespace,
                skipped_terms=skipped,
                max_tokens=max_tokens,
                detail="token budget exhausted; these terms reach the prompt without steward annotations",
            )

    def _parse_results(self, results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Parse Neptune SPARQL results into classes and properties.

        A property may span multiple result rows when it carries several
        coa:distinctValues (one row per value). We key properties by
        (domain, uri) and accumulate distinct sample values so the property
        appears once with all its sampled enum literals.
        """
        classes: dict[str, dict[str, Any]] = {}
        properties: dict[tuple[str, str], dict[str, Any]] = {}

        for row in results:
            cls_uri = row.get("class")
            if cls_uri and cls_uri not in classes:
                classes[cls_uri] = {
                    "uri": cls_uri,
                    "label": row.get("label", ""),
                    "parent": row.get("parentClass"),
                }
            prop_uri = row.get("property")
            if prop_uri and cls_uri:
                key = (cls_uri, prop_uri)
                prop = properties.get(key)
                if prop is None:
                    prop = {
                        "uri": prop_uri,
                        "label": row.get("propLabel", ""),
                        "domain": cls_uri,
                        "range": row.get("range", ""),
                        "distinct_values": [],
                    }
                    properties[key] = prop
                sample = row.get("distinctValue")
                if sample and sample not in prop["distinct_values"]:
                    prop["distinct_values"].append(str(sample))

        return list(classes.values()), list(properties.values())

    def _estimate_tokens(
        self,
        classes: list[dict[str, Any]],
        properties: list[dict[str, Any]],
        metrics: list[MetricContext],
        object_properties: list[dict[str, Any]] | None = None,
    ) -> int:
        """Estimate token count for the T-Box context."""
        return (
            (len(classes) * _TOKENS_PER_CLASS)
            + (len(properties) * _TOKENS_PER_PROPERTY)
            + (len(object_properties or []) * _TOKENS_PER_OBJECT_PROPERTY)
            + (len(metrics) * _TOKENS_PER_METRIC)
        )

    def _truncate(self, context: TBoxContext, max_tokens: int) -> TBoxContext:
        """Trim context to the token budget, spending the budget on classes first.

        Priority is metrics > classes > (properties, object properties): a class
        missing from the prompt cannot be queried at all, whereas a class that
        arrives with only some of its column hints or join paths is still
        answerable. So classes are charged before either hint list and cut only
        when they alone overrun.

        This replaces a single proportional ratio applied to all four lists. That
        ratio was set by whichever list dominates the estimate — datatype
        properties, at ~10 per class — so property volume alone silently removed
        mapped classes: on a 75-class namespace the class list arrived at 60.
        Nothing here is ordered by relevance (the full-namespace path runs no
        vector search and Neptune returns rows unordered), so a prefix slice drops
        classes arbitrarily rather than cheaply. For the same reason the surviving
        property budget is spread ROUND-ROBIN across domain classes: a flat slice
        in Neptune's row order can hand one class forty columns and the next none,
        which reads to the LLM as a class with no queryable columns.

        Both cuts floor at zero: ``max_tokens`` below what the metrics alone cost
        drives the remaining budget negative, and a negative slice index keeps the
        TAIL of a list instead of emptying it.
        """
        classes = context.classes
        metrics = context.metrics
        budget = max_tokens - (len(metrics) * _TOKENS_PER_METRIC)
        max_classes = max(budget // _TOKENS_PER_CLASS, 1)
        if len(classes) > max_classes:
            # Only reachable when the class list alone overruns the budget, i.e.
            # a namespace far past _FULL_CONTEXT_CLASS_THRESHOLD. Never silent:
            # everything downstream (validation, compilation) will fail on a
            # class the prompt never showed.
            logger.warning(
                "tbox_classes_truncated",
                classes=len(classes),
                kept=max_classes,
                max_tokens=max_tokens,
                detail="token budget cannot hold the mapped class list; dropped classes are unqueryable",
            )
            classes = classes[:max_classes]
        budget -= len(classes) * _TOKENS_PER_CLASS

        # Split what is left between the two hint lists in their original
        # proportion, so neither is starved by the other's volume.
        hint_tokens = (len(context.properties) * _TOKENS_PER_PROPERTY) + (
            len(context.object_properties) * _TOKENS_PER_OBJECT_PROPERTY
        )
        ratio = (budget / hint_tokens) if hint_tokens > 0 else 0.0
        # Clamp BOTH ends. The upper clamp is the ordinary "budget is roomier
        # than the lists" case; the lower one matters because ``budget`` can be
        # negative when the metrics alone exceed max_tokens, and int(len * -x) is
        # a negative slice index — ``object_properties[:-2]`` keeps all but two
        # rather than none.
        ratio = min(max(ratio, 0.0), 1.0)
        properties = self._spread_properties(context.properties, int(len(context.properties) * ratio))
        object_properties = context.object_properties[: int(len(context.object_properties) * ratio)]
        return TBoxContext(
            classes=classes,
            properties=properties,
            object_properties=object_properties,
            metrics=metrics,
            glossary=context.glossary,  # glossary is small; keep it intact on truncation
            token_estimate=self._estimate_tokens(classes, properties, metrics, object_properties),
            graph_iris=context.graph_iris,  # a prompt budget does not change which graphs exist
        )

    @staticmethod
    def _spread_properties(properties: list[dict[str, Any]], keep: int) -> list[dict[str, Any]]:
        """Keep ``keep`` properties spread evenly across their domain classes.

        One round per pass over the classes in first-appearance order, so every
        class keeps its first column hint before any class keeps its second. The
        output is regrouped by class afterwards, because ``format_for_prompt``
        renders properties per class and interleaved rows would read as noise.
        """
        if keep >= len(properties):
            return properties
        if keep <= 0:
            return []
        by_domain: dict[str, list[dict[str, Any]]] = {}
        for prop in properties:
            by_domain.setdefault(str(prop.get("domain", "")), []).append(prop)
        kept: dict[str, list[dict[str, Any]]] = {domain: [] for domain in by_domain}
        remaining = keep
        depth = 0
        while remaining > 0:
            progressed = False
            for domain, props in by_domain.items():
                if depth >= len(props):
                    continue
                kept[domain].append(props[depth])
                progressed = True
                remaining -= 1
                if remaining == 0:
                    break
            if not progressed:
                break
            depth += 1
        return [prop for domain in by_domain for prop in kept[domain]]

    def format_for_prompt(self, context: TBoxContext, namespace: str) -> str:
        """Format TBoxContext as text for inclusion in an LLM prompt.

        Uses prefixed short forms (ind:ClassName) so the LLM generates SPARQL
        with correct URIs instead of inventing its own prefix.
        """
        prefix_uri = ""
        if context.classes and context.classes[0].get("uri"):
            uri = context.classes[0]["uri"]
            prefix_uri = uri.rsplit("#", 1)[0] + "#" if "#" in uri else uri.rsplit("/", 1)[0] + "/"

        _XSD = "http://www.w3.org/2001/XMLSchema#"

        def _shorten(uri: str) -> str:
            if prefix_uri and uri.startswith(prefix_uri):
                return f"ind:{uri[len(prefix_uri) :]}"
            if uri.startswith(_XSD):
                return f"xsd:{uri[len(_XSD) :]}"
            return f"<{uri}>"

        sections = [f"Ontology namespace: {namespace}"]
        if prefix_uri:
            sections.append(f"PREFIX ind: <{prefix_uri}>")
            sections.append(f"PREFIX xsd: <{_XSD}>")
            sections.append("")

        if context.classes:
            sections.append("Classes:")
            for c in context.classes:
                short = _shorten(c["uri"])
                line = f" {short} (label: {c['label']})"
                if c.get("parent"):
                    line += f" subClassOf: {_shorten(c['parent'])}"
                line += _render_annotations(c, _MAX_CLASS_DESCRIPTION_CHARS)
                sections.append(line)

        if context.properties:
            sections.append("\nProperties:")
            for p in context.properties:
                domain = _shorten(p["domain"])
                rng = _shorten(p["range"])
                line = f" {_shorten(p['uri'])} (label: {p['label']}) domain: {domain} range: {rng}"
                # Sampled distinct values for categorical columns — tells the LLM
                # the EXACT literals to use in FILTER/WHERE clauses instead of
                # guessing (e.g. return_state = "RETURNED", not "returned").
                distinct_values = p.get("distinct_values") or []
                if distinct_values:
                    shown = ", ".join(f'"{v}"' for v in distinct_values[:_MAX_DISTINCT_VALUES_IN_PROMPT])
                    line += f" allowed values: [{shown}]"
                line += _render_annotations(p, _MAX_PROPERTY_DESCRIPTION_CHARS)
                sections.append(line)

        if context.object_properties:
            sections.append("\nJoin paths (ObjectProperties — use these for multi-table queries):")
            for op in context.object_properties:
                domain_short = _shorten(op["domain"]) if op.get("domain") else "?"
                range_short = _shorten(op["range_class"]) if op.get("range_class") else "?"
                op_short = _shorten(op["uri"])
                domain_label = op.get("domain_label", "")
                range_label = op.get("range_label", "")
                label_hint = ""
                if domain_label and range_label:
                    label_hint = f" ({domain_label} → {range_label})"
                sections.append(
                    f" {domain_short} → {range_short} via {op_short}{label_hint}"
                    + _render_annotations(op, _MAX_PROPERTY_DESCRIPTION_CHARS)
                )

        if context.metrics:
            sections.append("\nMetrics:")
            for m in context.metrics:
                line = f" Metric: {m.name}"
                if m.description:
                    line += f" — {m.description}"
                if m.formula:
                    line += f" [formula: {m.formula}]"
                if m.dimensions:
                    line += f" [dimensions: {', '.join(m.dimensions)}]"
                sections.append(line)

        # glossary grounds business terms to the ontology. Synonyms tell
        # the LLM which term maps to which class/property; instructions carry
        # author guidance (e.g. which field to prefer).
        if context.glossary:
            sections.append("\nGlossary (business terms → ontology):")
            for term in context.glossary:
                parts: list[str] = []
                if term.label:
                    parts.append(term.label)
                if term.synonyms:
                    parts.append(f"synonyms: {', '.join(term.synonyms)}")
                if term.instructions:
                    parts.append(f"note: {term.instructions}")
                if parts:
                    sections.append(f" - {' | '.join(parts)}")

        return "\n".join(sections)
