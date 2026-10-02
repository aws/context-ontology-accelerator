# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the DATABRICKS_SQL_WAREHOUSE sub-type: registration and delete.

Three properties carry most of the weight: what registration refuses, what it
deliberately does NOT refuse (a role in the deployment account), and the create/delete
ordering with its rollbacks.

Imports mirror ``test_database_routes.py``: ``sources_handler`` first to resolve the
circular import with ``database_routes``, then patches by full module-path string.
"""

from __future__ import annotations

import inspect
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from coa_common.dao.dynamodb import DynamoDBDAO

from tests.unit.conftest import dao_double

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.api.sources_handler  # noqa: F401, I001, E402
import coa_sources.api.database_routes as _dr  # noqa: E402, I001
import coa_sources.api.sources_handler as _sh  # noqa: E402, I001
from coa_sources.database import databricks as _dbx  # noqa: E402
from coa_sources.database.connectors.athena_catalog import (  # noqa: E402
    AthenaCatalogConflictError,
    AthenaCatalogError,
    derive_catalog_name,
)
from coa_sources.database.connectors.sts_assume import (  # noqa: E402
    DatasourceAssumeError,
    assume_datasource_session,
)

_DR = "coa_sources.api.database_routes"
_SH = "coa_sources.api.sources_handler"
_DBX = "coa_sources.database.databricks"

_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"

# Both accounts exist to assert that NEITHER changes the outcome: this sub-type performs
# no account comparison on either ARN.
_DEPLOYMENT_ACCOUNT = "111122223333"
_FOREIGN_ACCOUNT = "222233334444"

_CONNECTOR_ARN = "arn:aws:lambda:us-east-1:111122223333:function:coa-dev-databricks-connector"
_CONNECTOR_ARN_PARAM = "/coa/dev/connectors/databricks/deployment/function-arn"
_CONFIG_SSM_PREFIX = "/coa/dev/connectors/databricks/sources"

# Role names are part of the contract: COA's assume grant is scoped to
# `{RESOURCE_PREFIX}datasource-access-*`.
_GOOD_ROLE = f"arn:aws:iam::{_DEPLOYMENT_ACCOUNT}:role/coa-dev-datasource-access-dbx"
_FOREIGN_ACCOUNT_ROLE = f"arn:aws:iam::{_FOREIGN_ACCOUNT}:role/coa-dev-datasource-access-dbx"
_BADLY_NAMED_ROLE = f"arn:aws:iam::{_DEPLOYMENT_ACCOUNT}:role/my-databricks-reader"
_SECRET = f"arn:aws:secretsmanager:us-east-1:{_FOREIGN_ACCOUNT}:secret:dbx-sp-AbCdEf"
_OTHER_REGION_SECRET = f"arn:aws:secretsmanager:eu-west-1:{_FOREIGN_ACCOUNT}:secret:dbx-sp-AbCdEf"

_HOSTNAME = "dbc-a1b2345c-d6e7.cloud.databricks.com"
_HTTP_PATH = "/sql/1.0/warehouses/a1b234c567d8e9fa"


def _parse(result):
    return result["statusCode"], json.loads(result["body"]) if result.get("body") else {}


def _config_dict(**overrides) -> dict:
    body = {
        "workspaceHostname": _HOSTNAME,
        "httpPath": _HTTP_PATH,
        # Deliberately mixed case: the create path must lowercase both.
        "databricksCatalog": "Main",
        "databaseName": "Sales",
        "credentialSecretArn": _SECRET,
        "crossAccountRoleArn": _GOOD_ROLE,
    }
    body.update(overrides)
    return body


def _make_databricks_db_req(**overrides):
    """A CreateDatabaseSourceInput-shaped mock carrying only the Databricks config.

    The other three members are ``None`` explicitly: a MagicMock auto-attribute is
    truthy, which would trip the mutual-exclusivity guard.
    """
    config = _config_dict(**overrides)
    req = MagicMock()
    req.name = "my-warehouse"
    req.glue_configuration = None
    req.jdbc_configuration = None
    req.custom_connector_configuration = None
    dbx = MagicMock()
    dbx.cross_account_role_arn = config["crossAccountRoleArn"]
    dbx.credential_secret_arn = config["credentialSecretArn"]
    dbx.database_name = config["databaseName"]
    dbx.to_dict.return_value = config
    req.databricks_sql_warehouse_configuration = dbx
    req.metadata_enrichment_enabled = None
    return req


def _source_puts(mock_dao) -> list[dict]:
    return [c.args[0] for c in mock_dao.put.call_args_list if str(c.args[0].get("SK", "")).startswith("SRC#")]


def _source_put(mock_dao) -> dict:
    puts = _source_puts(mock_dao)
    assert len(puts) == 1, f"expected exactly one source row put, got {len(puts)}"
    return puts[0]


def _source_deletes(mock_dao) -> list[dict]:
    return [c.args[0] for c in mock_dao.delete.call_args_list if str(c.args[0].get("SK", "")).startswith("SRC#")]


def _wired_assume(session) -> list:
    """``assume_datasource_session`` side effect for a CORRECTLY wired role.

    Two calls, not one: the positive assume returns ``session``, and the
    negative-ExternalId probe that follows is denied.

    The denial is a ``DatasourceAssumeError`` carrying the STS code as an attribute,
    which is what the wiring check reads. A bare ``ValueError`` spelling the code into its
    message would not be recognised, which is the point of the typed exception.
    """
    return [session, DatasourceAssumeError("AccessDenied")]


@pytest.fixture(autouse=True)
def databricks_env():
    """Point the module at a deployed connector and a configuration path.

    Patched rather than re-imported, so the test does not depend on import order.
    """
    with (
        patch.object(_dbx, "CONNECTOR_ARN_SSM_PARAM", _CONNECTOR_ARN_PARAM),
        patch.object(_dbx, "CONFIG_SSM_PREFIX", _CONFIG_SSM_PREFIX),
        patch.dict(os.environ, {"RESOURCE_PREFIX": "coa-dev-"}),
    ):
        yield


@pytest.fixture(autouse=True)
def reset_lazy_clients():
    import coa_sources.api.namespace_counters as nc

    _sh._dao = None
    _sh._sqs = None
    _sh._scan_dao = None
    _sh._ns_dao = None
    nc._ns_dao = None
    # Create goes through database_routes, delete through sources_handler; each imported its own name.
    with patch(f"{_DR}.adjust_namespace_source_count"), patch(f"{_SH}.adjust_namespace_source_count"):
        yield
    _sh._dao = None
    _sh._sqs = None
    _sh._scan_dao = None
    _sh._ns_dao = None
    nc._ns_dao = None


class _Harness:
    """One place that stubs every AWS call a Databricks create makes.

    A class rather than a fixture per dependency so each test overrides exactly one
    behaviour and inherits the rest.
    """

    def __init__(self):
        # Autospecced, so every call the create path makes is checked against the real
        # `DynamoDBDAO` signature.
        self.dao = dao_double()
        # None rather than autospec's truthy default, so every absence check in the
        # create path reads as absent.
        self.dao.get.return_value = None
        self.sqs = MagicMock()
        self.register = MagicMock(return_value=True)
        self.delete_catalog = MagicMock()
        self.wiring = MagicMock(return_value=_dbx.WiringVerdict(ok=True, reason="wired"))
        self.connector_arn = MagicMock(return_value=_CONNECTOR_ARN)
        self.write_param = MagicMock(side_effect=lambda *, catalog_name, config: f"{_CONFIG_SSM_PREFIX}/{catalog_name}")
        self.delete_param = MagicMock()

    def create(self, req):
        with (
            patch(f"{_DR}._get_dao", return_value=self.dao),
            patch(f"{_DR}._get_scan_dao", return_value=MagicMock()),
            patch(f"{_DR}._get_sqs", return_value=self.sqs),
            patch(f"{_DR}.register_lambda_catalog", self.register),
            patch(f"{_DR}.delete_lambda_catalog", self.delete_catalog),
            patch(f"{_DR}.validate_credential_wiring", self.wiring),
            patch(f"{_DR}.resolve_connector_function_arn", self.connector_arn),
            patch(f"{_DR}.write_config_parameter", self.write_param),
            patch(f"{_DR}.delete_config_parameter", self.delete_param),
        ):
            return _parse(_dr._create_database_source(req, _NAMESPACE_ID))


@pytest.fixture
def harness() -> _Harness:
    return _Harness()


class _CommittingSsm:
    """Parameter Store double that really stores what it is given.

    A ``MagicMock`` can record a ``put_parameter`` call; it cannot show that the value is
    still there afterwards. This one can, which is what a rollback assertion needs:
    ``committed`` lists every name the store ever accepted and ``store`` holds the ones
    still present, so "the write landed" and "the delete removed it" are separate claims.

    ``time_out_after_commit`` reproduces the failure the best-effort delete exists for:
    ``PutParameter`` commits and the response never arrives.
    """

    def __init__(self, *, time_out_after_commit: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.committed: list[str] = []
        self._time_out = time_out_after_commit

    def put_parameter(self, *, Name, Value, Overwrite=False, **_):  # noqa: N803 - boto3 casing
        if Name in self.store and not Overwrite:
            raise ClientError({"Error": {"Code": "ParameterAlreadyExists"}}, "PutParameter")
        self.store[Name] = Value
        self.committed.append(Name)
        if self._time_out:
            raise ReadTimeoutError(endpoint_url="https://ssm.us-east-1.amazonaws.com")

    def get_parameter(self, *, Name, **_):  # noqa: N803 - boto3 casing
        if Name not in self.store:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": self.store[Name]}}

    def delete_parameter(self, *, Name, **_):  # noqa: N803 - boto3 casing
        if Name not in self.store:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "DeleteParameter")
        del self.store[Name]


# ===================================================================
# Registration — what it refuses
# ===================================================================


@pytest.mark.unit
class TestDatabricksRegistrationRejections:
    """Each rejection closes something specific, and each says which."""

    @pytest.mark.parametrize(
        ("overrides", "expected_field"),
        [
            ({"httpPath": "/sql/2.0/warehouses/abc"}, "httpPath"),
            ({"httpPath": "/sql/1.0/warehouses/abc;SSL=0"}, "httpPath"),
            ({"workspaceHostname": "evil.example.com"}, "workspaceHostname"),
            ({"workspaceHostname": "dbc-1.cloud.databricks.com.evil.com"}, "workspaceHostname"),
            ({"databricksCatalog": "main-catalog"}, "databricksCatalog"),
            ({"databricksCatalog": "1main"}, "databricksCatalog"),
            ({"databaseName": "sales schema"}, "databaseName"),
            ({"databaseName": 'sales"; DROP'}, "databaseName"),
        ],
    )
    def test_rejects_a_value_the_pattern_bars(self, harness, overrides, expected_field):
        (
            status,
            body,
        ) = harness.create(_make_databricks_db_req(**overrides))
        assert status == 400
        assert expected_field in body["error"], body
        harness.register.assert_not_called()
        assert _source_puts(harness.dao) == []

    def test_rejects_a_role_named_outside_the_reserved_prefix(self, harness):
        """COA's own assume grant is scoped to the prefix, so the message has to name the
        prefix rather than leaving an opaque AccessDenied to arrive at the first scan."""
        status, body = harness.create(_make_databricks_db_req(crossAccountRoleArn=_BADLY_NAMED_ROLE))
        assert status == 400
        assert "coa-dev-datasource-access-" in body["error"], body
        harness.register.assert_not_called()
        harness.wiring.assert_not_called()

    def test_rejects_a_non_role_arn(self, harness):
        """``IamRoleArn`` admits no user, no session and no wildcard, and registration
        re-runs the type, so a user ARN never reaches the assume."""
        user_arn = f"arn:aws:iam::{_DEPLOYMENT_ACCOUNT}:user/coa-dev-datasource-access-dbx"
        status, body = harness.create(_make_databricks_db_req(crossAccountRoleArn=user_arn))
        assert status == 400
        assert "crossAccountRoleArn" in body["error"], body
        harness.wiring.assert_not_called()

    def test_rejects_a_secret_outside_the_deployment_region(self, harness):
        """Athena does not support cross-Region federated queries, and nothing in the
        assume path cares about the secret's region, so COA states the rule itself."""
        status, body = harness.create(_make_databricks_db_req(credentialSecretArn=_OTHER_REGION_SECRET))
        assert status == 400
        assert "us-east-1" in body["error"] and "eu-west-1" in body["error"], body
        harness.wiring.assert_not_called()

    def test_rejects_a_create_when_no_connector_is_deployed(self, harness):
        """Discovery for this sub-type runs THROUGH the connector, so a source registered
        into an environment with none could never reach review."""
        harness.connector_arn.side_effect = _dbx.DatabricksConnectorUnavailableError(
            "No Databricks connector is deployed in this environment"
        )
        status, body = harness.create(_make_databricks_db_req())
        assert status == 400
        assert "no databricks connector is deployed" in body["error"].lower(), body
        harness.register.assert_not_called()
        assert _source_puts(harness.dao) == []


