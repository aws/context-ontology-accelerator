# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Namespace Roles handler (list + get)."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from coa_common.dao import PaginatedResult

os.environ.update(
    {
        "ROLES_TABLE": "test-roles",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.roles.namespace_roles_handler import handler  # noqa: E402

pytestmark = pytest.mark.unit

VALID_NS_ID = "550e8400-e29b-41d4-a716-446655440000"

ROLE_ITEMS = [
    {
        "PK": f"NS#{VALID_NS_ID}",
        "SK": "ROLE#owner",
        "name": "owner",
        "description": "Full control of namespace",
        "isBuiltIn": True,
        "scope": "NAMESPACE",
    },
    {
        "PK": f"NS#{VALID_NS_ID}",
        "SK": "ROLE#analyst",
        "name": "analyst",
        "description": "Query and read access",
        "isBuiltIn": True,
        "scope": "NAMESPACE",
    },
    {
        "PK": f"NS#{VALID_NS_ID}",
        "SK": "ROLE#data-steward",
        "name": "data-steward",
        "description": "Manage data sources and ontologies",
        "isBuiltIn": True,
        "scope": "NAMESPACE",
    },
]


def _event(namespace_id: str = VALID_NS_ID, role_id: str | None = None, method: str = "GET") -> dict:
    path_params = {"namespaceId": namespace_id}
    if role_id:
        path_params["roleId"] = role_id
    return {
        "httpMethod": method,
        "path": f"/namespaces/{namespace_id}/roles" + (f"/{role_id}" if role_id else ""),
        "pathParameters": path_params,
    }


class TestListNamespaceRoles:
    @patch("coa_control_plane.roles.namespace_roles_handler.DynamoDBDAO")
    def test_returns_roles_for_namespace(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=ROLE_ITEMS, last_evaluated_key=None)

        resp = handler(_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["roles"]) == 3
        names = {r["name"] for r in body["roles"]}
        assert names == {"owner", "analyst", "data-steward"}

    @patch("coa_control_plane.roles.namespace_roles_handler.DynamoDBDAO")
    def test_returns_empty_list_for_no_roles(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        resp = handler(_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["roles"] == []

    def test_invalid_namespace_id_returns_400(self):
        resp = handler(_event(namespace_id="invalid!"), None)
        assert resp["statusCode"] == 400

    def test_post_returns_405(self):
        resp = handler(_event(method="POST"), None)
        assert resp["statusCode"] == 405


class TestGetNamespaceRole:
    @patch("coa_control_plane.roles.namespace_roles_handler.DynamoDBDAO")
    def test_returns_role_detail(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.get.return_value = {
            "PK": f"NS#{VALID_NS_ID}",
            "SK": "ROLE#analyst",
            "name": "analyst",
            "description": "Query and read access",
            "isBuiltIn": True,
            "scope": "NAMESPACE",
            "cedarPolicy": "permit(principal, action, resource);",
        }

        resp = handler(_event(role_id="analyst"), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        role = body["role"]
        assert role["name"] == "analyst"
        assert role["roleId"] == "analyst"
        assert role["isBuiltIn"] is True
        assert role["scope"] == "NAMESPACE"
        assert role["cedarPolicy"] == "permit(principal, action, resource);"

    @patch("coa_control_plane.roles.namespace_roles_handler.DynamoDBDAO")
    def test_returns_404_for_missing_role(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.get.return_value = None

        resp = handler(_event(role_id="nonexistent"), None)
        assert resp["statusCode"] == 404
        body = json.loads(resp["body"])
        assert "not found" in body["message"].lower()

    def test_invalid_namespace_id_returns_400(self):
        resp = handler(_event(namespace_id="bad-id!", role_id="analyst"), None)
        assert resp["statusCode"] == 400

    def test_invalid_role_id_returns_400(self):
        resp = handler(_event(role_id="invalid/role!"), None)
        assert resp["statusCode"] == 400

    @patch("coa_control_plane.roles.namespace_roles_handler.DynamoDBDAO")
    def test_dynamodb_error_returns_500(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.get.side_effect = RuntimeError("connection timeout")

        resp = handler(_event(role_id="analyst"), None)
        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["message"] == "Internal server error"

    @patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://test.example.com"}, clear=True)
    def test_missing_env_vars_returns_500(self):
        resp = handler(_event(role_id="analyst"), None)
        assert resp["statusCode"] == 500
