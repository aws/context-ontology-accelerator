# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Graph traversal for entity context enrichment.

Performs 1-2 hop SPARQL traversal from entity URIs found in routing
vector hits.  Returns entities and their relationships for context
assembly.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import structlog

from ..clients.base import GraphClient
from ..namespace_language import current_label_preference, row_label
from ..query_utils import (
    MAX_QUERY_CODEPOINTS,
    MAX_SEARCH_TOKEN_CODEPOINTS,
    build_query_search_plan,
    escape_sparql_string_literal,
    get_graph_uri_template,
    is_semantic_search_token,
    namespace_graph_prefix,
    normalize_label_match_text,
    validate_namespace,
)

logger = structlog.get_logger(__name__)

# Safety gate for a keyword before it is interpolated into a SPARQL string
# literal (``CONTAINS(LCASE(?label), "…")`` in _build_label_query).
#
# Defined by what is DANGEROUS rather than by an ASCII allowlist. The previous
# ``^[a-z0-9][a-z0-9_-]{0,63}$`` rejected every keyword in a non-Latin script and
# every accented Latin one, so a Japanese or German query reached this stage with
# tokens and left it with none — Tier-3's keyword hop then returned nothing, for
# the languages the feature exists to serve.
#
# What the gate has to stop is escaping the literal. ``_build_label_query``
# already escapes backslash and double-quote, which covers the direct break-out;
# what escaping does NOT handle is a raw line terminator, because a SPARQL short
# string literal (''STRING_LITERAL2'') may not contain one — so a newline would
# end the literal and put the rest of the token into query position. Control
# characters are rejected here for that reason, and because no legitimate keyword
# contains one.
#
# The length bound stays: it is a cost guard on the generated query, not a
# character-class judgement. Counted in characters, so a CJK keyword is not
# penalised for encoding to more bytes.
_UNSAFE_ENTITY_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
# URI safety gate for SPARQL <uri> interpolation. Permits the '#' fragment
# separator — ontology class/property URIs are of the form
# "https://.../pcinsurance#Claim", and '#' is a legal URI character that is
# safe inside a <...> IRI ref. The genuinely dangerous characters for IRI-ref
# injection (whitespace, angle brackets, quotes, braces, pipe, backslash,
# caret, backtick) remain excluded. (Dropping '#' here silently rejected every
# induced-ontology seed URI, making Tier-3 URI-hop return 0.)
_SAFE_URI_RE = re.compile(r"^https?://[^\s<>\"{}|\\^`;]+$|^urn:[a-zA-Z0-9][a-zA-Z0-9._:-]*$")

_MAX_TRAVERSAL_RESULTS = 50
# Row cap for the URI-hop query. Raised 100→300 when _build_hop_query grew from
# 2 UNION arms to 8 (property-node, class-neighborhood, and 2-hop bridged arms):
# each arm emits one row PER domain/range relationship per entity, across up to
# 10 seeds, so the pre-dedup row count is now several-fold higher. At 100 the
# LIMIT truncated the UNION before enough distinct entities/relationships
# survived _parse_hop_results' de-dup, starving the synthesizer; 300 keeps the
# richer neighborhood while _GRAPH_QUERY_TIMEOUT_S still bounds worst-case cost.
_MAX_HOP_RESULTS = 300
_GRAPH_QUERY_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class GraphEntity:
    """A graph entity with its label, type, and outgoing relationships."""

    uri: str
    label: str
    type: str
    relationships: tuple[dict, ...] = ()


