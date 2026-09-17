# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unified response builder — every tier converges here."""

from __future__ import annotations

from collections.abc import Sequence

from .models import ConfidenceScore, QueryResult
from .trace import TraceCollector

# empty-result penalty factor for Tier-2 calibration.
# Conservative, documented default — NOT a tuned/measured value. Tuning the
# magnitude (and whether 0 rows should instead trigger a Tier-3 fallback) is a
# follow-up that intersects the Tier-2 accuracy eval; see MR "Notes for reviewers".
_TIER2_EMPTY_RESULT_PENALTY = 0.5


class ResponseAssembler:
    """Builds QueryResult from tier-specific resolution outputs."""

    def __init__(self, model_id: str = "unknown") -> None:
        """Store the default model id stamped into assembled response metadata.

        Args:
            model_id: Default LLM model id recorded in response metadata when a
                per-call override is not supplied.
        """
        self._model_id = model_id

    def _base_metadata(self, namespace: str, principal: str | None = None, model_id: str | None = None) -> dict:
        """Common metadata fields shared across all tiers."""
        meta: dict = {
            "namespace": namespace,
            "modelId": model_id or self._model_id,
        }
        if principal:
            meta["principal"] = principal
        return meta

    def assemble_nl_to_sql(
        self,
        *,
        rows: list[dict],
        columns: list[str],
        sql_used: str,
        confidence: float,
        retrieved_tables: list[str],
        expanded_tables: list[str],
        trace: TraceCollector,
        namespace: str,
        principal: str | None = None,
        strategy: str | None = None,
        model_id: str | None = None,
        ontology_version: str | None = None,
    ) -> QueryResult:
        """Build a Tier-2 QueryResult from an NL-to-SQL resolution.

        Args:
            rows: Result rows returned by the executed SQL.
            columns: Column names of the result set.
            sql_used: The SQL statement that was executed.
            confidence: Resolution confidence score in [0, 1].
            retrieved_tables: Tables surfaced by ontology retrieval.
            expanded_tables: Tables added via schema expansion.
            trace: Processing-trace collector for this request.
            namespace: Namespace the query ran against.
            principal: Optional authenticated caller identity.
            strategy: Optional Tier-2 strategy label recorded in metadata.
            model_id: Optional per-call model id override.
            ontology_version: Version of the ontology the query ran against,
                when the caller can name one (#986).

        Returns:
            A QueryResult tagged as Tier 2.
        """
        metadata = self._base_metadata(namespace, principal, model_id=model_id)
        metadata["columns"] = columns
        metadata["retrieved_tables"] = retrieved_tables
        metadata["expanded_tables"] = expanded_tables
        if strategy:
            metadata["strategy"] = strategy
        return QueryResult(
            tier=2,
            confidence=ConfidenceScore(score=confidence, rationale="LLM-generated SQL from ontology retrieval"),
            result_rows=rows,
            query_used=sql_used,
            trace=trace.steps,
            ontology_version=ontology_version,
            metadata=metadata,
        )

    def assemble_tier1(
        self,
        *,
        rows: list[dict],
        columns: list[str],
        metric_name: str,
        sql_used: str,
        trace: TraceCollector,
        namespace: str,
        principal: str | None = None,
        match_confidence: float = 1.0,
        match_source: str = "name",
        model_id: str | None = None,
        ontology_version: str | None = None,
    ) -> QueryResult:
        """Build a Tier-1 QueryResult from a pre-defined metric resolution.

        Args:
            rows: Result rows from the metric's SQL.
            columns: Column names of the result set.
            metric_name: Name of the resolved metric.
            sql_used: The SQL statement that was executed.
            trace: Processing-trace collector for this request.
            namespace: Namespace the metric ran against.
            principal: Optional authenticated caller identity.
            match_confidence: Matcher similarity ratio (used for fuzzy matches).
            match_source: How the metric was matched (``name`` or ``fuzzy``).
            model_id: Optional per-call model id override.
            ontology_version: Version of the ontology the query ran against,
                when the caller can name one (#986).

        Returns:
            A QueryResult tagged as Tier 1; confidence is 1.0 for exact matches
            and the similarity ratio for fuzzy near-matches.
        """
        # an EXACT name/synonym match is deterministic (confidence 1.0);
        # a FUZZY near-miss carries the matcher's similarity ratio (< 1.0) so the
        # response honestly signals it was an approximate resolution.
        # TODO: gate query_used on caller's includeDebugInfo option
        if match_source == "fuzzy":
            rationale = f"Fuzzy metric near-match: {metric_name} (similarity {match_confidence:.2f})"
            score = min(1.0, max(0.0, match_confidence))
        else:
            rationale = f"Deterministic metric: {metric_name}"
            score = 1.0
        metadata = self._base_metadata(namespace, principal, model_id=model_id)
        metadata["columns"] = columns
        return QueryResult(
            tier=1,
            confidence=ConfidenceScore(score=score, rationale=rationale),
            result_rows=rows,
            query_used=sql_used,
            trace=trace.steps,
            ontology_version=ontology_version,
            metadata=metadata,
        )

    @staticmethod
    def _calibrate_tier2(base_confidence: float, row_count: int | None, truncated: bool) -> ConfidenceScore:
        """multi-factor Tier-2 confidence from in-hand signals.

        Starts from the LLM self-rating (base_confidence) and applies, using
        ONLY signals the orchestrator already holds at assembly time:
        - empty-result penalty when the executed SQL returned zero rows;
        - a truncation annotation when results were capped.
        Returns a ConfidenceScore whose rationale enumerates the applied factors.
        No new pipeline call is made.

        row_count contract: ``None`` means UNKNOWN (no penalty — never guess);
        ``0`` is the caller's assertion of a CONFIRMED empty result (penalty).
        The production caller passes the executor QueryResult's required
        ``row_count``, where 0 is always a real executed-and-empty result.
        """
        factors: list[str] = ["NL-to-SPARQL translation"]
        score = base_confidence

        # Empty-result penalty: zero rows is weak evidence the translation
        # actually answered the question, so lower confidence.
        if row_count == 0:
            score *= _TIER2_EMPTY_RESULT_PENALTY
            factors.append("empty result (0 rows) penalty")

        # Truncation: annotate only — truncated results are still valid evidence,
        # so this does not change the score, just records the caveat.
        if truncated:
            factors.append("results truncated")

        score = min(1.0, max(0.0, score))
        return ConfidenceScore(score=score, rationale="; ".join(factors))

    def assemble_tier2(
        self,
        *,
        rows: list[dict] | None,
        columns: list[str] | None,
        sql_used: str | None,
        sparql: str,
        confidence: float,
        ontology_version: str | None,
        data_sources: list[str] | None,
        trace: TraceCollector,
        namespace: str,
        principal: str | None = None,
        row_count: int | None = None,
        truncated: bool = False,
        strategy: str | None = None,
        model_id: str | None = None,
    ) -> QueryResult:
        """Build a Tier-2 QueryResult from a VKG (NL-to-SPARQL) resolution.

        Applies multi-factor Tier-2 confidence calibration (empty-result penalty,
        truncation annotation) before assembly.

        Args:
            rows: Result rows from the executed SQL, or None if unknown.
            columns: Column names of the result set, if available.
            sql_used: The SQL compiled from SPARQL, if available.
            sparql: The generated SPARQL query.
            confidence: Base (LLM self-rated) confidence in [0, 1].
            ontology_version: Ontology version used for translation.
            data_sources: Data sources referenced by the query.
            trace: Processing-trace collector for this request.
            namespace: Namespace the query ran against.
            principal: Optional authenticated caller identity.
            row_count: Confirmed row count; ``None`` means unknown (no penalty).
            truncated: Whether the result set was capped.
            strategy: Optional Tier-2 strategy label recorded in metadata.
            model_id: Optional per-call model id override.

        Returns:
            A QueryResult tagged as Tier 2 with calibrated confidence.
        """
        metadata = self._base_metadata(namespace, principal, model_id=model_id)
        if columns:
            metadata["columns"] = columns
        if strategy:
            metadata["strategy"] = strategy

        # When row_count is not supplied but rows are materialized, derive it so a
        # visible empty result still triggers the calibration penalty (closes the
        # direct-call footgun). rows=None remains "unknown" → no penalty.
        if row_count is None and rows is not None:
            row_count = len(rows)

        # TODO: gate query_used and sparql_generated on caller's includeDebugInfo option
        return QueryResult(
            tier=2,
            confidence=self._calibrate_tier2(confidence, row_count, truncated),
            result_rows=rows,
            query_used=sql_used,
            sparql_generated=sparql,
            trace=trace.steps,
            ontology_version=ontology_version,
            data_sources=data_sources,
            metadata=metadata,
        )

    def assemble_tier3(
        self,
        *,
        synthesized_answer: str,
        supporting_content: Sequence[dict],
        graph_context: Sequence[dict],
        confidence: float,
        guardrail_blocked: bool = False,
        trace: TraceCollector,
        namespace: str,
        principal: str | None = None,
        degraded_sources: Sequence[dict] | None = None,
        model_id: str | None = None,
        partial: bool = False,
        ontology_version: str | None = None,
    ) -> QueryResult:
        """Build a Tier-3 QueryResult from an agentic synthesis.

        Args:
            synthesized_answer: The natural-language answer produced by synthesis.
            supporting_content: Retrieved chunks backing the answer.
            graph_context: Graph entities used as context.
            confidence: Resolution confidence in [0, 1]; forced to 0 when blocked.
            guardrail_blocked: Whether a guardrail blocked the synthesized answer.
            trace: Processing-trace collector for this request.
            namespace: Namespace the query ran against.
            principal: Optional authenticated caller identity.
            degraded_sources: Retrieval sources that errored/timed out; presence
                marks the result as ``partial``.
            model_id: Optional per-call model id override.
            partial: Whether the result is incomplete (e.g. a budget/timeout cut
                the reasoning loop short); surfaced on the QueryResult.
            ontology_version: Version of the ontology the query ran against,
                when the caller can name one (#986).

        Returns:
            A QueryResult tagged as Tier 3.
        """
        # defense-in-depth clamp. synthesizer.py already returns 0.0
        # on a guardrail block; force it here too so a blocked response can never
        # surface a non-zero confidence. This can only LOWER the score, never raise
        # it — honoring the no-weakening-safety-controls rule.
        if guardrail_blocked:
            confidence = 0.0
        metadata = self._base_metadata(namespace, principal, model_id=model_id)
        if degraded_sources:
            metadata["degradedSources"] = list(degraded_sources)
        return QueryResult(
            tier=3,
            confidence=ConfidenceScore(score=confidence, rationale="Agentic synthesis"),
            synthesized_answer=synthesized_answer,
            supporting_content=list(supporting_content),
            graph_context={"entities": list(graph_context)},
            guardrail_blocked=guardrail_blocked,
            trace=trace.steps,
            # ``partial`` signals the answer was built from INCOMPLETE inputs.
            # A degraded source (a retrieval source that errored/timed out) means the
            # synthesis ran on partial context, so flag it. (The REST 29s-timeout
            # truncation case is owned by Data-Layer ; this is the in-runtime
            # partial-context signal we can set authoritatively here.)
            partial=bool(degraded_sources) or partial,
            ontology_version=ontology_version,
            metadata=metadata,
        )
