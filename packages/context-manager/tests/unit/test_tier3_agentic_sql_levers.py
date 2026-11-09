# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the SQL-writer levers (all per-request, all off by default).

``sqlOriginalQuestion``, ``tokenCaps.nl2sql``, ``sqlTimeRules`` and
``sqlComputeMetric``: default prompts/calls are unchanged; each option changes
exactly its own piece. ``sqlComputeMetric`` reaches the writer only through the
deep-reasoning nl_to_sql tool.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from coa_serve.clients.base import ConverseResult
from coa_serve.clients.opensearch import VectorHit
from coa_serve.models import InvokeRequest
from coa_serve.request_caps import token_cap
from coa_serve.tier2.nl_to_sql.sql_generator import SQLGenerator, WriterOptions, _build_system_prompt
from coa_serve.tier2.nl_to_sql.strategy import writer_options
from coa_serve.tier3.agentic import AgenticBudgetConfig
from coa_serve.tier3.agentic.models import SubQuestion
from coa_serve.tier3.agentic.retriever import AgenticRetriever
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.agentic.tools.structured_tool import StructuredQueryTool
from coa_serve.tier3.synthesizer import SynthesisResult

pytestmark = pytest.mark.unit

NS = "acme"


def _generator():
    llm = AsyncMock()
    llm.embed.return_value = [0.1] * 4
    llm.converse.return_value = ConverseResult(text="```sql\nSELECT 1\n```\nConfidence: 0.9")
    vector = AsyncMock()
    vector.count_documents.return_value = 1
    vector.search.return_value = [
        VectorHit(id="1", text="Table: t | Columns: a (int)", score=0.9, metadata={"entity_type": "class"})
    ]
    return SQLGenerator(llm_client=llm, vector_client=vector), llm


class TestWriterOptions:
    def test_from_options(self):
        assert writer_options({}) == WriterOptions()
        w = writer_options(
            {
                "sqlOriginalQuestion": True,
                "originalQuestion": "orig",
                "tokenCaps": {"nl2sql": 99_999},
                "sqlTimeRules": True,
                "computeMetric": True,
            }
        )
        assert w == WriterOptions(original_question="orig", max_tokens=16000, time_rules=True, compute_metric=True)
        # originalQuestion alone (without the switch) is ignored
        assert writer_options({"originalQuestion": "orig"}).original_question == ""

    def test_compute_metric_ignored_on_a_direct_tier2_request(self):
        # Its extra input columns would change a direct Tier-2 answer's shape.
        w = writer_options({"sqlComputeMetric": True})
        assert w.compute_metric is False
        assert "EXCEPTION to 'SELECT EXACTLY'" not in _build_system_prompt("athena", w.extra_rules())

    def test_rules_only_when_requested(self):
        base = _build_system_prompt("athena")
        assert "TIME WINDOWS" not in base and "EXCEPTION to 'SELECT EXACTLY'" not in base
        extra = WriterOptions(time_rules=True, compute_metric=True).extra_rules()
        prompt = _build_system_prompt("athena", extra)
        assert "INTERVAL 'N' HOUR" in prompt and "HALF-OPEN" in prompt and "EXCEPTION to 'SELECT EXACTLY'" in prompt
        assert prompt.startswith(base.split("\n\nExamples:")[0].rsplit("\n", 1)[0])
        # The rules sit right before the worked examples.
        assert prompt.index(extra) < prompt.index("\n\nExamples:")

    @pytest.mark.asyncio
    async def test_default_generate_call_unchanged(self):
        gen, llm = _generator()
        await gen.generate("How many?", namespace="ns")
        kw = llm.converse.call_args.kwargs
        assert kw["guard_content"] == "How many?" and kw["max_tokens"] == 4096
        assert kw["system"] == gen._system_prompt

    @pytest.mark.asyncio
    async def test_writer_options_reach_generate_and_correct(self):
        gen, llm = _generator()
        writer = WriterOptions(original_question="Older ones only?", max_tokens=8000, time_rules=True)
        await gen.generate("How many?", namespace="ns", writer=writer)
        kw = llm.converse.call_args.kwargs
        assert kw["guard_content"] == "How many?\n\nOriginal question (for constraints): Older ones only?"
        assert kw["max_tokens"] == 8000 and "TIME WINDOWS" in kw["system"]
        await gen.correct("How many?", "ddl", failed_sql="SELECT x", execution_error="boom", writer=writer)
        kw = llm.converse.call_args.kwargs
        prompt = llm.converse.call_args.args[0]
        assert "Original question (for constraints): Older ones only?" in prompt
        assert kw["max_tokens"] == 8000 and "TIME WINDOWS" in kw["system"]

    def test_same_question_not_repeated(self):
        assert WriterOptions(original_question="q").guard_text("q") == "q"


