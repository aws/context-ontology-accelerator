# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for grant resolution — the fail-closed security boundary."""

import threading
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
    email: str = "",
) -> CallerIdentity:
    return CallerIdentity(
        user_id=user_id,
        agent_id="test-agent",
        namespaces=namespaces if namespaces is not None else ["sales", "hr"],
        roles=roles if roles is not None else ["viewer"],
        raw_claims={},
        email=email,
    )


def _grant(role: str = "namespace-owner", resource_uid: str = "sales"):
    """Non-empty ResolvedProfile that passes the fail-closed grant check."""
    from coa_serve.role_resolver import ResolvedProfile as ServeProfile

    return ServeProfile(
        user_id="alice",
        groups=[],
        global_roles=[],
        resource_roles=[{"role": role, "resourceUID": resource_uid}],
    )


@pytest.mark.unit
class TestResolveGrant:
    """Grant resolution tests."""

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_user_with_grant_resolves(self, mock_resolve):
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales", "hr"])
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    async def test_jwt_namespaces_claim_when_present_still_blocks_cross_namespace(self):
        """A JWT carrying ``custom:namespaces`` still denies cross-namespace access before the RRM lookup."""
        caller = _make_caller(namespaces=["sales"])
        with pytest.raises(GrantResolutionError, match="does not have access"):
            await resolve_grant(caller, "forbidden-namespace")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_admin_group_with_empty_jwt_namespaces_allows(self, mock_resolve):
        """Admin-group callers bypass the empty-grants check even with no RRM rows."""
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        mock_resolve.return_value = ServeProfile(user_id="alice", groups=[])
        caller = _make_caller(namespaces=[], roles=["admin"])
        profile = await resolve_grant(caller, "any-namespace")
        assert profile.namespace == "any-namespace"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_human_token_without_jwt_namespaces_resolves_via_rrm(self, mock_resolve):
        """An RRM grant resolves a caller whose token has no ``custom:namespaces``."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=[], roles=["viewer"])
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_empty_rrm_grants_non_admin_denied(self, mock_resolve):
        """A caller with no grants on the namespace and no admin group is denied after the RRM lookup."""
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        # DDB returned nothing for this caller/namespace.
        mock_resolve.return_value = ServeProfile(user_id="alice", groups=[])
        caller = _make_caller(namespaces=[], roles=["viewer"])
        with pytest.raises(GrantResolutionError, match="no grants on namespace"):
            await resolve_grant(caller, "sales")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_cross_namespace_grant_does_not_satisfy_empty_check(self, mock_resolve):
        """A grant on ``sales`` must not satisfy the empty-grants check for ``hr``.

        ``resource_roles`` spans all the caller's namespaces, so the check filters
        by resourceUID. With ``cedar_action=None`` this check is the only gate.
        """
        mock_resolve.return_value = _grant(resource_uid="sales")
        caller = _make_caller(namespaces=[], roles=["viewer"])

        with pytest.raises(GrantResolutionError, match="no grants on namespace 'hr'"):
            await resolve_grant(caller, "hr")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_global_role_still_satisfies_empty_check(self, mock_resolve):
        """A ``GLOBAL``-scoped grant is cross-namespace by design and passes the empty-grants check."""
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        mock_resolve.return_value = ServeProfile(
            user_id="alice",
            groups=[],
            global_roles=["platform-viewer"],
            resource_roles=[],
        )
        caller = _make_caller(namespaces=[], roles=["viewer"])
        profile = await resolve_grant(caller, "hr")
        assert profile.namespace == "hr"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_email_is_forwarded_to_resolve_profile(self, mock_resolve):
        """``caller.email`` must reach ``resolve_profile`` because UI grants are keyed on email."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(user_id="uuid-1234", email="alice@example.com", namespaces=[])

        await resolve_grant(caller, "sales")

        mock_resolve.assert_called_once()
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs.get("email") == "alice@example.com"
        assert kwargs.get("user_id") == "uuid-1234"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_profile_dict_format(self, mock_resolve):
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")
        d = profile.to_profile_dict()
        assert d["namespace"] == "sales"
        assert "tableAllowlist" not in d
        assert "columnDenylist" not in d

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_data_source_ids_populated_from_allowed_metrics(self, mock_resolve):
        """data_source_ids must be populated from allowed_metrics for RAG filtering."""
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

        assert profile.data_source_ids == ["ds-marketing", "ds-public"]
        assert profile.table_allowlist == ["orders", "customers"]
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_unrestricted_stays_none_not_empty(self, mock_resolve):
        """None means unrestricted; [] is a deny-all restriction under the SQL
        firewall's empty-vs-absent semantics. Collapsing None to [] would tell a
        downstream enforcer that an unrestricted principal may use NO metrics —
        the two must stay distinct.
        """
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

        assert profile.data_source_ids is None
        # And the unset field is omitted from the forwarded profile entirely.
        assert "dataSourceIds" not in profile.to_profile_dict()

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_declared_empty_restriction_is_forwarded(self, mock_resolve):
        """A declared-empty (deny-all) restriction survives the profile round-trip."""
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        mock_resolve.return_value = ServeProfile(
            user_id="alice",
            groups=["viewer"],
            global_roles=[],
            resource_roles=[{"role": "data-analyst", "resourceUID": "sales"}],
            table_allowlist=[],  # deny-all
            column_denylist=None,
            allowed_metrics=None,
        )

        caller = _make_caller(user_id="alice", namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")

        d = profile.to_profile_dict()
        assert d["tableAllowlist"] == []
        assert "columnDenylist" not in d


@pytest.mark.unit
class TestResolvedProfile:
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
            "schemaVisibility": {},
        }

    def test_empty_profile_dict(self):
        profile = ResolvedProfile(namespace="ns1")
        d = profile.to_profile_dict()
        assert d["namespace"] == "ns1"
        assert d["userId"] == ""
        assert d["globalRoles"] == []
        assert d["resourceRoles"] == []
        assert "tableAllowlist" not in d
        assert "columnDenylist" not in d
        assert "dataSourceIds" not in d


