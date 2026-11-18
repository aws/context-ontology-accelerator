# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Selectable graphrag-toolkit retrieval strategies.

Single source of truth mapping a named strategy to the concrete graphrag
engine + retriever configuration. Both the runtime factory/adapter and the
eval harness resolve strategies through this registry, so the adapter, the
request API, and the benchmark all agree on what each name means.

The seven traversal strategies are built through the non-deprecated
``for_traversal_based_search`` engine; ``topic_beam`` constructs its retriever
directly. The deployment default is ``topic_beam`` (``LEXICAL_RETRIEVER_STRATEGY``):

  | Strategy               | Retriever(s)                                          |
  |------------------------|-------------------------------------------------------|
  | ``chunk_based``        | ``ChunkBasedSearch``                                  |
  | ``chunk_based_semantic`` | ``ChunkBasedSemanticSearch``                        |
  | ``entity_based``       | ``EntityBasedSearch``                                 |
  | ``entity_context``     | ``EntityContextSearch``                               |
  | ``entity_network``     | ``EntityNetworkSearch``                               |
  | ``topic_based``        | ``TopicBasedSearch``                                  |
  | ``traversal``          | weighted ``ChunkBasedSearch@1.0 + EntityNetworkSearch@1.0 + TopicBasedSearch@0.5`` |
  | ``topic_beam``         | ``TopicBeamSearch``                                   |

Engine-construction notes (verified against the real graph):
  - All builders construct the engine WITHOUT an explicit ``BedrockContextFormat``
    post-processor (``post_processors=[]``, ``context_format="raw"``). That
    post-processor hard-reads top-level ``metadata["source"]``, which the
    toolkit's traversal retrievers do not emit (the source is nested under
    ``metadata["result"]["source"]``), so it raised ``KeyError: 'source'``. The
    adapter does its own node->``BaselineResult`` mapping (``_map_results``), so
    the toolkit formatter is redundant on the serve path.

IMPORTANT — botocore sync-only invariant: graphrag_toolkit must NOT be
imported at module load. All toolkit imports are LAZY (performed inside the
``build_engine`` callables), so importing this module never triggers the
graphrag/botocore import chain. Only invoking a ``build_engine`` does.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)


class RetrieverStrategy(StrEnum):
    """Named graphrag-toolkit retrieval strategies selectable per request.

    Seven non-deprecated traversal retrievers plus ``topic_beam``.
    """

    CHUNK_BASED = "chunk_based"
    CHUNK_BASED_SEMANTIC = "chunk_based_semantic"  # default
    ENTITY_BASED = "entity_based"
    ENTITY_CONTEXT = "entity_context"
    ENTITY_NETWORK = "entity_network"
    TOPIC_BASED = "topic_based"
    TRAVERSAL = "traversal"
    # TopicGraphRAG (graphrag-lexical-graph PR #318, introduced 3.18.7; pinned 3.20.0).
    # Unlike the seven above it is constructed as the engine's retriever directly
    # (engine_family="direct"), and it REQUIRES a populated ``topic`` vector index;
    # a graph ingested without topic vectors seeds empty.
    TOPIC_BEAM = "topic_beam"


DEFAULT_STRATEGY: RetrieverStrategy = RetrieverStrategy.CHUNK_BASED_SEMANTIC

# Stored on raw chunk metadata immediately before the toolkit clears the
# source-level score. Formatting clears chunks from the prompt copy, so this
# adapter-only marker never leaks into synthesis text.
_PRESERVED_RESULT_SCORE_METADATA_KEY = "_coa_result_relevance_score"

# Set on a query engine by :func:`_install_score_preserving_processors` to record
# whether the patch actually landed. The patch reads two PRIVATE graphrag-toolkit
# attributes (``engine.retriever`` and its ``retriever_fn``); a toolkit upgrade that
# renames or restructures either one makes the patch a silent no-op, and every
# lexical relevance score then reverts to 0.0 — indistinguishable from a genuine
# zero relevance, with nothing in the trace to say why. The retriever reads this
# flag and degrades the step instead.
SCORE_PRESERVATION_ATTR = "_coa_score_preservation_installed"


def score_preservation_installed(engine: object) -> bool:
    """Whether score preservation is active on ``engine``.

    Defaults to ``True`` for an engine this module never patched (a test double, or
    a direct-family engine that takes ``processors`` as a constructor argument), so
    the flag only ever reports a REAL miss.
    """
    return bool(getattr(engine, SCORE_PRESERVATION_ATTR, True))


