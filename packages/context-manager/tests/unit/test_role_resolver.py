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
    """Return a DynamoDBDAO factory whose query() dispatches on principalKey.

    ``items_by_pk`` maps the ``:pk`` expression value to the list of grant items
    returned for that principal.
    """
    dao = MagicMock()

    def _query(params):
        pk = params.expression_values[":pk"]
        return _result(items_by_pk.get(pk, []))

    dao.query.side_effect = _query
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

    def test_query_exception_is_swallowed(self):
        dao = MagicMock()
        dao.query.side_effect = RuntimeError("ddb down")
        with (
            patch.object(role_resolver, "_RRM_TABLE", "rrm"),
            patch.object(role_resolver, "DynamoDBDAO", return_value=dao),
        ):
            resolved = resolve_profile("bob@x.com", [], namespace=_NS)
        # No roles resolved, but no exception propagated (fail-closed posture).
        assert resolved.global_roles == []
        assert resolved.resource_roles == []

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

        queried_pks = [call.args[0].expression_values[":pk"] for call in dao.query.call_args_list]
        assert "User::foo%2B123@x.com" in queried_pks
        assert "Group::group%2Feng" in queried_pks


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

    def test_unrestricted_grant_wins_over_allowlist(self):
        # A grant with no allowlist (None) means unrestricted table access.
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "tableAllowlist": ["orders"]},
                {"role": "namespace-owner", "resourceId": _NS},  # no tableAllowlist key
            ]
        )
        assert resolved.table_allowlist is None

    def test_allowed_metrics_union(self):
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "allowedMetrics": ["m1"]},
                {"role": "data-analyst", "resourceId": _NS, "allowedMetrics": ["m2"]},
            ]
        )
        assert resolved.allowed_metrics == ["m1", "m2"]

    def test_column_denylist_intersection(self):
        # A column is denied only if ALL grants deny it.
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"orders": ["ssn", "email"]}},
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"orders": ["ssn"]}},
            ]
        )
        assert resolved.column_denylist == {"orders": ["ssn"]}

    def test_column_denylist_none_means_nothing_denied(self):
        # If any grant has no columnDenylist, the user is unrestricted on columns.
        resolved = self._resolve(
            [
                {"role": "data-analyst", "resourceId": _NS, "columnDenylist": {"orders": ["ssn"]}},
                {"role": "namespace-owner", "resourceId": _NS},  # no columnDenylist key
            ]
        )
        assert resolved.column_denylist is None

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
