# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cedar authorization for the SQL Firewall.

Real Cedar policy evaluation runs on the serve/query path: the
caller's profile (namespace, roles, scopes) is passed to a Cedar evaluator, and
a DENY is terminal (HTTP 403). This wires the REAL ``cedarpy``-backed engine
(the shared ``coa_authorization`` engine from libs/common — the same one the
control-plane authorizer uses) rather than a hand-rolled stand-in.

Authorization model (matches the shipped seed policies + Cedar schema):
- The serve check is the namespace-level ``SCL::Action::"query"`` on the
  ``Namespace`` resource — "may this principal query in this namespace at all?".
- The principal's roles come from the resolved auth context the caller supplies
  in ``profile`` (``globalRoles`` / ``resourceRoles`` / ``groups`` — the same
  fields the control-plane authorizer forwards). The ``namespace_data_analyst``
  / ``data_steward`` / ``owner`` seed policies permit ``query`` when the
  principal holds the matching ``resourceRole`` for that namespace.
- The firewall's table/column checks remain the fine-grained data-authz layer
  UNDER this; Cedar is the coarse admission gate ABOVE it. Cedar runs only as an
  additional deny — it never relaxes a structural firewall deny.

Environment posture (``SCL_ENVIRONMENT``), matching the agreed grant-resolver
stance: in a non-prod env, a caller with NO resolved roles is allowed through
(fail-open dev) so the deployment is usable before role provisioning; in prod,
no-roles → DENY (fail-closed). A genuine evaluator ERROR always denies, in every
environment.

- ``CedarAuthorizer`` — the protocol the firewall depends on.
- ``CedarDecision`` — allow/deny + reason.
- ``RealCedarAuthorizer`` — the default: real ``cedarpy`` evaluation vs seed policies.
- ``NullCedarAuthorizer`` — explicit opt-out (allow-all at the Cedar layer only).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

import structlog
from coa_authorization.policy_evaluator import evaluate, load_seed_policies
from coa_common import sync_boto_config
from coa_common.dao import DynamoDBDAO

logger = structlog.get_logger(__name__)

# DDB-backed policy loading (parity with the control-plane authorizer): when
# ROLES_TABLE_NAME is set, role policies come from the Roles table (`cedarPolicy`
# per role, PK GLOBAL / NAMESPACE_TEMPLATE) so namespace-custom policies apply on
# the data path too. The shipped seed policies remain the fallback when the table
# is not configured or a role has no stored policy. Cached with a TTL — the serve
# runtime is long-lived (unlike the Lambda authorizer, which leans on the API GW
# authorizer cache).
_POLICY_CACHE_TTL_S = float(os.environ.get("SCL_CEDAR_POLICY_CACHE_TTL_S", "60") or 60)


@dataclass(frozen=True)
class CedarDecision:
    """Outcome of a Cedar policy evaluation."""

    allowed: bool
    reason: str | None = None


@runtime_checkable
class CedarAuthorizer(Protocol):
    """Evaluates whether a principal may query within a namespace.

    The firewall calls this AFTER its own safety + structural authorization
    checks pass, as an additional policy gate. Implementations raise on
    evaluation failure (the firewall fails closed on exceptions).
    """

    def authorize(self, *, namespace: str, profile: dict[str, Any], tables: list[str]) -> CedarDecision:
        """Return a Cedar allow/deny decision for ``query`` in ``namespace``."""
        ...


class NullCedarAuthorizer:
    """Opt-out authorizer — allow-all at the Cedar-policy layer.

    The firewall's structural safety + allowlist/denylist checks still run and
    still bite; only the Cedar *policy* layer is a no-op. Kept as an explicit
    escape hatch (``SCL_DISABLE_CEDAR=true``); the real evaluator is the default.
    """

    def authorize(self, *, namespace: str, profile: dict[str, Any], tables: list[str]) -> CedarDecision:
        """Allow every request unconditionally at the Cedar-policy layer.

        Args:
            namespace: Namespace the query targets (logged only).
            profile: Caller's resolved auth context (ignored here).
            tables: Tables referenced by the query (logged only).

        Returns:
            A CedarDecision that always allows.
        """
        logger.debug("cedar_authorize_noop", namespace=namespace, table_count=len(tables))
        return CedarDecision(allowed=True)


# Profile keys carrying the principal's resolved auth context. These mirror what
# the control-plane authorizer resolves/forwards (see authorization/handler.py
# auth_context): the principal id, global roles, and resource-scoped roles.
_USER_ID_KEY = "userId"
_GLOBAL_ROLES_KEY = "globalRoles"
_RESOURCE_ROLES_KEY = "resourceRoles"  # list[{"role","resourceUID"}] — Cedar shape

_QUERY_ACTION = "query"
_NAMESPACE_RESOURCE = "Namespace"


