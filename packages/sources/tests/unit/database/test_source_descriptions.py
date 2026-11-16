# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for discovering source descriptions (Glue, JDBC dialects) and preserving them through enrichment."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    EnrichmentSource,
    ReviewStatus,
    Table,
)
from coa_sources.database.connectors.dialects import (
    MySqlDialect,
    OracleDialect,
    PostgresDialect,
    SnowflakeDialect,
    SqlServerDialect,
)
from coa_sources.database.enrichment.prompts import build_user_prompt
from coa_sources.database.enrichment.table_enricher import (
    _PROTECTED_DESCRIPTION_SOURCES,
    _apply_result,
)

_mock_pg8000 = sys.modules.setdefault("pg8000", MagicMock())

from coa_sources.database.connectors.glue_catalog import (
    GlueCatalogConnector,
)
from coa_sources.database.connectors.jdbc import JdbcConnector


@pytest.mark.unit
class TestGlueCatalogDescriptions:
    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_table_description_promoted_to_business_metadata(self, mock_boto3):
        mock_client = MagicMock()
        # get_database() supplies the database-level Description.
        mock_client.get_database.return_value = {"Database": {"Name": "sales", "Description": "Sales operational DB"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "Description": "All customer orders since 2018",
                        "StorageDescriptor": {"Columns": [], "Location": ""},
                        "PartitionKeys": [],
                    }
                ]
            }
        ]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client

        result = GlueCatalogConnector().discover_metadata({"database_name": "sales", "region": "us-east-1"})

        assert len(result.tables) == 1
        table = result.tables[0]
        # The table's own description is on business_metadata only.
        assert table.business_metadata.description == "All customer orders since 2018"
        assert table.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        assert table.business_metadata.review_status == ReviewStatus.PENDING_REVIEW
        assert table.business_metadata.confidence == 1.0
        assert table.database_description == "Sales operational DB"

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_missing_table_description_leaves_business_metadata_empty(self, mock_boto3):
        mock_client = MagicMock()
        mock_client.get_database.return_value = {"Database": {"Name": "sales"}}  # no Description
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "StorageDescriptor": {"Columns": [], "Location": ""},
                        "PartitionKeys": [],
                    }
                ]
            }
        ]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client

        table = GlueCatalogConnector().discover_metadata({"database_name": "sales", "region": "us-east-1"}).tables[0]

        assert table.business_metadata.description == ""
        assert table.business_metadata.enrichment_source == ""
        assert table.database_description == ""

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_column_comment_promoted_to_business_metadata(self, mock_boto3):
        mock_client = MagicMock()
        mock_client.get_database.return_value = {"Database": {"Name": "sales"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "Description": "",
                        "StorageDescriptor": {
                            "Columns": [
                                {"Name": "order_id", "Type": "bigint", "Comment": "Unique order identifier"},
                                {"Name": "amount", "Type": "decimal(10,2)", "Comment": ""},
                            ],
                            "Location": "",
                        },
                        "PartitionKeys": [
                            {"Name": "order_date", "Type": "date", "Comment": "Date the order was placed"}
                        ],
                    }
                ]
            }
        ]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client

        table = GlueCatalogConnector().discover_metadata({"database_name": "sales", "region": "us-east-1"}).tables[0]

        cols_by_name = {c.name: c for c in table.columns}
        # Column with comment: surfaced ONLY on business_metadata (single
        # canonical home), DETERMINISTIC + APPROVED + confidence 1.0.
        assert cols_by_name["order_id"].business_metadata.description == "Unique order identifier"
        assert cols_by_name["order_id"].business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        assert cols_by_name["order_id"].business_metadata.review_status == ReviewStatus.APPROVED
        assert cols_by_name["order_id"].business_metadata.confidence == 1.0
        # Partition keys participate in the same promotion.
        assert cols_by_name["order_date"].business_metadata.description == "Date the order was placed"
        assert cols_by_name["order_date"].business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        # Column without a comment must NOT be tagged.
        assert cols_by_name["amount"].business_metadata.description == ""
        assert cols_by_name["amount"].business_metadata.enrichment_source == ""


class _RecordingCursor:
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


class _RecordingConn:
    def __init__(self, responder):
        self.cursor_obj = _RecordingCursor(responder)

    def cursor(self):
        return self.cursor_obj


