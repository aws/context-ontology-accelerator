# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the NL→SQL structured tool and its composition gating (iteration 3).

The tool wraps ``StructuredQueryTier`` and produces on ``ToolResult.rows`` so a
structured answer counts as progress in the loop. Gating is done by excluding the
tool from ``registry.specs(exclude=...)`` per request — a namespace with no
structured source never shows the tool to the planner.
"""

from __future__ import annotations

import pytest
from coa_serve.tier3.agentic.tools.registry import ToolRegistry
from coa_serve.tier3.agentic.tools.structured_tool import (
    NL_TO_SPARQL_TOOL,
    NL_TO_SQL_TOOL,
    StructuredQueryTool,
    build_structured_tools,
)


def NlToSqlTool(tier, *, embed_client=None):  # noqa: N802 - test helper keeps old call sites
    """Build the SQL-flavored tool the way the factory does."""
    return StructuredQueryTool(
        tier, name=NL_TO_SQL_TOOL, description="sql", option="nl_to_sql", embed_client=embed_client
    )


def build_nl_to_sql_tool(registry, tier):
    """Register both structured tools; return the SQL one's name (old contract)."""
    build_structured_tools(registry, tier)
    return NL_TO_SQL_TOOL


class _FakeStrategyResult:
    """StrategyResult-shaped stand-in (attribute access only)."""

    def __init__(self, rows, *, confidence=0.9, strategy_name="nl_to_sql", sparql=""):
        self.sql = "SELECT 1"
        self.rows = rows
        self.confidence = confidence
        self.strategy_name = strategy_name
        # StrategyResult.sparql defaults to "" (empty for the NL→SQL path, set on
        # the VKG/ontop path). The structured tool reads it to capture query
        # artifacts (#1166), so the stand-in must expose it too.
        self.sparql = sparql


