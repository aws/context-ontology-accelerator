# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Athena federation provisioner (managed Glue Data Catalog federated catalog).

For each JDBC data source we create a queryable source under Athena's default
``AwsDataCatalog`` — governed by Lake Formation, with **no connector Lambda and
no CloudFormation stack** in the account. The steps mirror exactly what the
Athena "create PostgreSQL data source" console flow produces:

  1. A Glue Connection with ``AthenaProperties.MANAGED_CONNECTION=true`` (AWS runs
     the connector as a managed service) and ``ConnectionProperties.ROLE_ARN`` set
     to the Glue Data Catalog access role. No ``lambda_function_arn``.
  2. Lake Formation ``register-resource --with-federation`` on the connection.
  3. A Glue federated catalog (``create-catalog`` with a ``FederatedCatalog``
     block); catalog name = connection name = identifier.

The result is queryable as ``AwsDataCatalog.<catalog>.<schema>.<table>`` (the
catalog is a nested catalog inside the account's Glue Data Catalog; the
PostgreSQL schema is the Athena "database" level).

Why not the alternatives: the SAR multiplexer connector is incompatible with the
Glue-federation path (Athena passes the account id as the catalog name), and
``athena:CreateDataCatalog Type=FEDERATED`` produces a *separate* Athena catalog
(not under ``AwsDataCatalog``) and fails without broad provisioning IAM. The
``MANAGED_CONNECTION`` Glue-federated path is the one that actually works and is
fully scriptable.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from coa_common.aws_config import is_throttling_error

from coa_sources.database.connectors.dialects import DISCOVERY_ENGINES
from coa_sources.database.metrics import emit_metric

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Glue/LF provisioning is a handful of calls per source onboarding, so we rely
# on botocore's built-in retries (standard mode: exponential backoff + jitter on
# throttling) rather than hand-managing retries.
_RETRY_CONFIG = Config(retries={"mode": "standard"})

# Lake Formation federated catalog names must be lowercase; keep them short and
# collision-resistant. Connection name == catalog name == identifier.
_CATALOG_NAME_MAX = 41

# Glue's CreateConnection validates ConnectionProperties per connection type.
# The managed Redshift connector rejects CATALOG_CASING_FILTER ("...are not
# allowed in the request object") — it already folds identifiers to lowercase,
# matching what discovery records. For lowercase-native engines (PostgreSQL,
# MySQL) the filter normalizes schema/table names to lowercase so federated DB
# names line up with the discovered schemas.
#
# Snowflake must also be excluded, for a subtler reason than Redshift's: Glue
# ACCEPTS the property, the connection reaches READY, and the catalog is created
# — it just silently exposes NOTHING. Snowflake stores unquoted identifiers in
# UPPERCASE (TPCH_SF1, CUSTOMER), so LOWERCASE_ONLY filters out every object.
# Measured on one Snowflake account, two catalogs differing only in this
# property:
#   with LOWERCASE_ONLY  -> GetDatabases returns 0 databases
#   without it           -> tpch_sf1, tpch_sf10, ... and all 8 TPC-H tables
# The property is a SELECTOR on source-side casing, not a transform: it picks
# which objects to expose, then names are normalized to lowercase for Athena
# either way. Snowflake has no natively-lowercase objects, so LOWERCASE_ONLY
# selects none of them.
#
# Omitting it does not mean "no filter" — Glue substitutes an engine-appropriate
# default. Verified on the recreated connection: GetConnection reports
# CATALOG_CASING_FILTER=UPPERCASE_ONLY, which we never sent, and the catalog then
# exposes all 8 TPC-H tables (reported lowercase: tpch_sf1, customer). So the
# correct action is to stay out of Glue's way rather than to pick a value here;
# hardcoding UPPERCASE_ONLY would re-break the moment an engine disagrees.
#
# Oracle is excluded for exactly the same reason, confirmed the same way:
# onboarding a real Oracle Database Free instance produced a READY connection
# and a catalog with 0 databases, because Oracle also folds unquoted identifiers
# to UPPERCASE. So the rule is not "Snowflake is special" but "the filter is
# only safe on lowercase-native engines" — PostgreSQL and MySQL, verified
# unaffected (byte-identical table lists with and without it).
#
# This failure mode is expensive to diagnose because it surfaces far downstream:
# scan succeeds, source is marked queryable, and serve fails at query time with
# Athena TABLE_NOT_FOUND on a catalog that resolves but is empty. The property
# is also NON-UPDATABLE, so a connection created with it cannot be repaired —
# it has to be deleted and recreated.
_CASING_FILTER_UNSUPPORTED_ENGINES = frozenset({"REDSHIFT", "SNOWFLAKE", "ORACLE"})

