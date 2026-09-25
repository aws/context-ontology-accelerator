# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the serve-layer role resolver.

Covers resolve_profile (RRM PrincipalIndex lookup → global/resource roles) and
_merge_data_restrictions (tableAllowlist union, columnDenylist intersection,
allowedMetrics union), plus ResolvedProfile.inject_into.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_serve import role_resolver
from coa_serve.role_resolver import ResolvedProfile, resolve_profile

_NS = "ns-123"


def _result(items):
    """Build a stand-in PaginatedResult-like object with an .items attribute."""
    r = MagicMock()
    r.items = items
    return r


def _mock_dao(items_by_pk):
    """Return a DynamoDBDAO factory whose query dispatches on principalKey.

    ``items_by_pk`` maps the ``:pk`` expression value to the list of grant items
    returned for that principal.

    Both ``query`` and ``query_all`` are stubbed: the resolver uses ``query_all``
    (a single page would silently drop grants past 1 MB), and ``query_all``
    returns a plain list rather than a ``PaginatedResult``.
    """
    dao = MagicMock()

    def _items_for(params):
        return items_by_pk.get(params.expression_values[":pk"], [])

    dao.query.side_effect = lambda params: _result(_items_for(params))
    dao.query_all.side_effect = _items_for
    return dao


@pytest.mark.unit
class TestResolveProfileNoTable:
    def test_returns_minimal_profile_when_no_table_configured(self):
        with patch.object(role_resolver, "_RRM_TABLE", ""):
            resolved = resolve_profile("alice@x.com", ["Admin"], namespace=_NS)
        assert resolved.user_id == "alice@x.com"
        assert resolved.groups == ["Admin"]
        assert resolved.global_roles == []
        assert resolved.resource_roles == []
        assert resolved.table_allowlist is None
        assert resolved.column_denylist is None
        assert resolved.allowed_metrics is None


@pytest.mark.unit
class TestResolveProfileDaoConfig:
    def test_uses_sync_timeout_profile(self):
        """resolve_profile is on the synchronous serve invoke() request path.

        It must fail fast rather than inherit the DAO's background
        30-second/three-attempt default (matches the Cedar policy loader's
        #1092 fix).
        """
        dao_cls = MagicMock(return_value=_mock_dao({}))
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", dao_cls),
        ):
            resolve_profile("alice@x.com", [], namespace=_NS)

        config = dao_cls.call_args.kwargs["config"]
        assert config.read_timeout == 8
        assert config.retries["max_attempts"] == 2


@pytest.mark.unit
class TestResolveProfileRoles:
    def test_resolves_global_and_resource_roles(self):
        items = {
            "User::alice@x.com": [
                {"role": "platform-admin", "resourceId": "GLOBAL"},
                {"role": "namespace-maintainer", "resourceId": _NS},
            ],
            "Group::Admin": [
                {"role": "platform-viewer", "resourceId": "GLOBAL"},
            ],
        }
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("alice@x.com", ["Admin"], namespace=_NS)

        assert "platform-admin" in resolved.global_roles
        assert "platform-viewer" in resolved.global_roles
        assert {"role": "namespace-maintainer", "resourceUID": _NS} in resolved.resource_roles

    def test_query_exception_propagates(self):
        """A grant-lookup failure must NOT degrade to a partial read.

        Previously the exception was swallowed and resolution continued with
        whatever had been collected. That is unsafe for this specific data:
        restrictions computed from a subset of the grants are more PERMISSIVE
        than the truth (see `_merge_data_restrictions` — a missing restricted
        grant leaves `tableAllowlist`/`columnDenylist` unset, which the SQL
        firewall reads as unrestricted). So a transient DynamoDB error widened
        data access. Fail closed instead, matching the control-plane authorizer.
        """
        dao = MagicMock()
        dao.query_all.side_effect = RuntimeError("ddb down")
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=dao),
            pytest.raises(RuntimeError, match="ddb down"),
        ):
            resolve_profile("bob@x.com", [], namespace=_NS)

    def test_no_namespace_skips_data_restriction_merge(self):
        items = {"User::alice@x.com": [{"role": "platform-admin", "resourceId": "GLOBAL"}]}
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("alice@x.com", [], namespace="")
        assert resolved.global_roles == ["platform-admin"]
        assert resolved.table_allowlist is None

    def test_encodes_special_chars_in_principal_keys(self):
        """Special characters in the user id/groups must be URL-encoded when querying.

        Grants are written with URL-encoded principal keys, so the
        serve-layer resolver must query with the same encoding or grants for users
        like ``foo+123@x.com`` are silently unresolvable. Uses the shared
        ``sanitize_principal_key`` helper to stay in lockstep with the writers and
        the control-plane authorizer.
        """
        dao = _mock_dao({})
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=dao),
        ):
            resolve_profile("foo+123@x.com", ["group/eng"], namespace=_NS)

        queried_pks = [call.args[0].expression_values[":pk"] for call in dao.query_all.call_args_list]
        assert "User::foo%2B123@x.com" in queried_pks
        assert "Group::group%2Feng" in queried_pks

    def test_reads_every_page_of_grants(self):
        """Grants must be read with query_all, not a single 1 MB query page.

        A one-page read drops grants past the page boundary; when the dropped one
        carries the restrictions, the SQL firewall stops enforcing them.
        """
        dao = _mock_dao({"User::u": [{"role": "data-analyst", "resourceId": _NS}]})
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=dao),
        ):
            resolve_profile("u", [], namespace=_NS)

        dao.query_all.assert_called()
        dao.query.assert_not_called()


