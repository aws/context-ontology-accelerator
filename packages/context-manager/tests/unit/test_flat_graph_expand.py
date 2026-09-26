# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ontology-graph expansion on the FLAT (non-agentic) NL→SQL path.

The flat arm retrieves **once** — its 2-shot ``correct()`` re-generates on an
execution error or an empty result, but never re-retrieves — so a table vector
similarity did not rank is unreachable, and there is no tool call to recover it.
This lever walks the induced FK graph one hop out from the retrieved classes and
appends the reached tables' columns to the schema context.

What these tests pin down is the difference between this and the FK expansion the
generator already had, which measured nothing: that one adds table *names* from a
sources-API adjacency that production never populates, and ``_build_raw_context``
renders only tables that have a retrieved hit, so a name with no hit was dropped
before it reached the prompt. So the assertions here are about the *prompt*: the
walked table's columns are in the text handed to the model, the retrieved text is
never displaced by the thinner walked one, and the whole thing is byte-identical
to baseline when the flag is off.

The walk itself is ``OntologyGraphTool.expand_from`` — the same FK adjacency the
agentic arm's ``explore_graph`` tool walks (``test_graph_tools.py``), entered from
the retrieved classes instead of from a table name an agent chose.
"""

from __future__ import annotations

import os
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from coa_serve.clients.base import ConverseResult, VectorHit
from coa_serve.tier2.nl_to_sql.sql_generator import (
    DEFAULT_RETRIEVAL_K,
    SQLGenerator,
    _graph_context_block,
    _resolve_graph_expand_max_tables,
    hit_class_iri,
)
from coa_serve.tier2.nl_to_sql.strategy import NLtoSQLStrategy, _graph_expand_enabled
from coa_serve.tier2.tools import OntologyGraphTool

NS = "11111111-1111-1111-1111-111111111111"
TEMPLATE = "https://ontology-workbench.local/{namespace}"
BASE = f"https://demo.coa/{NS}/ontology#"
GRAPH_IRI = f"https://ontology-workbench.local/{NS}/https%3A%2F%2Fdemo.coa%2Fontology"


def _iri(name: str) -> str:
    return BASE + name


# Two databases, and a same-label collision on purpose: BOTH have a "customers"
# table, so a walk seeded by label rather than by IRI would bring back the wrong
# one. shop: orders -> customers, line_items -> orders. hr: staff -> customers.
_FK_ROWS = [
    {
        "op": _iri("shop_orders_customerid"),
        "domain": _iri("Shop_Orders"),
        "domainLabel": "orders",
        "range": _iri("Shop_Customers"),
        "rangeLabel": "customers",
        "comment": "Foreign key: orders.customerid references customers.id",
    },
    {
        "op": _iri("shop_line_items_orderid"),
        "domain": _iri("Shop_Line_Items"),
        "domainLabel": "line_items",
        "range": _iri("Shop_Orders"),
        "rangeLabel": "orders",
        "comment": "Foreign key: line_items.orderid references orders.id",
    },
    {
        "op": _iri("hr_staff_customerid"),
        "domain": _iri("Hr_Staff"),
        "domainLabel": "staff",
        "range": _iri("Hr_Customers"),
        "rangeLabel": "customers",
        "comment": "Foreign key: staff.customerid references customers.id",
    },
]

_COL_ROWS = {
    _iri("Shop_Customers"): [
        {"class": _iri("Shop_Customers"), "colLabel": "id", "range": "http://www.w3.org/2001/XMLSchema#integer"},
        {
            "class": _iri("Shop_Customers"),
            "colLabel": "city",
            "range": "http://www.w3.org/2001/XMLSchema#string",
            "colComment": "billing city",
        },
    ],
    _iri("Shop_Line_Items"): [
        {"class": _iri("Shop_Line_Items"), "colLabel": "qty", "range": "http://www.w3.org/2001/XMLSchema#integer"},
    ],
    # The hr homonym carries DIFFERENT columns, so a label-keyed lookup is visible
    # in the assertions rather than merely possible.
    _iri("Hr_Customers"): [
        {"class": _iri("Hr_Customers"), "colLabel": "badge_no", "range": "http://www.w3.org/2001/XMLSchema#string"},
    ],
}


class _MockGraph:
    """Dispatches on SPARQL shape, mirroring the tool's queries."""

    def __init__(self, fk_rows=None, col_rows=None):
        self._fk = _FK_ROWS if fk_rows is None else fk_rows
        self._cols = _COL_ROWS if col_rows is None else col_rows

    async def query(self, sparql, *, max_results=10000):
        if "?ont a owl:Ontology" in sparql:
            return [{"g": GRAPH_IRI}]
        if "?op a owl:ObjectProperty" in sparql:
            return self._fk
        if "?col a owl:DatatypeProperty" in sparql:
            body = sparql.split("VALUES ?class {", 1)[1].split("}", 1)[0]
            wanted = set(re.findall(r"<([^>]+)>", body))
            return [row for cls, rows in self._cols.items() if cls in wanted for row in rows]
        return []

    async def ask(self, sparql):  # pragma: no cover - protocol completeness
        return False

    async def health_check(self):  # pragma: no cover - protocol completeness
        return {"status": "ok"}


