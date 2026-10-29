# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the NL→SQL agent (prompt + bounded tool-use loop wiring).

The agent is exercised over the REAL Tier-2 tools and the REAL execution
primitive with mocked serve clients, so these tests pin the contract the model
actually sees: what each tool returns, what it may execute, and how an
observation feeds the next generation.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.agents import SqlAgent, build_system_prompt, sql_agent
from coa_serve.agents.sql_agent import AGENT_SYSTEM_PROMPT, INTENT_REVIEW_BLOCK
from coa_serve.clients.base import VectorHit
from coa_serve.deadline import Deadline
from coa_serve.sql_execution import SqlExecutionService
from coa_serve.tier2.sql_firewall import UnsafeSQLError
from coa_serve.tier2.tools import SqlAuthoringTool, TableCatalog

from . import strands_fake
from .strands_fake import install_fake_strands

GEN_SQL = "SELECT count(*) FROM orders"


def _hit(table: str, ds: str = "ds-1"):
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


def _build(
    *,
    hits=None,
    fw=None,
    fw_side_effect=None,
    exec_return=None,
    exec_side_effect=None,
    gen_sql=GEN_SQL,
    executor=None,
    **agent_kwargs,
):
    """Assemble a SqlAgent over the real tools with mocked clients."""
    vector = MagicMock()
    vector.search = AsyncMock(return_value=hits if hits is not None else [_hit("orders")])
    llm = MagicMock()
    llm.embed = AsyncMock(return_value=[0.1] * 8)

    generator = MagicMock()
    generator.generate_from_context = AsyncMock(return_value=(gen_sql, 0.9))

    firewall = MagicMock()
    firewall.evaluate = MagicMock(return_value=fw or _fw_result(), side_effect=fw_side_effect)

    if executor is None:
        executor = MagicMock()
        executor.execute = AsyncMock(
            return_value=exec_return if exec_return is not None else _exec_result(),
            side_effect=exec_side_effect,
        )

    catalog = TableCatalog(vector, llm, namespace="ns1", index="idx")
    # Prefetch OFF unless a test asks for it: it fires an extra search + schema
    # read before the first turn, which would otherwise blur every assertion
    # about what the model's own tool calls did.
    agent_kwargs.setdefault("prefetch_schemas", 0)
    agent = SqlAgent(
        catalog=catalog,
        authoring=SqlAuthoringTool(generator, catalog),
        execution=SqlExecutionService(firewall, executor),
        **agent_kwargs,
    )
    return agent, {
        "vector": vector,
        "llm": llm,
        "generator": generator,
        "firewall": firewall,
        "executor": executor,
        "catalog": catalog,
    }


async def _run(agent, question="how many orders", **kwargs):
    return await agent.run(question, namespace="ns1", profile={"userId": "u1"}, **kwargs)


async def _generate_and_execute(tools, tables=("orders",)):
    """Play the normal turn sequence: discover → generate → execute the handle."""
    await tools["search_tables"]("orders", 5)
    generated = json.loads(await tools["generate_sql"](list(tables)))
    return generated, await tools["execute_sql"](generated["handle"])


