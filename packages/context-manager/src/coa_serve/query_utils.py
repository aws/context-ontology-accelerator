# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared query utilities: stop words, entity extraction, namespace validation, graph URI helpers."""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass

import regex
import structlog
from coa_common import validate_namespace_name
from coa_common.constants import MAX_QUERY_CODEPOINTS

logger = structlog.get_logger(__name__)

# Filtered from NL queries before matching against ontology class labels — these common
# words would produce false-positive matches and dilute the keyword relevance signal.
STOP_WORDS: frozenset[str] = frozenset(
    {
        "what",
        "is",
        "the",
        "how",
        "many",
        "show",
        "me",
        "all",
        "for",
        "in",
        "by",
        "a",
        "an",
        "of",
        "to",
        "and",
        "or",
        "list",
        "find",
        "get",
        "total",
        "count",
        "average",
        "where",
        "which",
        "each",
        "with",
        "from",
        "between",
        "their",
        "has",
        "have",
        "does",
        "are",
        "was",
        "were",
        "this",
        "that",
        "these",
        "those",
        "can",
        "will",
        "do",
    }
)

# Graph URI template — {namespace} is replaced at query time.
# Set via GRAPH_URI_TEMPLATE env var (e.g. "https://ontology.example.com/{namespace}").
# TODO: Resolve graph URIs dynamically from the ontology catalog (DynamoDB registry).

# ``regex`` supplies Unicode extended grapheme clusters (UAX #29). Python ``re``'s
# ``\w`` excludes combining marks, which split Thai/Indic words and splits
# ``İstanbul`` after lowercasing (U+0130 -> ``i`` + COMBINING DOT ABOVE).
_GRAPHEME_RE = regex.compile(r"\X")

_SCRIPT_HAN = "han"
_SCRIPT_KATAKANA = "katakana"
_SCRIPT_HIRAGANA = "hiragana"
_SCRIPT_HANGUL = "hangul"
_SCRIPT_COMPLEX_CONTEXT = "complex_context"
_RUN_SCRIPT_MATCHERS = (
    (_SCRIPT_HAN, regex.compile(r"\p{scx=Han}")),
    (_SCRIPT_KATAKANA, regex.compile(r"\p{scx=Katakana}")),
    (_SCRIPT_HIRAGANA, regex.compile(r"\p{scx=Hiragana}")),
    (_SCRIPT_HANGUL, regex.compile(r"\p{scx=Hangul}")),
    # Unicode Line_Break=SA covers scripts such as Thai, Lao, Khmer, and
    # Myanmar whose normal word boundaries are not represented by spaces.
    (_SCRIPT_COMPLEX_CONTEXT, regex.compile(r"\p{lb=SA}")),
)

_LATIN_LETTER_RE = regex.compile(r"\p{scx=Latin}")

# Scripts excluded from reverse containment, and why each is excluded:
#
# * Latin — reliable whitespace boundaries, so the forward direction already
#   covers it. Admitting it would match two-letter Latin labels against ordinary
#   English words and silently suspend STOP_WORDS for any query holding one
#   non-Latin letter.
# * Greek — space-delimited AND its inflection rewrites the ending rather than
#   appending to the citation form, so the stored label is not a substring of the
#   inflected word (``νόμος`` is not inside ``νόμου``) and a container cannot find
#   a label the forward term missed.
#
# Cyrillic is deliberately INCLUDED even though it is space-delimited: a Russian
# noun's case forms append to the nominative stem, so the stored label really does
# sit inside the query word (``стол`` is inside all ten case forms of itself, which
# the forward direction reaches only in the nominative). The cost is that a short
# label can match inside a longer unrelated word — ``по`` is exactly two code
# points, so it passes the sinks' minimum and does match inside
# ``расположение``. What actually bounds this is that a space-delimited script
# yields one region PER WORD, so the haystack is a single word rather than a
# clause, plus the per-caller row limits. Recovering nine case forms is worth that.
#
# The rest are included because a stored label genuinely can sit inside a query
# word: Han/Kana/Thai/Lao/Khmer/Myanmar omit word spaces entirely, and Hangul,
# Devanagari, Bengali, Arabic and Hebrew attach particles, case endings or articles
# directly to the word.
_CONTAINER_LETTER = r"[\p{L}--[\p{scx=Latin}\p{scx=Greek}]]"
_CONTAINER_LETTER_RE = regex.compile(_CONTAINER_LETTER, regex.V1)
# A reverse-containment region: a maximal run of container-eligible letters, their
# combining marks, and the two joiners that sit inside a word (ZWNJ/ZWJ, which the
# forward terms also keep). Everything else — Latin/Greek letters, digits,
# whitespace, punctuation, symbols, and every other control/format/separator
# character — is a boundary, so a region can only ever hold script text. Marks are
# admitted unconditionally: 46 BMP combining marks carry ``scx=Latin`` (all of
# U+0300-U+036F among them, plus the Vedic marks used with Devanagari), so
# subtracting Latin from the mark class would split a decomposed word mid-way and
# drop the mark.
_CONTAINER_REGION_RE = regex.compile(rf"[\p{{M}}{_CONTAINER_LETTER}\u200c\u200d]+", regex.V1)

