# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""RealCedarAuthorizer — real cedarpy evaluation on the serve path."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog
from coa_serve.tier2.cedar_authorizer import (
    POLICIES_UNAVAILABLE_REASON,
    NullCedarAuthorizer,
    RealCedarAuthorizer,
)

_NS = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


@pytest.mark.unit
class TestRealCedarAuthorizer:
    def test_data_analyst_own_namespace_allowed(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(
            namespace=_NS,
            tables=["orders"],
            profile={"userId": "u1", "resourceRoles": [{"role": "data-analyst", "resourceUID": _NS}]},
        )
        assert d.allowed

    def test_data_analyst_other_namespace_denied(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(
            namespace=_NS,
            tables=["orders"],
            profile={"userId": "u1", "resourceRoles": [{"role": "data-analyst", "resourceUID": "other"}]},
        )
        assert not d.allowed

    def test_platform_admin_allowed(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "a", "globalRoles": ["platform-admin"]})
        assert d.allowed

    def test_platform_viewer_allowed_for_query(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "v", "globalRoles": ["platform-viewer"]})
        assert d.allowed

    def test_unknown_role_denied(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "x", "globalRoles": ["nope"]})
        assert not d.allowed

    def test_no_roles_prod_fails_closed(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "x"})
        assert not d.allowed
        assert "no authorization roles" in (d.reason or "")

    def test_no_roles_dev_fails_open(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=True)
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "x"})
        assert d.allowed

    def test_comma_joined_global_roles_accepted(self):
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "a", "globalRoles": "platform-admin"})
        assert d.allowed

    def test_malformed_resource_roles_ignored_safely(self):
        """A malformed resourceRoles entry is skipped, not crashed on; with no other roles → fail-closed in prod."""
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(
            namespace=_NS, tables=["t"], profile={"userId": "u", "resourceRoles": [{"role": "data-analyst"}]}
        )
        assert not d.allowed  # incomplete role (no resourceUID) → no valid roles → fail-closed

    def test_evaluator_error_fails_closed_even_in_dev(self, monkeypatch):
        """A genuine evaluator exception denies regardless of environment."""
        import coa_serve.tier2.cedar_authorizer as mod

        def _boom(**kwargs):
            raise RuntimeError("cedar engine exploded")

        monkeypatch.setattr(mod, "evaluate", _boom, raising=False)
        # patch the symbol the method imports lazily
        monkeypatch.setattr("coa_authorization.policy_evaluator.evaluate", _boom)
        auth = RealCedarAuthorizer(fail_open_no_roles=True)
        d = auth.authorize(
            namespace=_NS,
            tables=["t"],
            profile={"userId": "a", "globalRoles": ["platform-admin"]},
        )
        assert not d.allowed
        assert "evaluation failed" in (d.reason or "")


@pytest.mark.unit
class TestNullCedarAuthorizer:
    def test_allows_everything(self):
        auth = NullCedarAuthorizer()
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "x"})
        assert d.allowed


@pytest.mark.unit
class TestDefaultPostureIsFailClosed:
    """SAFETY: with NO env opt-in, the default authorizer fails CLOSED on no-roles."""

    def test_default_no_env_fails_closed(self, monkeypatch):
        # Explicitly clear the session opt-in (conftest sets it true) so this
        # asserts the prod-safe DEFAULT: unconfigured → fail-closed.
        monkeypatch.delenv("SCL_CEDAR_FAIL_OPEN_NO_ROLES", raising=False)
        auth = RealCedarAuthorizer()
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "x"})
        assert not d.allowed

    def test_env_opt_in_enables_no_roles_bypass(self, monkeypatch):
        monkeypatch.setenv("SCL_CEDAR_FAIL_OPEN_NO_ROLES", "true")
        auth = RealCedarAuthorizer()
        d = auth.authorize(namespace=_NS, tables=["t"], profile={"userId": "x"})
        assert d.allowed

    def test_firewall_default_authorizer_fails_closed_no_roles(self, monkeypatch):
        """SQLFirewall() with no injected authorizer + no env opt-in → no-roles denied."""
        from coa_serve.tier2.sql_firewall import SQLFirewall

        monkeypatch.delenv("SCL_CEDAR_FAIL_OPEN_NO_ROLES", raising=False)
        fw = SQLFirewall()
        result = fw.evaluate("SELECT id FROM orders", profile={"userId": "x"}, namespace=_NS)
        assert result.denied


