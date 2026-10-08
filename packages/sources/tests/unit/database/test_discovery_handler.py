# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the discovery handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.domain_models import (
    BusinessMetadata,
    Column,
    DiscoveredMetadata,
    EnrichmentSource,
    ReviewStatus,
    Table,
)
from coa_control_plane_server.models.source_status import SourceStatus
from coa_sources.database import glue_ownership as _go
from coa_sources.database.connectors import get_connector
from coa_sources.database.connectors.base import (
    ConnectionTestResult,
)
from coa_sources.database.errors import PermanentScanError, TransientScanError

from tests.unit.conftest import dao_double

MODULE = "coa_sources.database.pipeline.discovery_handler"


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("SOURCES_TABLE", "test-datasources")
    monkeypatch.setenv("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
    monkeypatch.setenv("SMUS_DOMAIN_ID", "dz-test-domain")
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset module-level DAO singletons between tests."""
    import coa_sources.database.pipeline.discovery_handler as mod

    mod._ds_dao = None
    mod._scan_dao = None
    mod._ns_dao = None
    yield
    mod._ds_dao = None
    mod._scan_dao = None
    mod._ns_dao = None


class TestDiscoveryHandler:
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_glue_database_discovery(self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write):
        from coa_sources.database.pipeline.discovery_handler import (
            handler,
        )

        # Mock DAOs
        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {
                "databaseName": "analytics_db",
                "catalogId": "123456789012",
                "region": "us-east-1",
            },
        }
        mock_get_ds.return_value = mock_ds_dao

        mock_scan_dao = dao_double()
        mock_get_scan.return_value = mock_scan_dao

        mock_ns_dao = dao_double()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_get_ns.return_value = mock_ns_dao

        # Mock connector
        mock_connector = MagicMock()
        mock_connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        test_table = Table(
            name="events",
            database="analytics_db",
            columns=[Column(name="id", data_type="bigint")],
        )
        mock_connector.discover_metadata.return_value = DiscoveredMetadata(tables=[test_table])
        mock_get_connector.return_value = mock_connector

        mock_write.return_value = {"assets_created": 1, "assets_revised": 0}

        result = handler(
            {
                "datasourceId": "DS#ds-123",
                "scanJobId": "SCAN#scan-456",
                "namespaceId": "ns-test",
                "scanType": "full",
            },
            None,
        )

        assert result["tablesDiscovered"] == 1
        assert result["columnsDiscovered"] == 1
        assert result["assetsCreated"] == 1
        mock_get_connector.assert_called_once_with("GLUE_DATABASE")
        mock_scan_dao.update.assert_called_once()
        # The source-record update persists the distinct discovered schemas
        # (used by the federation step to scope Lake Formation grants).
        schema_updates = [
            c.kwargs["update_fields"]["discoveredSchemas"]
            for c in mock_ds_dao.update.call_args_list
            if "discoveredSchemas" in c.kwargs.get("update_fields", {})
        ]
        assert schema_updates == [["analytics_db"]]
        # Native Glue sources are marked queryable after a successful scan.
        queryable_updates = [
            c.kwargs["update_fields"]["queryable"]
            for c in mock_ds_dao.update.call_args_list
            if "queryable" in c.kwargs.get("update_fields", {})
        ]
        assert queryable_updates == [True]
        # A first scan must NOT write tablesApproved — doing so would clobber the
        # create-time 0 and any per-table review increments. Only a re-scan
        # recomputes it (see the rescan recompute test below).
        assert all("tablesApproved" not in c.kwargs.get("update_fields", {}) for c in mock_ds_dao.update.call_args_list)

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_snowflake_discovers_via_driver_with_warehouse(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        from coa_sources.database.connectors.base import ConnectionTestResult
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "JDBC_DATABASE",
            "configuration": {
                "databaseName": "db",
                "engine": "SNOWFLAKE",
                "host": "acme.snowflakecomputing.com",
                "warehouse": "WH",
                "role": "R",
            },
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-123"}))
        mock_write.return_value = {"assets_created": 1}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="t", database="public", columns=[Column(name="c", data_type="text")])]
        )
        mock_get_connector.return_value = connector

        result = handler(
            {"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-1", "scanType": "full"}, None
        )

        # The handler is engine-agnostic: it routes every JDBC sub-type to the same
        # connector. The connector is mocked here, which is what keeps this covering
        # the handler's warehouse/role pass-through regardless of the engine.
        mock_get_connector.assert_called_once_with("JDBC_DATABASE")
        # warehouse/role are passed through to the connector for the Snowflake dialect.
        cfg = connector.discover_metadata.call_args[0][0]
        assert cfg["warehouse"] == "WH"
        assert cfg["role"] == "R"
        assert result["tablesDiscovered"] == 1

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_cross_account_role_and_external_id_passthrough(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """LEGACY BRIDGE: an externalId already in the stored config still wins.

        Those roles were onboarded before the derived ExternalId and their trust
        policy pins the old value, so honouring it is what keeps their next scan
        working. A caller can no longer get a value into storage — see
        ``database_routes._strip_external_id``."""
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {
                "databaseName": "analytics_db",
                "catalogId": "123456789012",
                "region": "us-east-1",
                "crossAccountRoleArn": "-dev-datasource-access-acme",
                "externalId": "ext-abc",
            },
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-123"}))
        mock_write.return_value = {"assets_created": 1}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="t", database="analytics_db", columns=[Column(name="c", data_type="text")])]
        )
        mock_get_connector.return_value = connector

        handler({"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-1", "scanType": "full"}, None)

        # Both test_connection and discover_metadata receive the same config.
        test_cfg = connector.test_connection.call_args[0][0]
        disc_cfg = connector.discover_metadata.call_args[0][0]
        for cfg in (test_cfg, disc_cfg):
            assert cfg["cross_account_role_arn"] == ("-dev-datasource-access-acme")
            assert cfg["external_id"] == "ext-abc"

    @patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"})
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_external_id_is_derived_from_namespace_not_the_request(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """The ExternalId presented on the assume comes from the namespace.

        The role ARN is caller-supplied, so this binding is what stops a caller
        with manageSource on one namespace from pointing a source at another
        tenant's datasource-access role and reading it (confused deputy).
        """
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {
                "databaseName": "analytics_db",
                "catalogId": "123456789012",
                "region": "us-east-1",
                "crossAccountRoleArn": "arn:aws:iam::999999999999:role/coa-dev-datasource-access-acme",
            },
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-123"}))
        mock_write.return_value = {"assets_created": 1}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="t", database="analytics_db", columns=[Column(name="c", data_type="text")])]
        )
        mock_get_connector.return_value = connector

        handler({"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-9", "scanType": "full"}, None)

        for cfg in (
            connector.test_connection.call_args[0][0],
            connector.discover_metadata.call_args[0][0],
        ):
            assert cfg["external_id"] == "coa-dev-ns-9"

    @patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"})
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_two_namespaces_get_different_external_ids(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """The same role ARN in two namespaces must present different ExternalIds.

        This is the property that makes the trust policy an authorization list:
        tenant B pins its own namespace's value, so tenant A's scan of B's role
        is denied by STS.
        """
        from coa_sources.database.pipeline.discovery_handler import handler

        shared_role = "arn:aws:iam::999999999999:role/coa-dev-datasource-access-acme"
        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {
                "databaseName": "analytics_db",
                "catalogId": "123456789012",
                "region": "us-east-1",
                "crossAccountRoleArn": shared_role,
            },
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-123"}))
        mock_write.return_value = {"assets_created": 1}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="t", database="analytics_db", columns=[Column(name="c", data_type="text")])]
        )
        mock_get_connector.return_value = connector

        seen = []
        for namespace_id in ("ns-tenant-a", "ns-tenant-b"):
            handler(
                {
                    "datasourceId": "DS#ds-1",
                    "scanJobId": "SCAN#s",
                    "namespaceId": namespace_id,
                    "scanType": "full",
                },
                None,
            )
            seen.append(connector.discover_metadata.call_args[0][0]["external_id"])

        assert seen == ["coa-dev-ns-tenant-a", "coa-dev-ns-tenant-b"]
        assert len(set(seen)) == 2

    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    def test_datasource_not_found(self, mock_get_ds, mock_get_scan):
        from coa_sources.database.pipeline.discovery_handler import (
            handler,
        )

        mock_dao = MagicMock()
        mock_dao.get.return_value = None
        mock_get_ds.return_value = mock_dao
        mock_get_scan.return_value = MagicMock()

        with pytest.raises(PermanentScanError, match="Data source not found"):
            handler(
                {
                    "datasourceId": "DS#nonexistent",
                    "scanJobId": "SCAN#x",
                    "namespaceId": "ns-test",
                },
                None,
            )

    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    def test_unsupported_source_type(self, mock_get_ds, mock_get_scan):
        from coa_sources.database.pipeline.discovery_handler import (
            handler,
        )

        mock_dao = MagicMock()
        mock_dao.get.return_value = {
            "sourceSubType": "UNKNOWN_TYPE",
            "configuration": {},
        }
        mock_get_ds.return_value = mock_dao
        mock_get_scan.return_value = MagicMock()

        with pytest.raises(PermanentScanError, match="Unsupported source type"):
            handler(
                {
                    "datasourceId": "DS#unknown-1",
                    "scanJobId": "SCAN#x",
                    "namespaceId": "ns-test",
                },
                None,
            )

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_too_many_tables_fails_fast(self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write):
        """When discovery yields more tables than MAX_TABLES_PER_SOURCE, the
        scan must fail fast with an actionable message and NOT attempt the
        DataZone write (which would otherwise hit the Lambda timeout)."""
        from coa_sources.database.pipeline import discovery_handler as mod
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "db", "catalogId": "123456789012", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_scan_dao = dao_double()
        mock_get_scan.return_value = mock_scan_dao
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))

        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name=f"t{i}", database="db", columns=[]) for i in range(3)]
        )
        mock_get_connector.return_value = connector

        with (
            patch.object(mod, "MAX_TABLES_PER_SOURCE", 2),
            pytest.raises(PermanentScanError, match="exceeding the limit"),
        ):
            handler(
                {"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-1", "scanType": "full"},
                None,
            )

        # Fail-fast: no DataZone write attempted.
        mock_write.assert_not_called()
        # Source marked SCAN_FAILED via the error path.
        statuses = [
            c.kwargs["update_fields"].get("status")
            for c in mock_ds_dao.update.call_args_list
            if "status" in c.kwargs.get("update_fields", {})
        ]
        assert SourceStatus.SCAN_FAILED in statuses

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_rescan_merges_onto_accepted_and_refreshes_source_summary(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
    ):
        """A re-scan (isRescan=True) merges the fresh scan onto the accepted
        assets — preserving curated metadata, resetting only changed items to
        PENDING_REVIEW — and refreshes the source summary to the fresh scan
        (showing the previous scan's counts after a re-scan would be wrong; a
        reject restores the pre-rescan counts from the backup)."""
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "analytics_db", "catalogId": "123456789012", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
        mock_write.return_value = {"assets_created": 0, "assets_revised": 1}

        # Accepted (approved) asset: orders.total is steward-edited + APPROVED.
        mock_read_accepted.return_value = [
            Table(
                name="orders",
                database="analytics_db",
                columns=[
                    Column(
                        name="total",
                        data_type="decimal",
                        business_metadata=BusinessMetadata(
                            description="net total (steward)",
                            enrichment_source=EnrichmentSource.STEWARD_EDITED,
                            review_status=ReviewStatus.APPROVED,
                        ),
                    )
                ],
            )
        ]

        # Fresh scan: same table, column type changed decimal -> varchar.
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="varchar")])]
        )
        mock_get_connector.return_value = connector

        handler(
            {
                "datasourceId": "DS#ds-1",
                "scanJobId": "SCAN#s",
                "namespaceId": "ns-1",
                "scanType": "full",
                "isRescan": True,
            },
            None,
        )

        # Accepted assets were read to compute the diff.
        mock_read_accepted.assert_called_once()

        # The write received the MERGED table: fresh type, preserved steward
        # description, review reset to PENDING_REVIEW.
        written = mock_write.call_args.kwargs["metadata"]
        (table,) = written.tables
        (col,) = table.columns
        assert col.data_type == "varchar"
        assert col.business_metadata.description == "net total (steward)"
        assert col.business_metadata.review_status == ReviewStatus.PENDING_REVIEW

        # The source summary IS refreshed to the fresh scan on a re-scan (showing
        # the old scan's counts would be wrong). A reject restores the pre-rescan
        # counts from the backup — covered in the worker tests.
        summary_writes = [
            c.kwargs.get("update_fields", {})
            for c in mock_ds_dao.update.call_args_list
            if "tablesDiscovered" in c.kwargs.get("update_fields", {})
        ]
        assert len(summary_writes) == 1
        assert summary_writes[0]["tablesDiscovered"] == 1
        assert summary_writes[0]["discoveredSchemas"] == ["analytics_db"]
        assert "lastScanAt" in summary_writes[0]

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_rescan_writes_backup_of_changeset_before_merge(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
    ):
        """A re-scan with drift writes the backup blob (added/removed/modified
        change-set + prior forms of modified tables) to S3, keyed per source,
        before the merge overwrites any asset — so approve/reject can act on it."""
        from coa_sources.database.pipeline.discovery_handler import handler
        from coa_sources.database.rescan_backup import backup_s3_key

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "analytics_db", "catalogId": "123456789012", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
        mock_write.return_value = {"assets_created": 1, "assets_revised": 1}

        # Accepted: orders (modified below) + legacy (removed below).
        mock_read_accepted.return_value = [
            Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="decimal")]),
            Table(name="legacy", database="analytics_db", columns=[Column(name="x", data_type="int")]),
        ]
        # Fresh: orders type-changed, new_tbl added, legacy gone.
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[
                Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="varchar")]),
                Table(name="new_tbl", database="analytics_db", columns=[Column(name="y", data_type="int")]),
            ]
        )
        mock_get_connector.return_value = connector

        handler(
            {
                "datasourceId": "DS#ds-1",
                "scanJobId": "SCAN#s",
                "namespaceId": "ns-1",
                "scanType": "full",
                "isRescan": True,
            },
            None,
        )

        mock_upload.assert_called_once()
        # upload_json(client, bucket, key, blob) — called positionally.
        _client, bucket, key, blob = mock_upload.call_args.args
        assert bucket == "test-bucket"
        assert key == backup_s3_key("ds-1")
        assert blob["added_tables"] == ["analytics_db.new_tbl"]
        assert blob["removed_tables"] == ["analytics_db.legacy"]
        assert "analytics_db.orders" in blob["modified_backup"]

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_rescan_with_no_drift_writes_no_backup(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
    ):
        """A re-scan that finds no change writes no backup — there is nothing to
        restore or delete, and no asset is overwritten."""
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "analytics_db", "catalogId": "1", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
        mock_write.return_value = {"assets_created": 0, "assets_revised": 0}

        mock_read_accepted.return_value = [
            Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="decimal")]),
        ]
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="decimal")])]
        )
        mock_get_connector.return_value = connector

        result = handler(
            {
                "datasourceId": "DS#ds-1",
                "scanJobId": "SCAN#s",
                "namespaceId": "ns-1",
                "scanType": "full",
                "isRescan": True,
            },
            None,
        )

        mock_upload.assert_not_called()
        # No drift and no orphaned added tables → nothing to review, so enrichment
        # returns the source straight to APPROVED.
        assert result["reviewNeeded"] == "false"

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_rescan_recomputes_tables_approved_from_unchanged_approved(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
    ):
        """A drift re-scan refreshes tablesApproved to the post-merge live state:
        the unchanged table keeps its approval, the drifted table is reset to
        PENDING and drops out of the count. Without this the source row keeps the
        stale pre-rescan count through the RESCAN_REVIEW window."""
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "analytics_db", "catalogId": "1", "region": "us-east-1"},
            "tablesApproved": 2,  # stale pre-rescan count: both tables were approved
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
        mock_write.return_value = {"assets_created": 0, "assets_revised": 1}

        # Two APPROVED tables. orders is unchanged in the fresh scan; customers
        # drifts (column type int -> bigint) and so resets to PENDING on merge.
        mock_read_accepted.return_value = [
            Table(
                name="orders",
                database="analytics_db",
                business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
                columns=[Column(name="total", data_type="decimal")],
            ),
            Table(
                name="customers",
                database="analytics_db",
                business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
                columns=[Column(name="id", data_type="int")],
            ),
        ]
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[
                Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="decimal")]),
                Table(name="customers", database="analytics_db", columns=[Column(name="id", data_type="bigint")]),
            ]
        )
        mock_get_connector.return_value = connector

        handler(
            {
                "datasourceId": "DS#ds-1",
                "scanJobId": "SCAN#s",
                "namespaceId": "ns-1",
                "scanType": "full",
                "isRescan": True,
            },
            None,
        )

        summary_writes = [
            c.kwargs.get("update_fields", {})
            for c in mock_ds_dao.update.call_args_list
            if "tablesDiscovered" in c.kwargs.get("update_fields", {})
        ]
        assert len(summary_writes) == 1
        assert summary_writes[0]["tablesDiscovered"] == 2
        # Only the unchanged 'orders' stays approved; drifted 'customers' -> PENDING.
        assert summary_writes[0]["tablesApproved"] == 1

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.read_file_bytes")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_rescan_open_review_diffs_against_approved_baseline_not_interim(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_read_bytes,
        mock_upload,
    ):
        """A re-scan while a PRIOR re-scan is still un-approved must diff against
        the APPROVED baseline (reconstructed from the existing backup blob), not
        the interim live assets. Here the fresh scan equals the live interim, so a
        diff-vs-live would wrongly find nothing; diff-vs-approved correctly
        re-flags the prior re-scan's column and writes a fresh backup + merge."""
        import json as _json

        from coa_common.datazone_forms import serialize_form
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "analytics_db", "catalogId": "1", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
        mock_write.return_value = {"assets_created": 0, "assets_revised": 1}

        # Approved pre-image captured in the existing backup blob: orders has [id].
        approved_orders = Table(
            name="orders",
            database="analytics_db",
            business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
            columns=[
                Column(
                    name="id",
                    data_type="int",
                    business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
                )
            ],
        )
        backup = {
            "version": 1,
            "source_id": "ds-1",
            "scan_job_sk": "sk-prev",
            "removed_tables": [],
            "added_tables": [],
            "removed_columns": {},
            "modified_backup": {"analytics_db.orders": serialize_form(approved_orders)},
        }
        mock_read_bytes.return_value = _json.dumps(backup).encode("utf-8")

        # Live (interim) assets: the prior un-approved re-scan already added promo_code.
        mock_read_accepted.return_value = [
            Table(
                name="orders",
                database="analytics_db",
                columns=[Column(name="id", data_type="int"), Column(name="promo_code", data_type="varchar")],
            ),
        ]
        # Fresh scan #2 == the live interim (no *new* drift vs live).
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[
                Table(
                    name="orders",
                    database="analytics_db",
                    columns=[Column(name="id", data_type="int"), Column(name="promo_code", data_type="varchar")],
                )
            ]
        )
        mock_get_connector.return_value = connector

        handler(
            {
                "datasourceId": "DS#ds-1",
                "scanJobId": "SCAN#s",
                "namespaceId": "ns-1",
                "scanType": "full",
                "isRescan": True,
                # A prior re-scan is still open in RESCAN_REVIEW, so the backup
                # blob is the approved pre-image and MUST be read.
                "hadOpenRescan": True,
            },
            None,
        )

        # Diff ran against the reconstructed approved baseline ([id]): promo_code is
        # re-flagged, so a fresh backup is written and the merged orders (with
        # promo_code) is written. A diff-vs-live would have found nothing and
        # written neither.
        mock_read_bytes.assert_called_once()  # backup was read (open review)
        mock_upload.assert_called_once()
        written = mock_write.call_args.kwargs["metadata"]
        (table,) = written.tables
        assert {c.name for c in table.columns} == {"id", "promo_code"}

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.read_file_bytes")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_rescan_open_review_carries_forward_vanished_added_tables(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_read_bytes,
        mock_upload,
    ):
        """A prior re-scan's added table that has since vanished from the source is
        carried into the new backup under BOTH added_tables and removed_tables, so a
        review outcome reaps it. It is NOT re-written as a live asset, and the backup
        is written even though the fresh diff is otherwise empty (no-drift)."""
        import json as _json

        from coa_common.datazone_forms import serialize_form
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "analytics_db", "catalogId": "1", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
        mock_write.return_value = {"assets_created": 0, "assets_revised": 0}

        # The prior (still-open) re-scan added analytics_db.promo — recorded in its
        # backup blob and still live as an interim asset.
        approved_orders = Table(
            name="orders",
            database="analytics_db",
            business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
            columns=[Column(name="id", data_type="int")],
        )
        backup = {
            "version": 1,
            "source_id": "ds-1",
            "scan_job_sk": "sk-prev",
            "removed_tables": [],
            "added_tables": ["analytics_db.promo"],
            "removed_columns": {},
            "modified_backup": {"analytics_db.orders": serialize_form(approved_orders)},
        }
        mock_read_bytes.return_value = _json.dumps(backup).encode("utf-8")

        # Live interim assets still carry promo (added by the prior re-scan).
        mock_read_accepted.return_value = [
            Table(name="orders", database="analytics_db", columns=[Column(name="id", data_type="int")]),
            Table(name="promo", database="analytics_db", columns=[Column(name="code", data_type="varchar")]),
        ]
        # Fresh scan #2: promo is GONE from the source; orders is unchanged. So the
        # approved baseline ([orders]) equals the fresh scan — no ordinary drift.
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="orders", database="analytics_db", columns=[Column(name="id", data_type="int")])]
        )
        mock_get_connector.return_value = connector

        result = handler(
            {
                "datasourceId": "DS#ds-1",
                "scanJobId": "SCAN#s",
                "namespaceId": "ns-1",
                "scanType": "full",
                "isRescan": True,
                "hadOpenRescan": True,
            },
            None,
        )

        # Orphans alone count as something to review, so the source is NOT
        # auto-returned to APPROVED even though the fresh diff is empty.
        assert result["reviewNeeded"] == "true"
        # Orphan present -> backup written despite the otherwise-empty diff, with
        # promo in BOTH lists (approve deletes removed_tables, reject deletes added).
        mock_upload.assert_called_once()
        _client, _bucket, _key, blob = mock_upload.call_args.args
        assert "analytics_db.promo" in blob["added_tables"]
        assert "analytics_db.promo" in blob["removed_tables"]
        # The orphan is NOT re-written as a live asset — it stays put until a
        # review outcome deletes it.
        written = mock_write.call_args.kwargs["metadata"]
        assert all(t.table_id != "analytics_db.promo" for t in written.tables)

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.read_file_bytes")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_rescan_from_approved_ignores_stale_backup(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_read_bytes,
        mock_upload,
    ):
        """A re-scan from APPROVED (hadOpenRescan false) must NOT reconstruct from a
        leftover backup blob. The blob is stale (from before delete-on-resolve, or a
        paged-approve gap): the live assets already ARE the approved baseline. Here
        the fresh scan equals live, so with the backup ignored the diff is empty —
        nothing is re-flagged, no new backup is written, no live asset is rewritten.
        If the stale backup were read, its added_tables entry would drop orders and
        re-add it, manufacturing drift on a clean re-scan (the bug this fix closes)."""
        import json as _json

        from coa_common.datazone_forms import serialize_form
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "analytics_db", "catalogId": "1", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_get_scan.return_value = MagicMock()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
        mock_write.return_value = {"assets_created": 0, "assets_revised": 0}

        # A STALE backup that, if read, would corrupt the baseline: it claims orders
        # was added by a (long-since resolved) re-scan and stores a truncated
        # pre-image. had_open_rescan is false, so it must never be read.
        stale_orders = Table(
            name="orders",
            database="analytics_db",
            business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
            columns=[Column(name="id", data_type="int")],
        )
        stale_backup = {
            "version": 1,
            "source_id": "ds-1",
            "scan_job_sk": "sk-ancient",
            "removed_tables": [],
            "added_tables": ["analytics_db.orders"],
            "removed_columns": {},
            "modified_backup": {"analytics_db.orders": serialize_form(stale_orders)},
        }
        mock_read_bytes.return_value = _json.dumps(stale_backup).encode("utf-8")

        # Live (approved) assets and the fresh scan agree exactly: no real drift.
        live_orders = [
            Table(
                name="orders",
                database="analytics_db",
                business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
                columns=[Column(name="id", data_type="int"), Column(name="promo_code", data_type="varchar")],
            ),
        ]
        mock_read_accepted.return_value = live_orders
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[
                Table(
                    name="orders",
                    database="analytics_db",
                    columns=[Column(name="id", data_type="int"), Column(name="promo_code", data_type="varchar")],
                )
            ]
        )
        mock_get_connector.return_value = connector

        handler(
            {
                "datasourceId": "DS#ds-1",
                "scanJobId": "SCAN#s",
                "namespaceId": "ns-1",
                "scanType": "full",
                "isRescan": True,
                "hadOpenRescan": False,  # re-scan from APPROVED, not an open review
            },
            None,
        )

        # Stale backup ignored: never read, so no drift manufactured. Diff-vs-live
        # is empty -> no new backup written and the merged write set is empty.
        mock_read_bytes.assert_not_called()
        mock_upload.assert_not_called()
        written = mock_write.call_args.kwargs["metadata"]
        assert written.tables == []


