# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Tier-2 tool layer (TableCatalog, SqlAuthoringTool), without an agent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.clients.base import VectorHit
from coa_serve.tier2.tools import MAX_TOP_K, SqlAuthoringTool, TableCatalog, UnknownTablesError


def _hit(table: str, ds: str = "ds-1", context_text: str | None = None):
    metadata = {"entity_type": "class", "data_source_id": ds}
    if context_text is not None:
        metadata["context_text"] = context_text
    return VectorHit(
        id=table,
        text=f"Table: {table} | Columns: id:int (pk), name:text (n)",
        score=0.9,
        metadata=metadata,
    )


def _llm():
    llm = MagicMock()
    llm.embed = AsyncMock(return_value=[0.1] * 8)
    return llm


def _catalog(hits, llm=None, namespace="ns1", index="idx"):
    vector = MagicMock()
    vector.search = AsyncMock(return_value=hits)
    return TableCatalog(vector, llm or _llm(), namespace=namespace, index=index), vector


@pytest.mark.unit
class TestTableCatalogSearch:
    async def test_search_returns_only_mapped_tables(self):
        # An unmapped class has no backing table, so authoring SQL against it would
        # target something that does not exist — it must never be offered.
        catalog, _ = _catalog([_hit("orders"), _hit("unmapped", ds=""), _hit("customers")])

        candidates = await catalog.search_tables("orders")

        assert [c.table for c in candidates] == ["orders", "customers"]
        assert candidates[0].preview.startswith("Table: orders")

    async def test_search_scopes_to_namespace_and_index_and_overfetches_pool(self):
        catalog, vector = _catalog([_hit("orders")])

        await catalog.search_tables("orders", top_k=3)

        kwargs = vector.search.await_args.kwargs
        assert kwargs["namespace"] == "ns1" and kwargs["index"] == "idx"
        assert kwargs["entity_type"] == "class"
        # Over-fetch before the mapped filter, so a namespace whose top hits are
        # unmapped still yields top_k usable tables.
        assert kwargs["top_k"] >= 40

    async def test_top_k_is_clamped_to_the_cap(self):
        catalog, _ = _catalog([_hit(f"t{i}") for i in range(30)])

        assert len(await catalog.search_tables("anything", top_k=99)) == MAX_TOP_K
        assert len(await catalog.search_tables("anything", top_k=0)) == 1

    async def test_search_uses_the_embedded_query(self):
        llm = _llm()
        catalog, vector = _catalog([_hit("orders")], llm=llm)

        await catalog.search_tables("how many orders")

        llm.embed.assert_awaited_once_with("how many orders")
        assert vector.search.await_args.args[0] == [0.1] * 8


@pytest.mark.unit
class TestTableCatalogSchema:
    async def test_schema_prefers_context_text_over_embedded_text(self):
        # context_text carries per-column allowed values; it is what the writer needs.
        rich = "Table: orders | Columns: status:text (allowed: open, closed)"
        catalog, _ = _catalog([_hit("orders", context_text=rich)])
        await catalog.search_tables("orders")

        assert await catalog.get_table_schema("ORDERS ") == rich

    async def test_schema_falls_back_to_targeted_search_for_uncached_table(self):
        # A caller can learn a table name from another table's FK comment; it must be
        # inspectable without re-running search_tables first.
        catalog, vector = _catalog([_hit("line_items")])

        assert "line_items" in await catalog.get_table_schema("line_items")
        assert vector.search.await_count == 1
        # And the targeted hit is now cached for generation.
        assert catalog.known_tables == ["line_items"]

    async def test_schema_returns_empty_for_unknown_table(self):
        catalog, _ = _catalog([])

        assert await catalog.get_table_schema("nope") == ""

    async def test_schema_returns_empty_when_retrieval_fails(self):
        # A backend failure on the lookup path is reported as "unknown table"; the
        # caller already handles that, and it must not abort the run.
        vector = MagicMock()
        vector.search = AsyncMock(side_effect=RuntimeError("opensearch down"))
        catalog = TableCatalog(vector, _llm(), namespace="ns1")

        assert await catalog.get_table_schema("orders") == ""

    async def test_search_failure_propagates(self):
        # search_tables RAISES (unlike the lookup path): a consumer that must not
        # raise translates it into its own protocol.
        vector = MagicMock()
        vector.search = AsyncMock(side_effect=RuntimeError("opensearch down"))
        catalog = TableCatalog(vector, _llm(), namespace="ns1")

        with pytest.raises(RuntimeError):
            await catalog.search_tables("orders")