@dataclass(frozen=True)
class StrategySpec:
    """Describes how to build a toolkit query engine for a named strategy.

    Attributes:
        name: The :class:`RetrieverStrategy` this spec implements.
        engine_family: How the engine is constructed —
            ``"traversal"`` (``for_traversal_based_search``) for the seven
            traversal retrievers, or ``"direct"`` (the bare
            ``LexicalGraphQueryEngine(..., retriever=...)`` constructor) for
            TopicBeamSearch.
        build_engine: Callable ``(graph_store, vector_store, *, tenant_id)``
            that returns a configured ``LexicalGraphQueryEngine``. All
            graphrag_toolkit imports happen inside this callable to keep them
            lazy (preserving the botocore sync-only invariant).
    """

    name: RetrieverStrategy
    engine_family: Literal["traversal", "direct"]
    build_engine: Callable[..., object]


# Benchmarked retrieval hyperparameters (ProcessorArgs), pinned EXPLICITLY so
# retrieval matches the toolkit team's benchmark config (reranker=tfidf,
# max_statements=200, derive_subqueries=False) and a graphrag-toolkit bump cannot
# silently move a default. Generation-time knobs (max_context_tokens) are not set:
# we build with ``post_processors=[]`` and do our own synthesis, so they are inert.
#
# Deviations from the installed 3.20.0 defaults: ``max_search_results`` (5 → 10,
# per the benchmark config) and ``vss_top_k`` (10 → 20, a deliberate recall-widening
# override that surfaces more seed chunks to the beam / statement expansion).
_BENCHMARK_PROCESSOR_ARGS: dict = {
    "reranker": "tfidf",
    "vss_top_k": 20,
    "max_search_results": 10,
    "max_statements": 200,
    "derive_subqueries": False,
    "include_chunk_details": True,
}


def _score_preserving_processors() -> list[type]:
    """Return the pinned toolkit processor chain with exact rank-score capture.

    The toolkit computes ``SearchResult.score`` before truncating each topic to
    ``max_statements_per_topic``, then ``ClearScores`` erases that exact ranking
    value. Reconstructing from the surviving statements is not equivalent. This
    replacement stores the already-computed score on raw chunk metadata before
    clearing it; the adapter reads the marker from the unformatted result copy.
    """
    from graphrag_toolkit.lexical_graph.retrieval.processors import ClearScores
    from graphrag_toolkit.lexical_graph.retrieval.retrievers.traversal_based_base_retriever import (
        DEFAULT_PROCESSORS,
    )

    class PreserveAndClearScores(ClearScores):
        def _process_results(self, search_results: Any, query: Any) -> Any:
            for search_result in search_results.results:
                score = search_result.score
                stored = False
                if score is not None:
                    for topic in search_result.topics:
                        for chunk in topic.chunks:
                            chunk.metadata = {
                                **chunk.metadata,
                                _PRESERVED_RESULT_SCORE_METADATA_KEY: score,
                            }
                            stored = True
                # A result without chunks has nowhere safe to store the marker;
                # retain its NodeWithScore value rather than losing the score.
                if stored:
                    search_result.score = None
            return search_results

    return [PreserveAndClearScores if processor is ClearScores else processor for processor in DEFAULT_PROCESSORS]


def _install_score_preserving_processors(engine: Any) -> Any:
    """Inject score preservation into the factory's per-query retriever.

    Depends on two private graphrag-toolkit attributes. On a miss the engine still
    works, but every relevance score comes back 0.0, so the outcome is recorded on
    the engine (:data:`SCORE_PRESERVATION_ATTR`) and warned about here rather than
    returning quietly — a silently unranked result set looks exactly like a
    genuinely irrelevant one.
    """
    query_mode_retriever = getattr(engine, "retriever", None)
    original_retriever_fn = getattr(query_mode_retriever, "retriever_fn", None)
    if not callable(original_retriever_fn):
        logger.warning(
            "lexical_score_preservation_not_installed",
            engine_type=type(engine).__name__,
            has_retriever=query_mode_retriever is not None,
            retriever_type=type(query_mode_retriever).__name__ if query_mode_retriever is not None else None,
            has_retriever_fn=original_retriever_fn is not None,
            consequence="lexical relevance scores will be 0.0",
        )
        with contextlib.suppress(AttributeError, TypeError):
            setattr(engine, SCORE_PRESERVATION_ATTR, False)
        return engine
    processors = _score_preserving_processors()

    def create_retriever(**kwargs: Any) -> Any:
        kwargs["processors"] = processors
        return original_retriever_fn(**kwargs)

    query_mode_retriever.retriever_fn = create_retriever
    with contextlib.suppress(AttributeError, TypeError):
        setattr(engine, SCORE_PRESERVATION_ATTR, True)
    return engine