@pytest.mark.unit
class TestPostgresDescriptions:
    """PostgreSQL/Redshift use pg_description."""

    def test_returns_table_and_column_descriptions(self):
        def responder(sql, params):
            if "obj_description" in sql:
                return [("orders", "All customer orders"), ("returns", "")]
            if "col_description" in sql:
                return [("orders", "id", "Primary key"), ("orders", "amount", "")]
            return []

        conn = _RecordingConn(responder)
        table_desc, col_desc = PostgresDialect().fetch_descriptions(conn, "public", ["orders", "returns"])
        assert table_desc == {"orders": "All customer orders"}  # empty string filtered out
        assert col_desc == {"orders": {"id": "Primary key"}}

    def test_uses_pg_catalog_views(self):
        captured: list[str] = []

        def responder(sql, params):
            captured.append(sql)
            return []

        PostgresDialect().fetch_descriptions(_RecordingConn(responder), "public", ["t1"])
        assert any("pg_catalog.pg_class" in s for s in captured)
        assert any("pg_catalog.pg_attribute" in s and "col_description" in s for s in captured)

    def test_empty_tables_skips_queries(self):
        conn = _RecordingConn(lambda sql, p: [])
        assert PostgresDialect().fetch_descriptions(conn, "public", []) == ({}, {})
        assert conn.cursor_obj.calls == []


@pytest.mark.unit
class TestMySqlDescriptions:
    """MySQL exposes table_comment / column_comment in information_schema."""

    def test_returns_table_and_column_descriptions(self):
        def responder(sql, params):
            if "table_comment" in sql:
                return [("orders", "Customer orders"), ("logs", "")]
            if "column_comment" in sql:
                return [("orders", "id", "Order PK"), ("orders", "ts", "")]
            return []

        table_desc, col_desc = MySqlDialect().fetch_descriptions(_RecordingConn(responder), "shop", ["orders", "logs"])
        assert table_desc == {"orders": "Customer orders"}
        assert col_desc == {"orders": {"id": "Order PK"}}


@pytest.mark.unit
class TestSqlServerDescriptions:
    """SQL Server reads MS_Description from sys.extended_properties."""

    def test_uses_extended_properties_view(self):
        captured: list[str] = []

        def responder(sql, params):
            captured.append(sql)
            if "sys.tables" in sql and "ep.minor_id = 0" in sql:
                return [("orders", "Customer orders")]
            if "sys.columns" in sql:
                return [("orders", "id", "Order PK")]
            return []

        table_desc, col_desc = SqlServerDialect().fetch_descriptions(_RecordingConn(responder), "dbo", ["orders"])
        assert table_desc == {"orders": "Customer orders"}
        assert col_desc == {"orders": {"id": "Order PK"}}
        assert all("ms_description" in s for s in captured)


@pytest.mark.unit
class TestSnowflakeDescriptions:
    """Snowflake exposes COMMENT on information_schema.tables / columns."""

    def test_returns_descriptions_via_information_schema_comment(self):
        def responder(sql, params):
            if "from information_schema.tables" in sql:
                return [("ORDERS", "Customer orders")]
            if "from information_schema.columns" in sql:
                return [("ORDERS", "ID", "Order PK")]
            return []

        table_desc, col_desc = SnowflakeDialect().fetch_descriptions(_RecordingConn(responder), "ANALYTICS", ["ORDERS"])
        assert table_desc == {"ORDERS": "Customer orders"}
        assert col_desc == {"ORDERS": {"ID": "Order PK"}}


@pytest.mark.unit
class TestOracleDescriptions:
    """Oracle reads comments from ALL_TAB_COMMENTS / ALL_COL_COMMENTS."""

    def test_uses_oracle_comment_views_with_named_binds(self):
        captured: list[tuple[str, tuple]] = []

        def responder(sql, params):
            captured.append((sql, params))
            if "all_tab_comments" in sql:
                return [("EMP", "Employee table")]
            if "all_col_comments" in sql:
                return [("EMP", "ID", "Employee ID")]
            return []

        table_desc, col_desc = OracleDialect().fetch_descriptions(_RecordingConn(responder), "HR", ["EMP", "DEPT"])
        assert table_desc == {"EMP": "Employee table"}
        assert col_desc == {"EMP": {"ID": "Employee ID"}}
        # Oracle uses named binds (:1 = owner, :2..:N = table names).
        for sql, params in captured:
            assert ":1" in sql and ":2" in sql
            assert params == ("HR", "EMP", "DEPT")