@pytest.mark.unit
class TestResolveProfileEmailFallback:
    """Cognito's JWT 'sub' is a UUID, but grants written by the control-plane API
    are keyed by email. resolve_profile must query BOTH so an email-keyed grant
    resolves on the authenticated (sub-keyed) serve path.

    These assert on resolved output rather than on the DAO's recorded calls: the
    seeded grant key IS the assertion, so a lookup key that is missing, raw
    instead of encoded, or duplicated changes the resolved roles observably.
    """

    def test_resolves_grants_under_both_sub_and_email_keys(self):
        """Distinct roles under each key — both must appear, proving both were queried."""
        items = {
            "User::uuid-1234": [{"role": "namespace-maintainer", "resourceId": _NS}],
            "User::alice@x.com": [{"role": "namespace-owner", "resourceId": _NS}],
        }
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("uuid-1234", [], namespace=_NS, email="alice@x.com")

        roles = {rr["role"] for rr in resolved.resource_roles}
        assert roles == {"namespace-maintainer", "namespace-owner"}

    def test_email_keyed_grant_resolves_for_uuid_sub(self):
        """The regression this fix targets: sub finds nothing, email finds the grant."""
        items = {"User::alice@x.com": [{"role": "namespace-owner", "resourceId": _NS}]}
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("uuid-1234", [], namespace=_NS, email="alice@x.com")

        assert {"role": "namespace-owner", "resourceUID": _NS} in resolved.resource_roles

    def test_email_lookup_is_encoded(self):
        """The email key must go through sanitize_principal_key like the sub key.

        The grant is seeded under the ENCODED key only, so a raw-key lookup
        resolves nothing.
        """
        items = {"User::foo%2B123@x.com": [{"role": "data-analyst", "resourceId": _NS}]}
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("uuid-1234", [], namespace=_NS, email="foo+123@x.com")

        assert {"role": "data-analyst", "resourceUID": _NS} in resolved.resource_roles

    def test_no_duplicate_roles_when_email_equals_user_id(self):
        """When user_id IS the email (no-JWT fallback), the key must not be queried
        twice — a second identical query would duplicate every resolved role.
        """
        items = {"User::alice@x.com": [{"role": "namespace-owner", "resourceId": _NS}]}
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("alice@x.com", [], namespace=_NS, email="alice@x.com")

        assert resolved.resource_roles == [{"role": "namespace-owner", "resourceUID": _NS}]

    def test_empty_email_does_not_query_bare_user_key(self):
        """An empty email must not produce a ``User::`` lookup that could collide
        with a malformed grant row.
        """
        items = {"User::": [{"role": "platform-admin", "resourceId": "GLOBAL"}]}
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("uuid-1234", [], namespace=_NS, email="")

        assert resolved.global_roles == []
        assert resolved.resource_roles == []

    def test_unknown_email_resolves_no_roles(self):
        """The second lookup must not become a blanket grant — an email with no
        grant rows still resolves to zero roles (Cedar then fails closed).
        """
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao({})),
        ):
            resolved = resolve_profile("uuid-1234", [], namespace=_NS, email="nobody@x.com")

        assert resolved.global_roles == []
        assert resolved.resource_roles == []

    def test_email_grant_for_other_namespace_is_not_applied(self):
        """An email-keyed grant on a different namespace must not leak into this one."""
        items = {"User::alice@x.com": [{"role": "namespace-owner", "resourceId": "other-ns"}]}
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao(items)),
        ):
            resolved = resolve_profile("uuid-1234", [], namespace=_NS, email="alice@x.com")

        # The role is resolved (it is a real grant) but scoped to other-ns, so
        # Cedar will deny for _NS. Nothing for _NS should appear.
        assert {"role": "namespace-owner", "resourceUID": "other-ns"} in resolved.resource_roles
        assert all(rr["resourceUID"] != _NS for rr in resolved.resource_roles)


