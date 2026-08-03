# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontop (VKG) strategy — NL→SPARQL → VKG translation → SQL execution.

Trace completeness: VKG SPARQL→SQL translation failures (t2.vkg.compile) are
replayed on every path — success, first-attempt error, and retry error — via
_replay_vkg_steps, so the cause of any retry/fallback is always visible.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from coa_common import ontology_vector_index_name

from ...clients.base import VectorClient
from ...exceptions import AccessDeniedError
from ...step_ids import TOOL_FOR_STEP, StepId
from ...trace import TraceCollector
from ..strategy import StrategyContext, StrategyOption, StrategyResult, capped_max_rows
from .nl_to_sparql import NLtoSPARQL
from .types import VectorHit
from .vkg_translator import Tier2Result, VKGTranslator

logger = structlog.get_logger(__name__)

_TIER2_TOOL_USED = TOOL_FOR_STEP

# k-NN depth for the TBox-context retrieval feeding NL→SPARQL.
_VECTOR_TOP_K = 10

_RETRYABLE_VKG_PATTERNS = (
    "cannot translate",
    "unsupported sparql",
    "parse error",
    "subquery",
    "not supported",
    "translation error",
    "unsupported expression",
    "illegal aggregate",
)


def _is_retryable_vkg_error(error: str) -> bool:
    error_lower = error.lower()
    return any(pattern in error_lower for pattern in _RETRYABLE_VKG_PATTERNS)