# Removed from the query before segmentation, so neither direction can be defeated
# by a character the reader cannot see. An ideographic variation selector is
# category Mn and would otherwise survive inside a word (a stored ``渡邊`` is not
# inside an IME-produced ``渡󠄀邊``, and the property graph's normalizer turns the
# selector into a space); the Cf members — ZWSP, SHY, LRM/RLM, WJ, ZWNBSP — would
# otherwise act as word boundaries and split a word in half. Persisted labels come
# from table/column names and ontology terms, which do not carry these; the query
# is where they appear. ZWNJ and ZWJ are default-ignorable too but are excluded
# from the strip, because they carry meaning inside Persian compounds and Indic
# conjuncts.
_DEFAULT_IGNORABLE_RE = regex.compile(r"[\p{Default_Ignorable_Code_Point}--[\u200c\u200d]]+", regex.V1)

# Connectors are retained only inside a generic word. Script punctuation is a
# boundary, preserving identifiers such as ``MSZ-ZW252`` without creating terms
# that contain trailing punctuation. Note the asymmetry with a reverse region,
# which excludes them: a term may carry ZWNJ/ZWJ, a container never does.
_INTERNAL_CONNECTORS = frozenset({"-", "_", "\u200c", "\u200d"})

_MIN_LATIN_TOKEN_GRAPHEMES = 3
_MIN_NON_LATIN_TOKEN_GRAPHEMES = 2

# Both sinks require a stored label of at least two characters before reverse
# containment may match it, so a shorter region could never match and would only
# spend a region slot.
_MIN_CONTAINER_CODEPOINTS = 2

# Reverse regions get their own bound rather than sharing ``max_count``. Sharing it
# reintroduced the failure the reverse direction exists to prevent: a spaced
# non-Latin script (Hindi, Bengali, Korean) yields one region per word, and a
# five-slot budget evicted the requested noun from the reverse direction too. Only
# region COUNT needs bounding — regions are disjoint substrings, so their total
# length is bounded by MAX_QUERY_CODEPOINTS however many survive.
MAX_CONTAINER_REGIONS = 16

# Shared query-cost bound for a single forward search term and for explicit
# Tier-3 terms. It is a code-point bound rather than a grapheme bound so one
# grapheme carrying an extreme number of combining marks cannot create an
# unbounded query. It deliberately does NOT apply to the reverse container, which
# is the query itself and is already bounded by MAX_QUERY_CODEPOINTS — capping the
# container would discard exactly the long no-space clauses it exists to serve.
MAX_SEARCH_TOKEN_CODEPOINTS = 64

# Modifier letters such as the Kana prolonged-sound mark can extend a real word,
# but a token made only from extenders is not a useful search term. These are the
# Unicode categories that provide an actual letter/number base.
_SEMANTIC_BASE_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lo"})


