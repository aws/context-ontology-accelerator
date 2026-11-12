# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared namespace-teardown cleanup primitives.

Used by the async DELETE handler's error paths and the Step Functions deletion
pipeline (``deletion_pipeline/``). Each function's docstring says whether it
raises or logs and continues.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

import boto3
import structlog
from coa_common import async_boto_config
from coa_common.constants import graphrag_index_names
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.opensearch import AossVectorClient

logger = structlog.get_logger(__name__)

_MAX_PAGINATION_PAGES = 1000


def delete_athena_workgroup(workgroup_name: str | None) -> None:
    """Delete Athena workgroup. Raises on failure (not-found is OK)."""
    if not workgroup_name:
        return
    athena = boto3.client("athena", region_name=os.environ.get("AWS_REGION", "us-east-1"), config=async_boto_config())
    try:
        athena.delete_work_group(WorkGroup=workgroup_name, RecursiveDeleteOption=True)
        logger.info("athena_workgroup_deleted", workgroup_name=workgroup_name)
    except athena.exceptions.InvalidRequestException as exc:
        if "not found" in str(exc).lower():
            logger.info("athena_workgroup_not_found", workgroup_name=workgroup_name)
        else:
            raise


def delete_smu_project(namespace_id: str, item: dict[str, Any]) -> None:
    """Delete DataZone project. Raises on failure (not-found is OK).

    Assumes PROJECT_ACCESS_ROLE_ARN: DataZone authorizes DeleteProject against
    project ownership, and only that role is registered as PROJECT_OWNER (see
    ``_add_service_role_memberships`` in service.py). The Lambda's own role gets
    AccessDeniedException regardless of its IAM policy.
    """
    project_id = item.get("dataZoneProjectId")
    domain_id = os.environ.get("DATAZONE_DOMAIN_ID")
    if not project_id or not domain_id:
        logger.info("smu_project_skip", namespace_id=namespace_id, project_id=project_id, domain_id=domain_id)
        return

    region = os.environ.get("AWS_REGION", "us-east-1")
    assume_role_arn = os.environ.get("PROJECT_ACCESS_ROLE_ARN")
    dz = _datazone_client(region=region, assume_role_arn=assume_role_arn)
    try:
        # skipDeletionCheck=True force-deletes the project even though it still
        # owns assets. Every induced namespace registers DataZone assets, so
        # without this the call raises ValidationException ("a dependency exists
        # ... asset ...") and namespace deletion fails — the asset lifecycle is
        # owned by the namespace teardown, not a manual pre-step.
        dz.delete_project(domainIdentifier=domain_id, identifier=project_id, skipDeletionCheck=True)
        logger.info("smu_project_deleted", namespace_id=namespace_id, project_id=project_id)
    except dz.exceptions.ResourceNotFoundException:
        logger.info("smu_project_not_found", namespace_id=namespace_id, project_id=project_id)


def _datazone_client(*, region: str, assume_role_arn: str | None):
    """Return a DataZone client, optionally assuming PROJECT_ACCESS_ROLE_ARN first.

    Falls back to the caller's own execution-role credentials when no role
    ARN is configured (e.g. local/unit-test environments).
    """
    if not assume_role_arn:
        return boto3.client("datazone", region_name=region, config=async_boto_config())

    sts = boto3.client("sts", region_name=region, config=async_boto_config())
    creds = sts.assume_role(
        RoleArn=assume_role_arn,
        RoleSessionName="ns-deletion-platform-dz-access",
    )["Credentials"]
    session = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )
    return session.client("datazone")


def delete_role_mappings(namespace_id: str) -> None:
    """Delete resource-role-mappings for namespace. Raises on failure."""
    if table := os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE"):
        _delete_all_by_index(table, "NamespaceGrantsIndex", "namespaceKey", f"NS#{namespace_id}")


def delete_roles(namespace_id: str) -> None:
    """Delete roles for namespace. Raises on failure."""
    if table := os.environ.get("ROLES_TABLE"):
        _delete_all_by_pk(table, f"NS#{namespace_id}")