_DNS_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
# Snowflake account identifiers occupy the first host label and may contain
# internal underscores; the service and PrivateLink suffix remains DNS-shaped.
_SNOWFLAKE_ACCOUNT_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"^(?=.{{1,253}}$)(?:{_DNS_HOST_LABEL}\.)*{_DNS_HOST_LABEL}$")
_SNOWFLAKE_HOST_RE = re.compile(rf"^(?=.{{1,253}}$){_SNOWFLAKE_ACCOUNT_HOST_LABEL}(?:\.{_DNS_HOST_LABEL})*$")
_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_DATABASE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
_IAM_ROLE_ARN_RE = re.compile(r"^arn:(aws|aws-us-gov|aws-cn):iam::\d+:role/.+$")


def _count_if_throttle(exc: ClientError, api: str) -> None:
    """Emit ``GlueApiThrottles`` when ``exc`` is a terminal throttle.

    ponytail: counts only throttles that survive botocore's standard retry mode
    (i.e. the ones that actually failed the call). Retried-then-succeeded
    throttles are invisible here — surfacing those needs a botocore event hook
    on ``needs-retry.*``, which is not worth the wiring until this metric shows
    real pressure.
    """
    if is_throttling_error(exc):
        emit_metric("GlueApiThrottles", 1, "Count", Api=api)


def _lf_grant(
    lf_client, principal: dict, resource: dict, permissions: list[str], *, database_name: str, resource_type: str
) -> bool:
    """Make a single LF grant_permissions call. AlreadyExistsException/InvalidInputException → idempotent success."""
    try:
        lf_client.grant_permissions(Principal=principal, Resource=resource, Permissions=permissions)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("AlreadyExistsException", "InvalidInputException"):
            return True
        _count_if_throttle(exc, "GrantPermissions")
        logger.warning(
            "lf_native_grant_failed",
            extra={"database": database_name, "resource_type": resource_type},
            exc_info=True,
        )
        return False


_glue_client = None
_lf_client = None
_sts_client = None
_account_id: str | None = None


def _get_glue():
    global _glue_client
    if _glue_client is None:
        _glue_client = boto3.client("glue", region_name=AWS_REGION, config=_RETRY_CONFIG)
    return _glue_client


def _get_lakeformation():
    global _lf_client
    if _lf_client is None:
        _lf_client = boto3.client("lakeformation", region_name=AWS_REGION, config=_RETRY_CONFIG)
    return _lf_client


def _get_account_id() -> str:
    global _account_id, _sts_client
    if _account_id is None:
        if _sts_client is None:
            _sts_client = boto3.client("sts", region_name=AWS_REGION)
        account = _sts_client.get_caller_identity().get("Account")
        if not account:
            raise RuntimeError("STS get_caller_identity returned no Account")
        _account_id = account
    return _account_id


def _partition(region: str) -> str:
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def build_catalog_name(resource_prefix: str, datasource_id: str) -> str:
    """Deterministic, lowercase, collision-resistant name (<=41 chars).

    Used as the Glue connection name, federated catalog name, and identifier for
    a federated JDBC source, and — via ``athena_catalog`` — as the Athena
    data-catalog name for a custom-connector source. Shared deliberately: the
    ``{sanitizedPrefix}ds_*`` shape is what lets one prefix-scoped IAM statement
    cover every catalog this deployment creates, so the two paths must not drift.
    """
    digest = hashlib.sha256(datasource_id.encode("utf-8")).hexdigest()[:16]
    safe_prefix = re.sub(r"[^a-z0-9]", "", resource_prefix.lower())
    name = f"{safe_prefix}ds_{digest}"
    if not name[0].isalpha():
        name = "ds_" + name
    return name[:_CATALOG_NAME_MAX]


