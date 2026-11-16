# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Session action handlers extracted from the invoke entrypoint.

These handle non-streaming session CRUD operations:
- getRecentSession: Restore the most recent session for a namespace
- getSessionHistory: Paginated message history for a session
- createSession: Create a new empty session
- listSessions: List sessions for a user/namespace
- deleteSession: Remove a session

All actions return a single dict (yielded once by invoke).
"""

from __future__ import annotations

from .session import SessionManager
from .session_metadata import SessionMetadataStore


async def handle_get_recent_session(
    payload: dict,
    request_id: str,
    user_id: str,
    session_metadata: SessionMetadataStore,
    session_manager: SessionManager | None,
) -> dict:
    """Return the most recent session, optionally with initial message page."""
    namespace = payload.get("namespace", "")

    if namespace and session_manager:
        recent = await session_metadata.get_recent_for_namespace(user_id, namespace)
        if not recent:
            return {
                "sessionId": None,
                "namespace": namespace,
                "messages": [],
                "requestId": request_id,
                "statusCode": 200,
            }
        # Inline the first page so restore needs no separate getSessionHistory call.
        messages, total, has_more = await session_manager.get_rich_history_page(
            user_id=user_id,
            session_id=recent.session_id,
            limit=5,
            offset=0,
            namespace_id=namespace,
        )
        return {
            "sessionId": recent.session_id,
            "namespace": namespace,
            "title": recent.title,
            "messages": messages,
            "totalCount": total,
            "hasMore": has_more,
            "cursor": len(messages) if has_more else None,
            "requestId": request_id,
            "statusCode": 200,
        }

    recent = await session_metadata.get_recent(user_id)
    if not recent:
        return {"sessionId": None, "requestId": request_id, "statusCode": 204}

    return {
        "sessionId": recent.session_id,
        "createdAt": recent.created_at,
        "lastActiveAt": recent.last_active_at,
        "messageCount": recent.message_count,
        "status": recent.status,
        "requestId": request_id,
        "statusCode": 200,
    }


async def handle_get_session_history(
    payload: dict,
    request_id: str,
    user_id: str,
    session_manager: SessionManager,
    session_metadata: SessionMetadataStore | None,
) -> dict:
    """Return a paginated page of session messages."""
    session_id = payload.get("sessionId", "")
    namespace = payload.get("namespace", "")
    cursor = max(0, int(payload.get("cursor", 0)))
    limit = min(int(payload.get("limit", 5)), 10)

    if not user_id or not session_id:
        return {
            "error": "ValidationError",
            "message": "userId (in profile) and sessionId required",
            "requestId": request_id,
            "statusCode": 400,
        }

    if session_metadata:
        is_owner = await session_metadata.validate_ownership(user_id, session_id)
        if not is_owner:
            return {
                "error": "AccessDeniedError",
                "message": "Session not found",
                "requestId": request_id,
                "statusCode": 403,
            }
        # History is keyed per (user, namespace); the web client omits namespace on
        # getSessionHistory, so resolve it from metadata unless the payload has one.
        if not namespace:
            namespace = await session_metadata.get_session_namespace(user_id, session_id) or ""

    messages, _total, has_more = await session_manager.get_rich_history_page(
        user_id=user_id,
        session_id=session_id,
        limit=limit,
        offset=cursor,
        max_turns=cursor + limit,
        namespace_id=namespace,
    )
    next_cursor = cursor + len(messages) if has_more else None
    return {
        "sessionId": session_id,
        "messages": messages,
        "hasMore": has_more,
        "cursor": next_cursor,
        "requestId": request_id,
        "statusCode": 200,
    }


async def handle_create_session(
    payload: dict,
    request_id: str,
    user_id: str,
    session_metadata: SessionMetadataStore,
) -> dict:
    """Create a new chat session."""
    namespace = payload.get("namespace", "")
    if not user_id or not namespace:
        return {
            "error": "ValidationError",
            "message": "userId and namespace required",
            "requestId": request_id,
            "statusCode": 400,
        }

    meta_record = await session_metadata.create(user_id, namespace_id=namespace)
    return {
        "action": "sessionCreated",
        "sessionId": meta_record.session_id,
        "namespace": namespace,
        "requestId": request_id,
        "statusCode": 200,
    }


async def handle_list_sessions(
    payload: dict,
    request_id: str,
    user_id: str,
    session_metadata: SessionMetadataStore,
) -> dict:
    """List sessions for a user, optionally filtered by namespace."""
    namespace = payload.get("namespace", "")
    limit = min(int(payload.get("limit", 20)), 50)
    page_token = payload.get("pageToken")

    if not user_id:
        return {"error": "ValidationError", "message": "userId required", "requestId": request_id, "statusCode": 400}

    if namespace:
        sessions_list, next_token = await session_metadata.list_for_namespace(
            user_id, namespace, limit=limit, page_token=page_token
        )
    else:
        sessions_list, next_token = await session_metadata.list_sessions(user_id, limit=limit, page_token=page_token)

    return {
        "action": "sessionList",
        "sessions": [
            {
                "sessionId": s.session_id,
                "title": s.title or "",
                "lastActiveAt": s.last_active_at,
                "messageCount": s.message_count,
                "namespaceId": s.namespace_id,
            }
            for s in sessions_list
        ],
        "nextPageToken": next_token,
        "requestId": request_id,
        "statusCode": 200,
    }


async def handle_delete_session(
    payload: dict,
    request_id: str,
    user_id: str,
    session_metadata: SessionMetadataStore,
) -> dict:
    """Delete a session after ownership validation."""
    session_id = payload.get("sessionId", "")
    if not user_id or not session_id:
        return {
            "error": "ValidationError",
            "message": "userId and sessionId required",
            "requestId": request_id,
            "statusCode": 400,
        }

    is_owner = await session_metadata.validate_ownership(user_id, session_id)
    if not is_owner:
        return {
            "error": "AccessDeniedError",
            "message": "Session not found",
            "requestId": request_id,
            "statusCode": 404,
        }

    await session_metadata.delete(user_id, session_id)
    return {"action": "sessionDeleted", "sessionId": session_id, "requestId": request_id, "statusCode": 200}
