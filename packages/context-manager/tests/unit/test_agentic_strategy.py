# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AgenticStrategy — the Tier-2 adapter over the NL→SQL agent.

The strategy owns no prompt, no tools and no loop: it builds the request-scoped
tool set, runs :class:`~coa_serve.agents.SqlAgent`, and maps the outcome onto
trace records and a ``StrategyResult``. These tests pin that seam; the tool
contracts themselves live in ``test_tier2_tools.py``, the loop in
``test_sql_agent.py`` and the execution primitive in ``test_sql_execution.py``.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.exceptions import AccessDeniedError
from coa_serve.step_ids import StepId
from coa_serve.tier2.strategy import StrategyContext, StrategyOption

from .strands_fake import install_fake_strands

GEN_SQL = "SELECT count(*) FROM orders"


def _make_generator(gen_sql=GEN_SQL):
    gen = MagicMock()
    # The tool layer reaches the LLM client through the generator's public seam.
    gen.llm = MagicMock()
    gen.llm.embed = AsyncMock(return_value=[0.1] * 8)
    gen.llm._region = "us-east-1"
    # Focused SQL writer used by the generate_sql tool → (sql, confidence)
    gen.generate_from_context = AsyncMock(return_value=(gen_sql, 0.9))
    return gen


def _make_hit(table: str, ds: str = "ds-1"):
    from coa_serve.clients.base import VectorHit

    return VectorHit(
        id=table,
        text=f"Table: {table} | Columns: id:int (pk), name:text (n)",
        score=0.9,
        metadata={"entity_type": "class", "data_source_id": ds},
    )


def _fw_result(denied=False, reason=None, sql=GEN_SQL):
    r = MagicMock()
    r.denied = denied
    r.reason = reason
    r.authorized_sql = sql
    return r


def _exec_result(rows=None, columns=None, row_count=1):
    r = MagicMock()
    r.rows = rows if rows is not None else [{"count": 42}]
    r.columns = columns or ["count"]
    r.row_count = row_count
    r.truncated = False
    r.engine = "athena"
    return r


def _ctx():
    return StrategyContext(embedding=None, profile={"userId": "u1"}, options={"maxResults": 100}, trace=MagicMock())


async def _solve(tools, tables=("orders",)):
    """Play the turn sequence a real agent would: discover → generate → execute."""
    await tools["search_tables"]("orders", 5)
    await tools["get_table_schema"]("orders")
    generated = json.loads(await tools["generate_sql"](list(tables)))
    return await tools["execute_sql"](generated["handle"])


def _recorded(trace, step_id):
    """Return the first trace.record call for ``step_id``, or None."""
    for call in trace.record.call_args_list:
        if call.args and call.args[0] == step_id:
            return call
    return None


