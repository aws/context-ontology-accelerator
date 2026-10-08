# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Create Grant handler."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.update(
    {
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-mappings",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.grants.create_handler import handler  # noqa: E402
from coa_control_plane.grants.grant_id import decode_grant_id  # noqa: E402

pytestmark = pytest.mark.unit

VALID_NS_ID = "550e8400-e29b-41d4-a716-446655440000"


def _event(namespace_id: str = VALID_NS_ID, body: dict | None = None) -> dict:
    event: dict = {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/grants",
        "pathParameters": {"namespaceId": namespace_id},
        "requestContext": {"authorizer": {"claims": {"email": "admin@example.com"}}},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


class TestCreateGrant:
    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_creates_grant_successfully(self, mock_dao_cls):
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        mock_roles_dao.get.return_value = {"PK": f"NS#{VALID_NS_ID}", "SK": "ROLE#analyst"}

        resp = handler(
            _event(body={"principalType": "User", "principalId": "alice@company.com", "role": "analyst"}),
            None,
        )
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        grant = body["grant"]
        assert grant["principalType"] == "User"
        assert grant["principalId"] == "alice@company.com"
        assert grant["role"] == "analyst"
        assert grant["namespaceId"] == VALID_NS_ID
        assert grant["grantedBy"] == "admin@example.com"

        # grantId should decode back to PK+SK
        pk, sk = decode_grant_id(grant["grantId"])
        assert pk == f"Namespace::{VALID_NS_ID}#User::alice@company.com"
        assert sk == "ROLE#analyst"

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_creates_grant_with_special_characters_in_email(self, mock_dao_cls):
        """Verify that emails with special characters are URL-encoded in principalKey.

        The authorizer queries grants using URL-encoded principal IDs, so grants
        must be written with the same encoding for users with special characters
        (like ``+``) in their email to be resolved correctly.
        """
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        mock_roles_dao.get.return_value = {"PK": f"NS#{VALID_NS_ID}", "SK": "ROLE#analyst"}

        resp = handler(
            _event(body={"principalType": "User", "principalId": "foo+123@amazon.com", "role": "analyst"}),
            None,
        )
        assert resp["statusCode"] == 201

        # Verify the item written to DynamoDB has URL-encoded principalKey
        put_call = mock_mappings_dao.put.call_args
        item = put_call[0][0]
        assert item["principalKey"] == "User::foo%2B123@amazon.com"
        assert item["PK"] == f"Namespace::{VALID_NS_ID}#User::foo%2B123@amazon.com"
        assert item["principalRoleKey"] == "User::foo%2B123@amazon.com#ROLE#analyst"
        # Raw principalId preserved for display
        assert item["principalId"] == "foo+123@amazon.com"

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_creates_grant_with_overrides(self, mock_dao_cls):
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        mock_roles_dao.get.return_value = {"PK": f"NS#{VALID_NS_ID}", "SK": "ROLE#analyst"}

        resp = handler(
            _event(
                body={
                    "principalType": "User",
                    "principalId": "alice@company.com",
                    "role": "analyst",
                    "tableAllowlist": ["orders", "customers"],
                    "columnDenylist": {"customers": ["ssn"]},
                    "allowedMetrics": ["revenue"],
                }
            ),
            None,
        )
        assert resp["statusCode"] == 201

        # Verify the item written to DynamoDB includes overrides
        put_call = mock_mappings_dao.put.call_args
        item = put_call[0][0]
        assert item["tableAllowlist"] == ["orders", "customers"]
        assert item["columnDenylist"] == {"customers": ["ssn"]}
        assert item["allowedMetrics"] == ["revenue"]
        # No grantId stored in DynamoDB
        assert "grantId" not in item

        # Response echoes the overrides on GrantSummary
        grant = json.loads(resp["body"])["grant"]
        assert grant["tableAllowlist"] == ["orders", "customers"]
        assert grant["columnDenylist"] == {"customers": ["ssn"]}
        assert grant["allowedMetrics"] == ["revenue"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_declared_empty_overrides_are_persisted(self, mock_dao_cls):
        """SEMANTICS CHANGE (was: empty lists silently dropped at write time).

        An explicitly empty tableAllowlist means "no tables permitted" — a
        deny-all restriction the SQL firewall enforces. Dropping it created a
        grant MORE permissive than requested with a 201 and no warning. Absence
        of the field (the web console's blank input) still means unrestricted.
        """
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        mock_roles_dao.get.return_value = {"PK": f"NS#{VALID_NS_ID}", "SK": "ROLE#analyst"}

        resp = handler(
            _event(
                body={
                    "principalType": "User",
                    "principalId": "alice@company.com",
                    "role": "analyst",
                    "tableAllowlist": [],
                    "allowedMetrics": [],
                }
            ),
            None,
        )
        assert resp["statusCode"] == 201

        item = mock_mappings_dao.put.call_args[0][0]
        assert item["tableAllowlist"] == []
        assert item["allowedMetrics"] == []
        assert "columnDenylist" not in item  # omitted field stays omitted

        grant = json.loads(resp["body"])["grant"]
        assert grant["tableAllowlist"] == []
        assert grant["allowedMetrics"] == []

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_omitted_overrides_are_not_persisted(self, mock_dao_cls):
        """The counterpart: absence really does mean unrestricted (None)."""
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        mock_roles_dao.get.return_value = {"PK": f"NS#{VALID_NS_ID}", "SK": "ROLE#analyst"}

        resp = handler(
            _event(
                body={
                    "principalType": "User",
                    "principalId": "alice@company.com",
                    "role": "analyst",
                }
            ),
            None,
        )
        assert resp["statusCode"] == 201

        item = mock_mappings_dao.put.call_args[0][0]
        for opt in ("tableAllowlist", "columnDenylist", "allowedMetrics"):
            assert opt not in item

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_duplicate_grant_returns_409(self, mock_dao_cls):
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        mock_roles_dao.get.return_value = {"PK": f"NS#{VALID_NS_ID}", "SK": "ROLE#analyst"}
        mock_mappings_dao.put.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )

        resp = handler(
            _event(body={"principalType": "User", "principalId": "alice@company.com", "role": "analyst"}),
            None,
        )
        assert resp["statusCode"] == 409
        body = json.loads(resp["body"])
        assert "already exists" in body["message"].lower()

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_role_not_found_returns_404(self, mock_dao_cls):
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        mock_roles_dao.get.return_value = None

        resp = handler(
            _event(body={"principalType": "User", "principalId": "alice@company.com", "role": "nonexistent"}),
            None,
        )
        assert resp["statusCode"] == 404

    def test_invalid_namespace_id_returns_400(self):
        resp = handler(
            _event(namespace_id="bad!", body={"principalType": "User", "principalId": "alice", "role": "analyst"}),
            None,
        )
        assert resp["statusCode"] == 400

    def test_missing_required_fields_returns_400(self):
        resp = handler(_event(body={"principalType": "User"}), None)
        assert resp["statusCode"] == 400

    def test_invalid_principal_type_returns_400(self):
        resp = handler(
            _event(body={"principalType": "Invalid", "principalId": "alice", "role": "analyst"}),
            None,
        )
        assert resp["statusCode"] == 400

    @pytest.mark.parametrize(
        "bad_id",
        [
            "<a onclick=prompt(1);>ClickMe</a>",
            "alice@example.com<script>",
            'alice"onmouseover=1',
            "a#b@co.com",
            "Namespace::x",
            "x" * 257,
        ],
    )
    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_malformed_principal_id_returns_400(self, mock_dao_cls, bad_id):
        resp = handler(
            _event(body={"principalType": "User", "principalId": bad_id, "role": "analyst"}),
            None,
        )
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["message"].startswith("Invalid principalId")
        mock_dao_cls.assert_not_called()

    def test_missing_body_returns_400(self):
        event = _event()
        event["body"] = None
        resp = handler(event, None)
        assert resp["statusCode"] == 400

    def test_invalid_json_body_returns_400(self):
        event = _event()
        event["body"] = "not json{"
        resp = handler(event, None)
        assert resp["statusCode"] == 400

    @patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://test.example.com"}, clear=True)
    def test_missing_env_vars_returns_500(self):
        resp = handler(
            _event(body={"principalType": "User", "principalId": "alice", "role": "analyst"}),
            None,
        )
        assert resp["statusCode"] == 500


class TestCreateGrantOverrideValidation:
    """Validate type enforcement on optional grant override fields.

    These fields are passed to the SQL firewall as-is via the principal's
    profile, so malformed shapes must be rejected at write time.
    """

    def _valid_body(self, **overrides) -> dict:
        body = {"principalType": "User", "principalId": "alice@company.com", "role": "analyst"}
        body.update(overrides)
        return body

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_table_allowlist_string_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(tableAllowlist="orders")), None)
        assert resp["statusCode"] == 400
        assert "tableAllowlist" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_table_allowlist_dict_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(tableAllowlist={"orders": True})), None)
        assert resp["statusCode"] == 400
        assert "tableAllowlist" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_table_allowlist_with_non_string_member_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(tableAllowlist=["orders", 123])), None)
        assert resp["statusCode"] == 400
        assert "tableAllowlist" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_column_denylist_list_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(columnDenylist=["ssn", "password"])), None)
        assert resp["statusCode"] == 400
        assert "columnDenylist" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_column_denylist_string_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(columnDenylist="ssn")), None)
        assert resp["statusCode"] == 400
        assert "columnDenylist" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_column_denylist_value_not_list_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(columnDenylist={"customers": "ssn"})), None)
        assert resp["statusCode"] == 400
        assert "columnDenylist" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_column_denylist_value_with_non_string_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(columnDenylist={"customers": ["ssn", 42]})), None)
        assert resp["statusCode"] == 400
        assert "columnDenylist" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_allowed_metrics_string_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(allowedMetrics="revenue")), None)
        assert resp["statusCode"] == 400
        assert "allowedMetrics" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_allowed_metrics_with_non_string_member_returns_400(self, mock_dao_cls):
        mock_dao_cls.side_effect = [MagicMock(), MagicMock()]
        resp = handler(_event(body=self._valid_body(allowedMetrics=["revenue", None])), None)
        assert resp["statusCode"] == 400
        assert "allowedMetrics" in json.loads(resp["body"])["message"]

    @patch("coa_control_plane.grants.create_handler.DynamoDBDAO")
    def test_invalid_overrides_do_not_write_to_dynamodb(self, mock_dao_cls):
        mock_roles_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_roles_dao, mock_mappings_dao]
        resp = handler(_event(body=self._valid_body(tableAllowlist="orders")), None)
        assert resp["statusCode"] == 400
        # Validation runs before any DynamoDB call, so neither DAO should be touched.
        mock_roles_dao.get.assert_not_called()
        mock_mappings_dao.put.assert_not_called()
