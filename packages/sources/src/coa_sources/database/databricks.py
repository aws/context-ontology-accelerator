# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Registration-time rules and the connector configuration store for ``DATABRICKS_SQL_WAREHOUSE``.

Security invariants, all of which have no local evidence in the code:

* The configuration parameter is a plaintext ``String``, which is only defensible
  while nothing in the payload is secret. ``write_config_parameter`` enforces that.
* The parameter path must carry the environment segment. Environments share an
  account, so without it a dev sources-API role can repoint a prod source's
  parameter and a dev query then resolves prod's credential.
* Neither ARN is compared against the deployment account, deliberately: COA is
  deployed *into* the customer's account, so the single-account topology is the
  common one. What bounds COA is the reserved role-name prefix plus the target
  role's own trust policy.
* Nothing here opens a Databricks connection. Registration validates the wiring
  (assume, then ``DescribeSecret`` — never ``GetSecretValue``) and leaves
  reachability to the first scan.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from coa_common import resolve_region
from coa_common.aws_config import sync_boto_config
from coa_common.constants import datasource_external_id, require_resource_prefix
from pydantic import BaseModel, ConfigDict, Field

from coa_sources.database.connectors.sts_assume import assume_datasource_session

logger = structlog.get_logger(__name__)

AWS_REGION = resolve_region()

# Set by the connector's own CDK app, which the platform deploy cannot see at synth.
CONNECTOR_ARN_SSM_PARAM = os.environ.get("DATABRICKS_CONNECTOR_ARN_SSM_PARAM", "")

# ``${ssmPrefix}/${envName}/connectors/databricks/sources``. The ${envName} segment is
# what keeps a dev registration from writing over a prod source's parameter.
CONFIG_SSM_PREFIX = os.environ.get("DATABRICKS_CONFIG_SSM_PREFIX", "")

# Reserved role-name prefix COA's `sts:AssumeRole` grant is scoped to (sid
# `AssumeRoleCoaManaged`, `arn:aws:iam::*:role/{RESOURCE_PREFIX}datasource-access-*`).
_ROLE_NAME_INFIX = "datasource-access-"

# Failures that are AWS's problem rather than a statement about the customer's wiring.
# They become a 503 so the caller retries instead of believing the ARN was rejected.
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
        "InternalServiceError",
        "InternalServiceErrorException",
        "RequestExpired",
    }
)

# Secrets Manager's way of saying the SecretId is not something it can act on: a malformed
# structure, or a partition it does not serve. Kept apart from the denial branch below
# because that one tells the operator to add `secretsmanager:DescribeSecret` to the role,
# which fixes nothing here and is the one edit that widens a policy for no reason.
_ARN_REJECTED_ERROR_CODES = frozenset({"InvalidParameterException", "InvalidRequestException", "ValidationException"})

_ssm = None


def _ssm_client():
    global _ssm  # noqa: PLW0603
    if _ssm is None:
        _ssm = boto3.client("ssm", region_name=AWS_REGION, config=sync_boto_config())
    return _ssm


class DatabricksConfigError(RuntimeError):
    """Writing or removing a connector configuration parameter failed."""


class DatabricksConfigExistsError(DatabricksConfigError):
    """A configuration parameter of that name already exists.

    Distinct from its parent so the create path reports a collision rather than a
    transient write failure, and so a rollback knows not to delete a parameter this
    call did not write.
    """


class DatabricksConnectorUnavailableError(RuntimeError):
    """No Databricks connector is deployed in this environment.

    Covers both a missing parameter name and a missing parameter: discovery runs
    *through* the connector, so a source registered now could never reach review.
    """


def resource_prefix() -> str:
    """This deployment's ``{prefix}-{env}-`` resource prefix.

    Same accessor as ``derive_catalog_name`` and ``datasource_external_id``; all three
    appear in the contract the customer's trust policy and role name are written against,
    and all three refuse to default. See ``require_resource_prefix``.
    """
    return require_resource_prefix()


def deployment_id() -> str:
    """Deployment identifier the connector checks its own environment against.

    The connector's CDK app applies this same rule to the resource prefix when setting
    ``COA_DEPLOYMENT_ID``, and the connector refuses a parameter whose ``deploymentId``
    disagrees with what it was given. Change both sides together.
    """
    return resource_prefix().rstrip("-")


