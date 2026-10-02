# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Databricks connector's configuration store and naming rules.

What the store must guarantee: the environment-scoped path, the refusal to overwrite, the
deployment-id derivation the Java side mirrors, and that nothing credential-shaped is ever
written into a plaintext parameter.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from coa_sources.database import databricks as dbx

pytestmark = pytest.mark.unit

_PREFIX = "/coa/dev/connectors/databricks/sources"

# The LIVE cross-language contract for one connector configuration parameter: without a
# shared artifact each side pins its own key list, so either could rename a field and both
# suites would stay green while every query failed.
#
# `SsmConnectionConfigProviderTest` parses a byte-identical copy at
# `connectors/databricks/src/test/resources/databricks_connector_parameter.golden.json`
# and asserts byte equality against THIS file. Two files rather than one shared path
# because `connectors/` is a standalone workspace customers copy out.
#
# MAINTENANCE: a change here is a change to BOTH files.
_GOLDEN_PATH = pathlib.Path(__file__).parents[2] / "fixtures" / "databricks_connector_parameter.golden.json"
_ARN_PARAM = "/coa/dev/connectors/databricks/deployment/function-arn"
_CONNECTOR_ARN = "arn:aws:lambda:us-east-1:111122223333:function:coa-dev-databricks-connector"


def _client_error(code: str, op: str = "PutParameter") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _config(**overrides) -> dbx.DatabricksConnectorConfig:
    values = {
        "source_id": "src-1",
        "namespace_id": "ns-1",
        "deployment_id": "coa-dev",
        "workspace_hostname": "dbc-a1b2345c-d6e7.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/a1b234c567d8e9fa",
        "databricks_catalog": "main",
        "database_name": "sales",
        "credential_secret_arn": "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-AbCdEf",
        "cross_account_role_arn": "arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx",
    }
    values.update(overrides)
    return dbx.DatabricksConnectorConfig(**values)


class TestDeploymentId:
    def test_strips_trailing_hyphens_from_the_resource_prefix(self, monkeypatch):
        """One rule and only one, shared with the connector's CDK app, which applies it to
        the resource prefix when setting ``COA_DEPLOYMENT_ID``. The jar compares that
        against the parameter's ``deploymentId`` with ``equals``, so a second rule on
        either side breaks every request for every source."""
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev-")
        assert dbx.deployment_id() == "coa-dev"
        monkeypatch.setenv("RESOURCE_PREFIX", "scl-prod-")
        assert dbx.deployment_id() == "scl-prod"

    def test_a_prefix_with_no_trailing_hyphen_is_unchanged(self):
        with patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev"}):
            assert dbx.deployment_id() == "coa-dev"


class TestReservedRoleName:
    """COA's ``sts:AssumeRole`` grant is scoped to
    ``arn:aws:iam::*:role/{RESOURCE_PREFIX}datasource-access-*``.

    Account-agnostic on purpose, so the NAME is what bounds which roles COA will assume,
    alongside the target role's own trust policy.
    """

    def test_the_prefix_is_derived_from_the_deployment_prefix(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "scl-prod-")
        assert dbx.reserved_role_name_prefix() == "scl-prod-datasource-access-"

    @pytest.mark.parametrize(
        ("arn", "expected"),
        [
            ("arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx", True),
            ("arn:aws:iam::111122223333:role/coa-dev-datasource-access-dbx", True),
            ("arn:aws:iam::222233334444:role/coa-dev-datasource-access-", True),
            ("arn:aws:iam::222233334444:role/my-reader", False),
            ("arn:aws:iam::222233334444:role/xcoa-dev-datasource-access-dbx", False),
            ("arn:aws:iam::222233334444:role/prod-datasource-access-dbx", False),
        ],
    )
    def test_membership(self, arn, expected):
        with patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"}):
            assert dbx.role_name_is_reserved(arn) is expected

    def test_a_role_under_an_iam_path_is_refused(self):
        """The grant matches the WHOLE resource portion, so
        ``role/team/finance/coa-dev-datasource-access-dbx`` does not match
        ``role/coa-dev-datasource-access-*`` however its trailing name is spelled.
        """
        arn = "arn:aws:iam::222233334444:role/team/finance/coa-dev-datasource-access-dbx"
        with patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"}):
            assert dbx.role_path_and_name(arn) == "team/finance/coa-dev-datasource-access-dbx"
            assert dbx.role_name_is_reserved(arn) is False

    def test_a_role_with_no_path_is_accepted(self):
        """An unpathed role's resource portion IS its name, so IAM matches it too."""
        arn = "arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx"
        with patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"}):
            assert dbx.role_path_and_name(arn) == "coa-dev-datasource-access-dbx"
            assert dbx.role_name_is_reserved(arn) is True

    def test_the_account_is_never_part_of_the_decision(self):
        """What bounds COA is the reserved prefix plus the target role's own trust
        policy, which ``sts:AssumeRole`` always consults."""
        deployment = "arn:aws:iam::111122223333:role/coa-dev-datasource-access-dbx"
        foreign = "arn:aws:iam::999988887777:role/coa-dev-datasource-access-dbx"
        with patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"}):
            assert dbx.role_name_is_reserved(deployment) == dbx.role_name_is_reserved(foreign) is True


