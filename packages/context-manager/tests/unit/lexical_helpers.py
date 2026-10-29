# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures/helpers for the lexical-baseline co-located unit suites.

Single source of truth for the small builders that the lexical co-located unit
suites (tasks 1.a–8.a) and the cross-cutting coverage sweep (task 9) all reuse:

- :func:`make_strategy_spec` — a ``StrategySpec`` factory (defaults to the real
  registry default, overridable for bespoke wiring tests).
- :data:`NDB_ENDPOINT` / :data:`NA_ENDPOINT` (+ ``*_GRAPH_ID``) — representative
  Neptune Database / Neptune Analytics endpoint shapes.
- :func:`make_toolkit_node`, :func:`source_metadata`, :func:`entity_contexts` —
  build a fake toolkit ``NodeWithScore`` and the real nested toolkit metadata
  shapes (``Source`` / ``EntityContexts``).
- :func:`make_synthesizer`, :func:`make_baseline_result`,
  :func:`make_baseline_retriever` — Tier-3 wiring helpers.

These are plain functions (not pytest fixtures) so they can be called inline
inside other helpers and list comprehensions. ``tests/unit/conftest.py`` exposes
thin fixture wrappers (``strategy_spec_factory``, ``toolkit_node_factory``,
``ndb_endpoint``, ``na_endpoint``) for ergonomic injection where preferred.

NOTE on the lazy-import invariant: the *strategies* module and ``load_config``
must not import ``graphrag_toolkit`` (verified by their own isolated tests).
This helpers module imports from the ``coa_serve.lexical`` package,
whose ``__init__`` eagerly imports the adapter (and thus graphrag) — the
existing, intended behavior — so these helpers are not themselves subject to the
sync-only import invariant. The ``build_engine`` callables are never invoked
here.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

from coa_serve.lexical.strategies import (
    DEFAULT_SPEC,
    STRATEGY_REGISTRY,
    RetrieverStrategy,
    StrategySpec,
)
from coa_serve.tier3.graph_traverser import GraphEntity
from coa_serve.tier3.synthesizer import MAX_PROMPT_CHUNKS
from coa_serve.tier3.vector_retriever import ChunkResult

# ── Endpoint fixtures (NDB / NA) ────────────────────────────────────

#: A representative Neptune Database (NDB) cluster endpoint.
NDB_ENDPOINT = "test-cluster.us-east-1.neptune.amazonaws.com"
#: The graph-store URI the factory should derive for :data:`NDB_ENDPOINT`.
NDB_GRAPH_STORE_URI = "neptune-db://test-cluster.us-east-1.neptune.amazonaws.com:8182"

#: A representative Neptune Analytics (NA) graph id.
NA_GRAPH_ID = "g-abc1234567"
#: A representative Neptune Analytics (NA) host endpoint embedding the graph id.
NA_ENDPOINT = "g-abc1234567.us-east-1.neptune-graph.amazonaws.com"
#: The graph-store URI the factory should derive for an NA endpoint/graph-id.
NA_GRAPH_STORE_URI = "neptune-graph://g-abc1234567"

#: A representative OpenSearch Serverless (AOSS) collection endpoint.
AOSS_ENDPOINT = "https://test.aoss.amazonaws.com"
#: The vector-store URI derived from :data:`AOSS_ENDPOINT`.
AOSS_VECTOR_STORE_URI = "aoss://https://test.aoss.amazonaws.com"


# ── StrategySpec factory ────────────────────────────────────────────


def make_strategy_spec(
    name: RetrieverStrategy = RetrieverStrategy.CHUNK_BASED_SEMANTIC,
    *,
    engine_family: str | None = None,
    build_engine: Callable[..., object] | None = None,
) -> StrategySpec:
    """Build a ``StrategySpec``.

    With no overrides this returns the real registry spec for ``name`` (so tests
    exercise the shipping spec). Supplying ``engine_family``/``build_engine``
    yields a bespoke frozen spec — useful for asserting wiring without invoking
    the toolkit (the supplied ``build_engine`` can be a stub/spy).
    """
    if engine_family is None and build_engine is None:
        return STRATEGY_REGISTRY[name]
    base = STRATEGY_REGISTRY[name]
    return StrategySpec(
        name=name,
        engine_family=engine_family or base.engine_family,  # type: ignore[arg-type]
        build_engine=build_engine or base.build_engine,
    )


# ── Fake toolkit node + metadata shapes ─────────────────────────────


