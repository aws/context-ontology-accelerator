# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Runtime enforcement of the grant.smithy request bounds.

The grants handlers parse the raw JSON body and query string rather than the
generated request models, so the ``@length`` / ``@range`` traits declared in
``models/src/main/smithy/grant.smithy`` are enforced here. Every constant is
pinned to its trait by ``tests/unit/test_contract_bounds.py``.

Error messages name the field and the bound, never the offending value.
"""

from __future__ import annotations

from typing import Any

from coa_common.authnz_types import PrincipalType
from coa_common.constants import PAGINATION_TOKEN_MAX_LENGTH, validate_principal_id

# CreateGrantRequest / CreatePlatformGrantRequest
ROLE_MAX_LENGTH = 128
# tableAllowlist / allowedMetrics / columnDenylist: @length(max: 1000)
OVERRIDE_MAX_ITEMS = 1000
# TableAllowlist member
TABLE_NAME_MAX_LENGTH = 1024
# MetricsList member
METRIC_NAME_MAX_LENGTH = 256
# ColumnDenylist key / value, ColumnList member
COLUMN_DENYLIST_KEY_MAX_LENGTH = 1024
COLUMN_LIST_MAX_ITEMS = 1000
COLUMN_NAME_MAX_LENGTH = 1024
# ListNamespaceGrants / ListPlatformGrants ``principal`` query filter
PRINCIPAL_FILTER_MAX_LENGTH = 1024

_VALID_PRINCIPAL_TYPES = frozenset(e.value for e in PrincipalType)
_PRINCIPAL_TYPE_ERROR = "principalType must be one of: " + ", ".join(sorted(_VALID_PRINCIPAL_TYPES))


# DynamoDB key-attribute size limits (bytes, UTF-8). A grant item's keys embed the
# percent-encoded principal, so a long non-ASCII principalId can pass its 256-char
# bound yet exceed these once encoded; the write would then fail as a 500.
_PARTITION_KEY_ATTRS = ("PK", "principalKey", "namespaceKey")
_SORT_KEY_ATTRS = ("SK", "resourceRoleKey", "principalRoleKey")
PARTITION_KEY_MAX_BYTES = 2048
SORT_KEY_MAX_BYTES = 1024
_KEY_TOO_LONG_ERROR = "principalId is too long to store as a grant; use a shorter principal identifier"


# The grant id travels in the DeleteGrant / DeletePlatformGrant request path, which
# the API's web ACL caps at 1,024 bytes (stage + "/namespaces/<uuid>/grants/" is
# about 60 of them). A grant whose id would not fit could be created but never
# revoked through the API, so refuse it at create time. Below GrantId's 1,024 max.
GRANT_ID_CREATE_MAX_LENGTH = 900


def validate_grant_item_keys(item: dict[str, Any], grant_id: str) -> str | None:
    """Error message if a grant item could not be stored or later revoked.

    Each key attribute must fit DynamoDB's size limits, and the grant id must fit
    the revoke request path (``GRANT_ID_CREATE_MAX_LENGTH``).
    """
    for attrs, limit in ((_PARTITION_KEY_ATTRS, PARTITION_KEY_MAX_BYTES), (_SORT_KEY_ATTRS, SORT_KEY_MAX_BYTES)):
        for attr in attrs:
            value = item.get(attr)
            if isinstance(value, str) and len(value.encode("utf-8")) > limit:
                return _KEY_TOO_LONG_ERROR
    if len(grant_id) > GRANT_ID_CREATE_MAX_LENGTH:
        return _KEY_TOO_LONG_ERROR
    return None


def validate_principal_binding(body: dict[str, Any]) -> str | None:
    """Validate ``principalType`` / ``principalId`` / ``role``; None when valid."""
    principal_type = body.get("principalType")
    principal_id = body.get("principalId")
    role = body.get("role")

    if not principal_type or not principal_id or not role:
        return "principalType, principalId, and role are required"
    if not isinstance(principal_type, str) or principal_type not in _VALID_PRINCIPAL_TYPES:
        return _PRINCIPAL_TYPE_ERROR
    try:
        validate_principal_id(principal_id)
    except ValueError as exc:
        return str(exc)
    if not isinstance(role, str) or len(role) > ROLE_MAX_LENGTH:
        return f"role must be a string of 1-{ROLE_MAX_LENGTH} characters"
    return None


def _string_list_error(field: str, value: Any, *, max_items: int, max_member_length: int, noun: str) -> str | None:
    if not isinstance(value, list):
        return f"{field} must be a list of {noun}"
    if len(value) > max_items:
        return f"{field} may contain at most {max_items} entries"
    if not all(isinstance(v, str) for v in value):
        return f"{field} must contain only strings"
    if any(len(v) > max_member_length for v in value):
        return f"{field} entries must be at most {max_member_length} characters"
    return None


def validate_grant_overrides(body: dict[str, Any]) -> str | None:
    """Validate the optional data-access overrides on a CreateGrant body; None when valid.

    ``tableAllowlist`` and ``allowedMetrics`` are ``list[str]``;
    ``columnDenylist`` is ``dict[str, list[str]]``. These values reach the SQL
    firewall through the principal's resolved profile, so the shape and size
    are enforced at write time.
    """
    table_allowlist = body.get("tableAllowlist")
    if table_allowlist is not None:
        error = _string_list_error(
            "tableAllowlist",
            table_allowlist,
            max_items=OVERRIDE_MAX_ITEMS,
            max_member_length=TABLE_NAME_MAX_LENGTH,
            noun="strings",
        )
        if error:
            return error

    allowed_metrics = body.get("allowedMetrics")
    if allowed_metrics is not None:
        error = _string_list_error(
            "allowedMetrics",
            allowed_metrics,
            max_items=OVERRIDE_MAX_ITEMS,
            max_member_length=METRIC_NAME_MAX_LENGTH,
            noun="strings",
        )
        if error:
            return error

    column_denylist = body.get("columnDenylist")
    if column_denylist is not None:
        if not isinstance(column_denylist, dict):
            return "columnDenylist must be a mapping of table name to list of column names"
        if len(column_denylist) > OVERRIDE_MAX_ITEMS:
            return f"columnDenylist may contain at most {OVERRIDE_MAX_ITEMS} tables"
        for table, columns in column_denylist.items():
            # JSON object keys are always strings; the check guards direct callers.
            if not isinstance(table, str):
                return "columnDenylist keys must be strings"
            if len(table) > COLUMN_DENYLIST_KEY_MAX_LENGTH:
                return f"columnDenylist table names must be at most {COLUMN_DENYLIST_KEY_MAX_LENGTH} characters"
            error = _string_list_error(
                "columnDenylist values",
                columns,
                max_items=COLUMN_LIST_MAX_ITEMS,
                max_member_length=COLUMN_NAME_MAX_LENGTH,
                noun="column names",
            )
            if error:
                return error

    return None


def validate_list_query(principal: Any, next_token: Any) -> str | None:
    """Validate the ``principal`` filter and ``nextToken`` lengths of a grants list call."""
    if principal is not None and len(principal) > PRINCIPAL_FILTER_MAX_LENGTH:
        return f"principal must be at most {PRINCIPAL_FILTER_MAX_LENGTH} characters"
    if next_token is not None and len(next_token) > PAGINATION_TOKEN_MAX_LENGTH:
        return "Invalid nextToken"
    return None
