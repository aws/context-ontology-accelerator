# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the caller-supplied DynamoDB resume-key checks."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from coa_common.pagination import check_start_key, is_rejected_start_key

pytestmark = pytest.mark.unit

_ATTRS = ("PK", "SK", "namespaceKey", "principalRoleKey")
_KEY = {"PK": "p", "SK": "s", "namespaceKey": "NS#a", "principalRoleKey": "User::x#ROLE#r"}


class TestCheckStartKey:
    def test_well_formed_key_is_returned(self):
        assert check_start_key(dict(_KEY), _ATTRS, expected={"namespaceKey": "NS#a"}) == _KEY

    @pytest.mark.parametrize(
        "key",
        [
            {"foo": "bar"},
            [1, 2],
            "x",
            None,
            {"PK": 1, "SK": 2},
            {"offset": 25},
            {"PK": "a", "SK": "b"},
            {**_KEY, "extra": "x"},
            {**_KEY, "PK": 1},
            {**_KEY, "SK": ""},
            {**_KEY, "SK": {"S": "s"}},
        ],
    )
    def test_malformed_key_raises(self, key):
        with pytest.raises(ValueError, match="not a key for this listing"):
            check_start_key(key, _ATTRS)

    def test_key_for_another_partition_raises(self):
        with pytest.raises(ValueError):
            check_start_key(dict(_KEY), _ATTRS, expected={"namespaceKey": "NS#b"})

    def test_key_outside_sort_key_prefix_raises(self):
        with pytest.raises(ValueError):
            check_start_key(dict(_KEY), _ATTRS, prefixes={"principalRoleKey": "Group::"})

    def test_key_inside_sort_key_prefix_passes(self):
        assert check_start_key(dict(_KEY), _ATTRS, prefixes={"principalRoleKey": "User::"}) == _KEY

    def test_error_message_never_echoes_the_key(self):
        with pytest.raises(ValueError) as exc_info:
            check_start_key({"PK": "<script>"}, _ATTRS)
        assert "<script>" not in str(exc_info.value)


class TestIsRejectedStartKey:
    def test_validation_exception_is_rejection(self):
        exc = ClientError({"Error": {"Code": "ValidationException", "Message": "bad key"}}, "Query")
        assert is_rejected_start_key(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}}, "Query"),
            ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Query"),
            RuntimeError("ValidationException"),
        ],
    )
    def test_other_errors_are_not_rejection(self, exc):
        assert is_rejected_start_key(exc) is False
