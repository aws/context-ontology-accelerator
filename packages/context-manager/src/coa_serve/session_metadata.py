# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session metadata store backed by DynamoDB.

Lightweight index for session discovery (most-recent lookup, listing).
Message content stays in AgentCore Memory; this stores only ordering metadata.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3
import structlog
from coa_common import sync_boto_config
from ulid import ULID

logger = structlog.get_logger(__name__)

_DEFAULT_TTL_DAYS = 30
_GSI_LAST_ACTIVE = "userId-lastActiveAt-index"
_GSI_NAMESPACE = "userId-nsLastActiveAt-index"
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="session-meta")


def _build_ns_composite(namespace_id: str, last_active_at: str) -> str:
    """Build composite sort key: '{namespaceId}#{lastActiveAt}'."""
    return f"{namespace_id}#{last_active_at}"


@dataclass(frozen=True)
class SessionMetadata:
    """Metadata record for a single chat session."""

    user_id: str
    session_id: str
    created_at: str
    last_active_at: str
    status: str
    message_count: int
    ttl: int
    namespace_id: str = ""
    title: str = ""


class SessionMetadataStore:
    """DynamoDB-backed session metadata for discovery and ordering."""

    def __init__(self, table_name: str, region: str | None = None, ttl_days: int = _DEFAULT_TTL_DAYS):
        """Bind the DynamoDB session-metadata table and TTL policy."""
        self._ttl_days = ttl_days
        self._ddb = boto3.resource("dynamodb", region_name=region, config=sync_boto_config())
        self._table = self._ddb.Table(table_name)

    def _compute_ttl(self) -> int:
        return int(time.time()) + (self._ttl_days * 86400)

    async def create(
        self, user_id: str, *, namespace_id: str = "", session_id: str | None = None, title: str = ""
    ) -> SessionMetadata:
        """Create a new session metadata record. Returns the new session."""
        session_id = session_id or str(ULID())
        now = datetime.now(UTC).isoformat()
        ttl = self._compute_ttl()

        item = {
            "userId": user_id,
            "sessionId": session_id,
            "namespaceId": namespace_id,
            "nsLastActiveAt": _build_ns_composite(namespace_id, now),
            "createdAt": now,
            "lastActiveAt": now,
            "status": "active",
            "messageCount": 0,
            "ttl": ttl,
        }
        if title:
            item["title"] = title[:100]

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_EXECUTOR, lambda: self._table.put_item(Item=item))

        logger.info("session_metadata_created", user_id=user_id, session_id=session_id, namespace_id=namespace_id)
        return SessionMetadata(
            user_id=user_id,
            session_id=session_id,
            created_at=now,
            last_active_at=now,
            status="active",
            message_count=0,
            ttl=ttl,
            namespace_id=namespace_id,
            title=title[:100] if title else "",
        )

    async def get_recent(self, user_id: str) -> SessionMetadata | None:
        """Get the most recently active session for a user (any namespace)."""
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._table.query(
                IndexName=_GSI_LAST_ACTIVE,
                KeyConditionExpression="userId = :uid",
                ExpressionAttributeValues={":uid": user_id},
                ScanIndexForward=False,
                Limit=1,
            ),
        )

        items = response.get("Items", [])
        if not items:
            return None

        return _item_to_metadata(items[0])

    async def get_recent_for_namespace(self, user_id: str, namespace_id: str) -> SessionMetadata | None:
        """Get the most recently active session for a user in a specific namespace."""
        ns_prefix = f"{namespace_id}#"
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._table.query(
                IndexName=_GSI_NAMESPACE,
                KeyConditionExpression="userId = :uid AND begins_with(nsLastActiveAt, :nsp)",
                ExpressionAttributeValues={":uid": user_id, ":nsp": ns_prefix},
                ScanIndexForward=False,
                Limit=1,
            ),
        )

        items = response.get("Items", [])
        if not items:
            return None

        return _item_to_metadata(items[0])

    async def list_for_namespace(
        self, user_id: str, namespace_id: str, *, limit: int = 20, page_token: str | None = None
    ) -> tuple[list[SessionMetadata], str | None]:
        """List sessions for a user in a namespace, ordered by lastActiveAt descending."""
        ns_prefix = f"{namespace_id}#"
        kwargs: dict = {
            "IndexName": _GSI_NAMESPACE,
            "KeyConditionExpression": "userId = :uid AND begins_with(nsLastActiveAt, :nsp)",
            "ExpressionAttributeValues": {":uid": user_id, ":nsp": ns_prefix},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if page_token:
            try:
                kwargs["ExclusiveStartKey"] = json.loads(page_token)
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError("Invalid page token format") from e

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(_EXECUTOR, lambda: self._table.query(**kwargs))

        items = response.get("Items", [])
        sessions = [_item_to_metadata(item) for item in items]

        next_token = None
        if "LastEvaluatedKey" in response:
            next_token = json.dumps(response["LastEvaluatedKey"])

        return sessions, next_token

    async def update_activity(self, user_id: str, session_id: str, namespace_id: str = "") -> None:
        """Update lastActiveAt, increment messageCount, refresh composite key and TTL."""
        now = datetime.now(UTC).isoformat()
        ttl = self._compute_ttl()
        ns_composite = _build_ns_composite(namespace_id, now) if namespace_id else ""

        # DynamoDB requires each clause keyword (SET, ADD, ...) to appear once with all
        # of its assignments contiguous, so build the full SET clause before ADD.
        set_assignments = ["lastActiveAt = :ts", "#ttl = :ttl"]
        expr_values: dict = {":ts": now, ":ttl": ttl, ":inc": 1}
        expr_names: dict = {"#ttl": "ttl"}

        if ns_composite:
            set_assignments.append("#nsla = :nsc")
            expr_values[":nsc"] = ns_composite
            expr_names["#nsla"] = "nsLastActiveAt"

        update_expr = f"SET {', '.join(set_assignments)} ADD messageCount :inc"

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._table.update_item(
                Key={"userId": user_id, "sessionId": session_id},
                UpdateExpression=update_expr,
                ExpressionAttributeValues=expr_values,
                ExpressionAttributeNames=expr_names,
            ),
        )

    async def update_title(self, user_id: str, session_id: str, title: str) -> None:
        """Set title only if not already set (idempotent, first-message-wins)."""
        loop = asyncio.get_running_loop()
        with contextlib.suppress(Exception):
            await loop.run_in_executor(
                _EXECUTOR,
                lambda: self._table.update_item(
                    Key={"userId": user_id, "sessionId": session_id},
                    UpdateExpression="SET title = :t",
                    ConditionExpression="attribute_not_exists(title) OR title = :empty",
                    ExpressionAttributeValues={":t": title[:100], ":empty": ""},
                ),
            )

    async def get_session_namespace(self, user_id: str, session_id: str) -> str | None:
        """Return the namespace a session belongs to, or None if unknown.

        Used to namespace-scope history reads (e.g. getSessionHistory) when the
        client does not supply a namespace: history is stored per (user,
        namespace, session), so retrieval must resolve the owning namespace to
        read from the correct AgentCore Memory actor.
        """
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._table.get_item(
                Key={"userId": user_id, "sessionId": session_id},
                ProjectionExpression="namespaceId",
                ConsistentRead=True,
            ),
        )
        item = response.get("Item")
        return item.get("namespaceId") if item else None

    async def validate_ownership(self, user_id: str, session_id: str) -> bool:
        """Check that a session belongs to the given user."""
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._table.get_item(
                Key={"userId": user_id, "sessionId": session_id},
                ProjectionExpression="userId",
                ConsistentRead=True,
            ),
        )
        return "Item" in response

    async def delete(self, user_id: str, session_id: str) -> None:
        """Delete a session metadata record."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _EXECUTOR,
            lambda: self._table.delete_item(Key={"userId": user_id, "sessionId": session_id}),
        )
        logger.info("session_metadata_deleted", user_id=user_id, session_id=session_id)

    async def list_sessions(
        self, user_id: str, *, page_token: str | None = None, limit: int = 50
    ) -> tuple[list[SessionMetadata], str | None]:
        """List sessions for a user (all namespaces), ordered by lastActiveAt descending."""
        kwargs: dict = {
            "IndexName": _GSI_LAST_ACTIVE,
            "KeyConditionExpression": "userId = :uid",
            "ExpressionAttributeValues": {":uid": user_id},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if page_token:
            try:
                kwargs["ExclusiveStartKey"] = json.loads(page_token)
            except (json.JSONDecodeError, TypeError) as e:
                raise ValueError("Invalid page token format") from e

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(_EXECUTOR, lambda: self._table.query(**kwargs))

        items = response.get("Items", [])
        sessions = [_item_to_metadata(item) for item in items]

        next_token = None
        if "LastEvaluatedKey" in response:
            next_token = json.dumps(response["LastEvaluatedKey"])

        return sessions, next_token


def _item_to_metadata(item: dict) -> SessionMetadata:
    return SessionMetadata(
        user_id=item["userId"],
        session_id=item["sessionId"],
        created_at=item.get("createdAt", ""),
        last_active_at=item.get("lastActiveAt", ""),
        status=item.get("status", "active"),
        message_count=int(item.get("messageCount", 0)),
        ttl=int(item.get("ttl", 0)),
        namespace_id=item.get("namespaceId", ""),
        title=item.get("title", ""),
    )
