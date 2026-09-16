# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the #116 dashboard metric emissions.

Every metric charted by the ``sources-structured-scan`` CloudWatch dashboard
that is NOT AWS-native must actually be emitted, with the exact name and
dimensions the dashboard's SEARCH() expressions look for. A rename on either
side silently darkens a widget, so these tests pin the contract:

  * ``ConnectionValidation``    dims SourceType + Result
  * ``CatalogAssetWrites``      dim  Operation (Create | Update)
  * ``TablesApprovedByReview``  dim  ReviewScope
  * ``TablesRejectedByReview``  dim  ReviewScope
  * ``GlueApiThrottles``        dim  Api
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
# discovery_handler reads these three at import time with no default, and the
# tests below import it inside the test body. Set them here so this module is
# self-sufficient: relying on another test module to have set them first makes
# the file pass only when something else is collected alongside it, and fail on
# a narrower selection. setdefault so a value another module already set wins.
os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")


def _emitted(mock_emit, name: str) -> list[dict]:
    """All kwargs-dimension dicts emitted for `name`, with the value attached."""
    return [
        {"value": call.args[1], **call.kwargs}
        for call in mock_emit.call_args_list
        if call.args and call.args[0] == name
    ]


# ---------------------------------------------------------------------------
# ConnectionValidation — discovery_handler._discover
# ---------------------------------------------------------------------------

_DISCOVERY = "coa_sources.database.pipeline.discovery_handler"


def _connector(*, success: bool) -> MagicMock:
    from coa_sources.database.connectors.base import ConnectionTestResult

    connector = MagicMock()
    connector.test_connection.return_value = ConnectionTestResult(success=success, message="msg", checks=[])
    connector.discover_metadata.return_value = MagicMock(tables=[])
    return connector


@pytest.mark.unit
class TestConnectionValidationMetric:
    _CONFIG = {"databaseName": "mydb"}

    def test_successful_validation_emits_result_success(self):
        from coa_sources.database.pipeline.discovery_handler import _discover

        with patch(f"{_DISCOVERY}.emit_metric") as mock_emit:
            _discover(_connector(success=True), self._CONFIG, "ds-1", "ns-1", "GLUE_DATABASE")

        emissions = _emitted(mock_emit, "ConnectionValidation")
        assert emissions == [{"value": 1, "SourceType": "GLUE_DATABASE", "Result": "Success"}], (
            "dashboard SEARCHes {SourceType,Result} — both dims are required"
        )

    def test_failed_validation_emits_result_failure_before_raising(self):
        """The counter must land even though the failure path raises — a dark
        Failure series would make the widget read as 100% success."""
        from coa_sources.database.pipeline.discovery_handler import _discover

        with (
            patch(f"{_DISCOVERY}.emit_metric") as mock_emit,
            pytest.raises(RuntimeError, match="Connection test failed"),
        ):
            _discover(_connector(success=False), self._CONFIG, "ds-1", "ns-1", "JDBC_DATABASE")

        assert _emitted(mock_emit, "ConnectionValidation") == [
            {"value": 1, "SourceType": "JDBC_DATABASE", "Result": "Failure"}
        ]


# ---------------------------------------------------------------------------
# CatalogAssetWrites — metadata_writer.write_to_datazone
# ---------------------------------------------------------------------------

_WRITER = "coa_sources.database.metadata_writer"


def _writer_table(table_id: str) -> MagicMock:
    table = MagicMock()
    table.table_id = table_id
    table.name = table_id.split(".")[-1]
    table.business_metadata.description = "d"
    table.technical_metadata.format = "parquet"
    table.columns = []
    return table