class _FakeNode:
    """A minimal stand-in for a toolkit ``NodeWithScore``.

    A plain object (not ``MagicMock``) so ``hasattr``/``getattr`` access in
    ``_map_results`` behaves like the real node rather than auto-creating
    attributes.
    """

    __slots__ = ("node_id", "text", "score", "metadata")


def make_toolkit_node(
    node_id: str = "n1",
    text: str = "some text",
    score: float | None = 0.5,
    metadata: dict | None = None,
) -> _FakeNode:
    """Build a fake toolkit node carrying ``metadata`` for ``_map_results``."""
    node = _FakeNode()
    node.node_id = node_id
    node.text = text
    node.score = score
    node.metadata = {} if metadata is None else metadata
    return node


def source_metadata(source_id: str = "doc-123", extra_metadata: dict | None = None) -> dict:
    """The real toolkit ``Source`` shape under ``metadata["source"]``.

    Mirrors ``graphrag_toolkit...retrieval.model.Source`` —
    ``{"sourceId": ..., "metadata": {...}}``.
    """
    return {"sourceId": source_id, "metadata": extra_metadata or {}}


def entity_contexts(entities: list[tuple[str, str, str]]) -> dict:
    """The real toolkit ``EntityContexts`` shape under ``metadata["entity_contexts"]``.

    ``entities`` is a list of ``(entityId, value, classification)`` tuples,
    nested as ``contexts[].entities[].entity`` to match
    ``EntityContexts.contexts[].entities[].entity`` (``ScoredEntity.entity``).
    """
    return {
        "contexts": [
            {
                "entities": [
                    {
                        "entity": {
                            "entityId": eid,
                            "value": value,
                            "classification": classification,
                        },
                        "score": 0.9,
                    }
                    for (eid, value, classification) in entities
                ]
            }
        ],
        "keywords": [],
    }


# ── Tier-3 wiring helpers ───────────────────────────────────────────


def make_synthesizer(
    answer: str = "synthesized.",
    confidence: float = 0.77,
    duration_ms: int = 42,
    guardrail_blocked: bool = False,
) -> AsyncMock:
    """An ``AsyncMock`` synthesizer returning a fixed synthesis result.

    Like the real ``Synthesizer`` (no screener configured), it reports the first
    ``MAX_PROMPT_CHUNKS`` input chunks as ``prompt_chunks``, the chunks the
    retriever then cites, and passes the graph entities through unchanged.
    """
    synth = AsyncMock()

    async def _synthesize(query, chunks, graph_entities, **kwargs):
        return MagicMock(
            answer=answer,
            confidence=confidence,
            duration_ms=duration_ms,
            guardrail_blocked=guardrail_blocked,
            prompt_chunks=tuple(chunks[:MAX_PROMPT_CHUNKS]),
            graph_entities=tuple(graph_entities),
        )

    synth.synthesize.side_effect = _synthesize
    return synth


def make_baseline_result(chunks=None, entities=None, trace_steps=None):
    """Build a populated ``BaselineResult`` (imported lazily to keep this module
    free of any eager adapter side effects at import time)."""
    from coa_serve.lexical.baseline_retriever import BaselineResult

    return BaselineResult(
        chunks=tuple(
            chunks
            if chunks is not None
            else [
                ChunkResult(
                    chunk_id="c1",
                    text="Apple revenue was $83 billion",
                    source_doc="2022 Q3 AAPL.pdf",
                    relevance_score=0.9,
                )
            ]
        ),
        entities=tuple(
            entities
            if entities is not None
            else [GraphEntity(uri="Apple Inc.", label="Apple Inc.", type="lexical_entity")]
        ),
        trace_steps=tuple(
            trace_steps
            if trace_steps is not None
            else (
                {
                    "step": "lexical_baseline_retrieve",
                    "status": "success",
                    "durationMs": 100,
                    "detail": "1 nodes retrieved",
                },
            )
        ),
    )


def make_baseline_retriever(
    *,
    graph_store_uri: str = NA_GRAPH_STORE_URI,
    vector_store_uri: str = AOSS_VECTOR_STORE_URI,
    strategy: StrategySpec = DEFAULT_SPEC,
    timeout_s: float = 15.0,
):
    """Construct a ``BaselineLexicalRetriever`` with sensible NA defaults."""
    from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever

    return BaselineLexicalRetriever(
        graph_store_uri=graph_store_uri,
        vector_store_uri=vector_store_uri,
        strategy=strategy,
        timeout_s=timeout_s,
    )
