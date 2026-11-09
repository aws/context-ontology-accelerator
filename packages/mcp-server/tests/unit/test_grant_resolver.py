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
    """Grant resolution tests — highest security value tests."""

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_user_with_grant_resolves(self, mock_resolve):
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales", "hr"])
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    async def test_jwt_namespaces_claim_when_present_still_blocks_cross_namespace(self):
        """When a JWT DOES carry ``custom:namespaces`` (agent tokens do), the
        advisory pre-check still denies cross-namespace access before the RRM
        lookup runs. This is a defense-in-depth check on top of the RRM check;
        an absent claim (see the human-token cases below) is not a denial."""
        caller = _make_caller(namespaces=["sales"])
        with pytest.raises(GrantResolutionError, match="does not have access"):
            await resolve_grant(caller, "forbidden-namespace")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_admin_group_with_empty_jwt_namespaces_allows(self, mock_resolve):
        """Admin-group callers still bypass the empty-grants check.

        Even with no RRM rows for the caller (mock returns an empty profile),
        the ``is_admin_group_member`` bypass in ``resolve_grant`` skips the
        empty-grants deny. Mocked explicitly so the test doesn't lean on
        ``RRM_TABLE_NAME`` being unset in the test env — production sets it,
        and without the mock we'd hit real DynamoDB.
        """
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        mock_resolve.return_value = ServeProfile(user_id="alice", groups=[])
        caller = _make_caller(namespaces=[], roles=["admin"])
        profile = await resolve_grant(caller, "any-namespace")
        assert profile.namespace == "any-namespace"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_human_token_without_jwt_namespaces_resolves_via_rrm(self, mock_resolve):
        """The bug this fix targets. Human Cognito access tokens never carry
        ``custom:namespaces`` — the old code denied every non-admin caller
        before the RRM grant table got a chance to speak. With the pre-filter
        gone, an RRM grant on the namespace resolves the caller normally."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=[], roles=["viewer"])
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_empty_rrm_grants_non_admin_denied(self, mock_resolve):
        """The fail-closed check moved from JWT claim to RRM: a caller with
        no grants on the namespace and no admin group is denied AFTER the
        DDB lookup, not before."""
        from coa_serve.role_resolver import ResolvedProfile as ServeProfile

        # DDB returned nothing for this caller/namespace.
        mock_resolve.return_value = ServeProfile(user_id="alice", groups=[])
        caller = _make_caller(namespaces=[], roles=["viewer"])
        with pytest.raises(GrantResolutionError, match="no grants on namespace"):
            await resolve_grant(caller, "sales")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_cross_namespace_grant_does_not_satisfy_empty_check(self, mock_resolve):
        """The correctness gap Kun Wang caught: ``resolve_profile.resource_roles``
        accumulates grants across every namespace the caller holds, not just
        the requested one. The empty-grants deny must filter by resourceUID
        first — otherwise a caller with ``namespace-owner`` on ``sales`` alone,
        requesting ``hr`` with ``cedar_action=None``, passes with the sales
        grant satisfying an "any grants at all" test and hits Cedar with a
        profile that has no bearing on hr.

        Cedar catches it today in prod (all six MCP tools pass
        ``cedar_action`` and ``ROLES_TABLE_NAME`` is set), but this layer is
        the last line of defense when ``cedar_action`` is None or the roles
        table isn't configured — both documented supported modes. Pin the
        namespace-scoped denial here so a future revert doesn't re-open the
        gap silently."""
        mock_resolve.return_value = _grant(resource_uid="sales")
        caller = _make_caller(namespaces=[], roles=["viewer"])

        with pytest.raises(GrantResolutionError, match="no grants on namespace 'hr'"):
            # Note: cedar_action=None by design — we're pinning the fail-closed
            # behavior of the RRM-emptiness check on its own, without Cedar.
            await resolve_grant(caller, "hr")

    @pytest.mark.asyncio
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_global_role_still_satisfies_empty_check(self, mock_resolve):
        """A ``GLOBAL``-scoped grant (platform-admin, platform-viewer) is
        cross-namespace by design and must still pass the empty-grants check
        even without a per-namespace resource_role. This is the sibling of
        the cross-namespace test above: filter resource_roles by namespace,
        but leave global_roles alone."""
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
        """The other half of the bug this fix targets. Grants written by the
        UI are keyed on the caller's email address (Cognito ``sub`` is a UUID
        users never see and never grant to). If the resolver only queries the
        ``sub`` key, those grants are invisible and every UI-onboarded user is
        denied with "No grants found". Assert that ``caller.email`` reaches
        ``resolve_profile`` so the shared helper queries both keys."""
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
        assert "namespace" in d
        assert d["namespace"] == "sales"
        # Unrestricted (None) restriction fields are OMITTED, not sent as empty
        # lists — an empty tableAllowlist now means deny-all to the SQL firewall.
        assert "tableAllowlist" not in d
        assert "columnDenylist" not in d

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
    async def test_unrestricted_stays_none_not_empty(self, mock_resolve):
        """SEMANTICS CHANGE (was: None collapsed to []).

        None means unrestricted; [] is a deny-all restriction under the SQL
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
            "schemaVisibility": {},
        }

    def test_empty_profile_dict(self):
        profile = ResolvedProfile(namespace="ns1")
        d = profile.to_profile_dict()
        assert d["namespace"] == "ns1"
        assert d["userId"] == ""
        assert d["globalRoles"] == []
        assert d["resourceRoles"] == []
        # Default = unrestricted (None) — restriction keys are omitted rather
        # than fabricated as empty (empty now means deny-all to the firewall).
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
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_cedar_skipped_when_no_action(self, mock_resolve):
        """No cedar_action means Cedar evaluation is skipped (backward compat)."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")
        assert profile.namespace == "sales"

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "")
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_cedar_action_without_roles_table_fails_closed(self, mock_resolve):
        """A required cedar_action with ROLES_TABLE_NAME unset now DENIES (fail-closed),
        rather than silently skipping the gate — matching the schema-view decisions, so the hard
        tool-gate and the output-suppression probe agree in this misconfiguration."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales"])
        with pytest.raises(GrantResolutionError, match="Authorization is temporarily unavailable"):
            await resolve_grant(caller, "sales", cedar_action="query")

    @pytest.mark.asyncio
    @patch("coa_mcp.auth.grant_resolver._ROLES_TABLE", "")
    @patch("coa_serve.role_resolver.resolve_profile")
    async def test_no_cedar_action_skips_gate_even_without_roles_table(self, mock_resolve):
        """With NO cedar_action (legacy callers), evaluation is still skipped regardless
        of the roles table — only a required action triggers the fail-closed deny above."""
        mock_resolve.return_value = _grant()
        caller = _make_caller(namespaces=["sales"])
        profile = await resolve_grant(caller, "sales")
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

    def test_schema_blind_tool_mappings_are_pinned(self):
        """describe_schema is HARD-denied to a schema-blind role via viewOntology;
        graph_traversal and translate_sparql stay on working-tool actions the
        analyst holds (their output is suppressed instead). Reverting
        describe_schema to viewNamespace would let a data-analyst dump the
        ontology vocabulary, so the exact values are pinned."""
        assert TOOL_CEDAR_ACTION["describe_schema"] == "viewOntology"
        assert TOOL_CEDAR_ACTION["graph_traversal"] == "traverseGraph"
        assert TOOL_CEDAR_ACTION["translate_sparql"] == "query"


@pytest.mark.unit
class TestSchemaVisibilityDecisions:
    """resolve_grant decides viewSource/viewOntology from the SAME policy load as the
    tool action and caches them on the profile (ADR: decompose-viewnamespace-
    schema-visibility), so the output-suppression gate and the metric flags never
    reload policies. Each decision fails closed."""

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
