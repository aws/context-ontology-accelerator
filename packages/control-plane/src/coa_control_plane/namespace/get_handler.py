# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Get Namespace handler."""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.constants import datasource_external_id
from coa_common.dao import DynamoDBDAO
from coa_common.response import api_response

from coa_control_plane.namespace.vkg_health import resolve_vkg_health_with_reason

logger = structlog.get_logger(__name__)

_ns_dao: DynamoDBDAO | None = None


def _get_ns_dao() -> DynamoDBDAO:
    global _ns_dao
    if _ns_dao is None:
        _ns_dao = DynamoDBDAO(os.environ["NAMESPACES_TABLE"], region=os.environ["AWS_REGION"])
    return _ns_dao


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle GET /namespaces/{namespaceId}."""
    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")

    if not namespace_id:
        return api_response(400, {"message": "namespaceId is required"})

    item = _get_ns_dao().get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not item:
        return api_response(404, {"message": f"Namespace not found: {namespace_id}"})

    _source_count = item.get("sourceCount", 0)
    _vkg_health, _vkg_health_reason = resolve_vkg_health_with_reason(namespace_id)

    response = {
        "namespace": {
            "namespaceId": item.get("namespaceId"),
            "name": item.get("name"),
            "displayName": item.get("displayName"),
            "description": item.get("description"),
            "owner": item.get("owner"),
            "status": item.get("status"),
            "dataZoneProjectId": item.get("dataZoneProjectId"),
            "sourceCount": _source_count,
            "createdAt": item.get("createdAt"),
            "updatedAt": item.get("updatedAt"),
            "athenaWorkgroupName": item.get("athenaWorkgroupName"),
            # Derived, not stored: the ExternalId a customer must pin in the trust
            # policy of any cross-account role they onboard into this namespace.
            "datasourceExternalId": datasource_external_id(namespace_id),
            "vkgHealth": _vkg_health,
            "vkgHealthReason": _vkg_health_reason,
        }
    }
    response["namespace"] = {k: v for k, v in response["namespace"].items() if v is not None}

    return api_response(200, response)