class _FakeTier:
    """StructuredQueryTier stand-in recording the resolve call."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    async def resolve(self, query, namespace, context, **kwargs):
        self.calls.append({"query": query, "namespace": namespace, "context": context, "option": kwargs.get("option")})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.unit
class TestNlToSqlTool:
    async def test_returns_rows_on_the_rows_channel(self):
        tier = _FakeTier(_FakeStrategyResult([{"revenue": 100}, {"revenue": 200}]))
        tool = NlToSqlTool(tier)

        result = await tool.invoke(namespace="ns", query="total revenue?")

        assert result.rows == ({"revenue": 100}, {"revenue": 200})
        assert result.chunks == () and result.entities == ()
        assert "2 rows" in result.detail

    async def test_rows_mirrored_into_items(self):
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        result = await NlToSqlTool(tier).invoke(namespace="ns", query="q")
        assert result.items == ({"type": "row", "a": 1},)

    async def test_captures_sql_query_artifact(self):
        """#1166 cut #3: the tool captures the SQL it executed so Deep Reasoning
        can return it. NL→SQL has no SPARQL, so only a single SQL artifact."""
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        result = await NlToSqlTool(tier).invoke(namespace="ns", query="q")
        assert [(e.kind, e.query) for e in result.executed_queries] == [("sql", "SELECT 1")]

    async def test_captures_both_sparql_and_sql_on_vkg_path(self):
        """The VKG/ontop path produces a SPARQL statement AND the SQL it compiled
        to — both are captured (SPARQL first, then SQL)."""
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}], sparql="SELECT ?x WHERE { ?x a :T }"))
        result = await NlToSqlTool(tier).invoke(namespace="ns", query="q")
        assert [(e.kind, e.query) for e in result.executed_queries] == [
            ("sparql", "SELECT ?x WHERE { ?x a :T }"),
            ("sql", "SELECT 1"),
        ]

    async def test_no_rows_captures_no_query_artifacts(self):
        """A tool that returned no rows surfaces no query artifacts (nothing ran
        to completion worth showing)."""
        result = await NlToSqlTool(_FakeTier(_FakeStrategyResult([]))).invoke(namespace="ns", query="q")
        assert result.executed_queries == ()

    async def test_none_result_is_empty_not_raised(self):
        result = await NlToSqlTool(_FakeTier(None)).invoke(namespace="ns", query="q")
        assert result.rows == ()
        assert "no rows" in result.detail

    async def test_empty_rows_result_is_empty(self):
        result = await NlToSqlTool(_FakeTier(_FakeStrategyResult([]))).invoke(namespace="ns", query="q")
        assert result.rows == ()
        assert "0 rows" in result.detail

    async def test_exception_is_swallowed_into_empty_result(self):
        """A Tool must never raise into the reasoning loop (Req 9.1)."""
        result = await NlToSqlTool(_FakeTier(RuntimeError("db down"))).invoke(namespace="ns", query="q")
        assert result.rows == ()
        assert "error" in result.detail
        assert "RuntimeError" in result.detail

    async def test_namespace_and_query_forwarded_to_tier(self):
        tier = _FakeTier(_FakeStrategyResult([{"x": 1}]))
        await NlToSqlTool(tier).invoke(namespace="ns-42", query="how many?")
        assert tier.calls[-1]["namespace"] == "ns-42"
        assert tier.calls[-1]["query"] == "how many?"

    def test_interface_fields_present(self):
        tool = NlToSqlTool(_FakeTier(None))
        assert tool.name == NL_TO_SQL_TOOL
        assert tool.description.strip()
        assert isinstance(tool.input_schema, dict)
        assert "rows" in tool.output_schema


@pytest.mark.unit
class TestCompositionGating:
    """The 'don't present structured tools when no structured source' requirement:
    exclusion hides the tool from the planner's spec list."""

    def test_build_registers_tool(self):
        reg = ToolRegistry()
        name = build_nl_to_sql_tool(reg, _FakeTier(None))
        assert name == NL_TO_SQL_TOOL
        assert NL_TO_SQL_TOOL in reg.names()

    def test_specs_exclude_hides_the_tool(self):
        reg = ToolRegistry()
        build_nl_to_sql_tool(reg, _FakeTier(None))

        visible = [s["name"] for s in reg.specs()]
        gated = [s["name"] for s in reg.specs(exclude=frozenset({NL_TO_SQL_TOOL}))]

        assert NL_TO_SQL_TOOL in visible
        assert NL_TO_SQL_TOOL not in gated

    def test_exclude_unregistered_name_is_noop(self):
        reg = ToolRegistry()
        build_nl_to_sql_tool(reg, _FakeTier(None))
        # Excluding a name that isn't registered must not drop anything else.
        specs = reg.specs(exclude=frozenset({"does_not_exist"}))
        assert [s["name"] for s in specs] == [NL_TO_SQL_TOOL, NL_TO_SPARQL_TOOL]

    def test_get_still_returns_excluded_tool(self):
        """Exclusion only affects the planner's VIEW (specs); the tool stays
        registered and invokable if named directly."""
        reg = ToolRegistry()
        build_nl_to_sql_tool(reg, _FakeTier(None))
        assert reg.get(NL_TO_SQL_TOOL) is not None


@pytest.mark.unit
class TestBidirectionalCompositionGating:
    """Composition gating runs BOTH ways: structured tools hidden without a DATABASE
    source, and the chunk/topic vector retrievers hidden without a DOCUMENTS source.
    A MIXED namespace withholds nothing."""

    @staticmethod
    def _gate(*, structured: bool, unstructured: bool) -> frozenset[str]:
        """Mirror of the retriever's composition_gated derivation."""
        from coa_serve.tier3.agentic.retriever import (
            _STRUCTURED_TOOL_NAMES,
            _UNSTRUCTURED_TOOL_NAMES,
        )

        gated: frozenset[str] = frozenset()
        if not structured:
            gated |= _STRUCTURED_TOOL_NAMES
        if not unstructured:
            gated |= _UNSTRUCTURED_TOOL_NAMES
        return gated

    def test_mixed_namespace_withholds_nothing(self):
        """Either kind of tool might answer, so the planner chooses."""
        assert self._gate(structured=True, unstructured=True) == frozenset()

    def test_document_only_hides_structured_tools(self):
        gated = self._gate(structured=False, unstructured=True)
        assert NL_TO_SQL_TOOL in gated
        assert "vector_search" not in gated

    def test_structured_only_hides_the_vector_retrievers(self):
        """The asymmetry fix: a structured-only namespace has no chunk index, so the
        chunk/topic retrievers can only return empty and waste reasoning steps.
        embedding=None is NOT sufficient — vector_tool re-embeds from query text."""
        gated = self._gate(structured=True, unstructured=False)
        assert "vector_search" in gated
        assert "chunk_window" in gated
        assert "strategy:chunk_based_semantic" in gated
        assert "strategy:topic_beam" in gated
        assert NL_TO_SQL_TOOL not in gated

    def test_graph_and_ontology_tools_are_never_withheld(self):
        """graph_traversal walks the graph (exists for structured namespaces too) and
        ontology_lookup needs no data source at all."""
        for structured, unstructured in ((True, False), (False, True), (True, True)):
            gated = self._gate(structured=structured, unstructured=unstructured)
            assert "graph_traversal" not in gated
            assert "ontology_lookup" not in gated


