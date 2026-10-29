# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lexical-KG retrieval via graphrag-toolkit. No ontology consulted.

Adapter that instantiates a graphrag-toolkit LexicalGraphQueryEngine
configured for traversal-based search and exposes a method shaped like
the rest of tier3/. Does NOT subclass any toolkit class.

The published ontology graph (urn:coa:{namespace}:published) is not
consulted. This is the floor against which future ontology-aware
retrieval will be measured.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import graphrag_toolkit.lexical_graph.config as _graphrag_cfg
import structlog
from coa_common import make_llama_index_embedding, resolve_region
from coa_common.constants import DEFAULT_EMBED_DIMENSIONS, DEFAULT_EMBED_MODEL_ID

from ..graph_context import (
    SUPPORTING_STATEMENTS_KEY,
    bound_supporting_statements,
    encode_supporting_statements,
)
from ..step_ids import StepId
from ..tier3.graph_traverser import GraphEntity
from ..tier3.vector_retriever import ChunkResult
from .strategies import (
    _PRESERVED_RESULT_SCORE_METADATA_KEY,
    DEFAULT_SPEC,
    StrategySpec,
    score_preservation_installed,
)

# ---------------------------------------------------------------------------
# WORKAROUND: graphrag-toolkit v3.18.3 hardcodes a retired Bedrock model
# (us.anthropic.claude-3-7-sonnet-20250219-v1:0) as the default for both
# extraction and response LLMs. Additionally, GraphRAGConfig.response_llm
# setter does not propagate to keyword_vss_provider (GitHub issue filed).
#
# Patching the module-level constants is the only reliable workaround.
# TODO: Remove once graphrag-toolkit publishes a fix with active model defaults.
#
# Model resolution: GRAPHRAG_LLM_MODEL (explicit override), then BEDROCK_MODEL_ID
# (serve-stack always sets it from bedrockLlmModelId, which synth validates for
# the deploy region), then the US default. ``or`` so an empty value falls through.
# ---------------------------------------------------------------------------
_ACTIVE_MODEL = (
    os.environ.get("GRAPHRAG_LLM_MODEL") or os.environ.get("BEDROCK_MODEL_ID") or "us.anthropic.claude-sonnet-5"
)
_graphrag_cfg.DEFAULT_RESPONSE_MODEL = _ACTIVE_MODEL
_graphrag_cfg.DEFAULT_EXTRACTION_MODEL = _ACTIVE_MODEL

logger = structlog.get_logger(__name__)

# Bound the number of worker threads used for synchronous toolkit retrieval.
# A timed-out toolkit call cannot be cancelled (it runs to completion on its
# worker), so without a bound each timed-out query would leak a thread against
# the shared default executor. A dedicated, bounded pool caps concurrency:
# excess concurrent lexical calls queue rather than spawning unbounded threads.
_LEXICAL_MAX_WORKERS = int(os.environ.get("LEXICAL_MAX_WORKERS", "4"))

# Default values used when the toolkit node metadata does not carry the
# expected fields. Kept as module constants so mapping fallbacks are explicit.
_DEFAULT_ENTITY_TYPE = "lexical_entity"

# Vector indexes serve OPENS for retrieval. MUST mirror kg-build's EMBEDDING_INDEXES
# (currently ``chunk,topic``) — naming an index the graph does not have is not
# harmless: the toolkit's ``index_exists`` treats a missing index as its cue to
# CREATE one, and on AOSS NEXTGEN that create is rejected with
# ``illegal_argument_exception: Field parameter 'engine' is not supported`` (the
# incompatibility kg-build monkey-patches, which serve does not). The exception is
# logged and swallowed, leaving a broken index handle behind.
#
# Naming ``statement`` here (the toolkit default, alongside ``chunk``) did exactly
# that on a chunk,topic graph, and the resulting failure surfaced as TopicBeamSearch
# returning 1 node with confidence 0.0 and "No relevant context" — a symptom that
# points nowhere near the real cause. ``topic`` must still be named explicitly or
# TopicBeamSearch gets a DummyVectorIndex.
#
# Env-overridable so a graph built with a different EMBEDDING_INDEXES set can be
# read without a deploy; keep the two in lockstep.
_READ_EMBEDDING_INDEXES = [
    name.strip() for name in os.environ.get("READ_EMBEDDING_INDEXES", "chunk,topic").split(",") if name.strip()
]