def reserved_role_name_prefix() -> str:
    """The ``{RESOURCE_PREFIX}datasource-access-`` name prefix COA will assume within."""
    return f"{resource_prefix()}{_ROLE_NAME_INFIX}"


def role_path_and_name(role_arn: str) -> str:
    """Everything after ``role/`` in a role ARN — the path AND the name.

    Path included because that is what IAM matches a ``Resource`` wildcard against: a
    role under a path (``role/team/a/coa-dev-datasource-access-dbx``) does not match
    ``role/coa-dev-datasource-access-*`` however its trailing name is spelled.
    Comparing only the last segment accepts such a role here and then has COA's own
    policy deny the assume.
    """
    resource = role_arn.rpartition(":")[2]
    return resource.removeprefix("role/")


def role_name_is_reserved(role_arn: str) -> bool:
    """Whether ``role_arn`` falls inside the reserved prefix COA will assume within."""
    return role_path_and_name(role_arn).startswith(reserved_role_name_prefix())


def arn_region(arn: str) -> str:
    """Region segment of an ARN, or ``""`` when it has no parsable one."""
    parts = (arn or "").split(":")
    return parts[3] if len(parts) > 5 else ""


# Python's `$` matches BEFORE a trailing newline, so the generated Smithy validators
# accept `…:role/coa-dev-datasource-access-b\n` (verified). Downstream consumers then
# disagree about the value: the prefix check still passes, the binding record keys its
# partition on the raw string (so the same role bound by another namespace lands under a
# different key, bypassing the binding), and the Java connector trims — so it would
# assume the canonical role while COA recorded the other one.
_WHITESPACE = re.compile(r"\s")


def normalise_arn(value: str | None) -> str:
    """Strip surrounding whitespace from an ARN so every consumer sees one spelling.

    Must run before the value reaches a prefix check, a partition key, the stored blob
    or the parameter, because the Java side trims too. Whitespace surviving the strip is
    rejected by :func:`arn_has_inner_whitespace`.
    """
    return (value or "").strip()


def arn_has_inner_whitespace(arn: str) -> bool:
    """Whether a stripped ARN still contains whitespace, which no valid ARN does."""
    return bool(_WHITESPACE.search(arn))


# Mirrors `ConnectionConfig.SECRET_ARN` in the Java connector, character for character:
# the shared Smithy `SecretArn` is wider, so without this check a source registers
# cleanly and then fails every query on the connector's own re-validation. Applied here
# rather than by narrowing `SecretArn`, which `JdbcConfiguration` and `GlueConfiguration`
# also use — tightening it would retroactively invalidate their registered sources.
_CONNECTOR_SECRET_ARN = re.compile(r"^arn:[a-z0-9-]+:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+$")


def secret_arn_is_connector_readable(secret_arn: str) -> bool:
    """Whether the connector's own secret-ARN pattern accepts ``secret_arn``."""
    return bool(_CONNECTOR_SECRET_ARN.fullmatch(secret_arn))