class GraphTraverser:
    """Neptune graph traversal for entity context enrichment."""

    def __init__(self, graph_client: GraphClient, graph_uri_template: str | None = None):
        """Bind the graph client and named-graph URI template for traversal.

        Args:
            graph_client: Graph client used to query entity relationships.
            graph_uri_template: Optional named-graph URI template; a default is
                resolved when None.
        """
        self._graph = graph_client
        self._graph_uri_template = get_graph_uri_template(graph_uri_template)

    async def traverse(self, query: str, namespace: str, entities: list[str] | None = None) -> list[GraphEntity]:
        """Keyword-based traversal: finds entities whose labels match extracted query terms."""
        validate_namespace(namespace)

        if not self._graph_uri_template:
            logger.warning("graph_traverser_no_template", namespace=namespace)
            return []

        if not entities:
            search_plan = build_query_search_plan(query, max_count=5)
            safe_entities = self._sanitize_entities(list(search_plan.terms))
            safe_containers = self._sanitize_containers(list(search_plan.containers))
        else:
            safe_entities = self._sanitize_entities(entities)
            safe_containers = []
        if not safe_entities and not safe_containers:
            logger.info(
                "graph_traverse_no_search_candidates",
                namespace=namespace,
                query_length=len(query),
                explicit_entity_count=len(entities or []),
            )
            return []

        sparql = self._build_label_query(namespace, safe_entities, safe_containers)
        results = await asyncio.wait_for(self._graph.query(sparql), timeout=_GRAPH_QUERY_TIMEOUT_S)
        parsed = self._parse_results(results)
        if len(results) >= _MAX_TRAVERSAL_RESULTS:
            # The row cap was reached, so Neptune's row order decided which matches
            # survived. Mirrors the count logging traverse_from_uris already does.
            logger.info(
                "graph_traverse_results_truncated",
                namespace=namespace,
                limit=_MAX_TRAVERSAL_RESULTS,
                term_count=len(safe_entities),
                container_count=len(safe_containers),
                entity_count=len(parsed),
            )
        return parsed

    async def traverse_from_uris(self, uris: list[str], namespace: str, max_hops: int = 2) -> list[GraphEntity]:
        """URI-based hop traversal: 1-2 hops outward from known entity URIs."""
        validate_namespace(namespace)

        if not self._graph_uri_template:
            logger.warning("graph_traverser_no_template", namespace=namespace)
            return []

        safe_uris = [u for u in uris if _SAFE_URI_RE.match(u)]
        if not safe_uris:
            logger.info("graph_traverse_uris_all_rejected", namespace=namespace, in_count=len(uris))
            return []

        sparql = self._build_hop_query(namespace, safe_uris[:10], max_hops)
        results = await asyncio.wait_for(self._graph.query(sparql), timeout=_GRAPH_QUERY_TIMEOUT_S)
        parsed = self._parse_hop_results(results)
        logger.info(
            "graph_traverse_from_uris_done",
            namespace=namespace,
            seed_count=len(safe_uris),
            row_count=len(results),
            entity_count=len(parsed),
        )
        return parsed

    @staticmethod
    def _sanitize_entities(entities: list[str]) -> list[str]:
        """Keep bounded semantic keywords that are safe in a SPARQL literal.

        Rejects on danger (control characters, line separators, over-length)
        rather than on an ASCII allowlist. A candidate must also contain an
        actual letter or number so punctuation-, emoji-, underscore-, or
        extender-only values cannot consume query budget. Quote and backslash
        remain valid inside a semantic term and are escaped by
        :meth:`_build_label_query`.

        Normalization is shared with :func:`extract_query_entities` and mirrors
        the stored-label comparison, ``LCASE(?label)``. In particular, this is
        ``lower()``, not ``casefold()`` or a query-only NFC/NFKC transform.
        """
        out: list[str] = []
        for entity in entities:
            folded = normalize_label_match_text(entity)
            if not folded or len(folded) > MAX_SEARCH_TOKEN_CODEPOINTS:
                continue
            if _UNSAFE_ENTITY_RE.search(folded) or not is_semantic_search_token(folded):
                continue
            out.append(folded)
        return out

    @staticmethod
    def _sanitize_containers(containers: list[str]) -> list[str]:
        """Keep reverse-containment haystacks that are safe in a SPARQL literal.

        A container is the query itself rather than a segmented token, so
        :data:`MAX_SEARCH_TOKEN_CODEPOINTS` deliberately does not apply — that bound
        exists to keep a single needle cheap, and enforcing it here would discard
        exactly the long no-space clauses reverse containment serves. The length is
        already bounded by :data:`MAX_QUERY_CODEPOINTS` at every ingress, which is
        re-asserted here because this method also runs for internal callers.
        """
        out: list[str] = []
        for container in containers:
            folded = normalize_label_match_text(container)
            if not folded or len(folded) > MAX_QUERY_CODEPOINTS:
                continue
            if _UNSAFE_ENTITY_RE.search(folded) or not is_semantic_search_token(folded):
                continue
            out.append(folded)
        return out

    def _graph_uri_prefix(self, namespace: str) -> str:
        """Build namespace graph URI prefix for STRSTARTS filtering.

        Neptune 1.2.0+ uses prefix-index matching for STRSTARTS on named graphs,
        so performance is comparable to exact GRAPH <uri> for small graph counts
        per namespace (typically <20). LIMIT clauses and _GRAPH_QUERY_TIMEOUT_S
        bound worst-case latency.
        """
        return namespace_graph_prefix(self._graph_uri_template, namespace)

    def _build_label_query(self, namespace: str, terms: list[str], containers: list[str] | None = None) -> str:
        forward_filters = [f'CONTAINS(LCASE(?label), "{escape_sparql_string_literal(term)}")' for term in terms]
        # ``STR()`` is required on the reverse arm and not on the forward one:
        # SPARQL argument compatibility accepts CONTAINS(lang-tagged, simple) but
        # rejects CONTAINS(simple, lang-tagged) as a type error, which FILTER then
        # swallows as false. Without STR() every ``@ja``/``@th`` label would be
        # invisible to reverse containment. STRLEN keeps a one-character label from
        # matching any query that happens to contain that character.
        reverse_filters = [
            f'(STRLEN(STR(?label)) >= 2 && CONTAINS("{escape_sparql_string_literal(container)}", LCASE(STR(?label))))'
            for container in containers or []
        ]
        filters = " || ".join(forward_filters + reverse_filters)
        graph_uri_prefix = self._graph_uri_prefix(namespace)
        # Ordering is for DETERMINISM only, deliberately not for relevance. The two
        # arms want opposite orders — a forward match is most specific when the
        # label is SHORTEST (an exact hit), a reverse match when the label is
        # LONGEST (a short one may have matched by straddling a word boundary) — so
        # any single sort key demotes one of them. Sorting by label length was tried
        # and put the exact ``claim`` hit last. ?entity keeps truncation reproducible
        # across identical requests without claiming a relevance order the query
        # cannot express.
        return f"""
        SELECT ?entity ?label (LANG(?label) AS ?labelLang) ?type ?predicate ?related
               ?relLabel (LANG(?relLabel) AS ?relLabelLang)
        WHERE {{
          GRAPH ?g {{
            ?entity rdfs:label ?label ;
                    a ?type .
            FILTER({filters})
            OPTIONAL {{
              ?entity ?predicate ?related .
              ?related rdfs:label ?relLabel .
            }}
          }}
          FILTER(STRSTARTS(STR(?g), "{graph_uri_prefix}"))
        }}
        ORDER BY ?entity
        LIMIT {_MAX_TRAVERSAL_RESULTS}
        """

    def _build_hop_query(self, namespace: str, uris: list[str], max_hops: int) -> str:
        values_clause = " ".join(f"<{uri}>" for uri in uris)
        graph_uri_prefix = self._graph_uri_prefix(namespace)

        # Induced (schema-only / T-Box) ontologies express class↔class edges NOT
        # as direct labelled ?seed→?class triples, but through property definition
        # nodes: a property P carries `P rdfs:domain ClassA` and `P rdfs:range
        # ClassB`, linking ClassA↔ClassB. The keyword path returns these property
        # nodes WITH their domain/range edges; the original hop pattern matched
        # only direct outgoing edges, so from a seed *class* it returned 0.
        #
        # Fix: surface the property nodes attached to the seed as first-class
        # ?entity results and expose EACH property's domain/range targets as its
        # relationships (?relPredicate/?related/?relLabel) — mirroring the keyword
        # path so the synthesizer sees the same class↔property↔class structure.
        # The direct-edge arms are retained for non-induced ontologies that DO
        # carry labelled class→class triples.
        dr = "(rdfs:domain|rdfs:range)"

        # ── 1-hop arms: the seed's immediate neighborhood ──────────────────
        # Cover both what the seed IS (property or class) and the direct
        # class→class edges a non-induced ontology may carry.
        arms = [
            # Seed IS a property node (the common case — the ontology vector
            # search returns mostly property URIs): emit the seed property as
            # ?entity with its own domain/range class targets as relationships.
            f"""{{
              ?seed {dr} ?anyClass .
              BIND(?seed AS ?entity)
              ?entity rdfs:label ?label .
              OPTIONAL {{ ?entity a ?type . }}
              OPTIONAL {{
                ?entity ?relPredicate ?related .
                ?related rdfs:label ?relLabel .
                FILTER(?relPredicate IN (rdfs:domain, rdfs:range))
              }}
            }}""",
            # Seed-as-property → its domain/range CLASSES, emitted as entities so
            # the class neighborhood is present (with the seed property as the
            # linking relationship back). Capture the ACTUAL predicate.
            """{
              ?seed ?relPredicate ?entity .
              FILTER(?relPredicate IN (rdfs:domain, rdfs:range))
              ?entity rdfs:label ?label .
              OPTIONAL { ?entity a ?type . }
              BIND(?seed AS ?related)
              OPTIONAL { ?seed rdfs:label ?relLabel . }
            }""",
            # Seed IS a class: property nodes whose domain/range is the seed
            # class. Emit the property as ?entity with its domain/range targets.
            f"""{{
              ?entity {dr} ?seed .
              ?entity rdfs:label ?label .
              OPTIONAL {{ ?entity a ?type . }}
              OPTIONAL {{
                ?entity ?relPredicate ?related .
                ?related rdfs:label ?relLabel .
                FILTER(?relPredicate IN (rdfs:domain, rdfs:range))
              }}
            }}""",
            # Direct outgoing edge (non-induced ontologies with class→class triples).
            """{
              ?seed ?relPredicate ?entity .
              ?entity rdfs:label ?label .
              OPTIONAL { ?entity a ?type . }
              BIND(?seed AS ?related)
            }""",
            # Direct incoming edge.
            """{
              ?entity ?relPredicate ?seed .
              ?entity rdfs:label ?label .
              OPTIONAL { ?entity a ?type . }
              BIND(?seed AS ?related)
            }""",
        ]

        # ── 2-hop arms: the seed's class neighborhood + those classes' own
        # properties (parity with the keyword path's breadth). Only when
        # max_hops >= 2 so a 1-hop caller gets a lighter, tighter query. ──────
        if max_hops >= 2:
            arms += [
                # Sibling properties of the seed-property's domain/range classes.
                f"""{{
              ?seed {dr} ?sharedClass .
              ?entity {dr} ?sharedClass .
              FILTER(?entity != ?seed)
              ?entity rdfs:label ?label .
              OPTIONAL {{ ?entity a ?type . }}
              OPTIONAL {{
                ?entity ?relPredicate ?related .
                ?related rdfs:label ?relLabel .
                FILTER(?relPredicate IN (rdfs:domain, rdfs:range))
              }}
            }}""",
                # Bridged classes: the class on the far side of a property that
                # also touches the seed, emitted as its own ?entity with the
                # bridging property as its relationship back (actual predicate).
                f"""{{
              ?bridge {dr} ?seed .
              ?bridge ?relPredicate ?entity .
              FILTER(?relPredicate IN (rdfs:domain, rdfs:range))
              ?entity rdfs:label ?label .
              OPTIONAL {{ ?entity a ?type . }}
              FILTER(?entity != ?seed)
              BIND(?bridge AS ?related)
              OPTIONAL {{ ?bridge rdfs:label ?relLabel . }}
            }}""",
                # Property nodes of the bridged classes: for each class reachable
                # from the seed via a bridging property, also emit ITS property
                # nodes (full class+property neighborhood parity), each with its
                # own domain/range targets as relationships.
                f"""{{
              ?bridge {dr} ?seed .
              ?bridge {dr} ?nbrClass .
              FILTER(?nbrClass != ?seed)
              ?entity {dr} ?nbrClass .
              ?entity rdfs:label ?label .
              OPTIONAL {{ ?entity a ?type . }}
              OPTIONAL {{
                ?entity ?relPredicate ?related .
                ?related rdfs:label ?relLabel .
                FILTER(?relPredicate IN (rdfs:domain, rdfs:range))
              }}
            }}""",
            ]

        union_body = " UNION ".join(arms)
        # LIMIT bounds the OUTPUT rows, not the intermediate join the 2-hop arms
        # build first, so on a very large induced ontology the join can still be
        # expensive. That worst case is capped by _GRAPH_QUERY_TIMEOUT_S (query
        # aborted at 15s); the seed set is already capped at 10 URIs, keeping the
        # common case cheap.
        return f"""
        SELECT ?entity ?label (LANG(?label) AS ?labelLang) ?type ?relPredicate ?related
               ?relLabel (LANG(?relLabel) AS ?relLabelLang)
        WHERE {{
          GRAPH ?g {{
            VALUES ?seed {{ {values_clause} }}
            {union_body}
          }}
          FILTER(STRSTARTS(STR(?g), "{graph_uri_prefix}"))
        }} LIMIT {_MAX_HOP_RESULTS}
        """

    def _parse_results(self, results: list[dict]) -> list[GraphEntity]:
        return _group_entities(results, predicate_var="predicate")

    def _parse_hop_results(self, results: list[dict]) -> list[GraphEntity]:
        return _group_entities(results, predicate_var="relPredicate")


