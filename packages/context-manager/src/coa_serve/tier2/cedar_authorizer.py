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
Policy source (FAIL-CLOSED): role policies come from the Roles table via the
shared ``coa_authorization.policy_loader.load_policies_for_roles`` (the loader the
MCP grant resolver uses). When the table cannot be read, or reads back empty, the
last successfully loaded policy of each role is reused for a bounded window and
then the query is denied with ``authorization_policies_unavailable``. The shipped
seed policies are used only when no Roles table is configured AND the dev/test
opt-in ``ALLOW_WITHOUT_ROLES=true`` is set (same name as the control-plane
authorizer's opt-in). They are never a runtime fallback for a failing table.

- ``RealCedarAuthorizer`` — the default: real ``cedarpy`` evaluation vs Roles-table policies.
- ``NullCedarAuthorizer`` — explicit opt-out (allow-all at the Cedar layer only).
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

import structlog
from coa_authorization.policy_evaluator import evaluate, load_seed_policies
from coa_authorization.policy_loader import load_policies_for_roles
from coa_common.aws_config import sync_boto_config
from coa_common.dao import DynamoDBDAO

logger = structlog.get_logger(__name__)

# Roles-table policy caching. The serve runtime is long-lived (unlike the Lambda
# authorizer, which leans on the API GW authorizer cache), so role policies are
# cached per role:
# - SCL_CEDAR_POLICY_CACHE_TTL_S (default 60): a cached role policy younger than
#   this is used without re-reading the table.
# - SCL_CEDAR_POLICY_MAX_STALE_S (default 900): when a refresh fails (read error,
#   or the table reads back empty), a role's last successfully loaded policy is
#   reused until it is this old, then the query is denied. A value below the TTL
#   is raised to the TTL: an entry is only re-read once it reaches the TTL, so a
#   shorter window would deny entries the cache never refreshes.
# Both settings must be finite and non-negative; anything else uses the default.
_DEFAULT_POLICY_CACHE_TTL_S = 60.0
_DEFAULT_POLICY_MAX_STALE_S = 900.0

# Denial reason when no usable role policy is available (Roles table unreadable
# or empty past the stale window, no table configured, loader not constructible).
POLICIES_UNAVAILABLE_REASON = "authorization_policies_unavailable"

# The always-loaded baseline role. It is seeded under PK=GLOBAL in every deployed
# environment, so a refresh in which it reads back empty means the table itself is
# missing or broken (``DynamoDBDAO.get`` swallows ResourceNotFound and returns
# None), not that the role has no policy.
_DEFAULT_ROLE = "default"


def _env_seconds(name: str, default: float) -> float:
    """Read a finite, non-negative duration in seconds from the environment.

    Args:
        name: Environment variable name.
        default: Value used when the variable is unset, empty or invalid
            (not a number, negative, ``nan`` or infinite).

    Returns:
        The parsed duration, or ``default``.
    """
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("cedar_policy_cache_setting_invalid", setting=name, value=raw, default=default)
        return default
    # ``nan`` passes a ``< 0`` check: a NaN TTL would never refresh and a NaN
    # max-stale would never expire, so non-finite values are rejected too.
    if not math.isfinite(value) or value < 0:
        logger.warning("cedar_policy_cache_setting_invalid", setting=name, value=raw, default=default)
        return default
    return value


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

    def can_view_namespace(self, *, namespace: str, profile: dict[str, Any]) -> bool:
        """Return whether the principal holds ``viewNamespace`` on ``namespace``."""
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

    def can_view_namespace(self, *, namespace: str, profile: dict[str, Any]) -> bool:
        """Allow viewing unconditionally at the Cedar-policy layer (opt-out).

        Mirrors :meth:`authorize`: when Cedar policy enforcement is disabled
        (``SCL_DISABLE_CEDAR=true``) the artifact gate must not independently
        withhold — artifacts follow the same allow-all posture as the query gate.
        """
        logger.debug("cedar_view_namespace_noop", namespace=namespace)
        return True


# Profile keys carrying the principal's resolved auth context. These mirror what
# the control-plane authorizer resolves/forwards (see authorization/handler.py
# auth_context): the principal id, global roles, and resource-scoped roles.
_USER_ID_KEY = "userId"
_GLOBAL_ROLES_KEY = "globalRoles"
_RESOURCE_ROLES_KEY = "resourceRoles"  # list[{"role","resourceUID"}] — Cedar shape

_QUERY_ACTION = "query"
_VIEW_NAMESPACE_ACTION = "viewNamespace"
_NAMESPACE_RESOURCE = "Namespace"


@lru_cache(maxsize=1)
def _seed_policies() -> str:
    """Load + cache the shipped seed Cedar policies (read once)."""
    return load_seed_policies()


@dataclass(frozen=True)
class _CachedRolePolicy:
    """One role's policy text and when it was last read from the table."""

    loaded_at: float
    text: str


class _RolePolicyCache:
    """Per-role cache of Roles-table policies, with bounded last-known-good.

    Reads go through the shared ``load_policies_for_roles`` (one role per call, so
    each role's text is attributable and cached on its own), all on one DAO built
    with the cache; only the caching and the failure handling are serve-specific.

    Refresh rules:
    - A role entry younger than ``ttl_s`` is served without a read.
    - Otherwise the stale roles are re-read together with ``default``. A read error,
      or ``default`` reading back empty, makes the whole refresh "unavailable": no
      entry is overwritten, and each role falls back to its cached entry while that
      entry is at most ``max_stale_s`` old.
    - On a healthy refresh, a role that reads back empty is cached as having no
      policy (the table answered, so that is authoritative).
    - If any requested role has no usable entry, the result is ``None``
      (unavailable) and the caller denies.
    """

    def __init__(
        self,
        table_name: str,
        region: str,
        *,
        ttl_s: float | None = None,
        max_stale_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Bind the cache to a Roles table.

        Args:
            table_name: DynamoDB Roles table name.
            region: AWS region of the table. Must be non-empty.
            ttl_s: Fresh-cache window; defaults to ``SCL_CEDAR_POLICY_CACHE_TTL_S``.
            max_stale_s: Last-known-good window; defaults to ``SCL_CEDAR_POLICY_MAX_STALE_S``.
                Raised to ``ttl_s`` when lower.
            clock: Monotonic clock, injectable for tests.

        Raises:
            ValueError: If ``table_name`` or ``region`` is empty.
        """
        if not table_name:
            raise ValueError("Roles table name is required")
        if not region:
            raise ValueError("AWS region is required for the Roles table")
        self._table_name = table_name
        self._region = region
        # One DAO for the cache's lifetime. Refreshes run from many request threads
        # at once (authorize is reached through asyncio.to_thread), and building a
        # DAO per read would create a boto3 resource on the shared default session
        # each time, which is slow and not thread-safe.
        self._dao = DynamoDBDAO(table_name, region=region, config=sync_boto_config())
        self._ttl_s = (
            _env_seconds("SCL_CEDAR_POLICY_CACHE_TTL_S", _DEFAULT_POLICY_CACHE_TTL_S) if ttl_s is None else ttl_s
        )
        self._max_stale_s = (
            _env_seconds("SCL_CEDAR_POLICY_MAX_STALE_S", _DEFAULT_POLICY_MAX_STALE_S)
            if max_stale_s is None
            else max_stale_s
        )
        if self._max_stale_s < self._ttl_s:
            # An entry is re-read only once it reaches the TTL, but rejected once it
            # is older than max-stale. Below the TTL that would deny every request in
            # between while the table is healthy, so the window starts at the TTL.
            logger.warning("cedar_policy_max_stale_below_ttl", ttl_s=self._ttl_s, max_stale_s=self._max_stale_s)
            self._max_stale_s = self._ttl_s
        self._clock = clock
        self._cache: dict[str, _CachedRolePolicy] = {}

    def _refresh(self, role_ids: list[str], now: float) -> bool:
        """Re-read ``role_ids`` (+ ``default``); update the cache only on a healthy read.

        Args:
            role_ids: Roles whose entries are missing or past the TTL.
            now: Current clock reading, stored as the entries' load time.

        Returns:
            True when the refresh succeeded and the cache was updated.
        """
        batch = sorted(set(role_ids) | {_DEFAULT_ROLE})
        loaded: dict[str, str] = {}
        try:
            for role_id in batch:
                loaded[role_id] = load_policies_for_roles(
                    [role_id], table_name=self._table_name, region=self._region, dao=self._dao
                )
        except Exception as exc:
            logger.warning(
                "cedar_ddb_policy_load_failed",
                error=type(exc).__name__,
                error_msg=str(exc),
                roles=batch,
                fallback="last-known-good",
            )
            return False
        if not loaded.get(_DEFAULT_ROLE):
            # Empty = unavailable: never let it overwrite a cached entry.
            logger.warning("cedar_ddb_policies_empty", roles=batch, fallback="last-known-good")
            return False
        for role_id, text in loaded.items():
            self._cache[role_id] = _CachedRolePolicy(loaded_at=now, text=text)
        return True

    def policies_for_roles(self, role_ids: set[str]) -> str | None:
        """Return the joined policy text for ``role_ids`` + ``default``, or None if unavailable.

        Args:
            role_ids: Roles the caller holds.

        Returns:
            Newline-joined Cedar policy text, or None when some role has no usable
            (fresh or within-max-age) policy entry.
        """
        roles = sorted(set(role_ids) | {_DEFAULT_ROLE})
        now = self._clock()
        stale = [r for r in roles if (hit := self._cache.get(r)) is None or now - hit.loaded_at >= self._ttl_s]
        if stale:
            self._refresh(stale, now)

        texts: list[str] = []
        oldest_age = 0.0
        for role_id in roles:
            hit = self._cache.get(role_id)
            age = None if hit is None else now - hit.loaded_at
            if hit is None or age is None or age > self._max_stale_s:
                logger.error(
                    "cedar_policies_unavailable",
                    role=role_id,
                    cached=hit is not None,
                    age_s=None if age is None else round(age, 1),
                    max_stale_s=self._max_stale_s,
                )
                return None
            oldest_age = max(oldest_age, age)
            if hit.text:
                texts.append(hit.text)
        if oldest_age >= self._ttl_s:
            logger.warning(
                "cedar_policy_last_known_good_used",
                roles=roles,
                age_s=round(oldest_age, 1),
                max_stale_s=self._max_stale_s,
            )
        joined = "\n".join(texts)
        return joined or None


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
    Roles-table policies of the principal's resolved roles from ``profile``
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

    Policy availability (SECURITY): no usable Roles-table policy → DENY
    with ``authorization_policies_unavailable``. The shipped seed policies are
    never substituted for a failing or empty table; they are used only when no
    Roles table is configured and ``ALLOW_WITHOUT_ROLES=true`` opts in (dev/test).
    """

    def __init__(
        self,
        fail_open_no_roles: bool | None = None,
        policy_loader: _RolePolicyCache | None = None,
        allow_without_roles: bool | None = None,
    ):
        """Configure the fail-open policy and the Cedar policy source.

        Args:
            fail_open_no_roles: Whether a caller with no resolved roles is allowed.
                Defaults to the ``SCL_CEDAR_FAIL_OPEN_NO_ROLES`` env var (fail-closed).
            policy_loader: Roles-table policy cache; built from ``ROLES_TABLE_NAME`` +
                ``AWS_REGION`` when omitted. If it cannot be built, every query is denied.
            allow_without_roles: Use the shipped seed policies when NO Roles table is
                configured. Defaults to the ``ALLOW_WITHOUT_ROLES`` env var (off → deny).
                Never applies when a table is configured.
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
        if allow_without_roles is None:
            allow_without_roles = os.environ.get("ALLOW_WITHOUT_ROLES", "").lower() == "true"
        # Policy source. Seeds are reachable only via the explicit no-table opt-in;
        # a configured table that cannot be used denies (fail-closed), never seeds.
        self._use_seed_policies = False
        if policy_loader is None:
            table = os.environ.get("ROLES_TABLE_NAME", "")
            if table:
                try:
                    # AWS_REGION is always set in the AgentCore runtime.
                    policy_loader = _RolePolicyCache(table, os.environ.get("AWS_REGION", ""))
                except Exception as exc:
                    logger.error(
                        "cedar_policy_loader_unavailable",
                        error=type(exc).__name__,
                        error_msg=str(exc),
                        effect="deny-all-queries",
                    )
            elif allow_without_roles:
                logger.warning("cedar_seed_policies_in_use", reason="no-roles-table", opt_in="ALLOW_WITHOUT_ROLES")
                self._use_seed_policies = True
            else:
                logger.error("cedar_roles_table_not_configured", effect="deny-all-queries")
        self._policy_loader = policy_loader

    def authorize(self, *, namespace: str, profile: dict[str, Any], tables: list[str]) -> CedarDecision:
        """Evaluate the Cedar ``query`` action on the namespace against role policies.

        Fails closed for a caller with no resolved roles unless the no-roles
        bypass is opted in; any evaluator error also denies.

        Args:
            namespace: Namespace the query targets (the Cedar resource).
            profile: Caller's resolved auth context (user id and roles).
            tables: Tables referenced by the query (logged for context).

        Returns:
            A CedarDecision indicating whether the query is allowed.
        """
        return self._evaluate_action(
            action=_QUERY_ACTION,
            namespace=namespace,
            profile=profile,
            deny_reason="not permitted to query this namespace",
        )

    def can_view_namespace(self, *, namespace: str, profile: dict[str, Any]) -> bool:
        """Whether the principal holds ``viewNamespace`` on ``namespace`` (#1166).

        This is the SAME right that gates reading a source's table/column schema
        on the control-plane read routes (``viewNamespace`` on ``Source`` reads;
        see authorization/handler.py). Used to decide whether the generated
        SQL/SPARQL artifacts may be returned: a caller who can already see the
        schema in the Sources/Tables view gains no new disclosure from seeing the
        query that touches it, while a caller who cannot is denied the artifacts
        even if they pass ``includeDebugInfo=true``.

        Fail-closed semantics are identical to :meth:`authorize` (no-roles →
        deny unless the explicit bypass is set; evaluator error → deny), so this
        never leaks artifacts to an unauthorized caller on error.
        """
        return self._evaluate_action(
            action=_VIEW_NAMESPACE_ACTION,
            namespace=namespace,
            profile=profile,
            deny_reason="not permitted to view this namespace",
        ).allowed

    def _evaluate_action(
        self, *, action: str, namespace: str, profile: dict[str, Any], deny_reason: str
    ) -> CedarDecision:
        """Evaluate ``action`` on ``namespace`` for the principal in ``profile``.

        Shared by :meth:`authorize` (``query``) and :meth:`can_view_namespace`
        (``viewNamespace``) so role resolution, the no-roles fail-closed policy,
        the DDB/seed policy source, and the error-denies semantics stay in exactly
        one place and cannot drift between the two actions.
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
                logger.warning(
                    "cedar_authorize_fail_open_no_roles", namespace=namespace, action=action, bypass="explicit-opt-in"
                )
                return CedarDecision(allowed=True)
            logger.warning("cedar_authorize_fail_closed_no_roles", namespace=namespace, action=action)
            return CedarDecision(allowed=False, reason="no authorization roles resolved for caller")

        # Policy source: Roles-table policies (namespace-custom policies apply on the
        # data path); seeds only under the explicit no-table opt-in. Anything else
        # (no table, loader not built, table unreadable/empty past the stale
        # window) denies — never the looser shipped seeds.
        policies: str | None = None
        if self._policy_loader is not None:
            role_ids = set(global_roles) | {rr["role"] for rr in resource_roles}
            policies = self._policy_loader.policies_for_roles(role_ids)
        elif self._use_seed_policies:
            policies = _seed_policies()
        if policies is None:
            logger.error("cedar_authorize_deny_policies_unavailable", namespace=namespace, user_id=user_id)
            return CedarDecision(allowed=False, reason=POLICIES_UNAVAILABLE_REASON)

        try:
            result = evaluate(
                policies=policies,
                user_id=user_id,
                action=action,
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
                action=action,
                user_id=user_id,
                global_roles=global_roles,
                resource_role_count=len(resource_roles),
                error=type(exc).__name__,
                error_msg=str(exc),
                exc_info=True,
            )
            return CedarDecision(allowed=False, reason="authorization policy evaluation failed")

        if result.allowed:
            logger.debug("cedar_authorize_allow", namespace=namespace, action=action, user_id=user_id)
            return CedarDecision(allowed=True)
        logger.warning("cedar_authorize_deny", namespace=namespace, action=action, user_id=user_id)
        return CedarDecision(allowed=False, reason=deny_reason)