# Engine builders. graphrag_toolkit imports are LAZY (inside each function); see
# the module docstring for ``post_processors=[]`` / ``context_format="raw"``.


def _build_chunk_based_engine(graph_store, vector_store, *, tenant_id):
    """``chunk_based``: single ``ChunkBasedSearch`` retriever."""
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import ChunkBasedSearch

    return _install_score_preserving_processors(
        LexicalGraphQueryEngine.for_traversal_based_search(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            retrievers=[ChunkBasedSearch],
            post_processors=[],
            context_format="raw",
            **_BENCHMARK_PROCESSOR_ARGS,
        )
    )


def _build_chunk_based_semantic_engine(graph_store, vector_store, *, tenant_id):
    """``chunk_based_semantic`` (default): single ``ChunkBasedSemanticSearch`` retriever.

    ``ChunkBasedSemanticSearch`` wraps ``ChunkCosineSimilaritySearch`` +
    ``SemanticChunkBeamGraphSearch`` + chunk->statement expansion.
    """
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import (
        ChunkBasedSemanticSearch,
    )

    return _install_score_preserving_processors(
        LexicalGraphQueryEngine.for_traversal_based_search(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            retrievers=[ChunkBasedSemanticSearch],
            post_processors=[],
            context_format="raw",
            **_BENCHMARK_PROCESSOR_ARGS,
        )
    )


def _build_entity_based_engine(graph_store, vector_store, *, tenant_id):
    """``entity_based``: single ``EntityBasedSearch`` retriever."""
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import EntityBasedSearch

    return _install_score_preserving_processors(
        LexicalGraphQueryEngine.for_traversal_based_search(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            retrievers=[EntityBasedSearch],
            post_processors=[],
            context_format="raw",
            **_BENCHMARK_PROCESSOR_ARGS,
        )
    )


def _build_entity_context_engine(graph_store, vector_store, *, tenant_id):
    """``entity_context``: single ``EntityContextSearch`` retriever."""
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import EntityContextSearch

    return _install_score_preserving_processors(
        LexicalGraphQueryEngine.for_traversal_based_search(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            retrievers=[EntityContextSearch],
            post_processors=[],
            context_format="raw",
            **_BENCHMARK_PROCESSOR_ARGS,
        )
    )


def _build_entity_network_engine(graph_store, vector_store, *, tenant_id):
    """``entity_network``: single ``EntityNetworkSearch`` retriever."""
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import EntityNetworkSearch

    return _install_score_preserving_processors(
        LexicalGraphQueryEngine.for_traversal_based_search(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            retrievers=[EntityNetworkSearch],
            post_processors=[],
            context_format="raw",
            **_BENCHMARK_PROCESSOR_ARGS,
        )
    )


def _build_topic_based_engine(graph_store, vector_store, *, tenant_id):
    """``topic_based``: single ``TopicBasedSearch`` retriever."""
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBasedSearch

    return _install_score_preserving_processors(
        LexicalGraphQueryEngine.for_traversal_based_search(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            retrievers=[TopicBasedSearch],
            post_processors=[],
            context_format="raw",
            **_BENCHMARK_PROCESSOR_ARGS,
        )
    )


def _build_traversal_engine(graph_store, vector_store, *, tenant_id):
    """``traversal``: weighted traversal composite.

    ChunkBasedSearch @ 1.0 + EntityNetworkSearch @ 1.0 + TopicBasedSearch @ 0.5,
    combined via ``WeightedTraversalBasedRetriever``.
    """
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import (
        ChunkBasedSearch,
        EntityNetworkSearch,
        TopicBasedSearch,
    )
    from graphrag_toolkit.lexical_graph.retrieval.retrievers.composite_traversal_based_retriever import (
        WeightedTraversalBasedRetriever,
    )

    return _install_score_preserving_processors(
        LexicalGraphQueryEngine.for_traversal_based_search(
            graph_store,
            vector_store,
            tenant_id=tenant_id,
            retrievers=[
                WeightedTraversalBasedRetriever(retriever=ChunkBasedSearch, weight=1.0),
                WeightedTraversalBasedRetriever(retriever=EntityNetworkSearch, weight=1.0),
                WeightedTraversalBasedRetriever(retriever=TopicBasedSearch, weight=0.5),
            ],
            post_processors=[],
            context_format="raw",
            **_BENCHMARK_PROCESSOR_ARGS,
        )
    )


