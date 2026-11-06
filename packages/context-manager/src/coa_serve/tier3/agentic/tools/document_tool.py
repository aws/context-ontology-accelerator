# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Read_Document_Tool: read the full text of a source document the agent has seen.

Retrieved chunks are short extracts (a topic plus a handful of statements) of
documents that are often tens of kilobytes long, so the specific section, date or
figure a question needs is frequently NOT inside any retrieved chunk even when the
right document was found. :class:`ReadDocumentTool` lets the planner follow a
relevant-but-incomplete chunk back to its source document and read the document
text itself, one bounded window at a time.

Document reference. GraphRAG chunk text usually starts with a JSON-ish header
whose ``"source"`` value begins with the ORIGINAL S3 key of the uploaded document
(``"source": "<key> (<filename>, <data source id>, ...)"``). :func:`document_ref`
recovers that key from a chunk; the planner summary lists the keys it has seen and
the planner passes one back as ``source_key``. A chunk without the header still
carries the toolkit's opaque document id (``ChunkResult.source_doc``); the planner
may pass that as ``doc_id`` and, when a resolver is wired (the graph tool's
``__Source__`` lookup), the tool resolves it to the key first.

Namespace scoping (Req 1.5). A key is read ONLY when it falls under the bucket and
one of the ``s3Prefixes`` of a DOCUMENTS source registered to the REQUEST
namespace (looked up through the shared :class:`SourcesRegistry`). Anything else —
a key outside every registered prefix, a bucket that is not a registered source
bucket, a namespace whose source inventory cannot be read completely — is REFUSED
without touching S3. The tool never accepts an arbitrary bucket/key pair.

Windowing. The tool returns at most ``max_chars`` characters starting at
``offset`` and reports the next offset when more text remains, so the planner can
page through a long document with repeated calls. The window cap defaults to
:data:`DEFAULT_MAX_CHARS` and is bounded by :data:`HARD_MAX_CHARS`; a per-request
``options.readDocumentMaxChars`` adjusts the default.

Only plain-text originals are read (see :data:`_TEXT_EXTENSIONS`): a PDF or office
document would need the ingest pipeline's preprocessing, which is not reproduced
here, so it is refused with a clear detail instead of returning binary noise.

Per-Tool failure tolerance (Req 9.1-9.2): ``invoke`` never raises for an expected
failure; refusals and S3 errors yield an empty :class:`ToolResult` whose ``detail``
names the reason.

Import discipline (Requirement 12.3): module load imports only the stdlib, the
dependency-free ``.base`` contract, the ``ChunkResult`` data class and
``structlog``; ``boto3`` is imported lazily on first read.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ...vector_retriever import ChunkResult
from .base import ToolResult
from .document_anchor import choose_center

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .registry import ToolRegistry

logger = structlog.get_logger(__name__)

TOOL_NAME = "read_document"

# Chunk-id prefix of a document window. The planner prompt and the assess view key
# on it to tell a full-document window apart from an ordinary retrieved chunk.
DOCUMENT_CHUNK_PREFIX = "doc:"
# Header of every window this tool adds (written in ``_window``); the partial-read
# guard parses it back with this pattern.
WINDOW_HEADER_RE = re.compile(
    r"^\[Source document (?P<key>.+?): characters (?P<start>\d+)-(?P<end>\d+) of (?P<total>\d+)"
)

# Default and maximum characters returned per call. ~12k chars is ~3k tokens: big
# enough to hold a whole typical business document section, small enough that two
# or three windows still fit comfortably in the synthesis prompt.
DEFAULT_MAX_CHARS = 12_000
HARD_MAX_CHARS = 32_000
_MIN_MAX_CHARS = 500

# Bytes fetched per object (a ranged GET). Bounds memory and latency on an
# unexpectedly huge object; text past this point is reported as truncated.
_MAX_OBJECT_BYTES = 2 * 1024 * 1024

