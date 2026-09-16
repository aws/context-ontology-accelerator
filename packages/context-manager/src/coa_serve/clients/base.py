# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Abstract protocols for upstream clients for serve component.

The resolution pipeline depends ONLY on these protocols. Concrete
implementations (Neptune, OpenSearch, Bedrock, asyncpg) live in separate
modules and are wired via the factory.

Error contract:
- Protocol methods (query, search, converse, execute) RAISE on failure.
  Callers handle exceptions and record them in processing traces.
- health_check() is exception-safe — always returns a status dict.
"""

from __future__ import annotations

import functools
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


def instrumented(client_name: str):
    """Decorator that logs latency and errors for async client methods."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
                logger.info(
                    "client_call",
                    client=client_name,
                    method=fn.__name__,
                    status="ok",
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )
                return result
            except Exception as e:
                logger.error(
                    "client_call",
                    client=client_name,
                    method=fn.__name__,
                    status="error",
                    error=type(e).__name__,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )
                raise

        return wrapper

    return decorator


# ── Graph Client (Neptune DB, Neptune Analytics) ─────────────────────────


# Enables isinstance() checks against this protocol at runtime.
@runtime_checkable
class GraphClient(Protocol):
    """Read-only graph query interface for SPARQL operations.

    Used by:
    - T-Box context assembly: retrieves ontology classes, properties, and constraints
    - Graph traversal: navigates entity relationships for context enrichment
    - URI existence validation: verifies resource identifiers before downstream queries
    """

    async def query(self, sparql: str) -> list[dict[str, Any]]:
        """Execute a SPARQL SELECT query. Returns list of binding dicts."""
        ...

    async def ask(self, sparql: str) -> bool:
        """Execute a SPARQL ASK query. Returns boolean."""
        ...

    async def health_check(self) -> dict[str, Any]:
        """Probe graph backend reachability; returns a status dict, never raises."""
        ...


# ── Vector Client (OpenSearch Serverless, Neptune Analytics) ─────────────


@dataclass
class VectorHit:
    """One nearest-neighbour search result from vector similarity search.

    Attributes:
        id: Unique identifier of the matched document chunk.
        text: The content of the matched chunk.
        score: Cosine similarity score (0.0–1.0, higher is more similar).
        metadata: Additional key-value pairs (e.g., source URI, namespace).
    """

    id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def data_source_id(self) -> str:
        """Provenance of this hit's entity, or "" if absent.

        Single source of truth for reading data_source_id off hit metadata. For an
        ontology class this is stamped only when the class is R2RML-mapped to a
        relational source, so a non-empty value == "structured/Tier-2-capable".
        """
        return self.metadata.get("data_source_id", "")


@runtime_checkable
class VectorClient(Protocol):
    """Vector search interface for document chunk retrieval.

    Used by:
    - k-NN retrieval: finds semantically similar document chunks for context augmentation
    - Hybrid search: combines BM25 keyword matching with vector similarity for improved recall
    """

    async def search(
        self,
        query_vector: list[float],
        *,
        namespace: str = "",
        top_k: int = 5,
        index: str | None = None,
        entity_type: str | None = None,
        require_mapped: bool = False,
    ) -> list[VectorHit]:
        """k-NN search scoped by index, optionally filtered by entity_type / mapped-only."""
        ...

    async def count_documents(
        self,
        *,
        index: str | None = None,
        require_mapped: bool = False,
        entity_type: str | None = None,
    ) -> int:
        """Count documents matching optional entity_type / mapped filters."""
        ...

    async def hybrid_search(
        self,
        query_text: str,
        query_vector: list[float],
        *,
        namespace: str = "",
        top_k: int = 5,
        index: str | None = None,
    ) -> list[VectorHit]:
        """Combined BM25 keyword + k-NN vector search."""
        ...

    async def health_check(self) -> dict[str, Any]:
        """Probe vector backend reachability; returns a status dict, never raises."""
        ...


# ── LLM Client (Bedrock) ─────────────────────────────────────────────────


class GuardrailOutcome(StrEnum):
    """What a Bedrock guardrail actually did to a Converse call.

    ``stopReason=guardrail_intervened`` alone cannot distinguish these: Bedrock uses
    it for masking and for blocking alike. Classifying them apart is what keeps a
    merely-anonymized answer from being discarded as blocked content.
    """

    NONE = "none"
    """No intervention — the stop reason was not a guardrail one."""

    ANONYMIZED = "anonymized"
    """Masked only (e.g. PII ``NAME`` → ``{NAME}``). The text remains usable."""

    BLOCKED = "blocked"
    """An explicit ``BLOCKED`` action. Bedrock has already substituted its own
    blocked-message text, so ``ConverseResult.text`` is never model content here."""

    UNKNOWN = "unknown"
    """Intervened, but no trace was available to classify it. Treated as blocked
    (fail closed) AND reported separately, because an unconfirmed suppression is not
    a confirmed policy block."""


STOP_REASON_MAX_TOKENS = "max_tokens"
"""Converse ``stopReason`` for a generation cut short by the output-token cap."""


