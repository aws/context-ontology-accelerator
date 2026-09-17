# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Tier 2 pluggable strategy architecture.

Tests cover:
1. StructuredQueryTier sequential policy — first success wins, failures skip
2. StructuredQueryTier parallel policy — highest confidence wins
3. AccessDeniedError propagation in both policies
4. StrategyContext data flow
5. filter_strategies helper
6. OrchestratorStrategy selection mapping (tierOverride/options.strategy)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.exceptions import AccessDeniedError, NoResultError
from coa_serve.orchestrator import Orchestrator
from coa_serve.tier2.strategy import (
    LEGACY_STRATEGY_ALIASES,
    StrategyContext,
    StrategyOption,
    StrategyResult,
    StructuredQueryTier,
    normalize_strategy_option,
)
from coa_serve.trace import TraceCollector

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_strategy(name: str, result: StrategyResult | None = None, error: Exception | None = None):
    """Create a mock strategy with a given name and behavior."""
    strategy = MagicMock()
    strategy.name = name
    if error:
        strategy.resolve = AsyncMock(side_effect=error)
    else:
        strategy.resolve = AsyncMock(return_value=result)
    return strategy


def _make_result(strategy_name: str = "test", confidence: float = 0.8, rows: list | None = None) -> StrategyResult:
    return StrategyResult(
        sql="SELECT 1",
        rows=rows or [{"id": 1}],
        columns=["id"],
        confidence=confidence,
        strategy_name=strategy_name,
        trace_steps=[],
        data_source_id="ds-1",
        row_count=len(rows) if rows else 1,
        truncated=False,
    )


def _make_empty_result(strategy_name: str = "test", confidence: float = 0.8) -> StrategyResult:
    """A result that executed cleanly but returned zero rows."""
    return StrategyResult(
        sql="SELECT 1",
        rows=[],
        columns=["id"],
        confidence=confidence,
        strategy_name=strategy_name,
        trace_steps=[],
        data_source_id="ds-1",
        row_count=0,
        truncated=False,
    )


# ── Sequential policy ───────────────────────────────────────────────────


@pytest.mark.unit
class TestSequentialPolicy:
    @pytest.mark.asyncio
    async def test_first_success_wins(self):
        s1 = _make_strategy("a", result=_make_result("a", confidence=0.7))
        s2 = _make_strategy("b", result=_make_result("b", confidence=0.9))
        tier = StructuredQueryTier(
            strategies=[s1, s2],
        )

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx)

        assert result is not None
        assert result.strategy_name == "a"
        # Second strategy never called
        s2.resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_none_results(self):
        s1 = _make_strategy("a", result=None)
        s2 = _make_strategy("b", result=_make_result("b"))
        tier = StructuredQueryTier(
            strategies=[s1, s2],
        )

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx)

        assert result is not None
        assert result.strategy_name == "b"

    @pytest.mark.asyncio
    async def test_skips_exceptions(self):
        s1 = _make_strategy("a", error=RuntimeError("oops"))
        s2 = _make_strategy("b", result=_make_result("b"))
        tier = StructuredQueryTier(
            strategies=[s1, s2],
        )

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx)

        assert result is not None
        assert result.strategy_name == "b"

    @pytest.mark.asyncio
    async def test_all_fail_returns_none(self):
        s1 = _make_strategy("a", result=None)
        s2 = _make_strategy("b", error=RuntimeError("fail"))
        tier = StructuredQueryTier(
            strategies=[s1, s2],
        )

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx)

        assert result is None

    @pytest.mark.asyncio
    async def test_low_confidence_empty_falls_through(self):
        """A zero-row, low-confidence result is skipped in favor of the next strategy."""
        empty_low = _make_empty_result("a", confidence=0.05)
        s1 = _make_strategy("a", result=empty_low)
        s2 = _make_strategy("b", result=_make_result("b", confidence=0.9))
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx)

        # Falls through to the second strategy rather than returning the empty result.
        assert result is not None
        assert result.strategy_name == "b"
        s2.resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confident_empty_is_accepted(self):
        """A zero-row result with confidence >= floor is a valid 'no data' answer."""
        empty_confident = _make_empty_result("a", confidence=0.8)
        s1 = _make_strategy("a", result=empty_confident)
        s2 = _make_strategy("b", result=_make_result("b", confidence=0.9))
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx)

        # Confident empty result stands; the next strategy is never tried.
        assert result is not None
        assert result.strategy_name == "a"
        assert result.row_count == 0
        s2.resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_low_confidence_empty_returns_none(self):
        """When every strategy yields a low-confidence empty, fall through entirely (→ Tier 3)."""
        s1 = _make_strategy("a", result=_make_empty_result("a", confidence=0.1))
        s2 = _make_strategy("b", result=_make_empty_result("b", confidence=0.05))
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx)

        assert result is None

    @pytest.mark.asyncio
    async def test_access_denied_propagates(self):
        s1 = _make_strategy("a", error=AccessDeniedError("denied"))
        s2 = _make_strategy("b", result=_make_result("b"))
        tier = StructuredQueryTier(
            strategies=[s1, s2],
        )

        ctx = StrategyContext(trace=TraceCollector())
        with pytest.raises(AccessDeniedError):
            await tier.resolve("query", "ns", ctx)

        # Second strategy never attempted after terminal deny
        s2.resolve.assert_not_awaited()