@pytest.mark.unit
class TestCedarEvaluation:
    """Cedar policy evaluation in the MCP grant resolver."""

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_serve.role_resolver.resolve_profile")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_cedar_allows_authorized_action(self, mock_evaluate, mock_resolve):
        """Cedar allow result proceeds to profile resolution."""
        mock_evaluate.return_value = MagicMock(allowed=True)
        mock_resolve.return_value = _grant()

        with patch("coa_authorization.policy_loader.DynamoDBDAO") as mock_dao_cls:
            mock_dao = MagicMock()
            mock_dao.get.return_value = {"cedarPolicy": "permit(principal, action, resource);"}
            mock_dao_cls.return_value = mock_dao

            caller = _make_caller(namespaces=["sales"])
            profile = await resolve_grant(caller, "sales", cedar_action="query")
            assert profile.namespace == "sales"
            # The tool action first, then the two schema-view decisions on the same policies.
            assert mock_evaluate.call_args_list[0].kwargs["action"] == "query"

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_serve.role_resolver.resolve_profile")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_cedar_denies_unauthorized_action(self, mock_evaluate, mock_resolve):
        """Cedar deny result raises GrantResolutionError."""
        mock_evaluate.return_value = MagicMock(allowed=False)
        mock_resolve.return_value = _grant()

        with patch("coa_authorization.policy_loader.DynamoDBDAO") as mock_dao_cls:
            mock_dao = MagicMock()
            mock_dao.get.return_value = {"cedarPolicy": "forbid(principal, action, resource);"}
            mock_dao_cls.return_value = mock_dao

            caller = _make_caller(namespaces=["sales"])
            with pytest.raises(GrantResolutionError, match="Cedar policy denied"):
                await resolve_grant(caller, "sales", cedar_action="query")

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_cedar_no_policies_denies(self, mock_resolve):
        """No Cedar policies found fails closed."""
        mock_resolve.return_value = _grant()

        with patch("coa_authorization.policy_loader.DynamoDBDAO") as mock_dao_cls:
            mock_dao = MagicMock()
            mock_dao.get.return_value = None
            mock_dao_cls.return_value = mock_dao

            caller = _make_caller(namespaces=["sales"])
            with pytest.raises(GrantResolutionError, match="No Cedar policies"):
                await resolve_grant(caller, "sales", cedar_action="query")

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "")
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_cedar_action_without_roles_table_fails_closed(self, mock_resolve):
        """A cedar_action with ROLES_TABLE_NAME unset denies (fail-closed) instead of skipping the gate."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales"])
        with pytest.raises(GrantResolutionError, match="Authorization is temporarily unavailable"):
            await resolve_grant(caller, "sales", cedar_action="query")

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "")
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_no_cedar_action_skips_gate_even_without_roles_table(self, mock_resolve):
        """With NO cedar_action, evaluation is still skipped regardless
        of the roles table — only a required action triggers the fail-closed deny above."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    def test_tool_cedar_action_mapping_complete(self):
        """Every MCP tool has a Cedar action mapping."""
        expected_tools = {
            "list_metrics",
            "describe_schema",
            "query",
            "translate_sparql",
            "rag_retrieval",
            "graph_traversal",
        }
        assert set(TOOL_CEDAR_ACTION.keys()) == expected_tools

    def test_schema_blind_tool_mappings_are_pinned(self):
        """Pin describe_schema to viewOntology so a data-analyst cannot dump the ontology vocabulary."""
        assert TOOL_CEDAR_ACTION["describe_schema"] == "viewOntology"
        assert TOOL_CEDAR_ACTION["graph_traversal"] == "traverseGraph"
        assert TOOL_CEDAR_ACTION["translate_sparql"] == "query"