@pytest.mark.unit
class TestSqlAgentHappyPath:
    @pytest.mark.asyncio
    async def test_generated_sql_is_executed_via_its_handle(self):
        # generate_sql delegates to the focused writer and hands back a HANDLE; the
        # agent executes that handle through the shared execute-with-authz primitive.
        agent, mocks = _build(exec_return=_exec_result(row_count=3))
        captured = {}

        async def driver(tools):
            captured["generated"], captured["observation"] = await _generate_and_execute(tools)

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert outcome.succeeded and outcome.row_count == 3
        assert outcome.executed_sql == GEN_SQL and outcome.data_source_id == "ds-1"
        assert outcome.tables == ["orders"]
        mocks["generator"].generate_from_context.assert_awaited_once()  # focused writer used
        assert captured["generated"]["handle"] and captured["generated"]["sql"] == GEN_SQL
        assert json.loads(captured["observation"])["row_count"] == 3

    @pytest.mark.asyncio
    async def test_no_turn_cap_is_passed_to_the_sdk(self):
        # Regression guard against re-adding a cap that does not cap: on the pinned
        # strands-agents, invoke_async takes max_iterations into **kwargs and ignores
        # it. The loop is bounded by the request deadline + the exec timeout instead,
        # so passing a number here would be documentation pretending to be a bound.
        agent, _ = _build()

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert "max_iterations" not in strands_fake.last_agent.invoke_kwargs

    @pytest.mark.asyncio
    async def test_model_and_region_reach_the_bedrock_provider(self):
        agent, _ = _build(model_id="model-x", region="eu-west-1")

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        kwargs = strands_fake.last_agent.model.kwargs
        assert kwargs["model_id"] == "model-x" and kwargs["region_name"] == "eu-west-1"
        assert kwargs["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_exec_timeout_is_applied_per_statement(self):
        agent, mocks = _build(exec_timeout_s=9)

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent, max_rows=25)

        kwargs = mocks["executor"].execute.await_args.kwargs
        assert kwargs["timeout_seconds"] == 9 and kwargs["max_rows"] == 25

    @pytest.mark.asyncio
    async def test_tools_offered_to_the_model(self):
        # Exactly four tools, and NO raw-SQL execution entry point among them.
        agent, _ = _build()

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert set(strands_fake.last_agent.tools) == {
            "search_tables",
            "get_table_schema",
            "generate_sql",
            "execute_sql",
        }


@pytest.mark.unit
class TestSqlAgentExecutionIsGated:
    @pytest.mark.asyncio
    async def test_only_generated_sql_can_be_executed(self):
        # The no-arbitrary-execution guarantee: execute_sql resolves a handle the
        # Tier-2 writer produced, so SQL the model invented has no path to an engine.
        agent, mocks = _build()
        captured = {}

        async def driver(tools):
            captured["raw"] = await tools["execute_sql"]("DELETE FROM orders")
            captured["bogus"] = await tools["execute_sql"]("sql-99")

        install_fake_strands(driver)
        outcome = await _run(agent)

        for key in ("raw", "bogus"):
            assert "unknown handle" in json.loads(captured[key])["error"]
        mocks["firewall"].evaluate.assert_not_called()
        mocks["executor"].execute.assert_not_called()
        assert not outcome.succeeded

    @pytest.mark.asyncio
    async def test_each_generation_gets_a_distinct_handle(self):
        # Handles must not collide: a stale handle from an earlier turn would
        # otherwise silently re-execute the wrong statement.
        agent, _ = _build()
        handles = []

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            for _ in range(2):
                handles.append(json.loads(await tools["generate_sql"](["orders"]))["handle"])

        install_fake_strands(driver)
        await _run(agent)

        assert len(set(handles)) == 2

    @pytest.mark.asyncio
    async def test_a_quoted_handle_is_still_resolved(self):
        # Models routinely quote tool arguments; that must not cost a whole turn.
        agent, mocks = _build()
        captured = {}

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            generated = json.loads(await tools["generate_sql"](["orders"]))
            captured["observation"] = await tools["execute_sql"](f'"{generated["handle"]}"')

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert "error" not in json.loads(captured["observation"])
        assert outcome.succeeded and mocks["executor"].execute.await_args.args[0] == GEN_SQL

    @pytest.mark.asyncio
    async def test_echoing_the_generated_statement_is_accepted(self):
        # The statement itself is already Tier-2 authored, so accepting it back
        # keeps the invariant while forgiving a model that ignores the handle.
        agent, mocks = _build()
        captured = {}

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            generated = json.loads(await tools["generate_sql"](["orders"]))
            captured["observation"] = await tools["execute_sql"](generated["sql"])

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert "error" not in json.loads(captured["observation"])
        assert outcome.succeeded and mocks["executor"].execute.await_args.args[0] == GEN_SQL

    @pytest.mark.asyncio
    async def test_a_variation_of_the_generated_statement_is_rejected(self):
        # Tolerance is exact-match only — an edited statement is not authored SQL.
        agent, mocks = _build()
        captured = {}

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            await tools["generate_sql"](["orders"])
            captured["observation"] = await tools["execute_sql"](f"{GEN_SQL} WHERE 1=1")

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert "unknown handle" in json.loads(captured["observation"])["error"]
        mocks["firewall"].evaluate.assert_not_called()
        mocks["executor"].execute.assert_not_called()
        assert not outcome.succeeded

    @pytest.mark.asyncio
    async def test_firewall_denial_is_recorded_and_never_executes(self):
        agent, mocks = _build(fw=_fw_result(denied=True, reason="no perm"))
        captured = {}

        async def driver(tools):
            _generated, captured["observation"] = await _generate_and_execute(tools)

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert outcome.denied_reason == "no perm" and not outcome.succeeded
        assert "access_denied" in json.loads(captured["observation"])["error"]
        mocks["executor"].execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsafe_sql_is_surfaced_without_denying_the_request(self):
        # An unsafe statement is a correctable mistake, not an authorization failure:
        # the agent is told, but the request is not turned into a 403.
        agent, mocks = _build(fw_side_effect=UnsafeSQLError("write statements not allowed"))
        captured = {}

        async def driver(tools):
            _generated, captured["observation"] = await _generate_and_execute(tools)

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert "unsafe_sql" in json.loads(captured["observation"])["error"]
        assert outcome.denied_reason == "" and not outcome.succeeded
        mocks["executor"].execute.assert_not_called()


