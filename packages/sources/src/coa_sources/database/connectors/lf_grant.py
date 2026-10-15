# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lake Formation grant helper for Glue catalog onboarding.

Bridges the two governance modes a Glue Data Catalog can run in:

- **IAM-mode** (``IAM_ALLOWED_PRINCIPALS`` present): IAM ``glue:Get*`` permissions
  alone authorize catalog reads. No Lake Formation grant is needed; the helper is
  a no-op the caller never invokes (the initial Glue call succeeds).
- **Strict-LF mode** (``IAM_ALLOWED_PRINCIPALS`` removed): the Glue read is denied
  until the principal holds an LF ``DESCRIBE``/``SELECT`` grant on the resource,
  *in addition* to its IAM permissions.

To keep the discovery connector least-privilege, it is NOT itself a Lake
Formation admin. Instead, when it hits an ``AccessDenied`` on an LF-governed
database, it transiently assumes the dedicated LF-admin *grantor* role (the
federation provisioner, registered via the ``LakeFormationAdmin`` construct) and
grants ``DESCRIBE``/``SELECT`` to the principals that need them (itself, for
discovery; the serve runtime role, for querying). The grant is idempotent and
safe to re-run.

Cross-account caveat: a self-grant only works when the grantor role is an LF
admin in the *catalog's* account. For customer-owned cross-account catalogs the
grant attempt fails (the grantor isn't an admin there) and the caller falls back
to surfacing an actionable error telling the data owner which principals to grant
(see ``lf_onboarding_hint``).
"""

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# ARN of the LF-admin grantor role the connector assumes to issue grants.
# Set on the connector Lambda by the sources stack; empty when LF self-heal is
# not configured (the caller then falls back to the actionable-error path).
LF_GRANTOR_ROLE_ARN = os.getenv("LF_GRANTOR_ROLE_ARN", "")

# Serve-runtime principal that queries the data later; granted SELECT/DESCRIBE so
# Athena can read LF-governed tables once discovery succeeds. Taken from the env
# when set, else resolved lazily from the SSM parameter named by
# CONSUMER_QUERY_ROLE_SSM_PARAM (written by the serve stack).
CONSUMER_QUERY_ROLE_ARN = os.getenv("CONSUMER_QUERY_ROLE_ARN", "")
CONSUMER_QUERY_ROLE_SSM_PARAM = os.getenv("CONSUMER_QUERY_ROLE_SSM_PARAM", "")


def _consumer_query_role_arn() -> str:
    """Return the serve-runtime role ARN, reading SSM once on first use."""
    global CONSUMER_QUERY_ROLE_ARN
    if CONSUMER_QUERY_ROLE_ARN or not CONSUMER_QUERY_ROLE_SSM_PARAM:
        return CONSUMER_QUERY_ROLE_ARN
    try:
        CONSUMER_QUERY_ROLE_ARN = boto3.client("ssm", region_name=AWS_REGION).get_parameter(
            Name=CONSUMER_QUERY_ROLE_SSM_PARAM
        )["Parameter"]["Value"]
    except (ClientError, BotoCoreError):
        logger.warning("lf_self_grant: could not read %s", CONSUMER_QUERY_ROLE_SSM_PARAM, exc_info=True)
    return CONSUMER_QUERY_ROLE_ARN


def is_access_denied(exc: Exception) -> bool:
    """True when a boto3 exception is an authorization failure (IAM or LF)."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in ("AccessDeniedException", "AccessDenied")
    # Lake Formation surfaces denials via the message even on non-ClientError
    # wrappers in some SDK paths.
    return "explicit deny" in str(exc).lower() or "not authorized" in str(exc).lower()


def _caller_role_arn() -> str | None:
    """Resolve the *role* ARN of the current principal (not the assumed-role session).

    ``sts:GetCallerIdentity`` returns an assumed-role ARN like
    ``arn:aws:sts::<acct>:assumed-role/<RoleName>/<session>``. Lake Formation
    grants target the IAM role ARN ``arn:aws:iam::<acct>:role/<RoleName>``, so we
    rewrite it.
    """
    try:
        ident = boto3.client("sts", region_name=AWS_REGION).get_caller_identity()
    except ClientError:
        logger.warning("lf_self_grant: could not resolve caller identity", exc_info=True)
        return None
    arn = ident.get("Arn", "")
    parts = arn.split(":")
    if len(parts) < 6 or "assumed-role/" not in parts[5]:
        return arn or None
    account = parts[4]
    role_name = parts[5].split("/")[1]
    return f"arn:aws:iam::{account}:role/{role_name}"


def _lf_client_via_grantor(region: str):
    """Return an LF client authenticated as the LF-admin grantor role.

    Returns ``None`` when no grantor is configured or the assume fails (e.g. the
    grantor is not an admin in a cross-account catalog) — the caller then falls
    back to the actionable-error path.
    """
    if not LF_GRANTOR_ROLE_ARN:
        return None
    try:
        creds = boto3.client("sts", region_name=region).assume_role(
            RoleArn=LF_GRANTOR_ROLE_ARN,
            RoleSessionName="coa-lf-self-grant",
        )["Credentials"]
    except ClientError:
        logger.warning("lf_self_grant: assume grantor role failed", exc_info=True)
        return None
    return boto3.client(
        "lakeformation",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _grant(lf, principal_arn: str, database: str, catalog_id: str, table_perms: list[str]) -> None:
    """Grant database DESCRIBE + table-wildcard permissions to one principal.

    Idempotent: re-granting an existing permission is a no-op in Lake Formation.
    """
    db_resource: dict = {"Database": {"Name": database}}
    tbl_resource: dict = {"Table": {"DatabaseName": database, "TableWildcard": {}}}
    if catalog_id:
        db_resource["Database"]["CatalogId"] = catalog_id
        tbl_resource["Table"]["CatalogId"] = catalog_id
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": principal_arn},
        Resource=db_resource,
        Permissions=["DESCRIBE"],
    )
    lf.grant_permissions(
        Principal={"DataLakePrincipalIdentifier": principal_arn},
        Resource=tbl_resource,
        Permissions=table_perms,
    )


def attempt_self_grant(database: str, catalog_id: str, region: str, *, owner_verified: bool) -> bool:
    """Best-effort: grant the connector (DESCRIBE) and serve role (SELECT) LF access.

    Returns ``True`` when grants were issued (caller should retry the Glue call),
    ``False`` when self-heal is unavailable (no grantor configured, assume failed,
    or cross-account) so the caller surfaces an actionable error instead.

    ``owner_verified`` MUST be the result of a namespace-ownership check on
    ``(catalog_id, database)`` — see ``coa_sources.database.glue_ownership``. This
    function is the platform's privilege-escalation primitive: it assumes a Lake
    Formation admin role and can unlock SELECT on any database in the account, for
    the shared serve role as well as itself. A caller that cannot say the target
    belongs to the requesting namespace gets no grant. Required rather than
    defaulted so that a new call site has to answer the question, and answering it
    wrong is a visible argument rather than an omission.
    """
    if not owner_verified:
        logger.warning(
            "lf_self_grant_refused_owner_unverified",
            extra={"database": database, "catalog_id": catalog_id},
        )
        return False

    lf = _lf_client_via_grantor(region)
    if lf is None:
        return False

    consumer_role = _consumer_query_role_arn()
    if not consumer_role and CONSUMER_QUERY_ROLE_SSM_PARAM:
        # The serve role is configured but its ARN could not be read. Fail the
        # self-heal so onboarding reports it, rather than succeeding without the
        # serve role's SELECT grant and failing later at query time. The next
        # attempt re-reads the parameter.
        logger.error(
            "lf_self_grant: could not resolve the serve runtime role from %s; no grants issued",
            CONSUMER_QUERY_ROLE_SSM_PARAM,
        )
        return False

    connector_arn = _caller_role_arn()
    granted_any = False
    try:
        if connector_arn:
            _grant(lf, connector_arn, database, catalog_id, ["DESCRIBE"])
            granted_any = True
        if consumer_role:
            _grant(lf, consumer_role, database, catalog_id, ["SELECT", "DESCRIBE"])
        else:
            logger.warning(
                "lf_self_grant: CONSUMER_QUERY_ROLE_ARN not set — serve runtime role will not "
                "receive Lake Formation SELECT grant. Queries against this database will fail "
                "at query time unless the grant is applied manually."
            )
    except ClientError:
        # Grantor lacks admin in this catalog's account (cross-account) or the
        # resource isn't LF-registered. Fall back to the actionable error.
        logger.warning("lf_self_grant: grant_permissions failed", exc_info=True)
        return granted_any
    return granted_any


def lf_onboarding_hint(database: str, catalog_id: str) -> str:
    """Actionable message for the Option A fallback (customer-prerequisite grant)."""
    cat = f" (catalog {catalog_id})" if catalog_id else ""
    consumer = _consumer_query_role_arn()
    if not consumer and CONSUMER_QUERY_ROLE_SSM_PARAM:
        # Not a missing grant: the serve role itself could not be resolved, so
        # attempt_self_grant refused to grant anything. Say so instead of
        # suggesting a cross-account prerequisite with a placeholder principal.
        return (
            f"Database '{database}'{cat} is Lake Formation-governed, and Context Ontology Accelerator could not "
            f"read the serve runtime role ARN from SSM parameter {CONSUMER_QUERY_ROLE_SSM_PARAM}, so it granted "
            f"no access. Check that the serve stack is deployed and the discovery role can read that parameter, "
            f"then retry."
        )
    connector_arn = _caller_role_arn() or "<coa-discovery-connector-role-arn>"
    consumer = consumer or "<coa-serve-runtime-role-arn>"
    return (
        f"Database '{database}'{cat} is Lake Formation-governed and Context Ontology Accelerator could not "
        f"self-grant access (this is expected for cross-account or customer-owned catalogs). "
        f"As a Lake Formation admin, grant DESCRIBE to the Context Ontology Accelerator discovery role and "
        f"SELECT/DESCRIBE to the Context Ontology Accelerator serve role, then retry:\n"
        f"  aws lakeformation grant-permissions "
        f"--principal DataLakePrincipalIdentifier={connector_arn} "
        f'--resource \'{{"Database":{{"Name":"{database}"}}}}\' --permissions DESCRIBE\n'
        f"  aws lakeformation grant-permissions "
        f"--principal DataLakePrincipalIdentifier={connector_arn} "
        f'--resource \'{{"Table":{{"DatabaseName":"{database}","TableWildcard":{{}}}}}}\' '
        f"--permissions DESCRIBE\n"
        f"  aws lakeformation grant-permissions "
        f"--principal DataLakePrincipalIdentifier={consumer} "
        f'--resource \'{{"Table":{{"DatabaseName":"{database}","TableWildcard":{{}}}}}}\' '
        f"--permissions SELECT DESCRIBE"
    )
