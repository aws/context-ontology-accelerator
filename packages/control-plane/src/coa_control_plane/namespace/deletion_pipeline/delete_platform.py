# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion pipeline step: platform-level cleanup.

Tears down the VKG ECS service, Athena workgroup, DataZone project, and
namespace role mappings/roles. Each raises on failure (already-deleted is
not a failure), so the state machine's retry policy applies and, once
retries are exhausted, the namespace lands in DELETE_FAILED: Finalize
deletes the namespace record, so a VKG service left running here would be
orphaned with nothing left to retry its delete.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.dao import DynamoDBDAO
from coa_control_plane_server.models.namespace_status import NamespaceStatus

from coa_control_plane.namespace.cleanup import (
    delete_athena_workgroup,
    delete_role_mappings,
    delete_roles,
    delete_smu_project,
    teardown_vkg_service,
)

logger = structlog.get_logger(__name__)

_ns_dao: DynamoDBDAO | None = None


def _get_ns_dao() -> DynamoDBDAO:
    global _ns_dao  # noqa: PLW0603
    if _ns_dao is None:
        _ns_dao = DynamoDBDAO(os.environ["NAMESPACES_TABLE"], region=os.environ["AWS_REGION"])
    return _ns_dao


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task: platform-level namespace cleanup.

    Input: {"namespaceId": str}. The workgroup and DataZone project ids come
    from the namespace record, which must be DELETING; ids in the input are ignored.
    Output: {"namespaceId": str}
    """
    namespace_id = event["namespaceId"]
    record = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"}, consistent_read=True)
    if record is None:
        logger.info("namespace_platform_cleanup_skipped_no_record", namespace_id=namespace_id)
        return {"namespaceId": namespace_id}
    if record.get("status") != NamespaceStatus.DELETING:
        # The delete API starts the execution just before it writes DELETING; the step's retry covers that window.
        raise RuntimeError(f"namespace {namespace_id} is {record.get('status')}, not DELETING")

    teardown_vkg_service(namespace_id)
    delete_athena_workgroup(record.get("athenaWorkgroupName"))
    delete_smu_project(namespace_id, {"dataZoneProjectId": record.get("dataZoneProjectId")})

    delete_role_mappings(namespace_id)
    delete_roles(namespace_id)

    logger.info("namespace_platform_cleanup_complete", namespace_id=namespace_id)
    return {"namespaceId": namespace_id}