@pytest.mark.unit
class TestSqlAgentObservations:
    @pytest.mark.asyncio
    async def test_observation_shape_success_empty_and_error(self):
        # The observation is the agent's only feedback channel: it must (1) carry the
        # real rows/columns/count, (2) serialize exotic DB types (Decimal/date) without
        # crashing, (3) signal empty distinctly, and (4) surface the real DB error.
        agent, _ = _build(
            exec_side_effect=[
                _exec_result(
                    rows=[{"amount": Decimal("12.50"), "ts": datetime.date(2024, 1, 2)}],
                    columns=["amount", "ts"],
                    row_count=1,
                ),
                _exec_result(rows=[], columns=["amount"], row_count=0),
                RuntimeError('column "amount" does not exist'),
            ]
        )
        captured = {}

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            for key in ("ok", "empty", "err"):
                handle = json.loads(await tools["generate_sql"](["orders"]))["handle"]
                captured[key] = await tools["execute_sql"](handle)

        install_fake_strands(driver)
        await _run(agent)

        ok = json.loads(captured["ok"])
        assert ok["row_count"] == 1 and ok["columns"] == ["amount", "ts"]
        assert ok["preview"] == [{"amount": "12.50", "ts": "2024-01-02"}]

        empty = json.loads(captured["empty"])
        assert empty["row_count"] == 0 and empty["preview"] == []

        assert "does not exist" in json.loads(captured["err"])["error"]

    @pytest.mark.asyncio
    async def test_search_and_schema_tool_shapes(self):
        agent, _ = _build(hits=[_hit("orders"), _hit("unmapped", ds="")])
        captured = {}

        async def driver(tools):
            captured["search"] = json.loads(await tools["search_tables"]("orders", 5))
            captured["schema"] = await tools["get_table_schema"]("orders")
            captured["unknown"] = await tools["get_table_schema"]("ghost")

        install_fake_strands(driver)
        await _run(agent)

        assert [t["table"] for t in captured["search"]["tables"]] == ["orders"]  # mapped only
        assert "Columns: id:int" in captured["schema"]
        assert "not found" in captured["unknown"]

    @pytest.mark.asyncio
    async def test_tool_backend_failures_are_reported_not_raised(self):
        # A client failure must reach the model as an observation; raising out of a
        # tool would abort the run instead of letting the agent try another route.
        agent, mocks = _build()
        mocks["vector"].search = AsyncMock(side_effect=RuntimeError("opensearch down"))
        mocks["generator"].generate_from_context = AsyncMock(side_effect=RuntimeError("bedrock down"))
        captured = {}

        async def driver(tools):
            captured["search"] = json.loads(await tools["search_tables"]("orders"))
            captured["schema"] = await tools["get_table_schema"]("orders")
            captured["generate"] = json.loads(await tools["generate_sql"](["orders"]))

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert "opensearch down" in captured["search"]["error"]
        assert "not found" in captured["schema"]
        # No table was ever discovered, so generation is an ordering error, and the
        # tool tells the agent how to recover rather than failing opaquely.
        assert "call search_tables first" in captured["generate"]["error"]
        assert not outcome.succeeded

    @pytest.mark.asyncio
    async def test_generation_failure_is_reported_to_the_agent(self):
        agent, mocks = _build()
        captured = {}

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            mocks["generator"].generate_from_context = AsyncMock(side_effect=RuntimeError("bedrock down"))
            captured["generate"] = json.loads(await tools["generate_sql"](["orders"]))

        install_fake_strands(driver)
        await _run(agent)

        assert "generation_failed" in captured["generate"]["error"]

    @pytest.mark.asyncio
    async def test_empty_generation_is_not_executable(self):
        agent, _ = _build(gen_sql="")
        captured = {}

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            captured["generate"] = json.loads(await tools["generate_sql"](["orders"]))

        install_fake_strands(driver)
        await _run(agent)

        assert "no SQL" in captured["generate"]["error"] and "handle" not in captured["generate"]