@pytest.mark.unit
class TestEveryWriteMatchesTheRealDaoSignature:
    """Regression guard: a bare ``MagicMock`` DAO accepts any keyword, so a ``put`` with a
    keyword ``DynamoDBDAO.put`` does not have raised ``TypeError`` only in production.
    """

    def test_a_create_reaches_202_rather_than_an_internal_server_error(self, harness):
        status, body = harness.create(_make_databricks_db_req())
        assert status == 202, body
        assert body.get("error") != "Internal server error"

    def test_every_put_the_create_makes_matches_the_real_dao_signature(self, harness):
        """Every call the create path makes has to be one the real DAO accepts."""
        harness.create(_make_databricks_db_req())
        signature = inspect.signature(DynamoDBDAO.put)
        for call in harness.dao.put.call_args_list:
            signature.bind(None, *call.args, **call.kwargs)


@pytest.mark.unit
class TestArnNormalisationAtTheApiBoundary:
    """A trailing newline passes the generated Smithy validator, and the two sides of the
    contract then act on different strings: COA's ``startswith`` still matches, while the
    Java connector trims — so COA would prove the wiring of a value it never assumes.
    """

    _RAW = _GOOD_ROLE + "\n"

    def test_the_stored_blob_carries_the_normalised_arn(self, harness):
        status, body = harness.create(_make_databricks_db_req(crossAccountRoleArn=self._RAW))
        assert status == 202, body
        stored = json.loads(_source_put(harness.dao)["configuration"])
        assert stored["crossAccountRoleArn"] == _GOOD_ROLE

    def test_the_connector_parameter_carries_the_normalised_arns(self, harness):
        """The parameter, not the record, is what the connector reads — and it trims."""
        harness.create(_make_databricks_db_req(crossAccountRoleArn=self._RAW, credentialSecretArn=_SECRET + "\n"))
        body = json.loads(harness.write_param.call_args.kwargs["config"].to_parameter_value())
        assert body["crossAccountRoleArn"] == _GOOD_ROLE
        assert body["credentialSecretArn"] == _SECRET

    def test_the_assume_is_attempted_with_the_normalised_arn(self, harness):
        harness.create(_make_databricks_db_req(crossAccountRoleArn=self._RAW))
        assert harness.wiring.call_args.kwargs["role_arn"] == _GOOD_ROLE

    def test_whitespace_inside_the_arn_is_refused(self, harness):
        """Whitespace surviving the strip is not a spelling of anything, so it is refused.

        Exercised through the SECRET ARN, the only one of the two it is reachable on:
        ``IamRoleArn``'s pattern admits no space anywhere, while ``SecretArn`` ends
        ``:secret:.+$`` and ``.`` matches a space."""
        arn = f"arn:aws:secretsmanager:us-east-1:{_FOREIGN_ACCOUNT}:secret:dbx sp-AbCdEf"
        status, body = harness.create(_make_databricks_db_req(credentialSecretArn=arn))
        assert status == 400
        assert "whitespace" in body["error"], body
        assert _source_puts(harness.dao) == []