class TestConfigParameterName:
    def test_the_path_carries_the_environment(self):
        """Environments share an account, so a path without ``${envName}`` would let a dev
        sources-API role repoint a prod source's parameter."""
        with patch.object(dbx, "CONFIG_SSM_PREFIX", _PREFIX):
            name = dbx.config_parameter_name("coadevds_abc123")
        assert name == f"{_PREFIX}/coadevds_abc123"
        assert "/dev/" in name

    def test_a_trailing_slash_on_the_prefix_does_not_double_up(self):
        with patch.object(dbx, "CONFIG_SSM_PREFIX", _PREFIX + "/"):
            assert dbx.config_parameter_name("cat") == f"{_PREFIX}/cat"

    def test_an_unset_prefix_raises_rather_than_writing_to_a_relative_path(self):
        with patch.object(dbx, "CONFIG_SSM_PREFIX", ""), pytest.raises(dbx.DatabricksConfigError):
            dbx.config_parameter_name("cat")


class TestConnectorArnResolution:
    """A missing connector fails the create at SUBMIT: discovery for this sub-type runs
    through the connector, so a source registered without one could never reach review.
    """

    def test_resolves_the_arn_from_the_named_parameter(self):
        client = MagicMock()
        client.get_parameter.return_value = {"Parameter": {"Value": _CONNECTOR_ARN}}
        with patch.object(dbx, "CONNECTOR_ARN_SSM_PARAM", _ARN_PARAM):
            assert dbx.resolve_connector_function_arn(client=client) == _CONNECTOR_ARN
        client.get_parameter.assert_called_once_with(Name=_ARN_PARAM)

    def test_an_unconfigured_parameter_name_is_the_same_condition_as_an_absent_one(self):
        """To a caller both mean "no connector is deployed here", so both raise the same
        type rather than one of them being an internal error."""
        with patch.object(dbx, "CONNECTOR_ARN_SSM_PARAM", ""), pytest.raises(dbx.DatabricksConnectorUnavailableError):
            dbx.resolve_connector_function_arn(client=MagicMock())

        client = MagicMock()
        client.get_parameter.side_effect = _client_error("ParameterNotFound", "GetParameter")
        with (
            patch.object(dbx, "CONNECTOR_ARN_SSM_PARAM", _ARN_PARAM),
            pytest.raises(dbx.DatabricksConnectorUnavailableError, match=_ARN_PARAM),
        ):
            dbx.resolve_connector_function_arn(client=client)

    def test_an_empty_value_is_also_no_connector(self):
        client = MagicMock()
        client.get_parameter.return_value = {"Parameter": {"Value": "   "}}
        with (
            patch.object(dbx, "CONNECTOR_ARN_SSM_PARAM", _ARN_PARAM),
            pytest.raises(dbx.DatabricksConnectorUnavailableError),
        ):
            dbx.resolve_connector_function_arn(client=client)

    def test_an_access_failure_is_not_reported_as_a_missing_connector(self):
        """Reporting a denied read as "deploy the connector" would send an operator to
        build something that already exists."""
        client = MagicMock()
        client.get_parameter.side_effect = _client_error("AccessDeniedException", "GetParameter")
        with patch.object(dbx, "CONNECTOR_ARN_SSM_PARAM", _ARN_PARAM), pytest.raises(dbx.DatabricksConfigError):
            dbx.resolve_connector_function_arn(client=client)