@pytest.mark.unit
class TestSchemaVisibilityDecisions:
    """resolve_grant decides viewSource/viewOntology from the same policy load as the tool action.

    The decisions are cached on the profile so downstream gates never reload policies.
    Each decision fails closed.
    """

    @staticmethod
    def _dao():
        dao = MagicMock()
        dao.get.return_value = {"cedarPolicy": "permit(principal, action, resource);"}
        return dao

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_serve.role_resolver.resolve_profile")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_policies_loaded_once_and_rights_cached(self, mock_evaluate, mock_resolve):
        mock_resolve.return_value = _grant()
        mock_evaluate.side_effect = lambda **kw: MagicMock(allowed=kw["action"] != "viewOntology")
        with patch("coa_authorization.policy_loader.load_policies_for_roles", return_value="p") as mock_load:
            profile = await resolve_grant(_make_caller(namespaces=["sales"]), "sales", cedar_action="query")

        mock_load.assert_called_once()
        assert [c.kwargs["action"] for c in mock_evaluate.call_args_list] == ["query", "viewSource", "viewOntology"]
        assert profile.schema_visibility == {"viewSource": True, "viewOntology": False}
        assert profile.to_profile_dict()["schemaVisibility"] == {"viewSource": True, "viewOntology": False}

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_serve.role_resolver.resolve_profile")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_policy_load_and_evaluation_run_off_the_event_loop(self, mock_evaluate, mock_resolve):
        """The blocking DynamoDB load and cedarpy calls run in a worker thread, not on the loop."""
        mock_resolve.return_value = _grant()
        loop_thread = threading.get_ident()
        threads: list[int] = []

        def load(**_kw):
            threads.append(threading.get_ident())
            return "p"

        def evaluate(**_kw):
            threads.append(threading.get_ident())
            return MagicMock(allowed=True)

        mock_evaluate.side_effect = evaluate
        with patch("coa_authorization.policy_loader.load_policies_for_roles", side_effect=load):
            profile = await resolve_grant(_make_caller(namespaces=["sales"]), "sales", cedar_action="query")

        assert len(threads) == 4  # one load + query + viewSource + viewOntology
        assert loop_thread not in threads
        assert profile.schema_visibility == {"viewSource": True, "viewOntology": True}

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_serve.role_resolver.resolve_profile")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_schema_view_error_fails_closed_without_failing_the_call(self, mock_evaluate, mock_resolve):
        """A schema-view evaluation error withholds schema; the tool call itself still runs."""
        mock_resolve.return_value = _grant()

        def evaluate(**kw):
            if kw["action"] == "query":
                return MagicMock(allowed=True)
            raise RuntimeError("cedar schema bug")

        mock_evaluate.side_effect = evaluate
        with patch("coa_authorization.policy_loader.DynamoDBDAO", return_value=self._dao()):
            profile = await resolve_grant(_make_caller(namespaces=["sales"]), "sales", cedar_action="query")
        assert profile.schema_visibility == {"viewSource": False, "viewOntology": False}

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "test-roles-table")
    @patch("coa_serve.role_resolver.resolve_profile")
    @patch("coa_mcp.auth.grant_resolver.cedar_evaluate")
    async def test_schema_blind_analyst(self, mock_evaluate, mock_resolve):
        mock_resolve.return_value = _grant(role="data-analyst")
        mock_evaluate.side_effect = lambda **kw: MagicMock(allowed=kw["action"] == "query")
        with patch("coa_authorization.policy_loader.DynamoDBDAO", return_value=self._dao()):
            profile = await resolve_grant(_make_caller(namespaces=["sales"]), "sales", cedar_action="query")
        assert profile.schema_visibility == {"viewSource": False, "viewOntology": False}

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "")
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_no_cedar_action_leaves_rights_empty(self, mock_resolve):
        """Without a cedar_action nothing is evaluated; consumers treat empty as withhold."""
        mock_resolve.return_value = _grant()
        profile = await resolve_grant(_make_caller(namespaces=["sales"]), "sales")
        assert profile.schema_visibility == {}


@pytest.mark.unit
class TestRoleResolutionFailure:
    """Behavior when the shared role_resolver raises."""

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
