# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the API Gateway authorizer handler."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_common.auth import TokenType, TokenValidationResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_handler_state():
    """Reset module-level singletons between tests."""
    import coa_control_plane.authorization.handler as h

    h._token_authorizer = None
    h._cache_invalidation_dao = None
    h._roles_cache.clear()
    h._local_cache_version = 0
    h._namespaces_dao = None
    yield


@pytest.fixture()
def env_vars(monkeypatch):
    monkeypatch.setenv("JWKS_ISSUER", "https://login.corp.com")
    monkeypatch.setenv("JWKS_URI", "https://login.corp.com/.well-known/jwks.json")
    monkeypatch.setenv("CLIENT_ID", "my-client")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("RESOURCE_ROLE_MAPPINGS_TABLE_NAME", "coa-resource-role-mappings")
    monkeypatch.setenv("ROLES_TABLE_NAME", "coa-roles")
    monkeypatch.setenv("CACHE_INVALIDATION_TABLE_NAME", "coa-cache-invalidation")
    monkeypatch.setenv("NAMESPACES_TABLE_NAME", "coa-namespaces")


@pytest.fixture()
def valid_result() -> TokenValidationResult:
    return TokenValidationResult(
        principal_id="alice@example.com",
        sub="user-123",
        email="alice@example.com",
        groups=["Admins"],
        token_type=TokenType.JWT,
        claims={"sub": "user-123", "email": "alice@example.com"},
    )


@pytest.fixture()
def apigw_event() -> dict[str, Any]:
    return {
        "type": "TOKEN",
        "authorizationToken": "Bearer valid-jwt-token",
        "methodArn": "arn:aws:execute-api:us-east-1:123456789:api-id/stage/GET/namespaces",
    }