def delete_namespace_records(namespace_id: str, namespace_name: str | None) -> None:
    """Atomically delete namespace record + name reservation via transaction.

    Idempotent: DynamoDB Delete operations in a transaction are no-ops when
    the item doesn't exist (no ConditionExpression is set), so retries from
    Step Functions succeed silently if the records were already removed.
    """
    region = os.environ["AWS_REGION"]
    dynamodb = boto3.client("dynamodb", region_name=region, config=async_boto_config())
    table_name = os.environ["NAMESPACES_TABLE"]

    transact_items = [
        {
            "Delete": {
                "TableName": table_name,
                "Key": {
                    "PK": {"S": f"NS#{namespace_id}"},
                    "SK": {"S": "METADATA"},
                },
            }
        },
    ]

    if namespace_name:
        transact_items.append(
            {
                "Delete": {
                    "TableName": table_name,
                    "Key": {
                        "PK": {"S": f"NS_NAME#{namespace_name}"},
                        "SK": {"S": "RESERVATION"},
                    },
                }
            }
        )

    dynamodb.transact_write_items(TransactItems=transact_items)
    logger.info("namespace_records_deleted", namespace_id=namespace_id, name=namespace_name)


def _delete_all_by_index(table_name: str, index_name: str, pk_attr: str, pk_value: str) -> None:
    """Query all items from a GSI and batch-delete them."""
    region = os.environ["AWS_REGION"]
    dao = DynamoDBDAO(table_name, region=region)
    items = dao.query_all(
        QueryParams(
            key_condition="#pk = :val",
            expression_names={"#pk": pk_attr},
            expression_values={":val": pk_value},
            index_name=index_name,
        ),
        max_pages=_MAX_PAGINATION_PAGES,
    )
    if items:
        keys = [{"PK": item["PK"], "SK": item["SK"]} for item in items]
        dao.batch_delete(keys)


def _delete_all_by_pk(table_name: str, pk_value: str) -> None:
    """Query all items by PK and batch-delete them."""
    region = os.environ["AWS_REGION"]
    dao = DynamoDBDAO(table_name, region=region)
    items = dao.query_all(
        QueryParams(
            key_condition="PK = :pk",
            expression_values={":pk": pk_value},
        ),
        max_pages=_MAX_PAGINATION_PAGES,
    )
    if items:
        keys = [{"PK": item["PK"], "SK": item["SK"]} for item in items]
        dao.batch_delete(keys)


