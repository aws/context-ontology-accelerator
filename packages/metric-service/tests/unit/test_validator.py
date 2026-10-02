# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for metric validation module — Checks 1-6.

Tests cover:
- Check 1: SQL syntax validation (ERROR severity)
- Check 2: Table reference existence (WARNING)
- Check 3: Column reference existence (WARNING)
- Check 4: Dimension column existence (WARNING)
- Check 5: Filter type compatibility (WARNING)
- Check 6: Ontology class linkage (WARNING)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_common.constants import URN_PREFIX
from coa_metrics.validator import (
    ColumnMetadata,
    DataSourceLookup,
    DynamoDBDataSourceLookup,
    NeptuneOntologyLookup,
    OntologyLookup,
    check_data_modifying,
    check_select_shape,
    check_tier1_execution_shape,
    validate_metric,
)

pytestmark = pytest.mark.unit


# ── Mock implementations ────────────────────────────────────────────────


class MockDataSourceLookup(DataSourceLookup):
    """Mock lookup with configurable data sources, tables, and columns."""

    def __init__(
        self,
        sources: set[str] | None = None,
        tables: dict[str, set[str]] | None = None,
        columns: dict[str, list[ColumnMetadata]] | None = None,
    ) -> None:
        self._sources = sources or set()
        self._tables = tables or {}
        self._columns = columns or {}

    def data_source_exists(self, data_source_id: str) -> bool:
        return data_source_id in self._sources

    def table_exists(self, data_source_id: str, table_name: str) -> bool:
        ds_tables = self._tables.get(data_source_id, set())
        # Case-insensitive table lookup
        return table_name.lower() in {t.lower() for t in ds_tables}

    def get_table_columns(self, data_source_id: str, table_name: str) -> list[ColumnMetadata] | None:
        key = f"{data_source_id}:{table_name.lower()}"
        return self._columns.get(key)


class MockOntologyLookup(OntologyLookup):
    """Mock ontology lookup with configurable existing classes."""

    def __init__(self, existing_classes: set[str] | None = None) -> None:
        self._classes = existing_classes or set()

    def class_exists(self, class_uri: str, namespace: str) -> bool:
        return class_uri in self._classes


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def valid_metric() -> dict:
    """A valid metric body with all fields populated."""
    return {
        "name": "monthly_revenue",
        "description": "Total revenue from all orders in a calendar month",
        "expression": {
            "dialects": [
                {
                    "dialect": "postgresql",
                    "expression": (
                        "SELECT region, SUM(total_amount) AS monthly_revenue"
                        " FROM orders WHERE status = 'completed' GROUP BY region"
                    ),
                },
            ]
        },
        "dataSourceId": "ds-abc123",
        "sourceTable": "orders",
        "ontologyConcepts": ["ind:Order"],
    }


@pytest.fixture
def fragment_metric() -> dict:
    """A metric with aggregate fragment (no SELECT/GROUP BY)."""
    return {
        "name": "total_sales",
        "description": "Sum of sales",
        "expression": {
            "dialects": [
                {"dialect": "trino", "expression": "SUM(orders.total_amount)"},
            ]
        },
        "dataSourceId": "ds-abc123",
        "sourceTable": "orders",
        "ontologyConcepts": [],
    }


@pytest.fixture
def lookup() -> MockDataSourceLookup:
    """Standard lookup with orders table and common columns."""
    return MockDataSourceLookup(
        sources={"ds-abc123"},
        tables={"ds-abc123": {"orders", "customers"}},
        columns={
            "ds-abc123:orders": [
                ColumnMetadata(name="total_amount", data_type="decimal"),
                ColumnMetadata(name="region", data_type="varchar"),
                ColumnMetadata(name="order_date", data_type="date"),
                ColumnMetadata(name="status", data_type="varchar"),
                ColumnMetadata(name="customer_id", data_type="integer"),
            ],
            "ds-abc123:customers": [
                ColumnMetadata(name="id", data_type="integer"),
                ColumnMetadata(name="name", data_type="varchar"),
            ],
        },
    )


@pytest.fixture
def ontology_lookup() -> MockOntologyLookup:
    """Ontology with Order and Customer classes."""
    return MockOntologyLookup(existing_classes={"ind:Order", "ind:Customer"})


# ── Check 1: SQL Syntax ─────────────────────────────────────────────────


class TestCheck1SqlSyntax:
    """Check 1: SQL parses without syntax errors in declared dialect."""

    def test_valid_sql_passes(self, valid_metric: dict) -> None:
        result = validate_metric(valid_metric)
        assert result.valid
        assert len(result.errors) == 0

    def test_fragment_fails_shape_check(self, fragment_metric: dict) -> None:
        """#617: fragments parse fine but the serve firewall rejects them —
        the sql_shape check must flag them so validate predicts serve-time
        acceptance (previously this asserted fragments were valid)."""
        result = validate_metric(fragment_metric)
        assert not result.valid
        shape_errors = [e for e in result.errors if e["check"] == "sql_shape"]
        assert len(shape_errors) == 1
        assert "full SELECT statement" in shape_errors[0]["message"]

    def test_invalid_sql_produces_error(self) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "postgresql", "expression": "SELECT FROM WHERE"}]},
            "dataSourceId": "ds-1",
            "sourceTable": "t",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric)
        # Check 1 errors block validation
        assert not result.valid
        assert len(result.errors) >= 1
        assert result.errors[0]["check"] == "sql_syntax"
        assert result.errors[0]["severity"] == "error"

    def test_multiple_dialects_validated_independently(self) -> None:
        metric = {
            "expression": {
                "dialects": [
                    {"dialect": "trino", "expression": "SUM(amount)"},
                    {"dialect": "postgresql", "expression": "INVALID SQL {{{{"},
                ]
            },
            "dataSourceId": "ds-1",
            "sourceTable": "t",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric)
        # One dialect fails, one passes
        assert not result.valid
        assert len(result.errors) >= 1

    def test_empty_expression_produces_error(self) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": ""}]},
            "dataSourceId": "ds-1",
            "sourceTable": "t",
            "ontologyConcepts": [],
        }
        # The generated API model also rejects this, and the direct validator
        # must stay fail-closed if called independently.
        result = validate_metric(metric)
        assert not result.valid
        assert any("empty SQL expression" in error["message"] for error in result.errors)

    def test_all_supported_dialects(self) -> None:
        for dialect in ["trino", "postgresql", "redshift", "snowflake", "mysql", "databricks"]:
            metric = {
                "expression": {"dialects": [{"dialect": dialect, "expression": "SELECT SUM(amount) FROM t"}]},
                "dataSourceId": "ds-1",
                "sourceTable": "t",
                "ontologyConcepts": [],
            }
            result = validate_metric(metric)
            assert result.valid, f"Failed for dialect: {dialect}"