class _Tier:
    def __init__(self, rows) -> None:
        self.rows, self.contexts = rows, []

    async def resolve(self, query, namespace, context, option=None):
        self.contexts.append(context)
        return SimpleNamespace(rows=self.rows, sql="", sparql="", strategy_name="nl_to_sql", confidence=0.9)


def _tool(rows=({"n": 3},)):
    return StructuredQueryTool(_Tier(list(rows)), name="nl_to_sql", description="d", option="nl_to_sql")


class TestStructuredTool:
    @pytest.mark.asyncio
    async def test_original_question_forwarded_and_used_only_with_switch(self):
        from coa_serve.tier2.nl_to_sql.strategy import writer_options

        tool = _tool()
        await tool.invoke(namespace=NS, query="sub", options={}, original_question="orig")
        sent = tool._tier.contexts[0].options
        assert sent["originalQuestion"] == "orig"  # the session gates what it sends
        assert writer_options(sent).original_question == ""  # the writer still needs the switch
        assert writer_options({**sent, "sqlOriginalQuestion": True}).original_question == "orig"

    @pytest.mark.asyncio
    async def test_compute_metric_reaches_the_writer_only_through_the_tool(self):
        tool = _tool()
        await tool.invoke(namespace=NS, query="q", options={"sqlComputeMetric": True})
        await tool.invoke(namespace=NS, query="q", options={"sqlComputeMetric": True}, compute_metric=True)
        plain, metric = (writer_options(c.options).extra_rules() for c in tool._tier.contexts)
        assert "EXCEPTION to 'SELECT EXACTLY'" not in plain
        assert "EXCEPTION to 'SELECT EXACTLY'" in metric


# ── retriever wiring ───────────────────────────────────────────────


class _Ctl:
    def __init__(self) -> None:
        self.seen = []

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **kw):
        self.seen.append(dict(kw.get("tool_context") or {}))
        return None


class _Dec:
    async def decompose_with_language(self, query, trace=None):
        return [SubQuestion(text="decomposed sub-question")], "en"


class _Synth:
    def __init__(self) -> None:
        self.kwargs = []

    async def synthesize(self, query, chunks, entities, **kw):
        self.kwargs.append(kw)
        return SynthesisResult(answer="ok", confidence=0.8, duration_ms=1)


class TestRetrieverWiring:
    @pytest.mark.asyncio
    async def test_original_question_only_with_switch(self):
        ctl, synth = _Ctl(), _Synth()
        r = AgenticRetriever(
            ToolRegistry(), ctl, _Dec(), synth, AgenticBudgetConfig(max_steps=2), final_semantic_search=False
        )
        await r.resolve("the original question", NS)
        await r.resolve("the original question", NS, options={"sqlOriginalQuestion": True})
        tc0, tc1 = ctl.seen
        assert "original_question" not in tc0
        assert tc1["original_question"] == "the original question"

    @pytest.mark.asyncio
    async def test_compute_metric_only_with_switch(self):
        ctl, synth = _Ctl(), _Synth()
        r = AgenticRetriever(
            ToolRegistry(), ctl, _Dec(), synth, AgenticBudgetConfig(max_steps=2), final_semantic_search=False
        )
        await r.resolve("q", NS)
        await r.resolve("q", NS, options={"sqlComputeMetric": True})
        tc0, tc1 = ctl.seen
        assert "compute_metric" not in tc0
        assert tc1["compute_metric"] is True


def test_nl2sql_cap_and_validation():
    assert token_cap({"tokenCaps": {"nl2sql": 99_999}}, "nl2sql") == 16000
    ok = {
        "tokenCaps": {"planner": 4000, "nl2sql": 8000},
        "sqlTimeRules": True,
        "sqlComputeMetric": True,
        "sqlOriginalQuestion": True,
        "plannerParseRetry": True,
    }
    assert InvokeRequest(query="q", namespace=NS, options=ok).options == ok
    for bad in ({"tokenCaps": {"nl2sql": "1"}}, {"sqlTimeRules": 1}, {"sqlComputeMetric": "yes"}):
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options=bad)