@dataclass(frozen=True)
class QuerySearchPlan:
    """Bounded candidates for forward and reverse label containment.

    ``terms`` are per-word forward candidates: ``stored_label CONTAINS term``.

    ``containers`` are the query's maximal script runs in the scripts where a
    stored label can sit inside a query word, for the reverse direction:
    ``container CONTAINS stored_label``. This inverts the dictionary problem
    instead of solving it — the serve layer does not know where words end in
    Chinese, Japanese, Thai or Khmer, and cannot strip the particles Korean, Hindi
    and Bengali agglutinate onto a noun, but the graph already stores the exact
    words as labels. Matching the label inside the query needs no per-language
    segmentation, morphology or stop-word list, and because a whole run is kept
    intact it is unaffected by which per-word candidates the ``max_count`` budget
    kept and by the per-term length bound.

    A run ends at any digit, space, punctuation mark, symbol, or letter of an
    excluded script; see :data:`_CONTAINER_LETTER` for which scripts are excluded
    and why. A container therefore holds script text only.

    ponytail: this is a recall-for-precision trade with a known ceiling. Within one
    run, containment can still match a label that straddles a word boundary — and
    for a monolingual Chinese, Japanese or Thai question there are no spaces, so
    that one run IS the whole question. A two-character label spanning the join of
    two adjacent words therefore still matches, as does a label that is a
    substring of one longer word. Only the sinks' two-character minimum and their
    row limits bound it. Raising the ceiling needs real per-language segmentation
    or a stored n-gram/boundary index, which is a larger change than this MR; the
    alternative today is no CJK/Thai recall at all.

    Source spelling is retained in both fields so each sink can apply its own
    persisted comparison contract in the correct order: RDF lowercases for SPARQL
    ``LCASE``; the property graph applies graphrag-toolkit's punctuation stripping
    before lowercasing.
    """

    terms: tuple[str, ...]
    containers: tuple[str, ...]


def normalize_label_match_text(value: str) -> str:
    """Apply the query-side transform paired with SPARQL ``LCASE(?label)``.

    Do not apply NFC/NFKC here: persisted RDF labels are compared verbatim after
    ``LCASE``, so query-only canonical or compatibility normalization is
    asymmetric and can make an exact stored spelling stop matching. Canonical
    equivalence requires a separately normalized label/index field and a backfill.
    """
    return value.lower()


def is_semantic_search_token(value: str) -> bool:
    """Return whether ``value`` contains an actual letter or number.

    Punctuation, emoji, underscores, combining marks, and script extenders may be
    part of a useful token, but must not be the token's only content.
    """
    for character in value:
        category = unicodedata.category(character)
        if category in _SEMANTIC_BASE_CATEGORIES or category.startswith("N"):
            return True
    return False


def escape_sparql_string_literal(value: str) -> str:
    """Escape ``value`` for a SPARQL double-quoted short string literal.

    Escaping must not double as character filtering: removing punctuation via a
    word-character allowlist also removes Unicode combining marks. Callers
    enforce their own semantic and control-character policies before interpolation.
    """
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )


def _cluster_has_word_character(cluster: str) -> bool:
    """Return whether a grapheme cluster can participate in a word run."""
    return any(unicodedata.category(character)[0] in {"L", "N"} for character in cluster)


def _run_script(cluster: str, current_script: str | None) -> str | None:
    """Classify a grapheme for run splitting, preserving ambiguous extenders."""
    matches = tuple(name for name, matcher in _RUN_SCRIPT_MATCHERS if matcher.search(cluster))
    if current_script in matches:
        return current_script
    return matches[0] if matches else None


def _iter_word_runs(query: str) -> Iterator[tuple[list[str], str | None]]:
    """Yield grapheme-preserving word runs, splitting at script transitions."""
    current: list[str] = []
    current_script: str | None = None
    pending_connector: str | None = None

    # ``finditer`` avoids materializing the whole grapheme list. The caller has
    # already applied MAX_QUERY_CODEPOINTS before this iterator is entered.
    for match in _GRAPHEME_RE.finditer(query):
        cluster = match.group(0)
        if _cluster_has_word_character(cluster):
            script = _run_script(cluster, current_script)
            changes_script = (
                bool(current) and script != current_script and (script is not None or current_script is not None)
            )
            if changes_script:
                yield current, current_script
                current = []
                pending_connector = None
            elif pending_connector is not None:
                current.append(pending_connector)

            current.append(cluster)
            current_script = script
            pending_connector = None
            continue

        if cluster in _INTERNAL_CONNECTORS and current and current_script is None and pending_connector is None:
            pending_connector = cluster
            continue

        if current:
            yield current, current_script
        current = []
        current_script = None
        pending_connector = None

    if current:
        yield current, current_script