class TestGlueOwnershipGate:
    """F-8: the last gate before the connector reads Glue, samples through Athena
    and (in strict-LF accounts) self-grants Lake Formation access.

    Re-checked here rather than trusted from source-create because this path is
    reached from the STORED configuration blob, not from the checked request.
    """

    def _connector_config(self, *, allowed: bool, sub_type: str = "GLUE_DATABASE") -> dict:
        """Run ``_discover`` and return the config the connector was handed."""
        import coa_sources.database.pipeline.discovery_handler as mod

        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(tables=[])
        config = {"databaseName": "analytics_db", "catalogId": "123456789012", "region": "us-east-1"}
        error = _go.GlueOwnershipError("not registered to namespace 'ns-test'")

        with (
            patch(f"{MODULE}._get_ds_dao", return_value=MagicMock()),
            patch(
                f"{MODULE}.assert_namespace_may_catalog",
                side_effect=None if allowed else error,
            ),
        ):
            mod._discover(connector, config, "DS#ds-1", "ns-test", "DATABASE", {"sourceSubType": sub_type})
        return connector.test_connection.call_args.args[0]

    def test_an_unowned_database_fails_the_scan_before_the_connector_runs(self):
        import coa_sources.database.pipeline.discovery_handler as mod

        connector = MagicMock()
        with (
            patch(f"{MODULE}._get_ds_dao", return_value=MagicMock()),
            patch(
                f"{MODULE}.assert_namespace_may_catalog",
                side_effect=_go.GlueOwnershipError("not registered to namespace 'ns-test'"),
            ),
            pytest.raises(RuntimeError, match="not registered to namespace"),
        ):
            mod._discover(
                connector,
                {"databaseName": "someone_elses_db", "catalogId": "123456789012", "region": "us-east-1"},
                "DS#ds-1",
                "ns-test",
                "DATABASE",
                {"sourceSubType": "GLUE_DATABASE"},
            )

        # Not one Glue or Athena call: the refusal has to land before the connector,
        # or the metadata read and the row sampling have already happened.
        connector.test_connection.assert_not_called()
        connector.discover_metadata.assert_not_called()

    def test_an_owned_database_authorizes_the_lake_formation_self_grant(self):
        assert self._connector_config(allowed=True)["lf_self_grant_allowed"] is True

    def test_the_pipeline_does_not_tolerate_a_missing_database(self):
        """Unlike create, which defers because nothing is read there. Here "absent"
        is one CreateDatabase away from "present and readable", so it must deny."""
        import coa_sources.database.pipeline.discovery_handler as mod

        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(tables=[])
        with (
            patch(f"{MODULE}._get_ds_dao", return_value=MagicMock()),
            patch(f"{MODULE}.assert_namespace_may_catalog") as check,
        ):
            mod._discover(
                connector,
                {"databaseName": "db", "catalogId": "123456789012", "region": "us-east-1"},
                "DS#ds-1",
                "ns-test",
                "DATABASE",
                {"sourceSubType": "GLUE_DATABASE"},
            )

        assert check.call_args.kwargs.get("allow_missing_database", False) is False

    def test_a_non_glue_source_never_authorizes_the_self_grant(self):
        """The flag must be absent, not False-by-default: the connector treats a
        missing value as "unverified", so a sub-type that skips the check must not
        arrive looking verified."""
        config = self._connector_config(allowed=True, sub_type="JDBC_DATABASE")
        assert "lf_self_grant_allowed" not in config


