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

from typing import Any

import structlog

from coa_control_plane.namespace.cleanup import (
    delete_athena_workgroup,
    delete_role_mappings,
    delete_roles,
    delete_smu_project,
    teardown_vkg_service,
)

logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task: platform-level namespace cleanup.

    Input: {"namespaceId": str, "athenaWorkgroupName": str | None,
            "dataZoneProjectId": str | None}
    Output: {"namespaceId": str}
    """
    namespace_id = event["namespaceId"]

    teardown_vkg_service(namespace_id)
    delete_athena_workgroup(event.get("athenaWorkgroupName"))
    delete_smu_project(namespace_id, {"dataZoneProjectId": event.get("dataZoneProjectId")})

    # Role mappings/roles use the real DynamoDBDAO path (not best-effort) —
    # a failure here must fail the step so Step Functions retries it.
    delete_role_mappings(namespace_id)
    delete_roles(namespace_id)

    logger.info("namespace_platform_cleanup_complete", namespace_id=namespace_id)
    return {"namespaceId": namespace_id}