class TestParameterWrite:
    def test_writes_a_plaintext_string_and_refuses_to_overwrite(self):
        client = MagicMock()
        with patch.object(dbx, "CONFIG_SSM_PREFIX", _PREFIX):
            name = dbx.write_config_parameter(catalog_name="coadevds_abc", config=_config(), client=client)
        assert name == f"{_PREFIX}/coadevds_abc"
        kwargs = client.put_parameter.call_args.kwargs
        # String, not SecureString: nothing in the payload is secret, and the threat is
        # INTEGRITY, which SecureString does not address.
        assert kwargs["Type"] == "String"
        # A collision means an assumption has broken, so it must not be reconciled.
        assert kwargs["Overwrite"] is False

    def test_the_body_matches_the_golden_fixture_key_for_key(self):
        """Key-for-key against the shared golden body, which is one half of a live
        two-sided contract: a Java-side rename fails the connector's own byte-equality test
        against this same file.
        """
        golden = json.loads(_GOLDEN_PATH.read_text())
        produced = json.loads(
            _config(
                source_id=golden["sourceId"],
                namespace_id=golden["namespaceId"],
                deployment_id=golden["deploymentId"],
                workspace_hostname=golden["workspaceHostname"],
                http_path=golden["httpPath"],
                databricks_catalog=golden["databricksCatalog"],
                database_name=golden["databaseName"],
                credential_secret_arn=golden["credentialSecretArn"],
                cross_account_role_arn=golden["crossAccountRoleArn"],
            ).to_parameter_value()
        )
        assert set(produced) == set(golden), (
            f"parameter keys drifted from {_GOLDEN_PATH.name}: "
            f"only in Python {sorted(set(produced) - set(golden))}, "
            f"only in the golden file {sorted(set(golden) - set(produced))}. "
            f"The connector parses these keys from a BYTE-IDENTICAL copy at "
            f"connectors/databricks/src/test/resources/{_GOLDEN_PATH.name}, and its "
            f"theGoldenFixtureIsTheSameFileThePythonSuiteReads test asserts the two files are "
            f"equal — so change BOTH copies together, or that test fails too."
        )
        for key in golden:
            assert produced[key] == golden[key], key

    def test_the_body_is_camel_case_json_the_connector_can_parse(self):
        client = MagicMock()
        with patch.object(dbx, "CONFIG_SSM_PREFIX", _PREFIX):
            dbx.write_config_parameter(catalog_name="cat", config=_config(), client=client)
        body = json.loads(client.put_parameter.call_args.kwargs["Value"])
        assert set(body) == {
            "sourceId",
            "namespaceId",
            "deploymentId",
            "workspaceHostname",
            "httpPath",
            "databricksCatalog",
            "databaseName",
            "credentialSecretArn",
            "crossAccountRoleArn",
        }

    def test_the_body_stays_well_inside_the_standard_tier_limit(self):
        """4 KB is the standard-tier ceiling; exceeding it forces the advanced tier, which
        is charged per parameter per month."""
        assert len(_config().to_parameter_value().encode()) < 4096

    def test_a_collision_is_its_own_error_type(self):
        client = MagicMock()
        client.put_parameter.side_effect = _client_error("ParameterAlreadyExists")
        with patch.object(dbx, "CONFIG_SSM_PREFIX", _PREFIX), pytest.raises(dbx.DatabricksConfigExistsError):
            dbx.write_config_parameter(catalog_name="cat", config=_config(), client=client)

    @pytest.mark.parametrize(
        "error",
        [_client_error("ThrottlingException"), EndpointConnectionError(endpoint_url="https://ssm")],
    )
    def test_any_other_failure_raises_the_base_error(self, error):
        client = MagicMock()
        client.put_parameter.side_effect = error
        with patch.object(dbx, "CONFIG_SSM_PREFIX", _PREFIX), pytest.raises(dbx.DatabricksConfigError) as exc:
            dbx.write_config_parameter(catalog_name="cat", config=_config(), client=client)
        assert not isinstance(exc.value, dbx.DatabricksConfigExistsError)