# ── Parallel policy ──────────────────────────────────────────────────────


@pytest.mark.unit
class TestParallelPolicy:
    @pytest.mark.asyncio
    async def test_precedence_order_wins_not_confidence(self):
        """Declared strategy order decides the winner, NOT self-reported confidence.

        Behaviour change (was `test_highest_confidence_wins`): selection used to
        sort on StrategyResult.confidence, which is a model self-assessment. Two
        strategies scoring within noise of each other swapped the winner between
        identical requests, changing the SQL and the answer — measured as
        per-question F1 flipping 0<->1 across identical benchmark runs. Order is
        stable and encodes real authority (governed metric > VKG > free NL->SQL).
        """
        ontop = _make_strategy("ontop", result=_make_result("ontop", confidence=0.9))
        nl = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql", confidence=0.6))
        tier = StructuredQueryTier(strategies=[ontop, nl])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx, option="best")

        assert result is not None
        # nl_to_sql outranks ontop on MEASURED reliability (0.590 vs 0.055 mean
        # F1), so it wins despite lower self-reported confidence and despite
        # ontop being declared first.
        assert result.strategy_name == "nl_to_sql"
        assert result.confidence == 0.6

    @pytest.mark.asyncio
    async def test_selection_is_stable_across_repeated_runs(self):
        """Same inputs must yield the same winner every time."""
        winners = []
        for _ in range(10):
            o = _make_strategy("ontop", result=_make_result("ontop", confidence=0.5001))
            n = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql", confidence=0.5))
            tier = StructuredQueryTier(strategies=[o, n])
            res = await tier.resolve("query", "ns", StrategyContext(trace=TraceCollector()), option="best")
            winners.append(res.strategy_name)
        assert set(winners) == {"nl_to_sql"}, f"winner flipped across runs: {set(winners)}"

    @pytest.mark.asyncio
    async def test_near_tie_confidence_does_not_flip_winner(self):
        """A confidence delta inside model noise must not change the outcome."""
        for conf_ontop in (0.49, 0.50, 0.51, 0.99):
            o = _make_strategy("ontop", result=_make_result("ontop", confidence=conf_ontop))
            n = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql", confidence=0.50))
            tier = StructuredQueryTier(strategies=[o, n])
            res = await tier.resolve("query", "ns", StrategyContext(trace=TraceCollector()), option="best")
            assert res.strategy_name == "nl_to_sql", f"precedence lost to confidence={conf_ontop}"

    @pytest.mark.asyncio
    async def test_confidence_still_gates_empty_results(self):
        """Confidence remains an admission FLOOR even though it no longer ranks.

        A low-confidence empty result from the first-precedence strategy must not
        win just because it is first; it is inadmissible, so the next strategy's
        real rows win.
        """
        n = _make_strategy("nl_to_sql", result=_make_empty_result("nl_to_sql", confidence=0.1))
        o = _make_strategy("ontop", result=_make_result("ontop", confidence=0.4))
        tier = StructuredQueryTier(strategies=[n, o])

        res = await tier.resolve("query", "ns", StrategyContext(trace=TraceCollector()), option="best")
        assert res is not None
        # nl_to_sql has higher precedence but its empty result is inadmissible.
        assert res.strategy_name == "ontop"

    @pytest.mark.asyncio
    async def test_degenerate_no_mapping_result_is_rejected(self):
        """Ontop's no-mapping "SELECT 1 AS uselessVariable" must never win.

        It is syntactically valid, returns a row, and was observed carrying
        confidence 0.73 — so neither the row count nor the confidence floor
        catches it. Only the SQL shape does.
        """
        degenerate = _make_result("ontop", confidence=0.95)
        degenerate.sql = "SELECT 1 AS uselessVariable FROM (SELECT 1) AS tdummy"
        o = _make_strategy("ontop", result=degenerate)
        n = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql", confidence=0.3))
        tier = StructuredQueryTier(strategies=[o, n])

        res = await tier.resolve("query", "ns", StrategyContext(trace=TraceCollector()), option="best")
        assert res is not None
        assert res.strategy_name == "nl_to_sql"

    @pytest.mark.asyncio
    async def test_all_degenerate_falls_through_to_tier3(self):
        degenerate = _make_result("ontop", confidence=0.9)
        degenerate.sql = "SELECT 1 AS uselessVariable FROM (SELECT 1) AS tdummy"
        o = _make_strategy("ontop", result=degenerate)
        tier = StructuredQueryTier(strategies=[o])

        res = await tier.resolve("query", "ns", StrategyContext(trace=TraceCollector()), option="best")
        assert res is None

    @pytest.mark.asyncio
    async def test_ignores_exceptions(self):
        s1 = _make_strategy("a", error=RuntimeError("oops"))
        s2 = _make_strategy("b", result=_make_result("b"))
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx, option="best")

        assert result is not None
        assert result.strategy_name == "b"

    @pytest.mark.asyncio
    async def test_all_fail_returns_none(self):
        s1 = _make_strategy("a", error=RuntimeError("fail"))
        s2 = _make_strategy("b", result=None)
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx, option="best")

        assert result is None

    @pytest.mark.asyncio
    async def test_access_denied_propagates(self):
        s1 = _make_strategy("a", error=AccessDeniedError("denied"))
        s2 = _make_strategy("b", result=_make_result("b"))
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(trace=TraceCollector())
        with pytest.raises(AccessDeniedError):
            await tier.resolve("query", "ns", ctx, option="best")

    @pytest.mark.asyncio
    async def test_prefers_rows_over_low_confidence_empty(self):
        """A LOW-confidence empty (below the floor) is filtered out, so a result
        that actually returned rows wins even at lower confidence. (A confident
        empty is a valid 'no data' answer and is covered separately.)"""
        empty_low = _make_empty_result("a", confidence=0.1)
        rows_mid = _make_result("b", confidence=0.4)
        tier = StructuredQueryTier(
            strategies=[_make_strategy("a", result=empty_low), _make_strategy("b", result=rows_mid)]
        )

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx, option="best")

        assert result is not None
        assert result.strategy_name == "b"

    @pytest.mark.asyncio
    async def test_all_low_confidence_empty_returns_none(self):
        """When every parallel candidate is a low-confidence empty, fall through
        (return None) so the orchestrator can try Tier 3."""
        s1 = _make_strategy("a", result=_make_empty_result("a", confidence=0.1))
        s2 = _make_strategy("b", result=_make_empty_result("b", confidence=0.05))
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx, option="best")

        assert result is None

    @pytest.mark.asyncio
    async def test_confident_empty_accepted_in_parallel(self):
        """A confident empty result is a valid 'no data' answer even in parallel mode."""
        s1 = _make_strategy("a", result=_make_empty_result("a", confidence=0.85))
        tier = StructuredQueryTier(strategies=[s1])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("query", "ns", ctx, option="best")

        assert result is not None
        assert result.strategy_name == "a"
        assert result.row_count == 0

    @pytest.mark.asyncio
    async def test_winner_trace_steps_merged_into_caller(self):
        """The winning parallel strategy's trace steps are replayed into the
        caller's trace (with parallel_group/wall_ms preserved)."""
        winner = _make_result("ontop", confidence=0.9)

        # The strategy records steps into ITS OWN context.trace during resolve;
        # simulate that by having the mock populate the per-strategy trace.
        async def _resolve_with_steps(_q, _ns, ctx):
            ctx.trace.record("t2.vkg.translate", "success", 200, detail="confidence=0.9")
            ctx.trace.record("t2.vkg.execute", "success", 120, detail={"rowCount": 1})
            return winner

        ontop = _make_strategy("ontop")
        ontop.resolve = AsyncMock(side_effect=_resolve_with_steps)
        tier = StructuredQueryTier(strategies=[ontop])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="best")

        assert result is not None
        merged = {s.step for s in ctx.trace.steps}
        assert "t2.vkg.translate" in merged
        assert "t2.vkg.execute" in merged
        # The synthetic strategy-selected marker is also emitted.
        assert "t2.strategy_selected" in merged


