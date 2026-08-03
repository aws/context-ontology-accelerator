# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the List Principal Grants handler."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.update(
    {
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-mappings",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.grants.list_principal_grants_handler import handler  # noqa: E402

pytestmark = pytest.mark.unit

VALID_NS_ID = "550e8400-e29b-41d4-a716-446655440000"


def _event_me(email: str = "alice@company.com", groups: str = "") -> dict:
    return {
        "httpMethod": "GET",
        "resource": "/principals/{principalId}/grants",
        "pathParameters": {"principalId": "me"},
        "requestContext": {
            "authorizer": {
                "email": email,
                "groups": groups,
            }
        },
    }


def _event_me_claims(email: str = "alice@company.com", groups: str = "") -> dict:
    """Event with claims nested under authorizer.claims (Cognito style)."""
    return {
        "httpMethod": "GET",
        "resource": "/principals/{principalId}/grants",
        "pathParameters": {"principalId": "me"},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "email": email,
                    "cognito:groups": groups,
                }
            }
        },
    }


class TestListPrincipalGrants:
    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_lists_grants_for_self(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.return_value = [
            {
                "PK": f"Namespace::{VALID_NS_ID}#User::alice@company.com",
                "SK": "ROLE#analyst",
                "resourceId": VALID_NS_ID,
                "principalType": "User",
                "principalId": "alice@company.com",
                "role": "analyst",
                "grantedBy": "bob@company.com",
                "grantedAt": "2026-04-09T10:00:00Z",
            },
        ]

        resp = handler(_event_me(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["grants"]) == 1
        assert body["grants"][0]["role"] == "analyst"

        # Verify PrincipalIndex GSI was used with sanitized email
        query_call = mock_dao.query_all.call_args[0][0]
        assert query_call.index_name == "PrincipalIndex"
        assert ":pk" in query_call.expression_values
        assert query_call.expression_values[":pk"] == "User::alice@company.com"

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_queries_user_and_groups(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.return_value = []

        resp = handler(_event_me("alice@co.com", "admins,viewers"), None)
        assert resp["statusCode"] == 200

        # Should query 3 times: User + 2 groups
        assert mock_dao.query_all.call_count == 3
        keys_queried = [call[0][0].expression_values[":pk"] for call in mock_dao.query_all.call_args_list]
        assert "User::alice@co.com" in keys_queried
        assert "Group::admins" in keys_queried
        assert "Group::viewers" in keys_queried

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_deduplicates_grants(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        # Same grant returned for both user and group query
        grant_item = {
            "PK": f"Namespace::{VALID_NS_ID}#User::alice@co.com",
            "SK": "ROLE#analyst",
            "resourceId": VALID_NS_ID,
            "principalType": "User",
            "principalId": "alice@co.com",
            "role": "analyst",
            "grantedBy": "admin@co.com",
            "grantedAt": "2026-05-01T00:00:00Z",
        }
        mock_dao.query_all.return_value = [grant_item]

        resp = handler(_event_me("alice@co.com", "team"), None)
        body = json.loads(resp["body"])
        # Should be deduplicated to 1 even though queried twice
        assert len(body["grants"]) == 1

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_normalizes_email_to_lowercase(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.return_value = []

        resp = handler(_event_me("Alice@Company.COM"), None)
        assert resp["statusCode"] == 200

        key = mock_dao.query_all.call_args[0][0].expression_values[":pk"]
        assert key == "User::alice@company.com"

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_limits_groups_to_max(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.return_value = []

        many_groups = ",".join(f"group{i}" for i in range(20))
        resp = handler(_event_me("a@b.com", many_groups), None)
        assert resp["statusCode"] == 200
        # 1 user + max 10 groups = 11 queries
        assert mock_dao.query_all.call_count == 11

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_skips_invalid_group_names(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.return_value = []

        resp = handler(_event_me("a@b.com", "valid-group,bad::group,ok_group"), None)
        assert resp["statusCode"] == 200
        # 1 user + 2 valid groups (bad::group skipped)
        assert mock_dao.query_all.call_count == 3

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_fallback_to_claims_email(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.return_value = []

        resp = handler(_event_me_claims("bob@co.com", "devs"), None)
        assert resp["statusCode"] == 200
        key = mock_dao.query_all.call_args_list[0][0][0].expression_values[":pk"]
        assert key == "User::bob@co.com"

    def test_rejects_non_me_principal(self):
        event = {
            "httpMethod": "GET",
            "resource": "/principals/{principalId}/grants",
            "pathParameters": {"principalId": "alice@company.com"},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 403

    def test_missing_principal_id_returns_400(self):
        event = {
            "httpMethod": "GET",
            "resource": "/principals/{principalId}/grants",
            "pathParameters": {"principalId": ""},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 400

    def test_missing_email_returns_400(self):
        event = {
            "httpMethod": "GET",
            "resource": "/principals/{principalId}/grants",
            "pathParameters": {"principalId": "me"},
            "requestContext": {"authorizer": {}},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 400

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_dynamo_error_returns_500(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.side_effect = Exception("DynamoDB throttled")

        resp = handler(_event_me(), None)
        assert resp["statusCode"] == 500


class TestListPrincipalGrantsToGrantSummary:
    """Verify list_principal_grants_handler._to_grant_summary correctly handles
    optional override fields (tableAllowlist / columnDenylist / allowedMetrics)."""

    BASE_ITEM = {
        "PK": f"Namespace::{VALID_NS_ID}#User::alice@company.com",
        "SK": "ROLE#analyst",
        "resourceId": VALID_NS_ID,
        "principalType": "User",
        "principalId": "alice@company.com",
        "role": "analyst",
        "grantedBy": "bob@company.com",
        "grantedAt": "2026-04-09T10:00:00Z",
    }

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_overrides_present_with_data_are_returned(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {
            **self.BASE_ITEM,
            "tableAllowlist": ["orders"],
            "columnDenylist": {"customers": ["ssn", "dob"]},
            "allowedMetrics": ["revenue", "order_count"],
        }
        mock_dao.query_all.return_value = [item]
        resp = handler(_event_me(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert grant["tableAllowlist"] == ["orders"]
        assert grant["columnDenylist"] == {"customers": ["ssn", "dob"]}
        assert grant["allowedMetrics"] == ["revenue", "order_count"]
        assert grant["namespaceId"] == VALID_NS_ID

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_overrides_absent_are_omitted(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query_all.return_value = [self.BASE_ITEM]
        resp = handler(_event_me(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert "tableAllowlist" not in grant
        assert "columnDenylist" not in grant
        assert "allowedMetrics" not in grant

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_empty_overrides_are_omitted(self, mock_dao_cls):
        """Empty list/dict are treated as unset — sparse response, matches write-side."""
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {
            **self.BASE_ITEM,
            "tableAllowlist": [],
            "columnDenylist": {},
            "allowedMetrics": [],
        }
        mock_dao.query_all.return_value = [item]
        resp = handler(_event_me(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert "tableAllowlist" not in grant
        assert "columnDenylist" not in grant
        assert "allowedMetrics" not in grant

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_partial_overrides_only_set_fields_returned(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {**self.BASE_ITEM, "columnDenylist": {"customers": ["ssn"]}}
        mock_dao.query_all.return_value = [item]
        resp = handler(_event_me(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert grant["columnDenylist"] == {"customers": ["ssn"]}
        assert "tableAllowlist" not in grant
        assert "allowedMetrics" not in grant
