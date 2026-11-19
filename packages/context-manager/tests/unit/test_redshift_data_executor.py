# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for RedshiftDataAPIExecutor."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlglot
import sqlglot.errors
import structlog.testing
from coa_serve.clients.redshift_data import (
    RedshiftDataAPIExecutor,
    RedshiftQueryError,
    RedshiftTimeoutError,
)
from coa_serve.clients.sources_registry import SQLNamespaceScope


def _make_executor(client: MagicMock) -> RedshiftDataAPIExecutor:
    reg = MagicMock()
    ex = RedshiftDataAPIExecutor.__new__(RedshiftDataAPIExecutor)
    # Bypass __init__ (which builds a real boto3 client); inject the stub directly.
    ex._region = "us-east-1"
    ex._sources = reg
    ex._database = "dev"
    ex._client = client
    return ex


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


@pytest.mark.unit
class TestAwsDataCatalogRewrite:
    """The rewriter must produce awsdatacatalog 3-part table names."""

    def test_bare_table_gets_catalog_and_db(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog("SELECT id FROM claims", "insurance_lake")
        n = _norm(out)
        assert "awsdatacatalog" in n
        assert "insurance_lake" in n
        assert "claims" in n

    def test_two_part_db_table_gets_catalog_prefix(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog("SELECT id FROM insurance_lake.claims", "default_db")
        n = _norm(out)
        assert "awsdatacatalog" in n and "insurance_lake" in n and "claims" in n
        assert "default_db" not in n

    def test_three_part_catalog_is_normalized_to_awsdatacatalog(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            'SELECT id FROM "AwsDataCatalog"."insurance_lake"."claims"', "ignored"
        )
        n = _norm(out)
        assert n.count("awsdatacatalog") == 1
        assert "insurance_lake" in n and "claims" in n

    def test_join_rewrites_all_tables(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            "SELECT a.id FROM claims a JOIN tbl_clm_hdr b ON a.id = b.id", "insurance_lake"
        )
        n = _norm(out)
        assert n.count("awsdatacatalog") == 2  # both tables qualified

    def test_unparseable_sql_raises_rewrite_error(self):
        # Never hand Redshift SQL the rewriter could not qualify.
        with pytest.raises(RedshiftQueryError, match="Unable to rewrite SQL for Redshift"):
            RedshiftDataAPIExecutor._qualify_awsdatacatalog(")(*&^ not sql", "db")

    def test_bare_table_without_default_database_raises(self):
        # Left bare, it would resolve through Redshift's search_path instead.
        with pytest.raises(RedshiftQueryError, match="Glue database"):
            RedshiftDataAPIExecutor._qualify_awsdatacatalog("SELECT * FROM claims", "")

    def test_bare_name_shadowed_by_nested_cte_is_qualified(self):
        """A nested subquery's CTE does not hide a same-named real table outside its
        scope: the outer reference must be qualified, not left to search_path."""
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            "SELECT * FROM (WITH sys_query_history AS (SELECT 1 AS a) SELECT a FROM sys_query_history) t "
            "CROSS JOIN sys_query_history q",
            "tenant_a_db",
        )
        assert out.lower().count('"awsdatacatalog"."tenant_a_db"."sys_query_history"') == 1
        # The inner reference is still the CTE.
        assert "from sys_query_history)" in _norm(out)

    def test_cte_reference_in_nested_subquery_is_not_qualified(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            "WITH a AS (SELECT id FROM claims) SELECT * FROM (SELECT id FROM a) s", "insurance_lake"
        )
        assert '"awsdatacatalog"."insurance_lake"."claims"' in out.lower()
        assert '"awsdatacatalog"."insurance_lake"."a"' not in out.lower()

    def test_cte_reference_is_not_qualified_as_a_glue_table(self):
        """A CTE reference is not qualified as a Glue table."""
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            "WITH a AS (SELECT id FROM claims), b AS (SELECT id FROM a) SELECT * FROM b",
            "insurance_lake",
        )
        n = _norm(out)
        assert "awsdatacatalog" in n and "insurance_lake" in n and "claims" in n
        assert '"awsdatacatalog"."insurance_lake"."a"' not in out.lower()
        assert '"awsdatacatalog"."insurance_lake"."b"' not in out.lower()

    def test_cte_with_multiple_real_tables_still_qualifies_them(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            "WITH t AS (SELECT * FROM claims JOIN tbl_clm_hdr ON claims.id = tbl_clm_hdr.id) SELECT * FROM t",
            "insurance_lake",
        )
        n = _norm(out)
        assert n.count("awsdatacatalog") == 2


