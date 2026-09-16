# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for session CRUD actions and streaming session persistence in invoke()."""

from __future__ import annotations

import importlib.util
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
from coa_serve.config import ServiceConfig
from coa_serve.models import (
    ConfidenceScore,
    InvokeResponse,
    QueryResult,
    TraceStep,
)

pytestmark = pytest.mark.unit

HAS_AGENTCORE = importlib.util.find_spec("bedrock_agentcore") is not None
agentcore_required = pytest.mark.skipif(not HAS_AGENTCORE, reason="bedrock_agentcore SDK not installed")


async def _invoke_collect(payload, context=None):
    """Helper: collect all yielded items from the async generator entrypoint."""
    from coa_serve.main import invoke

    results = []
    async for item in invoke(payload, context):
        results.append(item)
    return results[0] if len(results) == 1 else results


@pytest.fixture(autouse=True)
def _bypass_namespace_admission():
    """Bypass the F-2 Cedar namespace-admission gate for these invoke() tests.

    These exercise session persistence / streaming with bare-mock orchestrators
    and predate the gate (F-2, CWE-862); patch it to a no-op ALLOW so they stay
    focused on persistence. Session CRUD actions return before the gate anyway;
    this only affects the query/streaming path. The gate itself is covered in
    test_main.TestNamespaceAdmissionGate and test_authz_invariants. Returning
    (None, None) keeps the query path resolving its own profile as before.
    """
    with patch("coa_serve.main._authorize_namespace_access", new_callable=AsyncMock, return_value=(None, None)):
        yield


def _make_token(claims: dict) -> str:
    return pyjwt.encode(claims, "secret", algorithm="HS256")


def _make_context(user_id: str = "user-sub-123", email: str = "u@test.com") -> MagicMock:
    token = _make_token({"sub": user_id, "email": email, "groups": ["team-a"]})
    ctx = MagicMock()
    ctx.request_headers = {"authorization": f"Bearer {token}"}
    return ctx


@pytest.fixture
def config():
    return ServiceConfig(
        guardrail_id="test-guardrail",
        vkg_endpoint="http://vkg.local:8080",
        neptune_endpoint="",
        opensearch_endpoint="",
        bedrock_model_id="test-model",
        bedrock_region="us-east-1",
        data_sources_table="",
        metric_definitions_table="",
        memory_id="test-memory",
        session_metadata_table="test-session-metadata",
    )


# ── getRecentSession ──────────────────────────────────────────────────────


