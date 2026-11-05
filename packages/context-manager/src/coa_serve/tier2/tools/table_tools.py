# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier-2 table discovery tools — ``search_tables`` and ``get_table_schema``.

Two reusable Tier-2 functions over the serve clients (a ``VectorClient`` for
k-NN over the ontology index and an ``LLMClient`` for the query embedding),
bound to one namespace/index for the life of a request.

Both are exposed as methods of :class:`TableCatalog` rather than free functions
because they share one thing: the candidate cache. ``search_tables`` populates
it, ``get_table_schema`` reads it (falling back to a targeted search on a miss),
and downstream data-source routing needs the union of every hit seen so far.
Constructing the catalog per request keeps that state request-scoped.

Retrieval is restricted to R2RML-MAPPED classes (a non-empty
``data_source_id``): an unmapped class has no backing table, so authoring SQL
against it would target a nonexistent table.

Errors are RAISED here — a client backend failure is not something these
functions can meaningfully paper over. Callers that must not raise (an LLM tool
loop, an MCP handler) translate the exception into their own protocol.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from coa_common.constants import StructuredBackend

from ...clients.base import LLMClient, VectorClient, VectorHit

# Within-tier reuse of the NL→SQL retrieval helpers: table-name parsing and
# hit→source attribution must stay byte-identical to the single-shot strategy,
# so these are imported rather than reimplemented.
from ..nl_to_sql.sql_generator import (
    _extract_table_names,
    _resolve_data_source_from_hits,
    _resolve_data_source_from_sql,
)

logger = structlog.get_logger(__name__)

# Candidates returned to a caller per search. The default matches the Tier-2
# single-shot retrieval_k; the cap bounds how much schema text one tool call can
# push into an agent's context.
DEFAULT_TOP_K = 7
MAX_TOP_K = 12

# The k-NN pool is over-fetched before the mapped filter is applied client-side,
# so a namespace whose top hits are unmapped still yields `top_k` usable tables.
_POOL_MULTIPLIER = 5
_MIN_POOL = 40

# Characters of a table's stored text used as the search-result preview. Enough
# to judge relevance; the full schema comes from get_table_schema.
_PREVIEW_CHARS = 160


@dataclass(frozen=True)
class TableCandidate:
    """One mapped table surfaced by :meth:`TableCatalog.search_tables`.

    Attributes:
        table: Lowercased table name.
        preview: Leading slice of the table's stored context text.
    """

    table: str
    preview: str


