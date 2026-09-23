# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 1 Metric Resolver — in-memory metric definition cache.

Builds in-memory indexes (by_id, by_name, by_synonym) for exact
name/synonym matching during query routing, with a fuzzy near-miss
fallback (typos/plurals; confidence < 1.0) when exact matching misses.

Loads metric definitions from Neptune at startup and refreshes
periodically (every REFRESH_INTERVAL_S seconds) so newly-created
metrics become routable without a service restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

import sqlglot
import structlog
from coa_common.constants import URN_PREFIX as _URN_PREFIX

from coa_serve.query_utils import get_graph_uri_template, is_unspaced_script_letter, iter_word_tokens
from coa_serve.tier1.stopwords import RESIDUAL_STOP_WORDS

if TYPE_CHECKING:
    from coa_serve.clients.neptune import NeptuneGraphClient


class MetricSeedItem(TypedDict, total=False):
    """Expected shape of a metric seed data entry."""

    metric_id: str  # required
    name: str  # required
    display_name: str
    description: str
    sql_template: str
    dimensions: list[str]
    synonyms: list[str]
    namespace: str
    data_source_id: str
    columns: list[str]


logger = structlog.get_logger(__name__)


@dataclass
class MetricDefinition:
    """A single metric definition from the metric catalog."""

    metric_id: str
    name: str
    display_name: str = ""
    description: str = ""
    sql_template: str = ""
    dimensions: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    namespace: str = ""
    data_source_id: str = ""
    columns: list[str] = field(default_factory=list)

    @property
    def compact_summary(self) -> dict[str, Any]:
        """Return a compact summary for Tier 3 context."""
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "dimensions": self.dimensions,
        }


@dataclass
class _IndexSnapshot:
    """Immutable snapshot of all in-memory indexes for atomic swap."""

    by_id: dict[str, MetricDefinition]
    by_name: dict[str, list[MetricDefinition]]
    by_synonym: dict[str, list[MetricDefinition]]
    by_namespace: dict[str, list[MetricDefinition]]
    name_patterns: dict[str, list[tuple[re.Pattern, MetricDefinition]]] = field(default_factory=dict)
    synonym_patterns: dict[str, list[tuple[re.Pattern, MetricDefinition]]] = field(default_factory=dict)


@dataclass
class MetricMatch:
    """Result of a metric resolution attempt."""

    found: bool
    metric_id: str = ""
    metric_name: str = ""
    sql_template: str = ""
    dimensions: list[str] = field(default_factory=list)
    data_source_id: str = ""
    columns: list[str] = field(default_factory=list)
    match_source: str = ""  # "name", "synonym", or "fuzzy"
    match_count: int = 0  # number of distinct metrics matched
    match_confidence: float = 1.0  # 1.0 for exact name/synonym; <1.0 for fuzzy near-miss
    matched_text: str = ""  # the name/synonym span the query matched on
    residual: str = ""  # question words left over after the matched span + stop-words


REFRESH_INTERVAL_S = 30
"""Seconds between Neptune metric refresh polls."""

_METRIC_QUERY_MAX_RESULTS = 5000
"""Max metrics fetched per Neptune refresh (MVP volume assumption)."""

# Named dimension placeholders in a metric SQL template: ``:region`` or ``{region}``.
# The ``:`` form requires a non-``:`` char before it (so a Postgres ``::type`` cast
# is not mistaken for a placeholder); the ``{}`` form is unambiguous.
_PLACEHOLDER_RE = re.compile(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)|\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _fold(text: str) -> str:
    """NFKC-normalize and lowercase ``text`` for metric-index matching.

    Japanese input routinely mixes full- and half-width forms (``ＫＰＩ``,
    ``ｋＷｈ``, full-width digits); NFKC folds these onto the code points the
    stored names use. Unlike ``normalize_label_match_text`` — which must stay
    NFKC-free because it is compared against RDF labels persisted verbatim —
    both sides here (the in-memory index at build time, the query at match
    time) pass through this same function, so compatibility normalization is
    symmetric and cannot un-match a stored spelling.

    ``lower()`` (not ``casefold()``) deliberately, to stay consistent with the
    dimension path's lower/casefold invariant (see ``substitute_dimensions``).
    """
    return unicodedata.normalize("NFKC", text).lower()