@pytest.mark.unit
class TestSqlAgentSelfCorrection:
    @pytest.mark.asyncio
    async def test_first_generation_carries_no_feedback(self):
        # The first attempt must be unbiased — identical to a single-shot generation.
        agent, mocks = _build()

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert not mocks["generator"].generate_from_context.await_args_list[0].kwargs.get("feedback")

    @pytest.mark.asyncio
    async def test_regeneration_after_a_failed_execution_threads_the_attempt(self):
        # The writer is a stateless temp-0 function of (question, schema), so without
        # the prior attempt a retry returns byte-identical SQL — the loop would be a
        # no-op. The failed SQL plus the engine error must be threaded back.
        agent, mocks = _build(
            fw=_fw_result(sql="SELECT bogus FROM orders"),
            exec_side_effect=[RuntimeError("bad col"), _exec_result(row_count=7)],
        )

        async def driver(tools):
            await _generate_and_execute(tools)  # fails
            await _generate_and_execute(tools)  # re-generate with feedback, then succeed

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert outcome.succeeded and outcome.row_count == 7
        feedbacks = [c.kwargs.get("feedback", "") for c in mocks["generator"].generate_from_context.await_args_list]
        assert any("bogus" in fb and "bad col" in fb for fb in feedbacks)

    @pytest.mark.asyncio
    async def test_regeneration_after_a_successful_run_threads_the_observation(self):
        # Rows that executed fine can still be the wrong answer; a re-generation must
        # see what came back so it can revise from the data, not just the schema.
        agent, mocks = _build(exec_return=_exec_result(rows=[], columns=["c"], row_count=0))

        async def driver(tools):
            await _generate_and_execute(tools)
            await tools["generate_sql"](["orders"])

        install_fake_strands(driver)
        await _run(agent)

        feedback = mocks["generator"].generate_from_context.await_args_list[-1].kwargs["feedback"]
        assert "executed OK, returned 0 row(s)" in feedback

    @pytest.mark.asyncio
    async def test_last_successful_statement_is_sticky(self):
        # A later failed attempt must not downgrade an already-good answer, so the
        # interpretation-review re-run is safe by construction.
        agent, _ = _build(exec_side_effect=[_exec_result(row_count=7), RuntimeError("bad col")])

        async def driver(tools):
            await _generate_and_execute(tools)  # succeeds
            await _generate_and_execute(tools)  # fails

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert outcome.succeeded and outcome.row_count == 7


