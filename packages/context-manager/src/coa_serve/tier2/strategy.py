# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 2 pluggable strategy architecture for structured query resolution.

Provides a Protocol-based extension point so the orchestrator can run
multiple structured-query strategies (Ontop VKG, direct NL-to-SQL, future
strategies) with configurable execution policies (sequential, parallel).

Usage::

    # Order is SEMANTIC: earlier strategies win ties (see _resolve_parallel).
    tier = StructuredQueryTier(strategies=[OntopStrategy(...), NLtoSQLStrategy(...)])
    result = await tier.resolve(query, namespace, context, option="best")
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from ..exceptions import AccessDeniedError
from ..step_ids import StepId
from ..trace import TraceCollector

if TYPE_CHECKING:
    from ..deadline import Deadline

logger = structlog.get_logger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────


class StrategyOption(StrEnum):
    """Strategy selection values — used by client (options.strategy) and internally."""

    BEST = "best"  # Parallel: run both, return highest confidence
    ONTOP = "ontop"  # Only Ontop / identifies ontop strategy
    NL_TO_SQL = "nl_to_sql"  # Only NL→SQL / identifies nl_to_sql strategy
    ONTOP_FIRST = "ontop_first"  # Sequential: Ontop → NL→SQL fallback
    NL_TO_SQL_FIRST = "nl_to_sql_first"  # Sequential: NL→SQL → Ontop fallback
    DEEP_REASONING = "deep-reasoning"  # Only the bounded tool-use agent (opt-in; never a fallback)


DEFAULT_STRATEGY = StrategyOption.NL_TO_SQL_FIRST

LEGACY_STRATEGY_ALIASES = {"agentic": StrategyOption.DEEP_REASONING}
"""Pre-rename ``options.strategy`` spellings still accepted.

``deep-reasoning`` was called ``agentic`` before the rebrand. Dropping the old
spelling would not 400 — it would fall out of
``Orchestrator._EXPLICIT_STRATEGY_OPTIONS`` and silently resolve to
``DEFAULT_STRATEGY``, running the cheap single-shot fallback chain while the caller
believed it had pinned the agent. Remove once callers are migrated.

Note ``options.mode="deep-reasoning"`` (``coa_serve.mode``) is a DIFFERENT knob on a
different axis: it replaces the whole T1→T2→T3 cascade with the Tier-3 reasoning
loop, whereas this selects which Tier-2 engine answers a structured query. Both were
called "agentic" before the rebrand and both are now "deep reasoning"; ``mode``
versus ``strategy`` is what distinguishes them.
"""


def normalize_strategy_option(value: object) -> object:
    """Map a pre-rename ``options.strategy`` spelling onto its current member.

    Returns ``value`` unchanged when it is not a known legacy spelling, including
    for non-string input — ``options`` is caller-controlled, so an unhashable value
    must not raise here.
    """
    if isinstance(value, str):
        return LEGACY_STRATEGY_ALIASES.get(value, value)
    return value


# ── Data classes ─────────────────────────────────────────────────────────


@dataclass
class StrategyResult:
    """Unified result from any Tier 2 strategy."""

    sql: str
    rows: list[dict]
    columns: list[str]
    confidence: float
    strategy_name: str  # StrategyOption value: "ontop" | "nl_to_sql"
    trace_steps: list[dict]
    ontop_assembly: bool = False  # True = use ontop assembler (includes sparql, ontology_version)
    data_source_id: str = ""
    row_count: int = 0
    truncated: bool = False
    sparql: str = ""  # SPARQL generated (ontop path); empty for NL→SQL
    # Version of the ontology the VKG executed against (owl:versionInfo, read
    # off the VKG translate response). Empty when the strategy has no VKG leg
    # or the service could not name one (#986).
    ontology_version: str = ""
    retrieved_tables: list[str] = field(default_factory=list)
    expanded_tables: list[str] = field(default_factory=list)


