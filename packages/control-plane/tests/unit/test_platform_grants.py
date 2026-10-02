# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the platform grant handlers (create / list / delete)."""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_common.dao import PaginatedResult

os.environ.update(
    {
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-mappings",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.grants.create_platform_grant_handler import (  # noqa: E402
    handler as create_handler,
)
from coa_control_plane.grants.delete_platform_grant_handler import (  # noqa: E402
    handler as delete_handler,
)
from coa_control_plane.grants.grant_id import decode_grant_id, encode_grant_id  # noqa: E402
from coa_control_plane.grants.list_platform_grants_handler import (  # noqa: E402
    handler as list_handler,
)

pytestmark = pytest.mark.unit


# ──────────────────────────────────────────────────────────────────────────
# CreatePlatformGrant
# ──────────────────────────────────────────────────────────────────────────
def _create_event(body: dict | None, caller: str = "admin@company.com") -> dict:
    return {
        "httpMethod": "POST",
        "resource": "/grants",
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"authorizer": {"email": caller}},
    }


class TestCreatePlatformGrant:
    @patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
    def test_create_success(self, mock_dao_cls):
        roles_dao = MagicMock()
        mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [roles_dao, mappings_dao]
        roles_dao.get.return_value = {"PK": "GLOBAL", "SK": "ROLE#platform-admin", "name": "Platform Admin"}

        resp = create_handler(
            _create_event(
                {
                    "principalType": "User",
                    "principalId": "alice@company.com",
                    "role": "platform-admin",
                }
            ),
            None,
        )

        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        grant = body["grant"]
        assert grant["principalId"] == "alice@company.com"
        assert grant["role"] == "platform-admin"
        assert grant["grantedBy"] == "admin@company.com"
        # Platform grants carry no namespaceId
        assert "namespaceId" not in grant

        # Verify the persisted item uses the platform key shape
        written = mappings_dao.put.call_args[0][0]
        assert written["PK"] == "Platform::GLOBAL#User::alice@company.com"
        assert written["SK"] == "ROLE#platform-admin"
        assert written["resourceType"] == "Platform"
        assert written["resourceId"] == "GLOBAL"
        assert written["namespaceKey"] == "NS#GLOBAL"
        assert written["resourceRoleKey"] == "Platform::GLOBAL#ROLE#platform-admin"
        # grantId round-trips to PK+SK
        assert decode_grant_id(grant["grantId"]) == (written["PK"], written["SK"])

    @patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
    def test_create_with_special_characters_in_email(self, mock_dao_cls):
        """Verify that emails with special characters are URL-encoded in principalKey.

        The authorizer queries grants using URL-encoded principal IDs, so grants
        must be written with the same encoding for users with special characters
        (like ``+``) in their email to be resolved correctly.
        """
        roles_dao = MagicMock()
        mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [roles_dao, mappings_dao]
        roles_dao.get.return_value = {"PK": "GLOBAL", "SK": "ROLE#platform-admin"}

        resp = create_handler(
            _create_event({"principalType": "User", "principalId": "foo+123@amazon.com", "role": "platform-admin"}),
            None,
        )

        assert resp["statusCode"] == 201

        # Verify the item written to DynamoDB has URL-encoded principalKey
        written = mappings_dao.put.call_args[0][0]
        assert written["principalKey"] == "User::foo%2B123@amazon.com"
        assert written["PK"] == "Platform::GLOBAL#User::foo%2B123@amazon.com"
        assert written["principalRoleKey"] == "User::foo%2B123@amazon.com#ROLE#platform-admin"
        # Raw principalId preserved for display
        assert written["principalId"] == "foo+123@amazon.com"

    @patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
    def test_default_baseline_policy_is_not_grantable(self, mock_dao_cls):
        """#988: ``default`` exists under PK=GLOBAL (it is the baseline policy
        bundle), so the existence check alone would accept it. Granting it gives
        the principal a GLOBAL role row and the namespace list stops filtering."""
        roles_dao = MagicMock()
        mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [roles_dao, mappings_dao]
        roles_dao.get.return_value = {"PK": "GLOBAL", "SK": "ROLE#default", "name": "Default"}

        resp = create_handler(
            _create_event({"principalType": "User", "principalId": "a@b.com", "role": "default"}),
            None,
        )
        assert resp["statusCode"] == 400
        assert "not assignable" in json.loads(resp["body"])["message"]
        mappings_dao.put.assert_not_called()

    @patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
    def test_role_not_found_returns_404(self, mock_dao_cls):
        roles_dao = MagicMock()
        mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [roles_dao, mappings_dao]
        roles_dao.get.return_value = None

        resp = create_handler(
            _create_event({"principalType": "User", "principalId": "a@b.com", "role": "not-a-role"}),
            None,
        )
        assert resp["statusCode"] == 404
        mappings_dao.put.assert_not_called()

    @patch("coa_control_plane.grants.create_platform_grant_handler.DynamoDBDAO")
    def test_duplicate_returns_409(self, mock_dao_cls):
        roles_dao = MagicMock()
        mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [roles_dao, mappings_dao]
        roles_dao.get.return_value = {"PK": "GLOBAL", "SK": "ROLE#platform-viewer"}
        mappings_dao.put.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")

        resp = create_handler(
            _create_event({"principalType": "Group", "principalId": "analysts", "role": "platform-viewer"}),
            None,
        )
        assert resp["statusCode"] == 409

    def test_missing_fields_returns_400(self):
        resp = create_handler(_create_event({"principalType": "User"}), None)
        assert resp["statusCode"] == 400

    def test_principal_id_too_long_returns_400(self):
        long_id = "a" * 300 + "@co.com"
        resp = create_handler(
            _create_event({"principalType": "User", "principalId": long_id, "role": "platform-admin"}),
            None,
        )
        assert resp["statusCode"] == 400

    def test_role_too_long_returns_400(self):
        resp = create_handler(
            _create_event({"principalType": "User", "principalId": "a@b.com", "role": "x" * 200}),
            None,
        )
        assert resp["statusCode"] == 400

    def test_principal_id_with_delimiter_returns_400(self):
        for bad_id in ("a#b@co.com", "a|b@co.com", "Namespace::x"):
            resp = create_handler(
                _create_event({"principalType": "User", "principalId": bad_id, "role": "platform-admin"}),
                None,
            )
            assert resp["statusCode"] == 400, bad_id

    def test_invalid_principal_type_returns_400(self):
        resp = create_handler(
            _create_event({"principalType": "Robot", "principalId": "a@b.com", "role": "platform-admin"}),
            None,
        )
        assert resp["statusCode"] == 400

    def test_invalid_json_returns_400(self):
        event = {"httpMethod": "POST", "resource": "/grants", "body": "{not json"}
        resp = create_handler(event, None)
        assert resp["statusCode"] == 400

    def test_empty_body_returns_400(self):
        resp = create_handler(_create_event(None), None)
        assert resp["statusCode"] == 400

    @patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://test.example.com"}, clear=True)
    def test_missing_env_returns_500(self):
        resp = create_handler(
            _create_event({"principalType": "User", "principalId": "a@b.com", "role": "platform-admin"}),
            None,
        )
        assert resp["statusCode"] == 500