class TestConnectorRegistry:
    def test_get_connector_glue(self):
        from coa_sources.database.connectors.glue_catalog import GlueCatalogConnector

        connector = get_connector("GLUE_DATABASE")
        assert isinstance(connector, GlueCatalogConnector)

    def test_get_connector_unknown_raises(self):
        with pytest.raises(ValueError, match="Unsupported source type"):
            get_connector("UNKNOWN")


class TestDiscoveryHandlerStatusLifecycle:
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_sets_scanning_status_at_start(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "db", "catalogId": "123456789012", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao

        mock_scan_dao = dao_double()
        mock_get_scan.return_value = mock_scan_dao

        mock_ns_dao = dao_double()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-1"}
        mock_get_ns.return_value = mock_ns_dao

        mock_connector = MagicMock()
        mock_connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        mock_connector.discover_metadata.return_value = DiscoveredMetadata(tables=[])
        mock_get_connector.return_value = mock_connector

        mock_write.return_value = {"assets_created": 0}

        handler({"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s-1", "namespaceId": "ns-1", "scanType": "full"}, None)

        # First update call should set SCANNING
        first_update = mock_ds_dao.update.call_args_list[0]
        assert first_update[1]["update_fields"]["status"] == SourceStatus.SCANNING

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_datazone_write_exhaustion_writes_terminal_status(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """If DataZone asset writes exhaust their retries and the writer raises
        (e.g. sustained TooManyRequestsException, issue #857), discovery must
        still write a TERMINAL source status (SCAN_FAILED) — never leave the
        source stuck in SCANNING."""
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = {
            "sourceSubType": "GLUE_DATABASE",
            "configuration": {"databaseName": "db", "catalogId": "123456789012", "region": "us-east-1"},
        }
        mock_get_ds.return_value = mock_ds_dao
        mock_scan_dao = dao_double()
        mock_get_scan.return_value = mock_scan_dao
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))

        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="t", database="db", columns=[Column(name="c", data_type="text")])]
        )
        mock_get_connector.return_value = connector

        # Writer exhausts retries under throttling and fails loud.
        mock_write.side_effect = RuntimeError(
            "Failed to write 1 asset(s): TooManyRequestsException (reached max retries: 10)"
        )

        # A generic RuntimeError from the writer is not a permanent failure, so
        # discovery re-raises it as TransientScanError (the state machine retries).
        with pytest.raises(TransientScanError):
            handler(
                {"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-1", "scanType": "full"},
                None,
            )

        # Source must be driven to a terminal status, not left SCANNING.
        statuses = [
            c.kwargs["update_fields"].get("status")
            for c in mock_ds_dao.update.call_args_list
            if "status" in c.kwargs.get("update_fields", {})
        ]
        assert SourceStatus.SCAN_FAILED in statuses
        assert statuses[-1] == SourceStatus.SCAN_FAILED, "final status write must be terminal, not SCANNING"

    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    def test_sets_scan_failed_on_error(self, mock_get_ds, mock_get_scan):
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = MagicMock()
        mock_ds_dao.get.return_value = None  # Will cause ValueError
        mock_get_ds.return_value = mock_ds_dao

        mock_scan_dao = dao_double()
        mock_get_scan.return_value = mock_scan_dao

        with pytest.raises(PermanentScanError):
            handler({"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s-1", "namespaceId": "ns-1"}, None)

        # Data source status set to SCAN_FAILED
        ds_update = mock_ds_dao.update.call_args_list
        assert len(ds_update) == 1
        assert ds_update[0][1]["update_fields"]["status"] == SourceStatus.SCAN_FAILED

        # errorMessage set on scan job
        scan_update = mock_scan_dao.update.call_args_list
        assert len(scan_update) == 1
        assert "errorMessage" in scan_update[0][1]["update_fields"]