# ── Check 1b: Statement shape (serve-firewall parity, #617) ─────────────


class TestCheckSelectShape:
    """check_select_shape mirrors the serve firewall's SELECT-only rule."""

    def test_full_select_passes(self) -> None:
        assert check_select_shape("SELECT COUNT(*) AS total FROM orders", "TRINO") is None

    def test_cte_select_passes(self) -> None:
        sql = "WITH t AS (SELECT amount FROM orders) SELECT SUM(amount) FROM t"
        assert check_select_shape(sql, "POSTGRESQL") is None

    def test_union_passes(self) -> None:
        sql = "SELECT id FROM a UNION SELECT id FROM b"
        assert check_select_shape(sql, "TRINO") is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT",
            "SELECT *",
            "WITH x AS (SELECT 1) SELECT",
        ],
    )
    def test_incomplete_select_rejected(self, sql: str) -> None:
        error = check_select_shape(sql, "POSTGRESQL")
        assert error is not None
        assert "SELECT" in error

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT dblink_exec('conn', 'DELETE FROM orders')",
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_sleep(10)",
            "SELECT lo_import('/tmp/payload')",
            "SELECT lo_create(12345) FROM orders LIMIT 1",
            "SELECT lo_from_bytea(12345, 'payload') FROM orders LIMIT 1",
            "SELECT lo_put(12345, 0, 'payload') FROM orders LIMIT 1",
        ],
    )
    def test_dangerous_function_rejected(self, sql: str) -> None:
        error = check_select_shape(sql, "POSTGRESQL")
        assert error is not None
        assert "forbidden function" in error

    @pytest.mark.parametrize(
        ("dialect", "sql"),
        [
            ("POSTGRESQL", "SELECT setval('seq', 1)"),
            ("POSTGRESQL", "SELECT nextval('seq')"),
            ("POSTGRESQL", "SELECT pg_advisory_lock(1)"),
            ("POSTGRESQL", "SELECT pg_logical_emit_message(true, 'coa', 'x') FROM orders LIMIT 1"),
            ("POSTGRESQL", "SELECT pg_notify('channel', 'payload')"),
            ("MYSQL", "SELECT SLEEP(10)"),
            ("MYSQL", "SELECT BENCHMARK(1000000, MD5('x'))"),
            ("MYSQL", "SELECT LOAD_FILE('/etc/passwd')"),
            ("MYSQL", "SELECT GET_LOCK('resource', 10)"),
            ("TSQL", "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Server=x').db.dbo.orders"),
        ],
    )
    def test_dialect_specific_side_effect_function_rejected(self, dialect: str, sql: str) -> None:
        error = check_select_shape(sql, dialect)
        assert error is not None
        assert "forbidden function" in error

    def test_locking_select_rejected(self) -> None:
        error = check_select_shape("SELECT * FROM orders FOR UPDATE", "POSTGRESQL")
        assert error is not None
        assert "unsafe operation (Lock)" in error

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WITH (TABLOCKX)",
            "SELECT * FROM orders (TABLOCKX)",
            "SELECT * FROM orders TABLOCKX",
            "SELECT * FROM orders WITH (SERIALIZABLE)",
            "SELECT * FROM orders WITH (REPEATABLEREAD)",
            "SELECT * FROM orders WITH (READCOMMITTEDLOCK)",
            "SELECT * FROM orders /* outer /* inner */ AS */ TABLOCKX",
            "SELECT * FROM [orders]] AS] TABLOCKX",
            'SELECT * FROM dbo.fn(CAST(x AS "type\\") + safe) TABLOCKX',
            "SELECT NEXT VALUE FOR dbo.seq FROM orders",
        ],
    )
    def test_tsql_lock_or_sequence_side_effect_rejected(self, sql: str) -> None:
        error = check_select_shape(sql, "TSQL")
        assert error is not None
        assert "unsafe operation" in error

    def test_tsql_read_only_nolock_hint_still_passes(self) -> None:
        assert check_select_shape("SELECT * FROM orders WITH (NOLOCK)", "TSQL") is None
        assert check_select_shape("SELECT * FROM orders (NOLOCK)", "TSQL") is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders AS TABLOCKX",
            "SELECT * FROM orders AS [TABLOCKX]",
            "SELECT * FROM orders [SERIALIZABLE]",
            "SELECT * FROM orders AS /* alias */ TABLOCKX",
        ],
    )
    def test_explicit_tsql_alias_named_like_hint_still_passes(self, sql: str) -> None:
        assert check_select_shape(sql, "TSQL") is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM dbo.order_rows(@TABLOCKX)",
            "SELECT * FROM dbo.order_rows('TABLOCKX')",
            "SELECT * FROM dbo.order_rows([TABLOCKX])",
        ],
    )
    def test_tsql_table_valued_function_arguments_named_like_hint_pass(self, sql: str) -> None:
        assert check_select_shape(sql, "TSQL") is None

    def test_comment_as_does_not_hide_bare_tsql_hint(self) -> None:
        sql = "SELECT * FROM orders -- AS\nTABLOCKX"
        assert check_select_shape(sql, "TSQL") is not None

    def test_mysql_executable_comment_rejected_but_string_literal_passes(self) -> None:
        assert check_select_shape("SELECT /*!50000 SLEEP(10), */ 1", "MYSQL") is not None
        assert (
            check_select_shape(
                "SELECT `metric\\` /*!50000 , SLEEP(10) */ FROM `orders`",
                "MYSQL",
            )
            is not None
        )
        assert check_select_shape("SELECT '/*!50000 SLEEP(10) */'", "MYSQL") is None

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT COUNT(*)",
            "SELECT * FROM orders",
        ],
    )
    def test_executable_select_without_or_with_from_passes(self, sql: str) -> None:
        assert check_select_shape(sql, "POSTGRESQL") is None

    def test_count_fragment_rejected(self) -> None:
        error = check_select_shape("COUNT(*)", "TRINO")
        assert error is not None
        assert "full SELECT statement" in error
        assert "SELECT COUNT(*) FROM <source_table>" in error

    def test_sum_fragment_rejected(self) -> None:
        error = check_select_shape("SUM(orders.total_amount)", "POSTGRESQL")
        assert error is not None
        assert "full SELECT statement" in error

    def test_unparseable_sql_rejected(self) -> None:
        error = check_select_shape("SELECT FROM WHERE (((", "TRINO")
        assert error is not None

    def test_unknown_dialect_still_checks_shape(self) -> None:
        # Unresolvable dialect falls back to sqlglot's default parser
        assert check_select_shape("SELECT 1", "NOT_A_DIALECT") is None
        assert check_select_shape("COUNT(*)", "NOT_A_DIALECT") is not None

    # ── Deep scan: data-modifying nodes nested inside a SELECT ──────────
    # The DML matrix lives in TestCheckDataModifying (the hard block) — this
    # keeps one case to pin that check_select_shape shares that denylist, plus
    # the negative case proving the shared walk does not over-reach.

    def test_data_modifying_cte_delete_rejected(self) -> None:
        sql = "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d"
        error = check_select_shape(sql, "POSTGRESQL")
        assert error is not None
        assert "data-modifying" in error
        assert "Delete" in error

    def test_read_only_cte_and_subquery_still_pass(self) -> None:
        # Deep scan must not flag legitimate nested SELECTs
        sql = (
            "WITH recent AS (SELECT * FROM orders WHERE created > '2026-01-01') "
            "SELECT COUNT(*) FROM recent WHERE id IN (SELECT order_id FROM refunds)"
        )
        assert check_select_shape(sql, "POSTGRESQL") is None


