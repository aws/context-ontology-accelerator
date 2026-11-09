# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cedar policy evaluator for the Context Ontology Accelerator (COA).

At runtime, policies are loaded from DynamoDB (role policies for the
principal's resolved roles + default policy). The `evaluate()` function
requires policies as input — it does not load from disk.

`load_seed_policies()` is used for:
  - Unit tests (verify policy logic before deploy)
  - DDB seeding (write built-in role policies at deploy time)
"""

import logging
from pathlib import Path

from cedarpy import AuthzResult, is_authorized, is_authorized_batch

from .cedar_schema import SCL_SCHEMA

logger = logging.getLogger(__name__)

_SEED_DIR = Path(__file__).parent / "seed"


def load_seed_policies() -> str:
    """Load all seed .cedar files (for testing and DDB seeding)."""
    policies: list[str] = []
    for f in sorted(_SEED_DIR.glob("*.cedar")):
        policies.append(f.read_text())
    return "\n".join(policies)


def load_seed_policy(name: str) -> str:
    """Load a single seed policy by name (e.g., 'global_admin')."""
    return (_SEED_DIR / f"{name}.cedar").read_text()


def build_user_entity(
    user_id: str,
    global_roles: list[str] | None = None,
    resource_roles: list[dict[str, str]] | None = None,
) -> dict:
    """Build a Cedar User entity."""
    return {
        "uid": {"type": f"{SCL_SCHEMA}::User", "id": user_id},
        "attrs": {
            "userId": user_id,
            "groups": [],
            "globalRoles": global_roles or [],
            "resourceRoles": resource_roles or [],
        },
        "parents": [],
    }


def build_resource_entity(
    resource_type: str,
    resource_id: str,
    attrs: dict | None = None,
) -> dict:
    """Build a Cedar resource entity."""
    entity_attrs = {"id": resource_id}
    if attrs:
        entity_attrs.update(attrs)
    return {
        "uid": {"type": f"{SCL_SCHEMA}::{resource_type}", "id": resource_id},
        "attrs": entity_attrs,
        "parents": [],
    }


def evaluate(
    policies: str,
    user_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    global_roles: list[str] | None = None,
    resource_roles: list[dict[str, str]] | None = None,
    resource_attrs: dict | None = None,
    context: dict | None = None,
) -> AuthzResult:
    """Evaluate a Cedar authorization request.

    Constructs Cedar principal/action/resource references, builds entity
    objects, and delegates to ``cedarpy.is_authorized()``.  On any exception
    (malformed policy, entity parsing error, etc.) returns an explicit deny
    to ensure fail-closed behavior.

    Args:
        policies: Concatenated Cedar policy text (from DDB role policies).
        user_id: The principal's user ID.
        action: Cedar action name (e.g., "viewNamespace").
        resource_type: Cedar entity type (e.g., "Namespace").
        resource_id: Resource identifier.
        global_roles: User's resolved global roles (e.g., ["ADMIN"]).
        resource_roles: User's resource-scoped role mappings, each a dict
            with keys ``role`` and ``resourceUID``.
        resource_attrs: Additional attributes on the resource entity
            (e.g., {"namespace": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"} for namespace-scoped checks).
        context: Request-time context facts passed to Cedar as the request
            ``context`` record (e.g., {"namespaceStatus": "ARCHIVED"} so the
            archived-namespace guard can forbid mutating actions).

    Returns:
        AuthzResult: ``result.allowed`` is True if access is granted.
            ``result.diagnostics`` contains Cedar evaluation details.
            On evaluation error, returns ``AuthzResult(allowed=False)``.

    Example::

        from coa_authorization.policy_evaluator import evaluate, load_seed_policies

        result = evaluate(
            policies=load_seed_policies(),
            user_id="user-42",
            action="viewNamespace",
            resource_type="Namespace",
            resource_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            global_roles=["VIEWER"],
        )
        if not result.allowed:
            raise PermissionError("Access denied")
    """
    try:
        user = build_user_entity(user_id, global_roles, resource_roles)
        resource = build_resource_entity(resource_type, resource_id, resource_attrs)

        request = {
            "principal": f'{SCL_SCHEMA}::User::"{user_id}"',
            "action": f'{SCL_SCHEMA}::Action::"{action}"',
            "resource": f'{SCL_SCHEMA}::{resource_type}::"{resource_id}"',
            "context": context or {},
        }

        return is_authorized(request, policies, [user, resource])
    except Exception as e:
        logger.error(f"Authorization evaluation failed: {e}")
        return _deny()


def _deny() -> AuthzResult:
    return AuthzResult({"decision": "Deny", "diagnostics": {"reason": [], "errors": []}})


def evaluate_batch(
    policies: str,
    user_id: str,
    checks: list[tuple[str, str, str]],
    global_roles: list[str] | None = None,
    resource_roles: list[dict[str, str]] | None = None,
) -> list[AuthzResult]:
    """Evaluate several ``(action, resource_type, resource_id)`` checks in one Cedar call.

    The policy text is parsed once for the whole batch (``cedarpy.is_authorized_batch``).
    Each resource entity carries ``namespace = resource_id``, the convention every
    namespace-scoped caller of :func:`evaluate` already uses. Results are in the
    order of ``checks``. On any exception every result is an explicit deny
    (fail closed), matching :func:`evaluate`.
    """
    if not checks:
        return []
    try:
        user = build_user_entity(user_id, global_roles, resource_roles)
        entities: dict[tuple[str, str], dict] = {}
        requests = []
        for action, resource_type, resource_id in checks:
            entities.setdefault(
                (resource_type, resource_id),
                build_resource_entity(resource_type, resource_id, {"namespace": resource_id}),
            )
            requests.append(
                {
                    "principal": f'{SCL_SCHEMA}::User::"{user_id}"',
                    "action": f'{SCL_SCHEMA}::Action::"{action}"',
                    "resource": f'{SCL_SCHEMA}::{resource_type}::"{resource_id}"',
                    "context": {},
                }
            )
        results = is_authorized_batch(requests, policies, [user, *entities.values()])
        if len(results) != len(checks):
            raise ValueError("Cedar batch returned a result count that does not match the request count")
        return list(results)
    except Exception as e:
        logger.error(f"Batch authorization evaluation failed: {e}")
        return [_deny() for _ in checks]