@pytest.mark.unit
class TestMergeDataRestrictions:
    def _resolve(self, items):
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao({"User::u": items})),
        ):
            return resolve_profile("u", [], namespace=_NS)

    def test_table_allowlist_union(self):
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "tableAllowlist": ["orders"]},
                {"role": "data-analyst", "resourceId": _NS, "tableAllowlist": ["customers"]},
            ]
        )
        assert resolved.table_allowlist == ["customers", "orders"]

    def test_grant_without_allowlist_does_not_erase_one(self):
        # SEMANTICS CHANGE (was: a grant with no allowlist meant unrestricted).
        # Absence now contributes no constraint instead of removing every
        # constraint, so the declared allowlist stands. Otherwise adding a broader
        # role silently widened data access.
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "tableAllowlist": ["orders"]},
                {"role": "namespace-owner", "resourceId": _NS},  # no tableAllowlist key
            ]
        )
        assert resolved.table_allowlist == ["orders"]

    def test_allowed_metrics_union(self):
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "allowedMetrics": ["m1"]},
                {"role": "data-analyst", "resourceId": _NS, "allowedMetrics": ["m2"]},
            ]
        )
        assert resolved.allowed_metrics == ["m1", "m2"]

    def test_column_denylist_union(self):
        # SEMANTICS CHANGE (was: intersection — denied only if ALL grants denied).
        # A column is now denied if ANY grant denies it, so holding a second role
        # cannot un-deny a column the first role withheld.
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"orders": ["ssn", "email"]}},
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"orders": ["ssn"]}},
            ]
        )
        assert resolved.column_denylist == {"orders": ["email", "ssn"]}

    def test_grant_without_denylist_does_not_erase_one(self):
        # SEMANTICS CHANGE (was: any grant without a denylist meant nothing denied).
        # This was the sharpest edge of the old model: granting namespace-owner to
        # an analyst silently stopped their PII denylist from being enforced.
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"orders": ["ssn"]}},
                {"role": "namespace-owner", "resourceId": _NS},  # no columnDenylist key
            ]
        )
        assert resolved.column_denylist == {"orders": ["ssn"]}

    def test_no_matching_namespace_grants_leaves_restrictions_unset(self):
        resolved = self._resolve([{"role": "data-analyst", "resourceId": "other-ns", "tableAllowlist": ["orders"]}])
        assert resolved.table_allowlist is None
        assert resolved.column_denylist is None

    # ── GLOBAL grants must not relax namespace-scoped data restrictions ──────
    # A platform grant (platform-admin / platform-viewer) is written with
    # resourceId="GLOBAL" and carries no tableAllowlist/columnDenylist. It must
    # NOT enter the per-namespace restriction merge: the "grant without a
    # restriction means unrestricted" rule would otherwise erase the namespace
    # grant's restrictions and the SQL firewall would stop enforcing them.

    def test_global_grant_does_not_erase_column_denylist(self):
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"customers": ["ssn"]}},
                {"role": "platform-viewer", "resourceId": "GLOBAL"},  # no restriction fields
            ]
        )
        assert resolved.column_denylist == {"customers": ["ssn"]}

    def test_global_grant_does_not_erase_table_allowlist(self):
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "tableAllowlist": ["customers"]},
                {"role": "platform-viewer", "resourceId": "GLOBAL"},
            ]
        )
        assert resolved.table_allowlist == ["customers"]

    def test_global_grant_does_not_erase_allowed_metrics(self):
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "allowedMetrics": ["m1"]},
                {"role": "platform-viewer", "resourceId": "GLOBAL"},
            ]
        )
        assert resolved.allowed_metrics == ["m1"]

    def test_global_grant_still_resolves_into_global_roles(self):
        """Scoping the data merge must not affect Cedar-level platform privileges."""
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"customers": ["ssn"]}},
                {"role": "platform-viewer", "resourceId": "GLOBAL"},
            ]
        )
        assert resolved.global_roles == ["platform-viewer"]
        assert resolved.column_denylist == {"customers": ["ssn"]}