# ── sqlRevision ────────────────────────────────────────────────────


class _SqlTier(_Tier):
    async def resolve(self, query, namespace, context, option=None):
        self.contexts.append(context)
        return SimpleNamespace(
            rows=self.rows, sql="SELECT n FROM t", sparql="", strategy_name="nl_to_sql", confidence=0.9
        )


class TestSqlRevision:
    @pytest.mark.asyncio
    async def test_revise_sends_previous_sql_and_rows_as_feedback(self):
        from coa_serve.tier3.agentic.context import AccumulatedContext

        tier = _SqlTier([{"n": -3}])
        tool = StructuredQueryTool(tier, name="nl_to_sql", description="d", option="nl_to_sql").revision_variant()
        assert "revise" in tool.input_schema and tool.wants_context is True and "'revise'" in tool.description
        ctx, opts = AccumulatedContext(), {"sqlRevision": True}
        await tool.invoke(namespace=NS, query="q", options=opts, accumulated_context=ctx)
        assert ctx.last_sql["sql"] == "SELECT n FROM t" and "-3" in ctx.last_sql["rows"]
        assert "sqlFeedback" not in tier.contexts[0].options
        await tool.invoke(namespace=NS, query="q", options=opts, accumulated_context=ctx, revise="negative excess")
        feedback = tier.contexts[1].options["sqlFeedback"]
        assert "SELECT n FROM t" in feedback and "-3" in feedback and "negative excess" in feedback

    @pytest.mark.asyncio
    async def test_revise_after_an_empty_result_revises_that_query(self):
        from coa_serve.tier3.agentic.context import AccumulatedContext

        tier = _SqlTier([{"n": 1}])
        tool = StructuredQueryTool(tier, name="nl_to_sql", description="d", option="nl_to_sql").revision_variant()
        ctx, opts = AccumulatedContext(), {"sqlRevision": True}
        await tool.invoke(namespace=NS, query="q", options=opts, accumulated_context=ctx)
        assert ctx.last_sql["row_count"] == 1
        tier.rows = []
        await tool.invoke(namespace=NS, query="q2", options=opts, accumulated_context=ctx)
        assert ctx.last_sql == {"sql": "SELECT n FROM t", "rows": "[]", "row_count": 0}
        await tool.invoke(namespace=NS, query="q2", options=opts, accumulated_context=ctx, revise="no rows")
        assert "returned 0 row(s)" in tier.contexts[2].options["sqlFeedback"]

    def test_internal_options_rejected_from_callers(self):
        from coa_serve.models import InvokeRequest

        for key in ("sqlFeedback", "originalQuestion", "evidenceMaxChars", "computeMetric"):
            with pytest.raises(ValueError, match="set internally"):
                InvokeRequest(query="q", namespace=NS, options={key: "x"})

    @pytest.mark.asyncio
    async def test_revise_ignored_without_the_option(self):
        from coa_serve.tier3.agentic.context import AccumulatedContext

        tier = _SqlTier([{"n": 1}])
        tool = StructuredQueryTool(tier, name="nl_to_sql", description="d", option="nl_to_sql")
        assert "revise" not in tool.input_schema and not getattr(tool, "wants_context", False)
        ctx = AccumulatedContext()
        await tool.invoke(namespace=NS, query="q", accumulated_context=ctx, revise="x")
        assert ctx.last_sql is None and "sqlFeedback" not in tier.contexts[0].options

    def test_composes_with_the_evidence_variant(self):
        from coa_serve.tier3.agentic.tools.structured_tool import EvidenceStructuredQueryTool

        base = StructuredQueryTool(_SqlTier([]), name="nl_to_sql", description="d", option="nl_to_sql")
        both = base.evidence_variant().revision_variant()
        assert isinstance(both, EvidenceStructuredQueryTool) and "revise" in both.input_schema
        assert "revise" not in base.input_schema and "revise" not in base.evidence_variant().input_schema

    def test_writer_options_carry_feedback(self):
        assert writer_options({"sqlFeedback": "prev"}).feedback == "prev"
        assert writer_options({}).feedback == "" and writer_options({"sqlFeedback": 3}).feedback == ""

    @pytest.mark.asyncio
    async def test_feedback_reaches_the_first_shot_prompt(self):
        gen, llm = _generator()
        await gen.generate("How many?", namespace="ns", writer=WriterOptions(feedback="Previous SQL:\nSELECT 0"))
        prompt = llm.converse.call_args.args[0]
        assert "Prior attempt in this session" in prompt and "SELECT 0" in prompt
        await gen.generate("How many?", namespace="ns")
        assert "Prior attempt in this session" not in llm.converse.call_args.args[0]

    def test_planner_content_view_shows_the_latest_sql(self):
        from coa_serve.tier3.agentic.context import AccumulatedContext
        from coa_serve.tier3.agentic.planner import BedrockStepPlanner

        ctx = AccumulatedContext()
        ctx.rows.append({"n": 3})
        assert "SQL that produced the latest rows" not in BedrockStepPlanner._content_view(ctx)
        ctx.last_sql = {"sql": "SELECT n FROM t", "rows": "[{'n': 3}]"}
        assert "SQL that produced the latest rows:\nSELECT n FROM t" in BedrockStepPlanner._content_view(ctx)

    def test_planner_keeps_the_schema_out_of_its_working_answer_unless_visible(self):
        # The working answer is traced and is the fallback answer, so a caller who may not
        # see the schema gets the same rule as the answer step.
        from coa_serve.tier3.agentic.context import AccumulatedContext
        from coa_serve.tier3.agentic.planner import _WORKING_ANSWER_NO_SCHEMA, BedrockStepPlanner

        ctx = AccumulatedContext()
        ctx.rows.append({"n": 3})
        ctx.last_sql = {"sql": "SELECT n FROM t", "rows": "[{'n': 3}]"}
        assert _WORKING_ANSWER_NO_SCHEMA in BedrockStepPlanner._content_view(ctx)
        ctx.schema_visible = True
        assert _WORKING_ANSWER_NO_SCHEMA not in BedrockStepPlanner._content_view(ctx)

    def test_option_validated(self):
        assert InvokeRequest(query="q", namespace=NS, options={"sqlRevision": True}).options["sqlRevision"] is True
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options={"sqlRevision": "yes"})