@pytest.mark.unit
class TestSqlAgentRunOutcome:
    @pytest.mark.asyncio
    async def test_no_execution_yields_an_unsuccessful_outcome(self):
        agent, _ = _build()

        install_fake_strands(lambda tools: tools["search_tables"]("orders"))
        outcome = await _run(agent)

        assert not outcome.succeeded and not outcome.unavailable
        assert outcome.tables == ["orders"]  # what it inspected is still reported

    @pytest.mark.asyncio
    async def test_agent_crash_does_not_propagate(self):
        agent, _ = _build()

        async def driver(_tools):
            raise RuntimeError("model blew up")

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert not outcome.succeeded

    @pytest.mark.asyncio
    async def test_access_denied_from_the_loop_propagates(self):
        from coa_serve.exceptions import AccessDeniedError

        agent, _ = _build()

        async def driver(_tools):
            raise AccessDeniedError("nope")

        install_fake_strands(driver)
        with pytest.raises(AccessDeniedError):
            await _run(agent)

    @pytest.mark.asyncio
    async def test_missing_strands_reports_unavailable(self):
        # Distinct from "ran and produced nothing" so the caller can tell a missing
        # dependency from a genuine miss.
        agent, mocks = _build()
        saved = sys.modules.get("strands")
        sys.modules["strands"] = None
        try:
            outcome = await _run(agent)
        finally:
            if saved is None:
                sys.modules.pop("strands", None)
            else:
                sys.modules["strands"] = saved

        assert outcome.unavailable and not outcome.succeeded
        mocks["executor"].execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_evidence_is_folded_into_the_prompt_and_the_writer(self):
        agent, mocks = _build()

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent, evidence="orders.status = 'open' means active")

        assert "orders.status" in strands_fake.last_agent.prompt
        assert mocks["generator"].generate_from_context.await_args.kwargs["evidence"].startswith("orders.status")


@pytest.mark.unit
class TestSchemaPrefetch:
    """The prompt carries candidate tables + their schemas before the first turn."""

    @pytest.mark.asyncio
    async def test_candidates_and_schemas_are_inlined_in_the_prompt(self):
        agent, mocks = _build(hits=[_hit("orders"), _hit("customers")], prefetch_schemas=1)

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        prompt = strands_fake.last_agent.prompt
        assert "Candidate tables" in prompt
        assert "- orders" in prompt and "- customers" in prompt  # every candidate listed
        # Only the top prefetch_schemas tables get their full schema inlined.
        assert "### orders" in prompt and "### customers" not in prompt
        assert "Columns: id:int (pk)" in prompt

    @pytest.mark.asyncio
    async def test_prefetch_pins_the_source_without_any_tool_call(self):
        # The prefetched hits land in the catalog cache, so generate_sql can resolve
        # those names and the data source is pinned even if the model never calls a
        # discovery tool — which is the point of feeding the schema up front.
        agent, _ = _build(prefetch_schemas=2)

        async def driver(tools):
            generated = json.loads(await tools["generate_sql"](["orders"]))
            await tools["execute_sql"](generated["handle"])

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert outcome.succeeded and outcome.data_source_id == "ds-1"
        assert outcome.tables == ["orders"]

    @pytest.mark.asyncio
    async def test_disabled_prefetch_leaves_discovery_to_the_model(self):
        agent, mocks = _build(prefetch_schemas=0)

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert "Candidate tables" not in strands_fake.last_agent.prompt
        mocks["vector"].search.assert_awaited_once()  # only the model's own search

    @pytest.mark.asyncio
    async def test_a_prefetch_failure_falls_back_to_tool_discovery(self):
        # Retrieval is best-effort here: a backend failure must not fail the run.
        agent, mocks = _build(prefetch_schemas=2)
        mocks["vector"].search = AsyncMock(side_effect=[RuntimeError("index down"), [_hit("orders")]])

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        outcome = await _run(agent)

        assert outcome.succeeded
        assert "Candidate tables" not in strands_fake.last_agent.prompt


