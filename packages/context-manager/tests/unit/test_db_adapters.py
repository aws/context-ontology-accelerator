# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the per-engine direct-JDBC adapters.

Each engine's connection/session/dialect/limit behavior is co-located in its
adapter (clients/db_adapters/*). These tests assert:
- the registry resolves the right adapter and fail-safes to PostgreSQL,
- inject_limit emits LIMIT (PG/Redshift/MySQL) vs TOP (SQL Server),
- the SSL context is permissive, the Redshift dialect skips the probe,
- PostgreSQL issues SET search_path + SET LOCAL statement_timeout, MySQL issues
  SET SESSION max_execution_time and NO search_path.
"""

from __future__ import annotations

import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestAdapterRegistry:
    def test_get_adapter_resolves_each_engine(self):
        from coa_serve.clients.db_adapters import (
            MySQLAdapter,
            PostgresAdapter,
            RedshiftAdapter,
            SqlServerAdapter,
            get_adapter,
        )

        assert isinstance(get_adapter("POSTGRESQL"), PostgresAdapter)
        assert isinstance(get_adapter("REDSHIFT"), RedshiftAdapter)
        assert isinstance(get_adapter("MYSQL"), MySQLAdapter)
        assert isinstance(get_adapter("SQLSERVER"), SqlServerAdapter)

    def test_get_adapter_is_case_insensitive(self):
        from coa_serve.clients.db_adapters import MySQLAdapter, get_adapter

        assert isinstance(get_adapter("mysql"), MySQLAdapter)

    def test_get_adapter_fail_safe_to_postgres(self):
        """Unknown/missing engine falls back to PostgreSQL (historical default)."""
        from coa_serve.clients.db_adapters import PostgresAdapter, get_adapter

        for value in ("SNOWFLAKE", "ORACLE", "", None):
            assert isinstance(get_adapter(value), PostgresAdapter)

    def test_is_direct_query_engine_true_for_adapted_engines(self):
        from coa_serve.clients.db_adapters import is_direct_query_engine

        for engine in ("POSTGRESQL", "REDSHIFT", "MYSQL", "SQLSERVER", "mysql"):
            assert is_direct_query_engine(engine)

    def test_is_direct_query_engine_false_for_no_adapter_engines(self):
        """Engines without a direct adapter (Snowflake, Oracle) and unknown tokens
        must not take the direct route — get_adapter would hand back the PostgreSQL
        fail-safe and we would transpile Trino->postgres and run against the wrong
        engine.
        """
        from coa_serve.clients.db_adapters import is_direct_query_engine

        for engine in ("SNOWFLAKE", "ORACLE", "DB2", "", None):
            assert not is_direct_query_engine(engine)

    def test_dialect_and_port_per_engine(self):
        from coa_serve.clients.db_adapters import get_adapter

        assert (get_adapter("POSTGRESQL").sqlglot_dialect, get_adapter("POSTGRESQL").default_port) == ("postgres", 5432)
        assert (get_adapter("REDSHIFT").sqlglot_dialect, get_adapter("REDSHIFT").default_port) == ("redshift", 5439)
        assert (get_adapter("MYSQL").sqlglot_dialect, get_adapter("MYSQL").default_port) == ("mysql", 3306)
        assert (get_adapter("SQLSERVER").sqlglot_dialect, get_adapter("SQLSERVER").default_port) == ("tsql", 1433)


@pytest.mark.unit
class TestInjectLimit:
    @pytest.mark.parametrize("engine", ["POSTGRESQL", "REDSHIFT", "MYSQL"])
    def test_limit_engines_add_limit(self, engine):
        from coa_serve.clients.db_adapters import get_adapter

        adapter = get_adapter(engine)
        out = adapter.inject_limit("SELECT a FROM t", 100)
        assert "LIMIT 100" in out
        assert "TOP" not in out.upper()

    @pytest.mark.parametrize("engine", ["POSTGRESQL", "REDSHIFT", "MYSQL"])
    def test_limit_engines_cap_excessive(self, engine):
        from coa_serve.clients.db_adapters import get_adapter

        out = get_adapter(engine).inject_limit("SELECT a FROM t LIMIT 10000", 100)
        assert "LIMIT 100" in out
        assert "10000" not in out

    def test_limit_preserves_existing_within_cap(self):
        from coa_serve.clients.db_adapters import get_adapter

        sql = "SELECT a FROM t LIMIT 50"
        assert get_adapter("POSTGRESQL").inject_limit(sql, 100) == sql

    def test_sqlserver_uses_top_not_limit(self):
        from coa_serve.clients.db_adapters import get_adapter

        adapter = get_adapter("SQLSERVER")
        out = adapter.inject_limit("SELECT a FROM t", 100)
        assert "TOP 100" in out
        assert "LIMIT" not in out.upper()

    def test_sqlserver_caps_excessive_with_top(self):
        from coa_serve.clients.db_adapters import get_adapter

        out = get_adapter("SQLSERVER").inject_limit("SELECT a FROM t LIMIT 10000", 100)
        assert "TOP 100" in out
        assert "LIMIT" not in out.upper()
        assert "10000" not in out

    def test_sqlserver_fallback_uses_top_on_unparseable(self):
        """A parse failure must NOT emit a literal LIMIT for SQL Server."""
        from coa_serve.clients.db_adapters import get_adapter

        out = get_adapter("SQLSERVER").inject_limit("SELECT ??? FROM foo", 50)
        assert "TOP 50" in out
        assert "LIMIT" not in out.upper()

    @pytest.mark.parametrize("engine", ["POSTGRESQL", "REDSHIFT", "MYSQL"])
    def test_limit_engines_fallback_on_unparseable(self, engine):
        """Every LIMIT-idiom engine falls back to a literal LIMIT on a parse failure."""
        from coa_serve.clients.db_adapters import get_adapter

        out = get_adapter(engine).inject_limit("SELECT ??? FROM foo", 50)
        assert "LIMIT 50" in out
        assert "TOP" not in out.upper()

    def test_inject_limit_reraises_non_sqlglot_errors(self):
        """A genuine bug (not a parse failure) must surface, not fall into the
        string fallback — inject_limit catches SqlglotError specifically."""
        from unittest.mock import patch

        from coa_serve.clients.db_adapters import get_adapter

        parse_target = "coa_serve.clients.db_adapters.base.sqlglot.parse_one"
        with patch(parse_target, side_effect=RuntimeError("bug")), pytest.raises(RuntimeError, match="bug"):
            get_adapter("POSTGRESQL").inject_limit("SELECT a FROM t", 50)

    def test_redshift_dialect_sql_round_trips(self):
        """DATEDIFF(DAY, …) is Redshift syntax; the AST path (not string fallback)
        must parse+render it with the redshift dialect."""
        from coa_serve.clients.db_adapters import get_adapter

        sql = "SELECT DATEDIFF(DAY, MIN(order_date), MAX(order_date)) AS span FROM wi766.orders"
        out = get_adapter("REDSHIFT").inject_limit(sql, 100)
        assert "LIMIT 100" in out
        assert "DATEDIFF" in out.upper()

    @pytest.mark.parametrize("engine", ["POSTGRESQL", "REDSHIFT", "MYSQL", "SQLSERVER"])
    def test_nested_subquery_limit_is_not_the_cap(self, engine):
        """CRITICAL: a LIMIT inside a subquery must NOT be treated as the statement cap.

        ``find(exp.Limit)`` returns the first Limit anywhere (the INNER one), which
        would leave the OUTER result unbounded. We must cap at the ROOT: the outer
        query gets its own LIMIT/TOP and the inner LIMIT is untouched.
        """
        from coa_serve.clients.db_adapters import get_adapter

        out = get_adapter(engine).inject_limit("SELECT * FROM (SELECT a FROM t LIMIT 999999) z", 100)
        # inner limit preserved
        assert "999999" in out
        # a top-level cap of 100 was applied (LIMIT for most, TOP for tsql)
        if engine == "SQLSERVER":
            assert "TOP 100" in out
        else:
            # two LIMITs now present: inner 999999 + outer 100
            assert out.rstrip().endswith("LIMIT 100")


@pytest.mark.unit
class TestSslContext:
    def test_ssl_context_is_permissive(self):
        """Transport encryption without cert/hostname pinning (auth is creds + VPC/SG)."""
        from coa_serve.clients.db_adapters.base import SSL_CONTEXT

        assert isinstance(SSL_CONTEXT, ssl.SSLContext)
        assert SSL_CONTEXT.check_hostname is False
        assert SSL_CONTEXT.verify_mode == ssl.CERT_NONE


@pytest.mark.unit
class TestRedshiftDialect:
    def test_redshift_asyncpg_dialect_skips_standard_conforming_probe(self):
        """The Redshift asyncpg dialect must NOT run ``show standard_conforming_strings``."""
        from coa_serve.clients.db_adapters.postgres import PGDialect_asyncpg_redshift

        dialect = PGDialect_asyncpg_redshift()
        conn = MagicMock()
        conn.exec_driver_sql.side_effect = AssertionError("standard_conforming_strings probe must not run")
        dialect._set_backslash_escapes(conn)
        conn.exec_driver_sql.assert_not_called()
        assert dialect._backslash_escapes is False


def _make_pg_creds(engine_type="POSTGRESQL", host="pg.example.com", port=5432, schema="public"):
    from coa_serve.clients.db_adapters import DBCredentials

    return DBCredentials(
        username="u", password="p", host=host, port=port, database="demo", schema=schema, engine_type=engine_type
    )


@pytest.mark.unit
class TestPostgresAdapter:
    @patch("coa_serve.clients.db_adapters.postgres.create_async_engine")
    async def test_open_connection_passes_ssl_and_search_path(self, mock_create_engine):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter

        await PostgresAdapter().open_connection(_make_pg_creds(schema="public"))
        kwargs = mock_create_engine.call_args.kwargs
        connect_args = kwargs["connect_args"]
        assert isinstance(connect_args["ssl"], ssl.SSLContext)
        assert connect_args["server_settings"]["search_path"] == "public"
        # Bounded connect + capped pool (no unbounded overflow) + pre-ping.
        assert connect_args["timeout"] >= 1
        assert kwargs["max_overflow"] == 0
        assert kwargs["pool_pre_ping"] is True
        url = mock_create_engine.call_args.args[0]
        assert url.drivername == "postgresql+asyncpg"

    async def test_run_rejects_out_of_range_timeout(self):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter

        executed = self._capture_engine()
        for bad in (0, 400):
            with pytest.raises(ValueError, match="out of range"):
                await PostgresAdapter().run(executed.engine, "SELECT 1", None, bad, schema="")

    @patch("coa_serve.clients.db_adapters.postgres.create_async_engine")
    async def test_redshift_uses_probe_skipping_driver(self, mock_create_engine):
        from coa_serve.clients.db_adapters.postgres import _REDSHIFT_ASYNCPG_DRIVER, RedshiftAdapter

        await RedshiftAdapter().open_connection(_make_pg_creds(engine_type="REDSHIFT", port=5439))
        url = mock_create_engine.call_args.args[0]
        assert url.drivername == _REDSHIFT_ASYNCPG_DRIVER

    async def test_run_sets_search_path_and_timeout(self):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter

        executed = self._capture_engine()
        await PostgresAdapter().run(executed.engine, "SELECT 1", None, 10, schema="analytics")
        assert any("SET LOCAL statement_timeout = 10000" in s for s in executed.sql)
        assert "SET search_path TO analytics" in executed.sql

    async def test_run_multi_schema_search_path(self):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter

        executed = self._capture_engine()
        await PostgresAdapter().run(executed.engine, "SELECT 1", None, 10, schema="public, sales")
        assert "SET search_path TO public, sales" in executed.sql

    async def test_run_skips_search_path_when_no_schema(self):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter

        executed = self._capture_engine()
        await PostgresAdapter().run(executed.engine, "SELECT 1", None, 10, schema="")
        assert not any("search_path" in s for s in executed.sql)

    async def test_run_rejects_malicious_schema(self):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter

        executed = self._capture_engine()
        with pytest.raises(ValueError, match="invalid schema identifier"):
            await PostgresAdapter().run(
                executed.engine,
                "SELECT customer_data FROM orders",
                None,
                10,
                schema="public; DROP TABLE users --",
            )
        executed.engine.begin.assert_not_called()
        assert not any("SELECT customer_data FROM orders" in sql for sql in executed.sql)
        assert not any("DROP TABLE" in s for s in executed.sql)

    @pytest.mark.parametrize("schema", ["analytics, bad-name", "analytics,,public"])
    async def test_run_rejects_entire_search_path_when_any_schema_is_invalid(self, schema):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter

        executed = self._capture_engine()
        with pytest.raises(ValueError, match="invalid schema identifier"):
            await PostgresAdapter().run(
                executed.engine,
                "SELECT customer_data FROM orders",
                None,
                10,
                schema=schema,
            )
        executed.engine.begin.assert_not_called()
        assert executed.sql == []

    async def test_run_search_path_failure_is_raised_before_customer_query(self):
        from coa_serve.clients.db_adapters.postgres import PostgresAdapter
        from sqlalchemy.exc import ProgrammingError

        executed = self._capture_engine(
            fail_on="search_path",
            exc=ProgrammingError("SET failed", {}, Exception("boom")),
            fail_once=True,
        )
        with pytest.raises(ProgrammingError, match="SET failed"):
            await PostgresAdapter().run(
                executed.engine,
                "SELECT customer_data FROM orders",
                None,
                10,
                schema="analytics",
            )
        assert not any("SELECT customer_data FROM orders" in sql for sql in executed.sql)
        executed.begin_ctx.__aexit__.assert_awaited_once()
        assert executed.begin_ctx.__aexit__.await_args.args[0] is ProgrammingError

        rows, columns = await PostgresAdapter().run(
            executed.engine,
            "SELECT customer_data FROM orders",
            None,
            10,
            schema="analytics",
        )
        assert rows == []
        assert columns == []
        assert executed.engine.begin.call_count == 2
        assert sum("SELECT customer_data FROM orders" in sql for sql in executed.sql) == 1

    @staticmethod
    def _capture_engine(
        fail_on: str | None = None,
        exc: Exception | None = None,
        *,
        fail_once: bool = False,
    ):
        class _Capture:
            def __init__(self):
                self.sql: list[str] = []
                self.failures_remaining = 1 if fail_once else None
                conn = AsyncMock()

                async def _exec(stmt, *args, **kwargs):
                    s = str(stmt)
                    self.sql.append(s)
                    if (
                        fail_on
                        and fail_on in s
                        and exc is not None
                        and (self.failures_remaining is None or self.failures_remaining > 0)
                    ):
                        if self.failures_remaining is not None:
                            self.failures_remaining -= 1
                        raise exc
                    result = MagicMock()
                    result.fetchall.return_value = []
                    result.keys.return_value = []
                    return result

                conn.execute.side_effect = _exec
                engine = MagicMock()
                begin_ctx = AsyncMock()
                begin_ctx.__aenter__.return_value = conn
                begin_ctx.__aexit__.return_value = False
                engine.begin.return_value = begin_ctx
                self.engine = engine
                self.begin_ctx = begin_ctx

        return _Capture()


@pytest.mark.unit
class TestMySQLAdapter:
    @patch("coa_serve.clients.db_adapters.mysql.create_async_engine")
    async def test_open_connection_uses_aiomysql_and_ssl_no_search_path(self, mock_create_engine):
        from coa_serve.clients.db_adapters.mysql import MySQLAdapter

        await MySQLAdapter().open_connection(_make_pg_creds(engine_type="MYSQL", port=3306))
        url = mock_create_engine.call_args.args[0]
        assert url.drivername == "mysql+aiomysql"
        kwargs = mock_create_engine.call_args.kwargs
        connect_args = kwargs["connect_args"]
        assert isinstance(connect_args["ssl"], ssl.SSLContext)
        # Bounded connect so an unreachable host can't hang forever.
        assert connect_args["connect_timeout"] >= 1
        # Capped pool (no unbounded overflow) + pre-ping.
        assert kwargs["max_overflow"] == 0
        assert kwargs["pool_pre_ping"] is True
        # MySQL has no search_path concept — server_settings must NOT be set.
        assert "server_settings" not in connect_args

    async def test_run_sets_max_execution_time_and_no_search_path(self):
        from coa_serve.clients.db_adapters.mysql import MySQLAdapter

        sqls: list[str] = []
        conn = AsyncMock()

        async def _exec(stmt, *args, **kwargs):
            sqls.append(str(stmt))
            result = MagicMock()
            result.fetchall.return_value = []
            result.keys.return_value = []
            return result

        conn.execute.side_effect = _exec
        engine = MagicMock()
        begin_ctx = AsyncMock()
        begin_ctx.__aenter__.return_value = conn
        begin_ctx.__aexit__.return_value = False
        engine.begin.return_value = begin_ctx

        await MySQLAdapter().run(engine, "SELECT 1", None, 10, schema="ignored")
        assert any("SET SESSION max_execution_time = 10000" in s for s in sqls)
        assert not any("search_path" in s for s in sqls)

    async def test_run_falls_back_to_maria_statement_time(self):
        """On MariaDB (rejects max_execution_time) fall back to max_statement_time (seconds)."""
        from coa_serve.clients.db_adapters.mysql import MySQLAdapter
        from sqlalchemy.exc import OperationalError

        sqls: list[str] = []
        conn = AsyncMock()

        async def _exec(stmt, *args, **kwargs):
            s = str(stmt)
            sqls.append(s)
            if "max_execution_time" in s:
                raise OperationalError("unknown var", {}, Exception("maria"))
            result = MagicMock()
            result.fetchall.return_value = []
            result.keys.return_value = []
            return result

        conn.execute.side_effect = _exec
        engine = MagicMock()
        begin_ctx = AsyncMock()
        begin_ctx.__aenter__.return_value = conn
        begin_ctx.__aexit__.return_value = False
        engine.begin.return_value = begin_ctx

        await MySQLAdapter().run(engine, "SELECT 1", None, 10, schema="")
        # tried MySQL form, then the MariaDB form (10000ms -> 10.0s)
        assert any("max_execution_time" in s for s in sqls)
        assert any("max_statement_time = 10.0" in s for s in sqls)

    async def test_run_logs_when_both_timeout_forms_fail(self):
        """When neither max_execution_time NOR max_statement_time can be set, the
        query still runs but a mysql_statement_timeout_unset warning is emitted
        (the client-side wait_for is the last-resort bound)."""
        from unittest.mock import patch

        from coa_serve.clients.db_adapters.mysql import MySQLAdapter
        from sqlalchemy.exc import OperationalError

        conn = AsyncMock()

        async def _exec(stmt, *args, **kwargs):
            s = str(stmt)
            if "max_execution_time" in s or "max_statement_time" in s:
                raise OperationalError("no such var", {}, Exception("boom"))
            result = MagicMock()
            result.fetchall.return_value = []
            result.keys.return_value = []
            return result

        conn.execute.side_effect = _exec
        engine = MagicMock()
        begin_ctx = AsyncMock()
        begin_ctx.__aenter__.return_value = conn
        begin_ctx.__aexit__.return_value = False
        engine.begin.return_value = begin_ctx

        with patch("coa_serve.clients.db_adapters.mysql.logger") as mock_logger:
            rows, columns = await MySQLAdapter().run(engine, "SELECT 1", None, 10, schema="")
        assert rows == []
        warn_events = [c.args[0] for c in mock_logger.warning.call_args_list]
        assert "mysql_statement_timeout_unset" in warn_events

    async def test_run_rejects_out_of_range_timeout(self):
        from coa_serve.clients.db_adapters.mysql import MySQLAdapter

        engine = MagicMock()
        for bad in (0, 400):
            with pytest.raises(ValueError, match="out of range"):
                await MySQLAdapter().run(engine, "SELECT 1", None, bad, schema="")

    async def test_run_client_deadline_timeout_propagates(self):
        """If the transaction outlives the client-side wait_for deadline, the query
        raises asyncio.TimeoutError (the last-resort bound when a socket stalls past
        the server-side timeout) rather than hanging forever."""
        import asyncio

        from coa_serve.clients.db_adapters.mysql import MySQLAdapter

        engine = MagicMock()

        async def _never_returns(*args, **kwargs):
            await asyncio.sleep(3600)

        # Patch the inner txn to hang and force wait_for to fire, asserting run()
        # surfaces the TimeoutError (rather than awaiting the stalled txn forever).
        with (
            patch.object(MySQLAdapter, "_run_txn", side_effect=_never_returns),
            patch(
                "coa_serve.clients.db_adapters.mysql.asyncio.wait_for",
                side_effect=asyncio.TimeoutError,
            ),
            pytest.raises(asyncio.TimeoutError),
        ):
            await MySQLAdapter().run(engine, "SELECT 1", None, 1, schema="")


@pytest.mark.unit
class TestSqlServerAdapter:
    @staticmethod
    def _fake_pytds(cursor):
        """Return a fake ``pytds`` module whose connect() yields a conn with `cursor`."""
        connection = MagicMock()
        connection.cursor.return_value = cursor
        fake = MagicMock()
        fake.connect.return_value = connection
        return fake, connection

    async def test_run_connects_per_query_and_maps_rows(self):
        import sys

        from coa_serve.clients.db_adapters.base import DBCredentials
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        cursor = MagicMock()
        cursor.description = [("id",), ("name",)]
        cursor.fetchall.return_value = [(1, "acme")]
        fake, connection = self._fake_pytds(cursor)

        creds = DBCredentials(username="u", password="p", host="h", port=1433, database="db", engine_type="SQLSERVER")
        with patch.dict(sys.modules, {"pytds": fake}):
            handle = await SqlServerAdapter().open_connection(creds)
            # open_connection returns the creds (no live socket) — nothing connected yet.
            fake.connect.assert_not_called()
            rows, columns = await SqlServerAdapter().run(handle, "SELECT TOP 1 id, name FROM t", None, 10, schema="")

        assert columns == ["id", "name"]
        assert rows == [{"id": 1, "name": "acme"}]
        # Fresh connect per query; connection AND cursor closed (no leak).
        fake.connect.assert_called_once()
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    async def test_run_rejects_bind_params(self):
        """The pytds path uses %s paramstyle, not SQLAlchemy :name — reject params loudly."""
        from coa_serve.clients.db_adapters.base import DBCredentials
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        creds = DBCredentials(username="u", password="p", host="h", port=1433, database="db")
        with pytest.raises(ValueError, match="does not support bind params"):
            await SqlServerAdapter().run(creds, "SELECT 1", {"x": 1}, 10, schema="")

    async def test_run_closes_connection_and_cursor_on_error(self):
        """A driver error inside execute must still close the cursor AND connection (no leak)."""
        import sys

        from coa_serve.clients.db_adapters.base import DBCredentials
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        cursor = MagicMock()
        cursor.execute.side_effect = RuntimeError("boom")
        fake, connection = self._fake_pytds(cursor)
        creds = DBCredentials(username="u", password="p", host="h", port=1433, database="db")

        with patch.dict(sys.modules, {"pytds": fake}), pytest.raises(RuntimeError, match="boom"):
            await SqlServerAdapter().run(creds, "SELECT 1", None, 10, schema="")
        cursor.close.assert_called_once()
        connection.close.assert_called_once()

    async def test_run_passes_query_and_login_timeouts_to_connect(self):
        """Driver-side timeouts must be set so the server aborts a slow query/connect."""
        import sys

        from coa_serve.clients.db_adapters.base import DBCredentials
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        cursor = MagicMock()
        cursor.description = []
        cursor.fetchall.return_value = []
        fake, _ = self._fake_pytds(cursor)
        creds = DBCredentials(username="u", password="p", host="h", port=1433, database="db")

        with patch.dict(sys.modules, {"pytds": fake}):
            await SqlServerAdapter().run(creds, "SELECT 1", None, 25, schema="")
        kwargs = fake.connect.call_args.kwargs
        assert kwargs["timeout"] == 25  # per-query server-side abort
        assert kwargs["login_timeout"] >= 1  # bounded connect

    async def test_close_is_noop(self):
        from coa_serve.clients.db_adapters.base import DBCredentials
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        creds = DBCredentials(username="u", password="p", host="h", port=1433, database="db")
        assert await SqlServerAdapter().close(creds) is None  # no exception, nothing to dispose

    async def test_run_outer_timeout_propagates(self):
        """The outer asyncio.wait_for bound must raise TimeoutError when connect+query
        exceeds it — connect-per-query means the abandoned thread can't corrupt a later
        query, and the coroutine is freed rather than hanging on a stalled socket."""
        import asyncio

        from coa_serve.clients.db_adapters.base import DBCredentials
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        creds = DBCredentials(username="u", password="p", host="h", port=1433, database="db")
        with (
            patch(
                "coa_serve.clients.db_adapters.sqlserver.asyncio.wait_for",
                side_effect=asyncio.TimeoutError,
            ),
            pytest.raises(asyncio.TimeoutError),
        ):
            await SqlServerAdapter().run(creds, "SELECT 1", None, 5, schema="")

    async def test_run_strict_zip_raises_on_column_row_mismatch(self):
        """strict=True: a driver returning a row wider/narrower than cur.description
        surfaces a ValueError instead of silently truncating (data-corruption guard)."""
        import sys

        from coa_serve.clients.db_adapters.base import DBCredentials
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        cursor = MagicMock()
        cursor.description = [("id",), ("name",)]  # 2 columns
        cursor.fetchall.return_value = [(1, "acme", "EXTRA")]  # 3 values — mismatch
        fake, _ = self._fake_pytds(cursor)
        creds = DBCredentials(username="u", password="p", host="h", port=1433, database="db")

        with patch.dict(sys.modules, {"pytds": fake}), pytest.raises(ValueError):
            await SqlServerAdapter().run(creds, "SELECT id, name FROM t", None, 10, schema="")

    def test_top_fallback_preserves_distinct_order(self):
        """The parse-failure fallback must emit ``SELECT DISTINCT TOP N`` (valid T-SQL),
        NOT ``SELECT TOP N DISTINCT`` (invalid)."""
        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        adapter = SqlServerAdapter()
        # An unparseable tail forces the string fallback.
        assert adapter._top_fallback("SELECT DISTINCT a FROM t WHERE x = ???", 100) == (
            "SELECT DISTINCT TOP 100 a FROM t WHERE x = ???"
        )
        # The ALL quantifier must likewise precede TOP.
        assert adapter._top_fallback("SELECT ALL a FROM t WHERE x = ???", 100) == (
            "SELECT ALL TOP 100 a FROM t WHERE x = ???"
        )
        assert adapter._top_fallback("SELECT a FROM t WHERE x = ???", 100) == "SELECT TOP 100 a FROM t WHERE x = ???"

    def test_top_fallback_logs_and_returns_unchanged_for_uncappable_shape(self):
        """A shape we can't safely rewrite (leading WITH) is returned unchanged AND
        logged, so the uncapped path is diagnosable rather than a silent memory risk."""
        from unittest.mock import patch

        from coa_serve.clients.db_adapters.sqlserver import SqlServerAdapter

        adapter = SqlServerAdapter()
        with patch("coa_serve.clients.db_adapters.sqlserver.logger") as mock_logger:
            out = adapter._top_fallback("WITH c AS (SELECT 1) SELECT ???", 100)
        assert out.startswith("WITH c")
        assert "TOP" not in out.upper()
        warn_events = [c.args[0] for c in mock_logger.warning.call_args_list]
        assert "sqlserver_top_fallback_uncapped" in warn_events
