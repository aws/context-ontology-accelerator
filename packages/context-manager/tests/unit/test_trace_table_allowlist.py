# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A principal limited by ``tableAllowlist`` must not see other tables' names.

The SQL firewall already stops a query against a table outside the allowlist.
Retrieval and the FK walk, though, can put such tables in front of the SQL writer
without the SQL ever touching them, and their names used to reach the caller in
the ``t2.sql.generate`` / ``t2.sql.execute`` step details (streamed over SSE) and
in the response metadata. These tests pin every client-visible table list to the
grant, through the same allowlist normalization the firewall uses.

The ``t2.sql.generate`` / ``t2.sql.execute`` steps list the tables the SQL reads,
not the retrieval context, so even an unrestricted caller is never told a query
read a table it did not (``TestTraceListsTablesTheSqlReads``).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlglot
from coa_serve.exceptions import AccessDeniedError
from coa_serve.models import InvokeRequest
from coa_serve.sse_emitter import SSEEmitter
from coa_serve.step_ids import StepId
from coa_serve.tier2.cedar_authorizer import NullCedarAuthorizer
from coa_serve.tier2.nl_to_sql.strategy import NLtoSQLStrategy
from coa_serve.tier2.ontop.vkg_translator import Tier2Step
from coa_serve.tier2.sql_firewall import SQLFirewall, tables_visible_to
from coa_serve.tier2.strategy import StrategyContext, StrategyOption, StrategyResult
from coa_serve.trace import TraceCollector
from structlog.testing import capture_logs

from .test_orchestrator import _make_orchestrator
from .test_tier2_vkg_translator import _make_executor, _make_firewall, _make_translator, _make_vkg_client

pytestmark = pytest.mark.unit

ALLOWED = "orders"
RESTRICTED = ("employee_salaries", "customer_ssn")
ALLOWLIST_PROFILE = {"userId": "analyst", "tableAllowlist": [ALLOWED]}


class _Unprintable:
    """An allowlist member that cannot be turned into a table name."""

    def __str__(self) -> str:
        raise TypeError("not a table name")


# ── tables_visible_to ───────────────────────────────────────────────────


class TestTablesVisibleTo:
    def test_tables_visible_to_no_allowlist_returns_list_unchanged(self):
        tables = ["orders", "employee_salaries"]
        assert tables_visible_to(tables, {"userId": "u"}) == tables
        assert tables_visible_to(tables, None) == tables

    def test_tables_visible_to_allowlist_keeps_only_allowed_tables(self):
        assert tables_visible_to(["orders", *RESTRICTED], ALLOWLIST_PROFILE) == ["orders"]

    def test_tables_visible_to_preserves_order_of_allowed_tables(self):
        profile = {"tableAllowlist": ["customers", "orders"]}
        assert tables_visible_to(["orders", "secret", "customers"], profile) == ["orders", "customers"]

    def test_tables_visible_to_matches_case_insensitively_like_the_firewall(self):
        profile = {"tableAllowlist": ["Orders"]}
        assert tables_visible_to(["ORDERS", "orders", "salaries"], profile) == ["ORDERS", "orders"]

    def test_tables_visible_to_qualified_name_matches_on_bare_table_name(self):
        tables = ["catalog.schema.orders", '"db"."orders"', "db.employee_salaries"]
        assert tables_visible_to(tables, ALLOWLIST_PROFILE) == ["catalog.schema.orders", '"db"."orders"']

    def test_tables_visible_to_empty_allowlist_hides_every_table(self):
        assert tables_visible_to(["orders"], {"tableAllowlist": []}) == []

    def test_tables_visible_to_malformed_allowlist_fails_closed(self):
        assert tables_visible_to(["orders"], {"tableAllowlist": "orders"}) == []

    @pytest.mark.parametrize("allowlist", ["orders", [_Unprintable()]])
    def test_tables_visible_to_malformed_allowlist_logs_a_warning(self, allowlist):
        with capture_logs() as logs:
            assert tables_visible_to(["orders"], {"tableAllowlist": allowlist}) == []
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["trace_table_filter_malformed_allowlist"]

    @pytest.mark.parametrize("name", ["", ".", "db.", '""', "`[]"])
    def test_tables_visible_to_name_that_normalizes_to_empty_never_matches(self, name):
        # An empty-string entry in the allowlist must not let a nameless table through.
        assert tables_visible_to([name, "orders"], {"tableAllowlist": ["", "orders"]}) == ["orders"]

    def test_tables_visible_to_none_tables_returns_empty_list(self):
        assert tables_visible_to(None, ALLOWLIST_PROFILE) == []

    def test_tables_visible_to_agrees_with_firewall_decision(self):
        """A table listed in the trace is exactly a table the firewall lets the caller read."""
        firewall = SQLFirewall(cedar_authorizer=NullCedarAuthorizer())
        profile = {"tableAllowlist": ["ORDERS", "customers"]}
        for table in ("orders", "Customers", "db.orders", "employee_salaries"):
            allowed = not firewall.evaluate(f"SELECT 1 FROM {table}", profile).denied
            assert (tables_visible_to([table], profile) == [table]) is allowed, table


