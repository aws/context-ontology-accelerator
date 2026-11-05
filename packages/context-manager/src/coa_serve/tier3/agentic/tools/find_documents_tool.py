# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Find_Documents_Tool: route named terms from the question to their source documents.

Questions that name a specific identifier, person, product, system or document
subject are answered by the document that MENTIONS it, but the
reasoning loop otherwise reaches documents only through retrieved summaries
(topic/statement chunks), which may not carry the name at all. This tool takes
the names / identifiers / quoted phrases the planner (or the session's term
extraction) pulled from the question and finds the documents that mention them
through two independent channels:

1. **Graph** — entities whose normalized name equals or contains a term (exact
   matches rank higher; optionally of one class), walked through the lexical
   graph's mention path Entity → Fact → Statement → Chunk → Source to the raw
   chunk text and the source document (``GraphTraversalTool.find_entity_mentions``).
2. **Keyword** — an exact-phrase BM25 search over the raw chunk text field of the
   namespace's chunk index (``OpenSearchVectorClient.phrase_search``).

Both are merged into ONE document ranking. A document's rank key is, in order:
the number of DISTINCT query terms it matched (a document mentioning several of
the question's names is the likeliest answer source), its number of distinct
mentioning chunks, the best entity-match quality (exact > contains > keyword
only) and the best keyword score. The top documents are returned with their
matched terms and one or two raw mention snippets (with chunk ids), and the
mention chunks themselves are returned as evidence so the synthesizer sees the
verbatim text and read_document can anchor on them.

Namespace scoping: the graph anchor label and the chunk index are tenant-scoped,
and every returned key is additionally checked against the bucket/prefixes of
the namespace's registered DOCUMENTS sources (as read_document does); documents
outside them are dropped. Bounded: at most :data:`MAX_TERMS` terms, entity/row
limits on the graph query, the search proxy's result cap, and a per-channel
timeout inside the controller's per-tool timeout. A failing channel degrades to
the other one; the tool never raises.

Import discipline (Requirement 12.3): stdlib + ``structlog`` + sibling
dependency-free modules only.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from ...vector_retriever import ChunkResult
from ..models import NAMED_TERM_KINDS
from .base import ToolResult
from .document_tool import ReadDocumentTool

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import ToolRegistry

logger = structlog.get_logger(__name__)

TOOL_NAME = "find_documents"

# Bounds on one lookup, all chosen so a call stays cheap and its output small:
# - at most MAX_TERMS terms of _MAX_TERM_CHARS each (names, ids, short phrases);
# - DEFAULT_TOP_DOCUMENTS documents returned (a caller may ask for up to
#   _MAX_TOP_DOCUMENTS), each with up to _SNIPPETS_PER_DOCUMENT raw mention chunks
#   of at most _EVIDENCE_CHUNK_CHARS (the evidence), shown to the planner as
#   _SNIPPET_CHARS-character snippets;
# - each channel (graph, keyword search) gets _CHANNEL_TIMEOUT_S and the keyword
#   search returns at most _KEYWORD_SIZE hits, so a slow or broad term cannot hold
#   the loop.
MAX_TERMS = 8
_MAX_TERM_CHARS = 120
DEFAULT_TOP_DOCUMENTS = 5
_MAX_TOP_DOCUMENTS = 10
_SNIPPETS_PER_DOCUMENT = 2
_SNIPPET_CHARS = 300
_EVIDENCE_CHUNK_CHARS = 2500
_CHANNEL_TIMEOUT_S = 15.0
_KEYWORD_SIZE = 50

_DESCRIPTION = (
    f"Find the source documents that MENTION specific names from the question: {NAMED_TERM_KINDS}. "
    "Matches graph entities by name and searches the raw document text for "
    "the exact phrases, then returns the best-matching documents with the terms each "
    "matched and raw mention snippets (with chunk ids). Use it BEFORE topic or chunk "
    "searches when the question names something specific; then read the top document "
    "with read_document anchored on a returned chunk id."
)
_INPUT_SCHEMA: dict = {
    "terms": "list[str] (names / identifiers / quoted phrases copied from the question, 1-8 items)",
    "entity_class": "str (optional entity class to restrict the name match to, e.g. a class from the ontology)",
    "top_k": f"int (documents to return; default {DEFAULT_TOP_DOCUMENTS})",
}
_OUTPUT_SCHEMA: dict = {"items": "list (one document_match per document)", "chunks": "list (mention chunks)"}


def _clean_terms(terms: Any) -> list[str]:
    """De-duplicated, trimmed, bounded terms (strings only)."""
    if isinstance(terms, str):
        terms = [terms]
    out: list[str] = []
    seen: set[str] = set()
    for term in terms or []:
        if not isinstance(term, str):
            continue
        text = term.strip().strip("\"'“”‘’")[:_MAX_TERM_CHARS].strip()
        if len(text) < 2 or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
        if len(out) >= MAX_TERMS:
            break
    return out


class FindDocumentsTool:
    """Names → source documents, through the graph and an exact keyword search.

    Args:
        sources_registry: The serve-layer ``SourcesRegistry`` (scopes keys).
        graph_tool: Optional object with ``find_entity_mentions`` (the graph
            traversal tool); the graph channel is skipped without it.
        vector_client: Optional object with ``phrase_search`` (the OpenSearch
            client); the keyword channel is skipped without it.
        chunk_index_for: Maps a namespace to its chunk index name.
    """

    def __init__(
        self,
        sources_registry: Any,
        *,
        graph_tool: Any | None = None,
        vector_client: Any | None = None,
        chunk_index_for: Any | None = None,
    ) -> None:
        """Bind the registry and the two optional search channels."""
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._registry = sources_registry
        self._graph = graph_tool if callable(getattr(graph_tool, "find_entity_mentions", None)) else None
        self._vector = vector_client if callable(getattr(vector_client, "phrase_search", None)) else None
        self._chunk_index_for = chunk_index_for

    async def invoke(
        self,
        *,
        namespace: str,
        terms: list[str] | None = None,
        entity_class: str = "",
        top_k: int = DEFAULT_TOP_DOCUMENTS,
        **_: Any,
    ) -> ToolResult:
        """Return the documents mentioning ``terms``, best first (see module doc)."""
        clean = _clean_terms(terms)
        if not clean:
            return ToolResult(detail=f"{self.name}: no usable terms supplied")
        requested = top_k if isinstance(top_k, int) and not isinstance(top_k, bool) else DEFAULT_TOP_DOCUMENTS
        top = max(1, min(_MAX_TOP_DOCUMENTS, requested))
        cls = entity_class.strip() if isinstance(entity_class, str) else ""

        graph_rows, keyword_hits, notes = await self._search(namespace, clean, cls)
        try:
            sources = await self._registry.document_sources(namespace)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "find_documents_sources_error", namespace=namespace, error=str(exc)[:200], error_type=type(exc).__name__
            )
            sources = None
        if sources is None:
            return ToolResult(detail=f"{self.name} refused: document sources for this namespace are unavailable")

        docs = self._merge(clean, graph_rows, keyword_hits)
        matches: list[dict] = []
        chunks: list[ChunkResult] = []
        for doc in docs:
            bucket = ReadDocumentTool.authorize(sources, "", doc["source_key"])
            if not bucket:
                continue
            match, evidence = self._render(doc, bucket)
            matches.append(match)
            chunks.extend(evidence)
            if len(matches) >= top:
                break

        logger.info(
            "find_documents",
            namespace=namespace,
            terms=len(clean),
            graph_rows=len(graph_rows),
            keyword_hits=len(keyword_hits),
            documents=len(matches),
        )
        if not matches:
            return ToolResult(detail=f"{self.name}: no document mentions {clean}{notes}")
        listing = "; ".join(
            f"{i}. {m['source_key']} [terms: {', '.join(m['matched_terms'])}; {m['mentions']} mention(s)]"
            for i, m in enumerate(matches, 1)
        )
        return ToolResult(
            chunks=tuple(chunks),
            items=tuple(matches),
            detail=f"{self.name}: {len(matches)} document(s) — {listing}{notes}"[:1500],
        )

    # ── channels ───────────────────────────────────────────────────────

    async def _search(self, namespace: str, terms: list[str], cls: str) -> tuple[list[dict], list[dict], str]:
        """Run both channels concurrently, each under its own timeout; degrade per channel."""

        async def graph() -> list[dict]:
            if self._graph is None:
                return []
            return await asyncio.wait_for(
                self._graph.find_entity_mentions(namespace, terms, entity_class=cls), timeout=_CHANNEL_TIMEOUT_S
            )

        async def keyword() -> list[dict]:
            if self._vector is None or self._chunk_index_for is None:
                return []
            return await asyncio.wait_for(
                self._vector.phrase_search(terms, index=self._chunk_index_for(namespace), size=_KEYWORD_SIZE),
                timeout=_CHANNEL_TIMEOUT_S,
            )

        graph_res, keyword_res = await asyncio.gather(graph(), keyword(), return_exceptions=True)
        notes = ""
        if isinstance(graph_res, BaseException):
            logger.warning(
                "find_documents_graph_failed",
                namespace=namespace,
                error=str(graph_res)[:200],
                error_type=type(graph_res).__name__,
            )
            notes += f" (graph search failed: {type(graph_res).__name__})"
            graph_res = []
        if isinstance(keyword_res, BaseException):
            logger.warning(
                "find_documents_keyword_failed",
                namespace=namespace,
                error=str(keyword_res)[:200],
                error_type=type(keyword_res).__name__,
            )
            notes += f" (keyword search failed: {type(keyword_res).__name__})"
            keyword_res = []
        return list(graph_res or []), list(keyword_res or []), notes

    # ── ranking ────────────────────────────────────────────────────────

    @staticmethod
    def _merge(terms: list[str], graph_rows: list[dict], keyword_hits: list[dict]) -> list[dict]:
        """Pool both channels' mentions per source document and rank them (see module doc)."""
        docs: dict[str, dict] = {}

        def entry(key: str) -> dict:
            return docs.setdefault(
                key,
                {"source_key": key, "terms": set(), "chunks": {}, "quality": 0, "score": 0.0, "first": len(docs)},
            )

        for row in graph_rows:
            key = row.get("key") or ""
            chunk_id = row.get("chunk") or ""
            if not key or not chunk_id:
                continue
            doc = entry(key)
            term = row.get("term") or ""
            if term:
                doc["terms"].add(term)
            quality = int(row.get("quality") or 1)
            doc["quality"] = max(doc["quality"], quality)
            mention = doc["chunks"].setdefault(
                chunk_id, {"text": row.get("text") or "", "terms": set(), "quality": quality, "score": 0.0}
            )
            mention["terms"].add(term)
            mention["quality"] = max(mention["quality"], quality)

        for hit in keyword_hits:
            key = hit.get("source_s3_key") or ""
            chunk_id = hit.get("chunk_id") or ""
            if not key or not chunk_id:
                continue
            doc = entry(key)
            matched = {terms[i] for i in hit.get("matched", []) if isinstance(i, int) and 0 <= i < len(terms)}
            doc["terms"] |= matched
            score = float(hit.get("score") or 0.0)
            doc["score"] = max(doc["score"], score)
            mention = doc["chunks"].setdefault(
                chunk_id, {"text": hit.get("text") or "", "terms": set(), "quality": 0, "score": 0.0}
            )
            mention["terms"] |= matched
            mention["score"] = max(mention["score"], score)
            if not mention["text"]:
                mention["text"] = hit.get("text") or ""

        for doc in docs.values():
            doc["rank_key"] = (len(doc["terms"]), len(doc["chunks"]), doc["quality"], round(doc["score"], 4))
        return sorted(docs.values(), key=lambda d: (d["rank_key"], -d["first"]), reverse=True)

    def _render(self, doc: dict, bucket: str) -> tuple[dict, list[ChunkResult]]:
        """The ``document_match`` item and the mention evidence chunks for one document."""
        key = doc["source_key"]
        mentions = sorted(
            doc["chunks"].items(),
            key=lambda kv: (len(kv[1]["terms"]), kv[1]["quality"], kv[1]["score"]),
            reverse=True,
        )
        snippets = [
            {
                "chunk_id": chunk_id,
                "terms": sorted(t for t in mention["terms"] if t),
                "snippet": _snippet(mention["text"], mention["terms"]),
                "text": mention["text"][:_EVIDENCE_CHUNK_CHARS],
            }
            for chunk_id, mention in mentions[:_SNIPPETS_PER_DOCUMENT]
        ]
        evidence = [
            ChunkResult(
                chunk_id=chunk_id,
                text=mention["text"][:_EVIDENCE_CHUNK_CHARS],
                source_doc=key,
                relevance_score=float(len(mention["terms"])),
                label=key,
                uri=f"s3://{bucket}/{key}",
            )
            for chunk_id, mention in mentions[:_SNIPPETS_PER_DOCUMENT]
            if mention["text"]
        ]
        match = {
            "type": "document_match",
            "source_key": key,
            "matched_terms": sorted(t for t in doc["terms"] if t),
            "mentions": len(doc["chunks"]),
            "entity_match": {2: "exact", 1: "contains"}.get(doc["quality"], "keyword"),
            "snippets": snippets,
            "rank_key": list(doc["rank_key"]),
        }
        return match, evidence


