# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the JDBC engine dialects."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from coa_sources.database.connectors.dialects import (
    DISCOVERY_ENGINES,
    InformationSchemaDialect,
    MySqlDialect,
    OracleDialect,
    PostgresDialect,
    RedshiftDialect,
    SnowflakeDialect,
    SqlServerDialect,
    get_dialect,
)


class _Cursor:
    """Mock DB-API cursor that returns canned rows based on the executed SQL."""

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[tuple[str, tuple]] = []
        self._rows: list = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self._rows = self._responder(sql.lower(), params)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Conn:
    def __init__(self, responder):
        self.cursor_obj = _Cursor(responder)

    def cursor(self):
        return self.cursor_obj


@pytest.mark.unit
class TestRegistry:
    def test_enabled_engines_resolve(self):
        for engine, cls in [
            ("POSTGRESQL", PostgresDialect),
            ("REDSHIFT", RedshiftDialect),
            ("MYSQL", MySqlDialect),
            ("SQLSERVER", SqlServerDialect),
        ]:
            assert isinstance(get_dialect(engine), cls)

    def test_snowflake_resolves(self):
        assert isinstance(get_dialect("SNOWFLAKE"), SnowflakeDialect)
        assert isinstance(get_dialect("snowflake"), SnowflakeDialect)

    def test_oracle_resolves(self):
        assert isinstance(get_dialect("ORACLE"), OracleDialect)
        assert isinstance(get_dialect("oracle"), OracleDialect)

    def test_dialect_engine_sets(self):
        assert SnowflakeDialect().engines == {"SNOWFLAKE"}
        assert OracleDialect().engines == {"ORACLE"}

    def test_redshift_resolves_to_dedicated_dialect_not_plain_postgres(self):
        # RedshiftDialect subclasses PostgresDialect but POSTGRESQL must NOT
        # resolve to the Redshift dialect.
        assert type(get_dialect("REDSHIFT")) is RedshiftDialect
        assert type(get_dialect("POSTGRESQL")) is PostgresDialect

    def test_case_insensitive_and_unknown(self):
        assert isinstance(get_dialect("postgresql"), PostgresDialect)
        assert get_dialect("DB2") is None
        assert get_dialect(None) is None

    def test_discovery_engines_set(self):
        assert {"POSTGRESQL", "REDSHIFT", "MYSQL", "SQLSERVER", "ORACLE", "SNOWFLAKE"} == DISCOVERY_ENGINES


@pytest.mark.unit
class TestRedshiftDialect:
    """Redshift lists schemas from pg_namespace, not information_schema.schemata
    (which returns no rows on Redshift)."""

    def test_list_schemas_uses_pg_namespace(self):
        captured: list[str] = []

        def responder(sql, p):
            captured.append(sql)
            if "pg_catalog.pg_namespace" in sql:
                return [("public",), ("analytics",), ("pg_catalog",)]
            if "information_schema.schemata" in sql:
                return []  # Redshift returns nothing here
            return []

        schemas = RedshiftDialect().list_schemas(_Conn(responder))
        assert schemas == ["public", "analytics", "pg_catalog"]
        # Must NOT have queried information_schema.schemata.
        assert all("information_schema.schemata" not in s for s in captured)
        assert any("pg_catalog.pg_namespace" in s for s in captured)

    def test_default_port_and_system_schema_pattern(self):
        d = RedshiftDialect()
        assert d.default_port == 5439
        # Redshift-internal schemas are excluded; user schemas are not.
        import re

        pat = re.compile(d.system_schema_pattern)
        for sys_schema in ("pg_catalog", "pg_internal", "pg_auto_copy", "catalog_history", "information_schema"):
            assert pat.match(sys_schema), sys_schema
        assert not pat.match("public")
        assert not pat.match("analytics")

    def test_fetch_columns_recovers_late_binding_view(self):
        """A late-binding view returns nothing from information_schema.columns and
        must be recovered via pg_get_late_binding_view_cols(), marked nullable (#134)."""

        def responder(sql, p):
            if "information_schema.columns" in sql:
                # Normal table resolves; the late-binding view returns no rows.
                return [
                    ("orders", "id", "integer", "NO"),
                    ("orders", "amount", "numeric", "YES"),
                ]
            if "pg_get_late_binding_view_cols" in sql:
                return [
                    ("orders__daily", "day", "date"),
                    ("orders__daily", "total", "numeric(18,2)"),
                ]
            return []

        conn = _Conn(responder)
        rows = RedshiftDialect().fetch_columns(conn, "public", ["orders", "orders__daily"])

        # Normal table unchanged, with real nullability.
        assert ("orders", "id", "integer", False) in rows
        assert ("orders", "amount", "numeric", True) in rows
        # Late-binding view recovered, data types preserved, defaulted nullable.
        assert ("orders__daily", "day", "date", True) in rows
        assert ("orders__daily", "total", "numeric(18,2)", True) in rows
        assert len(rows) == 4

        # The fallback must be scoped to the missing table only.
        late_calls = [(s, pr) for (s, pr) in conn.cursor_obj.calls if "pg_get_late_binding_view_cols" in s]
        assert len(late_calls) == 1
        assert "orders__daily" in late_calls[0][1]
        assert "orders" not in late_calls[0][1]

    def test_fetch_columns_no_fallback_when_all_covered(self):
        """The late-binding scan (a full-catalog function) must NOT run when
        information_schema already answered for every requested table."""

        def responder(sql, p):
            if "information_schema.columns" in sql:
                return [("orders", "id", "integer", "NO")]
            if "pg_get_late_binding_view_cols" in sql:
                raise AssertionError("late-binding fallback ran despite full information_schema coverage")
            return []

        conn = _Conn(responder)
        rows = RedshiftDialect().fetch_columns(conn, "public", ["orders"])
        assert rows == [("orders", "id", "integer", False)]
        assert all("pg_get_late_binding_view_cols" not in s for (s, _p) in conn.cursor_obj.calls)


