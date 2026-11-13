# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for SessionManager (session.py)."""

from unittest.mock import MagicMock, patch

import pytest
from coa_serve.session import SessionManager


@pytest.fixture
def mock_memory_session():
    """Create a mock MemorySession."""
    session = MagicMock()
    session.get_last_k_turns.return_value = []
    return session


@pytest.fixture
def mock_manager(mock_memory_session):
    """Patch MemorySessionManager and return a SessionManager instance."""
    with patch("coa_serve.session.MemorySessionManager") as MockMSM:
        mock_msm = MockMSM.return_value
        mock_msm.create_memory_session.return_value = mock_memory_session
        sm = SessionManager(memory_id="test-memory-id", region="us-east-1")
        yield sm, mock_msm, mock_memory_session


@pytest.mark.asyncio
@pytest.mark.unit
class TestCreateOrResume:
    async def test_creates_new_session_with_default_id(self, mock_manager):
        sm, mock_msm, _ = mock_manager
        sid, history = await sm.create_or_resume("user-1", "ns-demo")

        assert sid is not None
        assert history == []
        mock_msm.create_memory_session.assert_called_once_with(actor_id="user-1/ns-demo", session_id=sid)

    async def test_resumes_existing_session_with_explicit_id(self, mock_manager):
        sm, mock_msm, _ = mock_manager
        sid, _ = await sm.create_or_resume("user-1", "ns-demo", session_id="my-session")

        assert sid == "my-session"
        mock_msm.create_memory_session.assert_called_once_with(actor_id="user-1/ns-demo", session_id="my-session")

    async def test_history_is_namespace_scoped(self, mock_manager):
        """Same user + same session id in different namespaces must read from
        distinct AgentCore Memory actors so history cannot leak across namespaces."""
        sm, mock_msm, _ = mock_manager
        await sm.create_or_resume("user-1", "ns-a", session_id="shared")
        await sm.create_or_resume("user-1", "ns-b", session_id="shared")

        actors = {c.kwargs["actor_id"] for c in mock_msm.create_memory_session.call_args_list}
        assert actors == {"user-1/ns-a", "user-1/ns-b"}

    async def test_returns_conversation_history(self, mock_manager):
        sm, _, mock_session = mock_manager

        mock_session.get_last_k_turns.return_value = [
            [
                {"content": {"text": "What is revenue?"}, "role": "USER"},
                {"content": {"text": "Revenue is $1M"}, "role": "ASSISTANT"},
            ]
        ]

        _, history = await sm.create_or_resume("user-1", "ns-demo")
        assert history == [
            {"role": "user", "content": "What is revenue?"},
            {"role": "assistant", "content": "Revenue is $1M"},
        ]

    async def test_respects_max_turns(self, mock_manager):
        sm, _, mock_session = mock_manager
        await sm.create_or_resume("user-1", "ns-demo", max_turns=3)
        mock_session.get_last_k_turns.assert_called_once_with(3)


@pytest.mark.asyncio
@pytest.mark.unit
class TestAppendTurn:
    async def test_stores_user_and_assistant_messages(self, mock_manager):
        sm, _, mock_session = mock_manager
        await sm.append_turn("user-1", "sess-1", "What is X?", "X is Y.")

        mock_session.add_turns.assert_called_once()
        messages = mock_session.add_turns.call_args[0][0]
        assert len(messages) == 2
        assert messages[0].text == "What is X?"
        assert messages[1].text == "X is Y."
