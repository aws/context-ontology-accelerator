# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for audit logging of MCP tool invocations.

Agent identity is logged for audit only and must NOT influence authorization.
"""

from __future__ import annotations

import pytest
import structlog
from coa_mcp.audit import log_tool_invocation
from coa_mcp.auth.claims import CallerIdentity


def _caller(user_id: str = "alice", agent_id: str | None = "agent-1") -> CallerIdentity:
    return CallerIdentity(
        user_id=user_id,
        agent_id=agent_id,
        namespaces=["sales"],
        roles=["viewer"],
        raw_claims={},
    )


@pytest.mark.unit
class TestLogToolInvocation:
    def test_success_logs_info_with_fields(self):
        with structlog.testing.capture_logs() as logs:
            log_tool_invocation(_caller(), "query", "sales", duration_ms=42)
        assert len(logs) == 1
        event = logs[0]
        assert event["event"] == "mcp_tool_invocation"
        assert event["log_level"] == "info"
        assert event["tool"] == "query"
        assert event["namespace"] == "sales"
        assert event["user_id"] == "alice"
        assert event["agent_id"] == "agent-1"
        assert event["success"] is True
        assert event["duration_ms"] == 42

    def test_failure_logs_warning_with_error(self):
        with structlog.testing.capture_logs() as logs:
            log_tool_invocation(_caller(), "query", "sales", success=False, error="boom")
        assert len(logs) == 1
        event = logs[0]
        assert event["log_level"] == "warning"
        assert event["success"] is False
        assert event["error"] == "boom"