# ═══════════════════════════════════════════════════════════════════
# _provision_athena_federation — error isolation + rollback
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# MAX_TABLES_PER_SOURCE env-var parsing — cold-start safety
# ═══════════════════════════════════════════════════════════════════


class TestMaxTablesEnvParsing:
    def test_invalid_value_falls_back_to_zero(self, monkeypatch):
        """A misconfigured (non-integer) MAX_TABLES_PER_SOURCE must not crash
        the Lambda on cold start; it falls back to 0 (cap disabled)."""
        import importlib

        import coa_sources.database.pipeline.discovery_handler as mod

        monkeypatch.setenv("MAX_TABLES_PER_SOURCE", "abc")
        try:
            reloaded = importlib.reload(mod)
            assert reloaded.MAX_TABLES_PER_SOURCE == 0
        finally:
            # Restore a clean module state for other tests.
            monkeypatch.delenv("MAX_TABLES_PER_SOURCE", raising=False)
            importlib.reload(mod)

    def test_valid_value_is_parsed(self, monkeypatch):
        import importlib

        import coa_sources.database.pipeline.discovery_handler as mod

        monkeypatch.setenv("MAX_TABLES_PER_SOURCE", "250")
        try:
            reloaded = importlib.reload(mod)
            assert reloaded.MAX_TABLES_PER_SOURCE == 250
        finally:
            monkeypatch.delenv("MAX_TABLES_PER_SOURCE", raising=False)
            importlib.reload(mod)