def _group_entities(results: list[dict], *, predicate_var: str) -> list[GraphEntity]:
    """Fold traversal rows into one :class:`GraphEntity` per entity URI.

    A resource with several ``rdfs:label`` values arrives once per label (and a
    related node once per ITS labels). Labels are collected per URI and chosen
    with the request's namespace-language preference (LLD §2.2.5), so the label
    does not depend on row order and no entity or relationship is dropped for its
    language. Entities keep first-seen order; each ``(predicate, target)``
    relationship appears once — the induced graph often carries the same
    domain/range triple twice, and a property can bridge to several classes.
    """
    preference = current_label_preference()
    types: dict[str, str] = {}
    labels: dict[str, list[tuple[str, str]]] = {}
    rels: dict[str, dict[tuple[str, str], list[tuple[str, str]]]] = {}
    for row in results:
        uri = row.get("entity", "")
        if not uri:
            continue
        if uri not in types:
            types[uri] = row.get("type", "")
            labels[uri] = []
            rels[uri] = {}
        elif not types[uri] and row.get("type"):
            types[uri] = row["type"]
        label = row_label(row, "label")
        if label[0]:
            labels[uri].append(label)
        if row.get(predicate_var) and row.get("related"):
            target_labels = rels[uri].setdefault((row[predicate_var], row["related"]), [])
            target_label = row_label(row, "relLabel")
            if target_label[0]:
                target_labels.append(target_label)
    return [
        GraphEntity(
            uri=uri,
            label=preference.pick(labels[uri]),
            type=types[uri],
            relationships=tuple(
                {"predicate": predicate, "target_uri": target, "target_label": preference.pick(target_labels)}
                for (predicate, target), target_labels in rels[uri].items()
            ),
        )
        for uri in types
    ]