# ── Tier 1 execution contract ──────────────────────────────────────────


class TestTier1ExecutionShape:
    def test_prefers_trino_expression_for_execution_validation(self) -> None:
        dialects = [
            {"dialect": "TSQL", "expression": "SELECT TOP 10 [value] FROM [orders]"},
            {"dialect": "TRINO", "expression": "SELECT value FROM orders LIMIT 10"},
        ]

        assert check_tier1_execution_shape(dialects) is None

    def test_non_trino_fallback_must_parse_as_trino(self) -> None:
        error = check_tier1_execution_shape([{"dialect": "TSQL", "expression": "SELECT TOP 10 [value] FROM [orders]"}])

        assert error is not None
        assert "Tier 1" in error
        assert "TRINO" in error

    def test_trino_compatible_first_fallback_passes(self) -> None:
        dialects = [{"dialect": "POSTGRESQL", "expression": "SELECT SUM(amount) FROM orders"}]

        assert check_tier1_execution_shape(dialects) is None


# ── check_data_modifying: read-only enforcement (#161) ──────────────────


class TestCheckDataModifying:
    """DML/DDL is this helper's concern; statement shape is checked separately."""

    # ── Delegated: shape validation handles fragments and parse errors ───

    @pytest.mark.parametrize("sql", ["COUNT(*)", "SUM(orders.total_amount)", "AVG(price)"])
    def test_fragment_is_allowed(self, sql: str) -> None:
        assert check_data_modifying(sql, "TRINO") is None

    @pytest.mark.parametrize("sql", ["SELECT FROM WHERE (((", "SELEKT 1 FRM", "))) nonsense ((("])
    def test_parse_error_without_dml_is_allowed(self, sql: str) -> None:
        assert check_data_modifying(sql, "TRINO") is None

    def test_plain_select_is_allowed(self) -> None:
        assert check_data_modifying("SELECT SUM(total_amount) AS r FROM orders", "TRINO") is None

    def test_read_only_cte_and_subquery_allowed(self) -> None:
        sql = (
            "WITH recent AS (SELECT * FROM orders WHERE created > '2026-01-01') "
            "SELECT COUNT(*) FROM recent WHERE id IN (SELECT order_id FROM refunds)"
        )
        assert check_data_modifying(sql, "POSTGRESQL") is None

    def test_column_named_like_a_keyword_allowed(self) -> None:
        """The regex is anchored at statement start — a column called
        `update_time` must not be mistaken for an UPDATE statement."""
        assert check_data_modifying("SELECT update_time, delete_flag FROM t", "POSTGRESQL") is None

    def test_dml_inside_a_comment_is_allowed(self) -> None:
        """Comments are stripped before the regex, so DML mentioned in a
        comment does not block — the AST walk governs the real statement."""
        assert check_data_modifying("-- DROP TABLE x\nSELECT 1", "POSTGRESQL") is None

    # ── Blocked: DML/DDL, however it is smuggled in ──────────────────────

    def test_trailing_dml_statement_blocked(self) -> None:
        error = check_data_modifying("SELECT 1; DROP TABLE x", "POSTGRESQL")
        assert error is not None
        assert "data-modifying" in error

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
            "WITH i AS (INSERT INTO audit (id) VALUES (1) RETURNING *) SELECT * FROM i",
            "WITH u AS (UPDATE orders SET total = 0 RETURNING *) SELECT * FROM u",
        ],
    )
    def test_dml_nested_in_cte_blocked(self, sql: str) -> None:
        error = check_data_modifying(sql, "POSTGRESQL")
        assert error is not None
        assert "data-modifying" in error

    def test_select_into_blocked(self) -> None:
        assert check_data_modifying("SELECT * INTO backup_orders FROM orders", "POSTGRESQL") is not None

    @pytest.mark.parametrize(
        "sql",
        ["DROP TABLE orders", "INSERT INTO t VALUES (1)", "ALTER TABLE t ADD c int", "CREATE TABLE t (c int)"],
    )
    def test_top_level_dml_blocked(self, sql: str) -> None:
        assert check_data_modifying(sql, "POSTGRESQL") is not None

    @pytest.mark.parametrize(
        "sql",
        [
            "MSCK REPAIR TABLE t",  # ParseError in sqlglot — regex must catch it
            "DELETE FROM orders WHERE (((",  # unparseable DML — regex must catch it
            "UNLOAD ('SELECT 1') TO 's3://b/k'",
            "EXPLAIN SELECT 1",
        ],
    )
    def test_unparseable_or_command_dml_blocked_by_regex(self, sql: str) -> None:
        """Pre-parse regex mirrors the serve firewall's _BLOCKED_STATEMENTS so
        DML hidden in an otherwise-unparseable string is still caught."""
        assert check_data_modifying(sql, "POSTGRESQL") is not None

    def test_block_message_is_actionable(self) -> None:
        error = check_data_modifying("DROP TABLE orders", "POSTGRESQL")
        assert error is not None
        assert "read-only" in error

    # ── DML/admin verbs beyond the original 8-node denylist ──────────────
    # The first cut copied the serve firewall's regex + node tuple but dropped
    # its ALLOWLIST stage, so every verb below published (201) instead of
    # hard-blocking. sqlglot funnels unknown verbs (CALL, VACUUM, OPTIMIZE, …)
    # into exp.Command, which is what makes the AST walk cover them.

    @pytest.mark.parametrize(
        "sql",
        [
            "TRUNCATE TABLE orders",
            "GRANT SELECT ON orders TO bob",
            "REVOKE SELECT ON orders FROM bob",
            "COPY orders FROM 's3://bucket/key' CREDENTIALS ''",
            "CALL some_proc()",
            "VACUUM orders",
            "SET search_path = evil_schema",
            "ANALYZE orders",
            "COMMENT ON TABLE orders IS 'x'",
            "LOAD DATA INPATH 'x' INTO TABLE orders",
            "RENAME TABLE orders TO orders_old",
            "ALTER SESSION SET query_tag = 'x'",
            "OPTIMIZE orders",
        ],
    )
    def test_admin_and_dml_verbs_blocked(self, sql: str) -> None:
        error = check_data_modifying(sql, "POSTGRESQL")
        assert error is not None, f"{sql!r} must be hard-blocked"
        assert "data-modifying" in error

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; TRUNCATE TABLE x",
            "SELECT 1; GRANT SELECT ON x TO bob",
            "SELECT 1; SET role admin",
            "SELECT 1; CALL p()",
        ],
    )
    def test_stacked_admin_statement_blocked(self, sql: str) -> None:
        """The regex is ``^\\s*``-anchored so it only sees the FIRST statement —
        the AST walk over every parsed statement is what catches these."""
        error = check_data_modifying(sql, "POSTGRESQL")
        assert error is not None, f"{sql!r} must be hard-blocked"
        assert "data-modifying" in error

    @pytest.mark.parametrize(
        "sql",
        [
            "COUNT(*)",
            "SUM(orders.total_amount)",
            "AVG(price)",
            "COUNT(DISTINCT user_id)",
            "SUM(CASE WHEN paid THEN 1 ELSE 0 END)",
            "SUM(amount) / NULLIF(COUNT(*), 0)",
            "SELECT COUNT(*) FROM orders GROUP BY region",
            "SELECT date_trunc('month', created_at) AS m, SUM(total) FROM orders GROUP BY 1",
            "SELECT SUM(x) OVER (PARTITION BY y ORDER BY z) FROM t",
            "SELECT a FROM t1 JOIN t2 ON t1.id = t2.id WHERE t2.d BETWEEN DATE '2020-01-01' AND CURRENT_DATE",
        ],
    )
    def test_widened_denylist_keeps_fragments_and_selects_soft(self, sql: str) -> None:
        """Invariant guard for the widened node tuple: aggregate fragments parse
        to exp.Count/exp.Sum (not statement nodes) and read-only SELECTs must
        keep returning None. Regression net against denylist over-reach."""
        assert check_data_modifying(sql, "POSTGRESQL") is None

    # ── Tokenizer / recursion failures must not escape as a 500 ──────────
    # sqlglot's TokenError is a SIBLING of ParseError (both SqlglotError), and
    # deeply-nested parens raise RecursionError. Neither is a ValueError, so
    # create/update handlers (which catch ValueError) turned them into a 500.

    @pytest.mark.parametrize(
        "sql",
        [
            "'; DROP TABLE users; --",  # TokenError (unterminated quote)
            "SELECT 'unterminated",
            'SELECT "unterminated',
            "'",
        ],
    )
    def test_token_error_does_not_raise(self, sql: str) -> None:
        result = check_data_modifying(sql, "POSTGRESQL")
        assert result is None or isinstance(result, str)

    def test_deeply_nested_parens_does_not_raise(self) -> None:
        """RecursionError from sqlglot's recursive-descent parser must be
        absorbed (soft), not surface as an unhandled 500."""
        sql = "SELECT " + "(" * 500 + "1" + ")" * 500
        result = check_data_modifying(sql, "POSTGRESQL")
        assert result is None or isinstance(result, str)

    def test_token_error_with_leading_dml_still_blocked(self) -> None:
        """Defense in depth: DML at the START of a token-error string is caught
        by the pre-parse regex before parsing is even attempted."""
        error = check_data_modifying("DROP TABLE users; --'", "POSTGRESQL")
        assert error is not None

    # ── Stacked DML that also BREAKS the parse ───────────────────────────
    # The gap the two other layers each miss: the ``^\s*`` regex only sees the
    # leading SELECT, and the AST walk never runs because the trailing statement
    # is exactly what makes sqlglot raise. All four below returned None (accepted)
    # until the parse-failure path started re-checking each ``;``-segment.

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; MSCK REPAIR TABLE t",  # ParseError
            "SELECT 1; UNLOAD ('SELECT 1') TO 's3://b/'",  # ParseError
            "SELECT 1; PUT file:///a @s",  # ParseError
            "'; DROP TABLE users; --",  # TokenError (unterminated quote)
        ],
    )
    def test_stacked_dml_that_breaks_the_parser_blocked(self, sql: str) -> None:
        error = check_data_modifying(sql, "POSTGRESQL")
        assert error is not None, f"{sql!r} must be hard-blocked"
        assert "data-modifying" in error

    @pytest.mark.parametrize(
        "sql",
        [
            "total_amount",  # bare column — the canonical soft fragment
            "SELECT FROM WHERE (((",  # parse error, read-only
            "COUNT(*)",
            "SUM(x) OVER (PARTITION BY y)",
            "RANK() OVER (ORDER BY total DESC)",
            "revenue - cost",
            "SELECT ((( FROM t",
            "SELECT SUM(amount) FROM orders WHERE note = 'a;b'",  # ';' inside a literal
            "SELECT * FROM t WHERE label = 'x; DELETE FROM y'",  # DML-looking literal
            "SELECT SUM(x) FROM t -- DELETE FROM y",  # DML in a trailing comment
        ],
    )
    def test_segment_scan_keeps_read_only_sql_soft(self, sql: str) -> None:
        """Regression guard on the segment scan: splitting on ``;`` must not
        start hard-blocking fragments, parse errors, or a ``;`` that lives
        inside a string literal or comment."""
        assert check_data_modifying(sql, "POSTGRESQL") is None


