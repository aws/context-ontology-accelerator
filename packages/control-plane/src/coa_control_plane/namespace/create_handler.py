# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Create Namespace Lambda handler.

Thin entry point: validates input, delegates to NamespaceService, returns
API Gateway proxy response.  Uses the common ``setup_logging()`` for
consistent structured JSON logging across all Lambda handlers.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from coa_common.dao import DynamoDBDAO
from coa_common.logging import setup_logging
from coa_common.metadata_store import MetadataStoreError, SMUSClient
from coa_common.response import api_response
from coa_control_plane_server.models.create_namespace_request_content import CreateNamespaceRequestContent
from pydantic import ValidationError

from .service import ConflictError, NamespaceService
from .validation_errors import format_validation_error

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_service: NamespaceService | None = None


def _get_service() -> NamespaceService:
    global _service  # noqa: PLW0603
    if _service is not None:
        return _service
    region = os.environ["AWS_REGION"]
    _service = NamespaceService(
        namespaces_dao=DynamoDBDAO(os.environ["NAMESPACES_TABLE"], region=region),
        roles_dao=DynamoDBDAO(os.environ["ROLES_TABLE"], region=region),
        mappings_dao=DynamoDBDAO(os.environ["RESOURCE_ROLE_MAPPINGS_TABLE"], region=region),
        metadata_store=SMUSClient(domain_id=os.environ["DATAZONE_DOMAIN_ID"], region_name=region),
        project_profile_id=os.environ.get("DATAZONE_PROJECT_PROFILE_ID", ""),
    )
    return _service


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Create a namespace from an API Gateway request.

    Rejects non-POST methods, parses and validates the request body, then
    delegates provisioning to ``NamespaceService`` and maps the outcome to an
    API Gateway proxy response.

    Args:
        event: API Gateway proxy event carrying the JSON namespace request
            body.
        context: Lambda context object.

    Returns:
        An API Gateway proxy response: 201 with the created namespace, 405 for
        a disallowed method, or a 4xx/5xx error response.
    """
    logger.info("event", path=event.get("path"), method=event.get("httpMethod"))

    if event.get("httpMethod") != "POST":
        return api_response(405, {"message": "Method not allowed"}, {"Allow": "POST"})

    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if body is None:
        return api_response(400, {"message": "Request body is required"})

    try:
        request = CreateNamespaceRequestContent.model_validate(body)
    except ValidationError as exc:
        return api_response(400, {"message": format_validation_error(exc)})

    try:
        ns = _get_service().create(request)
        return api_response(201, {"namespace": ns.model_dump(by_alias=True, exclude_none=True)})
    except ConflictError as exc:
        return api_response(409, {"message": str(exc)})
    except MetadataStoreError as exc:
        logger.exception("datazone_error", error=str(exc))
        return api_response(
            500,
            {
                "message": (
                    "We couldn't finish creating the namespace, and the changes were "
                    "rolled back. Please try again; if it persists, contact your administrator."
                )
            },
        )
    except Exception as exc:
        logger.exception("unhandled_error", error=str(exc))
        return api_response(500, {"message": "Internal server error"})
