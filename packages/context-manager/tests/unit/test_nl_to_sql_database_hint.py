# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The NL-to-SQL database hint must spell each table the way Athena accepts it.

In a namespace pooling several Glue databases each retrieved table is shown to
the SQL writer as ``Table: t | Database: db (write it as db.t)``, and the writer
copies the ``write it as`` form into the SQL. Written unquoted, a table whose
name is a reserved word (``group``) or a database with a hyphen is invalid SQL,
so the first Athena shot failed and only the correction shot answered. These
tests pin the quoted hint and that the routing and cross-source qualification
downstream still recognise the quoted references the writer then produces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import sqlglot
from coa_serve.clients.base import ConverseResult, VectorHit
from coa_serve.tier2.nl_to_sql.sql_generator import (
    SQLGenerator,
    _build_raw_context,
    _graph_context_block,
    _hint_identifier,
    _with_database,
    sql_table_routing,
)
from coa_serve.tier2.sql_firewall import SQLFirewall
from coa_serve.tier2.table_qualifier import prepare_execution_sql, real_tables


@pytest.mark.unit
class TestHintIdentifier:
    @pytest.mark.parametrize("name", ["orders", "sales_db", "Orders", "t1", "groups", "group_name"])
    def test_plain_name_stays_bare(self, name):
        assert _hint_identifier(name) == name

    @pytest.mark.parametrize("name", ["group", "GROUP", "order", "table", "values", "unnest", "current_date"])
    def test_reserved_word_is_quoted(self, name):
        assert _hint_identifier(name) == f'"{name}"'

    # Spelled out here, not read from the module, so trimming the hand-added set (or a
    # sqlglot upgrade that changes its own set) fails a test instead of silently leaving
    # one of these bare again.
    @pytest.mark.parametrize(
        "name",
        [
            "auto", "cube", "current_catalog", "current_date", "current_path", "current_role", "current_schema",
            "current_user", "grouping", "json_array", "json_exists", "json_object", "json_query", "json_serialize",
            "json_table", "json_value", "listagg", "localtime", "localtimestamp", "normalize", "overlaps",
            "recursive", "rollup", "skip", "trim", "uescape", "unnest",
        ],
    )  # fmt: skip
    def test_trino_reserved_word_missing_from_sqlglot_is_quoted(self, name):
        assert _hint_identifier(name) == f'"{name}"'

    @pytest.mark.parametrize("name", ["sales-ops", "1st_quarter", "a b"])
    def test_name_that_cannot_be_written_bare_is_quoted(self, name):
        assert _hint_identifier(name) == f'"{name}"'

    def test_leading_underscore_is_quoted(self):
        # Trino would accept ``_staging`` bare; quoting it anyway is harmless.
        assert _hint_identifier("_staging") == '"_staging"'

    def test_embedded_quote_is_doubled(self):
        assert _hint_identifier('we"ird') == '"we""ird"'


@pytest.mark.unit
class TestWithDatabase:
    def test_plain_names_unchanged(self):
        out = _with_database("Table: orders | Columns: id (int)", "orders", "sales_db")
        assert out == "Table: orders | Database: sales_db (write it as sales_db.orders) | Columns: id (int)"

    def test_reserved_word_table_is_quoted(self):
        out = _with_database("Table: group | Columns: group_name (string)", "group", "crm")
        assert out == 'Table: group | Database: crm (write it as crm."group") | Columns: group_name (string)'

    def test_hyphenated_database_is_quoted(self):
        out = _with_database("Table: warehouse_stock | Columns: sku (string)", "warehouse_stock", "sales-ops")
        assert out == (
            'Table: warehouse_stock | Database: sales-ops (write it as "sales-ops".warehouse_stock)'
            " | Columns: sku (string)"
        )

    def test_both_parts_quoted(self):
        out = _with_database("Table: group", "group", "crm-ops")
        assert out == 'Table: group | Database: crm-ops (write it as "crm-ops"."group")'

    def test_shared_list_spells_each_database_as_written(self):
        # The writer copies a listed database into its SQL, so a name Athena only
        # accepts quoted is listed quoted, as the single-database hint does.
        out = _with_database("Table: ds", "ds", "", ["sales-ops", "study_1"])
        assert out == (
            'Table: ds | Databases: "sales-ops", study_1 (several databases hold this table: '
            "qualify it with the database the question names, or read each of them)"
        )

    def test_shared_list_of_plain_names_is_unchanged(self):
        out = _with_database("Table: ds", "ds", "", ["sdtm_cs101", "sdtm_cs102"])
        assert "Databases: sdtm_cs101, sdtm_cs102 (" in out

    def test_mixed_case_reserved_word_keeps_its_case_inside_the_quotes(self):
        out = _with_database("Table: Group", "Group", "crm")
        assert out == 'Table: Group | Database: crm (write it as crm."Group")'

    def test_written_form_is_valid_athena_sql(self):
        # The hint is copied into FROM: it must parse to one table with that db and name.
        for table, database in (("group", "crm"), ("warehouse_stock", "sales-ops"), ("order", "a-b")):
            written = _with_database("Table: x", table, database).split("write it as ", 1)[1].rstrip(")")
            parsed = sqlglot.parse_one(f"SELECT 1 FROM {written}", read="trino")
            (ref,) = real_tables(parsed)
            assert (ref.db, ref.name) == (database, table)