# Narrower than `ManagedSource.IAM_ROLE_ARN` in the Java connector in one place, and
# narrower than the shared Smithy `IamRoleArn` in two.
#
# The path-and-name set, for the same reason as `_CONNECTOR_SECRET_ARN`: the Smithy
# `IamRoleArn` permits an IAM path that admits `;`, `%` and quote characters, so
# `…:role/coa-dev-datasource-access-a/x;y/name` clears registration and then fails every
# query on the connector's re-validation.
#
# The PARTITION, pinned to `aws`, because this platform's own assume grant is written
# `arn:aws:iam::*:role/{RESOURCE_PREFIX}datasource-access-*`. A partition-agnostic pattern
# accepts an `aws-cn` or `aws-us-gov` role, registration succeeds, and every assume is then
# denied by our own policy with an AccessDenied whose message points the customer at their
# principal and their ExternalId condition — both of which are correct. Refused at submit
# instead, with the partition named. The Java side stays partition-agnostic: registration is
# the only writer of these values, so it is the place the refusal belongs.
_CONNECTOR_ROLE_ARN = re.compile(r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+$")


def role_arn_is_connector_assumable(role_arn: str) -> bool:
    """Whether the connector's own role-ARN pattern accepts ``role_arn``."""
    return bool(_CONNECTOR_ROLE_ARN.fullmatch(role_arn))


class DatabricksConnectorConfig(BaseModel):
    """The body of one connector configuration parameter.

    The connector re-validates every field it reads against the same patterns
    registration applies, so a key renamed on one side and not the other fails every
    query for that source.

    ``namespaceId`` is the **requesting** namespace, written server-side; the connector
    passes it through ``datasource_external_id`` to obtain the ExternalId for its
    assume.
    """

    model_config = ConfigDict(populate_by_name=True)

    source_id: str = Field(serialization_alias="sourceId")
    namespace_id: str = Field(serialization_alias="namespaceId")
    deployment_id: str = Field(serialization_alias="deploymentId")
    workspace_hostname: str = Field(serialization_alias="workspaceHostname")
    http_path: str = Field(serialization_alias="httpPath")
    databricks_catalog: str = Field(serialization_alias="databricksCatalog")
    database_name: str = Field(serialization_alias="databaseName")
    credential_secret_arn: str = Field(serialization_alias="credentialSecretArn")
    cross_account_role_arn: str = Field(serialization_alias="crossAccountRoleArn")

    def to_parameter_value(self) -> str:
        """Serialise to the exact JSON the connector parses, camelCase keys and all."""
        return json.dumps(self.model_dump(by_alias=True))


def config_parameter_name(catalog_name: str) -> str:
    """Parameter name holding the configuration for ``catalog_name``.

    Keyed on the Athena catalog name because that is the only thing every metadata and
    record request carries.
    """
    if not CONFIG_SSM_PREFIX:
        raise DatabricksConfigError(
            "DATABRICKS_CONFIG_SSM_PREFIX is not set, so the connector configuration parameter "
            "has no path to be written to"
        )
    return f"{CONFIG_SSM_PREFIX.rstrip('/')}/{catalog_name}"


def resolve_connector_function_arn(client=None) -> str:
    """Function ARN of the Databricks connector deployed in this environment.

    Raises:
        DatabricksConnectorUnavailableError: no connector is deployed here.
    """
    if not CONNECTOR_ARN_SSM_PARAM:
        raise DatabricksConnectorUnavailableError(
            "No Databricks connector is deployed in this environment "
            "(DATABRICKS_CONNECTOR_ARN_SSM_PARAM is not set on the sources API)."
        )
    ssm = client or _ssm_client()
    try:
        arn = ssm.get_parameter(Name=CONNECTOR_ARN_SSM_PARAM)["Parameter"]["Value"].strip()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            raise DatabricksConnectorUnavailableError(
                f"No Databricks connector is deployed in this environment: the deployment "
                f"parameter '{CONNECTOR_ARN_SSM_PARAM}' does not exist. Deploy the connector's "
                f"CDK app before registering a Databricks source."
            ) from exc
        raise DatabricksConfigError(f"Could not resolve the Databricks connector ARN: {exc}") from exc
    except BotoCoreError as exc:
        raise DatabricksConfigError(f"Could not resolve the Databricks connector ARN: {exc}") from exc
    if not arn:
        raise DatabricksConnectorUnavailableError(
            f"No Databricks connector is deployed in this environment: the deployment "
            f"parameter '{CONNECTOR_ARN_SSM_PARAM}' is empty."
        )
    return arn


def write_config_parameter(*, catalog_name: str, config: DatabricksConnectorConfig, client=None) -> str:
    """Write the connector configuration for ``catalog_name``, refusing to overwrite.

    Returns the parameter name written, for the caller's rollback.

    Raises:
        DatabricksConfigExistsError: a parameter of that name already exists. This call
            did not write it, so a caller rolling back must NOT delete it.
        DatabricksConfigError: the write failed for any other reason. The parameter may
            or may not exist afterwards — ``PutParameter`` can commit server-side and
            then have its response time out — so roll back with a best-effort delete.
    """
    name = config_parameter_name(catalog_name)
    # Nothing secret can reach the parameter: DatabricksConnectorConfig declares nine string
    # fields and `model_dump` emits exactly those, so the payload's key set is fixed by the
    # type. test_the_body_matches_the_golden_fixture_key_for_key is what pins it.
    value = config.to_parameter_value()
    ssm = client or _ssm_client()
    try:
        ssm.put_parameter(
            Name=name,
            Value=value,
            # Not SecureString — see this module's docstring.
            Type="String",
            # The name is a pure function of the source id, so a collision means an
            # assumption has broken and must fail the registration loudly.
            Overwrite=False,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterAlreadyExists":
            raise DatabricksConfigExistsError(
                f"A Databricks connector configuration already exists at {name!r}; refusing to overwrite it"
            ) from exc
        raise DatabricksConfigError(f"Failed to write the Databricks connector configuration {name!r}: {exc}") from exc
    except BotoCoreError as exc:
        raise DatabricksConfigError(f"Failed to write the Databricks connector configuration {name!r}: {exc}") from exc
    logger.info("databricks_config_parameter_written", parameter_name=name, source_id=config.source_id)
    return name


def delete_config_parameter(*, parameter_name: str, client=None) -> None:
    """Remove a connector configuration parameter, treating absence as success.

    Raises:
        DatabricksConfigError: the parameter exists and could not be removed. The caller
            must surface this rather than proceeding — the parameter is what resolves a
            credential and the source record is what documents that it exists, so the
            record must not be deleted first.
    """
    ssm = client or _ssm_client()
    try:
        ssm.delete_parameter(Name=parameter_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            logger.info("databricks_config_parameter_already_absent", parameter_name=parameter_name)
            return
        raise DatabricksConfigError(
            f"Failed to delete the Databricks connector configuration {parameter_name!r}: {exc}"
        ) from exc
    except BotoCoreError as exc:
        raise DatabricksConfigError(
            f"Failed to delete the Databricks connector configuration {parameter_name!r}: {exc}"
        ) from exc
    logger.info("databricks_config_parameter_deleted", parameter_name=parameter_name)


@dataclass(frozen=True)
class WiringVerdict:
    """Outcome of the credential-wiring check.

    ``reason`` is a stable code for logs and tests; ``retryable`` marks an infrastructure
    failure, which the API turns into a 503 rather than a rejection of the wiring.
    """

    ok: bool
    reason: str
    message: str = ""
    retryable: bool = False


def _assume_error_code(exc: ValueError) -> str:
    """STS error code carried by an ``assume_datasource_session`` failure.

    That helper masks the ``ClientError`` deliberately, so the role ARN cannot leak into an
    API response, and puts the code on :class:`DatasourceAssumeError` as an attribute. Read
    as an attribute rather than split back out of the message: every branch below turns on
    this value, and a reworded message would make the whole set stop matching and return a
    server error on every registration.

    A plain ``ValueError`` from the same call is an empty-argument rejection, which carries
    no STS code. It reads as ``""``, which matches no branch and is treated as inconclusive.
    """
    return getattr(exc, "code", "")


# Appended to a namespace's real ExternalId to build a value no namespace can hold.
# Namespace ids are UUIDs, so no legitimate derived value ends in this.
_NEGATIVE_PROBE_SUFFIX = "-not-this-namespace"


def _refuse_unconditioned_role(
    *, role_arn: str, external_id: str, session_name: str, source_id: str
) -> WiringVerdict | None:
    """Prove the trust policy *requires* the ExternalId rather than merely tolerating it.

    STS silently ignores an ExternalId a trust policy does not ask for, so the positive
    assume succeeds either way. This one presents a value no namespace can hold: denial
    proves the condition is enforced, success proves the role is assumable on any
    namespace's behalf.

    The ExternalId must be the ONLY input that differs from the positive assume, hence
    ``session_name`` being passed in. A trust policy may also condition on
    ``sts:RoleSessionName``, and a probe carrying its own session name would be denied by
    that condition — reading as proof of the thing being tested. For the same reason only
    ``AccessDenied`` counts as proof.

    Returns ``None`` when the condition is enforced.
    """
    probe = f"{external_id}{_NEGATIVE_PROBE_SUFFIX}"
    try:
        assume_datasource_session(role_arn, probe, AWS_REGION, session_name, sync_boto_config())
    except ValueError as exc:
        code = _assume_error_code(exc)
        if code == "AccessDenied":
            return None
        if code in _TRANSIENT_ERROR_CODES:
            logger.warning("databricks_wiring_probe_unavailable", code=code, source_id=source_id)
            return WiringVerdict(
                ok=False,
                reason="probe-unavailable",
                message=(
                    f"Could not verify that crossAccountRoleArn's trust policy requires an "
                    f"sts:ExternalId condition: sts:AssumeRole was unavailable ({code}). Nothing is "
                    f"wrong with the ARN or the policies — retry."
                ),
                retryable=True,
            )
        logger.warning("databricks_wiring_probe_inconclusive", code=code, source_id=source_id)
        return WiringVerdict(
            ok=False,
            reason="probe-inconclusive",
            message=(
                f"Could not verify that crossAccountRoleArn's trust policy requires an "
                f"sts:ExternalId condition: the check was refused with {code} rather than "
                f"AccessDenied, so it proved nothing either way. The role was assumable a moment "
                f"earlier, so this is not a statement about the ARN or the trust policy — retry."
            ),
            retryable=True,
        )

    logger.warning("databricks_wiring_external_id_not_required", source_id=source_id)
    return WiringVerdict(
        ok=False,
        reason="external-id-not-required",
        message=(
            f'crossAccountRoleArn was assumed while presenting "{probe}", which belongs to no '
            f"namespace. Its trust policy therefore does not require an sts:ExternalId condition, "
            f"or requires one loose enough to match any value — so any namespace in this "
            f"deployment can have this service assume the role and read the credential on its "
            f"behalf.\n"
            f'Add Condition StringEquals sts:ExternalId = "{external_id}" to the statement naming '
            f"this deployment's principals. To share one role between namespaces deliberately, "
            f"list each namespace's value there."
        ),
    )


def validate_credential_wiring(
    *,
    role_arn: str,
    secret_arn: str,
    namespace_id: str,
    source_id: str,
) -> WiringVerdict:
    """Prove COA can assume the registered role and that the role can read the secret.

    Three AWS calls and no Databricks connection: assume ``role_arn`` with the
    namespace's ExternalId, assume it again with a value no namespace can hold, then
    ``DescribeSecret`` through the first session. The secret's value is never read.

    The second assume is what makes the role's own trust policy the authority on which
    namespaces may use the credential — see :func:`_refuse_unconditioned_role`. Without
    it, a policy that names this deployment's principals but omits the condition passes
    registration and is then assumable on any namespace's behalf.

    ``datasource_external_id`` derives the ExternalId rather than this function, because
    the Java connector mirrors the same derivation and a drift between them fails every
    onboarding with an ``AccessDenied`` that points at nothing.

    Each message must name the action or condition that actually failed. An operator
    facing an opaque ``AccessDenied`` reaches for the two fixes that break the model:
    widening the role to ``secretsmanager:*``, or naming platform principals in the trust
    policy without the ``sts:ExternalId`` condition. STS returns the same
    ``AccessDenied`` for a missing principal and for a mismatched ExternalId, so that one
    message has to name both causes.
    """
    external_id = datasource_external_id(namespace_id)
    session_name = f"coa-dbx-validate-{namespace_id}-{source_id}"
    try:
        session = assume_datasource_session(
            role_arn,
            external_id,
            AWS_REGION,
            session_name,
            sync_boto_config(),
        )
    except ValueError as exc:
        code = _assume_error_code(exc)
        if code in _TRANSIENT_ERROR_CODES:
            logger.warning("databricks_wiring_assume_unavailable", code=code, source_id=source_id)
            return WiringVerdict(
                ok=False,
                reason="assume-unavailable",
                message=(
                    f"Could not verify crossAccountRoleArn: sts:AssumeRole was unavailable ({code}). "
                    f"Nothing is wrong with the ARN or the policies — retry."
                ),
                retryable=True,
            )
        logger.warning("databricks_wiring_assume_denied", code=code, source_id=source_id)
        return WiringVerdict(
            ok=False,
            reason="assume-denied",
            message=(
                f"crossAccountRoleArn could not be assumed by sts:AssumeRole ({code}). STS returns "
                f"the same AccessDenied for two different causes and cannot distinguish them, so "
                f"check BOTH:\n"
                f"  1. the trust policy's Principal must name this deployment's SOURCES-API role, "
                f"which is the caller of this check. Its connector role must be named too — that "
                f"is the caller at query time — so both belong in the same statement; the "
                f"onboarding guide lists the two ARNs.\n"
                f'  2. the trust policy must carry Condition StringEquals sts:ExternalId = "'
                f'{external_id}". That is the exact value sent, and is also published as this '
                f"namespace's datasourceExternalId.\n"
                f"Do NOT drop the ExternalId condition to get past this. It is what stops another "
                f"namespace naming your role and having this service read your credential on its "
                f"behalf."
            ),
        )

    unconditioned = _refuse_unconditioned_role(
        role_arn=role_arn,
        external_id=external_id,
        session_name=session_name,
        source_id=source_id,
    )
    if unconditioned is not None:
        return unconditioned

    secret_region = arn_region(secret_arn) or AWS_REGION
    try:
        session.client("secretsmanager", region_name=secret_region).describe_secret(SecretId=secret_arn)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            logger.warning("databricks_wiring_secret_absent", source_id=source_id)
            return WiringVerdict(
                ok=False,
                reason="secret-absent",
                message=(
                    "secretsmanager:DescribeSecret on credentialSecretArn reported that no such "
                    "secret exists, as seen from crossAccountRoleArn's own account. This is not a "
                    "permission problem — the role was assumed and the call was allowed — so check "
                    "the ARN itself, including its 6-character suffix, and that the secret lives in "
                    "the account the role does."
                ),
            )
        if code in _ARN_REJECTED_ERROR_CODES:
            logger.warning("databricks_wiring_secret_arn_rejected", code=code, source_id=source_id)
            return WiringVerdict(
                ok=False,
                reason="secret-arn-invalid",
                message=(
                    f"secretsmanager:DescribeSecret refused credentialSecretArn as a value it cannot "
                    f"act on ({code}). The ARN itself is what to fix, not a policy: the role was "
                    f"assumed and the call reached Secrets Manager, so this is not a permission "
                    f"problem. Two causes produce it — an ARN whose structure is wrong (a missing "
                    f"6-character suffix, a stray segment, a name Secrets Manager does not permit), "
                    f"and an ARN naming a partition other than 'aws', which this deployment cannot "
                    f"reach at all. Re-copy the ARN from the secret's own console page or from "
                    f"`aws secretsmanager describe-secret`."
                ),
            )
        if code in _TRANSIENT_ERROR_CODES:
            logger.warning("databricks_wiring_describe_unavailable", code=code, source_id=source_id)
            return WiringVerdict(
                ok=False,
                reason="describe-unavailable",
                message=(
                    f"Could not verify credentialSecretArn: secretsmanager:DescribeSecret was "
                    f"unavailable ({code}). Nothing is wrong with the ARN or the policies — retry."
                ),
                retryable=True,
            )
        logger.warning("databricks_wiring_secret_unreadable", code=code, source_id=source_id)
        return WiringVerdict(
            ok=False,
            reason="secret-unreadable-by-role",
            message=(
                f"crossAccountRoleArn was assumed successfully, but secretsmanager:DescribeSecret on "
                f"credentialSecretArn was denied ({code}). That is the action to add — registration "
                f"reads the secret's METADATA only and never calls GetSecretValue, so holding "
                f"GetSecretValue already does not satisfy this check.\n"
                f"The role needs BOTH actions on that one secret, for two different callers: "
                f"secretsmanager:DescribeSecret for this registration check, and "
                f"secretsmanager:GetSecretValue for the connector at query time. Add kms:Decrypt on "
                f"the key as well when the key is customer-managed — which AWS REQUIRES when the "
                f"secret and the role are in different accounts, since aws/secretsmanager cannot be "
                f"shared across accounts by any policy.\n"
                f"Do NOT widen the role to secretsmanager:* to get past this; the two named actions "
                f"on the one named secret are the whole of what is needed."
            ),
        )
    except BotoCoreError:
        logger.warning("databricks_wiring_describe_unavailable", source_id=source_id, exc_info=True)
        return WiringVerdict(
            ok=False,
            reason="describe-unavailable",
            message=(
                "Could not verify credentialSecretArn: secretsmanager:DescribeSecret could not be "
                "reached. Nothing is wrong with the ARN or the policies — retry."
            ),
            retryable=True,
        )

    logger.info("databricks_wiring_validated", source_id=source_id, namespace_id=namespace_id)
    return WiringVerdict(ok=True, reason="wired")
