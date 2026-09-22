# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB implementation of the DatastoreDAO."""

from __future__ import annotations

import logging
import time
from typing import Any

import boto3
from boto3.dynamodb.conditions import ConditionBase, Key  # noqa: F401 — re-exported for callers
from botocore.config import Config
from botocore.exceptions import ClientError

from coa_common.aws_config import async_boto_config
from coa_common.dao.base import DatastoreDAO, PaginatedResult, QueryParams

logger = logging.getLogger(__name__)

_BATCH_GET_LIMIT = 100
_BATCH_WRITE_LIMIT = 25


class DynamoDBDAO(DatastoreDAO):
    """Concrete DAO backed by a single DynamoDB table.

    Parameters
    ----------
    table_name:
        The DynamoDB table name.
    region:
        AWS region. **Required**.
    config:
        Optional botocore client configuration. Background callers retain the
        shared async profile when omitted; synchronous request paths can supply
        the fail-fast profile explicitly.
    """

    def __init__(self, table_name: str, *, region: str, config: Config | None = None) -> None:
        """Bind the DAO to a DynamoDB table resource.

        Args:
            table_name: The DynamoDB table name.
            region: AWS region hosting the table.
            config: Optional botocore timeout/retry configuration.
        """
        self._table_name = table_name
        self._resource = boto3.resource(
            "dynamodb",
            region_name=region,
            config=config if config is not None else async_boto_config(),
        )
        self._table = self._resource.Table(table_name)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(
        self,
        key: dict[str, Any],
        *,
        projection: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a single item by primary key.

        Args:
            key: Primary key attributes identifying the item.
            projection: Optional attribute names to project; fetches all when omitted.

        Returns:
            The item as a dict, or None if it does not exist or the table is missing.
        """
        try:
            kwargs: dict[str, Any] = {"Key": key}
            if projection:
                attr_names = {f"#a{i}": attr for i, attr in enumerate(projection)}
                kwargs["ProjectionExpression"] = ", ".join(attr_names.keys())
                kwargs["ExpressionAttributeNames"] = attr_names
            resp = self._table.get_item(**kwargs)
            return resp.get("Item")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.warning(
                    "DDB get failed", extra={"table": self._table_name, "key_attrs": list(key.keys())}, exc_info=True
                )
                return None
            raise

    def get_attribute(self, key: dict[str, Any], attribute: str) -> Any | None:
        """Fetch a single attribute of an item by primary key.

        Args:
            key: Primary key attributes identifying the item.
            attribute: Name of the attribute to project and return.

        Returns:
            The attribute value, or None if the item or attribute is absent.
        """
        item = self.get(key, projection=[attribute])
        return item.get(attribute) if item else None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put(
        self,
        item: dict[str, Any],
        *,
        condition: str | ConditionBase | None = None,
        auto_timestamp: bool = True,
    ) -> None:
        """Write an item, optionally guarded by a condition expression.

        Args:
            item: The full item to write.
            condition: Optional condition expression that must hold for the write.
            auto_timestamp: When True, set ``createdAt`` (if absent) and ``updatedAt``.
        """
        if auto_timestamp:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            item.setdefault("createdAt", now)
            item["updatedAt"] = now
        kwargs: dict[str, Any] = {"Item": item}
        if condition:
            kwargs["ConditionExpression"] = condition
        self._table.put_item(**kwargs)

    def update(
        self,
        key: dict[str, Any],
        update_fields: dict[str, Any],
        *,
        remove_fields: list[str] | None = None,
        condition: str | None = None,
        condition_values: dict[str, Any] | None = None,
        condition_names: dict[str, str] | None = None,
        auto_timestamp: bool = True,
        raise_on_error: bool = True,
    ) -> bool:
        """Apply SET/REMOVE updates to an item, optionally under a condition.

        Args:
            key: Primary key attributes identifying the item.
            update_fields: Attribute name → new value pairs to SET.
            remove_fields: Attribute names to REMOVE from the item.
            condition: Optional condition expression that must hold for the update.
            condition_values: Placeholder → value bindings for ``condition``.
            condition_names: Alias → attribute-name bindings for ``condition``.
            auto_timestamp: When True, set ``updatedAt`` unless already provided.
            raise_on_error: When True, re-raise ClientError; otherwise return False.

        Returns:
            True on success, False when the update failed and ``raise_on_error`` is False.

        Raises:
            ValueError: If a condition alias/placeholder collides with a generated SET one.
            ClientError: If the update fails and ``raise_on_error`` is True.
        """
        if auto_timestamp and "updatedAt" not in update_fields:
            update_fields = {
                **update_fields,
                "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}
        parts: list[str] = []

        for i, (field_name, value) in enumerate(update_fields.items()):
            alias = f"#a{i}"
            placeholder = f":v{i}"
            attr_names[alias] = field_name
            attr_values[placeholder] = value
            parts.append(f"{alias} = {placeholder}")

        update_expr = f"SET {', '.join(parts)}"
        if remove_fields:
            remove_aliases = []
            for j, field_name in enumerate(remove_fields):
                alias = f"#r{j}"
                attr_names[alias] = field_name
                remove_aliases.append(alias)
            update_expr += f" REMOVE {', '.join(remove_aliases)}"

        # Merge condition placeholders. Names/values for the condition expression
        # are caller-supplied to avoid colliding with the auto-generated SET aliases.
        if condition_names:
            for name, value in condition_names.items():
                if name in attr_names and attr_names[name] != value:
                    raise ValueError(f"condition_names alias '{name}' collides with SET alias")
                attr_names[name] = value
        if condition_values:
            for name, value in condition_values.items():
                if name in attr_values and attr_values[name] != value:
                    raise ValueError(f"condition_values placeholder '{name}' collides with SET placeholder")
                attr_values[name] = value

        kwargs: dict[str, Any] = {
            "Key": key,
            "UpdateExpression": update_expr,
            "ExpressionAttributeValues": attr_values,
            "ExpressionAttributeNames": attr_names,
        }
        if condition:
            kwargs["ConditionExpression"] = condition

        try:
            self._table.update_item(**kwargs)
            return True
        except ClientError:
            logger.warning(
                "DDB update failed",
                extra={
                    "table": self._table_name,
                    "key_attrs": list(key.keys()),
                    "attributes": list(update_fields.keys()),
                },
                exc_info=True,
            )
            if raise_on_error:
                raise
            return False

    def delete(self, key: dict[str, Any]) -> None:
        """Delete a single item by primary key.

        Args:
            key: Primary key attributes identifying the item to delete.
        """
        self._table.delete_item(Key=key)

    def atomic_increment(
        self,
        key: dict[str, Any],
        attribute: str,
        amount: int = 1,
    ) -> int:
        """Atomically increment a numeric attribute and return its new value.

        Args:
            key: Primary key attributes identifying the item.
            attribute: Name of the numeric attribute to increment.
            amount: Delta to add (default 1; may be negative).

        Returns:
            The attribute's value after the increment.
        """
        resp = self._table.update_item(
            Key=key,
            UpdateExpression="ADD #attr :inc",
            ExpressionAttributeNames={"#attr": attribute},
            ExpressionAttributeValues={":inc": amount},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"][attribute])

    def atomic_add(
        self,
        key: dict[str, Any],
        increments: dict[str, Any],
    ) -> None:
        """Atomically ADD one or more numeric attributes in a single UpdateItem.

        DynamoDB ``ADD`` is server-serialized and commutative, so concurrent
        callers accumulating shared counters merge without lost updates or
        transaction conflicts. ``increments`` maps attribute name → delta (int
        or Decimal; not float — DynamoDB rejects native floats). No-op if
        ``increments`` is empty.
        """
        if not increments:
            return
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        parts: list[str] = []
        for i, (attr, delta) in enumerate(increments.items()):
            alias = f"#n{i}"
            placeholder = f":v{i}"
            names[alias] = attr
            values[placeholder] = delta
            parts.append(f"{alias} {placeholder}")
        self._table.update_item(
            Key=key,
            UpdateExpression="ADD " + ", ".join(parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, params: QueryParams) -> PaginatedResult:
        """Run a key-conditioned query against the table or a secondary index.

        Args:
            params: Query parameters (key condition, expression bindings, index,
                sort direction, limit, and pagination cursor).

        Returns:
            A page of matching items plus the ``LastEvaluatedKey`` cursor, if any.
        """
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": params.key_condition,
            "ExpressionAttributeValues": params.expression_values,
            "ScanIndexForward": params.scan_forward,
        }
        if params.expression_names:
            kwargs["ExpressionAttributeNames"] = params.expression_names
        if params.index_name:
            kwargs["IndexName"] = params.index_name
        if params.limit:
            kwargs["Limit"] = params.limit
        if params.exclusive_start_key:
            kwargs["ExclusiveStartKey"] = params.exclusive_start_key

        resp = self._table.query(**kwargs)
        return PaginatedResult(
            items=resp.get("Items", []),
            last_evaluated_key=resp.get("LastEvaluatedKey"),
        )

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------

    def batch_get(self, keys: list[dict[str, Any]], *, _max_retries: int = 3) -> list[dict[str, Any]]:
        """Fetch multiple items by key, chunking and retrying unprocessed keys.

        Keys are chunked into batches of 100 (the DynamoDB batch_get_item limit);
        unprocessed keys are retried with exponential backoff.

        Args:
            keys: Primary keys of the items to fetch.
            _max_retries: Maximum retries for unprocessed keys per chunk.

        Returns:
            The retrieved items (order not guaranteed).
        """
        results: list[dict[str, Any]] = []
        for i in range(0, len(keys), _BATCH_GET_LIMIT):
            chunk = keys[i : i + _BATCH_GET_LIMIT]
            request_items: dict[str, Any] = {self._table_name: {"Keys": chunk}}
            for attempt in range(_max_retries + 1):
                resp = self._resource.batch_get_item(RequestItems=request_items)
                results.extend(resp.get("Responses", {}).get(self._table_name, []))
                unprocessed = resp.get("UnprocessedKeys", {}).get(self._table_name)
                if not unprocessed:
                    break
                logger.warning(
                    "batch_get unprocessed keys",
                    extra={"table": self._table_name, "count": len(unprocessed["Keys"]), "attempt": attempt + 1},
                )
                if attempt < _max_retries:
                    time.sleep(2**attempt * 0.1)
                    request_items = {self._table_name: unprocessed}
        return results

    def batch_delete(self, keys: list[dict[str, Any]], *, _max_retries: int = 3) -> None:
        """Delete multiple items using batch_write_item with exponential backoff retry.

        Items are chunked into batches of 25 (DynamoDB batch_write_item limit).
        Unprocessed items are retried up to ``_max_retries`` times with exponential
        backoff (0.1s, 0.2s, 0.4s). No-ops gracefully when *keys* is empty.
        """
        for i in range(0, len(keys), _BATCH_WRITE_LIMIT):
            chunk = keys[i : i + _BATCH_WRITE_LIMIT]
            request_items: dict[str, Any] = {self._table_name: [{"DeleteRequest": {"Key": key}} for key in chunk]}
            for attempt in range(_max_retries + 1):
                resp = self._resource.meta.client.batch_write_item(RequestItems=request_items)
                unprocessed = resp.get("UnprocessedItems", {}).get(self._table_name)
                if not unprocessed:
                    break
                logger.warning(
                    "batch_delete unprocessed items",
                    extra={"table": self._table_name, "count": len(unprocessed), "attempt": attempt + 1},
                )
                if attempt < _max_retries:
                    time.sleep(2**attempt * 0.1)
                    request_items = {self._table_name: unprocessed}
            else:
                if unprocessed:
                    raise RuntimeError(
                        f"batch_delete failed: {len(unprocessed)} unprocessed items in {self._table_name} "
                        f"after {_max_retries} retries"
                    )

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def scan(
        self,
        *,
        filter_expression: str | None = None,
        expression_values: dict[str, Any] | None = None,
        limit: int | None = None,
        exclusive_start_key: dict[str, Any] | None = None,
    ) -> PaginatedResult:
        """Scan the table, optionally filtered and paginated.

        Args:
            filter_expression: Optional filter expression applied server-side.
            expression_values: Placeholder → value bindings for the filter.
            limit: Maximum number of items to evaluate for this page.
            exclusive_start_key: Pagination cursor from a prior scan.

        Returns:
            A page of items plus the ``LastEvaluatedKey`` cursor, if any.
        """
        kwargs: dict[str, Any] = {}
        if filter_expression:
            kwargs["FilterExpression"] = filter_expression
        if expression_values:
            kwargs["ExpressionAttributeValues"] = expression_values
        if limit:
            kwargs["Limit"] = limit
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        resp = self._table.scan(**kwargs)
        return PaginatedResult(
            items=resp.get("Items", []),
            last_evaluated_key=resp.get("LastEvaluatedKey"),
        )

    # ------------------------------------------------------------------
    # Transaction
    # ------------------------------------------------------------------

    def transact_write(self, items: list[dict[str, Any]]) -> None:
        """Atomically write items using the low-level DynamoDB client.

        *items* use the **high-level** ``TransactItems`` format (plain
        Python values — strings, numbers, etc.).  The boto3 resource
        client handles type serialization automatically.
        """
        client = self._resource.meta.client
        client.transact_write_items(TransactItems=items)