# ── Check 2: Table References ───────────────────────────────────────────


class TestCheck2TableReferences:
    """Check 2: All table references exist in OMS for the metric's dataSourceId."""

    def test_existing_table_passes(self, valid_metric: dict, lookup: MockDataSourceLookup) -> None:
        result = validate_metric(valid_metric, data_sources_lookup=lookup)
        table_warnings = [w for w in result.warnings if w["check"] == "table_reference"]
        assert all(w["details"]["table"] != "orders" for w in table_warnings if not w.get("passed", True))

    def test_missing_table_produces_warning(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT * FROM nonexistent_table"}]},
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        assert result.valid  # Warnings don't block
        table_warnings = [w for w in result.warnings if w["check"] == "table_reference"]
        assert len(table_warnings) >= 1
        assert "nonexistent_table" in table_warnings[0]["message"]

    def test_source_table_validated_even_if_not_in_sql(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(amount) FROM t"}]},
            "dataSourceId": "ds-abc123",
            "sourceTable": "missing_table",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        table_warnings = [w for w in result.warnings if w["check"] == "table_reference"]
        assert any("missing_table" in w["message"] for w in table_warnings)

    def test_no_lookup_reports_unverified_table(self, valid_metric: dict) -> None:
        result = validate_metric(valid_metric, data_sources_lookup=None)
        table_warnings = [w for w in result.warnings if w["check"] == "table_reference"]
        assert len(table_warnings) == 1
        assert "was not verified" in table_warnings[0]["message"]
        assert table_warnings[0]["details"]["verification"] == "unavailable"


class TestSourceTableProvableAbsence:
    """#161: the DECLARED sourceTable is an ERROR when its absence is provable.

    Provable = the catalog was read, it enumerates at least one table for the
    source, and the declared table is not among them. Everything else stays a
    soft WARNING — see the docstrings below for why each case is unprovable.
    """

    def _enumerating_lookup(self, *, available: bool = True, tables: set[str] | None = None):
        class _Lookup(MockDataSourceLookup):
            def catalog_available(self, data_source_id: str) -> bool:
                return available

            def known_tables(self, data_source_id: str) -> set[str]:
                return {t.lower() for t in (tables or set())}

        return _Lookup(sources={"ds-abc123"}, tables={"ds-abc123": tables or set()})

    def _metric(self, source_table: str) -> dict:
        return {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(amount) FROM t"}]},
            "dataSourceId": "ds-abc123",
            "sourceTable": source_table,
            "ontologyConcepts": [],
        }

    def test_provable_absence_is_error(self) -> None:
        lookup = self._enumerating_lookup(tables={"orders", "customers"})
        result = validate_metric(self._metric("no_such_table"), data_sources_lookup=lookup)

        assert not result.valid
        errors = [e for e in result.errors if e["check"] == "table_reference"]
        assert len(errors) == 1
        assert "no_such_table" in errors[0]["message"]

    def test_provable_absence_is_error_when_source_table_is_also_in_sql(self) -> None:
        """The extracted-table loop must not hide declared sourceTable severity.

        This is the common validate-before-create shape: SQL names the same table
        as sourceTable. The dedicated create/update gate rejects a provably
        absent sourceTable, so validate must report ERROR too.
        """
        lookup = self._enumerating_lookup(tables={"orders", "customers"})
        metric = self._metric("no_such_table")
        metric["expression"]["dialects"][0]["expression"] = "SELECT SUM(amount) FROM no_such_table"

        result = validate_metric(metric, data_sources_lookup=lookup)

        assert not result.valid
        errors = [e for e in result.errors if e["check"] == "table_reference"]
        assert len(errors) == 1
        assert errors[0]["details"]["table"] == "no_such_table"

    def test_present_source_table_passes(self) -> None:
        lookup = self._enumerating_lookup(tables={"orders"})
        result = validate_metric(self._metric("Orders"), data_sources_lookup=lookup)

        assert result.valid
        assert not [e for e in result.errors if e["check"] == "table_reference"]

    def test_schema_qualified_source_matches_known_bare_name(self) -> None:
        lookup = self._enumerating_lookup(tables={"orders", "customers"})
        result = validate_metric(self._metric("public.orders"), data_sources_lookup=lookup)

        assert result.valid
        assert not [e for e in result.errors if e["check"] == "table_reference"]

    def test_schema_qualified_sql_uses_exact_catalog_name(self) -> None:
        """Do not collapse a qualified reference to an ambiguous bare name."""

        class _QualifiedLookup(MockDataSourceLookup):
            def table_exists(self, data_source_id: str, table_name: str) -> bool:
                return table_name.lower() == "sales.orders"

            def catalog_available(self, data_source_id: str) -> bool:
                return True

            def known_tables(self, data_source_id: str) -> set[str]:
                return {"sales.orders", "archive.orders"}

        metric = self._metric("sales.orders")
        metric["expression"]["dialects"][0]["expression"] = "SELECT SUM(amount) FROM sales.orders"

        result = validate_metric(metric, data_sources_lookup=_QualifiedLookup(sources={"ds-abc123"}))

        assert result.valid
        assert not [w for w in result.warnings if w["check"] == "table_reference"]

    def test_known_but_unresolved_source_stays_warning(self) -> None:
        """Mirror create/update when an asset name is known but its approved
        form is unresolved (unapproved or an ambiguous bare name)."""

        class _UnresolvedKnownLookup(MockDataSourceLookup):
            def table_exists(self, data_source_id: str, table_name: str) -> bool:
                return False

            def catalog_available(self, data_source_id: str) -> bool:
                return True

            def known_tables(self, data_source_id: str) -> set[str]:
                return {"sales.orders", "archive.orders", "orders"}

        lookup = _UnresolvedKnownLookup(sources={"ds-abc123"})
        result = validate_metric(self._metric("orders"), data_sources_lookup=lookup)

        assert result.valid
        assert not [e for e in result.errors if e["check"] == "table_reference"]
        assert [w for w in result.warnings if w["check"] == "table_reference"]

    def test_empty_catalog_stays_warning(self) -> None:
        """A COMPLETED source with no approved assets enumerates nothing —
        absence unprovable, so this must not become an ERROR."""
        lookup = self._enumerating_lookup(tables=set())
        result = validate_metric(self._metric("no_such_table"), data_sources_lookup=lookup)

        assert result.valid
        assert [w for w in result.warnings if w["check"] == "table_reference"]

    def test_unavailable_catalog_stays_warning(self) -> None:
        """Read failure: table_exists fails open, so absence proves nothing."""
        lookup = self._enumerating_lookup(available=False, tables={"orders"})
        result = validate_metric(self._metric("no_such_table"), data_sources_lookup=lookup)

        assert result.valid
        assert [w for w in result.warnings if w["check"] == "table_reference"]

    def test_non_enumerating_lookup_stays_warning(self, lookup: MockDataSourceLookup) -> None:
        """A lookup that doesn't implement known_tables (base default) can never
        prove absence — this is what keeps the promotion safe by default."""
        result = validate_metric(self._metric("missing_table"), data_sources_lookup=lookup)

        assert result.valid
        assert [w for w in result.warnings if w["check"] == "table_reference"]

    def test_sql_extracted_tables_stay_warning(self) -> None:
        """find_all(exp.Table) also matches CTE/subquery aliases, which are not
        catalog tables — those must never be promoted to ERROR."""
        lookup = self._enumerating_lookup(tables={"orders"})
        metric = {
            "expression": {
                "dialects": [
                    {
                        "dialect": "trino",
                        "expression": "WITH cte AS (SELECT amount FROM orders) SELECT SUM(amount) FROM cte",
                    }
                ]
            },
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)

        assert result.valid  # 'cte' is not in the catalog but must not block
        assert any("cte" in w["message"] for w in result.warnings if w["check"] == "table_reference")


# ── Check 3: Column References ──────────────────────────────────────────


class TestCheck3ColumnReferences:
    """Check 3: All column references exist and match expected types."""

    def test_valid_columns_no_warning(self, valid_metric: dict, lookup: MockDataSourceLookup) -> None:
        result = validate_metric(valid_metric, data_sources_lookup=lookup)
        col_warnings = [w for w in result.warnings if w["check"] == "column_reference"]
        assert len(col_warnings) == 0

    def test_missing_column_produces_warning(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(nonexistent_col) FROM orders"}]},
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        assert result.valid  # Soft warning
        col_warnings = [w for w in result.warnings if w["check"] == "column_reference"]
        assert len(col_warnings) >= 1
        assert "nonexistent_col" in col_warnings[0]["message"]

    def test_column_check_case_insensitive(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(TOTAL_AMOUNT) FROM orders"}]},
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        col_warnings = [w for w in result.warnings if w["check"] == "column_reference"]
        assert len(col_warnings) == 0

    def test_no_column_metadata_skips_check(self) -> None:
        lookup = MockDataSourceLookup(
            sources={"ds-abc123"},
            tables={"ds-abc123": {"orders"}},
            columns={},  # No column metadata available
        )
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(anything) FROM orders"}]},
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        col_warnings = [w for w in result.warnings if w["check"] == "column_reference"]
        assert len(col_warnings) == 0


# ── Check 4: Dimension Columns ──────────────────────────────────────────


class TestCheck4DimensionColumns:
    """Check 4: Dimension columns (GROUP BY) exist in referenced tables."""

    def test_valid_dimension_passes(self, valid_metric: dict, lookup: MockDataSourceLookup) -> None:
        result = validate_metric(valid_metric, data_sources_lookup=lookup)
        dim_warnings = [w for w in result.warnings if w["check"] == "dimension_column"]
        assert len(dim_warnings) == 0

    def test_missing_dimension_column_produces_warning(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {
                "dialects": [
                    {
                        "dialect": "postgresql",
                        "expression": "SELECT bad_dim, SUM(total_amount) FROM orders GROUP BY bad_dim",
                    }
                ]
            },
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        assert result.valid
        dim_warnings = [w for w in result.warnings if w["check"] == "dimension_column"]
        assert len(dim_warnings) >= 1
        assert "bad_dim" in dim_warnings[0]["message"]

    def test_fragment_skips_dimension_check(self, fragment_metric: dict, lookup: MockDataSourceLookup) -> None:
        """Fragments don't have GROUP BY — dimension check should not fire."""
        result = validate_metric(fragment_metric, data_sources_lookup=lookup)
        dim_warnings = [w for w in result.warnings if w["check"] == "dimension_column"]
        assert len(dim_warnings) == 0


# ── Check 5: Filter Type Compatibility ──────────────────────────────────


class TestCheck5FilterCompatibility:
    """Check 5: Filter columns exist and type matches operator."""

    def test_compatible_filter_no_warning(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {
                "dialects": [
                    {
                        "dialect": "postgresql",
                        "expression": "SELECT SUM(total_amount) FROM orders WHERE total_amount > 100",
                    }
                ]
            },
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        filter_warnings = [w for w in result.warnings if w["check"] == "filter_type_compatibility"]
        assert len(filter_warnings) == 0

    def test_like_on_varchar_no_warning(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {
                "dialects": [
                    {
                        "dialect": "postgresql",
                        "expression": "SELECT SUM(total_amount) FROM orders WHERE region LIKE '%US%'",
                    }
                ]
            },
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        filter_warnings = [w for w in result.warnings if w["check"] == "filter_type_compatibility"]
        assert len(filter_warnings) == 0

    def test_like_on_integer_produces_warning(self, lookup: MockDataSourceLookup) -> None:
        metric = {
            "expression": {
                "dialects": [
                    {
                        "dialect": "postgresql",
                        "expression": "SELECT SUM(total_amount) FROM orders WHERE customer_id LIKE '%123%'",
                    }
                ]
            },
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, data_sources_lookup=lookup)
        filter_warnings = [w for w in result.warnings if w["check"] == "filter_type_compatibility"]
        assert len(filter_warnings) >= 1
        assert "customer_id" in filter_warnings[0]["message"]

    def test_no_where_clause_skips_check(self, fragment_metric: dict, lookup: MockDataSourceLookup) -> None:
        result = validate_metric(fragment_metric, data_sources_lookup=lookup)
        filter_warnings = [w for w in result.warnings if w["check"] == "filter_type_compatibility"]
        assert len(filter_warnings) == 0


# ── Check 6: Ontology Class Linkage ────────────────────────────────────


class TestCheck6OntologyLinkage:
    """Check 6: :governedMetricFor references a valid ontology class."""

    def test_existing_class_passes(self, valid_metric: dict, ontology_lookup: MockOntologyLookup) -> None:
        result = validate_metric(valid_metric, ontology_lookup=ontology_lookup, namespace="sales")
        onto_warnings = [w for w in result.warnings if w["check"] == "ontology_linkage"]
        assert len(onto_warnings) == 0

    def test_missing_class_produces_warning(self, ontology_lookup: MockOntologyLookup) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(amount) FROM t"}]},
            "dataSourceId": "ds-1",
            "sourceTable": "t",
            "ontologyConcepts": ["ind:NonExistentClass"],
        }
        result = validate_metric(metric, ontology_lookup=ontology_lookup, namespace="sales")
        assert result.valid  # Soft warning
        onto_warnings = [w for w in result.warnings if w["check"] == "ontology_linkage"]
        assert len(onto_warnings) == 1
        assert "NonExistentClass" in onto_warnings[0]["message"]

    def test_multiple_concepts_validated(self, ontology_lookup: MockOntologyLookup) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(amount) FROM t"}]},
            "dataSourceId": "ds-1",
            "sourceTable": "t",
            "ontologyConcepts": ["ind:Order", "ind:Missing", "ind:Customer"],
        }
        result = validate_metric(metric, ontology_lookup=ontology_lookup, namespace="sales")
        onto_warnings = [w for w in result.warnings if w["check"] == "ontology_linkage"]
        assert len(onto_warnings) == 1  # Only "ind:Missing" fails
        assert "Missing" in onto_warnings[0]["message"]

    def test_no_ontology_lookup_skips_check(self, valid_metric: dict) -> None:
        result = validate_metric(valid_metric, ontology_lookup=None)
        onto_warnings = [w for w in result.warnings if w["check"] == "ontology_linkage"]
        assert len(onto_warnings) == 0

    def test_empty_concepts_skips_check(self, ontology_lookup: MockOntologyLookup) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT SUM(amount) FROM t"}]},
            "dataSourceId": "ds-1",
            "sourceTable": "t",
            "ontologyConcepts": [],
        }
        result = validate_metric(metric, ontology_lookup=ontology_lookup, namespace="sales")
        onto_warnings = [w for w in result.warnings if w["check"] == "ontology_linkage"]
        assert len(onto_warnings) == 0


# ── Combined / Integration Tests ───────────────────────────────────────


class TestCombinedValidation:
    """Tests that verify multiple checks running together."""

    def test_all_checks_pass_for_valid_metric(
        self,
        valid_metric: dict,
        lookup: MockDataSourceLookup,
        ontology_lookup: MockOntologyLookup,
    ) -> None:
        result = validate_metric(
            valid_metric,
            data_sources_lookup=lookup,
            ontology_lookup=ontology_lookup,
            namespace="sales",
        )
        assert result.valid
        assert len(result.errors) == 0
        # May have some warnings for table references in SQL
        # but core columns should pass

    def test_syntax_error_blocks_even_with_other_warnings(
        self, lookup: MockDataSourceLookup, ontology_lookup: MockOntologyLookup
    ) -> None:
        metric = {
            "expression": {"dialects": [{"dialect": "trino", "expression": "SELECT FROM WHERE"}]},
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": ["ind:Missing"],
        }
        result = validate_metric(
            metric,
            data_sources_lookup=lookup,
            ontology_lookup=ontology_lookup,
            namespace="sales",
        )
        assert not result.valid  # Blocked by Check 1
        assert len(result.errors) >= 1
        assert result.errors[0]["check"] == "sql_syntax"

    def test_multiple_warning_types_returned(
        self, lookup: MockDataSourceLookup, ontology_lookup: MockOntologyLookup
    ) -> None:
        metric = {
            "expression": {
                "dialects": [
                    {
                        "dialect": "postgresql",
                        "expression": "SELECT bad_col FROM missing_table WHERE customer_id LIKE '%x%' GROUP BY bad_dim",
                    }
                ]
            },
            "dataSourceId": "ds-abc123",
            "sourceTable": "orders",
            "ontologyConcepts": ["ind:NonExistent"],
        }
        result = validate_metric(
            metric,
            data_sources_lookup=lookup,
            ontology_lookup=ontology_lookup,
            namespace="sales",
        )
        assert result.valid  # All are warnings, not errors
        # Should have warnings from multiple checks
        check_types = {w["check"] for w in result.warnings}
        assert len(check_types) >= 2  # At least table + ontology or column + ontology

    def test_validation_without_any_lookups(self, valid_metric: dict) -> None:
        """Validation without a catalog reports that metadata was not verified."""
        result = validate_metric(valid_metric)
        assert result.valid
        assert len(result.errors) == 0
        assert len(result.warnings) == 1
        assert result.warnings[0]["check"] == "table_reference"
        assert result.warnings[0]["details"]["verification"] == "unavailable"


# ── Concrete lookup resilience ──────────────────────────────────────────


@pytest.mark.skip(reason="DynamoDBDataSourceLookup deprecated — replaced by SmusCatalogDataSourceLookup")
class TestDynamoDBDataSourceLookupResilience:
    """DynamoDB errors must degrade gracefully, never crash validation."""

    def _lookup(self) -> DynamoDBDataSourceLookup:
        lookup = DynamoDBDataSourceLookup(table_name="t", region="us-east-1")
        lookup._table = MagicMock()
        return lookup

    def test_data_source_exists_returns_false_on_dynamodb_error(self) -> None:
        lookup = self._lookup()
        lookup._table.get_item.side_effect = Exception("ThrottlingException")
        assert lookup.data_source_exists("ds-1") is False

    def test_data_source_exists_true_when_item_present(self) -> None:
        lookup = self._lookup()
        lookup._table.get_item.return_value = {"Item": {"PK": "DS#ds-1"}}
        assert lookup.data_source_exists("ds-1") is True

    def test_get_table_columns_returns_none_on_dynamodb_error(self) -> None:
        lookup = self._lookup()
        lookup._table.get_item.side_effect = Exception("AccessDeniedException")
        assert lookup.get_table_columns("ds-1", "orders") is None

    def test_get_table_columns_caches_none_result(self) -> None:
        lookup = self._lookup()
        lookup._table.get_item.side_effect = Exception("boom")
        assert lookup.get_table_columns("ds-1", "orders") is None
        # Second call must hit the cache, not DynamoDB again.
        lookup._table.get_item.reset_mock()
        assert lookup.get_table_columns("ds-1", "orders") is None
        lookup._table.get_item.assert_not_called()


class TestNeptuneOntologyLookupUriValidation:
    """class_exists must reject unsafe/malformed concept URIs before querying."""

    def _lookup(self) -> NeptuneOntologyLookup:
        lookup = NeptuneOntologyLookup()
        lookup._sparql_query = MagicMock(return_value={"boolean": True})
        return lookup

    def test_valid_curie_runs_query(self) -> None:
        lookup = self._lookup()
        assert lookup.class_exists("ind:Order", "sales") is True
        lookup._sparql_query.assert_called_once()

    def test_valid_urn_iri_runs_query(self) -> None:
        lookup = self._lookup()
        assert lookup.class_exists(f"urn:{URN_PREFIX}:sales:Order", "sales") is True
        lookup._sparql_query.assert_called_once()

    @pytest.mark.parametrize(
        "bad_uri",
        [
            "ind:Order} . } #",  # SPARQL break-out attempt
            "ind:Order ?x",  # whitespace injection
            "no-colon-here",  # not a CURIE/IRI
            "",  # empty
            "ind:Or<der>",  # angle brackets
            'ind:"Order"',  # quotes
        ],
    )
    def test_unsafe_uri_rejected_without_query(self, bad_uri: str) -> None:
        lookup = self._lookup()
        assert lookup.class_exists(bad_uri, "sales") is False
        lookup._sparql_query.assert_not_called()

    def test_query_error_fails_open_to_false(self) -> None:
        lookup = self._lookup()
        lookup._sparql_query.side_effect = Exception("neptune down")
        assert lookup.class_exists("ind:Order", "sales") is False
