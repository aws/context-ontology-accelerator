# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""options.sqlResultCheck (off by default): check a prefetch SQL result before accepting it."""

from __future__ import annotations

import asyncio

import pytest
from coa_serve.models import InvokeRequest
from coa_serve.tier3.agentic.budget import AgenticBudgetConfig, BudgetClock
from coa_serve.tier3.agentic.context import AccumulatedContext
from coa_serve.tier3.agentic.controller import ReasoningController
from coa_serve.tier3.agentic.planner import BedrockStepPlanner
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools.base import ToolResult
from coa_serve.trace import TraceCollector

pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[dict] = []

    async def converse(self, **kw):
        self.calls.append(kw)

        class R:
            guardrail_blocked = False

        r = R()
        r.text = self._text
        return r


def test_option_validated():
    assert InvokeRequest(query="q", namespace="ns", options={"sqlResultCheck": True}).options["sqlResultCheck"]
    with pytest.raises(ValueError):
        InvokeRequest(query="q", namespace="ns", options={"sqlResultCheck": "yes"})


class TestReplaceRows:
    def test_old_rows_dropped_and_a_repeated_row_kept(self):
        ctx = AccumulatedContext()
        ctx.add(ToolResult(rows=({"a": 1}, {"a": 2})))
        assert ctx.replace_rows(ToolResult(rows=({"a": 2}, {"a": 3}))) == 2
        assert ctx.rows == [{"a": 2}, {"a": 3}]


class TestPlannerCheck:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ('{"ok": true}', ""),
            ('```json\n{"ok": false, "problem": "parts exceed the total"}\n```', "parts exceed the total"),
            ("not json", ""),
        ],
    )
    async def test_reply_parsed(self, reply, expected):
        client = _Client(reply)
        planner = BedrockStepPlanner(client)
        assert await planner.check_sql_result(question="q", sql="SELECT 1", rows="[]") == expected
        assert "SELECT 1" in client.calls[0]["prompt"]

    @pytest.mark.asyncio
    async def test_prompt_rules_out_schema_judgements(self):
        client = _Client('{"ok": true}')
        await BedrockStepPlanner(client).check_sql_result(question="q", sql="SELECT 1", rows="[]")
        assert "NOT the schema" in client.calls[0]["system"]


class TestControllerCheck:
    @pytest.mark.asyncio
    async def test_no_sql_result_means_no_call(self):
        client = _Client('{"ok": false, "problem": "x"}')
        controller = ReasoningController(BedrockStepPlanner(client))
        assert await controller.check_sql_result("q", AccumulatedContext()) == ""
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_planner_error_accepts_the_result(self):
        class _Raising:
            async def check_sql_result(self, **_):
                raise RuntimeError("boom")

        controller = ReasoningController(_Raising())
        ctx = AccumulatedContext()
        ctx.last_sql = {"sql": "SELECT 1", "rows": "[]"}
        assert await controller.check_sql_result("q", ctx) == ""


