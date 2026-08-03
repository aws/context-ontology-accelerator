# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for grant resolution — the fail-closed security boundary."""

from unittest.mock import MagicMock, patch

import pytest
from coa_mcp.auth.claims import CallerIdentity
from coa_mcp.auth.grant_resolver import (
    TOOL_CEDAR_ACTION,
    GrantResolutionError,
    ResolvedProfile,
    resolve_grant,
)


def _make_caller(
    user_id: str = "alice",
    namespaces: list[str] | None = None,
    roles: list[str] | None = None,
) -> CallerIdentity:
    return CallerIdentity(
        user_id=user_id,
        agent_id="test-agent",
        namespaces=namespaces if namespaces is not None else ["sales", "hr"],
        roles=roles if roles is not None else ["viewer"],
        raw_claims={},
    )


@pytest.mark.unit
class TestResolveGrant:
    """Grant resolution tests — highest security value tests."""

    @pytest.mark.asyncio
    async def test_user_with_namespace_access_resolves(self):
        caller = _make_caller(namespaces=["sales", "hr"])
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    async def test_user_without_namespace_access_denied(self):
        """CRITICAL: namespace scoping must deny cross-namespace access."""
        caller = _make_caller(namespaces=["sales"])
        with pytest.raises(GrantResolutionError, match="does not have access"):
            await resolve_grant(caller, "forbidden-namespace")

    @pytest.mark.asyncio
    async def test_empty_namespaces_admin_allows_all(self):
        """Admin users with empty namespace list can access any namespace."""
        caller = _make_caller(namespaces=[], roles=["admin"])
        profile = await resolve_grant(caller, "any-namespace")
        assert profile.namespace == "any-namespace"

    @pytest.mark.asyncio
    async def test_empty_namespaces_non_admin_denied(self):
        """Non-admin users with empty namespace list are denied (fail closed)."""
        caller = _make_caller(namespaces=[], roles=["viewer"])
        with pytest.raises(GrantResolutionError, match="no namespace grants"):
            await resolve_grant(caller, "any-namespace")

    @pytest.mark.asyncio
    async def test_profile_dict_format(self):
        caller = _make_caller(namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")
        d = profile.to_profile_dict()
        assert "namespace" in d
        assert "tableAllowlist" in d
        assert "columnDenylist" in d
        assert d["namespace"] == "sales"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_data_source_ids_populated_from_allowed_metrics(self, mock_resolve):
        """CRITICAL: data_source_ids must be populated from allowed_metrics for RAG filtering."""
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        mock_resolve.return_value = ServeProfile(
            user_id="alice",
            groups=["viewer"],
            global_roles=["data-viewer"],
            resource_roles=[{"role": "namespace-reader", "resourceUID": "sales"}],
            table_allowlist=["orders", "customers"],
            column_denylist={},
            allowed_metrics=["ds-marketing", "ds-public"],
        )

        caller = _make_caller(user_id="alice", namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")

        # Verify data_source_ids is populated from allowed_metrics
        assert profile.data_source_ids == ["ds-marketing", "ds-public"]
        assert profile.table_allowlist == ["orders", "customers"]
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_data_source_ids_empty_when_no_allowed_metrics(self, mock_resolve):
        """When allowed_metrics is None or empty, data_source_ids should be empty list."""
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        mock_resolve.return_value = ServeProfile(
            user_id="alice",
            groups=["viewer"],
            global_roles=["data-viewer"],
            resource_roles=[],
            table_allowlist=["orders"],
            column_denylist={},
            allowed_metrics=None,  # No restrictions
        )

        caller = _make_caller(user_id="alice", namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")

        # data_source_ids should be empty when allowed_metrics is None
        assert profile.data_source_ids == []


@pytest.mark.unit
class TestResolvedProfile:
    """Test the profile data structure."""

    def test_to_profile_dict(self):
        profile = ResolvedProfile(
            namespace="sales",
            user_id="user-123",
            global_roles=["platform-admin"],
            resource_roles=[{"role": "namespace-owner", "resourceUID": "sales"}],
            table_allowlist=["orders", "customers"],
            column_denylist=["ssn", "credit_card"],
            data_source_ids=["ds-001"],
        )
        d = profile.to_profile_dict()
        assert d == {
            "namespace": "sales",
            "userId": "user-123",
            "globalRoles": ["platform-admin"],
            "resourceRoles": [{"role": "namespace-owner", "resourceUID": "sales"}],
            "tableAllowlist": ["orders", "customers"],
            "columnDenylist": ["ssn", "credit_card"],
            "dataSourceIds": ["ds-001"],
        }

    def test_empty_profile_dict(self):
        profile = ResolvedProfile(namespace="ns1")
        d = profile.to_profile_dict()
        assert d["namespace"] == "ns1"
        assert d["userId"] == ""
        assert d["globalRoles"] == []
        assert d["resourceRoles"] == []
        assert d["tableAllowlist"] == []
        assert d["columnDenylist"] == {}


@pytest.mark.unit
class TestCedarEvaluation:
    """Cedar policy evaluation in the MCP grant resolver."""

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_cedar_allows_authorized_action(self, mock_evaluate):
        """Cedar allow result proceeds to profile resolution."""
        mock_evaluate.return_value = MagicMock(allowed=True)

        with patch("coa_authorization.policy_loader.DynamoDBDAO") as mock_dao_cls:
            mock_dao = MagicMock()
            mock_dao.get.return_value = {"cedarPolicy": "permit(principal, action, resource);"}
            mock_dao_cls.return_value = mock_dao

            caller = _make_caller(namespaces=["sales"])
            profile = await resolve_grant(caller, "sales", cedar_action="query")
            assert profile.namespace == "sales"
            mock_evaluate.assert_called_once()

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_cedar_denies_unauthorized_action(self, mock_evaluate):
        """Cedar deny result raises GrantResolutionError."""
        mock_evaluate.return_value = MagicMock(allowed=False)

        with patch("coa_authorization.policy_loader.DynamoDBDAO") as mock_dao_cls:
            mock_dao = MagicMock()
            mock_dao.get.return_value = {"cedarPolicy": "forbid(principal, action, resource);"}
            mock_dao_cls.return_value = mock_dao

            caller = _make_caller(namespaces=["sales"])
            with pytest.raises(GrantResolutionError, match="Cedar policy denied"):
                await resolve_grant(caller, "sales", cedar_action="query")

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    async def test_cedar_no_policies_denies(self):
        """No Cedar policies found fails closed."""
        with patch("coa_authorization.policy_loader.DynamoDBDAO") as mock_dao_cls:
            mock_dao = MagicMock()
            mock_dao.get.return_value = None
            mock_dao_cls.return_value = mock_dao

            caller = _make_caller(namespaces=["sales"])
            with pytest.raises(GrantResolutionError, match="No Cedar policies"):
                await resolve_grant(caller, "sales", cedar_action="query")

    @pytest.mark.asyncio
    async def test_cedar_skipped_when_no_action(self):
        """No cedar_action means Cedar evaluation is skipped (backward compat)."""
        caller = _make_caller(namespaces=["sales"])
        # Should succeed without any DDB call since cedar_action=None
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "")
    async def test_cedar_skipped_when_no_roles_table(self):
        """Empty ROLES_TABLE_NAME skips Cedar evaluation."""
        caller = _make_caller(namespaces=["sales"])
        profile = await resolve_grant(caller, "sales", cedar_action="query")
        assert profile.namespace == "sales"

    def test_tool_cedar_action_mapping_complete(self):
        """All 6 MCP tools have a Cedar action mapping."""
        expected_tools = {
            "list_metrics",
            "describe_schema",
            "query",
            "translate_sparql",
            "rag_retrieval",
            "graph_traversal",
        }
        assert set(TOOL_CEDAR_ACTION.keys()) == expected_tools


@pytest.mark.unit
class TestRoleResolutionFailure:
    """Behavior when the shared role_resolver raises."""

    # An admin caller used to get platform-admin + namespace-owner here, returned
    # EARLY — before the Cedar evaluation — off nothing but an "admin" entry in
    # their IdP groups. A failed grant read means we do not know what the caller
    # may do, which is never a reason to assume the most privileged answer. The
    # path also became reachable once resolve_profile stopped swallowing its query
    # errors, so a DynamoDB throttle would have escalated any admin-group caller.

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_admin_role_resolution_failure_raises(self, mock_resolve):
        """Admins fail closed too — no privilege escalation on a backend error."""
        mock_resolve.side_effect = RuntimeError("RRM table unavailable")
        caller = _make_caller(user_id="root", namespaces=["sales"], roles=["Admin"])

        with pytest.raises(GrantResolutionError, match="Failed to resolve grant"):
            await resolve_grant(caller, "sales")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_lowercase_admin_role_also_fails_closed(self, mock_resolve):
        mock_resolve.side_effect = RuntimeError("boom")
        caller = _make_caller(user_id="root", namespaces=["sales"], roles=["admin"])

        with pytest.raises(GrantResolutionError, match="Failed to resolve grant"):
            await resolve_grant(caller, "sales")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_failure_never_returns_a_profile_that_skips_cedar(self, mock_resolve):
        """The escalation was worse than its roles: it returned before Cedar ran."""
        mock_resolve.side_effect = RuntimeError("throttled")
        caller = _make_caller(user_id="root", namespaces=["sales"], roles=["Admin"])

        with (
            patch("coa_mcp.auth.grant_resolver._evaluate_cedar") as mock_cedar,
            pytest.raises(GrantResolutionError),
        ):
            await resolve_grant(caller, "sales")

        mock_cedar.assert_not_called()

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_non_admin_role_resolution_failure_raises(self, mock_resolve):
        """A non-admin caller whose profile can't be resolved is denied (fail closed)."""
        mock_resolve.side_effect = RuntimeError("RRM table unavailable")
        caller = _make_caller(user_id="alice", namespaces=["sales"], roles=["viewer"])

        with pytest.raises(GrantResolutionError, match="Failed to resolve grant"):
            await resolve_grant(caller, "sales")
