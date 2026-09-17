# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for GlueCatalogConnector."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.domain_models import Column, EnrichmentSource, Table
from coa_sources.database.connectors.glue_catalog import (
    GlueCatalogConnector,
)


@pytest.fixture
def connector():
    return GlueCatalogConnector()


class TestTestConnection:
    def test_missing_database_name(self, connector):
        result = connector.test_connection({})
        assert not result.success
        assert "database_name" in result.message

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_database_not_found(self, mock_boto3, connector):
        mock_client = MagicMock()
        not_found = type("EntityNotFoundException", (Exception,), {})
        mock_client.exceptions.EntityNotFoundException = not_found
        mock_client.get_database.side_effect = not_found()
        mock_boto3.client.return_value = mock_client

        result = connector.test_connection({"database_name": "nonexistent", "region": "us-east-1"})
        assert not result.success
        assert "does not exist" in result.message

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_successful_connection(self, mock_boto3, connector):
        mock_client = MagicMock()
        mock_client.get_database.return_value = {"Database": {"Name": "test-db", "Description": "Test database"}}
        mock_client.get_tables.return_value = {"TableList": [{"Name": "table1"}, {"Name": "table2"}]}
        mock_boto3.client.return_value = mock_client

        result = connector.test_connection({"database_name": "test-db", "region": "us-east-1"})
        assert result.success
        assert len(result.checks) == 2
        assert result.checks[0].status == "ok"
        assert result.checks[1].status == "ok"

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_empty_database_warning(self, mock_boto3, connector):
        mock_client = MagicMock()
        mock_client.get_database.return_value = {"Database": {"Name": "empty-db"}}
        mock_client.get_tables.return_value = {"TableList": []}
        mock_boto3.client.return_value = mock_client

        result = connector.test_connection({"database_name": "empty-db", "region": "us-east-1"})
        assert result.success
        assert result.checks[1].status == "warning"


class TestLakeFormationSelfGrantGate:
    """F-8: the self-grant assumes a Lake Formation admin role and can unlock
    SELECT on any database in the account, for the shared serve role as well as
    the connector. It only fires for a target the discovery handler has verified
    belongs to the requesting namespace.
    """

    def _denied_glue(self):
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
        client.get_database.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetDatabase")
        return client

    @patch("coa_sources.database.connectors.glue_catalog.lf_grant")
    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_verified_target_may_self_grant(self, mock_boto3, mock_lf, connector):
        mock_boto3.client.return_value = self._denied_glue()
        mock_lf.is_access_denied.return_value = True
        mock_lf.attempt_self_grant.return_value = False

        connector.test_connection({"database_name": "mydb", "region": "us-east-1", "lf_self_grant_allowed": True})

        assert mock_lf.attempt_self_grant.call_args.kwargs["owner_verified"] is True

    @patch("coa_sources.database.connectors.glue_catalog.lf_grant")
    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_an_absent_flag_reads_as_unverified(self, mock_boto3, mock_lf, connector):
        """Fail-closed on omission: a caller that never ran the ownership check
        must not look identical to one that ran it and passed."""
        mock_boto3.client.return_value = self._denied_glue()
        mock_lf.is_access_denied.return_value = True
        mock_lf.attempt_self_grant.return_value = False

        connector.test_connection({"database_name": "mydb", "region": "us-east-1"})

        assert mock_lf.attempt_self_grant.call_args.kwargs["owner_verified"] is False


