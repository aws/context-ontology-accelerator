# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for NLtoSQLStrategy."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.deadline import Deadline
from coa_serve.exceptions import AccessDeniedError, AmbiguousReferenceError
from coa_serve.tier1.metric_resolver import DeclinedMetricContext
from coa_serve.tier2.nl_to_sql.sql_generator import WriterOptions
from coa_serve.tier2.nl_to_sql.strategy import (
    NLtoSQLStrategy,
    _Candidate,
    _pick_winner,
    _result_signature,
    uploaded_ontology_iris,
)
from coa_serve.tier2.strategy import (
    EMPTY_RESULT_CONFIDENCE_FLOOR,
    MAX_RESULT_ROWS,
    StrategyContext,
    StrategyOption,
    StrategyResult,
)
from coa_serve.tier2.table_qualifier import PreparedSQL
from coa_serve.trace import TraceCollector


def _make_nl_to_sql_result(
    *,
    sql: str = "SELECT count(*) FROM orders",
    error: str | None = None,
    confidence: float = 0.85,
    retrieved_tables: list[str] | None = None,
    expanded_tables: list[str] | None = None,
    data_source_id: str = "ds-1",
    table_sources: dict[str, str] | None = None,
    table_candidates: dict[str, list[str]] | None = None,
):
    """Build a mock NLtoSQLResult."""
    result = MagicMock()
    result.sql = sql
    result.error = error
    result.confidence = confidence
    result.retrieved_tables = retrieved_tables or ["orders"]
    result.expanded_tables = expanded_tables or ["orders", "customers"]
    result.trace_steps = []
    result.data_source_id = data_source_id
    result.ddl_context = "CREATE TABLE orders (id int);"
    # Explicit, not a MagicMock attribute: cross-source qualification reads this
    # per shot, and an auto-created mock would fabricate table->source entries.
    result.table_sources = table_sources or {}
    result.table_candidates = table_candidates or {}
    return result


def _make_firewall_result(*, denied: bool = False, authorized_sql: str = "", reason: str | None = None):
    """Build a mock FirewallResult."""
    result = MagicMock()
    result.denied = denied
    result.authorized_sql = authorized_sql or "SELECT count(*) FROM orders"
    result.reason = reason
    return result


def _make_exec_result(*, rows: list[dict] | None = None, columns: list[str] | None = None, row_count: int = 1):
    """Build a mock QueryResult."""
    result = MagicMock()
    result.rows = rows or [{"count": 42}]
    result.columns = columns or ["count"]
    result.row_count = row_count
    result.truncated = False
    return result


def _make_context() -> StrategyContext:
    return StrategyContext(
        embedding=[0.1] * 10,
        profile={"userId": "user-1"},
        options={"maxResults": 100, "dataSourceId": "ds-1", "evidence": "some evidence text"},
        trace=TraceCollector(),
    )