@pytest.mark.unit
class TestInformationSchemaDialect:
    def _dialect(self):
        return PostgresDialect()

    def test_list_schemas(self):
        conn = _Conn(lambda sql, p: [("public",), ("analytics",)] if "schemata" in sql else [])
        assert self._dialect().list_schemas(conn) == ["public", "analytics"]

    def test_list_tables_filters_to_base_and_view(self):
        conn = _Conn(lambda sql, p: [("t1",), ("v1",)] if "information_schema.tables" in sql else [])
        assert self._dialect().list_tables(conn, "public") == ["t1", "v1"]
        sql, params = conn.cursor_obj.calls[0]
        assert "'BASE TABLE', 'VIEW'" in sql and params == ("public",)

    def test_fetch_columns_normalizes_nullability_and_builds_placeholders(self):
        rows = [("t", "a", "bigint", "NO"), ("t", "b", "text", "YES")]
        conn = _Conn(lambda sql, p: rows if "information_schema.columns" in sql else [])
        out = self._dialect().fetch_columns(conn, "public", ["t", "u"])
        assert out == [("t", "a", "bigint", False), ("t", "b", "text", True)]
        sql, params = conn.cursor_obj.calls[0]
        # one %s per table in the IN clause, plus the schema bind
        assert sql.count("%s") == 3 and params == ("public", "t", "u")

    def test_fetch_columns_empty_tables_no_query(self):
        conn = _Conn(lambda sql, p: [])
        assert self._dialect().fetch_columns(conn, "public", []) == []
        assert conn.cursor_obj.calls == []

    def test_fetch_constraints_pk_and_fk(self):
        def responder(sql, p):
            if "primary key" in sql:
                return [("orders", "id")]
            if "referential_constraints" in sql:
                return [("orders", "customer_id", "customers", "id")]
            return []

        pk, fk = self._dialect().fetch_constraints(_Conn(responder), "public", ["orders"])
        assert pk == {"orders": ["id"]}
        assert fk == {"orders": [("customer_id", "customers", "id")]}

    def test_fetch_constraints_composite_fk_paired_by_position(self):
        # A composite (2-column) FK must pair columns BY POSITION — (a,b)->(x,y),
        # NOT the N×N cartesian product the old constraint_column_usage join
        # produced. The position-matched query joins kcu (FK side) to the
        # referenced unique constraint on position_in_unique_constraint, so the
        # DB returns exactly the correctly-paired rows.
        conn = _Conn(
            lambda sql, p: (
                [("order_items", "order_id"), ("order_items", "line_no")]
                if "primary key" in sql
                else [
                    ("order_items", "order_id", "shipments", "order_id"),
                    ("order_items", "line_no", "shipments", "line_no"),
                ]
                if "referential_constraints" in sql
                else []
            )
        )
        pk, fk = self._dialect().fetch_constraints(conn, "public", ["order_items"])
        assert fk == {
            "order_items": [
                ("order_id", "shipments", "order_id"),
                ("line_no", "shipments", "line_no"),
            ]
        }
        # Must use referential_constraints with a positional join, NOT the
        # cartesian constraint_column_usage join that mis-paired composite FKs.
        fk_sql = next(s for s, _ in conn.cursor_obj.calls if "referential_constraints" in s.lower())
        assert "position_in_unique_constraint" in fk_sql.lower()
        assert "constraint_column_usage" not in fk_sql.lower()


