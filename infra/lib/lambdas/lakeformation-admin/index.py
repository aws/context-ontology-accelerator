# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Custom resource — non-destructively registers an IAM role as an LF data-lake admin.

On Create/Update: reads the target role ARN from SSM, then GetDataLakeSettings ->
append the role to DataLakeAdmins if absent -> PutDataLakeSettings (full settings
object), so existing admins and other settings are preserved.

On Delete: removes the role recorded in this physical resource's own
``PhysicalResourceId`` (``lf-admin-{arn}``) — deliberately NOT the current SSM
value, which under CloudFormation replacement semantics points at the *new* role
by the time the old resource's cleanup Delete arrives. Best-effort, so an LF
error never blocks stack teardown, but failures are logged.

Returns the Provider-framework OnEventResponse shape.
"""

from __future__ import annotations

import logging
import re

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_ssm = boto3.client("ssm")
_lf = boto3.client("lakeformation")
_sts = boto3.client("sts")

# Same-account IAM role ARN. The target becomes a Lake Formation data-lake admin,
# so it must be a role we own — validating the format + account guards against a
# misconfigured/compromised SSM parameter escalating an arbitrary principal.
_ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/.+")


_PHYSICAL_ID_PREFIX = "lf-admin-"


def _validate(arn: str, source: str) -> str:
    """Validate ``arn`` is a same-account IAM role ARN, or raise."""
    m = _ROLE_ARN_RE.match(arn or "")
    if not m:
        raise ValueError(f"{source} is not an IAM role ARN: {arn!r}")
    try:
        account = _sts.get_caller_identity()["Account"]
    except ClientError as e:
        raise ValueError(f"Failed to retrieve AWS account id: {e}") from e
    if m.group(1) != account:
        raise ValueError(f"Refusing to register cross-account principal as LF admin: {arn!r}")
    return arn


def _target(event: dict) -> str:
    """Resolve and validate the target role ARN from the SSM parameter."""
    name = event["ResourceProperties"]["RoleArnSsmParameterName"]
    try:
        arn = _ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as e:
        raise ValueError(f"Failed to read SSM parameter {name!r}: {e}") from e
    return _validate(arn, f"SSM parameter {name!r}")


def _delete_target(event: dict) -> str | None:
    """Resolve the ARN this physical resource registered, from its own id.

    Delete must deregister the principal this physical resource created, encoded in
    ``PhysicalResourceId`` (``lf-admin-{arn}``), not the current SSM value: when an update
    changes the role ARN, CloudFormation sends a cleanup Delete for the OLD id.

    Returns ``None`` when the id carries no ARN (the ``"lf-admin"`` fallback a failed
    Create returns, or a resource created before the id carried the ARN), in which case
    Delete is a no-op.
    """
    physical_id = event.get("PhysicalResourceId") or ""
    if not physical_id.startswith(_PHYSICAL_ID_PREFIX):
        return None
    arn = physical_id[len(_PHYSICAL_ID_PREFIX) :]
    if not arn:
        return None
    # Still validate: the id is round-tripped through CloudFormation, and this
    # keeps the cross-account guard on the deregister path too.
    return _validate(arn, f"PhysicalResourceId {physical_id!r}")


def _apply(request_type: str, target: str) -> None:
    # Read-modify-write: preserve existing admins and all other settings.
    try:
        settings = _lf.get_data_lake_settings().get("DataLakeSettings", {})
        admins = settings.get("DataLakeAdmins", []) or []
        ids = {a.get("DataLakePrincipalIdentifier") for a in admins}
        if request_type == "Delete":
            admins = [a for a in admins if a.get("DataLakePrincipalIdentifier") != target]
        elif target not in ids:
            admins.append({"DataLakePrincipalIdentifier": target})
        settings["DataLakeAdmins"] = admins
        _lf.put_data_lake_settings(DataLakeSettings=settings)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "AccessDeniedException":
            # NOT a missing data-lake-admin registration: PutDataLakeSettings is authorized
            # by the IAM action, so a denial means the ACTION is blocked (an SCP, a
            # permission boundary, or the grant removed from the role).
            raise PermissionError(
                f"lakeformation:PutDataLakeSettings denied for this Lambda role — check for an SCP or "
                f"permission boundary denying the action (data-lake admin membership is NOT required): {e}"
            ) from e
        raise


def handler(event: dict, context: object = None) -> dict:
    """Register or deregister the target IAM role as a Lake Formation admin."""
    if event["RequestType"] == "Delete":
        # Deregister the principal recorded in this physical resource's id; see
        # _delete_target. Best-effort: never block stack deletion.
        try:
            target = _delete_target(event)
            if target is not None:
                _apply("Delete", target)
        except Exception:  # noqa: BLE001 - deliberately non-fatal
            logger.warning("failed to deregister LF admin on Delete", exc_info=True)
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "lf-admin")}
    target = _target(event)
    _apply(event["RequestType"], target)
    return {"PhysicalResourceId": f"{_PHYSICAL_ID_PREFIX}{target}"}