@pytest.mark.unit
class TestNLtoSQLStrategyResolve:
    @pytest.fixture
    def sql_generator(self):
        return AsyncMock()

    @pytest.fixture
    def firewall(self):
        return MagicMock()

    @pytest.fixture
    def query_executor(self):
        return AsyncMock()

    @pytest.fixture
    def strategy(self, sql_generator, firewall, query_executor):
        return NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
        )

    @pytest.mark.asyncio
    async def test_declined_metric_becomes_governed_metric_argument(
        self, strategy, sql_generator, firewall, query_executor
    ):
        """The strategy is the seam between the declined match and the SQL writer. If
        it drops ``context.declined_metric``, the formula never reaches the prompt no
        matter how well the orchestrator threaded it."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.declined_metric = DeclinedMetricContext(
            metric_id="demo:open_claim_count",
            metric_name="open_claim_count",
            sql_template="SELECT count(*) FROM claims WHERE claim_status_code = 'OPEN'",
            unhandled_qualifier="anycompany",
        )

        await strategy.resolve("How many open claims does AnyCompany have?", "ns", context)

        governed = sql_generator.generate.call_args.kwargs["governed_metric"]
        assert "claim_status_code = 'OPEN'" in governed
        assert "'anycompany'" in governed

    @pytest.mark.asyncio
    async def test_no_declined_metric_passes_empty_governed_metric(
        self, strategy, sql_generator, firewall, query_executor
    ):
        """The common path must pass an empty string, not None — the generator
        concatenates it into the prompt."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()

        await strategy.resolve("who are our customers?", "ns", _make_context())

        assert sql_generator.generate.call_args.kwargs["governed_metric"] == ""

    @pytest.mark.asyncio
    async def test_templateless_declined_metric_forwards_nothing(
        self, strategy, sql_generator, firewall, query_executor
    ):
        """A declined metric with no formula must not
        put a block in the writer's prompt, even though context.declined_metric is
        non-None."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.declined_metric = DeclinedMetricContext(
            metric_id="demo:revenue",
            metric_name="revenue",
            sql_template="",  # matched by name, no formula
            unhandled_qualifier="last quarter",
        )

        await strategy.resolve("what was revenue last quarter?", "ns", context)

        assert sql_generator.generate.call_args.kwargs["governed_metric"] == ""

    @pytest.mark.asyncio
    async def test_governed_metric_suppressed_when_source_contradicts_resolved(
        self, strategy, sql_generator, firewall, query_executor
    ):
        """A metric authored against one source must NOT be forwarded when the request
        pins a different source: preserving a formula whose tables aren't in the
        resolved schema burns the correction shot or steers wrong-source SQL."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()  # options pins dataSourceId="ds-1"
        context.declined_metric = DeclinedMetricContext(
            metric_id="demo:open_claim_count",
            metric_name="open_claim_count",
            sql_template="SELECT count(*) FROM claims",
            unhandled_qualifier="anycompany",
            data_source_id="ds-claims",  # DIFFERENT from the pinned ds-1
        )

        await strategy.resolve("How many open claims does AnyCompany have?", "ns", context)

        assert sql_generator.generate.call_args.kwargs["governed_metric"] == ""

    @pytest.mark.asyncio
    async def test_governed_metric_forwarded_when_source_agrees(
        self, strategy, sql_generator, firewall, query_executor
    ):
        """Control: when the metric's source matches the resolved one, the block is
        still forwarded — the mismatch gate must not suppress the happy path."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()  # options pins dataSourceId="ds-1"
        context.declined_metric = DeclinedMetricContext(
            metric_id="demo:open_claim_count",
            metric_name="open_claim_count",
            sql_template="SELECT count(*) FROM claims",
            unhandled_qualifier="anycompany",
            data_source_id="ds-1",  # SAME as the pinned source
        )

        await strategy.resolve("How many open claims does AnyCompany have?", "ns", context)

        assert "claims" in sql_generator.generate.call_args.kwargs["governed_metric"]

    @pytest.mark.asyncio
    async def test_success_path_returns_strategy_result(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()

        result = await strategy.resolve("How many orders?", "ns1", context)

        assert result is not None
        assert isinstance(result, StrategyResult)
        assert result.strategy_name == StrategyOption.NL_TO_SQL
        assert result.sql == "SELECT count(*) FROM orders"
        assert result.rows == [{"count": 42}]
        assert result.columns == ["count"]
        assert result.confidence == 0.85
        assert result.row_count == 1
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_table_placements_resolve_through_the_executor(
        self, strategy, sql_generator, firewall, query_executor
    ):
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        query_executor.resolve_table_placements.return_value = {"orders": ("ds-1", "sales_db")}

        await strategy.resolve("How many orders?", "ns1", _make_context())

        resolver = sql_generator.generate.call_args.kwargs["table_placements"]
        assert await resolver({"orders": ""}) == {"orders": ("ds-1", "sales_db")}
        query_executor.resolve_table_placements.assert_awaited_once_with("ns1", {"orders": ""})

    @pytest.mark.asyncio
    async def test_same_name_databases_resolve_through_the_executor(
        self, strategy, sql_generator, firewall, query_executor
    ):
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        query_executor.resolve_same_name_databases.return_value = {"ds": {"db1": "s1", "db2": "s2"}}

        await strategy.resolve("How many orders?", "ns1", _make_context())

        resolver = sql_generator.generate.call_args.kwargs["same_name_databases"]
        assert await resolver(["ds"]) == {"ds": {"db1": "s1", "db2": "s2"}}
        query_executor.resolve_same_name_databases.assert_awaited_once_with("ns1", ["ds"])

    @pytest.mark.asyncio
    async def test_correct_strategy_result_fields(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            retrieved_tables=["orders", "products"],
            expanded_tables=["orders", "products", "categories"],
            data_source_id="my-ds",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql="SELECT 1 FROM orders")
        query_executor.execute.return_value = _make_exec_result(row_count=5)
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result.retrieved_tables == ["orders", "products"]
        assert result.expanded_tables == ["orders", "products", "categories"]
        assert result.data_source_id == "my-ds"
        assert result.row_count == 5

    @pytest.mark.asyncio
    async def test_sql_generation_error_returns_none(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="", error="LLM timeout")
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        firewall.evaluate.assert_not_called()
        query_executor.execute.assert_not_called()
        gen = [s for s in context.trace.steps if s.step == "t2.sql.generate"]
        assert gen and gen[0].status == "error"

    @pytest.mark.asyncio
    async def test_retrieve_failed_is_skipped_not_error(self, strategy, sql_generator, firewall, query_executor):
        # A retrieval-backend failure (vector proxy/index unavailable) means the
        # NL→SQL strategy could not start — it's a graceful SKIP with fallthrough
        # to VKG, not an error. Otherwise every query shows a red step when only
        # the optional NL→SQL retrieval path is degraded.
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql="", error="retrieve_failed: Vector search proxy returned an error"
        )
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        gen = [s for s in context.trace.steps if s.step == "t2.sql.generate"]
        assert gen and gen[0].status == "skipped", "retrieve_failed should record a skipped step, not error"

    @pytest.mark.asyncio
    async def test_empty_sql_generated_returns_none(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="", error=None)
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        firewall.evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_firewall_denied_raises_access_denied_error(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result(denied=True, reason="Table not in allowlist")
        context = _make_context()

        with pytest.raises(AccessDeniedError):
            await strategy.resolve("query", "ns1", context)

        query_executor.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsafe_sql_records_firewall_error_and_returns_none(
        self, strategy, sql_generator, firewall, query_executor
    ):
        from coa_serve.tier2.sql_firewall import UnsafeSQLError

        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.side_effect = UnsafeSQLError("Only SELECT statements are allowed, got: Union")
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        # Falls through (returns None) so the next strategy can try, but the
        # failure must be visible in the trace as an error firewall step.
        assert result is None
        query_executor.execute.assert_not_called()
        fw_err = [s for s in context.trace.steps if s.step == "t2.sql.firewall" and s.status == "error"]
        assert fw_err, "expected a t2.sql.firewall/error step for unsafe SQL"
        # The trace reaches the client: a fixed code, never the firewall message
        # (which quotes the offending statement).
        assert fw_err[0].detail == {"error": "unsafe_sql"}

    @pytest.mark.asyncio
    async def test_firewall_allows_with_rewritten_sql_executes_authorized_sql(
        self, strategy, sql_generator, firewall, query_executor
    ):
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT * FROM orders")
        rewritten_sql = "SELECT id, total FROM orders"
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=rewritten_sql)
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        execute_call_args = query_executor.execute.call_args
        assert execute_call_args[0][0] == rewritten_sql
        assert result.sql == rewritten_sql

    @pytest.mark.asyncio
    async def test_no_query_executor_returns_none(self, sql_generator, firewall):
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=None,
        )
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None

    @pytest.mark.asyncio
    async def test_execution_failure_returns_none(self, strategy, sql_generator, firewall, query_executor):
        # Both shots fail to execute → strategy falls through (returns None). With
        # the default 2-shot cap, the corrective regeneration is attempted once.
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        sql_generator.correct.return_value = ("SELECT count(*) FROM orders", 0.8)
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.side_effect = Exception("Database connection lost")
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        sql_generator.correct.assert_called_once()
        assert query_executor.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_correction_recovers_after_execution_error(self, strategy, sql_generator, firewall, query_executor):
        # Shot 1 SQL raises a DB error; the LLM correction produces valid SQL that
        # executes on shot 2 → the strategy returns the corrected result.
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT bad FROM orders")
        sql_generator.correct.return_value = ("SELECT count(*) FROM orders", 0.9)
        firewall.evaluate.side_effect = [
            _make_firewall_result(authorized_sql="SELECT bad FROM orders"),
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders"),
        ]
        query_executor.execute.side_effect = [
            Exception('column "bad" does not exist'),
            _make_exec_result(row_count=7),
        ]
        context = _make_context()

        result = await strategy.resolve("How many orders?", "ns1", context)

        assert result is not None
        assert result.sql == "SELECT count(*) FROM orders"
        assert result.row_count == 7
        args, kwargs = sql_generator.correct.call_args
        assert "does not exist" in kwargs["execution_error"]
        assert kwargs["failed_sql"] == "SELECT bad FROM orders"
        assert args[0] == "How many orders?"
        assert args[1] == "CREATE TABLE orders (id int);"

    @pytest.mark.asyncio
    async def test_correction_receives_the_statement_that_actually_ran(
        self, strategy, sql_generator, firewall, query_executor
    ):
        """On a cross-source query the executed statement is the QUALIFIED rewrite
        (catalog/schema prefixes, single-line rendering), not the firewall-authorized
        text. The engine reports its error line/column against the executed statement,
        so the correction prompt must carry that text — otherwise the model is pointed
        at coordinates inside a statement it never saw."""
        authorized = "SELECT count(*) FROM orders JOIN crm.customers ON TRUE"
        executed = 'SELECT count(*) FROM "awsdatacatalog"."sales"."orders" JOIN "pg_cat"."crm"."customers" ON TRUE'
        # Two real sources: that is what makes ``_prepare_sql`` run the qualifier at all.
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=authorized, table_sources={"orders": "ds-glue", "customers": "ds-pg"}
        )
        sql_generator.correct.return_value = ("SELECT count(*) FROM orders", 0.9)
        firewall.evaluate.side_effect = [
            _make_firewall_result(authorized_sql=authorized),
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders"),
        ]
        query_executor.execute.side_effect = [
            Exception("line 1:28: Table 'awsdatacatalog.sales.orders' does not exist"),
            _make_exec_result(row_count=3),
        ]
        prepare = AsyncMock(side_effect=[PreparedSQL(sql=executed), PreparedSQL(sql="SELECT count(*) FROM orders")])
        with patch("coa_serve.tier2.nl_to_sql.strategy.prepare_execution_sql", prepare):
            result = await strategy.resolve("How many orders?", "ns1", _make_context())

        assert result is not None
        assert result.row_count == 3
        # Bind the two seams: shot 1 executed the qualified text, shot 2 the corrected one...
        assert query_executor.execute.call_args_list[0].args[0] == executed
        assert query_executor.execute.call_args_list[1].args[0] == "SELECT count(*) FROM orders"
        # ...and correct() was handed exactly the text that shot 1 executed.
        _, kwargs = sql_generator.correct.call_args
        assert kwargs["failed_sql"] == executed

    @pytest.mark.asyncio
    async def test_no_correction_when_first_shot_succeeds(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is not None
        sql_generator.correct.assert_not_called()
        assert query_executor.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_zero_rows_returned_deterministically_no_correction(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # A clean execution that returns ZERO rows is a deterministic ANSWER, not
        # an error signal. The strategy must return it as-is and must NOT spend a
        # correction shot trying to coax rows out of it.
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT * FROM orders WHERE region='XX'")
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql="SELECT * FROM orders WHERE region='XX'")
        query_executor.execute.return_value = _make_exec_result(rows=[], row_count=0)
        context = _make_context()

        result = await strategy.resolve("orders in XX?", "ns1", context)

        assert result is not None
        assert result.row_count == 0
        sql_generator.correct.assert_not_called()
        assert query_executor.execute.call_count == 1
        exec_steps = [s for s in context.trace.steps if s.step == "t2.sql.execute" and s.status == "success"]
        assert exec_steps and exec_steps[-1].detail.get("deterministicEmpty") is True

    @pytest.mark.asyncio
    async def test_empty_result_no_correction_when_max_shots_one(self, sql_generator, firewall, query_executor):
        # max_shots=1: an empty result is returned as-is (no correction attempt).
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
            max_shots=1,
        )
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result(rows=[], row_count=0)
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is not None
        assert result.row_count == 0
        sql_generator.correct.assert_not_called()

    @pytest.mark.asyncio
    async def test_correction_returns_empty_sql_records_error_trace_step(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # Shot 1 raises a DB error → correction is attempted, but the LLM returns
        # empty SQL. That failed correction must be visible in the trace as an
        # error step (not just a log line), and the strategy falls through.
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT bad FROM orders")
        sql_generator.correct.return_value = ("", 0.0)
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql="SELECT bad FROM orders")
        query_executor.execute.side_effect = Exception('column "bad" does not exist')
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        gen_errors = [
            s
            for s in context.trace.steps
            if s.step == "t2.sql.generate" and s.status == "error" and "empty SQL" in str(s.detail)
        ]
        assert gen_errors, "expected an error t2.sql.generate step when correction returns empty SQL"

    @pytest.mark.asyncio
    async def test_correction_raises_records_error_trace_step(self, strategy, sql_generator, firewall, query_executor):
        # Shot 1 raises a DB error → the correction call itself raises. That must
        # also surface as an error trace step (symmetry with the empty-SQL branch).
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT bad FROM orders")
        sql_generator.correct.side_effect = RuntimeError("bedrock throttled")
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql="SELECT bad FROM orders")
        query_executor.execute.side_effect = Exception('column "bad" does not exist')
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        gen_errors = [
            s
            for s in context.trace.steps
            if s.step == "t2.sql.generate" and s.status == "error" and "RuntimeError" in str(s.detail)
        ]
        assert gen_errors, "expected an error t2.sql.generate step when the correction call raises"

    @pytest.mark.asyncio
    async def test_correction_returns_zero_rows_is_accepted(self, strategy, sql_generator, firewall, query_executor):
        # Cross-shot determinism: shot 1 raises a DB error → correction runs →
        # shot 2 executes cleanly but returns ZERO rows. That verified-empty
        # correction result must be ACCEPTED deterministically (returned as-is
        # with the deterministicEmpty marker), never re-coaxed for more rows.
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT bad FROM orders")
        sql_generator.correct.return_value = ("SELECT * FROM orders WHERE status='void'", 0.8)
        firewall.evaluate.side_effect = [
            _make_firewall_result(authorized_sql="SELECT bad FROM orders"),
            _make_firewall_result(authorized_sql="SELECT * FROM orders WHERE status='void'"),
        ]
        query_executor.execute.side_effect = [
            Exception('column "bad" does not exist'),
            _make_exec_result(rows=[], row_count=0),
        ]
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is not None
        assert result.row_count == 0
        sql_generator.correct.assert_called_once()
        assert query_executor.execute.call_count == 2
        exec_ok = [s for s in context.trace.steps if s.step == "t2.sql.execute" and s.status == "success"]
        assert exec_ok and exec_ok[-1].detail.get("deterministicEmpty") is True

    @pytest.mark.asyncio
    async def test_zero_rows_without_user_literal_still_self_corrects(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # Correction must STILL fire for a genuinely-wrong query whose zero
        # rows are NOT caused by a user-supplied value (e.g. a bad JOIN). The
        # question names no entity; shot 1's SQL carries no literal from it, so
        # the empty result is treated as a possibly-wrong query and corrected.
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"
        )
        sql_generator.correct.return_value = ("SELECT count(*) FROM orders", 0.8)
        firewall.evaluate.side_effect = [
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"),
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders"),
        ]
        query_executor.execute.side_effect = [
            _make_exec_result(rows=[], row_count=0),
            _make_exec_result(rows=[{"count": 5}], row_count=5),
        ]
        context = _make_context()

        result = await strategy.resolve("how many orders", "ns1", context)

        assert result is not None
        assert result.row_count == 5
        sql_generator.correct.assert_called_once()
        assert query_executor.execute.call_count == 2
        # The correction feedback must NOT tell the model to alter a value.
        _, kwargs = sql_generator.correct.call_args
        assert "ZERO rows" in kwargs["execution_error"]
        assert "Do NOT invent or alter" in kwargs["execution_error"]

    @pytest.mark.asyncio
    async def test_max_shots_one_disables_correction(self, sql_generator, firewall, query_executor):
        # max_shots=1 → pure one-shot; a failed execution returns None with no
        # correction attempt.
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
            max_shots=1,
        )
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.side_effect = Exception("boom")
        context = _make_context()

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        sql_generator.correct.assert_not_called()
        assert query_executor.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_execution_passes_correct_parameters(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result(data_source_id="custom-ds")
        authorized_sql = "SELECT 1 FROM t"
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=authorized_sql)
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.options["maxResults"] = 500

        await strategy.resolve("query", "ns1", context)

        query_executor.execute.assert_called_once_with(
            authorized_sql,
            namespace="ns1",
            data_source_id="custom-ds",
            max_rows=500,
            timeout_seconds=35,
        )

    @pytest.mark.asyncio
    async def test_max_rows_capped_at_system_max(self, strategy, sql_generator, firewall, query_executor):
        """A value below MAX_RESULT_ROWS passes through."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.options["maxResults"] = 5000

        await strategy.resolve("query", "ns1", context)

        call_kwargs = query_executor.execute.call_args[1]
        assert call_kwargs["max_rows"] == 5000

    @pytest.mark.asyncio
    async def test_max_rows_capped_above_system_max(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.options["maxResults"] = MAX_RESULT_ROWS * 10

        await strategy.resolve("query", "ns1", context)

        call_kwargs = query_executor.execute.call_args[1]
        assert call_kwargs["max_rows"] == MAX_RESULT_ROWS


@pytest.mark.unit
class TestNLtoSQLCrossSourceQualification:
    """Cross-source qualification on the NL→SQL path: qualify SQL that spans two sources.

    The generator authors BARE table names (its prompt examples are bare), so a
    statement joining two sources hits Athena's single pinned (Catalog, Database)
    context and fails TABLE_NOT_FOUND. Qualification happens in the strategy —
    after the firewall, per shot — because a corrected statement may reference a
    different set of tables.
    """

    _SQL = "SELECT a.id FROM claims a JOIN policies b ON a.id = b.claim_id"

    def _registry(self, sources):
        reg = AsyncMock()
        reg.get_source.side_effect = lambda namespace, data_source_id: sources.get(data_source_id, {})
        return reg

    def _strategy(self, sql_generator, firewall, query_executor, registry):
        return NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
            sources_registry=registry,
        )

    @pytest.mark.asyncio
    async def test_cross_source_sql_is_qualified(self):
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=self._SQL,
            # Retrieval mixed two sources; the generated SQL touches both.
            table_sources={"claims": "ds-pg", "policies": "ds-glue"},
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=self._SQL)
        query_executor.execute.return_value = _make_exec_result()
        registry = self._registry(
            {
                "ds-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]},
                "ds-glue": {"glueDatabaseName": "insurance"},
            }
        )
        strategy = self._strategy(sql_generator, firewall, query_executor, registry)

        result = await strategy.resolve("cross-source question", "ns1", _make_context())

        executed = query_executor.execute.call_args.args[0].replace('"', "")
        assert "pg_cat.public.claims" in executed
        assert "awsdatacatalog.insurance.policies" in executed
        # The reported SQL is the SQL that actually ran.
        assert result.sql == query_executor.execute.call_args.args[0]

    @pytest.mark.asyncio
    async def test_single_source_sql_unchanged(self):
        """A single-source statement must stay bare so it keeps the JDBC fast path."""
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=self._SQL,
            table_sources={"claims": "ds-pg", "policies": "ds-pg"},
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=self._SQL)
        query_executor.execute.return_value = _make_exec_result()
        registry = self._registry({"ds-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]}})
        strategy = self._strategy(sql_generator, firewall, query_executor, registry)

        await strategy.resolve("single-source question", "ns1", _make_context())

        assert query_executor.execute.call_args.args[0] == self._SQL
        registry.get_source.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ambiguous_table_name_is_refused_not_executed(self):
        """A table name owned by two classes → refuse the query, never guess.

        Executing bare would run against whichever schema the executor defaulted
        to and return a wrong answer as success — the silent-wrong-answer path.
        The strategy raises a terminal ``AmbiguousReferenceError`` (surfaced to the
        client as an explicit 400) rather than executing or silently skipping.
        """
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        sql = "SELECT a.id FROM customers a JOIN policies b ON a.id = b.claim_id"
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=sql,
            # _hit_table_source_map marks a name claimed by two classes ambiguous
            # (here two schemas of one source, or two sources — same marker).
            table_sources={"customers": "__ambiguous__", "policies": "ds-glue"},
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=sql)
        query_executor.execute.return_value = _make_exec_result()
        registry = self._registry({"ds-glue": {"glueDatabaseName": "insurance"}})
        strategy = self._strategy(sql_generator, firewall, query_executor, registry)

        with pytest.raises(AmbiguousReferenceError):
            await strategy.resolve("ambiguous question", "ns1", _make_context())

        query_executor.execute.assert_not_awaited()

    # Same-name tables in two databases: what the generator hands over for two
    # `customers` classes whose databases are known (see SQLGenerator.generate).
    _SAME_NAME_SOURCES = {
        "customers": "__ambiguous__",
        "sales_db.customers": "ds-sales",
        "crm_db.customers": "ds-crm",
    }
    _SAME_NAME_CANDIDATES = {"customers": ["crm_db.customers", "sales_db.customers"]}

    def _same_name_strategy(self, sql):
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=sql,
            table_sources=dict(self._SAME_NAME_SOURCES),
            table_candidates=dict(self._SAME_NAME_CANDIDATES),
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=sql)
        query_executor.execute.return_value = _make_exec_result()
        registry = self._registry(
            {"ds-sales": {"glueDatabaseName": "sales_db"}, "ds-crm": {"glueDatabaseName": "crm_db"}}
        )
        return self._strategy(sql_generator, firewall, query_executor, registry), query_executor

    @pytest.mark.asyncio
    async def test_bare_same_name_reference_is_refused_naming_the_candidates(self):
        strategy, query_executor = self._same_name_strategy("SELECT COUNT(*) FROM customers WHERE tier = 'gold'")

        with pytest.raises(AmbiguousReferenceError) as err:
            await strategy.resolve("How many customers have gold tier?", "ns1", _make_context())

        assert "crm_db.customers" in str(err.value)
        assert "sales_db.customers" in str(err.value)
        query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_qualified_same_name_reference_runs_against_that_database(self):
        strategy, query_executor = self._same_name_strategy("SELECT COUNT(*) FROM crm_db.customers WHERE tier = 'gold'")

        await strategy.resolve("How many customers have gold tier?", "ns1", _make_context())

        executed = query_executor.execute.call_args.args[0].replace('"', "").lower()
        # The name is shared across sources, so the reference carries its catalog too.
        assert "awsdatacatalog.crm_db.customers" in executed
        assert "sales_db" not in executed

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT COUNT(*) FROM wrongcat.crm_db.customers WHERE tier = 'gold'",
            "SELECT COUNT(*) FROM other_db.customers WHERE tier = 'gold'",
            "SELECT * FROM crm_db.customers c JOIN wrongcat.crm_db.customers w ON w.customer_id = c.customer_id",
        ],
        ids=["other-catalog", "other-database", "other-catalog-beside-the-right-one"],
    )
    async def test_same_name_reference_naming_no_candidate_is_refused(self, sql):
        strategy, query_executor = self._same_name_strategy(sql)

        with pytest.raises(AmbiguousReferenceError) as err:
            await strategy.resolve("How many customers have gold tier?", "ns1", _make_context())

        assert "crm_db.customers,sales_db.customers" in str(err.value)
        query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cte_named_like_the_shared_table_runs_against_the_qualified_one(self):
        # A common table expression named after its source table is not a bare
        # reference to the shared name: the query names crm_db.customers and runs.
        sql = "WITH customers AS (SELECT * FROM crm_db.customers) SELECT COUNT(*) FROM customers"
        strategy, query_executor = self._same_name_strategy(sql)

        await strategy.resolve("How many CRM customers are there?", "ns1", _make_context())

        executed = query_executor.execute.call_args.args[0].replace('"', "").lower()
        assert "from awsdatacatalog.crm_db.customers" in executed
        assert "sales_db" not in executed

    @pytest.mark.asyncio
    async def test_cte_named_like_the_shared_table_over_a_bare_reference_is_refused(self):
        sql = "WITH customers AS (SELECT * FROM customers) SELECT COUNT(*) FROM customers"
        strategy, query_executor = self._same_name_strategy(sql)

        with pytest.raises(AmbiguousReferenceError) as err:
            await strategy.resolve("How many customers are there?", "ns1", _make_context())

        assert "crm_db.customers,sales_db.customers" in str(err.value)
        query_executor.execute.assert_not_awaited()

    def _one_of_three_strategy(self, sql, writer=None):
        """The real generator over a namespace with one `ds` per study, of which retrieval returns one."""
        from coa_serve.clients.base import ConverseResult, VectorHit
        from coa_serve.tier2.nl_to_sql.sql_generator import SQLGenerator

        llm, vector = AsyncMock(), AsyncMock()
        llm.embed.return_value = [0.1] * 8
        llm.converse.return_value = ConverseResult(text=f"```sql\n{sql}\n```\nConfidence: 0.9")
        if writer is not None:
            llm.converse.side_effect = writer
        vector.search.return_value = [
            VectorHit(
                id="ds-102",
                text="Table: ds | Columns: usubjid (varchar), dsdecod (varchar)",
                score=0.9,
                metadata={"data_source_id": "src-102", "uri": "urn:x#Ds_102"},
            )
        ]
        databases = {"src-101": "sdtm_cs101", "src-102": "sdtm_cs102", "src-103": "sdtm_cs103"}
        firewall, query_executor = MagicMock(), AsyncMock()
        firewall.evaluate.side_effect = lambda written, *a, **k: _make_firewall_result(authorized_sql=written)
        query_executor.execute.return_value = _make_exec_result()
        query_executor.resolve_table_placements.side_effect = lambda ns, ts: {
            t: (s, databases[s]) for t, s in ts.items() if s in databases
        }
        query_executor.resolve_same_name_databases.side_effect = lambda ns, tables: (
            {"ds": {db: src for src, db in databases.items()}} if "ds" in tables else {}
        )
        registry = self._registry({src: {"glueDatabaseName": db} for src, db in databases.items()})
        generator = SQLGenerator(llm_client=llm, vector_client=vector)
        return self._strategy(generator, firewall, query_executor, registry), query_executor

    @pytest.mark.asyncio
    async def test_one_retrieved_copy_of_a_shared_name_is_refused_naming_every_database(self):
        # Only one study's `ds` was retrieved, so the count must not be scoped to it.
        strategy, query_executor = self._one_of_three_strategy("SELECT count(*) FROM ds")

        with pytest.raises(AmbiguousReferenceError) as err:
            await strategy.resolve("How many patients enrolled across all studies?", "ns1", _make_context())

        assert "sdtm_cs101.ds,sdtm_cs102.ds,sdtm_cs103.ds" in str(err.value)
        query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unretrieved_copy_named_by_its_database_runs_against_it(self):
        strategy, query_executor = self._one_of_three_strategy("SELECT count(*) FROM sdtm_cs101.ds")

        await strategy.resolve("How many patients enrolled in study CS-101?", "ns1", _make_context())

        executed = query_executor.execute.call_args.args[0].replace('"', "").lower()
        assert "from awsdatacatalog.sdtm_cs101.ds" in executed

    @staticmethod
    async def _schema_reading_writer(prompt, **kwargs):
        """``count(*)`` over ``ds``, qualified only with a database the question names and the prompt lists."""
        from coa_serve.clients.base import ConverseResult

        question = kwargs.get("guard_content") or ""
        line = next((ln for ln in prompt.splitlines() if ln.startswith("Table: ds |")), "")
        segment = line.split(" | ")[1] if " | " in line else ""
        listed = []
        if segment.startswith(("Database: ", "Databases: ")):
            listed = [d.strip() for d in segment.split(": ", 1)[1].split(" (", 1)[0].split(",")]
        named = next((d for d in listed if d and d in question), "")
        table = f"{named}.ds" if named else "ds"
        return ConverseResult(text=f"```sql\nSELECT count(*) FROM {table}\n```\nConfidence: 0.9")

    @pytest.mark.asyncio
    async def test_question_naming_one_database_of_a_partial_name_runs_against_it(self):
        # The question names sdtm_cs103; the writer must be told which databases hold `ds`.
        strategy, query_executor = self._one_of_three_strategy("", writer=self._schema_reading_writer)

        await strategy.resolve(
            "How many subjects are in the ds table of the sdtm_cs103 database?", "ns1", _make_context()
        )

        executed = query_executor.execute.call_args.args[0].replace('"', "").lower()
        assert "from awsdatacatalog.sdtm_cs103.ds" in executed

    @pytest.mark.asyncio
    async def test_across_all_question_with_the_database_list_is_still_refused(self):
        strategy, query_executor = self._one_of_three_strategy("", writer=self._schema_reading_writer)

        with pytest.raises(AmbiguousReferenceError) as err:
            await strategy.resolve("How many subjects are there across all studies?", "ns1", _make_context())

        assert "sdtm_cs101.ds,sdtm_cs102.ds,sdtm_cs103.ds" in str(err.value)
        query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_glue_catalog_qualified_same_name_reference_runs_unchanged(self):
        sql = 'SELECT COUNT(*) FROM "AwsDataCatalog"."crm_db"."customers" WHERE tier = 1'
        strategy, query_executor = self._same_name_strategy(sql)

        await strategy.resolve("How many customers have gold tier?", "ns1", _make_context())

        assert query_executor.execute.call_args.args[0] == sql

    @pytest.mark.asyncio
    async def test_firewall_sees_the_unqualified_sql(self):
        """The rewrite MUST run downstream of authorization, on this path too.

        Database-qualified grant entries and Cedar see the references the SQL
        makes, so qualifying first would change what they match on.
        The VKG path has the same assertion; keeping both makes the ordering
        executable rather than a comment in one module.
        """
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=self._SQL,
            table_sources={"claims": "ds-pg", "policies": "ds-glue"},
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=self._SQL)
        query_executor.execute.return_value = _make_exec_result()
        registry = self._registry(
            {
                "ds-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]},
                "ds-glue": {"glueDatabaseName": "insurance"},
            }
        )
        strategy = self._strategy(sql_generator, firewall, query_executor, registry)

        await strategy.resolve("cross-source question", "ns1", _make_context())

        assert firewall.evaluate.call_args.args[0] == self._SQL
        assert query_executor.execute.call_args.args[0] != self._SQL

    @pytest.mark.asyncio
    async def test_unattributable_reference_abandons_the_strategy(self):
        """Fail closed, matching the VKG path.

        An unattributable reference raises a terminal ``AmbiguousReferenceError``
        rather than executing or silently skipping.
        """
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=self._SQL,
            table_sources={"claims": "ds-pg", "policies": "ds-glue"},
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=self._SQL)
        query_executor.execute.return_value = _make_exec_result()
        strategy = self._strategy(sql_generator, firewall, query_executor, self._registry({}))

        with (
            patch(
                "coa_serve.tier2.nl_to_sql.strategy.prepare_execution_sql",
                AsyncMock(return_value=PreparedSQL(sql=self._SQL, error="table 'claims' is ambiguous")),
            ),
            pytest.raises(AmbiguousReferenceError),
        ):
            await strategy.resolve("cross-source question", "ns1", _make_context())

        query_executor.execute.assert_not_called()

    # These drive the real, unpatched ``prepare_execution_sql`` to prove each
    # metadata gap is refused.

    def _cross_source_mocks(self):
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql=self._SQL,
            table_sources={"claims": "ds-pg", "policies": "ds-glue"},
            data_source_id="",
        )
        firewall.evaluate.return_value = _make_firewall_result(authorized_sql=self._SQL)
        query_executor.execute.return_value = _make_exec_result()
        return sql_generator, firewall, query_executor

    @pytest.mark.asyncio
    async def test_no_registry_fails_closed_without_executing(self):
        sql_generator, firewall, query_executor = self._cross_source_mocks()
        strategy = self._strategy(sql_generator, firewall, query_executor, None)

        with pytest.raises(AmbiguousReferenceError, match="sources registry is unavailable"):
            await strategy.resolve("cross-source question", "ns1", _make_context())

        query_executor.execute.assert_not_awaited()
        # Terminal: a correction shot would hit the same missing metadata.
        sql_generator.correct.assert_not_called()

    @pytest.mark.asyncio
    async def test_datasource_missing_from_registry_fails_closed_without_executing(self):
        sql_generator, firewall, query_executor = self._cross_source_mocks()
        # ds-glue is routed but has no registry record.
        registry = self._registry({"ds-pg": {"athenaDataCatalogName": "pg_cat", "discoveredSchemas": ["public"]}})
        strategy = self._strategy(sql_generator, firewall, query_executor, registry)

        with pytest.raises(AmbiguousReferenceError, match="datasource 'ds-glue' was not found"):
            await strategy.resolve("cross-source question", "ns1", _make_context())

        query_executor.execute.assert_not_awaited()
        sql_generator.correct.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_schema_metadata_fails_closed_without_executing(self):
        sql_generator, firewall, query_executor = self._cross_source_mocks()
        # A federated source with no discoveredSchemas: schema_for_source returns
        # "" and the retrieval hits carry no per-table schema.
        registry = self._registry(
            {
                "ds-pg": {"athenaDataCatalogName": "pg_cat"},
                "ds-glue": {"glueDatabaseName": "insurance"},
            }
        )
        strategy = self._strategy(sql_generator, firewall, query_executor, registry)

        with pytest.raises(AmbiguousReferenceError, match="'claims' has no source schema metadata"):
            await strategy.resolve("cross-source question", "ns1", _make_context())

        query_executor.execute.assert_not_awaited()
        sql_generator.correct.assert_not_called()