@pytest.mark.unit
class TestMySqlDialect:
    def test_fk_uses_key_column_usage_not_constraint_column_usage(self):
        captured = []

        def responder(sql, p):
            captured.append(sql)
            if "referenced_table_name is not null" in sql:
                return [("orders", "customer_id", "customers", "id")]
            if "constraint_name = 'primary'" in sql:
                return [("orders", "id")]
            return []

        pk, fk = MySqlDialect().fetch_constraints(_Conn(responder), "shop", ["orders"])
        assert pk == {"orders": ["id"]}
        assert fk == {"orders": [("customer_id", "customers", "id")]}
        # MySQL has no constraint_column_usage view — must not be referenced.
        assert not any("constraint_column_usage" in s for s in captured)


@pytest.mark.unit
class TestOracleDialect:
    def test_list_schemas_uses_all_users(self):
        conn = _Conn(lambda sql, p: [("HR",), ("SALES",)] if "all_users" in sql else [])
        assert OracleDialect().list_schemas(conn) == ["HR", "SALES"]

    def test_list_tables_binds_owner_once_per_value_not_per_occurrence(self):
        """A reused bind must not require a second value.

        The UNION references the owner twice. With positional `:1` binds
        python-oracledb counts each OCCURRENCE, so one value for two `:1`s
        failed every Oracle discovery with:
            DPY-4009: 2 positional bind values are required but 1 were provided
        A named bind is supplied once and may be referenced freely.
        """
        conn = _Conn(lambda sql, p: [("EMP",), ("DEPT",)] if "all_tables" in sql else [])
        assert OracleDialect().list_tables(conn, "HR") == ["EMP", "DEPT"]
        sql, params = conn.cursor_obj.calls[0]
        # Named bind, referenced twice, supplied once.
        assert sql.count(":owner") == 2
        assert ":1" not in sql
        assert params == {"owner": "HR"}

    def test_fetch_columns_uses_all_tab_columns_with_named_binds(self):
        rows = [("EMP", "ID", "NUMBER", "N"), ("EMP", "NAME", "VARCHAR2", "Y")]
        conn = _Conn(lambda sql, p: rows if "all_tab_columns" in sql else [])
        out = OracleDialect().fetch_columns(conn, "HR", ["EMP", "DEPT"])
        assert out == [("EMP", "ID", "NUMBER", False), ("EMP", "NAME", "VARCHAR2", True)]
        sql, params = conn.cursor_obj.calls[0]
        assert ":1" in sql and ":2" in sql and ":3" in sql  # owner + 2 table binds
        assert params == ("HR", "EMP", "DEPT")

    def test_fetch_constraints_uses_all_constraints(self):
        def responder(sql, p):
            if "constraint_type = 'p'" in sql:
                return [("EMP", "ID")]
            if "constraint_type = 'r'" in sql:
                return [("EMP", "DEPT_ID", "DEPT", "ID")]
            return []

        pk, fk = OracleDialect().fetch_constraints(_Conn(responder), "HR", ["EMP"])
        assert pk == {"EMP": ["ID"]}
        assert fk == {"EMP": [("DEPT_ID", "DEPT", "ID")]}


