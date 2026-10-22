# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Delete Grant handler.

DELETE /namespaces/{namespaceId}/grants/{grantId}
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.constants import validate_namespace_id
from coa_common.dao import DynamoDBDAO
from coa_common.logging import setup_logging
from coa_common.response import api_response

from .grant_id import decode_grant_id

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """Delete a namespace-scoped grant identified by its encoded grant id.

    Verifies the decoded grant belongs to the target namespace before deleting
    so grants from other namespaces cannot be revoked through this endpoint.

    Args:
        event: API Gateway proxy event with ``namespaceId`` and ``grantId``
            path parameters.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 on success, or a 4xx/5xx error
        response.
    """
    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")
    grant_id = path_params.get("grantId", "")

    try:
        validate_namespace_id(namespace_id)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    if not grant_id:
        return api_response(400, {"message": "grantId is required"})

    try:
        pk, sk = decode_grant_id(grant_id)
    except Exception:
        return api_response(400, {"message": "Invalid grantId format"})

    # Verify the grant belongs to this namespace
    if not pk.startswith(f"Namespace::{namespace_id}#"):
        return api_response(404, {"message": "Grant not found"})

    region = os.environ.get("AWS_REGION")
    mappings_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE")

    if not region or not mappings_table:
        logger.error("missing_env_vars")
        return api_response(500, {"message": "Internal server error"})

    try:
        mappings_dao = DynamoDBDAO(mappings_table, region=region)
        mappings_dao.delete({"PK": pk, "SK": sk})
        return api_response(200, {})

    except Exception:
        logger.exception("delete_grant_error")
        return api_response(500, {"message": "Internal server error"})