@pytest.mark.unit
class TestReportedConfidence:
    @pytest.mark.asyncio
    async def test_outcome_carries_the_writers_rating_for_the_executed_sql(self):
        # Not a constant: the score is the writer's self-rating for the statement
        # that actually ran, so it moves with the generation the agent settled on.
        agent, _ = _build()

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        outcome = await _run(agent)

        assert outcome.confidence == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_a_regenerated_statement_replaces_the_earlier_rating(self):
        agent, mocks = _build(exec_side_effect=[RuntimeError("bad col"), _exec_result(row_count=2)])
        mocks["generator"].generate_from_context = AsyncMock(
            side_effect=[(GEN_SQL, 0.4), ("SELECT count(*) FROM orders o", 0.7)]
        )

        async def driver(tools):
            await _generate_and_execute(tools)  # rated 0.4, fails
            await _generate_and_execute(tools)  # rated 0.7, succeeds

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert outcome.succeeded and outcome.confidence == pytest.approx(0.7)

    @pytest.mark.asyncio
    async def test_nothing_executed_reports_no_confidence(self):
        agent, _ = _build()

        install_fake_strands(lambda tools: tools["search_tables"]("orders"))
        outcome = await _run(agent)

        assert not outcome.succeeded and outcome.confidence == 0.0


@pytest.mark.unit
class TestEnvKnobs:
    @pytest.mark.asyncio
    async def test_env_knobs_are_read_when_not_passed(self, monkeypatch):
        # The strategy leaves these unset, so the env is read per run — a
        # redeploy-free knob. The documented floors (0 schemas, 5s) are clamped.
        monkeypatch.setenv("SERVE_DEEP_REASONING_PREFETCH_SCHEMAS", "-4")
        monkeypatch.setenv("SERVE_DEEP_REASONING_EXEC_TIMEOUT_S", "1")
        agent, mocks = _build(prefetch_schemas=None)

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert "Candidate tables" not in strands_fake.last_agent.prompt  # clamped to 0 = off
        assert mocks["executor"].execute.await_args.kwargs["timeout_seconds"] == 5

    @pytest.mark.asyncio
    async def test_unparseable_env_knobs_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("SERVE_DEEP_REASONING_PREFETCH_SCHEMAS", "three")
        monkeypatch.setenv("SERVE_DEEP_REASONING_EXEC_TIMEOUT_S", "")
        agent, mocks = _build(prefetch_schemas=None)

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert "Candidate tables" in strands_fake.last_agent.prompt  # default 3 = on
        assert mocks["executor"].execute.await_args.kwargs["timeout_seconds"] == 35

    @pytest.mark.asyncio
    async def test_deprecated_serve_agentic_env_knobs_still_honored(self, monkeypatch):
        """A deployment on the pre-rename names keeps its tuned values.

        Fails if the ``SERVE_AGENTIC_*`` fallback is dropped: the timeout would
        silently revert to the 35s default instead of the configured 9s, changing
        behaviour with no error anywhere.
        """
        monkeypatch.delenv("SERVE_DEEP_REASONING_PREFETCH_SCHEMAS", raising=False)
        monkeypatch.delenv("SERVE_DEEP_REASONING_EXEC_TIMEOUT_S", raising=False)
        monkeypatch.setenv("SERVE_AGENTIC_PREFETCH_SCHEMAS", "0")
        monkeypatch.setenv("SERVE_AGENTIC_EXEC_TIMEOUT_S", "9")
        agent, mocks = _build(prefetch_schemas=None)

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert "Candidate tables" not in strands_fake.last_agent.prompt  # 0 = prefetch off
        assert mocks["executor"].execute.await_args.kwargs["timeout_seconds"] == 9

    @pytest.mark.asyncio
    async def test_new_env_knob_wins_over_deprecated(self, monkeypatch):
        monkeypatch.setenv("SERVE_DEEP_REASONING_EXEC_TIMEOUT_S", "20")
        monkeypatch.setenv("SERVE_AGENTIC_EXEC_TIMEOUT_S", "9")
        agent, mocks = _build(prefetch_schemas=None)

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert mocks["executor"].execute.await_args.kwargs["timeout_seconds"] == 20