def _build_topic_beam_engine(graph_store, vector_store, *, tenant_id):
    """``topic_beam``: TopicGraphRAG via TopicBeamSearch (graphrag PR #318, introduced 3.18.7; pinned at 3.20.0).

    Constructed as the engine's retriever directly (``LexicalGraphQueryEngine(...,
    retriever=...)``), matching how the graphrag-toolkit benchmark builds it
    (``benchmarks/eval_newproc.py`` "topic-beam-rework"). TopicBeamSearch subclasses
    ``TraversalBasedBaseRetriever``, NOT ``SemanticGuidedRetriever``.

    Params are the graphrag-toolkit benchmark's best SEC-10-Q config, restricted to
    the kwargs 3.20.0's ``TopicBeamSearch.__init__`` actually accepts (extra kwargs
    would be silently swallowed by ``**kwargs``): beam_width=100, max_depth=6,
    top_k=50, scoring_mode="path_weighted", all three neighbour strategies on.

    REQUIRES a populated ``topic`` vector index (kg-build EMBEDDING_INDEXES must
    include ``topic``); against a graph built before that change it seeds empty.
    """
    from graphrag_toolkit.lexical_graph import LexicalGraphQueryEngine
    from graphrag_toolkit.lexical_graph.retrieval.processors import ProcessorArgs
    from graphrag_toolkit.lexical_graph.retrieval.retrievers import TopicBeamSearch
    from graphrag_toolkit.lexical_graph.storage.graph.multi_tenant_graph_store import MultiTenantGraphStore
    from graphrag_toolkit.lexical_graph.storage.vector.multi_tenant_vector_store import MultiTenantVectorStore
    from graphrag_toolkit.lexical_graph.storage.vector.read_only_vector_store import ReadOnlyVectorStore
    from graphrag_toolkit.lexical_graph.tenant_id import to_tenant_id

    # Wrap the stores OURSELVES, because we pass the retriever as an INSTANCE.
    # ``LexicalGraphQueryEngine.__init__`` wraps its stores (MultiTenant + ReadOnly) and
    # then only threads those wrapped stores into the retriever when
    # ``retriever`` is a CLASS it instantiates; an instance is taken as-is. So a retriever
    # built from the raw stores keeps them, which breaks two things at once:
    #   * no MultiTenant wrapper -> it queries the untenanted ``topic`` index instead of
    #     ``topic_<tenant>``, finds nothing, and seeds an empty beam; and
    #   * no ReadOnly wrapper -> ``writeable`` stays True, so the toolkit's ``index_exists``
    #     treats that miss as a cue to CREATE the index, which AOSS NEXTGEN rejects
    #     ("Field parameter 'engine' is not supported").
    # ``to_tenant_id`` first: ``wrap`` compares against the default-tenant sentinel and
    # needs a real TenantId, not the raw string the caller passes. Both wraps are
    # idempotent, so the engine's own wrapping below is then a no-op.
    resolved_tenant = to_tenant_id(tenant_id)
    graph_store = MultiTenantGraphStore.wrap(graph_store, resolved_tenant)
    vector_store = ReadOnlyVectorStore.wrap(MultiTenantVectorStore.wrap(vector_store, resolved_tenant))

    # The retriever owns its own caps, so it needs ProcessorArgs explicitly — unlike the
    # traversal factory, the bare engine constructor does not build them from **kwargs
    # for the retriever. Mirrors the benchmark's "native_compile" topic-beam config
    # (eval_newproc.py): source-first count caps, no token budget, and
    # max_statements_per_topic=9999 on the RETRIEVER so the beam is capped by the
    # processor (10/topic) rather than truncated twice.
    #
    # ``include_chunk_details`` is threaded through too: without it the toolkit's
    # statements query returns each chunk with ``value: NULL``, so no chunk is citable.
    processor_args = ProcessorArgs(
        reranker=_BENCHMARK_PROCESSOR_ARGS["reranker"],
        max_search_results=_BENCHMARK_PROCESSOR_ARGS["max_search_results"],
        include_chunk_details=_BENCHMARK_PROCESSOR_ARGS["include_chunk_details"],
        max_statements_per_topic=10,
    )

    topic_retriever = TopicBeamSearch(
        graph_store=graph_store,
        vector_store=vector_store,
        processor_args=processor_args,
        processors=_score_preserving_processors(),
        beam_width=100,
        max_depth=6,
        top_k=50,
        scoring_mode="path_weighted",
        # All three neighbour strategies ON: same-chunk alone starves the beam on our
        # graph. max_entity_neighbors / max_chunk_neighbors (default 100 each) bound the fan-out.
        use_entity_neighbors=True,
        use_same_chunk_neighbors=True,
        use_adjacent_chunk_neighbors=True,
        # Cap once, in the processor above — not twice.
        max_statements_per_topic=9999,
    )
    return LexicalGraphQueryEngine(
        graph_store,
        vector_store,
        tenant_id=tenant_id,
        retriever=topic_retriever,
        post_processors=[],
        context_format="raw",
        streaming=False,
    )


