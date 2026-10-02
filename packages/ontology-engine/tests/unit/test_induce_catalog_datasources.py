# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for list_datasources in induce_catalog — new sources table schema and filtering."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX


def _make_request(table_items: list[dict], table_name: str = f"{RESOURCE_PREFIX}-dev-sources") -> MagicMock:
    """Build a mock FastAPI request with a DynamoDB table returning the given items."""
    mock_table = MagicMock()
    mock_table.scan.return_value = {"Items": table_items}

    mock_resource = MagicMock()
    mock_resource.Table.return_value = mock_table

    request = MagicMock()
    request.app.state.config = {
        "smus_datasources_table": table_name,
        "smus_region": "us-east-1",
    }
    return request, mock_resource


@pytest.mark.unit
class TestListDatasourcesFiltering:
    """Tests that list_datasources correctly filters to DATABASE sources only."""

    def test_includes_glue_database_source(self):
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#abc",
                    "name": "my-db",
                    "status": "APPROVED",
                    "sourceType": "DATABASE",
                    "sourceSubType": "GLUE_DATABASE",
                    "tablesDiscovered": 5,
                    "tablesApproved": 3,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert len(result) == 1
        assert result[0]["datasourceId"] == "abc"
        assert result[0]["sourceType"] == "DATABASE"

    def test_includes_jdbc_database_source(self):
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#jdbc1",
                    "name": "jdbc-db",
                    "status": "APPROVED",
                    "sourceType": "DATABASE",
                    "sourceSubType": "JDBC_DATABASE",
                    "tablesDiscovered": 10,
                    "tablesApproved": 10,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert len(result) == 1
        assert result[0]["datasourceId"] == "jdbc1"

    def test_includes_custom_connector_source(self):
        """The DATABASE sub-types are an inline literal tuple here rather than the
        Smithy enum, so a sub-type renamed or added upstream drops out of induction
        silently — the source simply stops appearing, with no error anywhere."""
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#conn1",
                    "name": "acme-sap",
                    "status": "APPROVED",
                    "sourceType": "DATABASE",
                    "sourceSubType": "CUSTOM_CONNECTOR",
                    "tablesDiscovered": 4,
                    "tablesApproved": 4,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert len(result) == 1
        assert result[0]["datasourceId"] == "conn1"

    def test_includes_databricks_sql_warehouse_source(self):
        """Missing this sub-type fails CLOSED: the source never appears in the workbench's
        datasource list, so it can never be induced, with no error raised anywhere."""
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#dbx1",
                    "name": "acme-databricks",
                    "status": "APPROVED",
                    "sourceType": "DATABASE",
                    "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
                    "tablesDiscovered": 6,
                    "tablesApproved": 6,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert len(result) == 1
        assert result[0]["datasourceId"] == "dbx1"

    def test_excludes_an_unknown_database_sub_type(self):
        """An unrecognised DATABASE sub-type is dropped rather than induced on a guess."""
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#future1",
                    "name": "not-yet-supported",
                    "status": "APPROVED",
                    "sourceType": "DATABASE",
                    "sourceSubType": "SOME_FUTURE_WAREHOUSE",
                    "tablesDiscovered": 1,
                    "tablesApproved": 1,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert result == []

    def test_excludes_document_s3_source(self):
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#doc1",
                    "name": "my-docs",
                    "status": "COMPLETED",
                    "sourceType": "DOCUMENTS",
                    "sourceSubType": "S3",
                    "tablesDiscovered": 0,
                    "tablesApproved": 0,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert result == []

    def test_excludes_local_upload_source(self):
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#up1",
                    "name": "upload",
                    "status": "COMPLETED",
                    "sourceType": "DOCUMENTS",
                    "sourceSubType": "LOCAL_UPLOAD",
                    "tablesDiscovered": 0,
                    "tablesApproved": 0,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert result == []

    def test_mixed_sources_only_returns_database(self):
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#db1",
                    "name": "glue-db",
                    "status": "APPROVED",
                    "sourceType": "DATABASE",
                    "sourceSubType": "GLUE_DATABASE",
                    "tablesDiscovered": 10,
                    "tablesApproved": 8,
                    "namespaceId": "ns1",
                },
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#doc1",
                    "name": "s3-docs",
                    "status": "COMPLETED",
                    "sourceType": "DOCUMENTS",
                    "sourceSubType": "S3",
                    "tablesDiscovered": 0,
                    "tablesApproved": 0,
                    "namespaceId": "ns1",
                },
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#db2",
                    "name": "jdbc-db",
                    "status": "APPROVED",
                    "sourceType": "DATABASE",
                    "sourceSubType": "JDBC_DATABASE",
                    "tablesDiscovered": 5,
                    "tablesApproved": 5,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert len(result) == 2
        ids = {r["datasourceId"] for r in result}
        assert ids == {"db1", "db2"}

    def test_extracts_source_id_from_src_prefix(self):
        """SK=SRC#{sourceId} — bare UUID is extracted correctly."""
        from coa_ontology.induce_catalog import list_datasources

        request, mock_resource = _make_request(
            [
                {
                    "PK": "NS#ns1",
                    "SK": "SRC#my-source-uuid",
                    "name": "db",
                    "sourceType": "DATABASE",
                    "sourceSubType": "GLUE_DATABASE",
                    "status": "APPROVED",
                    "tablesDiscovered": 1,
                    "tablesApproved": 1,
                    "namespaceId": "ns1",
                },
            ]
        )

        with patch("boto3.resource", return_value=mock_resource):
            result = list_datasources(request, "ns1")

        assert result[0]["datasourceId"] == "my-source-uuid"

    def test_returns_empty_when_no_table_configured(self):
        """Returns empty list when table name is not configured."""
        from coa_ontology.induce_catalog import list_datasources

        request = MagicMock()
        request.app.state.config = {}

        result = list_datasources(request, "ns1")

        assert result == []