@pytest.mark.unit
class TestGraphTraversalTool:
    """The opt-in explore_graph tool: absent unless a graph tool is wired."""

    def _graph(self, result=None, side_effect=None):
        graph = MagicMock()
        graph.explore_graph = AsyncMock(
            return_value=result if result is not None else {"hops": 1, "tables": [{"table": "customers", "hop": 1}]},
            side_effect=side_effect,
        )
        return graph

    @pytest.mark.asyncio
    async def test_absent_by_default(self):
        # No graph tool → same four tools and a prompt with no mention of it, so
        # the arm is byte-identical to baseline.
        agent, _ = _build()

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert "explore_graph" not in strands_fake.last_agent.tools
        assert "explore_graph" not in strands_fake.last_agent.system_prompt

    @pytest.mark.asyncio
    async def test_offered_as_a_tool_without_touching_the_prompt(self):
        # Wiring the graph adds the TOOL and nothing else: the prompt stays
        # byte-identical, so the tool's own description is the only place it is
        # advertised and the flag isolates availability from prompt pressure.
        agent, _ = _build(graph=self._graph())

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert "explore_graph" in strands_fake.last_agent.tools
        assert strands_fake.last_agent.system_prompt == build_system_prompt()
        assert "explore_graph" not in strands_fake.last_agent.system_prompt

    @pytest.mark.asyncio
    async def test_traversal_result_is_returned_to_the_model(self):
        graph = self._graph()
        agent, _ = _build(graph=graph)
        captured = {}

        async def driver(tools):
            captured["explore"] = await tools["explore_graph"]("orders", 2, ["table"], 30)
            await _generate_and_execute(tools)

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert json.loads(captured["explore"])["tables"][0]["table"] == "customers"
        assert graph.explore_graph.await_args.args == ("orders", 2, ["table"], 30)
        assert outcome.succeeded  # traversal is advisory; it does not gate the answer

    @pytest.mark.asyncio
    async def test_traversal_failure_is_reported_not_raised(self):
        # A graph outage must cost at most one turn, never the whole request.
        agent, _ = _build(graph=self._graph(side_effect=RuntimeError("neptune down")))
        captured = {}

        async def driver(tools):
            captured["explore"] = await tools["explore_graph"]("orders")
            await _generate_and_execute(tools)

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert "graph_traversal_failed" in json.loads(captured["explore"])["error"]
        assert outcome.succeeded

    @pytest.mark.asyncio
    async def test_seed_is_normalized_before_the_lookup(self):
        graph = self._graph()
        agent, _ = _build(graph=graph)

        async def driver(tools):
            await tools["explore_graph"]("  Orders  ")

        install_fake_strands(driver)
        await _run(agent)

        assert graph.explore_graph.await_args.args[0] == "orders"