# ---------------------------------------------------------------------------
# Tenant override seam (eval-only).
#
# Production multi-tenancy contract (design Property 3 / BUG 6): the retriever
# derives ``tenant_id = to_graphrag_tenant_id(namespace)`` and passes it to the
# toolkit factory. ``tenant_id_override=None`` (the default) preserves exactly
# that behavior.
#
# ``DEFAULT_TENANT_OVERRIDE`` is a sentinel — distinct from ``None`` — that
# means "ignore the namespace and read under the graphrag-toolkit *default*
# tenant" (``tenant_id=None`` → the factory's ``to_tenant_id(None)`` →
# ``DEFAULT_TENANT_ID`` → bare ``__Chunk__`` labels / bare ``chunk`` index).
# Its value is intentionally NOT a valid graphrag TenantId (which is 1–25
# lowercase alphanumerics/periods), so it can never collide with a real
# namespace-derived tenant. The seam is general (an explicit literal tenant
# string is also accepted), but the only in-tree use is the eval/integ harness
# reading the toolkit-built default-tenant SEC-10Q graph (task 12); it is NOT
# wired into ``build_lexical_retriever`` or the request path.
DEFAULT_TENANT_OVERRIDE = "__graphrag_default_tenant__"

# One-time guard so an unexpected toolkit metadata shape logs a single warning
# rather than once per node per request (which would be noisy under load).
_shape_mismatch_warned = False


def _warn_shape_mismatch_once(**details) -> None:
    """Emit a single ``logger.warning`` for an unexpected toolkit node shape.

    The first call logs; subsequent calls are no-ops for the lifetime of the
    process. This keeps a genuine shape regression visible without flooding
    logs when every node in every request shares the same unexpected shape.
    """
    global _shape_mismatch_warned
    if not _shape_mismatch_warned:
        _shape_mismatch_warned = True
        logger.warning("baseline_map_results_shape_mismatch", **details)


def _extract_source(metadata: dict) -> tuple[str, dict | None]:
    """Read one source identity and its matching metadata container.

    Tries, in order:
      * ``metadata["source"]["sourceId"]`` — the ``Source`` shape produced by
        ``BedrockContextFormat`` grouping / the ``Source`` pydantic model.
      * ``metadata["source"]`` as a plain string (simple/legacy shape).
      * ``metadata["result"]["source"]`` — the traversal retriever emits the
        raw ``SearchResult`` dump under ``"result"``, whose ``source`` is itself
        either a nested ``Source`` dict (``sourceId``) or a bare string.

    The returned source dict is the exact container that supplied the identity,
    preventing a display name from a lower-priority source from being paired
    with a different identity. Legacy string sources have no metadata container.
    """
    source = metadata.get("source")
    if isinstance(source, dict):
        source_id = source.get("sourceId")
        if isinstance(source_id, str) and source_id.strip():
            return source_id.strip(), source
    elif isinstance(source, str) and source.strip():
        return source.strip(), None

    result = metadata.get("result")
    if isinstance(result, dict):
        result_source = result.get("source")
        if isinstance(result_source, dict):
            source_id = result_source.get("sourceId")
            if isinstance(source_id, str) and source_id.strip():
                return source_id.strip(), result_source
        elif isinstance(result_source, str) and result_source.strip():
            return result_source.strip(), None

    return "", None


def _extract_source_doc(metadata: dict) -> str:
    """Read the opaque source document id from the real toolkit shape."""
    source_document_id, _ = _extract_source(metadata)
    return source_document_id


def _extract_source_doc_name(metadata: dict) -> str:
    """Read the original file name from a toolkit ``Source`` metadata payload.

    The raw ``source_s3_key`` preserves the uploaded file's extension, while
    ``filename`` may name a converted staging artifact, so the raw-key basename
    wins. This mirrors the OpenSearch provenance mapping used by the hand-rolled
    path without treating either path-shaped value as document identity.
    """
    _, source = _extract_source(metadata)
    if source is None:
        return ""

    source_metadata = source.get("metadata")
    if not isinstance(source_metadata, dict):
        return ""
    source_s3_key = source_metadata.get("source_s3_key")
    if isinstance(source_s3_key, str) and source_s3_key:
        return source_s3_key.rsplit("/", 1)[-1]
    filename = source_metadata.get("filename")
    return filename if isinstance(filename, str) else ""