@pytest.mark.unit
class TestConnectLazyImports:
    """connect() must lazily import the right driver and pass sane args."""

    def _run_connect(self, dialect, module_name, attr_path):
        mod = MagicMock()
        with patch.dict(sys.modules, {module_name: mod}):
            dialect.connect(host="h.example.com", port=1, database="db", user="u", password="p", options={})
        return mod

    def test_postgres_uses_pg8000_with_tls(self):
        mod = self._run_connect(PostgresDialect(), "pg8000", "connect")
        assert mod.connect.call_args.kwargs["ssl_context"] is True
        assert mod.connect.call_args.kwargs["database"] == "db"

    def test_mysql_uses_pymysql_with_ssl_context(self):
        mod = self._run_connect(MySqlDialect(), "pymysql", "connect")
        assert "ssl" in mod.connect.call_args.kwargs

    def test_sqlserver_uses_pytds_server_kwarg(self):
        mod = self._run_connect(SqlServerDialect(), "pytds", "connect")
        assert mod.connect.call_args.kwargs["server"] == "h.example.com"

    def test_oracle_uses_oracledb_dsn(self):
        mod = self._run_connect(OracleDialect(), "oracledb", "connect")
        assert mod.connect.call_args.kwargs["dsn"] == "h.example.com:1/db"

    def test_snowflake_derives_account_and_passes_warehouse(self):
        snow = MagicMock()
        mod = snow.connector  # `snowflake.connector` attribute resolves to this
        with patch.dict(sys.modules, {"snowflake": snow, "snowflake.connector": mod}):
            SnowflakeDialect().connect(
                host="acme.snowflakecomputing.com",
                port=443,
                database="db",
                user="u",
                password="p",
                options={"warehouse": "WH", "role": "R"},
            )
        assert mod.connect.call_args.kwargs["account"] == "acme"
        assert mod.connect.call_args.kwargs["warehouse"] == "WH"
        assert mod.connect.call_args.kwargs["role"] == "R"

    def test_snowflake_invalid_host_raises(self):
        with pytest.raises(ValueError, match="Invalid Snowflake host"):
            SnowflakeDialect().connect(host="bad-host", port=443, database="db", user="u", password="p")


@pytest.mark.unit
def test_information_schema_dialect_is_base_for_relational_engines():
    for cls in (PostgresDialect, MySqlDialect, SqlServerDialect, SnowflakeDialect):
        assert issubclass(cls, InformationSchemaDialect)
    # Oracle is NOT information_schema-based.
    assert not issubclass(OracleDialect, InformationSchemaDialect)


