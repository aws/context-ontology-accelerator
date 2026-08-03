# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""List Principal Grants handler.

GET /principals/{principalId}/grants

When principalId is the SELF_PRINCIPAL sentinel, resolves the caller's email
and groups from the authorizer context and returns all grants for the user
and their groups.
"""

from __future__ import annotations

import os
import re
from typing import Any

import structlog
from coa_common import sanitize_principal_key
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.logging import setup_logging
from coa_common.response import api_response

from .grant_id import encode_grant_id

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SELF_PRINCIPAL = "me"
"""Sentinel value for principalId that resolves to the authenticated caller."""

_CLAIM_EMAIL = "email"
_CLAIM_GROUPS = "groups"
_CLAIM_COGNITO_GROUPS = "cognito:groups"

_MAX_GROUPS = 10
"""Maximum number of groups to query to prevent unbounded DynamoDB calls."""

_GROUP_NAME_RE = re.compile(r"^[\w.@\-]+$")
"""Valid group name pattern — alphanumeric, dots, @, hyphens."""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """List all grants held by the calling principal and their groups.

    Only self-lookup is permitted: the principal id must be the ``me``
    sentinel. The caller's email and group memberships are resolved from the
    authorizer context and every matching grant is returned.

    Args:
        event: API Gateway proxy event with a ``principalId`` path parameter
            and authorizer-populated caller context.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 with the caller's grants, or a
        4xx/5xx error response.
    """
    path_params = event.get("pathParameters") or {}
    principal_id = path_params.get("principalId", "")

    if not principal_id:
        return api_response(400, {"message": "principalId is required"})

    # Only allow self-lookup — other users' grants require admin endpoints
    if principal_id != SELF_PRINCIPAL:
        return api_response(403, {"message": "Access denied. Use 'me' to query your own grants."})

    region = os.environ.get("AWS_REGION")
    mappings_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE")

    if not region or not mappings_table:
        logger.error("missing_env_vars")
        return api_response(500, {"message": "Internal server error"})

    auth_context = event.get("requestContext", {}).get("authorizer", {})
    claims = auth_context.get("claims", {})

    email = auth_context.get(_CLAIM_EMAIL) or claims.get(_CLAIM_EMAIL, "")
    groups_str = auth_context.get(_CLAIM_GROUPS) or claims.get(_CLAIM_COGNITO_GROUPS, "")

    if not email:
        return api_response(400, {"message": "Unable to resolve caller identity"})

    # Normalize email to lowercase for consistent lookups
    email = email.strip().lower()

    principal_keys: list[str] = [f"User::{sanitize_principal_key(email)}"]

    if groups_str:
        groups_added = 0
        for group in groups_str.split(","):
            group = group.strip()
            if not group:
                continue
            if not _GROUP_NAME_RE.match(group):
                logger.warning("invalid_group_name_skipped", group=group)
                continue
            if groups_added >= _MAX_GROUPS:
                logger.warning("groups_truncated", total_groups=groups_str.count(",") + 1, max=_MAX_GROUPS)
                break
            principal_keys.append(f"Group::{sanitize_principal_key(group)}")
            groups_added += 1

    try:
        mappings_dao = DynamoDBDAO(mappings_table, region=region)
        seen_grant_ids: set[str] = set()
        grants: list[dict[str, Any]] = []

        for pk in principal_keys:
            # query_all, not query: a single query returns one 1 MB page, so a
            # principal with many grants would see an arbitrary subset of their
            # own permissions here — with no indication the list is incomplete.
            items = mappings_dao.query_all(
                QueryParams(
                    key_condition="#pk = :pk",
                    expression_values={":pk": pk},
                    expression_names={"#pk": "principalKey"},
                    index_name="PrincipalIndex",
                )
            )
            for item in items:
                summary = _to_grant_summary(item)
                if summary["grantId"] not in seen_grant_ids:
                    seen_grant_ids.add(summary["grantId"])
                    grants.append(summary)

        return api_response(200, {"grants": grants})

    except Exception:
        logger.exception("list_principal_grants_error", principal_id=email)
        return api_response(500, {"message": "Internal server error"})


def _to_grant_summary(item: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "grantId": encode_grant_id(item["PK"], item["SK"]),
        "namespaceId": item.get("resourceId", ""),
        "principalType": item.get("principalType", ""),
        "principalId": item.get("principalId", ""),
        "role": item.get("role", ""),
        "grantedBy": item.get("grantedBy", ""),
        "grantedAt": item.get("grantedAt", ""),
    }
    for opt in ("tableAllowlist", "columnDenylist", "allowedMetrics"):
        if item.get(opt):
            summary[opt] = item[opt]
    return summary