def _snippet(text: str, terms: set[str]) -> str:
    """A short window of ``text`` around the first occurrence of any term."""
    if not text:
        return ""
    lowered = text.lower()
    positions = [lowered.find(t.lower()) for t in terms if t and t.lower() in lowered]
    center = min(positions) if positions else 0
    start = max(0, center - _SNIPPET_CHARS // 3)
    return " ".join(text[start : start + _SNIPPET_CHARS].split())


def build_find_documents_tool(
    registry: ToolRegistry,
    sources_registry: Any,
    *,
    graph_tool: Any | None = None,
    vector_client: Any | None = None,
    chunk_index_for: Any | None = None,
) -> str | None:
    """Register :class:`FindDocumentsTool` when at least one search channel exists.

    Offered to the planner only on requests that set ``options.entityRouting``.
    Returns the registered name, or ``None`` when neither channel is available.
    """
    tool = FindDocumentsTool(
        sources_registry, graph_tool=graph_tool, vector_client=vector_client, chunk_index_for=chunk_index_for
    )
    if tool._graph is None and tool._vector is None:
        logger.info("agentic_tool_omitted", tool=TOOL_NAME, reason="no graph or keyword search channel")
        return None
    registry.register(tool)
    logger.info(
        "find_documents_tool_registered",
        graph=tool._graph is not None,
        keyword=tool._vector is not None,
    )
    return tool.name
