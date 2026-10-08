# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared Bedrock text-embedding client — single source of truth.

Every service that produces or consumes embeddings (ontology induction,
doc-kg-build ingestion, metric onboarding, serve retrieval) should embed
through this client so the model, dimensions, and request/response handling
stay identical across the stack. Mixing models silently breaks retrieval:
vectors from different models are not comparable, so k-NN similarity collapses
to noise with no error raised.

The model id defaults to :data:`DEFAULT_EMBED_MODEL_ID` (Cohere Embed v4 via an
inference profile) but can be overridden per instance or via the
``BEDROCK_EMBED_MODEL_ID`` environment variable.

Two providers are supported because their Bedrock wire formats differ:

* **Cohere** (``cohere.embed-*``) — request ``{"texts": [...], "input_type":
  "search_document"|"search_query", "output_dimension": N, "embedding_types":
  ["float"]}`` → response ``{"embeddings": {"float": [[...]]}}``. Cohere is
  asymmetric: documents are embedded with ``search_document`` and queries with
  ``search_query`` — use :meth:`embed_document` / :meth:`embed_query`
  accordingly, or retrieval quality drops.
* **Amazon Titan** (``amazon.titan-embed-*``) — request ``{"inputText": "...",
  "dimensions": N, "normalize": true}`` → response ``{"embedding": [...]}``.
  Titan is symmetric (no input-type distinction).
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Literal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from coa_common.bedrock_metrics import CostTracker
from coa_common.config import resolve_region
from coa_common.constants import DEFAULT_EMBED_DIMENSIONS, DEFAULT_EMBED_MODEL_ID
from coa_common.metrics import emit_metric

# Character cap applied before embedding. Cohere's limit is ~2048 TOKENS and
# Titan's is 8192 tokens; at ~4 chars/token an 8k-char bound stays safely within
# both while preserving long class comments / metric descriptions (the previous
# direct-invoke code used 8000 chars, so this keeps parity — do not lower it).
_MAX_INPUT_CHARS = 8000

# Bedrock ModelTimeoutException (HTTP 408) is not in any botocore retryable
# set (neither the transient status list [500,502,503,504] nor the throttling
# codes), and its service-model shape carries no `retryable` metadata — so the
# client's Config(retries=...) never retries it and a single transient model
# timeout aborts a whole induction. Retry it at the application level,
# ONLY this code (429/5xx stay with botocore's adaptive retry). Dedicated,
# conservative knobs — NOT aliased to OSS_* — because _embed is shared with the
# user-facing serve embed_query path; induction can raise them via env for batch.
_EMBED_RETRY_ERROR_CODES = frozenset({"ModelTimeoutException"})
_EMBED_MAX_RETRIES = int(os.getenv("BEDROCK_EMBED_MAX_RETRIES", "2"))
_EMBED_MAX_BACKOFF_S = float(os.getenv("BEDROCK_EMBED_MAX_BACKOFF_S", "8"))

# Cohere Embed accepts up to 96 texts per InvokeModel (v3 + v4). Default the
# batch size to that documented hard cap (>96 → 400); clamp to [1,96] in
# __init__ so a bad operator value can never send an oversized body or a
# zero-width chunk. See HLD §3.5.
_DEFAULT_EMBED_BATCH_SIZE = 96

# Per-instance socket read timeout default (seconds) — boto3's own default, kept
# for serve parity. See BedrockEmbedder.read_timeout / HLD §3.4.
_DEFAULT_EMBED_READ_TIMEOUT = 60

# CloudWatch EMF namespace for embed-path counters (§3.6). Reuses the existing
# emit_metric helper — stdout EMF, no PutMetricData, no IAM grant, never raises.
_METRIC_NAMESPACE = "SemanticContext/Embeddings"

InputType = Literal["document", "query"]


def _embed_backoff(attempt: int) -> float:
    return min(_EMBED_MAX_BACKOFF_S, 2.0**attempt) + random.uniform(0, 1)