@pytest.mark.unit
class TestSystemPrompt:
    def test_base_prompt_directs_execution_through_handles(self):
        prompt = build_system_prompt(intent_review=False)

        assert prompt == AGENT_SYSTEM_PROMPT
        assert "execute_sql" in prompt and "pass the handle, not SQL text" in prompt
        # The removed raw-execution tool must not linger in the instructions.
        assert "run_sql" not in prompt

    def test_intent_review_block_is_opt_in(self):
        assert INTENT_REVIEW_BLOCK not in build_system_prompt(intent_review=False)
        assert INTENT_REVIEW_BLOCK in build_system_prompt(intent_review=True)

    def test_the_prompt_never_mentions_the_graph_tool(self):
        # The tool is advertised by its own docstring only — no second description,
        # no mandate to call it (see the note above build_system_prompt).
        assert "explore_graph" not in build_system_prompt(intent_review=False)
        assert "explore_graph" not in build_system_prompt(intent_review=True)

    def test_intent_review_defaults_to_the_env_flag(self, monkeypatch):
        monkeypatch.delenv("SERVE_DEEP_REASONING_INTENT_REVIEW", raising=False)
        assert build_system_prompt() == AGENT_SYSTEM_PROMPT

        monkeypatch.setenv("SERVE_DEEP_REASONING_INTENT_REVIEW", "true")
        assert INTENT_REVIEW_BLOCK in build_system_prompt()

    @pytest.mark.asyncio
    async def test_agent_uses_the_resolved_prompt(self, monkeypatch):
        monkeypatch.setenv("SERVE_DEEP_REASONING_INTENT_REVIEW", "1")
        agent, _ = _build()

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        await _run(agent)

        assert INTENT_REVIEW_BLOCK in strands_fake.last_agent.system_prompt


@pytest.mark.unit
class TestSqlAgentStops:
    @pytest.mark.asyncio
    async def test_sql_tools_refuse_past_the_execution_cap(self):
        agent, mocks = _build(max_executions=2)
        observations: list[dict] = []

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            for _ in range(4):
                generated = json.loads(await tools["generate_sql"](["orders"]))
                if "handle" not in generated:
                    observations.append(generated)
                    continue
                observations.append(json.loads(await tools["execute_sql"](generated["handle"])))

        install_fake_strands(driver)
        outcome = await _run(agent)

        assert mocks["executor"].execute.await_count == 2
        assert ["budget exhausted" in o.get("error", "") for o in observations] == [False, False, True, True]
        # The statement verified before the cap is still the answer.
        assert outcome.succeeded and outcome.executed_sql == GEN_SQL

    @pytest.mark.asyncio
    async def test_deadline_stop_keeps_the_last_verified_result(self):
        agent, _ = _build()

        async def driver(tools):
            await _generate_and_execute(tools)
            await asyncio.sleep(10)  # keeps looping past the deadline

        install_fake_strands(driver)
        deadline = Deadline(budget_s=sql_agent._DEADLINE_MARGIN_S + 1.0)
        outcome = await _run(agent, deadline=deadline)

        assert outcome.succeeded and outcome.executed_sql == GEN_SQL
        assert deadline.remaining_s() > sql_agent._DEADLINE_MARGIN_S - 2

    @pytest.mark.asyncio
    async def test_generations_are_capped_without_executions(self):
        agent, mocks = _build(max_executions=2)
        replies: list[dict] = []

        async def driver(tools):
            await tools["search_tables"]("orders", 5)
            for _ in range(6):
                replies.append(json.loads(await tools["generate_sql"](["orders"])))

        install_fake_strands(driver)
        await _run(agent)

        assert mocks["generator"].generate_from_context.await_count == 4  # 2 x max_executions
        assert all("budget exhausted" in r.get("error", "") for r in replies[4:])

    @pytest.mark.asyncio
    async def test_an_outer_cancel_still_propagates(self):
        agent, _ = _build()

        async def driver(tools):
            await asyncio.sleep(10)

        install_fake_strands(driver)
        task = asyncio.create_task(_run(agent, deadline=Deadline(budget_s=60.0)))
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