def _expander(graph=None, template=TEMPLATE) -> OntologyGraphTool:
    return OntologyGraphTool(graph or _MockGraph(), namespace=NS, graph_uri_template=template)


# ── expand_from ───────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
class TestExpandFrom:
    async def test_returns_neighbors_with_schemas_and_excludes_the_seed(self):
        out = await _expander().expand_from([_iri("Shop_Orders")], hops=1)
        by_table = {t["table"]: t for t in out}
        assert set(by_table) == {"customers", "line_items"}  # the seed itself is not re-listed
        # A neighbour is only usable if its COLUMNS came back with it.
        assert "city" in by_table["customers"]["schema"]
        assert by_table["customers"]["iri"] == _iri("Shop_Customers")
        assert by_table["customers"]["hop"] == 1
        assert "orders.customerid references customers.id" in by_table["customers"]["via"]

    async def test_seeding_by_iri_does_not_cross_the_homonym(self):
        """The hr "customers" is a different class with different columns."""
        out = await _expander().expand_from([_iri("Shop_Orders")], hops=1)
        customers = next(t for t in out if t["table"] == "customers")
        assert customers["iri"] == _iri("Shop_Customers")
        assert "badge_no" not in customers["schema"]
        # And nothing from the other database is reachable at all.
        assert not [t for t in out if t["table"] == "staff"]

    async def test_ranks_by_hop_then_label(self):
        # Both neighbours are one hop out, so the tie breaks on label — a total
        # order, which is what makes a max_tables cut reproducible across runs.
        out = await _expander().expand_from([_iri("Shop_Orders")], hops=1)
        assert [t["table"] for t in out] == ["customers", "line_items"]

    async def test_max_tables_truncates_after_ranking(self):
        out = await _expander().expand_from([_iri("Shop_Orders")], hops=1, max_tables=1)
        assert [t["table"] for t in out] == ["customers"]

    async def test_degenerate_inputs_return_empty(self):
        exp = _expander()
        assert await exp.expand_from([]) == []
        assert await exp.expand_from(["not-an-iri"]) == []  # rejected by the IRI gate
        assert await exp.expand_from([_iri("Shop_Orders")], max_tables=0) == []
        # A seed absent from the graph reaches nothing (rather than falling back to
        # its label, which is the whole point of passing an IRI).
        assert await exp.expand_from([_iri("Nope_Missing")]) == []

    async def test_no_graph_template_disables_expansion(self):
        assert await _expander(template="").expand_from([_iri("Shop_Orders")]) == []


# ── the prompt block ─────────────────────────────────────────────────────────


@pytest.mark.unit
class TestGraphContextBlock:
    def test_drops_tables_the_retrieved_block_already_carries(self):
        walked = [
            {"table": "customers", "schema": "Table: customers | Columns: id:integer", "via": "fk"},
            {"table": "line_items", "schema": "Table: line_items | Columns: qty:integer", "via": "fk"},
        ]
        block, added = _graph_context_block(["orders", "CUSTOMERS"], walked)
        assert added == ["line_items"]
        assert "line_items" in block
        # Not re-listed under its thinner walked schema.
        assert block.count("Table: customers") == 0

    def test_labels_provenance_and_the_join_key(self):
        block, added = _graph_context_block(
            ["orders"],
            [{"table": "customers", "schema": "Table: customers | Columns: id:integer", "via": "orders.cid = cust.id"}],
        )
        assert added == ["customers"]
        assert "NOT matched to the question by similarity" in block
        assert "reached via: orders.cid = cust.id" in block

    def test_nothing_to_add_is_byte_identical_to_baseline(self):
        assert _graph_context_block(["orders"], []) == ("", [])
        # A walk row with no schema text is not worth a bare name in the prompt.
        assert _graph_context_block(["orders"], [{"table": "x", "schema": ""}]) == ("", [])