@pytest.mark.unit
class TestFailureLogsOmitSql:
    """CWE-532: failure logs must not carry the SQL text, which
    can contain caller-supplied literal values (potential PII)."""

    _SQL = "SELECT * FROM claims WHERE email = 'alice@example.com'"

    @classmethod
    def _boom(cls, *_args, **_kwargs):
        # sqlglot error messages echo a snippet of the offending SQL.
        raise sqlglot.errors.ParseError(f"Invalid expression. Line 1.\n  {cls._SQL}")

    def test_transpile_failure_logs_length_not_sql(self, monkeypatch):
        monkeypatch.setattr("coa_serve.clients.redshift_data.sqlglot.transpile", self._boom)
        with structlog.testing.capture_logs() as logs:
            assert RedshiftDataAPIExecutor._transpile_to_redshift(self._SQL) == self._SQL
        event = next(log for log in logs if log["event"] == "redshift_trino_transpile_failed")
        assert "sql" not in event
        assert event["sql_len"] == len(self._SQL)
        assert event["error_type"] == "ParseError"
        assert "alice@example.com" not in repr(logs)

    def test_rewrite_parse_failure_logs_length_not_sql(self, monkeypatch):
        monkeypatch.setattr("coa_serve.clients.redshift_data.sqlglot.parse_one", self._boom)
        with structlog.testing.capture_logs() as logs, pytest.raises(RedshiftQueryError) as raised:
            RedshiftDataAPIExecutor._qualify_awsdatacatalog(self._SQL, "db")
        assert "alice@example.com" not in str(raised.value)
        event = next(log for log in logs if log["event"] == "redshift_awsdatacatalog_rewrite_parse_failed")
        assert "sql" not in event
        assert event["sql_len"] == len(self._SQL)
        assert event["error_type"] == "ParseError"
        assert "alice@example.com" not in repr(logs)

    def test_rewrite_render_failure_logs_type_not_message(self, monkeypatch):
        monkeypatch.setattr(sqlglot.exp.Expression, "sql", self._boom)
        with structlog.testing.capture_logs() as logs, pytest.raises(RedshiftQueryError) as raised:
            RedshiftDataAPIExecutor._qualify_awsdatacatalog(self._SQL, "db")
        assert "alice@example.com" not in str(raised.value)
        event = next(log for log in logs if log["event"] == "redshift_awsdatacatalog_rewrite_render_failed")
        assert "error" not in event
        assert event["error_type"] == "ParseError"
        assert "alice@example.com" not in repr(logs)


@pytest.mark.unit
class TestNonSqlglotErrorsPropagate:
    """Only sqlglot failures fall back to the input SQL; a genuine bug must surface."""

    @staticmethod
    def _bug(*_args, **_kwargs):
        raise AttributeError("bug")

    def test_transpile_non_sqlglot_error_propagates(self, monkeypatch):
        monkeypatch.setattr("coa_serve.clients.redshift_data.sqlglot.transpile", self._bug)
        with pytest.raises(AttributeError):
            RedshiftDataAPIExecutor._transpile_to_redshift("SELECT id FROM claims")

    def test_rewrite_parse_non_sqlglot_error_propagates(self, monkeypatch):
        monkeypatch.setattr("coa_serve.clients.redshift_data.sqlglot.parse_one", self._bug)
        with pytest.raises(AttributeError):
            RedshiftDataAPIExecutor._qualify_awsdatacatalog("SELECT id FROM claims", "db")

    def test_rewrite_render_non_sqlglot_error_propagates(self, monkeypatch):
        monkeypatch.setattr(sqlglot.exp.Expression, "sql", self._bug)
        with pytest.raises(AttributeError):
            RedshiftDataAPIExecutor._qualify_awsdatacatalog("SELECT id FROM claims", "db")