STRATEGY_REGISTRY: dict[RetrieverStrategy, StrategySpec] = {
    RetrieverStrategy.CHUNK_BASED: StrategySpec(
        name=RetrieverStrategy.CHUNK_BASED,
        engine_family="traversal",
        build_engine=_build_chunk_based_engine,
    ),
    RetrieverStrategy.CHUNK_BASED_SEMANTIC: StrategySpec(
        name=RetrieverStrategy.CHUNK_BASED_SEMANTIC,
        engine_family="traversal",
        build_engine=_build_chunk_based_semantic_engine,
    ),
    RetrieverStrategy.ENTITY_BASED: StrategySpec(
        name=RetrieverStrategy.ENTITY_BASED,
        engine_family="traversal",
        build_engine=_build_entity_based_engine,
    ),
    RetrieverStrategy.ENTITY_CONTEXT: StrategySpec(
        name=RetrieverStrategy.ENTITY_CONTEXT,
        engine_family="traversal",
        build_engine=_build_entity_context_engine,
    ),
    RetrieverStrategy.ENTITY_NETWORK: StrategySpec(
        name=RetrieverStrategy.ENTITY_NETWORK,
        engine_family="traversal",
        build_engine=_build_entity_network_engine,
    ),
    RetrieverStrategy.TOPIC_BASED: StrategySpec(
        name=RetrieverStrategy.TOPIC_BASED,
        engine_family="traversal",
        build_engine=_build_topic_based_engine,
    ),
    RetrieverStrategy.TRAVERSAL: StrategySpec(
        name=RetrieverStrategy.TRAVERSAL,
        engine_family="traversal",
        build_engine=_build_traversal_engine,
    ),
    RetrieverStrategy.TOPIC_BEAM: StrategySpec(
        name=RetrieverStrategy.TOPIC_BEAM,
        engine_family="direct",
        build_engine=_build_topic_beam_engine,
    ),
}

DEFAULT_SPEC: StrategySpec = STRATEGY_REGISTRY[DEFAULT_STRATEGY]


def resolve_strategy(request_value: str | None, config_default: str | None) -> RetrieverStrategy | None:
    """Resolve the effective strategy with precedence request > deployment.

    The request value is normally validated at the model layer, so by the time
    it reaches here it is either a valid member or absent. Defensively, an
    unrecognized request value is treated as absent (falls through to the
    deployment default).

    ``config_default`` is the per-deployment default and may be ``None`` when
    the deployment does not force a graphrag strategy. In that case, with no
    request value, this returns ``None`` to signal "no graphrag strategy". A
    non-``None`` but *unrecognized* deployment default warns and falls back to
    :data:`DEFAULT_STRATEGY`.

    Args:
        request_value: The per-request ``options.retrieverStrategy`` value (or
            ``None`` when absent). When present, selecting graphrag retrieval
            on-demand regardless of the deployment default.
        config_default: The per-deployment default, or ``None`` when the
            deployment does not engage a graphrag strategy by default.

    Returns:
        The resolved :class:`RetrieverStrategy`, or ``None`` when neither a
        request value nor a deployment default selects one.
    """
    if request_value is not None:
        try:
            return RetrieverStrategy(request_value)
        except ValueError:
            logger.warning(
                "unrecognized_request_retriever_strategy",
                request_value=request_value,
                action="treating as absent; falling back to deployment default",
            )

    if config_default is not None:
        try:
            return RetrieverStrategy(config_default)
        except ValueError:
            logger.warning(
                "invalid_lexical_retriever_strategy",
                config_default=config_default,
                fallback=str(DEFAULT_STRATEGY),
            )
            return DEFAULT_STRATEGY

    return None
