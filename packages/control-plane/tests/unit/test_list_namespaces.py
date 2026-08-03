# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the List Namespaces handler."""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from coa_common.dao import PaginatedResult

os.environ.update(
    {
        "NAMESPACES_TABLE": "test-namespaces",
        "ROLES_TABLE": "test-roles",
        "RESOURCE_ROLE_MAPPINGS_TABLE": "test-role-mappings",
        "DATAZONE_DOMAIN_ID": "dzd-test123",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)

from coa_control_plane.namespace.list_handler import handler  # noqa: E402

pytestmark = pytest.mark.unit


def _event(
    method: str = "GET",
    global_roles: str = "",
    user_id: str = "alice@co.com",
    groups: str = "",
    query_params: dict | None = None,
) -> dict:
    return {
        "httpMethod": method,
        "path": "/namespaces",
        "body": None,
        "headers": {},
        "queryStringParameters": query_params,
        "requestContext": {
            "authorizer": {
                "userId": user_id,
                "globalRoles": global_roles,
                "groups": groups,
            }
        },
    }


NS_ITEM = {
    "PK": "NS#11111111-1111-1111-1111-111111111111",
    "SK": "METADATA",
    "namespaceId": "11111111-1111-1111-1111-111111111111",
    "name": "sales",
    "displayName": "Sales",
    "status": "ACTIVE",
    "sourceCount": 3,
}

NS_ITEM_2 = {
    "PK": "NS#22222222-2222-2222-2222-222222222222",
    "SK": "METADATA",
    "namespaceId": "22222222-2222-2222-2222-222222222222",
    "name": "marketing",
    "status": "ACTIVE",
    "sourceCount": 0,
}


class TestHttpMethod:
    def test_post_returns_405(self):
        resp = handler(_event(method="POST"), None)
        assert resp["statusCode"] == 405
        assert resp["headers"]["Allow"] == "GET"

    def test_put_returns_405(self):
        resp = handler(_event(method="PUT"), None)
        assert resp["statusCode"] == 405


class TestResolveAccessibleNamespaceIds:
    """The namespace reader must query principal keys with the shared encoding."""

    def test_encodes_special_chars_in_principal_keys(self):
        from coa_control_plane.namespace.list_handler import resolve_accessible_namespace_ids

        mappings_dao = MagicMock()
        mappings_dao.query_all.return_value = []

        resolve_accessible_namespace_ids(mappings_dao, "foo+123@co.com", ["group/eng"])

        queried_pks = [call.args[0].expression_values[":pk"] for call in mappings_dao.query_all.call_args_list]
        assert "User::foo%2B123@co.com" in queried_pks
        assert "Group::group%2Feng" in queried_pks


class TestGlobalRoleUser:
    """Users with global roles see all namespaces (no status filter)."""

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_returns_all_namespaces(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_ns_dao.scan.return_value = PaginatedResult(items=[NS_ITEM, NS_ITEM_2])

        resp = handler(_event(global_roles="platform-admin"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 2
        assert "nextToken" not in body

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_includes_all_statuses(self, mock_dao_cls):
        """Global users see archived namespaces too."""
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        archived_item = {**NS_ITEM, "status": "ARCHIVED"}
        mock_ns_dao.scan.return_value = PaginatedResult(items=[archived_item, NS_ITEM_2])

        resp = handler(_event(global_roles="platform-admin"), None)

        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 2
        statuses = {ns["status"] for ns in body["namespaces"]}
        assert "ARCHIVED" in statuses

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_pagination_token_returned(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_ns_dao.scan.return_value = PaginatedResult(
            items=[NS_ITEM],
            last_evaluated_key={"PK": "NS#11111111-1111-1111-1111-111111111111", "SK": "METADATA"},
        )

        resp = handler(_event(global_roles="platform-viewer"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "nextToken" in body

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_next_token_passed_to_scan(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_ns_dao.scan.return_value = PaginatedResult(items=[NS_ITEM_2])

        token = base64.b64encode(
            json.dumps({"PK": "NS#11111111-1111-1111-1111-111111111111", "SK": "METADATA"}).encode()
        ).decode()
        resp = handler(_event(global_roles="platform-admin", query_params={"nextToken": token}), None)

        assert resp["statusCode"] == 200
        mock_ns_dao.scan.assert_called_once()
        call_kwargs = mock_ns_dao.scan.call_args[1]
        assert call_kwargs["exclusive_start_key"] == {"PK": "NS#11111111-1111-1111-1111-111111111111", "SK": "METADATA"}

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_scan_loops_until_limit_reached(self, mock_dao_cls):
        """Verifies the scan loops when first pass returns fewer items than limit."""
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_ns_dao.scan.side_effect = [
            PaginatedResult(
                items=[],
                last_evaluated_key={"PK": "NS#11111111-1111-1111-1111-111111111111", "SK": "METADATA"},
            ),
            PaginatedResult(items=[NS_ITEM_2]),
        ]

        resp = handler(_event(global_roles="platform-admin"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 1
        assert mock_ns_dao.scan.call_count == 2

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_single_page_over_limit_with_no_last_evaluated_key_still_paginates(self, mock_dao_cls):
        """Regression: a single Scan page can return MORE matching items than
        `limit` (the over-fetch is `limit * 3`) while the underlying DynamoDB
        scan is simultaneously exhausted (no LastEvaluatedKey — the raw table
        had fewer total items than the Limit passed to Scan). The handler
        must NOT drop the leftover items past the truncation point just
        because `last_evaluated_key` is absent — it must still emit a
        nextToken derived from the last item actually returned, so a
        follow-up call resumes and eventually surfaces every matching item.
        """
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        third_item = {**NS_ITEM_2, "PK": "NS#33333333-3333-3333-3333-333333333333", "name": "extra"}
        # One Scan call returns 3 matching items with NO LastEvaluatedKey
        # (the whole table was scanned in a single page), while the caller
        # only asked for maxResults=2.
        mock_ns_dao.scan.return_value = PaginatedResult(
            items=[NS_ITEM, NS_ITEM_2, third_item],
            last_evaluated_key=None,
        )

        resp = handler(_event(global_roles="platform-admin", query_params={"maxResults": "2"}), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 2
        # The bug: this key assertion used to fail because out_token was
        # None (last_evaluated_key was None), silently dropping `third_item`
        # with no way for the client to ever retrieve it.
        assert "nextToken" in body, (
            "nextToken must be present when the scan page had more matching "
            "items than the requested page size, even if the underlying "
            "DynamoDB scan itself returned no LastEvaluatedKey"
        )

        # Resuming with that token must surface the dropped item, not repeat
        # the ones already returned or return nothing.
        mock_ns_dao.scan.reset_mock()
        mock_ns_dao.scan.return_value = PaginatedResult(items=[third_item], last_evaluated_key=None)
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]
        resp2 = handler(
            _event(global_roles="platform-admin", query_params={"maxResults": "2", "nextToken": body["nextToken"]}),
            None,
        )
        assert resp2["statusCode"] == 200
        body2 = json.loads(resp2["body"])
        assert [ns["name"] for ns in body2["namespaces"]] == ["extra"]
        # The resume call must pass NS_ITEM_2's key (the last item actually
        # returned on the first call) as exclusive_start_key, not None.
        call_kwargs = mock_ns_dao.scan.call_args[1]
        assert call_kwargs["exclusive_start_key"] == {"PK": NS_ITEM_2["PK"], "SK": NS_ITEM_2["SK"]}


class TestScopedUser:
    """Users without global roles see only namespaces they have roles for."""

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_returns_only_assigned_namespaces(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_mappings_dao.query_all.return_value = [
            {"resourceType": "Namespace", "resourceId": "11111111-1111-1111-1111-111111111111", "role": "owner"}
        ]
        mock_ns_dao.batch_get.return_value = [NS_ITEM]

        resp = handler(_event(user_id="alice@co.com"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 1
        assert body["namespaces"][0]["namespaceId"] == "11111111-1111-1111-1111-111111111111"

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_no_roles_returns_empty(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_mappings_dao.query_all.return_value = []

        resp = handler(_event(user_id="nobody@co.com"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["namespaces"] == []

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_group_roles_included(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_mappings_dao.query_all.side_effect = [
            [],
            [
                {
                    "resourceType": "Namespace",
                    "resourceId": "22222222-2222-2222-2222-222222222222",
                    "role": "data-steward",
                }
            ],
        ]
        mock_ns_dao.batch_get.return_value = [NS_ITEM_2]

        resp = handler(_event(user_id="bob@co.com", groups="engineering"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 1
        assert body["namespaces"][0]["namespaceId"] == "22222222-2222-2222-2222-222222222222"

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_deduplicates_namespace_ids(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_mappings_dao.query_all.side_effect = [
            [{"resourceType": "Namespace", "resourceId": "11111111-1111-1111-1111-111111111111", "role": "owner"}],
            [
                {
                    "resourceType": "Namespace",
                    "resourceId": "11111111-1111-1111-1111-111111111111",
                    "role": "data-steward",
                }
            ],
        ]
        mock_ns_dao.batch_get.return_value = [NS_ITEM]

        resp = handler(_event(user_id="alice@co.com", groups="team-a"), None)

        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 1
        mock_ns_dao.batch_get.assert_called_once()
        keys = mock_ns_dao.batch_get.call_args[0][0]
        assert len(keys) == 1

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_pagination_for_scoped_user(self, mock_dao_cls):
        """Scoped users get paginated results with nextToken."""
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        # User has access to 3 namespaces
        mock_mappings_dao.query_all.return_value = [
            {"resourceType": "Namespace", "resourceId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "role": "owner"},
            {"resourceType": "Namespace", "resourceId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "role": "owner"},
            {"resourceType": "Namespace", "resourceId": "cccccccc-cccc-cccc-cccc-cccccccccccc", "role": "owner"},
        ]
        mock_ns_dao.batch_get.return_value = [
            {
                **NS_ITEM,
                "namespaceId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "PK": "NS#aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "name": "alpha",
            },
            {
                **NS_ITEM,
                "namespaceId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "PK": "NS#bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "name": "beta",
            },
            {
                **NS_ITEM,
                "namespaceId": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "PK": "NS#cccccccc-cccc-cccc-cccc-cccccccccccc",
                "name": "gamma",
            },
        ]

        # Request page 1 with limit 2
        resp = handler(_event(user_id="alice@co.com", query_params={"maxResults": "2"}), None)
        body = json.loads(resp["body"])
        assert len(body["namespaces"]) == 2
        assert body["namespaces"][0]["name"] == "alpha"
        assert body["namespaces"][1]["name"] == "beta"
        assert "nextToken" in body

        # Request page 2 using the token
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]
        mock_mappings_dao.query_all.return_value = [
            {"resourceType": "Namespace", "resourceId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "role": "owner"},
            {"resourceType": "Namespace", "resourceId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "role": "owner"},
            {"resourceType": "Namespace", "resourceId": "cccccccc-cccc-cccc-cccc-cccccccccccc", "role": "owner"},
        ]
        resp2 = handler(
            _event(user_id="alice@co.com", query_params={"maxResults": "2", "nextToken": body["nextToken"]}),
            None,
        )
        body2 = json.loads(resp2["body"])
        assert len(body2["namespaces"]) == 1
        assert body2["namespaces"][0]["name"] == "gamma"
        assert "nextToken" not in body2

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_queries_with_begins_with_namespace_prefix(self, mock_dao_cls):
        """Verifies the PrincipalIndex query uses begins_with to filter by resource type."""
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_mappings_dao.query_all.return_value = [
            {"resourceType": "Namespace", "resourceId": "11111111-1111-1111-1111-111111111111", "role": "owner"}
        ]
        mock_ns_dao.batch_get.return_value = [NS_ITEM]

        handler(_event(user_id="alice@co.com"), None)

        call_args = mock_mappings_dao.query_all.call_args[0][0]
        assert "begins_with" in call_args.key_condition
        assert call_args.expression_values[":prefix"] == "Namespace::"


class TestInputValidation:
    """Pydantic validation of query params."""

    def test_malformed_base64_returns_400(self):
        resp = handler(_event(global_roles="platform-admin", query_params={"nextToken": "not-valid!@#"}), None)
        assert resp["statusCode"] == 400
        assert "nextToken" in json.loads(resp["body"])["message"]

    def test_invalid_json_token_returns_400(self):
        token = base64.b64encode(b"not-json").decode()
        resp = handler(_event(global_roles="platform-admin", query_params={"nextToken": token}), None)
        assert resp["statusCode"] == 400

    def test_max_results_over_limit_returns_400(self):
        resp = handler(_event(global_roles="platform-admin", query_params={"maxResults": "999"}), None)
        assert resp["statusCode"] == 400


class TestResponseShape:
    """Verify the response matches the Smithy ListNamespacesOutput shape."""

    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_namespace_summary_fields(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_ns_dao.scan.return_value = PaginatedResult(items=[NS_ITEM])

        resp = handler(_event(global_roles="platform-admin"), None)

        body = json.loads(resp["body"])
        ns = body["namespaces"][0]
        assert ns["namespaceId"] == "11111111-1111-1111-1111-111111111111"
        assert ns["name"] == "sales"
        assert ns["displayName"] == "Sales"
        assert ns["status"] == "ACTIVE"
        assert ns["sourceCount"] == 3


class TestErrorHandling:
    @patch("coa_control_plane.namespace.list_handler.DynamoDBDAO")
    def test_unexpected_error_returns_500(self, mock_dao_cls):
        mock_ns_dao = MagicMock()
        mock_mappings_dao = MagicMock()
        mock_dao_cls.side_effect = [mock_ns_dao, mock_mappings_dao]

        mock_ns_dao.scan.side_effect = RuntimeError("DDB failure")

        resp = handler(_event(global_roles="platform-admin"), None)

        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert body["message"] == "Internal server error"