# ── NL→SQL strategy: trace streamed over SSE ────────────────────────────


def _nl_result(sql="SELECT count(*) AS n FROM orders"):
    r = MagicMock()
    r.sql = sql
    r.error = None
    r.confidence = 0.9
    r.retrieved_tables = ["orders"]
    # The FK walk appends neighbours the grant does not cover.
    r.expanded_tables = ["orders", *RESTRICTED]
    r.trace_steps = []
    r.data_source_id = "ds-1"
    r.ddl_context = "..."
    r.table_sources = {}
    return r


def _exec_result():
    r = MagicMock()
    r.rows = [{"n": 3}]
    r.columns = ["n"]
    r.row_count = 1
    r.truncated = False
    r.engine = "athena"
    return r


def _strategy(nl_result, executor=None, corrected_sql=None):
    gen = AsyncMock()
    gen.generate.return_value = nl_result
    if corrected_sql is not None:
        gen.correct.return_value = (corrected_sql, 0.8)
    if executor is None:
        executor = AsyncMock()
        executor.execute.return_value = _exec_result()
    return NLtoSQLStrategy(
        sql_generator=gen,
        firewall=SQLFirewall(cedar_authorizer=NullCedarAuthorizer()),
        query_executor=executor,
        oss_ontology_index="idx",
        max_shots=2,
    )


async def _resolve_streamed(profile, nl_result=None, executor=None, corrected_sql=None):
    """Run the strategy with a trace wired to a real SSEEmitter, as the streaming handler does."""
    emitter = SSEEmitter("req-1")

    async def on_step(step):
        emitter.queue_step(step)

    trace = TraceCollector(on_record=on_step)
    ctx = StrategyContext(
        embedding=[0.1] * 4,
        profile=profile,
        options={"maxResults": 10, "dataSourceId": "ds-1"},
        trace=trace,
    )
    strategy = _strategy(nl_result or _nl_result(), executor=executor, corrected_sql=corrected_sql)
    result = await strategy.resolve("how many orders?", "ns", ctx)
    await trace.flush()
    events = []
    while not emitter._queue.empty():
        events.append(emitter._queue.get_nowait())
    return result, trace, events


def _step_detail(events, step_id):
    return next(e["payload"]["detail"] for e in events if e["payload"]["stepName"] == step_id)


class TestNLtoSQLTraceTables:
    @pytest.mark.asyncio
    async def test_resolve_allowlisted_principal_sse_stream_names_no_restricted_table(self):
        result, _, events = await _resolve_streamed(ALLOWLIST_PROFILE)

        assert result is not None, "precondition: the allowed query must answer"
        wire = json.dumps(events, default=str)
        leaked = [t for t in RESTRICTED if t in wire]
        assert not leaked, f"trace streamed to an allowlisted principal names restricted tables: {leaked}"

    @pytest.mark.asyncio
    async def test_resolve_allowlisted_principal_allowed_table_still_listed(self):
        _, _, events = await _resolve_streamed(ALLOWLIST_PROFILE)

        assert _step_detail(events, StepId.T2_SQL_GENERATE)["tables"] == ["orders"]
        assert _step_detail(events, StepId.T2_SQL_EXECUTE)["tables"] == ["orders"]

    @pytest.mark.asyncio
    async def test_resolve_no_allowlist_trace_lists_only_tables_the_sql_reads(self):
        # The FK walk put two more tables in the context; the SQL reads ``orders`` only.
        _, _, events = await _resolve_streamed({"userId": "analyst"})

        assert _step_detail(events, StepId.T2_SQL_GENERATE)["tables"] == ["orders"]
        assert _step_detail(events, StepId.T2_SQL_EXECUTE)["tables"] == ["orders"]

    @pytest.mark.asyncio
    async def test_resolve_fk_walked_restricted_tables_removed_from_collected_trace(self):
        """The collected trace (what /invoke returns as ``trace``) is filtered too."""
        _, trace, _ = await _resolve_streamed(ALLOWLIST_PROFILE)

        wire = json.dumps(trace.steps_serializable, default=str)
        assert not [t for t in RESTRICTED if t in wire]

    @pytest.mark.asyncio
    async def test_resolve_sql_on_restricted_table_still_denied(self):
        """The deny path is unchanged: SQL reading a restricted table is a terminal 403."""
        nl_result = _nl_result(sql="SELECT * FROM employee_salaries")
        with pytest.raises(AccessDeniedError):
            await _resolve_streamed(ALLOWLIST_PROFILE, nl_result=nl_result)


