# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""List Namespaces Lambda handler.

Returns namespaces visible to the authenticated user:
- Users with a GLOBAL role see all namespaces.
- Users without a global role see only namespaces where they have a
  scoped role assignment in the ResourceRoleMappings table.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import structlog
from coa_common import sanitize_principal_key
from coa_common.authnz_types import PrincipalType, ResourceType
from coa_common.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.logging import setup_logging
from coa_common.response import api_response
from coa_control_plane_server.models.namespace_status import NamespaceStatus
from coa_control_plane_server.models.namespace_summary import NamespaceSummary
from pydantic import BaseModel, Field, field_validator

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# DynamoDB key constants
_NS_PK_PREFIX = "NS#"
_METADATA_SK = "METADATA"


# ── Pydantic input models ─────────────────────────────────────────


class ListNamespacesParams(BaseModel):
    """Validated query string parameters."""

    next_token: str | None = Field(None, alias="nextToken")
    max_results: int = Field(DEFAULT_PAGE_SIZE, alias="maxResults", ge=1, le=MAX_PAGE_SIZE)

    @field_validator("next_token")
    @classmethod
    def _validate_token(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            decoded = base64.b64decode(v)
            # Accept both JSON (scan key) and integer (offset) formats
            try:
                json.loads(decoded)
            except (json.JSONDecodeError, ValueError):
                int(decoded)
        except Exception as exc:
            raise ValueError("Invalid nextToken") from exc
        return v


class AuthorizerContext(BaseModel):
    """Extracted authorizer context from API Gateway."""

    user_id: str = Field("", alias="userId")
    global_roles: list[str] = Field(default_factory=list, alias="globalRoles")
    groups: list[str] = Field(default_factory=list, alias="groups")

    @field_validator("global_roles", "groups", mode="before")
    @classmethod
    def _split_csv(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            return [x for x in v.split(",") if x]
        return v or []


# ── Handler ───────────────────────────────────────────────────────


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # noqa: ARG001
    """List namespaces visible to the authenticated caller.

    Callers with a global role see every namespace; others see only namespaces
    where they hold a scoped role assignment. Rejects non-GET methods.

    Args:
        event: API Gateway proxy event with authorizer-populated caller
            context.
        context: Lambda context object (unused).

    Returns:
        An API Gateway proxy response: 200 with the visible namespaces, 405 for
        a disallowed method, or a 5xx error response.
    """
    logger.info("list_namespaces", path=event.get("path"), method=event.get("httpMethod"))

    if event.get("httpMethod") != "GET":
        return api_response(405, {"message": "Method not allowed"}, {"Allow": "GET"})

    region = os.environ["AWS_REGION"]
    ns_dao = DynamoDBDAO(os.environ["NAMESPACES_TABLE"], region=region)
    mappings_dao = DynamoDBDAO(os.environ["RESOURCE_ROLE_MAPPINGS_TABLE"], region=region)

    # Validate authorizer context
    authorizer_ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    auth = AuthorizerContext.model_validate(authorizer_ctx)

    # Validate query params
    raw_params = event.get("queryStringParameters") or {}
    try:
        params = ListNamespacesParams.model_validate(raw_params)
    except Exception as exc:
        return api_response(400, {"message": str(exc.errors()[0]["msg"]) if hasattr(exc, "errors") else str(exc)})

    try:
        if auth.global_roles:
            items, out_token = _list_all(ns_dao, params.max_results, params.next_token)
        else:
            items, out_token = _list_for_user(
                ns_dao,
                mappings_dao,
                auth.user_id,
                auth.groups,
                params.max_results,
                params.next_token,
            )

        body: dict[str, Any] = {
            "namespaces": [ns.model_dump(by_alias=True, exclude_none=True) for ns in items],
        }
        if out_token:
            body["nextToken"] = out_token
        return api_response(200, body)
    except Exception:
        logger.exception("list_namespaces_error")
        return api_response(500, {"message": "Internal server error"})


# ── Data access ───────────────────────────────────────────────────


def _list_all(
    ns_dao: DynamoDBDAO,
    limit: int,
    next_token: str | None,
) -> tuple[list[NamespaceSummary], str | None]:
    """Scan all namespace METADATA records (for users with global roles).

    Loops the scan until we collect `limit` items or exhaust the table.

    A single DynamoDB Scan page can return MORE matching items than the
    caller's `limit` (that's the point of over-fetching with `limit * 3`).
    When that happens, the inner loop breaks early, but the *scan itself*
    may already be exhausted (``result.last_evaluated_key`` is ``None``)
    even though the current page still has leftover, un-returned items
    past the truncation point. Relying on ``last_evaluated_key`` alone in
    that case drops those leftover items silently (no way for the caller
    to ever see them). Instead, whenever the inner loop truncates early,
    synthesize the next token from the DynamoDB key of the last item we
    actually returned, so the next call resumes the SAME scan page from
    there rather than skipping straight to (a nonexistent) subsequent page.
    """
    exclusive_start_key = json.loads(base64.b64decode(next_token)) if next_token else None

    items: list[NamespaceSummary] = []
    last_key = exclusive_start_key

    while len(items) < limit:
        result = ns_dao.scan(
            filter_expression="SK = :sk",
            expression_values={":sk": _METADATA_SK},
            limit=limit * 3,
            exclusive_start_key=last_key,
        )
        truncated_mid_page = False
        last_returned_raw_item: dict[str, Any] | None = None
        for item in result.items:
            items.append(_to_summary(item))
            last_returned_raw_item = item
            if len(items) >= limit:
                truncated_mid_page = item is not result.items[-1]
                break

        if truncated_mid_page and last_returned_raw_item is not None:
            # More matching items exist in THIS already-fetched page past
            # where we stopped — resume from the last item we kept, not
            # from result.last_evaluated_key (which may be None/exhausted
            # for the underlying scan even though we still have unreturned
            # items in hand).
            last_key = {"PK": last_returned_raw_item["PK"], "SK": last_returned_raw_item["SK"]}
            break

        last_key = result.last_evaluated_key
        if not last_key:
            break

    out_token = None
    if last_key:
        out_token = base64.b64encode(json.dumps(last_key).encode()).decode()

    return items, out_token


def _list_for_user(
    ns_dao: DynamoDBDAO,
    mappings_dao: DynamoDBDAO,
    user_id: str,
    groups: list[str],
    limit: int,
    next_token: str | None,
) -> tuple[list[NamespaceSummary], str | None]:
    """List namespaces where the user has a scoped role assignment."""
    namespace_ids = resolve_accessible_namespace_ids(mappings_dao, user_id, groups)

    if not namespace_ids:
        return [], None

    keys = [{"PK": f"{_NS_PK_PREFIX}{ns_id}", "SK": _METADATA_SK} for ns_id in namespace_ids]
    records = ns_dao.batch_get(keys)
    items = sorted(
        [_to_summary(r) for r in records],
        key=lambda ns: ns.name,
    )

    # Offset-based pagination over the full sorted list
    start = int(base64.b64decode(next_token).decode()) if next_token else 0
    page = items[start : start + limit]

    out_token = None
    if start + limit < len(items):
        out_token = base64.b64encode(str(start + limit).encode()).decode()

    return page, out_token


def resolve_accessible_namespace_ids(
    mappings_dao: DynamoDBDAO,
    user_id: str,
    groups: list[str],
) -> set[str]:
    """Query PrincipalIndex for all namespace IDs accessible to a user.

    Reusable by other handlers that need to scope queries to visible namespaces.
    """
    principal_keys = [f"{PrincipalType.USER}::{sanitize_principal_key(user_id)}"]
    principal_keys.extend(f"{PrincipalType.GROUP}::{sanitize_principal_key(g)}" for g in groups)

    namespace_ids: set[str] = set()
    for pk in principal_keys:
        # query_all, not query: one query returns a single 1 MB page, so a
        # principal with many grants would have the namespaces on later pages
        # omitted from every caller's view of "what this user can reach".
        items = mappings_dao.query_all(
            QueryParams(
                key_condition="#pk = :pk AND begins_with(#sk, :prefix)",
                expression_values={":pk": pk, ":prefix": f"{ResourceType.NAMESPACE}::"},
                expression_names={"#pk": "principalKey", "#sk": "resourceRoleKey"},
                index_name="PrincipalIndex",
            )
        )
        for item in items:
            namespace_ids.add(item["resourceId"])

    return namespace_ids


def _to_summary(item: dict[str, Any]) -> NamespaceSummary:
    try:
        return NamespaceSummary.model_construct(
            namespace_id=item["namespaceId"],
            name=item["name"],
            display_name=item.get("displayName"),
            status=NamespaceStatus(item.get("status", "ACTIVE")),
            source_count=item.get("sourceCount", 0),
            created_at=item.get("createdAt"),
            updated_at=item.get("updatedAt"),
        )
    except KeyError as exc:
        logger.warning("missing_required_field", field=str(exc), item_pk=item.get("PK"))
        raise
