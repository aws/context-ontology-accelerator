# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the List Principal Grants handler."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from coa_common.dao import PaginatedResult
from moto import mock_aws

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


def _event_me(email: str = "alice@company.com", groups: str = "", view_source_namespaces: str | None = "*") -> dict:
    # Default to a platform-wide source viewer so the serialization tests see the
    # full grant shape; withholding is covered in TestListPrincipalGrantsSchemaVisibility.
    authorizer: dict[str, str] = {"email": email, "groups": groups}
    if view_source_namespaces is not None:
        authorizer["viewSourceNamespaces"] = view_source_namespaces
    return {
        "httpMethod": "GET",
        "resource": "/principals/{principalId}/grants",
        "pathParameters": {"principalId": "me"},
        "requestContext": {"authorizer": authorizer},
    }


class TestListPrincipalGrantsSchemaVisibility:
    """The handler routes the caller's own listing through withhold_principal_grant_overrides."""

    @staticmethod
    def _item(resource_id: str) -> dict:
        return {
            "PK": f"Namespace::{resource_id}#User::alice@company.com",
            "SK": "ROLE#analyst",
            "resourceId": resource_id,
            "principalType": "User",
            "principalId": "alice@company.com",
            "role": "analyst",
            "grantedBy": "bob@company.com",
            "grantedAt": "2026-04-09T10:00:00Z",
            "tableAllowlist": [f"sentinel_tbl_{resource_id}"],
            "columnDenylist": {"t": [f"sentinel_col_{resource_id}"]},
            "allowedMetrics": ["revenue"],
        }

    def _run(self, mock_dao_cls, resource_ids: list[str], view_source_namespaces: str | None) -> dict[str, dict]:
        mock_dao_cls.return_value.query.return_value = PaginatedResult(items=[self._item(r) for r in resource_ids])
        resp = handler(_event_me(view_source_namespaces=view_source_namespaces), None)
        assert resp["statusCode"] == 200
        mock_dao_cls.return_value.query.assert_called()
        return {g["namespaceId"]: g for g in json.loads(resp["body"])["grants"]}

    @pytest.mark.parametrize(
        ("view_source_namespaces", "kept"),
        [
            ("*", {"ns-1", "ns-2", "GLOBAL"}),  # platform-wide viewSource: every grant
            ("ns-1", {"ns-1"}),  # GLOBAL grant is stripped without "*"
            ("ns-1,*", {"ns-1", "ns-2", "GLOBAL"}),
            ("", set()),
            (None, set()),  # key missing: fail closed
        ],
    )
    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_overrides_follow_view_source_namespaces(self, mock_dao_cls, view_source_namespaces, kept):
        grants = self._run(mock_dao_cls, ["ns-1", "ns-2", "GLOBAL"], view_source_namespaces)
        assert set(grants) == {"ns-1", "ns-2", "GLOBAL"}
        for ns, grant in grants.items():
            assert grant["allowedMetrics"] == ["revenue"]
            has = "tableAllowlist" in grant or "columnDenylist" in grant
            assert has is (ns in kept), ns
        body = json.dumps(grants)
        for ns in {"ns-1", "ns-2", "GLOBAL"} - kept:
            assert f"sentinel_tbl_{ns}" not in body and f"sentinel_col_{ns}" not in body


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
        mock_dao.query.return_value = PaginatedResult(
            items=[
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
        )

        resp = handler(_event_me(), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["grants"]) == 1
        assert body["grants"][0]["role"] == "analyst"

        # Verify PrincipalIndex GSI was used with sanitized email
        query_call = mock_dao.query.call_args[0][0]
        assert query_call.index_name == "PrincipalIndex"
        assert ":pk" in query_call.expression_values
        assert query_call.expression_values[":pk"] == "User::alice@company.com"

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_queries_user_and_groups(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[])

        resp = handler(_event_me("alice@co.com", "admins,viewers"), None)
        assert resp["statusCode"] == 200

        # Should query 3 times: User + 2 groups. Count via ``call_args_list``,
        # not ``call_count``: the handler fans out over a ``ThreadPoolExecutor``
        # and ``MagicMock.call_count`` uses a non-atomic ``+= 1`` that can lose
        # increments under concurrent access. ``call_args_list.append`` is
        # GIL-atomic and reflects the true count.
        assert len(mock_dao.query.call_args_list) == 3
        keys_queried = [call[0][0].expression_values[":pk"] for call in mock_dao.query.call_args_list]
        assert "User::alice@co.com" in keys_queried
        assert "Group::admins" in keys_queried
        assert "Group::viewers" in keys_queried

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_deduplicates_grants(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
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
        mock_dao.query.return_value = PaginatedResult(items=[grant_item])

        resp = handler(_event_me("alice@co.com", "team"), None)
        body = json.loads(resp["body"])
        assert len(body["grants"]) == 1

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_normalizes_email_to_lowercase(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[])

        resp = handler(_event_me("Alice@Company.COM"), None)
        assert resp["statusCode"] == 200

        key = mock_dao.query.call_args[0][0].expression_values[":pk"]
        assert key == "User::alice@company.com"

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_does_not_truncate_large_group_counts(self, mock_dao_cls):
        # Groups beyond the old cap of 10 are still queried.
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[])

        many_groups = ",".join(f"group{i}" for i in range(140))
        resp = handler(_event_me("a@b.com", many_groups), None)
        assert resp["statusCode"] == 200
        # 1 user + all 140 groups queried, nothing truncated.
        assert len(mock_dao.query.call_args_list) == 141

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_limits_groups_to_max_as_a_circuit_breaker(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[])

        many_groups = ",".join(f"group{i}" for i in range(300))
        resp = handler(_event_me("a@b.com", many_groups), None)
        assert resp["statusCode"] == 200
        # 1 user + max 250 groups = 251 queries; the circuit breaker still fires.
        assert len(mock_dao.query.call_args_list) == 251

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_skips_invalid_group_names(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[])

        resp = handler(_event_me("a@b.com", "valid-group,bad::group,ok_group"), None)
        assert resp["statusCode"] == 200
        # 1 user + 2 valid groups (bad::group skipped).
        assert len(mock_dao.query.call_args_list) == 3

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_fallback_to_claims_email(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        mock_dao.query.return_value = PaginatedResult(items=[])

        resp = handler(_event_me_claims("bob@co.com", "devs"), None)
        assert resp["statusCode"] == 200
        key = mock_dao.query.call_args_list[0][0][0].expression_values[":pk"]
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
        mock_dao.query.side_effect = Exception("DynamoDB throttled")

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
        mock_dao.query.return_value = PaginatedResult(items=[item])
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
        mock_dao.query.return_value = PaginatedResult(items=[self.BASE_ITEM])
        resp = handler(_event_me(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert "tableAllowlist" not in grant
        assert "columnDenylist" not in grant
        assert "allowedMetrics" not in grant

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_declared_empty_overrides_are_returned(self, mock_dao_cls):
        """A declared-empty override is a deny-all restriction and must stay visible to the principal."""
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {
            **self.BASE_ITEM,
            "tableAllowlist": [],
            "columnDenylist": {},
            "allowedMetrics": [],
        }
        mock_dao.query.return_value = PaginatedResult(items=[item])
        resp = handler(_event_me(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert grant["tableAllowlist"] == []
        assert grant["columnDenylist"] == {}
        assert grant["allowedMetrics"] == []

    @patch("coa_control_plane.grants.list_principal_grants_handler.DynamoDBDAO")
    def test_partial_overrides_only_set_fields_returned(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao_cls.return_value = mock_dao
        item = {**self.BASE_ITEM, "columnDenylist": {"customers": ["ssn"]}}
        mock_dao.query.return_value = PaginatedResult(items=[item])
        resp = handler(_event_me(), None)
        body = json.loads(resp["body"])
        grant = body["grants"][0]
        assert grant["columnDenylist"] == {"customers": ["ssn"]}
        assert "tableAllowlist" not in grant
        assert "allowedMetrics" not in grant


@pytest.fixture
def mappings_table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="test-mappings",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "principalKey", "resourceRoleKey")
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "PrincipalIndex",
                    "KeySchema": [
                        {"AttributeName": "principalKey", "KeyType": "HASH"},
                        {"AttributeName": "resourceRoleKey", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


def _put_grant(table, principal_key: str, ns: int, role: str = "analyst") -> None:
    ns_id = f"{ns:08d}-0000-4000-8000-000000000000"
    table.put_item(
        Item={
            "PK": f"Namespace::{ns_id}#{principal_key}",
            "SK": f"ROLE#{role}",
            "principalKey": principal_key,
            "resourceRoleKey": f"Namespace::{ns_id}#ROLE#{role}",
            "resourceId": ns_id,
            "principalType": principal_key.split("::")[0],
            "principalId": principal_key.split("::")[1],
            "role": role,
            "grantedBy": "admin@company.com",
            "grantedAt": "2026-04-09T10:00:00Z",
        }
    )


def _page(event: dict, **query: str) -> dict:
    event = {**event, "queryStringParameters": query or None}
    resp = handler(event, None)
    assert resp["statusCode"] == 200, resp["body"]
    return json.loads(resp["body"])


def test_list_principal_grants_pages_across_user_and_groups(mappings_table) -> None:
    for ns in range(3):
        _put_grant(mappings_table, "User::alice@company.com", ns)
    for ns in range(3, 5):
        _put_grant(mappings_table, "Group::admins", ns)
    _put_grant(mappings_table, "Group::viewers", 5)
    event = _event_me("alice@company.com", "admins,viewers")

    seen: list[str] = []
    token = None
    pages = 0
    while True:
        body = _page(event, maxResults="2", **({"nextToken": token} if token else {}))
        assert len(body["grants"]) <= 2
        seen.extend(g["namespaceId"] for g in body["grants"])
        pages += 1
        token = body.get("nextToken")
        if not token:
            break

    assert sorted(seen) == [f"{n:08d}-0000-4000-8000-000000000000" for n in range(6)]
    assert pages in (3, 4)  # an empty final page is allowed


def test_list_principal_grants_default_page_returns_everything(mappings_table) -> None:
    for ns in range(30):
        _put_grant(mappings_table, "User::alice@company.com", ns)
    body = _page(_event_me())
    assert len(body["grants"]) == 30
    assert "nextToken" not in body


def test_list_principal_grants_token_for_another_callers_key_is_rejected(mappings_table) -> None:
    _put_grant(mappings_table, "Group::admins", 1)
    _put_grant(mappings_table, "Group::admins", 2)
    body = _page(_event_me("alice@company.com", "admins"), maxResults="1")
    token = body["nextToken"]

    # bob is not in "admins": the cursor names a principal key bob does not hold.
    resp = handler({**_event_me("bob@company.com"), "queryStringParameters": {"nextToken": token}}, None)
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"]) == {"message": "Invalid nextToken"}


@pytest.mark.parametrize("token", ["!!!", "bm90LWpzb24", "x" * 8193])
def test_list_principal_grants_malformed_token_returns_400(token: str) -> None:
    resp = handler({**_event_me(), "queryStringParameters": {"nextToken": token}}, None)
    assert resp["statusCode"] == 400
    assert token not in resp["body"]


def test_list_principal_grants_token_with_foreign_start_key_is_rejected() -> None:
    import base64

    payload = {
        "p": "User::alice@company.com",
        "k": {"PK": "x", "SK": "y", "principalKey": "User::mallory", "resourceRoleKey": "z"},
    }
    token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    resp = handler({**_event_me(), "queryStringParameters": {"nextToken": token}}, None)
    assert resp["statusCode"] == 400


@pytest.mark.parametrize("value", ["0", "1001", "abc"])
def test_list_principal_grants_max_results_out_of_range_returns_400(value: str) -> None:
    resp = handler({**_event_me(), "queryStringParameters": {"maxResults": value}}, None)
    assert resp["statusCode"] == 400