@agentcore_required
@pytest.mark.asyncio
class TestGetRecentSession:
    @pytest.fixture(autouse=True)
    def setup(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        main_mod._orchestrator = MagicMock()
        yield
        main_mod._session_manager = None
        main_mod._session_metadata = None

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_session_with_paginated_messages(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_recent = MagicMock()
        mock_recent.session_id = "sess-abc"
        mock_recent.title = "Revenue query"
        mock_metadata.get_recent_for_namespace = AsyncMock(return_value=mock_recent)
        main_mod._session_metadata = mock_metadata

        mock_session_mgr = MagicMock()
        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        mock_session_mgr.get_rich_history_page = AsyncMock(return_value=(messages, 10, True))
        main_mod._session_manager = mock_session_mgr

        result = await _invoke_collect(
            {"action": "getRecentSession", "namespace": "ns1", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["sessionId"] == "sess-abc"
        assert result["messages"] == messages
        assert result["hasMore"] is True
        assert result["cursor"] == 2
        assert result["statusCode"] == 200

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_empty_when_no_recent_session(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_metadata.get_recent_for_namespace = AsyncMock(return_value=None)
        main_mod._session_metadata = mock_metadata
        main_mod._session_manager = MagicMock()

        result = await _invoke_collect(
            {"action": "getRecentSession", "namespace": "ns1", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["sessionId"] is None
        assert result["messages"] == []
        assert result["statusCode"] == 200

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_400_when_no_user_id(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        main_mod._session_metadata = mock_metadata
        main_mod._session_manager = MagicMock()

        # No context (no JWT) and no profile.userId
        result = await _invoke_collect(
            {"action": "getRecentSession", "namespace": "ns1", "profile": {}},
        )

        assert result["error"] == "ValidationError"
        assert result["statusCode"] == 400


# ── getSessionHistory ─────────────────────────────────────────────────────


@agentcore_required
@pytest.mark.asyncio
class TestGetSessionHistory:
    @pytest.fixture(autouse=True)
    def setup(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        main_mod._orchestrator = MagicMock()
        yield
        main_mod._session_manager = None
        main_mod._session_metadata = None

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_paginated_history(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_metadata.validate_ownership = AsyncMock(return_value=True)
        mock_metadata.get_session_namespace = AsyncMock(return_value="ns-1")
        main_mod._session_metadata = mock_metadata

        messages = [{"role": "user", "content": "q1"}]
        mock_session_mgr = MagicMock()
        mock_session_mgr.get_rich_history_page = AsyncMock(return_value=(messages, 5, True))
        main_mod._session_manager = mock_session_mgr

        result = await _invoke_collect(
            {
                "action": "getSessionHistory",
                "sessionId": "sess-1",
                "cursor": 0,
                "limit": 5,
                "profile": {"userId": "user-1"},
            },
            context=_make_context(),
        )

        assert result["sessionId"] == "sess-1"
        assert result["messages"] == messages
        assert result["hasMore"] is True
        assert result["cursor"] == 1  # 0 + len(messages)
        assert result["statusCode"] == 200

    @patch("coa_serve.main._ensure_initialized")
    async def test_max_turns_equals_cursor_plus_limit(self, mock_init):
        """Verify that max_turns is capped to cursor+limit (no over-fetch)."""
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_metadata.validate_ownership = AsyncMock(return_value=True)
        mock_metadata.get_session_namespace = AsyncMock(return_value="ns-1")
        main_mod._session_metadata = mock_metadata

        mock_session_mgr = MagicMock()
        mock_session_mgr.get_rich_history_page = AsyncMock(return_value=([], 0, False))
        main_mod._session_manager = mock_session_mgr

        await _invoke_collect(
            {
                "action": "getSessionHistory",
                "sessionId": "sess-1",
                "cursor": 10,
                "limit": 5,
                "profile": {"userId": "user-1"},
            },
            context=_make_context(),
        )

        call_kwargs = mock_session_mgr.get_rich_history_page.call_args[1]
        assert call_kwargs["max_turns"] == 15  # cursor(10) + limit(5)

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_403_for_non_owner(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_metadata.validate_ownership = AsyncMock(return_value=False)
        main_mod._session_metadata = mock_metadata
        main_mod._session_manager = MagicMock()

        result = await _invoke_collect(
            {
                "action": "getSessionHistory",
                "sessionId": "sess-other",
                "profile": {"userId": "user-1"},
            },
            context=_make_context(),
        )

        assert result["error"] == "AccessDeniedError"
        assert result["statusCode"] == 403

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_400_when_missing_session_id(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        main_mod._session_manager = MagicMock()
        main_mod._session_metadata = MagicMock()

        result = await _invoke_collect(
            {"action": "getSessionHistory", "sessionId": "", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["error"] == "ValidationError"
        assert result["statusCode"] == 400


# ── createSession ─────────────────────────────────────────────────────────


@agentcore_required
@pytest.mark.asyncio
class TestCreateSession:
    @pytest.fixture(autouse=True)
    def setup(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        main_mod._orchestrator = MagicMock()
        main_mod._session_manager = None
        yield
        main_mod._session_metadata = None

    @patch("coa_serve.main._ensure_initialized")
    async def test_creates_session_and_returns_id(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_record = MagicMock()
        mock_record.session_id = "new-sess-xyz"
        mock_metadata.create = AsyncMock(return_value=mock_record)
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "createSession", "namespace": "ns1", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["action"] == "sessionCreated"
        assert result["sessionId"] == "new-sess-xyz"
        assert result["namespace"] == "ns1"
        assert result["statusCode"] == 200
        # The validated JWT sub (from _make_context) is authoritative over profile.userId.
        mock_metadata.create.assert_called_once_with("user-sub-123", namespace_id="ns1")

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_400_when_namespace_missing(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "createSession", "namespace": "", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["error"] == "ValidationError"
        assert result["statusCode"] == 400


# ── listSessions ──────────────────────────────────────────────────────────


@agentcore_required
@pytest.mark.asyncio
class TestListSessions:
    @pytest.fixture(autouse=True)
    def setup(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        main_mod._orchestrator = MagicMock()
        main_mod._session_manager = None
        yield
        main_mod._session_metadata = None

    @patch("coa_serve.main._ensure_initialized")
    async def test_lists_sessions_for_namespace(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_session = MagicMock()
        mock_session.session_id = "sess-1"
        mock_session.title = "My query"
        mock_session.last_active_at = "2026-01-01T00:00:00Z"
        mock_session.message_count = 4
        mock_session.namespace_id = "ns1"
        mock_metadata.list_for_namespace = AsyncMock(return_value=([mock_session], "next-tok"))
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "listSessions", "namespace": "ns1", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["action"] == "sessionList"
        assert len(result["sessions"]) == 1
        assert result["sessions"][0]["sessionId"] == "sess-1"
        assert result["nextPageToken"] == "next-tok"
        assert result["statusCode"] == 200

    @patch("coa_serve.main._ensure_initialized")
    async def test_lists_all_sessions_when_no_namespace(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_metadata.list_sessions = AsyncMock(return_value=([], None))
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "listSessions", "namespace": "", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["sessions"] == []
        mock_metadata.list_sessions.assert_called_once()

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_400_when_no_user(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "listSessions", "namespace": "ns1", "profile": {}},
        )

        assert result["error"] == "ValidationError"
        assert result["statusCode"] == 400


# ── deleteSession ─────────────────────────────────────────────────────────


@agentcore_required
@pytest.mark.asyncio
class TestDeleteSession:
    @pytest.fixture(autouse=True)
    def setup(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        main_mod._orchestrator = MagicMock()
        main_mod._session_manager = None
        yield
        main_mod._session_metadata = None

    @patch("coa_serve.main._ensure_initialized")
    async def test_deletes_owned_session(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_metadata.validate_ownership = AsyncMock(return_value=True)
        mock_metadata.delete = AsyncMock()
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "deleteSession", "sessionId": "sess-del", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["action"] == "sessionDeleted"
        assert result["sessionId"] == "sess-del"
        assert result["statusCode"] == 200
        # The validated JWT sub (from _make_context) is authoritative over profile.userId.
        mock_metadata.delete.assert_called_once_with("user-sub-123", "sess-del")

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_404_for_non_owner(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        mock_metadata.validate_ownership = AsyncMock(return_value=False)
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "deleteSession", "sessionId": "sess-other", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["error"] == "AccessDeniedError"
        assert result["statusCode"] == 404

    @patch("coa_serve.main._ensure_initialized")
    async def test_returns_400_when_missing_fields(self, mock_init):
        import coa_serve.main as main_mod

        mock_init.return_value = None
        mock_metadata = MagicMock()
        main_mod._session_metadata = mock_metadata

        result = await _invoke_collect(
            {"action": "deleteSession", "sessionId": "", "profile": {"userId": "user-1"}},
            context=_make_context(),
        )

        assert result["error"] == "ValidationError"
        assert result["statusCode"] == 400


# ── Streaming SSE session persistence ─────────────────────────────────────


@agentcore_required
@pytest.mark.asyncio
class TestStreamingSessionPersistence:
    """Test session creation/resume and turn persistence in streaming mode."""

    @pytest.fixture(autouse=True)
    def setup(self, config):
        import coa_serve.main as main_mod

        main_mod._config = config
        yield
        main_mod._session_manager = None
        main_mod._session_metadata = None
        main_mod._orchestrator = None

    @patch("coa_serve.main._ensure_initialized")
    async def test_creates_session_when_no_session_id(self, mock_init):
        """Streaming without sessionId should auto-create via metadata store."""
        import coa_serve.main as main_mod

        mock_init.return_value = None

        # Orchestrator
        async def mock_resolve(request, trace=None, on_token=None, conversation_history=None, deadline=None):
            if trace:
                trace.record(step="t2.sql", status="done", duration_ms=50)
            return InvokeResponse(
                result=QueryResult(
                    tier=2,
                    confidence=ConfidenceScore(score=0.9, rationale="ok"),
                    trace=[TraceStep(step="t2.sql", status="done", durationMs=50)],
                ),
                requestId="req-1",
            )

        mock_orch = MagicMock()
        mock_orch.resolve = mock_resolve
        main_mod._orchestrator = mock_orch

        # Session metadata
        mock_metadata = MagicMock()
        mock_record = MagicMock()
        mock_record.session_id = "auto-sess-1"
        mock_metadata.create = AsyncMock(return_value=mock_record)
        mock_metadata.update_activity = AsyncMock()
        main_mod._session_metadata = mock_metadata

        # Session manager
        mock_session_mgr = MagicMock()
        mock_session_mgr.create_or_resume = AsyncMock(return_value=("auto-sess-1", None))
        mock_session_mgr.append_turn = AsyncMock()
        main_mod._session_manager = mock_session_mgr

        ctx = _make_context(user_id="user-sub-abc")

        results = await _invoke_collect(
            {"query": "show data", "namespace": "ns1", "stream": True, "profile": {}},
            context=ctx,
        )

        assert isinstance(results, list)
        done_events = [e for e in results if isinstance(e, dict) and e.get("type") == "done"]
        assert len(done_events) == 1
        assert done_events[0].get("sessionId") == "auto-sess-1"

        # Session was created and turn was persisted
        mock_metadata.create.assert_called_once()
        mock_session_mgr.append_turn.assert_called_once()
        mock_metadata.update_activity.assert_called_once()

    @patch("coa_serve.main._ensure_initialized")
    async def test_resumes_existing_session(self, mock_init):
        """Streaming with sessionId should validate ownership and resume."""
        import coa_serve.main as main_mod

        mock_init.return_value = None

        async def mock_resolve(request, trace=None, on_token=None, conversation_history=None, deadline=None):
            if trace:
                trace.record(step="t2.sql", status="done", duration_ms=30)
            return InvokeResponse(
                result=QueryResult(
                    tier=2,
                    confidence=ConfidenceScore(score=0.8, rationale="ok"),
                    trace=[TraceStep(step="t2.sql", status="done", durationMs=30)],
                ),
                requestId="req-2",
            )

        mock_orch = MagicMock()
        mock_orch.resolve = mock_resolve
        main_mod._orchestrator = mock_orch

        mock_metadata = MagicMock()
        mock_metadata.validate_ownership = AsyncMock(return_value=True)
        mock_metadata.update_activity = AsyncMock()
        main_mod._session_metadata = mock_metadata

        mock_session_mgr = MagicMock()
        mock_session_mgr.create_or_resume = AsyncMock(
            return_value=("existing-sess", [{"role": "user", "content": "prev"}])
        )
        mock_session_mgr.append_turn = AsyncMock()
        main_mod._session_manager = mock_session_mgr

        ctx = _make_context(user_id="user-sub-xyz")

        results = await _invoke_collect(
            {
                "query": "next question",
                "namespace": "ns1",
                "stream": True,
                "sessionId": "existing-sess",
                "profile": {},
            },
            context=ctx,
        )

        # May be list (with step events) or could include done
        all_events = results if isinstance(results, list) else [results]
        done_events = [e for e in all_events if isinstance(e, dict) and e.get("type") == "done"]
        assert len(done_events) == 1

        # Ownership was validated
        mock_metadata.validate_ownership.assert_called_once_with("user-sub-xyz", "existing-sess")
        # Session resumed with conversation history
        mock_session_mgr.create_or_resume.assert_called_once()
        call_kwargs = mock_session_mgr.create_or_resume.call_args[1]
        assert call_kwargs["session_id"] == "existing-sess"

    @patch("coa_serve.main._ensure_initialized")
    async def test_session_persist_failure_does_not_crash(self, mock_init):
        """Turn persistence failure should be swallowed (best-effort)."""
        import coa_serve.main as main_mod

        mock_init.return_value = None

        async def mock_resolve(request, trace=None, on_token=None, conversation_history=None, deadline=None):
            if trace:
                trace.record(step="t2.sql", status="done", duration_ms=20)
            return InvokeResponse(
                result=QueryResult(
                    tier=2,
                    confidence=ConfidenceScore(score=0.7, rationale="ok"),
                    trace=[TraceStep(step="t2.sql", status="done", durationMs=20)],
                ),
                requestId="req-3",
            )

        mock_orch = MagicMock()
        mock_orch.resolve = mock_resolve
        main_mod._orchestrator = mock_orch

        mock_metadata = MagicMock()
        mock_record = MagicMock()
        mock_record.session_id = "sess-persist-fail"
        mock_metadata.create = AsyncMock(return_value=mock_record)
        mock_metadata.update_activity = AsyncMock(side_effect=RuntimeError("DDB timeout"))
        main_mod._session_metadata = mock_metadata

        mock_session_mgr = MagicMock()
        mock_session_mgr.create_or_resume = AsyncMock(return_value=("sess-persist-fail", None))
        mock_session_mgr.append_turn = AsyncMock(side_effect=RuntimeError("DDB timeout"))
        main_mod._session_manager = mock_session_mgr

        ctx = _make_context(user_id="user-sub-fail")

        results = await _invoke_collect(
            {"query": "test", "namespace": "ns1", "stream": True, "profile": {}},
            context=ctx,
        )

        # Should still get a done event despite persistence failure
        all_events = results if isinstance(results, list) else [results]
        done_events = [e for e in all_events if isinstance(e, dict) and e.get("type") == "done"]
        assert len(done_events) == 1
