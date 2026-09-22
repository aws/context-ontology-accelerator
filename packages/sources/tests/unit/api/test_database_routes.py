# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for database_routes.py — DATABASE source route handlers.

Because database_routes.py has a circular import with sources_handler.py,
we import via sources_handler (the Lambda entry point) and patch using
full module path strings so the patches apply to the correct namespace.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, call, create_autospec, patch

import pytest
from botocore.exceptions import ClientError
from coa_control_plane_server.models.source_type import SourceType

# ---------------------------------------------------------------------------
# Environment setup — must happen before module import
# ---------------------------------------------------------------------------
os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
_SOURCE_ID = "src-db-001"

# Import sources_handler FIRST to resolve circular import, then database_routes
import coa_sources.api.sources_handler  # noqa: F401, I001
import coa_sources.api.database_routes as _dr  # noqa: E402, I001
from coa_sources.database import glue_ownership as _go  # noqa: E402, I001

_DR = "coa_sources.api.database_routes"
_SH = "coa_sources.api.sources_handler"


def _source_puts(mock_dao) -> list[dict]:
    """Every ``put`` that wrote a SOURCE row.

    Creating a JDBC or custom-connector source also writes a ``GLUECAT#`` claim
    recording which namespace owns the catalog it will be given, so ``put`` is no
    longer called once and its last call is not the source row.
    """
    return [c.args[0] for c in mock_dao.put.call_args_list if str(c.args[0].get("SK", "")).startswith("SRC#")]


def _source_put(mock_dao) -> dict:
    """The single source row written by a create. Fails loudly if there wasn't one."""
    puts = _source_puts(mock_dao)
    assert len(puts) == 1, f"expected exactly one source row put, got {len(puts)}"
    return puts[0]


def _source_deletes(mock_dao) -> list[dict]:
    """Every ``delete`` that removed a SOURCE row (i.e. not a catalog claim)."""
    return [c.args[0] for c in mock_dao.delete.call_args_list if str(c.args[0].get("SK", "")).startswith("SRC#")]


@pytest.fixture(autouse=True)
def reset_lazy_clients():
    """Reset module-level lazy client globals between tests to prevent cross-test pollution."""
    import coa_sources.api.namespace_counters as nc
    import coa_sources.api.sources_handler as sh

    sh._dao = None
    sh._sqs = None
    sh._s3 = None
    sh._sfn = None
    sh._scan_dao = None
    sh._ns_dao = None
    nc._ns_dao = None
    # The credential-secret → namespace binding check (DescribeSecret + tag
    # match) is exercised in test_credential_secret_binding.py. Stub it to a pass
    # here so the create-source tests can assert routing/metadata without setting
    # up a tagged secret.
    with (
        patch("coa_sources.api.database_routes.adjust_namespace_source_count"),
        patch("coa_sources.api.database_routes._validate_credential_secret_binding", return_value=None),
    ):
        yield
    sh._dao = None
    sh._sqs = None
    sh._s3 = None
    sh._sfn = None
    sh._scan_dao = None
    sh._ns_dao = None
    nc._ns_dao = None


def _parse(result):
    return result["statusCode"], json.loads(result["body"]) if result.get("body") else {}


def _make_glue_db_req(athena_data_catalog_name=None, execution_engine=None, redshift_workgroup=None):
    req = MagicMock()
    req.name = "my-glue-db"
    req.glue_configuration = MagicMock()
    # Mirror the real GlueConfiguration.to_dict(), which carries every member the
    # caller set — including athenaDataCatalogName. The blob is the only place a
    # caller's declared catalog is echoed back on GetSource now that it is kept out
    # of the system-managed top-level attribute, so a to_dict() that dropped it
    # would hide that regression rather than catch it.
    req.glue_configuration.to_dict.return_value = {
        "databaseName": "mydb",
        "region": "us-east-1",
        **({"athenaDataCatalogName": athena_data_catalog_name} if athena_data_catalog_name else {}),
    }
    req.glue_configuration.database_name = "mydb"
    req.glue_configuration.region = "us-east-1"
    req.glue_configuration.catalog_id = "123456789012"
    req.glue_configuration.athena_data_catalog_name = athena_data_catalog_name
    # Default a Glue source to no Redshift opt-in. MagicMock auto-creates
    # truthy attributes, which would falsely trip the Redshift branch — set both
    # explicitly so ATHENA remains the default unless a test opts in.
    req.glue_configuration.execution_engine = execution_engine
    req.glue_configuration.redshift_workgroup = redshift_workgroup
    req.jdbc_configuration = None
    req.custom_connector_configuration = None
    req.metadata_enrichment_enabled = None
    return req


def _make_jdbc_db_req(engine="POSTGRESQL"):
    req = MagicMock()
    req.name = "my-jdbc-db"
    req.jdbc_configuration = MagicMock()
    req.jdbc_configuration.engine = engine
    req.jdbc_configuration.to_dict.return_value = {"jdbcUrl": "jdbc:mysql://host/db"}
    req.glue_configuration = None
    req.custom_connector_configuration = None
    req.metadata_enrichment_enabled = None
    return req


# ===================================================================
# _create_database_source
# ===================================================================


@pytest.mark.unit
class TestCreateDatabaseSource:
    def test_create_glue_source_happy_path(self):
        mock_dao = MagicMock()
        mock_scan_dao = MagicMock()
        mock_sqs = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
            patch(f"{_DR}._get_sqs", return_value=mock_sqs),
        ):
            status, body = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 202
        assert "sourceId" in body
        assert body["status"] == "REGISTERED"
        assert "scanJobId" in body
        assert len(_source_puts(mock_dao)) == 1
        mock_sqs.send_message.assert_called_once()

    def test_create_glue_source_records_the_declared_catalog_without_system_authority(self):
        """A caller's nested catalog must reach the query layer via `athenaCatalog`
        and NOT via `athenaDataCatalogName`.

        `athenaDataCatalogName` is system-managed: DELETE derives the name it expects
        there and runs a Lake-Formation-admin teardown against it under IAM scoped to
        the whole deployment's `{prefix}ds_*` window, not to one namespace. Writing a
        caller's value there once let a steward name another namespace's federated
        catalog and have their own delete tear it down.
        """
        mock_dao = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_glue_db_req(athena_data_catalog_name="my_cat"), _NAMESPACE_ID)

        put_item = _source_put(mock_dao)
        assert put_item["athenaCatalog"] == "my_cat"
        assert "athenaDataCatalogName" not in put_item
        # Still round-trips to the client, out of the configuration blob.
        assert json.loads(put_item["configuration"])["athenaDataCatalogName"] == "my_cat"

    def test_create_glue_source_persists_query_metadata(self):
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID)

        put_item = _source_put(mock_dao)
        assert put_item["athenaCatalog"] == "AwsDataCatalog"
        assert put_item["athenaDatabase"] == "mydb"
        assert put_item["region"] == "us-east-1"
        assert put_item["queryable"] is False
        assert put_item["queryEngine"] == "ATHENA"

    def test_glue_nested_catalog_id_resolves_to_nested_catalog_name(self):
        mock_dao = MagicMock()
        req = _make_glue_db_req()
        req.glue_configuration.catalog_id = "999999999999:salescat"
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(req, _NAMESPACE_ID)
        assert _source_put(mock_dao)["athenaCatalog"] == "salescat"

    def test_glue_explicit_athena_catalog_name_wins(self):
        mock_dao = MagicMock()
        req = _make_glue_db_req(athena_data_catalog_name="explicit_cat")
        req.glue_configuration.catalog_id = "999999999999:ignored"
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(req, _NAMESPACE_ID)
        assert _source_put(mock_dao)["athenaCatalog"] == "explicit_cat"

    def test_create_jdbc_source_persists_query_metadata(self):
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_jdbc_db_req(), _NAMESPACE_ID)

        put_item = _source_put(mock_dao)
        assert put_item["athenaCatalog"] == "AwsDataCatalog"
        assert put_item["queryable"] is False
        assert put_item["region"]  # deployment region
        assert put_item["queryEngine"] == "JDBC"  # direct single-source execution
        # JDBC schema is resolved per-table, so no single athenaDatabase.
        assert "athenaDatabase" not in put_item

    def test_create_jdbc_source_mysql_uses_direct_jdbc(self):
        # MySQL now has a direct-JDBC serve adapter (aiomysql), so it routes to
        # the direct path rather than Athena federation.
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_jdbc_db_req(engine="MYSQL"), _NAMESPACE_ID)

        assert _source_put(mock_dao)["queryEngine"] == "JDBC"

    def test_create_jdbc_source_sqlserver_uses_direct_jdbc(self):
        # SQL Server now has a direct-JDBC serve adapter (python-tds).
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_jdbc_db_req(engine="SQLSERVER"), _NAMESPACE_ID)

        assert _source_put(mock_dao)["queryEngine"] == "JDBC"

    def test_create_jdbc_source_without_direct_dialect_uses_athena(self):
        # An engine with no direct dialect yet (e.g. Snowflake) must fall back to
        # Athena, not claim a direct JDBC path that isn't implemented.
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_jdbc_db_req(engine="SNOWFLAKE"), _NAMESPACE_ID)

        assert _source_put(mock_dao)["queryEngine"] == "ATHENA"

    def test_create_glue_source_omits_catalog_name_when_absent(self):
        mock_dao = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID)

        assert "athenaDataCatalogName" not in _source_put(mock_dao)

    # ── Redshift execution engine for Glue sources ──────────────

    def test_glue_source_defaults_to_athena_engine(self):
        """A Glue source with no executionEngine keeps queryEngine=ATHENA and
        persists no redshiftWorkgroup (default, unchanged behaviour)."""
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID)
        put_item = _source_put(mock_dao)
        assert put_item["queryEngine"] == "ATHENA"
        assert "redshiftWorkgroup" not in put_item

    def test_glue_redshift_engine_sets_query_engine_and_persists_workgroup(self):
        """executionEngine=REDSHIFT → queryEngine=REDSHIFT + redshiftWorkgroup persisted."""
        mock_dao = MagicMock()
        req = _make_glue_db_req(execution_engine="REDSHIFT", redshift_workgroup="my-wg")
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(req, _NAMESPACE_ID)
        put_item = _source_put(mock_dao)
        assert put_item["queryEngine"] == "REDSHIFT"
        assert put_item["redshiftWorkgroup"] == "my-wg"

    def test_glue_redshift_engine_object_value_is_unwrapped(self):
        """executionEngine may arrive as an enum-like object with a .value —
        the resolver must read .value, not str(obj)."""
        mock_dao = MagicMock()
        engine_obj = MagicMock()
        engine_obj.value = "REDSHIFT"
        req = _make_glue_db_req(execution_engine=engine_obj, redshift_workgroup="wg2")
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(req, _NAMESPACE_ID)
        assert _source_put(mock_dao)["queryEngine"] == "REDSHIFT"

    def test_glue_redshift_engine_without_workgroup_rejected(self):
        """A Redshift-engine Glue source with no workgroup is a 400 — the backend
        cannot infer which Serverless workgroup to use."""
        mock_dao = MagicMock()
        req = _make_glue_db_req(execution_engine="REDSHIFT", redshift_workgroup=None)
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            status, body = _parse(_dr._create_database_source(req, _NAMESPACE_ID))
        assert status == 400
        assert "redshiftWorkgroup" in body["error"]
        mock_dao.put.assert_not_called()

    def test_glue_athena_engine_does_not_persist_stray_workgroup(self):
        """An ATHENA (default) Glue source that happens to carry a workgroup does
        NOT persist it — the workgroup is only meaningful (and stored) for the
        REDSHIFT execution path."""
        mock_dao = MagicMock()
        req = _make_glue_db_req(execution_engine="ATHENA", redshift_workgroup="stray")
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(req, _NAMESPACE_ID)
        put_item = _source_put(mock_dao)
        assert put_item["queryEngine"] == "ATHENA"
        assert "redshiftWorkgroup" not in put_item

    def test_jdbc_redshift_source_unaffected_by_glue_engine_logic(self):
        """A native Redshift JDBC source still resolves to queryEngine=JDBC — the
        new Glue executionEngine branch must not touch the JDBC path."""
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_jdbc_db_req(engine="REDSHIFT"), _NAMESPACE_ID)
        put_item = _source_put(mock_dao)
        assert put_item["queryEngine"] == "JDBC"
        assert "redshiftWorkgroup" not in put_item

    def test_create_jdbc_source_happy_path(self):
        mock_dao = MagicMock()
        mock_scan_dao = MagicMock()
        mock_sqs = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
            patch(f"{_DR}._get_sqs", return_value=mock_sqs),
        ):
            status, body = _parse(_dr._create_database_source(_make_jdbc_db_req(), _NAMESPACE_ID))

        assert status == 202
        assert "sourceId" in body
        assert len(_source_puts(mock_dao)) == 1
        put_item = _source_put(mock_dao)
        assert put_item["sourceSubType"] == "JDBC_DATABASE"

    def test_create_omits_metadata_enrichment_when_unset(self):
        """When the caller does not specify the enrichment toggle, the field
        is absent from the persisted item — readers treat this as 'enabled'
        (the historical default). Round-trips never persist a synthetic value."""
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID)

        assert "metadataEnrichmentEnabled" not in _source_put(mock_dao)

    def test_create_persists_metadata_enrichment_disabled(self):
        mock_dao = MagicMock()
        req = _make_glue_db_req()
        req.metadata_enrichment_enabled = False
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(req, _NAMESPACE_ID)

        assert _source_put(mock_dao)["metadataEnrichmentEnabled"] is False

    def test_create_persists_metadata_enrichment_enabled_explicitly(self):
        mock_dao = MagicMock()
        req = _make_jdbc_db_req()
        req.metadata_enrichment_enabled = True
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _dr._create_database_source(req, _NAMESPACE_ID)

        assert _source_put(mock_dao)["metadataEnrichmentEnabled"] is True

    def test_create_blank_name_returns_400(self):
        req = _make_glue_db_req()
        req.name = "   "
        status, body = _parse(_dr._create_database_source(req, _NAMESPACE_ID))
        assert status == 400
        assert "name" in body["error"]

    def test_create_no_config_returns_400(self):
        req = _make_glue_db_req()
        req.glue_configuration = None
        req.jdbc_configuration = None
        status, body = _parse(_dr._create_database_source(req, _NAMESPACE_ID))
        assert status == 400

    def test_create_ddb_put_fails_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.put.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "PutItem")
        mock_scan_dao = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, _ = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 500

    def test_create_sqs_fails_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_scan_dao = MagicMock()
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError({"Error": {"Code": "SQSError"}}, "SendMessage")

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
            patch(f"{_DR}._get_sqs", return_value=mock_sqs),
        ):
            status, _ = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 500
        # Atomic rollback: the partial source + scan-job records are removed,
        # and the source is NOT mislabeled SCAN_FAILED.
        assert len(_source_deletes(mock_dao)) == 1
        mock_scan_dao.delete.assert_called_once()
        assert not any(c.args and c.args[1].get("status") == "SCAN_FAILED" for c in mock_dao.update.call_args_list)

    def test_create_no_scan_queue_url_skips_sqs(self):
        mock_dao = MagicMock()
        mock_scan_dao = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
            patch(f"{_DR}._SCAN_QUEUE_URL", ""),
        ):
            status, _ = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 202

    def test_create_stores_source_type_database(self):
        mock_dao = MagicMock()
        mock_scan_dao = MagicMock()
        mock_sqs = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
            patch(f"{_DR}._get_sqs", return_value=mock_sqs),
        ):
            status, body = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 202
        put_item = _source_put(mock_dao)
        assert put_item["sourceType"] == "DATABASE"
        assert put_item["namespaceId"] == _NAMESPACE_ID


# ===================================================================
# _create_database_source — CUSTOM_CONNECTOR (custom connector)
# ===================================================================


def _make_athena_db_req(
    metadata_arn="arn:aws:lambda:us-east-1:111122223333:function:acme-connector",
    database_name="widgets",
):
    req = MagicMock()
    req.name = "my-connector"
    req.glue_configuration = None
    req.jdbc_configuration = None
    req.custom_connector_configuration = MagicMock()
    req.custom_connector_configuration.connector_function_arn = metadata_arn
    req.custom_connector_configuration.database_name = database_name
    req.custom_connector_configuration.to_dict.return_value = {
        "connectorFunctionArn": metadata_arn,
        "databaseName": database_name,
    }
    req.metadata_enrichment_enabled = None
    return req


