# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""List Platform Roles Lambda handler.

Returns platform-level roles from DynamoDB (PK=GLOBAL, SK begins_with ROLE#).
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from coa_common.authnz_types import NON_ASSIGNABLE_ROLE_IDS
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.logging import setup_logging
from coa_common.response import api_response
from coa_control_plane_server.models.role_scope import RoleScope
from coa_control_plane_server.models.role_summary import RoleSummary

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_PLATFORM_PK = "GLOBAL"
_ROLE_SK_PREFIX = "ROLE#"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """List platform-level roles from the roles table.

    Rejects non-GET methods, then queries the roles table for the ``GLOBAL``
    partition (SK begins with ``ROLE#``).

    Args:
        event: API Gateway proxy event.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 with the platform roles, 405 for a
        disallowed method, or a 5xx error response.
    """
    logger.info("list_platform_roles", path=event.get("path"), method=event.get("httpMethod"))

    if event.get("httpMethod") != "GET":
        return api_response(405, {"message": "Method not allowed"}, {"Allow": "GET"})

    region = os.environ.get("AWS_REGION")
    roles_table = os.environ.get("ROLES_TABLE")

    if not region or not roles_table:
        logger.error("missing_env_vars", region=bool(region), roles_table=bool(roles_table))
        return api_response(500, {"message": "Internal server error"})

    roles_dao = DynamoDBDAO(roles_table, region=region)

    try:
        result = roles_dao.query(
            QueryParams(
                key_condition="PK = :pk AND begins_with(SK, :prefix)",
                expression_values={":pk": _PLATFORM_PK, ":prefix": _ROLE_SK_PREFIX},
            )
        )

        # The GLOBAL partition also holds the ``default`` baseline policy bundle,
        # which is applied to every principal and must not be offered as a
        # grantable role (#988).
        roles = [
            _to_summary(item)
            for item in result.items
            if item.get("SK", "").removeprefix(_ROLE_SK_PREFIX) not in NON_ASSIGNABLE_ROLE_IDS
        ]
        return api_response(200, {"roles": [r.model_dump(by_alias=True, exclude_none=True) for r in roles]})
    except Exception:
        logger.exception("list_platform_roles_error")
        return api_response(500, {"message": "Internal server error"})


def _to_summary(item: dict[str, Any]) -> RoleSummary:
    sk = item.get("SK", "")
    if not sk:
        logger.warning("role_item_missing_sk", item_pk=item.get("PK"))
    role_id = sk.removeprefix(_ROLE_SK_PREFIX)
    return RoleSummary.model_construct(
        role_id=role_id,
        name=item.get("name", role_id),
        description=item.get("description"),
        scope=RoleScope.PLATFORM,
        is_built_in=item.get("isBuiltIn", True),
    )