def _validate_connection_inputs(*, engine: str, host: str, port: int, database: str) -> None:
    normalized_engine = engine.upper() if isinstance(engine, str) else engine
    if normalized_engine not in DISCOVERY_ENGINES:
        allowed = ", ".join(sorted(DISCOVERY_ENGINES))
        raise ValueError(f"engine must be one of {allowed}: {engine!r}")
    if not isinstance(host, str) or not host:
        raise ValueError("host must be a non-empty string")
    if _IPV4_RE.match(host):
        if not all(0 <= int(o) <= 255 for o in host.split(".")):
            raise ValueError(f"invalid IPv4 host: {host!r}")
    else:
        is_snowflake = normalized_engine == "SNOWFLAKE"
        host_pattern = _SNOWFLAKE_HOST_RE if is_snowflake else _HOST_RE
        if not host_pattern.match(host):
            if not is_snowflake and "_" in host:
                raise ValueError(
                    f"host must be a valid hostname or IPv4 address: {host!r} "
                    "(underscores are only permitted in Snowflake account hostnames)"
                )
            raise ValueError(f"host must be a valid hostname or IPv4 address: {host!r}")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise ValueError(f"port must be an integer in 1-65535: {port!r}")
    if not isinstance(database, str) or not _DATABASE_RE.match(database):
        raise ValueError(f"database must be alphanumeric, underscore, or hyphen (<=128 chars): {database!r}")