@pytest.mark.unit
class TestRolePathIsPartOfThePrefixRule:
    def test_a_role_under_an_iam_path_is_refused(self, harness):
        """COA's grant matches everything after ``role/``, so ``role/team/a/coa-dev-…``
        does not match ``role/coa-dev-datasource-access-*`` however its name is spelled."""
        pathed = f"arn:aws:iam::{_DEPLOYMENT_ACCOUNT}:role/team/finance/coa-dev-datasource-access-dbx"
        status, body = harness.create(_make_databricks_db_req(crossAccountRoleArn=pathed))
        assert status == 400
        assert "coa-dev-datasource-access-" in body["error"], body
        # Refused before the assume, so the failure is the prefix rule.
        harness.wiring.assert_not_called()


@pytest.mark.unit
class TestRoleArnMustMatchTheConnectorsOwnPattern:
    """The connector's own role-ARN pattern is narrower than the shared
    ``IamRoleArn``, which permits an IAM path containing ``;``, ``%`` and quotes.

    Such an ARN clears the reserved-prefix check (the offending characters are in the
    path, after a correct prefix) and the IAM wildcard, then fails every query on the
    connector's re-validation. Refused BEFORE the prefix rule so the message names the
    actual problem.
    """

    def test_a_role_path_outside_the_connectors_charset_is_refused(self, harness):
        arn = f"arn:aws:iam::{_DEPLOYMENT_ACCOUNT}:role/coa-dev-datasource-access-a/x;y/name"
        status, body = harness.create(_make_databricks_db_req(crossAccountRoleArn=arn))
        assert status == 400, body
        assert "crossAccountRoleArn" in body["error"]
        # NOT the prefix rule, which this ARN satisfies.
        assert "coa-dev-datasource-access-" not in body["error"], body
        harness.wiring.assert_not_called()
        assert _source_puts(harness.dao) == []

    @pytest.mark.parametrize(
        ("partition", "expected"),
        [
            # The shared Smithy `IamRoleArn` names `aws` and `aws-us-gov`, so GovCloud is the
            # partition that reaches this check, and the one the pinned pattern closes. China
            # is refused a step earlier, by a generated message that prints the regex.
            ("aws-us-gov", "partition"),
            ("aws-cn", "crossAccountRoleArn"),
        ],
    )
    def test_a_role_outside_the_commercial_partition_is_refused_at_submit(self, harness, partition, expected):
        """This deployment's assume grant is written ``arn:aws:iam::*:role/…``, so a role in
        another partition would register and then have every assume denied by our own policy
        — with an AccessDenied whose message sends the customer to a trust policy that is
        already correct.

        The message has to say WHY: the only readable difference from a working ARN is a few
        characters in the partition segment, and nothing else names them.
        """
        arn = f"arn:{partition}:iam::{_DEPLOYMENT_ACCOUNT}:role/coa-dev-datasource-access-dbx"
        status, body = harness.create(_make_databricks_db_req(crossAccountRoleArn=arn))
        assert status == 400, body
        assert expected in body["error"], body
        harness.wiring.assert_not_called()
        assert _source_puts(harness.dao) == []


@pytest.mark.unit
class TestSecretArnMustMatchTheConnectorsOwnPattern:
    """The shared Smithy ``SecretArn`` ends ``:secret:.+$``; the connector's own pattern
    admits only the characters Secrets Manager permits in a secret name.

    Under the looser form registration succeeds and every query then fails, because the
    connector re-validates this value on each request.
    """

    @pytest.mark.parametrize(
        "name",
        ["dbx;SSL=0-AbCdEf", 'dbx"quoted-AbCdEf', "dbx AbCdEf"],
    )
    def test_a_secret_name_the_connector_will_reject_is_refused(self, harness, name):
        arn = f"arn:aws:secretsmanager:us-east-1:{_FOREIGN_ACCOUNT}:secret:{name}"
        status, body = harness.create(_make_databricks_db_req(credentialSecretArn=arn))
        assert status == 400, body
        assert "credentialSecretArn" in body["error"]
        harness.write_param.assert_not_called()
        assert _source_puts(harness.dao) == []

    def test_a_secret_name_within_the_permitted_set_is_accepted(self, harness):
        arn = f"arn:aws:secretsmanager:us-east-1:{_FOREIGN_ACCOUNT}:secret:team/dbx_sp+v2=1.0@x-AbCdEf"
        status, body = harness.create(_make_databricks_db_req(credentialSecretArn=arn))
        assert status == 202, body