@pytest.mark.unit
class TestCatalogAssetWritesMetric:
    def test_emits_create_and_update_counts_once_each(self):
        """Two aggregate datums per discovery, not one per table — a 1k-table
        scan must not write 1k EMF lines for the same two numbers."""
        from coa_sources.database.metadata_writer import write_to_datazone

        existing = MagicMock()
        existing.name = "DS#src-001:mydb.old"
        existing.asset_id = "asset-old"

        mock_client = MagicMock()
        mock_client.search_assets.return_value = MagicMock(items=[existing], next_token=None)

        with (
            patch(f"{_WRITER}.emit_metric") as mock_emit,
            patch(f"{_WRITER}.SMUSClient", MagicMock(return_value=mock_client)),
            patch(f"{_WRITER}.build_forms_input", return_value=[]),
        ):
            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=MagicMock(tables=[_writer_table("mydb.new"), _writer_table("mydb.old")]),
                data_source_id="DS#src-001",
            )

        assert result == {"assets_created": 1, "assets_revised": 1}
        assert _emitted(mock_emit, "CatalogAssetWrites") == [
            {"value": 1, "Operation": "Create"},
            {"value": 1, "Operation": "Update"},
        ], "Create/Update only — the writer has no delete path, so this is not full CRUD"

    def _write(self, *, existing_items: list) -> list[dict]:
        """Run write_to_datazone with two fresh tables and return CatalogAssetWrites emissions."""
        from coa_sources.database.metadata_writer import write_to_datazone

        mock_client = MagicMock()
        mock_client.search_assets.return_value = MagicMock(items=existing_items, next_token=None)

        with (
            patch(f"{_WRITER}.emit_metric") as mock_emit,
            patch(f"{_WRITER}.SMUSClient", MagicMock(return_value=mock_client)),
            patch(f"{_WRITER}.build_forms_input", return_value=[]),
        ):
            write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=MagicMock(tables=[_writer_table("mydb.a"), _writer_table("mydb.b")]),
                data_source_id="DS#src-001",
            )
        return _emitted(mock_emit, "CatalogAssetWrites")

    def test_emits_zero_update_when_all_tables_are_new(self):
        """Both series always fire, even at zero — a dark Update series would
        make the dashboard read as if no revisions ever happen."""
        assert self._write(existing_items=[]) == [
            {"value": 2, "Operation": "Create"},
            {"value": 0, "Operation": "Update"},
        ]

    def test_emits_zero_create_when_all_tables_exist(self):
        """The Create counterpart: all-revise still emits Create=0, not nothing."""

        def _existing(table_id: str) -> MagicMock:
            asset = MagicMock()
            asset.name = f"DS#src-001:{table_id}"
            asset.asset_id = f"asset-{table_id}"
            return asset

        assert self._write(existing_items=[_existing("mydb.a"), _existing("mydb.b")]) == [
            {"value": 0, "Operation": "Create"},
            {"value": 2, "Operation": "Update"},
        ]


# ---------------------------------------------------------------------------
# GlueApiThrottles — glue_connection_provisioner._count_if_throttle
# ---------------------------------------------------------------------------

_PROVISIONER = "coa_sources.database.connectors.glue_connection_provisioner"


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "CreateConnection")


@pytest.mark.unit
class TestGlueApiThrottlesMetric:
    @pytest.mark.parametrize(
        "code",
        ["ThrottlingException", "TooManyRequestsException", "RequestLimitExceeded", "SlowDown"],
    )
    def test_throttle_codes_emit_with_api_dimension(self, code):
        from coa_sources.database.connectors import glue_connection_provisioner as prov

        with patch(f"{_PROVISIONER}.emit_metric") as mock_emit:
            prov._count_if_throttle(_client_error(code), "CreateConnection")

        assert _emitted(mock_emit, "GlueApiThrottles") == [{"value": 1, "Api": "CreateConnection"}]

    @pytest.mark.parametrize("code", ["AccessDeniedException", "AlreadyExistsException", "ValidationException"])
    def test_non_throttle_codes_emit_nothing(self, code):
        """Counting every ClientError as a throttle would make the widget useless."""
        from coa_sources.database.connectors import glue_connection_provisioner as prov

        with patch(f"{_PROVISIONER}.emit_metric") as mock_emit:
            prov._count_if_throttle(_client_error(code), "CreateConnection")

        assert _emitted(mock_emit, "GlueApiThrottles") == []

    def test_lf_grant_failure_counts_the_throttle(self):
        """Wired at a real call site, not just the helper in isolation."""
        from coa_sources.database.connectors import glue_connection_provisioner as prov

        lf = MagicMock()
        lf.grant_permissions.side_effect = _client_error("ThrottlingException")

        with patch(f"{_PROVISIONER}.emit_metric") as mock_emit:
            granted = prov._lf_grant(
                lf,
                {"DataLakePrincipalIdentifier": "arn:aws:iam::1:role/r"},
                {"Database": {"Name": "db"}},
                ["SELECT"],
                database_name="db",
                resource_type="Database",
            )

        assert granted is False
        assert _emitted(mock_emit, "GlueApiThrottles") == [{"value": 1, "Api": "GrantPermissions"}]


