# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unified response builder — every tier converges here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import structlog

from .graph_context import (
    LEGACY_SUPPORTING_STATEMENTS_KEY,
    SUPPORTING_STATEMENTS_KEY,
    bound_supporting_statements,
    encode_supporting_statements,
    fair_bound_statement_groups,
)
from .models import ConfidenceScore, QueryResult
from .step_ids import StepId
from .trace import TraceCollector

logger = structlog.get_logger(__name__)

# Wire bounds on an assembled graph context. The statement bounds in
# ``graph_context`` cap how much text ONE entity carries; these cap how many
# entities and edges a single response can carry at all. Without them the list
# length is whatever the retrieval loop accumulated, which is unbounded on the
# wire, in the session envelope, and in the client's renderer.
MAX_GRAPH_CONTEXT_ENTITIES = 200
MAX_GRAPH_CONTEXT_RELATIONSHIPS = 500

# empty-result penalty factor for Tier-2 calibration.
# Conservative, documented default — NOT a tuned/measured value. Tuning the
# magnitude (and whether 0 rows should instead trigger a Tier-3 fallback) is a
# follow-up that intersects the Tier-2 accuracy eval; see MR "Notes for reviewers".
_TIER2_EMPTY_RESULT_PENALTY = 0.5


def _first_non_empty_string(*values: object) -> str | None:
    """Return the first non-blank string from ``values``."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass(frozen=True)
class GraphContextDrops:
    """Counts of graph evidence discarded while mapping to the wire shape.

    Every count here is evidence the caller supplied and the response does NOT
    contain, so each one has to reach the trace and ``degradedSources``: an empty
    ``relationships`` list must never be ambiguous between "the graph had no edges"
    and "every edge was thrown away".
    """

    relationships_without_identity: int = 0
    entities_without_uri: int = 0
    entities_over_bound: int = 0
    relationships_over_bound: int = 0

    @property
    def total(self) -> int:
        """How many pieces of supplied graph evidence were discarded."""
        return (
            self.relationships_without_identity
            + self.entities_without_uri
            + self.entities_over_bound
            + self.relationships_over_bound
        )

    def describe(self) -> str:
        """Human-readable summary for a trace detail / degraded-source record."""
        parts = [
            f"{self.relationships_without_identity} relationship(s) with no predicate identity",
            f"{self.entities_without_uri} entities with no uri",
            f"{self.entities_over_bound} entities over the {MAX_GRAPH_CONTEXT_ENTITIES} bound",
            f"{self.relationships_over_bound} relationship(s) over the {MAX_GRAPH_CONTEXT_RELATIONSHIPS} bound",
        ]
        counts = (
            self.relationships_without_identity,
            self.entities_without_uri,
            self.entities_over_bound,
            self.relationships_over_bound,
        )
        return "dropped " + "; ".join(part for part, count in zip(parts, counts, strict=True) if count)


def _canonical_graph_context(
    graph_context: Sequence[dict], *, include_statements: bool = True
) -> tuple[dict, GraphContextDrops]:
    """Map internal GraphEntity dictionaries to the Smithy GraphContext shape.

    Returns the canonical context plus a count of everything discarded, so the
    caller can make the discards observable rather than returning a quietly
    thinned-out graph.

    ``include_statements=False`` leaves every entity's ``supportingStatements``
    out. Those statements are retrieved text (an edge-typed traversal's
    statement, or lexical text with no source document) that used to travel as
    ``supportingContent`` items, so a caller who opted out of supporting content
    must not receive them through ``graphContext`` instead.
    """
    entities: list[dict] = []
    entity_statements: list[list[str]] = []
    relationships: list[dict] = []
    seen_relationships: set[tuple[str, str, str]] = set()
    entities_without_uri = 0
    relationships_without_identity = 0

    for raw_entity in graph_context:
        uri = _first_non_empty_string(raw_entity.get("uri"))
        if uri is None:
            entities_without_uri += 1
            continue

        entity: dict = {
            "uri": uri,
            "label": _first_non_empty_string(raw_entity.get("label")) or uri,
        }
        entity_type = _first_non_empty_string(raw_entity.get("type"))
        if entity_type is not None:
            entity["type"] = entity_type
        properties = raw_entity.get("properties")
        if isinstance(properties, dict) and properties:
            canonical_properties = {
                str(key): str(value)
                for key, value in properties.items()
                if key not in {SUPPORTING_STATEMENTS_KEY, LEGACY_SUPPORTING_STATEMENTS_KEY}
            }
            if canonical_properties:
                entity["properties"] = canonical_properties
            entity_statements.append(
                bound_supporting_statements(
                    properties.get(
                        SUPPORTING_STATEMENTS_KEY,
                        properties.get(LEGACY_SUPPORTING_STATEMENTS_KEY),
                    )
                )
                if include_statements
                else []
            )
        else:
            entity_statements.append([])
        entities.append(entity)

        raw_relationships = raw_entity.get("relationships")
        if not isinstance(raw_relationships, list | tuple):
            continue
        for raw_relationship in raw_relationships:
            if not isinstance(raw_relationship, dict):
                continue
            source_uri = _first_non_empty_string(
                raw_relationship.get("sourceUri"),
                raw_relationship.get("source_uri"),
            )
            predicate_uri = _first_non_empty_string(
                raw_relationship.get("predicateUri"),
                raw_relationship.get("predicate_uri"),
            )
            target_uri = _first_non_empty_string(
                raw_relationship.get("targetUri"),
                raw_relationship.get("target_uri"),
            )
            if source_uri is None or predicate_uri is None or target_uri is None:
                # A relationship with no stable predicate identity cannot be put on
                # the wire (``predicateUri`` is @required and a human-readable label
                # is not an identifier). Producers reading a label-only property
                # graph mint a synthetic tenant-scoped IRI upstream
                # (``graph_tool.synthetic_predicate_uri``); anything still arriving
                # without one is genuinely unidentifiable, so it is counted here and
                # reported by the caller.
                relationships_without_identity += 1
                continue
            relationship_key = (source_uri, predicate_uri, target_uri)
            if relationship_key in seen_relationships:
                continue
            seen_relationships.add(relationship_key)
            relationship = {
                "sourceUri": source_uri,
                "predicateUri": predicate_uri,
                "targetUri": target_uri,
            }
            predicate_label = _first_non_empty_string(
                raw_relationship.get("predicateLabel"),
                raw_relationship.get("predicate_label"),
            )
            if predicate_label is not None:
                relationship["predicateLabel"] = predicate_label
            relationships.append(relationship)

    entities_over_bound = max(0, len(entities) - MAX_GRAPH_CONTEXT_ENTITIES)
    relationships_over_bound = max(0, len(relationships) - MAX_GRAPH_CONTEXT_RELATIONSHIPS)
    del entities[MAX_GRAPH_CONTEXT_ENTITIES:]
    del entity_statements[MAX_GRAPH_CONTEXT_ENTITIES:]
    del relationships[MAX_GRAPH_CONTEXT_RELATIONSHIPS:]

    for entity, statements in zip(entities, fair_bound_statement_groups(entity_statements), strict=True):
        if not statements:
            continue
        properties = entity.setdefault("properties", {})
        properties[SUPPORTING_STATEMENTS_KEY] = encode_supporting_statements(statements)

    return {"entities": entities, "relationships": relationships}, GraphContextDrops(
        relationships_without_identity=relationships_without_identity,
        entities_without_uri=entities_without_uri,
        entities_over_bound=entities_over_bound,
        relationships_over_bound=relationships_over_bound,
    )


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
        include_debug_info: bool = False,
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
            include_debug_info: When False (default), the generated SQL is
                withheld from ``query_used``. Executed SQL / SPARQL exposes
                internal schema (table + column names, joins). Callers opt in
                via ``options.includeDebugInfo=true``. Withheld by default so
                the field cannot leak internal schema (table names, column
                names, joins) to a caller who did not opt in.

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
            query_used=sql_used if include_debug_info else None,
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
        include_debug_info: bool = False,
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
            include_debug_info: When False (default), the executed metric SQL
                is withheld from ``query_used`` — it exposes internal schema
                (table + column names). Opt in via
                ``options.includeDebugInfo=true``.

        Returns:
            A QueryResult tagged as Tier 1; confidence is 1.0 for exact matches
            and the similarity ratio for fuzzy near-matches.
        """
        # an EXACT name/synonym match is deterministic (confidence 1.0);
        # a FUZZY near-miss carries the matcher's similarity ratio (< 1.0) so the
        # response honestly signals it was an approximate resolution.
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
            query_used=sql_used if include_debug_info else None,
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
        include_debug_info: bool = False,
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
            include_debug_info: When False (default), the compiled SQL and
                generated SPARQL are withheld from ``query_used`` /
                ``sparql_generated`` — both expose internal schema (tables,
                columns, joins). Opt in via ``options.includeDebugInfo=true``.

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

        return QueryResult(
            tier=2,
            confidence=self._calibrate_tier2(confidence, row_count, truncated),
            result_rows=rows,
            query_used=sql_used if include_debug_info else None,
            sparql_generated=sparql if include_debug_info else None,
            trace=trace.steps,
            ontology_version=ontology_version,
            data_sources=data_sources,
            metadata=metadata,
        )

    def assemble_tier3(
        self,
        *,
        synthesized_answer: str,
        supporting_content: Sequence[dict] | None,
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
        execution_mode: str | None = None,
        query_used: str | None = None,
        sparql_generated: str | None = None,
        include_debug_info: bool = False,
    ) -> QueryResult:
        """Build a Tier-3 QueryResult from an agentic synthesis.

        Args:
            synthesized_answer: The natural-language answer produced by synthesis.
            supporting_content: Retrieved chunks backing the answer. ``None`` when the
                caller opted out (``includeSupporting=False``) — the field is then
                omitted from the response, as on Tier 1/2; ``[]`` means none found.
            graph_context: Graph entities used as context. Their
                ``supportingStatements`` are omitted when ``supporting_content``
                is ``None``; entities and relationships are always returned.
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
            execution_mode: The resolved execution mode ("standard" /
                "deep-reasoning"); surfaced structurally on the QueryResult so the
                console can label a Deep Reasoning answer by its MODE rather than
                mis-reading its Tier-3 resolution as Standard mode's Tier-3 label
                (#1166). ``None`` leaves the field unset.
            query_used: SQL the agentic loop built + executed (#1166).
            sparql_generated: SPARQL the agentic loop built + executed (#1166).
            include_debug_info: When False (default), ``query_used`` /
                ``sparql_generated`` are WITHHELD — they expose internal schema,
                exactly as the Tier-2 assembler withholds them. Opt in via
                ``options.includeDebugInfo=true``.

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

        # ``None`` is the opt-out (see Args). Graph statement text is retrieved
        # text too, and before #984 it was a ``supportingContent`` item, so it
        # goes with the opt-out rather than moving to ``graphContext``.
        canonical_graph_context, drops = _canonical_graph_context(
            graph_context, include_statements=supporting_content is not None
        )
        all_degraded_sources = list(degraded_sources or ())
        if drops.total:
            # Without this, "the graph returned no edges" and "every edge was
            # discarded" are the same empty list to an operator: no trace step, no
            # degraded source, no log. The synthesizer may already have asserted the
            # discarded relationship in the answer text.
            detail = drops.describe()
            logger.warning(
                "graph_context_evidence_dropped",
                namespace=namespace,
                relationships_without_identity=drops.relationships_without_identity,
                entities_without_uri=drops.entities_without_uri,
                entities_over_bound=drops.entities_over_bound,
                relationships_over_bound=drops.relationships_over_bound,
                retained_entities=len(canonical_graph_context["entities"]),
                retained_relationships=len(canonical_graph_context["relationships"]),
            )
            trace.record(StepId.RESPONSE_ASSEMBLE, "degraded", 0, detail=detail)
            all_degraded_sources.append(
                {
                    "source": str(StepId.RESPONSE_ASSEMBLE),
                    "reason": "graph_context_evidence_dropped",
                    "detail": detail,
                }
            )

        if all_degraded_sources:
            metadata["degradedSources"] = all_degraded_sources
        return QueryResult(
            tier=3,
            confidence=ConfidenceScore(score=confidence, rationale="Agentic synthesis"),
            synthesized_answer=synthesized_answer,
            supporting_content=None if supporting_content is None else list(supporting_content),
            graph_context=canonical_graph_context,
            guardrail_blocked=guardrail_blocked,
            # Same includeDebugInfo gate as the Tier-2 assembler: the compiled SQL
            # and generated SPARQL expose internal schema, so withhold them unless
            # the caller opted in. This closes the Deep-Reasoning artifact gap
            # (#1166) without widening what a default request leaks.
            query_used=query_used if include_debug_info else None,
            sparql_generated=sparql_generated if include_debug_info else None,
            execution_mode=execution_mode,
            trace=trace.steps,
            # ``partial`` signals the answer was built from INCOMPLETE inputs.
            # A degraded source (a retrieval source that errored/timed out) means the
            # synthesis ran on partial context, so flag it. (The REST 29s-timeout
            # truncation case is owned by Data-Layer ; this is the in-runtime
            # partial-context signal we can set authoritatively here.)
            # Discarded graph evidence counts too: the returned context is provably
            # narrower than what retrieval produced.
            partial=bool(all_degraded_sources) or partial,
            ontology_version=ontology_version,
            metadata=metadata,
        )