@pytest.mark.unit
class TestRolePolicyCache:
    """Serve's Roles-table policy source: shared loader + per-role cache.

    Reads go through ``coa_authorization.policy_loader.load_policies_for_roles``
    (patched here). A read error or an empty table reuses last-known-good per role
    for a bounded window, then denies. The shipped seed policies are never a
    fallback for a configured table.
    """

    _ALLOW_ANALYST = (
        'permit(principal, action == SCL::Action::"query", resource) '
        'when { principal.resourceRoles.contains({role: "data-analyst", resourceUID: resource.namespace}) };'
    )
    _DEFAULT = 'forbid(principal, action == SCL::Action::"denyAll", resource);'

    class _Clock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    class _Table:
        """Fake Roles table behind the shared loader: role -> policy text."""

        def __init__(self, policies: dict[str, str]) -> None:
            self.policies = dict(policies)
            self.error: Exception | None = None
            self.calls: list[list[str]] = []
            self.daos: list[object] = []

        def load(self, role_ids, table_name, region, *, dao=None):
            role_ids = list(role_ids)
            self.calls.append(role_ids)
            self.daos.append(dao)
            if self.error is not None:
                raise self.error
            return "\n".join(self.policies[r] for r in role_ids if self.policies.get(r))

    @pytest.fixture(autouse=True)
    def _no_real_dao(self, monkeypatch):
        """Unit tests never build a real boto3 resource for the cache's DAO."""
        from coa_serve.tier2 import cedar_authorizer as mod

        monkeypatch.setattr(mod, "DynamoDBDAO", MagicMock(name="DynamoDBDAO"))

    def _cache(self, monkeypatch, table, *, ttl_s=60.0, max_stale_s=900.0):
        from coa_serve.tier2 import cedar_authorizer as mod

        monkeypatch.setattr(mod, "load_policies_for_roles", table.load)
        clock = self._Clock()
        return mod._RolePolicyCache(
            "roles-table", "us-east-1", ttl_s=ttl_s, max_stale_s=max_stale_s, clock=clock
        ), clock

    def _authorizer(self, cache):
        return RealCedarAuthorizer(fail_open_no_roles=False, policy_loader=cache, allow_without_roles=False)

    @staticmethod
    def _analyst_profile():
        return {"userId": "u1", "resourceRoles": [{"role": "data-analyst", "resourceUID": _NS}]}

    def test_reads_through_shared_loader_one_role_per_call(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, _ = self._cache(monkeypatch, table)
        text = cache.policies_for_roles({"data-analyst"})
        assert text == f"{self._ALLOW_ANALYST}\n{self._DEFAULT}"
        assert sorted(table.calls) == [["data-analyst"], ["default"]]

    def test_dao_built_once_and_reused_by_every_refresh(self, monkeypatch):
        """All reads go through the one DAO built with the cache.

        Building a DAO creates a boto3 resource on the shared default session, which
        is not thread-safe, and refreshes run from many request threads at once. This
        goes through the real shared loader, with only the DAO class patched.
        """
        import coa_authorization.policy_loader as loader_mod
        from coa_serve.tier2 import cedar_authorizer as mod

        policies = {"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST}

        def _get(key):
            role = key["SK"].removeprefix("ROLE#")
            return {"cedarPolicy": policies[role]} if key["PK"] == "GLOBAL" and role in policies else None

        dao = MagicMock(name="dao")
        dao.get.side_effect = _get
        cache_dao_cls = MagicMock(name="cache DynamoDBDAO", return_value=dao)
        loader_dao_cls = MagicMock(name="loader DynamoDBDAO")
        monkeypatch.setattr(mod, "DynamoDBDAO", cache_dao_cls)
        monkeypatch.setattr(loader_mod, "DynamoDBDAO", loader_dao_cls)
        clock = self._Clock()
        cache = mod._RolePolicyCache("roles-table", "us-east-1", ttl_s=60.0, max_stale_s=900.0, clock=clock)

        for _ in range(3):
            text = cache.policies_for_roles({"data-analyst", "namespace-owner"})
            assert text == f"{self._ALLOW_ANALYST}\n{self._DEFAULT}"
            clock.now += 61

        cache_dao_cls.assert_called_once()
        assert cache_dao_cls.call_args.args == ("roles-table",)
        assert cache_dao_cls.call_args.kwargs["region"] == "us-east-1"
        # The synchronous request-path profile, not the DAO's background default.
        assert cache_dao_cls.call_args.kwargs["config"].read_timeout == 8
        loader_dao_cls.assert_not_called()
        # 3 refreshes x 3 roles: data-analyst and default hit GLOBAL, namespace-owner
        # misses both partitions.
        assert dao.get.call_count == 3 * (1 + 1 + 2)

    def test_ttl_cache_avoids_second_read(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT})
        cache, clock = self._cache(monkeypatch, table)
        first = cache.policies_for_roles(set())
        clock.now += 59
        table.error = AssertionError("should be cached")
        assert cache.policies_for_roles(set()) == first
        assert len(table.calls) == 1

    def test_read_error_serves_last_known_good_and_logs_age(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table)
        auth = self._authorizer(cache)
        assert auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile()).allowed
        clock.now += 300
        table.error = RuntimeError("throttled")
        with structlog.testing.capture_logs() as logs:
            d = auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile())
        assert d.allowed
        failed = next(log for log in logs if log["event"] == "cedar_ddb_policy_load_failed")
        assert failed["error"] == "RuntimeError"
        assert failed["fallback"] == "last-known-good"
        lkg = next(log for log in logs if log["event"] == "cedar_policy_last_known_good_used")
        assert lkg["age_s"] == 300.0

    def test_read_error_serves_tightened_policy_not_seeds(self, monkeypatch):
        """Regression: a tightened role must stay tight while the table is failing."""
        tightened = 'forbid(principal, action == SCL::Action::"query", resource);'
        table = self._Table({"default": self._DEFAULT, "data-analyst": tightened})
        cache, clock = self._cache(monkeypatch, table)
        auth = self._authorizer(cache)
        assert not auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile()).allowed
        clock.now += 120
        table.error = RuntimeError("throttled")
        d = auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile())
        # Seeds would ALLOW data-analyst in its own namespace; the stored policy forbids.
        assert not d.allowed
        assert d.reason == "not permitted to query this namespace"

    def test_read_error_with_no_cached_entry_denies(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        table.error = RuntimeError("throttled")
        cache, _ = self._cache(monkeypatch, table)
        d = self._authorizer(cache).authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile())
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON

    def test_new_role_combination_of_cached_roles_still_works_during_outage(self, monkeypatch):
        """Cached per role, not per role set."""
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST, "viewer": ""})
        cache, clock = self._cache(monkeypatch, table)
        assert cache.policies_for_roles({"data-analyst"}) is not None
        assert cache.policies_for_roles({"viewer"}) is not None
        clock.now += 120
        table.error = RuntimeError("throttled")
        assert cache.policies_for_roles({"data-analyst", "viewer"}) == f"{self._ALLOW_ANALYST}\n{self._DEFAULT}"

    def test_role_never_loaded_denies_during_outage(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table)
        assert cache.policies_for_roles(set()) is not None
        clock.now += 120
        table.error = RuntimeError("throttled")
        assert cache.policies_for_roles({"data-analyst"}) is None

    def test_empty_table_denies_when_nothing_cached(self, monkeypatch):
        table = self._Table({})
        cache, _ = self._cache(monkeypatch, table)
        with structlog.testing.capture_logs() as logs:
            d = self._authorizer(cache).authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile())
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON
        assert any(log["event"] == "cedar_ddb_policies_empty" for log in logs)

    def test_empty_table_does_not_overwrite_last_known_good(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table)
        good = cache.policies_for_roles({"data-analyst"})
        clock.now += 120
        table.policies = {}  # e.g. table missing: DynamoDBDAO.get returns None for everything
        assert cache.policies_for_roles({"data-analyst"}) == good
        # Still the good entry later (the empty read did not replace it).
        clock.now += 120
        assert cache.policies_for_roles({"data-analyst"}) == good

    def test_healthy_read_where_role_has_no_policy_is_authoritative(self, monkeypatch):
        """With ``default`` present the table answered, so a role's removed policy is taken as-is."""
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table)
        cache.policies_for_roles({"data-analyst"})
        clock.now += 120
        table.policies["data-analyst"] = ""
        assert cache.policies_for_roles({"data-analyst"}) == self._DEFAULT

    def test_last_known_good_past_max_age_denies(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table, max_stale_s=900.0)
        auth = self._authorizer(cache)
        assert auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile()).allowed
        table.error = RuntimeError("throttled")
        clock.now += 900
        assert auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile()).allowed
        clock.now += 1
        with structlog.testing.capture_logs() as logs:
            d = auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile())
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON
        unavailable = next(log for log in logs if log["event"] == "cedar_policies_unavailable")
        assert unavailable["age_s"] == 901.0

    def test_recovery_after_outage_refreshes_cache(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table)
        cache.policies_for_roles({"data-analyst"})
        table.error = RuntimeError("throttled")
        clock.now += 1000
        assert cache.policies_for_roles({"data-analyst"}) is None
        table.error = None
        assert cache.policies_for_roles({"data-analyst"}) is not None

    def test_max_age_and_ttl_read_from_env(self, monkeypatch):
        from coa_serve.tier2 import cedar_authorizer as mod

        monkeypatch.setenv("SCL_CEDAR_POLICY_CACHE_TTL_S", "5")
        monkeypatch.setenv("SCL_CEDAR_POLICY_MAX_STALE_S", "30")
        cache = mod._RolePolicyCache("roles-table", "us-east-1")
        assert (cache._ttl_s, cache._max_stale_s) == (5.0, 30.0)

    def test_invalid_env_values_use_defaults(self, monkeypatch):
        from coa_serve.tier2 import cedar_authorizer as mod

        monkeypatch.setenv("SCL_CEDAR_POLICY_CACHE_TTL_S", "soon")
        monkeypatch.setenv("SCL_CEDAR_POLICY_MAX_STALE_S", "-1")
        cache = mod._RolePolicyCache("roles-table", "us-east-1")
        assert (cache._ttl_s, cache._max_stale_s) == (60.0, 900.0)

    @pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity"])
    def test_non_finite_env_values_use_defaults(self, monkeypatch, raw):
        """A NaN TTL would never refresh and a NaN max-stale would never expire."""
        from coa_serve.tier2 import cedar_authorizer as mod

        monkeypatch.setenv("SCL_CEDAR_POLICY_CACHE_TTL_S", raw)
        monkeypatch.setenv("SCL_CEDAR_POLICY_MAX_STALE_S", raw)
        with structlog.testing.capture_logs() as logs:
            cache = mod._RolePolicyCache("roles-table", "us-east-1")
        assert (cache._ttl_s, cache._max_stale_s) == (60.0, 900.0)
        invalid = sorted(log["setting"] for log in logs if log["event"] == "cedar_policy_cache_setting_invalid")
        assert invalid == ["SCL_CEDAR_POLICY_CACHE_TTL_S", "SCL_CEDAR_POLICY_MAX_STALE_S"]

    @pytest.mark.parametrize("max_stale_s", [0.0, 30.0])
    def test_max_stale_below_ttl_is_raised_to_ttl(self, monkeypatch, max_stale_s):
        table = self._Table({"default": self._DEFAULT})
        with structlog.testing.capture_logs() as logs:
            cache, _ = self._cache(monkeypatch, table, ttl_s=60.0, max_stale_s=max_stale_s)
        assert cache._max_stale_s == 60.0
        warned = next(log for log in logs if log["event"] == "cedar_policy_max_stale_below_ttl")
        assert (warned["ttl_s"], warned["max_stale_s"]) == (60.0, max_stale_s)

    def test_max_stale_below_ttl_from_env_is_raised_to_ttl(self, monkeypatch):
        from coa_serve.tier2 import cedar_authorizer as mod

        monkeypatch.delenv("SCL_CEDAR_POLICY_CACHE_TTL_S", raising=False)
        monkeypatch.setenv("SCL_CEDAR_POLICY_MAX_STALE_S", "0")
        cache = mod._RolePolicyCache("roles-table", "us-east-1")
        assert (cache._ttl_s, cache._max_stale_s) == (60.0, 60.0)

    def test_max_stale_at_or_above_ttl_is_kept(self, monkeypatch):
        table = self._Table({"default": self._DEFAULT})
        with structlog.testing.capture_logs() as logs:
            equal, _ = self._cache(monkeypatch, table, ttl_s=60.0, max_stale_s=60.0)
            above, _ = self._cache(monkeypatch, table, ttl_s=60.0, max_stale_s=61.0)
        assert (equal._max_stale_s, above._max_stale_s) == (60.0, 61.0)
        assert not any(log["event"] == "cedar_policy_max_stale_below_ttl" for log in logs)

    @pytest.mark.parametrize("max_stale_s", [0.0, 30.0])
    def test_max_stale_below_ttl_does_not_deny_while_table_is_healthy(self, monkeypatch, max_stale_s):
        """Regression: an entry older than max-stale but younger than the TTL was never
        re-read yet always rejected, so a healthy table answered 403 for the rest of
        every TTL period (TTL 60 / max-stale 0 denied at t=1; max-stale 30 at t=40)."""
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table, ttl_s=60.0, max_stale_s=max_stale_s)
        auth = self._authorizer(cache)
        start = clock.now
        for t in (0, 1, 40, 59, 60, 61, 119, 120, 121):
            clock.now = start + t
            d = auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile())
            assert d.allowed, f"denied at t={t}: {d.reason}"
        # Still read once per TTL period, not on every request.
        assert len(table.calls) == 3 * 2  # t=0, 60, 120; data-analyst + default each

    def test_max_stale_zero_still_denies_past_the_ttl_during_an_outage(self, monkeypatch):
        """Raising max-stale to the TTL does not extend last-known-good past the TTL."""
        table = self._Table({"default": self._DEFAULT, "data-analyst": self._ALLOW_ANALYST})
        cache, clock = self._cache(monkeypatch, table, ttl_s=60.0, max_stale_s=0.0)
        auth = self._authorizer(cache)
        assert auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile()).allowed
        table.error = RuntimeError("throttled")
        clock.now += 61
        d = auth.authorize(namespace=_NS, tables=["t"], profile=self._analyst_profile())
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON


