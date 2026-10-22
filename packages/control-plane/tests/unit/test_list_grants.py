# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the List Grants handler."""

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
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-mappings",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.grants.grant_id import encode_grant_id  # noqa: E402
from coa_control_plane.grants.list_handler import handler  # noqa: E402

pytestmark = pytest.mark.unit

VALID_NS_ID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_NS_ID = "11111111-1111-4111-8111-111111111111"

_NS_INDEX_KEY = {
    "PK": f"Namespace::{VALID_NS_ID}#User::z@co.com",
    "SK": "ROLE#data-analyst",
    "namespaceKey": f"NS#{VALID_NS_ID}",
    "principalRoleKey": "User::z@co.com#ROLE#data-analyst",
}
_PRINCIPAL_INDEX_KEY = {
    "PK": f"Namespace::{VALID_NS_ID}#User::z@co.com",
    "SK": "ROLE#data-analyst",
    "principalKey": "User::z@co.com",
    "resourceRoleKey": f"Namespace::{VALID_NS_ID}#ROLE#data-analyst",
}


def _token(decoded: object) -> str:
    return base64.b64encode(json.dumps(decoded).encode()).decode()


def _event(namespace_id: str = VALID_NS_ID, query_params: dict | None = None) -> dict:
    return {
        "httpMethod": "GET",
        "resource": "/namespaces/{namespaceId}/grants",
        "pathParameters": {"namespaceId": namespace_id},
        "queryStringParameters": query_params,
    }