@pytest.mark.unit
class TestSparqlToolAndEmbedding:
    """The two review findings: NL→SPARQL is its own selectable tool, and the tools
    pass a SUB-QUESTION embedding so Ontop gets its ontology vector context."""

    def test_both_structured_tools_registered_separately(self):
        reg = ToolRegistry()
        names = build_structured_tools(reg, _FakeTier(None))
        assert names == [NL_TO_SQL_TOOL, NL_TO_SPARQL_TOOL]
        # Distinct descriptions — the planner must be able to tell them apart.
        by_name = {s["name"]: s["description"] for s in reg.specs()}
        assert "SQL" in by_name[NL_TO_SQL_TOOL]
        assert "SPARQL" in by_name[NL_TO_SPARQL_TOOL]
        assert by_name[NL_TO_SQL_TOOL] != by_name[NL_TO_SPARQL_TOOL]

    async def test_each_tool_pins_its_own_strategy_option(self):
        """Previously only nl_to_sql existed and it fell through to the tier's default
        (nl_to_sql_first), so the Ontop/SPARQL path was never selectable on purpose."""
        reg = ToolRegistry()
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        build_structured_tools(reg, tier)

        await reg.get(NL_TO_SQL_TOOL).invoke(namespace="ns", query="q")
        assert tier.calls[-1]["option"] == "nl_to_sql"

        await reg.get(NL_TO_SPARQL_TOOL).invoke(namespace="ns", query="q")
        assert tier.calls[-1]["option"] == "ontop"

    async def test_embeds_the_subquestion_not_the_original_query(self):
        """Tools are invoked per sub-question, so the embedding must be of the text
        the tool was actually given (matches vector_tool's behavior)."""

        class _Embed:
            def __init__(self):
                self.embedded = []

            async def embed(self, text):
                self.embedded.append(text)
                return [0.1, 0.2]

        embed = _Embed()
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        tool = NlToSqlTool(tier, embed_client=embed)

        await tool.invoke(namespace="ns", query="sub-question text")

        assert embed.embedded == ["sub-question text"]
        # ...and it reaches the strategy, which is what gives Ontop its vector hits.
        assert tier.calls[-1]["context"].embedding == [0.1, 0.2]

    async def test_no_embed_client_still_works(self):
        """Degrades to embedding=None (SQLGenerator self-embeds) rather than failing."""
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        result = await NlToSqlTool(tier).invoke(namespace="ns", query="q")
        assert result.rows == ({"a": 1},)
        assert tier.calls[-1]["context"].embedding is None

    async def test_embed_failure_degrades_without_raising(self):
        class _Boom:
            async def embed(self, text):
                raise RuntimeError("bedrock down")

        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        result = await NlToSqlTool(tier, embed_client=_Boom()).invoke(namespace="ns", query="q")
        # The query still runs, just without ontology vector context.
        assert result.rows == ({"a": 1},)
        assert tier.calls[-1]["context"].embedding is None