def teardown_vkg_service(namespace_id: str) -> None:
    """Delete a namespace's VKG ECS service, task definitions, log groups and Cloud Map entry.

    Steps:
    1. Delete ECS service (force=True stops running tasks), then confirm it
       is no longer ACTIVE
    2. Deregister the service's task-definition revisions
    3. Delete the service's log group and the legacy ``/ecs/vkg-<ns>`` one
    4. Deregister Cloud Map instances + delete Cloud Map service

    Raises on any failure other than "already gone" so the deletion
    pipeline's DeletePlatform step fails retryably instead of orphaning a
    running service. Safe to re-run after a partial or completed teardown.
    Steps 2 and 3 only log their failures: neither leftover keeps anything running.
    Step 3 runs while the service is still DRAINING, so the last lines of tasks
    that are still stopping can be lost with the log groups.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    prefix = os.environ.get("RESOURCE_PREFIX", "").rstrip("-")
    cluster_arn = os.environ.get("VKG_CLUSTER_ARN", "")
    cloud_map_ns_id = os.environ.get("CLOUD_MAP_NAMESPACE_ID", "")

    if not cluster_arn:
        logger.warning("vkg_teardown_skipped_no_cluster_arn", namespace_id=namespace_id)
        return

    ecs_client = boto3.client("ecs", region_name=region, config=async_boto_config())
    service_name = f"{prefix}-vkg-{namespace_id}" if prefix else f"vkg-{namespace_id}"
    _delete_vkg_ecs_service(ecs_client, cluster_arn, service_name, namespace_id)
    _deregister_vkg_task_definitions(ecs_client, namespace_id, service_name)
    logs_client = boto3.client("logs", region_name=region, config=async_boto_config())
    for log_group in (f"/ecs/{service_name}", f"/ecs/vkg-{namespace_id}"):
        _delete_vkg_log_group(logs_client, namespace_id, log_group)

    if not cloud_map_ns_id:
        return

    sd_client = boto3.client("servicediscovery", region_name=region, config=async_boto_config())
    _delete_vkg_cloud_map_service(sd_client, cloud_map_ns_id, namespace_id)


def _delete_vkg_ecs_service(ecs_client: Any, cluster_arn: str, service_name: str, namespace_id: str) -> None:
    """Force-delete the ECS service; raise unless it (or its cluster) is gone, DRAINING or INACTIVE afterwards."""
    try:
        response = ecs_client.delete_service(cluster=cluster_arn, service=service_name, force=True)
    except ecs_client.exceptions.ServiceNotFoundException:
        logger.info("vkg_service_not_found", namespace_id=namespace_id, service_name=service_name)
        return
    except ecs_client.exceptions.ClusterNotFoundException:
        # A deleted cluster has no services left to delete.
        logger.info("vkg_cluster_not_found", namespace_id=namespace_id, service_name=service_name)
        return
    except ecs_client.exceptions.ServiceNotActiveException:
        logger.info("vkg_service_not_active", namespace_id=namespace_id, service_name=service_name)
        return

    # The delete's own response carries the new status; a describe straight after
    # can still read the old one.
    status = (response.get("service") or {}).get("status")
    if status == "ACTIVE":
        raise RuntimeError(f"VKG service {service_name} is still ACTIVE after delete_service")
    logger.info("vkg_service_deleted", namespace_id=namespace_id, service_name=service_name, status=status)


def _delete_vkg_cloud_map_service(sd_client: Any, cloud_map_ns_id: str, namespace_id: str) -> None:
    """Deregister instances and delete the Cloud Map service; already-gone counts as done."""
    service_id = _find_cloud_map_service_id(sd_client, cloud_map_ns_id, f"vkg-{namespace_id}")
    if service_id is None:
        logger.info("vkg_cloud_map_not_found", namespace_id=namespace_id)
        return

    inst_paginator = sd_client.get_paginator("list_instances")
    for inst_page in inst_paginator.paginate(ServiceId=service_id):
        for inst in inst_page.get("Instances", []):
            try:
                sd_client.deregister_instance(ServiceId=service_id, InstanceId=inst["Id"])
            except (sd_client.exceptions.InstanceNotFound, sd_client.exceptions.DuplicateRequest):
                # DuplicateRequest: ECS is deregistering the same task after the force-delete.
                continue

    # Retry: ECS may still hold a reference briefly after force-delete
    for attempt in range(4):
        try:
            sd_client.delete_service(Id=service_id)
            break
        except sd_client.exceptions.ServiceNotFound:
            break
        except sd_client.exceptions.ResourceInUse:
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    logger.info("vkg_cloud_map_deleted", namespace_id=namespace_id)


def _find_cloud_map_service_id(sd_client: Any, cloud_map_ns_id: str, service_name: str) -> str | None:
    svc_paginator = sd_client.get_paginator("list_services")
    for page in svc_paginator.paginate(
        Filters=[{"Name": "NAMESPACE_ID", "Values": [cloud_map_ns_id], "Condition": "EQ"}]
    ):
        for svc in page.get("Services", []):
            if svc["Name"] == service_name:
                return svc["Id"]
    return None


def _deregister_vkg_task_definitions(ecs_client: Any, namespace_id: str, family: str) -> None:
    """Deregister every ACTIVE revision the VKG reload Lambda registered for the namespace. Best-effort.

    The family is named like the service. ``familyPrefix`` is a prefix match, so
    the exact family is compared on each ARN.
    """
    arns: list[str] = []
    done = 0
    try:
        arns = [
            arn
            for page in ecs_client.get_paginator("list_task_definitions").paginate(familyPrefix=family, status="ACTIVE")
            for arn in page.get("taskDefinitionArns", [])
            if arn.rpartition("/")[2].rpartition(":")[0] == family
        ]
        for arn in arns:
            ecs_client.deregister_task_definition(taskDefinition=arn)
            done += 1
        logger.info("vkg_task_definitions_deregistered", namespace_id=namespace_id, count=len(arns))
    except Exception as e:
        logger.error(
            "vkg_task_definitions_deregister_failed",
            namespace_id=namespace_id,
            family=family,
            deregistered=done,
            listed=len(arns),
            error_type=type(e).__name__,
        )


def _delete_vkg_log_group(logs_client: Any, namespace_id: str, log_group: str) -> None:
    try:
        logs_client.delete_log_group(logGroupName=log_group)
        logger.info("vkg_log_group_deleted", namespace_id=namespace_id, log_group=log_group)
    except logs_client.exceptions.ResourceNotFoundException:
        logger.info("vkg_log_group_not_found", namespace_id=namespace_id, log_group=log_group)
    except Exception as e:
        logger.error(
            "vkg_log_group_delete_failed",
            namespace_id=namespace_id,
            log_group=log_group,
            error_type=type(e).__name__,
            error=str(e),
        )


def delete_graphrag_indexes(namespace_id: str) -> list[str]:
    """Delete the namespace's GraphRAG doc-KG vector indexes. Returns those removed.

    The ``chunk_*``/``topic_*`` (and legacy ``statement_*``) indexes are
    namespace-scoped: every doc source in a namespace shares one GraphRAG tenant
    id, so they are dropped here and not on source deletion. AOSS caps a
    collection at 1000 indexes, so leaking them eventually fails every embedding
    write.

    Deleting an absent index is a no-op, so this is safe to retry. Raises on a
    real failure, because the leak is unreclaimable once the namespace record is
    gone.
    """
    endpoint = os.environ.get("OSS_ENDPOINT", "")
    if not endpoint:
        logger.warning("oss_endpoint_not_configured", namespace_id=namespace_id)
        return []

    region = os.environ.get("AWS_REGION", "us-east-1")
    # dimensions is irrelevant for a delete but required by the client ctor.
    client = AossVectorClient(endpoint=endpoint, region=region, dimensions=1024, timeout=60)

    removed: list[str] = []
    for index in graphrag_index_names(namespace_id):
        if client.delete_index(index):
            removed.append(index)

    logger.info(
        "graphrag_indexes_deleted",
        namespace_id=namespace_id,
        removed=removed,
        checked=graphrag_index_names(namespace_id),
    )
    return removed


def delete_all_ontologies(namespace_id: str) -> None:
    """Call the ontology-engine to delete all ontology data for a namespace.

    Drops Neptune named graphs, deletes the AOSS vector index, and purges
    all ontology DynamoDB records (registry, jobs, proposals).

    NOT best-effort: raises on failure. The AOSS index is capped at 1000 per
    collection and cannot be targeted once the namespace record is gone, so
    raising lets the pipeline retry or land in DELETE_FAILED instead of leaking.
    """
    endpoint = os.environ.get("ONTOLOGY_ENGINE_ENDPOINT", "").rstrip("/")
    if not endpoint:
        logger.warning("ontology_engine_endpoint_not_configured", namespace_id=namespace_id)
        return

    url = f"{endpoint}/ontologies/?namespace={namespace_id}"
    req = urllib_request.Request(url, method="DELETE")

    try:
        with urllib_request.urlopen(req, timeout=120) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
            logger.info(
                "all_ontologies_deleted",
                namespace_id=namespace_id,
                graphs_dropped=body.get("graphsDropped"),
                index_deleted=body.get("indexDeleted"),
                dynamo_items_deleted=body.get("dynamoItemsDeleted"),
            )
    except (HTTPError, URLError, OSError) as e:
        logger.error(
            "ontology_deletion_failed",
            namespace_id=namespace_id,
            error=str(e),
        )
        raise


def delete_ontology_artifacts(namespace_id: str) -> None:
    """Delete all S3 ontology artifacts for a namespace. Best-effort.

    Removes everything under ontologies/{namespaceId}/ including induced
    ontologies, latest VKG files, and schema.sql.
    """
    bucket = os.environ.get("ONTOLOGY_BUCKET", "")
    if not bucket:
        return

    region = os.environ.get("AWS_REGION", "us-east-1")
    s3 = boto3.client("s3", region_name=region, config=async_boto_config())
    prefix = f"ontologies/{namespace_id}/"

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            objects = page.get("Contents", [])
            if not objects:
                continue
            delete_keys = [{"Key": obj["Key"]} for obj in objects]
            s3.delete_objects(Bucket=bucket, Delete={"Objects": delete_keys})
        logger.info("ontology_artifacts_deleted", namespace_id=namespace_id, prefix=prefix)
    except Exception as e:
        logger.error(
            "ontology_artifacts_delete_failed",
            namespace_id=namespace_id,
            error_type=type(e).__name__,
        )