# ── Residual-qualifier detection ─────────────────────────────────────────────
#
# A Tier-1 match is a whole-question substring search: a metric whose synonym is
# a common noun ("revenue") matches ANY question containing that word, and the
# metric's UNFILTERED SQL is then executed verbatim — nothing in Tier-1 parses a
# filter, group-by, or time window out of the natural language. Without this
# gate, "what is revenue?" and "what is revenue for the Gold tier?" returned
# byte-identical rows at confidence 1.0, with no signal that the qualifier was
# dropped.
#
# The gate: after a name/synonym hit, strip the matched span and a closed list of
# question scaffolding. Anything LEFT OVER is a qualifier Tier-1 cannot honour, so
# Tier-1 declines the question and the cascade falls through to Tier 2, which can
# express predicates. Tier-1 keeps its deterministic-1.0 guarantee precisely
# because it now only claims questions it fully consumes.
#
# Deliberately a CLOSED stop-word list, not an LLM or POS tagger: Tier-1's whole
# value is being deterministic and sub-millisecond. The failure mode of an
# unlisted-but-harmless word is a fall-through to Tier 2 (a slower, still-correct
# answer), which is strictly safer than the silent wrong answer it replaces.
# The word lists themselves live in per-language modules — see
# coa_serve.tier1.stopwords for the curation policy (what must NEVER be
# listed) and the guide for adding a language.
_RESIDUAL_STOP_WORDS = RESIDUAL_STOP_WORDS

# Tokenization for the residual gate is the shared script-run segmentation
# (``iter_word_tokens``): Latin/digit runs keep hyphenated/possessive qualifiers
# intact, and unsegmented scripts yield one run per script stretch, so a Japanese
# particle (hiragana) separates from the content word (kanji/katakana) it
# follows. Tokenizing on ``[a-z0-9_]`` alone made every non-ASCII qualifier
# invisible: the gate saw an empty residual and Tier-1 answered the UNFILTERED
# question at confidence 1.0 — the exact silent-wrong-answer this gate exists to
# prevent, reintroduced for every non-English question (GitHub issue #95; the
# Hangul-range widening that first fixed Korean is subsumed by the script runs).

# The hiragana block (U+3041–U+309F). NOT the katakana prolonged-sound mark:
# katakana runs are content words and must never enter the decomposition below.
_HIRAGANA_RUN_RE = re.compile(r"[\u3041-\u309f]+")

# Ceiling on the hiragana run the decomposition below will attempt. The DP is
# O(n²) over a token the caller controls (`serve.smithy` puts no @length on the
# query), so an unbounded run is a CPU sink on one serve worker. A constant, not
# a tuned bound: no polite-form scaffolding run comes anywhere near it
# (「についてはどのくらいになっていますでしょうか」 is 22). Raise it if a real
# scaffolding run ever exceeds it; an over-long run survives as residual and
# trips the gate — the safe direction.
_MAX_SCAFFOLDING_RUN = 64


def _is_stop_scaffolding(token: str) -> bool:
    """Return whether a residual ``token`` is question scaffolding.

    Exact stop-word membership first. A HIRAGANA-ONLY token additionally passes
    when it can be segmented entirely into stop-word pieces: contiguous hiragana
    fuses into ONE word run under script-run tokenization (「はいくらですか」 is
    a single token = は+いくら+ですか), so exact membership alone would demote
    every politely-phrased Japanese question to Tier 2.

    Restricted to hiragana-only tokens on purpose. Hiragana carries Japanese
    grammar; the qualifier classes this gate exists to catch — dimension values,
    entity names, time windows, aggregate modifiers — are written with kanji,
    katakana, Latin, or digits, which this decomposition never touches. A
    hiragana content word that is NOT fully composed of listed
    particles/copula fragments (きのう, いま, すべて) still fails the
    decomposition and trips the gate — the safe direction.
    """
    if token in _RESIDUAL_STOP_WORDS:
        return True
    if not _HIRAGANA_RUN_RE.fullmatch(token):
        return False
    if len(token) > _MAX_SCAFFOLDING_RUN:
        return False
    # DP over prefix positions: reachable[i] ⇔ token[:i] splits into stop words.
    reachable = [True] + [False] * len(token)
    for i in range(len(token)):
        if not reachable[i]:
            continue
        for j in range(i + 1, len(token) + 1):
            if token[i:j] in _RESIDUAL_STOP_WORDS:
                reachable[j] = True
    return reachable[len(token)]


_FUZZY_MATCH_THRESHOLD = 0.88
"""Minimum SequenceMatcher ratio for a fuzzy metric near-miss to be accepted.

High by design: fuzzy matching is a typo/plural/abbreviation safety net, NOT a
semantic search (that's the downstream tiers' vector job). A fuzzy hit returns
confidence < 1.0 so the deterministic confidence=1.0 guarantee is reserved for
exact matches.

Why 0.88 (review ask): SequenceMatcher ratio = 2*matches/total_chars. On typical
metric-name tokens (>=8 chars) a single-character edit scores ~0.92-0.96
("total_revenu" vs "total_revenue" = 0.96) and plural/abbreviation variants land
~0.90-0.95 — all admitted. Semantically different lookalikes fall below it
("total_revenue" vs "total_reserve" = 0.77) — rejected. 0.88 is the conservative
midpoint: it admits one-edit near-misses on realistic name lengths while
rejecting cross-metric confusion; on very short names (<8 chars) a single edit
scores lower and is intentionally NOT matched (too ambiguous). Not eval-tuned —
revisit with the accuracy workstream if fuzzy misses show up in eval traces.
"""