# ── sqlInAnswer ────────────────────────────────────────────────────


class TestSqlInAnswer:
    def test_synthesis_prompt_shows_sql_and_note_only_with_queries(self):
        from coa_serve.tier3.synthesizer import Synthesizer

        synth = Synthesizer(object(), guardrail_id="gr-1")
        rows = [{"n": 3}]
        prompt, nonce, marker = synth._build_prompt("q", [], [], structured_data=rows)
        assert "SQL queries that produced" not in prompt
        prompt, nonce, marker = synth._build_prompt("q", [], [], structured_data=rows, structured_queries=["SELECT 3"])
        assert "SQL queries that produced these rows" in prompt and "SELECT 3" in prompt
        assert "retrieved evidence" in synth._build_system_instruction(nonce, marker, sql_evidence=True)
        assert "retrieved evidence" not in synth._build_system_instruction(nonce, marker)

    def test_answer_names_tables_and_columns_only_when_the_schema_is_visible(self):
        from coa_serve.tier3.synthesizer import Synthesizer

        synth = Synthesizer(object(), guardrail_id="gr-1")
        hidden = synth._build_system_instruction("n", "m", sql_evidence=True)
        assert "which table and column" not in hidden
        assert "do not name its tables or columns" in hidden and "Do not quote the SQL" in hidden
        shown = synth._build_system_instruction("n", "m", sql_evidence=True, schema_visible=True)
        assert "which table and column each one comes from" in shown

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_both_answer_paths_carry_the_findings_rule_and_the_no_schema_note(self, stream):
        from coa_serve.tier3.synthesizer import _PRELIMINARY_FINDINGS_PRIVATE, Synthesizer

        seen: dict = {}

        class _Bedrock:
            async def converse(self, prompt, **kw):
                seen["system"] = kw["system"]
                return ConverseResult(text="7 accounts. [CONFIDENCE: 0.8]", guardrail_blocked=False)

            async def converse_stream(self, prompt, **kw):
                seen["system"] = kw["system"]
                yield "7 accounts. [CONFIDENCE: 0.8]"

        synth = Synthesizer(_Bedrock(), guardrail_id="gr-1")
        call = synth.synthesize_stream if stream else synth.synthesize
        await call(
            "How many?",
            [],
            [],
            structured_data=[{"n": 7}],
            structured_queries=["SELECT n FROM accounts"],
            working_answer="7",
        )
        assert _PRELIMINARY_FINDINGS_PRIVATE in seen["system"]
        assert "do not name its tables or columns" in seen["system"]

    @pytest.mark.asyncio
    async def test_tool_logs_each_distinct_query(self):
        from coa_serve.tier3.agentic.context import AccumulatedContext

        tool = StructuredQueryTool(_SqlTier([{"n": 1}]), name="nl_to_sql", description="d", option="nl_to_sql")
        ctx = AccumulatedContext()
        await tool.invoke(namespace=NS, query="q", accumulated_context=ctx)
        await tool.invoke(namespace=NS, query="q", accumulated_context=ctx)
        assert ctx.sql_log == ["SELECT n FROM t"] and ctx.fork().sql_log is ctx.sql_log

    @pytest.mark.asyncio
    async def test_retriever_passes_queries_only_with_the_option(self):
        from coa_serve.tier3.agentic.context import AccumulatedContext

        synth = _Synth()
        r = AgenticRetriever(
            ToolRegistry(), _Ctl(), _Dec(), synth, AgenticBudgetConfig(max_steps=2), final_semantic_search=False
        )
        ctx = AccumulatedContext()
        ctx.rows.append({"n": 3})
        ctx.sql_log.append("SELECT 3")
        await r._synthesize("q", ctx, ctx.chunks, catalog_summary=None, conversation_history=None, on_token=None)
        ctx.sql_in_answer = True
        await r._synthesize("q", ctx, ctx.chunks, catalog_summary=None, conversation_history=None, on_token=None)
        assert "structured_queries" not in synth.kwargs[0]
        assert synth.kwargs[1]["structured_queries"] == ["SELECT 3"]
        # The schema stays hidden unless the orchestrator says the caller may see it.
        assert synth.kwargs[1]["schema_visible"] is False
        ctx.schema_visible = True
        await r._synthesize("q", ctx, ctx.chunks, catalog_summary=None, conversation_history=None, on_token=None)
        assert synth.kwargs[2]["schema_visible"] is True

    def test_option_validated(self):
        assert InvokeRequest(query="q", namespace=NS, options={"sqlInAnswer": True}).options["sqlInAnswer"] is True
        with pytest.raises(ValueError):
            InvokeRequest(query="q", namespace=NS, options={"sqlInAnswer": 1})


