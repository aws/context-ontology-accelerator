# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Abstract base class for data access objects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaginatedResult:
    """Wrapper for paginated query results."""

    items: list[dict[str, Any]]
    last_evaluated_key: dict[str, Any] | None = None


@dataclass
class QueryParams:
    """Parameters for a query operation."""

    key_condition: str
    expression_values: dict[str, Any] = field(default_factory=dict)
    expression_names: dict[str, str] | None = None
    index_name: str | None = None
    limit: int | None = None
    exclusive_start_key: dict[str, Any] | None = None
    scan_forward: bool = True


class DatastoreDAO(ABC):
    """Abstract DAO for CRUD + query operations on a single table."""

    @abstractmethod
    def get(
        self,
        key: dict[str, Any],
        *,
        projection: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a single item by primary key. Returns ``None`` if not found."""

    @abstractmethod
    def get_attribute(self, key: dict[str, Any], attribute: str) -> Any | None:
        """Return a single attribute value, or ``None`` if missing."""

    @abstractmethod
    def put(self, item: dict[str, Any], *, condition: str | None = None, auto_timestamp: bool = True) -> None:
        """Create or overwrite an item. Optionally enforce a condition expression."""

    @abstractmethod
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
        """Partially update an item's attributes. Returns True on success.

        ``condition_values`` and ``condition_names`` are caller-supplied
        placeholder maps for the ``ConditionExpression``. They are merged with
        the auto-generated SET placeholders. Use distinct prefixes (e.g.
        ``#status``, ``:pending``) to avoid collisions with the ``#aN`` /
        ``:vN`` SET aliases.
        """

    @abstractmethod
    def delete(self, key: dict[str, Any]) -> None:
        """Delete an item by primary key."""

    @abstractmethod
    def query(self, params: QueryParams) -> PaginatedResult:
        """Query items using a key condition expression."""

    def query_all(self, params: QueryParams, *, max_pages: int = 100) -> list[dict[str, Any]]:
        """Query every matching item, following the pagination cursor to exhaustion.

        :meth:`query` returns ONE page. DynamoDB caps a response at 1 MB
        regardless of how many items match, so a caller that reads
        ``result.items`` and stops silently truncates its result set with no
        error — the failure mode is invisible at the call site. That is merely a
        short list for a listing endpoint, but for authorization data it means
        roles or data restrictions disappear, so every authorization-critical
        read must use this method rather than ``query``.

        ``params.limit`` is left untouched (it bounds each page, not the total).
        Any ``exclusive_start_key`` on ``params`` is honoured as the starting
        cursor.

        Args:
            params: Query parameters; the cursor is advanced internally.
            max_pages: Safety bound on pages read. Reaching it raises rather than
                returning a partial set — a silent truncation here is exactly the
                bug this method exists to prevent, and a partition needing more
                than this indicates a data-model problem worth surfacing.

        Returns:
            Every matching item across all pages.

        Raises:
            RuntimeError: If ``max_pages`` is exhausted while a cursor remains.
        """
        from dataclasses import replace

        items: list[dict[str, Any]] = []
        page_params = params
        for _ in range(max_pages):
            page = self.query(page_params)
            items.extend(page.items)
            if not page.last_evaluated_key:
                return items
            page_params = replace(page_params, exclusive_start_key=page.last_evaluated_key)
        raise RuntimeError(
            f"query_all exceeded {max_pages} pages without exhausting the cursor "
            f"(index={params.index_name!r}); refusing to return a partial result set"
        )

    @abstractmethod
    def atomic_increment(
        self,
        key: dict[str, Any],
        attribute: str,
        amount: int = 1,
    ) -> int:
        """Atomically increment a numeric attribute.

        Returns the new value after the increment.
        """

    @abstractmethod
    def batch_get(self, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Retrieve multiple items by their primary keys."""

    @abstractmethod
    def batch_delete(self, keys: list[dict[str, Any]]) -> None:
        """Delete multiple items by their primary keys.

        Implementations should handle chunking and retry logic for
        unprocessed items. No-ops when *keys* is empty.
        """

    @abstractmethod
    def scan(
        self,
        *,
        filter_expression: str | None = None,
        expression_values: dict[str, Any] | None = None,
        limit: int | None = None,
        exclusive_start_key: dict[str, Any] | None = None,
    ) -> PaginatedResult:
        """Scan the table with an optional filter expression."""

    @abstractmethod
    def transact_write(self, items: list[dict[str, Any]]) -> None:
        """Atomically write multiple items across tables.

        Each element in *items* is a transact item dict with a
        single key (``Put``, ``Update``, ``Delete``, or ``ConditionCheck``)
        using the **high-level** (non-typed) attribute format.

        Raises ``ClientError`` on failure.
        """