# ── Strategy selection via option ─────────────────────────────────────────


@pytest.mark.unit
class TestStrategySelection:
    def test_option_ontop_selects_only_ontop(self):
        s1 = _make_strategy("ontop", result=_make_result("ontop"))
        s2 = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql"))
        tier = StructuredQueryTier(strategies=[s1, s2])

        selected = tier._strategies_for("ontop")
        assert len(selected) == 1
        assert selected[0].name == "ontop"

    def test_option_nl_to_sql_first_reorders(self):
        s1 = _make_strategy("ontop")
        s2 = _make_strategy("nl_to_sql")
        tier = StructuredQueryTier(strategies=[s1, s2])

        selected = tier._strategies_for("nl_to_sql_first")
        assert [s.name for s in selected] == ["nl_to_sql", "ontop"]

    def test_option_default_preserves_order(self):
        s1 = _make_strategy("ontop")
        s2 = _make_strategy("nl_to_sql")
        tier = StructuredQueryTier(strategies=[s1, s2])

        selected = tier._strategies_for("_default")
        assert [s.name for s in selected] == ["ontop", "nl_to_sql"]

    def test_option_ontop_first_reorders(self):
        s1 = _make_strategy("nl_to_sql")
        s2 = _make_strategy("ontop")
        tier = StructuredQueryTier(strategies=[s1, s2])

        selected = tier._strategies_for("ontop_first")
        assert [s.name for s in selected] == ["ontop", "nl_to_sql"]

    def test_option_nl_to_sql_selects_only_nl_to_sql(self):
        s1 = _make_strategy("ontop")
        s2 = _make_strategy("nl_to_sql")
        tier = StructuredQueryTier(strategies=[s1, s2])

        selected = tier._strategies_for("nl_to_sql")
        assert [s.name for s in selected] == ["nl_to_sql"]

    def test_option_best_keeps_all_strategies(self):
        s1 = _make_strategy("ontop")
        s2 = _make_strategy("nl_to_sql")
        tier = StructuredQueryTier(strategies=[s1, s2])

        selected = tier._strategies_for("best")
        assert {s.name for s in selected} == {"ontop", "nl_to_sql"}

    def test_unknown_strategy_returns_empty_list(self):
        s1 = _make_strategy("ontop")
        tier = StructuredQueryTier(strategies=[s1])

        selected = tier._strategies_for("nl_to_sql")
        assert len(selected) == 0