def provision_federated_catalog(
    *,
    datasource_id: str,
    engine: str,
    host: str,
    port: int,
    database_name: str,
    credential_secret_arn: str,
    public_schema_name: str = "public",
    subnet_id: str | None = None,
    security_group_id: str | None = None,
    availability_zone: str | None = None,
    warehouse: str | None = None,
) -> dict[str, str]:
    """Provision a managed Glue federated catalog for a JDBC source.

    Returns ``{'glueConnectionName', 'athenaDataCatalogName'}`` (both equal the
    catalog name). Raises RuntimeError on failure, rolling back partial state.
    """
    resource_prefix = os.environ.get("RESOURCE_PREFIX", "coa-dev-")
    spill_bucket = os.environ.get("ATHENA_SPILL_BUCKET", "")
    role_arn = os.environ.get("FEDERATED_CATALOG_ROLE_ARN", "")
    if not role_arn:
        raise RuntimeError("FEDERATED_CATALOG_ROLE_ARN is not configured")

    try:
        _validate_connection_inputs(engine=engine, host=host, port=port, database=database_name)
    except ValueError as exc:
        raise RuntimeError(f"Invalid JDBC connection parameters: {exc}") from exc

    if not _DATABASE_RE.match(public_schema_name):
        raise RuntimeError(f"Invalid public_schema_name: {public_schema_name!r}")

    name = build_catalog_name(resource_prefix, datasource_id.replace("DS#", ""))
    conn_arn = f"arn:{_partition(AWS_REGION)}:glue:{AWS_REGION}:{_get_account_id()}:connection/{name}"
    glue = _get_glue()

    # ── Step 1: Glue connection (managed connector — no Lambda) ─────────────
    connection_properties: dict = {
        "HOST": host,
        "PORT": str(port),
        "DATABASE": database_name,
        "ROLE_ARN": role_arn,
    }
    if engine.upper() not in _CASING_FILTER_UNSUPPORTED_ENGINES:
        connection_properties["CATALOG_CASING_FILTER"] = "LOWERCASE_ONLY"
    # Snowflake cannot execute anything without an active warehouse, and Glue
    # enforces this at CreateConnection time rather than at query time:
    # omitting it fails the whole call with
    #   InvalidInputException: [WAREHOUSE are missing in the request object]
    # Because federation failure is fatal for JDBC sources, that surfaced as a
    # SCAN_FAILED even though discovery had already found every table. Engine-
    # specific properties therefore have to be threaded through here — the rest
    # of this function is deliberately engine-agnostic, so keep additions keyed
    # on the engine rather than set unconditionally.
    if engine.upper() == "SNOWFLAKE":
        if not warehouse:
            raise RuntimeError(
                "Snowflake federation requires a warehouse: set jdbcConfiguration.warehouse on the source."
            )
        connection_properties["WAREHOUSE"] = warehouse
        # Deliberately NOT setting ROLE. Glue's managed Snowflake connector
        # rejects it outright:
        #   InvalidInputException: [ROLE are not allowed in the request object.]
        # The connector session therefore runs as the secret user's DEFAULT_ROLE,
        # which is why that user is created with
        # DEFAULT_ROLE = <least-privilege role> rather than relying on a
        # per-connection override. Discovery still honours jdbcConfiguration.role
        # because the Python driver accepts it — only federation cannot.
    connection_input: dict = {
        "Name": name,
        "ConnectionType": engine.upper(),
        "ConnectionProperties": connection_properties,
        "AthenaProperties": {
            "MANAGED_CONNECTION": "true",
            "spill_bucket": spill_bucket,
            "spill_prefix": "athena-spill",
        },
        "AuthenticationConfiguration": {
            "AuthenticationType": "BASIC",
            "SecretArn": credential_secret_arn,
        },
    }
    if subnet_id and security_group_id:
        physical_reqs: dict = {
            "SubnetId": subnet_id,
            "SecurityGroupIdList": [security_group_id],
        }
        # AvailabilityZone is required for Glue managed connections.
        # Resolve it from the subnet if not provided explicitly.
        if not availability_zone:
            try:
                ec2 = boto3.client("ec2", region_name=AWS_REGION)
                subnet_info = ec2.describe_subnets(SubnetIds=[subnet_id])
                availability_zone = subnet_info["Subnets"][0]["AvailabilityZone"]
            except Exception:
                pass
        if availability_zone:
            physical_reqs["AvailabilityZone"] = availability_zone
        connection_input["PhysicalConnectionRequirements"] = physical_reqs
    try:
        glue.create_connection(ConnectionInput=connection_input)
        logger.info("glue_connection_created", extra={"connection_name": name})
    except glue.exceptions.AlreadyExistsException:
        logger.info("glue_connection_already_exists", extra={"connection_name": name})
    except ClientError as exc:
        _count_if_throttle(exc, "CreateConnection")
        raise RuntimeError(f"Failed to create Glue connection {name}: {exc}") from exc

    # ── Step 1b: Poll the managed connection to READY and surface the real error ──
    # create_connection returns immediately; the managed connector then validates
    # asynchronously (it provisions an ENI in the connection's VPC using ROLE_ARN).
    # Poll here and log the concrete StatusReason so the failure is diagnosable in
    # this account's Lambda logs (the managed connector runs in AWS's service
    # account, so its own error is otherwise invisible to us).
    #
    # NON-FATAL, deliberately. Making this fatal was tried and reverted: a
    # not-READY connection is an ENVIRONMENT defect (VPC/SG/IAM), and failing the
    # scan on it takes down every JDBC source in the account — discovery has
    # already succeeded at this point and its results are worth keeping. The
    # catalog-emptiness integ assertion (`assert_federated_catalog_not_empty`) is
    # what fails loudly on a broken connection; this log line is what tells you
    # why. Search `glue_connection_not_ready` in the federation-provisioner logs.
    try:
        conn_status = ""
        status_reason = ""
        for _attempt in range(20):  # ~100s max (managed validation is usually <60s)
            conn_detail = glue.get_connection(Name=name).get("Connection", {})
            conn_status = conn_detail.get("Status", "")
            status_reason = conn_detail.get("StatusReason", "")
            if conn_status in ("READY", "FAILED"):
                break
            time.sleep(5)
        if conn_status == "READY":
            logger.info("glue_connection_ready", extra={"connection_name": name})
        else:
            logger.error(
                "glue_connection_not_ready",
                extra={
                    "connection_name": name,
                    "status": conn_status or "UNKNOWN",
                    "status_reason": status_reason,
                    "engine": engine.upper(),
                    "subnet_id": subnet_id,
                    "security_group_id": security_group_id,
                    "availability_zone": availability_zone,
                    "role_arn": role_arn,
                },
            )
    except ClientError as exc:
        logger.warning(
            "glue_connection_status_poll_failed",
            extra={"connection_name": name, "error": str(exc)},
        )

    # ── Step 2: Register the connection with Lake Formation federation ──────
    try:
        _get_lakeformation().register_resource(ResourceArn=conn_arn, RoleArn=role_arn, WithFederation=True)
        logger.info("lf_resource_registered", extra={"connection_name": name})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "AlreadyExistsException":
            logger.info("lf_resource_already_registered", extra={"connection_name": name})
        else:
            _count_if_throttle(exc, "RegisterResource")
            _rollback(name, conn_arn, drop_catalog=False, drop_lf=False)
            raise RuntimeError(f"Failed to register LF resource for {name}: {exc}") from exc

    # ── Step 3: Create the Glue federated catalog ───────────────────────────
    try:
        glue.create_catalog(
            Name=name,
            CatalogInput={
                "FederatedCatalog": {"ConnectionName": name, "Identifier": name},
                "CreateDatabaseDefaultPermissions": [],
                "CreateTableDefaultPermissions": [],
            },
        )
        logger.info("glue_federated_catalog_created", extra={"catalog_name": name})
    except glue.exceptions.AlreadyExistsException:
        logger.info("glue_federated_catalog_already_exists", extra={"catalog_name": name})
    except ClientError as exc:
        _count_if_throttle(exc, "CreateCatalog")
        _rollback(name, conn_arn, drop_catalog=False, drop_lf=True)
        raise RuntimeError(f"Failed to create federated catalog {name}: {exc}") from exc

    # ── Step 4: Grant IAM_ALLOWED_PRINCIPALS on the schema database ────────
    # Federated catalogs don't support CreateTableDefaultPermissions; tables
    # inherit access from a database-level IAM_ALLOWED_PRINCIPALS grant.
    try:
        _get_lakeformation().grant_permissions(
            Principal={"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"},
            Resource={"Database": {"CatalogId": name, "Name": public_schema_name}},
            Permissions=["ALL"],
        )
        logger.info("lf_database_grant_created", extra={"catalog_name": name, "database": public_schema_name})
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("AlreadyExistsException", "InvalidInputException"):
            logger.info("lf_database_grant_already_exists", extra={"catalog_name": name})
        else:
            _count_if_throttle(exc, "GrantPermissions")
            raise RuntimeError(f"Failed to grant LF permissions on {name}/{public_schema_name}: {exc}") from exc

    return {"glueConnectionName": name, "athenaDataCatalogName": name}