def _finite_score(value: object) -> float | None:
    """Return a finite numeric score, rejecting booleans and malformed values."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _extract_relevance_score(metadata: dict) -> float | None:
    """Read the exact ranked toolkit score preserved before ``ClearScores``."""
    result = metadata.get("result")
    if not isinstance(result, dict):
        return None

    result_score = _finite_score(result.get("score"))
    if result_score is not None:
        return result_score

    topics = result.get("topics")
    if not isinstance(topics, list):
        return None

    for topic in topics:
        if not isinstance(topic, dict):
            continue
        chunks = topic.get("chunks")
        if not isinstance(chunks, list):
            continue
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            chunk_metadata = chunk.get("metadata")
            if not isinstance(chunk_metadata, dict):
                continue
            preserved_score = _finite_score(chunk_metadata.get(_PRESERVED_RESULT_SCORE_METADATA_KEY))
            if preserved_score is not None:
                return preserved_score

    return None


def _source_level_chunks(metadata: dict, node_score: float | None) -> list[ChunkResult] | None:
    """Expand a toolkit source-level wrapper into its actual chunk records.

    Traversal retrievers return one formatted ``NodeWithScore`` per source, while
    retaining the unformatted ``SearchResult`` under ``metadata["result"]``.
    Its node id and text describe the aggregate wrapper, not evidence. ``None``
    means this is not that wrapper shape; an empty list means it is a wrapper but
    contains no usable chunk details and must not fall back to aggregate JSON.
    """
    result = metadata.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("topics"), list):
        return None

    source_document_id, _ = _extract_source(metadata)
    if not source_document_id:
        return []
    source_document_name = _extract_source_doc_name(metadata)
    result_score = _finite_score(result.get("score"))
    chunks: list[ChunkResult] = []

    for topic in result["topics"]:
        if not isinstance(topic, dict) or not isinstance(topic.get("chunks"), list):
            continue
        for raw_chunk in topic["chunks"]:
            if not isinstance(raw_chunk, dict):
                continue
            chunk_id = raw_chunk.get("chunkId")
            if chunk_id is None or not str(chunk_id).strip():
                continue
            chunk_metadata = raw_chunk.get("metadata")
            if not isinstance(chunk_metadata, dict):
                chunk_metadata = {}
            text = raw_chunk.get("value")
            if not isinstance(text, str) or not text:
                text = chunk_metadata.get("value")
            if not isinstance(text, str) or not text:
                continue

            score_candidates = (
                _finite_score(raw_chunk.get("score")),
                _finite_score(chunk_metadata.get(_PRESERVED_RESULT_SCORE_METADATA_KEY)),
                result_score,
                node_score,
            )
            score = next((candidate for candidate in score_candidates if candidate is not None), 0.0)
            chunks.append(
                ChunkResult(
                    chunk_id=str(chunk_id),
                    text=text,
                    source_doc=source_document_id,
                    source_doc_name=source_document_name,
                    relevance_score=score,
                )
            )

    return chunks


def _append_graph_statement(entity: GraphEntity, text: str) -> GraphEntity:
    """Retain anonymous graph-derived text as bounded GraphContext evidence."""
    properties = dict(entity.properties)
    statements = bound_supporting_statements(
        [
            *bound_supporting_statements(properties.get(SUPPORTING_STATEMENTS_KEY)),
            text,
        ]
    )
    properties[SUPPORTING_STATEMENTS_KEY] = encode_supporting_statements(statements)
    return replace(entity, properties=properties)


def _extract_entities(metadata: dict) -> list[tuple[str, str, str]]:
    """Read entities as ``(uri, label, type)`` tuples from the toolkit shape.

    Primary shape is the toolkit ``EntityContexts`` dump under
    ``metadata["entity_contexts"]``::

        {"contexts": [{"entities": [{"entity": {"entityId", "value",
         "classification"}, "score": ...}]}], "keywords": [...]}

    Falls back to the simple/legacy ``metadata["entities"]`` list-of-strings
    shape. Every access is defensive (``.get`` / ``isinstance``) so a partial
    or unexpected structure yields fewer entities rather than raising.
    """
    out: list[tuple[str, str, str]] = []

    entity_contexts = metadata.get("entity_contexts")
    if isinstance(entity_contexts, dict):
        contexts = entity_contexts.get("contexts")
        if isinstance(contexts, list):
            for context in contexts:
                if not isinstance(context, dict):
                    continue
                scored_entities = context.get("entities")
                if not isinstance(scored_entities, list):
                    continue
                for scored in scored_entities:
                    if not isinstance(scored, dict):
                        continue
                    entity = scored.get("entity")
                    if not isinstance(entity, dict):
                        continue
                    value = entity.get("value")
                    if not (isinstance(value, str) and value):
                        continue
                    entity_id = entity.get("entityId")
                    uri = entity_id if isinstance(entity_id, str) and entity_id else value
                    classification = entity.get("classification")
                    entity_type = (
                        classification if isinstance(classification, str) and classification else _DEFAULT_ENTITY_TYPE
                    )
                    out.append((uri, value, entity_type))

    legacy_entities = metadata.get("entities")
    if isinstance(legacy_entities, list):
        for name in legacy_entities:
            if isinstance(name, str) and name:
                out.append((name, name, _DEFAULT_ENTITY_TYPE))

    return out


@dataclass(frozen=True)
class BaselineResult:
    """Result from graphrag-toolkit lexical retrieval."""

    chunks: tuple[ChunkResult, ...]
    entities: tuple[GraphEntity, ...]
    source: str = "graphrag-toolkit/lexical"
    trace_steps: tuple[dict, ...] = ()
    dropped_anonymous_chunks: int = 0
    # True when toolkit score preservation could not be installed, so every
    # ``relevance_score`` on ``chunks`` is 0.0 for reasons unrelated to relevance.
    scores_unranked: bool = False


class BaselineLexicalRetriever:
    """Lexical-KG retrieval via graphrag-toolkit. No ontology consulted."""

    def __init__(
        self,
        graph_store_uri: str,
        vector_store_uri: str,
        *,
        strategy: StrategySpec = DEFAULT_SPEC,
        retrievers: list | None = None,
        post_processors: list | None = None,
        timeout_s: float = 15.0,
        tenant_id_override: str | None = None,
    ):
        """Configure the graphrag stores, strategy, and lexical retrieval knobs.

        Args:
            graph_store_uri: URI of the graphrag graph store.
            vector_store_uri: URI of the graphrag vector store.
            strategy: Retrieval strategy spec (retrievers + post-processors).
            retrievers: Optional explicit retrievers overriding the strategy (tests).
            post_processors: Optional explicit post-processors overriding the strategy.
            timeout_s: Per-query retrieval timeout in seconds.
            tenant_id_override: Eval-only tenant override; None derives the tenant
                from the namespace (production default).
        """
        self._graph_store_uri = graph_store_uri
        self._vector_store_uri = vector_store_uri
        self._strategy = strategy
        # Optional explicit escape hatch for tests: when provided, these override
        # the strategy's own retriever/post-processor configuration.
        self._retrievers = retrievers
        self._post_processors = post_processors
        self._timeout_s = timeout_s
        # Eval-only tenant override seam (see DEFAULT_TENANT_OVERRIDE).
        #   None                     -> derive tenant from namespace (production default).
        #   DEFAULT_TENANT_OVERRIDE  -> read under the toolkit default tenant (tenant_id=None).
        #   any other string         -> read under that literal tenant.
        # NOT wired into build_lexical_retriever / the request path; constructed
        # directly only by the eval/integ harness (task 12).
        self._tenant_id_override = tenant_id_override
        # Cache of query engines keyed by (effective_tenant, strategy.name) so a
        # per-request strategy override does not collide with the cache, and so an
        # override engine never collides with a namespace-derived engine. The
        # effective tenant is the override (or ``None`` for the toolkit default
        # tenant) when one is set, otherwise the namespace-derived tenant_id.
        self._engines: dict[tuple[str | None, str], object] = {}
        # Strategy names whose engine could NOT be patched for score preservation.
        # Membership is a property of the installed toolkit build and the strategy,
        # not of a tenant, so recording it per strategy name is sufficient. Every
        # retrieval on such a strategy returns 0.0 relevance for every chunk, which
        # is indistinguishable from genuinely irrelevant results — so the retrieval
        # step is marked degraded rather than reported as a clean success.
        self._unranked_strategies: set[str] = set()

        # Dedicated bounded executor for synchronous toolkit retrieval. Owning a
        # private pool (instead of asyncio's shared default `to_thread` executor)
        # bounds concurrency and prevents a timed-out, uncancellable toolkit call
        # from starving unrelated `to_thread` work.
        self._executor = ThreadPoolExecutor(
            max_workers=_LEXICAL_MAX_WORKERS,
            thread_name_prefix="lexical-retr",
        )

        from graphrag_toolkit.lexical_graph import GraphRAGConfig

        # Must set config BEFORE engine creation — singleton pattern.
        # Pin embed_model to the shared default so serve retrieves with the SAME
        # model doc-kg-build ingested chunks with. Pass an embedding
        # INSTANCE, not a string: the toolkit's string path omits Cohere's
        # output_dimension → 1536-dim query vectors vs the 1024-dim index.
        GraphRAGConfig.aws_region = resolve_region()
        GraphRAGConfig.embed_model = make_llama_index_embedding(
            model_id=os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID),
            dimensions=DEFAULT_EMBED_DIMENSIONS,
            region=resolve_region(),
        )
        GraphRAGConfig.embed_dimensions = DEFAULT_EMBED_DIMENSIONS

        logger.info(
            "baseline_lexical_retriever_init",
            graph_store_uri=graph_store_uri,
            vector_store_uri=vector_store_uri,
            timeout_s=timeout_s,
        )

    def _get_engine(self, namespace: str, strategy: StrategySpec):
        """Get or create a query engine, cached per (effective_tenant, strategy.name).

        By default (``tenant_id_override is None``) the effective tenant is
        ``to_graphrag_tenant_id(namespace)`` — the production multi-tenancy
        contract (design Property 3 / BUG 6), unchanged. When the eval-only
        override is set, the namespace derivation is bypassed:

          * ``DEFAULT_TENANT_OVERRIDE`` → ``tenant_id=None`` so the toolkit
            factory reads under its default tenant (bare ``__Chunk__`` labels /
            bare ``chunk`` index).
          * any other override string → that literal tenant is read.
        """
        from coa_common.constants import to_graphrag_tenant_id

        override_used = self._tenant_id_override is not None
        if not override_used:
            # Production default: derive the tenant from the namespace.
            tenant_id: str | None = to_graphrag_tenant_id(namespace)
        elif self._tenant_id_override == DEFAULT_TENANT_OVERRIDE:
            # Toolkit default tenant: pass tenant_id=None to the factory.
            tenant_id = None
        else:
            # Explicit literal tenant to read.
            tenant_id = self._tenant_id_override

        key = (tenant_id, strategy.name)

        if key in self._engines:
            return self._engines[key]

        from graphrag_toolkit.lexical_graph.storage import GraphStoreFactory, VectorStoreFactory

        from .cross_script_prune import patch_cross_script_statement_prune
        from .graphrag_patches import patch_paginated_search_source

        # Must run before any index is queried: AOSS NEXTGEN omits knn_vector from the
        # default _source, which silently collapses the TopicBeamSearch beam.
        patch_paginated_search_source()
        # A question in one script must not lose the statements written in another
        # because a few same-script statements share a word with it (#1263).
        patch_cross_script_statement_prune()

        graph_store = GraphStoreFactory.for_graph_store(self._graph_store_uri)
        # Open the topic index too, not just the toolkit default ['chunk','statement'].
        # TopicBeamSearch (TopicGraphRAG) seeds from ``get_index('topic')``; without
        # ``topic`` in index_names the factory hands back a DummyVectorIndex and the
        # retriever dies with "'DummyVectorIndex' object has no attribute
        # 'embed_model'" — observed live on a namespace whose topic index exists with
        # 5,681 vectors. This is the READ-side counterpart of the kg-build change that
        # started WRITING the topic index (EMBEDDING_INDEXES); the write side alone is
        # useless if serve never opens the index. Naming an extra index is NOT harmless
        # though — see _READ_EMBEDDING_INDEXES: a name the graph lacks triggers a create
        # that AOSS NEXTGEN rejects. Keep this list mirroring kg-build's.
        vector_store = VectorStoreFactory.for_vector_store(self._vector_store_uri, index_names=_READ_EMBEDDING_INDEXES)

        if self._retrievers is not None or self._post_processors is not None:
            # Explicit escape hatch for tests: build a traversal engine directly
            # with the supplied retrievers/post-processors, bypassing the strategy.
            engine = self._build_engine_with_overrides(graph_store, vector_store, tenant_id=tenant_id)
        else:
            engine = strategy.build_engine(graph_store, vector_store, tenant_id=tenant_id)

        self._engines[key] = engine
        score_preservation = score_preservation_installed(engine)
        if not score_preservation:
            self._unranked_strategies.add(str(strategy.name))
        logger.info(
            "baseline_lexical_engine_created",
            tenant_id=tenant_id,
            namespace=namespace,
            strategy=str(strategy.name),
            tenant_override_used=override_used,
            score_preservation_installed=score_preservation,
        )
        return engine

    def _build_engine_with_overrides(self, graph_store, vector_store, *, tenant_id):
        """Build a traversal engine from explicit retrievers/post-processors (test hook)."""
        from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
        from graphrag_toolkit.lexical_graph.retrieval.post_processors import BedrockContextFormat

        post_processors = self._post_processors if self._post_processors is not None else [BedrockContextFormat()]
        kwargs = {"tenant_id": tenant_id, "post_processors": post_processors}
        if self._retrievers is not None:
            kwargs["retrievers"] = self._retrievers
        return LexicalGraphQueryEngine.for_traversal_based_search(graph_store, vector_store, **kwargs)

    # Bounded retry for transient Neptune connection drops. The toolkit's Neptune
    # client hardcodes ``total_max_attempts=1`` (no retry) and does NOT let us
    # override ``retries`` via config (``create_config`` passes it as an explicit
    # Config kwarg, so an override raises "multiple values for keyword 'retries'").
    # Neptune reaps idle/stale pooled connections even when near-idle, so a reused
    # connection can drop mid-request and surface as a GraphQueryError. Two distinct
    # transient signatures are observed on the deployed serve runtime, both from a
    # dropped pooled connection on an idempotent read (safe to retry):
    #   * "Connection was closed before we received a valid response" (ConnectionClosedError)
    #   * "SSL: UNEXPECTED_EOF_WHILE_READING" (the TLS socket closed mid-read)
    # ``chunk_based_semantic`` is the most exposed strategy because it fans out the
    # most concurrent Neptune queries (cosine seeds x threaded per-chunk statement
    # expansion + beam), so a single dropped connection there previously zeroed the
    # whole retriever. We match on the message substring rather than the exception
    # type because the toolkit wraps every backend failure in a generic
    # GraphQueryError. ``unexpected_eof_while_reading`` is matched specifically (not
    # the generic "ssl validation failed") so a genuine, persistent cert
    # misconfiguration is NOT silently retried.
    _RETRIABLE_QUERY_ERROR_MARKERS = (
        "connection was closed",
        "connectionclosederror",
        "unexpected_eof_while_reading",
    )
    _MAX_QUERY_ATTEMPTS = 3
    _QUERY_RETRY_BACKOFF_S = 0.5

    def _retrieve_sync(self, query: str, namespace: str, strategy: StrategySpec) -> list:
        """Synchronous retrieval call (toolkit does not support async).

        Retries a transient Neptune connection drop (idempotent read) up to
        ``_MAX_QUERY_ATTEMPTS`` with linear backoff; any other error propagates on
        the first occurrence (the caller turns it into a degraded result).
        """
        engine = self._get_engine(namespace, strategy)
        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_QUERY_ATTEMPTS + 1):
            try:
                return engine.retrieve(query)
            except Exception as exc:  # noqa: BLE001 - inspect message to decide retry
                if not self._is_retriable_connection_error(exc) or attempt == self._MAX_QUERY_ATTEMPTS:
                    raise
                last_exc = exc
                logger.warning(
                    "baseline_retrieval_connection_retry",
                    attempt=attempt,
                    max_attempts=self._MAX_QUERY_ATTEMPTS,
                    error_type=type(exc).__name__,
                )
                time.sleep(self._QUERY_RETRY_BACKOFF_S * attempt)
        # Unreachable (the loop either returns or raises), but satisfies typing.
        raise last_exc if last_exc is not None else RuntimeError("retrieval failed")

    @classmethod
    def _is_retriable_connection_error(cls, exc: Exception) -> bool:
        """True for a transient Neptune connection drop (safe to retry an idempotent read)."""
        msg = str(exc).lower()
        return any(marker in msg for marker in cls._RETRIABLE_QUERY_ERROR_MARKERS)

    async def retrieve(self, query: str, namespace: str, strategy: StrategySpec | None = None) -> BaselineResult:
        """Run the toolkit's retrieval and return chunks + entities.

        Args:
            query: Natural language query.
            namespace: Namespace identifier — used to derive tenant_id for
                       scoping retrieval to the correct graph partition.
            strategy: Optional per-request strategy override; when ``None`` the
                      instance default (set at construction) is used.

        Returns:
            BaselineResult with chunks and entities mapped from toolkit output.
            On timeout or any retrieval error, an empty BaselineResult with an
            error trace step is returned rather than raising.
        """
        resolved = strategy if strategy is not None else self._strategy
        start = time.perf_counter()
        trace_steps: list[dict] = []

        try:
            loop = asyncio.get_running_loop()
            fut = loop.run_in_executor(self._executor, self._retrieve_sync, query, namespace, resolved)
            nodes = await asyncio.wait_for(asyncio.shield(fut), timeout=self._timeout_s)
            duration_ms = int((time.perf_counter() - start) * 1000)
            trace_steps.append(
                {
                    "step": StepId.LEXICAL_BASELINE_RETRIEVE,
                    "status": "success",
                    "durationMs": duration_ms,
                    "detail": f"strategy={resolved.name}; {len(nodes)} nodes retrieved",
                }
            )
        except TimeoutError:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.warning("baseline_retrieval_timeout", timeout_s=self._timeout_s, duration_ms=duration_ms)
            trace_steps.append(
                {
                    "step": StepId.LEXICAL_BASELINE_RETRIEVE,
                    "status": "error",
                    "durationMs": duration_ms,
                    "detail": f"strategy={resolved.name}; TimeoutError after {self._timeout_s}s",
                }
            )
            return BaselineResult(chunks=(), entities=(), trace_steps=tuple(trace_steps))
        except Exception as e:
            duration_ms = int((time.perf_counter() - start) * 1000)
            logger.error("baseline_retrieval_error", error=str(e), error_type=type(e).__name__)
            trace_steps.append(
                {
                    "step": StepId.LEXICAL_BASELINE_RETRIEVE,
                    "status": "error",
                    "durationMs": duration_ms,
                    "detail": f"strategy={resolved.name}; {type(e).__name__}",
                }
            )
            return BaselineResult(chunks=(), entities=(), trace_steps=tuple(trace_steps))

        chunks, entities, dropped_anonymous_chunks = self._map_results_with_diagnostics(nodes)
        if dropped_anonymous_chunks:
            trace_steps[-1] = {
                **trace_steps[-1],
                "status": "degraded",
                "detail": (
                    f"{trace_steps[-1]['detail']}; "
                    f"dropped {dropped_anonymous_chunks} anonymous or malformed document chunks"
                ),
            }

        scores_unranked = str(resolved.name) in self._unranked_strategies
        if scores_unranked:
            logger.warning(
                "lexical_relevance_scores_unavailable",
                strategy=str(resolved.name),
                namespace=namespace,
                chunks=len(chunks),
            )
            trace_steps[-1] = {
                **trace_steps[-1],
                "status": "degraded",
                "detail": (
                    f"{trace_steps[-1]['detail']}; "
                    "toolkit score preservation is not installed, so every relevance score is 0.0"
                ),
            }

        return BaselineResult(
            chunks=tuple(chunks),
            entities=tuple(entities),
            trace_steps=tuple(trace_steps),
            dropped_anonymous_chunks=dropped_anonymous_chunks,
            scores_unranked=scores_unranked,
        )

    @staticmethod
    def _map_results(nodes: list) -> tuple[list[ChunkResult], list[GraphEntity]]:
        """Map toolkit nodes while preserving the historical two-value test API."""
        chunks, entities, _ = BaselineLexicalRetriever._map_results_with_diagnostics(nodes)
        return chunks, entities

    @staticmethod
    def _map_results_with_diagnostics(nodes: list) -> tuple[list[ChunkResult], list[GraphEntity], int]:
        """Map graphrag-toolkit ``NodeWithScore`` results to Tier 3 data model.

        Defensive against the real toolkit metadata shapes (see
        ``bedrock_context_format.py`` / ``traversal_based_base_retriever.py``):
        ``source_doc`` is read from the nested ``Source`` (``metadata["source"]
        ["sourceId"]`` or ``metadata["result"]["source"]["sourceId"]``) and
        entities from the toolkit ``EntityContexts`` structure under
        ``metadata["entity_contexts"]``. All access uses ``.get``/``isinstance``
        with empty fallbacks, and a single ``logger.warning`` is emitted on a
        shape mismatch (a node that has metadata but yields neither a
        ``source_doc`` nor any entities) instead of crashing. Source-less text
        attached to real graph entities is retained in their
        ``supportingStatements`` property; all other anonymous/malformed text is
        dropped and counted for retrieval tracing.
        """
        chunks: list[ChunkResult] = []
        chunks_seen: set[tuple[str, str]] = set()
        entities_seen: dict[str, GraphEntity] = {}
        dropped_anonymous_chunks = 0

        for node in nodes:
            raw_node_id = getattr(node, "node_id", "")
            node_id = raw_node_id if isinstance(raw_node_id, str) and raw_node_id.strip() else ""
            raw_text = getattr(node, "text", "")
            text = raw_text if isinstance(raw_text, str) and raw_text.strip() else ""

            metadata = node.metadata if hasattr(node, "metadata") and isinstance(node.metadata, dict) else {}
            node_score = _finite_score(node.score) if hasattr(node, "score") else None

            source_doc = _extract_source_doc(metadata)
            extracted_entities = _extract_entities(metadata)
            source_level_chunks = _source_level_chunks(metadata, node_score)

            if metadata and not source_doc and not extracted_entities:
                _warn_shape_mismatch_once(
                    node_id=node_id,
                    metadata_keys=sorted(metadata.keys()),
                )

            if source_level_chunks is not None:
                for chunk in source_level_chunks:
                    chunk_key = (chunk.source_doc, chunk.chunk_id)
                    if chunk_key not in chunks_seen:
                        chunks_seen.add(chunk_key)
                        chunks.append(chunk)
                if not source_level_chunks:
                    dropped_anonymous_chunks += 1
            elif source_doc and node_id and text:
                score = node_score if node_score is not None else (_extract_relevance_score(metadata) or 0.0)
                chunk_key = (source_doc, node_id)
                if chunk_key not in chunks_seen:
                    chunks_seen.add(chunk_key)
                    chunks.append(
                        ChunkResult(
                            chunk_id=node_id,
                            text=text,
                            source_doc=source_doc,
                            source_doc_name=_extract_source_doc_name(metadata),
                            relevance_score=score,
                        )
                    )
            elif (node_id or text) and not (extracted_entities and text and not source_doc):
                # Count every drop except text that is kept below as a graph
                # statement (entities + text, no source document). A node with a
                # source document, text and entities but no node id is neither a
                # chunk nor a statement, so it is counted here too.
                dropped_anonymous_chunks += 1

            for uri, label, entity_type in extracted_entities:
                if uri not in entities_seen:
                    entities_seen[uri] = GraphEntity(
                        uri=uri,
                        label=label,
                        type=entity_type,
                    )
                if not source_doc and source_level_chunks is None and text:
                    entities_seen[uri] = _append_graph_statement(entities_seen[uri], text)

        return chunks, list(entities_seen.values()), dropped_anonymous_chunks