class TestHandler:
    @patch("coa_control_plane.authorization.handler._check_cache_version")
    @patch("coa_control_plane.authorization.handler._evaluate_cedar", return_value=True)
    @patch("coa_control_plane.authorization.handler._resolve_roles", return_value=(["ADMIN"], []))
    @patch("coa_control_plane.authorization.handler.DynamoDBDAO")
    def test_allow_on_valid_token(
        self, mock_dao_cls, mock_resolve, mock_cedar, mock_cache_check, env_vars, valid_result, apigw_event
    ):
        from coa_control_plane.authorization.handler import handler

        with patch("coa_control_plane.authorization.handler._get_token_authorizer") as mock_get_auth:
            mock_authorizer = MagicMock()
            mock_authorizer.validate.return_value = valid_result
            mock_get_auth.return_value = mock_authorizer

            result = handler(apigw_event, None)

        assert result["principalId"] == "alice@example.com"
        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
        assert result["context"]["userId"] == "alice@example.com"
        assert result["context"]["sub"] == "user-123"
        assert result["context"]["tokenType"] == "JWT"

    @patch("coa_control_plane.authorization.handler._check_cache_version")
    def test_deny_on_invalid_token(self, mock_cache_check, env_vars, apigw_event):
        from coa_control_plane.authorization.handler import handler

        with patch("coa_control_plane.authorization.handler._get_token_authorizer") as mock_get_auth:
            mock_authorizer = MagicMock()
            mock_authorizer.validate.side_effect = ValueError("Invalid token")
            mock_get_auth.return_value = mock_authorizer

            result = handler(apigw_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"

    @patch("coa_control_plane.authorization.handler._check_cache_version")
    def test_deny_on_empty_principal(self, mock_cache_check, env_vars, apigw_event):
        from coa_control_plane.authorization.handler import handler

        empty_result = TokenValidationResult(
            principal_id="",
            sub="",
            email="",
            groups=[],
            token_type=TokenType.JWT,
            claims={},
        )

        with patch("coa_control_plane.authorization.handler._get_token_authorizer") as mock_get_auth:
            mock_authorizer = MagicMock()
            mock_authorizer.validate.return_value = empty_result
            mock_get_auth.return_value = mock_authorizer

            result = handler(apigw_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


class TestCacheInvalidation:
    @patch("coa_control_plane.authorization.handler.DynamoDBDAO")
    def test_role_cache_cleared_on_version_bump(self, mock_dao_cls, env_vars):
        import coa_control_plane.authorization.handler as h
        from coa_control_plane.authorization.handler import _check_cache_version, _roles_cache

        _roles_cache["some-key"] = (["ADMIN"], [])
        h._local_cache_version = 1

        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": "CACHE_VERSION", "SK": "CURRENT", "version": 2}
        mock_dao_cls.return_value = mock_dao
        h._cache_invalidation_dao = mock_dao

        _check_cache_version()

        assert len(_roles_cache) == 0
        assert h._local_cache_version == 2

    @patch("coa_control_plane.authorization.handler.DynamoDBDAO")
    def test_role_cache_not_cleared_when_version_matches(self, mock_dao_cls, env_vars):
        import coa_control_plane.authorization.handler as h
        from coa_control_plane.authorization.handler import _check_cache_version, _roles_cache

        _roles_cache["some-key"] = (["ADMIN"], [])
        h._local_cache_version = 5

        mock_dao = MagicMock()
        mock_dao.get.return_value = {"PK": "CACHE_VERSION", "SK": "CURRENT", "version": 5}
        mock_dao_cls.return_value = mock_dao
        h._cache_invalidation_dao = mock_dao

        _check_cache_version()

        assert len(_roles_cache) == 1


class TestRolesCaching:
    """Tests for _resolve_roles cache behavior.

    NOTE: role cache is currently DISABLED pending security review (MR !622).
    These tests verify the disabled state — cache is never populated.
    Re-enable assertions when MR !620 lands.
    """

    def test_cache_miss_queries_ddb_and_populates_cache(self, env_vars):
        from coa_control_plane.authorization.handler import _resolve_roles, _roles_cache

        mock_dao = MagicMock()
        mock_dao.query_all.side_effect = [
            [{"role": "ADMIN", "resourceId": "GLOBAL"}],
            [{"role": "data-steward", "resourceId": "ns-123"}],
        ]

        global_roles, resource_roles = _resolve_roles("user@example.com", ["Admins"], mock_dao)

        assert global_roles == ["ADMIN"]
        assert resource_roles == [{"role": "data-steward", "resourceUID": "ns-123"}]
        # Cache population disabled — verify cache remains empty
        cache_key = "user@example.com:Admins"
        assert cache_key not in _roles_cache

    def test_principal_id_with_special_characters_is_url_encoded(self, env_vars):
        """Verify that principal IDs with special characters are URL-encoded when querying.

        This matches the encoding used when creating grants, ensuring that users with
        special characters (like ``+``) in their email can have their grants resolved.
        """
        from coa_control_plane.authorization.handler import _resolve_roles

        mock_dao = MagicMock()
        mock_dao.query_all.return_value = []

        _resolve_roles("foo+123@amazon.com", [], mock_dao)

        # Verify the query was made with URL-encoded principal ID
        query_call = mock_dao.query_all.call_args_list[0]
        query_params = query_call[0][0]
        assert query_params.expression_values[":pk"] == "User::foo%2B123@amazon.com"

    def test_cache_hit_skips_ddb_query(self, env_vars):
        from coa_control_plane.authorization.handler import _resolve_roles, _roles_cache

        cache_key = "cached-user@example.com:GroupA"
        cached_value = (["VIEWER"], [{"role": "data-analyst", "resourceUID": "ns-456"}])
        _roles_cache[cache_key] = cached_value

        mock_dao = MagicMock()
        global_roles, resource_roles = _resolve_roles("cached-user@example.com", ["GroupA"], mock_dao)

        assert global_roles == ["VIEWER"]
        assert resource_roles == [{"role": "data-analyst", "resourceUID": "ns-456"}]
        mock_dao.query_all.assert_not_called()

    def test_cache_key_groups_sorted(self, env_vars):
        from coa_control_plane.authorization.handler import _resolve_roles, _roles_cache

        mock_dao = MagicMock()
        mock_dao.query_all.return_value = []

        _resolve_roles("user@example.com", ["GroupB", "GroupA"], mock_dao)

        # Cache population disabled — verify cache not written
        assert "user@example.com:GroupA,GroupB" not in _roles_cache

    def test_corrupt_cache_entry_evicted_and_requeried(self, env_vars):
        """A corrupt/legacy cache entry should be evicted and the query re-run."""
        from coa_control_plane.authorization.handler import _resolve_roles, _roles_cache

        # Simulate a corrupt entry (wrong type — e.g. leftover from prior code)
        cache_key = "user@example.com:GroupX"
        _roles_cache[cache_key] = "garbage"  # type: ignore[assignment]

        mock_dao = MagicMock()
        # User query returns a role, group query returns nothing
        mock_dao.query_all.side_effect = [
            [{"role": "VIEWER", "resourceId": "GLOBAL"}],
            [],
        ]

        global_roles, resource_roles = _resolve_roles("user@example.com", ["GroupX"], mock_dao)

        # Should have queried DDB since cache was corrupt
        assert mock_dao.query_all.called
        assert global_roles == ["VIEWER"]
        # Cache population disabled — entry should not exist after eviction
        assert cache_key not in _roles_cache


class TestGetTokenAuthorizer:
    def test_cognito_detected(self, monkeypatch):
        monkeypatch.setenv("JWKS_ISSUER", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123")
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        from coa_common.auth import CognitoTokenAuthorizer
        from coa_control_plane.authorization.handler import _get_token_authorizer

        authorizer = _get_token_authorizer()
        assert isinstance(authorizer, CognitoTokenAuthorizer)

    def test_oidc_with_custom_group_claim(self, monkeypatch):
        monkeypatch.setenv("JWKS_ISSUER", "https://login.okta.com/oauth2/default")
        monkeypatch.setenv("JWKS_URI", "https://login.okta.com/oauth2/default/v1/keys")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("GROUP_CLAIM_NAME", "memberOf")

        from coa_common.auth import OIDCTokenAuthorizer
        from coa_control_plane.authorization.handler import _get_token_authorizer

        authorizer = _get_token_authorizer()
        assert isinstance(authorizer, OIDCTokenAuthorizer)
        assert authorizer._group_claim_name == "memberOf"


class TestDevModeDenial:
    """Without ROLES_TABLE_NAME, requests are denied unless ALLOW_WITHOUT_ROLES=true."""

    @patch("coa_control_plane.authorization.handler._check_cache_version")
    @patch("coa_control_plane.authorization.handler.DynamoDBDAO")
    def test_deny_without_roles_table_by_default(
        self, mock_dao_cls, mock_cache_check, valid_result, apigw_event, monkeypatch
    ):
        monkeypatch.setenv("JWKS_ISSUER", "https://login.corp.com")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("RESOURCE_ROLE_MAPPINGS_TABLE_NAME", "")
        monkeypatch.delenv("ROLES_TABLE_NAME", raising=False)
        monkeypatch.delenv("ALLOW_WITHOUT_ROLES", raising=False)

        from coa_control_plane.authorization.handler import handler

        with patch("coa_control_plane.authorization.handler._get_token_authorizer") as mock_get_auth:
            mock_authorizer = MagicMock()
            mock_authorizer.validate.return_value = valid_result
            mock_get_auth.return_value = mock_authorizer

            result = handler(apigw_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"

    @patch("coa_control_plane.authorization.handler._check_cache_version")
    @patch("coa_control_plane.authorization.handler.DynamoDBDAO")
    def test_allow_with_explicit_dev_mode(self, mock_dao_cls, mock_cache_check, valid_result, apigw_event, monkeypatch):
        monkeypatch.setenv("JWKS_ISSUER", "https://login.corp.com")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("RESOURCE_ROLE_MAPPINGS_TABLE_NAME", "")
        monkeypatch.delenv("ROLES_TABLE_NAME", raising=False)
        monkeypatch.setenv("ALLOW_WITHOUT_ROLES", "true")

        from coa_control_plane.authorization.handler import handler

        with patch("coa_control_plane.authorization.handler._get_token_authorizer") as mock_get_auth:
            mock_authorizer = MagicMock()
            mock_authorizer.validate.return_value = valid_result
            mock_get_auth.return_value = mock_authorizer

            result = handler(apigw_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"


class TestRoleResolutionErrorHandling:
    @patch("coa_control_plane.authorization.handler._check_cache_version")
    @patch("coa_control_plane.authorization.handler._resolve_roles", side_effect=Exception("DDB throttle"))
    @patch("coa_control_plane.authorization.handler.DynamoDBDAO")
    def test_deny_on_role_resolution_failure(
        self, mock_dao_cls, mock_resolve, mock_cache_check, env_vars, valid_result, apigw_event
    ):
        from coa_control_plane.authorization.handler import handler

        with patch("coa_control_plane.authorization.handler._get_token_authorizer") as mock_get_auth:
            mock_authorizer = MagicMock()
            mock_authorizer.validate.return_value = valid_result
            mock_get_auth.return_value = mock_authorizer

            result = handler(apigw_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


class TestCedarEvaluationErrorHandling:
    @patch("coa_control_plane.authorization.handler._check_cache_version")
    @patch("coa_control_plane.authorization.handler._evaluate_cedar", side_effect=Exception("Cedar error"))
    @patch("coa_control_plane.authorization.handler._resolve_roles", return_value=(["ADMIN"], []))
    @patch("coa_control_plane.authorization.handler.DynamoDBDAO")
    def test_deny_on_cedar_failure(
        self, mock_dao_cls, mock_resolve, mock_cedar, mock_cache_check, env_vars, valid_result, apigw_event
    ):
        from coa_control_plane.authorization.handler import handler

        with patch("coa_control_plane.authorization.handler._get_token_authorizer") as mock_get_auth:
            mock_authorizer = MagicMock()
            mock_authorizer.validate.return_value = valid_result
            mock_get_auth.return_value = mock_authorizer

            result = handler(apigw_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"


class TestKeySanitization:
    def test_sanitize_key_encodes_hash(self):
        from coa_common import sanitize_principal_key

        assert sanitize_principal_key("user@example.com") == "user@example.com"
        assert sanitize_principal_key("user#tag") == "user%23tag"
        assert sanitize_principal_key("a#b#c") == "a%23b%23c"

    def test_sanitize_key_no_collision(self):
        from coa_common import sanitize_principal_key

        assert sanitize_principal_key("user%23name") != sanitize_principal_key("user#name")


# ── Authoritative route → (action, resourceType) coverage ──────────────────
#
# This table mirrors the operations registered on the ControlPlaneService
# Smithy service (models/src/main/smithy/control-plane.smithy) — the only API
# behind this Lambda authorizer. Every secured operation MUST appear here with
# its intended Cedar action so the authorizer never falls through to denyAll.
# `/health` is intentionally excluded (it is @optionalAuth / unsecured).
#
# (HTTP method, API Gateway resource template, expected Cedar action)
CONTROL_PLANE_ROUTES: list[tuple[str, str, str]] = [
    # Namespace management
    ("GET", "/namespaces", "list"),
    ("POST", "/namespaces", "manageNamespace"),
    ("GET", "/namespaces/{namespaceId}", "viewNamespace"),
    ("PUT", "/namespaces/{namespaceId}", "manageNamespace"),
    ("DELETE", "/namespaces/{namespaceId}", "deleteNamespace"),
    ("PATCH", "/namespaces/{namespaceId}/status", "setNamespaceStatus"),
    # Role management (read-only)
    ("GET", "/roles", "list"),
    ("GET", "/namespaces/{namespaceId}/roles", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/roles/{roleId}", "viewNamespace"),
    # Grants (namespace-scoped)
    ("POST", "/namespaces/{namespaceId}/grants", "grantPermissions"),
    ("GET", "/namespaces/{namespaceId}/grants", "viewNamespace"),
    ("DELETE", "/namespaces/{namespaceId}/grants/{grantId}", "grantPermissions"),
    ("GET", "/principals/{principalId}/grants", "list"),
    # Grants (platform-scoped)
    ("POST", "/grants", "grantPermissions"),
    ("GET", "/grants", "viewNamespace"),
    ("DELETE", "/grants/{grantId}", "grantPermissions"),
    # Unified sources — reads
    ("GET", "/namespaces/{namespaceId}/sources", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/sources/{sourceId}", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/sources/{sourceId}/tables", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/sources/{sourceId}/scan/{jobId}", "viewNamespace"),
    # Unified sources — writes / management
    ("POST", "/namespaces/{namespaceId}/sources", "manageSource"),
    ("POST", "/namespaces/{namespaceId}/sources/upload-urls", "manageSource"),
    ("DELETE", "/namespaces/{namespaceId}/sources/{sourceId}", "manageSource"),
    ("POST", "/namespaces/{namespaceId}/sources/{sourceId}/rescan", "manageSource"),
    ("PUT", "/namespaces/{namespaceId}/sources/{sourceId}/metadata", "manageSource"),
    ("POST", "/namespaces/{namespaceId}/sources/{sourceId}/approve", "manageSource"),
    ("POST", "/namespaces/{namespaceId}/sources/{sourceId}/reject", "manageSource"),
    ("PUT", "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review", "manageSource"),
    ("PATCH", "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/metadata", "manageSource"),
    (
        "PUT",
        "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/review",
        "manageSource",
    ),
    (
        "PATCH",
        "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/columns/{columnName}/metadata",
        "manageSource",
    ),
    ("PATCH", "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/keys", "manageSource"),
    # Metric service — reads
    ("GET", "/namespaces/{namespaceId}/metrics", "readMetric"),
    ("GET", "/namespaces/{namespaceId}/metrics/{name}", "readMetric"),
    ("GET", "/namespaces/{namespaceId}/export-osi", "readMetric"),
    ("GET", "/namespaces/{namespaceId}/import-jobs/{jobId}", "readMetric"),
    # Metric service — authoring / writes
    ("POST", "/namespaces/{namespaceId}/metrics", "manageMetric"),
    ("PUT", "/namespaces/{namespaceId}/metrics/{name}", "manageMetric"),
    ("DELETE", "/namespaces/{namespaceId}/metrics/{name}", "manageMetric"),
    ("POST", "/namespaces/{namespaceId}/metrics/validate", "manageMetric"),
    ("POST", "/namespaces/{namespaceId}/bulk-delete-metrics", "manageMetric"),
    ("POST", "/namespaces/{namespaceId}/import-osi", "manageMetric"),
    ("POST", "/namespaces/{namespaceId}/import-osi/upload-url", "manageMetric"),
    # Ontology induction
    ("POST", "/namespaces/{namespaceId}/induce", "manageOntology"),
    ("GET", "/namespaces/{namespaceId}/induce/jobs", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/induce/jobs/{jobId}", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/induce/datasources/induced", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/proposals", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/proposals/{proposalId}", "viewNamespace"),
    ("POST", "/namespaces/{namespaceId}/proposals/{proposalId}/accept", "manageOntology"),
    ("POST", "/namespaces/{namespaceId}/proposals/{proposalId}/cancel", "manageOntology"),
    ("POST", "/namespaces/{namespaceId}/proposals/{proposalId}/validate", "manageOntology"),
    ("GET", "/namespaces/{namespaceId}/proposals/{proposalId}/validate/jobs/{jobId}", "viewNamespace"),
    ("POST", "/namespaces/{namespaceId}/proposals/update", "manageOntology"),
    ("POST", "/namespaces/{namespaceId}/proposals/reject", "manageOntology"),
    # Ontology graph + catalog
    ("GET", "/namespaces/{namespaceId}/ontologies", "viewNamespace"),
    ("DELETE", "/namespaces/{namespaceId}/ontologies", "manageOntology"),
    ("GET", "/namespaces/{namespaceId}/graph/search", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/graph/class", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/graph/object-property", "viewNamespace"),
    ("GET", "/namespaces/{namespaceId}/graph/datatype-property", "viewNamespace"),
]


class TestMapRequestToCedar:
    """Covers _map_request_to_cedar for the live ControlPlaneService routes."""

    def test_namespace_status_patch(self):
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, rtype, rid = _map_request_to_cedar("PATCH", "/namespaces/{namespaceId}/status", {"namespaceId": "ns-1"})
        # Status changes use a dedicated action (distinct from manageNamespace)
        # so the ARCHIVED mutation guard can still permit reactivation.
        assert (action, rtype, rid) == ("setNamespaceStatus", "Namespace", "ns-1")

    def test_namespace_roles_get(self):
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, rtype, rid = _map_request_to_cedar("GET", "/namespaces/{namespaceId}/roles", {"namespaceId": "ns-1"})
        assert (action, rtype, rid) == ("viewNamespace", "Namespace", "ns-1")

    def test_namespace_grants_post(self):
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, rtype, rid = _map_request_to_cedar("POST", "/namespaces/{namespaceId}/grants", {"namespaceId": "ns-1"})
        assert (action, rtype, rid) == ("grantPermissions", "Namespace", "ns-1")

    def test_global_roles_get(self):
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, rtype, rid = _map_request_to_cedar("GET", "/roles", {})
        assert (action, rtype, rid) == ("list", "Namespace", "*")

    def test_principal_grants_get(self):
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, rtype, rid = _map_request_to_cedar("GET", "/principals/{principalId}/grants", {"principalId": "user-1"})
        assert (action, rtype, rid) == ("list", "Namespace", "*")

    def test_unmapped_route_defaults_to_deny(self):
        """Unmapped routes fall through to ``denyAll`` so a new endpoint that
        hasn't been registered fails closed instead of inheriting read access."""
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, rtype, rid = _map_request_to_cedar("GET", "/unknown/route", {})
        assert (action, rtype, rid) == ("denyAll", "Namespace", "*")

    def test_source_create_is_manage_not_edit_namespace(self):
        """Regression: source writes must map to a Cedar action that exists in
        the schema (manageSource), never the non-existent 'editNamespace'."""
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, _rtype, _rid = _map_request_to_cedar(
            "POST", "/namespaces/{namespaceId}/sources", {"namespaceId": "ns-1"}
        )
        assert action == "manageSource"

    def test_metric_create_mapped(self):
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        action, _rtype, _rid = _map_request_to_cedar(
            "POST", "/namespaces/{namespaceId}/metrics", {"namespaceId": "ns-1"}
        )
        assert action == "manageMetric"

    @pytest.mark.parametrize(
        ("method", "path", "expected_type"),
        [
            ("GET", "/namespaces/{namespaceId}", "Namespace"),
            ("GET", "/roles", "Namespace"),
            ("POST", "/namespaces/{namespaceId}/grants", "Namespace"),
            ("POST", "/namespaces/{namespaceId}/sources", "Source"),
            ("GET", "/namespaces/{namespaceId}/sources/{sourceId}", "Source"),
            ("PUT", "/namespaces/{namespaceId}/sources/{sourceId}/tables/{tableId}/review", "Source"),
            ("POST", "/namespaces/{namespaceId}/metrics", "Metric"),
            ("GET", "/namespaces/{namespaceId}/metrics/{name}", "Metric"),
            ("POST", "/namespaces/{namespaceId}/proposals/{proposalId}/accept", "OntologyProposal"),
            ("GET", "/namespaces/{namespaceId}/proposals", "OntologyProposal"),
            ("POST", "/namespaces/{namespaceId}/induce", "Ontology"),
            ("GET", "/namespaces/{namespaceId}/graph/search", "Ontology"),
            ("GET", "/namespaces/{namespaceId}/ontologies", "Ontology"),
        ],
    )
    def test_routes_use_typed_resources(self, method: str, path: str, expected_type: str):
        """Routes evaluate against their proper Cedar resource type rather than
        always using ``Namespace`` (sources→Source, metrics→Metric,
        proposals→OntologyProposal, ontology graph/induction→Ontology)."""
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        _action, rtype, _rid = _map_request_to_cedar(method, path, {"namespaceId": "ns-1"})
        assert rtype == expected_type

    @pytest.mark.parametrize(("method", "path", "expected_action"), CONTROL_PLANE_ROUTES)
    def test_every_control_plane_route_is_mapped(self, method: str, path: str, expected_action: str):
        """Every secured ControlPlaneService operation maps to its intended
        Cedar action and never falls through to ``denyAll``."""
        from coa_control_plane.authorization.handler import _map_request_to_cedar

        # Provide a namespaceId path param when the template requires one.
        path_params = {"namespaceId": "ns-1"} if "{namespaceId}" in path else {}
        action, _rtype, _rid = _map_request_to_cedar(method, path, path_params)
        assert action != "denyAll", f"{method} {path} fell through to denyAll"
        assert action == expected_action, f"{method} {path} mapped to {action}, expected {expected_action}"

    def test_no_mapped_action_is_outside_cedar_schema(self):
        """Every action referenced by the path mapping must be a real Cedar
        action defined in cedar-schema.json (guards against typos like the
        former 'editNamespace')."""
        import json
        from pathlib import Path

        # coa_authorization lives in libs/common (shared by control-plane + serve);
        # resolve the schema from the installed package rather than a repo path.
        import coa_authorization
        from coa_control_plane.authorization.handler import _PATH_MAPPING

        schema_path = Path(coa_authorization.__file__).resolve().parent / "cedar-schema.json"
        schema = json.loads(schema_path.read_text())
        valid_actions = set(schema["SCL"]["actions"].keys())

        used_actions = {action for methods in _PATH_MAPPING.values() for action, _ in methods.values()}
        unknown = used_actions - valid_actions
        assert unknown == set(), f"Path mapping references actions not in Cedar schema: {unknown}"


# ── End-to-end: handler evaluation populates resource.namespace ─────────────


class _FakeRolesDao:
    """Returns the on-disk seed Cedar policy for ROLE#<name> lookups."""

    def __init__(self):
        from coa_authorization.policy_evaluator import load_seed_policy

        # Map role id → seed policy file name.
        self._role_to_seed = {
            "default": "default",
            "platform-admin": "global_admin",
            "platform-viewer": "global_viewer",
            "namespace-owner": "namespace_owner",
            "namespace-maintainer": "namespace_maintainer",
            "data-steward": "namespace_data_steward",
            "data-analyst": "namespace_data_analyst",
        }
        self._load = load_seed_policy

    def get(self, key: dict) -> dict | None:
        # Only the GLOBAL / NAMESPACE_TEMPLATE partitions are queried; the role
        # id is encoded in the SK as ROLE#<id>. Return the seed policy for any
        # known role regardless of partition (mirrors deploy-time seeding).
        sk = key.get("SK", "")
        role_id = sk.removeprefix("ROLE#")
        seed = self._role_to_seed.get(role_id)
        if seed is None:
            return None
        return {"cedarPolicy": self._load(seed)}


class TestEvaluateCedarResourceNamespace:
    """_evaluate_cedar must populate ``resource.namespace`` so that namespace-
    scoped policies (data-steward, data-analyst) match in production — not just
    in unit tests that pass resource_attrs explicitly."""

    NS = "11111111-1111-1111-1111-111111111111"
    OTHER_NS = "22222222-2222-2222-2222-222222222222"

    def _eval(self, action: str, resource_id: str, global_roles=None, resource_roles=None) -> bool:
        from coa_control_plane.authorization.handler import _evaluate_cedar

        return _evaluate_cedar(
            principal_id="alice@example.com",
            action=action,
            resource_type="Namespace",
            resource_id=resource_id,
            global_roles=global_roles or [],
            resource_roles=resource_roles or [],
            roles_dao=_FakeRolesDao(),
        )

    def test_steward_can_manage_data_source_in_own_namespace(self):
        roles = [{"role": "data-steward", "resourceUID": self.NS}]
        assert self._eval("manageSource", self.NS, resource_roles=roles) is True

    def test_steward_can_manage_metric_in_own_namespace(self):
        roles = [{"role": "data-steward", "resourceUID": self.NS}]
        assert self._eval("manageMetric", self.NS, resource_roles=roles) is True

    def test_analyst_can_read_metric_in_own_namespace(self):
        roles = [{"role": "data-analyst", "resourceUID": self.NS}]
        assert self._eval("readMetric", self.NS, resource_roles=roles) is True

    def test_analyst_cannot_manage_data_source(self):
        roles = [{"role": "data-analyst", "resourceUID": self.NS}]
        assert self._eval("manageSource", self.NS, resource_roles=roles) is False

    def test_steward_can_manage_ontology_in_own_namespace(self):
        # DELETE /ontologies maps to manageOntology; data-steward holds it, so
        # deleting an ontology must be permitted. Regression for the missing
        # DELETE route mapping that formerly fell through to denyAll and blocked
        # the steward.
        roles = [{"role": "data-steward", "resourceUID": self.NS}]
        assert self._eval("manageOntology", self.NS, resource_roles=roles) is True

    def test_analyst_cannot_manage_ontology(self):
        # data-analyst is read/query only — deleting an ontology stays denied.
        roles = [{"role": "data-analyst", "resourceUID": self.NS}]
        assert self._eval("manageOntology", self.NS, resource_roles=roles) is False

    def test_owner_can_manage_ontology(self):
        roles = [{"role": "namespace-owner", "resourceUID": self.NS}]
        assert self._eval("manageOntology", self.NS, resource_roles=roles) is True

    def test_platform_admin_can_manage_ontology(self):
        assert self._eval("manageOntology", self.NS, global_roles=["platform-admin"]) is True

    def test_steward_cannot_manage_other_namespace(self):
        roles = [{"role": "data-steward", "resourceUID": self.NS}]
        assert self._eval("manageSource", self.OTHER_NS, resource_roles=roles) is False

    def test_owner_can_manage_data_source(self):
        roles = [{"role": "namespace-owner", "resourceUID": self.NS}]
        assert self._eval("manageSource", self.NS, resource_roles=roles) is True

    def test_platform_admin_can_manage_metric(self):
        assert self._eval("manageMetric", self.NS, global_roles=["platform-admin"]) is True

    def test_no_role_denied_manage(self):
        assert self._eval("manageSource", self.NS) is False


class TestEvaluateCedarArchivedGuard:
    """_evaluate_cedar must enforce the non-ACTIVE-namespace mutation guard,
    which lives in default.cedar. Because the authorizer always loads
    ROLE#default, the guard's `forbid` participates in every evaluation and
    overrides every permit whenever context.namespaceStatus is present and
    != 'ACTIVE' (ARCHIVED, DELETING, or DELETE_FAILED).

    These exercise the real policy-loading path (not a patched _evaluate_cedar):
    the guard previously never fired in deployed environments because it was a
    standalone seed file that was neither seeded into the Roles table nor loaded
    by the authorizer. Folding it into default.cedar closes that gap. It was
    later extended from ARCHIVED-only to any non-ACTIVE status so a
    create/update can never race against the async deletion pipeline
    (DELETING) or a deletion retry (DELETE_FAILED).
    """

    NS = "11111111-1111-1111-1111-111111111111"

    def _eval(self, action: str, *, global_roles=None, resource_roles=None, context=None) -> bool:
        from coa_control_plane.authorization.handler import _evaluate_cedar

        return _evaluate_cedar(
            principal_id="alice@example.com",
            action=action,
            resource_type="Namespace",
            resource_id=self.NS,
            global_roles=global_roles or [],
            resource_roles=resource_roles or [],
            roles_dao=_FakeRolesDao(),
            context=context,
        )

    @pytest.mark.parametrize("status", ["ARCHIVED", "DELETING", "DELETE_FAILED"])
    def test_owner_mutation_denied_when_not_active(self, status: str):
        roles = [{"role": "namespace-owner", "resourceUID": self.NS}]
        assert self._eval("manageNamespace", resource_roles=roles, context={"namespaceStatus": status}) is False

    @pytest.mark.parametrize("status", ["ARCHIVED", "DELETING", "DELETE_FAILED"])
    def test_platform_admin_mutation_denied_when_not_active(self, status: str):
        # The guard overrides even platform-admin.
        assert (
            self._eval("grantPermissions", global_roles=["platform-admin"], context={"namespaceStatus": status})
            is False
        )

    def test_owner_mutation_allowed_when_active(self):
        roles = [{"role": "namespace-owner", "resourceUID": self.NS}]
        assert self._eval("manageNamespace", resource_roles=roles, context={"namespaceStatus": "ACTIVE"}) is True

    @pytest.mark.parametrize("status", ["ARCHIVED", "DELETING", "DELETE_FAILED"])
    def test_read_allowed_when_not_active(self, status: str):
        # Reads carry no namespaceStatus context, so the guard never fires.
        roles = [{"role": "namespace-owner", "resourceUID": self.NS}]
        assert self._eval("viewNamespace", resource_roles=roles, context=None) is True

    def test_set_status_allowed_when_archived(self):
        # setNamespaceStatus is not a guarded mutating action — reactivation must
        # remain possible. The authorizer does not forward namespaceStatus for it.
        roles = [{"role": "namespace-owner", "resourceUID": self.NS}]
        assert self._eval("setNamespaceStatus", resource_roles=roles, context=None) is True


_H = "coa_control_plane.authorization.handler"
_NS = "11111111-1111-1111-1111-111111111111"


def _archived_event(method: str, resource: str, path_params: dict[str, str]) -> dict[str, Any]:
    return {
        "methodArn": f"arn:aws:execute-api:us-east-1:123456789:api-id/stage/{method}{resource}",
        "headers": {"Authorization": "Bearer valid-jwt-token"},
        "httpMethod": method,
        "resource": resource,
        "pathParameters": path_params,
    }


class TestAuthorizerMetrics:
    """Every Deny/error return path in handler() must emit a metric so deny
    rates are observable in CloudWatch. AuthorizationDeny covers
    actual Cedar decisions (including denyAll for unmapped routes);
    AuthorizerError covers everything that never reached a Cedar decision."""

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorizer_error")
    def test_invalid_token_emits_authorizer_error(self, mock_emit, mock_cache_check, env_vars, apigw_event):
        from coa_control_plane.authorization.handler import handler

        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.side_effect = ValueError("bad token")
            handler(apigw_event, None)

        mock_emit.assert_called_once_with(reason="invalid_token")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorizer_error")
    def test_unexpected_token_error_emits_authorizer_error(self, mock_emit, mock_cache_check, env_vars, apigw_event):
        from coa_control_plane.authorization.handler import handler

        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.side_effect = RuntimeError("boom")
            handler(apigw_event, None)

        mock_emit.assert_called_once_with(reason="token_validation_error")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorizer_error")
    def test_empty_principal_emits_authorizer_error(self, mock_emit, mock_cache_check, env_vars, apigw_event):
        from coa_control_plane.authorization.handler import handler

        empty_result = TokenValidationResult(
            principal_id="", sub="", email="", groups=[], token_type=TokenType.JWT, claims={}
        )
        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.return_value = empty_result
            handler(apigw_event, None)

        mock_emit.assert_called_once_with(reason="no_principal")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorizer_error")
    @patch(f"{_H}._resolve_roles", side_effect=Exception("DDB throttle"))
    @patch(f"{_H}.DynamoDBDAO")
    def test_role_resolution_failure_emits_authorizer_error(
        self, mock_dao_cls, mock_resolve, mock_emit, mock_cache_check, env_vars, valid_result, apigw_event
    ):
        from coa_control_plane.authorization.handler import handler

        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.return_value = valid_result
            handler(apigw_event, None)

        mock_emit.assert_called_once_with(reason="role_resolution_error")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorizer_error")
    @patch(f"{_H}._get_namespace_status", side_effect=RuntimeError("DDB unavailable"))
    @patch(f"{_H}._resolve_roles", return_value=([], [{"role": "namespace-owner", "resourceUID": _NS}]))
    @patch(f"{_H}.DynamoDBDAO")
    def test_namespace_lookup_failure_emits_authorizer_error(
        self, mock_dao_cls, mock_resolve, mock_status, mock_emit, mock_cache_check, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        event = _archived_event("POST", "/namespaces/{namespaceId}/sources", {"namespaceId": _NS})
        with patch(f"{_H}._get_token_authorizer") as mock_auth:
            mock_auth.return_value.validate.return_value = valid_result
            handler(event, None)

        mock_emit.assert_called_once_with(reason="namespace_lookup_error", action="manageSource")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorizer_error")
    @patch(f"{_H}._evaluate_cedar", side_effect=Exception("Cedar error"))
    @patch(f"{_H}._resolve_roles", return_value=(["ADMIN"], []))
    @patch(f"{_H}.DynamoDBDAO")
    def test_cedar_exception_emits_authorizer_error(
        self, mock_dao_cls, mock_resolve, mock_cedar, mock_emit, mock_cache_check, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        event = _archived_event("GET", "/namespaces", {})
        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.return_value = valid_result
            handler(event, None)

        mock_emit.assert_called_once_with(reason="cedar_evaluation_error", action="list")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorization_deny")
    @patch(f"{_H}._evaluate_cedar", return_value=False)
    @patch(f"{_H}._resolve_roles", return_value=([], []))
    @patch(f"{_H}.DynamoDBDAO")
    def test_cedar_deny_emits_authorization_deny(
        self, mock_dao_cls, mock_resolve, mock_cedar, mock_emit, mock_cache_check, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        event = _archived_event("GET", "/namespaces", {})
        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.return_value = valid_result
            handler(event, None)

        mock_emit.assert_called_once_with(action="list", reason="cedar_deny")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorization_deny")
    @patch(f"{_H}._resolve_roles", return_value=([], []))
    @patch(f"{_H}.DynamoDBDAO")
    def test_unmapped_route_emits_deny_all_reason(
        self, mock_dao_cls, mock_resolve, mock_emit, mock_cache_check, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        # No Cedar policies found for any role (dao.get returns None) → clean
        # deny, not an exception, so this exercises the denyAll reason path
        # rather than cedar_evaluation_error.
        mock_dao_cls.return_value.get.return_value = None

        event = _archived_event("GET", "/unknown/route", {})
        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.return_value = valid_result
            handler(event, None)

        mock_emit.assert_called_once_with(action="denyAll", reason="deny_all_unmapped_route")

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorization_deny")
    @patch(f"{_H}.emit_authorizer_error")
    @patch(f"{_H}._evaluate_cedar", return_value=True)
    @patch(f"{_H}._resolve_roles", return_value=(["ADMIN"], []))
    @patch(f"{_H}.DynamoDBDAO")
    def test_allow_emits_no_deny_or_error_metric(
        self,
        mock_dao_cls,
        mock_resolve,
        mock_cedar,
        mock_emit_error,
        mock_emit_deny,
        mock_cache_check,
        env_vars,
        valid_result,
        apigw_event,
    ):
        from coa_control_plane.authorization.handler import handler

        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.return_value = valid_result
            result = handler(apigw_event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
        mock_emit_deny.assert_not_called()
        mock_emit_error.assert_not_called()

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}.emit_authorizer_error")
    def test_missing_roles_table_without_dev_mode_emits_authorizer_error(
        self, mock_emit, mock_cache_check, valid_result, monkeypatch
    ):
        monkeypatch.setenv("JWKS_ISSUER", "https://login.corp.com")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("RESOURCE_ROLE_MAPPINGS_TABLE_NAME", "")
        monkeypatch.delenv("ROLES_TABLE_NAME", raising=False)
        monkeypatch.delenv("ALLOW_WITHOUT_ROLES", raising=False)

        from coa_control_plane.authorization.handler import handler

        event = _archived_event("GET", "/namespaces", {})
        with patch(f"{_H}._get_token_authorizer") as mock_get_auth:
            mock_get_auth.return_value.validate.return_value = valid_result
            handler(event, None)

        mock_emit.assert_called_once_with(reason="roles_table_not_configured", action="list")


class TestArchivedMutationGuardWiring:
    """The authorizer must fetch namespace status for mutating routes and
    forward it to Cedar as context.namespaceStatus, while leaving read routes
    untouched (no DDB hit, no context)."""

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}._evaluate_cedar", return_value=False)
    @patch(f"{_H}._get_namespace_status", return_value="ARCHIVED")
    @patch(f"{_H}._resolve_roles", return_value=([], [{"role": "namespace-owner", "resourceUID": _NS}]))
    @patch(f"{_H}.DynamoDBDAO")
    def test_mutating_route_forwards_archived_context_and_denies(
        self, mock_dao, mock_resolve, mock_status, mock_cedar, mock_cache, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        event = _archived_event("POST", "/namespaces/{namespaceId}/sources", {"namespaceId": _NS})
        with patch(f"{_H}._get_token_authorizer") as mock_auth:
            mock_auth.return_value.validate.return_value = valid_result
            result = handler(event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
        mock_status.assert_called_once_with(_NS)
        _, kwargs = mock_cedar.call_args
        assert kwargs["action"] == "manageSource"
        assert kwargs["context"] == {"namespaceStatus": "ARCHIVED"}

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}._evaluate_cedar", return_value=True)
    @patch(f"{_H}._get_namespace_status", return_value="ACTIVE")
    @patch(f"{_H}._resolve_roles", return_value=([], [{"role": "namespace-owner", "resourceUID": _NS}]))
    @patch(f"{_H}.DynamoDBDAO")
    def test_mutating_route_allows_when_active(
        self, mock_dao, mock_resolve, mock_status, mock_cedar, mock_cache, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        event = _archived_event("POST", "/namespaces/{namespaceId}/sources", {"namespaceId": _NS})
        with patch(f"{_H}._get_token_authorizer") as mock_auth:
            mock_auth.return_value.validate.return_value = valid_result
            result = handler(event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
        _, kwargs = mock_cedar.call_args
        assert kwargs["context"] == {"namespaceStatus": "ACTIVE"}

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}._evaluate_cedar", return_value=True)
    @patch(f"{_H}._get_namespace_status")
    @patch(f"{_H}._resolve_roles", return_value=([], [{"role": "namespace-owner", "resourceUID": _NS}]))
    @patch(f"{_H}.DynamoDBDAO")
    def test_read_route_skips_status_fetch(
        self, mock_dao, mock_resolve, mock_status, mock_cedar, mock_cache, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        # GET sources → viewNamespace (a read): status must not be fetched and
        # no namespaceStatus context is forwarded, so queries stay allowed.
        event = _archived_event("GET", "/namespaces/{namespaceId}/sources", {"namespaceId": _NS})
        with patch(f"{_H}._get_token_authorizer") as mock_auth:
            mock_auth.return_value.validate.return_value = valid_result
            result = handler(event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Allow"
        mock_status.assert_not_called()
        _, kwargs = mock_cedar.call_args
        assert kwargs["context"] is None

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}._evaluate_cedar", return_value=True)
    @patch(f"{_H}._get_namespace_status", side_effect=RuntimeError("DDB unavailable"))
    @patch(f"{_H}._resolve_roles", return_value=([], [{"role": "namespace-owner", "resourceUID": _NS}]))
    @patch(f"{_H}.DynamoDBDAO")
    def test_status_lookup_failure_fails_closed(
        self, mock_dao, mock_resolve, mock_status, mock_cedar, mock_cache, env_vars, valid_result
    ):
        from coa_control_plane.authorization.handler import handler

        event = _archived_event("POST", "/namespaces/{namespaceId}/sources", {"namespaceId": _NS})
        with patch(f"{_H}._get_token_authorizer") as mock_auth:
            mock_auth.return_value.validate.return_value = valid_result
            result = handler(event, None)

        # Could not determine status → deny without even evaluating Cedar.
        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
        mock_cedar.assert_not_called()


class TestArchivedGuardFailClosed:
    """Configuration / lookup failures must fail closed (deny), never silently
    disable the ARCHIVED guard."""

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}._evaluate_cedar", return_value=True)
    @patch(f"{_H}._resolve_roles", return_value=([], [{"role": "namespace-owner", "resourceUID": _NS}]))
    @patch(f"{_H}.DynamoDBDAO")
    def test_missing_namespaces_table_denies_mutation(
        self, mock_dao, mock_resolve, mock_cedar, mock_cache, env_vars, valid_result, monkeypatch
    ):
        # If NAMESPACES_TABLE_NAME is not configured we cannot determine status,
        # so a mutating request must be denied rather than allowed (fail-open).
        monkeypatch.delenv("NAMESPACES_TABLE_NAME", raising=False)
        from coa_control_plane.authorization.handler import handler

        event = _archived_event("POST", "/namespaces/{namespaceId}/sources", {"namespaceId": _NS})
        with patch(f"{_H}._get_token_authorizer") as mock_auth:
            mock_auth.return_value.validate.return_value = valid_result
            result = handler(event, None)

        assert result["policyDocument"]["Statement"][0]["Effect"] == "Deny"
        mock_cedar.assert_not_called()

    @patch(f"{_H}._check_cache_version")
    @patch(f"{_H}._resolve_roles", return_value=([], [{"role": "namespace-owner", "resourceUID": _NS}]))
    @patch(f"{_H}.DynamoDBDAO")
    def test_status_fetched_fresh_each_invocation_not_cached(
        self, mock_dao, mock_resolve, mock_cache, env_vars, valid_result
    ):
        # Archiving must take effect on the next authorizer cache miss, so the
        # status is read fresh every invocation. Drive two requests for the same
        # namespace: ACTIVE then ARCHIVED. The handler must reflect the change
        # (allow then deny), proving the value is not cached in the Lambda.
        from coa_control_plane.authorization.handler import handler

        def cedar_reflects_context(**kwargs):
            ctx = kwargs.get("context") or {}
            return ctx.get("namespaceStatus") != "ARCHIVED"

        event = _archived_event("POST", "/namespaces/{namespaceId}/sources", {"namespaceId": _NS})
        with (
            patch(f"{_H}._evaluate_cedar", side_effect=cedar_reflects_context),
            patch(f"{_H}._get_namespace_status", side_effect=["ACTIVE", "ARCHIVED"]) as mock_status,
            patch(f"{_H}._get_token_authorizer") as mock_auth,
        ):
            mock_auth.return_value.validate.return_value = valid_result
            first = handler(event, None)
            second = handler(event, None)

        assert first["policyDocument"]["Statement"][0]["Effect"] == "Allow"
        assert second["policyDocument"]["Statement"][0]["Effect"] == "Deny"
        assert mock_status.call_count == 2  # fetched fresh, not cached


class TestGetNamespaceStatus:
    """Contract of the status helper: value when present, None for not-found,
    raise on misconfiguration / backend error, warn on corrupt values."""

    def test_returns_status_for_existing_namespace(self, env_vars):
        from coa_control_plane.authorization import handler as h

        dao = MagicMock()
        dao.get.return_value = {"PK": f"NS#{_NS}", "SK": "METADATA", "status": "ARCHIVED"}
        with patch.object(h, "_get_namespaces_dao", return_value=dao):
            assert h._get_namespace_status(_NS) == "ARCHIVED"

    def test_returns_none_for_missing_namespace(self, env_vars):
        from coa_control_plane.authorization import handler as h

        dao = MagicMock()
        dao.get.return_value = None
        with patch.object(h, "_get_namespaces_dao", return_value=dao):
            assert h._get_namespace_status(_NS) is None

    def test_warns_on_unrecognized_status(self, env_vars):
        from coa_control_plane.authorization import handler as h

        dao = MagicMock()
        dao.get.return_value = {"status": "BOGUS"}
        with (
            patch.object(h, "_get_namespaces_dao", return_value=dao),
            patch.object(h, "logger") as mock_logger,
        ):
            assert h._get_namespace_status(_NS) == "BOGUS"
        # Corrupt/unknown status is surfaced (cannot weaken the guard, but visible).
        mock_logger.warning.assert_called_once()
        assert "Unrecognized namespace status" in mock_logger.warning.call_args[0][0]

    def test_missing_config_raises(self, monkeypatch):
        # Reset singleton and clear config → _get_namespaces_dao must raise so
        # callers fail closed (distinguishes 'error' from 'not found').
        from coa_control_plane.authorization import handler as h

        h._namespaces_dao = None
        monkeypatch.delenv("NAMESPACES_TABLE_NAME", raising=False)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        with pytest.raises(RuntimeError):
            h._get_namespace_status(_NS)

    def test_backend_error_propagates(self, env_vars):
        from botocore.exceptions import ClientError
        from coa_control_plane.authorization import handler as h

        dao = MagicMock()
        dao.get.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "GetItem")
        with patch.object(h, "_get_namespaces_dao", return_value=dao), pytest.raises(ClientError):
            h._get_namespace_status(_NS)
