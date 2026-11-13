# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for JDBC deterministic PK/FK discovery and IAM authentication."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from coa_common.domain_models import EnrichmentSource

_mock_pg8000 = sys.modules.setdefault("pg8000", MagicMock())

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


def _mock_conn_with_constraints(*, pk_rows, fk_rows):
    """pg8000-style mock answering schema/table/column + constraint queries."""
    conn = MagicMock()

    def make_cursor():
        cursor = MagicMock()
        state = {"sql": "", "params": ()}

        def execute(sql, params=()):
            state["sql"] = sql.lower()
            state["params"] = params

        def fetchall():
            sql = state["sql"]
            if "from information_schema.schemata" in sql:
                return [("public",)]
            if "primary key" in sql:
                return pk_rows
            if "referential_constraints" in sql:
                return fk_rows
            if "from information_schema.tables" in sql:
                return [("orders", "BASE TABLE")]
            if "from information_schema.columns" in sql:
                return [("orders", "id", "bigint", "NO"), ("orders", "customer_id", "bigint", "YES")]
            return []

        cursor.execute.side_effect = execute
        cursor.fetchall.side_effect = fetchall
        return cursor

    conn.cursor.side_effect = make_cursor
    return conn


def _patch_creds(mock_boto3, secret=None):
    secret = secret or {"username": "u", "password": "p"}
    mock_session = MagicMock()
    mock_boto3.Session.return_value = mock_session
    mock_client = MagicMock()
    mock_session.client.return_value = mock_client
    mock_client.get_secret_value.return_value = {"SecretString": json.dumps(secret)}
    return mock_session


class TestDeterministicConstraints:
    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_primary_key_tagged_deterministic(self, mock_boto3, connector, base_config):
        _patch_creds(mock_boto3)
        _mock_pg8000.connect.reset_mock()
        _mock_pg8000.connect.side_effect = None
        _mock_pg8000.connect.return_value = _mock_conn_with_constraints(pk_rows=[("orders", "id")], fk_rows=[])

        table = connector.discover_metadata(base_config).tables[0]
        assert table.primary_key.columns == ["id"]
        assert table.primary_key.source == EnrichmentSource.DETERMINISTIC
        assert table.primary_key.confidence == 1.0

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_foreign_keys_tagged_deterministic(self, mock_boto3, connector, base_config):
        _patch_creds(mock_boto3)
        _mock_pg8000.connect.reset_mock()
        _mock_pg8000.connect.side_effect = None
        _mock_pg8000.connect.return_value = _mock_conn_with_constraints(
            pk_rows=[("orders", "id")], fk_rows=[("orders", "customer_id", "customers", "id")]
        )

        table = connector.discover_metadata(base_config).tables[0]
        assert len(table.foreign_keys) == 1
        fk = table.foreign_keys[0]
        assert (fk.column, fk.target_table, fk.target_column) == ("customer_id", "customers", "id")
        assert fk.source == EnrichmentSource.DETERMINISTIC

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_no_constraints_leaves_defaults(self, mock_boto3, connector, base_config):
        _patch_creds(mock_boto3)
        _mock_pg8000.connect.reset_mock()
        _mock_pg8000.connect.side_effect = None
        _mock_pg8000.connect.return_value = _mock_conn_with_constraints(pk_rows=[], fk_rows=[])

        table = connector.discover_metadata(base_config).tables[0]
        assert table.primary_key.columns == []
        assert table.foreign_keys == []

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_constraint_query_failure_degrades_gracefully(self, mock_boto3, connector, base_config):
        """If constraint queries fail (e.g. permissions), discovery continues without constraints."""
        _patch_creds(mock_boto3)
        _mock_pg8000.connect.reset_mock()
        _mock_pg8000.connect.side_effect = None

        conn = MagicMock()

        def make_cursor():
            cursor = MagicMock()
            state = {"sql": ""}

            def execute(sql, params=()):
                state["sql"] = sql.lower()
                if "table_constraints" in state["sql"]:
                    raise Exception("permission denied for information_schema.table_constraints")

            def fetchall():
                sql = state["sql"]
                if "from information_schema.schemata" in sql:
                    return [("public",)]
                if "from information_schema.tables" in sql:
                    return [("orders", "BASE TABLE")]
                if "from information_schema.columns" in sql:
                    return [("orders", "id", "bigint", "NO")]
                return []

            cursor.execute.side_effect = execute
            cursor.fetchall.side_effect = fetchall
            return cursor

        conn.cursor.side_effect = make_cursor
        _mock_pg8000.connect.return_value = conn

        tables = connector.discover_metadata(base_config).tables
        assert len(tables) == 1
        assert tables[0].primary_key.columns == []
        assert tables[0].foreign_keys == []