@pytest.mark.unit
class TestFetchDistinctValues:
    """InformationSchemaDialect.fetch_distinct_values — categorical-value sampling.

    Two queries per column: a ``COUNT(*), COUNT(DISTINCT col)`` aggregate gates
    on cardinality + repetition, then ``SELECT DISTINCT`` pulls the values.
    """

    def _dialect(self):
        return PostgresDialect()

    @staticmethod
    def _responder(*, total, distinct, values):
        """Build a responder answering both the count aggregate and DISTINCT."""

        def responder(sql, params):
            if "count(" in sql:
                return [(total, distinct)]
            if "select distinct" in sql:
                return list(values)
            return []

        return responder

    def test_low_cardinality_repeated_column_returns_values(self):
        # 2 distinct values over 133 rows → enum-like → keep
        conn = _Conn(self._responder(total=37, distinct=2, values=[("RETURNED",), ("RETURN_REQUESTED",)]))
        out = self._dialect().fetch_distinct_values(conn, "coa_demo", "orders", ["return_state"], max_distinct=50)
        assert out == {"return_state": ["RETURNED", "RETURN_REQUESTED"]}

    def test_high_cardinality_column_skipped(self):
        # 5000 distinct > max_distinct(50) → skip before running DISTINCT
        conn = _Conn(self._responder(total=9000, distinct=5000, values=[]))
        out = self._dialect().fetch_distinct_values(conn, "s", "t", ["id"], max_distinct=50)
        assert "id" not in out

    def test_unique_identifier_in_small_table_skipped(self):
        # distinct == total (a unique id) — even at 37 rows (< max_distinct)
        # the repetition test rejects it, where a count-only cap would not.
        conn = _Conn(self._responder(total=37, distinct=37, values=[(f"C-{i}",) for i in range(37)]))
        out = self._dialect().fetch_distinct_values(conn, "s", "return_requests", ["customer_id"], max_distinct=50)
        assert "customer_id" not in out

    def test_empty_column_skipped(self):
        # all-null/empty column → 0 distinct → skip
        conn = _Conn(self._responder(total=0, distinct=0, values=[]))
        out = self._dialect().fetch_distinct_values(conn, "s", "t", ["c"], max_distinct=50)
        assert "c" not in out

    def test_low_repetition_freetext_skipped(self):
        # A near-unique free-text/name column (e.g. first names where each value
        # appears only ~twice: 30 distinct in 60 rows, ratio 2.0) is NOT a useful
        # enum. Passes the distinct<=50 cap but fails MIN_REPETITION_FACTOR=3
        # (60 < 3*30=90) → skipped.
        conn = _Conn(self._responder(total=60, distinct=30, values=[(f"name{i}",) for i in range(30)]))
        out = self._dialect().fetch_distinct_values(conn, "s", "customers", ["first_name"], max_distinct=50)
        assert "first_name" not in out

    def test_sampling_error_is_swallowed_per_column(self):
        def responder(sql, params):
            raise RuntimeError("permission denied")

        conn = _Conn(responder)
        # must not raise; returns empty for the failing column
        out = self._dialect().fetch_distinct_values(conn, "s", "t", ["c"], max_distinct=50)
        assert out == {}

    def test_long_freetext_values_dropped_by_shape_filter(self):
        # A column whose values are long free text (500 chars) is not an enum —
        # the value-shape backstop drops it even though counts would pass.
        conn = _Conn(self._responder(total=10, distinct=1, values=[("x" * 500,)]))
        out = self._dialect().fetch_distinct_values(conn, "s", "t", ["c"], max_distinct=50)
        assert "c" not in out

    def test_values_are_length_capped(self):
        # A per-value 100-char storage cap still applies to values that survive
        # the shape filter. Use many short values + one oversized one so the SET
        # average stays under the ≤40-char shape threshold (column kept), then
        # assert the oversized value was truncated to 100 chars.
        vals = [(f"S{i}",) for i in range(9)] + [("Z" * 250,)]
        conn = _Conn(self._responder(total=100, distinct=10, values=vals))
        out = self._dialect().fetch_distinct_values(conn, "s", "t", ["code"], max_distinct=50)
        assert "code" in out
        assert max(len(v) for v in out["code"]) == 100

    def test_postgres_quotes_identifiers_with_double_quotes(self):
        conn = _Conn(self._responder(total=10, distinct=2, values=[("A",), ("B",)]))
        PostgresDialect().fetch_distinct_values(conn, "s", "t", ["c"], max_distinct=50)
        sqls = [sql for sql, _ in conn.cursor_obj.calls]
        assert any('"c"' in sql and "LIMIT 50" in sql and "select distinct" in sql.lower() for sql in sqls)

    def test_mysql_quotes_identifiers_with_backticks(self):
        # MySQL reads "x" as a string literal under default sql_mode, so a
        # backtick-quoted identifier is required for sampling to work at all.
        conn = _Conn(self._responder(total=10, distinct=2, values=[("A",), ("B",)]))
        MySqlDialect().fetch_distinct_values(conn, "s", "t", ["c"], max_distinct=50)
        sqls = [sql for sql, _ in conn.cursor_obj.calls]
        assert all('"' not in sql for sql in sqls)
        assert any("`c`" in sql for sql in sqls)

    def test_sqlserver_uses_top_not_limit(self):
        # SQL Server has no LIMIT clause — it must use SELECT DISTINCT TOP N.
        conn = _Conn(self._responder(total=10, distinct=2, values=[("A",), ("B",)]))
        SqlServerDialect().fetch_distinct_values(conn, "s", "t", ["c"], max_distinct=50)
        distinct_sqls = [sql for sql, _ in conn.cursor_obj.calls if "select distinct" in sql.lower()]
        assert distinct_sqls
        assert all("LIMIT" not in sql and "TOP 50" in sql for sql in distinct_sqls)