# Default graph-URI scheme — mirrors the metric-service writer
# (metric-service `neptune_client._resolve_graph_base` default) and the serve
# `GRAPH_URI_TEMPLATE` env var. Metrics are written to
# `{base}/{namespace}/{ontology_id}` — one path segment deeper than the
# namespace graph — so the namespace is the FIRST path segment after the prefix.
_DEFAULT_GRAPH_URI_TEMPLATE = "https://ontology-workbench.local/{namespace}"

# `@@PREFIX@@` = the static graph-URI prefix (everything before `{namespace}`);
# `@@NS_REGEX@@` extracts the namespace as the first path segment after it.
# Single-brace, valid SPARQL — placeholders are substituted via str.replace
# (NOT str.format), so the SPARQL braces stay literal.

_METRIC_LIST_SPARQL_TEMPLATE = """
SELECT ?name ?description ?aiContext ?expressionDialects ?dataSourceId ?namespace WHERE {
  GRAPH ?g {
    ?s a <http://www.w3.org/2002/07/owl#Class> .
    ?s <http://www.w3.org/2000/01/rdf-schema#subClassOf> <urn:coa:vocab#GovernedMetric> .
    ?s <http://www.w3.org/2000/01/rdf-schema#label> ?name .
    OPTIONAL { ?s <http://www.w3.org/2000/01/rdf-schema#comment> ?description }
    OPTIONAL { ?s <urn:coa:vocab#aiContext> ?aiContext }
    OPTIONAL { ?s <urn:coa:vocab#expressionDialects> ?expressionDialects }
    OPTIONAL { ?s <urn:coa:vocab#dataSourceId> ?dataSourceId }
  }
  BIND(REPLACE(STR(?g), "@@NS_REGEX@@", "$1") AS ?namespace)
  FILTER(STRSTARTS(STR(?g), "@@PREFIX@@"))
}
"""
_METRIC_LIST_SPARQL_TEMPLATE = _METRIC_LIST_SPARQL_TEMPLATE.replace("urn:coa:", f"urn:{_URN_PREFIX}:")


# XPath 1.0 regex metacharacters (Neptune's SPARQL REPLACE() engine). The graph
# prefix is interpolated into a REPLACE() pattern, so every metacharacter it
# could contain must be escaped — not just `.`/`\` (review: a prefix with `+`,
# e.g. `https://host/path+to/`, would otherwise corrupt the namespace capture).
_XPATH_REGEX_META = "\\.+*?[](){}|^$"


