# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 orchestrator -- parallel retrieval + single-shot LLM synthesis.

Runs four retrieval sources in parallel (partial failure tolerant):
1. VKG Structured Context -- exploratory SPARQL from entity URIs or failed
   Tier 2 SPARQL hint, translated via VKG for partial structured data.
2. Document Chunks -- k-NN search against chunk_{namespace_short_id} index.
3. Neptune Graph Traversal -- 1-2 hop traversal from entity URIs.
4. Catalog Summary -- always included; from MetricResolver.list_all().

After retrieval, assembles all context and makes one LLM Converse call.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

if TYPE_CHECKING:
    from ..lexical.baseline_retriever import BaselineLexicalRetriever
    from ..lexical.strategies import RetrieverStrategy

from ..step_ids import StepId
from ..trace import TraceCollector, client_error_detail
from .graph_traverser import GraphTraverser
from .synthesizer import MAX_PROMPT_CHUNKS, SynthesisResult, Synthesizer
from .vector_retriever import VectorRetriever

logger = structlog.get_logger(__name__)

T = TypeVar("T")

_MAX_RELATIONSHIPS_PER_ENTITY = 10

# Per-source retrieval timeouts (configurable via environment).
# TIER3_PER_SOURCE_TIMEOUT_S is a single-knob default for both sources; the
# per-source vars override it. Malformed values fall back rather than crashing
# service startup.


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


_PER_SOURCE_TIMEOUT_S = _float_env("TIER3_PER_SOURCE_TIMEOUT_S", 10.0)
VECTOR_SEARCH_TIMEOUT_S = _float_env("TIER3_VECTOR_TIMEOUT", _PER_SOURCE_TIMEOUT_S)
GRAPH_TRAVERSE_TIMEOUT_S = _float_env("TIER3_GRAPH_TIMEOUT", _PER_SOURCE_TIMEOUT_S)

# Callback type aliases
OnTokenCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Tier3Result:
    """Result of Tier 3 retrieval + synthesis: the answer plus supporting context."""

    synthesized_answer: str
    supporting_content: tuple[dict, ...]
    graph_context: tuple[dict, ...]
    confidence: float
    supporting_content_truncated: bool = False
    guardrail_blocked: bool = False
    trace_steps: tuple[dict, ...] = ()
    degraded_sources: tuple[dict, ...] = ()
    # Best-effort/partial signal. The hand-rolled and lexical-baseline paths
    # leave this at the default (False) and derive partiality from
    # ``degraded_sources`` at assembly time; the agentic path sets it on
    # time-budget-expiry / all-tools-fail / no-context paths (Req 7.5-7.8, 9.6-9.7).
    partial: bool = False