# Small per-process cache of decoded documents so paging through one document
# (offset 0, then 12000, ...) costs one S3 read. Keyed by (bucket, key); bounded by
# entries AND by total characters held, so a run of large documents cannot hold
# 64 x 2 MiB of text.
_CACHE_MAX_ENTRIES = 64
_CACHE_MAX_CHARS = 16 * 1024 * 1024
_CACHE_TTL_S = 300.0

# S3's own maximum object-key length; anything longer is not a key.
_MAX_KEY_LENGTH = 1024

# Extensions whose original object is readable as UTF-8 text. An extension-less
# key is also accepted (plain-text uploads often have none).
_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".text",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".xml",
        ".html",
        ".htm",
        ".yaml",
        ".yml",
        ".log",
        ".rst",
    }
)

# ``"source": "<key> (` — the GraphRAG chunk header. The key is the leading run of
# non-space characters inside the quoted value.
_SOURCE_HEADER_RE = re.compile(r'"source"\s*:\s*"([^"\s]+)')

_DESCRIPTION = (
    "Read the FULL TEXT of a source document you have already seen in a retrieved "
    "chunk. Retrieved chunks are short extracts of much longer documents, so when a "
    "chunk is on-topic but lacks the specific detail the question needs (a date, "
    "period, figure, section, or field mapping), read its source document. Pass the "
    "document's 'source_key' exactly as listed under 'Source documents seen so far' "
    "in the context summary (or its 'doc_id' when only that is listed). Returns up "
    "to 'max_chars' characters starting at "
    "'offset'; when more text remains, the result states the next offset to read."
)

_INPUT_SCHEMA: dict = {
    "source_key": "str (the document's source key, exactly as listed in the context summary)",
    "doc_id": "str (the document id listed for a document that has no source key; used only without source_key)",
    "offset": "int (character offset to start reading from; default 0)",
    "max_chars": f"int (maximum characters to return; default {DEFAULT_MAX_CHARS}, at most {HARD_MAX_CHARS})",
}
_OUTPUT_SCHEMA: dict = {"chunks": "list (one document window)", "items": "list"}

# The anchored variant (``options.readDocumentAnchor``): same tool name, a
# description and schema that teach the planner to read AROUND a passage.
_ANCHORED_DESCRIPTION = (
    "Read the FULL TEXT of a source document you have already seen in a retrieved "
    "chunk, centred on the relevant passage. Retrieved chunks are short extracts of "
    "much longer documents, so when a chunk is on-topic but lacks the specific detail "
    "the question needs (a date, period, figure, section, or field mapping), read its "
    "source document. Pass the document's 'source_key' exactly as listed under 'Source "
    "documents seen so far' in the context summary (or its 'doc_id' when only that is "
    "listed). The window of up to 'max_chars' characters is CENTRED on an anchor: pass "
    "'anchor' (a distinctive phrase — a sentence from the relevant chunk, or the entity, "
    "field, date or period the question asks about) or 'anchor_chunk_id' (a chunk id from "
    "the context summary); with neither, the first read of a document centres on the "
    "chunks already retrieved from it. The result states the offsets of the text before "
    "and after the window; pass 'offset' only to page there."
)
_ANCHORED_INPUT_SCHEMA: dict = {
    **_INPUT_SCHEMA,
    "anchor": "str (passage or short phrase to centre the window on; preferred over offset)",
    "anchor_chunk_id": "str (id of a retrieved chunk whose passage the window should centre on)",
    "offset": "int (character offset to page to; omit to read around the anchor)",
}


def document_ref(chunk: Any) -> str:
    """Return the source-document key a chunk came from, or ``""`` when unknown.

    Reads the ``"source"`` header GraphRAG puts at the top of a chunk's text. A
    ``source_doc`` that is itself a key (a window this tool produced) is returned
    as is; an opaque graph source id (``aws:...``) is not a readable key.
    """
    chunk_id = getattr(chunk, "chunk_id", "") or ""
    if chunk_id.startswith(DOCUMENT_CHUNK_PREFIX):
        return getattr(chunk, "source_doc", "") or ""
    text = getattr(chunk, "text", "") or ""
    # The document's own text can carry "source" fields too (e.g. a record naming the
    # system it came from); only a value shaped like an object key counts.
    for match in _SOURCE_HEADER_RE.finditer(text[:2000]):
        if _looks_like_key(match.group(1)):
            return match.group(1)
    # A raw mention chunk from find_documents carries its document as an s3 URI.
    uri = getattr(chunk, "uri", "") or ""
    if uri.startswith("s3://") and uri.count("/") >= 3:
        return uri.split("/", 3)[3]
    return ""