@pytest.mark.unit
def test_hit_class_iri_reads_either_metadata_key():
    assert hit_class_iri(VectorHit(id="1", text="t", score=1.0, metadata={"uri": "u1"})) == "u1"
    assert hit_class_iri(VectorHit(id="1", text="t", score=1.0, metadata={"entity_uri": "u2"})) == "u2"
    assert hit_class_iri(VectorHit(id="1", text="t", score=1.0, metadata={})) == ""


# ── generate() ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.embed.return_value = [0.1] * 1024
    llm.converse.return_value = ConverseResult(text="```sql\nSELECT 1\n```\nConfidence: 0.9")
    return llm


@pytest.fixture
def mock_vector():
    vector = AsyncMock()
    vector.count_documents.return_value = 1
    vector.search.return_value = [
        VectorHit(
            id="1",
            text="Table: orders | Description: Customer orders | Columns: id (int), customerid (int)",
            score=0.95,
            metadata={"entity_type": "class", "data_source_id": "ds-shop", "entity_uri": _iri("Shop_Orders")},
        )
    ]
    return vector


@pytest.mark.unit
@pytest.mark.asyncio
class TestGenerateWithExpansion:
    async def _generate(self, mock_llm, mock_vector, **kwargs):
        gen = SQLGenerator(llm_client=mock_llm, vector_client=mock_vector)
        result = await gen.generate("which city do our customers bill to?", namespace=NS, **kwargs)
        prompt = mock_llm.converse.await_args.args[0]
        return result, prompt

    async def test_walked_table_reaches_the_writers_prompt(self, mock_llm, mock_vector):
        result, prompt = await self._generate(mock_llm, mock_vector, graph_expander=_expander())
        # The whole point: a table similarity did not return is in the prompt WITH
        # its columns, so the writer can actually join it.
        assert "Table: customers" in prompt
        assert "city:string" in prompt
        # And it is reported as retrieved, so the retrieval metrics see it.
        assert "customers" in result.expanded_tables
        assert result.ddl_context.startswith("Table: orders")

    async def test_retrieved_text_is_not_displaced_by_the_thinner_walked_one(self, mock_llm, mock_vector):
        """orders is both retrieved and reachable; its retrieved description wins."""
        _, prompt = await self._generate(mock_llm, mock_vector, graph_expander=_expander())
        assert prompt.count("Table: orders") == 1
        assert "Description: Customer orders" in prompt

    async def test_off_by_default(self, mock_llm, mock_vector):
        result, prompt = await self._generate(mock_llm, mock_vector)
        assert "customers" not in result.expanded_tables
        assert "NOT matched to the question by similarity" not in prompt

    async def test_a_failing_walk_degrades_to_retrieval_only(self, mock_llm, mock_vector):
        expander = MagicMock()
        expander.expand_from = AsyncMock(side_effect=RuntimeError("neptune refused"))
        with structlog.testing.capture_logs() as logs:
            result, prompt = await self._generate(mock_llm, mock_vector, graph_expander=expander)
        assert result.sql == "SELECT 1"  # the question is still answered
        assert "Table: orders" in prompt
        assert [s for s in result.trace_steps if s["step"] == "graph_expand_ontology"][0]["status"] == "error"
        # A silent degradation on a default-on lever is indistinguishable from a
        # namespace with no FK edges, so the failure has to name itself — and name
        # the exception, since "neptune refused" and a bad seed IRI are different
        # problems.
        entry = next(log for log in logs if log["event"] == "nl_to_sql_graph_expand_failed")
        assert "RuntimeError" in entry["error"] and "neptune refused" in entry["error"]

    async def test_the_context_log_counts_the_walked_table(self, mock_llm, mock_vector):
        # `context_tables` is documented as what the writer saw, and a walked table
        # has no retrieved hit behind it — so the hit-keyed filter drops it unless the
        # walk's own additions are added back. Getting this wrong understates the
        # prompt, which is the exact misreading the field was introduced to end.
        with structlog.testing.capture_logs() as logs:
            await self._generate(mock_llm, mock_vector, graph_expander=_expander())

        entry = next(log for log in logs if log["event"] == "nl_to_sql_context")
        assert "customers" in entry["context_tables"]
        assert entry["context_tables"].count("customers") == 1
        assert entry["n_context_tables"] == len(entry["context_tables"])

    async def test_the_context_log_is_unchanged_when_the_walk_is_off(self, mock_llm, mock_vector):
        with structlog.testing.capture_logs() as logs:
            await self._generate(mock_llm, mock_vector)

        entry = next(log for log in logs if log["event"] == "nl_to_sql_context")
        assert "customers" not in entry["context_tables"]

    async def test_hits_without_a_class_iri_seed_nothing(self, mock_llm, mock_vector):
        """An index predating IRI stamping cannot apply the treatment — and says so."""
        mock_vector.search.return_value = [
            VectorHit(id="1", text="Table: orders | Columns: id (int)", score=0.9, metadata={"data_source_id": "ds"})
        ]
        result, prompt = await self._generate(mock_llm, mock_vector, graph_expander=_expander())
        assert "Table: customers" not in prompt
        step = [s for s in result.trace_steps if s["step"] == "graph_expand_ontology"][0]
        assert step["seeds"] == 0 and step["added"] == []