def _make_context_with_deadline(budget_s: float) -> StrategyContext:
    """A StrategyContext carrying a request-scoped deadline."""
    ctx = _make_context()
    ctx.deadline = Deadline.from_budget(budget_s, None)
    return ctx


@pytest.mark.unit
class TestNLtoSQLStrategyDeadline:
    """The correction shot and executor timeout are deadline-aware.

    A request-scoped deadline never STARTS a second generate→execute cycle it
    cannot finish; with no deadline, or an ample one, behaviour is unchanged.
    """

    @pytest.fixture
    def sql_generator(self):
        return AsyncMock()

    @pytest.fixture
    def firewall(self):
        return MagicMock()

    @pytest.fixture
    def query_executor(self):
        return AsyncMock()

    @pytest.fixture
    def strategy(self, sql_generator, firewall, query_executor):
        return NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
        )

    @pytest.mark.asyncio
    async def test_exhausted_deadline_skips_correction_returns_empty_fallback(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # Shot 1 returns a clean zero-row result that WOULD normally trigger a
        # correction (no user literal). With an already-exhausted budget the
        # correction is skipped and the verified-empty first-shot result is
        # returned instead of overrunning the ceiling and 504-ing.
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"
        )
        firewall.evaluate.return_value = _make_firewall_result(
            authorized_sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"
        )
        query_executor.execute.return_value = _make_exec_result(rows=[], row_count=0)
        # 1s budget — the first shot's own elapsed time already exceeds what's
        # left, so a second shot cannot fit.
        context = _make_context_with_deadline(1.0)

        result = await strategy.resolve("how many orders", "ns1", context)

        assert result is not None
        assert result.row_count == 0  # the clean empty first-shot result
        sql_generator.correct.assert_not_called()
        assert query_executor.execute.call_count == 1
        skipped = [s for s in context.trace.steps if s.step == "t2.sql.generate" and s.status == "skipped"]
        assert skipped, "deadline skip must record an observable skipped step"
        # A skipped step reports 0ms and no budget numbers.
        skip_step = skipped[0]
        assert skip_step.duration_ms == 0
        detail = skip_step.detail if isinstance(skip_step.detail, dict) else {}
        assert detail.get("reason") == "deadline_insufficient_for_correction"
        assert "lastShotSeconds" not in detail
        assert "remainingSeconds" not in detail

    @pytest.mark.asyncio
    async def test_exhausted_deadline_on_exec_error_returns_none(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # Execution error with an exhausted budget: no clean fallback exists, so
        # the strategy skips the correction and returns None (fall through to the
        # next strategy) rather than starting a shot that cannot return.
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.side_effect = Exception("boom")
        context = _make_context_with_deadline(1.0)

        result = await strategy.resolve("query", "ns1", context)

        assert result is None
        sql_generator.correct.assert_not_called()
        assert query_executor.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_ample_deadline_still_self_corrects(self, strategy, sql_generator, firewall, query_executor):
        # With an ample budget, the correction still fires.
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"
        )
        sql_generator.correct.return_value = ("SELECT count(*) FROM orders", 0.8)
        firewall.evaluate.side_effect = [
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"),
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders"),
        ]
        query_executor.execute.side_effect = [
            _make_exec_result(rows=[], row_count=0),
            _make_exec_result(rows=[{"count": 5}], row_count=5),
        ]
        context = _make_context_with_deadline(170.0)

        result = await strategy.resolve("how many orders", "ns1", context)

        assert result is not None
        assert result.row_count == 5
        sql_generator.correct.assert_called_once()
        assert query_executor.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_no_deadline_preserves_correction(self, strategy, sql_generator, firewall, query_executor):
        # No deadline threaded (context.deadline is None) → the correction is
        # unconditional.
        sql_generator.generate.return_value = _make_nl_to_sql_result(
            sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"
        )
        sql_generator.correct.return_value = ("SELECT count(*) FROM orders", 0.8)
        firewall.evaluate.side_effect = [
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders o JOIN customers c ON o.id = c.id"),
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders"),
        ]
        query_executor.execute.side_effect = [
            _make_exec_result(rows=[], row_count=0),
            _make_exec_result(rows=[{"count": 5}], row_count=5),
        ]
        context = _make_context()  # no deadline
        assert context.deadline is None

        result = await strategy.resolve("how many orders", "ns1", context)

        assert result.row_count == 5
        sql_generator.correct.assert_called_once()

    @pytest.mark.asyncio
    async def test_executor_timeout_default_without_deadline(self, strategy, sql_generator, firewall, query_executor):
        # No deadline → the historical fixed 35s executor timeout is preserved.
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()

        await strategy.resolve("query", "ns1", _make_context())

        assert query_executor.execute.call_args[1]["timeout_seconds"] == 35

    @pytest.mark.asyncio
    async def test_executor_timeout_clamped_to_remaining_budget(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # A tight deadline shrinks the per-statement executor timeout below 35s so
        # a single statement cannot itself overrun the request budget. Value is an
        # int (executor contract) and never exceeds the 35s default.
        import structlog

        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()

        with structlog.testing.capture_logs() as logs:
            await strategy.resolve("query", "ns1", _make_context_with_deadline(10.0))

        timeout = query_executor.execute.call_args[1]["timeout_seconds"]
        assert isinstance(timeout, int)
        assert 1 <= timeout < 35  # derived from ~10s budget minus margin
        # A below-default clamp is surfaced so a mis-tuned margin/floor is visible.
        assert any(e.get("event") == "nl_to_sql_exec_timeout_clamped" for e in logs)

    @pytest.mark.asyncio
    async def test_execute_step_latency_is_measured_not_the_cap(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # Trace-latency invariant (executor side): even when a tight deadline caps
        # timeout_seconds below 35, the recorded t2.sql.execute latency is the REAL
        # elapsed time of the call, never the cap value. Budget shapes the cap;
        # it must not become a fabricated latency number.
        import asyncio as _asyncio

        async def _slow_exec(*_args, **_kwargs):
            await _asyncio.sleep(0.05)  # 50ms real work
            return _make_exec_result()

        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.side_effect = _slow_exec

        context = _make_context_with_deadline(10.0)
        await strategy.resolve("query", "ns1", context)

        exec_steps = [s for s in context.trace.steps if s.step == "t2.sql.execute"]
        assert exec_steps
        # ~50ms measured, NOT the multi-second timeout cap. Generous upper bound
        # to stay non-flaky while still excluding any cap-derived value.
        assert 0 <= exec_steps[0].duration_ms < 1000


@pytest.mark.unit
class TestUploadedOntologyIris:
    """The top-down fallback's lookup: uploaded (user_created) ontologies of one namespace."""

    @pytest.mark.asyncio
    async def test_queries_user_created_ontologies_in_the_namespace(self):
        graph = AsyncMock()
        graph.query.return_value = [{"o": "https://ex.org/up"}, {"o": ""}]

        iris = await uploaded_ontology_iris(graph, "ns-a", "https://ontology-workbench.local/{namespace}")

        assert iris == ["https://ex.org/up"]
        sparql = graph.query.call_args.args[0]
        assert "<https://ontology-workbench.local/vocab#ontology_type> ?type" in sparql
        # Both user-loaded types: the web app uploads as "user_uploaded".
        assert '"user_created"' in sparql and '"user_uploaded"' in sparql
        assert '"induced"' not in sparql and '"foundational"' not in sparql
        assert "owl:Ontology" in sparql
        assert '"https://ontology-workbench.local/ns-a/"' in sparql

    @pytest.mark.asyncio
    async def test_rejects_an_unsafe_namespace(self):
        graph = AsyncMock()
        with pytest.raises(ValueError):
            await uploaded_ontology_iris(graph, 'ns"} INJECT {', "https://g.local/{namespace}")
        graph.query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_strategy_passes_the_resolver_when_a_graph_is_wired(self, monkeypatch):
        monkeypatch.setenv("GRAPH_URI_TEMPLATE", "https://ontology-workbench.local/{namespace}")
        graph = AsyncMock()
        graph.query.return_value = [{"o": "https://ex.org/up"}]
        sql_generator = AsyncMock()
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="", error="no_tables_retrieved")
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator, firewall=MagicMock(), query_executor=None, graph_client=graph
        )

        await strategy.resolve("How many orders?", "ns-a", _make_context())

        resolver = sql_generator.generate.call_args.kwargs["uploaded_ontologies"]
        assert await resolver() == ["https://ex.org/up"]


