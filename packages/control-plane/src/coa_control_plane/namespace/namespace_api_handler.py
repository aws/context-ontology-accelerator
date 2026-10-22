# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace API Lambda — combined handler for all namespace operations.

Routes:
  POST   /namespaces                       — create namespace
  GET    /namespaces                       — list namespaces
  GET    /namespaces/{namespaceId}         — get namespace
  PUT    /namespaces/{namespaceId}         — update namespace
  DELETE /namespaces/{namespaceId}         — delete namespace
  PATCH  /namespaces/{namespaceId}/status  — update namespace status
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.logging import setup_logging
from coa_common.response import api_response

from .create_handler import handler as create_handler
from .delete_handler import handler as delete_handler
from .get_handler import handler as get_handler
from .list_handler import handler as list_handler
from .update_handler import handler as update_handler
from .update_status_handler import handler as update_status_handler

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")

    # /namespaces (collection)
    if resource == "/namespaces":
        if http_method == "POST":
            return create_handler(event, context)
        if http_method == "GET":
            return list_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    # /namespaces/{namespaceId}/status
    if resource == "/namespaces/{namespaceId}/status":
        if http_method == "PATCH":
            return update_status_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    # /namespaces/{namespaceId}
    if resource == "/namespaces/{namespaceId}":
        if http_method == "GET":
            return get_handler(event, context)
        if http_method == "PUT":
            return update_handler(event, context)
        if http_method == "DELETE":
            return delete_handler(event, context)
        return api_response(405, {"message": "Method not allowed"})

    return api_response(404, {"message": "Unknown route"})