class TestDiscoverMetadata:
    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_discovers_typed_tables_and_columns(self, mock_boto3, connector):
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "Description": "Order records",
                        "Parameters": {"classification": "parquet"},
                        "StorageDescriptor": {
                            "Location": "s3://bucket/orders/",
                            "Columns": [
                                {"Name": "order_id", "Type": "bigint", "Comment": "PK"},
                                {"Name": "customer_id", "Type": "bigint", "Comment": ""},
                                {"Name": "amount", "Type": "decimal(10,2)", "Comment": ""},
                            ],
                        },
                        "PartitionKeys": [{"Name": "order_date", "Type": "date", "Comment": ""}],
                    }
                ]
            }
        ]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client

        result = connector.discover_metadata(
            {
                "database_name": "sales_db",
                "data_source_id": "DS#123",
                "namespace_id": "ns-test",
                "region": "us-east-1",
            }
        )

        assert len(result.tables) == 1
        table = result.tables[0]
        assert isinstance(table, Table)
        assert table.name == "orders"
        assert table.table_id == "sales_db.orders"
        assert table.technical_metadata_hash != ""
        assert table.technical_metadata.column_count == 4
        assert table.technical_metadata.format == "parquet"
        assert table.technical_metadata.location == "s3://bucket/orders/"
        assert table.technical_metadata.partition_keys == ["order_date"]

        assert len(table.columns) == 4
        assert all(isinstance(c, Column) for c in table.columns)

        partition_cols = [c for c in table.columns if c.is_partition_key]
        assert len(partition_cols) == 1
        assert partition_cols[0].name == "order_date"
        assert partition_cols[0].nullable is False

        assert result.total_columns == 4

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_source_owned_fields_are_populated_for_rescan_diff(self, mock_boto3, connector):
        """A discovered Table carries exactly the source-owned fields ``rescan.diff_tables``
        compares — so a Glue re-scan can actually detect drift.

        Pins: per-column ``data_type`` / ``is_partition_key`` / hardcoded ``nullable``
        (Glue exposes no nullability: regular cols → True, partition cols → False);
        ``technical_metadata`` format/location/partition_keys; a Glue Comment surfaced as
        a ``DETERMINISTIC`` (source-derived) ``business_metadata.description`` — the exact
        provenance that makes ``diff_tables`` compare it — while a comment-less column is
        left empty and NOT source-derived (so a no-op re-scan won't flag it); and that no
        primary/foreign keys are produced (documents the known Glue limitation).
        """
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "Description": "Order records table",
                        "Parameters": {"classification": "parquet"},
                        "StorageDescriptor": {
                            "Location": "s3://bucket/orders/",
                            "Columns": [
                                {"Name": "order_id", "Type": "bigint", "Comment": "unique order id"},
                                {"Name": "amount", "Type": "decimal(10,2)", "Comment": ""},
                            ],
                        },
                        "PartitionKeys": [{"Name": "order_date", "Type": "date", "Comment": ""}],
                    }
                ]
            }
        ]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client

        result = connector.discover_metadata({"database_name": "sales_db", "region": "us-east-1"})
        (table,) = result.tables
        cols = {c.name: c for c in table.columns}

        # data_type + is_partition_key
        assert cols["order_id"].data_type == "bigint"
        assert cols["order_id"].is_partition_key is False
        assert cols["order_date"].data_type == "date"
        assert cols["order_date"].is_partition_key is True

        # hardcoded nullability (Glue exposes none): regular → True, partition → False
        assert cols["order_id"].nullable is True
        assert cols["amount"].nullable is True
        assert cols["order_date"].nullable is False

        # technical metadata the diff reads
        assert table.technical_metadata.format == "parquet"
        assert table.technical_metadata.location == "s3://bucket/orders/"
        assert table.technical_metadata.partition_keys == ["order_date"]

        # a Glue Comment becomes a source-derived (DETERMINISTIC) description → diff compares it
        assert cols["order_id"].business_metadata.description == "unique order id"
        assert cols["order_id"].business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC
        # no comment → empty and NOT source-derived, so a no-op re-scan won't flag it
        assert cols["amount"].business_metadata.description == ""
        assert cols["amount"].business_metadata.enrichment_source == ""
        # table-level Description is likewise source-derived
        assert table.business_metadata.description == "Order records table"
        assert table.business_metadata.enrichment_source == EnrichmentSource.DETERMINISTIC

        # known Glue limitation: no primary/foreign keys are discovered
        assert table.primary_key.columns == []
        assert table.foreign_keys == []

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_table_exclude_filter(self, mock_boto3, connector):
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "StorageDescriptor": {"Columns": [], "Location": ""},
                        "PartitionKeys": [],
                    },
                    {
                        "Name": "orders_staging",
                        "StorageDescriptor": {"Columns": [], "Location": ""},
                        "PartitionKeys": [],
                    },
                ]
            }
        ]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client

        result = connector.discover_metadata(
            {
                "database_name": "db",
                "table_exclude_filter": "*_staging",
                "region": "us-east-1",
            }
        )

        assert len(result.tables) == 1
        assert result.tables[0].name == "orders"

    @patch("coa_sources.database.connectors.glue_catalog.boto3")
    def test_technical_metadata_hash_deterministic(self, mock_boto3, connector):
        """Same columns in different order produce the same hash."""
        mock_client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "t1",
                        "StorageDescriptor": {
                            "Columns": [
                                {"Name": "b", "Type": "int"},
                                {"Name": "a", "Type": "string"},
                            ],
                            "Location": "",
                        },
                        "PartitionKeys": [],
                    }
                ]
            }
        ]
        mock_client.get_paginator.return_value = paginator
        mock_boto3.client.return_value = mock_client

        r1 = connector.discover_metadata({"database_name": "db", "region": "us-east-1"})

        paginator.paginate.return_value = [
            {
                "TableList": [
                    {
                        "Name": "t1",
                        "StorageDescriptor": {
                            "Columns": [
                                {"Name": "a", "Type": "string"},
                                {"Name": "b", "Type": "int"},
                            ],
                            "Location": "",
                        },
                        "PartitionKeys": [],
                    }
                ]
            }
        ]
        r2 = connector.discover_metadata({"database_name": "db", "region": "us-east-1"})

        assert r1.tables[0].technical_metadata_hash == r2.tables[0].technical_metadata_hash

    @patch("coa_sources.database.connectors.sts_assume.boto3")
    def test_cross_account_role(self, mock_boto3, connector):
        """Verify STS AssumeRole is called for cross-account access."""
        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIA...",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }

        mock_session = MagicMock()
        mock_glue = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"TableList": []}]
        mock_glue.get_paginator.return_value = paginator
        mock_session.client.return_value = mock_glue

        mock_boto3.client.return_value = mock_sts
        mock_boto3.Session.return_value = mock_session

        connector.discover_metadata(
            {
                "database_name": "db",
                "cross_account_role_arn": "arn:aws:iam::999999999999:role/test",
                "external_id": "coa-dev-ns-1",
                "region": "us-east-1",
            }
        )

        mock_sts.assume_role.assert_called_once()
        mock_boto3.Session.assert_called_once()

    @patch("coa_sources.database.connectors.sts_assume.boto3")
    def test_cross_account_role_passes_external_id(self, mock_boto3, connector):
        """The configured external_id must reach AssumeRole."""
        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIA...",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_glue.get_database.return_value = {"Database": {"Name": "db"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [{"TableList": []}]
        mock_glue.get_paginator.return_value = paginator
        mock_session.client.return_value = mock_glue
        mock_boto3.client.return_value = mock_sts
        mock_boto3.Session.return_value = mock_session

        connector.discover_metadata(
            {
                "database_name": "db",
                "cross_account_role_arn": "arn:aws:iam::999999999999:role/test",
                "external_id": "ext-xyz",
                "region": "us-east-1",
            }
        )

        assert mock_sts.assume_role.call_args.kwargs["ExternalId"] == "ext-xyz"
        assert mock_sts.assume_role.call_args.kwargs["RoleArn"] == "arn:aws:iam::999999999999:role/test"

    @patch("coa_sources.database.connectors.sts_assume.boto3")
    def test_cross_account_role_without_external_id_is_refused(self, mock_boto3, connector):
        """No external_id → refuse to assume at all (confused-deputy guard).

        An assume with no ExternalId carries no evidence of which namespace asked
        for it, so a caller could point a source at another tenant's role and read
        it. The connector must raise rather than fall back to an unconditioned
        assume, which is what it used to do.
        """
        mock_sts = MagicMock()
        mock_boto3.client.return_value = mock_sts

        with pytest.raises(ValueError, match="external_id is required"):
            connector.discover_metadata(
                {
                    "database_name": "db",
                    "cross_account_role_arn": "arn:aws:iam::999999999999:role/test",
                    "region": "us-east-1",
                }
            )

        mock_sts.assume_role.assert_not_called()

    @patch("coa_sources.database.connectors.sts_assume.boto3")
    def test_role_session_name_carries_namespace(self, mock_boto3, connector):
        """RoleSessionName reaches the data owner's CloudTrail — it must name the namespace."""
        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}
        }
        mock_session = MagicMock()
        mock_glue = MagicMock()
        mock_glue.get_database.return_value = {"Database": {"Name": "db"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [{"TableList": []}]
        mock_glue.get_paginator.return_value = paginator
        mock_session.client.return_value = mock_glue
        mock_boto3.client.return_value = mock_sts
        mock_boto3.Session.return_value = mock_session

        connector.discover_metadata(
            {
                "database_name": "db",
                "cross_account_role_arn": "arn:aws:iam::999999999999:role/test",
                "external_id": "coa-dev-ns-42",
                "namespace_id": "ns-42",
                "region": "us-east-1",
            }
        )

        session_name = mock_sts.assume_role.call_args.kwargs["RoleSessionName"]
        assert "ns-42" in session_name
        assert len(session_name) <= 64
