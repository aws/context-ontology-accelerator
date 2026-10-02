# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for JdbcConnector."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure pg8000 is available as a mock module for tests
_mock_pg8000 = MagicMock()
sys.modules.setdefault("pg8000", _mock_pg8000)

from coa_sources.database.connectors import get_connector  # noqa: E402
from coa_sources.database.connectors.jdbc import JdbcConnector  # noqa: E402


@pytest.fixture
def connector():
    return JdbcConnector()


@pytest.fixture
def base_config():
    return {
        "host": "db.example.com",
        "port": 5432,
        "engine": "POSTGRESQL",
        "database_name": "testdb",
        "credential_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds",
        "region": "us-east-1",
    }


class TestRegistry:
    def test_jdbc_registered(self):
        connector = get_connector("JDBC_DATABASE")
        assert isinstance(connector, JdbcConnector)


class TestTestConnection:
    def test_empty_host(self, connector, base_config):
        base_config["host"] = ""
        result = connector.test_connection(base_config)
        assert not result.success
        assert result.checks[0].check == "network"
        assert result.checks[0].status == "failed"
        assert "host" in result.checks[0].message.lower()

    def test_missing_host(self, connector):
        result = connector.test_connection({"port": 5432, "credential_secret_arn": "arn:x"})
        assert not result.success
        assert "host" in result.checks[0].message.lower()

    def test_empty_credential_secret_arn(self, connector, base_config):
        base_config["credential_secret_arn"] = ""
        result = connector.test_connection(base_config)
        assert not result.success
        assert result.checks[0].check == "auth"
        assert "credential_secret_arn" in result.checks[0].message

    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_network_failure(self, mock_socket, connector, base_config):
        mock_socket.side_effect = OSError("Connection refused")

        result = connector.test_connection(base_config)

        assert not result.success
        assert len(result.checks) == 1
        assert result.checks[0].check == "network"
        assert result.checks[0].status == "failed"
        assert "cannot reach" in result.checks[0].message

    @patch("coa_sources.database.connectors.jdbc.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_credentials_failure(self, mock_socket, mock_boto3, connector, base_config):
        mock_socket.return_value = MagicMock()
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.get_secret_value.side_effect = Exception("Access denied")

        result = connector.test_connection(base_config)

        assert not result.success
        assert len(result.checks) == 2
        assert result.checks[0].status == "ok"
        assert result.checks[1].check == "auth"
        assert result.checks[1].status == "failed"
        assert "credentials" in result.checks[1].message.lower()

    @patch("coa_sources.database.connectors.jdbc.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_secret_missing_keys(self, mock_socket, mock_boto3, connector, base_config):
        mock_socket.return_value = MagicMock()
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": json.dumps({"user": "admin"})}

        result = connector.test_connection(base_config)

        assert not result.success
        assert result.checks[1].check == "auth"
        assert result.checks[1].status == "failed"

    @patch("coa_sources.database.connectors.jdbc.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_secret_binary_not_string(self, mock_socket, mock_boto3, connector, base_config):
        mock_socket.return_value = MagicMock()
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretBinary": b"binary"}

        result = connector.test_connection(base_config)

        assert not result.success
        assert result.checks[1].check == "auth"
        assert result.checks[1].status == "failed"

    @patch("coa_sources.database.connectors.jdbc.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_successful_postgresql(self, mock_socket, mock_boto3, connector, base_config):
        mock_socket.return_value = MagicMock()
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"username": "admin", "password": "secret"})
        }
        # Two pg8000 connections are opened: one for SELECT 1, one for the
        # schema-access probe. Both share the same mock connection, but the
        # cursor returns at least one schema so the probe passes.
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [("public",), ("analytics",)]
        mock_conn.cursor.return_value = mock_cursor
        _mock_pg8000.connect.return_value = mock_conn
        _mock_pg8000.connect.side_effect = None

        result = connector.test_connection(base_config)

        assert result.success
        # 4 checks now: network, auth, database (SELECT 1), schema_access
        assert len(result.checks) == 4
        assert [c.check for c in result.checks] == ["network", "auth", "database", "schema_access"]
        assert all(c.status == "ok" for c in result.checks)

    @patch("coa_sources.database.connectors.jdbc.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_database_connection_failure(self, mock_socket, mock_boto3, connector, base_config):
        mock_socket.return_value = MagicMock()
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"username": "admin", "password": "wrong"})
        }
        _mock_pg8000.connect.side_effect = Exception("password authentication failed")

        result = connector.test_connection(base_config)

        assert not result.success
        assert result.checks[2].check == "database"
        assert result.checks[2].status == "failed"
        assert "authentication" in result.checks[2].message.lower() or "connection" in result.checks[2].message.lower()

    @patch("coa_sources.database.connectors.jdbc.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_unsupported_engine_rejected_before_any_io(self, mock_socket, mock_boto3, connector, base_config):
        # An engine with no enabled dialect can never be discovered, so the
        # connection test must FAIL (it used to return success with a warning,
        # which let a source be created that could never be scanned) — and it must
        # do so before opening a socket or reading the credential secret.
        base_config["engine"] = "DB2"
        mock_socket.return_value = MagicMock()

        result = connector.test_connection(base_config)

        assert not result.success
        assert [c.check for c in result.checks] == ["engine"]
        assert result.checks[0].status == "failed"
        assert "DB2" in result.checks[0].message
        mock_socket.assert_not_called()
        mock_boto3.Session.assert_not_called()

    @pytest.mark.parametrize("engine", ["DB2", "TERADATA", "UNKNOWN"])
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_unknown_engine_rejected(self, mock_socket, connector, base_config, engine):
        # Engines without a dialect are rejected at the connect boundary.
        base_config["engine"] = engine

        result = connector.test_connection(base_config)

        assert not result.success
        assert engine in result.message
        mock_socket.assert_not_called()

    @patch("coa_sources.database.connectors.sts_assume.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_cross_account_role(self, mock_socket, mock_boto3, connector, base_config):
        base_config["cross_account_role_arn"] = "arn:aws:iam::999999999999:role/cross-role"
        base_config["external_id"] = "my-external-id"
        base_config["namespace_id"] = "ns-7"
        mock_socket.return_value = MagicMock()
        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST"}
        }
        mock_boto3.client.return_value = mock_sts
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        mock_sm_client = MagicMock()
        mock_session.client.return_value = mock_sm_client
        mock_sm_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"username": "admin", "password": "secret"})
        }
        # Runs on the default POSTGRESQL engine, so the connect + schema probe
        # execute too (an engine without a dialect is now rejected up front).
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [("public",)]
        mock_conn.cursor.return_value = mock_cursor
        _mock_pg8000.connect.return_value = mock_conn
        _mock_pg8000.connect.side_effect = None

        result = connector.test_connection(base_config)

        assert result.success
        # config=None is the discovery path's default; only a synchronous customer-path
        # caller passes the fail-fast preset.
        mock_boto3.client.assert_called_with("sts", region_name="us-east-1", config=None)
        # The session name reaches the data owner's CloudTrail, so it names the
        # requesting namespace rather than a fixed connector string.
        mock_sts.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::999999999999:role/cross-role",
            RoleSessionName="coa-jdbc-ns-7",
            ExternalId="my-external-id",
        )

    @patch("coa_sources.database.connectors.sts_assume.boto3")
    @patch("coa_sources.database.connectors.jdbc.socket.create_connection")
    def test_cross_account_role_without_external_id_is_refused(self, mock_socket, mock_boto3, connector, base_config):
        """No external_id → the credential fetch must not assume the role at all.

        Without an ExternalId the assume carries no evidence of which namespace
        requested it, so a caller could point a source at another tenant's role.
        The connector reports an auth failure instead of assuming unconditioned.
        """
        base_config["cross_account_role_arn"] = "arn:aws:iam::999999999999:role/cross-role"
        base_config.pop("external_id", None)
        mock_socket.return_value = MagicMock()
        mock_sts = MagicMock()
        mock_boto3.client.return_value = mock_sts

        result = connector.test_connection(base_config)

        assert not result.success
        mock_sts.assume_role.assert_not_called()