class TestCustomConnectorDiscovery:
    """Threading the custom-connector config, and surfacing a degraded scan.

    The catalog name is the interesting part: it is derived by the control plane
    and stored as a TOP-LEVEL attribute on the source record, not inside the
    caller-supplied ``configuration`` blob, so the handler has to read it from the
    item rather than from the config.
    """

    @staticmethod
    def _item(**overrides):
        item = {
            "sourceSubType": "CUSTOM_CONNECTOR",
            "athenaDataCatalogName": "coadevds_abc123",
            "configuration": {"databaseName": "widgets", "tableFilter": "dim_*"},
        }
        item.update(overrides)
        return item

    @staticmethod
    def _wire(mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, item):
        ds_dao = MagicMock()
        ds_dao.get.return_value = item
        mock_get_ds.return_value = ds_dao
        scan_dao = dao_double()
        mock_get_scan.return_value = scan_dao
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-123"}))
        mock_write.return_value = {"assets_created": 1}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = metadata
        mock_get_connector.return_value = connector
        return ds_dao, scan_dao, connector

    _EVENT = {"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-1", "scanType": "full"}

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_threads_the_derived_catalog_name_from_the_source_record(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        from coa_sources.database.pipeline.discovery_handler import handler

        metadata = DiscoveredMetadata(
            tables=[Table(name="dim_a", database="widgets", columns=[Column(name="c", data_type="int")])]
        )
        _, _, connector = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, self._item()
        )
        handler(self._EVENT, None)

        for cfg in (connector.test_connection.call_args[0][0], connector.discover_metadata.call_args[0][0]):
            # Read from the item, not the config blob — the caller never supplies it.
            assert cfg["athena_data_catalog_name"] == "coadevds_abc123"
            assert cfg["database_name"] == "widgets"
            assert cfg["table_filter"] == "dim_*"

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_a_source_without_the_attribute_threads_an_empty_catalog_name(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """The connector then fails its own connection test with an actionable
        message, rather than the handler raising a KeyError here."""
        from coa_sources.database.pipeline.discovery_handler import handler

        item = self._item()
        del item["athenaDataCatalogName"]
        metadata = DiscoveredMetadata(
            tables=[Table(name="t", database="widgets", columns=[Column(name="c", data_type="int")])]
        )
        _, _, connector = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, item
        )
        handler(self._EVENT, None)
        assert connector.discover_metadata.call_args[0][0]["athena_data_catalog_name"] == ""

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_records_unreadable_tables_on_the_scan_job(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """A table that fails to read reaches review with no columns and no keys
        while enrichment fills AI descriptions over the gap, so the count has to
        leave the logs and land on the scan job the steward sees."""
        from coa_sources.database.pipeline.discovery_handler import handler

        metadata = DiscoveredMetadata(
            tables=[Table(name="ok", database="widgets", columns=[Column(name="c", data_type="int")])],
            failed_tables=["widgets.bad", "widgets.worse"],
        )
        _, scan_dao, _ = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, self._item()
        )
        handler(self._EVENT, None)

        fields = scan_dao.update.call_args.kwargs["update_fields"]
        assert fields["tablesFailed"] == 2
        assert fields["failedTables"] == ["widgets.bad", "widgets.worse"]
        assert fields["tablesDiscovered"] == 1

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_a_clean_scan_writes_no_failure_fields(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """Absent rather than zero: a `tablesFailed: 0` on every scan job would
        make the field useless as a filter for the degraded ones."""
        from coa_sources.database.pipeline.discovery_handler import handler

        metadata = DiscoveredMetadata(
            tables=[Table(name="ok", database="widgets", columns=[Column(name="c", data_type="int")])]
        )
        _, scan_dao, _ = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, self._item()
        )
        handler(self._EVENT, None)

        fields = scan_dao.update.call_args.kwargs["update_fields"]
        assert "tablesFailed" not in fields
        assert "failedTables" not in fields
        assert "filterWarnings" not in fields

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_records_filter_warnings_on_the_scan_job(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """A regex filter that matched nothing leaves the scan successful but unfiltered;
        the reason must reach the scan job the steward sees (#168)."""
        from coa_sources.database.pipeline.discovery_handler import handler

        metadata = DiscoveredMetadata(
            tables=[Table(name="ok", database="widgets", columns=[Column(name="c", data_type="int")])],
            filter_warnings=["schema_exclude_filter matched nothing, so it had no effect."],
        )
        _, scan_dao, _ = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, self._item()
        )
        handler(self._EVENT, None)

        fields = scan_dao.update.call_args.kwargs["update_fields"]
        assert fields["filterWarnings"] == ["schema_exclude_filter matched nothing, so it had no effect."]

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_the_stored_failed_table_list_is_capped(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """A DynamoDB item is limited to 400 KB, so the list is a signal and the
        count beside it is the exact figure."""
        from coa_sources.database.pipeline.discovery_handler import (
            _MAX_REPORTED_FAILED_TABLES,
            handler,
        )

        failed = [f"widgets.t{i}" for i in range(_MAX_REPORTED_FAILED_TABLES + 25)]
        metadata = DiscoveredMetadata(
            tables=[Table(name="ok", database="widgets", columns=[Column(name="c", data_type="int")])],
            failed_tables=failed,
        )
        _, scan_dao, _ = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, self._item()
        )
        handler(self._EVENT, None)

        fields = scan_dao.update.call_args.kwargs["update_fields"]
        assert fields["tablesFailed"] == len(failed)
        assert len(fields["failedTables"]) == _MAX_REPORTED_FAILED_TABLES

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_discovery_does_not_mark_the_source_queryable(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """The post-discovery federation step owns that flip, as it does for JDBC."""
        from coa_sources.database.pipeline.discovery_handler import handler

        metadata = DiscoveredMetadata(
            tables=[Table(name="ok", database="widgets", columns=[Column(name="c", data_type="int")])]
        )
        ds_dao, _, _ = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, self._item()
        )
        handler(self._EVENT, None)

        source_fields = ds_dao.update.call_args.kwargs["update_fields"]
        assert "queryable" not in source_fields
        # discoveredSchemas is what serve pins the query Database to.
        assert source_fields["discoveredSchemas"] == ["widgets"]