@pytest.mark.unit
class TestDatabricksWiringFailuresAreDistinguishable:
    """A misconfiguration must say WHICH thing is wrong, because the causes are fixed in
    different places.

    Two of them collapse into one message as a property of STS: it returns the same
    ``AccessDenied`` whether the trust policy omits our principal or requires a different
    ExternalId, so the message names both and prints the exact ExternalId sent.
    """

    def _messages(self, harness) -> str:
        _, body = harness.create(_make_databricks_db_req())
        return body["error"]

    def test_trust_policy_and_external_id_are_reported_together_with_the_value(self):
        with patch(f"{_DBX}.assume_datasource_session", side_effect=DatasourceAssumeError("AccessDenied")):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.ok is False
        assert verdict.reason == "assume-denied"
        message = verdict.message
        # The denied CALL, named. Not "could not be verified".
        assert "sts:AssumeRole" in message
        # Both indistinguishable causes, since STS returns one AccessDenied for both.
        assert "sts:ExternalId" in message
        assert "Principal" in message
        # Against the shipped helper, never a literal: the Java connector mirrors the same
        # derivation, so a drift must fail here rather than at every onboarding.
        from coa_common.constants import datasource_external_id

        assert datasource_external_id(_NAMESPACE_ID) in message

    def test_the_assume_denial_names_the_sources_api_role_as_the_caller(self):
        """Registration assumes from the SOURCES-API role and the connector role is the
        caller at query time, so the message must name both."""
        with patch(f"{_DBX}.assume_datasource_session", side_effect=DatasourceAssumeError("AccessDenied")):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        lowered = verdict.message.lower()
        assert "sources-api" in lowered, verdict.message
        assert "connector role" in lowered, verdict.message
        assert "query time" in lowered, verdict.message

    def test_the_assume_denial_warns_against_dropping_the_external_id(self):
        """Dropping the condition is one of the two fixes an operator reaches for that
        break the model, so the message has to warn against it explicitly."""
        with patch(f"{_DBX}.assume_datasource_session", side_effect=DatasourceAssumeError("AccessDenied")):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert "Do NOT drop the ExternalId condition" in verdict.message

    def test_the_external_id_sent_is_the_shared_derivation(self):
        from coa_common.constants import datasource_external_id

        session = MagicMock()
        with patch(f"{_DBX}.assume_datasource_session", side_effect=_wired_assume(session)) as assume:
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.ok is True
        # The FIRST assume; the second is the negative probe, which carries a value no
        # namespace can hold.
        assert assume.call_args_list[0].args[1] == datasource_external_id(_NAMESPACE_ID)
        # DescribeSecret, never GetSecretValue: reading the value would give COA a durable
        # relationship with the credential.
        client = session.client.return_value
        client.describe_secret.assert_called_once_with(SecretId=_SECRET)
        assert not client.get_secret_value.called

    def test_a_role_that_cannot_read_the_secret_names_the_action_that_was_denied(self):
        """``DescribeSecret``, not ``GetSecretValue``, which registration never calls: a
        customer told to add a grant they already hold reaches for ``secretsmanager:*``.
        """
        session = MagicMock()
        session.client.return_value.describe_secret.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DescribeSecret"
        )
        with patch(f"{_DBX}.assume_datasource_session", side_effect=_wired_assume(session)):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.reason == "secret-unreadable-by-role"
        message = verdict.message
        assert "secretsmanager:DescribeSecret" in message, message
        # Said outright, or the reader takes the message for a repeat of what they hold.
        assert "GetSecretValue already does not satisfy" in message, message
        # Both actions ARE needed on that one secret, by two different callers.
        assert "secretsmanager:GetSecretValue" in message
        assert "query time" in message
        # And the widening it must not prompt.
        assert "secretsmanager:*" in message
        # The commonest cause of this failure in a cross-account topology.
        assert "customer-managed" in message

    def test_an_absent_secret_is_its_own_message(self):
        session = MagicMock()
        session.client.return_value.describe_secret.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException"}}, "DescribeSecret"
        )
        with patch(f"{_DBX}.assume_datasource_session", side_effect=_wired_assume(session)):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.reason == "secret-absent"
        # Names the call, and says outright that it is NOT a permission problem —
        # otherwise this reads as the failure above and prompts the same wrong fix.
        assert "secretsmanager:DescribeSecret" in verdict.message
        assert "not a permission problem" in verdict.message

    def test_a_transient_sts_failure_is_retryable_not_a_refusal(self):
        with patch(f"{_DBX}.assume_datasource_session", side_effect=DatasourceAssumeError("Throttling")):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.retryable is True
        # Must say nothing is wrong with the configuration, or a blip reads as a refusal
        # and prompts an edit to a policy that is already correct.
        assert "sts:AssumeRole" in verdict.message
        assert "Nothing is wrong" in verdict.message

    def test_a_retryable_wiring_failure_is_a_503_not_a_400(self, harness):
        """A 400 says the ARN was rejected; a 503 says retry with the same one."""
        harness.wiring.return_value = _dbx.WiringVerdict(
            ok=False, reason="assume-unavailable", message="retry", retryable=True
        )
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 503

    @pytest.mark.parametrize(
        "code",
        ["InvalidParameterException", "InvalidRequestException", "ValidationException"],
    )
    def test_an_arn_secrets_manager_will_not_act_on_names_the_arn_not_a_policy(self, code):
        """A malformed structure and a non-``aws`` partition both arrive as one of these
        codes, and both used to land in the denial branch — which tells the operator to add
        ``secretsmanager:DescribeSecret`` to a role that already holds it. That is the one
        edit that widens a policy for no reason, so these get their own message."""
        session = MagicMock()
        session.client.return_value.describe_secret.side_effect = ClientError(
            {"Error": {"Code": code}}, "DescribeSecret"
        )
        with patch(f"{_DBX}.assume_datasource_session", side_effect=_wired_assume(session)):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.reason == "secret-arn-invalid"
        # A retry with the same ARN fails identically, so this is a 400 rather than a 503.
        assert verdict.retryable is False
        message = verdict.message
        assert "credentialSecretArn" in message
        assert "not a permission problem" in message, message
        # Both causes, named: an operator who reads only one goes looking in the wrong place.
        assert "structure" in message, message
        assert "partition" in message, message
        # And NOT the grant advice from the denial branch.
        assert "secretsmanager:*" not in message, message


@pytest.mark.unit
class TestTheNegativeExternalIdProbe:
    """STS silently ignores an ExternalId a trust policy does not ask for, so the positive
    assume succeeds whether the condition is required or absent. A second assume
    presenting a value no namespace can hold distinguishes the two: denial proves the
    condition is enforced, success proves the role is assumable on any namespace's behalf.
    """

    def _wiring(self, side_effect):
        """Run the whole check with ``assume_datasource_session`` scripted per call.

        Returns the verdict and the assume mock, since the second call's ExternalId is
        itself asserted on.
        """
        with patch(f"{_DBX}.assume_datasource_session", side_effect=side_effect) as assume:
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        return verdict, assume

    def _verdict(self, side_effect) -> _dbx.WiringVerdict:
        return self._wiring(side_effect)[0]

    def test_a_denied_probe_is_what_lets_the_registration_proceed(self):
        """A denial is the PASS condition, not a failure."""
        verdict, assume = self._wiring(_wired_assume(MagicMock()))
        assert verdict.ok is True, verdict.message
        # One call would mean the probe was skipped and the distinction never tested.
        assert assume.call_count == 2
        assert assume.call_args_list[1].args[1].endswith("-not-this-namespace")

    def test_a_probe_that_succeeds_refuses_the_role_and_names_the_condition_to_add(self):
        """Success means any namespace in this deployment can have COA assume the role and
        read the credential on its behalf."""
        from coa_common.constants import datasource_external_id

        external_id = datasource_external_id(_NAMESPACE_ID)
        verdict = self._verdict([MagicMock(), MagicMock()])
        assert verdict.ok is False
        assert verdict.reason == "external-id-not-required"
        # NOT retryable: the policy is wrong, and a retry fails identically.
        assert verdict.retryable is False
        # The message echoes the probe value too, so this pins the one the customer must
        # paste rather than merely finding the external id somewhere in the prose.
        assert f'sts:ExternalId = "{external_id}"' in verdict.message, verdict.message
        assert "StringEquals" in verdict.message
        # And what was actually presented, or the reader cannot tell what was proved.
        assert f"{external_id}-not-this-namespace" in verdict.message

    def test_a_transient_probe_failure_is_retryable_not_a_refusal(self):
        """A throttled probe proves nothing either way, so it must not read as a refusal
        and send the customer to edit a trust policy that is already correct."""
        verdict = self._verdict([MagicMock(), DatasourceAssumeError("Throttling")])
        assert verdict.ok is False
        assert verdict.reason == "probe-unavailable"
        assert verdict.retryable is True
        assert "sts:ExternalId" in verdict.message
        assert "Nothing is wrong" in verdict.message

    def test_the_probe_changes_only_the_external_id(self):
        """The session name is reused deliberately: a trust policy may condition on
        sts:RoleSessionName too, and a probe carrying its own would be denied by THAT
        condition — reading as proof of the one thing this check exists to establish."""
        _, assume = self._wiring(_wired_assume(MagicMock()))
        positive, probe = assume.call_args_list
        assert probe.args[3] == positive.args[3]
        assert probe.args[0] == positive.args[0]
        assert probe.args[1] != positive.args[1]

    def test_a_probe_denied_for_a_reason_other_than_access_denied_proves_nothing(self):
        """Only AccessDenied distinguishes an enforced condition from an absent one, so any
        other refusal leaves enforcement unproven."""
        verdict = self._verdict([MagicMock(), DatasourceAssumeError("RegionDisabledException")])
        assert verdict.ok is False
        assert verdict.reason == "probe-inconclusive"
        assert verdict.retryable is True
        assert "proved nothing" in verdict.message

    def test_the_probe_runs_before_the_secret_is_described(self):
        """A role assumable on any namespace's behalf is refused without COA touching the
        credential at all."""
        session = MagicMock()
        verdict = self._verdict([session, MagicMock()])
        assert verdict.reason == "external-id-not-required"
        assert not session.client.return_value.describe_secret.called