class OntopStrategy:
    """StructuredQueryStrategy implementation for SPARQL→VKG→SQL resolution."""

    name: str = StrategyOption.ONTOP

    def __init__(
        self,
        nl_to_sparql: NLtoSPARQL,
        vkg_translator: VKGTranslator,
        vector_client: VectorClient | None = None,
        oss_ontology_index: str = "",
    ):
        """Wire the SPARQL translator, VKG translator, and vector retrieval index.

        Args:
            nl_to_sparql: Natural-language-to-SPARQL translator.
            vkg_translator: SPARQL-to-SQL VKG translator + executor.
            vector_client: Optional client for ontology-class vector hits.
            oss_ontology_index: OpenSearch ontology index for class retrieval.
        """
        self._nl_to_sparql = nl_to_sparql
        self._vkg_translator = vkg_translator
        self._vector_client = vector_client
        self._oss_ontology_index = oss_ontology_index

    async def resolve(self, query: str, namespace: str, context: StrategyContext) -> StrategyResult | None:
        """Translate the query to SPARQL, resolve it through the VKG, and execute.

        Args:
            query: The natural-language query to answer.
            namespace: Namespace the query targets.
            context: Shared strategy context (embedding, profile, options, trace).

        Returns:
            A StrategyResult on success, or None when the strategy should be
            skipped so the next strategy can try.
        """
        trace = context.trace
        profile = context.profile
        options = context.options
        embedding = context.embedding

        vector_hits = None
        if embedding:
            vector_hits = await self._fetch_vector_hits(embedding, namespace)

        sparql_result = await self._nl_to_sparql.translate(
            query, namespace, vector_hits=vector_hits, model_id=context.model_id
        )
        if sparql_result.trace_steps:
            annotated = [
                {**s, "toolUsed": s.get("toolUsed") or _TIER2_TOOL_USED.get(str(s.get("step", "")))}
                for s in sparql_result.trace_steps
            ]
            trace.add_dicts(annotated)

        if not (sparql_result.valid and sparql_result.sparql):
            return None

        tier2_result = await self._resolve_with_retry(
            query,
            namespace,
            profile,
            options,
            trace,
            sparql_result,
            vector_hits,
            model_id=context.model_id,
        )
        if tier2_result is None:
            return None

        # Record trace steps from VKG (pass detail as dict, not str repr — E4)
        self._replay_vkg_steps(tier2_result, trace)

        # Authorization deny is terminal
        if tier2_result.firewall_result and tier2_result.firewall_result.denied:
            logger.warning("tier2_firewall_denied", namespace=namespace, reason=tier2_result.firewall_result.reason)
            raise AccessDeniedError(tier2_result.firewall_result.reason)

        if tier2_result.query_result and not tier2_result.error:
            return StrategyResult(
                sql=tier2_result.vkg_result.sql if tier2_result.vkg_result else "",
                rows=tier2_result.query_result.rows,
                columns=tier2_result.query_result.columns,
                confidence=sparql_result.confidence,
                strategy_name=StrategyOption.ONTOP,
                trace_steps=[],
                ontop_assembly=True,
                sparql=sparql_result.sparql,
                row_count=tier2_result.query_result.row_count,
                truncated=tier2_result.query_result.truncated,
            )

        return None

    @staticmethod
    def _replay_vkg_steps(tier2_result: Tier2Result, trace: TraceCollector) -> None:
        """Replay a VKGTranslator result's trace steps into the outer trace.

        Used on success, error, and retry paths so the translator's own steps —
        including the authoritative t2.vkg.compile (SPARQL→SQL) step that records
        whether translation succeeded or failed — are always surfaced. Detail is
        passed through as a dict (not str repr) for the UI formatters (E4).
        """
        for ts in tier2_result.trace_steps:
            trace.record(
                ts.step,
                ts.status,
                ts.duration_ms,
                detail=ts.detail if ts.detail else None,
                tool_used=_TIER2_TOOL_USED.get(ts.step),
            )

    async def _resolve_with_retry(
        self,
        query: str,
        namespace: str,
        profile: dict,
        options: dict,
        trace: TraceCollector,
        sparql_result: Any,
        vector_hits: list | None,
        model_id: str | None = None,
    ) -> Any | None:
        """Run VKG translation with retry on retryable errors."""
        max_rows = capped_max_rows(options)
        data_source_id = options.get("dataSourceId", "default")

        vkg_start = time.perf_counter()
        try:
            tier2_result = await self._vkg_translator.resolve(
                sparql=sparql_result.sparql,
                namespace=namespace,
                data_source_id=data_source_id,
                profile=profile,
                max_rows=max_rows,
            )
        except Exception as e:
            vkg_ms = int((time.perf_counter() - vkg_start) * 1000)
            vkg_error = str(e)
            logger.warning("tier2_vkg_failed", error=vkg_error, error_type=type(e).__name__)
            trace.record(StepId.T2_VKG_COMPILE, "error", vkg_ms, detail={"error": vkg_error[:200]}, tool_used="ontop")
            if _is_retryable_vkg_error(vkg_error):
                return await self._retry_vkg(
                    query,
                    namespace,
                    profile,
                    data_source_id,
                    max_rows,
                    trace,
                    vector_hits,
                    # Intentional truncation: limit error context in LLM prompt to avoid
                    # consuming too much of the context window with stack traces.
                    hint=f"Your previous SPARQL failed VKG translation with error: {vkg_error[:300]}. "
                    "Rewrite using a simpler pattern. Avoid nested subqueries; prefer SUM(IF(...)) "
                    "for comparisons and flat aggregates (COUNT/SUM/AVG/MAX/MIN) for simple queries.",
                    model_id=model_id,
                    tbox_context=getattr(sparql_result, "tbox_context", None),
                )
            return None

        # Result-based retry: VKGTranslator swallows VKGTranslationError and
        # returns it as Tier2Result.error rather than raising. Replay the
        # translator's own trace steps (which include the authoritative
        # t2.vkg.compile failure step) so the retry isn't shown without its cause.
        if tier2_result.error and "vkg" in tier2_result.error.lower():
            logger.warning("tier2_vkg_result_error_retry", error=tier2_result.error, namespace=namespace)
            self._replay_vkg_steps(tier2_result, trace)
            return await self._retry_vkg(
                query,
                namespace,
                profile,
                data_source_id,
                max_rows,
                trace,
                vector_hits,
                hint="Your previous SPARQL failed VKG/Ontop translation. "
                "The VKG engine could not convert it to SQL. "
                "Rewrite using a simpler pattern. Avoid nested subqueries; "
                "prefer flat aggregates (COUNT/SUM/AVG/MAX/MIN) with GROUP BY. "
                "For comparisons use SUM(IF(...)) in a single flat query.",
                model_id=model_id,
                tbox_context=getattr(sparql_result, "tbox_context", None),
            )

        return tier2_result

    async def _retry_vkg(
        self,
        query: str,
        namespace: str,
        profile: dict,
        data_source_id: str,
        max_rows: int,
        trace: TraceCollector,
        vector_hits: list | None,
        *,
        hint: str,
        model_id: str | None = None,
        tbox_context: Any | None = None,
    ) -> Any | None:
        retry_start = time.perf_counter()
        # Reuse the first attempt's T-Box (the ontology is unchanged within a
        # request) so the retry only re-prompts the LLM, not re-queries Neptune.
        retry_result = await self._nl_to_sparql.translate(
            query,
            namespace,
            vector_hits=vector_hits,
            vkg_error_hint=hint,
            model_id=model_id,
            tbox_context=tbox_context,
        )
        # Replay sub-steps from retry translation into outer trace (E3)
        if retry_result.trace_steps:
            annotated = [
                {**s, "toolUsed": s.get("toolUsed") or _TIER2_TOOL_USED.get(str(s.get("step", "")))}
                for s in retry_result.trace_steps
            ]
            trace.add_dicts(annotated)

        if not (retry_result.valid and retry_result.sparql):
            retry_ms = int((time.perf_counter() - retry_start) * 1000)
            trace.record(StepId.T2_VKG_RETRY, "failed", retry_ms, detail={"reason": "re-translation failed validation"})
            return None
        try:
            tier2_result = await self._vkg_translator.resolve(
                sparql=retry_result.sparql,
                namespace=namespace,
                data_source_id=data_source_id,
                profile=profile,
                max_rows=max_rows,
            )
            retry_ms = int((time.perf_counter() - retry_start) * 1000)
            if tier2_result.error:
                # Replay the retry's own VKG steps (incl. the t2.vkg.compile
                # failure) so the cause of the retry failure is visible, then
                # mark the retry as failed.
                self._replay_vkg_steps(tier2_result, trace)
                trace.record(
                    StepId.T2_VKG_RETRY,
                    "failed",
                    retry_ms,
                    detail={"reason": f"retry also failed: {tier2_result.error}"},
                )
                return None
            trace.record(StepId.T2_VKG_RETRY, "success", retry_ms)
            return tier2_result
        except Exception as e:
            retry_ms = int((time.perf_counter() - retry_start) * 1000)
            logger.warning("tier2_vkg_retry_failed", error=str(e))
            trace.record(StepId.T2_VKG_RETRY, "failed", retry_ms, detail={"error": str(e)[:100]})
            return None

    async def _fetch_vector_hits(self, embedding: list[float], namespace: str) -> list | None:
        if not self._vector_client or not self._oss_ontology_index:
            return None
        try:
            index_name = ontology_vector_index_name(self._oss_ontology_index, namespace)

            # Restrict the CLASS retrieval to R2RML-mapped classes, matching the
            # gate NL→SQL applies (nl_to_sql/sql_generator.py). Unmapped classes —
            # document-induced or from a loaded foundational ontology — have no
            # table behind them, so authoring SPARQL against them produces a query
            # VKG cannot compile: nothing comes back, in exactly the mixed
            # DATABASE + DOCUMENTS namespace the ``is_mapped`` design exists to
            # protect (see external-docs "Mapped classes and Tier-2
            # answerability"). Without the gate a document-heavy query filled the
            # top-k with unmapped classes; the provenance skip evaluator only
            # bails when ALL top-k hits are unmapped, so the partial mix fell
            # through untouched.
            #
            # Legacy bridge, same as NL→SQL: namespaces ingested before
            # ``data_source_id`` was written carry zero mapped records, and gating
            # them would hide every class. One cheap count decides whether the
            # gate applies at all.
            mapped_count = await self._vector_client.count_documents(
                index=index_name, entity_type="class", require_mapped=True
            )
            use_mapped_gate = mapped_count > 0

            class_hits = await self._vector_client.search(
                embedding,
                namespace=namespace,
                top_k=_VECTOR_TOP_K,
                index=index_name,
                entity_type="class",
                require_mapped=use_mapped_gate,
            )
            # Properties and metrics are fetched unfiltered: only classes carry the
            # data_source_id stamp the mapped gate reads, and metrics are resolved
            # through Tier-1 rather than the VKG mapping. Classes are dropped from
            # this pass — they are already covered by the gated search above, and
            # keeping them would both duplicate hits and reintroduce the unmapped
            # classes the gate just excluded.
            other_hits = await self._vector_client.search(
                embedding, namespace=namespace, top_k=_VECTOR_TOP_K, index=index_name
            )
            raw_hits = [*class_hits, *(h for h in other_hits if h.metadata.get("entity_type") != "class")]

            logger.info(
                "tier2_aoss_raw_hits",
                namespace=namespace,
                count=len(raw_hits),
                mapped_gate=use_mapped_gate,
                mapped_count=mapped_count,
                class_hits=len(class_hits),
                sample_types=[h.metadata.get("entity_type", "MISSING") for h in raw_hits[:5]],
            )
            hits = []
            for h in raw_hits:
                entity_type = h.metadata.get("entity_type")
                if entity_type in ("class", "property"):
                    hits.append(
                        VectorHit(
                            type=f"ontology_{entity_type}",
                            score=h.score,
                            entity_id=h.metadata.get("text", h.id),
                            uri=h.metadata.get("entity_uri", ""),
                        )
                    )
                elif entity_type == "metric":
                    hits.append(
                        VectorHit(
                            type="metric",
                            score=h.score,
                            entity_id=h.metadata.get("text", h.id),
                            uri=h.metadata.get("entity_uri", ""),
                            metadata=h.metadata,
                        )
                    )
            logger.debug("tier2_filtered_hits", count=len(hits))
            return hits
        except Exception as e:
            logger.warning("tier2_vector_search_failed", error=str(e))
            return None
