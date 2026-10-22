# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""List Grants handler.

GET /namespaces/{namespaceId}/grants
GET /namespaces/{namespaceId}/grants?principal={id}

Supports pagination via ``maxResults`` (page size) and ``nextToken`` (opaque
base64-encoded DynamoDB LastEvaluatedKey).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

import structlog
from coa_common.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, validate_namespace_id
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.logging import setup_logging
from coa_common.pagination import check_start_key, is_rejected_start_key
from coa_common.response import api_response

from .contract import validate_list_query
from .grant_id import encode_grant_id

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# Key attributes of each GSI's items: base-table keys plus the index keys. A
# resume key must be exactly one of these, in the partition being queried.
_PRINCIPAL_INDEX_KEY_ATTRS = ("PK", "SK", "principalKey", "resourceRoleKey")
_NAMESPACE_INDEX_KEY_ATTRS = ("PK", "SK", "namespaceKey", "principalRoleKey")


def _decode_next_token(token: str) -> dict[str, Any]:
    """Decode a pagination token, raising ValueError with a specific reason."""
    try:
        raw = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not valid base64") from exc
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("must decode to a key object")
    return decoded


def _encode_next_token(key: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(key).encode()).decode()


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """List grants in a namespace, optionally filtered by principal.

    Reads pagination controls (``maxResults``, ``nextToken``) from the query
    string and queries the resource-role-mappings table accordingly.

    Args:
        event: API Gateway proxy event with a ``namespaceId`` path parameter
            and optional query-string filters.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 with the grant page and next token,
        or a 4xx/5xx error response.
    """
    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")

    try:
        validate_namespace_id(namespace_id)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    query_params = event.get("queryStringParameters") or {}
    principal = query_params.get("principal")

    # Pagination parameters
    raw_max = query_params.get("maxResults")
    try:
        max_results = int(raw_max) if raw_max is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        return api_response(400, {"message": "maxResults must be an integer"})
    if max_results < 1 or max_results > MAX_PAGE_SIZE:
        return api_response(400, {"message": f"maxResults must be between 1 and {MAX_PAGE_SIZE}"})

    next_token = query_params.get("nextToken")
    bounds_error = validate_list_query(principal, next_token)
    if bounds_error is not None:
        return api_response(400, {"message": bounds_error})
    exclusive_start_key: dict[str, Any] | None = None
    if next_token:
        try:
            exclusive_start_key = _decode_next_token(next_token)
            if principal:
                check_start_key(
                    exclusive_start_key,
                    _PRINCIPAL_INDEX_KEY_ATTRS,
                    expected={"principalKey": principal},
                    prefixes={"resourceRoleKey": f"Namespace::{namespace_id}#"},
                )
            else:
                check_start_key(
                    exclusive_start_key,
                    _NAMESPACE_INDEX_KEY_ATTRS,
                    expected={"namespaceKey": f"NS#{namespace_id}"},
                )
        except ValueError as exc:
            return api_response(400, {"message": f"Invalid nextToken: {exc}"})

    region = os.environ.get("AWS_REGION")
    mappings_table = os.environ.get("RESOURCE_ROLE_MAPPINGS_TABLE")

    if not region or not mappings_table:
        logger.error("missing_env_vars")
        return api_response(500, {"message": "Internal server error"})

    try:
        mappings_dao = DynamoDBDAO(mappings_table, region=region)

        if principal:
            # Query PrincipalIndex GSI filtered to this namespace
            result = mappings_dao.query(
                QueryParams(
                    key_condition="principalKey = :pk AND begins_with(resourceRoleKey, :ns)",
                    expression_values={
                        ":pk": principal,
                        ":ns": f"Namespace::{namespace_id}#",
                    },
                    index_name="PrincipalIndex",
                    limit=max_results,
                    exclusive_start_key=exclusive_start_key,
                )
            )
        else:
            # Query NamespaceGrantsIndex GSI
            result = mappings_dao.query(
                QueryParams(
                    key_condition="namespaceKey = :nk",
                    expression_values={":nk": f"NS#{namespace_id}"},
                    index_name="NamespaceGrantsIndex",
                    limit=max_results,
                    exclusive_start_key=exclusive_start_key,
                )
            )

        grants = [_to_grant_summary(item, namespace_id) for item in result.items]
        body: dict[str, Any] = {"grants": grants}
        if result.last_evaluated_key:
            body["nextToken"] = _encode_next_token(result.last_evaluated_key)
        return api_response(200, body)

    except Exception as exc:
        if exclusive_start_key is not None and is_rejected_start_key(exc):
            return api_response(400, {"message": "Invalid nextToken: not a key for this listing"})
        logger.exception("list_grants_error")
        return api_response(500, {"message": "Internal server error"})


def _to_grant_summary(item: dict[str, Any], namespace_id: str) -> dict[str, Any]:
    summary = {
        "grantId": encode_grant_id(item["PK"], item["SK"]),
        "namespaceId": namespace_id,
        "principalType": item.get("principalType", ""),
        "principalId": item.get("principalId", ""),
        "role": item.get("role", ""),
        "grantedBy": item.get("grantedBy", ""),
        "grantedAt": item.get("grantedAt", ""),
    }
    # ``is not None``, not truthiness: a declared-empty override (e.g.
    # tableAllowlist: []) is a deny-all restriction and must stay visible to
    # reviewers — omitting it makes a deny-all grant indistinguishable from an
    # unrestricted one in every listing.
    for opt in ("tableAllowlist", "columnDenylist", "allowedMetrics"):
        if item.get(opt) is not None:
            summary[opt] = item[opt]
    return summary
