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
from coa_common import build_principal_keys, resolve_region, sync_boto_config
from coa_common.dao import DynamoDBDAO, QueryParams

# Re-export for callers that historically imported this from coa_serve. The
# canonical location is ``coa_common.principal_keys``; keeping the alias here
# avoids churn in packages that already ``from coa_serve.role_resolver import
# build_principal_keys``.
__all__ = ["ResolvedProfile", "build_principal_keys", "resolve_profile"]

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
    # JWT ``email`` claim, empty when the token has none. Carried for DISPLAY
    # only (the rationale panel's Principal row) — ``user_id`` stays the key for
    # grant lookup, Cedar, and session tracking, since it is the one claim every
    # IdP guarantees. Injected from the validated token, which also overwrites
    # any client-supplied ``profile["email"]``.
    email: str = ""

    def inject_into(self, profile: dict[str, Any]) -> None:
        """Inject resolved fields into a mutable profile dict."""
        profile["userId"] = self.user_id
        if self.email:
            profile["email"] = self.email
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
    email: str = "",
) -> ResolvedProfile:
    """Resolve roles and data-access restrictions for a principal.

    Queries the PrincipalIndex GSI for User::<sub>, User::<email>, and
    Group::<group> keys. Querying both sub and email handles the case where
    grants were written against the user's email address (e.g. by namespace
    creation or manual bootstrap) while the JWT sub is a Cognito UUID.

    For the target namespace, merges tableAllowlist (union) and columnDenylist
    (intersection — a column is denied only if ALL matching grants deny it).

    If no namespace is provided, only roles are resolved (no table/column merge).
    If RRM_TABLE_NAME is not configured, returns a minimal profile with just userId/groups.

    Raises:
        Exception: Propagates a grant-lookup failure. Resolution must not fall
            back to a partial read — restrictions derived from a subset of the
            grants are more permissive than the truth, so a transient DynamoDB
            error would widen data access.
    """
    result = ResolvedProfile(user_id=user_id, groups=groups, email=email)

    if not _RRM_TABLE:
        return result

    # Authorization is on the synchronous serve invoke() request path. Opt into
    # the fail-fast profile instead of inheriting the DAO's background
    # 30-second/three-attempt default (matches the Cedar policy loader's #1092 fix).
    dao = DynamoDBDAO(_RRM_TABLE, region=_AWS_REGION, config=sync_boto_config())

    principal_keys = build_principal_keys(user_id=user_id, email=email, groups=groups)

    # Collect all grant items for this principal.
    #
    # query_all, not query: one query returns a single 1 MB page, and the grants
    # on later pages carry tableAllowlist / columnDenylist. Dropping a restricted
    # grant while an unrestricted one survives makes _merge_data_restrictions see
    # no restrictions at all, so the SQL firewall stops enforcing them — a PII
    # denylist silently disabled by a pagination boundary.
    #
    # A failure here must NOT degrade to a partial read for the same reason: the
    # restrictions computed from a subset are more permissive than the truth. Fail
    # the resolution instead and let the caller reject the request, matching the
    # control-plane authorizer's fail-closed posture (it re-raises in the
    # equivalent spot).
    all_items: list[dict[str, Any]] = []
    for pk in principal_keys:
        try:
            all_items.extend(
                dao.query_all(
                    QueryParams(
                        key_condition="#pk = :pk",
                        expression_values={":pk": pk},
                        expression_names={"#pk": "principalKey"},
                        index_name="PrincipalIndex",
                    )
                )
            )
        except Exception:
            logger.exception("role_resolve_query_failed", principal_key=pk)
            raise

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


@dataclass
class _Restrictions:
    """Effective data restrictions for one principal in one namespace."""

    table_allowlist: list[str] | None = None
    column_denylist: dict[str, list[str]] | None = None
    allowed_metrics: list[str] | None = None


def _merge_data_restrictions(
    result: ResolvedProfile,
    items: list[dict[str, Any]],
    namespace: str,
) -> None:
    """Compute the effective data restrictions for ``namespace`` onto ``result``.

    Scoped to grants on THIS namespace. A ``GLOBAL``-scoped grant (the shape
    platform-admin / platform-viewer are written with) does NOT participate:
    platform privileges flow through ``result.global_roles``, which is resolved
    separately and drives the Cedar action-level layer. Only the data-level merge
    is namespace-scoped, and no role bypasses it.

    Restrictions ACCUMULATE and never dissolve — see :func:`_restrictive_merge`.
    """
    namespace_grants = [item for item in items if item.get("resourceId") == namespace]
    if not namespace_grants:
        return

    effective = _restrictive_merge(namespace_grants)
    result.table_allowlist = effective.table_allowlist
    result.column_denylist = effective.column_denylist
    result.allowed_metrics = effective.allowed_metrics


def _restrictive_merge(grants: list[dict[str, Any]]) -> _Restrictions:
    """Deny-wins merge: restrictions accumulate across the namespace's grants.

    - ``columnDenylist`` — UNION. A column is denied if ANY grant denies it.
    - ``tableAllowlist`` / ``allowedMetrics`` — UNION of the grants that DECLARE
      one; a grant that declares none contributes no constraint rather than
      removing every constraint. Access is unrestricted only when NO grant
      declares a list.

    Why not the other way round. Permissions compose additively (Cedar does that,
    correctly — holding two roles grants the union of their actions), but
    restrictions are constraints and compose restrictively. Applying permission
    semantics to constraints is what let one unrestricted grant on a namespace
    erase a PII denylist attached to another: adding ``namespace-owner`` to an
    analyst silently stopped their ``columnDenylist`` from being enforced. Every
    mature system draws this line the same way — an explicit Deny beats any Allow
    in IAM, ``forbid`` beats ``permit`` in Cedar, and ``default.cedar`` in this repo
    already states that its ACTIVE-only invariant is "not a role-overridable
    default" even for platform-admin.

    Union (not intersection) for the allowlists is deliberate: intersecting a grant
    allowing ``[a]`` with one allowing ``[b]`` would deny both, when the principal
    was plainly granted each. An explicitly EMPTY declared allowlist is honoured as
    "no tables permitted" — that is an explicit administrative choice, unlike
    absence.
    """
    denied: dict[str, set[str]] = {}
    for grant in grants:
        denylist = grant.get("columnDenylist")
        if not isinstance(denylist, dict):
            continue
        for table, cols in denylist.items():
            if isinstance(cols, list):
                denied.setdefault(table, set()).update(cols)

    return _Restrictions(
        table_allowlist=_union_of_declared(grants, "tableAllowlist"),
        column_denylist={t: sorted(c) for t, c in sorted(denied.items()) if c} or None,
        allowed_metrics=_union_of_declared(grants, "allowedMetrics"),
    )


def _union_of_declared(grants: list[dict[str, Any]], field_name: str) -> list[str] | None:
    """Union of ``field_name`` across grants that declare it; ``None`` if none do."""
    declared = False
    merged: set[str] = set()
    for grant in grants:
        value = grant.get(field_name)
        if isinstance(value, list):
            declared = True
            merged.update(value)
    return sorted(merged) if declared else None