@pytest.mark.unit
class TestTranspileAndLimit:
    def test_transpile_trino_to_redshift_is_lossless_for_plain_select(self):
        out = RedshiftDataAPIExecutor._transpile_to_redshift("SELECT id FROM claims LIMIT 5")
        assert out == "SELECT id FROM claims LIMIT 5"

    def test_inject_limit_appends_when_absent(self):
        out = RedshiftDataAPIExecutor._inject_limit("SELECT id FROM claims", 1000)
        assert out.upper().rstrip().endswith("LIMIT 1000")

    def test_inject_limit_caps_larger(self):
        out = RedshiftDataAPIExecutor._inject_limit("SELECT id FROM claims LIMIT 99999", 1000)
        assert "99999" not in out
        assert "LIMIT 1000" in out.upper()

    def test_inject_limit_keeps_smaller(self):
        out = RedshiftDataAPIExecutor._inject_limit("SELECT id FROM claims LIMIT 10", 1000)
        assert out == "SELECT id FROM claims LIMIT 10"

    def test_inject_limit_preserves_inner_subquery_limit(self):
        out = RedshiftDataAPIExecutor._inject_limit("SELECT count(*) FROM (SELECT * FROM claims LIMIT 2000) t", 1000)
        assert "2000" in out
        assert out.rstrip().upper().endswith("LIMIT 1000")


@pytest.mark.unit
class TestRecordToDict:
    def test_maps_typed_cells_to_values(self):
        row = RedshiftDataAPIExecutor._record_to_dict(["id", "name"], [{"longValue": 3}, {"stringValue": "acme"}])
        assert row == {"id": 3, "name": "acme"}

    def test_explicit_null_cell_becomes_none(self):
        row = RedshiftDataAPIExecutor._record_to_dict(["id", "note"], [{"longValue": 1}, {"isNull": True}])
        assert row == {"id": 1, "note": None}


@pytest.mark.unit
class TestStateMachine:
    """describe_statement polling → FINISHED / FAILED / timeout classification."""

    async def test_finished_returns_rows(self):
        client = MagicMock()
        client.execute_statement.return_value = {"Id": "stmt-1"}
        client.describe_statement.return_value = {"Status": "FINISHED"}
        client.get_statement_result.return_value = {
            "ColumnMetadata": [{"label": "n"}],
            "Records": [[{"longValue": 3}]],
        }
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = AsyncMock(return_value=("my-wg", "insurance_lake"))

        result = await ex.execute(
            "SELECT count(*) AS n FROM claims", namespace="ns", data_source_id="src", timeout_seconds=5
        )
        assert result.rows == [{"n": 3}]
        assert result.columns == ["n"]
        # The executor tags the result so the serve trace can report the engine.
        assert result.engine == "redshift"

    async def test_failed_statement_raises_query_error(self):
        client = MagicMock()
        client.execute_statement.return_value = {"Id": "stmt-2"}
        client.describe_statement.return_value = {"Status": "FAILED", "Error": "syntax error"}
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = AsyncMock(return_value=("my-wg", "insurance_lake"))

        with pytest.raises(RedshiftQueryError, match="syntax error"):
            await ex.execute("SELECT bad FROM claims", namespace="ns", data_source_id="src", timeout_seconds=5)

    async def test_timeout_raises_timeout_error(self):
        client = MagicMock()
        client.execute_statement.return_value = {"Id": "stmt-3"}
        # Never terminal → poll loop must hit the deadline and raise.
        client.describe_statement.return_value = {"Status": "STARTED"}
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = AsyncMock(return_value=("my-wg", "insurance_lake"))

        with pytest.raises(RedshiftTimeoutError):
            await ex.execute("SELECT id FROM claims", namespace="ns", data_source_id="src", timeout_seconds=1)

    async def test_missing_workgroup_raises_value_error(self):
        client = MagicMock()
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = AsyncMock(return_value=("", ""))  # no workgroup resolvable

        with pytest.raises(ValueError, match="workgroup"):
            await ex.execute("SELECT id FROM claims", namespace="ns", data_source_id="src", timeout_seconds=5)
        client.execute_statement.assert_not_called()

    async def test_foreign_qualified_database_is_denied_before_data_api_submission(self):
        client = MagicMock()
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = AsyncMock(return_value=("my-wg", "tenant_a_db"))
        ex._sources.sql_namespace_scope = AsyncMock(
            return_value=SQLNamespaceScope(
                native_databases=frozenset({"tenant_a_db"}), federated_catalog_schemas=frozenset()
            )
        )

        with pytest.raises(RedshiftQueryError, match="outside the requested namespace"):
            await ex.execute(
                "SELECT * FROM AwsDataCatalog.tenant_b_db.customers",
                namespace="tenant-a",
                data_source_id="src",
                timeout_seconds=5,
            )

        client.execute_statement.assert_not_called()