class _SqlTool:
    """nl_to_sql fake: each call returns the next result and records its SQL like the real tool."""

    name = "nl_to_sql"
    wants_context = True

    def __init__(self, *results: tuple[str, ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    async def invoke(self, **kw):
        self.calls.append(kw)
        sql, result = self._results.pop(0)
        ctx = kw["accumulated_context"]
        if result.rows:
            ctx.last_sql = {"sql": sql, "rows": str(list(result.rows))}
            ctx.sql_log.append(sql)
        return result


class _Tools:
    def __init__(self, tool) -> None:
        self._tool = tool

    def get(self, name):
        return self._tool if name == "nl_to_sql" else None


class _Controller:
    def __init__(self, sufficient: bool, problem: str = "") -> None:
        self.sufficient = sufficient
        self.problem = problem
        self.checked = 0
        self.check_cancelled = False

    async def assess(self, sub_question, ctx):
        await asyncio.sleep(0)
        return self.sufficient, "working answer"

    async def check_sql_result(self, question, ctx):
        self.checked += 1
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.check_cancelled = True
            raise
        return self.problem


def _retriever(controller=None) -> AgenticRetriever:
    return AgenticRetriever(
        registry=object(),
        controller=controller or object(),
        decomposer=object(),
        synthesizer=object(),
        budget=AgenticBudgetConfig(),
        final_semantic_search=False,
    )


def _clock() -> BudgetClock:
    clock = BudgetClock(AgenticBudgetConfig())
    clock.started()
    return clock


_BAD = ("SELECT bad", ToolResult(rows=({"total": 5, "part": 0},), detail="nl_to_sql: 1 rows"))
_GOOD = ("SELECT good", ToolResult(rows=({"total": 5, "part": 3},), detail="nl_to_sql: 1 rows"))


async def _prefetch(controller, tool, options, *, schema_visible=False):
    ctx = AccumulatedContext()
    ctx.schema_visible = schema_visible
    trace = TraceCollector()
    sufficient = await _retriever(controller)._prefetch(
        "q",
        ctx,
        _clock(),
        trace,
        namespace="ns",
        tools=_Tools(tool),
        sql_allowed=True,
        read_allowed=False,
        tool_context={"options": options},
    )
    return sufficient, ctx, trace


class TestPrefetchCheck:
    @pytest.mark.asyncio
    async def test_flagged_result_is_rewritten_and_replaced(self):
        controller = _Controller(True, "parts are 0 beside a total of 5")
        tool = _SqlTool(_BAD, _GOOD)
        sufficient, ctx, trace = await _prefetch(controller, tool, {"sqlResultCheck": True, "sqlRevision": True})
        assert sufficient
        assert tool.calls[1]["revise"] == "parts are 0 beside a total of 5"
        assert ctx.rows == [{"total": 5, "part": 3}]
        assert ctx.sql_log == ["SELECT good"]
        # The working answer was written from the rejected rows.
        assert ctx.working_answers == []
        assert [s.step for s in trace.steps if s.step.startswith("sql_result")] == [
            "sql_result_check",
            "sql_result_revise",
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("schema_visible", [False, True])
    async def test_traced_reason_only_for_a_caller_who_may_see_the_schema(self, schema_visible):
        # The reason is written from the SQL and its rows, and the trace goes to every caller.
        controller = _Controller(True, "column part is 0 beside a total of 5")
        _, _, trace = await _prefetch(
            controller,
            _SqlTool(_BAD, _GOOD),
            {"sqlResultCheck": True, "sqlRevision": True},
            schema_visible=schema_visible,
        )
        detail = next(s.detail for s in trace.steps if s.step == "sql_result_check")
        assert detail == ("flagged: column part is 0 beside a total of 5" if schema_visible else "flagged")

    @pytest.mark.asyncio
    async def test_check_cancelled_when_the_loop_runs_anyway(self):
        controller = _Controller(False, "anything")
        tool = _SqlTool(_BAD)
        sufficient, ctx, _ = await _prefetch(controller, tool, {"sqlResultCheck": True, "sqlRevision": True})
        assert not sufficient
        assert controller.check_cancelled
        assert len(tool.calls) == 1 and ctx.rows == [{"total": 5, "part": 0}]

    @pytest.mark.asyncio
    async def test_off_when_not_requested(self):
        controller = _Controller(True, "anything")
        tool = _SqlTool(_BAD)
        await _prefetch(controller, tool, {"sqlRevision": True})
        assert controller.checked == 0 and len(tool.calls) == 1

    @pytest.mark.asyncio
    async def test_no_rewrite_when_the_check_passes(self):
        controller = _Controller(True, "")
        tool = _SqlTool(_BAD)
        _, ctx, _ = await _prefetch(controller, tool, {"sqlResultCheck": True, "sqlRevision": True})
        assert controller.checked == 1 and len(tool.calls) == 1
        assert ctx.working_answers


class TestRevisePrefetchSql:
    @pytest.mark.asyncio
    async def test_no_rows_keeps_the_first_result(self):
        ctx = AccumulatedContext()
        ctx.add(_BAD[1])
        ctx.last_sql = {"sql": "SELECT bad", "rows": "[]"}
        ctx.sql_log = ["SELECT bad"]
        tool = _SqlTool(("SELECT empty", ToolResult(detail="nl_to_sql: 0 rows")))
        ok = await _retriever()._revise_prefetch_sql(
            "p", "q", ctx, _clock(), TraceCollector(), namespace="ns", tools=_Tools(tool), tool_context={}
        )
        assert not ok
        assert ctx.rows == [{"total": 5, "part": 0}]
        assert ctx.sql_log == ["SELECT bad"]

    @pytest.mark.asyncio
    async def test_same_sql_stays_in_the_log(self):
        ctx = AccumulatedContext()
        ctx.add(_BAD[1])
        ctx.last_sql = {"sql": "SELECT same", "rows": "[]"}
        ctx.sql_log = ["SELECT same"]

        class _Same(_SqlTool):
            async def invoke(self, **kw):
                self.calls.append(kw)
                return _GOOD[1]

        ok = await _retriever()._revise_prefetch_sql(
            "p", "q", ctx, _clock(), TraceCollector(), namespace="ns", tools=_Tools(_Same()), tool_context={}
        )
        assert ok and ctx.sql_log == ["SELECT same"]
