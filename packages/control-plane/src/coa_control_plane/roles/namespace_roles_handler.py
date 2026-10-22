# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace Roles Lambda handler.

Routes:
  GET /namespaces/{namespaceId}/roles          — list roles in namespace
  GET /namespaces/{namespaceId}/roles/{roleId} — get role detail
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.constants import validate_id, validate_namespace_id
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.logging import setup_logging
from coa_common.response import api_response
from coa_control_plane_server.models.role_detail import RoleDetail
from coa_control_plane_server.models.role_scope import RoleScope
from coa_control_plane_server.models.role_summary import RoleSummary

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_ROLE_SK_PREFIX = "ROLE#"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """List roles in a namespace or return a single role's detail.

    Rejects non-GET methods, then routes on the presence of a ``roleId`` path
    parameter to either list namespace roles or fetch one role.

    Args:
        event: API Gateway proxy event with a ``namespaceId`` and optional
            ``roleId`` path parameter.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 with the role(s), 405 for a
        disallowed method, or a 4xx/5xx error response.
    """
    logger.info("namespace_roles", path=event.get("path"), method=event.get("httpMethod"))

    if event.get("httpMethod") != "GET":
        return api_response(405, {"message": "Method not allowed"}, {"Allow": "GET"})

    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")

    try:
        validate_namespace_id(namespace_id)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    role_id = path_params.get("roleId")
    if role_id:
        try:
            validate_id(role_id, "roleId")
        except ValueError as exc:
            return api_response(400, {"message": str(exc)})

    try:
        region = os.environ.get("AWS_REGION")
        roles_table = os.environ.get("ROLES_TABLE")

        if not region or not roles_table:
            logger.error("missing_env_vars", region=bool(region), roles_table=bool(roles_table))
            return api_response(500, {"message": "Internal server error"})

        roles_dao = DynamoDBDAO(roles_table, region=region)

        if role_id:
            return _get_role(roles_dao, namespace_id, role_id)
        return _list_roles(roles_dao, namespace_id)
    except Exception:
        logger.exception("namespace_roles_error")
        return api_response(500, {"message": "Internal server error"})


def _list_roles(roles_dao: DynamoDBDAO, namespace_id: str) -> dict[str, Any]:
    """List all roles (built-in + custom) for a namespace."""
    result = roles_dao.query(
        QueryParams(
            key_condition="PK = :pk AND begins_with(SK, :prefix)",
            expression_values={":pk": f"NS#{namespace_id}", ":prefix": _ROLE_SK_PREFIX},
        )
    )

    roles = [_to_summary(item) for item in result.items]
    return api_response(200, {"roles": [r.model_dump(by_alias=True, exclude_none=True) for r in roles]})


def _get_role(roles_dao: DynamoDBDAO, namespace_id: str, role_id: str) -> dict[str, Any]:
    """Get a single role's detail."""
    item = roles_dao.get({"PK": f"NS#{namespace_id}", "SK": f"{_ROLE_SK_PREFIX}{role_id}"})

    if not item:
        return api_response(404, {"message": "Role not found"})

    detail = RoleDetail.model_construct(
        role_id=role_id,
        name=item.get("name", role_id),
        description=item.get("description"),
        scope=RoleScope.NAMESPACE,
        is_built_in=item.get("isBuiltIn", False),
        cedar_policy=item.get("cedarPolicy"),
    )
    return api_response(200, {"role": detail.model_dump(by_alias=True, exclude_none=True)})


def _to_summary(item: dict[str, Any]) -> RoleSummary:
    sk = item.get("SK", "")
    if not sk:
        logger.warning("role_item_missing_sk", item_pk=item.get("PK"))
    role_id = sk.removeprefix(_ROLE_SK_PREFIX)
    return RoleSummary.model_construct(
        role_id=role_id,
        name=item.get("name", role_id),
        description=item.get("description"),
        scope=RoleScope(item.get("scope", "NAMESPACE")),
        is_built_in=item.get("isBuiltIn", False),
    )