@pytest.mark.unit
class TestDiscoveredSchemasIsNotClobberedByAnEmptyScan:
    """A connector-backed source is ONE known schema, written on the record at create.

    A scan that found no tables must not erase it: serve's namespace-qualifier check builds
    ``federated_catalog_schemas`` from this list, so an empty list has every
    catalog-qualified reference DENIED with no fallback and no repair path — configuration
    update refuses the Databricks sub-type and re-scan 409s outside SCAN_FAILED.

    Zero tables is not exotic here: an over-narrow ``tableFilter`` does it, and so does a
    credential with BROWSE but not SELECT.
    """

    _EVENT = {"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-1", "scanType": "full"}

    @staticmethod
    def _wire(mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, metadata, item):
        ds_dao = MagicMock()
        ds_dao.get.return_value = item
        mock_get_ds.return_value = ds_dao
        mock_get_scan.return_value = dao_double()
        mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-123"}))
        mock_write.return_value = {"assets_created": 0}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = metadata
        mock_get_connector.return_value = connector
        return ds_dao

    @pytest.mark.parametrize("sub_type", ["CUSTOM_CONNECTOR", "DATABRICKS_SQL_WAREHOUSE"])
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_a_zero_table_scan_leaves_the_create_time_value_alone(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, sub_type
    ):
        item = {
            "sourceSubType": sub_type,
            "athenaDataCatalogName": "coadevds_abc123",
            "discoveredSchemas": ["sales"],
            "configuration": {"databaseName": "sales", "tableFilter": "matches_nothing_*"},
        }
        ds_dao = self._wire(
            mock_get_ds,
            mock_get_scan,
            mock_get_ns,
            mock_write,
            mock_get_connector,
            DiscoveredMetadata(tables=[], failed_tables=["sales.orders"]),
            item,
        )
        from coa_sources.database.pipeline.discovery_handler import handler

        handler(self._EVENT, None)
        fields = ds_dao.update.call_args.kwargs["update_fields"]
        # Left out of the update entirely, rather than written as the stored value.
        assert "discoveredSchemas" not in fields, fields
        assert fields["tablesDiscovered"] == 0

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_a_scan_that_found_tables_still_writes_what_it_found(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """The suppression is scoped to the empty case, so a scan with results stays
        authoritative — including one reporting a schema the record did not carry."""
        item = {
            "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
            "athenaDataCatalogName": "coadevds_abc123",
            "discoveredSchemas": ["stale"],
            "configuration": {"databaseName": "sales"},
        }
        ds_dao = self._wire(
            mock_get_ds,
            mock_get_scan,
            mock_get_ns,
            mock_write,
            mock_get_connector,
            DiscoveredMetadata(tables=[Table(name="o", database="sales", columns=[Column(name="c", data_type="int")])]),
            item,
        )
        from coa_sources.database.pipeline.discovery_handler import handler

        handler(self._EVENT, None)
        assert ds_dao.update.call_args.kwargs["update_fields"]["discoveredSchemas"] == ["sales"]

    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_a_federated_jdbc_source_still_has_its_empty_scan_recorded(
        self, mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write
    ):
        """For federated JDBC the discovered set is authoritative — schemas can come and
        go, and the federation step grants Lake Formation against exactly this list — so
        suppressing an empty write there would mask a real drift."""
        item = {
            "sourceSubType": "JDBC_DATABASE",
            "discoveredSchemas": ["public"],
            "configuration": {"databaseName": "app", "credentialSecretArn": ""},
        }
        ds_dao = self._wire(
            mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_get_connector, DiscoveredMetadata(tables=[]), item
        )
        from coa_sources.database.pipeline.discovery_handler import handler

        handler(self._EVENT, None)
        assert ds_dao.update.call_args.kwargs["update_fields"]["discoveredSchemas"] == []


@pytest.mark.unit
class TestScanTimeNamespaceBinding:
    """Discovery re-checks the STORED credential-secret ARN before connecting.

    This is the earliest point the credentials leave the account: the connector
    fetches the secret and opens a connection to the host in the same row. The
    registration-time check validates the ARN a caller supplies, so it cannot
    cover a row written before the binding rule existed, or a secret re-tagged
    after the source was registered — both are caught here.

    ``DATABRICKS_SQL_WAREHOUSE`` is exempt, and the exemption is asserted rather
    than assumed: nothing on this role's path reads that secret.
    """

    _EVENT = {"datasourceId": "DS#ds-1", "scanJobId": "SCAN#s", "namespaceId": "ns-1", "scanType": "full"}
    _JDBC_CONFIG = {
        "engine": "POSTGRESQL",
        "host": "db.example.com",
        "port": 5432,
        "databaseName": "app",
        "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:s-AbCdEf",
    }

    def _dao(self, mock_get_ds, config, sub_type: str = "JDBC_DATABASE"):
        dao = dao_double()
        dao.get.return_value = {"sourceSubType": sub_type, "configuration": config}
        mock_get_ds.return_value = dao
        return dao

    def _scan(self, mock_get_ns, mock_get_connector):
        """Stub the rest of a successful scan so the binding call is the only variable."""
        mock_get_ns.return_value.get.return_value = {"dataZoneProjectId": "proj-1"}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(tables=[])
        mock_get_connector.return_value = connector
        return connector

    @patch(f"{MODULE}.require_secret_namespace_binding")
    @patch(f"{MODULE}.get_connector")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    def test_binding_checked_with_stored_arn_and_row_namespace(
        self, mock_get_ds, mock_get_scan, mock_get_ns, mock_get_connector, mock_require
    ):
        from coa_sources.database.pipeline.discovery_handler import handler

        self._dao(mock_get_ds, self._JDBC_CONFIG)
        self._scan(mock_get_ns, mock_get_connector)

        with patch(f"{MODULE}.write_to_datazone", return_value={"assets_created": 0, "assets_revised": 0}):
            handler(self._EVENT, None)

        mock_require.assert_called_once_with(
            self._JDBC_CONFIG["credentialSecretArn"],
            "ns-1",
            "DS#ds-1",
        )

    @patch(f"{MODULE}.require_secret_namespace_binding")
    @patch(f"{MODULE}.get_connector")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    def test_unbound_secret_fails_the_scan_without_connecting(
        self, mock_get_ds, mock_get_scan, mock_get_ns, mock_get_connector, mock_require
    ):
        """A refusal must abort before the connector runs.

        `get_connector` never being reached is the assertion that matters: the
        connector is what reads the secret and dials the host, so a refusal that
        merely failed the scan afterwards would have already leaked the credentials.
        """
        from coa_sources.database.pipeline.discovery_handler import handler

        dao = self._dao(mock_get_ds, self._JDBC_CONFIG)
        mock_require.side_effect = RuntimeError("Credential secret is not bound to namespace ns-1")

        with pytest.raises(RuntimeError, match="not bound to namespace"):
            handler(self._EVENT, None)

        mock_get_connector.assert_not_called()
        # The source is left SCAN_FAILED by the handler's own error path, so the
        # operator sees why rather than a source that silently never scanned.
        statuses = [c.kwargs.get("update_fields", {}).get("status") for c in dao.update.call_args_list]
        assert SourceStatus.SCAN_FAILED in statuses

    @patch(f"{MODULE}.require_secret_namespace_binding")
    @patch(f"{MODULE}.get_connector")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    def test_config_without_a_secret_passes_none_through(
        self, mock_get_ds, mock_get_scan, mock_get_ns, mock_get_connector, mock_require
    ):
        # Glue sources carry no credentialSecretArn; the check must be a no-op
        # rather than a refusal, so it stays out of the way of every other sub-type.
        from coa_sources.database.pipeline.discovery_handler import handler

        self._dao(mock_get_ds, {"databaseName": "analytics_db", "catalogId": "123456789012"})
        self._scan(mock_get_ns, mock_get_connector)

        with patch(f"{MODULE}.write_to_datazone", return_value={"assets_created": 0, "assets_revised": 0}):
            handler(self._EVENT, None)

        mock_require.assert_called_once_with(None, "ns-1", "DS#ds-1")

    @patch(f"{MODULE}.require_secret_namespace_binding")
    @patch(f"{MODULE}.get_connector")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    def test_a_databricks_warehouse_scan_does_not_check_the_secrets_namespace_tag(
        self, mock_get_ds, mock_get_scan, mock_get_ns, mock_get_connector, mock_require
    ):
        """This execution role never reads that secret: the connector assumes the
        customer's role and reads the credential as that session."""
        from coa_sources.database.pipeline.discovery_handler import handler

        self._dao(
            mock_get_ds,
            {"databaseName": "sales", "credentialSecretArn": self._JDBC_CONFIG["credentialSecretArn"]},
            sub_type="DATABRICKS_SQL_WAREHOUSE",
        )
        self._scan(mock_get_ns, mock_get_connector)

        with patch(f"{MODULE}.write_to_datazone", return_value={"assets_created": 0, "assets_revised": 0}):
            handler(self._EVENT, None)

        # Not consulted at all: a cross-account secret would pass the check anyway, so
        # asserting only that the scan succeeded would pass with the call still there.
        mock_require.assert_not_called()
        # And the scan really did run, or the assertion above is vacuous.
        mock_get_connector.assert_called_once()


def _drift_rescan_setup(mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted):
    """A re-scan whose fresh scan differs from the accepted assets (orders changed type)."""
    mock_ds_dao = MagicMock()
    mock_ds_dao.get.return_value = {
        "sourceSubType": "GLUE_DATABASE",
        "configuration": {"databaseName": "analytics_db", "catalogId": "123456789012", "region": "us-east-1"},
    }
    mock_get_ds.return_value = mock_ds_dao
    mock_get_scan.return_value = MagicMock()
    mock_get_ns.return_value = MagicMock(get=MagicMock(return_value={"dataZoneProjectId": "proj-1"}))
    mock_write.return_value = {"assets_created": 0, "assets_revised": 1}
    mock_read_accepted.return_value = [
        Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="decimal")]),
    ]
    connector = MagicMock()
    connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
    connector.discover_metadata.return_value = DiscoveredMetadata(
        tables=[Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="varchar")])]
    )
    mock_get_connector.return_value = connector
    return mock_ds_dao


