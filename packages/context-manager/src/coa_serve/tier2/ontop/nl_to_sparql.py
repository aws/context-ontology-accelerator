# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL-to-SPARQL translation via Bedrock Converse API.

Orchestrates:
1. T-Box context assembly (from routing's vector hits)
2. Prompt assembly with system instructions + few-shot + context
3. LLM call via Bedrock Converse API
4. SPARQL extraction from response (handles markdown code blocks)
5. SPARQL validation (syntax + URI existence)

Returns TranslationResult with the generated SPARQL, confidence, and validity.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from ...clients.base import GraphClient, LLMClient
from .sparql_validator import SPARQLValidator, ValidationResult
from .tbox_context import TBoxContext, TBoxContextBuilder
from .types import VectorHit

logger = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S = 60.0

_SYSTEM_PROMPT = (
    "You are a SPARQL query generator for the Ontop Virtual Knowledge Graph (VKG) engine. "
    "Given a natural language question and an ontology schema, generate a valid SPARQL 1.1 query.\n\n"
    "CRITICAL RULES:\n"
    "- The schema provides a PREFIX declaration (e.g. PREFIX ind: <...>). "
    "You MUST copy that EXACT PREFIX into your SPARQL query.\n"
    "- Use ONLY the prefixed names shown in the schema (e.g. ind:ClassName, ind:propertyName)\n"
    "- NEVER invent your own prefix URI — use exactly what the schema provides\n"
    "- Generate valid SPARQL 1.1 syntax\n"
    "- Include FILTER for numeric/date constraints\n"
    "- When a property lists 'allowed values: [...]', match one of those literals "
    "exactly and case-sensitively (do NOT wrap in LCASE or invent a value)\n"
    "- Use OPTIONAL for fields that may not exist on all instances\n"
    "- Return the SPARQL query in a ```sparql code block\n"
    "- After the query, rate your confidence (0.0-1.0) that this query "
    "correctly answers the question\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "AGGREGATION — MANDATORY PATTERNS (use these, NOT subqueries)\n"
    "═══════════════════════════════════════════════════════════════\n\n"
    "The Ontop VKG engine supports: COUNT, SUM, MIN, MAX, AVG, GROUP_CONCAT, IF, COALESCE.\n\n"
    "★ RULE: For ANY question that compares, contrasts, or computes differences/ratios between \n"
    " categories, you MUST use the SUM(IF(...)) pattern in a SINGLE flat query.\n"
    "★ RULE: NEVER use nested SELECT subqueries for category comparison. They fail on Ontop.\n"
    "★ RULE: NEVER use { SELECT ... } inside WHERE. Use flat SUM(IF) instead.\n\n"
    "DECISION TREE — pick the pattern that matches the question type:\n\n"
    '1. "How many X vs Y" / "difference between A and B" / "compare counts"\n'
    ' → SUM(IF(?var = "X", 1, 0)) pattern (count per category)\n\n'
    '2. "Total/sum of X vs Y" / "difference in totals"\n'
    ' → SUM(IF(?var = "X", ?amount, 0)) pattern (sum per category)\n\n'
    '3. "Ratio of X to Y" / "proportion" / "percentage"\n'
    ' → SUM(IF(?var = "X", 1, 0)) / SUM(IF(?var = "Y", 1, 0)) pattern\n\n'
    '4. "How many more laps/items/events in condition A vs B"\n'
    ' → SUM(IF(?condition = "A", 1, 0)) - SUM(IF(?condition = "B", 1, 0))\n\n'
    "5. Simple total/count/min/max/avg (no comparison)\n"
    " → Direct aggregate: SELECT (COUNT(?x) AS ?total) WHERE { ... }\n\n"
    "6. GROUP BY with aggregates\n"
    " → SELECT ?group (SUM(?amount) AS ?total) WHERE { ... } GROUP BY ?group\n\n"
    "7. HAVING (post-aggregation filter)\n"
    " → GROUP BY ?x HAVING(COUNT(?y) > 5)\n\n"
    "ANTI-PATTERNS (NEVER do these):\n"
    '❌ { SELECT (COUNT(?x) AS ?c1) WHERE { FILTER(?cat = "A") } } ← nested subquery\n'
    "❌ Two separate SELECT blocks joined — use ONE flat query with SUM(IF)\n"
    "❌ MINUS or NOT EXISTS for simple count comparisons — use SUM(IF) instead\n\n"
    "STRING MATCHING:\n"
    '- Use LCASE() for case-insensitive matching: FILTER(LCASE(?x) = "value")\n'
    '- Use CONTAINS() for partial matching: FILTER(CONTAINS(LCASE(?x), "val"))\n\n'
    "═══════════════════════════════════════════════════════════════\n"
    "EXAMPLES\n"
    "═══════════════════════════════════════════════════════════════\n\n"
    "Example 1 (basic filter + projection):\n\n"
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Product (label: product)\n"
    "Properties:\n"
    " ind:product_revenue domain: ind:Product range: xsd:decimal\n"
    " ind:product_name domain: ind:Product range: xsd:string\n\n"
    "Question: What products have revenue greater than 1 million?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
    "SELECT ?product ?name ?revenue\n"
    "WHERE {\n"
    " ?product a ind:Product ;\n"
    " ind:product_name ?name ;\n"
    " ind:product_revenue ?revenue .\n"
    " FILTER(?revenue > 1000000)\n"
    "}\n"
    "ORDER BY DESC(?revenue)\n"
    "```\n"
    "Confidence: 0.85\n\n"
    'Example 2 (conditional count — SUM(IF) for "how many more X than Y"):\n\n'
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Station (label: gas station)\n"
    "Properties:\n"
    " ind:station_country domain: ind:Station range: xsd:string\n"
    " ind:station_segment domain: ind:Station range: xsd:string\n\n"
    "Question: How many more discount stations does CZE have compared to SVK?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    'SELECT (SUM(IF(LCASE(?country) = "cze", 1, 0)) - SUM(IF(LCASE(?country) = "svk", 1, 0)) AS ?difference)\n'
    "WHERE {\n"
    " ?s a ind:Station ;\n"
    " ind:station_country ?country ;\n"
    " ind:station_segment ?segment .\n"
    ' FILTER(LCASE(?segment) = "discount")\n'
    "}\n"
    "```\n"
    "Confidence: 0.85\n\n"
    'Example 3 (conditional sum — SUM(IF) for "difference in totals"):\n\n'
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Consumption (label: energy consumption)\n"
    "Properties:\n"
    " ind:consumption_type domain: ind:Consumption range: xsd:string\n"
    " ind:consumption_amount domain: ind:Consumption range: xsd:decimal\n\n"
    "Question: What is the difference in total consumption between residential and commercial?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
    'SELECT (SUM(IF(LCASE(?type) = "residential", ?amount, 0)) - '
    'SUM(IF(LCASE(?type) = "commercial", ?amount, 0)) AS ?difference)\n'
    "WHERE {\n"
    " ?c a ind:Consumption ;\n"
    " ind:consumption_type ?type ;\n"
    " ind:consumption_amount ?amount .\n"
    "}\n"
    "```\n"
    "Confidence: 0.80\n\n"
    'Example 4 (ratio — SUM(IF) for "ratio of X to Y"):\n\n'
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Transaction (label: bank transaction)\n"
    "Properties:\n"
    " ind:transaction_type domain: ind:Transaction range: xsd:string\n\n"
    "Question: What is the ratio of credit to debit transactions?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    'SELECT (SUM(IF(LCASE(?type) = "credit", 1, 0)) AS ?creditCount)\n'
    ' (SUM(IF(LCASE(?type) = "debit", 1, 0)) AS ?debitCount)\n'
    ' (xsd:decimal(SUM(IF(LCASE(?type) = "credit", 1, 0))) / '
    'SUM(IF(LCASE(?type) = "debit", 1, 0)) AS ?ratio)\n'
    "WHERE {\n"
    " ?t a ind:Transaction ;\n"
    " ind:transaction_type ?type .\n"
    "}\n"
    "```\n"
    "Confidence: 0.80\n\n"
    "Example 5 (compare counts across years — SUM(IF) with FILTER on year):\n\n"
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Badge (label: user badge)\n"
    "Properties:\n"
    " ind:badge_date domain: ind:Badge range: xsd:string\n"
    " ind:badge_userid domain: ind:Badge range: xsd:integer\n\n"
    "Question: Compare the number of badges earned in 2010 vs 2011\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
    'SELECT (SUM(IF(CONTAINS(?date, "2010"), 1, 0)) AS ?badges2010)\n'
    ' (SUM(IF(CONTAINS(?date, "2011"), 1, 0)) AS ?badges2011)\n'
    ' (SUM(IF(CONTAINS(?date, "2010"), 1, 0)) - '
    'SUM(IF(CONTAINS(?date, "2011"), 1, 0)) AS ?difference)\n'
    "WHERE {\n"
    " ?b a ind:Badge ;\n"
    " ind:badge_date ?date .\n"
    "}\n"
    "```\n"
    "Confidence: 0.80\n\n"
    'Example 6 (count with condition — "how many laps in wet vs dry"):\n\n'
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Lap (label: race lap)\n"
    "Properties:\n"
    " ind:lap_condition domain: ind:Lap range: xsd:string\n"
    " ind:lap_time domain: ind:Lap range: xsd:decimal\n\n"
    "Question: How many more laps were completed in wet conditions vs dry conditions?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    'SELECT (SUM(IF(LCASE(?condition) = "wet", 1, 0)) AS ?wetLaps)\n'
    ' (SUM(IF(LCASE(?condition) = "dry", 1, 0)) AS ?dryLaps)\n'
    ' (SUM(IF(LCASE(?condition) = "wet", 1, 0)) - '
    'SUM(IF(LCASE(?condition) = "dry", 1, 0)) AS ?difference)\n'
    "WHERE {\n"
    " ?l a ind:Lap ;\n"
    " ind:lap_condition ?condition .\n"
    "}\n"
    "```\n"
    "Confidence: 0.85\n\n"
    "Example 7 (simple aggregation — COUNT):\n\n"
    "Question: What is the total number of products?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "SELECT (COUNT(?p) AS ?total)\n"
    "WHERE {\n"
    " ?p a ind:Product .\n"
    "}\n"
    "```\n"
    "Confidence: 0.90\n\n"
    "Example 8 (AVG with filter):\n\n"
    "Schema:\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "Classes: ind:Order (label: order)\n"
    "Properties:\n"
    " ind:order_amount domain: ind:Order range: xsd:decimal\n"
    " ind:order_status domain: ind:Order range: xsd:string\n\n"
    "Question: What is the average order amount for completed orders?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
    "SELECT (AVG(?amount) AS ?avgAmount)\n"
    "WHERE {\n"
    " ?o a ind:Order ;\n"
    " ind:order_amount ?amount ;\n"
    " ind:order_status ?status .\n"
    ' FILTER(LCASE(?status) = "completed")\n'
    "}\n"
    "```\n"
    "Confidence: 0.85\n\n"
    "Example 9 (SUM + GROUP BY):\n\n"
    "Question: What is the total revenue per product category?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "SELECT ?category (SUM(?revenue) AS ?totalRevenue)\n"
    "WHERE {\n"
    " ?p a ind:Product ;\n"
    " ind:product_category ?category ;\n"
    " ind:product_revenue ?revenue .\n"
    "}\n"
    "GROUP BY ?category\n"
    "```\n"
    "Confidence: 0.85\n\n"
    "Example 10 (MAX/MIN):\n\n"
    "Question: What is the highest score in the competition?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "SELECT (MAX(?score) AS ?highestScore)\n"
    "WHERE {\n"
    " ?e a ind:Entry ;\n"
    " ind:entry_score ?score .\n"
    "}\n"
    "```\n"
    "Confidence: 0.90\n\n"
    "Example 11 (GROUP BY + HAVING):\n\n"
    "Question: Which customers have placed more than 5 orders?\n"
    "```sparql\n"
    "PREFIX ind: <https://example.org/ontology/>\n"
    "SELECT ?customer (COUNT(?order) AS ?orderCount)\n"
    "WHERE {\n"
    " ?order a ind:Order ;\n"
    " ind:order_customer ?customer .\n"
    "}\n"
    "GROUP BY ?customer\n"
    "HAVING(COUNT(?order) > 5)\n"
    "```\n"
    "Confidence: 0.85\n\n"
    "SELECT VARIABLE RULES:\n"
    "- Always SELECT only the specific variables needed to answer the question.\n"
    "- Use descriptive named variables (e.g. SELECT ?name ?count ?total), "
    "NOT the subject variable ?s or wildcard *.\n"
    "- If the question asks for a count or aggregate, SELECT only the aggregate result.\n"
    "- If the question asks for a specific field (name, date, amount), "
    "SELECT only that field.\n\n"
    "═══════════════════════════════════════════════════════════════\n"
    "DETERMINISM RULES — MANDATORY (identical question ⇒ identical query)\n"
    "═══════════════════════════════════════════════════════════════\n\n"
    "The SAME question must always produce the SAME query shape. Ambiguity is\n"
    "resolved by these fixed rules, NOT by free choice — otherwise the same\n"
    "question silently returns different answers on different runs.\n\n"
    "1. LIMIT — decide explicitly, never implicitly:\n"
    '   - A superlative singular ("the most", "the top", "the highest", "which X\n'
    '     has the most") means the SINGLE best row → add `LIMIT 1`.\n'
    '   - A ranking or plural ("rank", "list", "which colours", "all", "by\n'
    '     count") means ALL rows ordered → NO LIMIT (use ORDER BY instead).\n'
    "   - When neither is signalled, DEFAULT to NO LIMIT and return all rows.\n"
    "   - Never omit LIMIT on a singular-superlative question and never add\n"
    "     LIMIT on a ranking question.\n\n"
    "2. COUNT vs COUNT(DISTINCT) — decide explicitly:\n"
    "   - Use `COUNT(DISTINCT ?x)` when counting how many UNIQUE entities/values\n"
    '     ("how many colours", "number of distinct brands", "unique products").\n'
    "   - ALWAYS use `COUNT(DISTINCT ?e)` when ?e is an ENTITY variable (one bound\n"
    "     via `?e a :Class`) — counting a typed entity with a plain COUNT counts\n"
    "     join-multiplied rows and inflates on 1-to-many data, so the same question\n"
    "     returns a different number between runs. This is enforced by the validator.\n"
    "   - Use plain `COUNT(?x)` only when counting ROWS/occurrences including\n"
    '     duplicates ("how many product listings", "number of rows").\n'
    "   - When in doubt, prefer `COUNT(DISTINCT ?x)` — entity counts are the\n"
    "     common intent and duplicates otherwise inflate the answer silently.\n\n"
    "3. PROJECTION ALIAS — deterministic, stable names:\n"
    "   - Every projected column MUST have an explicit alias via `AS ?name`.\n"
    "   - Name the alias after the THING, not the run: an entity by its concept\n"
    "     (`?colorName`, `?brandName`, `?materialName`), an aggregate by\n"
    "     metric+subject in lowerCamelCase (`?productCount`, `?totalRevenue`,\n"
    "     `?avgPrice`). NEVER a bare `?count`, `?value`, `?result`, `?n`.\n"
    "   - Use the SAME alias for the SAME concept every time — do not rename\n"
    "     `?productCount` to `?count` between runs.\n"
    "   - Keep the projection SHAPE fixed: answer the asked question with the\n"
    "     minimal columns. Do not sometimes return pairs/extra columns for a\n"
    "     question about single entities.\n\n"
    "REMEMBER: When comparing categories, ALWAYS use SUM(IF(...)) in ONE flat query. "
    "NEVER use nested subqueries { SELECT ... WHERE { ... } }."
)


class PipelineStep(StrEnum):
    """Trace step identifiers for the NL-to-SPARQL translation pipeline stages."""

    CONTEXT_ASSEMBLY = "t2.vkg.context"
    LLM_CALL = "t2.vkg.translate"
    VALIDATION = "t2.vkg.validate"


class TranslationError(StrEnum):
    """Canonical error codes reported when NL-to-SPARQL translation fails."""

    TIMEOUT = "translation_timed_out"
    NO_CONTEXT = "no_relevant_ontology_context"
    LLM_EMPTY = "llm_returned_empty_response"
    VALIDATION_FAILED = "sparql_validation_failed"
    STEP_FAILED = "pipeline_step_failed"


@dataclass
class TranslationResult:
    """Result of NL-to-SPARQL translation."""

    sparql: str | None
    confidence: float
    valid: bool
    error: str | None = None
    trace_steps: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0
    tbox_context: TBoxContext | None = None
    guardrail_blocked: bool = False


# calibration adjustments (_calibrate_confidence). Conservative defaults,
# not eval-tuned — the BIRD accuracy work informs tuning (see MR notes). Bounds
# and magnitudes are deliberately small so the LLM self-rating dominates.
_CAL_VALIDATION_BOOST = 0.05
"""Boost for passing syntax + URI + domain/range validation."""
_CAL_STRONG_HIT_THRESHOLD = 0.8
"""Top vector-hit similarity at/above which retrieval is considered on-target."""
_CAL_STRONG_HIT_BOOST = 0.05
_CAL_WEAK_HIT_THRESHOLD = 0.5
"""Top vector-hit similarity below which the T-Box grounding is suspect."""
_CAL_WEAK_HIT_PENALTY = 0.1
_CAL_HIGH_COMPLEXITY = 8
"""Triple+FILTER count above which the larger complexity penalty applies."""
_CAL_HIGH_COMPLEXITY_PENALTY = 0.1
_CAL_MID_COMPLEXITY = 4
_CAL_MID_COMPLEXITY_PENALTY = 0.05


class NLtoSPARQL:
    """Translates natural language queries to SPARQL via LLM.

    Pipeline: T-Box context -> prompt assembly -> LLM call -> parse -> validate.
    """

    def __init__(
        self,
        graph_client: GraphClient,
        llm_client: LLMClient,
        guardrail_id: str = "",
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        graph_uri_template: str | None = None,
        metric_resolver=None,
        few_shot_loader=None,
    ):
        """Assemble the T-Box builder, validator, and few-shot loader for translation.

        Args:
            graph_client: Graph client used for T-Box assembly and URI validation.
            llm_client: LLM client that performs the NL-to-SPARQL translation.
            guardrail_id: Optional Bedrock guardrail applied during translation.
            timeout_s: Overall translation timeout in seconds.
            graph_uri_template: Optional named-graph URI template for the namespace.
            metric_resolver: Optional Tier-1 resolver enriching the prompt with
                full metric definitions.
            few_shot_loader: Optional loader of namespace-scoped few-shot examples;
                falls back to generic examples when absent.
        """
        # pass the Tier-1 MetricResolver to the T-Box builder so metric
        # hits are enriched with full Neptune metric definitions in the prompt.
        self._tbox_builder = TBoxContextBuilder(graph_client, graph_uri_template, metric_resolver=metric_resolver)
        self._llm = llm_client
        self._guardrail_id = guardrail_id
        self._validator = SPARQLValidator(graph_client, graph_uri_template)
        self._timeout_s = timeout_s
        # optional few-shot example loader — when present, namespace-scoped
        # examples from S3 are injected into the prompt, replacing the generic
        # hardcoded examples. None / no file → generic examples (graceful fallback).
        self._few_shot_loader = few_shot_loader

    async def translate(
        self,
        query: str,
        namespace: str,
        vector_hits: list[VectorHit] | None = None,
        vkg_error_hint: str = "",
        model_id: str | None = None,
        tbox_context: TBoxContext | None = None,
    ) -> TranslationResult:
        """Translate a natural language query to SPARQL.

        Args:
            query: Natural language query.
            namespace: Ontology namespace.
            vector_hits: Vector search hits from routing (used for T-Box assembly).
            vkg_error_hint: Optional VKG error from a prior failed attempt — appended
                to the user prompt so the LLM can rewrite to avoid the pattern.
            model_id: Optional per-call LLM model override.
            tbox_context: Pre-built T-Box context from a prior attempt. When supplied
                (e.g. on a VKG retry), the ontology is NOT re-fetched from Neptune —
                only the LLM re-prompt runs. The ontology is unchanged within a single
                request, so re-assembly is pure waste (~1.3s + Neptune round-trips).

        Returns:
            TranslationResult with SPARQL, confidence, and validation status.
        """
        try:
            result = await asyncio.wait_for(
                self._translate_inner(
                    query,
                    namespace,
                    vector_hits or [],
                    vkg_error_hint=vkg_error_hint,
                    model_id=model_id,
                    tbox_context=tbox_context,
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            result = TranslationResult(
                sparql=None,
                confidence=0.0,
                valid=False,
                error=TranslationError.TIMEOUT,
                duration_ms=int(self._timeout_s * 1000),
            )

        logger.info(
            "translation_complete",
            namespace=namespace,
            confidence=result.confidence,
            valid=result.valid,
            error=result.error,
            duration_ms=result.duration_ms,
        )
        return result

    async def _translate_inner(
        self,
        query: str,
        namespace: str,
        vector_hits: list[VectorHit],
        vkg_error_hint: str = "",
        model_id: str | None = None,
        tbox_context: TBoxContext | None = None,
    ) -> TranslationResult:
        start = time.perf_counter()
        trace_steps: list[dict[str, Any]] = []

        # Step 1: Assemble T-Box context from vector hits — unless a prior attempt
        # already built it (retry path), in which case reuse it to avoid a
        # redundant Neptune round-trip for an ontology that cannot have changed.
        if tbox_context is not None:
            tbox = tbox_context
            trace_steps.append(
                {
                    "step": PipelineStep.CONTEXT_ASSEMBLY,
                    "status": "success",
                    "durationMs": 0,
                    "detail": (f"{len(tbox.classes)} classes, {len(tbox.properties)} properties (reused)"),
                }
            )
        else:
            tbox, ok = await self._traced_step(
                PipelineStep.CONTEXT_ASSEMBLY,
                self._tbox_builder.build(vector_hits, namespace, query=query),
                trace_steps,
                start,
            )
            if not ok:
                return self._error_result(trace_steps, start)
            assert isinstance(tbox, TBoxContext)

            trace_steps[-1]["detail"] = f"{len(tbox.classes)} classes, {len(tbox.properties)} properties"

        # No classes matched — cannot generate meaningful SPARQL
        if not tbox.classes:
            return TranslationResult(
                sparql=None,
                confidence=0.0,
                valid=False,
                error=TranslationError.NO_CONTEXT,
                trace_steps=trace_steps,
                duration_ms=self._elapsed_ms(start),
                tbox_context=tbox,
            )

        # load namespace-scoped few-shot examples (best-effort; falls back
        # to the generic examples in the system prompt when none are published).
        few_shot_block = ""
        if self._few_shot_loader is not None:
            try:
                from .sparql_example_loader import FewShotExampleLoader

                examples = await self._few_shot_loader.load(namespace)
                few_shot_block = FewShotExampleLoader.format_for_prompt(examples)
            except Exception as e:
                logger.warning("few_shot_injection_skipped", error=str(e))

        # Steps 2-3: LLM call + validation with retry loop.
        # On validation failure, feed the specific error back and retry (max 2).
        max_validation_retries = 2
        validation_feedback: list[str] = []
        if vkg_error_hint:
            validation_feedback.append(vkg_error_hint)
        sparql = ""
        confidence = 0.0
        validation: ValidationResult | None = None

        for attempt in range(max_validation_retries + 1):
            # Step 2: LLM call
            user_prompt = self._build_user_prompt(query, tbox, namespace, few_shot_block)
            if validation_feedback:
                user_prompt += (
                    "\n\n⚠️ CORRECTION REQUIRED — your previous SPARQL had errors:\n"
                    + "\n".join(f"- {fb}" for fb in validation_feedback)
                    + "\n\nPlease generate a corrected SPARQL query that avoids these issues."
                )

            # `guard_content` carries the user's untrusted query. It is the
            # ONLY channel for that text — the prompt itself intentionally
            # omits the query so it isn't duplicated (mirrors the tier3
            # synthesizer pattern). With a guardrail configured, Bedrock
            # scopes the prompt-attack check to just this block; without
            # one, the text still reaches the model
            # (see `bedrock.py::_build_converse_kwargs`).
            llm_response, ok = await self._traced_step(
                PipelineStep.LLM_CALL,
                self._llm.converse(
                    prompt=user_prompt,
                    system=_SYSTEM_PROMPT,
                    guardrail_id=self._guardrail_id or None,
                    guard_content=query,
                    temperature=0,
                    # top_p=0 requests greedy decoding for determinism, but treat
                    # it as BEST-EFFORT, not a guarantee. Newer Anthropic models
                    # (e.g. claude-sonnet-5) have deprecated BOTH temperature AND
                    # top_p: bedrock.py strips each on ValidationException
                    # (_MODELS_REJECTING_TEMPERATURE / _MODELS_REJECTING_TOP_P), so
                    # on those models NEITHER sampling constraint actually reaches
                    # Bedrock. The real, model-independent B1 fix is the SEMANTIC
                    # pinning (LIMIT / COUNT(DISTINCT) / stable descriptive alias) in
                    # the prompt + sparql_validator fail-closed enforcement; the
                    # sampling params only remove avoidable variance WHEN the model
                    # accepts them. Determinism must not depend on them surviving.
                    top_p=0,
                    model_id=model_id,
                ),
                trace_steps,
                start,
            )
            if not ok:
                return self._error_result(trace_steps, start)

            if not llm_response or not llm_response.text.strip():
                trace_steps[-1]["status"] = "error"
                trace_steps[-1]["detail"] = "empty_response"
                return TranslationResult(
                    sparql=None,
                    confidence=0.0,
                    valid=False,
                    error=TranslationError.LLM_EMPTY,
                    trace_steps=trace_steps,
                    duration_ms=self._elapsed_ms(start),
                    tbox_context=tbox,
                )

            if llm_response.guardrail_blocked:
                trace_steps[-1]["status"] = "blocked"
                trace_steps[-1]["detail"] = "guardrail"
                return TranslationResult(
                    sparql=None,
                    confidence=0.0,
                    valid=False,
                    error=TranslationError.LLM_EMPTY,
                    trace_steps=trace_steps,
                    duration_ms=self._elapsed_ms(start),
                    tbox_context=tbox,
                    guardrail_blocked=True,
                )

            sparql = self._extract_sparql(llm_response.text)
            confidence = self._extract_confidence(llm_response.text)
            trace_steps[-1]["detail"] = f"confidence={confidence:.2f}"
            logger.info(
                "nl_to_sparql_raw_output",
                sparql_length=len(sparql),
                sparql_preview=sparql[:500],
                attempt=attempt + 1,
            )

            # Step 3: Validate SPARQL
            validation, ok = await self._traced_step(
                PipelineStep.VALIDATION,
                self._validator.validate(sparql, namespace),
                trace_steps,
                start,
            )
            if not ok:
                return self._error_result(trace_steps, start, sparql=sparql, tbox=tbox)

            assert isinstance(validation, ValidationResult)

            if validation.valid:
                break  # Success — exit retry loop

            # Validation failed — retry with feedback if attempts remain
            if attempt < max_validation_retries:
                validation_feedback.append(validation.error or "validation failed")
                trace_steps[-1]["status"] = "retry"
                trace_steps[-1]["detail"] = f"attempt {attempt + 1} failed: {validation.error}"
                logger.info(
                    "sparql_validation_retry",
                    attempt=attempt + 1,
                    error=validation.error,
                    namespace=namespace,
                )
            else:
                # Final attempt failed
                trace_steps[-1]["status"] = "miss"
                trace_steps[-1]["detail"] = validation.error or "failed"

        # After loop: check final validation result
        if validation and not validation.valid:
            return TranslationResult(
                sparql=sparql,
                confidence=0.0,
                valid=False,
                error=TranslationError.VALIDATION_FAILED,
                trace_steps=trace_steps,
                duration_ms=self._elapsed_ms(start),
                tbox_context=tbox,
            )

        trace_steps[-1]["detail"] = "passed"
        # refine the LLM self-reported confidence with deterministic
        # signals available here (validation outcome, vector-hit strength, query
        # complexity). This calibrated score is the value the orchestrator/
        # NL-to-SQL fallback reads to decide "below threshold → fall back to the
        # LLM-SQL path"; see _calibrate_confidence.
        calibrated = self._calibrate_confidence(
            confidence, validation_passed=True, vector_hits=vector_hits, sparql=sparql
        )
        return TranslationResult(
            sparql=sparql,
            confidence=calibrated,
            valid=True,
            trace_steps=trace_steps,
            duration_ms=self._elapsed_ms(start),
            tbox_context=tbox,
        )

    async def _traced_step(
        self,
        step_name: str,
        coro,
        trace_steps: list[dict[str, Any]],
        pipeline_start: float,
    ) -> tuple[Any, bool]:
        """Execute a coroutine and record its trace entry.

        Returns (result, success_bool).
        """
        step_start = time.perf_counter()
        try:
            result = await coro
            trace_steps.append(
                {
                    "step": step_name,
                    "status": "success",
                    "durationMs": self._elapsed_ms(step_start),
                    "detail": "",
                }
            )
            return result, True
        except Exception as e:
            # Trace keeps the compact type name (client-facing); operators get the
            # full message + step name in structured logs for production debugging.
            logger.warning(
                "pipeline_step_failed",
                step=step_name,
                error_type=type(e).__name__,
                error_msg=str(e),
            )
            trace_steps.append(
                {
                    "step": step_name,
                    "status": "error",
                    "durationMs": self._elapsed_ms(step_start),
                    "detail": type(e).__name__,
                }
            )
            return None, False

    def _error_result(
        self,
        trace_steps: list[dict[str, Any]],
        start: float,
        sparql: str | None = None,
        tbox: TBoxContext | None = None,
    ) -> TranslationResult:
        return TranslationResult(
            sparql=sparql,
            confidence=0.0,
            valid=False,
            error=TranslationError.STEP_FAILED,
            trace_steps=trace_steps,
            duration_ms=self._elapsed_ms(start),
            tbox_context=tbox,
        )

    def _elapsed_ms(self, start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    def _build_user_prompt(self, query: str, tbox: TBoxContext, namespace: str, few_shot_block: str = "") -> str:
        """Build the LLM user prompt: ontology schema (+ optional few-shot) + NL question."""
        context_text = self._tbox_builder.format_for_prompt(tbox, namespace)
        # Log the actual schema context handed to the LLM so the NL→SPARQL INPUT
        # is observable (previously only the output SPARQL was logged).
        # `has_allowed_values` flags whether any sampled enum literals reached the
        # context — the signal that lets the LLM write correct WHERE values.
        logger.info(
            "tbox_context_prompt",
            namespace=namespace,
            context_chars=len(context_text),
            has_allowed_values="allowed values:" in context_text,
            context_preview=context_text[:2000],
        )
        parts = [context_text]
        if few_shot_block:
            parts.append(few_shot_block)
        # Prompt-injection scoping via Bedrock guardContent (tier3 pattern):
        # the untrusted user query is NOT embedded in the prompt text.
        # Instead it reaches the model as a separate guardContent block on
        # the converse call at the caller (see `guard_content=query` in
        # `translate`). That block is the only channel for the untrusted
        # text and is the segment the Bedrock guardrail evaluates for
        # prompt attacks — retrieved TBox context + instructions remain
        # guardrail-exempt. Duplicating the query here would double the
        # token cost with no additional protection, since the guardrail
        # can only score the guardContent copy.
        parts.append("Generate a SPARQL query for the user question.")
        return "\n\n".join(parts)

    def _extract_sparql(self, response: str) -> str:
        """Extract SPARQL from LLM response (handles markdown code blocks)."""
        # Try ```sparql ... ``` block first
        match = re.search(r"```sparql\s*(.*?)\s*```", response, re.DOTALL)
        if match:
            return match.group(1).strip()

        # Try generic ``` ... ``` with SPARQL-like content
        match = re.search(r"```\s*(SELECT.*?)\s*```", response, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # Fallback: extract SPARQL-like block tracking brace depth
        lines = response.strip().split("\n")
        sparql_lines: list[str] = []
        capture = False
        depth = 0
        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith(("SELECT", "PREFIX", "ASK", "CONSTRUCT")):
                capture = True
            if capture:
                if not stripped and depth == 0:
                    break
                depth += stripped.count("{") - stripped.count("}")
                sparql_lines.append(line)

        return "\n".join(sparql_lines) if sparql_lines else ""

    def _extract_confidence(self, response: str) -> float:
        """Extract confidence score from LLM response."""
        match = re.search(r"confidence[:\s]*([01](?:\.\d+)?)", response, re.IGNORECASE)
        if match:
            return min(float(match.group(1)), 1.0)
        # 0.5 = "unrated" — LLM produced a query but omitted confidence
        return 0.5

    def _calibrate_confidence(
        self,
        base: float,
        *,
        validation_passed: bool,
        vector_hits: list[VectorHit] | None,
        sparql: str | None,
    ) -> float:
        """Calibrate translation confidence from deterministic signals.

        Starts from the LLM self-rating and adjusts using signals known here —
        NO extra LLM call:
        - validation: a query that PASSED syntax + URI + domain/range checks is
          more trustworthy → small boost; a fail would already have returned 0.
        - retrieval grounding: strong top vector-hit similarity means the T-Box
          context was on-target → small boost; weak/absent hits → small penalty.
        - complexity: many joins/filters are likelier to be subtly wrong →
          small penalty that scales with triple/FILTER count.

        Bounded to [0,1]. This is the score the NL-to-SQL fallback compares
        against its threshold; the magnitudes are conservative defaults, not
        eval-tuned (the BIRD accuracy work informs tuning — see MR notes).
        """
        score = base
        if validation_passed:
            score += _CAL_VALIDATION_BOOST
        # Retrieval grounding from routing's top vector-hit score.
        top = max((h.score for h in (vector_hits or []) if getattr(h, "score", None) is not None), default=None)
        if top is not None:
            if top >= _CAL_STRONG_HIT_THRESHOLD:
                score += _CAL_STRONG_HIT_BOOST
            elif top < _CAL_WEAK_HIT_THRESHOLD:
                score -= _CAL_WEAK_HIT_PENALTY
        # Complexity penalty — count triple patterns (".") and FILTERs as a proxy.
        if sparql:
            complexity = sparql.count(" .") + sparql.upper().count("FILTER")
            if complexity > _CAL_HIGH_COMPLEXITY:
                score -= _CAL_HIGH_COMPLEXITY_PENALTY
            elif complexity > _CAL_MID_COMPLEXITY:
                score -= _CAL_MID_COMPLEXITY_PENALTY
        return min(1.0, max(0.0, score))