@pytest.mark.unit
class TestCreateCustomConnectorSource:
    """Creating a source backed by a customer-authored Athena federation
    connector. The load-bearing parts are the ORDER (DynamoDB row before the
    catalog, because every delete path keys off the row) and the rollbacks, since
    a catalog nobody has a record of cannot be found again."""

    def _create(self, req, *, register=None, delete=None, sqs=None, dao=None):
        mock_dao = dao or MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=sqs or MagicMock()),
            patch(f"{_DR}.register_lambda_catalog", register or MagicMock()) as reg,
            patch(f"{_DR}.delete_lambda_catalog", delete or MagicMock()) as dele,
        ):
            status, body = _parse(_dr._create_database_source(req, _NAMESPACE_ID))
        return status, body, mock_dao, reg, dele

    def test_derives_the_custom_connector_sub_type(self):
        status, _, mock_dao, _, _ = self._create(_make_athena_db_req())
        assert status == 202
        assert _source_put(mock_dao)["sourceSubType"] == "CUSTOM_CONNECTOR"

    def test_the_catalog_name_is_derived_from_the_source_id(self):
        """Not caller-supplied, and not arbitrary. Two properties rest on this: a
        1:1 source-to-catalog mapping (so teardown removes only this source's
        catalog), and the `{sanitizedPrefix}ds_*` shape the IAM policy is scoped
        to — any other derivation fails closed as AccessDenied."""
        from coa_sources.database.connectors.athena_catalog import derive_catalog_name

        _, _, mock_dao, reg, _ = self._create(_make_athena_db_req())
        item = _source_put(mock_dao)
        expected = derive_catalog_name(item["sourceId"])
        assert item["athenaDataCatalogName"] == expected
        assert reg.call_args.kwargs["catalog_name"] == expected

    def test_persists_the_query_layer_attributes(self):
        _, _, mock_dao, _, _ = self._create(_make_athena_db_req(database_name="sales"))
        item = _source_put(mock_dao)
        # queryEngine is ATHENA because there is no direct adapter for an
        # arbitrary customer connector.
        assert item["queryEngine"] == "ATHENA"
        # Serve routes on the PRESENCE of athenaDataCatalogName, so this is what
        # makes the source addressable at all.
        assert item["athenaDataCatalogName"]
        assert item["athenaCatalog"] == item["athenaDataCatalogName"]
        # Recorded up front rather than waiting for discovery, so serve has the
        # right namespace even when a scan discovers zero tables.
        assert item["athenaDatabase"] == "sales"
        assert item["queryable"] is False
        assert item["region"] == _dr._AWS_REGION

    def test_registers_the_catalog_with_the_derived_name(self):
        _, _, mock_dao, reg, _ = self._create(_make_athena_db_req())
        item = _source_put(mock_dao)
        assert reg.call_args.kwargs["catalog_name"] == item["athenaDataCatalogName"]
        assert reg.call_args.kwargs["connector_function_arn"].endswith("acme-connector")
        # One ARN reaches the catalog registrar, because the shape models one. A
        # `record_function_arn` kwarg would not be ignored here — it no longer exists.
        assert "record_function_arn" not in reg.call_args.kwargs

    # A catalog registered before the row would be orphaned: every delete path
    # finds the catalog through the source record.
    def test_registers_the_catalog_after_the_dynamodb_put(self):
        order: list[str] = []
        mock_dao = MagicMock()
        # The catalog claim is written between the two and is not what this
        # orders, so record only the source row. `**_` because the claim write
        # passes `auto_timestamp=False` (it must stay out of the ByNamespace GSI).
        mock_dao.put.side_effect = lambda item, **_: (
            order.append("put") if str(item.get("SK", "")).startswith("SRC#") else None
        )
        self._create(
            _make_athena_db_req(),
            dao=mock_dao,
            register=MagicMock(side_effect=lambda **kw: order.append("register")),
        )
        assert order == ["put", "register"]

    def test_rolls_back_the_row_when_registration_fails(self):
        from coa_sources.database.connectors.athena_catalog import AthenaCatalogError

        status, _, mock_dao, _, _ = self._create(
            _make_athena_db_req(),
            register=MagicMock(side_effect=AthenaCatalogError("nope")),
        )
        assert status == 500
        # A source with no catalog could never be discovered, so it must not
        # survive the failed create.
        assert len(_source_deletes(mock_dao)) == 1

    # A read timeout after Athena committed the create is indistinguishable from a
    # failure, so the catalog may exist — and the row that names it is about to go.
    def test_deletes_the_catalog_when_registration_reports_failure(self):
        from coa_sources.database.connectors.athena_catalog import AthenaCatalogError

        _, _, mock_dao, _, dele = self._create(
            _make_athena_db_req(),
            register=MagicMock(side_effect=AthenaCatalogError("timeout")),
        )
        item = _source_put(mock_dao)
        dele.assert_called_once_with(catalog_name=item["athenaDataCatalogName"])

    # ...but NOT on a conflict: that catalog demonstrably belongs to something
    # else, and deleting it would destroy a resource we did not create.
    def test_does_not_delete_a_conflicting_catalog(self):
        from coa_sources.database.connectors.athena_catalog import AthenaCatalogConflictError

        status, _, mock_dao, _, dele = self._create(
            _make_athena_db_req(),
            register=MagicMock(side_effect=AthenaCatalogConflictError("someone else's")),
        )
        assert status == 500
        dele.assert_not_called()
        assert len(_source_deletes(mock_dao)) == 1

    # REGISTERED is an active status, so a row that survives a failed rollback is
    # undeletable (409), un-rescannable, and blocks namespace deletion — and no
    # reaper sweeps it, because no Step Functions execution ever started.
    def test_marks_the_source_recoverable_when_the_rollback_delete_fails(self):
        from coa_sources.database.connectors.athena_catalog import AthenaCatalogError

        mock_dao = MagicMock()
        mock_dao.delete.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteItem")
        status, _, _, _, _ = self._create(
            _make_athena_db_req(),
            dao=mock_dao,
            register=MagicMock(side_effect=AthenaCatalogError("nope")),
        )
        assert status == 500
        # SCAN_FAILED is what makes it both deletable and re-scannable again.
        assert mock_dao.update.call_args.kwargs["update_fields"]["status"] == "SCAN_FAILED"

    def test_registration_failure_enqueues_no_scan(self):
        from coa_sources.database.connectors.athena_catalog import AthenaCatalogError

        mock_sqs = MagicMock()
        self._create(
            _make_athena_db_req(),
            sqs=mock_sqs,
            register=MagicMock(side_effect=AthenaCatalogError("nope")),
        )
        mock_sqs.send_message.assert_not_called()

    # The row is about to be removed, and it is the only handle on the catalog.
    def test_deletes_the_catalog_when_the_scan_enqueue_fails(self):
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "SendMessage")
        status, _, mock_dao, _, dele = self._create(_make_athena_db_req(), sqs=mock_sqs)
        assert status == 500
        item = _source_put(mock_dao)
        dele.assert_called_once_with(catalog_name=item["athenaDataCatalogName"])

    # Failing the rollback must not turn a retryable create failure into an
    # unretryable one, so the 500 is still returned.
    def test_a_failed_rollback_delete_still_returns_500(self):
        from coa_sources.database.connectors.athena_catalog import AthenaCatalogError

        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "SendMessage")
        status, _, _, _, _ = self._create(
            _make_athena_db_req(),
            sqs=mock_sqs,
            delete=MagicMock(side_effect=AthenaCatalogError("still there")),
        )
        assert status == 500

    # A cross-region connector registers fine — registration only records a
    # name → ARN mapping — and then fails every statement with an AccessDenied
    # that says nothing about the region, because the IAM grant is region-pinned.
    def test_rejects_a_connector_arn_in_another_region(self):
        req = _make_athena_db_req(metadata_arn="arn:aws:lambda:eu-west-1:111122223333:function:acme")
        status, body, _, reg, _ = self._create(req)
        assert status == 400
        assert "eu-west-1" in body["error"]
        reg.assert_not_called()

    # The config member selects the sub-type, so two of them is ambiguous rather
    # than additive — silently preferring one would persist a record whose stored
    # blob does not match its sourceSubType.
    def test_rejects_more_than_one_configuration(self):
        req = _make_athena_db_req()
        req.jdbc_configuration = MagicMock()
        status, body, _, reg, _ = self._create(req)
        assert status == 400
        assert "exactly one" in body["error"]
        reg.assert_not_called()

    def test_error_names_custom_connector_configuration_when_no_config_is_given(self):
        req = _make_athena_db_req()
        req.custom_connector_configuration = None
        status, body, _, _, _ = self._create(req)
        assert status == 400
        assert "customConnectorConfiguration" in body["error"]

    # Glue and JDBC sources must not acquire a catalog registration.
    def test_a_glue_source_registers_no_catalog(self):
        _, _, mock_dao, reg, _ = self._create(_make_glue_db_req())
        reg.assert_not_called()
        assert "athenaDataCatalogName" not in _source_put(mock_dao)

    def test_a_jdbc_source_registers_no_catalog(self):
        _, _, mock_dao, reg, _ = self._create(_make_jdbc_db_req())
        reg.assert_not_called()
        # Nor the attribute: a JDBC source's federated catalog does not exist until
        # the post-discovery federation step provisions it, and that step is what
        # writes the name. A value present at create could only be a caller's, and
        # DELETE reads this attribute to aim a Lake-Formation-admin teardown.
        assert "athenaDataCatalogName" not in mock_dao.put.call_args[0][0]


# ===================================================================
# _create_database_source — namespace sourceCount maintenance
# ===================================================================


@pytest.mark.unit
class TestExternalIdIsNotCallerControlled:
    """A caller must not be able to choose the ExternalId used on the assume.

    The role ARN is caller-supplied; the ExternalId is derived from the namespace
    at discovery time (``discovery_handler._external_id``). If the API persisted a
    caller-supplied value, the caller would get back control of the only thing
    binding an assume to its namespace, and could read another tenant's source.
    """

    @staticmethod
    def _stored_config(mock_dao):
        args, kwargs = mock_dao.put.call_args.args, mock_dao.put.call_args.kwargs
        item = args[0] if args else kwargs["item"]
        return json.loads(item["configuration"])

    def test_create_drops_caller_supplied_external_id(self):
        mock_dao = MagicMock()
        req = _make_glue_db_req()
        req.glue_configuration.to_dict.return_value = {
            "databaseName": "mydb",
            "region": "us-east-1",
            "crossAccountRoleArn": "arn:aws:iam::999999999999:role/scl-dev-datasource-access-victim",
            "externalId": "the-victims-external-id",
        }

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            status, _ = _parse(_dr._create_database_source(req, _NAMESPACE_ID))

        assert status == 202
        config = self._stored_config(mock_dao)
        assert "externalId" not in config
        # The role ARN is still stored — it is the assume target, not the control.
        assert config["crossAccountRoleArn"].endswith("datasource-access-victim")

    def test_create_keeps_other_config_fields(self):
        """The strip must be surgical — nothing else may be dropped."""
        mock_dao = MagicMock()
        req = _make_glue_db_req()
        req.glue_configuration.to_dict.return_value = {
            "databaseName": "mydb",
            "region": "us-east-1",
            "tableFilter": "sales_*",
            "externalId": "nope",
        }

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _parse(_dr._create_database_source(req, _NAMESPACE_ID))

        config = self._stored_config(mock_dao)
        assert config["tableFilter"] == "sales_*"
        assert config["databaseName"] == "mydb"
        assert "externalId" not in config

    @staticmethod
    def _updated_config(mock_dao):
        args, kwargs = mock_dao.update.call_args.args, mock_dao.update.call_args.kwargs
        update_fields = args[1] if len(args) >= 2 else kwargs["update_fields"]
        return json.loads(update_fields["configuration"])

    # `databaseName` echoes the stored value rather than repointing: the target is
    # immutable after create (`_apply_glue_configuration_update`), so changing it
    # here would return 400 and test that guard instead of the externalId strip.
    def test_update_drops_caller_supplied_external_id(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "APPROVED",
            "updatedAt": "2026-01-01T00:00:00Z",
            "configuration": json.dumps({"databaseName": "mydb", "region": "us-east-1"}),
        }

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {
                "body": json.dumps(
                    {
                        "glueConfiguration": {
                            "catalogId": "123456789012",
                            "region": "us-east-1",
                            "databaseName": "mydb",
                            "externalId": "injected-on-update",
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert "externalId" not in self._updated_config(mock_dao)

    def test_update_carries_forward_a_legacy_stored_external_id(self):
        """LEGACY BRIDGE: a pre-existing value survives an unrelated config edit.

        Its trust policy still pins that value, so dropping it here would break
        the source's next scan. The caller's value is still ignored.
        """
        mock_dao = MagicMock()
        mock_dao.get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "APPROVED",
            "updatedAt": "2026-01-01T00:00:00Z",
            "configuration": json.dumps({"databaseName": "mydb", "region": "us-east-1", "externalId": "legacy-value"}),
        }

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {
                "body": json.dumps(
                    {
                        "glueConfiguration": {
                            "catalogId": "123456789012",
                            "region": "us-east-1",
                            "databaseName": "mydb",
                            "externalId": "attacker-chosen",
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert self._updated_config(mock_dao)["externalId"] == "legacy-value"

    def test_update_carries_forward_legacy_external_id_from_dict_config(self):
        """Some older records store `configuration` as a dict, not a JSON string.

        discovery_handler tolerates both shapes, so the carry-forward must too —
        otherwise updating one of those sources silently drops its pinned
        ExternalId and its next scan fails on AccessDenied.
        """
        mock_dao = MagicMock()
        mock_dao.get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "APPROVED",
            "updatedAt": "2026-01-01T00:00:00Z",
            "configuration": {"databaseName": "mydb", "externalId": "legacy-dict-value"},
        }

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {
                "body": json.dumps(
                    {
                        "glueConfiguration": {
                            "catalogId": "123456789012",
                            "region": "us-east-1",
                            "databaseName": "mydb",
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert self._updated_config(mock_dao)["externalId"] == "legacy-dict-value"


@pytest.mark.unit
class TestCreateDatabaseSourceCounter:
    """The namespace ``sourceCount`` must be incremented when a DATABASE
    source is created, and rolled back (decremented) if the partial create
    is unwound because the scan could not be enqueued.

    The autouse ``reset_lazy_clients`` fixture patches
    ``adjust_namespace_source_count`` globally; each test re-patches it with a
    local handle so the call can be asserted.
    """

    def test_create_increments_namespace_source_count(self):
        mock_dao = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
            patch(f"{_DR}.adjust_namespace_source_count") as mock_counter,
        ):
            status, _ = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 202
        mock_counter.assert_called_once_with(_NAMESPACE_ID, SourceType.DATABASE, 1)

    def test_create_rolls_back_count_when_scan_enqueue_fails(self):
        """If the scan message cannot be enqueued the source row is deleted to
        avoid an orphaned record; the counter must be decremented to match so
        it does not drift above the true source total."""
        mock_dao = MagicMock()
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError({"Error": {"Code": "ServiceUnavailable"}}, "SendMessage")

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DR}._SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue"),
            patch(f"{_DR}.adjust_namespace_source_count") as mock_counter,
        ):
            status, _ = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 500
        # +1 on create, then -1 once the source row is rolled back.
        assert mock_counter.call_args_list == [
            call(_NAMESPACE_ID, SourceType.DATABASE, 1),
            call(_NAMESPACE_ID, SourceType.DATABASE, -1),
        ]
        assert len(_source_deletes(mock_dao)) == 1

    def test_create_does_not_double_count_when_ddb_put_fails(self):
        """If the source row never persists (DDB put fails) the counter must
        not be touched at all — there is nothing to count."""
        mock_dao = MagicMock()
        mock_dao.put.side_effect = ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "PutItem")

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
            patch(f"{_DR}.adjust_namespace_source_count") as mock_counter,
        ):
            status, _ = _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        assert status == 500
        mock_counter.assert_not_called()


# ===================================================================
# _handle_get_scan_job
# ===================================================================


@pytest.mark.unit
class TestHandleGetScanJob:
    def test_get_scan_job_happy_path(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.return_value = {
            "PK": f"SRC#{_SOURCE_ID}",
            "SK": "2026-01-01T00:00:00Z",
            "status": "COMPLETED",
            "scanType": "full",
            "tablesDiscovered": 5,
            "startedAt": "2026-01-01T00:00:00Z",
            "completedAt": "2026-01-01T01:00:00Z",
        }

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, body = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "2026-01-01T00:00:00Z"))

        assert status == 200
        assert body["status"] == "COMPLETED"
        assert body["tablesDiscovered"] == 5

    def test_get_scan_job_surfaces_enrichment_partial_failure(self):
        # A scan that enriched some tables but had per-table failures records
        # the failed names on the scan-job row; the read handler surfaces them
        # so a steward can see which tables came back without enrichment.
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.return_value = {
            "PK": f"SRC#{_SOURCE_ID}",
            "SK": "2026-01-01T00:00:00Z",
            "status": "COMPLETED",
            "enrichmentPartialFailure": True,
            "enrichmentFailedTables": ["public.rescan_demo_widgets"],
        }

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, body = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "2026-01-01T00:00:00Z"))

        assert status == 200
        assert body["enrichmentPartialFailure"] is True
        assert body["enrichmentFailedTables"] == ["public.rescan_demo_widgets"]

    def test_get_scan_job_omits_partial_failure_fields_on_clean_scan(self):
        # Absent on a clean scan — the None-drop keeps the response backward compatible.
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.return_value = {
            "PK": f"SRC#{_SOURCE_ID}",
            "SK": "2026-01-01T00:00:00Z",
            "status": "COMPLETED",
        }

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, body = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "2026-01-01T00:00:00Z"))

        assert status == 200
        assert "enrichmentPartialFailure" not in body
        assert "enrichmentFailedTables" not in body

    def test_get_scan_job_source_not_found(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = None
        mock_scan_dao = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, _ = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "job-123"))

        assert status == 404

    def test_get_scan_job_job_not_found(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.return_value = None

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, _ = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "missing-job"))

        assert status == 404

    def test_get_scan_job_ddb_error_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.get.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "GetItem")
        mock_scan_dao = MagicMock()

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, _ = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "job-123"))

        assert status == 500

    def test_get_scan_job_scan_dao_error_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "GetItem")

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, _ = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "job-123"))

        assert status == 500

    def test_get_scan_job_decodes_url_encoded_timestamp(self):
        # ISO-8601 job IDs contain colons that arrive percent-encoded in the
        # path (e.g. "2026-01-01T00%3A00%3A00Z"). The handler must unquote()
        # before looking up the scan-jobs row by SK.
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.return_value = {
            "PK": f"SRC#{_SOURCE_ID}",
            "SK": "2026-01-01T00:00:00Z",
            "status": "COMPLETED",
        }

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, body = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "2026-01-01T00%3A00%3A00Z"))

        assert status == 200
        # The scan-jobs lookup must use the decoded SK, not the encoded one.
        scan_lookup_key = mock_scan_dao.get.call_args[0][0]
        assert scan_lookup_key["SK"] == "2026-01-01T00:00:00Z"
        # The decoded id is echoed back in the response.
        assert body["scanJobId"] == "2026-01-01T00:00:00Z"

    def test_get_scan_job_handles_already_decoded_timestamp(self):
        # unquote() on an already-decoded string is a no-op — a plain ISO
        # timestamp (no percent-encoding) must still resolve correctly.
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.return_value = {
            "PK": f"SRC#{_SOURCE_ID}",
            "SK": "2026-01-01T00:00:00Z",
            "status": "COMPLETED",
        }

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, body = _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "2026-01-01T00:00:00Z"))

        assert status == 200
        assert mock_scan_dao.get.call_args[0][0]["SK"] == "2026-01-01T00:00:00Z"
        assert body["scanJobId"] == "2026-01-01T00:00:00Z"