class TableCatalog:
    """Table discovery + schema inspection over one namespace's ontology index."""

    def __init__(
        self,
        vector_client: VectorClient,
        llm_client: LLMClient,
        *,
        namespace: str,
        index: str | None = None,
    ):
        """Bind the tools to the serve clients and the request's namespace.

        Args:
            vector_client: OpenSearch k-NN client over the ontology index.
            llm_client: Embedding client used to vectorize search phrases.
            namespace: Namespace to scope retrieval to.
            index: Resolved per-namespace ontology index name, or None for the
                client's default.
        """
        self._vector = vector_client
        self._llm = llm_client
        self._namespace = namespace
        self._index = index
        self._hits_by_table: dict[str, VectorHit] = {}
        self._all_hits: list[VectorHit] = []

    async def search_tables(self, search: str, top_k: int = DEFAULT_TOP_K) -> list[TableCandidate]:
        """Find mapped tables semantically related to ``search``.

        Args:
            search: Natural-language description of the data/tables needed.
            top_k: Candidates to return; clamped to [1, :data:`MAX_TOP_K`].

        Returns:
            Mapped table candidates, most similar first.
        """
        top_k = min(max(int(top_k), 1), MAX_TOP_K)
        embedding = await self._llm.embed(search)
        raw = await self._vector.search(
            embedding,
            namespace=self._namespace,
            top_k=max(top_k * _POOL_MULTIPLIER, _MIN_POOL),
            index=self._index,
            entity_type="class",
            # A graph class carries a data_source_id too; it is not a SQL table.
            query_backend=StructuredBackend.SQL,
        )
        mapped = [hit for hit in raw if hit.data_source_id][:top_k]
        candidates: list[TableCandidate] = []
        for hit in mapped:
            name = self._remember(hit)
            candidates.append(
                TableCandidate(table=name, preview=(hit.metadata.get("context_text") or hit.text)[:_PREVIEW_CHARS])
            )
        logger.debug("tier2_search_tables", namespace=self._namespace, pool=len(raw), kept=len(candidates), top_k=top_k)
        return candidates

    async def get_table_schema(self, table_name: str) -> str:
        """Return the full stored schema text for one table, or "" if unknown.

        The text carries columns with types, FK relationships and per-column
        allowed values — the context the SQL writer needs to avoid guessing.

        Args:
            table_name: Exact table name (case-insensitive).

        Returns:
            The table's schema text, or an empty string when no mapped class
            matches the name.
        """
        name = table_name.strip().lower()
        hit = self._hits_by_table.get(name)
        if hit is None:
            hit = await self._lookup_uncached(name)
        if hit is None:
            return ""
        return hit.metadata.get("context_text") or hit.text

    async def _lookup_uncached(self, name: str) -> VectorHit | None:
        """Targeted search for a table the cache has not seen yet.

        Lets a caller inspect a table it learned about elsewhere (a FK comment
        in another table's schema, a prior turn) without first re-running
        ``search_tables``. Retrieval failures are swallowed here — a miss is
        reported as "unknown table", which the caller already handles.
        """
        try:
            embedding = await self._llm.embed(name)
            raw = await self._vector.search(
                embedding,
                namespace=self._namespace,
                top_k=_MIN_POOL,
                index=self._index,
                entity_type="class",
                query_backend=StructuredBackend.SQL,
            )
        except Exception as e:
            logger.debug("tier2_table_lookup_failed", table=name, error=type(e).__name__, message=str(e)[:200])
            return None
        for candidate in raw:
            names = _extract_table_names([candidate])
            if names and names[0] == name and candidate.data_source_id:
                self._remember(candidate)
                return candidate
        return None

    def _remember(self, hit: VectorHit) -> str:
        """Cache a hit under its table name; returns that name (may be "")."""
        self._all_hits.append(hit)
        names = _extract_table_names([hit])
        if not names:
            return ""
        self._hits_by_table[names[0]] = hit
        return names[0]

    def hits_for_tables(self, table_names: Sequence[str]) -> tuple[list[VectorHit], list[str]]:
        """Split requested table names into cached hits and unknown names.

        Args:
            table_names: Names to look up (case-insensitive).

        Returns:
            ``(hits, missing)`` — the cached hits for known tables, and the
            names with no cached hit.
        """
        hits: list[VectorHit] = []
        missing: list[str] = []
        for name in table_names or []:
            hit = self._hits_by_table.get(str(name).strip().lower())
            if hit is not None:
                hits.append(hit)
            else:
                missing.append(str(name))
        return hits, missing

    def resolve_data_source(self, sql: str = "") -> str:
        """Resolve the data source for ``sql`` from every hit seen so far.

        Prefers attribution by the tables the statement actually references
        (precise even when retrieval mixed sources), falling back to whole-pool
        agreement. Returns "" when the source cannot be pinned unambiguously —
        never a guess, since a wrong source mis-routes the query.

        Args:
            sql: Statement to attribute; omit to use pool agreement only.

        Returns:
            The resolved ``data_source_id``, or "".
        """
        from_sql = _resolve_data_source_from_sql(sql, self._all_hits) if sql else ""
        return from_sql or _resolve_data_source_from_hits(self._all_hits)

    @property
    def known_tables(self) -> list[str]:
        """Sorted names of every table cached so far."""
        return sorted(self._hits_by_table.keys())

    @property
    def hits(self) -> list[VectorHit]:
        """Every hit seen so far, in the order retrieved."""
        return list(self._all_hits)
