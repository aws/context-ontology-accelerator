# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace creation service — orchestrates DynamoDB + DataZone."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime

import boto3
import structlog
from botocore.exceptions import ClientError
from coa_common import sanitize_principal_key
from coa_common.authnz_types import PrincipalType, ResourceType
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.language_tags import DEFAULT_LANGUAGE, canonicalize_language_tag
from coa_common.metadata_store import MetadataStoreClient, MetadataStoreError
from coa_control_plane_server.models.create_namespace_request_content import CreateNamespaceRequestContent
from coa_control_plane_server.models.namespace_detail import NamespaceDetail
from coa_control_plane_server.models.namespace_status import NamespaceStatus

logger = structlog.get_logger(__name__)

# Pending reservations expire after 5 minutes (safety net for crashes).
_RESERVATION_TTL_SECONDS = 300


class ConflictError(Exception):
    """Namespace name already exists."""


class NamespaceService:
    """Creates namespaces with atomic DynamoDB writes and DataZone project provisioning."""

    def __init__(
        self,
        *,
        namespaces_dao: DynamoDBDAO,
        roles_dao: DynamoDBDAO,
        mappings_dao: DynamoDBDAO,
        metadata_store: MetadataStoreClient,
        project_profile_id: str = "",
    ) -> None:
        """Initialize the service with its persistence and provisioning collaborators.

        Args:
            namespaces_dao: DAO for the namespaces table.
            roles_dao: DAO for the roles table.
            mappings_dao: DAO for the resource-role-mappings table.
            metadata_store: Client used to provision the DataZone project.
            project_profile_id: DataZone project profile id applied to new
                projects.
        """
        self._ns_dao = namespaces_dao
        self._roles_dao = roles_dao
        self._mappings_dao = mappings_dao
        self._metadata_store = metadata_store
        self._project_profile_id = project_profile_id

    def create(self, request: CreateNamespaceRequestContent) -> NamespaceDetail:
        """Create a namespace and its DataZone project atomically.

        Canonicalizes the requested language (``en`` when omitted), reserves
        the name, provisions the DataZone project and Athena workgroup, then
        writes the namespace record, roles, and mappings, rolling back the
        reservation if project creation fails.

        Args:
            request: The validated create-namespace request.

        Returns:
            The detail of the newly created namespace.

        Raises:
            LanguageTagError: If the requested language is not a supported
                tag. Raised before any reservation or provisioning.
            ConflictError: If a namespace with the same name already exists.
            MetadataStoreError: If DataZone project provisioning fails.
        """
        # Validate the language before anything is reserved or provisioned so
        # an invalid tag has no side effect. The language is immutable, so this
        # canonical value is the only one the namespace will ever carry.
        language = DEFAULT_LANGUAGE if request.language is None else canonicalize_language_tag(request.language)

        namespace_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        # Pre-flight: fast-fail if name already taken (GSI query).
        self._check_name_available(request.name)

        # Step 1: Reserve the name with a conditional put.
        self._reserve_name(request.name, namespace_id, now)

        # Step 2: Create DataZone project. Clean up reservation on failure.
        try:
            project = self._metadata_store.create_project(
                name=namespace_id,
                description=request.description or f"Context Ontology Accelerator namespace: {request.name}",
                project_profile_id=self._project_profile_id,
            )
        except MetadataStoreError:
            self._delete_reservation(request.name)
            raise
        logger.info("datazone_project_created", project_id=project.project_id, namespace_id=namespace_id)

        # Step 2b: Ensure connector/enrichment roles are project members.
        # A namespace whose project-access role is not a project member can
        # never scan a source — DataZone Search requires project membership,
        # not just IAM permission. A membership failure must therefore
        # fail namespace creation loudly and roll back the half-created project
        # rather than leave a permanently scan-broken namespace behind.
        try:
            self._add_service_role_memberships(project.project_id)
        except MetadataStoreError:
            logger.error(
                "service_role_membership_failed",
                project_id=project.project_id,
                namespace_id=namespace_id,
            )
            self._rollback_project(project.project_id, request.name)
            raise

        # Step 2c: Create Athena workgroup for query isolation.
        # `_create_athena_workgroup` returns None if the workgroup could not
        # be confirmed to exist in Athena (e.g., SSM param missing); in that
        # case we do not persist a stale workgroup name in DDB.
        workgroup_name = self._create_athena_workgroup(namespace_id)

        # Step 3: Write namespace record, roles, mappings, and confirm reservation.
        self._finalize(namespace_id, request, language, project.project_id, workgroup_name, now)

        return NamespaceDetail.model_construct(
            namespace_id=namespace_id,
            name=request.name,
            display_name=request.display_name,
            description=request.description,
            owner=request.owner,
            status=NamespaceStatus.ACTIVE,
            language=language,
            data_zone_project_id=project.project_id,
            source_count=0,
            created_at=now,
            updated_at=now,
            athena_workgroup_name=workgroup_name,
        )

    # ── Helpers ───────────────────────────────────────────────────

    def _create_athena_workgroup(self, namespace_id: str) -> str | None:
        """Create a per-namespace Athena workgroup for query isolation.

        Returns the workgroup name on confirmed success (newly created OR
        pre-existing). Returns None if the workgroup could not be confirmed
        to exist in Athena (e.g., SSM parameter missing, AWS error). The
        caller MUST NOT persist a name returned as None — doing so creates
        a namespace record that references a non-existent workgroup.
        """
        from botocore.exceptions import ClientError as BotoClientError

        resource_prefix = os.environ.get("RESOURCE_PREFIX", "coa-dev-")
        workgroup_name = f"{resource_prefix}{namespace_id}"
        results_bucket_ssm = os.environ.get("ATHENA_RESULTS_BUCKET_SSM", "")
        tag_prefix = os.environ.get("TAG_PREFIX", "coa")

        if not results_bucket_ssm:
            logger.warning("athena_results_bucket_ssm_not_configured", namespace_id=namespace_id)
            return None

        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        try:
            resp = ssm.get_parameter(Name=results_bucket_ssm)
            results_bucket = resp["Parameter"]["Value"]
        except BotoClientError:
            logger.warning("athena_results_bucket_ssm_not_found", param=results_bucket_ssm)
            return None

        athena = boto3.client("athena", region_name=os.environ.get("AWS_REGION", "us-east-1"))

        try:
            athena.create_work_group(
                Name=workgroup_name,
                Configuration={
                    "ResultConfiguration": {
                        "OutputLocation": f"s3://{results_bucket}/{namespace_id}/",
                        "EncryptionConfiguration": {"EncryptionOption": "SSE_S3"},
                    },
                    "EnforceWorkGroupConfiguration": True,
                    "PublishCloudWatchMetricsEnabled": True,
                    "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
                },
                Tags=[
                    {"Key": f"{tag_prefix}:namespace", "Value": namespace_id},
                    {"Key": f"{tag_prefix}:managed", "Value": "true"},
                ],
            )
            logger.info("athena_workgroup_created", workgroup_name=workgroup_name)
            return workgroup_name
        except athena.exceptions.InvalidRequestException as exc:
            if "already exists" in str(exc):
                logger.info("athena_workgroup_already_exists", workgroup_name=workgroup_name)
                return workgroup_name
            # Any other InvalidRequestException is a real failure; don't
            # claim the workgroup exists.
            logger.exception("athena_workgroup_create_failed", workgroup_name=workgroup_name)
            return None
        except BotoClientError:
            logger.exception("athena_workgroup_create_failed", workgroup_name=workgroup_name)
            return None

    def _check_name_available(self, name: str) -> None:
        result = self._ns_dao.query(
            QueryParams(
                key_condition="#n = :name",
                expression_names={"#n": "name"},
                expression_values={":name": name},
                index_name="NameByIndex",
                limit=1,
            )
        )
        if result.items:
            raise ConflictError(f"Namespace '{name}' already exists")

    def _reserve_name(self, name: str, namespace_id: str, now: str) -> None:
        """Atomically reserve a namespace name. Raises ConflictError on duplicate."""
        ttl = int(time.time()) + _RESERVATION_TTL_SECONDS
        try:
            self._ns_dao.put(
                {
                    "PK": f"NS_NAME#{name}",
                    "SK": "RESERVATION",
                    "namespaceId": namespace_id,
                    "status": "PENDING",
                    "createdAt": now,
                    "ttl": ttl,
                },
                condition="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConflictError(f"Namespace '{name}' already exists") from exc
            raise

    def _delete_reservation(self, name: str) -> None:
        """Best-effort cleanup of a pending reservation."""
        try:
            self._ns_dao.delete({"PK": f"NS_NAME#{name}", "SK": "RESERVATION"})
        except Exception:
            logger.warning("reservation_cleanup_failed", name=name)

    def _rollback_project(self, project_id: str, name: str) -> None:
        """Best-effort teardown of a half-created namespace.

        Called when a step after ``create_project`` fails (e.g. service-role
        membership registration). Deletes the DataZone project and releases the
        name reservation so the failed create neither orphans a DataZone
        project nor blocks the name from being reused. Cleanup errors are
        swallowed — the caller re-raises the original, more actionable failure.
        """
        try:
            self._metadata_store.delete_project(project_id=project_id)
        except Exception:
            logger.warning("rollback_delete_project_failed", project_id=project_id)
        self._delete_reservation(name)

    def _finalize(
        self,
        namespace_id: str,
        request: CreateNamespaceRequestContent,
        language: str,
        datazone_project_id: str,
        athena_workgroup_name: str | None,
        now: str,
    ) -> None:
        """Write namespace record, roles, mappings, and confirm the reservation.

        ``athena_workgroup_name`` is None when the workgroup could not be
        provisioned (e.g., SSM parameter missing). In that case, we omit the
        attribute from the namespace record entirely rather than persist a
        stale name that doesn't correspond to a real Athena resource.
        """
        # Namespace record
        ns_item = {
            "PK": f"NS#{namespace_id}",
            "SK": "METADATA",
            "namespaceId": namespace_id,
            "name": request.name,
            "displayName": request.display_name,
            "description": request.description,
            "owner": request.owner,
            "status": NamespaceStatus.ACTIVE,
            "language": language,
            "dataZoneProjectId": datazone_project_id,
            "sourceCount": 0,
        }
        if athena_workgroup_name:
            ns_item["athenaWorkgroupName"] = athena_workgroup_name
        self._ns_dao.put(ns_item)

        # Confirm reservation (remove TTL so it persists).
        self._ns_dao.update(
            {"PK": f"NS_NAME#{request.name}", "SK": "RESERVATION"},
            {"status": "CONFIRMED"},
            remove_fields=["ttl"],
            auto_timestamp=False,
        )

        # Copy namespace-scoped role templates into the new namespace.
        templates = self._roles_dao.query(
            QueryParams(
                key_condition="PK = :pk AND begins_with(SK, :prefix)",
                expression_values={":pk": "NAMESPACE_TEMPLATE", ":prefix": "ROLE#"},
            )
        )
        if not templates.items:
            raise RuntimeError("No role templates found in NAMESPACE_TEMPLATE partition")

        for tmpl in templates.items:
            role_item = {
                "PK": f"NS#{namespace_id}",
                "SK": tmpl["SK"],
                "name": tmpl.get("name", ""),
                "description": tmpl.get("description", ""),
                "isBuiltIn": True,
                "scope": tmpl.get("scope", "NAMESPACE"),
                "cedarPolicy": tmpl.get("cedarPolicy"),
            }
            role_item = {k: v for k, v in role_item.items() if v is not None}
            self._roles_dao.put(role_item)

        # Owner role mapping — uses "namespace-owner" role name to match the
        # built-in role template (NAMESPACE_TEMPLATE partition in roles table).
        # The owner is URL-encoded in the key fields to match how the authorizer
        # and serve-layer role resolver query principal keys; the raw
        # value is kept in principalId/grantedBy for human readability.
        sanitized_owner = sanitize_principal_key(request.owner)
        self._mappings_dao.put(
            {
                "PK": f"{ResourceType.NAMESPACE}::{namespace_id}#{PrincipalType.USER}::{sanitized_owner}",
                "SK": "ROLE#namespace-owner",
                "resourceType": ResourceType.NAMESPACE,
                "resourceId": namespace_id,
                "principalType": PrincipalType.USER,
                "principalId": request.owner,
                "role": "namespace-owner",
                "principalKey": f"{PrincipalType.USER}::{sanitized_owner}",
                "resourceRoleKey": f"{ResourceType.NAMESPACE}::{namespace_id}#ROLE#namespace-owner",
                "namespaceKey": f"NS#{namespace_id}",
                "principalRoleKey": f"{PrincipalType.USER}::{sanitized_owner}#ROLE#namespace-owner",
                "grantedBy": request.owner,
                "grantedAt": now,
            },
            auto_timestamp=False,
        )

    def _add_service_role_memberships(self, project_id: str) -> None:
        """Add the shared project-access role as a project member.

        The project-access role has a CfnUserProfile created at deploy time.
        Service lambdas (connector, enrichment, datasource-api) assume into
        this role for DataZone project operations (Search, GetAsset,
        CreateAssetRevision) — the default PROJECT_CONTRIBUTOR designation
        is sufficient for those. Namespace deletion's DeleteProject call
        instead assumes namespaceApiFn's own execution role (the actual
        DataZone-recorded project owner, since it is the role that called
        CreateProject) rather than this role — see
        namespace-deletion-pipeline.ts / cleanup.delete_smu_project.

        Raises MetadataStoreError if the SSM parameter is missing or the
        membership cannot be created — creating a namespace without this
        membership leaves scans permanently broken (Search requires project
        membership).
        """
        from botocore.exceptions import ClientError as BotoClientError

        ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))

        param_name = os.environ.get("PROJECT_ACCESS_ROLE_ARN_SSM")
        if not param_name:
            raise MetadataStoreError("PROJECT_ACCESS_ROLE_ARN_SSM environment variable is not set")

        try:
            resp = ssm.get_parameter(Name=param_name)
            role_arn = resp["Parameter"]["Value"]
        except BotoClientError as exc:
            if exc.response["Error"]["Code"] == "ParameterNotFound":
                raise MetadataStoreError(
                    f"SSM parameter {param_name} not found — cannot register project-access role"
                ) from exc
            raise MetadataStoreError(f"Failed to retrieve SSM parameter {param_name}") from exc

        self._metadata_store.create_project_membership(
            project_id=project_id,
            user_identifier=role_arn,
        )