@dataclass
class StrategyContext:
    """Shared context passed to all strategies during resolution."""

    embedding: list[float] | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    trace: TraceCollector = field(default_factory=TraceCollector)
    model_id: str | None = None
    # Request-scoped time budget. None means "no deadline threaded" — strategies
    # then fall back to their fixed internal timeouts (pre-A0 behaviour), so an
    # unwired call site degrades safely instead of erroring.
    deadline: Deadline | None = None


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class StructuredQueryStrategy(Protocol):
    """Protocol for a pluggable Tier 2 strategy."""

    name: str

    async def resolve(self, query: str, namespace: str, context: StrategyContext) -> StrategyResult | None:
        """Resolve a query with this strategy, returning a result or None on miss."""
        ...


# ── Strategy runner ──────────────────────────────────────────────────────


_PARALLEL_STRATEGY_TIMEOUT_S = 90

# Ceiling on rows returned to a caller. Raised from 1000 once
# AthenaClient._get_results learned to follow NextToken across pages — before
# that, any value above ~1000 was inert because the fetch read a single page and
# truncated at 999 rows regardless.
#
# Both halves must move together: `_inject_limit` pushes this value into the SQL
# as a LIMIT (bounding the server-side scan), while `_get_results` pages up to
# it. Raising one without the other either truncates silently (old behaviour) or
# scans more than it returns.
#
# 10_000 rather than 100_000 deliberately: the result set is serialized as JSON
# across the Context Manager -> MCP -> client boundary, and there is no payload
# guard in this path. 10k rows keeps a typical response in the low single-digit
# MB while covering the long tail of legitimate analytical answers. Callers
# needing more should page, or read the Athena result file from S3 directly.
MAX_RESULT_ROWS = 10_000

# A strategy result with zero rows is accepted as a truthful "no data" answer
# ONLY when the strategy was confident in its query. Below this confidence, an
# empty result is more likely a bad query than a genuinely empty answer, so we
# fall through to the next strategy (and ultimately Tier 3) instead of stopping.
EMPTY_RESULT_CONFIDENCE_FLOOR = 0.3


def capped_max_rows(options: dict[str, Any]) -> int:
    """Cap maxResults to the system maximum."""
    return min(options.get("maxResults", MAX_RESULT_ROWS), MAX_RESULT_ROWS)


# Upper bound on user-provided evidence/hint text folded into a generation prompt.
# Keeps token spend + prompt-injection surface bounded regardless of what the
# caller passes. Overridable via SERVE_EVIDENCE_MAX_CHARS for hard datasets.
_DEFAULT_EVIDENCE_MAX_CHARS = 500


def capped_evidence(evidence: str) -> str:
    """Truncate user-provided evidence to the configured character cap."""
    try:
        limit = max(0, int(os.environ.get("SERVE_EVIDENCE_MAX_CHARS", str(_DEFAULT_EVIDENCE_MAX_CHARS))))
    except (TypeError, ValueError):
        limit = _DEFAULT_EVIDENCE_MAX_CHARS
    return (evidence or "")[:limit]


# Empirically-ordered strategy precedence for parallel ("best") selection.
# LOWER number wins. Ordered by measured reliability on the 22-query TPC-H
# benchmark, NOT by architectural authority:
#     nl_to_sql  mean_f1 0.590, execution_rate 1.00
#     ontop      mean_f1 0.055, execution_rate 0.41
# Revisit this table when the VKG path improves — it is a statement about the
# current implementation, not about which approach is better in principle.
_STRATEGY_PRECEDENCE: dict[str, int] = {
    StrategyOption.NL_TO_SQL: 0,
    StrategyOption.ONTOP: 1,
}


def _strategy_precedence(strategy_name: str) -> int:
    """Rank a strategy for deterministic tie-breaking (lower wins)."""
    return _STRATEGY_PRECEDENCE.get(strategy_name, len(_STRATEGY_PRECEDENCE))


