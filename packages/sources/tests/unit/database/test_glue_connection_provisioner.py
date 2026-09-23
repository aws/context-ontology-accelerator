# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the managed Glue Data Catalog federated catalog provisioner."""

from unittest.mock import MagicMock, patch

# Imported for its side effect: mock.patch() resolves dotted targets by
# attribute traversal, so this submodule must be bound on its parent
# package before any patch(f"{MODULE}...") is evaluated.
import coa_sources.database.connectors.glue_connection_provisioner  # noqa: F401
import pytest
from botocore.exceptions import ClientError
from coa_common.constants import RESOURCE_PREFIX

MODULE = "coa_sources.database.connectors.glue_connection_provisioner"


class _AlreadyExists(Exception):
    """Stand-in for glue.exceptions.AlreadyExistsException."""


def _lf_already_exists():
    return ClientError({"Error": {"Code": "AlreadyExistsException", "Message": "x"}}, "RegisterResource")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("RESOURCE_PREFIX", f"{RESOURCE_PREFIX}-dev-")
    monkeypatch.setenv("ATHENA_SPILL_BUCKET", "my-spill-bucket")
    monkeypatch.setenv("FEDERATED_CATALOG_ROLE_ARN", "arn:aws:iam::123456789012:role/fed-catalog")


@pytest.fixture
def _mocks():
    mock_glue = MagicMock()
    mock_glue.exceptions.AlreadyExistsException = _AlreadyExists
    # The provisioner polls get_connection after create until the managed
    # connection reaches READY/FAILED. Default the mock to READY so the happy-path
    # tests don't spin on the poll loop.
    mock_glue.get_connection.return_value = {"Connection": {"Status": "READY"}}
    mock_lf = MagicMock()
    with (
        patch(f"{MODULE}._get_glue", return_value=mock_glue),
        patch(f"{MODULE}._get_lakeformation", return_value=mock_lf),
        patch(f"{MODULE}._get_account_id", return_value="123456789012"),
    ):
        yield {"glue": mock_glue, "lf": mock_lf}