@pytest.mark.unit
class TestUpdateCustomConnectorConfiguration:
    """The connector Lambda ARN is baked into the registered Athena data catalog at
    create, and nothing re-registers it afterwards — so accepting a new ARN here
    would leave every read path reporting it while the catalog still invoked the old
    Lambda."""

    _ARN = "arn:aws:lambda:us-east-1:111122223333:function:acme-connector"

    def _item(self, **config_overrides):
        config = {"connectorFunctionArn": self._ARN, "databaseName": "widgets"}
        config.update(config_overrides)
        return {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceId": _SOURCE_ID,
            "sourceType": "DATABASE",
            "sourceSubType": "CUSTOM_CONNECTOR",
            "status": "APPROVED",
            "athenaDatabase": "widgets",
            "configuration": json.dumps(config),
            "updatedAt": "2026-01-01T00:00:00Z",
        }

    def _update(self, body, item=None):
        mock_dao = MagicMock()
        mock_dao.get.return_value = item or self._item()
        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            status, resp = _parse(_dr._handle_update_metadata({"body": json.dumps(body)}, _NAMESPACE_ID, _SOURCE_ID))
        return status, resp, mock_dao

    def test_rejects_a_changed_connector_function_arn(self):
        status, body, mock_dao = self._update(
            {
                "customConnectorConfiguration": {
                    "connectorFunctionArn": "arn:aws:lambda:us-east-1:111122223333:function:acme-v2",
                    "databaseName": "widgets",
                }
            }
        )
        assert status == 400
        assert "cannot be changed after creation" in body["error"]
        mock_dao.update.assert_not_called()

    def test_ignores_an_unmodelled_record_function_arn(self):
        """A stray recordFunctionArn is not an immutable-field change — it is not a field.

        The member was removed from CustomConnectorConfiguration, so a client still sending it is
        sending something the shape does not define. That must not be mistaken for an
        attempt to change an immutable ARN: the immutability check iterates the members
        that exist, and an unknown key is simply not one of them.
        """
        status, _, mock_dao = self._update(
            {
                "customConnectorConfiguration": {
                    "connectorFunctionArn": self._ARN,
                    "recordFunctionArn": "arn:aws:lambda:us-east-1:111122223333:function:acme-record",
                    "databaseName": "widgets",
                }
            }
        )
        assert status == 200
        mock_dao.update.assert_called_once()

    # Serve prefers discoveredSchemas[0] and falls back to athenaDatabase only when
    # that list is empty. Discovery writes it on every successful scan, so writing
    # athenaDatabase retargets nothing: the update would return 200 while serve kept
    # querying the old database, and a re-scan cannot reconcile it because
    # _handle_rescan 409s any DATABASE source that is not SCAN_FAILED.
    def test_rejects_a_changed_database_name(self):
        status, body, mock_dao = self._update(
            {"customConnectorConfiguration": {"connectorFunctionArn": self._ARN, "databaseName": "gadgets"}}
        )
        assert status == 400
        assert "databaseName cannot be changed after creation" in body["error"]
        # Names the target, so the message is actionable without reading the source.
        assert "gadgets" in body["error"]
        mock_dao.update.assert_not_called()

    # Rejecting a CHANGE must not reject an echo: the shape is shared with create,
    # where databaseName is required, so a client editing a filter sends the whole
    # configuration back including the unchanged database.
    def test_allows_an_unchanged_database_name(self):
        status, _, mock_dao = self._update(
            {"customConnectorConfiguration": {"connectorFunctionArn": self._ARN, "databaseName": "widgets"}}
        )
        assert status == 200
        mock_dao.update.assert_called_once()

    # A source whose athenaDatabase attribute was never written still gets one:
    # mirroring an unchanged value does not move where the source points.
    def test_mirrors_the_database_name_when_the_attribute_is_absent(self):
        item = self._item()
        del item["athenaDatabase"]
        status, _, mock_dao = self._update(
            {"customConnectorConfiguration": {"connectorFunctionArn": self._ARN, "databaseName": "widgets"}}, item=item
        )
        assert status == 200
        assert mock_dao.update.call_args[0][1]["athenaDatabase"] == "widgets"

    def test_filters_can_be_updated_freely(self):
        status, _, mock_dao = self._update(
            {
                "customConnectorConfiguration": {
                    "connectorFunctionArn": self._ARN,
                    "databaseName": "widgets",
                    "tableFilter": "dim_*",
                }
            }
        )
        assert status == 200
        fields = mock_dao.update.call_args[0][1]
        assert json.loads(fields["configuration"])["tableFilter"] == "dim_*"
        # Unchanged database must not be rewritten needlessly.
        assert "athenaDatabase" not in fields

    # A Glue config on a CUSTOM_CONNECTOR row would leave the blob and the
    # sub-type disagreeing, and GET then 500s on GlueConfiguration's required
    # members rather than mislabelling anything.
    def test_rejects_a_mismatched_configuration_shape(self):
        status, body, mock_dao = self._update(
            {"glueConfiguration": {"catalogId": "123456789012", "region": "us-east-1", "databaseName": "x"}}
        )
        assert status == 400
        assert "customConnectorConfiguration" in body["error"]
        mock_dao.update.assert_not_called()


@pytest.mark.unit
class TestCreateGlueSourceOwnership:
    """F-8: catalogId/databaseName must be bound to the caller's namespace.

    ``manageSource`` authorizes against the NAMESPACE and the Smithy shape only
    checks the form of these two fields, so without this the create path is the
    entry point to reading any Glue database in the platform account.
    """

    def _create(self, req, *, allowed: bool):
        mock_dao = MagicMock()
        mock_scan_dao = MagicMock()
        error = _go.GlueOwnershipError("Glue database 'mydb' is not registered to namespace")
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
            patch(
                f"{_DR}.assert_namespace_may_catalog",
                side_effect=None if allowed else error,
            ) as mock_check,
        ):
            status, body = _parse(_dr._create_database_source(req, _NAMESPACE_ID))
        return status, body, mock_dao, mock_check

    def test_an_unowned_database_is_refused_with_403(self):
        status, body, mock_dao, _ = self._create(_make_glue_db_req(), allowed=False)
        assert status == 403
        assert "not registered to namespace" in body["error"]
        # Nothing may be stored: a source row is what the scan pipeline reads, so a
        # row written before the refusal would still get discovered.
        mock_dao.put.assert_not_called()

    def test_the_check_receives_the_caller_supplied_target(self):
        _, _, _, mock_check = self._create(_make_glue_db_req(), allowed=True)
        kwargs = mock_check.call_args.kwargs
        assert kwargs["namespace_id"] == _NAMESPACE_ID
        assert kwargs["catalog_id"] == "123456789012"
        assert kwargs["database_name"] == "mydb"

    def test_create_tolerates_a_database_that_does_not_exist_yet(self):
        """Create has never required the target to exist — it persists the config and
        the async scan reports a bad target. Refusing here would be a 403 telling the
        caller to tag something that isn't there. The pipeline stays strict."""
        _, _, _, mock_check = self._create(_make_glue_db_req(), allowed=True)
        assert mock_check.call_args.kwargs["allow_missing_database"] is True

    def test_an_owned_database_is_created(self):
        status, _, mock_dao, _ = self._create(_make_glue_db_req(), allowed=True)
        assert status == 202
        assert len(_source_puts(mock_dao)) == 1

    def test_jdbc_sources_are_not_subject_to_the_glue_check(self):
        """The check is about a caller-named Glue database. A JDBC source names a
        host and credentials it must already hold, and gets a catalog of its own."""
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
            patch(f"{_DR}.assert_namespace_may_catalog") as mock_check,
        ):
            status, _ = _parse(_dr._create_database_source(_make_jdbc_db_req(), _NAMESPACE_ID))
        assert status == 202
        mock_check.assert_not_called()

    def test_a_jdbc_source_claims_the_catalog_it_will_be_given(self):
        """The claim is what stops another namespace naming this catalog later."""
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _parse(_dr._create_database_source(_make_jdbc_db_req(), _NAMESPACE_ID))

        claims = [c.args[0] for c in mock_dao.put.call_args_list if c.args[0].get("SK") == "CLAIM"]
        assert len(claims) == 1
        assert claims[0]["namespaceId"] == _NAMESPACE_ID
        assert claims[0]["PK"].startswith("GLUECAT#")

    def test_a_glue_source_claims_nothing(self):
        """It is given no catalog, so there is nothing for it to own."""
        mock_dao = MagicMock()
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=MagicMock()),
        ):
            _parse(_dr._create_database_source(_make_glue_db_req(), _NAMESPACE_ID))

        claims = [c.args[0] for c in mock_dao.put.call_args_list if c.args[0].get("SK") == "CLAIM"]
        assert claims == []


class TestUpdateGlueConfiguration:
    """The stored blob is discovery's input, so a repoint here would bypass the
    ownership check the create path applied to the original target."""

    def _item(self, **config_overrides):
        config = {"catalogId": "123456789012", "region": "us-east-1", "databaseName": "mydb"}
        config.update(config_overrides)
        return {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceId": _SOURCE_ID,
            "sourceType": "DATABASE",
            "sourceSubType": "GLUE_DATABASE",
            "status": "APPROVED",
            "athenaDatabase": "mydb",
            "configuration": json.dumps(config),
            "updatedAt": "2026-01-01T00:00:00Z",
        }

    def _update(self, config, item=None):
        mock_dao = MagicMock()
        mock_dao.get.return_value = item or self._item()
        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            status, resp = _parse(
                _dr._handle_update_metadata(
                    {"body": json.dumps({"glueConfiguration": config})}, _NAMESPACE_ID, _SOURCE_ID
                )
            )
        return status, resp, mock_dao

    def test_rejects_a_repointed_database_name(self):
        status, body, mock_dao = self._update(
            {"catalogId": "123456789012", "region": "us-east-1", "databaseName": "someone_elses_db"}
        )
        assert status == 400
        assert "databaseName cannot be changed after creation" in body["error"]
        mock_dao.update.assert_not_called()

    def test_rejects_a_repointed_catalog_id(self):
        status, body, mock_dao = self._update(
            {"catalogId": "123456789012:scldevds_deadbeef", "region": "us-east-1", "databaseName": "mydb"}
        )
        assert status == 400
        assert "catalogId cannot be changed after creation" in body["error"]
        mock_dao.update.assert_not_called()

    def test_allows_an_unchanged_target(self):
        """Echoing the stored value must stay allowed, or another member of the blob
        becomes uneditable."""
        status, _, mock_dao = self._update(
            {
                "catalogId": "123456789012",
                "region": "us-east-1",
                "databaseName": "mydb",
                "tableExcludeFilter": "staging_*",
            }
        )
        assert status == 200
        mock_dao.update.assert_called_once()

    def test_falls_back_to_athena_database_for_a_row_with_no_stored_blob(self):
        """A row whose blob predates the field must not read as "changed" for a
        value it never disagreed with."""
        item = self._item()
        item["configuration"] = json.dumps({"catalogId": "123456789012", "region": "us-east-1"})
        status, body, _ = self._update(
            {"catalogId": "123456789012", "region": "us-east-1", "databaseName": "other"}, item=item
        )
        assert status == 400
        assert "databaseName cannot be changed" in body["error"]


class TestScanJobDegradationSignal:
    """A scan can SUCCEED while individual tables were unreadable. Unsurfaced, the
    result is indistinguishable from a complete one, so a reviewer would approve a
    silently incomplete ontology."""

    _SOURCE = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}", "sourceType": "DATABASE"}

    def _get(self, scan_item):
        mock_dao = MagicMock()
        mock_dao.get.return_value = dict(self._SOURCE)
        mock_scan_dao = MagicMock()
        mock_scan_dao.get.return_value = scan_item
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            return _parse(_dr._handle_get_scan_job(_NAMESPACE_ID, _SOURCE_ID, "2026-01-01T00:00:00Z"))

    def test_reports_unreadable_tables(self):
        status, body = self._get(
            {
                "status": "COMPLETED",
                "tablesDiscovered": 3,
                "tablesFailed": 2,
                "failedTables": ["widgets.bad", "widgets.worse"],
            }
        )
        assert status == 200
        assert body["tablesFailed"] == 2
        assert body["failedTables"] == ["widgets.bad", "widgets.worse"]

    # Decimal, not int, because that is what DynamoDB actually returns through
    # boto3's resource interface — and api_response serialises with
    # `default=str`, so an uncoerced Decimal ships as the JSON STRING "2". The
    # only consumer requires `typeof x === "number"`, so the whole degraded-scan
    # signal would be silently dropped. Mocking this with a native int (as the
    # test above does) cannot catch that.
    def test_numeric_fields_survive_json_as_numbers_not_strings(self):
        from decimal import Decimal

        status, body = self._get(
            {
                "status": "COMPLETED",
                "tablesDiscovered": Decimal("40"),
                "columnsDiscovered": Decimal("312"),
                "tablesFailed": Decimal("2"),
                "failedTables": ["widgets.bad", "widgets.worse"],
            }
        )
        assert status == 200
        # `body` is the round-tripped JSON, so these assertions cover serialisation.
        for field, expected in (
            ("tablesFailed", 2),
            ("tablesDiscovered", 40),
            ("columnsDiscovered", 312),
        ):
            assert body[field] == expected, field
            assert isinstance(body[field], int), f"{field} shipped as {type(body[field]).__name__}"
            assert not isinstance(body[field], str), f"{field} shipped as a string"

    # Absent rather than zero, so the field can be used directly to filter for
    # degraded scans — and so a clean scan's response is byte-for-byte unchanged.
    def test_a_clean_scan_omits_the_fields(self):
        _, body = self._get({"status": "COMPLETED", "tablesDiscovered": 3})
        assert "tablesFailed" not in body
        assert "failedTables" not in body


