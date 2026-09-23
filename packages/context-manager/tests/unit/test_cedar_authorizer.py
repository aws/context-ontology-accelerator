# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""RealCedarAuthorizer — real cedarpy evaluation on the serve path."""

from __future__ import annotations

import pytest
import structlog
from coa_serve.tier2.cedar_authorizer import (
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
class TestDdbPolicyLoader:
    """The serve-path DDB role-policy loader reads through the shared
    ``DynamoDBDAO`` (control-plane parity) and is fail-SAFE to seed policies.
    """

    @staticmethod
    def _loader_with(monkeypatch, *, items=None, get_raises=None):
        """Build a `_DdbPolicyLoader` whose DAO is a fake.

        ``items`` maps (PK, SK) -> item dict; ``get_raises`` is an exception the
        fake DAO.get raises (to exercise the fail-safe path).
        """
        from coa_serve.tier2 import cedar_authorizer as mod

        items = items or {}

        class _FakeDao:
            def __init__(self, table_name, *, region, config=None):
                self.table_name = table_name
                self.region = region
                self.config = config

            def get(self, key, *, projection=None):
                if get_raises is not None:
                    raise get_raises
                return items.get((key["PK"], key["SK"]))

        monkeypatch.setattr(mod, "DynamoDBDAO", _FakeDao)
        return mod._DdbPolicyLoader("roles-table", "us-east-1")

    def test_uses_sync_timeout_profile(self, monkeypatch):
        loader = self._loader_with(monkeypatch)
        assert loader._dao.config.read_timeout == 8
        assert loader._dao.config.retries["max_attempts"] == 2

    def test_global_wins_over_namespace_template(self, monkeypatch):
        # Both PKs hold a policy for the role; GLOBAL is consulted first and
        # short-circuits, so its text must win.
        loader = self._loader_with(
            monkeypatch,
            items={
                ("GLOBAL", "ROLE#default"): {"cedarPolicy": "GLOBAL_POLICY"},
                ("NAMESPACE_TEMPLATE", "ROLE#default"): {"cedarPolicy": "NS_TEMPLATE_POLICY"},
            },
        )
        assert loader.policies_for_roles(set()) == "GLOBAL_POLICY"  # only "default"

    def test_falls_back_to_namespace_template_when_no_global(self, monkeypatch):
        # GLOBAL absent for the role → NAMESPACE_TEMPLATE is used.
        loader = self._loader_with(
            monkeypatch,
            items={("NAMESPACE_TEMPLATE", "ROLE#default"): {"cedarPolicy": "NS_TEMPLATE_POLICY"}},
        )
        assert loader.policies_for_roles(set()) == "NS_TEMPLATE_POLICY"

    def test_no_stored_policies_returns_none_for_seed_fallback(self, monkeypatch):
        # No items for any role → None → authorizer uses shipped seed policies.
        loader = self._loader_with(monkeypatch, items={})
        assert loader.policies_for_roles({"data-analyst"}) is None

    def test_dao_error_is_failsafe_to_seeds(self, monkeypatch):
        # A non-NotFound ClientError (DAO re-raises it) is caught → None → seeds.
        # This preserves the fail-SAFE-to-seeds contract through the DAO swap.
        loader = self._loader_with(monkeypatch, get_raises=RuntimeError("throttled"))
        with structlog.testing.capture_logs() as logs:
            assert loader.policies_for_roles({"data-analyst"}) is None

        warning = next(log for log in logs if log["event"] == "cedar_ddb_policy_load_failed")
        assert warning["error"] == "RuntimeError"
        assert warning["fallback"] == "seed-policies"

    def test_ttl_cache_avoids_second_read(self, monkeypatch):
        loader = self._loader_with(
            monkeypatch,
            items={("GLOBAL", "ROLE#default"): {"cedarPolicy": "permit(principal, action, resource);"}},
        )
        first = loader.policies_for_roles(set())
        # Swap the DAO for one that would raise if read again; cache must serve it.
        loader._dao.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached"))
        second = loader.policies_for_roles(set())
        assert first == second