# ──────────────────────────────────────────────────────────────────────────
# ListPlatformGrants
# ──────────────────────────────────────────────────────────────────────────
def _list_event(query_params: dict | None = None) -> dict:
    return {
        "httpMethod": "GET",
        "resource": "/grants",
        "queryStringParameters": query_params,
    }


class TestListPlatformGrants:
    @patch("coa_control_plane.grants.list_platform_grants_handler.DynamoDBDAO")
    def test_list_all_uses_namespace_grants_index(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.query.return_value = PaginatedResult(
            items=[
                {
                    "PK": "Platform::GLOBAL#User::alice@company.com",
                    "SK": "ROLE#platform-admin",
                    "principalType": "User",
                    "principalId": "alice@company.com",
                    "role": "platform-admin",
                    "grantedBy": "system",
                    "grantedAt": "2026-04-09T10:00:00Z",
                },
            ],
            last_evaluated_key=None,
        )

        resp = list_handler(_list_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["grants"]) == 1
        assert body["grants"][0]["role"] == "platform-admin"
        assert "namespaceId" not in body["grants"][0]
        assert body["grants"][0]["grantId"] == encode_grant_id(
            "Platform::GLOBAL#User::alice@company.com", "ROLE#platform-admin"
        )

        params = dao.query.call_args[0][0]
        assert params.index_name == "NamespaceGrantsIndex"
        assert params.expression_values[":nk"] == "NS#GLOBAL"

    @patch("coa_control_plane.grants.list_platform_grants_handler.DynamoDBDAO")
    def test_list_by_principal_uses_principal_index_scoped_to_platform(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        resp = list_handler(_list_event({"principal": "User::alice@company.com"}), None)
        assert resp["statusCode"] == 200

        params = dao.query.call_args[0][0]
        assert params.index_name == "PrincipalIndex"
        assert params.expression_values[":pk"] == "User::alice@company.com"
        assert params.expression_values[":rp"] == "Platform::GLOBAL#"

    @patch("coa_control_plane.grants.list_platform_grants_handler.DynamoDBDAO")
    def test_empty(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)
        resp = list_handler(_list_event(), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["grants"] == []

    @patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://test.example.com"}, clear=True)
    def test_missing_env_returns_500(self):
        resp = list_handler(_list_event(), None)
        assert resp["statusCode"] == 500


class TestListPlatformGrantsPagination:
    @patch("coa_control_plane.grants.list_platform_grants_handler.DynamoDBDAO")
    def test_passes_max_results_as_limit(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        resp = list_handler(_list_event({"maxResults": "5"}), None)
        assert resp["statusCode"] == 200
        assert dao.query.call_args[0][0].limit == 5

    @patch("coa_control_plane.grants.list_platform_grants_handler.DynamoDBDAO")
    def test_returns_next_token_when_more_pages(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        last_key = {"PK": "Platform::GLOBAL#User::z@co.com", "SK": "ROLE#platform-viewer"}
        dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=last_key)

        resp = list_handler(_list_event(), None)
        body = json.loads(resp["body"])
        assert "nextToken" in body
        assert json.loads(base64.b64decode(body["nextToken"])) == last_key

    @patch("coa_control_plane.grants.list_platform_grants_handler.DynamoDBDAO")
    def test_omits_next_token_on_last_page(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        body = json.loads(list_handler(_list_event(), None)["body"])
        assert "nextToken" not in body

    @patch("coa_control_plane.grants.list_platform_grants_handler.DynamoDBDAO")
    def test_decodes_next_token_into_start_key(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)
        start_key = {"PK": "p", "SK": "s"}
        token = base64.b64encode(json.dumps(start_key).encode()).decode()

        resp = list_handler(_list_event({"nextToken": token}), None)
        assert resp["statusCode"] == 200
        assert dao.query.call_args[0][0].exclusive_start_key == start_key

    def test_invalid_next_token_returns_400(self):
        bad = base64.b64encode(b"not-json").decode()
        resp = list_handler(_list_event({"nextToken": bad}), None)
        assert resp["statusCode"] == 400

    def test_zero_max_results_returns_400(self):
        resp = list_handler(_list_event({"maxResults": "0"}), None)
        assert resp["statusCode"] == 400

    def test_too_large_max_results_returns_400(self):
        resp = list_handler(_list_event({"maxResults": "1000"}), None)
        assert resp["statusCode"] == 400

    def test_non_integer_max_results_returns_400(self):
        resp = list_handler(_list_event({"maxResults": "abc"}), None)
        assert resp["statusCode"] == 400


# ──────────────────────────────────────────────────────────────────────────
# DeletePlatformGrant
# ──────────────────────────────────────────────────────────────────────────
def _delete_event(grant_id: str) -> dict:
    return {
        "httpMethod": "DELETE",
        "resource": "/grants/{grantId}",
        "pathParameters": {"grantId": grant_id},
    }


class TestDeletePlatformGrant:
    @patch("coa_control_plane.grants.delete_platform_grant_handler.DynamoDBDAO")
    def test_delete_success(self, mock_dao_cls):
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.get.return_value = {"PK": "Platform::GLOBAL#User::alice@company.com", "SK": "ROLE#platform-admin"}
        gid = encode_grant_id("Platform::GLOBAL#User::alice@company.com", "ROLE#platform-admin")

        resp = delete_handler(_delete_event(gid), None)
        assert resp["statusCode"] == 200
        dao.delete.assert_called_once_with(
            {"PK": "Platform::GLOBAL#User::alice@company.com", "SK": "ROLE#platform-admin"}
        )

    @patch("coa_control_plane.grants.delete_platform_grant_handler.DynamoDBDAO")
    def test_delete_nonexistent_returns_404(self, mock_dao_cls):
        """A grant id that decodes to a platform key but does not exist → 404."""
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        dao.get.return_value = None
        gid = encode_grant_id("Platform::GLOBAL#User::ghost@company.com", "ROLE#platform-admin")

        resp = delete_handler(_delete_event(gid), None)
        assert resp["statusCode"] == 404
        dao.delete.assert_not_called()

    @patch("coa_control_plane.grants.delete_platform_grant_handler.DynamoDBDAO")
    def test_rejects_namespace_grant(self, mock_dao_cls):
        """A namespace grant id must not be revokable via the platform endpoint."""
        dao = MagicMock()
        mock_dao_cls.return_value = dao
        gid = encode_grant_id(
            "Namespace::550e8400-e29b-41d4-a716-446655440000#User::alice@company.com",
            "ROLE#namespace-owner",
        )

        resp = delete_handler(_delete_event(gid), None)
        assert resp["statusCode"] == 404
        dao.delete.assert_not_called()

    def test_invalid_grant_id_returns_400(self):
        resp = delete_handler(_delete_event("!!!not-base64!!!"), None)
        assert resp["statusCode"] == 400

    def test_missing_grant_id_returns_400(self):
        resp = delete_handler(_delete_event(""), None)
        assert resp["statusCode"] == 400


# ──────────────────────────────────────────────────────────────────────────
# Router dispatch (grants_handler)
# ──────────────────────────────────────────────────────────────────────────
class TestGrantsRouterPlatformRoutes:
    @patch("coa_control_plane.grants.grants_handler.create_platform_handler")
    def test_post_grants_routes_to_create_platform(self, mock_create):
        from coa_control_plane.grants import grants_handler

        mock_create.return_value = {"statusCode": 201}
        resp = grants_handler.handler({"httpMethod": "POST", "resource": "/grants"}, None)
        assert resp["statusCode"] == 201
        mock_create.assert_called_once()

    @patch("coa_control_plane.grants.grants_handler.list_platform_handler")
    def test_get_grants_routes_to_list_platform(self, mock_list):
        from coa_control_plane.grants import grants_handler

        mock_list.return_value = {"statusCode": 200}
        resp = grants_handler.handler({"httpMethod": "GET", "resource": "/grants"}, None)
        assert resp["statusCode"] == 200
        mock_list.assert_called_once()

    @patch("coa_control_plane.grants.grants_handler.delete_platform_handler")
    def test_delete_grant_routes_to_delete_platform(self, mock_delete):
        from coa_control_plane.grants import grants_handler

        mock_delete.return_value = {"statusCode": 200}
        resp = grants_handler.handler({"httpMethod": "DELETE", "resource": "/grants/{grantId}"}, None)
        assert resp["statusCode"] == 200
        mock_delete.assert_called_once()

    def test_unsupported_method_on_grants_returns_405(self):
        from coa_control_plane.grants import grants_handler

        resp = grants_handler.handler({"httpMethod": "PUT", "resource": "/grants"}, None)
        assert resp["statusCode"] == 405