def _is_cohere(model_id: str) -> bool:
    # Handles bare ids ("cohere.embed-v4:0") and inference-profile ids
    # ("us.cohere.embed-v4:0", "global.cohere.embed-v4:0").
    return "cohere.embed" in model_id


def _is_cohere_v4(model_id: str) -> bool:
    # v4-ONLY gate for the texts[] batch path. Every payload/dimension
    # assumption (output_dimension, 96-cap, per-text token budget) is verified
    # for v4 only, so a non-v4 Cohere id ("cohere.embed-english-v3") falls back
    # to per-text fan-out rather than a v4-shaped body that may 400. Covers the
    # "us."/"global." profile prefixes; excludes v3 and Titan. HLD §3.2.
    return "cohere.embed-v4" in model_id


def _is_retryable(exc: Exception) -> bool:
    # ClientError-only: BotoCoreError (Read/ConnectTimeout) has no .response, so
    # guard the isinstance check first to avoid an AttributeError. HLD §3.4.1.
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in _EMBED_RETRY_ERROR_CODES


# Errors that halving can isolate: model/read/connect timeouts subdivide toward
# the timeout budget; a per-text ValidationException subdivides to pinpoint the
# offender. Throttle/5xx/AccessDenied are NOT here — they propagate to botocore
# adaptive retry or surface as a real failure. HLD §3.3.1 / §3.4.
_SUBDIVIDE_ON = (ClientError, ReadTimeoutError, ConnectTimeoutError)


def _error_code(exc: Exception) -> str | None:
    # `or {}` guards a BotoCoreError whose `.response` attribute exists but is
    # None (e.g. ReadTimeoutError) — getattr's default only fires when absent.
    return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code")


def _is_subdividable(exc: Exception) -> bool:
    if isinstance(exc, (ReadTimeoutError, ConnectTimeoutError)):
        return True
    return _error_code(exc) in {"ModelTimeoutException", "ValidationException"}


def _fatal_if_validation(exc: Exception, chunk: list[str]) -> Exception:
    """Escalate a size-1-leaf ValidationException into a loud ValueError.

    At a size-1 leaf, turn a ValidationException into a loud ValueError that
    names the offending text; re-raise everything else unchanged. Never silently
    drop a text — that would break the len==len / index-zip contract. HLD §3.3.1.
    """
    if _error_code(exc) == "ValidationException":
        msg = (getattr(exc, "response", None) or {}).get("Error", {}).get("Message", "")
        return ValueError(
            f"Cohere ValidationException on 1-text leaf (len={len(chunk[0])}): {chunk[0][:120]!r}; bedrock={msg!r}"
        )
    return exc


def _prep(text: str) -> str:
    # Shared by single + batch paths. Per-text char cap (unchanged), then a
    # single-space sentinel for empty/whitespace-only input — Cohere 400s on
    # empty/whitespace, and whitespace is truthy so `text or " "` would NOT
    # catch it. HLD §3.3.
    truncated = text[:_MAX_INPUT_CHARS]
    return truncated if truncated.strip() else " "


def _env_batch_size() -> int:
    raw = os.getenv("BEDROCK_EMBED_BATCH_SIZE")
    if raw is None:
        return _DEFAULT_EMBED_BATCH_SIZE
    try:
        return int(raw)  # clamp in __init__
    except ValueError:
        # Bad env → default, never crash import for every service that imports
        # embeddings.py (serve / metric-service). HLD §3.5.
        return _DEFAULT_EMBED_BATCH_SIZE


def _env_read_timeout() -> int:
    raw = os.getenv("BEDROCK_EMBED_READ_TIMEOUT")
    if raw is None:
        return _DEFAULT_EMBED_READ_TIMEOUT
    try:
        return int(raw)
    except ValueError:
        # Bad env (e.g. "120s") → default, never crash BedrockEmbedder()
        # construction for serve / metric-service / induction. HLD §3.4.
        return _DEFAULT_EMBED_READ_TIMEOUT


