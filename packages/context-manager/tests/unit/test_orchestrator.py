# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Orchestrator — tiered resolution pipeline."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.clients.base import QueryResult
from coa_serve.exceptions import AccessDeniedError, NoResultError
from coa_serve.models import InvokeRequest, InvokeResponse
from coa_serve.orchestrator import Orchestrator, TierGating
from coa_serve.tier1.metric_resolver import MetricMatch
from coa_serve.tier2.sql_firewall import FirewallResult, SQLFirewall
from coa_serve.tier2.strategy import StrategyOption, StrategyResult, StructuredQueryTier
from coa_serve.tier3.knowledge_retriever import Tier3Result
from coa_serve.trace import TraceCollector


def _make_strategy_result(*, success=True, empty=False, confidence=0.85, ontop_assembly=True):
    """Build a StrategyResult mimicking Ontop VKG output."""
    if not success:
        return None
    rows = [] if empty else [{"id": "1"}]
    return StrategyResult(
        sql="SELECT id FROM claims",
        rows=rows,
        columns=["id"],
        confidence=confidence,
        strategy_name=StrategyOption.ONTOP if ontop_assembly else StrategyOption.NL_TO_SQL,
        trace_steps=[],
        ontop_assembly=ontop_assembly,
        sparql="SELECT ?x WHERE { ?x a :Claim }" if ontop_assembly else "",
        row_count=len(rows),
        truncated=False,
        retrieved_tables=[] if ontop_assembly else ["claims"],
        expanded_tables=[] if ontop_assembly else ["claims", "policies"],
    )


def _make_orchestrator(
    metric_found=False,
    tier2_success=True,
    tier2_empty=False,
    tier2_ontop_assembly=True,
    tier2_confidence=0.85,
    strategy_side_effect=None,
    sources_registry=None,
    tier2_skip_evaluator=None,
    return_parts=False,
    vector_client=None,
    oss_ontology_index=None,
):
    """Build orchestrator with mocked strategy tier.

    Args:
        metric_found: If True, Tier 1 metric resolver finds a match.
        tier2_success: If True, strategy returns a StrategyResult; if False, returns None.
        tier2_empty: If True, strategy result has no rows.
        tier2_ontop_assembly: If True, uses ontop assembly path; if False, nl_to_sql path.
        tier2_confidence: Confidence score for the strategy result.
        strategy_side_effect: If set, strategy.resolve raises this exception.
    """
    metric_resolver = AsyncMock()
    metric_resolver.match.return_value = MetricMatch(found=metric_found)
    query_executor = AsyncMock()
    if metric_found:
        metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders",
            columns=["total"],
            data_source_id="ds-orders",
        )
        query_executor.execute.return_value = QueryResult(
            rows=[{"total": 42000}], columns=["total"], row_count=1, truncated=False
        )

    # Build a mock strategy
    if strategy_side_effect:
        strategy_resolve = AsyncMock(side_effect=strategy_side_effect)
    elif not tier2_success:
        strategy_resolve = AsyncMock(return_value=None)
    else:
        strategy_result = _make_strategy_result(
            success=True,
            empty=tier2_empty,
            confidence=tier2_confidence,
            ontop_assembly=tier2_ontop_assembly,
        )
        strategy_resolve = AsyncMock(return_value=strategy_result)

    mock_strategy = MagicMock()
    mock_strategy.name = StrategyOption.ONTOP
    mock_strategy.resolve = strategy_resolve
    structured_query_tier = StructuredQueryTier(strategies=[mock_strategy])

    knowledge_retriever = AsyncMock()
    knowledge_retriever.lexical_enabled = False
    knowledge_retriever.resolve.return_value = Tier3Result(
        synthesized_answer="Based on the available context, claims are insurance requests.",
        supporting_content=(
            {
                "chunkId": "c1",
                "text": "Claims are...",
                "sourceDocumentId": "d1",
                "sourceDocumentName": "glossary.md",
                "relevanceScore": 0.92,
            },
        ),
        graph_context=({"uri": "urn:Claim", "label": "Claim", "type": "owl:Class", "relationships": ()},),
        confidence=0.75,
        trace_steps=(
            {"step": "vector_search", "status": "success", "durationMs": 200},
            {"step": "graph_traverse", "status": "success", "durationMs": 150},
            {"step": "llm_synthesize", "status": "success", "durationMs": 3000},
        ),
    )

    orch = Orchestrator(
        metric_resolver=metric_resolver,
        knowledge_retriever=knowledge_retriever,
        structured_query_tier=structured_query_tier,
        query_executor=query_executor,
        sources_registry=sources_registry,
        tier2_skip_evaluator=tier2_skip_evaluator,
        vector_client=vector_client,
        oss_ontology_index=oss_ontology_index,
    )
    if return_parts:
        return orch, {
            "strategy_resolve": strategy_resolve,
            "knowledge_retriever": knowledge_retriever,
            "metric_resolver": metric_resolver,
        }
    return orch


def _find_step(trace, step_name):
    return next((s for s in trace if s.step == step_name), None)


# ── Tier 2: Strategy Result Conversion ──────────────────────────────────


