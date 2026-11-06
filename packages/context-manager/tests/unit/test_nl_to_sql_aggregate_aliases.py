# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import sqlglot
from coa_serve.clients.base import ConverseResult, VectorClient
from coa_serve.tier2.nl_to_sql.aggregate_aliases import normalize_aggregate_aliases
from coa_serve.tier2.nl_to_sql.sql_generator import SQLGenerator


def _generator(sql: str, dialect: str = "athena") -> SQLGenerator:
    llm = AsyncMock()
    llm.converse.return_value = ConverseResult(text=f"```sql\n{sql}\n```\nConfidence: 0.8")
    return SQLGenerator(llm_client=llm, vector_client=AsyncMock(spec=VectorClient), dialect=dialect)


@pytest.mark.unit
class TestGeneratorOutput:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("drifted", ["count_closed", "count_closures", "n_closed"])
    async def test_drifting_aliases_converge_on_one_name(self, drifted):
        sql, confidence = await _generator(
            f"SELECT status, COUNT(closed_at) AS {drifted} FROM tickets GROUP BY status ORDER BY {drifted} DESC"
        ).generate_from_context("How many closures per status?", "CREATE TABLE tickets (...)")

        assert sql == (
            "SELECT status, COUNT(closed_at) AS count_closed_at FROM tickets"
            " GROUP BY status ORDER BY count_closed_at DESC"
        )
        assert confidence == 0.8

    @pytest.mark.asyncio
    async def test_per_call_dialect_is_used(self):
        sql, _ = await _generator(
            "SELECT SUM(total) AS revenue FROM orders WHERE d >= '2024-01-01'::date"
        ).generate_from_context("Revenue?", "ddl", dialect="postgresql")

        assert sql == "SELECT SUM(total) AS sum_total FROM orders WHERE d >= CAST('2024-01-01' AS DATE)"

    @pytest.mark.asyncio
    async def test_correction_output_is_normalized(self):
        sql, _ = await _generator("SELECT AVG(price) AS mean_price FROM items").correct(
            "Average price?", "ddl", failed_sql="SELECT AVG(prce) FROM items", execution_error="no column prce"
        )

        assert sql == "SELECT AVG(price) AS avg_price FROM items"


@pytest.mark.unit
class TestNormalizeAggregateAliases:
    def test_count_star_without_alias_is_named_all(self):
        assert normalize_aggregate_aliases("SELECT COUNT(*) FROM orders", "athena") == (
            "SELECT COUNT(*) AS count_all FROM orders"
        )

    def test_order_by_alias_follows_rename(self):
        assert (
            normalize_aggregate_aliases(
                "SELECT region, SUM(Total) AS rev FROM o GROUP BY region ORDER BY rev DESC LIMIT 1", "athena"
            )
            == "SELECT region, SUM(Total) AS sum_total FROM o GROUP BY region ORDER BY sum_total DESC LIMIT 1"
        )

    def test_cte_root_select_is_renamed(self):
        assert (
            normalize_aggregate_aliases("WITH c AS (SELECT a FROM t) SELECT MAX(a) AS top FROM c", "redshift")
            == "WITH c AS (SELECT a FROM t) SELECT MAX(a) AS max_a FROM c"
        )

    def test_postgres_output_stays_postgres(self):
        out = normalize_aggregate_aliases("SELECT MIN(x) AS lo FROM t WHERE name ILIKE 'a%'", "postgresql")

        assert out == "SELECT MIN(x) AS min_x FROM t WHERE name ILIKE 'a%'"

    def test_athena_output_stays_trino(self):
        out = normalize_aggregate_aliases("SELECT APPROX_DISTINCT(user_id) AS users FROM events", "athena")

        assert out == "SELECT APPROX_DISTINCT(user_id) AS approx_distinct_user_id FROM events"

    @pytest.mark.parametrize(
        ("sql", "expected_alias"),
        [
            ("SELECT APPROX_PERCENTILE(total, 0.5) AS p50 FROM o", "approx_percentile_total"),
            ("SELECT COUNT_IF(flag = 'Y') AS flagged FROM o", "count_if_flag"),
            ("SELECT ARRAY_AGG(x) AS xs FROM o", "array_agg_x"),
        ],
    )
    def test_athena_alias_uses_sql_function_name(self, sql, expected_alias):
        out = normalize_aggregate_aliases(sql, "athena")

        assert sqlglot.parse_one(out, read="trino").expressions[0].alias == expected_alias

    def test_postgres_alias_uses_sql_function_name(self):
        out = normalize_aggregate_aliases("SELECT ARRAY_AGG(x) AS xs FROM o", "postgresql")

        assert out == "SELECT ARRAY_AGG(x) AS array_agg_x FROM o"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT COUNT(*) AS n FROM o WHERE d >= DATE '2024-01-01'",
            "SELECT COUNT(*) AS n FROM o WHERE d >= CURRENT_DATE - INTERVAL '7' DAY",
        ],
    )
    def test_athena_date_literals_stay_valid_trino(self, sql):
        out = normalize_aggregate_aliases(sql, "athena")

        assert sqlglot.parse_one(out, read="trino").expressions[0].alias == "count_all"

    def test_quoted_identifiers_with_spaces_survive(self):
        out = normalize_aggregate_aliases(
            'SELECT "Order Region", SUM("Order Total") AS rev FROM o GROUP BY "Order Region" ORDER BY rev DESC',
            "athena",
        )

        assert out == (
            'SELECT "Order Region", SUM("Order Total") AS "sum_order total" FROM o'
            ' GROUP BY "Order Region" ORDER BY "sum_order total" DESC'
        )

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT COUNT(*) FILTER (WHERE status = 'open') AS open_count FROM t",
            "SELECT region, name FROM o",
            "SELECT count(*)   AS count_all FROM orders",
            "SELECT ROUND(AVG(x), 2) AS avg_rounded FROM t",
            "SELECT SUM(x) OVER (PARTITION BY g) AS running FROM t",
        ],
    )
    def test_nothing_to_rename_returns_input_verbatim(self, sql):
        assert normalize_aggregate_aliases(sql, "athena") == sql

    @pytest.mark.parametrize(
        "sql",
        [
            "SELEC COUNT(*) FROM",
            "SELECT COUNT(*) FROM a; SELECT COUNT(*) FROM b",
            "SELECT COUNT(*) AS n FROM a UNION ALL SELECT COUNT(*) AS n FROM b",
            "SELECT 'NOT_ANSWERABLE_FIELD: no revenue field for store' AS answer, COUNT(*) AS n FROM t",
            "SELECT COUNT(a) AS total, COUNT(DISTINCT a) AS uniq FROM t",
            "SELECT count_a, COUNT(a) AS n FROM t GROUP BY count_a",
            "SELECT g, SUM(a) AS s FROM t GROUP BY g HAVING s > 1",
            "SELECT AVG(a) AS m FROM t ORDER BY m + 1",
        ],
        ids=[
            "parse_error",
            "multiple_statements",
            "union_root",
            "sentinel",
            "duplicate_output_name",
            "collides_with_plain_column",
            "having_references_alias",
            "order_by_expression_references_alias",
        ],
    )
    def test_unsafe_rewrite_fails_open(self, sql):
        assert normalize_aggregate_aliases(sql, "athena") == sql

    def test_unknown_dialect_fails_open(self):
        sql = "SELECT AVG(a) AS m FROM t"

        assert normalize_aggregate_aliases(sql, "no-such-engine") == sql