@pytest.mark.unit
class TestPolicySourceConfiguration:
    """No runtime seed fallback: seeds only with no table AND the explicit opt-in."""

    _ADMIN = {"userId": "a", "globalRoles": ["platform-admin"]}

    def test_roles_table_unset_denies_without_opt_in(self, monkeypatch):
        monkeypatch.delenv("ROLES_TABLE_NAME", raising=False)
        monkeypatch.delenv("ALLOW_WITHOUT_ROLES", raising=False)
        with structlog.testing.capture_logs() as logs:
            auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(namespace=_NS, tables=["t"], profile=self._ADMIN)
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON
        assert any(log["event"] == "cedar_roles_table_not_configured" for log in logs)

    def test_roles_table_unset_with_opt_in_uses_seed_policies(self, monkeypatch):
        monkeypatch.delenv("ROLES_TABLE_NAME", raising=False)
        monkeypatch.setenv("ALLOW_WITHOUT_ROLES", "true")
        auth = RealCedarAuthorizer(fail_open_no_roles=False)
        assert auth.authorize(namespace=_NS, tables=["t"], profile=self._ADMIN).allowed

    def test_loader_construction_failure_denies(self, monkeypatch):
        monkeypatch.setenv("ROLES_TABLE_NAME", "roles-table")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("ALLOW_WITHOUT_ROLES", raising=False)
        with structlog.testing.capture_logs() as logs:
            auth = RealCedarAuthorizer(fail_open_no_roles=False)
        d = auth.authorize(namespace=_NS, tables=["t"], profile=self._ADMIN)
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON
        assert any(log["event"] == "cedar_policy_loader_unavailable" for log in logs)

    def test_opt_in_does_not_apply_when_a_table_is_configured(self, monkeypatch):
        """A configured but unusable table denies even with the opt-in set."""
        monkeypatch.setenv("ROLES_TABLE_NAME", "roles-table")
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("ALLOW_WITHOUT_ROLES", "true")
        d = RealCedarAuthorizer(fail_open_no_roles=False).authorize(namespace=_NS, tables=["t"], profile=self._ADMIN)
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON

    def test_configured_table_failing_read_denies_not_seeds(self, monkeypatch):
        """End-to-end through env construction: a failing table with nothing cached denies."""
        import coa_serve.tier2.cedar_authorizer as mod

        def _boom(role_ids, table_name, region, *, dao=None):
            raise RuntimeError("table unavailable")

        monkeypatch.setattr(mod, "load_policies_for_roles", _boom)
        monkeypatch.setenv("ROLES_TABLE_NAME", "roles-table")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("ALLOW_WITHOUT_ROLES", "true")
        d = RealCedarAuthorizer(fail_open_no_roles=False).authorize(namespace=_NS, tables=["t"], profile=self._ADMIN)
        assert not d.allowed
        assert d.reason == POLICIES_UNAVAILABLE_REASON

    def test_firewall_surfaces_policies_unavailable_reason(self, monkeypatch):
        from coa_serve.tier2.sql_firewall import SQLFirewall

        monkeypatch.delenv("ROLES_TABLE_NAME", raising=False)
        monkeypatch.delenv("ALLOW_WITHOUT_ROLES", raising=False)
        fw = SQLFirewall()
        result = fw.authorize_namespace(_NS, self._ADMIN)
        assert result.denied
        assert result.reason == POLICIES_UNAVAILABLE_REASON