@pytest.mark.unit
class TestTheStsCodeTravelsOnTheException:
    """Every branch of the wiring check turns on the STS error code, so the code has to
    arrive as data.

    It used to be recovered by splitting the assume helper's message on ``": "``. Under that
    rule a reworded message stops matching every branch at once, and a correctly configured
    trust policy returns a server error on every Databricks registration.
    """

    def test_the_assume_helper_puts_the_sts_code_on_the_exception(self):
        """The producer half: the helper masks the ``ClientError`` so the role ARN cannot
        reach an API response, and carries the code as an attribute instead."""
        with patch("coa_sources.database.connectors.sts_assume.boto3") as boto:
            boto.client.return_value.assume_role.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied"}}, "AssumeRole"
            )
            with pytest.raises(DatasourceAssumeError) as exc:
                assume_datasource_session(_GOOD_ROLE, "coa-dev-ns", "us-east-1", "sess")
        assert exc.value.code == "AccessDenied"
        # The whole reason the code is masked in the first place.
        assert _GOOD_ROLE not in str(exc.value)

    def test_a_denial_is_recognised_whatever_the_message_says(self):
        """The consumer half, asserted against a message the old split could not parse: the
        probe must still read the denial that lets a correct registration proceed."""
        denied = DatasourceAssumeError("AccessDenied")
        denied.args = ("some future wording carrying no separator",)
        with patch(f"{_DBX}.assume_datasource_session", side_effect=[MagicMock(), denied]):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.ok is True, verdict.message

    def test_an_exception_carrying_no_code_is_inconclusive_rather_than_a_pass(self):
        """``assume_datasource_session`` also raises a plain ``ValueError`` for an empty
        argument, which carries no STS code. That must not read as the denial the probe is
        looking for."""
        with patch(f"{_DBX}.assume_datasource_session", side_effect=[MagicMock(), ValueError("role_arn is required")]):
            verdict = _dbx.validate_credential_wiring(
                role_arn=_GOOD_ROLE,
                secret_arn=_SECRET,
                namespace_id=_NAMESPACE_ID,
                source_id="src-1",
            )
        assert verdict.ok is False
        assert verdict.reason == "probe-inconclusive"


# ===================================================================
# Registration — what it deliberately accepts
# ===================================================================


@pytest.mark.unit
class TestDatabricksRegistrationDoesNotConstrainAccounts:
    """The assume scope is ``arn:aws:iam::*:role/…`` on purpose: COA is deployed INTO the
    customer's account, so a rule excluding the deployment account would refuse the most
    common topology. What bounds COA is the reserved name prefix plus the target role's
    own trust policy.
    """

    def test_a_correctly_named_role_in_the_deployment_account_is_accepted(self, harness):
        in_account_secret = f"arn:aws:secretsmanager:us-east-1:{_DEPLOYMENT_ACCOUNT}:secret:dbx-AbCdEf"
        status, _ = harness.create(
            _make_databricks_db_req(crossAccountRoleArn=_GOOD_ROLE, credentialSecretArn=in_account_secret)
        )
        assert status == 202
        assert _source_put(harness.dao)["sourceSubType"] == "DATABRICKS_SQL_WAREHOUSE"

    def test_a_role_in_another_account_is_equally_accepted(self, harness):
        status, _ = harness.create(_make_databricks_db_req(crossAccountRoleArn=_FOREIGN_ACCOUNT_ROLE))
        assert status == 202


# ===================================================================
# Registration — the record and the parameter
# ===================================================================


@pytest.mark.unit
class TestDatabricksSourceRecord:
    def test_derives_the_databricks_sub_type(self, harness):
        """The sub-type resolution ends in an `else` assigning GLUE_DATABASE, so a payload
        the mutual-exclusivity guard accepts but this does not recognise becomes a Glue
        source with a Databricks blob."""
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 202
        assert _source_put(harness.dao)["sourceSubType"] == "DATABRICKS_SQL_WAREHOUSE"

    def test_query_engine_is_athena(self, harness):
        """What keeps serve's direct-JDBC gate from claiming it: there is no direct adapter
        for a warehouse reached through a federation connector."""
        harness.create(_make_databricks_db_req())
        assert _source_put(harness.dao)["queryEngine"] == "ATHENA"

    def test_populates_the_two_fields_the_namespace_scope_needs(self, harness):
        """Neither is optional: the namespace-qualifier check builds
        ``federated_catalog_schemas`` from ``discoveredSchemas``, so a record with a catalog
        and no schemas has every qualified query against it DENIED, not merely
        unresolved."""
        harness.create(_make_databricks_db_req())
        item = _source_put(harness.dao)
        assert item["athenaDataCatalogName"] == derive_catalog_name(item["sourceId"])
        assert item["athenaCatalog"] == item["athenaDataCatalogName"]
        assert item["athenaDatabase"] == "sales"
        assert item["discoveredSchemas"] == ["sales"]
        assert item["queryable"] is False

    def test_lowercases_the_unity_catalog_identifiers_in_the_stored_blob(self, harness):
        """``information_schema`` stores identifiers lowercase, so the customer-typed values
        need normalising in the blob as well as in the parameter."""
        harness.create(_make_databricks_db_req(databricksCatalog="MAIN", databaseName="SaLeS"))
        stored = json.loads(_source_put(harness.dao)["configuration"])
        assert stored["databricksCatalog"] == "main"
        assert stored["databaseName"] == "sales"

    def test_a_caller_supplied_external_id_is_dropped(self, harness):
        """Persisting a caller-supplied ExternalId would hand back control of the only
        value binding an assume to its namespace."""
        harness.create(_make_databricks_db_req(externalId="attacker-chosen"))
        stored = json.loads(_source_put(harness.dao)["configuration"])
        assert "externalId" not in stored