@pytest.mark.unit
class TestOutOfBandRequestContext:
    """profile / options / model_id must reach StrategyContext.

    Before this, the controller invoked tools with only ``namespace`` + planner
    inputs, so ``profile`` was always None -> ``StrategyContext(profile={})`` ->
    SQLFirewall/Cedar resolved no roles -> fail-CLOSED AccessDeniedError, which the
    tool's blanket ``except`` rendered as a plain "0 rows" retrieval miss. The deny
    is also RE-RAISED by ``_resolve_sequential``, so it aborted the whole strategy
    sequence, not just one leg. (The fail-closed behavior itself is already covered
    by tests/unit/test_sql_firewall.py::test_no_roles_prod_fails_closed.)
    """

    async def test_profile_reaches_strategy_context(self):
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        profile = {"userId": "u1", "groups": ["Admin"]}

        await NlToSqlTool(tier).invoke(namespace="ns", query="q", profile=profile)

        assert tier.calls[-1]["context"].profile == profile

    async def test_options_and_model_id_reach_strategy_context(self):
        """options carries the row cap; model_id the per-request model override —
        both honored on the standard Tier-2 path and previously dropped here."""
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))

        await NlToSqlTool(tier).invoke(namespace="ns", query="q", options={"maxRows": 25}, model_id="some-model")

        ctx = tier.calls[-1]["context"]
        assert ctx.options == {"maxRows": 25}
        assert ctx.model_id == "some-model"

    async def test_missing_context_degrades_to_empty_not_none(self):
        """StrategyContext requires dicts, so absent profile/options become {}."""
        tier = _FakeTier(_FakeStrategyResult([{"a": 1}]))
        await NlToSqlTool(tier).invoke(namespace="ns", query="q")
        ctx = tier.calls[-1]["context"]
        assert ctx.profile == {}
        assert ctx.options == {}
        assert ctx.model_id is None

    def test_request_context_is_not_in_the_input_schema(self):
        """It must arrive OUT-OF-BAND: identity is not the LLM's to supply, and
        validate_inputs rejects any key outside input_schema — so a planner-supplied
        profile would be refused as an input_contract_violation."""
        from coa_serve.tier3.agentic.controller import validate_inputs

        tool = NlToSqlTool(_FakeTier(None))
        assert "profile" not in tool.input_schema
        assert validate_inputs({"profile": {"userId": "u"}}, tool.input_schema) == ["unknown field 'profile'"]


@pytest.mark.unit
class TestAuthorizationDenialsPropagate:
    """An authorization denial is NOT a retrieval failure.

    AccessDeniedError is the firewall/Cedar 403 (exceptions.py:33) and main.py maps
    it to a 403 response. Swallowing it into an empty ToolResult would turn a 403
    into a 200 with "0 rows" — hiding a misconfigured grant behind a plausible
    "no data found". The standard Tier-2 path raises it; the agentic path must too.
    """

    async def test_access_denied_is_reraised_not_swallowed(self):
        from coa_serve.exceptions import AccessDeniedError

        tier = _FakeTier(AccessDeniedError("no authorization roles resolved"))
        with pytest.raises(AccessDeniedError):
            await NlToSqlTool(tier).invoke(namespace="ns", query="q")

    async def test_ordinary_failure_is_still_degraded(self):
        """Only auth denials propagate — a real retrieval failure must still map to
        an empty result so the loop can escalate (Req 9.1-9.2)."""
        result = await NlToSqlTool(_FakeTier(RuntimeError("db down"))).invoke(namespace="ns", query="q")
        assert result.rows == ()
        assert "RuntimeError" in result.detail

    def test_planner_cannot_supply_a_profile(self):
        """The injection defense: identity must come from the validated request, never
        from LLM-generated tool inputs. validate_inputs rejects any key outside the
        tool's input_schema, and profile is deliberately not in it."""
        from coa_serve.tier3.agentic.controller import validate_inputs

        tool = NlToSqlTool(_FakeTier(None))
        for injected in ("profile", "options", "model_id"):
            assert injected not in tool.input_schema
            assert validate_inputs({injected: {"userId": "attacker"}}, tool.input_schema) == [
                f"unknown field {injected!r}"
            ]