# ── NL→SQL strategy: trace lists the tables the SQL reads ───────────────

JOIN_SQL = (
    "SELECT c.region, SUM(o.order_total_usd) AS total FROM orders o "
    "JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.region"
)
# Trino-only syntax: sqlglot's default dialect cannot parse a ROW type cast.
ROW_CAST_SQL = "SELECT CAST(ROW(o.id, o.amount) AS ROW(id bigint, amt double)).amt AS amt FROM orders o"


def _context_result(sql):
    """Retrieval found four tables; the generated SQL may read fewer of them."""
    r = _nl_result(sql=sql)
    r.retrieved_tables = ["orders", "customers", "products", "order_items"]
    r.expanded_tables = ["orders", "customers", "products", "order_items"]
    return r


class TestTraceListsTablesTheSqlReads:
    @pytest.mark.asyncio
    async def test_resolve_trino_only_row_cast_lists_its_table(self):
        """Unrestricted grant: the firewall only parses as Trino, and the query runs.

        The trace used to show ``tables: []`` here, plus two parse-error warnings.
        """
        with capture_logs() as logs:
            result, _, events = await _resolve_streamed({"userId": "analyst"}, nl_result=_context_result(ROW_CAST_SQL))

        assert result is not None, "precondition: the query passes the firewall and runs"
        assert _step_detail(events, StepId.T2_SQL_GENERATE)["tables"] == ["orders"]
        assert _step_detail(events, StepId.T2_SQL_EXECUTE)["tables"] == ["orders"]
        assert not [log for log in logs if log["event"] == "sql_firewall_parse_error"]

    @pytest.mark.asyncio
    async def test_resolve_join_lists_joined_tables_not_the_retrieval_context(self):
        _, _, events = await _resolve_streamed({"userId": "analyst"}, nl_result=_context_result(JOIN_SQL))

        assert _step_detail(events, StepId.T2_SQL_GENERATE)["tables"] == ["customers", "orders"]
        assert _step_detail(events, StepId.T2_SQL_EXECUTE)["tables"] == ["customers", "orders"]

    @pytest.mark.asyncio
    async def test_resolve_join_response_metadata_keeps_the_retrieval_context(self):
        result, _, _ = await _resolve_streamed({"userId": "analyst"}, nl_result=_context_result(JOIN_SQL))

        assert result.expanded_tables == ["orders", "customers", "products", "order_items"]

    @pytest.mark.asyncio
    async def test_resolve_allowlisted_principal_join_lists_joined_tables(self):
        profile = {"userId": "analyst", "tableAllowlist": ["orders", "customers", "products"]}
        _, _, events = await _resolve_streamed(profile, nl_result=_context_result(JOIN_SQL))

        assert _step_detail(events, StepId.T2_SQL_EXECUTE)["tables"] == ["customers", "orders"]

    @pytest.mark.asyncio
    async def test_resolve_cte_name_is_not_listed_as_a_table(self):
        sql = "WITH done AS (SELECT * FROM orders WHERE status = 'completed') SELECT count(*) AS n FROM done"
        _, _, events = await _resolve_streamed({"userId": "analyst"}, nl_result=_context_result(sql))

        assert _step_detail(events, StepId.T2_SQL_EXECUTE)["tables"] == ["orders"]

    @pytest.mark.asyncio
    async def test_resolve_correction_shot_lists_the_corrected_sql_tables(self):
        executor = AsyncMock()
        executor.execute.side_effect = [RuntimeError("COLUMN_NOT_FOUND"), _exec_result()]
        _, _, events = await _resolve_streamed(
            {"userId": "analyst"},
            nl_result=_context_result(JOIN_SQL),
            executor=executor,
            corrected_sql="SELECT count(*) AS n FROM customers",
        )

        executes = [e["payload"] for e in events if e["payload"]["stepName"] == StepId.T2_SQL_EXECUTE]
        assert [e["status"] for e in executes] == ["error", "success"]
        assert executes[1]["detail"]["tables"] == ["customers"]


