# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Create Platform Grant handler.

POST /grants

Binds a principal (user/group/agent) to a PLATFORM-scoped role such as
``platform-admin`` or ``platform-viewer``. Platform roles live in the roles
table under the synthetic ``GLOBAL`` partition. The grant is persisted under
the ``Platform::GLOBAL`` resource so it applies across all namespaces,
mirroring the record shape seeded by the authnz stack for IdP group mappings.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import structlog
from botocore.exceptions import ClientError
from coa_common import sanitize_principal_key
from coa_common.authnz_types import NON_ASSIGNABLE_ROLE_IDS, PrincipalType, ResourceType
from coa_common.dao import DynamoDBDAO
from coa_common.logging import setup_logging
from coa_common.response import api_response, get_caller_identity

from .grant_id import encode_grant_id

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_VALID_PRINCIPAL_TYPES = {e.value for e in PrincipalType}
_ROLE_SK_PREFIX = "ROLE#"
# Bounds on caller-supplied identifiers. They become DynamoDB key components,
# so cap their length and reject the delimiter characters used in key
# construction to prevent malformed/ambiguous keys and resource exhaustion.
_MAX_PRINCIPAL_ID_LEN = 256
_MAX_ROLE_LEN = 128
_KEY_DELIMITERS = ("#", "|", "::")
# Platform roles are stored under PK=GLOBAL in the roles table.
_PLATFORM_ROLE_PK = "GLOBAL"
# Synthetic resource id used for the cross-namespace platform scope. Matches
# the seed shape written by AuthnzStack (resourceId="GLOBAL").
_PLATFORM_RESOURCE_ID = "GLOBAL"
# NamespaceGrantsIndex partition key used for platform grants. Real namespace
# grants use NS#<uuid>; platform grants share this sentinel so they can be
# listed independently without a new GSI.
_PLATFORM_NAMESPACE_KEY = "NS#GLOBAL"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """Create a platform-scoped grant binding a principal to a platform role.

    Validates the request body and identifier bounds, confirms the platform
    role exists, and writes the cross-namespace grant with a
    duplicate-prevention condition.

    Args:
        event: API Gateway proxy event carrying the JSON grant request body.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 201 with the created grant, or a 4xx/5xx
        error response.
    """
    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if not body:
        return api_response(400, {"message": "Request body is required"})

    principal_type = body.get("principalType")
    principal_id = body.get("principalId")
    role = body.get("role")

    if not principal_type or not principal_id or not role:
        return api_response(400, {"message": "principalType, principalId, and role are required"})

    if principal_type not in _VALID_PRINCIPAL_TYPES:
        return api_response(400, {"message": f"Invalid principalType: {principal_type}"})

    if len(principal_id) > _MAX_PRINCIPAL_ID_LEN:
        return api_response(400, {"message": f"principalId exceeds maximum length of {_MAX_PRINCIPAL_ID_LEN}"})

    if len(role) > _MAX_ROLE_LEN:
        return api_response(400, {"message": f"role exceeds maximum length of {_MAX_ROLE_LEN}"})

    if role in NON_ASSIGNABLE_ROLE_IDS:
        # ``default`` exists under PK=GLOBAL as the baseline policy bundle, so
        # the existence check below would accept it; granting it hands the
        # principal a GLOBAL role row that filtering code treats as
        # cross-namespace (#988).
        return api_response(400, {"message": f"Role '{role}' is not assignable"})

    if any(delim in principal_id for delim in _KEY_DELIMITERS):
        return api_response(400, {"message": "principalId may not contain reserved delimiter characters (#, |, ::)"})

    region = os.environ.get("AWS_REGION")
    roles_table = os.environ.get("ROLES_TABLE")
    mappings_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE")

    if not region or not roles_table or not mappings_table:
        logger.error("missing_env_vars")
        return api_response(500, {"message": "Internal server error"})

    try:
        roles_dao = DynamoDBDAO(roles_table, region=region)
        mappings_dao = DynamoDBDAO(mappings_table, region=region)

        # 1. Validate the role exists as a platform role (PK=GLOBAL).
        role_item = roles_dao.get({"PK": _PLATFORM_ROLE_PK, "SK": f"{_ROLE_SK_PREFIX}{role}"})
        if not role_item:
            return api_response(404, {"message": f"Platform role '{role}' not found"})

        # 2. Build the platform grant record.
        caller = get_caller_identity(event)
        now = datetime.now(UTC).isoformat()

        # URL-encode the principal_id to match what the authorizer queries with
        sanitized_principal = sanitize_principal_key(principal_id)

        pk = f"{ResourceType.PLATFORM}::{_PLATFORM_RESOURCE_ID}#{principal_type}::{sanitized_principal}"
        sk = f"{_ROLE_SK_PREFIX}{role}"

        item: dict[str, Any] = {
            "PK": pk,
            "SK": sk,
            "resourceType": ResourceType.PLATFORM,
            "resourceId": _PLATFORM_RESOURCE_ID,
            "principalType": principal_type,
            "principalId": principal_id,
            "role": role,
            "principalKey": f"{principal_type}::{sanitized_principal}",
            "resourceRoleKey": f"{ResourceType.PLATFORM}::{_PLATFORM_RESOURCE_ID}#{_ROLE_SK_PREFIX}{role}",
            "namespaceKey": _PLATFORM_NAMESPACE_KEY,
            "principalRoleKey": f"{principal_type}::{sanitized_principal}#{_ROLE_SK_PREFIX}{role}",
            "grantedBy": caller,
            "grantedAt": now,
        }

        # 3. Write with a condition to prevent duplicate grants.
        mappings_dao.put(item, condition="attribute_not_exists(PK) AND attribute_not_exists(SK)", auto_timestamp=False)

        grant_response = {
            "grantId": encode_grant_id(pk, sk),
            "principalType": principal_type,
            "principalId": principal_id,
            "role": role,
            "grantedBy": caller,
            "grantedAt": now,
        }
        return api_response(201, {"grant": grant_response})

    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return api_response(409, {"message": "Platform grant already exists for this principal and role"})
        logger.exception("create_platform_grant_error")
        return api_response(500, {"message": "Internal server error"})
    except Exception:
        logger.exception("create_platform_grant_error")
        return api_response(500, {"message": "Internal server error"})
