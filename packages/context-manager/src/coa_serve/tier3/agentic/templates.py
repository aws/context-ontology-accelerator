# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Prepared, parameterized graph-query templates for the Agentic Retriever.

A Query_Template is a *prepared* retrieval pattern the agent fills in rather than
authoring a graph query from scratch (Req 5). Preferring a template over an
Ad_Hoc_Query keeps graph retrieval reliable and avoids query-generation errors
(Req 5.1-5.3). This module owns the templates the ``Graph_Traversal_Tool`` runs,
including the one required by Requirement 5.5 — the edge-typed traversal.

Which graph these target (load-bearing — see the design's "Two graphs")
----------------------------------------------------------------------
These are **openCypher** templates over the graphrag-toolkit **property graph**
(the graph kg-build populates in Neptune Database), NOT SPARQL over the RDF
ontology graph. They are executed through the toolkit ``GraphStore`` (which
speaks openCypher to Neptune), never through the serve layer's SPARQL
``GraphTraverser``/``NeptuneGraphClient``.

Property-graph schema these templates assume (verified against the toolkit
``EntityGraphBuilder`` / ``EntityRelationGraphBuilder`` / ``StatementGraphBuilder``):

- Entity node label ``__Entity__``, **tenant-scoped** — the caller formats it via
  ``TenantId(tenant).format_label("__Entity__")`` and passes the already
  backtick-wrapped label as ``entity_label``. The entity's ``entityId`` is stored
  as the Neptune node id, so entities are matched/returned with ``id(...)`` (true
  for both Neptune Database and Neptune Analytics, which both key entities by
  ``~id``).
- Entity properties: ``value`` (display name) and ``class`` (classification).
- Entity↔entity relations: ``(:__Entity__)-[r:`__RELATION__`]->(:__Entity__)``.
  There is a single relationship type ``__RELATION__``. ``r.value`` is its
  human-readable predicate label; when present, ``r.predicateUri`` is the actual
  predicate identifier and must remain distinct from that label.
- Connected-node text: an entity's supporting statements are reached via
  ``(:__Entity__)-[:`__SUBJECT__`|`__OBJECT__`]->()-[:`__SUPPORTS__`]->(:__Statement__)``;
  the statement text is the ``__Statement__.value`` property. The statement label
  is also tenant-scoped (``statement_label``).

Import discipline (Requirement 12.3): this module is dependency-free — it holds
only template strings, bound constants, and a normalization helper, and never
imports ``graphrag_toolkit`` (nor anything that does), so importing it never pulls
the toolkit into ``sys.modules``.

Parameter discipline: entity ids and edge types are passed to Neptune as
openCypher **query parameters** (``$entity_ids``, ``$edge_types_norm``,
``$terms``) — never interpolated into the query text — so there is no
query-injection surface. Only the tenant-scoped labels (derived internally, never
from caller input) and the integer ``limit`` are formatted into the query text.

Why the traversal templates use one id-anchored undirected match
----------------------------------------------------------------
The three ``__RELATION__`` templates start from one variable only:
``MATCH (seed)-[r]-(neighbor) WHERE id(seed) IN $entity_ids``. This keeps the
lookup anchored on the caller's bounded id set instead of asking the planner to
evaluate an ``id(source) ... OR id(target) ...`` predicate over two variables.

An undirected match alone cannot tell which endpoint the edge actually points
FROM. The templates therefore recover the stored direction with the standard
openCypher ``startNode(r)`` / ``endNode(r)`` functions before returning
``sourceUri`` and ``targetUri``. ``WITH DISTINCT`` removes the duplicate match
produced when both endpoints are seeds while preserving distinct parallel
relationships (the relationship value ``r`` is part of the distinct key).

The connected-node choice deliberately keeps the previous directed-query
semantics: when the stored source is a seed, the target is connected; otherwise
the source is connected. The templates retain their tenant-scoped labels,
``$entity_ids`` bound of :data:`MAX_ENTITIES`, and global ``LIMIT {limit}``
bounded by :data:`MAX_TRAVERSAL_LIMIT`.
"""

from __future__ import annotations

# Inclusive parameter bounds for the edge-typed traversal template (Req 5.5).
# The Graph_Traversal_Tool enforces these before populating the template so an
# out-of-bounds request is never assembled into a query.
MIN_ENTITIES: int = 1
MAX_ENTITIES: int = 100
MIN_EDGE_TYPES: int = 1
MAX_EDGE_TYPES: int = 50

# Default row cap for a traversal result set.
MIN_TRAVERSAL_LIMIT: int = 1
MAX_TRAVERSAL_LIMIT: int = 100
DEFAULT_EDGE_TYPED_LIMIT: int = 100

# A graph statement is copied into both the entity's ``supportingStatements``
# property and a synthesis chunk. Keep the graph-store result itself bounded,
# before Python or the LLM can accumulate a high-degree entity's full statement
# fan-out. The query asks for one extra row so the caller can distinguish an
# exact fit from truncation.
MAX_CONNECTED_TEXT_ROWS: int = 100
CONNECTED_TEXT_QUERY_LIMIT: int = MAX_CONNECTED_TEXT_ROWS + 1

# ── Chunk-window expansion bounds (idea 6) ─────────────────────────────
# Inclusive bounds for the chunk-window expansion template. Enforced by the
# Chunk_Expansion_Tool before a query is assembled.
MIN_CHUNK_IDS: int = 1
MAX_CHUNK_IDS: int = 50
DEFAULT_CHUNK_WINDOW_HOPS: int = 1
MAX_CHUNK_WINDOW_HOPS: int = 5
DEFAULT_CHUNK_WINDOW_LIMIT: int = 50


def normalize_edge_type(value: str) -> str:
    """Normalize an edge-type label for cross-graph matching (Req 3.9 mapping).

    The RDF ontology's edge-type labels and the property graph's
    ``__RELATION__.value`` predicate strings come from independent vocabularies,
    so they are compared on a normalized form rather than byte-for-byte. This
    Python normalization MUST stay identical to the openCypher normalization
    applied to ``r.value`` in the templates below (``_CYPHER_EDGE_NORM``):
    lowercase, then map spaces and hyphens to underscores.

    Args:
        value: An edge-type label (from the ontology) or a raw ``r.value``.

    Returns:
        The normalized comparison key.
    """
    return value.lower().replace(" ", "_").replace("-", "_")


# The openCypher expression that normalizes a stored ``r.value`` the same way
# :func:`normalize_edge_type` normalizes the requested edge types. Kept as a
# named constant so the two normalizations cannot drift.
_CYPHER_EDGE_NORM: str = "toLower(replace(replace(r.value, ' ', '_'), '-', '_'))"


# The Requirement 5.5 prepared template: traverse the given edge types from the
# given entities and return the connected nodes. Supporting statement text is
# fetched by :data:`CONNECTED_TEXT_RETRIEVAL` in a second, independently bounded
# query. Keeping text out of this relationship query prevents one high-degree
# entity from multiplying or hiding otherwise valid relationship rows.
#
# The single-variable id anchor and direction recovery are explained under "Why
# the traversal templates use one id-anchored undirected match" in this module's
# docstring. Same for URI_EXPANSION and ENTITY_EDGE_TYPES below.
EDGE_TYPED_TRAVERSAL: str = (
    "MATCH (seed:{entity_label})-[r:`__RELATION__`]-(neighbor:{entity_label})\n"
    "WHERE id(seed) IN $entity_ids\n"
    "  AND " + _CYPHER_EDGE_NORM + " IN $edge_types_norm\n"
    "WITH DISTINCT startNode(r) AS source, r, endNode(r) AS target\n"
    "WITH source, r, target,\n"
    "     CASE WHEN id(source) IN $entity_ids THEN target ELSE source END AS connected\n"
    "RETURN id(source) AS sourceUri, source.value AS sourceLabel,\n"
    "       r.predicateUri AS predicateUri, r.value AS predicateLabel,\n"
    "       id(target) AS targetUri, target.value AS targetLabel,\n"
    "       id(connected) AS connectedUri, connected.value AS connectedLabel,\n"
    "       connected.class AS connectedClass\n"
    "LIMIT {limit}"
)


# Fetch supporting statements for the already-bounded connected entity set.
# This deliberately returns one statement per row instead of using ``collect``:
# a Cypher aggregation would materialize every statement before a trailing
# ``LIMIT`` could apply. ``limit`` is always
# :data:`CONNECTED_TEXT_QUERY_LIMIT`, so the backend and the Python consumer both
# have a hard result bound and the extra row truthfully signals degradation.
#
# ``DISTINCT`` is required because the match runs through facts: a statement that
# supports several facts of the same entity would otherwise come back once per
# fact, spend the row limit on duplicates, and report a cap that was never reached.
CONNECTED_TEXT_RETRIEVAL: str = (
    "MATCH (connected:{entity_label})-[:`__SUBJECT__`|`__OBJECT__`]->()-[:`__SUPPORTS__`]->"
    "(st:{statement_label})\n"
    "WHERE id(connected) IN $connected_ids\n"
    "RETURN DISTINCT id(connected) AS connectedUri, st.value AS connectedText\n"
    "LIMIT {limit}"
)


# ``uris`` mode: expand one hop over any ``__RELATION__`` edge from the given
# entity ids (no edge-type constraint). Same connected-node shape as the
# edge-typed template minus the text hop.
URI_EXPANSION: str = (
    "MATCH (seed:{entity_label})-[r:`__RELATION__`]-(neighbor:{entity_label})\n"
    "WHERE id(seed) IN $entity_ids\n"
    "WITH DISTINCT startNode(r) AS source, r, endNode(r) AS target\n"
    "WITH source, r, target,\n"
    "     CASE WHEN id(source) IN $entity_ids THEN target ELSE source END AS connected\n"
    "RETURN id(source) AS sourceUri, source.value AS sourceLabel,\n"
    "       r.predicateUri AS predicateUri, r.value AS predicateLabel,\n"
    "       id(target) AS targetUri, target.value AS targetLabel,\n"
    "       id(connected) AS connectedUri, connected.value AS connectedLabel,\n"
    "       connected.class AS connectedClass\n"
    "LIMIT {limit}"
)


# ``edge_types`` mode: discover the edge-type predicates actually PRESENT on the
# given entities in the graph, with an occurrence count so the planner can prefer
# the most common ones. This exists because the induced ontology is a heavily-
# pruned subset of the graph's predicate vocabulary (~14% in measured cases), so
# the planner — shown only ontology edges — cannot otherwise propose the majority
# of real, relevant predicates (e.g. financial relations like "HAS SEGMENT").
# Returns the RAW ``r.value`` (not normalized) so the planner sees real labels; the
# edge-typed traversal re-normalizes on match. ``$entity_ids`` is a query
# parameter; ``limit`` is an int.
ENTITY_EDGE_TYPES: str = (
    "MATCH (seed:{entity_label})-[r:`__RELATION__`]-(neighbor:{entity_label})\n"
    "WHERE id(seed) IN $entity_ids\n"
    "WITH DISTINCT r\n"
    "RETURN r.value AS edge, count(*) AS freq\n"
    "ORDER BY freq DESC\n"
    "LIMIT {limit}"
)


# ``keyword`` mode: match entities by their persisted ``search_str``. ``$terms``
# are per-word forward candidates (``search_str CONTAINS term``); ``$containers``
# are the query's non-Latin script runs for the reverse direction (``container
# CONTAINS search_str``), which is how a stored label is found inside a Chinese,
# Japanese, Thai or Korean clause the serve layer cannot segment. ``$probes`` is the
# union in source order, because Neptune openCypher supports neither the ``any()``
# list predicate nor two UNWIND arms in one WHERE. All three values are parameters,
# never interpolated.
KEYWORD_ENTITY_SEARCH: str = (
    "UNWIND $probes AS probe\n"
    "MATCH (e:{entity_label})\n"
    "WHERE probe IN $terms AND e.search_str CONTAINS probe\n"
    "RETURN DISTINCT id(e) AS uri, e.value AS label, e.class AS class\n"
    "LIMIT {limit}"
)


# The reverse arm is a SEPARATE template so a query with no containers never ships
# it. ``size()`` on a string is supported by Neptune ("this overloaded method
# currently only works for pattern expressions, lists, and strings" — openCypher
# specification compliance), but it is the only use of it here, and a rejected
# construct would degrade keyword mode for EVERY language rather than only the
# scripts that need the reverse direction.
#
# The floor is counted over the search string WITHOUT spaces, unlike the SPARQL
# sinks' ``STRLEN(STR(?label)) >= 2``. The toolkit's ``search_str`` normalizer
# replaces every combining mark with a space, so a Thai or Indic label arrives
# shredded (``รุ่น`` -> ``ร น``); counting raw length would admit one-character
# fragments that appear inside almost any container of the same script.
KEYWORD_ENTITY_SEARCH_WITH_CONTAINERS: str = (
    "UNWIND $probes AS probe\n"
    "MATCH (e:{entity_label})\n"
    "WHERE (probe IN $terms AND e.search_str CONTAINS probe)\n"
    "   OR (probe IN $containers AND e.search_str IS NOT NULL\n"
    "       AND size(replace(e.search_str, ' ', '')) >= 2\n"
    "       AND probe CONTAINS e.search_str)\n"
    "RETURN DISTINCT id(e) AS uri, e.value AS label, e.class AS class\n"
    "LIMIT {limit}"
)


# Chunk-window expansion (idea 6): given chunk ids the agent deemed relevant,
# return the chunks PHYSICALLY ADJACENT in the source document by traversing the
# toolkit's chunk-sequence edges (`__NEXT__` / `__PREVIOUS__`) up to `hops` hops.
# Verified on the dev graph: chunks are keyed by the Neptune node id (the
# ``chunkId`` property is null), and the chunk text lives in ``c.value`` — so
# seeds are matched and neighbors returned via ``id(...)``. Every chunk is linked
# to its tenant-scoped ``__Source__`` node by ``__EXTRACTED_FROM__``; the query
# returns the source identity and display-name fields so document-window text can
# be cited rather than synthesized anonymously.
# ``chunk_label`` / ``source_label`` are tenant-scoped labels; ``$chunk_ids`` is
# a query parameter; ``hops``/``limit`` are ints. The seed chunks themselves are
# excluded from the result (they are already in the accumulated context). An
# undirected match (`-[...]-`) returns neighbors on BOTH sides (before and after).
#
# The source join is OPTIONAL on purpose. As a mandatory ``MATCH`` it silently
# converted a SCHEMA MISMATCH into an empty success: a namespace ingested before
# the ``__EXTRACTED_FROM__`` / ``__Source__`` shape existed — or one whose source
# label is spelled differently — returned zero rows, so the tool reported
# "expanded N chunk(s) -> 0 adjacent chunk(s)" with status ``succeeded``, no drop
# count, and no degraded reason, while chunk-window expansion contributed nothing
# to every deep-reasoning query. With ``OPTIONAL MATCH`` the adjacency rows still
# come back and ``sourceDocumentId`` arrives null, which the tool counts as a
# dropped anonymous chunk and reports as ``missing_document_provenance``. The
# anonymous rows are still never used as document citations.
CHUNK_WINDOW_EXPANSION: str = (
    "MATCH (c:{chunk_label})\n"
    "WHERE id(c) IN $chunk_ids\n"
    "MATCH (c)-[:`__NEXT__`|`__PREVIOUS__`*1..{hops}]-(adj:{chunk_label})\n"
    "WHERE NOT id(adj) IN $chunk_ids\n"
    "OPTIONAL MATCH (adj)-[:`__EXTRACTED_FROM__`]->(source:{source_label})\n"
    "RETURN DISTINCT id(adj) AS chunkId, adj.value AS text,\n"
    "       id(source) AS sourceDocumentId,\n"
    "       source.source_s3_key AS sourceS3Key,\n"
    "       source.filename AS sourceFilename\n"
    "LIMIT {limit}"
)
