# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.namespace_resolver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_common import namespace_resolver
from coa_common.namespace_resolver import resolve_namespace_id

pytestmark = pytest.mark.unit

_UUID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture(autouse=True)
def _clear_cache():
    namespace_resolver._RESOLVE_CACHE.clear()
    yield
    namespace_resolver._RESOLVE_CACHE.clear()


class TestResolveNamespaceId:
    def test_uuid_with_metadata_returns_as_is(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"PK": f"NS#{_UUID}"}}

        assert resolve_namespace_id(_UUID, table) == _UUID
        table.get_item.assert_called_once_with(Key={"PK": f"NS#{_UUID}", "SK": "METADATA"})

    def test_name_resolved_via_reservation_record(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"namespaceId": _UUID}}

        assert resolve_namespace_id("bird-benchmark", table) == _UUID
        table.get_item.assert_called_once_with(Key={"PK": "NS_NAME#bird-benchmark", "SK": "RESERVATION"})

    def test_uuid_without_metadata_falls_back_to_uuid(self):
        table = MagicMock()
        # First lookup (NS#uuid METADATA) misses, name reservation also misses
        table.get_item.side_effect = [{}, {}]

        assert resolve_namespace_id(_UUID, table) == _UUID

    def test_uuid_with_trailing_newline_is_not_returned_as_a_uuid(self):
        table = MagicMock()
        table.get_item.return_value = {}

        with pytest.raises(ValueError, match="Cannot resolve namespace"):
            resolve_namespace_id(f"{_UUID}\n", table)

    def test_unresolvable_name_raises(self):
        table = MagicMock()
        table.get_item.return_value = {}

        with pytest.raises(ValueError, match="Cannot resolve namespace"):
            resolve_namespace_id("nonexistent-name", table)

    def test_result_is_cached(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"namespaceId": _UUID}}

        first = resolve_namespace_id("bird-benchmark", table)
        second = resolve_namespace_id("bird-benchmark", table)

        assert first == second == _UUID
        # Second call served from cache — no additional DDB lookup
        table.get_item.assert_called_once()