@pytest.mark.unit
def test_default_dialect_returns_empty_descriptions():
    """Engines without a description override return ({}, {}) from the base."""
    from coa_sources.database.connectors.dialects import Dialect

    class _Custom(Dialect):
        pass

    assert _Custom().fetch_descriptions(MagicMock(), "x", ["y"]) == ({}, {})


@pytest.mark.unit
class TestJdbcDescriptionsWiring:
    """The JDBC discovery loop must surface dialect descriptions on the model."""

    @pytest.fixture
    def connector(self):
        return JdbcConnector()

    @pytest.fixture
    def base_config(self):
        return {
            "host": "db.example.com",
            "port": 5432,
            "engine": "POSTGRESQL",
            "database_name": "testdb",
            "credential_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds",
            "region": "us-east-1",
        }

    @staticmethod
    def _build_mock_conn():
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
                if "from information_schema.tables" in sql:
                    return [("orders", "BASE TABLE")]
                if "from information_schema.columns" in sql:
                    return [("orders", "id", "bigint", "NO"), ("orders", "amount", "decimal", "YES")]
                # PG schema-level description: FROM pg_catalog.pg_namespace.
                if "from pg_catalog.pg_namespace" in sql:
                    return [("",)]  # no schema comment
                # PG table-level description: FROM pg_catalog.pg_class.
                if "from pg_catalog.pg_class" in sql and "obj_description" in sql:
                    return [("orders", "Customer orders table")]
                if "col_description" in sql:
                    return [("orders", "id", "Order PK")]
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
    def test_discovery_populates_table_and_column_descriptions(self, mock_boto3, connector, base_config):
        self._patch_creds(mock_boto3)
        _mock_pg8000.connect.return_value = self._build_mock_conn()

        result = connector.discover_metadata(base_config)

        assert len(result.tables) == 1
        table = result.tables[0]
        # Table description came from the catalog and is tagged DETERMINISTIC.
        assert table.business_metadata.description == "Customer orders table"
        assert table.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        assert table.business_metadata.review_status == ReviewStatus.PENDING_REVIEW
        assert table.business_metadata.confidence == 1.0
        # The mock connection returns no schema-level COMMENT.
        assert table.database_description == ""

        cols = {c.name: c for c in table.columns}
        # Column with comment is surfaced ONLY on business_metadata.
        assert cols["id"].business_metadata.description == "Order PK"
        assert cols["id"].business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        assert cols["id"].business_metadata.review_status == ReviewStatus.APPROVED
        # Column without a comment stays at default.
        assert cols["amount"].business_metadata.description == ""
        assert cols["amount"].business_metadata.enrichment_source == ""

    @patch("coa_sources.database.connectors.jdbc.boto3")
    def test_description_query_failure_does_not_abort_discovery(self, mock_boto3, connector, base_config):
        """If the catalog rejects the description SQL, discovery still succeeds."""
        self._patch_creds(mock_boto3)
        conn = MagicMock()

        def make_cursor():
            cursor = MagicMock()
            state = {"sql": ""}

            def execute(sql, params=()):
                state["sql"] = sql.lower()
                if "obj_description" in state["sql"] or "col_description" in state["sql"]:
                    raise RuntimeError("permission denied")

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

        result = connector.discover_metadata(base_config)
        assert len(result.tables) == 1
        # No descriptions, but discovery must not raise.
        assert result.tables[0].business_metadata.description == ""


def _table_with_protected_description(table_desc: str, columns: list[Column], source: str) -> Table:
    return Table(
        name="orders",
        database="sales",
        columns=columns,
        business_metadata=BusinessMetadata(
            description=table_desc,
            enrichment_source=source,
        ),
    )


# Bedrock response with all four metadata fields populated, used to verify
# that AI-supplied synonyms/glossary/tags merge in even when description is
# preserved.
_AI_TABLE_OVERWRITE = {
    "table": {
        "description": "AI guessed description",
        "synonyms": ["ord"],
        "glossary_terms": ["sales"],
        "tags": ["transactional"],
    },
    "primary_key": {"columns": [], "confidence": 0.0},
    "foreign_keys": [],
    "columns": [],
}