@pytest.mark.unit
class TestPromptContext:
    def test_build_raw_context_quotes_reserved_word_table(self):
        hits = [VectorHit(id="1", text="Table: group | Columns: member_count (int)", score=1.0, metadata={})]
        assert (
            _build_raw_context(hits, ["group"], {"group": "crm"})
            == 'Table: group | Database: crm (write it as crm."group") | Columns: member_count (int)'
        )

    def test_graph_walk_table_gets_the_quoted_hint(self):
        # A walk-discovered table is hinted through the same _with_database as a retrieved one.
        graph_tables = [{"table": "group", "schema": "Table: group | Columns: member_count (int)", "via": "fk"}]
        block, added = _graph_context_block(["orders"], graph_tables, {"group": "sales-ops"})
        assert added == ["group"]
        assert (
            'Table: group | Database: sales-ops (write it as "sales-ops"."group") | Columns: member_count (int)'
            in block
        )

    @pytest.mark.asyncio
    async def test_generate_shows_quoted_hint_to_the_writer(self):
        llm = AsyncMock()
        llm.embed.return_value = [0.1] * 1024
        llm.converse.return_value = ConverseResult(
            text='```sql\nSELECT group_name FROM crm."group"\n```\nConfidence: 0.9'
        )
        vector = AsyncMock()
        vector.count_documents.return_value = 2
        vector.search.return_value = [
            VectorHit(
                id="1",
                text="Table: group | Columns: group_name (string), member_count (int)",
                score=0.9,
                metadata={"data_source_id": "s-crm"},
            ),
            VectorHit(
                id="2",
                text="Table: warehouse_stock | Columns: sku (string)",
                score=0.8,
                metadata={"data_source_id": "s-ops"},
            ),
        ]

        async def resolver(_):
            return {"group": ("s-crm", "crm"), "warehouse_stock": ("s-ops", "sales-ops")}

        result = await SQLGenerator(llm_client=llm, vector_client=vector, fk_adjacency={}).generate(
            "Which group has the most members?", namespace="demo", table_placements=resolver
        )

        prompt = llm.converse.call_args.args[0]
        assert '(write it as crm."group")' in prompt
        assert '(write it as "sales-ops".warehouse_stock)' in prompt
        assert "write it as crm.group)" not in prompt
        assert result.table_sources == {"group": "s-crm", "warehouse_stock": "s-ops"}


def _registry(sources: dict[str, dict]):
    reg = AsyncMock()
    reg.get_source.side_effect = lambda namespace, data_source_id: sources.get(data_source_id, {})
    return reg


_GLUE_SOURCES = {
    "s-crm": {"glueDatabaseName": "crm"},
    "s-ops": {"glueDatabaseName": "sales-ops"},
}


@pytest.mark.unit
class TestQuotedReferencesDownstream:
    """The writer now emits ``crm."group"`` / ``"sales-ops".t``; routing must still see them."""

    @pytest.mark.parametrize(
        "sql",
        [
            'SELECT group_name FROM crm."group"',
            'SELECT group_name FROM "crm"."group"',
            'SELECT group_name FROM crm."group" g WHERE g.member_count > 1',
        ],
    )
    def test_firewall_extracts_the_bare_table_name(self, sql):
        assert [t.split(".")[-1] for t in SQLFirewall.extract_tables(sql)] == ["group"]

    def test_routing_recognises_quoted_reserved_word_table(self):
        routing = sql_table_routing('SELECT group_name FROM "crm"."group"', {"group": "s-crm"})
        assert routing == {"group": {"datasourceId": "s-crm"}}

    def test_routing_recognises_quoted_hyphenated_database(self):
        routing = sql_table_routing('SELECT sku FROM "sales-ops".warehouse_stock', {"warehouse_stock": "s-ops"})
        assert routing == {"warehouse_stock": {"datasourceId": "s-ops"}}

    @pytest.mark.asyncio
    async def test_cross_database_join_with_quoted_references_is_qualified(self):
        sql = 'SELECT g.group_name, w.sku FROM crm."group" g JOIN "sales-ops".warehouse_stock w ON g.group_name = w.sku'
        routing = sql_table_routing(sql, {"group": "s-crm", "warehouse_stock": "s-ops"})

        prepared = await prepare_execution_sql(sql, routing, _registry(_GLUE_SOURCES), namespace="ns")

        assert prepared.error is None
        refs = {(t.catalog, t.db, t.name) for t in real_tables(sqlglot.parse_one(prepared.sql, read="trino"))}
        assert refs == {("awsdatacatalog", "crm", "group"), ("awsdatacatalog", "sales-ops", "warehouse_stock")}
        # The reserved word keeps its quotes in the statement that is executed.
        assert '"group"' in prepared.sql

    @pytest.mark.asyncio
    async def test_single_database_quoted_reference_executes_as_written(self):
        sql = 'SELECT group_name FROM crm."group" ORDER BY member_count DESC LIMIT 1'
        routing = sql_table_routing(sql, {"group": "s-crm"})

        prepared = await prepare_execution_sql(sql, routing, _registry(_GLUE_SOURCES), namespace="ns")

        assert prepared.error is None
        (ref,) = real_tables(sqlglot.parse_one(prepared.sql, read="trino"))
        assert (ref.db, ref.name) == ("crm", "group")
        assert '"group"' in prepared.sql