@pytest.mark.unit
class TestAgenticStrategy:
    def _build(self, firewall, executor, vector, generator=None, prefetch_schemas=0, graph_client=None):
        from coa_serve.tier2.nl_to_sql.agentic_strategy import AgenticStrategy

        # Schema prefetch off by default here so these tests stay about the
        # strategy/agent seam rather than about the extra pre-turn retrieval
        # (which test_sql_agent.py::TestSchemaPrefetch covers directly).
        return AgenticStrategy(
            sql_generator=generator or _make_generator(),
            firewall=firewall,
            query_executor=executor,
            vector_client=vector,
            oss_ontology_index="idx",
            prefetch_schemas=prefetch_schemas,
            graph_client=graph_client,
        )

    def _clients(self, *, fw=None, exec_return=None, exec_side_effect=None):
        vector = MagicMock()
        vector.search = AsyncMock(return_value=[_make_hit("orders")])
        firewall = MagicMock()
        firewall.evaluate = MagicMock(return_value=fw or _fw_result())
        executor = MagicMock()
        executor.execute = AsyncMock(
            return_value=exec_return if exec_return is not None else _exec_result(),
            side_effect=exec_side_effect,
        )
        return vector, firewall, executor

    @pytest.mark.asyncio
    async def test_success_maps_onto_a_strategy_result(self):
        vector, firewall, executor = self._clients(exec_return=_exec_result(row_count=3))
        gen = _make_generator()
        install_fake_strands(_solve)

        strat = self._build(firewall, executor, vector, generator=gen)
        ctx = _ctx()
        res = await strat.resolve("how many orders", "ns1", ctx)

        assert res is not None
        assert res.strategy_name == StrategyOption.DEEP_REASONING
        assert res.sql == GEN_SQL and res.row_count == 3 and res.confidence == 0.9
        assert res.rows == [{"count": 42}] and res.columns == ["count"]
        assert res.retrieved_tables == ["orders"] and res.expanded_tables == ["orders"]
        assert res.data_source_id == "ds-1"
        gen.generate_from_context.assert_awaited_once()  # the focused writer wrote the SQL

        record = _recorded(ctx.trace, StepId.T2_SQL_EXECUTE)
        assert record.args[1] == "success"
        assert record.kwargs["detail"] == {
            "rowCount": 3,
            "tables": ["orders"],
            "deepReasoning": True,
            "confidence": 0.9,
        }

    @pytest.mark.asyncio
    async def test_request_context_reaches_the_tools(self):
        # The strategy is what threads the request's namespace, index, evidence cap,
        # row cap, model and default source into the tool set.
        vector, firewall, executor = self._clients()
        gen = _make_generator()
        install_fake_strands(_solve)

        strat = self._build(firewall, executor, vector, generator=gen)
        ctx = StrategyContext(
            embedding=None,
            profile={"userId": "u1"},
            options={"maxResults": 25, "evidence": "x" * 5000, "dataSourceId": "ds-fallback"},
            trace=MagicMock(),
            model_id="model-x",
        )
        await strat.resolve("how many orders", "ns1", ctx)

        search_kwargs = vector.search.await_args.kwargs
        assert search_kwargs["namespace"] == "ns1" and search_kwargs["index"] == "idx-ns1"

        gen_kwargs = gen.generate_from_context.await_args.kwargs
        assert gen_kwargs["model_id"] == "model-x"
        # Caller evidence is capped before it reaches a prompt.
        assert 0 < len(gen_kwargs["evidence"]) < 5000

        exec_kwargs = executor.execute.await_args.kwargs
        assert exec_kwargs["namespace"] == "ns1" and exec_kwargs["max_rows"] == 25
        # The retrieved hits pin the source, so the caller's default is only a fallback.
        assert exec_kwargs["data_source_id"] == "ds-1"
        firewall.evaluate.assert_called_once()
        assert firewall.evaluate.call_args.kwargs["namespace"] == "ns1"
        assert firewall.evaluate.call_args.args[1] == {"userId": "u1"}

    @pytest.mark.asyncio
    async def test_firewall_denial_raises_access_denied_and_is_traced(self):
        vector, firewall, executor = self._clients(fw=_fw_result(denied=True, reason="no perm"))
        install_fake_strands(_solve)

        strat = self._build(firewall, executor, vector)
        ctx = _ctx()
        with pytest.raises(AccessDeniedError):
            await strat.resolve("q", "ns1", ctx)

        executor.execute.assert_not_called()
        record = _recorded(ctx.trace, StepId.T2_SQL_FIREWALL)
        assert record.args[1] == "denied" and record.kwargs["detail"] == {"reason": "no perm"}

    @pytest.mark.asyncio
    async def test_no_executed_sql_returns_none_and_is_traced(self):
        vector, firewall, executor = self._clients()

        async def driver(tools):
            await tools["search_tables"]("orders")  # never executes anything

        install_fake_strands(driver)

        strat = self._build(firewall, executor, vector)
        ctx = _ctx()
        assert await strat.resolve("q", "ns1", ctx) is None

        record = _recorded(ctx.trace, StepId.T2_SQL_GENERATE)
        assert record.args[1] == "error" and record.kwargs["detail"] == "agent_produced_no_executed_sql"

    @pytest.mark.asyncio
    async def test_execution_error_without_recovery_returns_none(self):
        vector, firewall, executor = self._clients(exec_side_effect=RuntimeError("boom"))
        install_fake_strands(_solve)

        strat = self._build(firewall, executor, vector)
        assert await strat.resolve("q", "ns1", _ctx()) is None

    @pytest.mark.asyncio
    async def test_recovers_after_error_on_retry(self):
        vector, firewall, executor = self._clients(
            exec_side_effect=[RuntimeError("bad col"), _exec_result(row_count=7)]
        )

        async def driver(tools):
            await _solve(tools)  # fails
            await _solve(tools)  # revised generation succeeds

        install_fake_strands(driver)

        strat = self._build(firewall, executor, vector)
        res = await strat.resolve("q", "ns1", _ctx())
        assert res is not None and res.row_count == 7

    @pytest.mark.asyncio
    async def test_no_query_executor_skips_without_calling_the_agent(self):
        vector = MagicMock()
        vector.search = AsyncMock()
        strat = self._build(MagicMock(), None, vector)
        ctx = _ctx()

        assert await strat.resolve("q", "ns1", ctx) is None

        vector.search.assert_not_called()
        record = _recorded(ctx.trace, StepId.T2_SQL_EXECUTE)
        assert record.args[1] == "skipped"

    @pytest.mark.asyncio
    async def test_missing_strands_returns_none(self, monkeypatch):
        vector, firewall, executor = self._clients()
        strat = self._build(firewall, executor, vector)
        monkeypatch.setitem(sys.modules, "strands", None)
        assert await strat.resolve("q", "ns1", _ctx()) is None

    @pytest.mark.asyncio
    async def test_graph_tool_needs_both_the_flag_and_a_client(self, monkeypatch):
        # Two independent switches, so neither a stale env nor a wired client alone
        # can change what the agent sees.
        from . import strands_fake

        graph_client = MagicMock()
        graph_client.query = AsyncMock(return_value=[])

        for env, client, expected in (
            (None, graph_client, False),  # client but no flag
            ("true", None, False),  # flag but no client
            ("true", graph_client, True),
        ):
            if env is None:
                monkeypatch.delenv("SERVE_DEEP_REASONING_GRAPH_TRAVERSAL", raising=False)
            else:
                monkeypatch.setenv("SERVE_DEEP_REASONING_GRAPH_TRAVERSAL", env)
            vector, firewall, executor = self._clients()
            install_fake_strands(_solve)

            strat = self._build(firewall, executor, vector, graph_client=client)
            await strat.resolve("q", "ns1", _ctx())

            assert ("explore_graph" in strands_fake.last_agent.tools) is expected

    @pytest.mark.asyncio
    async def test_per_request_option_enables_the_tool_with_the_env_flag_off(self, monkeypatch):
        from . import strands_fake

        monkeypatch.delenv("SERVE_DEEP_REASONING_GRAPH_TRAVERSAL", raising=False)
        graph_client = MagicMock()
        graph_client.query = AsyncMock(return_value=[])

        for options, expected in (
            ({"deepReasoningGraphTraversal": True}, True),
            ({"deepReasoningGraphTraversal": "true"}, True),
            # Pre-rename spelling: still enables the tool, so a benchmark script
            # pinned to it does not silently measure the no-traversal arm.
            ({"agenticGraphTraversal": True}, True),
            ({"agenticGraphTraversal": "yes"}, True),
            ({}, False),
            # excludeTools is a veto: it beats the per-request enable.
            ({"deepReasoningGraphTraversal": True, "excludeTools": ["explore_graph"]}, False),
            ({"agenticGraphTraversal": True, "excludeTools": ["explore_graph"]}, False),
        ):
            vector, firewall, executor = self._clients()
            install_fake_strands(_solve)
            ctx = StrategyContext(
                embedding=None,
                profile={"userId": "u1"},
                options={"maxResults": 100, **options},
                trace=MagicMock(),
            )
            strat = self._build(firewall, executor, vector, graph_client=graph_client)
            await strat.resolve("q", "ns1", ctx)

            assert ("explore_graph" in strands_fake.last_agent.tools) is expected

    @pytest.mark.asyncio
    async def test_exclude_tools_withholds_the_graph_tool_for_one_request(self, monkeypatch):
        from . import strands_fake

        monkeypatch.setenv("SERVE_DEEP_REASONING_GRAPH_TRAVERSAL", "1")
        graph_client = MagicMock()
        graph_client.query = AsyncMock(return_value=[])

        for exclude, expected in ((["explore_graph"], False), (["search_tables"], True), ([], True)):
            vector, firewall, executor = self._clients()
            install_fake_strands(_solve)
            ctx = StrategyContext(
                embedding=None,
                profile={"userId": "u1"},
                options={"maxResults": 100, "excludeTools": exclude},
                trace=MagicMock(),
            )
            strat = self._build(firewall, executor, vector, graph_client=graph_client)
            await strat.resolve("q", "ns1", ctx)

            assert ("explore_graph" in strands_fake.last_agent.tools) is expected

    @pytest.mark.asyncio
    async def test_graph_tool_is_scoped_to_the_request_namespace(self, monkeypatch):
        from . import strands_fake

        monkeypatch.setenv("SERVE_DEEP_REASONING_GRAPH_TRAVERSAL", "1")
        monkeypatch.setenv("GRAPH_URI_TEMPLATE", "https://graphs.local/{namespace}")
        graph_client = MagicMock()
        graph_client.query = AsyncMock(return_value=[])
        vector, firewall, executor = self._clients()

        async def driver(tools):
            await tools["explore_graph"]("orders")
            await _solve(tools)

        install_fake_strands(driver)
        strat = self._build(firewall, executor, vector, graph_client=graph_client)
        res = await strat.resolve("q", "ns1", _ctx())

        assert res is not None  # traversal is advisory, the answer still lands
        assert "explore_graph" in strands_fake.last_agent.tools
        # Every SPARQL the tool issued is filtered to this namespace's graphs.
        assert graph_client.query.await_count >= 1
        for call in graph_client.query.await_args_list:
            assert "https://graphs.local/ns1" in call.args[0]

    @pytest.mark.asyncio
    async def test_prefetch_setting_reaches_the_agent(self):
        # Regression guard: the strategy's prefetch setting must survive the
        # hand-off, since leaving it None is what lets the env knob govern.
        from . import strands_fake

        vector, firewall, executor = self._clients()
        install_fake_strands(_solve)

        strat = self._build(firewall, executor, vector, prefetch_schemas=2)
        await strat.resolve("q", "ns1", _ctx())

        assert "Candidate tables" in strands_fake.last_agent.prompt

    @pytest.mark.asyncio
    async def test_confidence_is_the_writers_rating_not_a_constant(self):
        # The score must track the writer's self-rating for the executed statement
        # (same signal + scale as the single-shot NL→SQL path), floored so a
        # verified result is never dropped by the low-confidence gates.
        from coa_serve.tier2.strategy import EMPTY_RESULT_CONFIDENCE_FLOOR

        vector, firewall, executor = self._clients()
        install_fake_strands(_solve)

        gen = _make_generator()
        gen.generate_from_context = AsyncMock(return_value=(GEN_SQL, 0.55))
        res = await self._build(firewall, executor, vector, generator=gen).resolve("q", "ns1", _ctx())
        assert res is not None and res.confidence == pytest.approx(0.55)

        gen = _make_generator()
        gen.generate_from_context = AsyncMock(return_value=(GEN_SQL, 0.0))
        res = await self._build(firewall, executor, vector, generator=gen).resolve("q", "ns1", _ctx())
        assert res is not None and res.confidence == EMPTY_RESULT_CONFIDENCE_FLOOR