# Ontop emits this when the SPARQL matches no R2RML mapping: a syntactically
# valid query carrying no data. It was returned with confidence 0.73 in
# benchmarking, so neither the row count nor the confidence flags it.
_DEGENERATE_SQL_MARKERS = ("uselessVariable",)


def is_degenerate_result(result: StrategyResult) -> bool:
    """True when a strategy returned a structurally empty no-mapping query.

    Distinct from `is_low_confidence_empty`: this fires regardless of confidence,
    because the SQL itself proves nothing was resolved.
    """
    sql = result.sql or ""
    return any(marker in sql for marker in _DEGENERATE_SQL_MARKERS)


def is_low_confidence_empty(result: StrategyResult) -> bool:
    """True when a result returned no rows AND the strategy had low confidence.

    Such a result is treated as a miss (fall through to the next strategy /
    Tier 3) rather than a final answer, since a low-confidence query that
    returns nothing is more likely wrong than genuinely empty. A confident
    empty result (>= floor) is kept as a valid "no data found" answer.
    """
    return result.row_count == 0 and result.confidence < EMPTY_RESULT_CONFIDENCE_FLOOR


def _failed_step_id(strategy_name: str) -> str:
    """Trace step ID for an abandoned strategy, namespaced by its family."""
    family = "sql" if strategy_name == StrategyOption.NL_TO_SQL else "vkg"
    return f"t2.{family}.failed"


