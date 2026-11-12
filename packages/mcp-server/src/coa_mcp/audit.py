# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Audit logging for MCP tool invocations.

Logs agent identity for audit trail. The agent identity is logged for audit
but does not drive authorization decisions.
"""

from __future__ import annotations

import structlog

from .auth.claims import CallerIdentity

logger = structlog.get_logger(__name__)


def log_tool_invocation(
    caller: CallerIdentity,
    tool_name: str,
    namespace_id: str,
    *,
    success: bool = True,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Log an MCP tool invocation for audit purposes.

    Agent identity is logged but NOT used for authorization decisions.
    Authorization is based on the delegating USER identity (caller.user_id).
    """
    log_fn = logger.info if success else logger.warning
    log_fn(
        "mcp_tool_invocation",
        tool=tool_name,
        namespace=namespace_id,
        user_id=caller.user_id,
        agent_id=caller.agent_id,
        success=success,
        error=error,
        duration_ms=duration_ms,
    )