@pytest.mark.unit
class TestEnumValueShapeFilter:
    """Enum detection is DATA-driven, never name-driven. The value-shape filter
    separates low-cardinality free-text from genuine categories by the shape of
    the sampled values — so a `category_name`/`payment_type` enum is kept while a
    short free-text column is dropped, both decided by their values not names."""

    def test_value_shape_rejects_freetext_keeps_codes(self):
        from coa_sources.database.connectors.dialects import values_look_categorical

        assert values_look_categorical(["RETURNED", "RETURN_REQUESTED"]) is True
        assert values_look_categorical(["amazon.de", "amazon.com"]) is True
        assert values_look_categorical(["StandardReturnSOP", "FraudReviewSOP"]) is True
        # free text: long / sentence-like
        assert values_look_categorical(["The box arrived but was empty. Please refund."]) is False
        assert (
            values_look_categorical(["Warranty active, defect confirmed", "Fraud review: multiple prior claims"])
            is False
        )
        assert values_look_categorical([]) is False

    def test_name_containing_columns_are_NOT_dropped_by_name(self):
        # A real enum whose NAME contains "name"/"type" (category_name,
        # payment_type) must be sampled — the guard is purely cardinality +
        # value shape, so short repeated codes are kept regardless of the name.
        def responder(sql, params):
            if "count(" in sql:
                return [(200, 4)]  # 4 distinct over 200 rows → enum-like
            if "select distinct" in sql:
                return [("Electronics",), ("Books",), ("Home",), ("Toys",)]
            return []

        conn = _Conn(responder)
        out = PostgresDialect().fetch_distinct_values(conn, "s", "products", ["category_name"], max_distinct=25)
        assert out == {"category_name": ["Electronics", "Books", "Home", "Toys"]}

    def test_low_cardinality_freetext_dropped_by_value_shape(self):
        # A column that passes the cardinality gate but holds sentence-like
        # values (e.g. a low-distinct "comment") is dropped by value shape.
        def responder(sql, params):
            if "count(" in sql:
                return [(100, 2)]
            if "select distinct" in sql:
                return [
                    ("The box arrived but was empty. Please refund.",),
                    ("Item was missing when I opened the package.",),
                ]
            return []

        conn = _Conn(responder)
        out = PostgresDialect().fetch_distinct_values(conn, "s", "t", ["note"], max_distinct=25)
        assert out == {}


@pytest.mark.unit
class TestRunRollsBackOnError:
    """_run() must roll back on a failed statement so a poisoned Postgres/Redshift
    transaction cannot make the next query fail with a misleading 25P02 (issue #129)."""

    class _AbortableCursor:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            # Model Postgres/Redshift: once the txn is aborted, every statement is
            # rejected with 25P02 until an explicit rollback.
            if self._conn.aborted:
                raise RuntimeError(
                    "25P02: current transaction is aborted, commands ignored until end of transaction block"
                )
            if "will_fail" in sql.lower():
                self._conn.aborted = True
                raise RuntimeError("42883: catalog query failed")
            self._rows = [(1,)]

        def fetchall(self):
            return self._rows

        def close(self):
            pass

    class _AbortableConn:
        """pg8000-style: autocommit off; an errored statement poisons the session
        until rollback() is called."""

        def __init__(self, rollback_raises=False):
            self.aborted = False
            self.rollbacks = 0
            self._rollback_raises = rollback_raises

        def cursor(self):
            return TestRunRollsBackOnError._AbortableCursor(self)

        def rollback(self):
            self.rollbacks += 1
            if self._rollback_raises:
                raise RuntimeError("connection reset during rollback")
            self.aborted = False

    def test_next_query_succeeds_after_a_swallowed_failure(self):
        from coa_sources.database.connectors.dialects import _run

        conn = self._AbortableConn()
        assert _run(conn, "SELECT nspname FROM pg_catalog.pg_namespace") == [(1,)]

        # A best-effort catalog query errors and is swallowed by a _safe_* caller.
        with pytest.raises(RuntimeError, match="42883"):
            _run(conn, "SELECT will_fail", ("public",))

        # _run must have rolled back, so the NEXT query on the same connection works.
        assert conn.rollbacks == 1
        assert conn.aborted is False
        assert _run(conn, "SELECT relname FROM pg_catalog.pg_class WHERE relnamespace = %s", ("public",)) == [(1,)]

    def test_reraises_original_error_even_if_rollback_fails(self):
        from coa_sources.database.connectors.dialects import _run

        conn = self._AbortableConn(rollback_raises=True)
        # The original catalog error must surface, not the rollback failure.
        with pytest.raises(RuntimeError, match="42883"):
            _run(conn, "SELECT will_fail")
        assert conn.rollbacks == 1

    def test_no_rollback_on_success(self):
        from coa_sources.database.connectors.dialects import _run

        conn = self._AbortableConn()
        assert _run(conn, "SELECT 1") == [(1,)]
        assert conn.rollbacks == 0