# ── End-to-end execution per strategy option ─────────────────────────────


@pytest.mark.unit
class TestStrategyExecutionByOption:
    """Verify each option drives the right strategies end-to-end (selection +
    sequential/parallel execution), using realistic two-strategy registries."""

    @pytest.mark.asyncio
    async def test_nl_to_sql_first_uses_nl_to_sql_when_it_succeeds(self):
        nl = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql", confidence=0.6))
        ontop = _make_strategy("ontop", result=_make_result("ontop", confidence=0.99))
        tier = StructuredQueryTier(strategies=[ontop, nl])  # registry order shouldn't matter

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="nl_to_sql_first")

        assert result.strategy_name == "nl_to_sql"
        ontop.resolve.assert_not_awaited()  # never reached — nl_to_sql won first

    @pytest.mark.asyncio
    async def test_nl_to_sql_first_falls_back_to_ontop(self):
        nl = _make_strategy("nl_to_sql", result=None)  # nl_to_sql misses
        ontop = _make_strategy("ontop", result=_make_result("ontop", confidence=0.7))
        tier = StructuredQueryTier(strategies=[nl, ontop])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="nl_to_sql_first")

        assert result.strategy_name == "ontop"
        nl.resolve.assert_awaited_once()
        ontop.resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ontop_first_uses_ontop_when_it_succeeds(self):
        nl = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql"))
        ontop = _make_strategy("ontop", result=_make_result("ontop"))
        tier = StructuredQueryTier(strategies=[nl, ontop])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="ontop_first")

        assert result.strategy_name == "ontop"
        nl.resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ontop_first_falls_back_to_nl_to_sql(self):
        nl = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql"))
        ontop = _make_strategy("ontop", result=None)  # ontop misses
        tier = StructuredQueryTier(strategies=[nl, ontop])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="ontop_first")

        assert result.strategy_name == "nl_to_sql"

    @pytest.mark.asyncio
    async def test_ontop_only_never_runs_nl_to_sql(self):
        nl = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql"))
        ontop = _make_strategy("ontop", result=None)  # ontop misses → no fallback
        tier = StructuredQueryTier(strategies=[nl, ontop])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="ontop")

        assert result is None  # pinned to ontop, which missed
        nl.resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_nl_to_sql_only_never_runs_ontop(self):
        nl = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql"))
        ontop = _make_strategy("ontop", result=_make_result("ontop"))
        tier = StructuredQueryTier(strategies=[nl, ontop])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="nl_to_sql")

        assert result.strategy_name == "nl_to_sql"
        ontop.resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_best_runs_both_and_precedence_wins(self):
        """`best` runs both strategies; the earlier-declared one wins deterministically."""
        nl = _make_strategy("nl_to_sql", result=_make_result("nl_to_sql", confidence=0.6))
        ontop = _make_strategy("ontop", result=_make_result("ontop", confidence=0.9))
        tier = StructuredQueryTier(strategies=[nl, ontop])

        ctx = StrategyContext(trace=TraceCollector())
        result = await tier.resolve("q", "ns", ctx, option="best")

        # Declared first wins regardless of the higher-confidence alternative.
        assert result.strategy_name == "nl_to_sql"
        # Parallel: both strategies are invoked.
        nl.resolve.assert_awaited_once()
        ontop.resolve.assert_awaited_once()