class BedrockEmbedder:
    """Provider-agnostic Bedrock text embedder.

    Args:
        model_id: Bedrock model / inference-profile id. Defaults to the
            ``BEDROCK_EMBED_MODEL_ID`` env var, then :data:`DEFAULT_EMBED_MODEL_ID`.
        dimensions: Output embedding dimensions.
        region: AWS region; defaults to ``BEDROCK_REGION`` / resolved region.
        max_workers: Thread-pool size for :meth:`embed_documents` batch calls.
        read_timeout: Per-instance socket read timeout (seconds). Defaults to
            ``BEDROCK_EMBED_READ_TIMEOUT`` then boto3's 60s (serve parity);
            induction opts up to 120s for the 96-text worst case. Per-instance
            so serve's single-text ``embed_query`` is never slowed. HLD §3.4.
        batch_size: Cohere v4 texts-per-``InvokeModel`` (clamped to [1,96]).
            Defaults to ``BEDROCK_EMBED_BATCH_SIZE`` then 96. ``=1`` is a
            per-instance batch kill-switch. HLD §3.5.
        cost_tracker: Optional per-job Bedrock usage accumulator. When present,
            every successful embed call records input tokens under the "embed"
            stage; when ``None`` the embedder behaves exactly as before.
    """

    def __init__(
        self,
        model_id: str | None = None,
        dimensions: int = DEFAULT_EMBED_DIMENSIONS,
        region: str | None = None,
        max_workers: int = 10,
        read_timeout: int | None = None,
        batch_size: int | None = None,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        """Resolve model/region/timeout config and clamp the batch size (see class Args)."""
        self.model_id = model_id or os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID)
        self.dimensions = dimensions
        self._region = region or os.environ.get("BEDROCK_REGION", resolve_region())
        self._max_workers = max_workers
        self._read_timeout = read_timeout if read_timeout is not None else _env_read_timeout()
        self._batch_size = max(1, min(96, batch_size if batch_size is not None else _env_batch_size()))
        self._cost_tracker = cost_tracker
        self._client: Any = None
        self._client_lock = threading.Lock()

    @property
    def client(self) -> Any:
        """Lazily construct and cache the Bedrock runtime client (thread-safe)."""
        # Double-checked locking: embed_documents() fans out over a thread pool,
        # and concurrent boto3 client CREATION is not thread-safe.
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = boto3.client(
                        "bedrock-runtime",
                        region_name=self._region,
                        config=Config(
                            max_pool_connections=max(self._max_workers, 10),
                            read_timeout=self._read_timeout,
                            retries={"max_attempts": 5, "mode": "adaptive"},
                        ),
                    )
        return self._client

    def set_cost_tracker(self, cost_tracker: CostTracker | None) -> None:
        """Attach (or clear) a per-job Bedrock usage tracker after construction.

        The embedder is often built once (app startup / pipeline factory) while
        the tracker is created per induction job, so it must be settable late.
        """
        self._cost_tracker = cost_tracker

    # ── request/response encoding per provider ──────────────────────────
    def _request_body(self, text: str, input_type: InputType) -> str:
        payload = _prep(text)
        if _is_cohere(self.model_id):
            return json.dumps(
                {
                    "texts": [payload],
                    "input_type": "search_document" if input_type == "document" else "search_query",
                    "output_dimension": self.dimensions,
                    "embedding_types": ["float"],
                }
            )
        # Titan (symmetric — input_type ignored)
        return json.dumps({"inputText": payload, "dimensions": self.dimensions, "normalize": True})

    def _batch_request_body(self, chunk: list[str], input_type: InputType) -> str:
        # Populated texts[] (all N, _prep'd) — the actual batch. Mirrors
        # _request_body's single-element Cohere body. HLD §3.3.
        return json.dumps(
            {
                "texts": [_prep(t) for t in chunk],
                "input_type": "search_document" if input_type == "document" else "search_query",
                "output_dimension": self.dimensions,
                "embedding_types": ["float"],
            }
        )

    def _parse_response(self, body: dict[str, Any]) -> list[float]:
        if _is_cohere(self.model_id):
            # {"embeddings": {"float": [[...]]}}
            floats = body.get("embeddings", {}).get("float")
            if not floats:
                raise ValueError(f"No Cohere embedding in response: keys={list(body.keys())}")
            return floats[0]
        embedding = body.get("embedding")
        if not embedding:
            raise ValueError(f"No Titan embedding in response: keys={list(body.keys())}")
        return embedding

    def _parse_batch(self, body: dict[str, Any], expected: int) -> list[list[float]]:
        # Read the WHOLE by-type list (not [0]) and enforce len==len at the
        # source — a partial/truncated response becomes a loud ValueError at the
        # InvokeModel boundary, not a silent index-misalign in a rigor caller or
        # an opaque strict-zip failure deep in ingest. HLD §3.3 (C3a).
        floats = body.get("embeddings", {}).get("float")
        if not floats:
            raise ValueError(f"No Cohere embeddings in batch response: keys={list(body.keys())}")
        if len(floats) != expected:
            raise ValueError(f"Cohere returned {len(floats)} vectors for {expected} inputs (model={self.model_id})")
        return floats

    def _input_tokens(self, text: str, body: dict[str, Any]) -> int:
        """Best-effort input-token count for one embedded text.

        Titan returns ``inputTextTokenCount`` in the response body — use it when
        present. Cohere embed-v4 returns NO token count, so estimate from the
        (already char-capped) input text.
        """
        titan_count = body.get("inputTextTokenCount")
        # Accept any real number but reject bool (isinstance(True, int) is True) and
        # coerce to int, so a float count (e.g. 12.0) is honoured instead of estimated.
        if isinstance(titan_count, (int, float)) and not isinstance(titan_count, bool):
            return int(titan_count)
        # ponytail: 4-chars/token estimate for Cohere (no token count in response); good enough for cost signal
        return max(1, math.ceil(len(text[:_MAX_INPUT_CHARS]) / 4))

    def _record_cost(self, texts: list[str], body: dict[str, Any]) -> None:
        # Success-path only, mirroring the LLM/rerank seams. Embeddings bill on
        # input tokens exclusively (output_tokens always 0). Sum over the texts
        # so a batched call meters the same total as the old per-text calls did.
        # A per-call Titan `inputTextTokenCount` only appears on the single-text
        # path (Titan is never batched), so it maps to its one text. HLD §335e41e5.
        if self._cost_tracker is None:
            return
        tokens = sum(self._input_tokens(t, body) for t in texts)
        self._cost_tracker.record("embed", self.model_id, tokens, 0)

    # ── transport + retry core (shared by single + batch) ────────────────
    def _invoke(self, body: str, *, retry_timeouts: bool = True) -> dict[str, Any]:
        """Invoke the model, retrying app-level timeouts only when subdivision is impossible.

        Botocore owns throttle/5xx + transport-timeout retry.
        App-level 408 (ModelTimeoutException, #777) retry applies ONLY when
        ``retry_timeouts`` — i.e. the caller cannot subdivide (single text /
        size-1 leaf). Batch chunks pass ``False`` so a timeout bubbles on first
        occurrence and the caller halves. ``except ClientError`` only — a
        BotoCoreError (Read/ConnectTimeout) propagates untouched to the
        subdivide. HLD §3.1 / §3.4.1.
        """
        attempt = 0
        while True:
            try:
                resp = self.client.invoke_model(modelId=self.model_id, body=body)
                return json.loads(resp["body"].read())
            except ClientError as exc:
                if not (retry_timeouts and _is_retryable(exc)) or attempt >= _EMBED_MAX_RETRIES:
                    raise
                self._emit("EmbedRetries")
                time.sleep(_embed_backoff(attempt))
                attempt += 1

    def _embed(self, text: str, input_type: InputType) -> list[float]:
        body = self._invoke(self._request_body(text, input_type))
        self._record_cost([text], body)
        return self._parse_response(body)

    # ── observability (§3.6) ─────────────────────────────────────────────
    def _emit(self, name: str, value: float = 1.0, input_type: InputType | None = None) -> None:
        emit_metric(
            _METRIC_NAMESPACE,
            name,
            value,
            unit="Count",
            model_id=self.model_id,
            input_type=input_type,
        )

    # ── batch path (Cohere v4 only) ──────────────────────────────────────
    def _embed_batch(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        # Chunk ≤ batch_size, keep the ~10× thread-pool fan-out over chunks.
        # Executor.map yields in submission order, so flattening preserves the
        # len==len / input-order contract by construction. HLD §3.3.
        chunks = [texts[i : i + self._batch_size] for i in range(0, len(texts), self._batch_size)]
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            per_chunk = pool.map(lambda c: self._embed_chunk(c, input_type), chunks)
        return [vec for chunk_vecs in per_chunk for vec in chunk_vecs]

    def _embed_chunk(self, chunk: list[str], input_type: InputType) -> list[list[float]]:
        # Subdivide-on-timeout: halve toward the timeout budget and cap the blast
        # radius to the genuinely-failing texts. Recursion runs inside each worker
        # so concurrency never perturbs ordering. HLD §3.3 / §3.3.1.
        self._emit("EmbedBatchSizeUsed", value=len(chunk), input_type=input_type)
        try:
            body = self._batch_request_body(chunk, input_type)
            parsed = self._invoke(body, retry_timeouts=len(chunk) == 1)
            vectors = self._parse_batch(parsed, len(chunk))
            self._record_cost(chunk, parsed)  # meter cost on the batch success path too (335e41e5)
            return vectors
        except _SUBDIVIDE_ON as exc:
            if isinstance(exc, (ReadTimeoutError, ConnectTimeoutError)):
                self._emit("EmbedReadTimeouts", input_type=input_type)
            if len(chunk) == 1 or not _is_subdividable(exc):
                raise _fatal_if_validation(exc, chunk) from exc
            self._emit("EmbedChunkSubdivisions", input_type=input_type)
            mid = len(chunk) // 2
            return self._embed_chunk(chunk[:mid], input_type) + self._embed_chunk(chunk[mid:], input_type)

    # ── public API ──────────────────────────────────────────────────────
    def embed_document(self, text: str) -> list[float]:
        """Embed indexable content (Cohere ``search_document``)."""
        return self._embed(text, "document")

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (Cohere ``search_query``)."""
        return self._embed(text, "query")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed many documents, order preserved, one vector per input (incl. duplicates).

        Cohere Embed v4 batches ≤ ``batch_size`` texts per ``InvokeModel``
        (~96× fewer requests). Titan / non-v4 Cohere keep the per-text
        thread-pool fan-out — no verified v4 ``texts[]`` batch there. HLD §3.2.
        """
        if not _is_cohere_v4(self.model_id):
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                return list(pool.map(self.embed_document, texts))
        return self._embed_batch(texts, "document")


def make_llama_index_embedding(
    model_id: str | None = None,
    dimensions: int = DEFAULT_EMBED_DIMENSIONS,
    region: str | None = None,
) -> Any:
    """A LlamaIndex ``BaseEmbedding`` backed by :class:`BedrockEmbedder`.

    graphrag-toolkit configures embeddings via ``GraphRAGConfig.embed_model``.
    If given a model-id STRING it builds its own ``llama_index`` ``BedrockEmbedding``,
    whose Cohere request omits ``output_dimension`` — so Cohere Embed v4 returns
    its DEFAULT 1536-dim vector instead of the 1024 the index is created with,
    breaking ingestion. Passing a ``BaseEmbedding`` INSTANCE instead makes the
    toolkit use it as-is (``GraphRAGConfig.to_embedding_model`` returns instances
    unchanged), so we control the exact request and guarantee ``dimensions`` and
    the search_document/search_query asymmetry are honoured.

    Imported lazily: only the graphrag paths (doc-kg-build, serve lexical
    retriever) depend on ``llama_index``; other services must not.

    MUST be picklable: graphrag's build pipeline fans out over a
    ProcessPoolExecutor, so ``embed_model`` is pickled to worker processes. The
    adapter is therefore a MODULE-LEVEL class (not a closure) that reconstructs
    its :class:`BedrockEmbedder` from plain fields — a locally-defined class or a
    captured closure fails to pickle and graphrag silently drops the vector
    store, breaking ingestion. The class is built lazily; the module
    ``__getattr__`` below makes its name resolvable in a freshly spawned worker.
    """
    cls = _bedrock_embedder_llama_index_cls()
    resolved_id = model_id or os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID)
    return cls(
        model_name=resolved_id,
        embed_batch_size=10,
        embed_model_id=resolved_id,
        embed_dimensions=dimensions,
        embed_region=region,
    )


