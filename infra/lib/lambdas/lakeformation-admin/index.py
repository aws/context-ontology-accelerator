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

import re

import boto3
from botocore.exceptions import ClientError

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

    Delete MUST deregister the principal *this* physical resource created, which
    is encoded in ``PhysicalResourceId`` (``lf-admin-{arn}``) — NOT whatever the
    SSM parameter happens to point at now. When an update changes the role ARN,
    CloudFormation creates the new physical resource first and then sends a
    cleanup Delete for the OLD id; resolving from SSM at that point returns the
    NEW arn, so the handler deregistered the role it had just registered and left
    the stale one in place. Every LF-privileged operation downstream (federated
    catalog creation during JDBC scans, LF-governed catalog teardown) then failed
    with authorization errors unrelated-looking to the deploy — and
    ``contextlib.suppress`` in the caller hid it while the deployment reported
    success.

    Returns ``None`` when the id carries no ARN (the ``"lf-admin"`` fallback a
    failed Create returns, or a resource created before the id carried the ARN),
    in which case there is nothing safe to deregister and Delete is a no-op.
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
            # Bootstrap chicken-and-egg: this Lambda's role must itself be an LF
            # admin in an account that already has admins (see infra/README.md).
            raise PermissionError(f"Lambda role must be a Lake Formation data-lake admin to manage admins: {e}") from e
        raise


def handler(event: dict, context: object = None) -> dict:
    """Register or deregister the target IAM role as a Lake Formation admin.

    CloudFormation custom-resource entry point. On Create/Update, resolves and
    validates the target role ARN from SSM and appends it to the data-lake
    admins (read-modify-write, preserving existing settings). On Delete, removes
    it best-effort so an LF error never blocks stack teardown.

    Args:
        event: Custom-resource event with ``RequestType`` and
            ``ResourceProperties.RoleArnSsmParameterName``.
        context: Lambda runtime context (unused).

    Returns:
        A Provider-framework ``OnEventResponse`` dict carrying
        ``PhysicalResourceId``.
    """
    if event["RequestType"] == "Delete":
        # Deregister the principal recorded in THIS physical resource's id, not
        # the current SSM value — see _delete_target. Best-effort: never block
        # stack deletion on an LF error, but log it, since a swallowed failure
        # here is how a stale admin silently survives a teardown.
        try:
            target = _delete_target(event)
            if target is not None:
                _apply("Delete", target)
        except Exception as e:  # noqa: BLE001 - deliberately non-fatal
            print(f"WARNING: failed to deregister LF admin on Delete: {type(e).__name__}: {e}")
        return {"PhysicalResourceId": event.get("PhysicalResourceId", "lf-admin")}
    target = _target(event)
    _apply(event["RequestType"], target)
    return {"PhysicalResourceId": f"{_PHYSICAL_ID_PREFIX}{target}"}
