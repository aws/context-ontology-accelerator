# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve principal identity, roles, and data-access restrictions from DynamoDB.

Mirrors the control-plane authorizer's role resolution logic: queries the
ResourceRoleMappings PrincipalIndex GSI for User:: and Group:: keys,
returning globalRoles, resourceRoles, and the merged table/column restrictions
for a specific namespace.

Used by the invoke() entrypoint to populate
the profile before orchestration. Keeps resolution logic in one place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import structlog
from coa_common import resolve_region, sanitize_principal_key
from coa_common.dao import DynamoDBDAO, QueryParams

logger = structlog.get_logger(__name__)

_RRM_TABLE = os.environ.get("RRM_TABLE_NAME", "")
_AWS_REGION = resolve_region()


@dataclass
class ResolvedProfile:
    """Resolved identity, roles, and data-access restrictions for a principal."""

    user_id: str
    groups: list[str] = field(default_factory=list)
    global_roles: list[str] = field(default_factory=list)
    resource_roles: list[dict[str, str]] = field(default_factory=list)
    table_allowlist: list[str] | None = None
    column_denylist: dict[str, list[str]] | None = None
    allowed_metrics: list[str] | None = None

    def inject_into(self, profile: dict[str, Any]) -> None:
        """Inject resolved fields into a mutable profile dict."""
        profile["userId"] = self.user_id
        if self.groups:
            profile["groups"] = self.groups
        if self.global_roles:
            profile["globalRoles"] = self.global_roles
        if self.resource_roles:
            profile["resourceRoles"] = self.resource_roles
        if self.table_allowlist is not None:
            profile["tableAllowlist"] = self.table_allowlist
        if self.column_denylist:
            profile["columnDenylist"] = self.column_denylist
        if self.allowed_metrics is not None:
            profile["allowedMetrics"] = self.allowed_metrics


def resolve_profile(
    user_id: str,
    groups: list[str],
    namespace: str = "",
) -> ResolvedProfile:
    """Resolve roles and data-access restrictions for a principal.

    Queries the PrincipalIndex GSI for User::<email> and Group::<group> keys.
    For the target namespace, merges tableAllowlist (union) and columnDenylist
    (intersection — a column is denied only if ALL matching grants deny it).

    If no namespace is provided, only roles are resolved (no table/column merge).
    If RRM_TABLE_NAME is not configured, returns a minimal profile with just userId/groups.
    """
    result = ResolvedProfile(user_id=user_id, groups=groups)

    if not _RRM_TABLE:
        return result

    dao = DynamoDBDAO(_RRM_TABLE, region=_AWS_REGION)

    principal_keys = [f"User::{sanitize_principal_key(user_id)}"]
    principal_keys.extend(f"Group::{sanitize_principal_key(g)}" for g in groups)

    # Collect all grant items for this principal
    all_items: list[dict[str, Any]] = []
    for pk in principal_keys:
        try:
            query_result = dao.query(
                QueryParams(
                    key_condition="#pk = :pk",
                    expression_values={":pk": pk},
                    expression_names={"#pk": "principalKey"},
                    index_name="PrincipalIndex",
                )
            )
            all_items.extend(query_result.items)
        except Exception:
            logger.exception("role_resolve_query_failed", principal_key=pk)

    # Resolve roles from all items
    for item in all_items:
        role = item.get("role", "")
        resource_id = item.get("resourceId", "")
        if resource_id == "GLOBAL" and role:
            result.global_roles.append(role)
        elif resource_id and role:
            result.resource_roles.append({"role": role, "resourceUID": resource_id})

    # Resolve table/column restrictions for the target namespace
    if namespace:
        _merge_data_restrictions(result, all_items, namespace)

    return result


def _merge_data_restrictions(
    result: ResolvedProfile,
    items: list[dict[str, Any]],
    namespace: str,
) -> None:
    """Merge tableAllowlist/columnDenylist from grants matching the namespace.

    Merge strategy:
    - tableAllowlist: UNION across all matching grants (most permissive).
      If ANY grant has no allowlist, the user has unrestricted table access.
    - columnDenylist: INTERSECTION across all matching grants (most permissive).
      A column is denied only if ALL matching grants deny it.

    Scoped to grants on THIS namespace. A ``GLOBAL``-scoped grant (the shape
    platform-admin / platform-viewer are written with) must NOT participate:
    platform grants carry no tableAllowlist/columnDenylist fields, so the
    "any grant without a restriction means unrestricted" rule above would fire
    on them and erase the namespace grant's restrictions entirely — a user who
    holds both a restricted namespace role and a platform role would get
    unrestricted data access, and the SQL firewall would log the query as
    ``restricted=False`` (no trace of a bypass). Platform privileges still flow
    through ``result.global_roles``, which is resolved separately in
    ``resolve_profile`` and drives the Cedar action-level layer; only the
    data-level merge is namespace-scoped.
    """
    namespace_grants = [item for item in items if item.get("resourceId") == namespace]

    if not namespace_grants:
        return

    # Table allowlist: union (None = no restriction on that grant)
    has_unrestricted_grant = False
    merged_tables: set[str] = set()
    for item in namespace_grants:
        allowlist = item.get("tableAllowlist")
        if allowlist is None:
            has_unrestricted_grant = True
            break
        if isinstance(allowlist, list):
            merged_tables.update(allowlist)

    if not has_unrestricted_grant and merged_tables:
        result.table_allowlist = sorted(merged_tables)

    # Allowed metrics: union (None = no restriction on that grant)
    has_unrestricted_metrics = False
    merged_metrics: set[str] = set()
    for item in namespace_grants:
        metrics = item.get("allowedMetrics")
        if metrics is None:
            has_unrestricted_metrics = True
            break
        if isinstance(metrics, list):
            merged_metrics.update(metrics)

    if not has_unrestricted_metrics and merged_metrics:
        result.allowed_metrics = sorted(merged_metrics)

    # Column denylist: intersection (only deny if ALL grants deny it)
    denylist_sets: list[dict[str, set[str]]] = []
    for item in namespace_grants:
        denylist = item.get("columnDenylist")
        if denylist is None:
            # Grant has no column restrictions → nothing is denied
            return
        if isinstance(denylist, dict):
            denylist_sets.append({t: set(cols) for t, cols in denylist.items() if isinstance(cols, list)})

    if not denylist_sets:
        return

    # Intersect: a column is denied only if present in ALL grants' denylists
    all_tables = set()
    for d in denylist_sets:
        all_tables.update(d.keys())

    merged_denylist: dict[str, list[str]] = {}
    for table in all_tables:
        # Start with the first grant's denied columns for this table
        cols = None
        for d in denylist_sets:
            if table in d:
                if cols is None:
                    cols = set(d[table])
                else:
                    cols &= d[table]
            else:
                # This grant doesn't restrict this table → nothing denied
                cols = set()
                break
        if cols:
            merged_denylist[table] = sorted(cols)

    if merged_denylist:
        result.column_denylist = merged_denylist
