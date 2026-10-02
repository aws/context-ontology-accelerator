# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the List Platform Roles handler."""

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

from coa_control_plane.roles.list_platform_roles_handler import handler  # noqa: E402

pytestmark = pytest.mark.unit


class TestListPlatformRoles:
    @patch("coa_control_plane.roles.list_platform_roles_handler.DynamoDBDAO")
    def test_returns_200_with_platform_roles(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(
            items=[
                {
                    "PK": "GLOBAL",
                    "SK": "ROLE#platform-admin",
                    "name": "Platform Admin",
                    "description": "Full access",
                    "isBuiltIn": True,
                    "scope": "GLOBAL",
                },
                {
                    "PK": "GLOBAL",
                    "SK": "ROLE#platform-viewer",
                    "name": "Platform Viewer",
                    "description": "Read-only",
                    "isBuiltIn": True,
                    "scope": "GLOBAL",
                },
            ],
            last_evaluated_key=None,
        )

        resp = handler({"httpMethod": "GET", "path": "/roles"}, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["roles"]) == 2
        names = {r["name"] for r in body["roles"]}
        assert names == {"Platform Admin", "Platform Viewer"}

    @patch("coa_control_plane.roles.list_platform_roles_handler.DynamoDBDAO")
    def test_all_roles_are_built_in(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(
            items=[
                {
                    "PK": "GLOBAL",
                    "SK": "ROLE#platform-admin",
                    "name": "Platform Admin",
                    "isBuiltIn": True,
                    "scope": "GLOBAL",
                },
            ],
            last_evaluated_key=None,
        )

        resp = handler({"httpMethod": "GET", "path": "/roles"}, None)
        body = json.loads(resp["body"])
        for role in body["roles"]:
            assert role["isBuiltIn"] is True
            assert role["scope"] == "PLATFORM"

    @patch("coa_control_plane.roles.list_platform_roles_handler.DynamoDBDAO")
    def test_default_baseline_policy_is_not_advertised_as_a_role(self, mock_dao_cls):
        """#988: the seeded ``default`` policy bundle shares the GLOBAL partition
        but is not grantable; it must not appear in the platform-role picker."""
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(
            items=[
                {"PK": "GLOBAL", "SK": "ROLE#default", "name": "Default", "isBuiltIn": True, "scope": "GLOBAL"},
                {"PK": "GLOBAL", "SK": "ROLE#platform-admin", "name": "Platform Admin", "isBuiltIn": True},
                {"PK": "GLOBAL", "SK": "ROLE#platform-viewer", "name": "Platform Viewer", "isBuiltIn": True},
            ],
            last_evaluated_key=None,
        )

        resp = handler({"httpMethod": "GET", "path": "/roles"}, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert [r["roleId"] for r in body["roles"]] == ["platform-admin", "platform-viewer"]

    @patch("coa_control_plane.roles.list_platform_roles_handler.DynamoDBDAO")
    def test_returns_empty_when_no_roles(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        resp = handler({"httpMethod": "GET", "path": "/roles"}, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["roles"] == []

    def test_post_returns_405(self):
        resp = handler({"httpMethod": "POST", "path": "/roles"}, None)
        assert resp["statusCode"] == 405
        assert resp["headers"]["Allow"] == "GET"

    @patch("coa_control_plane.roles.list_platform_roles_handler.DynamoDBDAO")
    def test_dynamodb_error_returns_500(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.side_effect = RuntimeError("connection timeout")

        resp = handler({"httpMethod": "GET", "path": "/roles"}, None)
        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["message"] == "Internal server error"

    @patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://test.example.com"}, clear=True)
    def test_missing_env_vars_returns_500(self):
        # AWS_REGION and ROLES_TABLE not set
        os.environ.pop("AWS_REGION", None)
        os.environ.pop("ROLES_TABLE", None)
        resp = handler({"httpMethod": "GET", "path": "/roles"}, None)
        assert resp["statusCode"] == 500