@lru_cache(maxsize=1)
def _seed_policies() -> str:
    """Load + cache the shipped seed Cedar policies (read once)."""
    return load_seed_policies()


class _DdbPolicyLoader:
    """Loads role Cedar policies from the Roles DDB table with a TTL cache.

    Mirrors the control-plane authorizer's `_evaluate_cedar` loading: for each
    role id (+ "default"), try PK=GLOBAL then PK=NAMESPACE_TEMPLATE, take the
    item's ``cedarPolicy``. Reads through the shared ``DynamoDBDAO`` (the same
    DAO the control-plane authorizer uses), so the data-path and control-plane
    role lookups stay in lockstep. Failure or an empty result falls back to the
    seed policies (fail-SAFE for availability; the evaluation itself remains
    fail-closed on error in the authorizer).
    """

    def __init__(self, table_name: str, region: str):
        # Authorization is on the synchronous customer request path. Opt into
        # the fail-fast profile instead of inheriting the DAO's background
        # 30-second/three-attempt default.
        self._dao = DynamoDBDAO(table_name, region=region, config=sync_boto_config())
        self._cache: dict[frozenset[str], tuple[float, str]] = {}

    def policies_for_roles(self, role_ids: set[str]) -> str | None:
        """Return joined Cedar policies for the role set, or None to use seeds."""
        key = frozenset(role_ids | {"default"})
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < _POLICY_CACHE_TTL_S:
            return hit[1] or None
        texts: list[str] = []
        try:
            for role_id in sorted(key):
                for pk in ("GLOBAL", "NAMESPACE_TEMPLATE"):
                    item = self._dao.get({"PK": pk, "SK": f"ROLE#{role_id}"})
                    if item and item.get("cedarPolicy"):
                        texts.append(str(item["cedarPolicy"]))
                        break
        except Exception as exc:
            logger.warning(
                "cedar_ddb_policy_load_failed",
                error=type(exc).__name__,
                error_msg=str(exc),
                fallback="seed-policies",
            )
            return None
        joined = "\n".join(texts)
        self._cache[key] = (now, joined)
        if not texts:
            logger.warning("cedar_ddb_policies_empty", roles=sorted(key), fallback="seed-policies")
            return None
        return joined


