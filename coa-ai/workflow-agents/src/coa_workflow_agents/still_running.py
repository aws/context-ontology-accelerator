# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Turn a tool's ``RunDeadlineReachedError`` into a ``still_running`` result.

When a poll ends because the RUN is about to reach its deadline
(``tools/polling.py``), the COA operation is still in progress. Rather than
change every tool module, :class:`StillRunningHook` is registered on each
sub-agent (``agents/sub_agents.py``) and rewrites that one exception into a
result the model can report: which operation (with its id) is still running,
and that the caller should check back with a read-only follow-up.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent

from coa_workflow_agents.tools.polling import RunDeadlineReachedError

logger = structlog.get_logger(__name__)

STILL_RUNNING_MESSAGE = (
    "The run reached its deadline; COA is still processing. Send a read-only follow-up to check status."
)


def still_running_result(description: str) -> dict[str, str]:
    """The tool result that replaces ``RunDeadlineReachedError`` for the poll ``description``.

    ``description`` is the poll's human-readable label, with the resource id
    (it goes to the caller, never to the logs), not polling.py's fixed
    ``operation`` kind. The result key stays ``operation``, which clients read.
    """
    return {"status": "still_running", "operation": description, "message": STILL_RUNNING_MESSAGE}


class StillRunningHook(HookProvider):
    """Replaces a tool's ``RunDeadlineReachedError`` with a ``still_running`` result.

    Strands catches the exception a tool raises, builds an error result and
    fires ``AfterToolCallEvent`` with the exception attached. ``result`` is
    writable on that event, so this swaps in a success result. Any other
    exception keeps its error result.
    """

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register the after-tool-call callback."""
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        """Rewrite the result when the tool raised ``RunDeadlineReachedError``."""
        exc = event.exception
        if not isinstance(exc, RunDeadlineReachedError):
            return
        logger.warning("tool_still_running", tool_name=event.tool_use.get("name", ""))
        event.result = {
            "toolUseId": str(event.tool_use.get("toolUseId", "")),
            "status": "success",
            "content": [{"text": json.dumps(still_running_result(exc.description))}],
        }