@pytest.mark.unit
class TestOrchestratorTier2:
    @pytest.mark.asyncio
    async def test_tier2_success_ontop_assembly(self):
        """Ontop strategy result is correctly assembled with sparql and sql."""
        orch = _make_orchestrator(tier2_success=True, tier2_ontop_assembly=True)
        request = InvokeRequest(query="Show all claims", namespace="demo", options={"tierOverride": 2})
        response = await orch.resolve(request)

        assert isinstance(response, InvokeResponse)
        assert response.result.tier == 2
        assert response.result.confidence.score == 0.85
        assert response.result.result_rows == [{"id": "1"}]
        assert response.result.sparql_generated == "SELECT ?x WHERE { ?x a :Claim }"
        assert response.result.query_used == "SELECT id FROM claims"

    @pytest.mark.asyncio
    async def test_tier2_success_nl_to_sql_assembly(self):
        """NL-to-SQL strategy result uses the nl_to_sql assembler path."""
        orch = _make_orchestrator(tier2_success=True, tier2_ontop_assembly=False)
        request = InvokeRequest(query="How many orders?", namespace="demo", options={"tierOverride": 2})
        response = await orch.resolve(request)

        assert isinstance(response, InvokeResponse)
        assert response.result.tier == 2
        assert response.result.confidence.score == 0.85
        assert response.result.result_rows == [{"id": "1"}]

    @pytest.mark.asyncio
    async def test_tier2_failure_falls_to_tier3(self):
        """When all strategies return None, orchestrator falls through to Tier 3."""
        orch = _make_orchestrator(tier2_success=False)
        request = InvokeRequest(query="Why did processing spike?", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 3
        assert response.result.synthesized_answer is not None
        assert response.result.confidence.score == 0.75

    @pytest.mark.asyncio
    async def test_tier2_low_confidence_nonempty_falls_to_tier3(self):
        """A Tier-2 result that returned ROWS but with confidence below the floor
        is not a usable structured answer (e.g. a knowledge question forced through
        NL→SQL/VKG) — the orchestrator drops it and falls through to Tier 3."""
        orch = _make_orchestrator(tier2_success=True, tier2_empty=False, tier2_confidence=0.05)
        request = InvokeRequest(query="What does the handbook recommend?", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 3
        assert response.result.synthesized_answer is not None

    @pytest.mark.asyncio
    async def test_tier2_pinned_keeps_low_confidence_result(self):
        """tierOverride=2 is an explicit request for structured — a low-confidence
        (non-empty) result is still returned, NOT dropped to Tier 3."""
        orch = _make_orchestrator(tier2_success=True, tier2_empty=False, tier2_confidence=0.05)
        request = InvokeRequest(query="Show claims", namespace="demo", options={"tierOverride": 2})
        response = await orch.resolve(request)

        assert response.result.tier == 2

    @pytest.mark.asyncio
    async def test_tier2_high_confidence_still_accepted(self):
        """Regression guard: a confident Tier-2 result (>= floor) is returned as
        Tier 2, not demoted."""
        orch = _make_orchestrator(tier2_success=True, tier2_empty=False, tier2_confidence=0.85)
        request = InvokeRequest(query="claims grouped by catastrophe", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 2

    @pytest.mark.asyncio
    async def test_tier2_pinned_miss_raises_no_result(self):
        """/ Req 2: tierOverride=2 with a strategy miss raises a typed
        NoResultError (404 'no data found'), NOT a bare ValueError that the WS
        handler would surface as InternalError 500."""
        orch = _make_orchestrator(tier2_success=False)
        request = InvokeRequest(query="Show claims", namespace="demo", options={"tierOverride": 2})
        with pytest.raises(NoResultError) as exc_info:
            await orch.resolve(request)
        assert exc_info.value.status_code == 404
        assert exc_info.value.tier == 2

    @pytest.mark.asyncio
    async def test_tier2_empty_result_calibrated_confidence(self):
        """Empty rows from strategy result in calibrated (lowered) confidence."""
        orch = _make_orchestrator(tier2_success=True, tier2_empty=True)
        request = InvokeRequest(query="Show all claims", namespace="demo", options={"tierOverride": 2})
        response = await orch.resolve(request)

        assert response.result.tier == 2
        # 0.85 is the LLM self-rating in the mock; empty result must lower it
        assert response.result.confidence.score < 0.85
        assert "empty result" in response.result.confidence.rationale.lower()

    @pytest.mark.asyncio
    async def test_tier2_empty_result_still_accepted_without_pin(self):
        """Empty T2 result is still a valid answer (DB confirmed the query)."""
        orch = _make_orchestrator(tier2_success=True, tier2_empty=True)
        response = await orch.resolve(InvokeRequest(query="Show all claims", namespace="demo"))
        assert response.result.tier == 2
        assert response.result.confidence.score < 0.85

    @pytest.mark.asyncio
    async def test_tier2_low_confidence_still_returns_if_db_succeeded(self):
        """Tier 2 with low confidence but rows present still returns Tier 2."""
        orch = _make_orchestrator(tier2_success=True, tier2_confidence=0.4)
        request = InvokeRequest(query="Complex aggregation query", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 2
        assert response.result.confidence.score == 0.4
        assert response.result.result_rows == [{"id": "1"}]


# ── Tier 1: Metric Resolution ───────────────────────────────────────────


@pytest.mark.unit
class TestOrchestratorTier1:
    @pytest.mark.asyncio
    async def test_tier1_pinned_miss_raises_no_result(self):
        """tierOverride=1 with no metric match is a 'no data found'
        outcome — raises NoResultError (404), not a bare ValueError → InternalError
        500. This is the exact path that 500'd when 0 metrics were loaded due to
        the legacy graph-URI bug."""
        orch = _make_orchestrator(metric_found=False)
        request = InvokeRequest(query="unmatched query", namespace="demo", options={"tierOverride": 1})
        with pytest.raises(NoResultError) as exc_info:
            await orch.resolve(request)
        assert exc_info.value.status_code == 404
        assert exc_info.value.tier == 1

    @pytest.mark.asyncio
    async def test_tier1_match_returns_deterministic(self):
        orch = _make_orchestrator(metric_found=True)
        request = InvokeRequest(query="What is revenue?", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 1
        assert response.result.confidence.score == 1.0
        assert response.result.result_rows == [{"total": 42000}]
        assert response.result.query_used == "SELECT sum(amount) FROM orders"

    @pytest.mark.asyncio
    async def test_multi_metric_bypasses_tier1(self):
        """DoD: a query matching >1 metric must NOT run Tier-1 (the first
        metric) — it bypasses to Tier-2/3. Regression for the codex-found bug."""
        orch = _make_orchestrator(metric_found=True)
        # Resolver reports an ambiguous multi-metric match.
        orch._metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders",
            match_count=2,
        )
        request = InvokeRequest(query="compare revenue and cost", namespace="demo")
        response = await orch.resolve(request)

        # Must NOT be a Tier-1 answer, and the metric executor must not have run.
        assert response.result.tier != 1
        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_metrics_blocks_unauthorized_metric(self):
        """allowedMetrics grant restriction denies execution of
        metrics not in the list."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_id="demo:revenue",
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders",
            columns=["total"],
            data_source_id="ds-orders",
        )
        request = InvokeRequest(
            query="What is revenue?",
            namespace="demo",
            profile={
                "userId": "alice@example.com",
                "allowedMetrics": ["demo:cost", "demo:profit"],  # revenue not in list
            },
        )

        with pytest.raises(AccessDeniedError) as exc_info:
            await orch.resolve(request)

        # Check the reason attribute (message is always "Access denied")
        assert "revenue" in exc_info.value.reason.lower()
        assert "allowedmetrics" in exc_info.value.reason.lower()
        # Executor must not have been called
        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_metrics_allows_metric_by_id(self):
        """allowedMetrics match by metric_id allows execution."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_id="demo:revenue",
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders",
            columns=["total"],
            data_source_id="ds-orders",
        )
        request = InvokeRequest(
            query="What is revenue?",
            namespace="demo",
            profile={
                "userId": "alice@example.com",
                "allowedMetrics": ["demo:revenue", "demo:cost"],
            },
        )

        response = await orch.resolve(request)

        assert response.result.tier == 1
        orch._query_executor.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_allowed_metrics_allows_metric_by_name(self):
        """allowedMetrics match by metric_name allows execution."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_id="demo:revenue",
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders",
            columns=["total"],
            data_source_id="ds-orders",
        )
        request = InvokeRequest(
            query="What is revenue?",
            namespace="demo",
            profile={
                "userId": "alice@example.com",
                "allowedMetrics": ["revenue", "cost"],  # match by name, not id
            },
        )

        response = await orch.resolve(request)

        assert response.result.tier == 1
        orch._query_executor.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_allowed_metrics_denies_all(self):
        """SEMANTICS CHANGE — a declared-empty allowedMetrics is a deny-all restriction.

        This previously asserted the opposite (empty = unrestricted), which was the
        same empty-vs-absent collapse that made an empty tableAllowlist fail open in
        the SQL firewall. A grant declaring ``allowedMetrics: []`` means "no metrics
        permitted"; absence of the key still means unrestricted — see the test below.
        """
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_id="demo:revenue",
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders",
            columns=["total"],
            data_source_id="ds-orders",
        )
        request = InvokeRequest(
            query="What is revenue?",
            namespace="demo",
            profile={
                "userId": "alice@example.com",
                "allowedMetrics": [],  # declared empty = no metrics permitted
            },
        )

        with pytest.raises(AccessDeniedError) as exc_info:
            await orch.resolve(request)

        assert "allowedmetrics" in exc_info.value.reason.lower()
        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unset_allowed_metrics_permits_all(self):
        """Unset allowedMetrics = no restriction (sparse opt-in convention)."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_id="demo:revenue",
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders",
            columns=["total"],
            data_source_id="ds-orders",
        )
        request = InvokeRequest(
            query="What is revenue?",
            namespace="demo",
            profile={"userId": "alice@example.com"},  # no allowedMetrics key
        )

        response = await orch.resolve(request)

        assert response.result.tier == 1
        orch._query_executor.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_contract_shape_dimensions_round_trip(self):
        """Round-trip the CONTRACT's dimensions shape (a list of DimensionFilter)
        through the orchestrator, not a hand-built dict — the gap that let issue
        #53 through. Uses the REAL substitute_dimensions so the bound SQL is
        asserted end to end."""
        from coa_serve.tier1.metric_resolver import MetricResolver

        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.substitute_dimensions = MetricResolver.substitute_dimensions
        orch._metric_resolver.match.return_value = MetricMatch(
            found=True,
            metric_name="revenue",
            sql_template="SELECT sum(amount) FROM orders WHERE region = :region",
            columns=["total"],
            data_source_id="ds-orders",
            dimensions=["region"],
            match_count=1,
            match_confidence=1.0,
        )
        request = InvokeRequest(
            query="What is revenue?",
            namespace="demo",
            options={"dimensions": [{"name": "region", "value": "EMEA", "operator": "="}]},
        )

        response = await orch.resolve(request)

        assert response.result.tier == 1
        # The caller's value is bound as a quoted SQL literal, not interpolated.
        assert response.result.query_used == "SELECT sum(amount) FROM orders WHERE region = 'EMEA'"

    @pytest.mark.asyncio
    async def test_contract_shape_dimensions_no_placeholder_template(self):
        """A filter the matched metric's SQL cannot apply falls through to a
        richer tier rather than answering the WRONG question.

        Two things are asserted at once. The shape no longer 500s (issue #53 —
        ``.items()`` ran before placeholder matching, so every Tier-1 metric
        raised AttributeError, not just parameterized ones). And the fix does not
        trade that 500 for a silent wrong answer: the unfiltered GLOBAL total
        must NOT be returned at Tier-1's confidence 1.0 just because the template
        has nowhere to bind ``region``.
        """
        from coa_serve.tier1.metric_resolver import MetricResolver

        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.substitute_dimensions = MetricResolver.substitute_dimensions
        request = InvokeRequest(
            query="What is revenue?",
            namespace="demo",
            options={"dimensions": [{"name": "region", "value": "EMEA"}]},
        )

        response = await orch.resolve(request)

        assert response.result.tier == 2
        # The unfiltered template must never be executed.
        assert response.result.query_used != "SELECT sum(amount) FROM orders"
        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_dimensions_falls_through_not_500(self):
        """Defense in depth: a non-mapping reaching Tier-1 from an unvalidated
        internal caller fails CLOSED to Tier-2/3 instead of raising
        AttributeError → blanket 500."""
        from coa_serve.tier1.metric_resolver import MetricResolver

        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.substitute_dimensions = MetricResolver.substitute_dimensions
        request = InvokeRequest(query="What is revenue?", namespace="demo")
        # Bypass model normalization the way an internal caller would.
        request.options["dimensions"] = [{"name": "region", "value": "EMEA"}]

        response = await orch.resolve(request)

        assert response.result.tier == 2
        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_executor_error_raises_data_source_unavailable(self):
        """executor failure (e.g. table not found) must not crash with
        generic 500. Raises DataSourceUnavailableError (502) with trace."""
        from coa_serve.exceptions import DataSourceUnavailableError

        orch = _make_orchestrator(metric_found=True)
        orch._query_executor.execute.side_effect = RuntimeError('relation "orders" does not exist')

        request = InvokeRequest(query="What is revenue?", namespace="demo")
        with pytest.raises(DataSourceUnavailableError) as exc_info:
            await orch.resolve(request)
        assert "revenue" in exc_info.value.message
        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_cross_namespace_sql_reference_raises_access_denied(self):
        """A Tier-1 executor error caused by the namespace-scope firewall surfaces
        as NamespaceScopeDeniedError (403) with its policy reason — not a masked
        500/502 — while generic executor errors stay DataSourceUnavailableError."""
        from coa_serve.exceptions import NamespaceScopeDeniedError
        from coa_serve.tier2.sql_firewall import NamespaceSQLScopeError

        orch = _make_orchestrator(metric_found=True)
        cause = NamespaceSQLScopeError("database 'other' not available in the requested namespace")
        err = RuntimeError("Access denied: SQL reference is outside the requested namespace")
        err.__cause__ = cause
        orch._query_executor.execute.side_effect = err

        request = InvokeRequest(query="What is revenue?", namespace="demo")
        with pytest.raises(NamespaceScopeDeniedError) as exc_info:
            await orch.resolve(request)
        assert exc_info.value.status_code == 403
        assert "outside the requested namespace" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_firewall_error_falls_through_to_tier3(self):
        """firewall rejection (e.g. legacy expression fragment) must not
        crash with 500. Falls through to Tier 2/3."""
        from coa_serve.tier2.sql_firewall import UnsafeSQLError

        orch = _make_orchestrator(metric_found=True)
        orch._firewall = MagicMock()
        orch._firewall.evaluate.side_effect = UnsafeSQLError("Only SELECT statements are allowed, got: Sum")

        request = InvokeRequest(query="What is revenue?", namespace="demo")
        response = await orch.resolve(request)

        # Falls through (no 500 crash); lands on next available tier
        assert response.result.tier != 1


# ── Tier 1: Residual-Qualifier Gate ─────────────────────────────────────


def _qualified_match(residual: str, **overrides) -> MetricMatch:
    """A Tier-1 hit whose question carried ``residual`` words the metric can't express."""
    fields = {
        "found": True,
        "metric_id": "demo:revenue",
        "metric_name": "revenue",
        "sql_template": "SELECT sum(amount) FROM orders",
        "columns": ["total"],
        "data_source_id": "ds-orders",
        "matched_text": "revenue",
        "residual": residual,
    }
    fields.update(overrides)
    return MetricMatch(**fields)


@pytest.mark.unit
class TestTier1ResidualQualifierGate:
    """A metric match that leaves part of the question unconsumed is a PARTIAL
    match. Tier-1 runs the metric's SQL verbatim and parses no filter/grouping/time
    window out of the NL, so answering would return the unfiltered aggregate at
    confidence 1.0 with nothing marking the drop. It must decline to Tier 2 instead.
    """

    @pytest.mark.asyncio
    async def test_residual_qualifier_bypasses_tier1(self):
        """ "What was total revenue for the Gold loyalty tier?" — the Gold filter is
        unexpressible, so Tier-1 declines and the metric SQL never runs."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = _qualified_match("gold loyalty tier")
        request = InvokeRequest(
            query="What was total revenue for the Gold loyalty tier?",
            namespace="demo",
        )

        response = await orch.resolve(request)

        assert response.result.tier != 1
        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bypass_records_unhandled_qualifier_in_trace(self):
        """The dropped span is named in the trace — the evidence previously missing."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = _qualified_match("last quarter")
        request = InvokeRequest(query="What is revenue last quarter?", namespace="demo")

        response = await orch.resolve(request)

        step = _find_step(response.result.trace, "t1.metric_match")
        assert step is not None
        assert step.status == "residual_qualifier_bypass"
        assert step.detail["unhandledQualifier"] == "last quarter"
        assert step.detail["metricName"] == "revenue"
        # Paired with the span that DID match, so a surprising bypass is diagnosable
        # from the trace alone.
        assert step.detail["matchedText"] == "revenue"

    @pytest.mark.asyncio
    async def test_bypass_records_no_tier1_success_step(self):
        """A declined question must not also record a Tier-1 hit — the gate is
        checked before the success trace, so there is exactly one metric_match step."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = _qualified_match("gold loyalty tier")
        request = InvokeRequest(query="revenue for the Gold loyalty tier", namespace="demo")

        response = await orch.resolve(request)

        matches = [s for s in response.result.trace if s.step == "t1.metric_match"]
        assert len(matches) == 1
        assert matches[0].status != "success"

    @pytest.mark.asyncio
    async def test_no_residual_still_answers_from_tier1(self):
        """Regression guard: a fully-consumed question is unaffected by the gate."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = _qualified_match("")
        request = InvokeRequest(query="What is revenue?", namespace="demo")

        response = await orch.resolve(request)

        assert response.result.tier == 1
        assert response.result.confidence.score == 1.0
        orch._query_executor.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_caller_supplied_dimensions_exempt_from_gate(self):
        """``options.dimensions`` is the documented out-of-band filter channel, so a
        request carrying them has already expressed its qualifier — the residual
        words are that same intent restated in prose. Tier-1 still answers."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = _qualified_match(
            "north region", dimensions=["region"], sql_template="SELECT sum(amount) FROM orders WHERE region = :region"
        )
        # substitute_dimensions is sync; the resolver mock is an AsyncMock, so it
        # must be stubbed explicitly or the bound SQL comes back as a coroutine.
        orch._metric_resolver.substitute_dimensions = MagicMock(
            return_value="SELECT sum(amount) FROM orders WHERE region = 'north'"
        )
        request = InvokeRequest(
            query="revenue for the north region",
            namespace="demo",
            options={"dimensions": {"region": "north"}},
        )

        response = await orch.resolve(request)

        assert response.result.tier == 1
        orch._query_executor.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tier1_pin_exempt_from_gate(self):
        """``tierOverride=1`` is an explicit instruction to answer from the metric
        path; bypassing would turn the pin into a 404. Only the AUTOMATIC path
        re-routes."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = _qualified_match("gold loyalty tier")
        request = InvokeRequest(
            query="revenue for the Gold loyalty tier",
            namespace="demo",
            options={"tierOverride": 1},
        )

        response = await orch.resolve(request)

        assert response.result.tier == 1
        orch._query_executor.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_multi_metric_bypass_takes_precedence(self):
        """Ambiguity is reported as the multi-metric bypass even when a residual is
        also present — that check runs first and is the more specific reason."""
        orch = _make_orchestrator(metric_found=True)
        orch._metric_resolver.match.return_value = _qualified_match("compare last year", match_count=2)
        request = InvokeRequest(query="compare revenue and cost last year", namespace="demo")

        response = await orch.resolve(request)

        step = _find_step(response.result.trace, "t1.metric_match")
        assert step is not None
        assert step.status == "multi_metric_bypass"
        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_residual_on_a_miss_is_not_a_bypass(self):
        """No match at all is still recorded as a miss, not a residual bypass — the
        gate only applies to a found metric."""
        orch = _make_orchestrator(metric_found=False)
        orch._metric_resolver.match.return_value = MetricMatch(found=False, residual="gold loyalty tier")
        request = InvokeRequest(query="who are our Gold loyalty tier customers?", namespace="demo")

        response = await orch.resolve(request)

        step = _find_step(response.result.trace, "t1.metric_match")
        assert step is not None
        assert step.status == "miss"


# ── Tier 3 Fallback ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestOrchestratorTier3Fallback:
    @pytest.mark.asyncio
    async def test_strategy_failure_falls_to_tier3(self):
        orch = _make_orchestrator(tier2_success=False)
        request = InvokeRequest(query="Explain the data model", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 3
        assert "claims are insurance" in response.result.synthesized_answer.lower()

    @pytest.mark.asyncio
    async def test_tier_override_skips_earlier_tiers(self):
        orch = _make_orchestrator(metric_found=True)
        request = InvokeRequest(query="test", namespace="demo", options={"tierOverride": 3})
        response = await orch.resolve(request)

        # tierOverride=3 skips tier1 and tier2, goes straight to tier3
        assert response.result.tier == 3

    @pytest.mark.asyncio
    async def test_invalid_tier_override_raises(self):
        orch = _make_orchestrator()
        request = InvokeRequest(query="test", namespace="demo", options={"tierOverride": 5})
        with pytest.raises(ValueError, match="Invalid tierOverride"):
            await orch.resolve(request)

    @pytest.mark.asyncio
    async def test_tier_override_99_raises(self):
        orch = _make_orchestrator()
        request = InvokeRequest(query="test", namespace="demo", options={"tierOverride": 99})
        with pytest.raises(ValueError, match="Invalid tierOverride"):
            await orch.resolve(request)

    @pytest.mark.asyncio
    async def test_tier2_failure_falls_to_tier3(self):
        """When strategy tier returns None, falls to Tier 3."""
        orch = _make_orchestrator(tier2_success=False)
        request = InvokeRequest(query="test", namespace="demo")
        response = await orch.resolve(request)
        assert response.result.tier == 3

    @pytest.mark.asyncio
    async def test_tier1_miss_passes_catalog_summary_to_tier3(self):
        """after a Tier-1 miss, list_all() catalog is passed to Tier 3
        so questions like 'list my metrics' get answered from the catalog."""
        orch, parts = _make_orchestrator(metric_found=False, tier2_success=False, return_parts=True)
        catalog = [{"name": "revenue", "description": "Total rev", "dimensions": ["month"]}]
        # list_all is a sync method, override the AsyncMock default
        parts["metric_resolver"].list_all = MagicMock(return_value=catalog)

        request = InvokeRequest(query="list my metrics", namespace="demo")
        await orch.resolve(request)

        parts["metric_resolver"].list_all.assert_called_once_with("demo")
        # Verify catalog_summary was forwarded to knowledge_retriever.resolve
        call_kwargs = parts["knowledge_retriever"].resolve.call_args.kwargs
        assert call_kwargs["catalog_summary"] == catalog


# ── startTier option ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestOrchestratorStartTier:
    @pytest.mark.asyncio
    async def test_start_tier_skips_earlier_tiers(self):
        orch = _make_orchestrator(metric_found=True)
        request = InvokeRequest(query="What is revenue?", namespace="demo", options={"startTier": 2})
        response = await orch.resolve(request)
        # startTier=2 skips tier1, goes to tier2 (which succeeds with high confidence)
        assert response.result.tier == 2

    @pytest.mark.asyncio
    async def test_start_tier_3_skips_tier1_and_tier2(self):
        """startTier=3 skips both Tier 1 and Tier 2, goes directly to Tier 3."""
        orch = _make_orchestrator(metric_found=True, tier2_success=True)
        request = InvokeRequest(query="test", namespace="demo", options={"startTier": 3})
        response = await orch.resolve(request)

        assert response.result.tier == 3
        # Strategy should not have been called
        orch._metric_resolver.match.assert_not_awaited()


# ── Strategy Option Mapping ──────────────────────────────────────────────


@pytest.mark.unit
class TestStrategyOptionMapping:
    """Verify that tierOverride and options.strategy map to correct strategy selection."""

    def test_tier_override_2_maps_to_default_strategy(self):
        result = Orchestrator._resolve_strategy_selection({}, tier_override=2)
        from coa_serve.tier2.strategy import DEFAULT_STRATEGY

        assert result == DEFAULT_STRATEGY

    def test_tier_override_3_skips_structured_query(self):
        result = Orchestrator._resolve_strategy_selection({}, tier_override=3)
        assert result is None

    def test_tier_override_1_skips_structured_query(self):
        result = Orchestrator._resolve_strategy_selection({}, tier_override=1)
        assert result is None

    def test_explicit_strategy_option_takes_precedence(self):
        result = Orchestrator._resolve_strategy_selection({"strategy": "best"}, tier_override=2)
        assert result == StrategyOption.BEST

    def test_no_override_uses_default(self):
        result = Orchestrator._resolve_strategy_selection({}, tier_override=None)
        assert result == StrategyOption.NL_TO_SQL_FIRST

    def test_start_tier_above_2_skips(self):
        result = Orchestrator._resolve_strategy_selection({"startTier": 3}, tier_override=None)
        assert result is None


# ── Trace ────────────────────────────────────────────────────────────────


@pytest.mark.unit
class TestOrchestratorTrace:
    @pytest.mark.asyncio
    async def test_trace_includes_typed_steps(self):
        orch = _make_orchestrator()
        request = InvokeRequest(query="test", namespace="demo")
        response = await orch.resolve(request)

        for step in response.result.trace:
            assert step.step
            assert step.status
            assert step.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_namespace_in_metadata(self):
        orch = _make_orchestrator()
        request = InvokeRequest(query="test", namespace="my-ns")
        response = await orch.resolve(request)

        assert response.result.metadata["namespace"] == "my-ns"


# ── Trace Completeness: Tier 1 ──────────────────────────────────────────


@pytest.mark.unit
class TestTraceCompletenessTier1:
    """A1: tier1_execute success carries row-count detail + toolUsed."""

    @pytest.mark.asyncio
    async def test_tier1_execute_has_detail_and_tool_used(self):
        orch = _make_orchestrator(metric_found=True)
        request = InvokeRequest(query="What is revenue?", namespace="demo")
        response = await orch.resolve(request)

        step = _find_step(response.result.trace, "t1.execute")
        assert step is not None
        assert step.status == "success"
        assert step.detail  # non-empty
        assert step.detail["rowCount"] == 1  # row_count=1 from the mock
        assert step.tool_used


# ── Trace Completeness: Tier 2 ──────────────────────────────────────────


@pytest.mark.unit
class TestTraceCompletenessTier2:
    """Tier 2 trace steps come from the strategy via context.trace."""

    @pytest.mark.asyncio
    async def test_embed_query_traced_with_tool_used(self):
        """when a Bedrock client is present, embedding generation is traced."""
        orch = _make_orchestrator(tier2_success=True)
        bedrock = AsyncMock()
        bedrock.embed.return_value = [0.1] * 8
        orch._bedrock_client = bedrock
        request = InvokeRequest(query="Show all claims", namespace="demo")
        response = await orch.resolve(request)

        step = _find_step(response.result.trace, "query.embed")
        assert step is not None and step.status == "success"
        assert step.tool_used == "bedrock"

    @pytest.mark.asyncio
    async def test_embed_query_failure_traced(self):
        """When embedding fails, error step is recorded but resolution continues."""
        orch = _make_orchestrator(tier2_success=True)
        bedrock = AsyncMock()
        bedrock.embed.side_effect = RuntimeError("Bedrock timeout")
        orch._bedrock_client = bedrock
        request = InvokeRequest(query="Show all claims", namespace="demo")
        response = await orch.resolve(request)

        step = _find_step(response.result.trace, "query.embed")
        assert step is not None and step.status == "error"
        assert step.tool_used == "bedrock"
        # Should still resolve (via strategy)
        assert response.result.tier == 2

    @pytest.mark.asyncio
    async def test_embed_query_error_detail_sanitized(self):
        """CWE-209: Bedrock embedding failure must NOT leak raw exception message to trace detail."""
        from botocore.exceptions import ClientError

        orch = _make_orchestrator(tier2_success=True)
        bedrock = AsyncMock()

        # Simulate a ClientError with sensitive infrastructure details
        error_resp = {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": (
                    "User: arn:aws:iam::123456789012:assumed-role/BedrockRole/session "
                    "is not authorized to perform: bedrock:InvokeModel on resource: "
                    "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
                ),
            }
        }
        bedrock.embed.side_effect = ClientError(error_resp, "InvokeModel")
        orch._bedrock_client = bedrock
        request = InvokeRequest(query="Show all claims", namespace="demo")
        response = await orch.resolve(request)

        step = _find_step(response.result.trace, "query.embed")
        assert step is not None and step.status == "error"

        # detail should contain only the error type, not the full message
        assert isinstance(step.detail, dict)
        assert step.detail.get("error") == "ClientError"
        # Must NOT leak ARN, account ID, or role name
        assert "message" not in step.detail, "detail must not have 'message' key with raw exception"
        # Verify serialization doesn't leak it either
        import json

        serialized = json.dumps(step.model_dump(by_alias=True))
        assert "123456789012" not in serialized
        assert "arn:aws" not in serialized
        assert "BedrockRole" not in serialized


# ── Terminal Assembly Step ───────────────────────────────────────────────


@pytest.mark.unit
class TestTerminalAssemblyStep:
    """totalMs is set in metadata; no response_assembled step emitted."""

    @pytest.mark.asyncio
    async def test_tier1_no_response_assembled_step(self):
        orch = _make_orchestrator(metric_found=True)
        response = await orch.resolve(InvokeRequest(query="revenue?", namespace="demo"))
        step = _find_step(response.result.trace, "response_assembled")
        assert step is None, "response_assembled step must not be present"

    @pytest.mark.asyncio
    async def test_tier2_no_response_assembled_step(self):
        orch = _make_orchestrator(tier2_success=True)
        response = await orch.resolve(InvokeRequest(query="Show all claims", namespace="demo"))
        step = _find_step(response.result.trace, "response_assembled")
        assert step is None, "response_assembled step must not be present"

    @pytest.mark.asyncio
    async def test_tier3_no_response_assembled_step(self):
        orch = _make_orchestrator(tier2_success=False)
        response = await orch.resolve(InvokeRequest(query="Explain the data model", namespace="demo"))
        step = _find_step(response.result.trace, "response_assembled")
        assert step is None, "response_assembled step must not be present"


# ── AccessDeniedError Propagation from Strategy ─────────────────────────


@pytest.mark.unit
class TestWI122Tier2TerminalDeny:
    """Tier-2 authorization deny propagates AccessDeniedError and does NOT fall to Tier-3."""

    @pytest.mark.asyncio
    async def test_access_denied_from_strategy_propagates(self):
        """AccessDeniedError raised by strategy bubbles up without Tier 3 fallback."""
        orch = _make_orchestrator(
            strategy_side_effect=AccessDeniedError("Access denied to table(s): payroll"),
        )
        request = InvokeRequest(query="Show payroll", namespace="demo")

        with pytest.raises(AccessDeniedError):
            await orch.resolve(request)

        # No Tier-3 fall-through.
        orch._knowledge_retriever.resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tier2_non_auth_failure_still_falls_to_tier3(self):
        """A non-auth Tier-2 failure (strategy returns None) still reaches Tier-3."""
        orch = _make_orchestrator(tier2_success=False)
        request = InvokeRequest(query="Why did processing spike?", namespace="demo")
        response = await orch.resolve(request)

        assert response.result.tier == 3
        orch._knowledge_retriever.resolve.assert_awaited()


# ── Tier 1 Firewall Terminal Deny ────────────────────────────────────────


@pytest.mark.unit
class TestWI122Tier1TerminalDeny:
    """Tier-1 evaluates the firewall before execute; deny is terminal."""

    @pytest.mark.asyncio
    async def test_tier1_firewall_denied_raises_before_execute(self):
        """Injected firewall denies -> AccessDeniedError, executor never called."""
        firewall = MagicMock(spec=SQLFirewall)
        firewall.evaluate.return_value = FirewallResult(
            denied=True, authorized_sql="", reason="Access denied to table(s): orders"
        )
        orch = _make_orchestrator(metric_found=True)
        orch._firewall = firewall
        request = InvokeRequest(query="What is revenue?", namespace="demo")

        with pytest.raises(AccessDeniedError):
            await orch.resolve(request)

        orch._query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tier1_allowed_executes_authorized_sql(self):
        """Firewall allows with authorized_sql -> that SQL is what the executor runs."""
        firewall = MagicMock(spec=SQLFirewall)
        firewall.evaluate.return_value = FirewallResult(
            denied=False, authorized_sql="SELECT sum(amount) FROM orders /*authz*/"
        )
        orch = _make_orchestrator(metric_found=True)
        orch._firewall = firewall
        request = InvokeRequest(query="What is revenue?", namespace="demo")

        response = await orch.resolve(request)

        assert response.result.tier == 1
        orch._query_executor.execute.assert_awaited_once()
        call = orch._query_executor.execute.call_args
        # First positional arg is the SQL to execute.
        assert call.args[0] == "SELECT sum(amount) FROM orders /*authz*/"

    @pytest.mark.asyncio
    async def test_tier1_empty_profile_fail_open_characterization(self):
        """Empty profile, REAL SQLFirewall -> allowed -> tier 1 (sparse opt-in default)."""
        orch = _make_orchestrator(metric_found=True)
        orch._firewall = SQLFirewall()
        request = InvokeRequest(query="What is revenue?", namespace="demo", profile={})

        response = await orch.resolve(request)

        assert response.result.tier == 1


# ── Constructor Back-Compat ──────────────────────────────────────────────


@pytest.mark.unit
class TestWI122ConstructorBackCompat:
    """AUDIT: existing constructions without a firewall arg still work."""

    def test_orchestrator_defaults_firewall(self):
        orch = _make_orchestrator()
        # No firewall passed by _make_orchestrator -> a default SQLFirewall is built.
        assert isinstance(orch._firewall, SQLFirewall)


# ── Source-composition tier gating ────────────────────────────────────────


def _registry_with_composition(*, has_structured_source, has_unstructured_source, unknown=False, raises=None):
    """Build a mock SourcesRegistry whose get_source_composition returns a fixed
    SourceComposition (or raises)."""
    from coa_serve.clients.sources_registry import SourceComposition

    registry = MagicMock()
    if raises is not None:
        registry.get_source_composition = AsyncMock(side_effect=raises)
    elif unknown:
        registry.get_source_composition = AsyncMock(return_value=SourceComposition.unknown_composition())
    else:
        registry.get_source_composition = AsyncMock(
            return_value=SourceComposition(
                has_structured_source=has_structured_source, has_unstructured_source=has_unstructured_source
            )
        )
    return registry


@pytest.mark.unit
class TestSourceCompositionGating:
    """Skip inapplicable tiers based on a namespace's source composition.

    - document-only (no DATABASE) → Tier 1 + Tier 2 skipped, Tier 3 runs WITH vector.
    - structured-only (no DOCUMENTS) → Tier 3 vector search skipped (embedding
      not passed to Tier 3), Tier 1 + Tier 2 still run.
    - mixed / unknown / explicit override → nothing skipped (fail-open).
    """

    @staticmethod
    def _attach_bedrock(orch):
        """Give the orchestrator a mock Bedrock client so query embedding is
        generated (default _make_orchestrator omits it)."""
        bedrock = AsyncMock()
        bedrock.embed.return_value = [0.1] * 8
        orch._bedrock_client = bedrock
        return orch

    @pytest.mark.asyncio
    async def test_document_only_skips_tier1_and_tier2(self):
        """No DATABASE source → Tier 1 metric resolver AND Tier 2 strategy are never
        called (both need a relational source); falls straight to Tier 3."""
        registry = _registry_with_composition(has_structured_source=False, has_unstructured_source=True)
        orch, parts = _make_orchestrator(tier2_success=True, sources_registry=registry, return_parts=True)
        self._attach_bedrock(orch)
        request = InvokeRequest(query="Tell me about BankingProcess", namespace="doc-only")
        response = await orch.resolve(request)

        # Both Tier 1 and Tier 2 skipped despite a would-be-successful strategy, so we land on Tier 3.
        assert response.result.tier == 3
        parts["metric_resolver"].match.assert_not_awaited()
        parts["strategy_resolve"].assert_not_awaited()
        # Tier 3 received a real embedding (vector search NOT skipped for doc-only).
        _, kwargs = parts["knowledge_retriever"].resolve.call_args
        assert kwargs["embedding"] is not None

    @pytest.mark.asyncio
    async def test_structured_only_skips_tier3_vector(self):
        """No DOCUMENTS source → Tier 3 receives embedding=None so vector search self-skips,
        but Tier 2 still runs normally."""
        registry = _registry_with_composition(has_structured_source=True, has_unstructured_source=False)
        # tier2 fails so we fall through to Tier 3 and can inspect its embedding arg.
        orch, parts = _make_orchestrator(tier2_success=False, sources_registry=registry, return_parts=True)
        self._attach_bedrock(orch)
        request = InvokeRequest(query="anything", namespace="db-only")
        response = await orch.resolve(request)

        assert response.result.tier == 3
        # Tier 1 and Tier 2 still ran — not skipped for structured-only.
        parts["metric_resolver"].match.assert_awaited()
        parts["strategy_resolve"].assert_awaited()
        # Vector search is disabled by passing embedding=None into Tier 3.
        _, kwargs = parts["knowledge_retriever"].resolve.call_args
        assert kwargs["embedding"] is None

    @pytest.mark.asyncio
    async def test_mixed_namespace_skips_nothing(self):
        """Both source types present → run everything (Tier 2 executes)."""
        registry = _registry_with_composition(has_structured_source=True, has_unstructured_source=True)
        orch, parts = _make_orchestrator(tier2_success=True, sources_registry=registry, return_parts=True)
        request = InvokeRequest(query="q", namespace="mixed")
        response = await orch.resolve(request)

        # Tier 2 ran and produced the answer.
        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()

    @pytest.mark.asyncio
    async def test_unknown_composition_fails_open(self):
        """Registry returns unknown (error/timeout) → nothing skipped."""
        registry = _registry_with_composition(has_structured_source=False, has_unstructured_source=False, unknown=True)
        orch, parts = _make_orchestrator(tier2_success=True, sources_registry=registry, return_parts=True)
        request = InvokeRequest(query="q", namespace="ns")
        response = await orch.resolve(request)

        # unknown → treated as has-everything → Tier 2 runs.
        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()

    @pytest.mark.asyncio
    async def test_lookup_exception_fails_open(self):
        """An unexpected error from the registry must fail open (run all tiers)."""
        registry = _registry_with_composition(
            has_structured_source=False, has_unstructured_source=True, raises=RuntimeError("ddb boom")
        )
        orch, parts = _make_orchestrator(tier2_success=True, sources_registry=registry, return_parts=True)
        request = InvokeRequest(query="q", namespace="ns")
        response = await orch.resolve(request)

        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()

    @pytest.mark.asyncio
    async def test_explicit_tier_override_bypasses_gating(self):
        """A user tierOverride is intent — gating must not override it. tierOverride=2
        on a document-only namespace still runs Tier 2 (registry not consulted)."""
        registry = _registry_with_composition(has_structured_source=False, has_unstructured_source=True)
        orch, parts = _make_orchestrator(tier2_success=True, sources_registry=registry, return_parts=True)
        request = InvokeRequest(query="q", namespace="doc-only", options={"tierOverride": 2})
        response = await orch.resolve(request)

        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()
        registry.get_source_composition.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_registry_skips_nothing(self):
        """No registry configured → feature inert, Tier 2 runs as before."""
        orch, parts = _make_orchestrator(tier2_success=True, sources_registry=None, return_parts=True)
        request = InvokeRequest(query="q", namespace="ns")
        response = await orch.resolve(request)

        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()

    @pytest.mark.asyncio
    async def test_document_only_records_routing_trace(self):
        """A skip is surfaced in the trace via ROUTING_SELECT."""
        registry = _registry_with_composition(has_structured_source=False, has_unstructured_source=True)
        orch = _make_orchestrator(tier2_success=True, sources_registry=registry)
        trace = TraceCollector()
        request = InvokeRequest(query="q", namespace="doc-only")
        await orch.resolve(request, trace=trace)

        step = _find_step(trace.steps, "routing.select")
        assert step is not None
        # Document-only gates BOTH Tier 1 and Tier 2 (no DATABASE source).
        assert "tier1" in step.detail["skipped"]
        assert "tier2" in step.detail["skipped"]


@pytest.mark.unit
class TestTierGating:
    """The TierGating value object — pure mapping logic, no orchestrator needed."""

    @staticmethod
    def _composition(*, has_structured_source, has_unstructured_source):
        from coa_serve.clients.sources_registry import SourceComposition

        return SourceComposition(
            has_structured_source=has_structured_source, has_unstructured_source=has_unstructured_source
        )

    def test_default_skips_nothing(self):
        g = TierGating()
        assert (g.skip_tier1, g.skip_tier2, g.skip_tier3_vector_search) == (False, False, False)
        assert g.any_skipped is False
        assert g.skipped == []

    def test_document_only_gates_tier1_and_tier2_together(self):
        """No DATABASE source → Tier 1 and Tier 2 gate as one (they must never drift)."""
        g = TierGating.from_composition(self._composition(has_structured_source=False, has_unstructured_source=True))
        assert g.skip_tier1 is True
        assert g.skip_tier2 is True
        assert g.skip_tier3_vector_search is False
        assert g.skipped == ["tier1", "tier2"]

    def test_structured_only_gates_only_vector_search(self):
        g = TierGating.from_composition(self._composition(has_structured_source=True, has_unstructured_source=False))
        assert (g.skip_tier1, g.skip_tier2) == (False, False)
        assert g.skip_tier3_vector_search is True
        # Label matches the t3.vector_search trace step id, distinct from a whole-tier skip.
        assert g.skipped == ["tier3_vector_search"]

    def test_mixed_skips_nothing(self):
        g = TierGating.from_composition(self._composition(has_structured_source=True, has_unstructured_source=True))
        assert g.any_skipped is False
        assert g.skipped == []


@pytest.mark.unit
class TestTier2SkipEvaluatorHook:
    """Orchestrator's guarded call into an optional Tier2SkipEvaluator.

    Only a SKIP decision prunes Tier 2; RUN/ABSTAIN run it. The evaluator is not
    consulted when an override is set or when namespace-level gating already skipped
    Tier 2, and its absence (None) is a pure no-op.
    """

    @staticmethod
    def _evaluator(decision):
        ev = MagicMock()
        ev.should_skip_tier2 = AsyncMock(return_value=decision)
        return ev

    @pytest.mark.asyncio
    async def test_skip_decision_skips_tier2(self):
        from coa_serve.tier2.skip import Tier2SkipDecision

        ev = self._evaluator(Tier2SkipDecision.SKIP)
        orch, parts = _make_orchestrator(tier2_success=True, tier2_skip_evaluator=ev, return_parts=True)
        response = await orch.resolve(InvokeRequest(query="what does the policy say?", namespace="mixed"))

        assert response.result.tier == 3  # Tier 2 pruned → falls to Tier 3
        parts["strategy_resolve"].assert_not_awaited()
        ev.should_skip_tier2.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_decision_runs_tier2(self):
        from coa_serve.tier2.skip import Tier2SkipDecision

        ev = self._evaluator(Tier2SkipDecision.RUN)
        orch, parts = _make_orchestrator(tier2_success=True, tier2_skip_evaluator=ev, return_parts=True)
        response = await orch.resolve(InvokeRequest(query="q", namespace="mixed"))

        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()

    @pytest.mark.asyncio
    async def test_abstain_decision_runs_tier2(self):
        from coa_serve.tier2.skip import Tier2SkipDecision

        ev = self._evaluator(Tier2SkipDecision.ABSTAIN)
        orch, parts = _make_orchestrator(tier2_success=True, tier2_skip_evaluator=ev, return_parts=True)
        response = await orch.resolve(InvokeRequest(query="q", namespace="mixed"))

        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()

    @pytest.mark.asyncio
    async def test_tier_override_bypasses_evaluator(self):
        from coa_serve.tier2.skip import Tier2SkipDecision

        ev = self._evaluator(Tier2SkipDecision.SKIP)
        orch, parts = _make_orchestrator(tier2_success=True, tier2_skip_evaluator=ev, return_parts=True)
        response = await orch.resolve(InvokeRequest(query="q", namespace="mixed", options={"tierOverride": 2}))

        assert response.result.tier == 2
        ev.should_skip_tier2.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_strategy_pin_bypasses_evaluator(self):
        """An explicit options['strategy'] is user intent — a SKIP must not override
        it (regression guard for the reviewed strategy-pin bug)."""
        from coa_serve.tier2.skip import Tier2SkipDecision
        from coa_serve.tier2.strategy import StrategyOption

        ev = self._evaluator(Tier2SkipDecision.SKIP)
        orch, parts = _make_orchestrator(tier2_success=True, tier2_skip_evaluator=ev, return_parts=True)
        # Pin ONTOP — the strategy the mock StructuredQueryTier registers.
        response = await orch.resolve(
            InvokeRequest(query="q", namespace="mixed", options={"strategy": StrategyOption.ONTOP})
        )

        assert response.result.tier == 2  # explicitly-pinned Tier 2 still runs
        parts["strategy_resolve"].assert_awaited()
        ev.should_skip_tier2.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluator_not_consulted_when_namespace_gating_already_skipped_tier2(self):
        """A structured-only namespace already skips Tier 2 via namespace-level gating;
        the per-query evaluator must not run (nothing to prune, no wasted AOSS search)."""
        from coa_serve.tier2.skip import Tier2SkipDecision

        registry = _registry_with_composition(has_structured_source=False, has_unstructured_source=True)
        ev = self._evaluator(Tier2SkipDecision.SKIP)
        orch, parts = _make_orchestrator(
            tier2_success=True, sources_registry=registry, tier2_skip_evaluator=ev, return_parts=True
        )
        await orch.resolve(InvokeRequest(query="q", namespace="doc-only"))
        ev.should_skip_tier2.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_evaluator_is_noop(self):
        orch, parts = _make_orchestrator(tier2_success=True, tier2_skip_evaluator=None, return_parts=True)
        response = await orch.resolve(InvokeRequest(query="q", namespace="mixed"))
        assert response.result.tier == 2
        parts["strategy_resolve"].assert_awaited()


@pytest.mark.unit
class TestEmbedQuerySecurity:
    """CWE-209: an embedding failure must not leak raw exception detail into the
    client-facing trace (only the exception type). Full detail stays in server logs.
    Preserves the coverage of the removed test_router_security.py on the live path."""

    @pytest.mark.asyncio
    async def test_embed_failure_trace_omits_raw_exception_string(self):
        orch = _make_orchestrator(tier2_success=False)
        secret = "arn:aws:iam::123456789012:role/secret-role at vpc-0abc"
        bedrock = AsyncMock()
        bedrock.embed.side_effect = RuntimeError(secret)
        orch._bedrock_client = bedrock

        trace = TraceCollector()
        await orch._embed_query("q", tier_override=None, trace=trace)

        step = _find_step(trace.steps, "query.embed")
        assert step is not None and step.status == "error"
        assert step.detail == {"error": "RuntimeError"}
        assert secret not in str(step.detail)


def _base_vector_hit(entity_type, entity_uri, hit_id="h1", score=0.9):
    """Build a base VectorHit (clients/base.py) as the OpenSearch vector client
    returns it — entity type + URI live in metadata, NOT as typed fields."""
    from coa_serve.clients.base import VectorHit

    return VectorHit(
        id=hit_id,
        text=entity_type,
        score=score,
        metadata={"entity_type": entity_type, "entity_uri": entity_uri},
    )


@pytest.mark.unit
class TestResolveSeedUris:
    """U1 (Change 1): _resolve_seed_uris mirrors the Tier-2 ontology-concept
    vector search and returns class + property URIs from base VectorHits.
    Fail-open: returns [] (no crash) when index/embedding/client is missing."""

    @pytest.mark.asyncio
    async def test_returns_class_and_property_uris(self):
        vector_client = AsyncMock()
        vector_client.search.return_value = [
            _base_vector_hit("class", "http://ex.org/Claim"),
            _base_vector_hit("property", "http://ex.org/covers", hit_id="h2"),
            _base_vector_hit("metric", "http://ex.org/revenue", hit_id="h3"),  # excluded
            _base_vector_hit("class", "", hit_id="h4"),  # empty URI dropped
        ]
        orch = _make_orchestrator(vector_client=vector_client, oss_ontology_index="scl-onto")

        uris = await orch._resolve_seed_uris([0.1] * 10, "insurance")

        assert uris == ["http://ex.org/Claim", "http://ex.org/covers"]
        vector_client.search.assert_awaited_once()
        # search must be scoped to the ontology index for this namespace
        _, kwargs = vector_client.search.call_args
        assert kwargs["index"] and "insurance" in kwargs["index"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_index(self):
        vector_client = AsyncMock()
        orch = _make_orchestrator(vector_client=vector_client, oss_ontology_index=None)
        assert await orch._resolve_seed_uris([0.1] * 10, "ns") == []
        vector_client.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_embedding(self):
        vector_client = AsyncMock()
        orch = _make_orchestrator(vector_client=vector_client, oss_ontology_index="scl-onto")
        assert await orch._resolve_seed_uris(None, "ns") == []
        vector_client.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_vector_client(self):
        orch = _make_orchestrator(vector_client=None, oss_ontology_index="scl-onto")
        assert await orch._resolve_seed_uris([0.1] * 10, "ns") == []

    @pytest.mark.asyncio
    async def test_search_failure_is_fail_open(self):
        vector_client = AsyncMock()
        vector_client.search.side_effect = RuntimeError("aoss down")
        orch = _make_orchestrator(vector_client=vector_client, oss_ontology_index="scl-onto")
        assert await orch._resolve_seed_uris([0.1] * 10, "ns") == []

    @pytest.mark.asyncio
    async def test_search_timeout_is_fail_open(self):
        """A slow AOSS query is bounded by the Tier-3 vector timeout (not the 60s
        client default); a timeout fails open to [] (keyword-branch fallback)."""
        from unittest.mock import patch

        async def _never(*args, **kwargs):
            await asyncio.sleep(3600)

        vector_client = AsyncMock()
        vector_client.search = _never
        orch = _make_orchestrator(vector_client=vector_client, oss_ontology_index="scl-onto")

        with patch("coa_serve.orchestrator.VECTOR_SEARCH_TIMEOUT_S", 0.01):
            assert await orch._resolve_seed_uris([0.1] * 10, "ns") == []


@pytest.mark.unit
class TestRunTier3SeedsEntityUris:
    """U2 (Change 1, Cause A regression guard): _run_tier3 passes non-empty
    entity_uris into knowledge_retriever.resolve() when the seed search yields
    hits, and None when it yields nothing (keyword fallback)."""

    @pytest.mark.asyncio
    async def test_run_tier3_passes_seed_uris_when_present(self):
        vector_client = AsyncMock()
        vector_client.search.return_value = [_base_vector_hit("class", "http://ex.org/Claim")]
        orch, parts = _make_orchestrator(return_parts=True, vector_client=vector_client, oss_ontology_index="scl-onto")

        await orch._run_tier3(
            "契約と請求の関係は？",
            "insurance",
            TraceCollector(),
            seed_embedding=[0.1] * 10,
        )

        _, kwargs = parts["knowledge_retriever"].resolve.call_args
        assert kwargs["entity_uris"] == ["http://ex.org/Claim"]

    @pytest.mark.asyncio
    async def test_run_tier3_passes_none_when_no_seed_hits(self):
        vector_client = AsyncMock()
        vector_client.search.return_value = []
        orch, parts = _make_orchestrator(return_parts=True, vector_client=vector_client, oss_ontology_index="scl-onto")

        await orch._run_tier3("What is the SLA?", "insurance", TraceCollector(), seed_embedding=[0.1] * 10)

        _, kwargs = parts["knowledge_retriever"].resolve.call_args
        assert kwargs["entity_uris"] is None