@pytest.mark.unit
class TestProvisionFederatedCatalog:
    _KWARGS = {
        "datasource_id": "DS#abc-123",
        "engine": "POSTGRESQL",
        "host": "db.example.com",
        "port": 5432,
        "database_name": "mydb",
        "credential_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret-AbCdEf",
        "subnet_id": "subnet-abc",
        "security_group_id": "sg-123",
    }

    def test_creates_managed_connection_lf_and_catalog(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        result = provision_federated_catalog(**self._KWARGS)

        # Connection: managed (no Lambda), ROLE_ARN, VPC, secret.
        ci = _mocks["glue"].create_connection.call_args[1]["ConnectionInput"]
        assert ci["ConnectionType"] == "POSTGRESQL"
        assert ci["AthenaProperties"]["MANAGED_CONNECTION"] == "true"
        assert "lambda_function_arn" not in ci["AthenaProperties"]
        assert ci["ConnectionProperties"]["ROLE_ARN"] == "arn:aws:iam::123456789012:role/fed-catalog"
        assert ci["ConnectionProperties"]["HOST"] == "db.example.com"
        assert ci["AuthenticationConfiguration"]["SecretArn"] == self._KWARGS["credential_secret_arn"]
        assert "PhysicalConnectionRequirements" in ci

        # LF registration with federation.
        lf_call = _mocks["lf"].register_resource.call_args[1]
        assert lf_call["WithFederation"] is True
        assert lf_call["RoleArn"] == "arn:aws:iam::123456789012:role/fed-catalog"
        assert lf_call["ResourceArn"].endswith(f":connection/{result['athenaDataCatalogName']}")

        # Federated catalog (name == connection == identifier).
        cat = _mocks["glue"].create_catalog.call_args[1]
        name = result["athenaDataCatalogName"]
        assert cat["Name"] == name
        assert cat["CatalogInput"]["FederatedCatalog"] == {"ConnectionName": name, "Identifier": name}
        assert result["glueConnectionName"] == name

    def test_postgres_includes_casing_filter(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(**self._KWARGS)
        ci = _mocks["glue"].create_connection.call_args[1]["ConnectionInput"]
        assert ci["ConnectionType"] == "POSTGRESQL"
        assert ci["ConnectionProperties"]["CATALOG_CASING_FILTER"] == "LOWERCASE_ONLY"

    def test_redshift_omits_casing_filter(self, _mocks):
        # Glue's managed Redshift connector rejects CATALOG_CASING_FILTER.
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(**{**self._KWARGS, "engine": "REDSHIFT", "port": 5439})
        ci = _mocks["glue"].create_connection.call_args[1]["ConnectionInput"]
        assert ci["ConnectionType"] == "REDSHIFT"
        assert "CATALOG_CASING_FILTER" not in ci["ConnectionProperties"]
        # Other required properties are still present.
        assert ci["ConnectionProperties"]["HOST"] == "db.example.com"
        assert ci["ConnectionProperties"]["ROLE_ARN"] == "arn:aws:iam::123456789012:role/fed-catalog"

    def test_missing_role_arn_raises(self, _mocks, monkeypatch):
        monkeypatch.delenv("FEDERATED_CATALOG_ROLE_ARN", raising=False)
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        with pytest.raises(RuntimeError, match="FEDERATED_CATALOG_ROLE_ARN"):
            provision_federated_catalog(**self._KWARGS)
        _mocks["glue"].create_connection.assert_not_called()

    def test_idempotent_already_exists(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        _mocks["glue"].create_connection.side_effect = _AlreadyExists("x")
        _mocks["lf"].register_resource.side_effect = _lf_already_exists()
        _mocks["glue"].create_catalog.side_effect = _AlreadyExists("x")
        result = provision_federated_catalog(**self._KWARGS)
        assert result["athenaDataCatalogName"]
        _mocks["glue"].delete_connection.assert_not_called()

    def test_failed_connection_status_is_logged_not_fatal(self, _mocks, caplog):
        """A connection that never reaches READY is logged with its real StatusReason
        and provisioning CONTINUES.

        Deliberately non-fatal: a not-READY connection is an environment defect
        (VPC/SG/IAM), and failing the scan on it takes down every JDBC source in the
        account after discovery has already succeeded. The catalog-emptiness integ
        assertion is what fails loudly; this log line is the diagnostic.
        """
        import logging as _logging

        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        _mocks["glue"].get_connection.return_value = {
            "Connection": {
                "Status": "FAILED",
                "StatusReason": "Unable to access VPC provided in the connection.",
            }
        }
        with caplog.at_level(_logging.ERROR):
            result = provision_federated_catalog(**self._KWARGS)
        assert any("glue_connection_not_ready" in r.message for r in caplog.records)
        # Provisioning still completed — the catalog reference is returned.
        assert result["athenaDataCatalogName"]
        _mocks["glue"].delete_connection.assert_not_called()

    def test_catalog_failure_rolls_back_lf_and_connection(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        _mocks["glue"].create_catalog.side_effect = ClientError(
            {"Error": {"Code": "InternalServiceException", "Message": "bad"}}, "CreateCatalog"
        )
        with pytest.raises(RuntimeError, match="Failed to create federated catalog"):
            provision_federated_catalog(**self._KWARGS)
        _mocks["lf"].deregister_resource.assert_called_once()
        _mocks["glue"].delete_connection.assert_called_once()

    def test_no_vpc_config_when_subnet_not_provided(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(**{**self._KWARGS, "subnet_id": None, "security_group_id": None})
        ci = _mocks["glue"].create_connection.call_args[1]["ConnectionInput"]
        assert "PhysicalConnectionRequirements" not in ci

    def test_availability_zone_resolved_from_subnet(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        mock_ec2 = MagicMock()
        mock_ec2.describe_subnets.return_value = {"Subnets": [{"AvailabilityZone": "us-east-1a"}]}
        with patch(f"{MODULE}.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_ec2
            provision_federated_catalog(**self._KWARGS)

        ci = _mocks["glue"].create_connection.call_args[1]["ConnectionInput"]
        reqs = ci["PhysicalConnectionRequirements"]
        assert reqs["AvailabilityZone"] == "us-east-1a"
        assert reqs["SubnetId"] == "subnet-abc"
        assert reqs["SecurityGroupIdList"] == ["sg-123"]

    def test_invalid_inputs_rejected_before_aws(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        for bad in (
            {"engine": "UNSUPPORTED"},
            {"engine": 1},
            {"host": "x?y&"},
            {"port": 70000},
            {"database_name": "d;DROP"},
        ):
            with pytest.raises(RuntimeError):
                provision_federated_catalog(**{**self._KWARGS, **bad})
        _mocks["glue"].create_connection.assert_not_called()

    @pytest.mark.parametrize("host", ["localhost", "database1"])
    def test_non_snowflake_accepts_single_label_hostname(self, _mocks, host):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(**{**self._KWARGS, "host": host})

        connection = _mocks["glue"].create_connection.call_args.kwargs["ConnectionInput"]
        assert connection["ConnectionProperties"]["HOST"] == host

    @pytest.mark.parametrize(
        "host",
        [
            "my_account.snowflakecomputing.com",
            "acme-marketing_test_account.snowflakecomputing.com",
            "acme-account.privatelink.snowflakecomputing.com",
            "mydb_instance",
            "192.0.2.10",
        ],
    )
    def test_snowflake_accepts_documented_account_hosts_and_ipv4(self, _mocks, host):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(
            **{
                **self._KWARGS,
                "engine": "SNOWFLAKE",
                "host": host,
                "port": 443,
                "warehouse": "COMPUTE_WH",
            }
        )

        connection = _mocks["glue"].create_connection.call_args.kwargs["ConnectionInput"]
        assert connection["ConnectionProperties"]["HOST"] == host

    def test_non_snowflake_still_rejects_underscore_hostname(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        with pytest.raises(RuntimeError, match="only permitted in Snowflake account hostnames"):
            provision_federated_catalog(
                **{
                    **self._KWARGS,
                    "engine": "POSTGRESQL",
                    "host": "db_account.example.com",
                }
            )
        _mocks["glue"].create_connection.assert_not_called()

    @pytest.mark.parametrize(
        "host",
        [
            "https://my_account.snowflakecomputing.com",
            "my_account.snowflakecomputing.com:443",
            "my_account.snowflakecomputing.com/path",
            "my account.snowflakecomputing.com",
            "my_account..snowflakecomputing.com",
            "my_account_.snowflakecomputing.com",
            "my_account.snow_flakecomputing.com",
            "my_account._privatelink.snowflakecomputing.com",
            f"{'a' * 62}_a.snowflakecomputing.com",
            "999.999.1.1",
        ],
    )
    def test_snowflake_rejects_malformed_hosts(self, _mocks, host):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        with pytest.raises(RuntimeError):
            provision_federated_catalog(
                **{
                    **self._KWARGS,
                    "engine": "SNOWFLAKE",
                    "host": host,
                    "port": 443,
                    "warehouse": "COMPUTE_WH",
                }
            )
        _mocks["glue"].create_connection.assert_not_called()

    def test_grants_lf_iam_allowed_principals_on_schema(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        result = provision_federated_catalog(**self._KWARGS)
        name = result["athenaDataCatalogName"]
        _mocks["lf"].grant_permissions.assert_called_once_with(
            Principal={"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
            Resource={"Database": {"CatalogId": name, "Name": "public"}},
            Permissions=["ALL"],
        )

    def test_custom_schema_name_used_in_lf_grant(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        result = provision_federated_catalog(**{**self._KWARGS, "public_schema_name": "analytics"})
        name = result["athenaDataCatalogName"]
        _mocks["lf"].grant_permissions.assert_called_once_with(
            Principal={"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
            Resource={"Database": {"CatalogId": name, "Name": "analytics"}},
            Permissions=["ALL"],
        )

    def test_lf_grant_failure_raises(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        _mocks["lf"].grant_permissions.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GrantPermissions"
        )
        with pytest.raises(RuntimeError, match="Failed to grant LF permissions"):
            provision_federated_catalog(**self._KWARGS)

    def test_lf_grant_already_exists_is_silent(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        _mocks["lf"].grant_permissions.side_effect = ClientError(
            {"Error": {"Code": "AlreadyExistsException", "Message": "exists"}}, "GrantPermissions"
        )
        result = provision_federated_catalog(**self._KWARGS)
        assert result["athenaDataCatalogName"]


@pytest.mark.unit
class TestCatalogNaming:
    def test_distinct_ids_distinct_names(self):
        from coa_sources.database.connectors.glue_connection_provisioner import build_catalog_name

        assert build_catalog_name(f"{RESOURCE_PREFIX}-dev-", "ds-1") != build_catalog_name(
            f"{RESOURCE_PREFIX}-dev-", "ds_1"
        )

    def test_name_is_lowercase_and_bounded(self):
        from coa_sources.database.connectors.glue_connection_provisioner import build_catalog_name

        n = build_catalog_name(f"{RESOURCE_PREFIX}-dev-", "abc-123")
        assert n == build_catalog_name(f"{RESOURCE_PREFIX}-dev-", "abc-123")
        assert n[0].isalpha() and n == n.lower() and len(n) <= 41
        assert all(c.isalnum() or c == "_" for c in n)


@pytest.mark.unit
class TestCleanup:
    def test_deletes_catalog_lf_and_connection(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            cleanup_federated_resources,
        )

        cleanup_federated_resources(glue_connection_name="scldevds_abc", athena_catalog_name="scldevds_abc")
        _mocks["glue"].delete_catalog.assert_called_once()
        _mocks["lf"].deregister_resource.assert_called_once()
        _mocks["glue"].delete_connection.assert_called_once()

    def test_no_op_when_names_none(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            cleanup_federated_resources,
        )

        cleanup_federated_resources(glue_connection_name=None, athena_catalog_name=None)
        _mocks["glue"].delete_connection.assert_not_called()

    def test_swallows_errors(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            cleanup_federated_resources,
        )

        _mocks["glue"].delete_catalog.side_effect = Exception("boom")
        _mocks["lf"].deregister_resource.side_effect = Exception("boom")
        _mocks["glue"].delete_connection.side_effect = Exception("boom")
        cleanup_federated_resources(athena_catalog_name="scldevds_abc")  # no raise

    def test_uses_session_clients_when_provided(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            cleanup_federated_resources,
        )

        sess_glue = MagicMock()
        sess_lf = MagicMock()
        session = MagicMock()
        session.client.side_effect = lambda svc, **kw: {"glue": sess_glue, "lakeformation": sess_lf}[svc]

        cleanup_federated_resources(athena_catalog_name="scldevds_abc", session=session)

        # Teardown must run on the assumed-role session's clients, not the defaults.
        sess_glue.delete_catalog.assert_called_once()
        sess_lf.deregister_resource.assert_called_once()
        sess_glue.delete_connection.assert_called_once()
        _mocks["glue"].delete_catalog.assert_not_called()


@pytest.mark.unit
class TestGetAccountId:
    def test_raises_when_sts_returns_no_account(self):
        import coa_sources.database.connectors.glue_connection_provisioner as mod

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {}  # no Account
        mod._account_id = None
        mod._sts_client = None
        with patch(f"{MODULE}.boto3.client", return_value=mock_sts), pytest.raises(RuntimeError, match="no Account"):
            mod._get_account_id()
        mod._sts_client = None

    def test_returns_account_and_caches(self):
        import coa_sources.database.connectors.glue_connection_provisioner as mod

        mock_sts = MagicMock()
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}
        mod._account_id = None
        mod._sts_client = None
        with patch(f"{MODULE}.boto3.client", return_value=mock_sts) as mk:
            assert mod._get_account_id() == "123456789012"
            assert mod._get_account_id() == "123456789012"  # cached
        mk.assert_called_once()  # STS client built once
        mod._account_id = None
        mod._sts_client = None


@pytest.mark.unit
class TestGrantConsumerSelect:
    def test_grants_select_and_describe_per_schema(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select,
        )

        result = grant_consumer_select(
            catalog_name="scldevds_abc",
            schemas=["public", "sales"],
            principal_arn="arn:aws:iam::123456789012:role/consumer",
        )

        assert result is True
        # Two grants (Database DESCRIBE + Table SELECT/DESCRIBE) per schema.
        assert _mocks["lf"].grant_permissions.call_count == 4
        kwargs = [c.kwargs for c in _mocks["lf"].grant_permissions.call_args_list]
        # Nested catalog id is "<account>:<catalogName>".
        assert all(list(k["Resource"].values())[0]["CatalogId"] == "123456789012:scldevds_abc" for k in kwargs)
        table_grant = next(k for k in kwargs if "Table" in k["Resource"])
        assert table_grant["Permissions"] == ["SELECT", "DESCRIBE"]
        assert table_grant["Resource"]["Table"]["TableWildcard"] == {}

    def test_noop_without_principal_or_schemas(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select,
        )

        assert grant_consumer_select(catalog_name="c", schemas=[], principal_arn="arn:aws:iam::1:role/x") is False
        assert grant_consumer_select(catalog_name="c", schemas=["public"], principal_arn="") is False
        _mocks["lf"].grant_permissions.assert_not_called()

    def test_grant_failure_is_swallowed(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select,
        )

        _mocks["lf"].grant_permissions.side_effect = ClientError(
            {"Error": {"Code": "InvalidInputException", "Message": "bad"}}, "GrantPermissions"
        )
        # Best-effort: must not raise, and reports failure so queryable stays False.
        assert (
            grant_consumer_select(catalog_name="c", schemas=["public"], principal_arn="arn:aws:iam::1:role/x") is False
        )


@pytest.mark.unit
@pytest.mark.unit
class TestGrantIamAllowedPrincipals:
    """Regression cover: tables inherit access from a database-level
    IAM_ALLOWED_PRINCIPALS grant, and that grant must target the DISCOVERED schemas.

    Granting only the hardcoded `public_schema_name` governed nothing on engines
    without a "public" database (MySQL, SQL Server, Oracle): LF accepts a grant
    naming a non-existent database, so `GetDatabases` returned an empty list for
    every non-LF-admin caller on a catalog that resolved fine.
    """

    def test_grants_all_per_discovered_schema(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_iam_allowed_principals,
        )

        result = grant_iam_allowed_principals(catalog_name="scldevds_abc", schemas=["integration_test", "dbo"])

        assert result is True
        assert _mocks["lf"].grant_permissions.call_count == 2
        kwargs = [c.kwargs for c in _mocks["lf"].grant_permissions.call_args_list]
        assert {k["Resource"]["Database"]["Name"] for k in kwargs} == {"integration_test", "dbo"}
        assert all(k["Principal"]["DataLakePrincipalIdentifier"] == "IAM_ALLOWED_PRINCIPALS" for k in kwargs)
        assert all(k["Permissions"] == ["ALL"] for k in kwargs)
        # Nested catalog id is "<account>:<catalogName>".
        assert all(k["Resource"]["Database"]["CatalogId"] == "123456789012:scldevds_abc" for k in kwargs)

    def test_noop_without_schemas(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_iam_allowed_principals,
        )

        assert grant_iam_allowed_principals(catalog_name="scldevds_abc", schemas=[]) is False
        _mocks["lf"].grant_permissions.assert_not_called()


@pytest.mark.unit
class TestGrantConsumerSelectNative:
    """Tests for grant_consumer_select_native (GLUE_DATABASE / strict-LF accounts).

    Regression coverage for: accounts with IAM_ALLOWED_PRINCIPALS removed receive
    PERMISSION_DENIED from Athena when querying native Glue tables because no LF
    data permission was ever granted to the serve runtime role.
    """

    def test_grants_describe_and_select_on_native_database(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select_native,
        )

        result = grant_consumer_select_native(
            database_name="retail_demo",
            principal_arn="arn:aws:iam::123456789012:role/serve-role",
        )

        assert result is True
        # Two grant_permissions calls: Database DESCRIBE + Table SELECT/DESCRIBE.
        assert _mocks["lf"].grant_permissions.call_count == 2
        kwargs = [c.kwargs for c in _mocks["lf"].grant_permissions.call_args_list]
        # Native catalog: CatalogId is the bare account ID (not "account:catalog").
        assert all(list(k["Resource"].values())[0]["CatalogId"] == "123456789012" for k in kwargs)
        db_grant = next(k for k in kwargs if "Database" in k["Resource"])
        assert db_grant["Resource"]["Database"]["Name"] == "retail_demo"
        assert db_grant["Permissions"] == ["DESCRIBE"]
        table_grant = next(k for k in kwargs if "Table" in k["Resource"])
        assert table_grant["Resource"]["Table"]["DatabaseName"] == "retail_demo"
        assert table_grant["Permissions"] == ["SELECT", "DESCRIBE"]
        assert table_grant["Resource"]["Table"]["TableWildcard"] == {}

    def test_noop_without_principal_or_database(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select_native,
        )

        assert grant_consumer_select_native(database_name="", principal_arn="arn:aws:iam::1:role/x") is False
        assert grant_consumer_select_native(database_name="mydb", principal_arn="") is False
        # Invalid ARN format (not a role ARN) → rejected
        assert grant_consumer_select_native(database_name="mydb", principal_arn="not-an-arn") is False
        # Invalid database name (contains spaces/special chars) → rejected
        assert grant_consumer_select_native(database_name="bad name!", principal_arn="arn:aws:iam::1:role/x") is False
        _mocks["lf"].grant_permissions.assert_not_called()

    def test_already_exists_both_grants_treated_as_success(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select_native,
        )

        _mocks["lf"].grant_permissions.side_effect = ClientError(
            {"Error": {"Code": "AlreadyExistsException", "Message": "already exists"}},
            "GrantPermissions",
        )
        # Both grants already exist → idempotent success, both calls still made.
        assert grant_consumer_select_native(database_name="mydb", principal_arn="arn:aws:iam::1:role/x") is True
        assert _mocks["lf"].grant_permissions.call_count == 2

    def test_invalid_input_exception_treated_as_idempotent_success(self, _mocks):
        """LF raises InvalidInputException for duplicate grants in some configurations.

        Regression: this was not treated as idempotent, causing re-scans to return
        False and leave sources non-queryable even though the grant existed.
        """
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select_native,
        )

        _mocks["lf"].grant_permissions.side_effect = ClientError(
            {"Error": {"Code": "InvalidInputException", "Message": "already granted"}},
            "GrantPermissions",
        )
        assert grant_consumer_select_native(database_name="mydb", principal_arn="arn:aws:iam::1:role/x") is True
        assert _mocks["lf"].grant_permissions.call_count == 2

    def test_db_already_exists_still_makes_table_grant(self, _mocks):
        """Regression: DB DESCRIBE grant already exists must not skip the Table SELECT grant.

        Previously both grants shared one try/except — AlreadyExistsException on the DB grant
        caused early return, leaving the Table SELECT grant unmade. Each grant is now
        independent so partial idempotency is handled correctly.
        """
        from botocore.exceptions import ClientError as BotoClientError
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select_native,
        )

        already_exists = BotoClientError(
            {"Error": {"Code": "AlreadyExistsException", "Message": "x"}}, "GrantPermissions"
        )
        # First call (DB DESCRIBE) → AlreadyExists; second call (Table SELECT) → success.
        _mocks["lf"].grant_permissions.side_effect = [already_exists, None]

        result = grant_consumer_select_native(database_name="mydb", principal_arn="arn:aws:iam::1:role/x")

        assert result is True
        # Both calls MUST have been made — the second is not skipped.
        assert _mocks["lf"].grant_permissions.call_count == 2

    def test_other_lf_error_is_swallowed(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            grant_consumer_select_native,
        )

        _mocks["lf"].grant_permissions.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GrantPermissions",
        )
        # Best-effort: must not raise; returns False so queryable stays as-is.
        assert grant_consumer_select_native(database_name="mydb", principal_arn="arn:aws:iam::1:role/x") is False


@pytest.mark.unit
class TestSnowflakeWarehouse:
    """Snowflake needs WAREHOUSE in the Glue connection properties.

    Glue enforces this at CreateConnection time, not at query time:
      InvalidInputException: [WAREHOUSE are missing in the request object]
    Since federation failure is fatal for JDBC sources, omitting it turned a
    fully-successful discovery (all 8 tables found) into SCAN_FAILED.
    """

    _BASE = {
        "datasource_id": "DS#snow-1",
        "engine": "SNOWFLAKE",
        "host": "myorg-myacct.snowflakecomputing.com",
        "port": 443,
        "database_name": "SNOWFLAKE_SAMPLE_DATA",
        "credential_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:s-AbCdEf",
    }

    @staticmethod
    def _conn_props(mock_glue):
        return mock_glue.create_connection.call_args[1]["ConnectionInput"]["ConnectionProperties"]

    def test_warehouse_is_passed_to_glue(self, _mocks):
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(**self._BASE, warehouse="COMPUTE_WH")
        assert self._conn_props(_mocks["glue"])["WAREHOUSE"] == "COMPUTE_WH"

    def test_casing_filter_not_sent_for_snowflake(self, _mocks):
        """LOWERCASE_ONLY makes a Snowflake federated catalog expose nothing.

        Snowflake stores unquoted identifiers UPPERCASE, so the filter excludes
        every object. Glue accepts the property and the connection reaches READY,
        so the only symptom is a catalog that resolves but is empty — surfacing
        much later as Athena TABLE_NOT_FOUND at serve time. Verified against a
        live account: 0 databases with the filter, all 7 schemas without it.
        """
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(**self._BASE, warehouse="COMPUTE_WH")
        assert "CATALOG_CASING_FILTER" not in self._conn_props(_mocks["glue"])

    def test_casing_filter_not_sent_for_any_uppercase_native_engine(self, _mocks):
        """The rule is engine casing convention, not one vendor.

        Oracle folds unquoted identifiers to UPPERCASE like Snowflake, and
        onboarding a real instance reproduced the same symptom: READY connection,
        catalog with 0 databases. PostgreSQL/MySQL are lowercase-native and were
        verified unaffected (byte-identical table lists with and without it).
        """
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        base = {k: v for k, v in self._BASE.items() if k != "engine"}
        for engine in ("ORACLE", "REDSHIFT", "SNOWFLAKE"):
            _mocks["glue"].reset_mock()
            kwargs = dict(base, engine=engine)
            if engine == "SNOWFLAKE":
                kwargs["warehouse"] = "COMPUTE_WH"
            provision_federated_catalog(**kwargs)
            assert "CATALOG_CASING_FILTER" not in self._conn_props(_mocks["glue"]), engine

    def test_role_is_never_sent_to_glue(self, _mocks):
        """Glue rejects a ROLE connection property on the Snowflake connector.

        Observed live: InvalidInputException: [ROLE are not allowed in the
        request object.] The connector session runs as the secret user's
        DEFAULT_ROLE instead, so nothing may set ROLE here even though
        jdbcConfiguration.role is valid for the discovery driver.
        """
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(**self._BASE, warehouse="COMPUTE_WH")
        assert "ROLE" not in self._conn_props(_mocks["glue"])

    def test_missing_warehouse_fails_with_actionable_message(self, _mocks):
        """Fail before calling Glue, with a message naming the field to set."""
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        with pytest.raises(RuntimeError, match="jdbcConfiguration.warehouse"):
            provision_federated_catalog(**self._BASE)
        _mocks["glue"].create_connection.assert_not_called()

    def test_non_snowflake_engines_get_no_warehouse_property(self, _mocks):
        """The property is engine-specific; other connectors reject unknown keys."""
        from coa_sources.database.connectors.glue_connection_provisioner import (
            provision_federated_catalog,
        )

        provision_federated_catalog(
            datasource_id="DS#pg-1",
            engine="POSTGRESQL",
            host="db.example.com",
            port=5432,
            database_name="mydb",
            credential_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:s-AbCdEf",
            warehouse="COMPUTE_WH",
        )
        props = self._conn_props(_mocks["glue"])
        assert "WAREHOUSE" not in props
        assert "ROLE" not in props
