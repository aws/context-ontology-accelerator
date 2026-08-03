# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the DAO ABC and DynamoDB implementation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_common.dao import DynamoDBDAO, PaginatedResult, QueryParams
from coa_common.dao.base import DatastoreDAO

pytestmark = pytest.mark.unit


def _client_error(code: str = "InternalError", msg: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": msg}}, "Op")


class TestDatastoreDAOABC:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            DatastoreDAO()  # type: ignore[abstract]


@pytest.fixture()
def mock_table():
    with patch("coa_common.dao.dynamodb.boto3") as mock_boto3:
        mock_resource = MagicMock()
        mock_boto3.resource.return_value = mock_resource
        table = MagicMock()
        mock_resource.Table.return_value = table
        yield table, mock_resource


class TestDynamoDBDAOGet:
    def test_get_existing_item(self, mock_table):
        table, _ = mock_table
        table.get_item.return_value = {"Item": {"PK": "user#1", "name": "Alice"}}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        result = dao.get({"PK": "user#1"})

        assert result == {"PK": "user#1", "name": "Alice"}

    def test_get_missing_item(self, mock_table):
        table, _ = mock_table
        table.get_item.return_value = {}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        assert dao.get({"PK": "missing"}) is None

    def test_get_returns_none_on_resource_not_found(self, mock_table):
        table, _ = mock_table
        table.get_item.side_effect = _client_error(code="ResourceNotFoundException")

        dao = DynamoDBDAO("test-table", region="us-east-1")
        assert dao.get({"PK": "a"}) is None

    def test_get_raises_on_access_denied(self, mock_table):
        table, _ = mock_table
        table.get_item.side_effect = _client_error(code="AccessDeniedException")

        dao = DynamoDBDAO("test-table", region="us-east-1")
        with pytest.raises(ClientError):
            dao.get({"PK": "a"})

    def test_get_with_projection(self, mock_table):
        table, _ = mock_table
        table.get_item.return_value = {"Item": {"status": "ok"}}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.get({"PK": "a"}, projection=["status"])

        call_kwargs = table.get_item.call_args[1]
        assert "ProjectionExpression" in call_kwargs
        assert "ExpressionAttributeNames" in call_kwargs


class TestDynamoDBDAOGetAttribute:
    def test_returns_value(self, mock_table):
        table, _ = mock_table
        table.get_item.return_value = {"Item": {"status": "ingesting"}}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        assert dao.get_attribute({"PK": "a"}, "status") == "ingesting"

    def test_returns_none_for_missing(self, mock_table):
        table, _ = mock_table
        table.get_item.return_value = {}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        assert dao.get_attribute({"PK": "a"}, "status") is None


class TestDynamoDBDAOPut:
    def test_put_sets_timestamps(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.put({"PK": "user#1", "name": "Alice"})

        item = table.put_item.call_args.kwargs["Item"]
        assert "createdAt" in item
        assert "updatedAt" in item

    def test_put_with_condition(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.put({"PK": "user#1"}, condition="attribute_not_exists(PK)")

        assert table.put_item.call_args.kwargs["ConditionExpression"] == "attribute_not_exists(PK)"


class TestDynamoDBDAOUpdate:
    def test_update_builds_expression(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        result = dao.update({"PK": "user#1"}, {"name": "Bob"})

        assert result is True
        call_args = table.update_item.call_args.kwargs
        assert "SET" in call_args["UpdateExpression"]
        attr_names = list(call_args["ExpressionAttributeNames"].values())
        assert "updatedAt" in attr_names
        assert "name" in attr_names

    def test_update_no_auto_timestamp(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.update({"PK": "user#1"}, {"name": "Bob"}, auto_timestamp=False)

        call_args = table.update_item.call_args.kwargs
        attr_names = list(call_args["ExpressionAttributeNames"].values())
        assert "updatedAt" not in attr_names

    def test_update_returns_false_on_error_when_not_raising(self, mock_table):
        table, _ = mock_table
        table.update_item.side_effect = _client_error()

        dao = DynamoDBDAO("test-table", region="us-east-1")
        result = dao.update({"PK": "a"}, {"count": 1}, raise_on_error=False)
        assert result is False

    def test_update_raises_on_error_by_default(self, mock_table):
        table, _ = mock_table
        table.update_item.side_effect = _client_error()

        dao = DynamoDBDAO("test-table", region="us-east-1")
        with pytest.raises(ClientError):
            dao.update({"PK": "a"}, {"count": 1})

    def test_update_with_condition(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.update({"PK": "user#1"}, {"name": "Bob"}, condition="attribute_exists(PK)")

        call_args = table.update_item.call_args.kwargs
        assert call_args["ConditionExpression"] == "attribute_exists(PK)"


class TestDynamoDBDAODelete:
    def test_delete(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.delete({"PK": "user#1"})

        table.delete_item.assert_called_once_with(Key={"PK": "user#1"})


class TestDynamoDBDAOAtomicAdd:
    def test_single_increment_builds_add_expression(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.atomic_add({"PK": "METRICS#ns", "SK": "KGBUILD#ds"}, {"chunks_llm": 5})

        table.update_item.assert_called_once()
        kwargs = table.update_item.call_args.kwargs
        assert kwargs["Key"] == {"PK": "METRICS#ns", "SK": "KGBUILD#ds"}
        assert kwargs["UpdateExpression"] == "ADD #n0 :v0"
        assert kwargs["ExpressionAttributeNames"] == {"#n0": "chunks_llm"}
        assert kwargs["ExpressionAttributeValues"] == {":v0": 5}

    def test_multiple_increments_joined(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.atomic_add({"PK": "k"}, {"a": 1, "b": 2})

        kwargs = table.update_item.call_args.kwargs
        assert kwargs["UpdateExpression"] == "ADD #n0 :v0, #n1 :v1"
        assert kwargs["ExpressionAttributeNames"] == {"#n0": "a", "#n1": "b"}
        assert kwargs["ExpressionAttributeValues"] == {":v0": 1, ":v1": 2}

    def test_empty_increments_is_noop(self, mock_table):
        table, _ = mock_table

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.atomic_add({"PK": "k"}, {})

        table.update_item.assert_not_called()


class TestDynamoDBDAOQuery:
    def test_query_basic(self, mock_table):
        table, _ = mock_table
        table.query.return_value = {"Items": [{"PK": "user#1", "SK": "role#admin"}], "LastEvaluatedKey": None}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        result = dao.query(QueryParams(key_condition="PK = :pk", expression_values={":pk": "user#1"}))

        assert isinstance(result, PaginatedResult)
        assert len(result.items) == 1

    def test_query_with_index_and_limit(self, mock_table):
        table, _ = mock_table
        table.query.return_value = {"Items": [], "LastEvaluatedKey": None}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.query(
            QueryParams(
                key_condition="GSI1PK = :pk",
                expression_values={":pk": "ns#sales"},
                index_name="GSI1",
                limit=10,
            )
        )

        call_args = table.query.call_args.kwargs
        assert call_args["IndexName"] == "GSI1"
        assert call_args["Limit"] == 10


class TestDynamoDBDAOQueryAll:
    """query_all must follow LastEvaluatedKey to exhaustion.

    DynamoDB caps a query response at 1 MB regardless of how many items match, so
    a caller reading only ``result.items`` silently truncates. For authorization
    reads that means roles / data restrictions disappear with no error.
    """

    def test_follows_pagination_cursor_across_pages(self, mock_table):
        table, _ = mock_table
        table.query.side_effect = [
            {"Items": [{"PK": "a"}], "LastEvaluatedKey": {"PK": "a"}},
            {"Items": [{"PK": "b"}], "LastEvaluatedKey": {"PK": "b"}},
            {"Items": [{"PK": "c"}]},
        ]

        dao = DynamoDBDAO("test-table", region="us-east-1")
        items = dao.query_all(QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"}))

        assert [i["PK"] for i in items] == ["a", "b", "c"]
        assert table.query.call_count == 3

    def test_passes_cursor_as_exclusive_start_key(self, mock_table):
        table, _ = mock_table
        table.query.side_effect = [
            {"Items": [{"PK": "a"}], "LastEvaluatedKey": {"PK": "a", "SK": "s"}},
            {"Items": [{"PK": "b"}]},
        ]

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.query_all(QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"}))

        assert table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"PK": "a", "SK": "s"}

    def test_single_page_makes_one_call(self, mock_table):
        table, _ = mock_table
        table.query.return_value = {"Items": [{"PK": "a"}]}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        items = dao.query_all(QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"}))

        assert len(items) == 1
        assert table.query.call_count == 1

    def test_empty_result(self, mock_table):
        table, _ = mock_table
        table.query.return_value = {"Items": []}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        assert dao.query_all(QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"})) == []

    def test_preserves_index_and_expression_names_on_every_page(self, mock_table):
        table, _ = mock_table
        table.query.side_effect = [
            {"Items": [{"PK": "a"}], "LastEvaluatedKey": {"PK": "a"}},
            {"Items": [{"PK": "b"}]},
        ]

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.query_all(
            QueryParams(
                key_condition="#pk = :pk",
                expression_values={":pk": "x"},
                expression_names={"#pk": "principalKey"},
                index_name="PrincipalIndex",
            )
        )

        for call in table.query.call_args_list:
            assert call.kwargs["IndexName"] == "PrincipalIndex"
            assert call.kwargs["ExpressionAttributeNames"] == {"#pk": "principalKey"}

    def test_honours_a_caller_supplied_start_cursor(self, mock_table):
        table, _ = mock_table
        table.query.return_value = {"Items": [{"PK": "b"}]}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.query_all(
            QueryParams(
                key_condition="PK = :pk",
                expression_values={":pk": "x"},
                exclusive_start_key={"PK": "a"},
            )
        )

        assert table.query.call_args.kwargs["ExclusiveStartKey"] == {"PK": "a"}

    def test_raises_rather_than_returning_a_partial_set(self, mock_table):
        """Hitting the page bound must fail loudly — silent truncation is the bug."""
        table, _ = mock_table
        table.query.return_value = {"Items": [{"PK": "a"}], "LastEvaluatedKey": {"PK": "a"}}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        with pytest.raises(RuntimeError, match="without exhausting the cursor"):
            dao.query_all(
                QueryParams(key_condition="PK = :pk", expression_values={":pk": "x"}),
                max_pages=3,
            )

        assert table.query.call_count == 3


class TestDynamoDBDAOBatchGet:
    def test_batch_get(self, mock_table):
        _, mock_resource = mock_table
        mock_resource.batch_get_item.return_value = {"Responses": {"test-table": [{"PK": "a"}, {"PK": "b"}]}}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        results = dao.batch_get([{"PK": "a"}, {"PK": "b"}])

        assert len(results) == 2

    def test_batch_get_retries_unprocessed_keys(self, mock_table):
        _, mock_resource = mock_table
        mock_resource.batch_get_item.side_effect = [
            {
                "Responses": {"test-table": [{"PK": "a"}]},
                "UnprocessedKeys": {"test-table": {"Keys": [{"PK": "b"}]}},
            },
            {"Responses": {"test-table": [{"PK": "b"}]}},
        ]

        dao = DynamoDBDAO("test-table", region="us-east-1")
        results = dao.batch_get([{"PK": "a"}, {"PK": "b"}])

        assert len(results) == 2
        assert mock_resource.batch_get_item.call_count == 2


class TestDynamoDBDAOBatchDelete:
    def test_batch_delete_single_chunk(self, mock_table):
        _, mock_resource = mock_table
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client
        mock_client.batch_write_item.return_value = {"UnprocessedItems": {}}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.batch_delete([{"PK": "a", "SK": "1"}, {"PK": "b", "SK": "2"}])

        mock_client.batch_write_item.assert_called_once()
        request_items = mock_client.batch_write_item.call_args.kwargs["RequestItems"]["test-table"]
        assert len(request_items) == 2

    def test_batch_delete_retries_unprocessed(self, mock_table):
        _, mock_resource = mock_table
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client
        unprocessed = [{"DeleteRequest": {"Key": {"PK": "b", "SK": "2"}}}]
        mock_client.batch_write_item.side_effect = [
            {"UnprocessedItems": {"test-table": unprocessed}},
            {"UnprocessedItems": {}},
        ]

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.batch_delete([{"PK": "a", "SK": "1"}, {"PK": "b", "SK": "2"}])

        assert mock_client.batch_write_item.call_count == 2

    def test_batch_delete_empty_list(self, mock_table):
        _, mock_resource = mock_table
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client

        dao = DynamoDBDAO("test-table", region="us-east-1")
        dao.batch_delete([])

        mock_client.batch_write_item.assert_not_called()

    def test_batch_delete_raises_on_retry_exhaustion(self, mock_table):
        _, mock_resource = mock_table
        mock_client = MagicMock()
        mock_resource.meta.client = mock_client
        unprocessed = [{"DeleteRequest": {"Key": {"PK": "b", "SK": "2"}}}]
        mock_client.batch_write_item.return_value = {"UnprocessedItems": {"test-table": unprocessed}}

        dao = DynamoDBDAO("test-table", region="us-east-1")
        with pytest.raises(RuntimeError, match="unprocessed items"):
            dao.batch_delete([{"PK": "a", "SK": "1"}, {"PK": "b", "SK": "2"}], _max_retries=1)
