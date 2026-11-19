# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SessionMetadataStore."""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from coa_serve.session_metadata import SessionMetadata, SessionMetadataStore
from moto import mock_aws

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_table():
    """Patch boto3 DynamoDB resource and return a mock table."""
    with patch("coa_serve.session_metadata.boto3") as mock_boto3:
        mock_ddb = MagicMock()
        mock_boto3.resource.return_value = mock_ddb
        mock_tbl = MagicMock()
        mock_ddb.Table.return_value = mock_tbl
        store = SessionMetadataStore(table_name="test-sessions", region="us-east-1")
        yield store, mock_tbl


@pytest.mark.asyncio
class TestCreate:
    async def test_creates_session_with_ulid(self, mock_table):
        store, tbl = mock_table
        result = await store.create("user-1")

        assert isinstance(result, SessionMetadata)
        assert result.user_id == "user-1"
        assert len(result.session_id) == 26  # ULID length
        assert result.status == "active"
        assert result.message_count == 0
        tbl.put_item.assert_called_once()

    async def test_put_item_includes_all_fields(self, mock_table):
        store, tbl = mock_table
        await store.create("user-2")

        item = tbl.put_item.call_args[1]["Item"]
        assert item["userId"] == "user-2"
        assert "sessionId" in item
        assert "createdAt" in item
        assert "lastActiveAt" in item
        assert item["status"] == "active"
        assert item["messageCount"] == 0
        assert item["ttl"] > 0


@pytest.mark.asyncio
class TestGetRecent:
    async def test_returns_none_when_no_sessions(self, mock_table):
        store, tbl = mock_table
        tbl.query.return_value = {"Items": []}

        result = await store.get_recent("user-1")
        assert result is None

    async def test_returns_most_recent_session(self, mock_table):
        store, tbl = mock_table
        tbl.query.return_value = {
            "Items": [
                {
                    "userId": "user-1",
                    "sessionId": "01JXYZ",
                    "createdAt": "2025-01-01T00:00:00",
                    "lastActiveAt": "2025-01-01T01:00:00",
                    "status": "active",
                    "messageCount": 3,
                    "ttl": 9999999999,
                }
            ]
        }

        result = await store.get_recent("user-1")
        assert result is not None
        assert result.session_id == "01JXYZ"
        assert result.message_count == 3

    async def test_queries_gsi_descending(self, mock_table):
        store, tbl = mock_table
        tbl.query.return_value = {"Items": []}

        await store.get_recent("user-1")
        call_kwargs = tbl.query.call_args[1]
        assert call_kwargs["IndexName"] == "userId-lastActiveAt-index"
        assert call_kwargs["ScanIndexForward"] is False
        assert call_kwargs["Limit"] == 1


@pytest.mark.asyncio
class TestUpdateActivity:
    async def test_updates_timestamp_and_increments_count(self, mock_table):
        store, tbl = mock_table
        await store.update_activity("user-1", "sess-1")

        tbl.update_item.assert_called_once()
        call_kwargs = tbl.update_item.call_args[1]
        assert call_kwargs["Key"] == {"userId": "user-1", "sessionId": "sess-1"}
        assert "lastActiveAt" in call_kwargs["UpdateExpression"]
        assert "messageCount" in call_kwargs["UpdateExpression"]

    async def test_aliases_nslastactiveat_reserved_word(self, mock_table):
        """Verify nsLastActiveAt is aliased via ExpressionAttributeNames (DynamoDB reserved word)."""
        store, tbl = mock_table
        await store.update_activity("user-1", "sess-1", namespace_id="ns-1")

        tbl.update_item.assert_called_once()
        call_kwargs = tbl.update_item.call_args[1]

        assert "#nsla = :nsc" in call_kwargs["UpdateExpression"]
        assert "nsLastActiveAt = :nsc" not in call_kwargs["UpdateExpression"]

        assert call_kwargs["ExpressionAttributeNames"]["#nsla"] == "nsLastActiveAt"

        assert ":nsc" in call_kwargs["ExpressionAttributeValues"]
        composite = call_kwargs["ExpressionAttributeValues"][":nsc"]
        assert composite.startswith("ns-1#")

    async def test_no_alias_when_namespace_omitted(self, mock_table):
        """When namespace_id is empty, nsLastActiveAt should not appear in the expression."""
        store, tbl = mock_table
        await store.update_activity("user-1", "sess-1", namespace_id="")

        tbl.update_item.assert_called_once()
        call_kwargs = tbl.update_item.call_args[1]

        assert "#nsla" not in call_kwargs["UpdateExpression"]
        assert "nsLastActiveAt" not in call_kwargs["UpdateExpression"]
        assert ":nsc" not in call_kwargs["ExpressionAttributeValues"]


@pytest.mark.asyncio
class TestValidateOwnership:
    async def test_returns_true_when_item_exists(self, mock_table):
        store, tbl = mock_table
        tbl.get_item.return_value = {"Item": {"userId": "user-1"}}

        result = await store.validate_ownership("user-1", "sess-1")
        assert result is True

    async def test_returns_false_when_item_missing(self, mock_table):
        store, tbl = mock_table
        tbl.get_item.return_value = {}

        result = await store.validate_ownership("user-1", "sess-x")
        assert result is False


@pytest.mark.asyncio
class TestDelete:
    async def test_deletes_item(self, mock_table):
        store, tbl = mock_table
        await store.delete("user-1", "sess-1")

        tbl.delete_item.assert_called_once_with(Key={"userId": "user-1", "sessionId": "sess-1"})


@pytest.fixture
def moto_store():
    """A SessionMetadataStore backed by a moto DynamoDB table.

    DynamoDB parses the UpdateExpression here; the MagicMock tests never execute it.
    """
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="test-sessions",
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "sessionId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "sessionId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        store = SessionMetadataStore(table_name="test-sessions", region="us-east-1")
        yield store


@pytest.mark.asyncio
class TestUpdateActivityAgainstRealTable:
    """Execute update_activity against moto so DynamoDB parses the UpdateExpression."""

    async def test_update_activity_with_namespace_is_valid_expression(self, moto_store):
        """A namespaced update builds a valid UpdateExpression (SET clauses contiguous, ADD last)."""
        created = await moto_store.create("user-1", namespace_id="ns-1")
        # Must not raise — this is the exact call _persist_turn makes on every turn.
        await moto_store.update_activity("user-1", created.session_id, namespace_id="ns-1")

        item = moto_store._table.get_item(  # noqa: SLF001
            Key={"userId": "user-1", "sessionId": created.session_id}
        )["Item"]
        # messageCount actually incremented (the whole UpdateItem committed atomically).
        assert item["messageCount"] == 1
        assert item["nsLastActiveAt"].startswith("ns-1#")

    async def test_update_activity_without_namespace_increments_count(self, moto_store):
        """The no-namespace path (SET + ADD, no nsLastActiveAt) must also increment."""
        created = await moto_store.create("user-2")
        await moto_store.update_activity("user-2", created.session_id)
        await moto_store.update_activity("user-2", created.session_id)

        item = moto_store._table.get_item(  # noqa: SLF001
            Key={"userId": "user-2", "sessionId": created.session_id}
        )["Item"]
        assert item["messageCount"] == 2