def iter_word_tokens(text: str) -> Iterator[str]:
    """Yield ``text``'s word tokens: grapheme-preserving runs split at script transitions.

    The segmentation is :func:`_iter_word_runs`'s — Latin/digit runs keep their
    internal connectors (``-``/``_``), and scripts written without spaces yield
    one run per script stretch (a Han run, then a Hiragana run, ...). This is the
    shared tokenizer for callers that need to test individual question words
    against a closed word list (Tier-1's residual gate) rather than build search
    terms. Unlike :func:`build_query_search_plan` it applies no grapheme floors,
    no stop-word filtering, and does NOT drop hiragana runs: for a residual gate,
    dropping a run hides a qualifier — the unsafe direction (a hiragana time word
    like ``きのう`` must survive to trip the gate).

    Default-ignorable code points are stripped first, exactly as in
    :func:`build_query_search_plan`, so an invisible character cannot split a
    word into two tokens neither of which matches a stop-word list.
    """
    text = _DEFAULT_IGNORABLE_RE.sub("", text)
    for clusters, _script in _iter_word_runs(text):
        yield "".join(clusters)


def is_unspaced_script_letter(character: str) -> bool:
    r"""Return whether ``character`` is a letter of a script without reliable spaces.

    True for letters of the scripts eligible for reverse containment (see
    :data:`_CONTAINER_LETTER`): scripts that omit word spaces entirely
    (Han/Kana/Thai/Lao/Khmer/Myanmar) or attach particles, case endings, or
    articles directly to the word (Hangul, Devanagari, Bengali, Arabic, Hebrew,
    Cyrillic, ...). For these, a regex ``\b`` boundary asserts a transition that
    natural text never provides (``総売上は`` has no ``\b`` between the metric
    name and the particle), so word-boundary matching must be disabled at that
    edge. False for Latin/Greek letters, digits, and everything else.
    """
    return bool(character) and bool(_CONTAINER_LETTER_RE.match(character[0]))


def _uses_non_latin_floor(clusters: list[str]) -> bool:
    """Return whether a run is a non-Latin word eligible for the 2-cluster floor."""
    has_letter = False
    for cluster in clusters:
        for character in cluster:
            if not unicodedata.category(character).startswith("L"):
                continue
            has_letter = True
            if _LATIN_LETTER_RE.fullmatch(character):
                return False
    return has_letter