def _exec_rows(rows: list[dict]):
    result = MagicMock()
    result.rows = rows
    result.columns = list(rows[0]) if rows else []
    result.row_count = len(rows)
    result.truncated = False
    result.engine = "athena"
    return result


def _vote_context(k: object | None = 3) -> StrategyContext:
    ctx = _make_context()
    if k is not None:
        ctx.options["flatSqlVoteK"] = k
    return ctx


def _steps(context: StrategyContext, step: str) -> list:
    return [s for s in context.trace.steps if s.step == step]


@pytest.mark.unit
class TestNLtoSQLVote:
    @pytest.fixture(autouse=True)
    def _no_vote_env(self, monkeypatch):
        monkeypatch.delenv("SERVE_NL2SQL_VOTE_K", raising=False)

    @pytest.fixture
    def sql_generator(self):
        return AsyncMock()

    @pytest.fixture
    def firewall(self):
        return MagicMock()

    @pytest.fixture
    def query_executor(self):
        return AsyncMock()

    @pytest.fixture
    def strategy(self, sql_generator, firewall, query_executor):
        return NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
        )

    @pytest.fixture
    def wire(self, sql_generator, firewall, query_executor):
        def _wire(statements: list[str], results: dict, *, denied: tuple[str, ...] = ()):
            sql_generator.generate.return_value = _make_nl_to_sql_result(sql=statements[0], confidence=0.9)
            sql_generator.generate_from_context.side_effect = [(s, 0.8) for s in statements[1:]]
            firewall.evaluate.side_effect = lambda sql, profile, namespace: _make_firewall_result(
                denied=sql in denied, authorized_sql=sql, reason="table not granted" if sql in denied else None
            )

            async def execute(sql, **_kwargs):
                out = results[sql]
                if isinstance(out, Exception):
                    raise out
                return out

            query_executor.execute.side_effect = execute

        return _wire

    @pytest.mark.asyncio
    async def test_extra_candidates_get_the_first_candidates_writer_options(self, strategy, sql_generator, wire):
        same = "SELECT count(*) FROM orders"
        wire([same, same, same], {same: _exec_rows([{"count": 42}])})
        context = _vote_context(3)
        context.options.update({"evidence": "E" * 3000, "evidenceMaxChars": 3000, "sqlTimeRules": True})

        await strategy.resolve("How many orders?", "ns", context)

        first = sql_generator.generate.call_args.kwargs
        assert first["writer"].time_rules is True
        assert sql_generator.generate_from_context.await_count == 2
        for call in sql_generator.generate_from_context.call_args_list:
            assert call.kwargs["writer"] == first["writer"]
            assert call.kwargs["evidence_max_chars"] == first["evidence_max_chars"] == 3000

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("env", "option"), [(None, None), ("1", None), ("3", "1")])
    async def test_k1_runs_the_single_shot_path(
        self, monkeypatch, strategy, sql_generator, query_executor, wire, env, option
    ):
        if env is not None:
            monkeypatch.setenv("SERVE_NL2SQL_VOTE_K", env)
        wire(["SELECT count(*) FROM orders"], {"SELECT count(*) FROM orders": _exec_rows([{"count": 42}])})
        context = _vote_context(option)

        result = await strategy.resolve("How many orders?", "ns", context)

        assert result.rows == [{"count": 42}]
        assert result.confidence == 0.9
        sql_generator.generate_from_context.assert_not_awaited()
        assert query_executor.execute.await_count == 1
        assert _steps(context, "t2.sql.vote") == []
        assert [s.step for s in context.trace.steps] == [
            "t2.sql.generate",
            "t2.sql.authorize",
            "t2.sql.firewall",
            "t2.sql.execute",
        ]
        assert "candidate" not in _steps(context, "t2.sql.generate")[0].detail

    @pytest.mark.asyncio
    async def test_identical_statements_execute_once_and_are_unanimous(self, strategy, query_executor, wire):
        wire(
            ["SELECT count(*) FROM orders", "SELECT count(*)\n  FROM orders;", "SELECT  count(*) FROM orders"],
            {"SELECT count(*) FROM orders": _exec_rows([{"count": 42}])},
        )
        context = _vote_context()

        result = await strategy.resolve("How many orders?", "ns", context)

        assert result.rows == [{"count": 42}]
        assert result.confidence == 0.6
        assert query_executor.execute.await_count == 1
        (vote,) = _steps(context, "t2.sql.vote")
        assert vote.detail["votes"] == 3
        assert vote.detail["distinctSql"] == 1
        assert vote.detail["selfConfidence"] == 0.9

    @pytest.mark.asyncio
    async def test_majority_wins_across_aliases_and_row_order(self, strategy, wire):
        b = "SELECT region FROM orders"
        a1 = "SELECT id AS order_id, region FROM orders WHERE amount > 10"
        a2 = "SELECT region, id FROM orders WHERE amount > 10 ORDER BY id DESC"
        wire(
            [b, a1, a2],
            {
                b: _exec_rows([{"region": "west"}]),
                a1: _exec_rows([{"order_id": 1, "region": "east"}, {"order_id": 2, "region": "west"}]),
                a2: _exec_rows([{"region": "west", "id": 2}, {"region": "east", "id": 1}]),
            },
        )
        context = _vote_context()

        result = await strategy.resolve("Which orders are over 10?", "ns", context)

        assert result.sql == a1
        assert result.row_count == 2
        assert result.confidence == 0.35
        (vote,) = _steps(context, "t2.sql.vote")
        assert vote.detail["winnerIndex"] == 1
        assert vote.detail["groupSizes"] == [2, 1]

    @pytest.mark.asyncio
    async def test_no_agreement_returns_candidate_zero_at_the_floor(self, strategy, wire):
        s0, s1, s2 = "SELECT 1 FROM orders", "SELECT 2 FROM orders", "SELECT 3 FROM orders"
        wire([s0, s1, s2], {s: _exec_rows([{"n": i}]) for i, s in enumerate([s0, s1, s2])})

        result = await strategy.resolve("q", "ns", _vote_context())

        assert result.sql == s0
        assert result.confidence == EMPTY_RESULT_CONFIDENCE_FLOOR

    @pytest.mark.asyncio
    async def test_non_empty_breaks_a_tie_with_empty(self, strategy, wire):
        s0, s1, s2 = "SELECT 1 FROM orders", "SELECT 2 FROM orders", "SELECT 3 FROM orders"
        wire([s0, s1, s2], {s0: _exec_rows([]), s1: _exec_rows([{"n": 7}]), s2: RuntimeError("boom")})
        context = _vote_context()

        result = await strategy.resolve("q", "ns", context)

        assert result.sql == s1
        assert result.rows == [{"n": 7}]
        (vote,) = _steps(context, "t2.sql.vote")
        assert vote.detail["tieBroken"] is True

    @pytest.mark.asyncio
    async def test_failed_execution_still_counts_toward_k(self, strategy, sql_generator, wire):
        s0, s1, s2 = "SELECT bad FROM orders", "SELECT count(*) FROM orders", "SELECT count(id) FROM orders"
        wire([s0, s1, s2], {s0: RuntimeError("no column bad"), s1: _exec_rows([{"c": 5}]), s2: _exec_rows([{"n": 5}])})
        context = _vote_context()

        result = await strategy.resolve("q", "ns", context)

        assert result.rows == [{"c": 5}]
        assert result.confidence == 0.35
        sql_generator.correct.assert_not_awaited()
        (vote,) = _steps(context, "t2.sql.vote")
        assert vote.detail["errored"] == 1
        assert vote.detail["votes"] == 2
        failed = [s for s in _steps(context, "t2.sql.execute") if s.status == "error"]
        assert failed[0].detail["candidate"] == 0

    @pytest.mark.asyncio
    async def test_denied_candidate_never_runs_and_the_rest_answer(self, strategy, query_executor, wire):
        s0, s1, s2 = "SELECT * FROM salaries", "SELECT count(*) FROM orders", "SELECT count(1) FROM orders"
        wire([s0, s1, s2], {s1: _exec_rows([{"c": 5}]), s2: _exec_rows([{"c": 5}])}, denied=(s0,))

        result = await strategy.resolve("q", "ns", _vote_context())

        assert result.rows == [{"c": 5}]
        assert result.confidence == 0.35
        assert s0 not in [c.args[0] for c in query_executor.execute.await_args_list]

    @pytest.mark.asyncio
    async def test_every_candidate_denied_raises(self, strategy, query_executor, wire):
        s0, s1, s2 = "SELECT * FROM salaries", "SELECT pay FROM salaries", "SELECT * FROM salaries s"
        wire([s0, s1, s2], {}, denied=(s0, s1, s2))

        context = _vote_context()
        with pytest.raises(AccessDeniedError):
            await strategy.resolve("q", "ns", context)

        query_executor.execute.assert_not_awaited()
        denials = [s for s in _steps(context, "t2.sql.authorize") if s.status == "denied"]
        assert len(denials) == 3

    @pytest.mark.asyncio
    async def test_empty_majority_without_user_literal_gets_one_correction(
        self, strategy, sql_generator, query_executor, wire
    ):
        s0 = "SELECT * FROM orders o JOIN customers c ON o.cid = c.id"
        s1 = "SELECT * FROM orders o JOIN customers c ON o.id = c.id"
        s2 = "SELECT id FROM orders"
        fixed = "SELECT * FROM orders o LEFT JOIN customers c ON o.cid = c.id"
        wire(
            [s0, s1, s2],
            {s0: RuntimeError("x"), s1: _exec_rows([]), s2: _exec_rows([]), fixed: _exec_rows([{"id": 1}])},
        )
        sql_generator.correct.return_value = (fixed, 0.7)

        result = await strategy.resolve("Which orders have customers?", "ns", _vote_context())

        assert result.rows == [{"id": 1}]
        assert sql_generator.correct.await_count == 1
        assert sql_generator.correct.call_args.kwargs["failed_sql"] == s1
        assert "ZERO rows" in sql_generator.correct.call_args.kwargs["execution_error"]
        assert sql_generator.generate_from_context.await_count == 2
        # The empty winner is corrected from the vote's own result, not re-run.
        assert [c.args[0] for c in query_executor.execute.await_args_list].count(s1) == 1

    @pytest.mark.asyncio
    async def test_empty_majority_with_user_literal_is_the_answer(self, strategy, sql_generator, query_executor, wire):
        s0 = "SELECT * FROM customers WHERE name = 'Zyxcorp'"
        s1 = "SELECT id FROM customers WHERE name = 'Zyxcorp'"
        wire([s0, s1, s0], {s0: _exec_rows([]), s1: _exec_rows([])})
        context = _vote_context()

        result = await strategy.resolve("Show the customer Zyxcorp", "ns", context)

        assert result is not None
        assert result.row_count == 0
        sql_generator.correct.assert_not_awaited()
        assert query_executor.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_no_clean_candidate_corrects_from_candidate_zero_error(
        self, strategy, sql_generator, query_executor, wire
    ):
        s0, s1, s2 = "SELECT bad FROM orders", "SELECT worse FROM orders", "SELECT worst FROM orders"
        fixed = "SELECT count(*) FROM orders"
        wire(
            [s0, s1, s2],
            {
                s0: RuntimeError("no column bad"),
                s1: RuntimeError("no column worse"),
                s2: RuntimeError("no column worst"),
                fixed: _exec_rows([{"c": 5}]),
            },
        )
        sql_generator.correct.return_value = (fixed, 0.7)

        result = await strategy.resolve("q", "ns", _vote_context())

        assert result.rows == [{"c": 5}]
        kwargs = sql_generator.correct.call_args.kwargs
        assert kwargs["failed_sql"] == s0
        assert kwargs["execution_error"] == "RuntimeError: no column bad"
        assert [c.args[0] for c in query_executor.execute.await_args_list].count(s0) == 1

    @pytest.mark.asyncio
    async def test_ambiguous_candidate_is_dropped_and_the_rest_answer(self, strategy, query_executor, wire):
        s0, s1, s2 = "SELECT * FROM claims", "SELECT count(*) FROM orders", "SELECT count(1) FROM orders"
        wire([s0, s1, s2], {s1: _exec_rows([{"c": 5}]), s2: _exec_rows([{"c": 5}])})

        async def prepare(sql, _nl_result, _namespace):
            return PreparedSQL(sql=sql, error="table 'claims' is ambiguous" if sql == s0 else None)

        with patch.object(strategy, "_prepare_sql", side_effect=prepare):
            result = await strategy.resolve("q", "ns", _vote_context())

        assert result.rows == [{"c": 5}]
        assert result.confidence == 0.35
        assert s0 not in [c.args[0] for c in query_executor.execute.await_args_list]

    @pytest.mark.asyncio
    async def test_all_refused_raises_the_lowest_index_refusal(self, strategy, query_executor, wire):
        s0, s1, s2 = "SELECT * FROM claims", "SELECT pay FROM salaries", "SELECT * FROM salaries s"
        wire([s0, s1, s2], {}, denied=(s1, s2))

        async def prepare(sql, _nl_result, _namespace):
            return PreparedSQL(sql=sql, error="table 'claims' is ambiguous")

        with patch.object(strategy, "_prepare_sql", side_effect=prepare), pytest.raises(AmbiguousReferenceError):
            await strategy.resolve("q", "ns", _vote_context())

        query_executor.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unexpected_failure_waits_for_sibling_executions(self, strategy, query_executor, wire):
        s0, s1 = "SELECT 1 FROM orders", "SELECT 2 FROM orders"
        wire([s0, s1, s1], {s1: _exec_rows([{"n": 2}])})

        async def prepare(sql, _nl_result, _namespace):
            if sql == s0:
                raise KeyError("routing")
            return PreparedSQL(sql=sql)

        with patch.object(strategy, "_prepare_sql", side_effect=prepare), pytest.raises(KeyError):
            await strategy.resolve("q", "ns", _vote_context())

        assert query_executor.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_short_deadline_skips_the_vote(self, strategy, sql_generator, query_executor, wire):
        wire(["SELECT count(*) FROM orders"], {"SELECT count(*) FROM orders": _exec_rows([{"count": 42}])})
        context = _make_context_with_deadline(1.0)
        context.options["flatSqlVoteK"] = 3

        result = await strategy.resolve("How many orders?", "ns", context)

        assert result.rows == [{"count": 42}]
        sql_generator.generate_from_context.assert_not_awaited()
        assert query_executor.execute.await_count == 1
        (vote,) = _steps(context, "t2.sql.vote")
        assert vote.status == "skipped"
        assert vote.detail["skippedReason"] == "deadline"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("env", "option", "k"), [("0", None, 1), ("9", None, 5), ("x", None, 1), ("5", 2, 2)])
    async def test_vote_k_is_clamped_and_overridable(self, monkeypatch, strategy, sql_generator, wire, env, option, k):
        monkeypatch.setenv("SERVE_NL2SQL_VOTE_K", env)
        stmt = "SELECT count(*) FROM orders"
        wire([stmt] * k, {stmt: _exec_rows([{"count": 42}])})

        await strategy.resolve("How many orders?", "ns", _vote_context(option))

        assert sql_generator.generate_from_context.await_count == k - 1


