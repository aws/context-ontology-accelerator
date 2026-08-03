# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grant resolution — resolve user identity to InvokeRequest.profile.

This is the critical data-scoping component. Without a populated profile,
the SQL Firewall no-ops (empty profile = unrestricted access).

An empty/missing authorization object must FAIL CLOSED,
never default-allow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import structlog
from coa_authorization.policy_evaluator import evaluate as cedar_evaluate
from coa_common import resolve_region

from .claims import CallerIdentity

logger = structlog.get_logger(__name__)

_ROLES_TABLE = os.environ.get("ROLES_TABLE_NAME", "")
_AWS_REGION = resolve_region()


class GrantResolutionError(Exception):
    """Raised when a user's grant cannot be resolved."""


# ── MCP tool → Cedar action mapping ──────────────────────────────────────────

TOOL_CEDAR_ACTION: dict[str, str] = {
    "list_metrics": "viewNamespace",
    "describe_schema": "viewNamespace",
    "query": "query",
    "translate_sparql": "query",
    "rag_retrieval": "searchDocuments",
    "graph_traversal": "viewNamespace",
}


def _evaluate_cedar(
    user_id: str,
    action: str,
    namespace_id: str,
    *,
    global_roles: list[str],
    resource_roles: list[dict[str, str]],
) -> None:
    """Load Cedar policies for the principal's roles and evaluate.

    Mirrors the control-plane authorizer pattern: loads cedarPolicy from the
    Roles table for each resolved role + default, then calls
    coa_authorization.policy_evaluator.evaluate().

    Raises GrantResolutionError on deny (fail-closed).
    """
    from coa_authorization.policy_loader import load_policies_for_roles

    role_ids: set[str] = {"default"}
    role_ids.update(global_roles)
    role_ids.update(r.get("role", "") for r in resource_roles if r.get("role"))

    policies = load_policies_for_roles(
        role_ids=role_ids,
        table_name=_ROLES_TABLE,
        region=_AWS_REGION,
    )

    if not policies:
        logger.warning("cedar_no_policies_loaded", user_id=user_id, roles=list(role_ids))
        raise GrantResolutionError(f"No Cedar policies found for user '{user_id}'")

    result = cedar_evaluate(
        policies=policies,
        user_id=user_id,
        action=action,
        resource_type="Namespace",
        resource_id=namespace_id,
        global_roles=global_roles,
        resource_roles=resource_roles,
        resource_attrs={"namespace": namespace_id},
    )

    if not result.allowed:
        logger.warning(
            "cedar_access_denied",
            user_id=user_id,
            action=action,
            namespace=namespace_id,
            roles=list(role_ids),
        )
        raise GrantResolutionError(
            f"Cedar policy denied action '{action}' for user '{user_id}' on namespace '{namespace_id}'"
        )


@dataclass
class ResolvedProfile:
    """The resolved data-access profile for a user in a namespace.

    This populates InvokeRequest.profile, which the SQL Firewall uses
    for table allowlist and column denylist enforcement, and the Cedar
    authorizer uses for role-based policy evaluation.
    """

    namespace: str
    user_id: str = ""
    global_roles: list[str] = field(default_factory=list)
    resource_roles: list[dict[str, str]] = field(default_factory=list)
    table_allowlist: list[str] = field(default_factory=list)
    column_denylist: dict[str, list[str]] = field(default_factory=dict)
    data_source_ids: list[str] = field(default_factory=list)

    def to_profile_dict(self) -> dict:
        """Convert to the dict format expected by InvokeRequest.profile."""
        return {
            "namespace": self.namespace,
            "userId": self.user_id,
            "globalRoles": self.global_roles,
            "resourceRoles": self.resource_roles,
            "tableAllowlist": self.table_allowlist,
            "columnDenylist": self.column_denylist,
            "dataSourceIds": self.data_source_ids,
        }


