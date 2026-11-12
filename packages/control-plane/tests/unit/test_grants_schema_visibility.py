# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grant summaries withhold table/column overrides from schema-blind callers."""

from __future__ import annotations

import pytest
from coa_control_plane.grants.schema_visibility import (
    withhold_namespace_grant_overrides,
    withhold_principal_grant_overrides,
)

_GRANT = {
    "grantId": "g1",
    "namespaceId": "ns-1",
    "role": "data-analyst",
    "tableAllowlist": ["orders"],
    "columnDenylist": {"orders": ["ssn"]},
    "allowedMetrics": ["revenue"],
}


def _event(**ctx: str) -> dict:
    return {"requestContext": {"authorizer": {"userId": "u", **ctx}}}


@pytest.mark.unit
class TestNamespaceListing:
    def test_schema_blind_caller_gets_no_overrides(self):
        (g,) = withhold_namespace_grant_overrides([_GRANT], _event(canViewSource="false"))
        assert "tableAllowlist" not in g and "columnDenylist" not in g
        assert g["allowedMetrics"] == ["revenue"]  # metric names are not schema

    def test_source_viewer_keeps_overrides(self):
        assert withhold_namespace_grant_overrides([_GRANT], _event(canViewSource="true")) == [_GRANT]

    def test_flag_is_case_insensitive(self):
        assert withhold_namespace_grant_overrides([_GRANT], _event(canViewSource="TRUE")) == [_GRANT]

    def test_missing_flag_withholds(self):
        # Fail closed: only the metric read keeps a missing-flag exception.
        (g,) = withhold_namespace_grant_overrides([_GRANT], _event())
        assert "tableAllowlist" not in g and "columnDenylist" not in g
        assert g["allowedMetrics"] == ["revenue"]


@pytest.mark.unit
class TestPrincipalListing:
    def test_overrides_kept_only_where_caller_views_source(self):
        other = {**_GRANT, "grantId": "g2", "namespaceId": "ns-2"}
        out = withhold_principal_grant_overrides([_GRANT, other], _event(viewSourceNamespaces="ns-2"))
        assert "tableAllowlist" not in out[0]
        assert out[1]["tableAllowlist"] == ["orders"]

    def test_empty_list_withholds_everywhere(self):
        (g,) = withhold_principal_grant_overrides([_GRANT], _event(viewSourceNamespaces=""))
        assert "columnDenylist" not in g

    def test_platform_grant_follows_star(self):
        platform = {**_GRANT, "namespaceId": "GLOBAL"}
        assert withhold_principal_grant_overrides([platform], _event(viewSourceNamespaces="*")) == [platform]

    def test_platform_grant_stripped_without_star(self):
        platform = {**_GRANT, "namespaceId": "GLOBAL"}
        (g,) = withhold_principal_grant_overrides([platform], _event(viewSourceNamespaces="ns-1"))
        assert "tableAllowlist" not in g and "columnDenylist" not in g

    def test_star_makes_every_namespace_visible(self):
        # A platform-wide viewSource holder sees source schema in every namespace,
        # including ones absent from its resourceRoles candidate list.
        other = {**_GRANT, "grantId": "g2", "namespaceId": "ns-unlisted"}
        assert withhold_principal_grant_overrides([_GRANT, other], _event(viewSourceNamespaces="*")) == [
            _GRANT,
            other,
        ]

    def test_missing_key_withholds(self):
        (g,) = withhold_principal_grant_overrides([_GRANT], _event())
        assert "tableAllowlist" not in g and "columnDenylist" not in g