@pytest.mark.unit
class TestVoteHelpers:
    def test_signature_ignores_aliases_value_order_and_row_order(self):
        a = [{"id": 1, "region": "east"}, {"id": 2, "region": "west"}]
        b = [{"region": "west ", "order_id": 2}, {"region": "east", "order_id": 1}]
        assert _result_signature(a, False) == _result_signature(b, False)

    def test_signature_normalizes_numbers_dates_and_nulls(self):
        assert _result_signature([{"x": 0.1 + 0.2}], False) == _result_signature([{"x": Decimal("0.30")}], False)
        assert _result_signature([{"x": 100}], False) == _result_signature([{"x": 100.0}], False)
        assert _result_signature([{"x": 1234567}], False) != _result_signature([{"x": 1234568}], False)
        assert _result_signature([{"x": Decimal("0.30000001")}], False) != _result_signature([{"x": 0.3}], False)
        assert _result_signature([{"d": date(2024, 1, 2)}], False) == _result_signature([{"d": "2024-01-02"}], False)
        assert _result_signature([{"x": None}], False) != _result_signature([{"x": ""}], False)
        assert _result_signature([{"x": 1}], False) != _result_signature([{"x": 2}], False)

    def test_signature_counts_duplicate_rows_and_truncation(self):
        assert _result_signature([{"x": 1}, {"x": 1}], False) != _result_signature([{"x": 1}], False)
        assert _result_signature([{"x": 1}], True) != _result_signature([{"x": 1}], False)

    def test_pick_winner_with_no_clean_execution_has_no_winner(self):
        cands = [
            _Candidate(1, "s1", 0.5, status="exec_error", error="RuntimeError"),
            _Candidate(0, "s0", 0.9, status="refused"),
        ]
        outcome = _pick_winner(cands, 3)
        assert outcome.winner is None
        assert outcome.votes == 0
        assert outcome.errored == 1

    def test_pick_winner_prefers_size_then_non_empty_then_lowest_index(self):
        rows = [{"n": 1}]
        empty = [
            _Candidate(0, "e0", 0.5, rows=[], row_count=0, status="executed"),
            _Candidate(1, "e1", 0.5, rows=[], row_count=0, status="executed"),
        ]
        full = [_Candidate(2, "f", 0.5, rows=rows, row_count=1, status="executed")]
        assert _pick_winner(empty + full, 3).winner.index == 0
        tie = _pick_winner(
            [
                _Candidate(0, "e", 0.5, rows=[], row_count=0, status="executed"),
                _Candidate(1, "f", 0.5, rows=rows, row_count=1, status="executed"),
            ],
            2,
        )
        assert tie.winner.index == 1
        assert tie.tie_broken is True
        distinct = _pick_winner(
            [_Candidate(i, f"s{i}", 0.5, rows=[{"n": i}], row_count=1, status="executed") for i in (2, 0, 1)], 3
        )
        assert distinct.winner.index == 0