@pytest.mark.unit
class TestHandleUpdateMetadata:
    def _db_item(self):
        return {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceId": _SOURCE_ID,
            "sourceType": "DATABASE",
            "status": "APPROVED",
            "updatedAt": "2026-01-01T00:00:00Z",
        }

    def test_update_name_happy_path(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"name": "new-name"})}
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        mock_dao.update.assert_called_once()

    def test_update_metadata_enrichment_enabled(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"metadataEnrichmentEnabled": False})}
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        update_kwargs = mock_dao.update.call_args
        # update() may be called positionally or with kwargs — pull the
        # update_fields dict regardless of call style.
        args, kwargs = update_kwargs.args, update_kwargs.kwargs
        update_fields = args[1] if len(args) >= 2 else kwargs["update_fields"]
        assert update_fields["metadataEnrichmentEnabled"] is False

    def test_update_glue_config(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {
                "body": json.dumps(
                    {
                        "glueConfiguration": {
                            "catalogId": "123456789012",
                            "region": "us-east-1",
                            "databaseName": "newdb",
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200

    def test_update_jdbc_config(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {
                "body": json.dumps(
                    {
                        "jdbcConfiguration": {
                            "engine": "POSTGRESQL",
                            "host": "db.example.com",
                            "port": 5432,
                            "databaseName": "mydb",
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200

    def test_update_jdbc_config_invokes_binding_validation(self):
        # The credential-secret → namespace binding must run on the update path,
        # not only at create — otherwise a source could be repointed at a secret
        # bound to another namespace after creation.
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()
        spy = MagicMock(return_value=None)
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._validate_credential_secret_binding", spy),
        ):
            event = {
                "body": json.dumps(
                    {
                        "jdbcConfiguration": {
                            "engine": "POSTGRESQL",
                            "host": "db.example.com",
                            "port": 5432,
                            "databaseName": "mydb",
                            "credentialSecretArn": ("arn:aws:secretsmanager:us-east-1:111122223333:secret:s-AbCdEf"),
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 200
        spy.assert_called_once()

    def test_update_jdbc_config_rejected_when_binding_fails(self):
        # A binding failure blocks the update and must not write to DynamoDB.
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()
        from coa_common.response import api_response

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(
                f"{_DR}._validate_credential_secret_binding",
                return_value=api_response(400, {"error": "not bound"}),
            ),
        ):
            event = {
                "body": json.dumps(
                    {
                        "jdbcConfiguration": {
                            "engine": "POSTGRESQL",
                            "host": "db.example.com",
                            "port": 5432,
                            "databaseName": "mydb",
                            "credentialSecretArn": (
                                "arn:aws:secretsmanager:us-east-1:111122223333:secret:other-XyZ123"
                            ),
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400
        mock_dao.update.assert_not_called()

    def test_update_cannot_repoint_jdbc_host(self):
        """host is immutable: it decides which server receives the credentials.

        The credential secret is bound to the NAMESPACE, not to whoever edits the
        source, so without this `manageSource` on a namespace is enough to redirect
        a source another steward registered at a host the editor controls and have
        discovery deliver that namespace's database credentials to it. The binding
        check still passes there — the secret is unchanged; the destination moved.
        """
        mock_dao = MagicMock()
        item = self._db_item()
        item["sourceSubType"] = "JDBC_DATABASE"
        item["configuration"] = json.dumps(
            {
                "engine": "POSTGRESQL",
                "host": "prod-db.internal",
                "port": 5432,
                "databaseName": "mydb",
                "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:s-AbCdEf",
            }
        )
        mock_dao.get.return_value = item
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._validate_credential_secret_binding", return_value=None),
        ):
            event = {
                "body": json.dumps(
                    {
                        "jdbcConfiguration": {
                            "engine": "POSTGRESQL",
                            "host": "attacker.example.com",
                            "port": 5432,
                            "databaseName": "mydb",
                            "credentialSecretArn": ("arn:aws:secretsmanager:us-east-1:111122223333:secret:s-AbCdEf"),
                        }
                    }
                )
            }
            status, body = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400
        assert "host" in body["error"]
        mock_dao.update.assert_not_called()

    def test_update_cannot_swap_jdbc_credential_secret(self):
        # Swapping the secret is caught here as well as by the binding check, so a
        # secret that happens to be tagged for this namespace still cannot be
        # substituted for the one the source was registered with.
        mock_dao = MagicMock()
        item = self._db_item()
        item["sourceSubType"] = "JDBC_DATABASE"
        item["configuration"] = json.dumps(
            {
                "engine": "POSTGRESQL",
                "host": "prod-db.internal",
                "port": 5432,
                "databaseName": "mydb",
                "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:s-AbCdEf",
            }
        )
        mock_dao.get.return_value = item
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._validate_credential_secret_binding", return_value=None),
        ):
            event = {
                "body": json.dumps(
                    {
                        "jdbcConfiguration": {
                            "engine": "POSTGRESQL",
                            "host": "prod-db.internal",
                            "port": 5432,
                            "databaseName": "mydb",
                            "credentialSecretArn": (
                                "arn:aws:secretsmanager:us-east-1:111122223333:secret:elevated-XyZ123"
                            ),
                        }
                    }
                )
            }
            status, body = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400
        assert "credentialSecretArn" in body["error"]
        mock_dao.update.assert_not_called()

    def test_update_jdbc_may_echo_immutable_fields_to_edit_a_filter(self):
        # Rejecting only CHANGES keeps a whole-blob PUT usable: the client resends
        # host/port/secret unchanged in order to edit schemaFilter.
        mock_dao = MagicMock()
        item = self._db_item()
        item["sourceSubType"] = "JDBC_DATABASE"
        item["configuration"] = json.dumps(
            {
                "engine": "POSTGRESQL",
                "host": "prod-db.internal",
                "port": 5432,
                "databaseName": "mydb",
                "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:s-AbCdEf",
            }
        )
        mock_dao.get.return_value = item
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._validate_credential_secret_binding", return_value=None),
        ):
            event = {
                "body": json.dumps(
                    {
                        "jdbcConfiguration": {
                            "engine": "POSTGRESQL",
                            "host": "prod-db.internal",
                            "port": 5432,
                            "databaseName": "mydb",
                            "credentialSecretArn": ("arn:aws:secretsmanager:us-east-1:111122223333:secret:s-AbCdEf"),
                            "schemaFilter": "^public$",
                        }
                    }
                )
            }
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 200
        mock_dao.update.assert_called_once()

    def test_update_both_configs_returns_400(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"glueConfiguration": {}, "jdbcConfiguration": {}})}
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 400

    def test_update_empty_body_returns_400(self):
        status, _ = _parse(_dr._handle_update_metadata({"body": "{}"}, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400

    def test_update_invalid_json_returns_400(self):
        status, _ = _parse(_dr._handle_update_metadata({"body": "not-json"}, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400

    def test_update_source_not_found_returns_404(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = None

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"name": "new-name"})}
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 404

    def test_update_non_database_source_returns_400(self):
        mock_dao = MagicMock()
        item = self._db_item()
        item["sourceType"] = "DOCUMENTS"
        mock_dao.get.return_value = item

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"name": "new-name"})}
            status, body = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 400
        assert "DATABASE" in body["error"]

    def test_update_blank_name_returns_400(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"name": "   "})}
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 400

    def test_update_ddb_error_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()
        mock_dao.update.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "UpdateItem")

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"name": "new-name"})}
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 500

    def test_update_no_updatable_fields_returns_400(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._db_item()

        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            event = {"body": json.dumps({"name": None})}
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, _SOURCE_ID))

        assert status == 400


# ===================================================================
# _handle_list_tables
# ===================================================================


@pytest.mark.unit
class TestHandleListTables:
    def _make_list_event(self, qs=None):
        return {"httpMethod": "GET", "queryStringParameters": qs or {}}

    def test_list_tables_no_smus_domain_returns_500(self):
        with patch(f"{_DR}._SMUS_DOMAIN_ID", ""):
            status, _ = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))
        assert status == 500

    def test_list_tables_namespace_not_found_returns_404(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = None

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
        ):
            status, _ = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 404

    def test_list_tables_invalid_max_results_returns_400(self):
        with patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"):
            status, _ = _parse(
                _dr._handle_list_tables(self._make_list_event({"maxResults": "abc"}), _NAMESPACE_ID, _SOURCE_ID)
            )
        assert status == 400

    def test_list_tables_invalid_review_status_returns_400(self):
        with patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"):
            status, _ = _parse(
                _dr._handle_list_tables(
                    self._make_list_event({"reviewStatus": "INVALID_STATUS"}), _NAMESPACE_ID, _SOURCE_ID
                )
            )
        assert status == 400

    def test_list_tables_happy_path(self):
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        forms_list = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {
                        "databaseName": "mydb",
                        "tableName": "mytable",
                        "columnCount": 3,
                        "reviewStatus": "PENDING_REVIEW",
                    }
                ),
            }
        ]

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"
        mock_asset.forms_output = forms_list

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "mytable"

    def test_list_tables_column_counts_honest_denominator_and_pending_deletion(self):
        """columnCount reflects ALL merged columns (incl. a retained
        pending-deletion column), a column slated for deletion is not counted
        approved, and the per-table pending-deletion column count is surfaced."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = MagicMock(items=[mock_asset], next_token=None)
        mock_smus.get_asset_forms.return_value = {
            "formsOutput": [
                {
                    "formName": FORM_TYPE_NAME,
                    "content": json.dumps(
                        {
                            "databaseName": "mydb",
                            "tableName": "mytable",
                            # Stored scalar is intentionally wrong (2) to prove the
                            # handler counts the merged columns list, not the scalar.
                            "columnCount": 2,
                            "reviewStatus": "PENDING_REVIEW",
                            "columns": [
                                {"name": "kept_approved", "business_metadata": {"review_status": "APPROVED"}},
                                {"name": "pending_col", "business_metadata": {"review_status": "PENDING_REVIEW"}},
                                {"name": "removed_approved", "business_metadata": {"review_status": "APPROVED"}},
                            ],
                        }
                    ),
                }
            ]
        }

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            # A re-scan retained an APPROVED column tagged pending-deletion.
            patch(f"{_DR}._removed_sets", return_value=(set(), {"mydb.mytable": {"removed_approved"}}, set())),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        item = body["items"][0]
        # 3 merged columns retained; the removed-but-APPROVED column is not
        # counted approved; the retained removed column is surfaced.
        assert item["columnCount"] == 3
        assert item["columnsApproved"] == 1
        assert item["columnsPendingDeletion"] == 1

    def test_list_tables_get_asset_forms_failure_logs_warning_and_marks_degraded(self):
        """When inline forms are absent and the get_asset_forms fallback raises,
        the asset is skipped with a warning log and the response includes
        a skippedAssets count."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset_ok = MagicMock()
        mock_asset_ok.name = f"DS#{_SOURCE_ID}:mydb.good_table"
        mock_asset_ok.asset_id = "asset-ok"
        mock_asset_ok.forms_output = None

        mock_asset_bad = MagicMock()
        mock_asset_bad.name = f"DS#{_SOURCE_ID}:mydb.bad_table"
        mock_asset_bad.asset_id = "asset-bad"
        mock_asset_bad.forms_output = None

        mock_result = MagicMock()
        mock_result.items = [mock_asset_bad, mock_asset_ok]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        def _get_forms(asset_id):
            if asset_id == "asset-bad":
                raise RuntimeError("DataZone unavailable")
            return {
                "formsOutput": [
                    {
                        "formName": FORM_TYPE_NAME,
                        "content": json.dumps(
                            {
                                "databaseName": "mydb",
                                "tableName": "good_table",
                                "columnCount": 2,
                                "reviewStatus": "PENDING_REVIEW",
                            }
                        ),
                    }
                ]
            }

        mock_smus.get_asset_forms.side_effect = _get_forms

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}.logger") as mock_logger,
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        # Only the good table is returned
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "good_table"
        # Skipped assets indicator present
        assert body["skippedAssets"] == 1
        # Warning was logged with asset context
        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args
        assert call_kwargs[0][0] == "list_tables_asset_forms_failed"
        assert call_kwargs[1]["source_id"] == _SOURCE_ID
        assert call_kwargs[1]["asset_id"] == "asset-bad"
        assert call_kwargs[1]["asset_name"] == f"DS#{_SOURCE_ID}:mydb.bad_table"

    def test_list_tables_malformed_form_json_logs_warning_and_marks_degraded(self):
        """When form JSON is unparseable, the asset is skipped with a warning
        and the response includes a 'degraded' indicator."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.corrupt_table"
        mock_asset.asset_id = "asset-corrupt"
        mock_asset.forms_output = [
            {
                "formName": FORM_TYPE_NAME,
                "content": "NOT VALID JSON {{{",
            }
        ]

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}.logger") as mock_logger,
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 0
        assert body["skippedAssets"] == 1
        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args
        assert call_kwargs[0][0] == "list_tables_form_parse_failed"
        assert call_kwargs[1]["source_id"] == _SOURCE_ID
        assert call_kwargs[1]["asset_id"] == "asset-corrupt"
        assert call_kwargs[1]["asset_name"] == f"DS#{_SOURCE_ID}:mydb.corrupt_table"
        assert call_kwargs[1]["form_name"] == FORM_TYPE_NAME

    def test_list_tables_multiple_failures_accumulates_skipped_count(self):
        """When multiple assets fail (both API and parse errors),
        skippedAssets accurately reflects the total count."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        # Asset 1: inline forms absent, fallback get_asset_forms will raise
        mock_asset_api_fail = MagicMock()
        mock_asset_api_fail.name = f"DS#{_SOURCE_ID}:mydb.api_fail"
        mock_asset_api_fail.asset_id = "asset-api-fail"
        mock_asset_api_fail.forms_output = None

        # Asset 2: inline form content will be malformed JSON
        mock_asset_parse_fail = MagicMock()
        mock_asset_parse_fail.name = f"DS#{_SOURCE_ID}:mydb.parse_fail"
        mock_asset_parse_fail.asset_id = "asset-parse-fail"
        mock_asset_parse_fail.forms_output = [{"formName": FORM_TYPE_NAME, "content": "{{{INVALID"}]

        # Asset 3: inline forms populated with good data
        mock_asset_ok = MagicMock()
        mock_asset_ok.name = f"DS#{_SOURCE_ID}:mydb.good_table"
        mock_asset_ok.asset_id = "asset-ok"
        mock_asset_ok.forms_output = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {
                        "databaseName": "mydb",
                        "tableName": "good_table",
                        "columnCount": 1,
                        "reviewStatus": "PENDING_REVIEW",
                    }
                ),
            }
        ]

        mock_result = MagicMock()
        mock_result.items = [mock_asset_api_fail, mock_asset_parse_fail, mock_asset_ok]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result
        # Only asset-api-fail triggers fallback (forms_output=None);
        # it raises, so it becomes a skipped asset.
        mock_smus.get_asset_forms.side_effect = RuntimeError("DataZone unavailable")

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "good_table"
        assert body["skippedAssets"] == 2

    def test_list_tables_malformed_columns_field_skips_gracefully(self):
        """When the columns field within a valid form payload contains malformed
        JSON, the asset is skipped and counted in skippedAssets."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.bad_cols"
        mock_asset.asset_id = "asset-bad-cols"
        mock_asset.forms_output = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {
                        "databaseName": "mydb",
                        "tableName": "bad_cols",
                        "columnCount": 3,
                        "reviewStatus": "PENDING_REVIEW",
                        "columns": "NOT VALID JSON [[[",
                    }
                ),
            }
        ]

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}.logger") as mock_logger,
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 0
        assert body["skippedAssets"] == 1
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "list_tables_columns_parse_failed"
        assert mock_logger.warning.call_args[1]["asset_id"] == "asset-bad-cols"
        assert mock_logger.warning.call_args[1]["table_id"] == "mydb.bad_cols"

    def test_list_tables_no_failures_omits_degraded_field(self):
        """When all assets parse successfully, the response does not include
        'degraded' or 'skippedAssets' fields."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        forms_list = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {
                        "databaseName": "mydb",
                        "tableName": "mytable",
                        "columnCount": 3,
                        "reviewStatus": "PENDING_REVIEW",
                    }
                ),
            }
        ]

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"
        mock_asset.forms_output = forms_list

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert "degraded" not in body
        assert "skippedAssets" not in body

    def test_list_tables_search_fails_returns_500(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_smus = MagicMock()
        mock_smus.search_assets.side_effect = Exception("search failed")

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, _ = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 500

    def test_list_tables_with_next_token(self):
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        forms_list = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {
                        "databaseName": "mydb",
                        "tableName": "mytable",
                        "columnCount": 1,
                        "reviewStatus": "PENDING_REVIEW",
                    }
                ),
            }
        ]

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"
        mock_asset.forms_output = forms_list

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = "next-page-token"

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert body.get("nextToken") == "next-page-token"

    def test_list_tables_filters_by_review_status(self):
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"
        mock_asset.forms_output = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {"databaseName": "mydb", "tableName": "mytable", "columnCount": 1, "reviewStatus": "APPROVED"}
                ),
            }
        ]

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            # Filter for PENDING_REVIEW — the table is APPROVED, so it should be excluded
            status, body = _parse(
                _dr._handle_list_tables(
                    self._make_list_event({"reviewStatus": "PENDING_REVIEW"}), _NAMESPACE_ID, _SOURCE_ID
                )
            )

        assert status == 200
        assert len(body["items"]) == 0

    def test_list_tables_inline_forms_skips_get_asset_forms(self):
        """Canary: when search returns populated forms_output, get_asset_forms
        is NOT called — this proves the N+1 is gone."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        forms_list = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {
                        "databaseName": "mydb",
                        "tableName": "mytable",
                        "columnCount": 5,
                        "reviewStatus": "PENDING_REVIEW",
                    }
                ),
            }
        ]

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"
        mock_asset.forms_output = forms_list

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "mytable"
        mock_smus.get_asset_forms.assert_not_called()

    def test_list_tables_inline_forms_still_apply_rescan_enrichment(self):
        """Inline forms and re-scan enrichment must both hold AT ONCE.

        Regression guard for the rebase of GH-133 onto the re-scan work. The two
        features were written independently and each side's tests only exercise
        its own path: the re-scan assertions elsewhere in this file all reach the
        payload through the ``get_asset_forms`` fallback (their mock assets have
        no ``forms_output``), while the inline-forms tests all use payloads with
        no re-scan state. Removing the per-form inner loop de-indented the whole
        ``TableSummary`` construction by one level, so a bad merge could quietly
        drop the re-scan fields from the inline path — the common path in
        production — while every existing test still passed.
        """
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        def _asset(asset_id: str, table: str, payload: dict) -> MagicMock:
            asset = MagicMock()
            asset.name = f"DS#{_SOURCE_ID}:{table}"
            asset.asset_id = asset_id
            asset.forms_output = [{"formName": FORM_TYPE_NAME, "content": json.dumps(payload)}]
            return asset

        # Dropped a column; status was reset to PENDING_REVIEW by the merge.
        shrunk = _asset(
            "asset-shrunk",
            "mydb.shrunk",
            {
                "databaseName": "mydb",
                "tableName": "shrunk",
                "columnCount": 2,  # deliberately stale scalar
                "reviewStatus": "PENDING_REVIEW",
                "columns": [
                    {"name": "kept", "business_metadata": {"review_status": "APPROVED"}},
                    {"name": "gone", "business_metadata": {"review_status": "APPROVED"}},
                ],
            },
        )
        # Dropped wholesale, still carrying its old APPROVED status.
        gone = _asset(
            "asset-gone",
            "mydb.gone",
            {"databaseName": "mydb", "tableName": "gone", "reviewStatus": "APPROVED", "columns": []},
        )
        # Brand new in this re-scan.
        fresh = _asset(
            "asset-fresh",
            "mydb.fresh",
            {"databaseName": "mydb", "tableName": "fresh", "reviewStatus": "PENDING_REVIEW", "columns": []},
        )

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = MagicMock(items=[shrunk, gone, fresh], next_token=None)

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(
                f"{_DR}._removed_sets",
                return_value=({"mydb.gone"}, {"mydb.shrunk": {"gone"}}, {"mydb.fresh"}),
            ),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        items = {i["name"]: i for i in body["items"]}
        assert set(items) == {"shrunk", "gone", "fresh"}

        # Honest denominator from the merged list, not the stale scalar; the
        # APPROVED-but-doomed column is neither approved nor hidden.
        assert items["shrunk"]["columnCount"] == 2
        assert items["shrunk"]["columnsApproved"] == 1
        assert items["shrunk"]["columnsPendingDeletion"] == 1
        assert items["shrunk"].get("pendingDeletion") is None

        # A wholly-removed table reports PENDING_REVIEW despite its stored
        # APPROVED, so it shows up in the steward's pending queue.
        assert items["gone"]["reviewStatus"] == "PENDING_REVIEW"
        assert items["gone"]["pendingDeletion"] is True

        assert items["fresh"]["added"] is True
        assert items["fresh"].get("pendingDeletion") is None

        # All of the above came from inline forms — the N+1 stays gone.
        mock_smus.get_asset_forms.assert_not_called()

    def test_list_tables_fallback_when_forms_output_absent(self):
        """When search returns absent forms_output, the fallback calls
        get_asset_forms and the table is still listed correctly."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.fb_table"
        mock_asset.asset_id = "asset-fb"
        mock_asset.forms_output = None

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result
        mock_smus.get_asset_forms.return_value = {
            "formsOutput": [
                {
                    "formName": FORM_TYPE_NAME,
                    "content": json.dumps(
                        {
                            "databaseName": "mydb",
                            "tableName": "fb_table",
                            "columnCount": 2,
                            "reviewStatus": "PENDING_REVIEW",
                        }
                    ),
                }
            ]
        }

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "fb_table"
        mock_smus.get_asset_forms.assert_called_once_with(asset_id="asset-fb")

    def test_list_tables_fallback_when_forms_output_empty_list(self):
        """When search returns an empty forms_output list, the fallback fires."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.empty_table"
        mock_asset.asset_id = "asset-empty"
        mock_asset.forms_output = []

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result
        mock_smus.get_asset_forms.return_value = {
            "formsOutput": [
                {
                    "formName": FORM_TYPE_NAME,
                    "content": json.dumps(
                        {
                            "databaseName": "mydb",
                            "tableName": "empty_table",
                            "columnCount": 1,
                            "reviewStatus": "PENDING_REVIEW",
                        }
                    ),
                }
            ]
        }

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "empty_table"
        mock_smus.get_asset_forms.assert_called_once_with(asset_id="asset-empty")

    def test_list_tables_fallback_when_inline_form_content_is_null(self):
        """A NON-EMPTY forms_output whose SCL form has ``content: null`` must take
        the per-asset fallback, not the inline parse.

        Regression: the emptiness probe was at list level, so a populated
        formsOutput with an unpopulated ``content`` counted as usable and reached
        ``json.loads(form["content"])`` -> TypeError -> 500 for the WHOLE page.
        The MR's stated contract is worst-case-today's-behaviour, i.e. fall back."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.null_content"
        mock_asset.asset_id = "asset-null"
        mock_asset.forms_output = [{"formName": FORM_TYPE_NAME, "content": None}]

        mock_result = MagicMock()
        mock_result.items = [mock_asset]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result
        mock_smus.get_asset_forms.return_value = {
            "formsOutput": [
                {
                    "formName": FORM_TYPE_NAME,
                    "content": json.dumps(
                        {
                            "databaseName": "mydb",
                            "tableName": "null_content",
                            "columnCount": 3,
                            "reviewStatus": "PENDING_REVIEW",
                        }
                    ),
                }
            ]
        }

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "null_content"
        mock_smus.get_asset_forms.assert_called_once_with(asset_id="asset-null")

    def test_list_tables_absent_content_key_skips_the_asset_not_the_page(self):
        """A form dict with NO ``content`` key, whose fallback is also unusable,
        degrades to a per-asset skip — never a KeyError escaping to a 500."""
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_bad = MagicMock()
        mock_bad.name = f"DS#{_SOURCE_ID}:mydb.no_content_key"
        mock_bad.asset_id = "asset-nokey"
        mock_bad.forms_output = [{"formName": FORM_TYPE_NAME}]

        mock_ok = MagicMock()
        mock_ok.name = f"DS#{_SOURCE_ID}:mydb.good_table"
        mock_ok.asset_id = "asset-ok"
        mock_ok.forms_output = [
            {
                "formName": FORM_TYPE_NAME,
                "content": json.dumps(
                    {
                        "databaseName": "mydb",
                        "tableName": "good_table",
                        "columnCount": 4,
                        "reviewStatus": "PENDING_REVIEW",
                    }
                ),
            }
        ]

        mock_result = MagicMock()
        mock_result.items = [mock_bad, mock_ok]
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result
        # Fallback cannot help either: the form comes back with empty content.
        mock_smus.get_asset_forms.return_value = {"formsOutput": [{"formName": FORM_TYPE_NAME, "content": ""}]}

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        # The healthy sibling asset still lists; only the broken one is skipped.
        assert status == 200
        assert [i["name"] for i in body["items"]] == ["good_table"]
        assert body["skippedAssets"] == 1
        mock_smus.get_asset_forms.assert_called_once_with(asset_id="asset-nokey")

    def test_list_tables_passes_include_forms_true(self):
        """Verify search_assets is called with include_forms=True."""
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_result = MagicMock()
        mock_result.items = []
        mock_result.next_token = None

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            _dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID)

        _, kwargs = mock_smus.search_assets.call_args
        assert kwargs["include_forms"] is True


