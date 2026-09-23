# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_authorization.policy_loader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_authorization.policy_loader import load_policies_for_roles

pytestmark = pytest.mark.unit


class TestLoadPoliciesForRoles:
    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_returns_empty_string_when_no_roles(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao.get.return_value = None
        mock_dao_cls.return_value = mock_dao

        result = load_policies_for_roles([], table_name="roles", region="us-east-1")

        assert result == ""
        mock_dao.get.assert_not_called()

    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_returns_empty_string_when_no_policies_found(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao.get.return_value = None
        mock_dao_cls.return_value = mock_dao

        result = load_policies_for_roles(["missing-role"], table_name="roles", region="us-east-1")

        assert result == ""
        # Checks both GLOBAL and NAMESPACE_TEMPLATE partitions
        assert mock_dao.get.call_count == 2

    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_loads_policy_from_global_partition(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"cedarPolicy": "permit(principal, action, resource);"}
        mock_dao_cls.return_value = mock_dao

        result = load_policies_for_roles(["default"], table_name="roles", region="us-east-1")

        assert result == "permit(principal, action, resource);"
        # First match wins — only the GLOBAL partition is queried
        mock_dao.get.assert_called_once_with({"PK": "GLOBAL", "SK": "ROLE#default"})

    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_falls_back_to_namespace_template_partition(self, mock_dao_cls):
        mock_dao = MagicMock()
        # GLOBAL miss, NAMESPACE_TEMPLATE hit
        mock_dao.get.side_effect = [None, {"cedarPolicy": "permit(principal, action, resource);"}]
        mock_dao_cls.return_value = mock_dao

        result = load_policies_for_roles(["namespace-owner"], table_name="roles", region="us-east-1")

        assert result == "permit(principal, action, resource);"
        assert mock_dao.get.call_count == 2

    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_concatenates_multiple_role_policies_with_newline(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao.get.side_effect = [
            {"cedarPolicy": "policyA"},
            {"cedarPolicy": "policyB"},
        ]
        mock_dao_cls.return_value = mock_dao

        result = load_policies_for_roles(["roleA", "roleB"], table_name="roles", region="us-east-1")

        assert result == "policyA\npolicyB"

    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_item_without_cedar_policy_attr_is_skipped(self, mock_dao_cls):
        mock_dao = MagicMock()
        # Item exists in both partitions but lacks cedarPolicy attr
        mock_dao.get.return_value = {"someOtherAttr": "x"}
        mock_dao_cls.return_value = mock_dao

        result = load_policies_for_roles(["role"], table_name="roles", region="us-east-1")

        assert result == ""

    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_constructs_dao_with_table_and_region(self, mock_dao_cls):
        mock_dao = MagicMock()
        mock_dao.get.return_value = None
        mock_dao_cls.return_value = mock_dao

        load_policies_for_roles(["r"], table_name="my-roles", region="eu-west-1")

        mock_dao_cls.assert_called_once()
        assert mock_dao_cls.call_args.args == ("my-roles",)
        assert mock_dao_cls.call_args.kwargs["region"] == "eu-west-1"

    @patch("coa_authorization.policy_loader.DynamoDBDAO")
    def test_uses_sync_timeout_profile(self, mock_dao_cls):
        # Both known callers (control-plane authorizer, MCP grant resolver) are
        # on a synchronous request path — the DAO must fail fast, not inherit
        # the DAO's background 30-second/three-attempt default.
        mock_dao = MagicMock()
        mock_dao.get.return_value = None
        mock_dao_cls.return_value = mock_dao

        load_policies_for_roles(["r"], table_name="roles", region="us-east-1")

        config = mock_dao_cls.call_args.kwargs["config"]
        assert config.read_timeout == 8
        assert config.retries["max_attempts"] == 2
