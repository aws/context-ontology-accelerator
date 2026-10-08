# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Update Namespace handler."""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from coa_common.dao import DynamoDBDAO
from coa_common.response import api_response
from coa_control_plane_server.models.update_namespace_request_content import (
    UpdateNamespaceRequestContent,
)
from pydantic import ValidationError

from .validation_errors import format_validation_error

logger = structlog.get_logger(__name__)

_ns_dao: DynamoDBDAO | None = None

_MAX_DISPLAY_NAME = 256
_MAX_DESCRIPTION = 1024


def _get_ns_dao() -> DynamoDBDAO:
    global _ns_dao
    if _ns_dao is None:
        _ns_dao = DynamoDBDAO(os.environ["NAMESPACES_TABLE"], region=os.environ["AWS_REGION"])
    return _ns_dao


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle PUT /namespaces/{namespaceId}."""
    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")

    if not namespace_id:
        return api_response(400, {"message": "namespaceId is required"})

    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON body"})

    try:
        req = UpdateNamespaceRequestContent.model_validate(body)
    except ValidationError as exc:
        return api_response(400, {"message": format_validation_error(exc)})

    # Verify namespace exists
    item = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not item:
        return api_response(404, {"message": f"Namespace not found: {namespace_id}"})

    # Build update fields from validated request
    update_fields: dict[str, Any] = {}
    if req.display_name is not None:
        if len(req.display_name) > _MAX_DISPLAY_NAME:
            return api_response(400, {"message": f"displayName must be {_MAX_DISPLAY_NAME} characters or fewer"})
        update_fields["displayName"] = req.display_name
    if req.description is not None:
        if len(req.description) > _MAX_DESCRIPTION:
            return api_response(400, {"message": f"description must be {_MAX_DESCRIPTION} characters or fewer"})
        update_fields["description"] = req.description

    if not update_fields:
        return api_response(400, {"message": "No updatable fields provided"})

    _get_ns_dao().update(key={"PK": f"NS#{namespace_id}", "SK": "METADATA"}, update_fields=update_fields)

    # Return updated namespace
    updated = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    ns = {
        "namespaceId": updated.get("namespaceId"),
        "name": updated.get("name"),
        "displayName": updated.get("displayName"),
        "description": updated.get("description"),
        "owner": updated.get("owner"),
        "status": updated.get("status"),
        "dataZoneProjectId": updated.get("dataZoneProjectId"),
        "sourceCount": updated.get("sourceCount", 0),
        "createdAt": updated.get("createdAt"),
        "updatedAt": updated.get("updatedAt"),
        "athenaWorkgroupName": updated.get("athenaWorkgroupName"),
    }
    ns = {k: v for k, v in ns.items() if v is not None}

    return api_response(200, {"namespace": ns})
