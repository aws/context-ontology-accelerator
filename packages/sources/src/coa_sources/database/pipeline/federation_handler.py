# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Federation provisioner Lambda — runs AFTER discovery in the pipeline.

Separated from the discovery handler so the Lake Formation data-lake-admin
privilege required to create federated catalogs and grant LF data permissions is
isolated to this single-purpose function's role (not the broad discovery connector).

Discovery runs first; this step then:
- **JDBC sources**: provisions the managed Glue federated catalog and grants the
  consumer query principal LF SELECT/DESCRIBE on it. ``queryable`` is set to the
  grant result.
- **GLUE_DATABASE sources**: skips catalog provisioning (they query natively via
  AwsDataCatalog) but still grants the consumer LF SELECT/DESCRIBE on the native
  Glue database. This is a no-op in IAM-mode accounts and idempotent on re-scans.
  Accounts with strict Lake Formation mode (IAM_ALLOWED_PRINCIPALS removed) require
  this grant for Athena to access LF-governed Glue tables.

Fails loudly: a provisioning or persistence error raises, so the Lambda fails and
the scan-pipeline Catch marks the scan FAILED and the source SCAN_FAILED. Sources
with incomplete config are skipped, not failed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from coa_common import async_boto_config
from coa_common.constants import namespace_tag_condition_patterns, namespace_tag_key
from coa_common.dao import DynamoDBDAO
from coa_control_plane_server.models.source_sub_type import SourceSubType

from coa_sources.database.connectors.glue_connection_provisioner import (
    cleanup_federated_resources,
    grant_consumer_select,
    grant_consumer_select_native,
    grant_iam_allowed_principals,
    provision_federated_catalog,
)
from coa_sources.database.glue_ownership import (
    GlueOwnershipError,
    assert_namespace_may_catalog,
)
from coa_sources.database.secret_binding import require_secret_namespace_binding
from coa_sources.database.sub_types import CONNECTOR_BACKED_SUB_TYPES

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

