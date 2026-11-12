# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grant resolution — resolve user identity to InvokeRequest.profile.

This is the critical data-scoping component. Without a populated profile,
the SQL Firewall no-ops (empty profile = unrestricted access).

An empty/missing authorization object must FAIL CLOSED,
never default-allow.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

import structlog
from coa_authorization.policy_evaluator import evaluate as cedar_evaluate
from coa_authorization.schema_visibility import VIEW_ONTOLOGY_ACTION, VIEW_SOURCE_ACTION
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
    # describe_schema is a pure ontology-vocabulary dump (classes/properties)
    # with no query utility, so it is HARD-denied to a schema-blind role via
    # viewOntology. translate_sparql/graph_traversal remain gated on their
    # working-tool actions (query / traverseGraph) — a schema-blind analyst
    # KEEPS these tools, and their raw ontology/schema
    # identifiers are removed by the output-suppression layer, not by denying
    # the action.
    "describe_schema": VIEW_ONTOLOGY_ACTION,
    "query": "query",
    "translate_sparql": "query",
    "rag_retrieval": "searchDocuments",
    "graph_traversal": "traverseGraph",
}


def _evaluate_cedar(
    user_id: str,
    action: str,
    namespace_id: str,
    *,
    global_roles: list[str],
    resource_roles: list[dict[str, str]],
) -> str:
    """Load Cedar policies for the principal's roles and evaluate ``action``.

    Raises GrantResolutionError on deny (fail-closed). Returns the loaded
    policies so the caller can evaluate further actions without reloading them.
    """
    policies, role_ids = _load_policies(global_roles, resource_roles)

    if not policies:
        logger.warning("cedar_no_policies_loaded", user_id=user_id, roles=list(role_ids))
        raise GrantResolutionError(f"No Cedar policies found for user '{user_id}'")

    if not _is_allowed(
        policies, user_id, action, namespace_id, global_roles=global_roles, resource_roles=resource_roles
    ):
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
    return policies


# Schema rights decided alongside the tool action. Cached on the resolved profile
# so the output-suppression gate and the metric-service flags never reload the policies.
SCHEMA_VIEW_ACTIONS: tuple[str, ...] = (VIEW_SOURCE_ACTION, VIEW_ONTOLOGY_ACTION)


def _load_policies(global_roles: list[str], resource_roles: list[dict[str, str]]) -> tuple[str, set[str]]:
    """Load the Cedar policy text for the principal's roles (plus ``default``) once."""
    from coa_authorization.policy_loader import load_policies_for_roles

    role_ids: set[str] = {"default"}
    role_ids.update(global_roles)
    role_ids.update(r.get("role", "") for r in resource_roles if r.get("role"))
    policies = load_policies_for_roles(role_ids=role_ids, table_name=_ROLES_TABLE, region=_AWS_REGION)
    return policies, role_ids


def _is_allowed(
    policies: str,
    user_id: str,
    action: str,
    namespace_id: str,
    *,
    global_roles: list[str],
    resource_roles: list[dict[str, str]],
) -> bool:
    """Evaluate ``action`` on the namespace against already-loaded policies."""
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
    return bool(result.allowed)


def _schema_visibility(
    policies: str,
    user_id: str,
    namespace_id: str,
    *,
    global_roles: list[str],
    resource_roles: list[dict[str, str]],
) -> dict[str, bool]:
    """``viewSource`` / ``viewOntology`` decisions from the same policy set, fail-closed.

    An evaluation error yields False for that action (schema withheld) and is
    logged at error level, so an outage is not mistaken for a real deny.
    """
    decisions: dict[str, bool] = {}
    for action in SCHEMA_VIEW_ACTIONS:
        try:
            decisions[action] = _is_allowed(
                policies, user_id, action, namespace_id, global_roles=global_roles, resource_roles=resource_roles
            )
        except Exception as exc:  # fail closed — withhold schema on any error
            logger.error(
                "cedar_schema_view_error",
                user_id=user_id,
                action=action,
                namespace=namespace_id,
                error=type(exc).__name__,
                error_msg=str(exc),
                exc_info=True,
            )
            decisions[action] = False
    return decisions


