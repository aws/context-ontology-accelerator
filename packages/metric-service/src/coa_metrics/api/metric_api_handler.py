# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metric API Lambda — combined handler for all metric CRUD + OSI operations.

Routes:
  POST   /namespaces/{ns}/metrics                  → create
  GET    /namespaces/{ns}/metrics                  → list
  GET    /namespaces/{ns}/metrics/{name}           → get
  PUT    /namespaces/{ns}/metrics/{name}           → update
  DELETE /namespaces/{ns}/metrics/{name}           → delete
  POST   /namespaces/{ns}/metrics/validate         → validate
  POST   /namespaces/{ns}/metrics/import-osi       → import OSI
  GET    /namespaces/{ns}/metrics/export-osi       → export OSI
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.logging import setup_logging
from coa_common.response import api_response

from .bulk_delete_metrics import handler as bulk_delete_handler
from .create_metric import handler as create_handler
from .delete_metric import handler as delete_handler
from .export_osi import handler as export_osi_handler
from .get_import_job import handler as get_import_job_handler
from .get_metric import handler as get_handler
from .get_osi_upload_url import handler as get_osi_upload_url_handler
from .import_osi import handler as import_osi_handler
from .list_metrics import handler as list_handler
from .update_metric import handler as update_handler
from .validate_metric import handler as validate_handler

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy-integration Lambda handler — routes by method + path."""
    http_method = event.get("httpMethod", "")
    resource = event.get("resource", "")
    path_params = event.get("pathParameters") or {}
    has_name = bool(path_params.get("name"))

    if resource.endswith("/import-osi/upload-url") and http_method == "POST":
        return get_osi_upload_url_handler(event, context)
    elif resource.endswith("/import-osi") and http_method == "POST":
        return import_osi_handler(event, context)
    elif resource.endswith("/export-osi") and http_method == "GET":
        return export_osi_handler(event, context)
    elif "/import-jobs/" in resource and http_method == "GET":
        return get_import_job_handler(event, context)
    elif resource.endswith("/bulk-delete-metrics") and http_method == "POST":
        return bulk_delete_handler(event, context)

    # Validate route (must be before generic POST to avoid conflict)
    if resource.endswith("/validate") and http_method == "POST":
        return validate_handler(event, context)

    if http_method == "POST":
        return create_handler(event, context)
    elif http_method == "GET" and has_name:
        return get_handler(event, context)
    elif http_method == "GET":
        return list_handler(event, context)
    elif http_method == "PUT" and has_name:
        return update_handler(event, context)
    elif http_method == "DELETE" and has_name:
        return delete_handler(event, context)
    else:
        return api_response(405, {"message": "Method not allowed"}, {"Allow": "GET, POST, PUT, DELETE"})