@pytest.mark.unit
class TestInjectInto:
    def test_inject_populates_all_fields(self):
        rp = ResolvedProfile(
            user_id="u@x.com",
            groups=["Admin"],
            global_roles=["platform-admin"],
            resource_roles=[{"role": "namespace-owner", "resourceUID": _NS}],
            table_allowlist=["orders"],
            column_denylist={"orders": ["ssn"]},
            allowed_metrics=["m1"],
        )
        profile: dict = {}
        rp.inject_into(profile)
        assert profile["userId"] == "u@x.com"
        assert profile["groups"] == ["Admin"]
        assert profile["globalRoles"] == ["platform-admin"]
        assert profile["resourceRoles"] == [{"role": "namespace-owner", "resourceUID": _NS}]
        assert profile["tableAllowlist"] == ["orders"]
        assert profile["columnDenylist"] == {"orders": ["ssn"]}
        assert profile["allowedMetrics"] == ["m1"]

    def test_inject_minimal_profile_only_sets_user_id(self):
        rp = ResolvedProfile(user_id="u@x.com")
        profile: dict = {}
        rp.inject_into(profile)
        assert profile["userId"] == "u@x.com"
        assert "globalRoles" not in profile
        assert "tableAllowlist" not in profile


@pytest.mark.unit
class TestRestrictionsAccumulate:
    """Data restrictions are constraints: they accumulate and never dissolve.

    Permissions compose additively (Cedar unions the actions two roles allow), but
    restrictions compose restrictively. Applying permission semantics to
    constraints is what let one unrestricted grant on a namespace erase a PII
    denylist attached to another — adding ``namespace-owner`` to an analyst
    silently stopped their ``columnDenylist`` from being enforced.
    """

    DENY_SSN = {"role": "data-analyst", "resourceId": "ns-a", "columnDenylist": {"customers": ["ssn"]}}
    DENY_DOB = {"role": "reviewer", "resourceId": "ns-a", "columnDenylist": {"customers": ["dob"]}}
    ALLOW_CUSTOMERS = {"role": "data-analyst", "resourceId": "ns-a", "tableAllowlist": ["customers"]}
    ALLOW_ORDERS = {"role": "reviewer", "resourceId": "ns-a", "tableAllowlist": ["orders"]}
    UNRESTRICTED_NS = {"role": "namespace-owner", "resourceId": "ns-a"}
    PLATFORM_ADMIN = {"role": "platform-admin", "resourceId": "GLOBAL"}

    @staticmethod
    def _resolve(items):
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao({"User::bob@x.com": items})),
        ):
            return role_resolver.resolve_profile("bob@x.com", [], namespace="ns-a")

    def test_unrestricted_grant_no_longer_erases_a_denylist(self):
        """The core change. Previously this returned no restrictions at all."""
        resolved = self._resolve([self.DENY_SSN, self.UNRESTRICTED_NS])

        assert resolved.column_denylist == {"customers": ["ssn"]}

    def test_denylists_union_across_grants(self):
        resolved = self._resolve([self.DENY_SSN, self.DENY_DOB])

        assert resolved.column_denylist == {"customers": ["dob", "ssn"]}

    def test_platform_admin_does_not_bypass(self):
        resolved = self._resolve([self.DENY_SSN, self.PLATFORM_ADMIN])

        assert resolved.column_denylist == {"customers": ["ssn"]}
        assert "platform-admin" in resolved.global_roles  # Cedar layer untouched

    def test_allowlists_union_rather_than_intersect(self):
        """Intersecting would deny both tables the principal was plainly granted."""
        resolved = self._resolve([self.ALLOW_CUSTOMERS, self.ALLOW_ORDERS])

        assert resolved.table_allowlist == ["customers", "orders"]

    def test_grant_without_an_allowlist_adds_no_constraint(self):
        """Absence contributes nothing; it must not remove the declared allowlist."""
        resolved = self._resolve([self.ALLOW_CUSTOMERS, self.UNRESTRICTED_NS])

        assert resolved.table_allowlist == ["customers"]

    def test_unrestricted_when_no_grant_declares_an_allowlist(self):
        resolved = self._resolve([self.DENY_SSN, self.UNRESTRICTED_NS])

        assert resolved.table_allowlist is None

    def test_explicitly_empty_allowlist_denies_all_tables(self):
        """An empty DECLARED list is an administrative choice, unlike absence."""
        resolved = self._resolve([{"role": "r", "resourceId": "ns-a", "tableAllowlist": []}])

        assert resolved.table_allowlist == []

    def test_other_namespaces_are_unaffected(self):
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=_mock_dao({"User::bob@x.com": [self.DENY_SSN]})),
        ):
            resolved = role_resolver.resolve_profile("bob@x.com", [], namespace="ns-OTHER")

        assert resolved.column_denylist is None