def _looks_like_key(value: str) -> bool:
    """Whether a ``"source"`` header value is an object key (a path or a file name)."""
    return "/" in value or "." + value.rsplit(".", 1)[-1].lower() in _TEXT_EXTENSIONS


def document_id(chunk: Any) -> str:
    """Return the chunk's opaque toolkit document id (``aws:...``), or ``""``."""
    source_doc = getattr(chunk, "source_doc", "") or ""
    return source_doc if source_doc.startswith("aws:") else ""


def document_topic(chunk: Any) -> str:
    """Return the chunk's ``"topic"`` header value (a short title), or ``""``."""
    text = getattr(chunk, "text", "") or ""
    match = re.search(r'"topic"\s*:\s*"([^"]{1,200})"', text[:2000])
    return match.group(1) if match else ""


def _is_upload_source(item: dict) -> bool:
    """Whether a DOCUMENTS source record is an upload source (files in the platform bucket)."""
    return item.get("docSourceType") == "upload" or item.get("sourceSubType") == "LOCAL_UPLOAD"


def _bucket_from_source(item: dict) -> str:
    """The bucket name of a DOCUMENTS source record.

    An S3 source names its bucket (``sourceBucketArn``). An upload source names
    none: its files sit in the platform's sources-data bucket, which serve learns
    from ``SOURCES_DATA_BUCKET``; unset, upload sources stay unreadable.
    """
    arn = item.get("sourceBucketArn") or ""
    if isinstance(arn, str) and arn.startswith("arn:aws:s3:::"):
        return arn[len("arn:aws:s3:::") :].split("/", 1)[0]
    bucket = item.get("bucket") or item.get("sourceBucket") or ""
    if isinstance(bucket, str) and bucket:
        return bucket
    if _is_upload_source(item):
        return os.environ.get("SOURCES_DATA_BUCKET", "").strip()
    return ""


def _prefixes_from_source(item: dict) -> list[str]:
    """The ``s3Prefixes`` of a DOCUMENTS source record (empty = whole bucket).

    An upload source shares its bucket with every namespace, so only its
    server-issued ``<namespaceId>/raw/`` prefixes count; a record without one
    yields no prefixes and is skipped by :meth:`ReadDocumentTool.authorize`,
    never read as the whole bucket.
    """
    raw = item.get("s3Prefixes") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list | tuple | set):
        return []
    prefixes = [p for p in raw if isinstance(p, str)]
    if _is_upload_source(item):
        own = f"{item.get('namespaceId') or ''}/raw/"
        prefixes = [p for p in prefixes if item.get("namespaceId") and p.lstrip("/").startswith(own)]
    return prefixes


def _split_reference(source_key: str) -> tuple[str, str]:
    """Split a planner reference into ``(bucket, key)``; bucket is ``""`` when not given."""
    ref = source_key.strip()
    if ref.startswith("s3://"):
        rest = ref[len("s3://") :]
        bucket, _, key = rest.partition("/")
        return bucket, key
    return "", ref.lstrip("/")


def _key_is_safe(key: str) -> bool:
    """Reject keys that are empty, oversized, contain control chars or ``..`` segments."""
    if not key or len(key) > _MAX_KEY_LENGTH:
        return False
    if any(ord(ch) < 32 for ch in key):
        return False
    return ".." not in key.split("/")


def _is_text_key(key: str) -> bool:
    """Whether the object's extension marks it as readable plain text."""
    name = key.rsplit("/", 1)[-1]
    if "." not in name:
        return True
    return "." + name.rsplit(".", 1)[-1].lower() in _TEXT_EXTENSIONS


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int coercion (planner inputs are already type-checked)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return default


