# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for NLtoSQLStrategy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coa_serve.deadline import Deadline
from coa_serve.exceptions import AccessDeniedError, AmbiguousReferenceError
from coa_serve.tier2.nl_to_sql.strategy import NLtoSQLStrategy
from coa_serve.tier2.strategy import MAX_RESULT_ROWS, StrategyContext, StrategyOption, StrategyResult
from coa_serve.tier2.table_qualifier import PreparedSQL
from coa_serve.trace import TraceCollector

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_nl_to_sql_result(
    *,
    sql: str = "SELECT count(*) FROM orders",
    error: str | None = None,
    confidence: float = 0.85,
    retrieved_tables: list[str] | None = None,
    expanded_tables: list[str] | None = None,
    data_source_id: str = "ds-1",
    table_sources: dict[str, str] | None = None,
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


# ── Tests ────────────────────────────────────────────────────────────────


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
        # A genuine generation failure is an error step.
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

        # The executor should receive the rewritten (authorized) SQL
        execute_call_args = query_executor.execute.call_args
        assert execute_call_args[0][0] == rewritten_sql
        # The result should also contain the authorized SQL
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
        # First shot failed → one corrective regeneration attempted.
        sql_generator.correct.assert_called_once()
        # Two execution attempts total (shot 1 + shot 2), bounded by max_shots.
        assert query_executor.execute.call_count == 2

    # ── Two-shot self-correction ────────────────────────────────────────────

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
        # correct() got the verbatim engine error + prior SQL + reused schema.
        args, kwargs = sql_generator.correct.call_args
        assert "does not exist" in kwargs["execution_error"]
        assert kwargs["failed_sql"] == "SELECT bad FROM orders"
        # question + ddl_context are passed positionally.
        assert args[0] == "How many orders?"
        assert args[1] == "CREATE TABLE orders (id int);"

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
        # The whole point: no correction shot on a clean empty result.
        sql_generator.correct.assert_not_called()
        assert query_executor.execute.call_count == 1
        # The empty result is surfaced as a deterministic answer in the trace.
        exec_steps = [s for s in context.trace.steps if s.step == "t2.sql.execute" and s.status == "success"]
        assert exec_steps and exec_steps[-1].detail.get("deterministicEmpty") is True

    @pytest.mark.asyncio
    async def test_nonexistent_entity_value_is_not_coaxed_into_rows(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # Regression: a query naming a non-existent entity ('Gamma')
        # filters to 0 rows. The strategy must NOT feed that back to the model as
        # "likely wrong — loosen the filter", which is what fabricated rows for a
        # different, real value ('Alpha') nondeterministically across runs. If
        # correct() is never called, no such substitution can happen.
        sql_generator.generate.return_value = _make_nl_to_sql_result(sql="SELECT count(*) FROM t WHERE name='Gamma'")
        firewall.evaluate.return_value = _make_firewall_result(
            authorized_sql="SELECT count(*) FROM t WHERE name='Gamma'"
        )
        # If a correction were (wrongly) attempted, it would return rows for a
        # real value — assert we never get here.
        sql_generator.correct.return_value = ("SELECT count(*) FROM t WHERE name='Alpha'", 0.8)
        query_executor.execute.return_value = _make_exec_result(rows=[], row_count=0)
        context = _make_context()

        result = await strategy.resolve("How many records are associated with Gamma?", "ns1", context)

        assert result is not None
        assert result.row_count == 0
        sql_generator.correct.assert_not_called()
        assert query_executor.execute.call_count == 1

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
        # Exactly one correction (shot 2); the empty shot-2 result is accepted, not re-coaxed.
        sql_generator.correct.assert_called_once()
        assert query_executor.execute.call_count == 2
        exec_ok = [s for s in context.trace.steps if s.step == "t2.sql.execute" and s.status == "success"]
        assert exec_ok and exec_ok[-1].detail.get("deterministicEmpty") is True

    @pytest.mark.asyncio
    async def test_zero_rows_without_user_literal_still_self_corrects(
        self, strategy, sql_generator, firewall, query_executor
    ):
        # AC: correction must STILL fire for a genuinely-wrong query whose zero
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
        # No user literal in the filter → the zero-row result was corrected.
        sql_generator.correct.assert_called_once()
        assert query_executor.execute.call_count == 2
        # The correction feedback must NOT tell the model to alter a value.
        _, kwargs = sql_generator.correct.call_args
        assert "ZERO rows" in kwargs["execution_error"]
        assert "Do NOT invent or alter" in kwargs["execution_error"]

    @pytest.mark.asyncio
    async def test_max_shots_one_disables_correction(self, sql_generator, firewall, query_executor):
        # max_shots=1 → pure one-shot; a failed execution returns None with no
        # correction attempt (backwards-compatible with the pre-two-shot path).
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
        """Cap is MAX_RESULT_ROWS (raised 1000 -> 10_000 once Athena results paginate)."""
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.options["maxResults"] = 5000  # below the new cap -> passes through

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

    @pytest.mark.asyncio
    async def test_evidence_truncated_to_500_chars(self, strategy, sql_generator, firewall, query_executor):
        sql_generator.generate.return_value = _make_nl_to_sql_result()
        firewall.evaluate.return_value = _make_firewall_result()
        query_executor.execute.return_value = _make_exec_result()
        context = _make_context()
        context.options["evidence"] = "x" * 1000  # Longer than 500

        await strategy.resolve("query", "ns1", context)

        generate_kwargs = sql_generator.generate.call_args[1]
        assert len(generate_kwargs["evidence"]) == 500


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

    @pytest.mark.asyncio
    async def test_firewall_sees_the_unqualified_sql(self):
        """The rewrite MUST run downstream of authorization, on this path too.

        Cedar receives ``SQLFirewall.extract_tables()`` output verbatim, so
        qualifying first would change the strings a table-scoped policy matches on.
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

        This path previously executed the unqualified statement instead, on the
        theory that ``sql_table_routing`` can never produce an ambiguous name —
        true today, but a property of a different module that nothing enforced.
        Executing risks reading a different source's table than the one authorized,
        and a correction shot would hit the same ambiguity, so the strategy raises a
        terminal ``AmbiguousReferenceError`` (surfaced to the client) rather than
        executing or silently skipping.
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


def _make_context_with_deadline(budget_s: float) -> StrategyContext:
    """A StrategyContext carrying a request-scoped deadline (A0)."""
    ctx = _make_context()
    ctx.deadline = Deadline.from_budget(budget_s, None)
    return ctx


@pytest.mark.unit
class TestNLtoSQLStrategyDeadline:
    """A4/A6 — the correction shot and executor timeout become deadline-aware.

    The invariant under test: a request-scoped deadline never STARTS a second
    generate→execute cycle it cannot finish (which is what 504s the caller after
    a usable first-shot answer already exists), while the pre-A0 behaviour — no
    deadline, or an ample one — is preserved exactly.
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
        # The skip is observable in the trace as a reasoned skipped step.
        skipped = [s for s in context.trace.steps if s.step == "t2.sql.generate" and s.status == "skipped"]
        assert skipped, "deadline skip must record an observable skipped step"
        # Trace-latency invariant: a step that did NOT run reports 0ms duration and
        # carries NO latency-looking / budget-derived number in its detail. Step
        # latency reflects real elapsed time only; the budget is an independent
        # dimension. Guards against re-mixing seconds into a non-event step.
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
        # REGRESSION GUARD for the 170s AgentCore/Playground path: with an ample
        # budget the second shot always fits, so self-correction fires exactly as
        # before A0. This is the behaviour we must NOT break.
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
    async def test_no_deadline_preserves_pre_a0_correction(self, strategy, sql_generator, firewall, query_executor):
        # No deadline threaded (context.deadline is None) → the correction is
        # unconditional, identical to the pre-A0 code path.
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