@pytest.mark.unit
class TestDatabricksConnectorParameter:
    def test_writes_the_parameter_after_the_record_and_the_catalog(self, harness):
        """Every delete path keys off the source record, so a catalog or parameter written
        first would be orphaned. The parameter is last because it is the only one whose
        absence is harmless: a catalog with no parameter fails loudly, while a parameter
        with no catalog still points at a credential."""
        order: list[str] = []
        harness.dao.put.side_effect = lambda item, **_: (
            order.append("record") if str(item.get("SK", "")).startswith("SRC#") else None
        )
        harness.register.side_effect = lambda **kw: order.append("catalog")
        harness.write_param.side_effect = lambda **kw: (order.append("parameter"), "param-name")[1]
        harness.create(_make_databricks_db_req())
        assert order == ["record", "catalog", "parameter"]

    def test_requires_the_catalog_to_be_absent_and_tags_it(self, harness):
        """With ONE shared handler ARN, ``register_lambda_catalog``'s ownership check is
        trivially true for every Databricks catalog, so a real collision would come back as
        ``False`` ("a retried create"). Requiring absence restores the discrimination, and
        the ``coa:sourceId`` tag is what delete verifies instead."""
        harness.create(_make_databricks_db_req())
        kwargs = harness.register.call_args.kwargs
        assert kwargs["require_absent"] is True
        assert kwargs["source_id"] == _source_put(harness.dao)["sourceId"]
        assert kwargs["connector_function_arn"] == _CONNECTOR_ARN

    def test_the_parameter_body_carries_the_requesting_namespace_and_deployment_id(self, harness):
        harness.create(_make_databricks_db_req())
        config = harness.write_param.call_args.kwargs["config"]
        body = json.loads(config.to_parameter_value())
        assert body["namespaceId"] == _NAMESPACE_ID
        # RESOURCE_PREFIX with trailing hyphens stripped: the Java connector derives its
        # own the same way and refuses a parameter that disagrees.
        assert body["deploymentId"] == "coa-dev"
        assert body["databricksCatalog"] == "main"
        assert body["databaseName"] == "sales"
        assert body["crossAccountRoleArn"] == _GOOD_ROLE
        assert body["credentialSecretArn"] == _SECRET

    def test_the_parameter_never_carries_credential_material(self, harness):
        """The invariant the ``String``-not-``SecureString`` decision rests on: plaintext is
        correct only because nothing in the payload is secret."""
        harness.create(_make_databricks_db_req())
        body = json.loads(harness.write_param.call_args.kwargs["config"].to_parameter_value())
        forbidden = {"token", "client_secret", "password", "access_token"}
        assert forbidden.isdisjoint(body), body
        # Nor anything that merely looks like one: a renamed field would slip past an
        # exact-key check.
        for key in body:
            assert not any(word in key.lower() for word in ("token", "secretvalue", "password", "credentialvalue")), key

    def test_the_parameter_name_is_the_env_scoped_path_plus_the_catalog_name(self):
        """The path carries the ENVIRONMENT: environments share an account, so without it a
        dev sources-API role could repoint a prod source's parameter and a dev query would
        resolve prod's credential."""
        with patch.object(_dbx, "CONFIG_SSM_PREFIX", _CONFIG_SSM_PREFIX):
            assert _dbx.config_parameter_name("coadevds_abc") == f"{_CONFIG_SSM_PREFIX}/coadevds_abc"


# ===================================================================
# Registration — rollback
# ===================================================================


@pytest.mark.unit
class TestDatabricksCreateRollback:
    def test_a_catalog_failure_removes_the_record_and_the_claim(self, harness):
        harness.register.side_effect = AthenaCatalogError("nope")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        deleted = [str(c.args[0].get("SK")) for c in harness.dao.delete.call_args_list]
        assert len(_source_deletes(harness.dao)) == 1
        assert "CLAIM" in deleted
        harness.write_param.assert_not_called()

    def test_a_conflicting_catalog_is_not_deleted(self, harness):
        """A conflict means the catalog belongs to something else, so deleting it would
        destroy a resource this create did not make."""
        harness.register.side_effect = AthenaCatalogConflictError("someone else's")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        harness.delete_catalog.assert_not_called()
        assert len(_source_deletes(harness.dao)) == 1

    def test_a_parameter_write_failure_rolls_back_the_catalog_too(self, harness):
        """The record is the only handle on the catalog's name, and unlike a registration
        failure there is no conflict case: the parameter write never touches the catalog."""
        harness.write_param.side_effect = _dbx.DatabricksConfigError("throttled")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        catalog_name = derive_catalog_name(_source_put(harness.dao)["sourceId"])
        harness.delete_catalog.assert_called_once_with(catalog_name=catalog_name)
        assert len(_source_deletes(harness.dao)) == 1

    def test_a_parameter_write_failure_also_removes_the_parameter_it_may_have_written(self, harness):
        """``PutParameter`` can commit server-side and have its response time out, so the
        parameter may exist even though the call raised. The name has to be re-derived,
        because the assignment that would have held it is the statement that raised.
        """
        harness.write_param.side_effect = _dbx.DatabricksConfigError("read timeout after commit")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        catalog_name = derive_catalog_name(_source_put(harness.dao)["sourceId"])
        harness.delete_param.assert_called_once_with(parameter_name=f"{_CONFIG_SSM_PREFIX}/{catalog_name}")

    def test_a_parameter_collision_does_not_delete_the_colliding_parameter(self, harness):
        """A collision means this create did NOT write it, so deleting it would destroy
        another source's live credential pointer."""
        harness.write_param.side_effect = _dbx.DatabricksConfigExistsError("already there")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        harness.delete_param.assert_not_called()
        # The catalog IS still removed: the parameter write never touches it.
        harness.delete_catalog.assert_called_once()

    def test_an_unresolvable_parameter_path_still_rolls_back_the_record_and_catalog(self, harness):
        """When the path prefix is unset the write raised before reaching PutParameter, so
        there is nothing to remove — and letting the name lookup escape would skip the
        rollback and leave the record AND the catalog behind."""
        harness.write_param.side_effect = _dbx.DatabricksConfigError("no prefix configured")
        with patch.object(_dbx, "CONFIG_SSM_PREFIX", ""):
            status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        harness.delete_catalog.assert_called_once()
        assert len(_source_deletes(harness.dao)) == 1

    def test_a_parameter_collision_fails_the_create_loudly(self, harness):
        """A collision means an assumption has broken, so it must not be reconciled
        silently."""
        harness.write_param.side_effect = _dbx.DatabricksConfigExistsError("already there")
        status, body = harness.create(_make_databricks_db_req())
        assert status == 500
        assert "already exists" in body["error"]

    def test_a_failed_row_delete_leaves_the_row_recoverable(self, harness):
        """A row the rollback could not remove must not be left looking scanned;
        SCAN_FAILED is deletable and re-scannable."""
        harness.register.side_effect = AthenaCatalogError("nope")
        harness.dao.delete.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteItem")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        assert harness.dao.update.call_args.kwargs["update_fields"]["status"] == "SCAN_FAILED"

    def test_a_scan_enqueue_failure_removes_the_catalog_and_the_parameter(self, harness):
        harness.sqs.send_message.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "SendMessage")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        catalog_name = derive_catalog_name(_source_put(harness.dao)["sourceId"])
        harness.delete_catalog.assert_called_once_with(catalog_name=catalog_name)
        harness.delete_param.assert_called_once_with(parameter_name=f"{_CONFIG_SSM_PREFIX}/{catalog_name}")

    def test_a_parameter_delete_failure_keeps_the_row_so_a_delete_can_retry(self, harness):
        """Nothing else would ever remove an orphaned parameter, so the record that says it
        is there stays — in SCAN_FAILED, because REGISTERED is refused by both delete and
        re-scan."""
        harness.sqs.send_message.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "SendMessage")
        harness.delete_param.side_effect = _dbx.DatabricksConfigError("throttled")
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        assert _source_deletes(harness.dao) == []
        assert harness.dao.update.call_args.kwargs["update_fields"]["status"] == "SCAN_FAILED"

    def test_a_write_that_commits_and_then_times_out_leaves_no_orphan(self, harness):
        """The case the best-effort delete exists for, against a store that really holds the
        value rather than a mock that records the call.

        ``PutParameter`` commits server-side and the response never arrives, so the caller
        sees a failure over a parameter that IS there. Nothing else in the system would ever
        find it: the source record is the only handle on its name, and the rollback deletes
        the record. The write and the delete both run for real here, so the assertion is on
        the store's contents and not on a call having been made.
        """
        ssm = _CommittingSsm(time_out_after_commit=True)
        harness.write_param.side_effect = lambda **kw: _dbx.write_config_parameter(client=ssm, **kw)
        harness.delete_param.side_effect = lambda **kw: _dbx.delete_config_parameter(client=ssm, **kw)
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        name = f"{_CONFIG_SSM_PREFIX}/{derive_catalog_name(_source_put(harness.dao)['sourceId'])}"
        # The write did land, so there was something to clean up.
        assert ssm.committed == [name]
        # And a read now finds nothing, which is the whole claim.
        assert ssm.store == {}
        with pytest.raises(ClientError):
            ssm.get_parameter(Name=name)

    def test_a_record_write_failure_leaves_nothing_behind(self, harness):
        """The record is written FIRST, so a failed put leaves nothing to roll back."""
        harness.dao.put.side_effect = lambda item, **kw: (
            (_ for _ in ()).throw(ClientError({"Error": {"Code": "ThrottlingException"}}, "PutItem"))
            if str(item.get("SK", "")).startswith("SRC#")
            else None
        )
        status, _ = harness.create(_make_databricks_db_req())
        assert status == 500
        harness.register.assert_not_called()
        harness.write_param.assert_not_called()
        assert _source_deletes(harness.dao) == []