# ── the flag and its wiring ──────────────────────────────────────────────────


@pytest.mark.unit
class TestGraphExpandFlag:
    def test_per_request_option(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _graph_expand_enabled({"flatGraphExpand": True}) is True
            assert _graph_expand_enabled({"flatGraphExpand": "on"}) is True
            assert _graph_expand_enabled({"flatGraphExpand": "false"}) is False
            assert _graph_expand_enabled({"flatGraphExpand": False}) is False

    def test_on_when_nothing_says_otherwise(self):
        """The default. A control arm must now opt OUT, not merely omit the option."""
        with patch.dict(os.environ, {}, clear=True):
            assert _graph_expand_enabled({}) is True
            assert _graph_expand_enabled(None) is True

    def test_deployment_wide_env(self):
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND": "true"}):
            assert _graph_expand_enabled({}) is True
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND": "no"}):
            assert _graph_expand_enabled({}) is False

    def test_a_request_overrides_the_deployment_in_both_directions(self):
        """Both directions, because paired A/B runs need each arm pinned per request."""
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND": "false"}):
            assert _graph_expand_enabled({"flatGraphExpand": True}) is True
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND": "true"}):
            assert _graph_expand_enabled({"flatGraphExpand": False}) is False

    def test_an_unrecognised_value_defers_instead_of_disabling(self):
        # A typo must not silently mean "off" at either layer — it falls through to
        # the next one, and only an explicit falsy value turns the walk off.
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND": "maybe"}):
            assert _graph_expand_enabled({}) is True
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND": "false"}):
            assert _graph_expand_enabled({"flatGraphExpand": "maybe"}) is False


# ── the appended-table budget ─────────────────────────────────────────────────


@pytest.mark.unit
class TestGraphExpandMaxTables:
    """``SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES`` — how many walked tables the prompt takes.

    The cap is a context budget rather than a tuned constant, so what matters is
    that an operator can move it on a running deployment and that a bad value costs
    the override, not the query.
    """

    def test_the_default_is_the_value_the_walk_was_measured_at(self):
        # 8 is what every benchmark cell behind default-on ran at, and it is also
        # about the retrieved block's own table count, so the appended block
        # supplements the retrieved one instead of drowning it.
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_graph_expand_max_tables() == 8
        assert DEFAULT_RETRIEVAL_K == 7

    def test_env_override(self):
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": "40"}):
            assert _resolve_graph_expand_max_tables() == 40

    def test_zero_appends_nothing_and_is_not_the_default(self):
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": "0"}):
            assert _resolve_graph_expand_max_tables() == 0

    def test_a_bad_value_costs_the_override_not_the_query(self):
        # Anything unparseable degrades to the default; a negative clamps to the
        # floor. Neither raises out of a request path.
        for bad, expected in (("", 8), ("eight", 8), ("8.5", 8), ("-3", 0)):
            with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": bad}):
                assert _resolve_graph_expand_max_tables() == expected, bad

    def test_an_ignored_override_says_so(self):
        # Falling back to the default is the safe behaviour, but doing it quietly
        # means an operator who mistyped the cap reads the default's results as
        # their own setting's. Both events carry the value that was rejected.
        with (
            patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": "eight"}),
            structlog.testing.capture_logs() as logs,
        ):
            _resolve_graph_expand_max_tables()
        invalid = next(log for log in logs if log["event"] == "nl_to_sql_graph_expand_max_tables_invalid")
        assert invalid["value"] == "eight" and invalid["using"] == 8

        with (
            patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": "-3"}),
            structlog.testing.capture_logs() as logs,
        ):
            _resolve_graph_expand_max_tables()
        clamped = next(log for log in logs if log["event"] == "nl_to_sql_graph_expand_max_tables_clamped")
        assert clamped["requested"] == -3 and clamped["using"] == 0

    def test_the_upper_bound_belongs_to_the_graph_tool(self):
        # Deliberately NOT clamped here: duplicating MAX_NODE_LIMIT would put the
        # tool's ceiling in two places, and expand_from already enforces it.
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": "9999"}):
            assert _resolve_graph_expand_max_tables() == 9999


@pytest.mark.unit
@pytest.mark.asyncio
class TestMaxTablesReachesTheWalk:
    async def test_generate_passes_the_resolved_cap(self, mock_llm, mock_vector):
        expander = MagicMock()
        expander.expand_from = AsyncMock(return_value=[])
        gen = SQLGenerator(llm_client=mock_llm, vector_client=mock_vector)
        with patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": "3"}):
            await gen.generate("q", namespace=NS, graph_expander=expander)
        assert expander.expand_from.await_args.kwargs["max_tables"] == 3

    async def test_the_log_says_whether_the_budget_bound(self, mock_llm, mock_vector):
        """`capped` separates "the FK frontier ended" from "the budget cut it"."""
        with (
            patch.dict(os.environ, {"SERVE_NL2SQL_GRAPH_EXPAND_MAX_TABLES": "1"}),
            structlog.testing.capture_logs() as logs,
        ):
            gen = SQLGenerator(llm_client=mock_llm, vector_client=mock_vector)
            await gen.generate("q", namespace=NS, graph_expander=_expander())

        entry = next(log for log in logs if log["event"] == "nl_to_sql_graph_expand")
        assert entry["max_tables"] == 1
        assert entry["added"] == 1
        assert entry["capped"] is True


@pytest.mark.unit
@pytest.mark.asyncio
class TestStrategyPassesExpander:
    async def _resolve(self, options, *, graph_client):
        from coa_serve.tier2.strategy import StrategyContext
        from coa_serve.trace import TraceCollector

        generator = AsyncMock()
        generator.generate.return_value = MagicMock(sql="", error="empty_sql")
        strategy = NLtoSQLStrategy(
            sql_generator=generator,
            firewall=MagicMock(),
            query_executor=None,
            graph_client=graph_client,
        )
        await strategy.resolve(
            "q",
            NS,
            StrategyContext(embedding=[0.1], profile={}, options=options, trace=TraceCollector()),
        )
        return generator.generate.await_args.kwargs["graph_expander"]

    async def test_the_expander_reaches_the_generator_by_default(self):
        for options in ({}, {"flatGraphExpand": True}):
            expander = await self._resolve(options, graph_client=_MockGraph())
            assert isinstance(expander, OntologyGraphTool), options

    async def test_opting_out_withholds_the_expander(self):
        assert await self._resolve({"flatGraphExpand": False}, graph_client=_MockGraph()) is None

    async def test_the_expander_is_scoped_to_the_requests_namespace(self):
        # The tool binds its namespace at construction, so a request-scoped instance
        # is what keeps one request's walk out of another namespace's graph.
        expander = await self._resolve({"flatGraphExpand": True}, graph_client=_MockGraph())
        assert expander._namespace == NS

    async def test_no_graph_client_means_no_expander(self):
        """An unwired deployment degrades to the baseline instead of erroring."""
        assert await self._resolve({"flatGraphExpand": True}, graph_client=None) is None