# Cache the dynamically-built adapter class at module scope so instances pickle
# by qualified name (coa_common.embeddings.<cls>) across processes.
_LLAMA_ADAPTER_CLS: Any = None


def _bedrock_embedder_llama_index_cls() -> Any:
    global _LLAMA_ADAPTER_CLS
    if _LLAMA_ADAPTER_CLS is not None:
        return _LLAMA_ADAPTER_CLS

    from llama_index.core.base.embeddings.base import BaseEmbedding
    from pydantic import PrivateAttr

    class BedrockEmbedderLlamaIndex(BaseEmbedding):
        """Picklable LlamaIndex adapter over :class:`BedrockEmbedder`.

        The embedder is rebuilt lazily from the model-id/dimensions/region
        fields, so instances carry no unpicklable client across process
        boundaries.
        """

        embed_model_id: str
        embed_dimensions: int = DEFAULT_EMBED_DIMENSIONS
        embed_region: str | None = None
        _embedder: BedrockEmbedder | None = PrivateAttr(default=None)

        def _get_embedder(self) -> BedrockEmbedder:
            if self._embedder is None:
                self._embedder = BedrockEmbedder(
                    model_id=self.embed_model_id,
                    dimensions=self.embed_dimensions,
                    region=self.embed_region,
                )
            return self._embedder

        def _get_query_embedding(self, query: str) -> list[float]:
            return self._get_embedder().embed_query(query)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._get_embedder().embed_query(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._get_embedder().embed_document(text)

        async def _aget_text_embedding(self, text: str) -> list[float]:
            return self._get_embedder().embed_document(text)

    # Expose at module scope so pickle can resolve it by qualified name.
    BedrockEmbedderLlamaIndex.__module__ = __name__
    BedrockEmbedderLlamaIndex.__qualname__ = "BedrockEmbedderLlamaIndex"
    globals()["BedrockEmbedderLlamaIndex"] = BedrockEmbedderLlamaIndex
    _LLAMA_ADAPTER_CLS = BedrockEmbedderLlamaIndex
    return _LLAMA_ADAPTER_CLS


# Names this module builds on first access instead of at import time.
_LAZY_ATTRS = frozenset({"BedrockEmbedderLlamaIndex"})

if not TYPE_CHECKING:
    # Hidden from type checkers so a typo in an import is still reported as a
    # missing attribute instead of silently typing as ``Any``.

    def __getattr__(name: str) -> Any:
        """Build the lazy LlamaIndex adapter class when it is looked up by name.

        Pickle stores an adapter instance as ``coa_common.embeddings.BedrockEmbedderLlamaIndex``.
        A worker started with the ``spawn`` or ``forkserver`` method imports this
        module fresh and never calls :func:`make_llama_index_embedding`, so the
        name would not exist there and unpickling would fail, killing the
        worker pool. This hook (PEP 562; only consulted when normal lookup fails)
        builds and caches the class on first access, which also keeps
        ``llama_index`` out of the import path of services that never use it.
        """
        if name in _LAZY_ATTRS:
            return _bedrock_embedder_llama_index_cls()
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