# ===================================================================
# _handle_get_table
# ===================================================================


@pytest.mark.unit
class TestHandleGetTable:
    def test_get_table_no_smus_domain_returns_500(self):
        with patch(f"{_DR}._SMUS_DOMAIN_ID", ""):
            status, _ = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "mydb.mytable"))
        assert status == 500

    def test_get_table_returns_column_confidence(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(
            mock_smus,
            table_id="sales.orders",
            form_content=_serialize_table(columns=[{"name": "col_a", "description": "An id", "confidence": 0.88}]),
        )
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, body = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "sales.orders"))
        assert status == 200
        assert body["columns"][0]["businessMetadata"]["confidence"] == 0.88

    def test_get_table_namespace_not_found_returns_404(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = None

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
        ):
            status, _ = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "mydb.mytable"))

        assert status == 404

    def test_get_table_asset_not_found_returns_404(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_result = MagicMock()
        mock_result.items = []

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, _ = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "mydb.mytable"))

        assert status == 404

    def test_get_table_search_fails_returns_500(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_smus = MagicMock()
        mock_smus.find_asset_by_name.side_effect = Exception("search error")

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, _ = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "mydb.mytable"))

        assert status == 500

    def test_get_table_forms_fetch_fails_returns_500(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"

        mock_result = MagicMock()
        mock_result.items = [mock_asset]

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = mock_result
        mock_smus.get_asset_forms.side_effect = Exception("forms error")

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        ):
            status, _ = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "mydb.mytable"))

        assert status == 500


# ===================================================================
# pendingDeletion surfacing (re-scan removals)
# ===================================================================


class TestPendingDeletionSurfacing:
    def _make_list_event(self, qs=None):
        return {"httpMethod": "GET", "queryStringParameters": qs or {}}

    def test_list_tables_flags_removed_tables(self):
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        def _asset(tid):
            a = MagicMock()
            a.name = f"DS#{_SOURCE_ID}:{tid}"
            a.asset_id = f"asset-{tid}"
            return a

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = MagicMock(
            items=[_asset("mydb.gone"), _asset("mydb.keep")], next_token=None
        )

        def _forms(asset_id):
            db, name = asset_id.removeprefix("asset-").split(".")
            return {
                "formsOutput": [
                    {
                        "formName": FORM_TYPE_NAME,
                        "content": json.dumps(
                            {"databaseName": db, "tableName": name, "columnCount": 1, "reviewStatus": "APPROVED"}
                        ),
                    }
                ]
            }

        mock_smus.get_asset_forms.side_effect = _forms
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._removed_sets", return_value=({"mydb.gone"}, {}, set())),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        flags = {i["name"]: i.get("pendingDeletion") for i in body["items"]}
        assert flags["gone"] is True
        assert not flags.get("keep")
        # A removed table (stored APPROVED) reads as PENDING_REVIEW so it shows in
        # the review filter/count; a kept table keeps its stored status.
        statuses = {i["name"]: i["reviewStatus"] for i in body["items"]}
        assert statuses["gone"] == "PENDING_REVIEW"
        assert statuses["keep"] == "APPROVED"

    def test_get_table_flags_removed_table_and_columns(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(
            mock_smus,
            table_id="sales.orders",
            form_content=_serialize_table(
                table_name="orders",
                database="sales",
                table_status="APPROVED",
                columns=[
                    {"name": "keep_col", "review_status": "APPROVED"},
                    {"name": "gone_col", "review_status": "APPROVED"},
                ],
            ),
        )
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._removed_sets", return_value=({"sales.orders"}, {"sales.orders": {"gone_col"}}, set())),
        ):
            status, body = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "sales.orders"))

        assert status == 200
        assert body["pendingDeletion"] is True
        # The removed table (stored APPROVED) reads as PENDING_REVIEW.
        assert body["reviewStatus"] == "PENDING_REVIEW"
        col_flags = {c["name"]: c.get("pendingDeletion") for c in body["columns"]}
        assert col_flags["gone_col"] is True
        assert not col_flags.get("keep_col")
        # The whole table is dropped, so EVERY column reads PENDING_REVIEW — a
        # dropped table is never shown as still-Approved, columns included. The
        # per-column pendingDeletion badge still only flags the individually
        # removed column (gone_col), not keep_col.
        col_status = {c["name"]: c["businessMetadata"]["reviewStatus"] for c in body["columns"]}
        assert col_status["gone_col"] == "PENDING_REVIEW"
        assert col_status["keep_col"] == "PENDING_REVIEW"

    def test_get_table_column_only_removal_does_not_flip_other_columns(self):
        # Table is NOT dropped; only one column is removed. That column reads
        # PENDING_REVIEW; the other keeps its APPROVED status.
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(
            mock_smus,
            table_id="sales.orders",
            form_content=_serialize_table(
                table_name="orders",
                database="sales",
                table_status="APPROVED",
                columns=[
                    {"name": "keep_col", "review_status": "APPROVED"},
                    {"name": "gone_col", "review_status": "APPROVED"},
                ],
            ),
        )
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._removed_sets", return_value=(set(), {"sales.orders": {"gone_col"}}, set())),
        ):
            status, body = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "sales.orders"))

        assert status == 200
        assert body["pendingDeletion"] is None  # table itself not dropped
        col_status = {c["name"]: c["businessMetadata"]["reviewStatus"] for c in body["columns"]}
        assert col_status["gone_col"] == "PENDING_REVIEW"
        assert col_status["keep_col"] == "APPROVED"

    def test_get_table_flags_added_table(self):
        # A table the re-scan discovered for the first time carries added=True
        # (and no pendingDeletion) — the UI shows a "new table" note.
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(
            mock_smus,
            table_id="sales.fresh",
            form_content=_serialize_table(
                table_name="fresh",
                database="sales",
                table_status="PENDING_REVIEW",
                columns=[{"name": "c1", "review_status": "PENDING_REVIEW"}],
            ),
        )
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._removed_sets", return_value=(set(), {}, {"sales.fresh"})),
        ):
            status, body = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "sales.fresh"))

        assert status == 200
        assert body["added"] is True
        assert body["pendingDeletion"] is None

    def test_get_table_no_added_flag_when_not_in_added_set(self):
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(
            mock_smus,
            table_id="sales.existing",
            form_content=_serialize_table(
                table_name="existing",
                database="sales",
                table_status="APPROVED",
                columns=[{"name": "c1", "review_status": "APPROVED"}],
            ),
        )
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._removed_sets", return_value=(set(), {}, {"sales.other"})),
        ):
            status, body = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, "sales.existing"))

        assert status == 200
        assert body.get("added") is None

    def test_effective_review_status_pending_deletion_flips_to_pending(self):
        assert _dr._effective_review_status("APPROVED", pending_deletion=True) == "PENDING_REVIEW"

    def test_effective_review_status_preserves_rejected(self):
        # A rejected item stays rejected even when pending deletion (mirrors the
        # re-scan merge's status rules).
        assert _dr._effective_review_status("REJECTED", pending_deletion=True) == "REJECTED"

    def test_effective_review_status_passthrough_when_not_pending(self):
        assert _dr._effective_review_status("APPROVED", pending_deletion=False) == "APPROVED"
        assert _dr._effective_review_status(None, pending_deletion=False) is None

    def test_removed_sets_empty_unless_rescan_review(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVED"}
        with (
            patch(f"{_DR}._BUCKET_NAME", "bucket"),
            patch(f"{_DR}._get_dao", return_value=mock_dao),
        ):
            assert _dr._removed_sets(_NAMESPACE_ID, _SOURCE_ID) == (set(), {}, set())

    def test_removed_sets_reads_backup_in_rescan_review(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "RESCAN_REVIEW"}
        blob = json.dumps(
            {
                "removed_tables": ["db.gone"],
                "removed_columns": {"db.mod": ["c1"]},
                "added_tables": ["db.fresh"],
            }
        ).encode()
        with (
            patch(f"{_DR}._BUCKET_NAME", "bucket"),
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}.get_s3_client", return_value=MagicMock()),
            patch(f"{_DR}.read_file_bytes", return_value=blob),
        ):
            removed_tables, removed_columns, added_tables = _dr._removed_sets(_NAMESPACE_ID, _SOURCE_ID)
        assert removed_tables == {"db.gone"}
        assert removed_columns == {"db.mod": {"c1"}}
        assert added_tables == {"db.fresh"}

    def _rescan_review_dao(self):
        dao = MagicMock()
        dao.get.return_value = {"status": "RESCAN_REVIEW"}
        return dao

    def _patched_read(self, error: Exception):
        return (
            patch(f"{_DR}._BUCKET_NAME", "bucket"),
            patch(f"{_DR}._get_dao", return_value=self._rescan_review_dao()),
            patch(f"{_DR}.get_s3_client", return_value=MagicMock()),
            patch(f"{_DR}.read_file_bytes", MagicMock(side_effect=error)),
        )

    def test_removed_sets_empty_when_backup_absent(self):
        """No backup object means no open re-scan — genuinely nothing flagged."""
        absent = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        a, b, c, d = self._patched_read(absent)
        with a, b, c, d:
            assert _dr._removed_sets(_NAMESPACE_ID, _SOURCE_ID) == (set(), {}, set())

    def test_removed_sets_propagates_real_s3_error(self):
        """A failed read must NOT look like "nothing pending deletion".

        Reporting empty here hid pending deletions from the review page while
        approve still deleted them, so a steward could approve removals they
        were never shown. Fail loudly instead.
        """
        for code in ("ThrottlingException", "AccessDenied", "InternalError"):
            failure = ClientError({"Error": {"Code": code}}, "GetObject")
            a, b, c, d = self._patched_read(failure)
            with a, b, c, d, pytest.raises(ClientError):
                _dr._removed_sets(_NAMESPACE_ID, _SOURCE_ID)

    def test_rescan_table_diff_none_when_backup_absent(self):
        absent = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        a, b, c, d = self._patched_read(absent)
        with a, b, c, d:
            assert _dr._rescan_table_diff(_NAMESPACE_ID, _SOURCE_ID, "db.t", MagicMock()) is None

    def test_rescan_table_diff_propagates_real_s3_error(self):
        """An S3 fault must not render as "no changes" in the diff panel."""
        failure = ClientError({"Error": {"Code": "ThrottlingException"}}, "GetObject")
        a, b, c, d = self._patched_read(failure)
        with a, b, c, d, pytest.raises(ClientError):
            _dr._rescan_table_diff(_NAMESPACE_ID, _SOURCE_ID, "db.t", MagicMock())

    def test_list_tables_flags_added_tables(self):
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        def _asset(tid):
            a = MagicMock()
            a.name = f"DS#{_SOURCE_ID}:{tid}"
            a.asset_id = f"asset-{tid}"
            return a

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = MagicMock(
            items=[_asset("mydb.fresh"), _asset("mydb.same")], next_token=None
        )

        def _forms(asset_id):
            db, name = asset_id.removeprefix("asset-").split(".")
            return {
                "formsOutput": [
                    {
                        "formName": FORM_TYPE_NAME,
                        "content": json.dumps(
                            {"databaseName": db, "tableName": name, "columnCount": 1, "reviewStatus": "PENDING_REVIEW"}
                        ),
                    }
                ]
            }

        mock_smus.get_asset_forms.side_effect = _forms
        # A net-new table (mydb.fresh) is flagged; a modified/unchanged one is not.
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._removed_sets", return_value=(set(), {}, {"mydb.fresh"})),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        flags = {i["name"]: i.get("added") for i in body["items"]}
        assert flags["fresh"] is True
        assert not flags.get("same")

    def test_list_tables_no_added_flag_outside_rescan_review(self):
        from coa_common.datazone_forms import FORM_TYPE_NAME

        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}

        mock_asset = MagicMock()
        mock_asset.name = f"DS#{_SOURCE_ID}:mydb.mytable"
        mock_asset.asset_id = "asset-001"

        mock_smus = MagicMock()
        mock_smus.search_assets.return_value = MagicMock(items=[mock_asset], next_token=None)
        mock_smus.get_asset_forms.return_value = {
            "formsOutput": [
                {
                    "formName": FORM_TYPE_NAME,
                    "content": json.dumps(
                        {"databaseName": "mydb", "tableName": "mytable", "columnCount": 1, "reviewStatus": "APPROVED"}
                    ),
                }
            ]
        }
        # Not in RESCAN_REVIEW → _removed_sets returns all-empty → no added flag.
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._removed_sets", return_value=(set(), {}, set())),
        ):
            status, body = _parse(_dr._handle_list_tables(self._make_list_event(), _NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        assert body["items"][0].get("added") is None


@pytest.mark.unit
class TestRescanDiffSurfacing:
    """rescanDiff on GET table — old-vs-new breakdown during RESCAN_REVIEW.

    ``_rescan_table_diff`` reconstructs the pre-rescan (last-approved) table from
    the S3 backup's ``modified_backup[table_id]`` (a ``serialize_form`` of the
    OLD table) and re-diffs it against the freshly-merged current asset with the
    same pure ``diff_tables`` rules. Only surfaces while the source is in
    RESCAN_REVIEW.
    """

    _TABLE_ID = "sales.orders"

    def _old_table_with_deterministic_description(self, description: str):
        """OLD table whose *source-derived* (DETERMINISTIC) description differs
        from the current asset — a change ``diff_tables`` will flag. Columns
        mirror the current asset's so only the table-level field changes."""
        from coa_common.domain_models import BusinessMetadata, Column, EnrichmentSource, ReviewStatus, Table

        return Table(
            name="orders",
            database="sales",
            data_source_id=_SOURCE_ID,
            namespace_id=_NAMESPACE_ID,
            business_metadata=BusinessMetadata(
                description=description,
                enrichment_source=EnrichmentSource.DETERMINISTIC,
                review_status=ReviewStatus.APPROVED,
            ),
            columns=[
                Column(name="col_a", data_type="string"),
                Column(name="col_b", data_type="string"),
            ],
        )

    def _backup_blob(self, old_table) -> bytes:
        from coa_common.datazone_forms import serialize_form

        return json.dumps(
            {
                "removed_tables": [],
                "removed_columns": {},
                "modified_backup": {self._TABLE_ID: serialize_form(old_table)},
            }
        ).encode()

    def test_rescan_review_surfaces_table_field_change(self):
        # Source-derived (DETERMINISTIC) table description drifted between the
        # last approved scan and the fresh scan → one DESCRIPTION tableField.
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(
            mock_smus,
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_name="orders",
                database="sales",
                description="Updated order records",
            ),
        )
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "RESCAN_REVIEW"}
        blob = self._backup_blob(self._old_table_with_deterministic_description("Customer order records"))

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._BUCKET_NAME", "bucket"),
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}.get_s3_client", return_value=MagicMock()),
            patch(f"{_DR}.read_file_bytes", return_value=blob),
        ):
            status, body = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))

        assert status == 200
        diff = body["rescanDiff"]
        assert diff is not None
        assert diff["tableFields"] == [
            {
                "field": "description",
                "kind": "DESCRIPTION",
                "old": "Customer order records",
                "new": "Updated order records",
            }
        ]
        # Only the table-level description changed — no per-column breakdown.
        assert not diff.get("columns")

    def test_no_rescan_diff_when_not_rescan_review(self):
        # A backup blob exists (stale from a prior re-scan), but the source is
        # PENDING_REVIEW, not RESCAN_REVIEW — rescanDiff must stay absent/None.
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(
            mock_smus,
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_name="orders",
                database="sales",
                description="Updated order records",
            ),
        )
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "PENDING_REVIEW"}
        blob = self._backup_blob(self._old_table_with_deterministic_description("Customer order records"))

        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._BUCKET_NAME", "bucket"),
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}.get_s3_client", return_value=MagicMock()),
            patch(f"{_DR}.read_file_bytes", return_value=blob),
        ):
            status, body = _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))

        assert status == 200
        assert body.get("rescanDiff") is None

    def _blob_with(self, *, modified_form: str, removed_columns: dict) -> bytes:
        """Backup blob whose ``modified_backup`` pre-image is the deserialized
        form and whose ``removed_columns`` records the source-dropped columns."""
        return json.dumps(
            {
                "removed_tables": [],
                "removed_columns": removed_columns,
                "modified_backup": {self._TABLE_ID: json.loads(modified_form)},
            }
        ).encode()

    def _get_table(self, *, form_content: str, blob: bytes, status: str = "RESCAN_REVIEW"):
        """Drive ``_handle_get_table`` with a live asset (``form_content``) and a
        backup blob, in the given source status. Returns ``(status, body)``."""
        mock_ns_dao = MagicMock()
        mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        mock_smus = MagicMock()
        _mock_single_asset_load(mock_smus, table_id=self._TABLE_ID, form_content=form_content)
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": status}
        with (
            patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
            patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
            patch(f"{_DR}._get_smus_client", return_value=mock_smus),
            patch(f"{_DR}._BUCKET_NAME", "bucket"),
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}.get_s3_client", return_value=MagicMock()),
            patch(f"{_DR}.read_file_bytes", return_value=blob),
        ):
            return _parse(_dr._handle_get_table(_NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))

    def test_rescan_review_surfaces_removed_columns_from_backup(self):
        # A re-scan dropped col_c, but the discovery merge KEEPS col_c in the live
        # asset (tagged pendingDeletion so a steward can "Keep" it), so diff_tables
        # sees col_c on BOTH sides and never flags it removed. The backup's
        # removed_columns is the authoritative record — the panel must still list
        # col_c as removed even though the diff is otherwise empty. (Regression:
        # rescanDiff used to come back None for a dropped-columns-only re-scan.)
        cols = [{"name": "col_a"}, {"name": "col_b"}, {"name": "col_c"}]
        form = _serialize_table(table_name="orders", database="sales", columns=cols)
        blob = self._blob_with(modified_form=form, removed_columns={self._TABLE_ID: ["col_c"]})

        status, body = self._get_table(form_content=form, blob=blob)

        assert status == 200
        diff = body["rescanDiff"]
        assert diff is not None
        assert not diff.get("tableFields")  # only a column was dropped
        removed = [c["name"] for c in diff["columns"] if c["status"] == "removed"]
        assert removed == ["col_c"]

    def test_no_rescan_diff_when_no_changes_and_no_removed_columns(self):
        # Backup pre-image identical to the current asset and no removed columns →
        # nothing to show → rescanDiff stays None (a genuine no-op re-diff).
        form = _serialize_table(table_name="orders", database="sales")
        blob = self._blob_with(modified_form=form, removed_columns={})

        status, body = self._get_table(form_content=form, blob=blob)

        assert status == 200
        assert body.get("rescanDiff") is None

    def test_rescan_review_surfaces_added_modified_and_removed_columns(self):
        # Mixed change set: col_d added, col_b modified (data type string→integer),
        # col_c dropped-but-retained. diff_tables catches added + modified; the
        # removed column is folded in from the backup. All three must appear.
        backup_cols = [{"name": "col_a"}, {"name": "col_b"}, {"name": "col_c"}]
        current_cols = [
            {"name": "col_a"},
            {"name": "col_b", "data_type": "integer"},
            {"name": "col_c"},
            {"name": "col_d"},
        ]
        backup_form = _serialize_table(table_name="orders", database="sales", columns=backup_cols)
        current_form = _serialize_table(table_name="orders", database="sales", columns=current_cols)
        blob = self._blob_with(modified_form=backup_form, removed_columns={self._TABLE_ID: ["col_c"]})

        status, body = self._get_table(form_content=current_form, blob=blob)

        assert status == 200
        diff = body["rescanDiff"]
        assert diff is not None
        statuses = {c["name"]: c["status"] for c in diff["columns"]}
        assert statuses["col_d"] == "added"
        assert statuses["col_b"] == "modified"
        assert statuses["col_c"] == "removed"