def _authorize_and_decide_visibility(
    user_id: str,
    action: str,
    namespace_id: str,
    *,
    global_roles: list[str],
    resource_roles: list[dict[str, str]],
) -> dict[str, bool]:
    """One policy load: gate ``action`` (raises on deny), then decide the schema rights.

    Blocking (DynamoDB read + cedarpy), so :func:`resolve_grant` runs it off the
    event loop.
    """
    policies = _evaluate_cedar(user_id, action, namespace_id, global_roles=global_roles, resource_roles=resource_roles)
    return _schema_visibility(policies, user_id, namespace_id, global_roles=global_roles, resource_roles=resource_roles)


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
    # None = unrestricted (no grant declares a constraint). An explicitly EMPTY
    # list/map is a real restriction (deny-all) under the SQL firewall's
    # empty-vs-absent semantics, so the two must not be collapsed here.
    table_allowlist: list[str] | None = None
    column_denylist: dict[str, list[str]] | None = None
    data_source_ids: list[str] | None = None
    # viewSource / viewOntology decided with the tool action; empty when no
    # cedar_action was evaluated, which consumers treat as "withhold".
    schema_visibility: dict[str, bool] = field(default_factory=dict)

    def to_profile_dict(self) -> dict:
        """Convert to the dict format expected by InvokeRequest.profile.

        Restriction keys are omitted when unrestricted (None) rather than sent
        as empty lists: an empty tableAllowlist means deny-all to the SQL
        firewall, and the serve entrypoint strips + re-resolves these fields
        server-side anyway — forwarding fabricated empties only trips its
        client_role_injection_stripped warning on every call.
        """
        profile: dict = {
            "namespace": self.namespace,
            "userId": self.user_id,
            "globalRoles": self.global_roles,
            "resourceRoles": self.resource_roles,
            "schemaVisibility": dict(self.schema_visibility),
        }
        if self.table_allowlist is not None:
            profile["tableAllowlist"] = self.table_allowlist
        if self.column_denylist is not None:
            profile["columnDenylist"] = self.column_denylist
        if self.data_source_ids is not None:
            profile["dataSourceIds"] = self.data_source_ids
        return profile


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
    # ── Namespace pre-filter (advisory) ───────────────────────────────
    # Only enforced when the JWT carried a ``custom:namespaces`` claim; most tokens
    # never do, so an empty list is not a denial. The RRM lookup below and Cedar
    # are authoritative.
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

    # ── Resolve data-access profile via shared role resolver ──────────
    # Pass ``email``: UI-written grants are keyed on email, while ``sub`` is a UUID.
    try:
        from coa_serve.role_resolver import resolve_profile

        resolved = resolve_profile(
            user_id=caller.user_id,
            groups=caller.roles,
            namespace=namespace_id,
            email=caller.email,
        )
    except Exception as e:
        # Fail closed for EVERY caller, admins included: there is no admin
        # fallback, because an unknown grant must never default to privileged.
        logger.error(
            "grant_resolution_failed",
            user_id=caller.user_id,
            namespace=namespace_id,
            is_admin_group_member=any(r.lower() == "admin" for r in caller.roles),
            error=str(e),
        )
        raise GrantResolutionError(f"Failed to resolve grant for user '{caller.user_id}': {e}") from e

    # ── Fail closed on empty grants for THIS namespace ────────────────
    # ``resolved.resource_roles`` spans every namespace the caller holds, so filter
    # by ``resourceUID`` before the emptiness test. This is the last line of
    # defense when Cedar is skipped (``cedar_action`` None), so it must be
    # namespace-scoped. Global roles and admin IdP-group members pass by design.
    namespace_resource_roles = [r for r in resolved.resource_roles if r.get("resourceUID") == namespace_id]
    is_admin_group_member = any(r.lower() == "admin" for r in caller.roles)
    if not namespace_resource_roles and not resolved.global_roles and not is_admin_group_member:
        logger.warning(
            "grant_lookup_empty",
            user_id=caller.user_id,
            agent_id=caller.agent_id,
            requested_namespace=namespace_id,
            cross_namespace_role_count=len(resolved.resource_roles),
        )
        raise GrantResolutionError(
            f"User '{caller.user_id}' has no grants on namespace '{namespace_id}' and is not an admin"
        )

    # ── Cedar policy evaluation (OUTSIDE try/except, never bypassed) ──
    # resolve_grant is only reached on the authenticated path (local dev returns
    # before calling it), so a caller-supplied cedar_action MUST be evaluated.
    # Fail CLOSED when the roles table is unset: skipping the gate would let a
    # hard-gated tool (e.g. describe_schema -> viewOntology) run for a role that
    # should be denied, and the schema-view decisions below (taken from the same
    # policy set) would have no policy source either.
    if cedar_action:
        if not _ROLES_TABLE:
            # The detail (env var name) stays in the log; the client gets a
            # message it can act on, not an internal deployment detail.
            logger.error(
                "cedar_gate_no_roles_table",
                user_id=caller.user_id,
                action=cedar_action,
                detail="Cedar evaluation required but ROLES_TABLE_NAME is unset",
            )
            raise GrantResolutionError("Authorization is temporarily unavailable; contact your administrator.")
        schema_visibility = await asyncio.to_thread(
            _authorize_and_decide_visibility,
            caller.user_id,
            cedar_action,
            namespace_id,
            global_roles=resolved.global_roles,
            resource_roles=resolved.resource_roles,
        )
    else:
        schema_visibility = {}

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
        # Pass through as-is: None (unrestricted) must not become [] — an empty
        # list is a deny-all restriction under the firewall's semantics.
        table_allowlist=resolved.table_allowlist,
        column_denylist=resolved.column_denylist,
        data_source_ids=resolved.allowed_metrics,
        schema_visibility=schema_visibility,
    )