class TestReferencedTables:
    def test_referenced_tables_qualified_names_reduce_to_bare_lower_case_names(self):
        sql = 'SELECT 1 FROM "AwsDataCatalog"."sales"."Orders" o JOIN sales.customers c ON o.id = c.id'
        assert SQLFirewall.referenced_tables(sql) == ["customers", "orders"]

    def test_referenced_tables_subquery_and_cte_tables_are_listed_once(self):
        sql = "WITH t AS (SELECT * FROM orders) SELECT * FROM t WHERE id IN (SELECT order_id FROM orders)"
        assert SQLFirewall.referenced_tables(sql) == ["orders"]

    @pytest.mark.parametrize("sql", ["", "SELECT FROM WHERE (((", None])
    def test_referenced_tables_unparseable_or_empty_returns_empty_list(self, sql):
        assert SQLFirewall.referenced_tables(sql) == []

    def test_referenced_tables_trino_only_row_cast_lists_its_table(self):
        """sqlglot's default dialect can't parse a ROW type cast; Trino can."""
        assert SQLFirewall.referenced_tables(ROW_CAST_SQL) == ["orders"]

    def test_referenced_tables_default_dialect_fallback(self, monkeypatch):
        """A statement only the default dialect parses still lists its tables."""
        real_parse_one = sqlglot.parse_one

        def trino_fails(sql, read=None, **kwargs):
            if read == "trino":
                raise sqlglot.errors.ParseError("trino can't")
            return real_parse_one(sql, read=read, **kwargs)

        monkeypatch.setattr(sqlglot, "parse_one", trino_fails)
        assert SQLFirewall.referenced_tables(JOIN_SQL) == ["customers", "orders"]

    def test_referenced_tables_untokenizable_sql_returns_empty_list_without_raising(self):
        # TokenError is not a ParseError: it used to escape into the trace step.
        assert SQLFirewall.referenced_tables("SELECT 'unterminated FROM orders") == []

    def test_referenced_tables_logs_no_firewall_parse_error(self):
        """Nothing was authorized here, so the firewall's parse-error event is not logged."""
        with capture_logs() as logs:
            SQLFirewall.referenced_tables(ROW_CAST_SQL)
            SQLFirewall.referenced_tables("SELECT FROM WHERE (((")
        assert not [log for log in logs if log["event"] == "sql_firewall_parse_error"]

    def test_referenced_tables_matches_what_the_allowlist_checks(self):
        """Every listed table is one the firewall checked against the allowlist."""
        firewall = SQLFirewall(cedar_authorizer=NullCedarAuthorizer())
        tables = SQLFirewall.referenced_tables(JOIN_SQL)
        assert not firewall.evaluate(JOIN_SQL, {"tableAllowlist": tables}).denied
        for dropped in tables:
            remaining = [t for t in tables if t != dropped]
            assert firewall.evaluate(JOIN_SQL, {"tableAllowlist": remaining}).denied


# ── /invoke response metadata ───────────────────────────────────────────


class TestInvokeResponseTables:
    @staticmethod
    def _orchestrator():
        orch = _make_orchestrator(tier2_success=True, tier2_ontop_assembly=False)
        result: StrategyResult = orch._structured_query_tier._strategies[0].resolve.return_value
        result.retrieved_tables = ["orders", "employee_salaries"]
        result.expanded_tables = ["orders", *RESTRICTED]
        return orch

    @pytest.mark.asyncio
    async def test_resolve_allowlisted_principal_response_metadata_names_no_restricted_table(self):
        request = InvokeRequest(
            query="How many orders?", namespace="demo", profile=ALLOWLIST_PROFILE, options={"tierOverride": 2}
        )
        response = await self._orchestrator().resolve(request)

        assert response.result.metadata["retrieved_tables"] == ["orders"]
        assert response.result.metadata["expanded_tables"] == ["orders"]
        wire = response.model_dump_json(by_alias=True)
        assert not [t for t in RESTRICTED if t in wire]

    @pytest.mark.asyncio
    async def test_resolve_no_allowlist_response_metadata_unchanged(self):
        request = InvokeRequest(query="How many orders?", namespace="demo", options={"tierOverride": 2})
        response = await self._orchestrator().resolve(request)

        assert response.result.metadata["retrieved_tables"] == ["orders", "employee_salaries"]
        assert response.result.metadata["expanded_tables"] == ["orders", *RESTRICTED]


# ── deep-reasoning (agentic) strategy ───────────────────────────────────