class TestKeepRescanRemoval:
    """PUT /tables/{tableId}/keep — decline a re-scan-flagged removal (B6.2).

    Keep edits the S3 backup blob's removal set that the approve worker reads,
    so approve stops deleting the kept item. No DataZone or worker interaction.
    """

    _TABLE = "sales.orders"

    def _event(self, column_name=None):
        body = {} if column_name is None else {"columnName": column_name}
        return {"httpMethod": "PUT", "body": json.dumps(body)}

    def _dao(self, *, status="RESCAN_REVIEW", source_type="DATABASE", found=True):
        dao = MagicMock()
        dao.get.return_value = {"status": status, "sourceType": source_type} if found else None
        return dao

    def _run(self, event, dao, blob, *, read_error: Exception | None = None):
        upload = MagicMock()
        if read_error is not None:
            read = MagicMock(side_effect=read_error)
        else:
            read = MagicMock(return_value=json.dumps(blob).encode())
        with (
            patch(f"{_DR}._BUCKET_NAME", "bucket"),
            patch(f"{_DR}._get_dao", return_value=dao),
            patch(f"{_DR}.get_s3_client", return_value=MagicMock()),
            patch(f"{_DR}.read_file_bytes", read),
            patch(f"{_DR}.upload_json", upload),
        ):
            result = _dr._handle_keep_rescan_removal(event, _NAMESPACE_ID, _SOURCE_ID, self._TABLE)
        return _parse(result), upload

    def test_keep_table_removes_from_removed_tables(self):
        blob = {"removed_tables": [self._TABLE, "sales.other"], "removed_columns": {}}
        (status, body), upload = self._run(self._event(), self._dao(), blob)
        assert status == 200
        assert body == {"tableId": self._TABLE, "pendingDeletion": False}
        upload.assert_called_once()
        written = upload.call_args.args[3]
        assert written["removed_tables"] == ["sales.other"]

    def test_keep_column_removes_from_removed_columns(self):
        blob = {"removed_tables": [], "removed_columns": {self._TABLE: ["gone_col", "other_col"]}}
        (status, body), upload = self._run(self._event(column_name="gone_col"), self._dao(), blob)
        assert status == 200
        assert body == {"tableId": self._TABLE, "columnName": "gone_col", "pendingDeletion": False}
        written = upload.call_args.args[3]
        assert written["removed_columns"] == {self._TABLE: ["other_col"]}

    def test_keep_last_column_drops_empty_table_entry(self):
        blob = {"removed_tables": [], "removed_columns": {self._TABLE: ["gone_col"]}}
        (status, _body), upload = self._run(self._event(column_name="gone_col"), self._dao(), blob)
        assert status == 200
        written = upload.call_args.args[3]
        assert written["removed_columns"] == {}

    def test_keep_is_idempotent_noop_when_not_in_removal_set(self):
        blob = {"removed_tables": ["sales.other"], "removed_columns": {}}
        (status, body), upload = self._run(self._event(), self._dao(), blob)
        assert status == 200
        assert body["pendingDeletion"] is False
        upload.assert_not_called()

    def test_keep_requires_rescan_review(self):
        blob = {"removed_tables": [self._TABLE], "removed_columns": {}}
        (status, _body), upload = self._run(self._event(), self._dao(status="APPROVED"), blob)
        assert status == 409
        upload.assert_not_called()

    def test_keep_source_not_found_returns_404(self):
        (status, _body), upload = self._run(self._event(), self._dao(found=False), {"removed_tables": []})
        assert status == 404
        upload.assert_not_called()

    def test_keep_non_database_source_returns_400(self):
        blob = {"removed_tables": [self._TABLE], "removed_columns": {}}
        (status, _body), upload = self._run(self._event(), self._dao(source_type="DOCUMENT"), blob)
        assert status == 400
        upload.assert_not_called()

    def test_keep_missing_backup_returns_404(self):
        absent = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        (status, _body), upload = self._run(self._event(), self._dao(), {}, read_error=absent)
        assert status == 404
        upload.assert_not_called()

    def test_keep_s3_failure_returns_500_not_404(self):
        """A throttled or denied read must not masquerade as "nothing to keep".

        404 tells the caller the removal set does not exist, so it stops
        retrying. Only a genuine NoSuchKey means that; every other S3 fault is
        server-side and transient-looking, so it has to surface as a 500.
        """
        for code in ("ThrottlingException", "AccessDenied", "InternalError"):
            failure = ClientError({"Error": {"Code": code}}, "GetObject")
            (status, body), upload = self._run(self._event(), self._dao(), {}, read_error=failure)
            assert status == 500, f"{code} should be a server fault, not 404"
            assert "retry" in body["error"].lower()
            upload.assert_not_called()


# ===================================================================
# Resource-oriented review/edit handlers
# ===================================================================


def _serialize_table(
    *,
    table_name: str = "orders",
    database: str = "sales",
    table_status: str = "PENDING_REVIEW",
    columns: list[dict] | None = None,
    description: str = "Existing description",
) -> str:
    """Build a serialized CoaTableMetadata form payload."""
    if columns is None:
        columns = [
            {"name": "col_a", "review_status": "PENDING_REVIEW"},
            {"name": "col_b", "review_status": "PENDING_REVIEW"},
        ]
    return json.dumps(
        {
            "tableName": table_name,
            "databaseName": database,
            "dataSourceId": _SOURCE_ID,
            "namespaceId": _NAMESPACE_ID,
            "databaseDescription": "",
            "description": description,
            "columnCount": len(columns),
            "partitionKeys": "[]",
            "format": "",
            "location": "",
            "synonyms": "[]",
            "glossaryTerms": "[]",
            "tags": "[]",
            "enrichmentSource": "AI_GENERATED",
            "reviewStatus": table_status,
            "primaryKeyColumns": "[]",
            "primaryKeySource": "",
            "primaryKeyConfidence": 0.0,
            "foreignKeys": "[]",
            "columns": json.dumps(
                [
                    {
                        "name": c["name"],
                        "data_type": c.get("data_type", "string"),
                        "nullable": True,
                        "is_partition_key": False,
                        "description": "",
                        "business_metadata": {
                            "description": c.get("description", ""),
                            "synonyms": c.get("synonyms", []),
                            "glossary_terms": c.get("glossary_terms", []),
                            "tags": c.get("tags", []),
                            "enrichment_source": c.get("enrichment_source", "AI_GENERATED"),
                            "review_status": c.get("review_status", "PENDING_REVIEW"),
                            "confidence": c.get("confidence", 0.0),
                        },
                    }
                    for c in columns
                ]
            ),
        }
    )


def _mock_single_asset_load(
    smus_mock: MagicMock,
    *,
    table_id: str,
    form_content: str,
) -> None:
    """Configure an SMUSClient mock to return one matching asset for table_id.

    Exact-name lookups go through ``find_asset_by_name``, so that is what the
    handlers call. ``search_assets`` is still configured for the paged
    enumeration paths (list-tables and the bulk cascade) that search by source
    prefix rather than by an exact asset name.
    """
    asset_name = f"DS#{_SOURCE_ID}:{table_id}"
    asset = MagicMock()
    asset.name = asset_name
    asset.asset_id = f"asset-{table_id}"
    catalog = smus_mock.__dict__.setdefault("_coa_test_table_catalog", {})
    catalog[asset_name] = (asset, form_content)
    smus_mock.find_asset_by_name.side_effect = lambda *, name, **_: catalog[name][0] if name in catalog else None
    smus_mock.search_assets.return_value = MagicMock(
        items=[catalog_item[0] for catalog_item in catalog.values()],
        next_token=None,
    )
    smus_mock.get_asset_forms.side_effect = lambda *, asset_id: {
        "formsOutput": [
            {
                "formName": "CoaTableMetadata",
                "content": next(content for found, content in catalog.values() if found.asset_id == asset_id),
            }
        ]
    }
    smus_mock.create_asset_revision.return_value = MagicMock(asset_id=asset.asset_id)


@pytest.fixture
def _review_env():
    """Patch shared dependencies (SMUS_DOMAIN_ID, ns_dao, smus client, sources dao).

    The sources DAO is configured to return a reviewable source record by
    default so the ``_assert_source_reviewable`` guard passes. Tests that want
    to exercise the guard (e.g. APPROVING source) should override
    ``_review_env["dao"].get.return_value`` themselves.
    """
    mock_ns_dao = MagicMock()
    mock_ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
    mock_smus = create_autospec(_dr.SMUSClient, instance=True)
    mock_dao = MagicMock()
    mock_dao.get.return_value = {
        "PK": f"NS#{_NAMESPACE_ID}",
        "SK": f"SRC#{_SOURCE_ID}",
        "sourceType": "DATABASE",
        "status": "PENDING_REVIEW",
        "discoveredSchemas": ["crm", "sales"],
    }
    with (
        patch(f"{_DR}._SMUS_DOMAIN_ID", "domain-id"),
        patch(f"{_DR}._get_ns_dao", return_value=mock_ns_dao),
        patch(f"{_DR}._get_smus_client", return_value=mock_smus),
        patch(f"{_DR}._get_dao", return_value=mock_dao),
    ):
        yield {"ns_dao": mock_ns_dao, "smus": mock_smus, "dao": mock_dao}


# ===================================================================
# _handle_review_table
# ===================================================================