async def resolve_grant(
    caller: CallerIdentity,
    namespace_id: str,
    *,
    cedar_action: str | None = None,
) -> ResolvedProfile:
    """Resolve the caller's grant for the given namespace.

    This function:
    1. Validates the caller has access to the requested namespace
    2. Evaluates Cedar policies if cedar_action is provided (fail-closed)
    3. Resolves their data-access profile (table allowlist, column denylist)
       via the shared role_resolver from coa_serve.

    FAIL CLOSED: If the grant cannot be resolved, access is DENIED.

    Args:
        caller: The extracted caller identity from JWT claims.
        namespace_id: The namespace being accessed.
        cedar_action: Cedar action to evaluate (e.g., "query", "viewNamespace").
            If None, Cedar evaluation is skipped (backward compat).

    Returns:
        ResolvedProfile with the caller's data-access permissions.

    Raises:
        GrantResolutionError: If the caller lacks access or grant cannot be resolved.
    """
    # ── Namespace access check ────────────────────────────────────────
    if caller.namespaces and namespace_id not in caller.namespaces:
        logger.warning(
            "namespace_access_denied",
            user_id=caller.user_id,
            agent_id=caller.agent_id,
            requested_namespace=namespace_id,
            # Count, not the full entitlement list — the list exposes the
            # caller's entire authorization perimeter to log readers.
            allowed_namespace_count=len(caller.namespaces),
        )
        raise GrantResolutionError(f"User '{caller.user_id}' does not have access to namespace '{namespace_id}'")

    # Fail closed: if namespaces list is empty and caller is not an admin,
    # deny access.
    if not caller.namespaces and "admin" not in [r.lower() for r in caller.roles]:
        logger.warning(
            "namespace_access_denied_empty",
            user_id=caller.user_id,
            agent_id=caller.agent_id,
            requested_namespace=namespace_id,
            roles=caller.roles,
        )
        raise GrantResolutionError(f"User '{caller.user_id}' has no namespace grants and is not an admin")

    # ── Resolve data-access profile via shared role resolver ──────────
    try:
        from coa_serve.role_resolver import resolve_profile

        resolved = resolve_profile(
            user_id=caller.user_id,
            groups=caller.roles,
            namespace=namespace_id,
        )
    except Exception as e:
        # Fail closed for EVERY caller, admins included.
        #
        # There used to be an admin fallback here that returned platform-admin +
        # namespace-owner and returned EARLY — before the Cedar evaluation below,
        # despite that block's comment claiming it is never bypassed. It granted
        # those roles off nothing but an "admin" entry in the caller's IdP groups,
        # with no policy check and no data restrictions.
        #
        # It was near-unreachable while resolve_profile swallowed its own query
        # errors, but that method now propagates them (a partial grant read is more
        # permissive than the truth, so degrading there widened data access). A
        # DynamoDB throttle would therefore have escalated any admin-group caller.
        #
        # A failed grant read means we do not know what this caller may do, which is
        # never a reason to assume the most privileged answer. Denying turns a
        # transient backend error into a retryable failure instead of a silent
        # privilege escalation.
        logger.error(
            "grant_resolution_failed",
            user_id=caller.user_id,
            namespace=namespace_id,
            is_admin_group_member=any(r.lower() == "admin" for r in caller.roles),
            error=str(e),
        )
        raise GrantResolutionError(f"Failed to resolve grant for user '{caller.user_id}': {e}") from e

    # ── Cedar policy evaluation (OUTSIDE try/except, never bypassed) ──
    if cedar_action and _ROLES_TABLE:
        _evaluate_cedar(
            caller.user_id,
            cedar_action,
            namespace_id,
            global_roles=resolved.global_roles,
            resource_roles=resolved.resource_roles,
        )

    logger.info(
        "grant_resolved",
        user_id=caller.user_id,
        namespace=namespace_id,
        global_roles=resolved.global_roles,
        table_allowlist_count=len(resolved.table_allowlist or []),
        column_denylist_count=len(resolved.column_denylist or {}),
        data_source_ids_count=len(resolved.allowed_metrics or []),
    )

    return ResolvedProfile(
        namespace=namespace_id,
        user_id=caller.user_id,
        global_roles=resolved.global_roles,
        resource_roles=resolved.resource_roles,
        table_allowlist=resolved.table_allowlist or [],
        column_denylist=resolved.column_denylist or {},
        data_source_ids=resolved.allowed_metrics or [],
    )