class TestDiscoverMetadata:
    """Tests for the schema-aware JDBC discover_metadata() implementation."""

    @pytest.fixture(autouse=True)
    def _reset_pg8000_mock(self):
        """Each test re-configures the shared pg8000 mock; ensure clean state."""
        _mock_pg8000.connect.reset_mock()
        _mock_pg8000.connect.side_effect = None
        yield

    @staticmethod
    def _build_mock_conn(
        *,
        schemas: list[str],
        tables_per_schema: dict[str, list[str]],
        columns_per_table: dict[str, list[tuple[str, str, str]]],
    ):
        """Assemble a pg8000-style mock connection that returns canned rows.

        Each call to conn.cursor() returns a fresh cursor whose execute()
        is inspected to decide what fetchall() should return.

        Args:
            schemas: List of schema names returned by information_schema.schemata.
            tables_per_schema: Maps schema -> list of table names.
            columns_per_table: Maps table -> list of (name, type, is_nullable).
        """
        conn = MagicMock()

        def make_cursor():
            cursor = MagicMock()
            recorded_sql = {"sql": ""}

            def execute(sql, params=()):
                recorded_sql["sql"] = sql.lower()
                recorded_sql["params"] = params

            def fetchall():
                sql = recorded_sql["sql"]
                params = recorded_sql.get("params", ())
                if "from information_schema.schemata" in sql:
                    return [(s,) for s in schemas]
                if "from information_schema.tables" in sql:
                    schema = params[0]
                    return [(t, "BASE TABLE") for t in tables_per_schema.get(schema, [])]
                if "from information_schema.columns" in sql:
                    schema = params[0]
                    table_names = params[1:]
                    rows = []
                    for table in table_names:
                        for name, dtype, nullable in columns_per_table.get(table, []):
                            rows.append((table, name, dtype, nullable))
                    return rows
                return []

            cursor.execute.side_effect = execute
            cursor.fetchall.side_effect = fetchall
            return cursor

        conn.cursor.side_effect = make_cursor
        return conn

    def _patch_creds(self, mock_boto3):
        mock_session = MagicMock()
        mock_boto3.Session.return_value = mock_session
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": json.dumps({"username": "u", "password": "p"})}

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_unsupported_engine_raises(self, mock_boto3, connector, base_config):
        base_config["engine"] = "DB2"  # no registered dialect
        with pytest.raises(NotImplementedError, match="DB2"):
            connector.discover_metadata(base_config)

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_missing_database_name_raises(self, mock_boto3, connector, base_config):
        base_config["database_name"] = ""
        with pytest.raises(ValueError, match="database_name"):
            connector.discover_metadata(base_config)

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_glob_filter_treats_brackets_as_character_class(self, mock_boto3, connector, base_config):
        """Glob [abc] matches a single character from the set."""
        self._patch_creds(mock_boto3)
        base_config["schema_filter"] = "[ps]*"
        conn = self._build_mock_conn(
            schemas=["public", "staging", "analytics"],
            tables_per_schema={"public": ["t1"], "staging": ["t2"], "analytics": ["t3"]},
            columns_per_table={
                "t1": [("c", "text", "YES")],
                "t2": [("c", "text", "YES")],
                "t3": [("c", "text", "YES")],
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)
        schemas = sorted(t.database for t in result.tables)
        assert schemas == ["public", "staging"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_default_excludes_system_schemas(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        # System schemas should be excluded by default. `analytics` is the only
        # user schema; pg_catalog and information_schema must NOT appear in output.
        conn = self._build_mock_conn(
            schemas=["analytics", "pg_catalog", "information_schema", "pg_toast"],
            tables_per_schema={"analytics": ["users"]},
            columns_per_table={"users": [("id", "bigint", "NO"), ("email", "text", "YES")]},
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        assert len(result.tables) == 1
        # Table ID format is `{schema}.{table}` — the design's JDBC identifier
        assert result.tables[0].table_id == "analytics.users"
        assert result.tables[0].database == "analytics"
        assert {c.name for c in result.tables[0].columns} == {"id", "email"}

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_schema_filter_applies_inclusion(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        base_config["schema_filter"] = "public"
        conn = self._build_mock_conn(
            schemas=["public", "staging", "analytics"],
            tables_per_schema={"public": ["t1"], "staging": ["t2"], "analytics": ["t3"]},
            columns_per_table={
                "t1": [("c", "text", "YES")],
                "t2": [("c", "text", "YES")],
                "t3": [("c", "text", "YES")],
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        # Only `public.t1` survives — staging and analytics are excluded by include filter
        assert [t.table_id for t in result.tables] == ["public.t1"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_schema_exclude_filter_overrides_default(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        # Caller-supplied exclude REPLACES the default system-schema exclude.
        # A pattern matching 'staging' lets pg_catalog through (since the test
        # would normally be excluded by default).
        base_config["schema_exclude_filter"] = "staging"
        conn = self._build_mock_conn(
            schemas=["public", "staging", "pg_catalog"],
            tables_per_schema={"public": ["a"], "staging": ["b"], "pg_catalog": ["c"]},
            columns_per_table={
                "a": [("x", "text", "YES")],
                "b": [("x", "text", "YES")],
                "c": [("x", "text", "YES")],
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        names = sorted(t.table_id for t in result.tables)
        assert names == ["pg_catalog.c", "public.a"]
        assert "staging.b" not in names

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_table_filter_and_exclude_apply(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        base_config["table_filter"] = "orders*"
        base_config["table_exclude_filter"] = "*_staging"
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["orders", "orders_staging", "products"]},
            columns_per_table={
                "orders": [("id", "bigint", "NO")],
                "orders_staging": [("id", "bigint", "NO")],
                "products": [("id", "bigint", "NO")],
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)
        # Only `orders` (matches include, doesn't match exclude). `orders_staging`
        # matches include but is excluded; `products` doesn't match include.
        assert [t.table_id for t in result.tables] == ["public.orders"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_glob_star_only_matches_prefix(self, mock_boto3, connector, base_config):
        """Verify that 'c*' glob matches only tables starting with 'c'."""
        self._patch_creds(mock_boto3)
        base_config["table_filter"] = "c*"
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["catastrophe", "policy", "loss_reserve", "claim"]},
            columns_per_table={
                "catastrophe": [("id", "int", "NO")],
                "policy": [("id", "int", "NO")],
                "loss_reserve": [("id", "int", "NO")],
                "claim": [("id", "int", "NO")],
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        table_names = sorted(t.name for t in result.tables)
        assert table_names == ["catastrophe", "claim"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_glob_question_mark_single_char(self, mock_boto3, connector, base_config):
        """Verify that '?' glob matches exactly one character."""
        self._patch_creds(mock_boto3)
        base_config["schema_filter"] = "d?"
        conn = self._build_mock_conn(
            schemas=["db", "dw", "data", "public"],
            tables_per_schema={"db": ["t1"], "dw": ["t2"], "data": ["t3"], "public": ["t4"]},
            columns_per_table={
                "t1": [("x", "text", "YES")],
                "t2": [("x", "text", "YES")],
                "t3": [("x", "text", "YES")],
                "t4": [("x", "text", "YES")],
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        schemas = sorted(t.database for t in result.tables)
        assert schemas == ["db", "dw"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_table_filter_pipe_list_matches_any_glob(self, mock_boto3, connector, base_config):
        """A pipe-delimited list of globs includes a table matching ANY of them.

        Matches the source-creation UI, which labels this field "regex" with an
        ``orders|customers`` placeholder. The names need not share a common
        glob — exactly the case for isolating an arbitrary table subset (e.g. a
        Formula-1 dataset's circuits/drivers/races/... out of a shared schema).
        """
        self._patch_creds(mock_boto3)
        base_config["table_filter"] = "circuits|drivers|races|status"
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["circuits", "drivers", "races", "status", "superhero", "race"]},
            columns_per_table={
                n: [("id", "int", "NO")] for n in ["circuits", "drivers", "races", "status", "superhero", "race"]
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        # All four listed tables, and ONLY those. `superhero` isn't listed;
        # `race` must NOT match the `races` glob (full-match, not substring).
        assert sorted(t.name for t in result.tables) == ["circuits", "drivers", "races", "status"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_table_filter_comma_list_with_globs(self, mock_boto3, connector, base_config):
        """Comma is an accepted delimiter too, and list entries may be globs."""
        self._patch_creds(mock_boto3)
        base_config["table_filter"] = "constructor*, drivers"
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["constructors", "constructorresults", "drivers", "races"]},
            columns_per_table={
                n: [("id", "int", "NO")] for n in ["constructors", "constructorresults", "drivers", "races"]
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        # `constructor*` expands to both constructor tables; `drivers` exact; surrounding
        # whitespace around list entries is tolerated. `races` is excluded.
        assert sorted(t.name for t in result.tables) == ["constructorresults", "constructors", "drivers"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_table_exclude_filter_pipe_list_excludes_any(self, mock_boto3, connector, base_config):
        """The list semantics apply to EXCLUDE filters too: a table is excluded
        if it matches ANY entry. (``_compile_filter`` is shared by include and
        exclude paths, so this pins the exclude behavior explicitly.)"""
        self._patch_creds(mock_boto3)
        base_config["table_exclude_filter"] = "*_staging|*_tmp|audit_log"
        names = ["orders", "orders_staging", "customers_tmp", "audit_log", "products"]
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": names},
            columns_per_table={n: [("id", "int", "NO")] for n in names},
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        # Excluded: orders_staging (*_staging), customers_tmp (*_tmp), audit_log
        # (exact). Survivors: orders, products.
        assert sorted(t.name for t in result.tables) == ["orders", "products"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_filter_glob_character_class_with_comma_is_not_split(self, mock_boto3, connector, base_config):
        """A comma INSIDE a glob character class (``[...]``) must not be treated
        as a list delimiter — ``log_[a,c]`` is one glob matching ``log_a`` /
        ``log_c`` (and the literal ``,``), NOT the two broken globs ``log_[a``
        and ``c]``."""
        self._patch_creds(mock_boto3)
        base_config["table_filter"] = "log_[a,c]"
        names = ["log_a", "log_b", "log_c", "log_ac"]
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": names},
            columns_per_table={n: [("id", "int", "NO")] for n in names},
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)

        # `[a,c]` is a single-char class {a, c, ,} — matches log_a and log_c,
        # NOT log_b (not in class) and NOT log_ac (class matches exactly one char).
        assert sorted(t.name for t in result.tables) == ["log_a", "log_c"]

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_columns_populated_with_correct_types_and_nullability(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["users"]},
            columns_per_table={
                "users": [
                    ("id", "bigint", "NO"),
                    ("email", "varchar", "YES"),
                    ("created_at", "timestamp", "NO"),
                ]
            },
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)
        cols = {c.name: c for c in result.tables[0].columns}
        assert cols["id"].data_type == "bigint"
        assert cols["id"].nullable is False
        assert cols["email"].nullable is True
        assert result.tables[0].technical_metadata.column_count == 3

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_empty_schema_yields_no_tables_no_error(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": []},
            columns_per_table={},
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)
        assert result.tables == []

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_data_source_id_and_namespace_propagated_to_tables(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        base_config["data_source_id"] = "ds-123"
        base_config["namespace_id"] = "ns-456"
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["t"]},
            columns_per_table={"t": [("c", "text", "YES")]},
        )
        _mock_pg8000.connect.return_value = conn

        result = connector.discover_metadata(base_config)
        assert result.tables[0].data_source_id == "ds-123"
        assert result.tables[0].namespace_id == "ns-456"

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_connection_open_failure_raises_runtime_error(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        _mock_pg8000.connect.side_effect = Exception("Connection refused")
        with pytest.raises(RuntimeError, match="Failed to open database connection"):
            connector.discover_metadata(base_config)

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_technical_metadata_hash_is_deterministic(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        conn = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["t"]},
            columns_per_table={"t": [("a", "text", "NO"), ("b", "int", "YES")]},
        )
        _mock_pg8000.connect.return_value = conn

        first = connector.discover_metadata(base_config).tables[0].technical_metadata_hash
        # Reset and run again — same inputs must yield same hash
        _mock_pg8000.connect.reset_mock()
        _mock_pg8000.connect.return_value = self._build_mock_conn(
            schemas=["public"],
            tables_per_schema={"public": ["t"]},
            columns_per_table={"t": [("a", "text", "NO"), ("b", "int", "YES")]},
        )
        second = connector.discover_metadata(base_config).tables[0].technical_metadata_hash
        assert first == second
        assert len(first) == 16