# ===================================================================
# The four-way mutual-exclusivity guard
# ===================================================================


@pytest.mark.unit
class TestFourWayConfigurationGuard:
    def test_more_than_one_configuration_is_refused(self, harness):
        req = _make_databricks_db_req()
        req.jdbc_configuration = MagicMock()
        status, body = harness.create(req)
        assert status == 400
        assert "exactly one" in body["error"]
        assert "databricksSqlWarehouseConfiguration" in body["error"]

    def test_the_no_configuration_error_names_all_four_members(self, harness):
        req = _make_databricks_db_req()
        req.databricks_sql_warehouse_configuration = None
        status, body = harness.create(req)
        assert status == 400
        for member in (
            "glueConfiguration",
            "jdbcConfiguration",
            "customConnectorConfiguration",
            "databricksSqlWarehouseConfiguration",
        ):
            assert member in body["error"]


# ===================================================================
# Configuration update — a deliberate refusal
# ===================================================================


@pytest.mark.unit
class TestDatabricksConfigurationUpdateIsRefused:
    """DELIBERATELY unmapped, and this test exists so nobody "fixes" it by accident.

    A Databricks source's connection facts are written TWICE at create — once on the
    record and once into the connector's SSM parameter, which is what the connector
    actually reads — and write-once is what makes that duplication safe. Do not add
    ``DATABRICKS_SQL_WAREHOUSE`` to ``config_key_for_sub_type`` to make these pass; make
    the two writes transactional first, then delete these tests on purpose.
    """

    def _update(self, item, body):
        mock_dao = MagicMock()
        mock_dao.get.return_value = item
        event = {"body": json.dumps(body)}
        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            return _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, "src-1"))

    @staticmethod
    def _databricks_row():
        return {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": "SRC#src-1",
            "sourceId": "src-1",
            "sourceType": "DATABASE",
            "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
            "configuration": json.dumps(_config_dict()),
        }

    def test_supplying_the_databricks_configuration_is_a_400(self):
        status, body = self._update(
            self._databricks_row(),
            {"databricksSqlWarehouseConfiguration": _config_dict(httpPath="/sql/1.0/warehouses/other")},
        )
        assert status == 400
        assert "DATABRICKS_SQL_WAREHOUSE" in body["error"], body

    def test_supplying_another_sub_types_configuration_on_a_databricks_row_is_a_400(self):
        status, body = self._update(
            self._databricks_row(),
            {"customConnectorConfiguration": {"connectorFunctionArn": _CONNECTOR_ARN, "databaseName": "x"}},
        )
        assert status == 400
        assert "DATABRICKS_SQL_WAREHOUSE" in body["error"], body

    def test_the_configuration_is_never_silently_ignored(self):
        """A body carrying the unmapped member alongside a mutable field must not return
        200 having changed the name and dropped the configuration."""
        status, _ = self._update(
            self._databricks_row(),
            {"name": "renamed", "databricksSqlWarehouseConfiguration": _config_dict()},
        )
        assert status == 400

    def test_a_name_only_update_still_works(self):
        """Renaming touches neither copy of the connection facts, so it stays allowed."""
        mock_dao = MagicMock()
        mock_dao.get.return_value = self._databricks_row()
        event = {"body": json.dumps({"name": "renamed"})}
        with patch(f"{_DR}._get_dao", return_value=mock_dao):
            status, _ = _parse(_dr._handle_update_metadata(event, _NAMESPACE_ID, "src-1"))
        assert status == 200


# ===================================================================
# Delete
# ===================================================================


def _databricks_source_row(source_id: str, *, role_arn: str = _GOOD_ROLE, status: str = "APPROVED") -> dict:
    return {
        "PK": f"NS#{_NAMESPACE_ID}",
        "SK": f"SRC#{source_id}",
        "sourceId": source_id,
        "namespaceId": _NAMESPACE_ID,
        "name": "my-warehouse",
        "sourceType": "DATABASE",
        "sourceSubType": "DATABRICKS_SQL_WAREHOUSE",
        "status": status,
        "athenaDataCatalogName": derive_catalog_name(source_id),
        "athenaDatabase": "sales",
        "discoveredSchemas": ["sales"],
        "configuration": json.dumps(_config_dict(crossAccountRoleArn=role_arn)),
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
    }