@dataclass(frozen=True)
class ConverseResult:
    """Result of a Bedrock Converse call.

    ``outcome`` is authoritative; ``guardrail_blocked`` is retained as the
    compatibility surface for existing callers and is kept consistent with it. Passing
    either one alone is supported: whichever is omitted is derived in
    ``__post_init__``.

    ``stop_reason`` is the raw Converse ``stopReason``. It is carried because
    ``max_tokens`` truncation is otherwise INVISIBLE: Bedrock returns the partial
    text with HTTP 200, and a caller that only reads ``.text`` cannot tell a
    finished answer from one cut mid-sentence. For NL→SQL that is a query cut
    mid-statement which still parses as SQL, so it fails at execution rather than
    at generation, where the cause would be obvious.
    """

    text: str
    guardrail_blocked: bool = False
    outcome: GuardrailOutcome | None = None
    stop_reason: str = ""

    def __post_init__(self) -> None:
        """Reconcile ``outcome`` and ``guardrail_blocked`` so they can never disagree."""
        if self.outcome is None:
            derived = GuardrailOutcome.BLOCKED if self.guardrail_blocked else GuardrailOutcome.NONE
            object.__setattr__(self, "outcome", derived)
        else:
            object.__setattr__(
                self,
                "guardrail_blocked",
                self.outcome in (GuardrailOutcome.BLOCKED, GuardrailOutcome.UNKNOWN),
            )

    @property
    def truncated(self) -> bool:
        """True when the model stopped because it hit the output-token cap."""
        return self.stop_reason == STOP_REASON_MAX_TOKENS


class GuardrailBlockedError(Exception):
    """Raised when Bedrock guardrail blocks content (not anonymize — actual block)."""


class ContentFilteredError(ValueError):
    """The MODEL filtered its own output (``stopReason=content_filtered``).

    Deliberately NOT a :class:`GuardrailBlockedError`: this is native model-side
    filtering, not a Bedrock Guardrail policy action, and conflating them would make
    the two indistinguishable in traces and on dashboards. Subclasses ``ValueError``
    because that is what this condition raised before it was typed.
    """

    def __init__(self, message: str, *, stop_reason: str = "content_filtered") -> None:
        """Carry the originating ``stopReason`` alongside the message."""
        super().__init__(message)
        self.stop_reason = stop_reason


@runtime_checkable
class LLMClient(Protocol):
    """LLM inference interface for Converse API and embeddings.

    Used by:
    - NL-to-SPARQL translation: converts natural language queries into SPARQL via LLM
    - Context synthesis: generates natural language summaries from retrieved data
    - Query embedding: vectorizes user queries for downstream k-NN search
    """

    async def converse(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        top_p: float | None = None,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> ConverseResult:
        """Call Bedrock Converse API. Returns ConverseResult with text and guardrail status.

        ``guard_content`` is the user-supplied text to tag for guardrail
        evaluation (M-5 prompt-injection scoping); when set, only that segment is
        guard-evaluated, leaving retrieved context/instructions untouched.

        ``top_p`` is an optional nucleus-sampling cutoff; determinism-sensitive
        callers set it alongside temperature=0 so a sampling constraint survives
        even on models that drop temperature.

        ``model_id`` overrides the default LLM model for this call. When None,
        the client's configured default is used.
        """
        ...

    def converse_stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text fragments from Bedrock ConverseStream.

        Raises GuardrailBlockedError if the guardrail intervenes mid-stream with a
        BLOCK action.

        ``model_id`` overrides the default LLM model for this call.
        """
        ...

    async def embed(self, text: str) -> list[float]:
        """Generate embedding vector for text."""
        ...

    async def health_check(self) -> dict[str, Any]:
        """Probe LLM backend reachability; returns a status dict, never raises."""
        ...


# ── Query Executor (asyncpg, Athena) ─────────────────────────────────────


@dataclass
class QueryResult:
    """Result of a SQL query execution."""

    rows: list[dict[str, Any]]
    columns: list[str]
    row_count: int
    truncated: bool
    duration_ms: int = 0
    # Which execution engine produced this result: "athena" | "redshift" | "jdbc".
    # Surfaced in the serve trace (t1.execute / t2.sql.execute) so the routing is
    # observable end-to-end. Empty when the executor doesn't tag itself.
    engine: str = ""


@runtime_checkable
class QueryExecutor(Protocol):
    """SQL execution interface against source databases.

    Used by:
    - Tier 1 metric execution: runs pre-defined SQL for KPI/metric retrieval
    - Tier 2 VKG-translated queries: executes SQL generated from virtual knowledge graph resolution

    Implementations route queries based on source type:
    - Single-source → direct JDBC (asyncpg)
    - Cross-source → Athena federation
    """

    async def execute(
        self,
        sql: str,
        *,
        namespace: str,
        data_source_id: str,
        params: dict[str, Any] | None = None,
        max_rows: int = 1000,
        timeout_seconds: int = 10,
    ) -> QueryResult:
        """Execute authorized SQL against the target data source."""
        ...

    async def health_check(self) -> dict[str, Any]:
        """Probe query executor reachability; returns a status dict, never raises."""
        ...


# ── Client Set ────────────────────────────────────────────────────────────


@dataclass
class ClientSet:
    """All upstream clients needed by the resolution pipeline.

    Passed as a unit to the Orchestrator — modules destructure what they need.
    Call close() during application shutdown to release connections.
    """

    graph: GraphClient
    vector: VectorClient
    llm: LLMClient
    query_executor: QueryExecutor

    async def close(self) -> None:
        """Release resources held by clients that support cleanup."""
        for client in (self.graph, self.vector, self.llm, self.query_executor):
            close_fn = getattr(client, "close", None)
            if close_fn and callable(close_fn):
                try:
                    await close_fn()
                except Exception:
                    logger.warning("client_close_error", client=type(client).__name__, exc_info=True)