@dataclass(frozen=True)
class RankedDocument:
    """A source document ranked by the evidence chunks retrieved from it.

    Attributes:
        source_key: The document's readable key (``""`` when only ``doc_id`` is known).
        doc_id: The toolkit document id (``""`` when the chunks carried a key).
        chunk_count: Retrieved (non-window) chunks attributed to the document.
        best_score: Highest ``relevance_score`` among those chunks.
        first_rank: Arrival index of the document's first chunk.
        read: Whether a window of the document was already read this request.
    """

    source_key: str
    doc_id: str
    chunk_count: int
    best_score: float
    first_rank: int
    read: bool


def rank_source_documents(chunks: list[Any]) -> list[RankedDocument]:
    """Aggregate passage-level evidence into a document ranking.

    Standard passage-to-document aggregation: each retrieved chunk is a vote for
    the document it came from, and a document's score pools its chunks — first the
    NUMBER of retrieved chunks (a document several independent retrievals keep
    returning is more likely the one the question is about), then the best
    single-chunk relevance score, then the earliest rank at which it surfaced. No
    corpus-specific signal is used. Chunks are attributed by their source header
    key, else by the toolkit document id; full-document windows mark their document
    as read but cast no vote.
    """
    order: list[tuple[str, str]] = []
    stats: dict[tuple[str, str], list[float]] = {}  # count, best score, first rank
    read: set[str] = set()
    for index, chunk in enumerate(chunks):
        chunk_id = getattr(chunk, "chunk_id", "") or ""
        if chunk_id.startswith(DOCUMENT_CHUNK_PREFIX):
            read.add(getattr(chunk, "source_doc", "") or "")
            continue
        key = document_ref(chunk)
        ident = (key, "") if key else ("", document_id(chunk))
        if not any(ident):
            continue
        score = float(getattr(chunk, "relevance_score", 0.0) or 0.0)
        if ident not in stats:
            order.append(ident)
            stats[ident] = [0.0, score, float(index)]
        entry = stats[ident]
        entry[0] += 1
        entry[1] = max(entry[1], score)
    ranked = sorted(order, key=lambda i: (-stats[i][0], -stats[i][1], stats[i][2]))
    return [
        RankedDocument(
            source_key=key,
            doc_id=doc,
            chunk_count=int(stats[(key, doc)][0]),
            best_score=stats[(key, doc)][1],
            first_rank=int(stats[(key, doc)][2]),
            read=bool(key) and key in read,
        )
        for key, doc in ranked
    ]