class _GlueAlreadyExists(Exception):
    """Stand-in for glue.exceptions.AlreadyExistsException."""


@pytest.mark.unit
class TestProvisionFederatedCatalogThrottles:
    """GlueApiThrottles wired at every provisioning call site, not just LF grant.

    Each step depends on the previous succeeding, so the test throttles one step
    at a time and asserts the Api dimension. A typo in any of the dimension
    strings ("RegisterResource", "CreateCatalog", ...) silently darkens a widget
    series — this pins them.
    """

    _KWARGS = {
        "datasource_id": "DS#abc-123",
        "engine": "POSTGRESQL",
        "host": "db.example.com",
        "port": 5432,
        "database_name": "mydb",
        "credential_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:s-AbCdEf",
    }

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        monkeypatch.setenv("ATHENA_SPILL_BUCKET", "my-spill-bucket")
        monkeypatch.setenv("FEDERATED_CATALOG_ROLE_ARN", "arn:aws:iam::123456789012:role/fed-catalog")

    @pytest.mark.parametrize(
        ("throttle_step", "expected_api"),
        [
            ("create_connection", "CreateConnection"),
            ("register_resource", "RegisterResource"),
            ("create_catalog", "CreateCatalog"),
        ],
    )
    def test_throttle_at_each_step_emits_matching_api(self, throttle_step, expected_api):
        from coa_sources.database.connectors import glue_connection_provisioner as prov

        mock_glue = MagicMock()
        mock_glue.exceptions.AlreadyExistsException = _GlueAlreadyExists
        mock_lf = MagicMock()
        throttle = _client_error("ThrottlingException")
        # register_resource lives on the LF client; the other two on the glue client.
        target = mock_lf if throttle_step == "register_resource" else mock_glue
        getattr(target, throttle_step).side_effect = throttle

        with (
            patch(f"{_PROVISIONER}._get_glue", return_value=mock_glue),
            patch(f"{_PROVISIONER}._get_lakeformation", return_value=mock_lf),
            patch(f"{_PROVISIONER}._get_account_id", return_value="123456789012"),
            patch(f"{_PROVISIONER}.emit_metric") as mock_emit,
            pytest.raises(RuntimeError),
        ):
            prov.provision_federated_catalog(**self._KWARGS)

        assert _emitted(mock_emit, "GlueApiThrottles") == [{"value": 1, "Api": expected_api}]


# ---------------------------------------------------------------------------
# Enrichment acceptance rate — bulk_review/worker + api/database_routes
# ---------------------------------------------------------------------------

_WORKER = "coa_sources.database.bulk_review.worker"