def grant_iam_allowed_principals(*, catalog_name: str, schemas: list[str]) -> bool:
    """Grant IAM_ALLOWED_PRINCIPALS on each DISCOVERED schema of a federated catalog.

    Federated catalogs don't support ``CreateTableDefaultPermissions``, so tables
    inherit access from a database-level IAM_ALLOWED_PRINCIPALS grant — the same
    intent as the ``public_schema_name`` grant in ``provision_federated_catalog``,
    but applied to the schemas that actually exist.

    That single hardcoded grant defaulted to ``"public"``, a database only
    PostgreSQL and Redshift have. Lake Formation accepts a grant naming a database
    that doesn't exist (no error, nothing granted), so on MySQL / SQL Server /
    Oracle no real database ended up governed. The failure is invisible to an LF
    data lake admin — admins bypass filtering and see every database — and shows up
    for every other principal as ``GetDatabases`` returning an EMPTY LIST on a
    catalog that resolves fine. Measured on one account, same engine and connection,
    differing only in this grant:

        granted on the real database -> GetDatabases -> ["integration_test"]
        granted on "public" only     -> GetDatabases -> []

    Idempotent. Best-effort: returns False on any error without raising, so a
    transient LF failure defers governance rather than failing the scan.
    """
    if not schemas:
        return False
    # Defensive by design: this runs as a scan-pipeline step whose failure is caught
    # by the state machine's error chain and marks the source SCAN_FAILED. Governance
    # is not worth failing a scan whose discovery already succeeded, so every error
    # (including a failed STS account lookup) degrades to "not granted".
    try:
        lf = _get_lakeformation()
        catalog_id = f"{_get_account_id()}:{catalog_name}"
    except Exception:
        logger.warning("lf_iam_allowed_principals_setup_failed", extra={"catalog": catalog_name}, exc_info=True)
        return False
    principal = {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"}
    all_granted = True
    for schema in schemas:
        if not _lf_grant(
            lf,
            principal,
            {"Database": {"CatalogId": catalog_id, "Name": schema}},
            ["ALL"],
            database_name=schema,
            resource_type="federated_database",
        ):
            all_granted = False
    logger.info(
        "lf_iam_allowed_principals_granted",
        extra={"catalog": catalog_name, "schemas": schemas, "all_granted": all_granted},
    )
    return all_granted


def grant_consumer_select(*, catalog_name: str, schemas: list[str], principal_arn: str) -> bool:
    """Grant the consumer query principal LF SELECT/DESCRIBE on a federated catalog.

    Federated-catalog databases mirror the source schemas; we grant per discovered
    schema (DESCRIBE on the database + SELECT on all its tables via TableWildcard).
    Idempotent. Returns ``True`` only if every schema was granted (so the caller can
    gate ``queryable`` on it); a missing principal/schemas or any LF error returns
    ``False`` without raising, so a transient failure defers queryability rather than
    failing the scan (a re-scan re-applies the grants).
    """
    if not principal_arn or not schemas:
        logger.info("lf_grant_skipped", extra={"catalog": catalog_name, "has_principal": bool(principal_arn)})
        return False
    lf = _get_lakeformation()
    # Nested (federated) catalog id is "<accountId>:<catalogName>".
    catalog_id = f"{_get_account_id()}:{catalog_name}"
    principal = {"DataLakePrincipalIdentifier": principal_arn}
    all_granted = True
    for schema in schemas:
        try:
            lf.grant_permissions(
                Principal=principal,
                Resource={"Database": {"CatalogId": catalog_id, "Name": schema}},
                Permissions=["DESCRIBE"],
            )
            lf.grant_permissions(
                Principal=principal,
                Resource={"Table": {"CatalogId": catalog_id, "DatabaseName": schema, "TableWildcard": {}}},
                Permissions=["SELECT", "DESCRIBE"],
            )
            logger.info("lf_grant_applied", extra={"catalog": catalog_name, "schema": schema})
        except ClientError as exc:
            _count_if_throttle(exc, "GrantPermissions")
            logger.warning("lf_grant_failed", extra={"catalog": catalog_name, "schema": schema}, exc_info=True)
            all_granted = False
    return all_granted


def grant_consumer_select_native(*, database_name: str, principal_arn: str) -> bool:
    """Grant the consumer query principal LF SELECT/DESCRIBE on a native Glue database.

    Used for GLUE_DATABASE (S3/Iceberg) sources in accounts with strict Lake
    Formation mode (IAM_ALLOWED_PRINCIPALS default removed). In those accounts
    IAM permissions alone are insufficient — the principal also needs an explicit
    LF data permission on the specific Glue database and its tables.

    Targets the account's own Data Catalog (catalog_id = account_id), which is
    how AwsDataCatalog tables are governed. Idempotent — safe to call on re-scan.
    Returns True on success, False on any error (caller gates queryable on it).
    """
    if not database_name or not _DATABASE_RE.match(database_name):
        logger.warning(
            "lf_native_grant_skipped",
            extra={"reason": "invalid_database_name", "has_principal": bool(principal_arn)},
        )
        return False
    if not principal_arn or not _IAM_ROLE_ARN_RE.match(principal_arn):
        logger.warning(
            "lf_native_grant_skipped",
            extra={"database": database_name, "reason": "invalid_principal_arn"},
        )
        return False
    lf = _get_lakeformation()
    account_id = _get_account_id()
    principal = {"DataLakePrincipalIdentifier": principal_arn}

    db_granted = _lf_grant(
        lf,
        principal,
        {"Database": {"CatalogId": account_id, "Name": database_name}},
        ["DESCRIBE"],
        database_name=database_name,
        resource_type="database",
    )
    table_granted = _lf_grant(
        lf,
        principal,
        {"Table": {"CatalogId": account_id, "DatabaseName": database_name, "TableWildcard": {}}},
        ["SELECT", "DESCRIBE"],
        database_name=database_name,
        resource_type="table",
    )
    all_granted = db_granted and table_granted
    if all_granted:
        log_key = "lf_native_grant_applied"
    elif db_granted or table_granted:
        log_key = "lf_native_grant_partial"
    else:
        log_key = "lf_native_grant_failed_both"
    logger.info(
        log_key,
        extra={"database": database_name, "db_granted": db_granted, "table_granted": table_granted},
    )
    return all_granted


def cleanup_federated_resources(
    *,
    glue_connection_name: str | None = None,
    athena_catalog_name: str | None = None,
    session=None,
) -> None:
    """Tear down federated resources: catalog, LF registration, Glue connection.

    Deletes the federated catalog, deregisters the Lake Formation resource, and
    deletes the Glue connection. Best-effort: individual failures are logged, not
    raised.

    Pass ``session`` (e.g. an assumed Lake Formation admin role) to run the
    teardown with credentials that hold DROP on the catalog; otherwise the
    module's default clients are used.
    """
    name = athena_catalog_name or glue_connection_name
    if not name:
        return
    glue = session.client("glue", region_name=AWS_REGION) if session else _get_glue()
    lf = session.client("lakeformation", region_name=AWS_REGION) if session else _get_lakeformation()
    conn_arn = f"arn:{_partition(AWS_REGION)}:glue:{AWS_REGION}:{_get_account_id()}:connection/{name}"
    _rollback(name, conn_arn, drop_catalog=True, drop_lf=True, glue=glue, lf=lf)


def _rollback(
    name: str,
    conn_arn: str,
    *,
    drop_catalog: bool,
    drop_lf: bool,
    glue=None,
    lf=None,
) -> None:
    """Best-effort teardown of catalog / LF registration / connection."""
    from coa_sources.database.metrics import emit_metric  # noqa: PLC0415

    glue = glue or _get_glue()
    lf = lf or _get_lakeformation()
    if drop_catalog:
        try:
            glue.delete_catalog(CatalogId=name)
            logger.info("glue_federated_catalog_deleted", extra={"catalog_name": name})
        except Exception:
            logger.warning("glue_federated_catalog_delete_failed", extra={"catalog_name": name}, exc_info=True)
            emit_metric("RollbackFailureCount", 1, "Count", Resource="catalog")
    if drop_lf:
        try:
            lf.deregister_resource(ResourceArn=conn_arn)
            logger.info("lf_resource_deregistered", extra={"connection_name": name})
        except Exception:
            logger.warning("lf_resource_deregister_failed", extra={"connection_name": name}, exc_info=True)
            emit_metric("RollbackFailureCount", 1, "Count", Resource="lf_registration")
    try:
        glue.delete_connection(ConnectionName=name)
        logger.info("glue_connection_deleted", extra={"connection_name": name})
    except Exception:
        logger.warning("glue_connection_delete_failed", extra={"connection_name": name}, exc_info=True)
        emit_metric("RollbackFailureCount", 1, "Count", Resource="connection")