_RESCAN_EVENT = {
    "datasourceId": "DS#ds-1",
    "scanJobId": "SCAN#s",
    "scanJobSK": "2026-10-04T00:00:00.000000Z",
    "namespaceId": "ns-1",
    "scanType": "full",
    "isRescan": True,
}


def _marker_writes(mock_ds_dao) -> list[dict]:
    from coa_sources.database.rescan_backup import BACKUP_SCAN_JOB_FIELD

    return [
        c.kwargs["update_fields"]
        for c in mock_ds_dao.update.call_args_list
        if BACKUP_SCAN_JOB_FIELD in c.kwargs.get("update_fields", {})
    ]


@pytest.mark.unit
class TestRescanBackupMarker:
    """A re-scan records on the source row that it wrote its backup blob,
    before the merge overwrites a live asset. If the run fails after that point the
    live assets are an unreviewed merge, and the retry has to rebuild its baseline
    from the blob, so the trigger reads this marker to send ``hadOpenRescan``."""

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_marker_is_written_after_the_backup_and_before_the_merge(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
    ):
        from coa_sources.database.pipeline.discovery_handler import handler
        from coa_sources.database.rescan_backup import BACKUP_SCAN_JOB_FIELD

        mock_ds_dao = _drift_rescan_setup(
            mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted
        )
        order: list[str] = []
        mock_upload.side_effect = lambda *a, **k: order.append("backup")
        mock_write.side_effect = lambda **k: order.append("merge") or {"assets_created": 0}

        def _update(**kwargs):
            if BACKUP_SCAN_JOB_FIELD in kwargs.get("update_fields", {}):
                order.append("marker")
            return True

        mock_ds_dao.update.side_effect = _update

        handler(dict(_RESCAN_EVENT), None)

        assert order == ["backup", "marker", "merge"]
        assert _marker_writes(mock_ds_dao) == [{BACKUP_SCAN_JOB_FIELD: _RESCAN_EVENT["scanJobSK"]}]

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_no_drift_rescan_writes_no_marker(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
    ):
        # No drift writes no blob, and the merge then leaves every asset as it was,
        # so a retry of this run can treat the live assets as the baseline.
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = _drift_rescan_setup(
            mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted
        )
        mock_get_connector.return_value.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="decimal")])]
        )

        handler(dict(_RESCAN_EVENT), None)

        mock_upload.assert_not_called()
        assert _marker_writes(mock_ds_dao) == []

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_marker_write_failure_fails_the_scan_before_the_merge(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
    ):
        # Without the marker a retry assumes the live assets are the baseline, which
        # is only true if nothing was overwritten. So a failed marker write must stop
        # the run before the merge.
        from botocore.exceptions import ClientError
        from coa_sources.database.pipeline.discovery_handler import handler
        from coa_sources.database.rescan_backup import BACKUP_SCAN_JOB_FIELD

        mock_ds_dao = _drift_rescan_setup(
            mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted
        )

        def _update(**kwargs):
            if BACKUP_SCAN_JOB_FIELD in kwargs.get("update_fields", {}):
                raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
            return True

        mock_ds_dao.update.side_effect = _update

        with pytest.raises((TransientScanError, PermanentScanError)):
            handler(dict(_RESCAN_EVENT), None)

        mock_upload.assert_called_once()
        mock_write.assert_not_called()

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}._read_existing_backup")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_handler_step_functions_retry_of_own_run_rebuilds_baseline_from_backup(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
        mock_read_backup,
    ):
        # A retry inside the same execution gets the first attempt's input, so
        # hadOpenRescan is still false. The first attempt had written its backup
        # (the marker names this run) and merged orders, so the live asset already
        # matches the fresh scan. The retry must still diff against the backup.
        from coa_common.datazone_forms import serialize_form
        from coa_sources.database.pipeline.discovery_handler import handler
        from coa_sources.database.rescan_backup import BACKUP_SCAN_JOB_FIELD

        mock_ds_dao = _drift_rescan_setup(
            mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted
        )
        mock_ds_dao.get.return_value[BACKUP_SCAN_JOB_FIELD] = _RESCAN_EVENT["scanJobSK"]
        mock_read_accepted.return_value = [
            Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="varchar")]),
        ]
        approved_orders = Table(
            name="orders",
            database="analytics_db",
            business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
            columns=[Column(name="total", data_type="decimal")],
        )
        mock_read_backup.return_value = {
            "version": 1,
            "source_id": "ds-1",
            "scan_job_sk": _RESCAN_EVENT["scanJobSK"],
            "removed_tables": [],
            "added_tables": [],
            "removed_columns": {},
            "modified_backup": {"analytics_db.orders": serialize_form(approved_orders)},
        }

        result = handler(dict(_RESCAN_EVENT), None)

        mock_read_backup.assert_called_once_with("ds-1")
        assert result["reviewNeeded"] == "true"

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}._read_existing_backup")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_handler_marker_from_another_run_does_not_read_backup(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
        mock_read_backup,
    ):
        # A marker naming an EARLIER run is not a retry of this one: with
        # hadOpenRescan false the live assets are the baseline and no blob is read.
        from coa_sources.database.pipeline.discovery_handler import handler
        from coa_sources.database.rescan_backup import BACKUP_SCAN_JOB_FIELD

        mock_ds_dao = _drift_rescan_setup(
            mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted
        )
        mock_ds_dao.get.return_value[BACKUP_SCAN_JOB_FIELD] = "2026-09-01T00:00:00.000000Z"

        handler(dict(_RESCAN_EVENT), None)

        mock_read_backup.assert_not_called()


