# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for RedshiftDataAPIExecutor.

Covers the pure SQL-preparation logic (Trino→Redshift transpile + awsdatacatalog
3-part rewrite + LIMIT injection), the Data-API record→dict conversion, and the
FINISHED / FAILED / timeout state-machine classification. The boto3 redshift-data
client is stubbed — no network calls.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

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
        # explicit db in the SQL wins over the default
        assert "awsdatacatalog" in n and "insurance_lake" in n and "claims" in n

    def test_three_part_catalog_is_normalized_to_awsdatacatalog(self):
        # An Athena-style AwsDataCatalog prefix is normalized to the Redshift
        # external-catalog name (lowercased awsdatacatalog).
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

    def test_unparseable_sql_returns_input_unchanged(self):
        junk = ")(*&^ not sql"
        assert RedshiftDataAPIExecutor._qualify_awsdatacatalog(junk, "db") == junk

    def test_cte_reference_is_not_qualified_as_a_glue_table(self):
        """Review finding: a bare reference to a CTE must NOT be rewritten
        into awsdatacatalog.<db>.<cte> — only real Glue tables get qualified."""
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            "WITH a AS (SELECT id FROM claims), b AS (SELECT id FROM a) SELECT * FROM b",
            "insurance_lake",
        )
        n = _norm(out)
        # The real table (claims) IS qualified...
        assert "awsdatacatalog" in n and "insurance_lake" in n and "claims" in n
        # ...but the CTE names a/b are NOT turned into catalog tables.
        assert '"awsdatacatalog"."insurance_lake"."a"' not in out.lower()
        assert '"awsdatacatalog"."insurance_lake"."b"' not in out.lower()

    def test_cte_with_multiple_real_tables_still_qualifies_them(self):
        out = RedshiftDataAPIExecutor._qualify_awsdatacatalog(
            "WITH t AS (SELECT * FROM claims JOIN tbl_clm_hdr ON claims.id = tbl_clm_hdr.id) SELECT * FROM t",
            "insurance_lake",
        )
        n = _norm(out)
        # Both real tables qualified; the CTE 't' reference is not.
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
        with structlog.testing.capture_logs() as logs:
            assert RedshiftDataAPIExecutor._qualify_awsdatacatalog(self._SQL, "db") == self._SQL
        event = next(log for log in logs if log["event"] == "redshift_awsdatacatalog_rewrite_parse_failed")
        assert "sql" not in event
        assert event["sql_len"] == len(self._SQL)
        assert event["error_type"] == "ParseError"
        assert "alice@example.com" not in repr(logs)

    def test_rewrite_render_failure_logs_type_not_message(self, monkeypatch):
        monkeypatch.setattr(sqlglot.exp.Expression, "sql", self._boom)
        with structlog.testing.capture_logs() as logs:
            assert RedshiftDataAPIExecutor._qualify_awsdatacatalog(self._SQL, "db") == self._SQL
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
        assert "claims" in _norm(out)

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
        # Stub source resolution to a Redshift-enabled Glue source.
        ex._resolve_workgroup_and_database = _AsyncReturn(("my-wg", "insurance_lake"))

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
        ex._resolve_workgroup_and_database = _AsyncReturn(("my-wg", "insurance_lake"))

        with pytest.raises(RedshiftQueryError, match="syntax error"):
            await ex.execute("SELECT bad FROM claims", namespace="ns", data_source_id="src", timeout_seconds=5)

    async def test_timeout_raises_timeout_error(self):
        client = MagicMock()
        client.execute_statement.return_value = {"Id": "stmt-3"}
        # Never terminal → poll loop must hit the deadline and raise.
        client.describe_statement.return_value = {"Status": "STARTED"}
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = _AsyncReturn(("my-wg", "insurance_lake"))

        with pytest.raises(RedshiftTimeoutError):
            await ex.execute("SELECT id FROM claims", namespace="ns", data_source_id="src", timeout_seconds=1)

    async def test_missing_workgroup_raises_value_error(self):
        client = MagicMock()
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = _AsyncReturn(("", ""))  # no workgroup resolvable

        with pytest.raises(ValueError, match="workgroup"):
            await ex.execute("SELECT id FROM claims", namespace="ns", data_source_id="src", timeout_seconds=5)
        client.execute_statement.assert_not_called()

    async def test_foreign_qualified_database_is_denied_before_data_api_submission(self):
        client = MagicMock()
        ex = _make_executor(client)
        ex._resolve_workgroup_and_database = _AsyncReturn(("my-wg", "tenant_a_db"))
        ex._sources.sql_namespace_scope = _AsyncReturn(
            SQLNamespaceScope(native_databases=frozenset({"tenant_a_db"}), federated_catalog_schemas=frozenset())
        )

        with pytest.raises(RedshiftQueryError, match="outside the requested namespace"):
            await ex.execute(
                "SELECT * FROM AwsDataCatalog.tenant_b_db.customers",
                namespace="tenant-a",
                data_source_id="src",
                timeout_seconds=5,
            )

        client.execute_statement.assert_not_called()


class _AsyncReturn:
    """Tiny awaitable-returning stub: replaces an async method with a fixed value."""

    def __init__(self, value):
        self._value = value

    async def __call__(self, *args, **kwargs):
        return self._value