class TestListGrants:
    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_list_grants_by_namespace(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(
            items=[
                {
                    "PK": f"Namespace::{VALID_NS_ID}#User::alice@company.com",
                    "SK": "ROLE#analyst",
                    "principalType": "User",
                    "principalId": "alice@company.com",
                    "role": "analyst",
                    "grantedBy": "bob@company.com",
                    "grantedAt": "2026-04-09T10:00:00Z",
                },
                {
                    "PK": f"Namespace::{VALID_NS_ID}#Group::data-team",
                    "SK": "ROLE#data-steward",
                    "principalType": "Group",
                    "principalId": "data-team",
                    "role": "data-steward",
                    "grantedBy": "bob@company.com",
                    "grantedAt": "2026-04-10T10:00:00Z",
                },
            ],
            last_evaluated_key=None,
        )

        resp = handler(_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["grants"]) == 2

        # Verify grantId is derived from PK+SK
        expected_id = encode_grant_id(f"Namespace::{VALID_NS_ID}#User::alice@company.com", "ROLE#analyst")
        assert body["grants"][0]["grantId"] == expected_id

        # Verify NamespaceGrantsIndex GSI was used
        query_call = mock_dao.query.call_args[0][0]
        assert query_call.index_name == "NamespaceGrantsIndex"

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_list_grants_by_principal_filter(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(
            items=[
                {
                    "PK": f"Namespace::{VALID_NS_ID}#User::alice@company.com",
                    "SK": "ROLE#analyst",
                    "principalType": "User",
                    "principalId": "alice@company.com",
                    "role": "analyst",
                    "grantedBy": "bob@company.com",
                    "grantedAt": "2026-04-09T10:00:00Z",
                },
            ],
            last_evaluated_key=None,
        )

        resp = handler(_event(query_params={"principal": "User::alice@company.com"}), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["grants"]) == 1

        # Verify PrincipalIndex GSI was used
        query_call = mock_dao.query.call_args[0][0]
        assert query_call.index_name == "PrincipalIndex"

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_list_grants_empty(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        resp = handler(_event(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["grants"] == []

    def test_invalid_namespace_id_returns_400(self):
        resp = handler(_event(namespace_id="bad!"), None)
        assert resp["statusCode"] == 400

    @patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://test.example.com"}, clear=True)
    def test_missing_env_vars_returns_500(self):
        resp = handler(_event(), None)
        assert resp["statusCode"] == 500


class TestListGrantsToGrantSummary:
    """Verify list_handler._to_grant_summary correctly handles optional override fields."""

    BASE_ITEM = {
        "PK": f"Namespace::{VALID_NS_ID}#User::alice@company.com",
        "SK": "ROLE#analyst",
        "principalType": "User",
        "principalId": "alice@company.com",
        "role": "analyst",
        "grantedBy": "bob@company.com",
        "grantedAt": "2026-04-09T10:00:00Z",
    }

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_overrides_present_with_data_are_returned(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {
            **self.BASE_ITEM,
            "tableAllowlist": ["orders", "customers"],
            "columnDenylist": {"customers": ["ssn"]},
            "allowedMetrics": ["revenue"],
        }
        mock_dao.query.return_value = PaginatedResult(items=[item], last_evaluated_key=None)
        resp = handler(_event(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert grant["tableAllowlist"] == ["orders", "customers"]
        assert grant["columnDenylist"] == {"customers": ["ssn"]}
        assert grant["allowedMetrics"] == ["revenue"]

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_overrides_absent_are_omitted(self, mock_dao_cls):
        """When DynamoDB items have no override attributes, the response must
        omit them entirely (sparse) — not return null/empty placeholders."""
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[self.BASE_ITEM], last_evaluated_key=None)
        resp = handler(_event(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert "tableAllowlist" not in grant
        assert "columnDenylist" not in grant
        assert "allowedMetrics" not in grant

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_declared_empty_overrides_are_returned(self, mock_dao_cls):
        """SEMANTICS CHANGE (was: empty treated as absent and omitted).

        A declared-empty tableAllowlist/allowedMetrics is a deny-all restriction
        the SQL firewall enforces; omitting it from the listing would make a
        deny-all grant indistinguishable from an unrestricted one to reviewers.
        """
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {
            **self.BASE_ITEM,
            "tableAllowlist": [],
            "columnDenylist": {},
            "allowedMetrics": [],
        }
        mock_dao.query.return_value = PaginatedResult(items=[item], last_evaluated_key=None)
        resp = handler(_event(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert grant["tableAllowlist"] == []
        assert grant["columnDenylist"] == {}
        assert grant["allowedMetrics"] == []

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_partial_overrides_only_set_fields_returned(self, mock_dao_cls):
        """Mixed item — only the set field is echoed; others are omitted."""
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {**self.BASE_ITEM, "tableAllowlist": ["orders"]}
        mock_dao.query.return_value = PaginatedResult(items=[item], last_evaluated_key=None)
        resp = handler(_event(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert grant["tableAllowlist"] == ["orders"]
        assert "columnDenylist" not in grant
        assert "allowedMetrics" not in grant


class TestListGrantsPagination:
    """Pagination support for the namespace grants list handler."""

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_passes_max_results_as_limit(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        resp = handler(_event(query_params={"maxResults": "7"}), None)
        assert resp["statusCode"] == 200
        assert mock_dao.query.call_args[0][0].limit == 7

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_returns_next_token_when_more_pages(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        last_key = {"PK": f"Namespace::{VALID_NS_ID}#User::z@co.com", "SK": "ROLE#data-analyst"}
        mock_dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=last_key)

        body = json.loads(handler(_event(), None)["body"])
        assert json.loads(base64.b64decode(body["nextToken"])) == last_key

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_decodes_next_token_into_start_key(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)
        start_key = _NS_INDEX_KEY
        token = _token(start_key)

        resp = handler(_event(query_params={"nextToken": token}), None)
        assert resp["statusCode"] == 200
        assert mock_dao.query.call_args[0][0].exclusive_start_key == start_key

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_principal_filtered_next_token_round_trips(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[], last_evaluated_key=None)

        query = {"principal": "User::z@co.com", "nextToken": _token(_PRINCIPAL_INDEX_KEY)}
        assert handler(_event(query_params=query), None)["statusCode"] == 200
        assert mock_dao.query.call_args[0][0].exclusive_start_key == _PRINCIPAL_INDEX_KEY

    @pytest.mark.parametrize(
        "decoded",
        [
            {"foo": "bar"},
            [1, 2],
            "x",
            {"PK": 1, "SK": 2},
            {"offset": 25},
            {"PK": "a", "SK": "b"},
            {**_NS_INDEX_KEY, "PK": 1},
            {**_NS_INDEX_KEY, "extra": "x"},
            # A key minted for another namespace.
            {**_NS_INDEX_KEY, "namespaceKey": f"NS#{OTHER_NS_ID}"},
            # A principal-index key replayed on the unfiltered listing.
            _PRINCIPAL_INDEX_KEY,
        ],
    )
    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_malformed_next_token_returns_400_without_query(self, mock_dao_cls, decoded):
        resp = handler(_event(query_params={"nextToken": _token(decoded)}), None)
        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["message"].startswith("Invalid nextToken")
        mock_dao_cls.return_value.query.assert_not_called()

    def test_smithy_example_next_token_returns_400(self):
        assert handler(_event(query_params={"nextToken": "eyJvZmZzZXQiOjI1fQ=="}), None)["statusCode"] == 400

    @pytest.mark.parametrize(
        "decoded",
        [
            {**_PRINCIPAL_INDEX_KEY, "principalKey": "User::mallory@co.com"},
            {**_PRINCIPAL_INDEX_KEY, "resourceRoleKey": f"Namespace::{OTHER_NS_ID}#ROLE#data-analyst"},
            _NS_INDEX_KEY,
        ],
    )
    def test_principal_filtered_foreign_next_token_returns_400(self, decoded):
        query = {"principal": "User::z@co.com", "nextToken": _token(decoded)}
        assert handler(_event(query_params=query), None)["statusCode"] == 400

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_start_key_rejected_by_dynamodb_returns_400(self, mock_dao_cls):
        mock_dao_cls.return_value.query.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "The provided starting key is invalid"}}, "Query"
        )
        resp = handler(_event(query_params={"nextToken": _token(_NS_INDEX_KEY)}), None)
        assert resp["statusCode"] == 400
        assert "starting key" not in resp["body"]

    @patch("coa_control_plane.grants.list_handler.DynamoDBDAO")
    def test_other_dynamodb_error_with_next_token_stays_500(self, mock_dao_cls):
        mock_dao_cls.return_value.query.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}}, "Query"
        )
        resp = handler(_event(query_params={"nextToken": _token(_NS_INDEX_KEY)}), None)
        assert resp["statusCode"] == 500

    def test_invalid_next_token_returns_400(self):
        bad = base64.b64encode(b"not-json").decode()
        resp = handler(_event(query_params={"nextToken": bad}), None)
        assert resp["statusCode"] == 400

    def test_out_of_range_max_results_returns_400(self):
        assert handler(_event(query_params={"maxResults": "0"}), None)["statusCode"] == 400
        assert handler(_event(query_params={"maxResults": "1000"}), None)["statusCode"] == 400
