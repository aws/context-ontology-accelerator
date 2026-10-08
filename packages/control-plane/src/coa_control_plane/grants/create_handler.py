# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Create Grant handler.

POST /namespaces/{namespaceId}/grants
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import structlog
from botocore.exceptions import ClientError
from coa_common import sanitize_principal_key
from coa_common.authnz_types import PrincipalType, ResourceType
from coa_common.constants import validate_namespace_id, validate_principal_id
from coa_common.dao import DynamoDBDAO
from coa_common.logging import setup_logging
from coa_common.response import api_response, get_caller_identity

from .grant_id import encode_grant_id

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_VALID_PRINCIPAL_TYPES = {e.value for e in PrincipalType}
_ROLE_SK_PREFIX = "ROLE#"


def _validate_grant_overrides(body: dict[str, Any]) -> str | None:
    """Validate optional data-access override fields on a grant request.

    Returns ``None`` when valid, or an error message describing the first
    violation. Mirrors the Smithy contract: ``tableAllowlist`` and
    ``allowedMetrics`` are ``list[str]``; ``columnDenylist`` is
    ``dict[str, list[str]]``.
    """
    table_allowlist = body.get("tableAllowlist")
    if table_allowlist is not None:
        if not isinstance(table_allowlist, list):
            return "tableAllowlist must be a list of strings"
        if not all(isinstance(t, str) for t in table_allowlist):
            return "tableAllowlist must contain only strings"

    allowed_metrics = body.get("allowedMetrics")
    if allowed_metrics is not None:
        if not isinstance(allowed_metrics, list):
            return "allowedMetrics must be a list of strings"
        if not all(isinstance(m, str) for m in allowed_metrics):
            return "allowedMetrics must contain only strings"

    column_denylist = body.get("columnDenylist")
    if column_denylist is not None:
        # Reject bool explicitly: bool is a subclass of int, but isinstance(True, dict) is False
        # so the standard check is sufficient.
        if not isinstance(column_denylist, dict):
            return "columnDenylist must be a mapping of table name to list of column names"
        for table, columns in column_denylist.items():
            if not isinstance(table, str):
                return "columnDenylist keys must be strings"
            if not isinstance(columns, list):
                return f"columnDenylist['{table}'] must be a list of column names"
            if not all(isinstance(c, str) for c in columns):
                return f"columnDenylist['{table}'] must contain only strings"

    return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """Create a namespace-scoped grant binding a principal to a role.

    Validates the request body, confirms the role exists in the namespace, and
    writes the grant record with a duplicate-prevention condition.

    Args:
        event: API Gateway proxy event with path parameters and JSON body.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 201 with the created grant, or a 4xx/5xx
        error response.
    """
    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")

    try:
        validate_namespace_id(namespace_id)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if not body:
        return api_response(400, {"message": "Request body is required"})

    # Validate required fields
    principal_type = body.get("principalType")
    principal_id = body.get("principalId")
    role = body.get("role")

    if not principal_type or not principal_id or not role:
        return api_response(400, {"message": "principalType, principalId, and role are required"})

    if principal_type not in _VALID_PRINCIPAL_TYPES:
        return api_response(400, {"message": f"Invalid principalType: {principal_type}"})

    try:
        validate_principal_id(principal_id)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    # Validate optional grant overrides — these fields are passed verbatim to
    # the SQL firewall via the principal's resolved profile, so the data shape
    # must be enforced at write time. A malformed value (e.g. tableAllowlist
    # as a string) would otherwise cause runtime errors or silently incorrect
    # authorization (set("orders") iterates over characters).
    override_error = _validate_grant_overrides(body)
    if override_error is not None:
        return api_response(400, {"message": override_error})

    region = os.environ.get("AWS_REGION")
    roles_table = os.environ.get("ROLES_TABLE")
    mappings_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE")

    if not region or not roles_table or not mappings_table:
        logger.error("missing_env_vars")
        return api_response(500, {"message": "Internal server error"})

    try:
        roles_dao = DynamoDBDAO(roles_table, region=region)
        mappings_dao = DynamoDBDAO(mappings_table, region=region)

        # 1. Validate role exists in namespace
        role_item = roles_dao.get({"PK": f"NS#{namespace_id}", "SK": f"{_ROLE_SK_PREFIX}{role}"})
        if not role_item:
            return api_response(404, {"message": f"Role '{role}' not found in namespace"})

        # 2. Build the grant record
        caller = get_caller_identity(event)
        now = datetime.now(UTC).isoformat()

        # URL-encode the principal_id to match what the authorizer queries with
        sanitized_principal = sanitize_principal_key(principal_id)

        pk = f"{ResourceType.NAMESPACE}::{namespace_id}#{principal_type}::{sanitized_principal}"
        sk = f"{_ROLE_SK_PREFIX}{role}"

        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "resourceType": ResourceType.NAMESPACE,
            "resourceId": namespace_id,
            "principalType": principal_type,
            "principalId": principal_id,
            "role": role,
            "principalKey": f"{principal_type}::{sanitized_principal}",
            "resourceRoleKey": f"{ResourceType.NAMESPACE}::{namespace_id}#{_ROLE_SK_PREFIX}{role}",
            "namespaceKey": f"NS#{namespace_id}",
            "principalRoleKey": f"{principal_type}::{sanitized_principal}#{_ROLE_SK_PREFIX}{role}",
            "grantedBy": caller,
            "grantedAt": now,
        }

        # Optional overrides — persisted when PRESENT (`is not None`), not when
        # truthy. An explicitly empty tableAllowlist means "no tables permitted"
        # (a deny-all restriction the SQL firewall enforces); dropping it here
        # would silently create a grant MORE permissive than the caller asked
        # for. Absence (None / key omitted) still means unrestricted.
        for opt in ("tableAllowlist", "columnDenylist", "allowedMetrics"):
            if body.get(opt) is not None:
                item[opt] = body[opt]

        # 3. Write with condition to prevent duplicates
        mappings_dao.put(item, condition="attribute_not_exists(PK) AND attribute_not_exists(SK)", auto_timestamp=False)

        grant_response = {
            "grantId": encode_grant_id(pk, sk),
            "namespaceId": namespace_id,
            "principalType": principal_type,
            "principalId": principal_id,
            "role": role,
            "grantedBy": caller,
            "grantedAt": now,
        }
        for opt in ("tableAllowlist", "columnDenylist", "allowedMetrics"):
            if opt in item:
                grant_response[opt] = item[opt]

        return api_response(201, {"grant": grant_response})

    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return api_response(409, {"message": "Grant already exists for this principal and role in namespace"})
        logger.exception("create_grant_error")
        return api_response(500, {"message": "Internal server error"})
    except Exception:
        logger.exception("create_grant_error")
        return api_response(500, {"message": "Internal server error"})