class KnowledgeRetriever:
    """Tier 3 orchestrator -- parallel retrieval + LLM synthesis."""

    def __init__(
        self,
        vector_retriever: VectorRetriever | None,
        graph_traverser: GraphTraverser | None,
        synthesizer: Synthesizer,
        lexical_retriever: BaselineLexicalRetriever | None = None,
    ):
        """Wire the retrieval sources and synthesizer for Tier 3.

        Args:
            vector_retriever: Optional k-NN chunk retriever.
            graph_traverser: Optional Neptune graph traverser.
            synthesizer: LLM synthesizer that composes the final answer.
            lexical_retriever: Optional graphrag lexical baseline retriever.
        """
        self._vector = vector_retriever
        self._graph = graph_traverser
        self._synthesizer = synthesizer
        self._lexical = lexical_retriever

    @property
    def vector_retriever(self) -> VectorRetriever | None:
        """Public access to vector retriever for T-Box context assembly."""
        return self._vector

    @property
    def lexical_enabled(self) -> bool:
        """True when a lexical baseline retriever is configured.

        Lets the orchestrator decide whether to resolve a retriever strategy
        without importing the strategy registry (which eagerly pulls in
        graphrag_toolkit) on the default hand-rolled path.
        """
        return self._lexical is not None

    async def resolve(
        self,
        query: str,
        namespace: str,
        *,
        embedding: list[float] | None = None,
        entity_uris: list[str] | None = None,
        tier2_sparql_hint: str | None = None,
        catalog_summary: list[dict[str, Any]] | None = None,
        structured_data: list[dict[str, Any]] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        on_token: OnTokenCallback | None = None,
        trace: TraceCollector | None = None,
        retriever_strategy: RetrieverStrategy | None = None,
        model_id: str | None = None,
    ) -> Tier3Result:
        """Run parallel retrieval and synthesize a response.

        Args:
            query: Natural language query.
            namespace: Namespace scope.
            embedding: Pre-computed query embedding from routing (avoids re-embedding).
            entity_uris: Entity URIs from routing vector hits for graph traversal.
            tier2_sparql_hint: Failed Tier 2 SPARQL for VKG context retrieval.
            catalog_summary: Metric definitions from MetricResolver.list_all().
            structured_data: Any partial structured data from upstream.
            conversation_history: Prior conversation turns.
            on_token: Callback for streaming tokens during synthesis.
            trace: TraceCollector for progressive step emission.
            retriever_strategy: Resolved lexical retriever strategy for this
                request. Used only when a lexical retriever is configured;
                ignored entirely on the hand-rolled path (3.1).
            model_id: Optional per-call LLM model override for synthesis.

        Returns:
            Tier3Result with synthesized answer, supporting content, and trace.
        """
        if trace is None:
            trace = TraceCollector()
        trace_steps: list[dict] = []

        # Delegate to the graphrag lexical retriever only when a strategy was
        # resolved for this request (a per-request options.retrieverStrategy, or
        # a deployment default under TIER3_STRATEGY=lexical-baseline). When no
        # strategy is resolved (the default hand-rolled path, or lexical-baseline
        # absent a request value with no deployment default), fall through to the
        # parallel vector + graph retrieval below. The retriever is built on every
        # deployment, so the strategy — not its mere presence — is the trigger.
        if self._lexical is not None and retriever_strategy is not None:
            return await self._resolve_via_lexical(
                query,
                namespace,
                trace_steps,
                structured_data,
                catalog_summary,
                conversation_history,
                trace=trace,
                retriever_strategy=retriever_strategy,
                model_id=model_id,
            )

        # Build parallel retrieval coroutines
        coros: list[tuple[str, Coroutine]] = []

        # Graph traversal (skip if no graph traverser)
        if self._graph is not None:
            if entity_uris:
                graph_coro = self._graph.traverse_from_uris(entity_uris, namespace)
            else:
                graph_coro = self._graph.traverse(query, namespace)
            coros.append(("graph_traverse", graph_coro))

        # Vector search requires an embedding and a vector retriever; skip if unavailable
        if embedding is not None and self._vector is not None:
            coros.append(("vector_search", self._vector.search(embedding, namespace)))

        # Execute all retrieval in parallel with partial failure tolerance and per-source timeouts
        results: dict[str, tuple[Any, int]] = {}
        degraded_sources: list[dict] = []
        timeout_map = {"vector_search": VECTOR_SEARCH_TIMEOUT_S, "graph_traverse": GRAPH_TRAVERSE_TIMEOUT_S}
        timed_coros = [(name, self._timed_safe_call(coro, timeout_s=timeout_map.get(name))) for name, coro in coros]

        # Wall-clock timer wraps the gather so parallel retrieval shows true elapsed time
        gather_start = time.perf_counter()
        gathered = await asyncio.gather(*(tc[1] for tc in timed_coros))
        gather_wall_ms = int((time.perf_counter() - gather_start) * 1000)

        for i, (name, _) in enumerate(timed_coros):
            results[name] = gathered[i]

        # Extract results with fallback for failures
        vec_chunks = []
        if "vector_search" in results:
            vec_result, vec_ms = results["vector_search"]
            vec_chunks, vec_trace = self._classify_result(
                vec_result, vec_ms, StepId.T3_VECTOR_SEARCH, VECTOR_SEARCH_TIMEOUT_S, degraded_sources
            )
            trace_steps.append(vec_trace)
            trace.record(
                vec_trace["step"],
                vec_trace["status"],
                vec_trace.get("durationMs", 0),
                detail=vec_trace.get("detail"),
                tool_used="opensearch",
                parallel_group=StepId.T3_RETRIEVE,
            )
        else:
            detail = "no_embedding" if self._vector is not None else "no_vector_retriever"
            trace_steps.append(
                {
                    "step": StepId.T3_VECTOR_SEARCH,
                    "status": "skipped",
                    "durationMs": 0,
                    "detail": detail,
                }
            )
            trace.record(StepId.T3_VECTOR_SEARCH, "skipped", 0, detail=detail, parallel_group=StepId.T3_RETRIEVE)

        graph_entities = []
        if "graph_traverse" in results:
            graph_result, graph_ms = results["graph_traverse"]
            graph_entities, graph_trace = self._classify_result(
                graph_result, graph_ms, StepId.T3_GRAPH_TRAVERSE, GRAPH_TRAVERSE_TIMEOUT_S, degraded_sources
            )
            graph_trace["graphSeedMode"] = "uri_hop" if entity_uris else "keyword"
            trace_steps.append(graph_trace)
            trace.record(
                graph_trace["step"],
                graph_trace["status"],
                graph_trace.get("durationMs", 0),
                detail=graph_trace.get("detail"),
                tool_used="neptune",
                parallel_group=StepId.T3_RETRIEVE,
                graph_seed_mode=graph_trace["graphSeedMode"],
            )
        else:
            trace_steps.append(
                {
                    "step": StepId.T3_GRAPH_TRAVERSE,
                    "status": "skipped",
                    "durationMs": 0,
                    "detail": "no_graph_traverser",
                }
            )
            trace.record(
                StepId.T3_GRAPH_TRAVERSE,
                "skipped",
                0,
                detail="no_graph_traverser",
                parallel_group=StepId.T3_RETRIEVE,
            )

        # Group wrapper carries wall-clock time for the parallel gather
        group_status = "degraded" if degraded_sources else ("skipped" if not coros else "success")
        trace.record(
            StepId.T3_RETRIEVE,
            group_status,
            0,
            wall_ms=gather_wall_ms,
            detail={"sources": [name for name, _ in timed_coros]},
        )
        logger.info(
            "t3_retrieve_done",
            wall_ms=gather_wall_ms,
            vector_status=("success" if "vector_search" in results else "skipped"),
            graph_status=("success" if "graph_traverse" in results else "skipped"),
        )

        # Catalog context trace. catalog_summary is pre-computed upstream and
        # passed in, so there is no retrieval work to time here — record 0.
        if catalog_summary is not None:
            trace_steps.append(
                {
                    "step": StepId.T3_CATALOG_CONTEXT,
                    "status": "success",
                    "durationMs": 0,
                    "detail": f"{len(catalog_summary)} metrics",
                }
            )
            trace.record(StepId.T3_CATALOG_CONTEXT, "success", 0, detail=f"{len(catalog_summary)} metrics")

        # Synthesize (stream when on_token callback is provided)
        try:
            if on_token and hasattr(self._synthesizer, "synthesize_stream"):
                synthesis = await self._synthesizer.synthesize_stream(
                    query,
                    vec_chunks,
                    graph_entities,
                    structured_data=structured_data,
                    catalog_summary=catalog_summary,
                    conversation_history=conversation_history,
                    on_token=on_token,
                    model_id=model_id,
                )
            else:
                synthesis = await self._synthesizer.synthesize(
                    query,
                    vec_chunks,
                    graph_entities,
                    structured_data=structured_data,
                    catalog_summary=catalog_summary,
                    conversation_history=conversation_history,
                    model_id=model_id,
                )
            trace_steps.append(
                {
                    "step": StepId.T3_SYNTHESIZE,
                    "status": "success",
                    "durationMs": synthesis.duration_ms,
                }
            )
            trace.record(StepId.T3_SYNTHESIZE, "success", synthesis.duration_ms, tool_used="bedrock")
            self._record_guardrail_step(synthesis, trace_steps, trace)
        except Exception as exc:
            logger.error("synthesis_failure", error_type=type(exc).__name__, exc_info=True)
            error_detail = client_error_detail(exc)
            trace_steps.append(
                {
                    "step": StepId.T3_SYNTHESIZE,
                    "status": "error",
                    "detail": error_detail,
                }
            )
            trace.record(StepId.T3_SYNTHESIZE, "error", 0, detail=error_detail, tool_used="bedrock")
            raise

        grounded_chunks = vec_chunks[:MAX_PROMPT_CHUNKS]

        return Tier3Result(
            synthesized_answer=synthesis.answer,
            supporting_content=tuple(
                {
                    "chunkId": c.chunk_id,
                    "text": c.text,
                    "sourceDoc": c.source_doc,
                    "label": c.label,
                    "relevanceScore": c.relevance_score,
                }
                for c in grounded_chunks
            ),
            graph_context=tuple(
                {
                    "uri": e.uri,
                    "label": e.label,
                    "type": e.type,
                    "relationships": e.relationships[:_MAX_RELATIONSHIPS_PER_ENTITY],
                }
                for e in graph_entities
            ),
            confidence=synthesis.confidence,
            supporting_content_truncated=len(vec_chunks) > MAX_PROMPT_CHUNKS,
            guardrail_blocked=synthesis.guardrail_blocked,
            trace_steps=tuple(trace_steps),
            degraded_sources=tuple(degraded_sources),
        )

    async def _resolve_via_lexical(
        self,
        query: str,
        namespace: str,
        trace_steps: list[dict],
        structured_data: list[dict[str, Any]] | None,
        catalog_summary: list[dict[str, Any]] | None,
        conversation_history: list[dict[str, str]] | None,
        *,
        trace: TraceCollector | None = None,
        retriever_strategy: RetrieverStrategy | None = None,
        model_id: str | None = None,
    ) -> Tier3Result:
        """Delegate retrieval to the graphrag-toolkit baseline retriever.

        The retriever already degrades to an empty ``BaselineResult`` with an
        error trace step on timeout / internal error (task 6). An additional
        defensive ``try/except`` here guarantees that an unexpected raise out of
        ``retrieve`` still records an error trace step and proceeds with empties,
        so a lexical failure never fails the whole Tier 3 resolve (2.9, 2.18).
        """
        if trace is None:
            trace = TraceCollector()
        try:
            spec = None
            if retriever_strategy is not None:
                # Map the resolved RetrieverStrategy enum to its StrategySpec for
                # the adapter (which builds/caches engines from the spec). Import
                # lazily so the hand-rolled path never pulls in the registry.
                from ..lexical.strategies import STRATEGY_REGISTRY

                spec = STRATEGY_REGISTRY[retriever_strategy]
            baseline_result = await self._lexical.retrieve(query, namespace, strategy=spec)
            trace_steps.extend(baseline_result.trace_steps)
            for s in baseline_result.trace_steps:
                trace.record(
                    s.get("step", "unknown"),
                    s.get("status", "unknown"),
                    s.get("durationMs", 0),
                    detail=s.get("detail"),
                )
            vec_chunks = list(baseline_result.chunks)
            graph_entities = list(baseline_result.entities)
        except Exception as exc:
            logger.error("lexical_retrieval_unhandled_error", error_type=type(exc).__name__, exc_info=True)
            detail = "retrieval_error"
            if retriever_strategy is not None:
                detail = f"strategy={retriever_strategy}; retrieval_error"
            trace_steps.append(
                {
                    "step": StepId.LEXICAL_BASELINE_RETRIEVE,
                    "status": "error",
                    "durationMs": 0,
                    "detail": detail,
                }
            )
            trace.record(StepId.LEXICAL_BASELINE_RETRIEVE, "error", 0, detail=detail)
            vec_chunks = []
            graph_entities = []

        if catalog_summary is not None:
            trace_steps.append(
                {
                    "step": StepId.T3_CATALOG_CONTEXT,
                    "status": "success",
                    "durationMs": 0,
                    "detail": f"{len(catalog_summary)} metrics",
                }
            )
            trace.record(StepId.T3_CATALOG_CONTEXT, "success", 0, detail=f"{len(catalog_summary)} metrics")

        try:
            synthesis = await self._synthesizer.synthesize(
                query,
                vec_chunks,
                graph_entities,
                structured_data=structured_data,
                catalog_summary=catalog_summary,
                conversation_history=conversation_history,
                model_id=model_id,
            )
            trace_steps.append(
                {
                    "step": StepId.T3_SYNTHESIZE,
                    "status": "success",
                    "durationMs": synthesis.duration_ms,
                }
            )
            trace.record(StepId.T3_SYNTHESIZE, "success", synthesis.duration_ms, tool_used="bedrock")
            self._record_guardrail_step(synthesis, trace_steps, trace)
        except Exception as exc:
            logger.error("synthesis_failure", error_type=type(exc).__name__, exc_info=True)
            error_detail = client_error_detail(exc)
            trace_steps.append(
                {
                    "step": StepId.T3_SYNTHESIZE,
                    "status": "error",
                    "detail": error_detail,
                }
            )
            trace.record(StepId.T3_SYNTHESIZE, "error", 0, detail=error_detail, tool_used="bedrock")
            raise

        grounded_chunks = vec_chunks[:MAX_PROMPT_CHUNKS]
        return Tier3Result(
            synthesized_answer=synthesis.answer,
            supporting_content=tuple(
                {
                    "chunkId": c.chunk_id,
                    "text": c.text,
                    "sourceDoc": c.source_doc,
                    "label": c.label,
                    "relevanceScore": c.relevance_score,
                }
                for c in grounded_chunks
            ),
            graph_context=tuple(
                {
                    "uri": e.uri,
                    "label": e.label,
                    "type": e.type,
                    "relationships": e.relationships[:_MAX_RELATIONSHIPS_PER_ENTITY],
                }
                for e in graph_entities
            ),
            confidence=synthesis.confidence,
            supporting_content_truncated=len(vec_chunks) > MAX_PROMPT_CHUNKS,
            guardrail_blocked=synthesis.guardrail_blocked,
            trace_steps=tuple(trace_steps),
            degraded_sources=(),
        )

    def _record_guardrail_step(
        self, synthesis: SynthesisResult, trace_steps: list[dict], trace: TraceCollector
    ) -> None:
        """Emit a positive guardrail step so the UI can distinguish "ran and passed" from "did not run".

        Cedar/Firewall already do this via their own trace steps. Only recorded
        when the synthesizer is configured with a guardrail (otherwise the
        guardrail didn't run and the absence of this step correctly renders as
        n/a).
        """
        if not self._synthesizer.has_guardrail:
            return
        guardrail_status = "denied" if synthesis.guardrail_blocked else "success"
        trace_steps.append(
            {
                "step": StepId.T3_GUARDRAIL,
                "status": guardrail_status,
                "durationMs": 0,
            }
        )
        trace.record(StepId.T3_GUARDRAIL, guardrail_status, 0, tool_used="bedrock-guardrail")

    @staticmethod
    def _classify_result(
        result: Any, duration_ms: int, source_name: str, timeout_s: float, degraded_sources: list[dict]
    ) -> tuple[list, dict]:
        """Classify a retrieval result as success/timeout/error and build trace entry.

        Returns (data_list, trace_dict). Mutates degraded_sources if degraded.
        """
        is_timeout = isinstance(result, TimeoutError)
        is_error = isinstance(result, Exception) and not is_timeout
        data = result if not isinstance(result, Exception) else []

        if is_timeout:
            status = "timeout"
            detail = f"Exceeded {timeout_s}s timeout"
            degraded_sources.append({"source": source_name, "reason": "timeout", "detail": detail})
        elif is_error:
            status = "error"
            detail = "retrieval_error"
            # ``degradedSources`` reaches the client, so carry the exception type,
            # never its message (backend text, hostnames). ``_safe_call`` logged it.
            degraded_sources.append({"source": source_name, "reason": "error", "detail": type(result).__name__})
        else:
            status = "success"
            detail = f"{len(data)} items"

        trace = {"step": source_name, "status": status, "durationMs": duration_ms, "detail": detail}
        return data, trace

    @staticmethod
    async def _safe_call(coro: Coroutine[Any, Any, T]) -> T | Exception:
        try:
            return await coro
        except Exception as e:
            logger.warning("retrieval_partial_failure", error_type=type(e).__name__, error_msg=str(e))
            return e

    @classmethod
    async def _timed_safe_call(
        cls, coro: Coroutine[Any, Any, T], *, timeout_s: float | None = None
    ) -> tuple[T | Exception, int]:
        """Execute a coroutine with timing and optional per-source timeout.

        Returns (result_or_exception, duration_ms). On timeout, returns
        an asyncio.TimeoutError as the result (not raised).
        """
        start = time.perf_counter()
        if timeout_s is not None:
            try:
                result = await asyncio.wait_for(cls._safe_call(coro), timeout=timeout_s)
            except TimeoutError:
                logger.warning("retrieval_timeout", timeout_s=timeout_s)
                result = TimeoutError(f"Exceeded {timeout_s}s timeout")
        else:
            result = await cls._safe_call(coro)
        return result, int((time.perf_counter() - start) * 1000)
