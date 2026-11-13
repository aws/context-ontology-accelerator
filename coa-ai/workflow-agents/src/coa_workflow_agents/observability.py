# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run telemetry log events: one per tool call, and the run's token usage.

``ToolCallObserver`` pairs each ``BeforeToolCallEvent`` with its ``AfterToolCallEvent`` by
``toolUseId`` and logs ``sub_agent_call_completed`` for a sub-agent, ``tool_call_completed``
for any other tool: names, an outcome and a duration, never input or result. A tool name
the agent does not have (one the model invented) logs as ``"unknown"``, since it may quote
caller data. ``log_token_usage`` logs ``run_token_usage``.

An agent that also rewrites deadline-cut results to ``still_running`` must register this
observer first: Strands runs ``AfterToolCallEvent`` callbacks in reverse order, so it then
sees the rewritten result.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import structlog
from strands import Agent
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent, BeforeToolCallEvent

from coa_workflow_agents.tools.polling import RunDeadlineReachedError

logger = structlog.get_logger(__name__)

# Logged in place of a tool name the agent does not have.
UNKNOWN_TOOL = "unknown"

# Strands prefixes the cancel message of an intervention's Deny with this.
# MutatingToolGate is the only intervention registered, so a Deny is its denial.
_DENIED_PREFIX = "DENIED:"


def _outcome(event: AfterToolCallEvent) -> str:
    """``denied``, ``still_running``, ``error`` or ``success`` for one finished tool call."""
    if event.cancel_message is not None and event.cancel_message.startswith(_DENIED_PREFIX):
        return "denied"
    status = event.result.get("status")
    # StillRunningHook leaves the exception on the event and swaps in a success result.
    if isinstance(event.exception, RunDeadlineReachedError) and status == "success":
        return "still_running"
    if event.exception is not None or status == "error":
        return "error"
    return "success"


def registered_tool_name(event: AfterToolCallEvent | BeforeToolCallEvent) -> str:
    """The called tool's name when it is one of the agent's own tools, else ``UNKNOWN_TOOL``."""
    # The agent's registry, not ``event.selected_tool``: Strands passes no
    # selected tool on a call an intervention cancelled, and that tool is known.
    name = event.tool_use.get("name", "")
    registry = getattr(getattr(getattr(event, "agent", None), "tool_registry", None), "registry", None)
    if not isinstance(registry, Mapping) or name not in registry:
        return UNKNOWN_TOOL
    return str(name)


class ToolCallObserver(HookProvider):
    """Logs the outcome and duration of every tool call one agent makes."""

    def __init__(self, agent_name: str, sub_agent_names: frozenset[str] = frozenset()) -> None:
        """Observe the agent named ``agent_name``; tools in ``sub_agent_names`` are sub-agents."""
        self._agent_name = agent_name
        self._sub_agent_names = sub_agent_names
        self._started: dict[str, float] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register the before- and after-tool-call callbacks."""
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """Start the clock for this call."""
        self._started[str(event.tool_use.get("toolUseId", ""))] = time.monotonic()

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Log the call's outcome and duration (names only, no input or result)."""
        now = time.monotonic()
        started = self._started.pop(str(event.tool_use.get("toolUseId", "")), now)
        duration_s = round(now - started, 3)
        tool = registered_tool_name(event)
        outcome = _outcome(event)
        if tool in self._sub_agent_names:
            # A sub-agent call ends in success or error; it is never gated or rewritten itself.
            sub_outcome = "success" if outcome == "success" else "error"
            logger.info("sub_agent_call_completed", sub_agent=tool, outcome=sub_outcome, duration_s=duration_s)
            return
        logger.info("tool_call_completed", agent=self._agent_name, tool=tool, outcome=outcome, duration_s=duration_s)


# Strands usage keys -> run_token_usage fields.
_USAGE_FIELDS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("totalTokens", "total_tokens"),
    ("cacheReadInputTokens", "cache_read_input_tokens"),
    ("cacheWriteInputTokens", "cache_write_input_tokens"),
)


def log_token_usage(agents: Sequence[Agent]) -> None:
    """Log ``run_token_usage``: the token counts of ``agents`` summed (a missing count is 0)."""
    totals = dict.fromkeys((name for _, name in _USAGE_FIELDS), 0)
    for agent in agents:
        usage: Mapping[str, object] = agent.event_loop_metrics.accumulated_usage
        for key, name in _USAGE_FIELDS:
            value = usage.get(key, 0)
            totals[name] += value if isinstance(value, int) else 0
    logger.info("run_token_usage", **totals, agents=len(agents))