@pytest.mark.unit
class TestRedshiftRouteRefusesTableFunctions:
    """The Redshift route's scope gate must not skip an unqualified table function."""

    _SQL = "SELECT * FROM TABLE(system.query(query => 'SELECT ssn FROM hr.salaries'))"

    async def test_unqualified_table_function_runs_the_scope_check_and_is_refused(self):
        ex = _make_executor(MagicMock())
        calls: list[str] = []

        async def scope(namespace):
            calls.append(namespace)
            return SQLNamespaceScope(native_databases=frozenset({"tenant_a_db"}), federated_catalog_schemas=frozenset())

        ex._sources.sql_namespace_scope = scope
        with pytest.raises(RedshiftQueryError, match="outside the requested namespace"):
            await ex._authorize_qualified_references(self._SQL, "tenant-a")
        assert calls == ["tenant-a"]

    async def test_unavailable_inventory_fails_closed(self):
        ex = _make_executor(MagicMock())
        ex._sources.sql_namespace_scope = _AsyncReturn(None)
        with pytest.raises(RedshiftQueryError, match="Unable to verify"):
            await ex._authorize_qualified_references(self._SQL, "tenant-a")


@pytest.mark.unit
class TestFederatedCatalogNeverReachesAwsDataCatalog:
    """A reference authorized as a federated (catalog, schema) pair must never run
    against a same-named root Glue database on Redshift."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM fedcat.sales.orders",
            'SELECT * FROM "FedCat"."Sales"."Orders"',
            "SELECT o.id FROM awsdatacatalog.tenant_a_db.claims c JOIN fedcat.sales.orders o ON c.id = o.id",
        ],
    )
    def test_rewriter_refuses_non_root_catalog(self, sql):
        with pytest.raises(RedshiftQueryError, match="awsdatacatalog"):
            RedshiftDataAPIExecutor._qualify_awsdatacatalog(sql, "tenant_a_db")

    def test_rewriter_lowercases_database(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog('SELECT id FROM "AwsDataCatalog"."Sales"."orders"', "x")
        assert '"awsdatacatalog"."sales"."orders"' in out
        assert '"Sales"' not in out

    def test_rewriter_lowercases_default_database(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog("SELECT id FROM orders", "Insurance_Lake")
        assert '"awsdatacatalog"."insurance_lake"."orders"' in out

    @staticmethod
    def _executor(native, federated, *, ok=False):
        client = MagicMock()
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = AsyncMock(return_value=("my-wg", "tenant_a_db"))
        ex._sources.sql_namespace_scope = AsyncMock(
            return_value=SQLNamespaceScope(
                native_databases=frozenset(native), federated_catalog_schemas=frozenset(federated)
            )
        )
        if ok:
            client.execute_statement.return_value = {"Id": "stmt-ok"}
            client.describe_statement.return_value = {"Status": "FINISHED"}
            client.get_statement_result.return_value = {"ColumnMetadata": [{"label": "id"}], "Records": []}
        return ex, client

    @pytest.mark.parametrize(
        ("sql", "native", "federated"),
        [
            # (fedcat, sales) is this namespace's federated pair; "sales" is ANOTHER
            # namespace's root Glue database. It must never run as
            # awsdatacatalog."sales"."orders" under the shared serve role.
            ("SELECT * FROM fedcat.sales.orders", {"tenant_a_db"}, {("fedcat", "sales")}),
            ('SELECT * FROM "FedCat"."SALES"."orders"', {"tenant_a_db"}, {("fedcat", "sales")}),
            # Within one namespace: the connector table pgconn.sales.orders must not
            # run as the root Glue table sales.orders, which grants judge differently.
            ("SELECT * FROM pgconn.sales.orders", {"tenant_a_db", "sales"}, {("pgconn", "sales")}),
        ],
    )
    async def test_federated_reference_is_not_executed_on_redshift(self, sql, native, federated):
        ex, client = self._executor(native, federated)
        with pytest.raises(RedshiftQueryError):
            await ex.execute(sql, namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        client.execute_statement.assert_not_called()

    async def test_root_catalog_reference_in_scope_still_executes(self):
        ex, client = self._executor({"tenant_a_db"}, set(), ok=True)

        await ex.execute(
            'SELECT id FROM "AwsDataCatalog"."Tenant_A_DB"."claims"', namespace="tenant-a", data_source_id="src"
        )
        submitted = client.execute_statement.call_args.kwargs["Sql"]
        assert '"awsdatacatalog"."tenant_a_db"."claims"' in submitted

    async def test_rewritten_statement_outside_scope_is_denied(self, monkeypatch):
        # Simulate a Trino/Redshift parse divergence: the authorized statement is
        # bare, but the statement that would run names another database.
        ex, client = self._executor({"tenant_a_db"}, set())
        monkeypatch.setattr(
            ex, "_prepare_sql", lambda *_a, **_k: 'SELECT * FROM "awsdatacatalog"."tenant_b_db"."customers"'
        )
        with pytest.raises(RedshiftQueryError, match="outside the requested namespace"):
            await ex.execute("SELECT * FROM customers", namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        client.execute_statement.assert_not_called()

    async def test_other_in_scope_native_database_executes_with_one_scope_lookup(self):
        # A qualified reference to ANOTHER of the namespace's native databases
        # (not the source's own glue_database) must run, and the first check's
        # scope must be reused rather than fetched a second time.
        ex, client = self._executor({"tenant_a_db", "tenant_a_ref"}, set(), ok=True)
        lookup = ex._sources.sql_namespace_scope

        await ex.execute("SELECT * FROM tenant_a_ref.lookup", namespace="tenant-a", data_source_id="src")

        submitted = client.execute_statement.call_args.kwargs["Sql"]
        assert '"awsdatacatalog"."tenant_a_ref"."lookup"' in submitted
        assert lookup.await_count == 1

    async def test_rewritten_other_database_fetches_scope_once_when_first_check_skipped(self, monkeypatch):
        # Bare caller SQL skips the first lookup; the re-check fetches it exactly once.
        ex, client = self._executor({"tenant_a_db", "tenant_a_ref"}, set(), ok=True)
        lookup = ex._sources.sql_namespace_scope
        monkeypatch.setattr(
            ex, "_prepare_sql", lambda *_a, **_k: 'SELECT * FROM "awsdatacatalog"."tenant_a_ref"."lookup"'
        )

        await ex.execute("SELECT * FROM lookup", namespace="tenant-a", data_source_id="src")

        client.execute_statement.assert_called_once()
        assert lookup.await_count == 1

    async def test_unavailable_scope_during_recheck_is_logged_and_denied(self, monkeypatch):
        ex, client = self._executor({"tenant_a_db"}, set())
        ex._sources.sql_namespace_scope = AsyncMock(return_value=None)
        monkeypatch.setattr(
            ex, "_prepare_sql", lambda *_a, **_k: 'SELECT * FROM "awsdatacatalog"."tenant_a_ref"."lookup"'
        )
        with structlog.testing.capture_logs() as logs, pytest.raises(RedshiftQueryError, match="Unable to verify"):
            await ex.execute("SELECT * FROM lookup", namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        event = next(log for log in logs if log["event"] == "namespace_scope_unavailable")
        assert event["route"] == "redshift_rewritten"
        assert event["namespace"] == "tenant-a"
        client.execute_statement.assert_not_called()

    async def test_unparseable_rewritten_sql_is_not_reported_as_scope_denial(self, monkeypatch):
        ex, client = self._executor({"tenant_a_db"}, set())
        lookup = ex._sources.sql_namespace_scope
        monkeypatch.setattr(ex, "_prepare_sql", lambda *_a, **_k: ")(*&^ not sql")
        with pytest.raises(RedshiftQueryError, match="Unable to verify SQL for Redshift"):
            await ex.execute("SELECT * FROM claims", namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        client.execute_statement.assert_not_called()
        # A parse failure is not a scope decision: no registry lookup is spent on it.
        assert lookup.await_count == 0

    async def test_sql_the_rewriter_cannot_parse_is_never_submitted(self, monkeypatch):
        # The real rewriter runs; only its input is made unparseable (as a transpile
        # fallback can hand it). It must refuse, not submit the SQL unchanged.
        ex, client = self._executor({"tenant_a_db"}, set())
        lookup = ex._sources.sql_namespace_scope
        monkeypatch.setattr(RedshiftDataAPIExecutor, "_transpile_to_redshift", staticmethod(lambda _sql: ")(*&^"))
        with pytest.raises(RedshiftQueryError, match="Unable to rewrite SQL for Redshift"):
            await ex.execute("SELECT * FROM claims", namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        client.execute_statement.assert_not_called()
        assert lookup.await_count == 0

    async def test_bare_system_view_shadowed_by_nested_cte_never_reaches_search_path(self):
        """The outer sys_query_history is outside the CTE's scope: it must run as the
        namespace's Glue table, never as Redshift's local system view."""
        ex, client = self._executor({"tenant_a_db"}, set(), ok=True)

        await ex.execute(
            "SELECT * FROM (WITH sys_query_history AS (SELECT 1 AS a) SELECT a FROM sys_query_history) t "
            "CROSS JOIN sys_query_history q",
            namespace="tenant-a",
            data_source_id="src",
        )
        submitted = client.execute_statement.call_args.kwargs["Sql"].lower()
        assert '"awsdatacatalog"."tenant_a_db"."sys_query_history"' in submitted

    async def test_unqualified_table_left_in_rewritten_sql_is_denied(self, monkeypatch):
        # Defense in depth: if a rewrite ever leaves a bare non-CTE table, the
        # re-check refuses it instead of letting search_path resolve it.
        ex, client = self._executor({"tenant_a_db"}, set())
        monkeypatch.setattr(
            ex,
            "_prepare_sql",
            lambda *_a, **_k: (
                "SELECT * FROM (WITH sys_query_history AS (SELECT 1 AS a) SELECT a FROM sys_query_history) t "
                "CROSS JOIN sys_query_history q"
            ),
        )
        with structlog.testing.capture_logs() as logs, pytest.raises(RedshiftQueryError, match="outside"):
            await ex.execute("SELECT * FROM claims", namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        assert any(log["event"] == "redshift_unqualified_reference" for log in logs)
        client.execute_statement.assert_not_called()

    # A CTE name that differs from the reference only in case or quoting binds to
    # the CTE on a default workgroup but, with enable_case_sensitive_identifier,
    # leaves the bare reference to search_path (a system view or local table).
    _CASE_FOLDED_CTE = [
        'WITH "PG_USER" AS (SELECT 1 AS x) SELECT * FROM pg_user',
        'WITH "SYS_QUERY_HISTORY" AS (SELECT 1 AS x) SELECT * FROM sys_query_history',
        "WITH Orders AS (SELECT 1 AS x) SELECT * FROM ORDERS",
    ]

    @pytest.mark.parametrize("sql", _CASE_FOLDED_CTE)
    async def test_case_folded_cte_reference_is_refused(self, sql):
        ex, client = self._executor({"tenant_a_db"}, set(), ok=True)
        with pytest.raises(RedshiftQueryError, match="only in case or quoting"):
            await ex.execute(sql, namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        client.execute_statement.assert_not_called()

    @pytest.mark.parametrize("sql", _CASE_FOLDED_CTE)
    async def test_case_folded_cte_left_in_rewritten_sql_is_denied(self, sql, monkeypatch):
        # The re-check applies the same rule: the bare name is a stored table there.
        ex, client = self._executor({"tenant_a_db"}, set(), ok=True)
        monkeypatch.setattr(ex, "_prepare_sql", lambda *_a, **_k: sql)
        with pytest.raises(RedshiftQueryError, match="outside the requested namespace"):
            await ex.execute("SELECT * FROM claims", namespace="tenant-a", data_source_id="src", timeout_seconds=1)
        client.execute_statement.assert_not_called()

    @pytest.mark.parametrize(
        "sql",
        ["WITH t AS (SELECT 1 AS x) SELECT * FROM t", 'WITH "t" AS (SELECT 1 AS x) SELECT * FROM t'],
    )
    def test_exactly_matching_cte_reference_stays_bare(self, sql):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(sql, "tenant_a_db")
        assert "awsdatacatalog" not in out.lower()

    def test_non_awsdatacatalog_backstop_logs_the_catalog(self):
        with structlog.testing.capture_logs() as logs, pytest.raises(RedshiftQueryError):
            RedshiftDataAPIExecutor._qualify_awsdatacatalog("SELECT * FROM FedCat.sales.orders", "tenant_a_db")
        event = next(log for log in logs if log["event"] == "redshift_non_awsdatacatalog_reference")
        assert event["catalog"] == "fedcat"


class _AsyncReturn:
    """Tiny awaitable-returning stub: replaces an async method with a fixed value."""

    def __init__(self, value):
        self._value = value

    async def __call__(self, *args, **kwargs):
        return self._value