# ── StrategyContext data flow ────────────────────────────────────────────


@pytest.mark.unit
class TestStrategyContext:
    @pytest.mark.asyncio
    async def test_context_passed_to_strategies(self):
        s1 = _make_strategy("a", result=_make_result("a"))
        tier = StructuredQueryTier(
            strategies=[s1],
        )

        ctx = StrategyContext(
            embedding=[0.1, 0.2],
            profile={"userId": "test-user"},
            options={"maxResults": 500},
            trace=TraceCollector(),
        )
        await tier.resolve("test query", "demo-ns", ctx)

        call_args = s1.resolve.call_args
        assert call_args.args == ("test query", "demo-ns", ctx)


# ── Orchestrator strategy selection mapping ──────────────────────────────


@pytest.mark.unit
class TestResolveStrategySelection:
    def test_explicit_strategy_option(self):
        assert Orchestrator._resolve_strategy_selection({"strategy": "ontop"}, None) == "ontop"
        assert Orchestrator._resolve_strategy_selection({"strategy": "nl_to_sql"}, None) == "nl_to_sql"
        assert Orchestrator._resolve_strategy_selection({"strategy": "best"}, None) == "best"
        assert Orchestrator._resolve_strategy_selection({"strategy": "ontop_first"}, None) == "ontop_first"
        assert Orchestrator._resolve_strategy_selection({"strategy": "nl_to_sql_first"}, None) == "nl_to_sql_first"
        assert Orchestrator._resolve_strategy_selection({"strategy": "deep-reasoning"}, None) == "deep-reasoning"

    def test_deprecated_agentic_pin_resolves_to_deep_reasoning(self):
        """The pre-rename spelling must still resolve to an explicit pin.

        This is the silent-downgrade guard. Unlike ``options.mode``, an unknown
        ``options.strategy`` does NOT 400 — it drops out of
        ``_EXPLICIT_STRATEGY_OPTIONS`` and returns ``DEFAULT_STRATEGY``, so without
        the alias a caller pinning ``"agentic"`` would quietly run the cheap
        nl_to_sql_first chain and never reach the agent.
        """
        assert Orchestrator._resolve_strategy_selection({"strategy": "agentic"}, None) == "deep-reasoning"
        assert Orchestrator._has_explicit_strategy({"strategy": "agentic"}) is True

    def test_unknown_strategy_is_not_an_explicit_pin(self):
        assert Orchestrator._has_explicit_strategy({"strategy": "turbo"}) is False
        assert Orchestrator._resolve_strategy_selection({"strategy": "turbo"}, None) == "nl_to_sql_first"

    def test_tier_override_2_maps_to_default_strategy(self):
        assert Orchestrator._resolve_strategy_selection({}, 2) == "nl_to_sql_first"

    def test_strategy_nl_to_sql_option(self):
        assert Orchestrator._resolve_strategy_selection({"strategy": "nl_to_sql"}, None) == "nl_to_sql"

    def test_tier_override_1_skips(self):
        assert Orchestrator._resolve_strategy_selection({}, 1) is None

    def test_tier_override_3_skips(self):
        assert Orchestrator._resolve_strategy_selection({}, 3) is None

    def test_no_override_defaults_to_namespace_default(self):
        assert Orchestrator._resolve_strategy_selection({}, None) == "nl_to_sql_first"

    def test_start_tier_beyond_2_skips(self):
        assert Orchestrator._resolve_strategy_selection({"startTier": 3}, None) is None

    def test_explicit_strategy_overrides_tier_override(self):
        # strategy option takes precedence over tierOverride
        assert Orchestrator._resolve_strategy_selection({"strategy": "nl_to_sql"}, 2) == "nl_to_sql"