@pytest.mark.unit
class TestDatabricksDelete:
    """Order is the substance here: catalog, parameter, claim, record.

    Catalog first so no NEW query can reach the connector; parameter next so nothing left
    resolvable outlives it; the claim before the record. There is no credential-revoke
    step because COA holds no grant on the customer's secret.
    """

    _SOURCE_ID = "src-dbx-1"

    def _delete(
        self,
        item=None,
        *,
        tag=None,
        dao=None,
        delete_param=None,
        delete_catalog=None,
        config_prefix=_CONFIG_SSM_PREFIX,
    ):
        mock_dao = dao or MagicMock()
        mock_dao.get.return_value = item if item is not None else _databricks_source_row(self._SOURCE_ID)
        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._delete_source_datazone_assets", return_value=(0, True)),
            patch(f"{_SH}._delete_source_scan_jobs", return_value=0),
            patch(f"{_SH}.catalog_source_id", return_value=tag) as tag_reader,
            patch(f"{_SH}.delete_lambda_catalog", delete_catalog or MagicMock()) as del_cat,
            patch(f"{_SH}.delete_config_parameter", delete_param or MagicMock()) as del_param,
            patch(f"{_SH}.cleanup_federated_resources") as cleanup,
            patch.object(_dbx, "CONFIG_SSM_PREFIX", config_prefix),
        ):
            status, body = _parse(_sh._handle_delete(_NAMESPACE_ID, self._SOURCE_ID))
        return status, body, mock_dao, del_cat, del_param, cleanup, tag_reader

    def test_tears_down_in_order_catalog_parameter_claim_record(self):
        order: list[str] = []
        mock_dao = MagicMock()

        def _record_delete(key):
            order.append("claim" if str(key.get("SK", "")) == "CLAIM" else "record")

        mock_dao.delete.side_effect = _record_delete
        status, _, mock_dao, del_cat, del_param, _, _ = self._delete(
            dao=mock_dao,
            tag=self._SOURCE_ID,
            delete_catalog=MagicMock(side_effect=lambda **kw: order.append("catalog")),
            delete_param=MagicMock(side_effect=lambda **kw: order.append("parameter")),
        )
        assert status == 200
        # None of the first three may outlive the record: it is the only handle on the
        # names they derive from, so anything left after it is unreachable.
        assert order == ["catalog", "parameter", "claim", "record"]

    def test_removes_the_catalog_and_the_parameter_by_derived_name(self):
        catalog_name = derive_catalog_name(self._SOURCE_ID)
        _, _, _, del_cat, del_param, _, _ = self._delete(tag=self._SOURCE_ID)
        del_cat.assert_called_once_with(catalog_name=catalog_name)
        del_param.assert_called_once_with(parameter_name=f"{_CONFIG_SSM_PREFIX}/{catalog_name}")

    def test_does_not_enter_the_federated_teardown(self):
        """``cleanup_federated_resources`` calls ``glue.delete_catalog``, which against a
        Lambda-backed catalog is a no-op that reports success while leaking the
        registration."""
        _, _, _, _, _, cleanup, _ = self._delete(tag=self._SOURCE_ID)
        cleanup.assert_not_called()

    def test_proceeds_when_the_catalog_carries_no_source_id_tag(self):
        """No tag means the catalog predates tagging or came from the CUSTOM_CONNECTOR
        path, and refusing it would make every such source undeletable."""
        status, _, _, del_cat, _, _, _ = self._delete(tag=None)
        assert status == 200
        del_cat.assert_called_once()

    def test_refuses_when_the_tag_names_a_different_source(self):
        """Deleting somebody else's catalog is the one outcome not recoverable from a log
        line."""
        status, body, mock_dao, del_cat, del_param, _, _ = self._delete(tag="some-other-source")
        assert status == 500
        del_cat.assert_not_called()
        del_param.assert_not_called()
        # The row survives, so the delete can be retried once the cause is understood.
        assert _source_deletes(mock_dao) == []

    def test_a_parameter_delete_failure_blocks_the_record_delete(self):
        """A parameter that outlived the row would be unreachable and still live."""
        status, _, mock_dao, _, _, _, _ = self._delete(
            tag=self._SOURCE_ID,
            delete_param=MagicMock(side_effect=_dbx.DatabricksConfigError("throttled")),
        )
        assert status == 500
        assert _source_deletes(mock_dao) == []

    def test_an_unresolvable_parameter_name_stops_the_delete_before_anything_is_destroyed(self):
        """The parameter's NAME comes from an environment variable, so it can go missing on
        a partial deploy. Resolved AFTER the catalog delete, that leaves a permanently
        undeletable source — catalog gone, parameter orphaned, and every retry reaching the
        same raise because the catalog delete is idempotent.
        """
        status, body, mock_dao, del_cat, del_param, _, tag_reader = self._delete(tag=self._SOURCE_ID, config_prefix="")
        assert status == 500
        assert "configuration parameter" in body["error"], body
        del_cat.assert_not_called()
        del_param.assert_not_called()
        # Not even the tag read, the teardown's first AWS call.
        tag_reader.assert_not_called()
        assert _source_deletes(mock_dao) == []

    def test_the_parameter_name_is_resolved_before_the_catalog_is_removed(self):
        """The other half of the same defect: a CHANGED prefix targets a name that does not
        exist, absence reads as success, and the real parameter is orphaned with its only
        handle — the row — deleted. The name has to be settled while the catalog is still
        there to be found again.
        """
        order: list[str] = []
        self._delete(
            tag=self._SOURCE_ID,
            delete_catalog=MagicMock(side_effect=lambda **kw: order.append("catalog")),
            delete_param=MagicMock(side_effect=lambda *, parameter_name: order.append(f"param:{parameter_name}")),
        )
        catalog_name = derive_catalog_name(self._SOURCE_ID)
        assert order == ["catalog", f"param:{_CONFIG_SSM_PREFIX}/{catalog_name}"]

    def test_releases_the_platform_catalog_claim_so_the_name_is_re_derivable(self):
        """An unreleased claim leaves the derived name owned by a source that no longer
        exists, and ``assert_namespace_may_catalog`` then refuses it to everyone."""
        _, _, mock_dao, _, _, _, _ = self._delete(tag=self._SOURCE_ID)
        claim_keys = [c.args[0] for c in mock_dao.delete.call_args_list if c.args[0].get("SK") == "CLAIM"]
        assert len(claim_keys) == 1
        assert claim_keys[0]["PK"] == f"GLUECAT#{derive_catalog_name(self._SOURCE_ID)}"

    def test_a_failed_row_delete_is_a_500_so_the_delete_can_be_retried(self):
        """A 200 here would leave a source whose resources are gone and whose retry looks
        like work already done."""
        mock_dao = MagicMock()

        def _delete_row(key):
            if str(key.get("SK", "")).startswith("SRC#") and str(key.get("PK", "")).startswith("NS#"):
                raise ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteItem")

        mock_dao.delete.side_effect = _delete_row
        status, _, mock_dao, _, _, _, _ = self._delete(tag=self._SOURCE_ID, dao=mock_dao)
        assert status == 500


@pytest.mark.unit
class TestCustomConnectorDeleteIsUnchanged:
    """A CUSTOM_CONNECTOR catalog is NOT tag-verified: its per-source handler ARN still
    discriminates, and reading tags it never wrote would turn a missing
    ``athena:ListTagsForResource`` into a 500 on every existing delete."""

    def test_a_custom_connector_delete_reads_no_tags_and_writes_no_parameter(self):
        source_id = "src-cc-1"
        item = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{source_id}",
            "sourceId": source_id,
            "sourceType": "DATABASE",
            "sourceSubType": "CUSTOM_CONNECTOR",
            "status": "APPROVED",
            "athenaDataCatalogName": derive_catalog_name(source_id),
        }
        mock_dao = MagicMock()
        mock_dao.get.return_value = item
        with (
            patch(f"{_SH}._get_dao", return_value=mock_dao),
            patch(f"{_SH}._delete_source_datazone_assets", return_value=(0, True)),
            patch(f"{_SH}._delete_source_scan_jobs", return_value=0),
            patch(f"{_SH}.catalog_source_id") as tag_reader,
            patch(f"{_SH}.delete_lambda_catalog") as del_cat,
            patch(f"{_SH}.delete_config_parameter") as del_param,
            patch(f"{_SH}.cleanup_federated_resources") as cleanup,
        ):
            status, _ = _parse(_sh._handle_delete(_NAMESPACE_ID, source_id))
        assert status == 200
        tag_reader.assert_not_called()
        del_param.assert_not_called()
        cleanup.assert_not_called()
        del_cat.assert_called_once_with(catalog_name=derive_catalog_name(source_id))