class ReadDocumentTool:
    """Reads a namespace-scoped source document, one bounded window per call.

    Args:
        sources_registry: The shared serve-layer ``SourcesRegistry``; its
            ``document_sources(namespace)`` supplies the buckets/prefixes a
            namespace may read from.
        s3_client: Optional pre-built S3 client (tests inject a fake). Defaults to
            a lazily-built ``boto3`` client.
        source_key_resolver: Optional ``async (namespace, doc_id) -> key`` used to
            resolve a ``doc_id`` input; without it ``doc_id`` is refused.
        default_max_chars: Window size when neither the planner nor the request
            options name one.
        clock: Monotonic time source for the document cache TTL (injectable).
    """

    def __init__(
        self,
        sources_registry: Any,
        *,
        s3_client: Any | None = None,
        source_key_resolver: Any | None = None,
        default_max_chars: int = DEFAULT_MAX_CHARS,
        clock: Any = time.monotonic,
    ) -> None:
        """Bind the registry, S3 client, window default and cache."""
        self.name = TOOL_NAME
        self.description = _DESCRIPTION
        self.input_schema = dict(_INPUT_SCHEMA)
        self.output_schema = dict(_OUTPUT_SCHEMA)
        self._registry = sources_registry
        self._s3 = s3_client
        self._resolve_key = source_key_resolver
        self._default_max_chars = default_max_chars
        self._clock = clock
        self._cache: OrderedDict[tuple[str, str], tuple[str, bool, float]] = OrderedDict()
        self._anchored: AnchoredReadDocumentTool | None = None

    def anchored_variant(self) -> AnchoredReadDocumentTool:
        """The anchored view of this tool (``options.readDocumentAnchor``), built once.

        Shares this tool's registry, resolver and document cache; differs only in
        the description/schema the planner sees and in centring windows on anchors.
        """
        if self._anchored is None:
            self._anchored = AnchoredReadDocumentTool(self)
        return self._anchored

    async def invoke(
        self,
        *,
        namespace: str,
        source_key: str = "",
        doc_id: str = "",
        offset: int = 0,
        max_chars: int | None = None,
        options: dict | None = None,
        **_: Any,
    ) -> ToolResult:
        """Return one window of the document ``source_key`` if the namespace owns it.

        Args:
            namespace: The originating request namespace; the read is authorized
                against ITS registered DOCUMENTS sources only (Req 1.5).
            source_key: The document's original S3 key (optionally ``s3://bucket/key``).
            doc_id: The toolkit document id, resolved to a key when no
                ``source_key`` is given.
            offset: Character offset of the window start (clamped to >= 0).
            max_chars: Window size; clamped to ``[500, HARD_MAX_CHARS]``.
            options: The per-request options bag (``readDocumentMaxChars``).

        Returns:
            A :class:`ToolResult` carrying one document-window chunk on success, or
            no chunks and a ``detail`` naming why the read was refused or failed.
        """
        loaded = await self._resolve_and_load(namespace, source_key, doc_id)
        if isinstance(loaded, ToolResult):
            return loaded
        bucket, key, text, truncated = loaded
        return self._window(bucket, key, text, truncated, offset, self.window_size(max_chars, options))

    async def _resolve_and_load(
        self, namespace: str, source_key: Any, doc_id: Any
    ) -> ToolResult | tuple[str, str, str, bool]:
        """Resolve, authorize and load a document; a :class:`ToolResult` on refusal/failure.

        Returns ``(bucket, key, text, truncated)`` once the key is shown to belong to
        one of the namespace's DOCUMENTS sources and the object was read.
        """
        if (not isinstance(source_key, str) or not source_key.strip()) and isinstance(doc_id, str) and doc_id:
            resolved = await self._key_for_doc_id(namespace, doc_id.strip())
            if isinstance(resolved, Exception):
                return ToolResult(
                    detail=f"{self.name} error: {type(resolved).__name__} resolving doc_id {doc_id[:80]}; "
                    "retry, or pass source_key"
                )
            source_key = resolved
            if not source_key:
                return ToolResult(detail=f"{self.name}: no readable source key found for doc_id {doc_id[:80]}")
        if not isinstance(source_key, str) or not source_key.strip():
            return ToolResult(detail=f"{self.name}: no source_key supplied")
        ref_bucket, key = _split_reference(source_key)
        if not _key_is_safe(key):
            logger.warning("read_document_refused", namespace=namespace, reason="unsafe_key")
            return ToolResult(detail=f"{self.name} refused: invalid source_key")

        try:
            sources = await self._registry.document_sources(namespace)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "read_document_sources_error",
                namespace=namespace,
                error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            sources = None
        if sources is None:
            # Fail CLOSED: without the complete inventory the key cannot be shown
            # to belong to this namespace.
            logger.warning("read_document_refused", namespace=namespace, reason="source_inventory_unavailable")
            return ToolResult(detail=f"{self.name} refused: document sources for this namespace are unavailable")

        bucket = self.authorize(sources, ref_bucket, key)
        if not bucket:
            logger.warning(
                "read_document_refused",
                namespace=namespace,
                reason="key_outside_namespace_sources",
                key=key[:200],
            )
            return ToolResult(detail=f"{self.name} refused: {key[:120]} is not a document of this namespace")

        if not _is_text_key(key):
            return ToolResult(detail=f"{self.name}: {key[:120]} is not a plain-text document; read its chunks instead")

        try:
            text, truncated = await self._load(bucket, key)
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "read_document_s3_error",
                namespace=namespace,
                key=key[:200],
                error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            return ToolResult(detail=f"{self.name} error: {type(exc).__name__} reading {key[:120]}")

        return bucket, key, text, truncated

    async def _key_for_doc_id(self, namespace: str, doc_id: str) -> str | Exception:
        """Resolve a toolkit document id to its source key.

        ``""`` when the id has no known key; the exception (returned, not raised)
        when the lookup itself failed, so the caller can tell a backend failure
        from a real miss.
        """
        if self._resolve_key is None:
            return ""
        try:
            return await self._resolve_key(namespace, doc_id) or ""
        except Exception as exc:  # noqa: BLE001 - a Tool must never raise (Req 9.1)
            logger.warning(
                "read_document_doc_id_lookup_failed",
                namespace=namespace,
                doc_id=doc_id[:80],
                error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            return exc

    # ── authorization ──────────────────────────────────────────────────

    @staticmethod
    def authorize(sources: list[dict], ref_bucket: str, key: str) -> str:
        """Return the bucket ``key`` may be read from, or ``""`` when it is not owned.

        The key must start with one of a DOCUMENTS source's ``s3Prefixes`` (a
        source with no prefixes, or an empty one, ingested its whole bucket). This
        is a plain string-prefix test, the same one S3 ``ListObjects`` applied when
        the source was ingested: a prefix without a trailing ``/`` (``reports``)
        also covers sibling keys such as ``reports-private/x.txt``, exactly the
        objects ingestion read from it. When the planner named
        a bucket (``s3://bucket/key``) it must be that source's bucket.
        """
        for item in sources:
            bucket = _bucket_from_source(item)
            if not bucket or (ref_bucket and ref_bucket != bucket):
                continue
            prefixes = _prefixes_from_source(item)
            if not prefixes and _is_upload_source(item):
                continue  # a shared bucket is never readable whole
            if not prefixes or any(key.startswith(p.lstrip("/")) for p in prefixes):
                return bucket
        return ""

    # ── reading ────────────────────────────────────────────────────────

    def window_size(self, max_chars: Any, options: dict | None) -> int:
        """Resolve the window size: planner input, else request option, else default."""
        size = _coerce_int(max_chars, 0) if max_chars is not None else 0
        if size <= 0:
            size = _coerce_int((options or {}).get("readDocumentMaxChars"), self._default_max_chars)
        return max(_MIN_MAX_CHARS, min(HARD_MAX_CHARS, size))

    async def _load(self, bucket: str, key: str) -> tuple[str, bool]:
        """Return ``(text, truncated)`` for the object, via the per-process cache."""
        now = self._clock()
        cached = self._cache.get((bucket, key))
        if cached is not None and now - cached[2] < _CACHE_TTL_S:
            self._cache.move_to_end((bucket, key))
            return cached[0], cached[1]

        body = await asyncio.to_thread(self._get_object, bucket, key)
        truncated = len(body) >= _MAX_OBJECT_BYTES
        text = body.decode("utf-8", errors="replace")
        self._cache[(bucket, key)] = (text, truncated, now)
        self._cache.move_to_end((bucket, key))
        held = sum(len(entry[0]) for entry in self._cache.values())
        while len(self._cache) > 1 and (len(self._cache) > _CACHE_MAX_ENTRIES or held > _CACHE_MAX_CHARS):
            _, (old_text, _, _) = self._cache.popitem(last=False)
            held -= len(old_text)
        return text, truncated

    def _get_object(self, bucket: str, key: str) -> bytes:
        """Blocking ranged GET of the first ``_MAX_OBJECT_BYTES`` of the object."""
        if self._s3 is None:
            import boto3
            from coa_common import resolve_region, sync_boto_config

            self._s3 = boto3.client("s3", region_name=resolve_region(), config=sync_boto_config())
        response = self._s3.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{_MAX_OBJECT_BYTES - 1}")
        return response["Body"].read()

    def _window(
        self,
        bucket: str,
        key: str,
        text: str,
        truncated: bool,
        offset: Any,
        size: int,
        *,
        anchor_note: str = "",
        show_previous: bool = False,
        extra_item: dict | None = None,
    ) -> ToolResult:
        """Slice one window out of ``text`` and wrap it as a document-window chunk.

        ``anchor_note`` / ``show_previous`` / ``extra_item`` are used only by the
        anchored variant (to say where the window was centred and where the text
        before it starts); the plain tool never sets them.
        """
        total = len(text)
        start = max(0, _coerce_int(offset, 0))
        if start >= total:
            return ToolResult(detail=f"{self.name}: offset {start} is past the end of {key[:120]} ({total} chars)")
        end = min(total, start + size)
        more = end < total
        previous = max(0, start - size) if show_previous and start > 0 else None
        header = f"[Source document {key}: characters {start}-{end} of {total}"
        if anchor_note:
            header += f"; {anchor_note}"
        if previous is not None:
            header += f"; earlier text starts at offset={previous}"
        if more:
            header += f"; more text follows, read it with offset={end}"
        elif truncated:
            header += "; the document is longer than the readable limit"
        header += "]"
        chunk = ChunkResult(
            chunk_id=f"{DOCUMENT_CHUNK_PREFIX}{key}@{start}",
            text=f"{header}\n{text[start:end]}",
            source_doc=key,
            relevance_score=1.0,
            label=key,
            uri=f"s3://{bucket}/{key}",
        )
        item = {
            "type": "document_window",
            "source_key": key,
            "offset": start,
            "end": end,
            "total_chars": total,
            "next_offset": end if more else None,
        }
        if show_previous:
            item["prev_offset"] = previous
        if extra_item:
            item.update(extra_item)
        logger.info("read_document_window", key=key[:200], start=start, end=end, total=total)
        detail = f"{self.name}: {key[:120]} characters {start}-{end} of {total}"
        if more:
            detail += f" (next offset={end})"
        return ToolResult(chunks=(chunk,), items=(item,), detail=detail)


class AnchoredReadDocumentTool(ReadDocumentTool):
    """read_document that centres its window on an anchor passage (opt-in variant).

    Offered in place of the plain tool when the request sets
    ``options.readDocumentAnchor``. The anchor is, in order: the planner's
    ``anchor`` text; the text of ``anchor_chunk_id`` from the accumulated context;
    or — on the FIRST read of a document with no offset — the chunks already
    retrieved from that document. The window is centred where
    :func:`~.document_anchor.choose_center` locates the anchor (ties broken by the
    sub-question's key terms). An explicit ``offset`` with no anchor pages exactly
    like the plain tool. The controller hands this tool the accumulated context and
    sub-question (``wants_context``) so it can auto-anchor.
    """

    wants_context = True

    def __init__(self, base: ReadDocumentTool) -> None:
        """Share ``base``'s state (registry, S3 client, resolver, cache)."""
        self.__dict__.update(base.__dict__)
        self.description = _ANCHORED_DESCRIPTION
        self.input_schema = dict(_ANCHORED_INPUT_SCHEMA)

    def anchored_variant(self) -> AnchoredReadDocumentTool:
        """Already the anchored variant."""
        return self

    async def invoke(
        self,
        *,
        namespace: str,
        source_key: str = "",
        doc_id: str = "",
        offset: int | None = None,
        max_chars: int | None = None,
        anchor: str = "",
        anchor_chunk_id: str = "",
        options: dict | None = None,
        accumulated_context: Any | None = None,
        sub_question: str = "",
        **_: Any,
    ) -> ToolResult:
        """Return the window of ``source_key`` centred on the anchor (see class doc)."""
        loaded = await self._resolve_and_load(namespace, source_key, doc_id)
        if isinstance(loaded, ToolResult):
            return loaded
        bucket, key, text, truncated = loaded
        size = self.window_size(max_chars, options)

        chunks = list(getattr(accumulated_context, "chunks", None) or [])
        anchors: list[str] = []
        source = ""
        if isinstance(anchor, str) and anchor.strip():
            anchors, source = [anchor.strip()], "anchor"
        elif isinstance(anchor_chunk_id, str) and anchor_chunk_id.strip():
            match = [c for c in chunks if (getattr(c, "chunk_id", "") or "") == anchor_chunk_id.strip()]
            if match:
                anchors, source = [getattr(match[0], "text", "") or ""], "anchor_chunk_id"
            else:
                # A mention chunk find_documents returned to another sub-question.
                texts = [
                    snip.get("text", "")
                    for m in getattr(accumulated_context, "document_matches", None) or []
                    for snip in m.get("snippets", [])
                    if snip.get("chunk_id") == anchor_chunk_id.strip() and snip.get("text")
                ]
                if texts:
                    anchors, source = texts[:1], "anchor_chunk_id"
        start_offset = _coerce_int(offset, 0) if offset is not None else 0
        if not anchors and start_offset == 0 and not self._already_read(chunks, key):
            anchors = [
                getattr(c, "text", "") or ""
                for c in chunks
                if not (getattr(c, "chunk_id", "") or "").startswith(DOCUMENT_CHUNK_PREFIX)
                and (document_ref(c) == key or (doc_id and (getattr(c, "source_doc", "") or "") == doc_id))
            ]
            # Mentions find_documents located in this document (shared across
            # sub-questions) anchor the first read too.
            anchors += [
                snip.get("text", "")
                for m in getattr(accumulated_context, "document_matches", None) or []
                if m.get("source_key") == key
                for snip in m.get("snippets", [])
                if snip.get("text")
            ]
            source = "retrieved_chunks" if anchors else ""

        if not anchors:
            return self._window(bucket, key, text, truncated, start_offset, size, show_previous=True)

        # The anchor search is pure-Python string work over up to 2 MiB of text; run
        # it off the event loop so other requests (and the per-tool timeout) proceed.
        match = await asyncio.to_thread(choose_center, text, anchors, sub_question or "", size)
        if match.method == "none":
            note = "anchor not found in the document; reading from the requested offset"
            return self._window(
                bucket,
                key,
                text,
                truncated,
                start_offset,
                size,
                anchor_note=note,
                show_previous=True,
                extra_item={"anchor_source": source, "anchor_method": "none"},
            )
        start = max(0, min(match.center - size // 2, len(text) - size))
        logger.info(
            "read_document_anchored",
            key=key[:200],
            source=source,
            method=match.method,
            center=match.center,
            anchors=len(anchors),
            located=match.anchors_located,
        )
        return self._window(
            bucket,
            key,
            text,
            truncated,
            start,
            size,
            anchor_note=f"centred on the anchored passage ({match.method} match at character {match.center})",
            show_previous=True,
            extra_item={
                "anchor_source": source,
                "anchor_method": match.method,
                "anchor_center": match.center,
                "anchors_located": match.anchors_located,
                "anchors_in_window": match.anchors_in_window,
            },
        )

    @staticmethod
    def _already_read(chunks: list, key: str) -> bool:
        """Whether a window of ``key`` was already read this request (then offset 0 pages)."""
        return any((getattr(c, "chunk_id", "") or "").startswith(f"{DOCUMENT_CHUNK_PREFIX}{key}@") for c in chunks)


def build_document_tool(
    registry: ToolRegistry,
    sources_registry: Any,
    *,
    s3_client: Any | None = None,
    source_key_resolver: Any | None = None,
) -> str:
    """Register the single :class:`ReadDocumentTool` into ``registry``.

    The tool is registered process-wide but withheld from the planner unless the
    request opts in (``options.readDocument``), so registering it changes nothing
    for a request that does not ask for it. Returns the registered Tool name.
    """
    tool = ReadDocumentTool(sources_registry, s3_client=s3_client, source_key_resolver=source_key_resolver)
    registry.register(tool)
    logger.info("document_tool_registered", tool_name=tool.name)
    return tool.name