class StructuredQueryTier:
    """Runs registered strategies with sequential fallback or parallel execution.

    Constructed once with the full strategy list. Call ``resolve()`` with an
    option to control per-request behavior (which strategies, sequential vs parallel).
    """

    def __init__(self, strategies: list[StructuredQueryStrategy]):
        """Store the ordered strategy list used for fallback/parallel resolution.

        Args:
            strategies: Registered Tier-2 strategies, in fallback priority order.
        """
        self._strategies = list(strategies)

    async def resolve(
        self,
        query: str,
        namespace: str,
        context: StrategyContext,
        *,
        option: str | StrategyOption = DEFAULT_STRATEGY,
    ) -> StrategyResult | None:
        """Execute strategies for the given option. Returns first successful result, or None."""
        strategies = self._strategies_for(option)
        if option == StrategyOption.BEST:
            return await self._resolve_parallel(query, namespace, context, strategies)
        return await self._resolve_sequential(query, namespace, context, strategies)

    def _strategies_for(self, option: str) -> list[StructuredQueryStrategy]:
        """Select and order strategies based on the option."""
        # Normalize here as well as in the orchestrator: a direct caller (test,
        # benchmark harness) reaching resolve(option=...) with the pre-rename
        # spelling must still get the agent, not the fallback chain.
        option = normalize_strategy_option(option)
        if option == StrategyOption.DEEP_REASONING:
            # Opt-in only: the agent runs solely when explicitly pinned, never as a
            # fallback in any of the sequential/parallel paths below.
            return [s for s in self._strategies if s.name == StrategyOption.DEEP_REASONING]
        if option == StrategyOption.ONTOP:
            return [s for s in self._strategies if s.name == StrategyOption.ONTOP]
        if option == StrategyOption.NL_TO_SQL:
            return [s for s in self._strategies if s.name == StrategyOption.NL_TO_SQL]
        # DEEP_REASONING is never part of a fallback chain — it is an expensive tool-use
        # loop that must not fire implicitly when a cheaper strategy misses.
        fallback = [s for s in self._strategies if s.name != StrategyOption.DEEP_REASONING]
        if option == StrategyOption.ONTOP_FIRST:
            # Stable sort: matching strategy moves to front; others keep insertion order.
            return sorted(fallback, key=lambda s: s.name != StrategyOption.ONTOP)
        if option == StrategyOption.NL_TO_SQL_FIRST:
            return sorted(fallback, key=lambda s: s.name != StrategyOption.NL_TO_SQL)
        # "best" or unrecognized — use all in configured order
        if option != StrategyOption.BEST:
            logger.debug("strategy_option_passthrough", option=option)
        return fallback

    async def _resolve_sequential(
        self, query: str, namespace: str, context: StrategyContext, strategies: list[StructuredQueryStrategy]
    ) -> StrategyResult | None:
        attempted: list[str] = []
        for strategy in strategies:
            attempted.append(strategy.name)
            try:
                result = await strategy.resolve(query, namespace, context)
                if result is not None:
                    # Confidence-gated empty result (option C): a zero-row result
                    # from a low-confidence query is more likely a bad query than
                    # a true empty answer — fall through to the next strategy /
                    # Tier 3 rather than returning it as the final answer.
                    if is_low_confidence_empty(result):
                        logger.info(
                            "strategy_low_confidence_empty",
                            strategy=strategy.name,
                            confidence=result.confidence,
                            attempted=attempted,
                        )
                        context.trace.record(
                            _failed_step_id(strategy.name),
                            "failed",
                            0,
                            detail={
                                "strategy": strategy.name,
                                "reason": "empty_low_confidence",
                                "confidence": result.confidence,
                            },
                        )
                        continue
                    logger.info(
                        "strategy_resolved",
                        strategy=strategy.name,
                        confidence=result.confidence,
                        row_count=result.row_count,
                        attempted=attempted,
                    )
                    return result
            except Exception as e:
                logger.warning(
                    "strategy_failed",
                    strategy=strategy.name,
                    error=type(e).__name__,
                    message=str(e)[:200],
                )
                if isinstance(e, AccessDeniedError):
                    raise
                # Record a visible step so the trace explains why this strategy
                # was abandoned and the next one tried (avoids silent transitions
                # when a strategy raises before recording its own failure step).
                context.trace.record(
                    _failed_step_id(strategy.name),
                    "failed",
                    0,
                    detail={"strategy": strategy.name, "error": type(e).__name__, "message": str(e)[:200]},
                )
                continue
        logger.info("strategy_all_failed", attempted=attempted)
        return None

    async def _resolve_parallel(
        self, query: str, namespace: str, context: StrategyContext, strategies: list[StructuredQueryStrategy]
    ) -> StrategyResult | None:
        # Each parallel strategy gets isolated context to avoid shared-state races.
        per_strategy_contexts = [
            StrategyContext(
                embedding=context.embedding,
                profile=dict(context.profile),
                options=dict(context.options),
                trace=TraceCollector(),
                model_id=context.model_id,
                deadline=context.deadline,
            )
            for _ in strategies
        ]

        async def _run_with_timeout(strategy: StructuredQueryStrategy, ctx: StrategyContext) -> StrategyResult | None:
            return await asyncio.wait_for(strategy.resolve(query, namespace, ctx), timeout=_PARALLEL_STRATEGY_TIMEOUT_S)

        results = await asyncio.gather(
            *[_run_with_timeout(s, ctx) for s, ctx in zip(strategies, per_strategy_contexts, strict=True)],
            return_exceptions=True,
        )

        # Re-raise terminal exceptions (AccessDenied, cancellation, system exit)
        for r in results:
            if isinstance(r, (AccessDeniedError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise r
            if isinstance(r, BaseException) and not isinstance(r, Exception):
                raise r

        # Log any strategy errors (timeouts, exceptions) for observability
        errors = [
            (s.name, type(r).__name__, str(r)[:100])
            for s, r in zip(strategies, results, strict=True)
            if isinstance(r, Exception)
        ]
        if errors:
            logger.warning(
                "tier2_parallel_errors",
                errors=[{"strategy": n, "error": t, "message": m} for n, t, m in errors],
            )

        valid_with_ctx = [
            (r, ctx) for r, ctx in zip(results, per_strategy_contexts, strict=True) if isinstance(r, StrategyResult)
        ]
        # Confidence-gated empty result (option C): prefer results that aren't a
        # low-confidence empty. If every candidate is one, fall through to Tier 3
        # rather than returning a likely-wrong empty answer.
        acceptable = [
            (r, ctx) for r, ctx in valid_with_ctx if not is_low_confidence_empty(r) and not is_degenerate_result(r)
        ]
        if not acceptable:
            if valid_with_ctx:
                logger.info(
                    "tier2_parallel_all_low_confidence_empty",
                    candidates=[r.strategy_name for r, _ in valid_with_ctx],
                )
                # Record a visible failure step per abandoned candidate so the
                # trace explains why Tier 2 fell through (mirrors the sequential
                # path); the per-strategy isolated traces are otherwise dropped.
                for r, _ in valid_with_ctx:
                    context.trace.record(
                        _failed_step_id(r.strategy_name),
                        "failed",
                        0,
                        detail={
                            "strategy": r.strategy_name,
                            "reason": "empty_low_confidence",
                            "confidence": r.confidence,
                        },
                    )
            return None
        # Pick by DETERMINISTIC precedence, not by LLM self-reported confidence.
        #
        # Previously: `acceptable.sort(key=lambda p: p[0].confidence, reverse=True)`.
        # Strategy confidences are self-assessments produced by the model, so two
        # strategies landing within noise of each other swapped the winner between
        # otherwise-identical requests — and with it the SQL and the answer. In
        # benchmarking this showed up as per-question F1 flipping 0 <-> 1 across
        # identical runs (mean spread 0.037), which exceeded most real effect sizes
        # and made A/B comparison unreliable.
        #
        # Confidence is still USED, but only as an admission floor
        # (`is_low_confidence_empty` above), never as a ranking key.
        #
        # Precedence is EMPIRICAL, not architectural. An earlier revision of this
        # code ranked by the caller's declared list order on the assumption that an
        # ontology-grounded answer outranks a free-form one. Measurement on the 22
        # TPC-H queries contradicted that:
        #
        #     strategy=ontop      mean_f1 0.055   execution_rate 0.41
        #     strategy=nl_to_sql  mean_f1 0.590   execution_rate 1.00
        #     strategy=best       mean_f1 0.498   execution_rate 1.00
        #
        # `best` scored BELOW nl_to_sql alone because parallel selection kept
        # Ontop's answer on q01/q07/q19. Ontop also reports high confidence on
        # useless output (0.85 on failures; 0.73 on a degenerate
        # "SELECT 1 AS uselessVariable" no-mapping result), so neither its own
        # confidence nor its notional authority is a safe ranking signal today.
        # Until the VKG path improves, prefer the strategy that measurably works.
        acceptable.sort(key=lambda pair: (_strategy_precedence(pair[0].strategy_name), -pair[0].confidence))
        winner, winner_ctx = acceptable[0]
        logger.info(
            "tier2_parallel_winner",
            strategy=winner.strategy_name,
            confidence=winner.confidence,
            selected_by="precedence",
            candidates=[{"strategy": r.strategy_name, "confidence": r.confidence} for r, _ in acceptable],
        )
        # Merge winner's trace steps into caller's trace (via record() so on_record callback fires for UI)
        for step in winner_ctx.trace.steps:
            context.trace.record(
                step.step,
                step.status,
                step.duration_ms,
                detail=step.detail,
                tool_used=step.tool_used,
                parallel_group=step.parallel_group,
                wall_ms=step.wall_ms,
            )
        # Emit a synthetic step so the UI knows which strategy was chosen (E9)
        context.trace.record(StepId.T2_STRATEGY_SELECTED, "success", 0, detail={"strategy": winner.strategy_name})
        # Log all candidates for observability (winner + losers)
        logger.info(
            "tier2_parallel_resolved",
            winner=winner.strategy_name,
            winner_confidence=winner.confidence,
            candidates=[
                {"strategy": r.strategy_name, "confidence": r.confidence, "row_count": r.row_count}
                for r, _ in valid_with_ctx
            ],
        )
        return winner
