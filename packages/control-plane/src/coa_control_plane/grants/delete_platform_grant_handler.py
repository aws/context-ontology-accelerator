# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Delete Platform Grant handler.

DELETE /grants/{grantId}

Revokes a PLATFORM-scoped grant. The grant id encodes the DynamoDB PK+SK; the
handler verifies the decoded PK belongs to the platform resource before
deleting so namespace grants cannot be revoked through this endpoint.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.dao import DynamoDBDAO
from coa_common.logging import setup_logging
from coa_common.response import api_response

from .grant_id import decode_grant_id

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# PK prefix that identifies platform grants (Platform::GLOBAL#...).
_PLATFORM_RESOURCE_PREFIX = "Platform::GLOBAL#"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """Revoke a platform-scoped grant identified by its encoded grant id.

    Verifies the decoded grant belongs to the platform resource and that it
    exists before deleting, so namespace grants cannot be revoked here and a
    missing grant returns 404 rather than a misleading 200.

    Args:
        event: API Gateway proxy event with a ``grantId`` path parameter.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 on success, or a 4xx/5xx error
        response.
    """
    path_params = event.get("pathParameters") or {}
    grant_id = path_params.get("grantId", "")

    if not grant_id:
        return api_response(400, {"message": "grantId is required"})

    try:
        pk, sk = decode_grant_id(grant_id)
    except Exception:
        return api_response(400, {"message": "Invalid grantId format"})

    # Only platform grants may be revoked through this endpoint.
    if not pk.startswith(_PLATFORM_RESOURCE_PREFIX):
        return api_response(404, {"message": "Platform grant not found"})

    region = os.environ.get("AWS_REGION")
    mappings_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE")

    if not region or not mappings_table:
        logger.error("missing_env_vars")
        return api_response(500, {"message": "Internal server error"})

    try:
        mappings_dao = DynamoDBDAO(mappings_table, region=region)
        # Verify the grant exists first. DynamoDB DeleteItem is idempotent and
        # succeeds even when the item is absent, which would otherwise return a
        # misleading 200 for a non-existent grant. Return 404 instead.
        existing = mappings_dao.get({"PK": pk, "SK": sk})
        if not existing:
            return api_response(404, {"message": "Platform grant not found"})
        mappings_dao.delete({"PK": pk, "SK": sk})
        return api_response(200, {})

    except Exception:
        logger.exception("delete_platform_grant_error")
        return api_response(500, {"message": "Internal server error"})