class TestParameterDelete:
    def test_deletes_by_name(self):
        client = MagicMock()
        dbx.delete_config_parameter(parameter_name=f"{_PREFIX}/cat", client=client)
        client.delete_parameter.assert_called_once_with(Name=f"{_PREFIX}/cat")

    def test_an_already_absent_parameter_is_success(self):
        """So a retried delete converges instead of stalling on a 404 for work that is
        already done."""
        client = MagicMock()
        client.delete_parameter.side_effect = _client_error("ParameterNotFound", "DeleteParameter")
        dbx.delete_config_parameter(parameter_name=f"{_PREFIX}/cat", client=client)

    def test_a_real_failure_raises(self):
        """The parameter must not outlive the source record, which is the only handle on
        its name."""
        client = MagicMock()
        client.delete_parameter.side_effect = _client_error("AccessDeniedException", "DeleteParameter")
        with pytest.raises(dbx.DatabricksConfigError):
            dbx.delete_config_parameter(parameter_name=f"{_PREFIX}/cat", client=client)


class TestArnNormalisation:
    """Python's ``$`` matches BEFORE a trailing newline, so the generated Smithy
    validators accept an ARN with one. The Java connector trims where COA does not, so it
    would assume the canonical role while COA recorded and proved the wiring of the other.
    """

    def test_the_generated_validator_really_does_accept_a_trailing_newline(self):
        """Pinned so a regeneration that tightens the pattern makes the normalisation
        provably redundant rather than silently so."""
        from coa_control_plane_server.models.databricks_sql_warehouse_configuration import (
            DatabricksSqlWarehouseConfiguration,
        )

        model = DatabricksSqlWarehouseConfiguration.model_validate(
            {
                "workspaceHostname": "dbc-a1b2345c-d6e7.cloud.databricks.com",
                "httpPath": "/sql/1.0/warehouses/a1b234c567d8e9fa",
                "databricksCatalog": "main",
                "databaseName": "sales",
                "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-AbCdEf",
                "crossAccountRoleArn": "arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx\n",
            }
        )
        assert model.cross_account_role_arn.endswith("\n")

    @pytest.mark.parametrize("suffix", ["\n", "\r\n", " ", "\t"])
    def test_surrounding_whitespace_is_stripped(self, suffix):
        arn = "arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx"
        assert dbx.normalise_arn(arn + suffix) == arn
        assert dbx.normalise_arn(suffix + arn) == arn

    def test_a_none_or_empty_value_normalises_to_the_empty_string(self):
        assert dbx.normalise_arn(None) == ""
        assert dbx.normalise_arn("   ") == ""

    def test_the_prefix_check_passes_on_the_raw_value_so_it_catches_nothing(self):
        """The prefix check is satisfied by both spellings, so it cannot be what keeps the
        two sides agreeing about which role was registered."""
        canonical = "arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx"
        with patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"}):
            assert dbx.role_name_is_reserved(canonical + "\n") is True
        assert dbx.normalise_arn(canonical + "\n") == canonical

    def test_whitespace_surviving_the_strip_is_detected(self):
        assert dbx.arn_has_inner_whitespace("arn:aws:iam::222233334444:role/coa dev") is True
        assert dbx.arn_has_inner_whitespace("arn:aws:iam::222233334444:role/coa-dev") is False