def _even_positions(candidate_count: int, max_count: int) -> list[int]:
    """Return source-order indexes sampled across a bounded candidate list."""
    if candidate_count <= max_count:
        return list(range(candidate_count))
    if max_count == 1:
        return [(candidate_count - 1) // 2]

    # Round each equally spaced position to the nearest source index. The first
    # and last candidates are always kept, so a polite prefix cannot evict the
    # concept a question ends with.
    half_denominator = (max_count - 1) // 2
    return [(index * (candidate_count - 1) + half_denominator) // (max_count - 1) for index in range(max_count)]


def _iter_container_regions(query: str) -> Iterator[str]:
    """Yield the query's maximal container-eligible script runs.

    A spaced script (Hangul, Devanagari, Bengali, Arabic, Hebrew) yields one region
    per word, which is as tight as this can get without a segmenter. A script that
    omits word spaces yields one region per clause, because there is no boundary to
    split on.
    """
    for match in _CONTAINER_REGION_RE.finditer(query):
        region = match.group(0)
        if len(region) < _MIN_CONTAINER_CODEPOINTS:
            continue
        # A run of bare combining marks carries no word to match against.
        if _CONTAINER_LETTER_RE.search(region):
            yield region


def build_query_search_plan(query: str, *, max_count: int = 10) -> QuerySearchPlan:
    """Build bounded multilingual label-search candidates for one query.

    Forward terms are complete grapheme-preserving runs, split at script
    transitions, with a floor of three graphemes for Latin words and two for other
    scripts. Hiragana-only runs are excluded: in mixed Japanese text they are
    grammar (particles and inflectional tails), and genuine hiragana vocabulary is
    still reachable through the reverse container. When more runs survive than
    ``max_count``, selection is spread across the whole query instead of taking its
    prefix. Reverse regions have their own :data:`MAX_CONTAINER_REGIONS` bound.

    See :class:`QuerySearchPlan` for the reverse container and why it exists.

    Raises:
        TypeError: If ``query`` is not a string.
        ValueError: If ``query`` exceeds :data:`MAX_QUERY_CODEPOINTS`.
    """
    if not isinstance(query, str):
        raise TypeError("query must be a string")
    if len(query) > MAX_QUERY_CODEPOINTS:
        raise ValueError(f"query must be at most {MAX_QUERY_CODEPOINTS} characters")
    # Strip default-ignorables from the WHOLE query, before segmentation. Doing it
    # per-candidate reached only the members that can sit inside a run (Mn marks,
    # the Hangul filler letters); the Cf members — ZWSP, SHY, LRM/RLM, WJ, ZWNBSP —
    # were still boundaries, so an invisible character inside a word split it and
    # cost both directions their match. Stripping first also means the grapheme
    # floors and length bounds below are measured on the text that will actually be
    # sent, rather than on text that later shrinks past them.
    query = _DEFAULT_IGNORABLE_RE.sub("", query)
    if max_count <= 0 or not query:
        return QuerySearchPlan(terms=(), containers=())

    candidates: list[str] = []
    seen: set[str] = set()
    for clusters, script in _iter_word_runs(query):
        if script == _SCRIPT_HIRAGANA:
            continue
        semantic_clusters = [cluster for cluster in clusters if _cluster_has_word_character(cluster)]
        grapheme_count = len(semantic_clusters)
        floor = (
            _MIN_NON_LATIN_TOKEN_GRAPHEMES if _uses_non_latin_floor(semantic_clusters) else _MIN_LATIN_TOKEN_GRAPHEMES
        )
        raw_token = "".join(clusters)
        normalized_token = normalize_label_match_text(raw_token)
        if grapheme_count < floor or len(normalized_token) > MAX_SEARCH_TOKEN_CODEPOINTS:
            continue
        if normalized_token in STOP_WORDS or normalized_token in seen:
            continue
        if not is_semantic_search_token(normalized_token):
            continue
        seen.add(normalized_token)
        candidates.append(raw_token)

    terms = tuple(candidates[index] for index in _even_positions(len(candidates), max_count))
    regions = list(dict.fromkeys(_iter_container_regions(query)))
    containers = tuple(regions[index] for index in _even_positions(len(regions), MAX_CONTAINER_REGIONS))
    if len(regions) > MAX_CONTAINER_REGIONS:
        # Only bites on a query with many separate script runs. Worth a line because
        # a no-space script has no working forward arm to fall back on: for pure
        # Chinese the forward term is the whole clause, so a dropped region is a
        # dropped chance to match anything.
        logger.info(
            "query_container_regions_truncated",
            regions=len(regions),
            limit=MAX_CONTAINER_REGIONS,
            forward_terms=len(terms),
        )
    return QuerySearchPlan(terms=terms, containers=containers)


def extract_query_entities(query: str, *, max_count: int = 10) -> list[str]:
    """Extract the forward-containment terms from :func:`build_query_search_plan`.

    Callers that only need ordinary per-word terms retain the list return type.
    Label-search consumers should use the full plan instead, so a stored label can
    also be matched inside the query for scripts where per-word terms are not
    reliable.
    """
    return [normalize_label_match_text(term) for term in build_query_search_plan(query, max_count=max_count).terms]


def validate_namespace(namespace: str) -> None:
    """Validate a namespace string for safe use in SPARQL interpolation.

    Delegates to ``coa_common.validate_namespace_name``.
    """
    validate_namespace_name(namespace)


DEFAULT_GRAPH_URI_TEMPLATE = os.environ.get("GRAPH_URI_TEMPLATE", "")


def get_graph_uri_template(override: str | None = None) -> str:
    """Return the graph URI template to use, respecting override and env-var fallback.

    Resolution order:
    1. ``override`` parameter (if non-empty)
    2. ``GRAPH_URI_TEMPLATE`` environment variable

    Args:
        override: Caller-supplied template string; ``None`` or empty string to skip.

    Returns:
        A graph URI template string with a ``{namespace}`` placeholder, or empty string if unconfigured.

    Raises:
        ValueError: If the resolved template is non-empty but missing the ``{namespace}`` placeholder.
    """
    result = override or os.environ.get("GRAPH_URI_TEMPLATE", "")
    if result and "{namespace}" not in result:
        raise ValueError(f"GRAPH_URI_TEMPLATE must contain '{{namespace}}' placeholder, got: {result!r}")
    return result


def namespace_graph_prefix(template: str, namespace: str) -> str:
    """Return the graph URI prefix for a namespace, suitable for STRSTARTS filtering.

    All Neptune queries that target namespace-scoped named graphs should use::

        GRAPH ?g { ... }
        FILTER(STRSTARTS(STR(?g), "<prefix>"))

    where ``<prefix>`` is the return value of this function.

    Args:
        template: The graph URI template (from ``get_graph_uri_template()``).
        namespace: The namespace identifier.

    Returns:
        A URI prefix ending with ``/`` (e.g. ``https://ontology-workbench.local/my-ns/``).

    Note:
        Prefix matching (rather than an exact ``?g`` match) is deliberate and safe:
        a namespace is validated against ``^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$`` so it
        cannot contain ``/``, and the returned prefix always ends in ``/`` — so
        ``…/ns-a/`` cannot match ``…/ns-ab/``. It is also load-bearing, since one
        namespace may hold several named graphs under its prefix.
    """
    return template.format(namespace=namespace).rstrip("/") + "/"


# Prolog for the shared SPARQL builders below. Neptune resolves ``rdfs:``/``owl:``
# without a prolog, but declaring them keeps the query portable to stores that do
# not (and duplicate declarations are legal SPARQL, so callers may add their own).
SPARQL_PREFIXES = "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\nPREFIX owl: <http://www.w3.org/2002/07/owl#>\n"


def graph_scoped_body(body: str, graph_uri_prefix: str, graph_iris: list[str] | None = None) -> str:
    """Scope a triple-pattern body to one namespace's named graphs.

    Prefers binding ``?g`` to the concrete graph IRIs (``VALUES``), which lets
    Neptune serve the patterns from the quad index for those graphs only. The bare
    ``GRAPH ?g`` + ``STRSTARTS`` form matches in EVERY graph on the cluster and
    filters afterwards, so its cost is set by the whole store rather than by the
    namespace: with ~30 ingested namespaces the FK-edge query stopped fitting in
    its timeout (pinning Neptune's CPU at 99%) and, because a failed load is never
    cached, traversal then errored on every call.

    Falls back to the prefix filter when the caller could not resolve the graphs,
    so an unexpected graph layout degrades to slow rather than to no traversal.

    Args:
        body: The triple patterns to place inside ``GRAPH ?g { … }``.
        graph_uri_prefix: Prefix from :func:`namespace_graph_prefix`, used by the
            fallback filter.
        graph_iris: The namespace's resolved named-graph IRIs, if known. Callers
            must have validated them as safe IRI refs.

    Returns:
        The ``GRAPH``-wrapped body, with either a ``VALUES`` binding or a
        ``STRSTARTS`` filter doing the scoping.
    """
    if graph_iris:
        values = " ".join(f"<{g}>" for g in graph_iris)
        return f"VALUES ?g {{ {values} }}\n          GRAPH ?g {{\n{body}\n          }}"
    return f'          GRAPH ?g {{\n{body}\n          }}\n          FILTER(STRSTARTS(STR(?g), "{graph_uri_prefix}"))'


def named_graphs_sparql(graph_uri_prefix: str, *, limit: int) -> str:
    """Build the query that resolves a namespace's named-graph IRIs.

    Anchored on ``owl:Ontology``, of which there is one subject per published
    graph — a few hundred triples cluster-wide, so this scan is cheap even
    un-bound, unlike the queries it lets :func:`graph_scoped_body` bind.

    Args:
        graph_uri_prefix: Prefix from :func:`namespace_graph_prefix`.
        limit: SPARQL ``LIMIT``; a backstop, since a namespace holds one graph per
            published ontology.

    Returns:
        A SPARQL SELECT binding ``?g``.
    """
    return f"""{SPARQL_PREFIXES}
        SELECT DISTINCT ?g WHERE {{
          GRAPH ?g {{ ?ont a owl:Ontology }}
          FILTER(STRSTARTS(STR(?g), "{graph_uri_prefix}"))
        }} LIMIT {int(limit)}"""


def object_properties_sparql(
    graph_uri_prefix: str,
    *,
    limit: int,
    mapped_gate_iri: str | None = None,
    with_comment: bool = False,
    graph_iris: list[str] | None = None,
) -> str:
    """Build the namespace-scoped ``owl:ObjectProperty`` (FK join path) query.

    ONE definition of "read the FK edges out of a namespace's ontology graph",
    shared by every consumer so a fix to it (label handling, scoping, performance)
    reaches all of them. Current consumers: the Tier-2 T-Box context builder
    (``ontop/tbox_context._fetch_object_properties``) and the FK-traversal tool
    (``tier2/tools/graph_tools.OntologyGraphTool._load_fk_edges``).

    Deliberately does NOT re-assert ``?domain a owl:Class`` / ``?range a owl:Class``:
    an FK object property's domain/range are classes by construction, and those
    redundant type checks combined with the unbound ``GRAPH ?g`` scan make Neptune
    verify the type of every candidate — which pushed the query past its timeout on
    a 400-table namespace.

    Args:
        graph_uri_prefix: Prefix from :func:`namespace_graph_prefix` (already
            validated by the caller).
        limit: SPARQL ``LIMIT`` on the returned edges.
        mapped_gate_iri: When given, require the marker ``<iri> true`` on BOTH ends,
            so only edges between answerable (mapped) classes are returned. ``None``
            returns every edge.
        with_comment: Also select ``?comment`` (the ingest pipeline's
            ``"Foreign key: a.b references c.d"`` provenance string).
        graph_iris: The namespace's named graphs, when the caller has resolved them
            (see :func:`named_graphs_sparql`). Binding them keeps this query's cost
            proportional to the namespace instead of to the whole cluster; omitting
            them falls back to the prefix filter (see :func:`graph_scoped_body`).

    Returns:
        A SPARQL SELECT binding ``?op ?opLabel ?domain ?domainLabel ?range
        ?rangeLabel`` (plus ``?comment`` when requested).
    """
    gate = ""
    if mapped_gate_iri:
        gate = f"?domain <{mapped_gate_iri}> true .\n            ?range <{mapped_gate_iri}> true ."
    comment_select = " ?comment" if with_comment else ""
    comment_pattern = "\n            OPTIONAL { ?op rdfs:comment ?comment }" if with_comment else ""
    body = f"""            ?op a owl:ObjectProperty .
            ?op rdfs:domain ?domain .
            ?op rdfs:range ?range .
            {gate}
            ?domain rdfs:label ?domainLabel .
            ?range rdfs:label ?rangeLabel .
            OPTIONAL {{ ?op rdfs:label ?opLabel }}{comment_pattern}"""
    return f"""{SPARQL_PREFIXES}
        SELECT ?op ?opLabel ?domain ?domainLabel ?range ?rangeLabel{comment_select} WHERE {{
{graph_scoped_body(body, graph_uri_prefix, graph_iris)}
        }} LIMIT {int(limit)}"""