def _as_list(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    # The authorizer forwards roles/groups as comma-joined strings; accept both.
    if isinstance(value, str) and value:
        return [v for v in value.split(",") if v]
    return []


class RealCedarAuthorizer:
    """Real ``cedarpy``-backed Cedar evaluation for the serve query path.

    Evaluates ``SCL::Action::"query"`` on the ``Namespace`` resource against the
    vendored seed policies, using the principal's resolved roles from ``profile``
    (``globalRoles`` / ``resourceRoles``). FAIL-CLOSED by default on a caller with
    no resolved roles; the no-roles bypass is an explicit opt-in
    (``SCL_CEDAR_FAIL_OPEN_NO_ROLES=true``, for dev/pre-provisioning). Any
    evaluator error denies, always.

    Roles provenance (SECURITY): the ``profile`` passed here is built by the
    serve entrypoints, which STRIP any client-supplied identity/role keys and set
    identity only from the validated token — a client cannot self-escalate by
    putting ``globalRoles`` in the request body (see main.py identity resolution).

    NOTE: the seed policies grant ``query`` on ``resourceRoles``/``globalRoles``,
    NOT on raw IdP ``groups`` (the vendored engine intentionally does not map
    groups→roles — that DDB-backed resolution is the control-plane authorizer's
    job). So on the JWT-only serve path, a caller with only groups and no resolved
    roles hits the fail-closed/opt-in path above; resolved roles must be forwarded
    by a trusted upstream for a prod ALLOW.
    """

    def __init__(self, fail_open_no_roles: bool | None = None, policy_loader: _DdbPolicyLoader | None = None):
        """Configure the fail-open policy and the Cedar policy source.

        Args:
            fail_open_no_roles: Whether a caller with no resolved roles is allowed.
                Defaults to the ``SCL_CEDAR_FAIL_OPEN_NO_ROLES`` env var (fail-closed).
            policy_loader: DDB-backed policy loader; built from ``ROLES_TABLE_NAME``
                when omitted, falling back to the shipped seed policies.
        """
        # SAFETY DEFAULT = FAIL-CLOSED. A caller with NO resolved roles is DENIED
        # unless the no-roles bypass is EXPLICITLY opted in via
        # SCL_CEDAR_FAIL_OPEN_NO_ROLES=true (a dev/pre-provisioning convenience so
        # the JWT-only deployment is usable before roles are forwarded). An
        # unconfigured / prod deployment therefore fails closed by default — never
        # silently fail-open. An explicit policy DENY is always honored; an
        # evaluator error always denies.
        if fail_open_no_roles is None:
            fail_open_no_roles = os.environ.get("SCL_CEDAR_FAIL_OPEN_NO_ROLES", "").lower() == "true"
        self._fail_open = fail_open_no_roles
        # DDB-backed policies (control-plane parity): built from ROLES_TABLE_NAME
        # when configured; falls back to the shipped seed policies otherwise.
        if policy_loader is None:
            table = os.environ.get("ROLES_TABLE_NAME", "")
            if table:
                try:
                    # AWS_REGION is always set in the AgentCore runtime; the DAO
                    # requires an explicit region (same as the control-plane
                    # authorizer). A missing region falls through to seeds below.
                    policy_loader = _DdbPolicyLoader(table, os.environ["AWS_REGION"])
                except Exception as exc:
                    logger.warning("cedar_ddb_loader_unavailable", error=type(exc).__name__)
        self._policy_loader = policy_loader

    def authorize(self, *, namespace: str, profile: dict[str, Any], tables: list[str]) -> CedarDecision:
        """Evaluate the Cedar ``query`` action on the namespace against seed policies.

        Fails closed for a caller with no resolved roles unless the no-roles
        bypass is opted in; any evaluator error also denies.

        Args:
            namespace: Namespace the query targets (the Cedar resource).
            profile: Caller's resolved auth context (user id and roles).
            tables: Tables referenced by the query (logged for context).

        Returns:
            A CedarDecision indicating whether the query is allowed.
        """
        profile = profile or {}
        user_id = str(profile.get(_USER_ID_KEY) or profile.get("sub") or "anonymous")
        global_roles = [str(r) for r in _as_list(profile.get(_GLOBAL_ROLES_KEY))]
        resource_roles_raw = _as_list(profile.get(_RESOURCE_ROLES_KEY))
        # Normalize resource roles to Cedar's {"role","resourceUID"} shape.
        resource_roles: list[dict[str, str]] = []
        for rr in resource_roles_raw:
            if isinstance(rr, dict) and rr.get("role") and rr.get("resourceUID"):
                resource_roles.append({"role": str(rr["role"]), "resourceUID": str(rr["resourceUID"])})
            else:
                # A malformed entry silently dropped would surface downstream as a
                # mysterious no-roles deny — log it so misconfiguration is findable.
                logger.warning(
                    "cedar_malformed_resource_role_dropped", namespace=namespace, entry_type=type(rr).__name__
                )

        # No resolved roles at all → FAIL CLOSED unless the bypass is explicitly
        # opted in (the AgentCore WS path is JWT-only today; until resolved roles
        # are forwarded, a dev env may set SCL_CEDAR_FAIL_OPEN_NO_ROLES=true).
        if not global_roles and not resource_roles:
            if self._fail_open:
                logger.warning("cedar_authorize_fail_open_no_roles", namespace=namespace, bypass="explicit-opt-in")
                return CedarDecision(allowed=True)
            logger.warning("cedar_authorize_fail_closed_no_roles", namespace=namespace)
            return CedarDecision(allowed=False, reason="no authorization roles resolved for caller")

        # Policy source: DDB role policies when configured (namespace-custom
        # policies apply on the data path), seed policies otherwise.
        policies = None
        if self._policy_loader is not None:
            role_ids = set(global_roles) | {rr["role"] for rr in resource_roles}
            policies = self._policy_loader.policies_for_roles(role_ids)
        if policies is None:
            policies = _seed_policies()

        try:
            result = evaluate(
                policies=policies,
                user_id=user_id,
                action=_QUERY_ACTION,
                resource_type=_NAMESPACE_RESOURCE,
                resource_id=namespace,
                global_roles=global_roles,
                resource_roles=resource_roles,
                # Namespace-scoped seed policies match on resource.namespace; the
                # query resource IS the namespace, so populate it (mirrors the
                # control-plane authorizer's resource_attrs).
                resource_attrs={"namespace": namespace},
            )
        except Exception as exc:
            # Evaluator failure ALWAYS denies (fail-closed), regardless of env.
            # Log full diagnostics server-side for this security-critical path —
            # the client still only sees the generic reason below.
            logger.warning(
                "cedar_authorize_error",
                namespace=namespace,
                user_id=user_id,
                global_roles=global_roles,
                resource_role_count=len(resource_roles),
                error=type(exc).__name__,
                error_msg=str(exc),
                exc_info=True,
            )
            return CedarDecision(allowed=False, reason="authorization policy evaluation failed")

        if result.allowed:
            logger.debug("cedar_authorize_allow", namespace=namespace, user_id=user_id)
            return CedarDecision(allowed=True)
        logger.warning("cedar_authorize_deny", namespace=namespace, user_id=user_id)
        return CedarDecision(allowed=False, reason="not permitted to query this namespace")