@pytest.mark.unit
class TestTableCatalogRouting:
    async def test_hits_for_tables_splits_known_from_unknown(self):
        catalog, _ = _catalog([_hit("orders")])
        await catalog.search_tables("orders")

        hits, missing = catalog.hits_for_tables(["Orders", "ghost"])

        assert len(hits) == 1 and missing == ["ghost"]

    async def test_resolve_data_source_prefers_tables_in_the_sql(self):
        # Retrieval deliberately mixes sources; attributing by the tables the
        # statement references is precise where pool agreement is ambiguous.
        catalog, _ = _catalog([_hit("orders", ds="ds-pg"), _hit("events", ds="ds-glue")])
        await catalog.search_tables("orders")

        assert catalog.resolve_data_source("SELECT count(*) FROM orders") == "ds-pg"
        # Without a statement the mixed pool cannot be pinned — "" beats a guess,
        # since a wrong source mis-routes the query.
        assert catalog.resolve_data_source() == ""

    async def test_resolve_data_source_falls_back_to_pool_agreement(self):
        catalog, _ = _catalog([_hit("orders"), _hit("customers")])
        await catalog.search_tables("orders")

        # SQL references a table outside the pool → fall back to unanimous agreement.
        assert catalog.resolve_data_source("SELECT 1 FROM other_thing") == "ds-1"

    async def test_known_tables_is_sorted_and_deduped(self):
        catalog, _ = _catalog([_hit("orders"), _hit("customers"), _hit("orders")])
        await catalog.search_tables("orders")

        assert catalog.known_tables == ["customers", "orders"]


@pytest.mark.unit
class TestSqlAuthoringTool:
    def _tool(self, catalog, gen_sql="SELECT count(*) FROM orders", **kwargs):
        generator = MagicMock()
        generator.generate_from_context = AsyncMock(return_value=(gen_sql, 0.75))
        return SqlAuthoringTool(generator, catalog, **kwargs), generator

    async def test_generates_from_the_chosen_tables_schema(self):
        rich = "Table: orders | Columns: status:text (allowed: open, closed)"
        catalog, _ = _catalog([_hit("orders", context_text=rich)])
        await catalog.search_tables("orders")
        tool, generator = self._tool(catalog)

        out = await tool.generate_sql("how many orders", ["orders"], evidence="hint")

        assert out.sql == "SELECT count(*) FROM orders"
        assert out.confidence == 0.75 and out.tables_used == ["orders"] and out.missing_tables == []
        # The writer sees the rich context, the question and the evidence — and no
        # dialect override, so the generator's configured prompt is used verbatim.
        args, kwargs = generator.generate_from_context.await_args
        assert args[0] == "how many orders" and rich in args[1]
        assert kwargs["evidence"] == "hint" and kwargs["dialect"] is None and kwargs["feedback"] == ""

    async def test_unknown_tables_raise_ordering_error(self):
        # Generating before discovering is a caller ordering error, not a backend
        # failure — it is signalled distinctly so the caller can go search first.
        catalog, _ = _catalog([])
        tool, generator = self._tool(catalog)

        with pytest.raises(UnknownTablesError) as excinfo:
            await tool.generate_sql("q", ["ghost"])

        assert excinfo.value.table_names == ["ghost"]
        generator.generate_from_context.assert_not_awaited()

    async def test_partially_known_tables_generate_and_report_the_gap(self):
        catalog, _ = _catalog([_hit("orders")])
        await catalog.search_tables("orders")
        tool, _ = self._tool(catalog)

        out = await tool.generate_sql("q", ["orders", "ghost"])

        assert out.tables_used == ["orders"] and out.missing_tables == ["ghost"]

    async def test_feedback_and_model_override_reach_the_writer(self):
        # feedback is the self-correction channel: the writer is a stateless temp-0
        # function of (question, schema), so without it a retry returns identical SQL.
        catalog, _ = _catalog([_hit("orders")])
        await catalog.search_tables("orders")
        tool, generator = self._tool(catalog, model_id="model-x", dialect="postgres")

        await tool.generate_sql("q", ["orders"], feedback="prior attempt failed")

        kwargs = generator.generate_from_context.await_args.kwargs
        assert kwargs["feedback"] == "prior attempt failed"
        assert kwargs["model_id"] == "model-x" and kwargs["dialect"] == "postgres"