# ── Orchestrator integration with StructuredQueryTier ────────────────────


@pytest.mark.unit
class TestOrchestratorWithStrategy:
    def _make_orchestrator_with_strategy(self, strategy_result: StrategyResult | None = None):
        """Build an orchestrator with a structured_query_tier that returns the given result."""
        from coa_serve.tier1.metric_resolver import MetricMatch
        from coa_serve.tier3.knowledge_retriever import Tier3Result

        metric_resolver = AsyncMock()
        metric_resolver.match.return_value = MetricMatch(found=False)

        knowledge_retriever = AsyncMock()
        knowledge_retriever.lexical_enabled = False
        knowledge_retriever.resolve.return_value = Tier3Result(
            synthesized_answer="Fallback.",
            supporting_content=(),
            graph_context=(),
            confidence=0.5,
            trace_steps=(),
        )

        mock_strategy = _make_strategy("nl_to_sql", result=strategy_result)
        structured_tier = StructuredQueryTier(
            strategies=[mock_strategy],
        )

        orch = Orchestrator(
            metric_resolver=metric_resolver,
            knowledge_retriever=knowledge_retriever,
            structured_query_tier=structured_tier,
            query_executor=AsyncMock(),
            bedrock_client=None,
        )
        return orch, mock_strategy

    @pytest.mark.asyncio
    async def test_strategy_tier_produces_response(self):
        from coa_serve.models import InvokeRequest, InvokeResponse

        result = _make_result("nl_to_sql", confidence=0.85, rows=[{"count": 42}])
        orch, strategy = self._make_orchestrator_with_strategy(result)

        request = InvokeRequest(query="How many orders?", namespace="demo")
        response = await orch.resolve(request)

        assert isinstance(response, InvokeResponse)
        assert response.result.result_rows == [{"count": 42}]
        strategy.resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_strategy_tier_falls_through_to_tier3(self):
        from coa_serve.models import InvokeRequest

        orch, strategy = self._make_orchestrator_with_strategy(None)

        request = InvokeRequest(query="What is the data model?", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 3
        assert response.result.synthesized_answer == "Fallback."

    @pytest.mark.asyncio
    async def test_tier_override_2_with_strategy_raises_no_result(self):
        from coa_serve.models import InvokeRequest

        orch, _ = self._make_orchestrator_with_strategy(None)

        request = InvokeRequest(query="test", namespace="demo", options={"tierOverride": 2})
        # / Req 2: a pinned Tier-2 strategy miss is a 404 NoResultError,
        # not a bare ValueError (which the WS handler would map to 500).
        with pytest.raises(NoResultError) as exc_info:
            await orch.resolve(request)
        assert exc_info.value.status_code == 404
        assert exc_info.value.tier == 2

    @pytest.mark.asyncio
    async def test_strategy_nl_to_sql_pinned_falls_to_tier3_on_failure(self):
        from coa_serve.models import InvokeRequest

        orch, _ = self._make_orchestrator_with_strategy(None)

        request = InvokeRequest(query="test", namespace="demo", options={"strategy": "nl_to_sql"})
        response = await orch.resolve(request)
        # Strategy fails → falls through to Tier 3
        assert response.result.tier == 3


# ── model_id propagation ──────────────────────────────────────────────────


@pytest.mark.unit
class TestModelIdPropagation:
    @pytest.mark.asyncio
    async def test_parallel_context_preserves_model_id(self):
        """Verify _resolve_parallel copies model_id into per-strategy contexts."""
        s1 = _make_strategy("a", result=_make_result("a", confidence=0.6))
        s2 = _make_strategy("b", result=_make_result("b", confidence=0.9))
        tier = StructuredQueryTier(strategies=[s1, s2])

        ctx = StrategyContext(
            trace=TraceCollector(),
            model_id="us.anthropic.claude-haiku-4-5",
        )
        await tier.resolve("query", "ns", ctx, option="best")

        # Both strategies should receive contexts with model_id set
        for strategy in [s1, s2]:
            call_ctx = strategy.resolve.call_args.args[2]
            assert call_ctx.model_id == "us.anthropic.claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_parallel_context_preserves_none_model_id(self):
        """Verify _resolve_parallel works when model_id is None (no override)."""
        s1 = _make_strategy("a", result=_make_result("a", confidence=0.8))
        tier = StructuredQueryTier(strategies=[s1])

        ctx = StrategyContext(trace=TraceCollector(), model_id=None)
        await tier.resolve("query", "ns", ctx, option="best")

        call_ctx = s1.resolve.call_args.args[2]
        assert call_ctx.model_id is None

    @pytest.mark.asyncio
    async def test_sequential_context_passes_model_id(self):
        """Verify sequential policy passes model_id via the original context."""
        s1 = _make_strategy("a", result=_make_result("a"))
        tier = StructuredQueryTier(strategies=[s1])

        ctx = StrategyContext(trace=TraceCollector(), model_id="custom-model")
        await tier.resolve("query", "ns", ctx)

        call_ctx = s1.resolve.call_args.args[2]
        assert call_ctx.model_id == "custom-model"


@pytest.mark.unit
class TestOrchestratorModelValidation:
    def test_invalid_model_id_raises(self):
        from coa_serve.model_validation import validate_model_id

        with pytest.raises(ValueError, match="invalid characters"):
            validate_model_id("model with spaces")

    def test_valid_model_id_in_options(self):
        from coa_serve.model_validation import validate_model_id

        # Should not raise
        validate_model_id("us.anthropic.claude-sonnet-4-6")
        validate_model_id("arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-v2:1")


# ── Strategy selection (_strategies_for) ──────────────────────────────────


@pytest.mark.unit
class TestAgenticStrategySelection:
    """The DEEP_REASONING arm is opt-in: it runs ONLY when pinned, never as a fallback."""

    def _tier(self):
        return StructuredQueryTier(
            strategies=[
                _make_strategy(StrategyOption.ONTOP),
                _make_strategy(StrategyOption.NL_TO_SQL),
                _make_strategy(StrategyOption.DEEP_REASONING),
            ]
        )

    def test_deep_reasoning_pin_selects_only_deep_reasoning(self):
        picked = self._tier()._strategies_for(StrategyOption.DEEP_REASONING)
        assert [s.name for s in picked] == [StrategyOption.DEEP_REASONING]

    def test_deprecated_agentic_pin_still_selects_the_agent(self):
        """The pre-rename spelling must not fall through to the fallback chain.

        Fails if the alias is dropped: ``"agentic"`` stops matching any branch in
        ``_strategies_for`` and lands on ``fallback``, which deliberately EXCLUDES
        the agent — so a caller pinning it would silently get ontop + nl_to_sql
        instead, with no error.
        """
        picked = self._tier()._strategies_for("agentic")
        assert [s.name for s in picked] == [StrategyOption.DEEP_REASONING]

    def test_strategy_values_are_wire_stable(self):
        """These strings are the public API (options.strategy); pinning guards a rename."""
        assert StrategyOption.DEEP_REASONING == "deep-reasoning"
        assert LEGACY_STRATEGY_ALIASES == {"agentic": StrategyOption.DEEP_REASONING}

    def test_normalize_tolerates_non_string_input(self):
        """``options`` is caller-controlled; an unhashable value must not raise."""
        assert normalize_strategy_option(["explore_graph"]) == ["explore_graph"]
        assert normalize_strategy_option(None) is None

    def test_deep_reasoning_excluded_from_nl_to_sql_first_fallback(self):
        picked = self._tier()._strategies_for(StrategyOption.NL_TO_SQL_FIRST)
        names = [s.name for s in picked]
        assert StrategyOption.DEEP_REASONING not in names
        assert names[0] == StrategyOption.NL_TO_SQL  # matching strategy moves to front

    def test_deep_reasoning_excluded_from_ontop_first_fallback(self):
        picked = self._tier()._strategies_for(StrategyOption.ONTOP_FIRST)
        names = [s.name for s in picked]
        assert StrategyOption.DEEP_REASONING not in names
        assert names[0] == StrategyOption.ONTOP

    def test_deep_reasoning_excluded_from_best(self):
        picked = self._tier()._strategies_for(StrategyOption.BEST)
        assert StrategyOption.DEEP_REASONING not in [s.name for s in picked]


@pytest.mark.unit
class TestSmithyQueryStrategyParity:
    """The advertised contract and the honoured values must not drift apart.

    ``strategy`` is a closed enum on the Smithy ``Query`` input, so two silent
    failures are possible and neither shows up in a normal test:

    * the contract advertises a value serve does not honour — it drops out of
      ``Orchestrator._EXPLICIT_STRATEGY_OPTIONS`` and quietly resolves to
      ``DEFAULT_STRATEGY``, running the cheap fallback chain while the caller
      believes it pinned an engine (the exact trap ``LEGACY_STRATEGY_ALIASES``
      documents);
    * serve gains a strategy the contract cannot express, so no REST or MCP caller
      can reach it — the defect this field was added to fix: before it, the
      Ontop/VKG engine ran only as an automatic fallback, so no REST or MCP
      caller could select it deliberately.

    Parsing the model file is deliberate: comparing against the *generated* client
    would only prove codegen ran, not that the source of truth agrees with the
    runtime enum.
    """

    @staticmethod
    def _smithy_enum_values() -> set[str]:
        import re
        from pathlib import Path

        # tests/unit/ -> tests/ -> context-manager/ -> packages/ -> repo root
        repo_root = Path(__file__).resolve().parents[4]
        model = repo_root / "models" / "src" / "main" / "smithy" / "serve.smithy"
        assert model.is_file(), f"Smithy model not found at {model}"
        text = model.read_text()
        match = re.search(r"enum QueryStrategy \{(.*?)\n\}", text, re.DOTALL)
        assert match, "enum QueryStrategy not found in serve.smithy"
        return set(re.findall(r'=\s*"([^"]+)"', match.group(1)))

    def test_enum_matches_strategy_option_exactly(self):
        from coa_serve.tier2.strategy import StrategyOption

        declared = self._smithy_enum_values()
        runtime = {option.value for option in StrategyOption}
        assert declared == runtime, (
            f"Smithy QueryStrategy and StrategyOption disagree.\n"
            f"  advertised but not honoured: {sorted(declared - runtime)}\n"
            f"  honoured but not advertised: {sorted(runtime - declared)}"
        )

    def test_every_declared_value_is_an_explicit_pin(self):
        """Each contract value must survive normalisation into the pin set.

        A value that normalises to something outside
        ``_EXPLICIT_STRATEGY_OPTIONS`` would be accepted at the edge and then
        silently ignored — worse than rejecting it.
        """
        from coa_serve.orchestrator import Orchestrator
        from coa_serve.tier2.strategy import normalize_strategy_option

        for value in sorted(self._smithy_enum_values()):
            normalised = normalize_strategy_option(value)
            assert normalised in Orchestrator._EXPLICIT_STRATEGY_OPTIONS, (
                f"{value!r} is advertised on the contract but is not an explicit pin; "
                f"it would silently fall back to DEFAULT_STRATEGY"
            )

    def test_coa_common_allowed_set_matches_too(self):
        """The data-layer validates against ``coa_common.QUERY_STRATEGIES``.

        That is a third copy of the same set — it exists because the Lambda depends
        only on ``coa-common``, so it cannot import the generated enum or
        ``StrategyOption`` without adding an undeclared runtime dependency. All three
        must agree or the REST edge starts rejecting values serve honours (or
        accepting ones it does not).
        """
        from coa_common.constants import QUERY_STRATEGIES
        from coa_serve.tier2.strategy import StrategyOption

        declared = self._smithy_enum_values()
        runtime = {option.value for option in StrategyOption}
        assert set(QUERY_STRATEGIES) == declared == runtime, (
            f"three-way drift.\n"
            f"  smithy:      {sorted(declared)}\n"
            f"  StrategyOption: {sorted(runtime)}\n"
            f"  coa_common:  {sorted(QUERY_STRATEGIES)}"
        )

    def test_default_strategy_is_itself_selectable(self):
        """A caller must be able to ask for the default explicitly, so that
        'no opinion' and 'deliberately the default' are distinguishable in a trace."""
        from coa_serve.tier2.strategy import DEFAULT_STRATEGY

        assert DEFAULT_STRATEGY.value in self._smithy_enum_values()