def test_sql_feedback_cannot_be_sent_by_a_caller():
    from coa_serve.models import InvokeRequest

    with pytest.raises(ValueError, match="sqlFeedback"):
        InvokeRequest(query="q", namespace="ns-1234", options={"sqlRevision": True, "sqlFeedback": "drop table"})


class _RowsController:
    """Loop stand-in: gathers one structured row and leaves a working answer naming its column."""

    async def run_one_loop(self, sub_question, ctx, registry, clock, queue, trace, *, namespace, **_):
        from coa_serve.tier3.agentic.models import StopReason

        ctx.rows.append({"churned_accounts": 7})
        ctx.set_working_answer(sub_question.text, "churned_accounts in table accounts is 7")
        return StopReason.sufficiency_satisfied


class _OneQuestion:
    async def decompose(self, query, trace=None):
        return [SubQuestion(text=query)]


class _AnswerOnly:
    async def synthesize(self, query, chunks, entities, **_kw):
        return SynthesisResult(answer="7 accounts churned.", confidence=0.8, duration_ms=1)


@pytest.mark.unit
class TestWorkingAnswerTrace:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("schema_visible", [False, True])
    async def test_working_answer_text_traced_only_for_a_caller_who_may_see_the_schema(self, schema_visible):
        from coa_serve.trace import TraceCollector

        class _Clock:
            t = 1000.0

            def __call__(self):
                return self.t

        retriever = AgenticRetriever(
            registry=object(),
            controller=_RowsController(),
            decomposer=_OneQuestion(),
            synthesizer=_AnswerOnly(),
            budget=AgenticBudgetConfig(),
            clock_source=_Clock(),
            final_semantic_search=False,
        )
        trace = TraceCollector()
        await retriever.resolve("How many accounts churned?", "ns", trace=trace, schema_visible=schema_visible)
        detail = next(s.detail for s in trace.steps if s.step == "agentic_working_answer")
        assert ("churned_accounts" in detail) is schema_visible