@pytest.mark.unit
class TestEnricherPreservesProtectedDescriptions:
    """Code-side defense: descriptions tagged with any protected source are
    never overwritten by AI enrichment, even if the LLM ignores the prompt."""

    def test_protected_sources_set_membership(self) -> None:
        """The set must include all four authoritative provenance values."""
        assert EnrichmentSource.DETERMINISTIC in _PROTECTED_DESCRIPTION_SOURCES
        assert EnrichmentSource.CATALOG_EXISTING in _PROTECTED_DESCRIPTION_SOURCES
        assert EnrichmentSource.STEWARD_EDITED in _PROTECTED_DESCRIPTION_SOURCES
        assert EnrichmentSource.STEWARD_SPECIFIED in _PROTECTED_DESCRIPTION_SOURCES
        # AI-generated descriptions are NOT protected — they may be re-generated.
        assert EnrichmentSource.AI_GENERATED not in _PROTECTED_DESCRIPTION_SOURCES
        assert EnrichmentSource.AI_INFERRED not in _PROTECTED_DESCRIPTION_SOURCES

    @pytest.mark.parametrize(
        "source",
        [
            EnrichmentSource.DETERMINISTIC,
            EnrichmentSource.CATALOG_EXISTING,
            EnrichmentSource.STEWARD_EDITED,
            EnrichmentSource.STEWARD_SPECIFIED,
        ],
    )
    def test_table_description_preserved_for_each_protected_source(self, source: str) -> None:
        col = Column(name="id", data_type="bigint")
        table = _table_with_protected_description("Original description", [col], source)

        _apply_result(table, _AI_TABLE_OVERWRITE, table.columns)

        # Description and source attribution preserved.
        assert table.business_metadata.description == "Original description"
        assert table.business_metadata.enrichment_source == source
        # AI-supplied synonyms/glossary/tags still merged in (existing was empty).
        assert table.business_metadata.synonyms == ["ord"]
        assert table.business_metadata.glossary_terms == ["sales"]
        assert table.business_metadata.tags == ["transactional"]

    def test_table_existing_synonyms_not_overwritten_by_ai(self) -> None:
        col = Column(name="id", data_type="bigint")
        table = Table(
            name="orders",
            database="sales",
            columns=[col],
            business_metadata=BusinessMetadata(
                description="Original",
                synonyms=["existing-syn"],
                tags=["existing-tag"],
                enrichment_source=EnrichmentSource.DETERMINISTIC,
            ),
        )

        _apply_result(table, _AI_TABLE_OVERWRITE, table.columns)

        # Existing synonyms/tags win when both sides have a value.
        assert table.business_metadata.synonyms == ["existing-syn"]
        assert table.business_metadata.tags == ["existing-tag"]
        # Glossary was empty so AI's value is merged in.
        assert table.business_metadata.glossary_terms == ["sales"]

    @pytest.mark.parametrize(
        "source",
        [
            EnrichmentSource.DETERMINISTIC,
            EnrichmentSource.CATALOG_EXISTING,
            EnrichmentSource.STEWARD_EDITED,
            EnrichmentSource.STEWARD_SPECIFIED,
        ],
    )
    def test_column_description_preserved_for_each_protected_source(self, source: str) -> None:
        col = Column(
            name="id",
            data_type="bigint",
            business_metadata=BusinessMetadata(description="Order PK", enrichment_source=source),
        )
        table = Table(name="orders", database="sales", columns=[col])

        result = {
            "table": {"description": "Orders table", "synonyms": [], "glossary_terms": [], "tags": []},
            "primary_key": {"columns": [], "confidence": 0.0},
            "foreign_keys": [],
            "columns": [
                {
                    "name": "id",
                    "description": "AI rewrote this",
                    "synonyms": ["pk"],
                    "glossary_terms": [],
                    "tags": [],
                    "confidence": 0.9,
                }
            ],
        }
        _apply_result(table, result, table.columns)

        out = table.columns[0]
        assert out.business_metadata.description == "Order PK"
        assert out.business_metadata.enrichment_source == source
        # AI synonyms still merged when existing was empty.
        assert out.business_metadata.synonyms == ["pk"]

    def test_form_round_trip_preserves_business_metadata_description(self) -> None:
        """Form serialize → deserialize round-trip preserves the column's
        single canonical description home and its provenance."""
        import json as _json

        from coa_common.datazone_forms import deserialize_form

        payload = {
            "tableName": "orders",
            "databaseName": "sales",
            "description": "",
            "enrichmentSource": "",
            "columns": _json.dumps(
                [
                    {
                        "name": "id",
                        "data_type": "bigint",
                        "nullable": False,
                        "is_partition_key": False,
                        "business_metadata": {
                            "description": "Order PK",
                            "enrichment_source": EnrichmentSource.DETERMINISTIC,
                            "review_status": ReviewStatus.APPROVED,
                            "confidence": 1.0,
                        },
                    },
                    {
                        "name": "amount",
                        "data_type": "decimal",
                    },
                ]
            ),
        }

        table = deserialize_form(payload)
        cols = {c.name: c for c in table.columns}
        assert cols["id"].business_metadata.description == "Order PK"
        assert cols["id"].business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        assert cols["id"].business_metadata.review_status == ReviewStatus.APPROVED
        assert cols["id"].business_metadata.confidence == 1.0
        assert cols["amount"].business_metadata.description == ""

    def test_ai_still_applies_when_no_protected_description(self) -> None:
        col = Column(name="id", data_type="bigint")
        table = Table(name="orders", database="sales", columns=[col])

        result = {
            "table": {"description": "AI description", "synonyms": [], "glossary_terms": [], "tags": []},
            "primary_key": {"columns": [], "confidence": 0.0},
            "foreign_keys": [],
            "columns": [
                {
                    "name": "id",
                    "description": "AI column description",
                    "synonyms": [],
                    "glossary_terms": [],
                    "tags": [],
                    "confidence": 0.9,
                }
            ],
        }
        _apply_result(table, result, table.columns)

        assert table.business_metadata.description == "AI description"
        assert table.business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED
        assert table.business_metadata.review_status == ReviewStatus.PENDING_REVIEW
        assert table.columns[0].business_metadata.description == "AI column description"
        assert table.columns[0].business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED
        assert table.columns[0].business_metadata.review_status == ReviewStatus.PENDING_REVIEW

    def test_ai_generated_description_not_protected(self) -> None:
        """A column previously enriched (AI_GENERATED) is overwritten by a
        re-enrichment — only protected sources are immune."""
        col = Column(
            name="id",
            data_type="bigint",
            business_metadata=BusinessMetadata(
                description="Old AI guess",
                enrichment_source=EnrichmentSource.AI_GENERATED,
            ),
        )
        table = Table(name="orders", database="sales", columns=[col])

        result = {
            "table": {"description": "AI description", "synonyms": [], "glossary_terms": [], "tags": []},
            "primary_key": {"columns": [], "confidence": 0.0},
            "foreign_keys": [],
            "columns": [
                {
                    "name": "id",
                    "description": "Better AI guess",
                    "synonyms": [],
                    "glossary_terms": [],
                    "tags": [],
                    "confidence": 0.9,
                }
            ],
        }
        _apply_result(table, result, table.columns)

        assert table.columns[0].business_metadata.description == "Better AI guess"
        assert table.columns[0].business_metadata.enrichment_source == EnrichmentSource.AI_GENERATED


@pytest.mark.unit
class TestPromptDescriptionAwareness:
    def test_columns_with_business_metadata_description_listed(self):
        cols = [
            Column(
                name="id",
                data_type="bigint",
                business_metadata=BusinessMetadata(description="Order PK"),
            ),
            Column(
                name="amount",
                data_type="decimal",
                business_metadata=BusinessMetadata(description="Amount paid"),
            ),
            Column(name="ts", data_type="timestamp"),
        ]

        prompt = build_user_prompt("orders", "sales", cols)

        assert "columns_with_descriptions: ['id', 'amount']" in prompt

    def test_no_marker_when_no_descriptions(self):
        cols = [Column(name="id", data_type="bigint")]
        prompt = build_user_prompt("orders", "sales", cols)
        assert "columns_with_descriptions" not in prompt

    def test_existing_table_description_propagated(self):
        cols = [Column(name="id", data_type="bigint")]
        prompt = build_user_prompt("orders", "sales", cols, existing_description="Customer orders")
        assert "description: Customer orders" in prompt
