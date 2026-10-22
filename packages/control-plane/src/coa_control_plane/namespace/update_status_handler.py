# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Update Namespace Status handler — enforces lifecycle state machine."""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from coa_common.constants import validate_namespace_id
from coa_common.dao import DynamoDBDAO
from coa_common.language_tags import effective_namespace_language
from coa_common.response import api_response
from coa_control_plane_server.models.namespace_status import NamespaceStatus

logger = structlog.get_logger(__name__)

_ns_dao: DynamoDBDAO | None = None

# Valid state transitions per the LLD state machine:
#
#   ACTIVE ──────► ARCHIVED
#     ▲               │
#     └───────────────┘
#
# DELETING/DELETED are only reachable via the DELETE endpoint
# (delete_handler.py + the async deletion pipeline), which performs the
# actual resource cleanup. DELETE_FAILED→DELETING is also handled
# exclusively by the DELETE endpoint (retry pattern) — none of these three
# statuses are valid PATCH targets or sources here.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    NamespaceStatus.ACTIVE: {NamespaceStatus.ARCHIVED},
    NamespaceStatus.ARCHIVED: {NamespaceStatus.ACTIVE},
}


def _get_ns_dao() -> DynamoDBDAO:
    global _ns_dao
    if _ns_dao is None:
        _ns_dao = DynamoDBDAO(os.environ["NAMESPACES_TABLE"], region=os.environ["AWS_REGION"])
    return _ns_dao


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle PATCH /namespaces/{namespaceId}/status.

    Enforces the namespace lifecycle state machine. Returns 409 Conflict
    if the requested transition is not allowed from the current state.
    """
    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")

    if not namespace_id:
        return api_response(400, {"message": "namespaceId is required"})
    try:
        validate_namespace_id(namespace_id)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON body"})

    if not isinstance(body, dict):
        return api_response(400, {"message": "Request body must be a JSON object"})

    new_status = body.get("status")
    if not new_status:
        return api_response(400, {"message": "status is required"})

    # Validate status value
    try:
        NamespaceStatus(new_status)
    except (ValueError, TypeError):
        valid = [s.value for s in NamespaceStatus]
        return api_response(400, {"message": f"Invalid status. Must be one of: {valid}"})

    # Fetch current namespace
    item = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not item:
        return api_response(404, {"message": "Namespace not found"})

    current_status = item.get("status")

    # Enforce state machine transitions
    allowed = _VALID_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        return api_response(
            409,
            {"message": f"Cannot transition from {current_status} to {new_status}. Allowed: {sorted(allowed)}"},
        )

    _get_ns_dao().update(key={"PK": f"NS#{namespace_id}", "SK": "METADATA"}, update_fields={"status": new_status})

    # Return updated namespace
    updated = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    ns = {
        "namespaceId": updated.get("namespaceId"),
        "name": updated.get("name"),
        "displayName": updated.get("displayName"),
        "description": updated.get("description"),
        "owner": updated.get("owner"),
        "status": updated.get("status"),
        "language": effective_namespace_language(updated),
        "dataZoneProjectId": updated.get("dataZoneProjectId"),
        "sourceCount": updated.get("sourceCount", 0),
        "createdAt": updated.get("createdAt"),
        "updatedAt": updated.get("updatedAt"),
        "athenaWorkgroupName": updated.get("athenaWorkgroupName"),
    }
    ns = {k: v for k, v in ns.items() if v is not None}

    return api_response(200, {"namespace": ns})