@pytest.mark.unit
class TestReviewTable:
    _TABLE_ID = "sales.orders"

    def _event(self, body):
        return {"body": json.dumps(body) if body is not None else "{}"}

    def test_invalid_json_returns_400(self, _review_env):
        status, _ = _parse(_dr._handle_review_table({"body": "not-json"}, _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 400

    def test_missing_decision_returns_400(self, _review_env):
        status, body = _parse(_dr._handle_review_table(self._event({}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 400
        assert "decision" in body["error"]

    def test_invalid_decision_returns_400(self, _review_env):
        status, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "MAYBE"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 400

    def test_no_smus_domain_returns_500(self, _review_env):
        with patch(f"{_DR}._SMUS_DOMAIN_ID", ""):
            status, _ = _parse(
                _dr._handle_review_table(
                    self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
                )
            )
        assert status == 500

    def test_namespace_not_found_returns_404(self, _review_env):
        _review_env["ns_dao"].get.return_value = None
        status, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 404

    def test_table_not_found_returns_404(self, _review_env):
        # None from find_asset_by_name is the only "absent" signal now.
        _review_env["smus"].find_asset_by_name.return_value = None
        status, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 404

    def test_pending_to_approved_writes_revision_and_increments_counter(self, _review_env):
        # Per-asset APPROVE now requires all columns to be terminal first.
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="PENDING_REVIEW",
                columns=[
                    {"name": "col_a", "review_status": "APPROVED"},
                    {"name": "col_b", "review_status": "REJECTED"},
                ],
            ),
        )
        status, body = _parse(
            _dr._handle_review_table(self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 200
        assert body["reviewStatus"] == "APPROVED"
        _review_env["smus"].create_asset_revision.assert_called_once()
        _review_env["dao"].atomic_increment.assert_called_once_with(
            {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"},
            "tablesApproved",
            amount=1,
        )

    # ── #116 enrichment acceptance rate (ReviewScope=Table) ──────────────
    # The sources-structured-scan dashboard SEARCHes
    # {COA/Sources,ReviewScope} for these exact metric names, so a
    # rename on either side silently darkens the widget.

    def _review_and_capture_metrics(self, _review_env, *, decision, table_status):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status=table_status,
                columns=[{"name": "col_a", "review_status": table_status}],
            ),
        )
        with patch(f"{_DR}.emit_metric") as mock_emit:
            status, _ = _parse(
                _dr._handle_review_table(self._event({"decision": decision}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
            )
        assert status == 200
        return [(c.args[0], c.args[1], c.kwargs) for c in mock_emit.call_args_list if c.args]

    def test_approve_transition_emits_tables_approved_metric(self, _review_env):
        emitted = self._review_and_capture_metrics(_review_env, decision="APPROVED", table_status="PENDING_REVIEW")
        assert ("TablesApprovedByReview", 1, {"ReviewScope": "Table"}) in emitted
        assert not any(name == "TablesRejectedByReview" for name, _, _ in emitted)

    def test_reject_transition_emits_tables_rejected_metric(self, _review_env):
        emitted = self._review_and_capture_metrics(_review_env, decision="REJECTED", table_status="PENDING_REVIEW")
        assert ("TablesRejectedByReview", 1, {"ReviewScope": "Table"}) in emitted
        assert not any(name == "TablesApprovedByReview" for name, _, _ in emitted)

    def test_idempotent_review_emits_no_acceptance_metric(self, _review_env):
        """Re-approving an already-APPROVED table is not a new human decision —
        counting it would inflate the acceptance rate on every retry."""
        emitted = self._review_and_capture_metrics(_review_env, decision="APPROVED", table_status="APPROVED")
        assert not any(name in ("TablesApprovedByReview", "TablesRejectedByReview") for name, _, _ in emitted)

    def test_approve_cascades_pending_columns_to_approved(self, _review_env):
        # Per-asset APPROVE cascades PENDING columns to APPROVED (children
        # first), then approves the table — no 409 precondition anymore.
        from coa_common.datazone_forms import deserialize_form

        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="PENDING_REVIEW",
                columns=[
                    {"name": "col_a", "review_status": "PENDING_REVIEW"},
                    {"name": "col_b", "review_status": "APPROVED"},
                ],
            ),
        )
        status, body = _parse(
            _dr._handle_review_table(self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 200
        assert body["reviewStatus"] == "APPROVED"
        forms_input = _review_env["smus"].create_asset_revision.call_args.kwargs["forms_input"]
        written = deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=_SOURCE_ID)
        col_status = {c.name: c.business_metadata.review_status for c in written.columns}
        assert col_status == {"col_a": "APPROVED", "col_b": "APPROVED"}

    def test_reject_persists_column_change_when_table_already_rejected(self, _review_env):
        # Aggressive reject: the table is already REJECTED but a stray column is
        # still APPROVED. The handler must persist the column flip even though
        # the table status does not change (regression guard for the
        # write-on-any-change fix).
        from coa_common.datazone_forms import deserialize_form

        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="REJECTED",
                columns=[
                    {"name": "col_a", "review_status": "APPROVED"},
                    {"name": "col_b", "review_status": "REJECTED"},
                ],
            ),
        )
        status, body = _parse(
            _dr._handle_review_table(self._event({"decision": "REJECTED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 200
        assert body["reviewStatus"] == "REJECTED"
        _review_env["smus"].create_asset_revision.assert_called_once()
        forms_input = _review_env["smus"].create_asset_revision.call_args.kwargs["forms_input"]
        written = deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=_SOURCE_ID)
        col_status = {c.name: c.business_metadata.review_status for c in written.columns}
        assert col_status == {"col_a": "REJECTED", "col_b": "REJECTED"}

    def test_already_approved_is_idempotent_no_write_no_counter(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="APPROVED",
                columns=[
                    {"name": "col_a", "review_status": "APPROVED"},
                    {"name": "col_b", "review_status": "APPROVED"},
                ],
            ),
        )
        status, body = _parse(
            _dr._handle_review_table(self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 200
        assert body["reviewStatus"] == "APPROVED"
        _review_env["smus"].create_asset_revision.assert_not_called()
        _review_env["dao"].atomic_increment.assert_not_called()

    def test_approved_to_rejected_decrements_counter(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(table_status="APPROVED"),
        )
        _, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "REJECTED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        _review_env["dao"].atomic_increment.assert_called_once_with(
            {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"},
            "tablesApproved",
            amount=-1,
        )

    def test_rejected_to_approved_increments_counter(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="REJECTED",
                columns=[
                    {"name": "col_a", "review_status": "APPROVED"},
                    {"name": "col_b", "review_status": "APPROVED"},
                ],
            ),
        )
        _, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        _review_env["dao"].atomic_increment.assert_called_once_with(
            {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"},
            "tablesApproved",
            amount=1,
        )

    def test_pending_to_rejected_does_not_change_counter(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(table_status="PENDING_REVIEW"),
        )
        _, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "REJECTED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        _review_env["smus"].create_asset_revision.assert_called_once()
        _review_env["dao"].atomic_increment.assert_not_called()

    def test_counter_failure_is_swallowed_and_emits_metric(self, _review_env):
        # The DataZone revision is the source of truth; a failed counter
        # increment must NOT fail the request, but it MUST emit a metric so
        # ops can alarm on systematic drift.
        from botocore.exceptions import ClientError

        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="PENDING_REVIEW",
                columns=[{"name": "col_a", "review_status": "APPROVED"}],
            ),
        )
        _review_env["dao"].atomic_increment.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with patch(f"{_DR}.emit_metric") as mock_emit:
            status, body = _parse(
                _dr._handle_review_table(
                    self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
                )
            )
        # Request still succeeds despite the counter failure.
        assert status == 200
        assert body["reviewStatus"] == "APPROVED"
        # Drift is made observable. (The same path also emits the #116
        # acceptance-rate metric, so assert on the name rather than call count.)
        assert "ApprovalCounterUpdateFailed" in [c.args[0] for c in mock_emit.call_args_list if c.args]

    def test_approve_is_noop_on_columns_when_all_terminal(self, _review_env):
        # With the precondition that all columns must already be terminal,
        # approving the table preserves each column's prior decision and only
        # flips the table itself.
        from coa_common.datazone_forms import deserialize_form

        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="PENDING_REVIEW",
                columns=[
                    {"name": "col_a", "review_status": "REJECTED"},
                    {"name": "col_b", "review_status": "APPROVED"},
                    {"name": "col_c", "review_status": "APPROVED"},
                ],
            ),
        )
        status, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "APPROVED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 200
        # Inspect the form payload that was written back
        forms_input = _review_env["smus"].create_asset_revision.call_args.kwargs["forms_input"]
        written = deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=_SOURCE_ID)
        col_status = {c.name: c.business_metadata.review_status for c in written.columns}
        assert col_status == {"col_a": "REJECTED", "col_b": "APPROVED", "col_c": "APPROVED"}

    def test_reject_cascades_to_all_non_rejected_columns(self, _review_env):
        from coa_common.datazone_forms import deserialize_form

        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="APPROVED",
                columns=[
                    {"name": "col_a", "review_status": "PENDING_REVIEW"},
                    {"name": "col_b", "review_status": "REJECTED"},
                    {"name": "col_c", "review_status": "APPROVED"},
                ],
            ),
        )
        _, _ = _parse(
            _dr._handle_review_table(self._event({"decision": "REJECTED"}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        forms_input = _review_env["smus"].create_asset_revision.call_args.kwargs["forms_input"]
        written = deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=_SOURCE_ID)
        col_status = {c.name: c.business_metadata.review_status for c in written.columns}
        assert all(s == "REJECTED" for s in col_status.values())


# ===================================================================
# _handle_update_table_metadata
# ===================================================================


@pytest.mark.unit
class TestUpdateTableMetadata:
    _TABLE_ID = "sales.orders"

    def _event(self, body):
        return {"body": json.dumps(body) if body is not None else "{}"}

    def test_invalid_json_returns_400(self, _review_env):
        status, _ = _parse(_dr._handle_update_table_metadata({"body": "x"}, _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 400

    def test_missing_overrides_returns_400(self, _review_env):
        status, _ = _parse(
            _dr._handle_update_table_metadata(self._event({}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 400

    def test_empty_overrides_returns_400(self, _review_env):
        status, _ = _parse(
            _dr._handle_update_table_metadata(self._event({"overrides": {}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 400

    def test_happy_path_writes_revision_and_marks_steward_edited(self, _review_env):
        from coa_common.datazone_forms import deserialize_form

        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(table_status="PENDING_REVIEW"),
        )
        status, body = _parse(
            _dr._handle_update_table_metadata(
                self._event({"overrides": {"description": "New description", "tags": ["pii"]}}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
            )
        )
        assert status == 200
        # reviewStatus is preserved (PATCH must not change it)
        assert body["reviewStatus"] == "PENDING_REVIEW"
        forms_input = _review_env["smus"].create_asset_revision.call_args.kwargs["forms_input"]
        written = deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=_SOURCE_ID)
        assert written.business_metadata.description == "New description"
        assert written.business_metadata.tags == ["pii"]
        assert written.business_metadata.enrichment_source == "STEWARD_EDITED"
        assert written.business_metadata.review_status == "PENDING_REVIEW"
        # Counter is never touched on metadata edits
        _review_env["dao"].atomic_increment.assert_not_called()

    def test_no_op_when_override_matches_existing(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(description="Existing description"),
        )
        status, _ = _parse(
            _dr._handle_update_table_metadata(
                self._event({"overrides": {"description": "Existing description"}}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
            )
        )
        assert status == 200
        _review_env["smus"].create_asset_revision.assert_not_called()


# ===================================================================
# _handle_review_column
# ===================================================================


@pytest.mark.unit
class TestReviewColumn:
    _TABLE_ID = "sales.orders"
    _COLUMN = "col_a"

    def _event(self, body):
        return {"body": json.dumps(body) if body is not None else "{}"}

    def test_missing_decision_returns_400(self, _review_env):
        status, _ = _parse(
            _dr._handle_review_column(self._event({}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID, self._COLUMN)
        )
        assert status == 400

    def test_column_not_found_returns_404(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(),
        )
        status, _ = _parse(
            _dr._handle_review_column(
                self._event({"decision": "APPROVED"}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
                "ghost_column",
            )
        )
        assert status == 404

    def test_pending_to_approved_writes_revision_no_counter(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(),
        )
        status, body = _parse(
            _dr._handle_review_column(
                self._event({"decision": "APPROVED"}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
                self._COLUMN,
            )
        )
        assert status == 200
        assert body["reviewStatus"] == "APPROVED"
        _review_env["smus"].create_asset_revision.assert_called_once()
        # Column-level review never touches the table-level counter
        _review_env["dao"].atomic_increment.assert_not_called()

    def test_already_approved_column_is_idempotent(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(columns=[{"name": self._COLUMN, "review_status": "APPROVED"}]),
        )
        status, _ = _parse(
            _dr._handle_review_column(
                self._event({"decision": "APPROVED"}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
                self._COLUMN,
            )
        )
        assert status == 200
        _review_env["smus"].create_asset_revision.assert_not_called()


# ===================================================================
# _handle_update_column_metadata
# ===================================================================


@pytest.mark.unit
class TestUpdateColumnMetadata:
    _TABLE_ID = "sales.orders"
    _COLUMN = "col_a"

    def _event(self, body):
        return {"body": json.dumps(body) if body is not None else "{}"}

    def test_missing_overrides_returns_400(self, _review_env):
        status, _ = _parse(
            _dr._handle_update_column_metadata(self._event({}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID, self._COLUMN)
        )
        assert status == 400

    def test_column_not_found_returns_404(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(),
        )
        status, _ = _parse(
            _dr._handle_update_column_metadata(
                self._event({"overrides": {"description": "x"}}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
                "ghost",
            )
        )
        assert status == 404

    def test_happy_path_marks_steward_edited(self, _review_env):
        from coa_common.datazone_forms import deserialize_form

        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(),
        )
        status, body = _parse(
            _dr._handle_update_column_metadata(
                self._event({"overrides": {"description": "New col desc"}}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
                self._COLUMN,
            )
        )
        assert status == 200
        # reviewStatus on the column is unchanged by an edit
        assert body["reviewStatus"] == "PENDING_REVIEW"
        forms_input = _review_env["smus"].create_asset_revision.call_args.kwargs["forms_input"]
        written = deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=_SOURCE_ID)
        col = next(c for c in written.columns if c.name == self._COLUMN)
        assert col.business_metadata.description == "New col desc"
        assert col.business_metadata.enrichment_source == "STEWARD_EDITED"


# ===================================================================
# _handle_approve_source / _handle_reject_source (async bulk)
# ===================================================================


@pytest.fixture
def _bulk_env():
    """Patch dependencies for bulk approve/reject handlers."""
    mock_dao = MagicMock()
    mock_sqs = MagicMock()
    with (
        patch(f"{_DR}._REVIEW_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/review-queue"),
        patch(f"{_DR}._get_dao", return_value=mock_dao),
        patch(f"{_DR}._get_sqs", return_value=mock_sqs),
    ):
        yield {"dao": mock_dao, "sqs": mock_sqs}


@pytest.mark.unit
class TestApproveSource:
    _EVENT = {"body": "{}"}

    def test_happy_path_returns_202_and_sends_sqs(self, _bulk_env):
        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
            "tablesDiscovered": 5,
        }
        # Conditional update succeeds (returns True)
        _bulk_env["dao"].update.return_value = True
        status, body = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 202
        assert body == {"sourceId": _SOURCE_ID, "status": "APPROVING"}
        # Conditional update was called with the right transition condition.
        # The helper builds dynamic IN-clause placeholders, so assert by value-set
        # rather than pinning specific placeholder names.
        update_call = _bulk_env["dao"].update.call_args
        assert set(update_call.kwargs["condition_values"].values()) == {
            "PENDING_REVIEW",
            "APPROVAL_FAILED",
            "RESCAN_REVIEW",
        }
        assert "#status" in update_call.kwargs["condition_names"]
        # SQS message contains decision=APPROVED
        sqs_call = _bulk_env["sqs"].send_message.call_args
        assert sqs_call.kwargs["QueueUrl"] == "https://sqs.us-east-1.amazonaws.com/123/review-queue"
        msg_body = json.loads(sqs_call.kwargs["MessageBody"])
        assert msg_body == {
            "namespaceId": _NAMESPACE_ID,
            "sourceId": _SOURCE_ID,
            "decision": "APPROVED",
            "isRescan": False,
        }

    def test_rescan_review_source_can_be_approved_with_isrescan_flag(self, _bulk_env):
        # A re-scan lands the source in RESCAN_REVIEW; approve must be allowed
        # from there and must tell the worker it is a re-scan.
        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "RESCAN_REVIEW",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.return_value = True
        status, _body = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 202
        assert "RESCAN_REVIEW" in set(_bulk_env["dao"].update.call_args.kwargs["condition_values"].values())
        msg_body = json.loads(_bulk_env["sqs"].send_message.call_args.kwargs["MessageBody"])
        assert msg_body["isRescan"] is True

    def test_approve_from_rejected_source_is_locked_409(self, _bulk_env):
        # REJECTED is terminal: bulk approve is not an allowed entry state
        # either — a rejected source cannot be resurrected, only re-onboarded.
        from botocore.exceptions import ClientError

        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "REJECTED",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem"
        )
        status, body = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 409
        assert "REJECTED" in body["error"]
        _bulk_env["sqs"].send_message.assert_not_called()

    def test_source_not_found_returns_404(self, _bulk_env):
        _bulk_env["dao"].get.return_value = None
        status, _ = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 404
        _bulk_env["sqs"].send_message.assert_not_called()

    def test_non_database_source_returns_400(self, _bulk_env):
        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DOCUMENTS",
            "status": "PENDING_REVIEW",
        }
        status, _ = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400
        _bulk_env["sqs"].send_message.assert_not_called()

    def test_already_approving_returns_409(self, _bulk_env):
        from botocore.exceptions import ClientError

        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "APPROVING",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )
        status, body = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 409
        assert "APPROVING" in body["error"]
        _bulk_env["sqs"].send_message.assert_not_called()

    def test_retry_from_approval_failed_succeeds(self, _bulk_env):
        # User retries after a previous failure — should be allowed
        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "APPROVAL_FAILED",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.return_value = True
        status, _ = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 202

    def test_review_queue_url_unset_returns_500(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
            "tablesDiscovered": 5,
        }
        mock_dao.update.return_value = True
        with (
            patch(f"{_DR}._REVIEW_QUEUE_URL", ""),
            patch(f"{_DR}._get_dao", return_value=mock_dao),
        ):
            status, _ = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 500

    def test_sqs_failure_rolls_back_status(self, _bulk_env):
        from botocore.exceptions import ClientError

        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.return_value = True
        _bulk_env["sqs"].send_message.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "SendMessage")
        status, _ = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 500
        # Two update calls: first to APPROVING, second rollback to APPROVAL_FAILED
        assert _bulk_env["dao"].update.call_count == 2
        rollback_call = _bulk_env["dao"].update.call_args_list[1]
        assert rollback_call.args[1] == {"status": "APPROVAL_FAILED"}


@pytest.mark.unit
class TestRejectSource:
    _EVENT = {"body": "{}"}

    def test_happy_path_returns_202_and_sends_reject_decision(self, _bulk_env):
        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.return_value = True
        status, body = _parse(_dr._handle_reject_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 202
        # Reject flow uses REJECTING (not APPROVING) for the transient state
        assert body == {"sourceId": _SOURCE_ID, "status": "REJECTING"}
        msg_body = json.loads(_bulk_env["sqs"].send_message.call_args.kwargs["MessageBody"])
        assert msg_body["decision"] == "REJECTED"
        # Conditional update allows entry from PENDING_REVIEW or REJECTION_FAILED
        update_call = _bulk_env["dao"].update.call_args
        assert set(update_call.kwargs["condition_values"].values()) == {
            "PENDING_REVIEW",
            "REJECTION_FAILED",
            "RESCAN_REVIEW",
        }

    def test_rescan_review_source_can_be_rejected_with_isrescan_flag(self, _bulk_env):
        # A re-scan reject must be allowed from RESCAN_REVIEW and flagged so the
        # worker restores the pre-rescan state (and returns the source to APPROVED).
        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "RESCAN_REVIEW",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.return_value = True
        status, _body = _parse(_dr._handle_reject_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 202
        assert "RESCAN_REVIEW" in set(_bulk_env["dao"].update.call_args.kwargs["condition_values"].values())
        msg_body = json.loads(_bulk_env["sqs"].send_message.call_args.kwargs["MessageBody"])
        assert msg_body["isRescan"] is True

    def test_retry_from_rejection_failed_succeeds(self, _bulk_env):
        # User retries after a previous reject worker failure
        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "REJECTION_FAILED",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.return_value = True
        status, body = _parse(_dr._handle_reject_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 202
        assert body["status"] == "REJECTING"

    def test_sqs_failure_rolls_back_to_rejection_failed(self, _bulk_env):
        from botocore.exceptions import ClientError

        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.return_value = True
        _bulk_env["sqs"].send_message.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "SendMessage")
        status, _ = _parse(_dr._handle_reject_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 500
        # Rollback uses REJECTION_FAILED, not APPROVAL_FAILED
        rollback_call = _bulk_env["dao"].update.call_args_list[1]
        assert rollback_call.args[1] == {"status": "REJECTION_FAILED"}

    def test_reject_from_rejected_source_is_locked_409(self, _bulk_env):
        # REJECTED is terminal: bulk reject is not an allowed entry state, so
        # the conditional transition fails and the source stays locked.
        from botocore.exceptions import ClientError

        _bulk_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "REJECTED",
            "tablesDiscovered": 5,
        }
        _bulk_env["dao"].update.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem"
        )
        status, body = _parse(_dr._handle_reject_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 409
        assert "REJECTED" in body["error"]
        _bulk_env["sqs"].send_message.assert_not_called()


# ===================================================================
# Bulk-review zero-tables precondition
#
# ApproveSource and RejectSource must reject the call if the source has
# zero discovered tables. The UI also disables the buttons in that case;
# this guard prevents direct API misuse from leaving the source stuck in
# APPROVING / REJECTING with no work to do.
# ===================================================================


@pytest.mark.unit
class TestBulkReviewZeroTablesGuard:
    _EVENT = {"body": "{}"}

    @pytest.mark.parametrize("missing_tables_value", [None, 0])
    def test_approve_with_zero_tables_returns_400(self, _bulk_env, missing_tables_value):
        item = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
        }
        if missing_tables_value is not None:
            item["tablesDiscovered"] = missing_tables_value
        _bulk_env["dao"].get.return_value = item
        status, body = _parse(_dr._handle_approve_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400
        assert "tablesDiscovered" in body
        assert body["tablesDiscovered"] == 0
        # Must not transition the source or send a worker message.
        _bulk_env["dao"].update.assert_not_called()
        _bulk_env["sqs"].send_message.assert_not_called()

    @pytest.mark.parametrize("missing_tables_value", [None, 0])
    def test_reject_with_zero_tables_returns_400(self, _bulk_env, missing_tables_value):
        item = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "PENDING_REVIEW",
        }
        if missing_tables_value is not None:
            item["tablesDiscovered"] = missing_tables_value
        _bulk_env["dao"].get.return_value = item
        status, body = _parse(_dr._handle_reject_source(self._EVENT, _NAMESPACE_ID, _SOURCE_ID))
        assert status == 400
        assert "tablesDiscovered" in body
        _bulk_env["dao"].update.assert_not_called()
        _bulk_env["sqs"].send_message.assert_not_called()


# ===================================================================
# Reviewable-state guard (_assert_source_reviewable)
#
# The guard runs before per-table/column review and edit operations to
# prevent races with the bulk worker (and to block ops during scan/enrich).
# These tests pin every transition point: which states are allowed through,
# which return 409, plus the missing-source and non-DATABASE error paths.
# ===================================================================


@pytest.mark.unit
class TestReviewableGuard:
    """The guard protects per-table/column review AND edit endpoints."""

    _TABLE_ID = "sales.orders"
    _COLUMN = "col_a"

    def _approve_event(self):
        return {"body": json.dumps({"decision": "APPROVED"})}

    def _override_event(self):
        return {"body": json.dumps({"overrides": {"description": "x"}})}

    @pytest.mark.parametrize(
        "status",
        ["PENDING_REVIEW", "RESCAN_REVIEW", "APPROVED", "APPROVAL_FAILED", "REJECTION_FAILED"],
    )
    def test_review_table_allows_all_reviewable_states(self, _review_env, status):
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": status,
        }
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                columns=[{"name": "col_a", "review_status": "APPROVED"}],
            ),
        )
        resp_status, _ = _parse(
            _dr._handle_review_table(self._approve_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert resp_status == 200

    @pytest.mark.parametrize(
        "status",
        ["REGISTERED", "SCANNING", "ENRICHING", "APPROVING", "REJECTING", "REJECTED", "DELETING"],
    )
    def test_review_table_blocks_non_reviewable_states_with_409(self, _review_env, status):
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": status,
        }
        resp_status, body = _parse(
            _dr._handle_review_table(self._approve_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert resp_status == 409
        assert status in body["error"]
        # No DataZone calls should have been made
        _review_env["smus"].search_assets.assert_not_called()
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_review_table_404_when_source_missing(self, _review_env):
        _review_env["dao"].get.return_value = None
        resp_status, _ = _parse(
            _dr._handle_review_table(self._approve_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert resp_status == 404

    def test_409_body_structure_preserves_status_and_allowed_states(self, _review_env):
        # The UI renders the 409 body's "error" string directly, so its exact
        # structure is a contract: a single "error" key whose message embeds the
        # offending status and the sorted list of reviewable states.
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "APPROVING",
        }
        resp_status, body = _parse(
            _dr._handle_review_table(self._approve_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert resp_status == 409
        # Exactly one field, named "error", carrying a human-readable string.
        assert list(body.keys()) == ["error"]
        assert isinstance(body["error"], str)
        # The blocking status must be preserved verbatim for the user.
        assert "APPROVING" in body["error"]
        # Each allowed state is named so the user knows when the action is valid.
        for allowed in ("PENDING_REVIEW", "APPROVED"):
            assert allowed in body["error"]

    def test_review_table_400_when_source_not_database(self, _review_env):
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DOCUMENTS",
            "status": "PENDING_REVIEW",
        }
        resp_status, _ = _parse(
            _dr._handle_review_table(self._approve_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert resp_status == 400

    def test_update_table_metadata_blocks_during_approving(self, _review_env):
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "APPROVING",
        }
        resp_status, _ = _parse(
            _dr._handle_update_table_metadata(self._override_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert resp_status == 409
        # Without the guard, this is the silent-edit-loss race scenario.
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_update_table_metadata_allows_rescan_review(self, _review_env):
        # A re-scan lands the source in RESCAN_REVIEW; the steward must be able
        # to touch up the regenerated metadata there, exactly as on a first scan.
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "RESCAN_REVIEW",
        }
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(columns=[{"name": "col_a", "review_status": "PENDING_REVIEW"}]),
        )
        resp_status, _ = _parse(
            _dr._handle_update_table_metadata(self._override_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert resp_status == 200

    def test_review_column_blocks_during_rejecting(self, _review_env):
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "REJECTING",
        }
        resp_status, _ = _parse(
            _dr._handle_review_column(self._approve_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID, self._COLUMN)
        )
        assert resp_status == 409

    def test_update_column_metadata_blocks_during_enriching(self, _review_env):
        _review_env["dao"].get.return_value = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "status": "ENRICHING",
        }
        resp_status, _ = _parse(
            _dr._handle_update_column_metadata(
                self._override_event(), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID, self._COLUMN
            )
        )
        assert resp_status == 409


# ===================================================================
# _handle_update_table_keys
# ===================================================================


@pytest.mark.unit
class TestTerminalEditGuard:
    """Metadata and key edits are blocked (409) once an asset is APPROVED or REJECTED."""

    _TABLE_ID = "sales.orders"

    def _event(self, body):
        return {"body": json.dumps(body)}

    @pytest.mark.parametrize("terminal", ["REJECTED"])
    def test_table_metadata_edit_blocked_when_terminal(self, _review_env, terminal):
        _mock_single_asset_load(
            _review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table(table_status=terminal)
        )
        status, body = _parse(
            _dr._handle_update_table_metadata(
                self._event({"overrides": {"description": "x"}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
            )
        )
        assert status == 409
        assert "terminal" in body["error"].lower()
        _review_env["smus"].create_asset_revision.assert_not_called()

    @pytest.mark.parametrize("terminal", ["REJECTED"])
    def test_table_keys_edit_blocked_when_terminal(self, _review_env, terminal):
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(table_status=terminal, columns=[{"name": "id", "review_status": terminal}]),
        )
        status, _ = _parse(
            _dr._handle_update_table_keys(
                self._event({"primaryKey": {"columns": ["id"]}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
            )
        )
        assert status == 409
        _review_env["smus"].create_asset_revision.assert_not_called()

    @pytest.mark.parametrize("terminal", ["REJECTED"])
    def test_column_metadata_edit_blocked_when_terminal(self, _review_env, terminal):
        # Source/table reviewable; only the column itself is terminal → 409.
        _mock_single_asset_load(
            _review_env["smus"],
            table_id=self._TABLE_ID,
            form_content=_serialize_table(
                table_status="PENDING_REVIEW",
                columns=[{"name": "col_a", "review_status": terminal}],
            ),
        )
        status, _ = _parse(
            _dr._handle_update_column_metadata(
                self._event({"overrides": {"description": "x"}}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
                "col_a",
            )
        )
        assert status == 409
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_table_metadata_edit_allowed_when_pending(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table(table_status="PENDING_REVIEW")
        )
        status, _ = _parse(
            _dr._handle_update_table_metadata(
                self._event({"overrides": {"description": "x"}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
            )
        )
        assert status == 200

    def test_table_metadata_edit_allowed_when_approved(self, _review_env):
        _mock_single_asset_load(
            _review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table(table_status="APPROVED")
        )
        status, _ = _parse(
            _dr._handle_update_table_metadata(
                self._event({"overrides": {"description": "x"}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
            )
        )
        assert status == 200


@pytest.mark.unit
class TestUpdateTableKeys:
    _TABLE_ID = "sales.orders"

    def _event(self, body):
        return {"body": json.dumps(body) if body is not None else "{}"}

    def _written_table(self, _review_env):
        from coa_common.datazone_forms import deserialize_form

        forms_input = _review_env["smus"].create_asset_revision.call_args.kwargs["forms_input"]
        return deserialize_form(json.loads(forms_input[0]["content"]), data_source_id=_SOURCE_ID)

    def test_invalid_json_returns_400(self, _review_env):
        status, _ = _parse(
            _dr._handle_update_table_keys({"body": "not-json"}, _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 400

    def test_missing_both_returns_400(self, _review_env):
        status, body = _parse(_dr._handle_update_table_keys(self._event({}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 400
        assert "primaryKey" in body["error"]

    def test_set_primary_key_marks_steward_specified(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        status, _ = _parse(
            _dr._handle_update_table_keys(
                self._event({"primaryKey": {"columns": ["col_a"]}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
            )
        )
        assert status == 200
        table = self._written_table(_review_env)
        assert table.primary_key.columns == ["col_a"]
        assert table.primary_key.source == "STEWARD_SPECIFIED"

    def test_duplicate_primary_key_columns_are_deduped(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        status, _ = _parse(
            _dr._handle_update_table_keys(
                self._event({"primaryKey": {"columns": ["col_a", "col_b", "col_a"]}}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
            )
        )
        assert status == 200
        assert self._written_table(_review_env).primary_key.columns == ["col_a", "col_b"]

    def _form_with_keys(self):
        form = json.loads(_serialize_table())
        form["primaryKeyColumns"] = json.dumps(["col_a"])
        form["primaryKeySource"] = "DETERMINISTIC"
        form["foreignKeys"] = json.dumps(
            [{"column": "col_a", "target_table": "customers", "target_column": "id", "source": "DETERMINISTIC"}]
        )
        return json.dumps(form)

    def test_unchanged_primary_key_keeps_original_source(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=self._form_with_keys())
        status, _ = _parse(
            _dr._handle_update_table_keys(
                self._event({"primaryKey": {"columns": ["col_a"]}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
            )
        )
        assert status == 200
        assert self._written_table(_review_env).primary_key.source == "DETERMINISTIC"

    def test_unchanged_foreign_key_kept_only_edited_one_marked_steward(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=self._form_with_keys())
        _mock_single_asset_load(
            _review_env["smus"],
            table_id="sales.customers",
            form_content=_serialize_table(table_name="customers", columns=[{"name": "id"}]),
        )
        # Re-send the existing FK unchanged plus one new FK (simulates the UI
        # sending the full list when only adding one).
        body = {
            "foreignKeys": [
                {"column": "col_a", "targetTable": "customers", "targetColumn": "id"},
                {"column": "col_b", "targetTable": "orders", "targetColumn": "col_a"},
            ]
        }
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 200
        by_col = {fk.column: fk.source for fk in self._written_table(_review_env).foreign_keys}
        assert by_col == {"col_a": "DETERMINISTIC", "col_b": "STEWARD_SPECIFIED"}

    def test_unchanged_ambiguous_bare_foreign_key_does_not_block_primary_key_edit(self, _review_env):
        smus = _review_env["smus"]
        _mock_single_asset_load(smus, table_id=self._TABLE_ID, form_content=self._form_with_keys())
        for database in ("sales", "crm"):
            _mock_single_asset_load(
                smus,
                table_id=f"{database}.customers",
                form_content=_serialize_table(
                    table_name="customers",
                    database=database,
                    columns=[{"name": "id"}],
                ),
            )
        body = {
            "primaryKey": {"columns": ["col_b"]},
            "foreignKeys": [{"column": "col_a", "targetTable": "customers", "targetColumn": "id"}],
        }

        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))

        assert status == 200
        table = self._written_table(_review_env)
        assert table.primary_key.columns == ["col_b"]
        assert table.foreign_keys[0].source == "DETERMINISTIC"
        smus.search_assets.assert_not_called()
        assert [call.kwargs["name"] for call in smus.find_asset_by_name.call_args_list] == [
            f"DS#{_SOURCE_ID}:{self._TABLE_ID}"
        ]

    def test_unknown_primary_key_column_returns_400(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        status, _ = _parse(
            _dr._handle_update_table_keys(
                self._event({"primaryKey": {"columns": ["nope"]}}), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID
            )
        )
        assert status == 400
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_set_foreign_keys_marks_steward_specified(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        _mock_single_asset_load(
            _review_env["smus"],
            table_id="sales.customers",
            form_content=_serialize_table(table_name="customers", columns=[{"name": "id"}]),
        )
        body = {"foreignKeys": [{"column": "col_a", "targetTable": "customers", "targetColumn": "id"}]}
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 200
        table = self._written_table(_review_env)
        assert len(table.foreign_keys) == 1
        assert table.foreign_keys[0].column == "col_a"
        assert table.foreign_keys[0].target_table == "customers"
        assert table.foreign_keys[0].source == "STEWARD_SPECIFIED"

    def test_foreign_key_unknown_column_returns_400(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        body = {"foreignKeys": [{"column": "nope", "targetTable": "customers"}]}
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 400

    def test_foreign_key_missing_target_returns_400(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        body = {"foreignKeys": [{"column": "col_a"}]}
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 400

    @pytest.mark.parametrize(
        "foreign_key",
        [
            {"column": ["col_a"], "targetTable": "customers"},
            {"column": "col_a", "targetTable": ["customers"]},
            {"column": "col_a", "targetTable": "customers", "targetColumn": ["id"]},
        ],
    )
    def test_foreign_key_non_string_fields_return_400(self, _review_env, foreign_key):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        status, _ = _parse(
            _dr._handle_update_table_keys(
                self._event({"foreignKeys": [foreign_key]}),
                _NAMESPACE_ID,
                _SOURCE_ID,
                self._TABLE_ID,
            )
        )
        assert status == 400
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_foreign_key_unknown_target_table_returns_400(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        body = {"foreignKeys": [{"column": "col_a", "targetTable": "sales.missing_table", "targetColumn": "id"}]}
        status, response = _parse(
            _dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 400
        assert "sales.missing_table" in response["error"]
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_foreign_key_unknown_target_column_returns_400(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        _mock_single_asset_load(
            _review_env["smus"],
            table_id="sales.customers",
            form_content=_serialize_table(table_name="customers", columns=[{"name": "id"}]),
        )
        body = {
            "foreignKeys": [
                {
                    "column": "col_a",
                    "targetTable": "sales.customers",
                    "targetColumn": "missing_column",
                }
            ]
        }
        status, response = _parse(
            _dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 400
        assert "missing_column" in response["error"]
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_foreign_key_ambiguous_bare_target_returns_400(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        for database in ("sales", "crm"):
            _mock_single_asset_load(
                _review_env["smus"],
                table_id=f"{database}.customers",
                form_content=_serialize_table(
                    table_name="customers",
                    database=database,
                    columns=[{"name": "id"}],
                ),
            )
        body = {"foreignKeys": [{"column": "col_a", "targetTable": "customers", "targetColumn": "id"}]}
        status, response = _parse(
            _dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )
        assert status == 400
        assert "Ambiguous" in response["error"]
        _review_env["smus"].create_asset_revision.assert_not_called()

    def test_foreign_key_unique_bare_target_in_other_database_is_valid(self, _review_env):
        _mock_single_asset_load(_review_env["smus"], table_id=self._TABLE_ID, form_content=_serialize_table())
        _mock_single_asset_load(
            _review_env["smus"],
            table_id="crm.customers",
            form_content=_serialize_table(
                table_name="customers",
                database="crm",
                columns=[{"name": "id"}],
            ),
        )
        body = {"foreignKeys": [{"column": "col_a", "targetTable": "customers", "targetColumn": "id"}]}
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))
        assert status == 200
        assert self._written_table(_review_env).foreign_keys[0].target_table == "customers"

    def test_foreign_key_bare_target_uses_schema_index_and_reuses_matched_asset(self, _review_env):
        smus = _review_env["smus"]
        _mock_single_asset_load(smus, table_id=self._TABLE_ID, form_content=_serialize_table())
        _mock_single_asset_load(
            smus,
            table_id="crm.customers",
            form_content=_serialize_table(
                table_name="customers",
                database="crm",
                columns=[{"name": "id"}],
            ),
        )

        body = {"foreignKeys": [{"column": "col_a", "targetTable": "customers", "targetColumn": "id"}]}
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))

        assert status == 200
        smus.search_assets.assert_not_called()
        names = [call.kwargs["name"] for call in smus.find_asset_by_name.call_args_list]
        assert names == [
            f"DS#{_SOURCE_ID}:{self._TABLE_ID}",
            f"DS#{_SOURCE_ID}:crm.customers",
            f"DS#{_SOURCE_ID}:sales.customers",
        ]
        assert names.count(f"DS#{_SOURCE_ID}:crm.customers") == 1
        assert smus.get_asset_forms.call_count == 2

    def test_foreign_key_bare_target_catalog_failure_returns_500(self, _review_env):
        smus = _review_env["smus"]
        _mock_single_asset_load(smus, table_id=self._TABLE_ID, form_content=_serialize_table())
        catalog = smus.__dict__["_coa_test_table_catalog"]

        def _find_asset(*, name, **_):
            if name.endswith(":crm.customers"):
                raise RuntimeError("catalog unavailable")
            return catalog[name][0] if name in catalog else None

        smus.find_asset_by_name.side_effect = _find_asset

        body = {"foreignKeys": [{"column": "col_a", "targetTable": "customers", "targetColumn": "id"}]}
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))

        assert status == 500
        smus.create_asset_revision.assert_not_called()

    def test_foreign_key_bare_target_without_schema_index_returns_500(self, _review_env):
        smus = _review_env["smus"]
        _review_env["dao"].get.return_value.pop("discoveredSchemas")
        _mock_single_asset_load(smus, table_id=self._TABLE_ID, form_content=_serialize_table())

        body = {"foreignKeys": [{"column": "col_a", "targetTable": "customers", "targetColumn": "id"}]}
        status, response = _parse(
            _dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID)
        )

        assert status == 500
        assert "schema index" in response["error"]
        smus.create_asset_revision.assert_not_called()

    def test_foreign_key_qualified_target_lookup_failure_returns_500(self, _review_env):
        smus = _review_env["smus"]
        _mock_single_asset_load(smus, table_id=self._TABLE_ID, form_content=_serialize_table())
        catalog = smus.__dict__["_coa_test_table_catalog"]

        def _find_asset(*, name, **_):
            if name == f"DS#{_SOURCE_ID}:sales.customers":
                raise RuntimeError("catalog unavailable")
            return catalog[name][0] if name in catalog else None

        smus.find_asset_by_name.side_effect = _find_asset
        body = {
            "foreignKeys": [
                {
                    "column": "col_a",
                    "targetTable": "sales.customers",
                    "targetColumn": "id",
                }
            ]
        }
        status, _ = _parse(_dr._handle_update_table_keys(self._event(body), _NAMESPACE_ID, _SOURCE_ID, self._TABLE_ID))

        assert status == 500
        smus.create_asset_revision.assert_not_called()


# ===================================================================
# _handle_list_scan_jobs — Scan History event list
# ===================================================================


@pytest.mark.unit
class TestHandleListScanJobs:
    def test_lists_newest_first_and_defaults_missing_event_type_to_scan(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        # Returned oldest-first on purpose: the handler must sort newest-first.
        mock_scan_dao.query_all.return_value = [
            {
                "PK": f"SRC#{_SOURCE_ID}",
                "SK": "2026-01-01T00:00:00Z",
                "status": "COMPLETED",
                "scanType": "full",
                "tablesDiscovered": 5,
                # No eventType — a legacy scan row; must default to SCAN.
            },
            {
                "PK": f"SRC#{_SOURCE_ID}",
                "SK": "2026-01-02T00:00:00Z",
                "eventType": "REVIEW",
                "decision": "APPROVED",
                "isRescan": False,
                "tablesApproved": 5,
                "status": "APPROVED",
            },
        ]

        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, body = _parse(_dr._handle_list_scan_jobs(_NAMESPACE_ID, _SOURCE_ID))

        assert status == 200
        items = body["items"]
        assert len(items) == 2
        # Newest (the REVIEW at 2026-01-02) first.
        assert items[0]["eventType"] == "REVIEW"
        assert items[0]["decision"] == "APPROVED"
        assert items[0]["isRescan"] is False
        assert items[0]["tablesApproved"] == 5
        # The legacy scan row defaults to SCAN and drops absent fields.
        assert items[1]["eventType"] == "SCAN"
        assert items[1]["tablesDiscovered"] == 5
        assert "decision" not in items[1]

    def test_list_scan_jobs_source_not_found(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = None
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
        ):
            status, _ = _parse(_dr._handle_list_scan_jobs(_NAMESPACE_ID, _SOURCE_ID))
        assert status == 404

    def test_list_scan_jobs_empty_returns_empty_items(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.query_all.return_value = []
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, body = _parse(_dr._handle_list_scan_jobs(_NAMESPACE_ID, _SOURCE_ID))
        assert status == 200
        assert body["items"] == []

    def test_list_scan_jobs_scan_dao_error_returns_500(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": f"SRC#{_SOURCE_ID}"}
        mock_scan_dao = MagicMock()
        mock_scan_dao.query_all.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "Query")
        with (
            patch(f"{_DR}._get_dao", return_value=mock_dao),
            patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        ):
            status, _ = _parse(_dr._handle_list_scan_jobs(_NAMESPACE_ID, _SOURCE_ID))
        assert status == 500