def _open_rescan_backup(source_summary: dict | None) -> dict:
    """An existing backup whose approved pre-image of orders has total as decimal."""
    from coa_common.datazone_forms import serialize_form

    approved_orders = Table(
        name="orders",
        database="analytics_db",
        business_metadata=BusinessMetadata(review_status=ReviewStatus.APPROVED),
        columns=[Column(name="total", data_type="decimal")],
    )
    backup = {
        "version": 1,
        "source_id": "ds-1",
        "scan_job_sk": "2026-10-03T00:00:00.000000Z",
        "removed_tables": [],
        "added_tables": [],
        "removed_columns": {},
        "modified_backup": {"analytics_db.orders": serialize_form(approved_orders)},
    }
    if source_summary is not None:
        backup["source_summary"] = source_summary
    return backup


@pytest.mark.unit
class TestRescanBackupSummaryFromExistingBackup:
    """When the baseline is rebuilt from an existing backup, the source row's counts
    may already be the failed run's, so the new backup keeps the old backup's."""

    _ROW_COUNTS = {
        "tablesApproved": 7,
        "tablesDiscovered": 10,
        "lastScanAt": "2026-10-03T00:05:00+00:00",
        "lastScanJobId": "2026-10-03T00:00:00.000000Z",
    }

    def _run(self, mocks, backup_summary):
        (mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted) = mocks[:6]
        mock_read_backup, mock_upload = mocks[6], mocks[7]
        from coa_sources.database.pipeline.discovery_handler import handler

        mock_ds_dao = _drift_rescan_setup(
            mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted
        )
        mock_ds_dao.get.return_value.update(self._ROW_COUNTS)
        mock_read_backup.return_value = _open_rescan_backup(backup_summary)
        handler({**_RESCAN_EVENT, "hadOpenRescan": "true"}, None)
        mock_upload.assert_called_once()
        return mock_upload.call_args.args[3]["source_summary"]

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}._read_existing_backup")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_handler_retry_with_existing_backup_keeps_backup_source_summary(
        self, c, ds, scan, ns, write, live, s3, upload, read_backup
    ):
        approved_summary = {
            "tablesApproved": 10,
            "tablesDiscovered": 10,
            "lastScanAt": "2026-09-01T00:05:00+00:00",
            "lastScanJobId": "2026-09-01T00:00:00.000000Z",
        }

        summary = self._run((c, ds, scan, ns, write, live, read_backup, upload), approved_summary)

        assert summary == approved_summary

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}._read_existing_backup")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_handler_existing_backup_without_summary_falls_back_to_row_counts(
        self, c, ds, scan, ns, write, live, s3, upload, read_backup
    ):
        # A backup with no summary still gives a reject something to restore.
        summary = self._run((c, ds, scan, ns, write, live, read_backup, upload), None)

        assert summary == self._ROW_COUNTS


@pytest.mark.unit
class TestRescanRestoresTablesBackToApproved:
    """A table an earlier run merged that now diffs as unchanged against the
    rebuilt baseline is written back to its approved version from the backup."""

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}._read_existing_backup")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_handler_reverted_table_is_written_back_from_backup_and_needs_no_review(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
        mock_read_backup,
    ):
        from coa_sources.database.pipeline.discovery_handler import handler
        from coa_sources.database.rescan_backup import backup_s3_key

        mock_ds_dao = _drift_rescan_setup(
            mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted
        )
        # Live asset: the earlier run's merge (total became varchar).
        mock_read_accepted.return_value = [
            Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="varchar")]),
        ]
        # Fresh scan: back to the approved pre-image (decimal).
        mock_get_connector.return_value.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="orders", database="analytics_db", columns=[Column(name="total", data_type="decimal")])]
        )
        mock_read_backup.return_value = _open_rescan_backup({"tablesApproved": 1})

        result = handler({**_RESCAN_EVENT, "hadOpenRescan": "true"}, None)

        (written,) = mock_write.call_args.kwargs["metadata"].tables
        assert written.table_id == "analytics_db.orders"
        assert written.columns[0].data_type == "decimal"
        assert written.business_metadata.review_status == ReviewStatus.APPROVED
        assert result["reviewNeeded"] == "false"
        # Nothing to review, so no new backup, and the old one is deleted.
        mock_upload.assert_not_called()
        mock_s3_client.return_value.delete_object.assert_called_once_with(
            Bucket="test-bucket", Key=backup_s3_key("ds-1")
        )
        # The restored table is approved again, so it counts.
        source_updates = [
            c.kwargs["update_fields"]
            for c in mock_ds_dao.update.call_args_list
            if "tablesApproved" in c.kwargs.get("update_fields", {})
        ]
        assert source_updates[-1]["tablesApproved"] == 1

    @patch(f"{MODULE}.BUCKET_NAME", "test-bucket")
    @patch(f"{MODULE}._read_existing_backup")
    @patch(f"{MODULE}.upload_json")
    @patch(f"{MODULE}.get_s3_client")
    @patch(f"{MODULE}.read_assets_for_datasource")
    @patch(f"{MODULE}.write_to_datazone")
    @patch(f"{MODULE}._get_ns_dao")
    @patch(f"{MODULE}._get_scan_dao")
    @patch(f"{MODULE}._get_ds_dao")
    @patch(f"{MODULE}.get_connector")
    def test_handler_rescan_still_needing_review_keeps_new_backup(
        self,
        mock_get_connector,
        mock_get_ds,
        mock_get_scan,
        mock_get_ns,
        mock_write,
        mock_read_accepted,
        mock_s3_client,
        mock_upload,
        mock_read_backup,
    ):
        # With drift left to review, the new backup replaces the old one; nothing
        # is deleted, or approve/reject would have no backup to read.
        from coa_sources.database.pipeline.discovery_handler import handler

        _drift_rescan_setup(mock_get_connector, mock_get_ds, mock_get_scan, mock_get_ns, mock_write, mock_read_accepted)
        mock_read_backup.return_value = _open_rescan_backup({"tablesApproved": 1})

        result = handler({**_RESCAN_EVENT, "hadOpenRescan": "true"}, None)

        assert result["reviewNeeded"] == "true"
        mock_upload.assert_called_once()
        mock_s3_client.return_value.delete_object.assert_not_called()