@pytest.mark.unit
class TestBulkReviewAcceptanceMetrics:
    """The bulk half of the acceptance-rate widget (ReviewScope=Bulk)."""

    def _run(self, decision: str, statuses: list[str], fail_asset_ids: set[str] | None = None):
        import json

        os.environ.setdefault("SOURCES_TABLE", "test-sources")
        os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
        os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")

        import coa_sources.database.bulk_review.worker as worker
        from coa_common.datazone_forms import FORM_TYPE_NAME, serialize_form
        from coa_common.domain_models import BusinessMetadata, Column, Table

        namespace_id = "550e8400-e29b-41d4-a716-446655440000"
        tables = [
            Table(
                name=f"t{i}",
                database="db",
                data_source_id="src-db-001",
                namespace_id=namespace_id,
                business_metadata=BusinessMetadata(review_status=status),
                columns=[
                    Column(name="c", data_type="string", business_metadata=BusinessMetadata(review_status=status))
                ],
            )
            for i, status in enumerate(statuses)
        ]

        mock_client = MagicMock()
        items, forms = [], {}
        for i, table in enumerate(tables):
            asset = MagicMock()
            asset.name = f"DS#src-db-001:{table.table_id}"
            asset.asset_id = f"asset-{i}"
            items.append(asset)
            # FORM_TYPE_NAME, not a literal: worker._load_one_asset skips any
            # form whose name does not match, so a hardcoded name silently
            # loads zero assets and the metric assertions below pass vacuously.
            forms[asset.asset_id] = {
                "formsOutput": [{"formName": FORM_TYPE_NAME, "content": json.dumps(serialize_form(table))}]
            }
        mock_client.search_assets.return_value = MagicMock(items=items, next_token=None)
        mock_client.get_asset_forms.side_effect = lambda asset_id: forms[asset_id]

        # A write failure for these asset_ids makes _write_revision return the
        # table_id (its failure signal), which the acceptance metric must exclude.
        failed = fail_asset_ids or set()

        def _revision(*, asset_id: str, **_kwargs):
            if asset_id in failed:
                raise RuntimeError("boom")

        mock_client.create_asset_revision.side_effect = _revision

        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING" if decision == "APPROVED" else "REJECTING"}

        with (
            patch(f"{_WORKER}.emit_metric") as mock_emit,
            patch(f"{_WORKER}.DynamoDBDAO", return_value=mock_dao),
        ):
            worker.process_bulk_review(
                msg=worker.BulkReviewMessage(namespace_id=namespace_id, source_id="src-db-001", decision=decision),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
                scan_jobs_table="test-scan-jobs",
            )
        return mock_emit

    def test_bulk_approve_emits_approved_count(self):
        mock_emit = self._run("APPROVED", ["PENDING_REVIEW", "PENDING_REVIEW"])
        assert _emitted(mock_emit, "TablesApprovedByReview") == [{"value": 2, "ReviewScope": "Bulk"}]
        assert _emitted(mock_emit, "TablesRejectedByReview") == []

    def test_bulk_reject_emits_rejected_count(self):
        mock_emit = self._run("REJECTED", ["PENDING_REVIEW", "PENDING_REVIEW"])
        assert _emitted(mock_emit, "TablesRejectedByReview") == [{"value": 2, "ReviewScope": "Bulk"}]
        assert _emitted(mock_emit, "TablesApprovedByReview") == []

    def test_bulk_approve_counts_transitions_not_selection_size(self):
        """An already-APPROVED table in the selection is not a new decision.

        Mirrors test_idempotent_review_emits_no_acceptance_metric on the
        single-table half: re-approving what is already APPROVED must not
        inflate the acceptance rate, so a 2-table selection with 1 prior
        approval emits 1, not 2.
        """
        mock_emit = self._run("APPROVED", ["APPROVED", "PENDING_REVIEW"])
        assert _emitted(mock_emit, "TablesApprovedByReview") == [{"value": 1, "ReviewScope": "Bulk"}]

    def test_bulk_approve_excludes_failed_writes_from_acceptance_metric(self):
        """A table whose revision write fails is not a kept decision.

        worker.py filters ``transitioned`` by the ``failed`` set before emitting
        the acceptance count (``decided = sum(... if tid not in failed)``). Two
        PENDING tables both transition, but one write raises, so the acceptance
        metric must report 1 — otherwise a phantom approval inflates the rate.
        """
        mock_emit = self._run("APPROVED", ["PENDING_REVIEW", "PENDING_REVIEW"], fail_asset_ids={"asset-0"})
        assert _emitted(mock_emit, "TablesApprovedByReview") == [{"value": 1, "ReviewScope": "Bulk"}]


# The single-table half of the acceptance-rate widget (ReviewScope=Table) is
# tested in tests/unit/api/test_database_routes.py, which already owns the
# _review_env fixture that wires up SMUS + DynamoDB for that handler.