class TestConnectorRoleArnShape:
    """Same class of gap as the secret ARN, on the other ARN: the shared ``IamRoleArn``
    shape permits an IAM path admitting ``;``, ``%`` and quote characters, while the
    connector's ``ManagedSource.IAM_ROLE_ARN`` does not. Such an ARN clears BOTH the
    reserved-prefix check and the IAM wildcard, then fails every query on the connector's
    re-validation.
    """

    @pytest.mark.parametrize(
        "resource",
        ["coa-dev-datasource-access-dbx", "team/coa-dev-datasource-access-dbx", "a+b=c,d.e@f_g-h"],
    )
    def test_accepts_the_connectors_own_charset(self, resource):
        assert dbx.role_arn_is_connector_assumable(f"arn:aws:iam::222233334444:role/{resource}")

    @pytest.mark.parametrize(
        "resource",
        [
            "coa-dev-datasource-access-a/x;y/name",
            "coa-dev-datasource-access-a/x%y/name",
            'coa-dev-datasource-access-a/x"y/name',
            "coa-dev-datasource-access-a/x y/name",
        ],
    )
    def test_refuses_what_the_connector_will_refuse(self, resource):
        assert not dbx.role_arn_is_connector_assumable(f"arn:aws:iam::222233334444:role/{resource}")

    def test_the_reserved_prefix_check_alone_would_have_admitted_it(self):
        """A separate check, not a consequence of the prefix rule: the offending characters
        sit in the PATH, after a good prefix."""
        arn = "arn:aws:iam::222233334444:role/coa-dev-datasource-access-a/x;y/name"
        with patch.dict("os.environ", {"RESOURCE_PREFIX": "coa-dev-"}):
            assert dbx.role_name_is_reserved(arn) is True
        assert not dbx.role_arn_is_connector_assumable(arn)

    def test_the_shared_smithy_pattern_is_looser_than_this_one(self):
        from coa_control_plane_server.models.databricks_sql_warehouse_configuration import (
            DatabricksSqlWarehouseConfiguration,
        )

        loose = "arn:aws:iam::222233334444:role/coa-dev-datasource-access-a/x;y/name"
        model = DatabricksSqlWarehouseConfiguration.model_validate(
            {
                "workspaceHostname": "dbc-a1b2345c-d6e7.cloud.databricks.com",
                "httpPath": "/sql/1.0/warehouses/a1b234c567d8e9fa",
                "databricksCatalog": "main",
                "databaseName": "sales",
                "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx-AbCdEf",
                "crossAccountRoleArn": loose,
            }
        )
        assert model.cross_account_role_arn == loose
        assert not dbx.role_arn_is_connector_assumable(loose)


class TestConnectorSecretArnShape:
    """The shared Smithy ``SecretArn`` ends ``:secret:.+$``; the connector's own pattern
    admits only the character set Secrets Manager permits in a secret NAME.

    A source registered outside that set passes every registration check and then fails
    EVERY query, because the connector re-validates the value on each request.
    """

    @pytest.mark.parametrize(
        "name",
        ["dbx-AbCdEf", "team/dbx_sp+v2=1.0@x-AbCdEf", "a"],
    )
    def test_accepts_what_secrets_manager_permits(self, name):
        assert dbx.secret_arn_is_connector_readable(f"arn:aws:secretsmanager:us-east-1:222233334444:secret:{name}")

    @pytest.mark.parametrize(
        "name",
        [
            "dbx;SSL=0-AbCdEf",
            'dbx"quoted-AbCdEf',
            "dbx'quoted-AbCdEf",
            "dbx AbCdEf",
            "dbx*-AbCdEf",
            "",
        ],
    )
    def test_refuses_what_the_connector_will_refuse(self, name):
        assert not dbx.secret_arn_is_connector_readable(f"arn:aws:secretsmanager:us-east-1:222233334444:secret:{name}")

    def test_the_shared_smithy_pattern_is_looser_than_this_one(self):
        """If a regeneration ever tightens the shared shape, this test is what says the
        local check became redundant."""
        from coa_control_plane_server.models.databricks_sql_warehouse_configuration import (
            DatabricksSqlWarehouseConfiguration,
        )

        loose = "arn:aws:secretsmanager:us-east-1:222233334444:secret:dbx;SSL=0-AbCdEf"
        model = DatabricksSqlWarehouseConfiguration.model_validate(
            {
                "workspaceHostname": "dbc-a1b2345c-d6e7.cloud.databricks.com",
                "httpPath": "/sql/1.0/warehouses/a1b234c567d8e9fa",
                "databricksCatalog": "main",
                "databaseName": "sales",
                "credentialSecretArn": loose,
                "crossAccountRoleArn": "arn:aws:iam::222233334444:role/coa-dev-datasource-access-dbx",
            }
        )
        assert model.credential_secret_arn == loose
        assert not dbx.secret_arn_is_connector_readable(loose)