def _escape_xpath_regex(s: str) -> str:
    """Backslash-escape every XPath-1.0 regex metacharacter in ``s``."""
    return "".join("\\" + c if c in _XPATH_REGEX_META else c for c in s)


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Collapse overlapping/adjacent ``(start, end)`` spans into disjoint ones.

    A question can hit the same metric through several aliases whose spans
    OVERLAP ("total revenue" as a synonym and "revenue" as the name both match
    "what is total revenue"). Rendering those spans individually would repeat the
    shared text ("total revenue revenue"), so they are merged before display.
    """
    if not spans:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def residual_tokens(folded_query: str, matched_spans: list[tuple[int, int]]) -> list[str]:
    """Return the query tokens left unconsumed by the metric match.

    Removes the character ranges the metric name/synonym matched on, then drops
    every token in :data:`_RESIDUAL_STOP_WORDS`. What remains is content the
    caller asked about that Tier-1's unfiltered SQL cannot express — a filter
    ("for the Gold tier"), a grouping ("by region"), a time window ("last
    quarter"), or an entity that may not exist at all.

    Args:
        folded_query: The natural-language question, already passed through
            :func:`_fold` (NFKC + lowercase). Folding must happen exactly once,
            by the caller: NFKC can change string length, so the spans and the
            text they index must come from the same folded string.
        matched_spans: ``(start, end)`` character offsets, into ``folded_query``,
            of each name/synonym span that matched.

    Returns:
        The unconsumed, non-stop-word tokens in query order. Empty means the
        match consumed the whole question and Tier-1 can answer it in full.
    """
    # Blank the matched spans rather than slicing them out, so the surviving
    # tokens keep their original boundaries (a span removal by concatenation
    # could fuse two neighbouring words into a spurious token).
    chars = list(folded_query)
    for start, end in matched_spans:
        for i in range(max(0, start), min(len(chars), end)):
            chars[i] = " "
    remainder = "".join(chars)
    return [t for t in iter_word_tokens(remainder) if not _is_stop_scaffolding(t)]


def _metric_list_sparql(graph_uri_template: str = "") -> str:
    """Build the metric-list SPARQL for the configured graph-URI scheme.

    metric-service publishes GovernedMetrics into `{base}/{namespace}/{ontology_id}`
    (see metric-service `neptune_client._named_graph`); the `{base}/{namespace}`
    prefix is exactly the deployed `GRAPH_URI_TEMPLATE`. The resolver matches any
    graph under the template's static prefix and extracts the namespace as the
    first path segment after that prefix.

    This replaces the legacy hardcoded `urn:coa:{ns}:published` scheme, which no
    writer uses — so the resolver previously loaded 0 metrics and Tier-1 never
    matched API-published metrics.
    """
    if not graph_uri_template:
        # Review (Kun): the env-unset fallback hits a hostname no real deployment
        # uses, so Neptune returns 0 bindings and the empty cache looks identical
        # to "no metrics published yet". Warn so a misconfiguration is distinguishable.
        logger.warning("metric_resolver_graph_uri_template_unset_fallback", fallback=_DEFAULT_GRAPH_URI_TEMPLATE)
    template = graph_uri_template or _DEFAULT_GRAPH_URI_TEMPLATE
    prefix = template.split("{namespace}", 1)[0]
    # Escape ALL XPath-regex metacharacters in the prefix for the REPLACE pattern.
    prefix_re = _escape_xpath_regex(prefix)
    # SPARQL string literals treat `\` as an escape char, so each literal
    # backslash in the regex must be doubled (XPath `\.` → SPARQL `\\.`).
    prefix_re_sparql = prefix_re.replace("\\", "\\\\")
    # Namespace = first path segment after the static prefix (the metric graph
    # carries a trailing `/{ontology_id}` the namespace graph does not).
    ns_regex = f"^{prefix_re_sparql}([^/]+).*$"
    return _METRIC_LIST_SPARQL_TEMPLATE.replace("@@NS_REGEX@@", ns_regex).replace("@@PREFIX@@", prefix)


class MetricResolver:
    """Matches queries to pre-defined metrics via in-memory indexes.

    Builds indexes for fast lookup:
    - by_id: metric_id -> MetricDefinition
    - by_name: lowercase name -> list[MetricDefinition] (same name in different namespaces coexist)
    - by_synonym: lowercase synonym -> list[MetricDefinition]
    - by_namespace: namespace -> list[MetricDefinition]
    """

    def __init__(
        self,
        seed: list[MetricSeedItem] | None = None,
        neptune_client: NeptuneGraphClient | None = None,
    ):
        """Build initial metric indexes from a seed and/or a Neptune source.

        Args:
            seed: Optional metric definitions to index immediately.
            neptune_client: Optional client used to load and periodically refresh
                metrics from Neptune. With neither seed nor client, the resolver
                is marked loaded (empty).
        """
        self._snapshot = _IndexSnapshot({}, {}, {}, {})
        self._loaded = False
        self._neptune = neptune_client
        self._refresh_task: asyncio.Task | None = None
        # Built once from the configured GRAPH_URI_TEMPLATE.
        self._metric_list_sparql = _metric_list_sparql(get_graph_uri_template())
        if seed:
            self._build_indexes(seed)
        elif not neptune_client:
            # No seed and no Neptune — mark as loaded (empty)
            self._loaded = True

    @property
    def loaded(self) -> bool:
        """Return whether the metric indexes have been populated."""
        return self._loaded

    async def start(self) -> None:
        """Load metrics from Neptune and start background refresh."""
        if self._neptune:
            await self._refresh_from_neptune()
            self._refresh_task = asyncio.create_task(self._periodic_refresh())

    async def stop(self) -> None:
        """Cancel background refresh."""
        if self._refresh_task:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task

    async def _refresh_from_neptune(self) -> None:
        """Query Neptune for all metrics and rebuild indexes."""
        if self._neptune is None:
            return
        try:
            bindings = await self._neptune.query(self._metric_list_sparql, max_results=_METRIC_QUERY_MAX_RESULTS)
            items = self._bindings_to_seed(bindings)
            if items:
                self._build_indexes(items)
                logger.info("metric_resolver_refreshed_from_neptune", metric_count=len(self._snapshot.by_id))
            elif not self._loaded:
                self._loaded = True
                logger.info("metric_resolver_neptune_empty")
        except Exception as exc:
            logger.warning("metric_resolver_refresh_failed", error=str(exc))

    async def _periodic_refresh(self) -> None:
        """Background loop that refreshes metrics from Neptune."""
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_S)
            await self._refresh_from_neptune()

    @staticmethod
    def _select_expression(dialects: list[dict[str, Any]]) -> str:
        """Pick the SQL expression to execute from a metric's dialect list (#809).

        The direct-JDBC path re-transpiles metric SQL with sqlglot ``read="trino"``,
        so a TRINO-authored expression round-trips correctly regardless of the
        target engine. Prefer it when present; otherwise fall back to the first
        listed expression (the pre-#809 behavior) so metrics with no TRINO variant
        still resolve. ``dialect`` labels are compared case-insensitively.

        Always returns ``str``: an empty ``dialects`` list yields ``""`` (never an
        IndexError), and a JSON-``null`` ``expression`` (Neptune stores these as
        JSON, so ``{"dialect": "TRINO", "expression": null}`` parses to ``None``)
        is coerced to ``""`` — ``dict.get(k, "")`` only supplies the default when
        the key is ABSENT, not when it is present with value ``None``.
        """
        if not dialects:
            return ""
        for entry in dialects:
            if str(entry.get("dialect", "")).upper() == "TRINO":
                return str(entry.get("expression") or "")
        return str(dialects[0].get("expression") or "")

    @staticmethod
    def _bindings_to_seed(bindings: list[dict[str, Any]]) -> list[MetricSeedItem]:
        """Convert SPARQL bindings into MetricSeedItem list (group by (namespace, name)).

        The NeptuneGraphClient.query() pre-extracts binding values, so each row
        is a flat dict of {variable: value_string}.
        """
        import json as _json

        grouped: dict[tuple[str, str], MetricSeedItem] = {}
        for row in bindings:
            name = row.get("name", "")
            namespace = row.get("namespace", "")
            if not name:
                continue
            key = (namespace, name)
            if key not in grouped:
                # Parse aiContext JSON for synonyms
                synonyms: list[str] = []
                ai_context_raw = row.get("aiContext", "")
                if ai_context_raw:
                    try:
                        ai_ctx = _json.loads(ai_context_raw)
                        synonyms = ai_ctx.get("synonyms", [])
                    except (ValueError, TypeError) as exc:
                        logger.warning("metric_json_parse_failed", field="aiContext", name=name, error=str(exc))

                # Parse expressionDialects JSON for SQL. Select the TRINO
                # expression when present rather than blindly taking index 0:
                # the shared CompositeQueryExecutor re-transpiles every metric
                # from Trino (``read="trino"``) before direct-JDBC execution, so
                # a non-Trino ``dialects[0]`` (metrics may be authored in
                # POSTGRESQL/REDSHIFT/SNOWFLAKE/… and the list is neither
                # TRINO-required nor reordered at ingest) would be mis-parsed —
                # silently wrong results or a spurious firewall reject (#809).
                # Fall back to the first expression when no TRINO variant exists
                # (preserves prior behavior for legacy/non-Trino-authored metrics).
                sql_template = ""
                dialects_raw = row.get("expressionDialects", "")
                if dialects_raw:
                    try:
                        dialects = _json.loads(dialects_raw)
                        if dialects:
                            sql_template = MetricResolver._select_expression(dialects)
                    except (ValueError, TypeError) as exc:
                        logger.warning(
                            "metric_json_parse_failed", field="expressionDialects", name=name, error=str(exc)
                        )

                metric_id = f"{namespace}:{name}" if namespace else name

                grouped[key] = {
                    "metric_id": metric_id,
                    "name": name,
                    "description": row.get("description", ""),
                    "sql_template": sql_template,
                    "data_source_id": row.get("dataSourceId", ""),
                    "namespace": namespace,
                    "synonyms": synonyms,
                }
        return list(grouped.values())

    def _build_indexes(self, items: list[MetricSeedItem]) -> None:
        """Build in-memory indexes from seed data."""
        by_id: dict[str, MetricDefinition] = {}
        by_name: dict[str, list[MetricDefinition]] = {}
        by_synonym: dict[str, list[MetricDefinition]] = {}
        by_namespace: dict[str, list[MetricDefinition]] = {}

        for item in items:
            metric_id = item.get("metric_id", "")
            name = item.get("name", "")
            if not metric_id or not name:
                continue

            defn = MetricDefinition(
                metric_id=metric_id,
                name=name,
                display_name=item.get("display_name", ""),
                description=item.get("description", ""),
                sql_template=item.get("sql_template", ""),
                dimensions=item.get("dimensions", []),
                synonyms=item.get("synonyms", []),
                namespace=item.get("namespace", ""),
                data_source_id=item.get("data_source_id", ""),
                columns=item.get("columns", []),
            )

            by_id[metric_id] = defn
            by_name.setdefault(_fold(name), []).append(defn)
            for syn in defn.synonyms:
                # An empty synonym would compile to a bare `\b\b` pattern, which
                # matches at every word boundary — i.e. EVERY query resolves to
                # this metric. Skip it at index time.
                if not syn:
                    continue
                by_synonym.setdefault(_fold(syn), []).append(defn)

            ns_key = defn.namespace.lower()
            by_namespace.setdefault(ns_key, []).append(defn)

        # Pre-compile word-boundary regex patterns for fast matching
        # Replace underscores with flexible whitespace/underscore pattern for matching
        def _name_pattern(name: str) -> re.Pattern:
            escaped = re.escape(name).replace("_", r"[\s_]")
            # `\b` is Unicode-aware, so it cannot separate a Latin-edged alias
            # from adjacent CJK text: both sides are `\w` in 「月間kwh使用量」.
            # For Latin/digit/underscore edges, reject only a neighbour from that
            # same identifier alphabet. A CJK neighbour is a valid boundary.
            # Unspaced-script edges need no assertion; their over-matches are
            # handled by the residual/multi-match gates.
            left = "" if is_unspaced_script_letter(name[:1]) else r"(?<![a-z0-9_])"
            right = "" if is_unspaced_script_letter(name[-1:]) else r"(?![a-z0-9_])"
            return re.compile(rf"{left}{escaped}{right}")

        # Build patterns for all metrics with each name/synonym
        name_patterns: dict[str, list[tuple[re.Pattern, MetricDefinition]]] = {}
        for name_lower, defns in by_name.items():
            name_patterns[name_lower] = [(_name_pattern(name_lower), defn) for defn in defns]

        synonym_patterns: dict[str, list[tuple[re.Pattern, MetricDefinition]]] = {}
        for syn_lower, defns in by_synonym.items():
            synonym_patterns[syn_lower] = [(_name_pattern(syn_lower), defn) for defn in defns]

        self._snapshot = _IndexSnapshot(by_id, by_name, by_synonym, by_namespace, name_patterns, synonym_patterns)
        self._loaded = True
        logger.info("metric_resolver_loaded", metric_count=len(by_id), namespace_count=len(by_namespace))

    def exact_name_synonym_match(self, query: str, namespace: str) -> MetricMatch:
        """Check if the query contains an exact metric name or synonym.

        Returns MetricMatch with match_count indicating how many distinct
        metrics were matched. match_count > 1 means multi-metric query.
        """
        query_lower = _fold(query)
        ns_lower = namespace.lower()

        matched_metrics: dict[str, tuple[MetricDefinition, str]] = {}
        # Every matched span per metric, for the residual computation. A
        # question may hit the same metric through several aliases ("total sales
        # revenue"); all of their spans count as consumed.
        matched_spans: dict[str, list[tuple[int, int]]] = {}
        snapshot = self._snapshot

        # Iterate over all pattern lists (each name may have metrics from multiple namespaces)
        for _name_lower, pattern_defn_list in snapshot.name_patterns.items():
            for pattern, defn in pattern_defn_list:
                if defn.namespace and defn.namespace.lower() != ns_lower:
                    continue
                spans = [m.span() for m in pattern.finditer(query_lower)]
                if spans:
                    matched_metrics[defn.metric_id] = (defn, "name")
                    matched_spans.setdefault(defn.metric_id, []).extend(spans)

        for _syn_lower, pattern_defn_list in snapshot.synonym_patterns.items():
            for pattern, defn in pattern_defn_list:
                if defn.namespace and defn.namespace.lower() != ns_lower:
                    continue
                spans = [m.span() for m in pattern.finditer(query_lower)]
                if not spans:
                    continue
                # A metric already matched by NAME keeps match_source="name", but
                # its synonym spans are still consumed text for residual purposes.
                matched_spans.setdefault(defn.metric_id, []).extend(spans)
                if defn.metric_id not in matched_metrics:
                    matched_metrics[defn.metric_id] = (defn, "synonym")

        if not matched_metrics:
            return MetricMatch(found=False, match_count=0)

        first_id = next(iter(matched_metrics))
        defn, source = matched_metrics[first_id]

        spans = matched_spans.get(first_id, [])
        residual = residual_tokens(query_lower, spans)
        # Merged, so overlapping aliases don't repeat their shared text.
        matched_text = " ".join(query_lower[s:e] for s, e in merge_spans(spans))

        return MetricMatch(
            found=True,
            metric_id=defn.metric_id,
            metric_name=defn.name,
            sql_template=defn.sql_template,
            dimensions=defn.dimensions,
            data_source_id=defn.data_source_id,
            columns=defn.columns,
            match_source=source,
            match_count=len(matched_metrics),
            matched_text=matched_text,
            residual=" ".join(residual),
        )

    def fuzzy_match(self, query: str, namespace: str) -> MetricMatch:
        """Fuzzy near-miss match for typos/plurals/abbreviations.

        Runs ONLY after an exact name/synonym match misses. Scores the query's
        token windows against each metric's name + synonyms via
        difflib.SequenceMatcher and returns the single best candidate whose
        ratio ≥ _FUZZY_MATCH_THRESHOLD, with ``match_confidence`` = that ratio
        (always < 1.0 — exact matches keep the deterministic 1.0). Returns
        found=False if nothing clears the bar (→ Tier-2 fallthrough).

        This is a deterministic, LLM-free safety net; it deliberately does NOT
        attempt semantic matching (the downstream tiers' vector search owns that).
        """
        ns_lower = namespace.lower()
        snapshot = self._snapshot
        q_tokens = list(iter_word_tokens(_fold(query)))
        if not q_tokens:
            return MetricMatch(found=False, match_count=0)

        whole_query = " ".join(q_tokens)
        best_defn: MetricDefinition | None = None
        best_ratio = 0.0
        best_target = ""

        # Reuse one SequenceMatcher per target (set_seq2 once) and gate the
        # expensive ratio() behind quick_ratio()/real_quick_ratio() upper bounds —
        # the standard difflib fast-path. Fuzzy runs on every Tier-1 miss, so this
        # keeps the hot path cheap even with many metrics.
        matcher = difflib.SequenceMatcher(autojunk=False)

        def _score(target_lc: str) -> tuple[float, tuple[int, int]]:
            """Best ratio for ``target_lc`` and the winning window's token range.

            The range is ``(start, end)`` indices into ``q_tokens``; the
            whole-query window spans every token. Returned so the caller can
            compute the residual — the question tokens the fuzzy hit did NOT
            account for.
            """
            matcher.set_seq2(target_lc)
            t_len = max(1, len(target_lc.split()))
            # (window_text, token_range) pairs — the sliding windows first, then
            # the whole query as a final catch-all.
            windows = [(" ".join(q_tokens[i : i + t_len]), (i, i + t_len)) for i in range(len(q_tokens) - t_len + 1)]
            windows.append((whole_query, (0, len(q_tokens))))
            best = 0.0
            best_range = (0, 0)
            for window, token_range in windows:
                matcher.set_seq1(window)
                # Cheap upper bounds first; skip ratio() when it can't beat best/threshold.
                bar = max(best, _FUZZY_MATCH_THRESHOLD)
                if matcher.real_quick_ratio() < bar or matcher.quick_ratio() < bar:
                    continue
                ratio = matcher.ratio()
                if ratio > best:
                    best, best_range = ratio, token_range
            return best, best_range

        # Use the per-namespace index (+ namespace-agnostic metrics under "") so
        # scoring cost scales with the TARGET namespace's metric count, not the
        # whole catalog (review: a large namespace must not slow lookups in a
        # small one).
        candidates = snapshot.by_namespace.get(ns_lower, []) + snapshot.by_namespace.get("", [])
        best_range = (0, 0)
        for defn in candidates:
            for target in (defn.name, *defn.synonyms):
                if not target:
                    continue
                ratio, token_range = _score(_fold(target))
                if ratio > best_ratio:
                    best_ratio, best_defn, best_target, best_range = ratio, defn, target, token_range

        if not best_defn or best_ratio < _FUZZY_MATCH_THRESHOLD:
            return MetricMatch(found=False, match_count=0)

        # Same residual gate as the exact path: a fuzzy hit also returns the metric's
        # UNFILTERED SQL, so an unconsumed qualifier is just as wrong here. Token
        # ranges (not char spans) because fuzzy matches windows, not literals.
        consumed = set(range(*best_range))
        residual = [t for i, t in enumerate(q_tokens) if i not in consumed and not _is_stop_scaffolding(t)]

        logger.info(
            "metric_resolver_fuzzy_match",
            metric=best_defn.metric_id,
            matched_against=best_target,
            ratio=round(best_ratio, 3),
            residual_token_count=len(residual),
        )
        return MetricMatch(
            found=True,
            metric_id=best_defn.metric_id,
            metric_name=best_defn.name,
            sql_template=best_defn.sql_template,
            dimensions=best_defn.dimensions,
            data_source_id=best_defn.data_source_id,
            columns=best_defn.columns,
            match_source="fuzzy",
            match_count=1,
            match_confidence=round(best_ratio, 3),
            matched_text=" ".join(q_tokens[best_range[0] : best_range[1]]),
            residual=" ".join(residual),
        )

    def lookup(self, metric_id: str) -> MetricDefinition | None:
        """Look up a metric definition by ID."""
        return self._snapshot.by_id.get(metric_id)

    @staticmethod
    def substitute_dimensions(sql_template: str, dimensions: dict[str, Any], allowed: list[str]) -> str:
        """Substitute dimension values into a metric SQL template.

        Replaces named placeholders of the form ``:dimension`` (or ``{dimension}``)
        in the pre-compiled template with the caller's dimension values, using
        sqlglot to render each value as a properly-typed, quoted SQL LITERAL — NOT
        naive string interpolation. Building literals through sqlglot's AST means
        the value is escaped/quoted by the SQL engine's own rules (a string can't
        break out into SQL), which is the parameterized-binding guarantee the WI
        requires on a path (Athena) that has no native bind-parameter mechanism.

        Only dimensions in ``allowed`` (the metric's declared dimensions) are
        substituted. A template with no placeholders and no supplied values is
        returned unchanged. Raises ValueError if a declared placeholder has no
        provided value (fail-closed: never execute a half-substituted template).

        Also raises ValueError if a SUPPLIED dimension matches no placeholder in the
        template. Ignoring it would execute the UNFILTERED metric and return the
        global figure at confidence 1.0 — a wrong answer the caller cannot detect,
        which is worse than bypassing Tier-1. The orchestrator catches this and
        falls through to Tier-2/3, which can still answer the filtered question.
        """
        if not dimensions:
            # No values supplied — only valid if the template has no placeholders.
            if _PLACEHOLDER_RE.search(sql_template):
                missing = sorted({(m.group(1) or m.group(2)) for m in _PLACEHOLDER_RE.finditer(sql_template)})
                raise ValueError(f"Metric template requires dimensions with no values: {missing}")
            return sql_template

        allowed_set = {a.lower() for a in allowed}

        def _literal(value: Any) -> str:
            # Render value as a SQL literal via sqlglot (engine-correct quoting/escaping).
            if isinstance(value, bool):
                return "TRUE" if value else "FALSE"
            if isinstance(value, (int, float)):
                return sqlglot.exp.Literal.number(value).sql()
            return sqlglot.exp.Literal.string(str(value)).sql()

        # Case-insensitive value lookup, consistent with the allowed-set check
        # (a caller passing {"Region": ...} must satisfy placeholder :region).
        #
        # lower() is sufficient here — and agrees with the casefold() dedup in
        # ``InvokeRequest._normalize_dimensions`` — only because _PLACEHOLDER_RE
        # admits ASCII names exclusively, and no ASCII character has
        # lower() != casefold(). Widening that pattern to accept non-ASCII would
        # split the two paths: names distinct under lower() but equal under
        # casefold() would clear dedup and then collapse here, dropping a filter.
        # Both halves of that invariant are pinned by
        # test_metric_resolver.py::TestDimensionSubstitution (the ASCII-only and
        # lower-equals-casefold tests).
        dimensions_ci = {str(k).lower(): v for k, v in dimensions.items()}

        bound: set[str] = set()

        def _replace(match: re.Match) -> str:
            name = match.group(1) or match.group(2)
            if name.lower() not in allowed_set:
                raise ValueError(f"Dimension '{name}' is not a declared dimension of this metric")
            if name.lower() not in dimensions_ci:
                raise ValueError(f"No value supplied for dimension placeholder '{name}'")
            bound.add(name.lower())
            return _literal(dimensions_ci[name.lower()])

        substituted = _PLACEHOLDER_RE.sub(_replace, sql_template)

        # Fail closed on any supplied filter the template cannot apply, rather than
        # silently answering the unfiltered question (see docstring).
        unapplied = sorted(set(dimensions_ci) - bound)
        if unapplied:
            raise ValueError(f"Metric template has no placeholder for supplied dimensions: {unapplied}")

        return substituted

    def list_all(self, namespace: str) -> list[dict[str, Any]]:
        """Return compact summaries of all metrics for a namespace (for Tier 3 context)."""
        ns_lower = namespace.lower()
        definitions = self._snapshot.by_namespace.get(ns_lower, [])
        # Include namespace-agnostic metrics (empty namespace matches all)
        definitions = definitions + self._snapshot.by_namespace.get("", [])
        return [d.compact_summary for d in definitions]

    async def match(self, query: str, namespace: str) -> MetricMatch:
        """Resolve a query to a metric: exact name/synonym first, then fuzzy.

        Exact/synonym matches (confidence 1.0) take precedence and short-circuit.
        Only on an exact miss do we attempt a fuzzy near-miss (confidence < 1.0).
        A multi-metric exact match (match_count > 1) is returned as-is so the
        orchestrator can bypass Tier-1 — fuzzy is not attempted in that case.
        """
        # Lazy-load guard: start() is kicked off fire-and-forget at init, so the
        # first request(s) on a fresh instance can race ahead of the initial
        # Neptune refresh and match against an EMPTY snapshot (Tier-1 false miss).
        # Block on a one-shot refresh before matching when we haven't loaded yet.
        # Keying on ``_loaded`` (not the index contents) is important: a namespace
        # with legitimately ZERO metrics leaves the index empty but sets
        # ``_loaded`` (see _refresh_from_neptune), so this no-ops thereafter
        # instead of hitting Neptune on every request. A FAILED refresh leaves
        # ``_loaded`` False, so it correctly retries next time.
        if self._neptune is not None and not self._loaded:
            await self._refresh_from_neptune()

        exact = self.exact_name_synonym_match(query, namespace)
        if exact.found:
            # Covers multi-metric too: match_count > 1 always has found=True.
            # Returned as-is so the orchestrator can bypass Tier-1; fuzzy is
            # never attempted after any exact hit.
            return exact
        return self.fuzzy_match(query, namespace)