SOURCES_TABLE = os.environ["SOURCES_TABLE"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# SSM parameter holding the consumer query principal (AgentCore runtime role) ARN.
# Optional: when unset/absent the consumer grant is skipped (e.g. envs without serve).
CONSUMER_QUERY_ROLE_SSM_PARAM = os.environ.get("CONSUMER_QUERY_ROLE_SSM_PARAM", "")
# Role the AWS-managed federated connector uses to read the credential secret.
# We assume it to pre-check secret readability before provisioning.
FEDERATED_CATALOG_ROLE_ARN = os.environ.get("FEDERATED_CATALOG_ROLE_ARN", "")
# Tag key binding a credential secret to the namespaces entitled to it. Derived
# from the deployment prefix, so it must match the key the registration check
# (database_routes) and the sources-stack IAM conditions use.
_NAMESPACE_TAG_KEY = namespace_tag_key()

_dao: DynamoDBDAO | None = None
_ssm = None

# DATABASE sub-types this handler has an explicit branch for.
#
# Built from CONNECTOR_BACKED_SUB_TYPES because this set and the `queryable` write below
# are TWO branches that must move together: adding a sub-type here alone only silences
# the tripwire at the bottom of `handler`, and a sub-type in this set but not in that
# comparison scans cleanly and then silently answers nothing.
_HANDLED_SUB_TYPES = (
    frozenset(
        {
            SourceSubType.GLUE_DATABASE.value,
            SourceSubType.JDBC_DATABASE.value,
        }
    )
    | CONNECTOR_BACKED_SUB_TYPES
)
# DOCUMENTS sub-types never reach this pipeline, so a row carrying one is a
# mis-stored record. The right treatment there is the long-standing no-op, not a
# scan failure — this handler is not the place to police that.
_DOCUMENT_SUB_TYPES = frozenset({SourceSubType.S3.value, SourceSubType.LOCAL_UPLOAD.value})
# What is left is a DATABASE sub-type the enum recognises and this handler has no
# branch for. Empty today, and that is the point: it becomes non-empty only when a
# new DATABASE sub-type ships without its branch here, which the catch-all in
# :func:`handler` then reports instead of silently leaving every source of that
# type not-queryable. An absent or unrecognised value is deliberately NOT in this
# set, so a concurrently-deleted source (which reads back as an empty dict) and a
# legacy row both keep the no-op.
_UNHANDLED_DATABASE_SUB_TYPES = frozenset(m.value for m in SourceSubType) - _HANDLED_SUB_TYPES - _DOCUMENT_SUB_TYPES


def _get_dao() -> DynamoDBDAO:
    global _dao
    if _dao is None:
        _dao = DynamoDBDAO(SOURCES_TABLE, region=AWS_REGION)
    return _dao


def _secret_readable_by_connector(secret_arn: str) -> bool:
    """Verify the managed connector can read the credential secret.

    The federated connector reads the secret AS ``FEDERATED_CATALOG_ROLE_ARN``,
    so we assume that role and attempt ``GetSecretValue``. Returns ``False`` when
    it can't be read — e.g. a cross-account secret whose resource/KMS policy
    doesn't yet grant the connector role — so the caller skips provisioning a
    connection that would otherwise fail silently at query time. Returns ``True``
    when no dedicated role is configured (nothing to validate against).
    """
    if not FEDERATED_CATALOG_ROLE_ARN or not secret_arn:
        return True
    # SecretId must be queried in the secret's own region (it may be cross-region).
    # ARN format: arn:partition:service:region:account:resource (>= 6 parts).
    parts = secret_arn.split(":")
    if len(parts) < 6:
        logger.warning("malformed_secret_arn", extra={"secret_arn": secret_arn})
        return False
    secret_region = parts[3] or AWS_REGION
    try:
        creds = boto3.client("sts", region_name=AWS_REGION).assume_role(
            RoleArn=FEDERATED_CATALOG_ROLE_ARN, RoleSessionName="coa-secret-precheck"
        )["Credentials"]
        boto3.client(
            "secretsmanager",
            region_name=secret_region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        ).get_secret_value(SecretId=secret_arn)
        return True
    except (ClientError, BotoCoreError):
        # Only AWS-side failures mean "not readable"; programming errors surface.
        logger.warning("federation_secret_precheck_failed", extra={"secret_arn": secret_arn}, exc_info=True)
        return False


def _consumer_role_arn() -> str:
    """Resolve the consumer query role ARN from SSM at runtime (empty if unavailable)."""
    if not CONSUMER_QUERY_ROLE_SSM_PARAM:
        return ""
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", region_name=AWS_REGION)
    try:
        return _ssm.get_parameter(Name=CONSUMER_QUERY_ROLE_SSM_PARAM)["Parameter"]["Value"]
    except Exception:
        logger.warning("consumer_role_ssm_lookup_failed", extra={"param": CONSUMER_QUERY_ROLE_SSM_PARAM})
        return ""


def _grant_secret_read_to_consumer(secret_arn: str, namespace_id: str) -> None:
    """Attach a resource policy on the credential secret granting the consumer role GetSecretValue.

    Idempotent: merges the consumer principal into the existing policy if one exists.
    Best-effort: failures are logged but don't block provisioning (the source remains
    queryable via Athena federation; only the direct JDBC fast-path is affected).

    Namespace binding: the grant is conditioned on the secret's
    ``<prefix>:namespace`` tag LISTING ``namespace_id``, so the serve runtime can
    read the secret only while it is tagged for this namespace. This is the
    serve-side half of the namespace binding — it keeps a stale or rebound grant
    from being used to read a secret this namespace has since been removed from,
    without giving the serve role any DescribeSecret permission of its own. The
    tag value may bind several namespaces, so this is an entry match rather than
    an equality one (see ``namespace_tag_condition_patterns``).
    """
    consumer_arn = _consumer_role_arn()
    if not consumer_arn or not secret_arn:
        return

    parts = secret_arn.split(":")
    if len(parts) < 6 or not parts[3]:
        logger.warning("malformed_secret_arn_skipping_grant", extra={"secret_arn": secret_arn})
        return
    secret_region = parts[3]
    sm = boto3.client("secretsmanager", region_name=secret_region, config=async_boto_config())

    try:
        # Fetch existing policy (if any)
        try:
            existing = sm.get_resource_policy(SecretId=secret_arn)
            policy = json.loads(existing.get("ResourcePolicy") or "{}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                policy = {}
            else:
                logger.debug(
                    "get_resource_policy_error", extra={"secret_arn": secret_arn, "code": e.response["Error"]["Code"]}
                )
                policy = {}
        except (json.JSONDecodeError, TypeError):
            policy = {}

        # Build/merge the statement
        sid = "SCLRuntimeSecretRead"
        statements = policy.get("Statement", [])
        if not isinstance(statements, list):
            logger.warning("invalid_policy_statement_type", extra={"secret_arn": secret_arn})
            statements = []
        # Remove any existing SCL statement to avoid duplicates
        statements = [s for s in statements if s.get("Sid") != sid]
        statements.append(
            {
                "Sid": sid,
                "Effect": "Allow",
                "Principal": {"AWS": consumer_arn},
                "Action": "secretsmanager:GetSecretValue",
                "Resource": "*",
                # Only readable while the secret's namespace tag lists this
                # namespace. StringLike (not StringEquals) because the tag value
                # may bind several namespaces; the patterns are space-anchored so
                # this is an entry match, not a substring one.
                "Condition": {
                    "StringLike": {
                        f"secretsmanager:ResourceTag/{_NAMESPACE_TAG_KEY}": namespace_tag_condition_patterns(
                            namespace_id
                        )
                    }
                },
            }
        )
        policy = {
            "Version": "2012-10-17",
            "Statement": statements,
        }

        sm.put_resource_policy(SecretId=secret_arn, ResourcePolicy=json.dumps(policy))
        logger.info("secret_resource_policy_granted", extra={"secret_arn": secret_arn, "principal": consumer_arn})
    except (ClientError, BotoCoreError):
        logger.warning("secret_resource_policy_grant_failed", extra={"secret_arn": secret_arn}, exc_info=True)


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Provision a managed Glue federated catalog for a JDBC source, then grant LF.

    Input (from Step Functions): {datasourceId, sourceId, namespaceId, ...}.
    Glue (S3/Iceberg) sources are skipped — they query natively via
    AwsDataCatalog and need no provisioning.
    """
    datasource_id = event["datasourceId"]
    source_id = event.get("sourceId") or datasource_id.removeprefix("DS#")
    namespace_id = event["namespaceId"]
    source_key = {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"}

    item = _get_dao().get(source_key) or {}
    sub_type = item.get("sourceSubType")

    # GLUE_DATABASE sources need no catalog provisioning but still need an LF
    # SELECT grant so the serve runtime role can query LF-governed Glue tables in
    # accounts with strict Lake Formation mode (IAM_ALLOWED_PRINCIPALS removed).
    if sub_type == SourceSubType.GLUE_DATABASE:
        database_name = item.get("athenaDatabase") or ""
        if not database_name:
            logger.warning(
                "glue_native_lf_grant_skipped_no_database",
                extra={"datasource_id": datasource_id},
            )
            return {"provisioned": False, "reason": "no-athena-database"}

        # This function runs as a Lake Formation admin and the principal it grants
        # is the SHARED serve runtime role, so a grant here makes the database
        # readable by every namespace's queries — the widest single effect in the
        # pipeline. Re-verify the namespace owns the target rather than trusting
        # that source-create did: this step is reached from the stored row, and the
        # cost of the check is one GetTags against a decision that cannot be undone
        # by deleting the source. See ``coa_sources.database.glue_ownership``.
        raw_config = item.get("configuration", "{}")
        glue_config = json.loads(raw_config) if isinstance(raw_config, str) else (raw_config or {})
        try:
            assert_namespace_may_catalog(
                _get_dao(),
                namespace_id=namespace_id,
                catalog_id=glue_config.get("catalogId", ""),
                database_name=database_name,
                region=glue_config.get("region") or AWS_REGION,
                cross_account_role_arn=glue_config.get("crossAccountRoleArn"),
            )
        except GlueOwnershipError as exc:
            # Not a scan failure: discovery already refused the same target, so a
            # source reaching here unverified is a row that predates the check or
            # whose tag was removed after onboarding. Leave it not-queryable and
            # say why, rather than failing a step whose only job is a grant.
            #
            # The message travels in `reason`, not just the log: this return value
            # is the Step Functions execution output an operator reads first, and
            # a bare "namespace-not-owner" tells them what happened without
            # telling them the tag command that fixes it.
            logger.warning(
                "glue_native_lf_grant_refused_unowned",
                extra={"datasource_id": datasource_id, "database": database_name, "reason": str(exc)},
            )
            return {
                "provisioned": False,
                "reason": f"namespace-not-owner: {exc}",
                "queryable": False,
            }

        granted = grant_consumer_select_native(
            database_name=database_name,
            principal_arn=_consumer_role_arn(),
        )
        try:
            _get_dao().update(
                key=source_key,
                update_fields={"queryable": granted},
                condition="attribute_exists(PK)",
            )
        except Exception:
            # The LF grant is idempotent — even if the DDB write races with a
            # concurrent delete, the permission persists and a re-scan will retry.
            logger.warning(
                "glue_native_queryable_update_failed",
                extra={"datasource_id": datasource_id},
                exc_info=True,
            )
        logger.info(
            "glue_native_lf_grant",
            extra={"datasource_id": datasource_id, "granted": granted},
        )
        return {"provisioned": False, "reason": "glue-native", "queryable": granted}

    # Connector-backed sources need no provisioning here: the Lambda-backed Athena data
    # catalog was registered at source-create, since this sub-type's discovery queries it
    # and discovery runs BEFORE this step. All that remains is to mark the source
    # queryable, and discovery having succeeded is the evidence for it.
    #
    # Keyed on the SET, not on an exact CUSTOM_CONNECTOR comparison, because this is the
    # branch that fails open when a sub-type is missed: it would fall through to the
    # `not JDBC_DATABASE` no-op below, which writes nothing, and create persists
    # `queryable: False` — so the source would scan cleanly and then answer nothing.
    #
    # DATABRICKS_SQL_WAREHOUSE needs no grant step either, for a different reason from
    # CUSTOM_CONNECTOR's: the connector assumes the CUSTOMER's role at request time, so
    # COA holds no grant on the credential to make or revoke.
    if sub_type in CONNECTOR_BACKED_SUB_TYPES:
        # Raises on failure, matching the JDBC path: leaving queryable False after
        # a successful discovery would present as a source that scanned fine and
        # silently answers nothing. Nothing needs rolling back — the catalog
        # belongs to the create path — and a re-scan retries.
        _get_dao().update(
            key=source_key,
            update_fields={"queryable": True},
            condition="attribute_exists(PK)",
        )
        logger.info(
            "connector_backed_source_marked_queryable",
            extra={"datasource_id": datasource_id, "sub_type": sub_type},
        )
        return {"provisioned": False, "reason": "connector-backed", "queryable": True}

    if sub_type != SourceSubType.JDBC_DATABASE:
        # An ABSENT sub-type is the benign case and must stay a no-op: a source
        # deleted concurrently with its scan reads back as an empty dict, and a
        # legacy row may predate the attribute. Turning either into a pipeline
        # failure would convert a race into an alarm.
        #
        # A sub-type the enum RECOGNISES but this handler does not is different —
        # it means a new DATABASE sub-type shipped without its branch here, and
        # every source of that type would silently stay queryable=False, scanning
        # cleanly and then answering nothing. That has to be loud.
        if sub_type in _UNHANDLED_DATABASE_SUB_TYPES:
            raise RuntimeError(
                f"No federation branch for sourceSubType {sub_type!r} ({datasource_id}); "
                f"the source would stay not-queryable with no other signal"
            )
        logger.info("Skipping federation for non-JDBC source: %s (type=%s)", datasource_id, sub_type)
        return {"provisioned": False, "reason": "not-jdbc"}

    raw_config = item.get("configuration", "{}")
    config = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
    host = config.get("host")
    port = config.get("port")
    engine = config.get("engine")
    credential_secret_arn = config.get("credentialSecretArn")
    database_name = config.get("databaseName")
    if not all([host, port, engine, credential_secret_arn, database_name]):
        logger.info("Skipping federation — incomplete JDBC config for %s", datasource_id)
        return {"provisioned": False, "reason": "incomplete-config"}

    # Re-verify the namespace binding on the STORED ARN before anything reads the
    # secret or writes a policy onto it. Discovery already did this earlier in the
    # pipeline, so reaching here with an unbound secret means the row or the
    # secret's tag list changed mid-scan — which is exactly the window this closes,
    # because the two steps that follow both act on the named secret: the
    # readability precheck reads it as FEDERATED_CATALOG_ROLE_ARN, and
    # `_grant_secret_read_to_consumer` rewrites its resource policy.
    #
    # Raises rather than skipping, matching this handler's fail-loudly contract:
    # a secret whose tag does not list this namespace is a refusal, not an
    # incomplete-config no-op to be silently reported as not-queryable.
    require_secret_namespace_binding(credential_secret_arn, namespace_id, datasource_id)

    # Fail fast: don't provision a connection the managed connector can't
    # authenticate (e.g. a cross-account secret not yet shared with the catalog
    # role). The source stays not-queryable; a re-scan retries once fixed.
    if not _secret_readable_by_connector(credential_secret_arn):
        logger.warning("Skipping federation — connector role cannot read secret for %s", datasource_id)
        return {"provisioned": False, "reason": "secret-unreadable"}

    result = provision_federated_catalog(
        datasource_id=datasource_id,
        engine=engine,
        host=host,
        port=int(port),
        database_name=database_name,
        credential_secret_arn=credential_secret_arn,
        public_schema_name=config.get("schemaName", "public"),
        subnet_id=os.environ.get("CONNECTOR_SUBNET_ID"),
        security_group_id=os.environ.get("CONNECTOR_SECURITY_GROUP_ID"),
        # Snowflake-only; ignored by every other engine. Sourced from the same
        # jdbcConfiguration field discovery already uses, so there is one place
        # to configure a warehouse rather than two. `role` is intentionally NOT
        # forwarded: Glue rejects a ROLE connection property, so the connector
        # runs as the secret user's DEFAULT_ROLE instead.
        warehouse=config.get("warehouse"),
    )
    catalog_name = result["athenaDataCatalogName"]

    # Schemas come from discovery (which ran first). Federated DB names are
    # lowercase — via CATALOG_CASING_FILTER=LOWERCASE_ONLY for most connectors,
    # or natively for Redshift (which folds identifiers to lowercase and rejects
    # that property) — so we lowercase the discovered schemas to match. We grant
    # by name — no need to list the catalog's databases (which the connector
    # materializes lazily, so GetDatabases can return empty right after create).
    schemas = sorted({s.lower() for s in (item.get("discoveredSchemas") or []) if s})

    # Governs the databases that actually exist. provision_federated_catalog grants
    # IAM_ALLOWED_PRINCIPALS on a single `public_schema_name` (default "public"),
    # which only PostgreSQL and Redshift have — on every other engine that grant
    # names a non-existent database, LF accepts it silently, and nothing is
    # governed. The symptom is invisible to LF data lake admins (they bypass
    # filtering) and surfaces for everyone else as GetDatabases returning an empty
    # list on a catalog that resolves. Grant per discovered schema here, where the
    # discovery results are available.
    grant_iam_allowed_principals(catalog_name=catalog_name, schemas=schemas)

    # Grant LF SELECT/DESCRIBE on the federated catalog to the consumer query
    # principal, scoped to the catalog's actual (casing-correct) database names.
    # The consumer grant gates queryable. Best-effort/idempotent: a failure (or no
    # principal) leaves queryable False without failing the scan — a re-scan retries.
    granted = grant_consumer_select(catalog_name=catalog_name, schemas=schemas, principal_arn=_consumer_role_arn())

    # Grant the consumer query principal (AgentCore runtime role) read access to
    # the credential secret so the direct JDBC executor can authenticate at query time.
    # Uses a resource-based policy on the secret (least-privilege, no broad IAM grant).
    _grant_secret_read_to_consumer(credential_secret_arn, namespace_id)

    # Persist the references; roll back the cloud resources if the write fails
    # so we never leave orphaned Glue/LF resources unrecorded, then re-raise to
    # fail the scan.
    try:
        _get_dao().update(
            key=source_key,
            update_fields={
                "glueConnectionName": result["glueConnectionName"],
                "athenaDataCatalogName": catalog_name,
                # Queryable only once the consumer holds the LF grant.
                "queryable": granted,
            },
            condition="attribute_exists(PK)",
        )
    except Exception:
        logger.exception("Failed to persist federation refs for %s; rolling back", datasource_id)
        cleanup_federated_resources(
            glue_connection_name=result.get("glueConnectionName"),
            athena_catalog_name=catalog_name,
        )
        raise

    return {"provisioned": True, "queryable": granted, **result}