@pytest.mark.unit
class TestNLtoSQLStrategyWriterOptions:
    """The per-request evidence cap and writer options reach both the first shot and the correction."""

    @pytest.mark.asyncio
    async def test_evidence_cap_and_writer_reach_generate_and_correct(self):
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
        )
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT bad FROM orders")
        sql_generator.correct.return_value = ("SELECT count(*) FROM orders", 0.9)
        firewall.evaluate.side_effect = [
            _make_firewall_result(authorized_sql="SELECT bad FROM orders"),
            _make_firewall_result(authorized_sql="SELECT count(*) FROM orders"),
        ]
        query_executor.execute.side_effect = [Exception('column "bad" does not exist'), _make_exec_result()]
        evidence = "E" * 3000
        context = StrategyContext(
            embedding=[0.1] * 10,
            profile={"userId": "user-1"},
            options={
                "dataSourceId": "ds-1",
                "evidence": evidence,
                "evidenceMaxChars": 3000,
                "sqlTimeRules": True,
                "tokenCaps": {"nl2sql": 8000},
            },
            trace=TraceCollector(),
        )

        await strategy.resolve("How many orders?", "ns1", context)

        gen = sql_generator.generate.call_args.kwargs
        assert gen["evidence"] == evidence and gen["evidence_max_chars"] == 3000
        assert gen["writer"].time_rules is True and gen["writer"].max_tokens == 8000
        fix = sql_generator.correct.call_args.kwargs
        assert fix["evidence"] == evidence and fix["evidence_max_chars"] == 3000
        assert fix["writer"] == gen["writer"]

    @pytest.mark.asyncio
    async def test_default_keeps_the_500_char_cap_and_baseline_writer(self):
        sql_generator, firewall, query_executor = AsyncMock(), MagicMock(), AsyncMock()
        strategy = NLtoSQLStrategy(
            sql_generator=sql_generator,
            firewall=firewall,
            query_executor=query_executor,
            oss_ontology_index="test-index",
        )
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.options["evidence"] = "E" * 3000

        await strategy.resolve("How many orders?", "ns1", context)

        gen = sql_generator.generate.call_args.kwargs
        assert gen["evidence"] == "E" * 500 and gen["evidence_max_chars"] == 500
        assert gen["writer"] == WriterOptions()
