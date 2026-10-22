# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grants API Lambda — combined handler for all grant operations.

Routes:
  POST   /namespaces/{namespaceId}/grants              — create grant
  GET    /namespaces/{namespaceId}/grants              — list grants (optionally by principal)
  DELETE /namespaces/{namespaceId}/grants/{grantId}    — delete grant
  GET    /principals/{principalId}/grants              — list grants for a principal
  POST   /grants                                       — create platform grant
  GET    /grants                                       — list platform grants
  DELETE /grants/{grantId}                             — delete platform grant
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.logging import setup_logging
from coa_common.response import api_response

from .create_handler import handler as create_handler
from .create_platform_grant_handler import handler as create_platform_handler
from .delete_handler import handler as delete_handler
from .delete_platform_grant_handler import handler as delete_platform_handler
from .list_handler import handler as list_handler
from .list_platform_grants_handler import handler as list_platform_handler
from .list_principal_grants_handler import handler as list_principal_handler

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")

    # /namespaces/{namespaceId}/grants (collection)
    if resource == "/namespaces/{namespaceId}/grants":
        if http_method == "POST":
            return create_handler(event, context)
        if http_method == "GET":
            return list_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    # /namespaces/{namespaceId}/grants/{grantId}
    if resource == "/namespaces/{namespaceId}/grants/{grantId}":
        if http_method == "DELETE":
            return delete_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    # /principals/{principalId}/grants
    if resource == "/principals/{principalId}/grants":
        if http_method == "GET":
            return list_principal_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    # /grants (platform-scoped collection)
    if resource == "/grants":
        if http_method == "POST":
            return create_platform_handler(event, context)
        if http_method == "GET":
            return list_platform_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    # /grants/{grantId} (platform-scoped)
    if resource == "/grants/{grantId}":
        if http_method == "DELETE":
            return delete_platform_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    return api_response(404, {"message": "Unknown route"})