class TestAgenticTraceTables:
    @pytest.mark.asyncio
    async def test_resolve_allowlisted_principal_inspected_tables_filtered(self, monkeypatch):
        from coa_serve.agents.sql_agent import SqlAgentOutcome
        from coa_serve.tier2.nl_to_sql import agentic_strategy as mod

        outcome = SqlAgentOutcome(
            executed_sql="SELECT count(*) FROM orders",
            rows=[{"n": 1}],
            columns=["n"],
            row_count=1,
            confidence=0.8,
            data_source_id="ds-1",
            tables=["customer_ssn", "orders"],
        )
        agent = MagicMock()
        agent.run = AsyncMock(return_value=outcome)
        monkeypatch.setattr(mod, "SqlAgent", MagicMock(return_value=agent))
        gen = MagicMock()
        gen.llm = MagicMock()
        strategy = mod.AgenticStrategy(
            sql_generator=gen,
            firewall=MagicMock(),
            query_executor=MagicMock(),
            vector_client=MagicMock(),
            oss_ontology_index="idx",
        )
        trace = TraceCollector()
        ctx = StrategyContext(embedding=None, profile=ALLOWLIST_PROFILE, options={}, trace=trace)

        result = await strategy.resolve("how many orders", "ns", ctx)

        assert result is not None
        assert result.strategy_name == StrategyOption.DEEP_REASONING
        assert result.retrieved_tables == ["orders"] and result.expanded_tables == ["orders"]
        execute = next(s for s in trace.steps if s.step == StepId.T2_SQL_EXECUTE)
        assert execute.detail["tables"] == ["orders"]

    @pytest.mark.asyncio
    async def test_resolve_execute_step_lists_executed_sql_tables_not_inspected_ones(self, monkeypatch):
        from coa_serve.agents.sql_agent import SqlAgentOutcome
        from coa_serve.tier2.nl_to_sql import agentic_strategy as mod

        outcome = SqlAgentOutcome(
            executed_sql="SELECT count(*) FROM orders",
            rows=[{"n": 1}],
            columns=["n"],
            row_count=1,
            confidence=0.8,
            data_source_id="ds-1",
            tables=["customers", "orders", "products"],
        )
        agent = MagicMock()
        agent.run = AsyncMock(return_value=outcome)
        monkeypatch.setattr(mod, "SqlAgent", MagicMock(return_value=agent))
        gen = MagicMock()
        gen.llm = MagicMock()
        strategy = mod.AgenticStrategy(
            sql_generator=gen,
            firewall=MagicMock(),
            query_executor=MagicMock(),
            vector_client=MagicMock(),
            oss_ontology_index="idx",
        )
        trace = TraceCollector()
        ctx = StrategyContext(embedding=None, profile={"userId": "analyst"}, options={}, trace=trace)

        result = await strategy.resolve("how many orders", "ns", ctx)

        execute = next(s for s in trace.steps if s.step == StepId.T2_SQL_EXECUTE)
        assert execute.detail["tables"] == ["orders"]
        # What the agent inspected is still reported as the retrieval context.
        assert result.retrieved_tables == ["customers", "orders", "products"]


# ── Ontop/VKG compile step ──────────────────────────────────────────────


class TestVKGCompileTables:
    @pytest.mark.asyncio
    async def test_resolve_allowlisted_principal_compile_step_lists_allowed_tables_only(self):
        vkg = _make_vkg_client(
            sql="SELECT id FROM orders", tables=["catalog.schema.orders", "catalog.schema.employee_salaries"]
        )
        translator = _make_translator(vkg_client=vkg, firewall=_make_firewall(), executor=_make_executor())

        result = await translator.resolve(
            "SELECT ?o WHERE { ?o a :Order }", namespace="demo", profile=ALLOWLIST_PROFILE
        )

        compile_step = next(s for s in result.trace_steps if s.step == Tier2Step.VKG_COMPILE)
        assert compile_step.detail["tables"] == ["catalog.schema.orders"]

    @pytest.mark.asyncio
    async def test_resolve_no_allowlist_compile_step_unchanged(self):
        tables = ["catalog.schema.orders", "catalog.schema.employee_salaries"]
        vkg = _make_vkg_client(sql="SELECT id FROM orders", tables=tables)
        translator = _make_translator(vkg_client=vkg, firewall=_make_firewall(), executor=_make_executor())

        result = await translator.resolve("SELECT ?o WHERE { ?o a :Order }", namespace="demo")

        compile_step = next(s for s in result.trace_steps if s.step == Tier2Step.VKG_COMPILE)
        assert compile_step.detail["tables"] == tables
