# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Code-level confirmation gate for mutating COA tool calls.

``MutatingToolGate`` is a Strands ``InterventionHandler``. It denies every call to a
mutating tool unless the caller named that tool in ``run_workflow``'s
``confirm_mutations`` (or passed ``"*"``). Rules:

- Authorization is structured, per-tool data from the caller, never inferred from the
  instruction text, which a model or a prompt injection could shape.
- The gate denies rather than asks: the server runs unattended, so there is nobody to
  answer a confirmation prompt mid-run.
- Interventions are per ``Agent``, and a sub-agent called as a tool runs its own event
  loop, so the gate is registered on every agent that has mutating tools, not only on
  the supervisor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from strands.hooks.events import BeforeToolCallEvent
from strands.interventions.actions import Deny, Proceed
from strands.interventions.handler import InterventionHandler

# The ``confirm_mutations`` entry that authorizes every mutating tool for the run.
CONFIRM_ALL = "*"

logger = structlog.get_logger(__name__)


# Frozen: one gate is shared by every agent of a run; its authorization must not change mid-run.
@dataclass(frozen=True)
class MutatingToolGate(InterventionHandler):
    """Denies a call to any tool in ``mutating_tool_names`` that ``confirmed_tools`` does not name.

    Tools outside ``mutating_tool_names`` always proceed. ``CONFIRM_ALL`` in
    ``confirmed_tools`` authorizes every mutating tool.
    """

    mutating_tool_names: frozenset[str]
    confirmed_tools: frozenset[str]
    name: str = field(default="coa:mutating-tool-gate")

    def _is_confirmed(self, tool_name: str) -> bool:
        return CONFIRM_ALL in self.confirmed_tools or tool_name in self.confirmed_tools

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: object) -> Proceed | Deny:
        """Deny a mutating tool call the caller did not name in ``confirm_mutations``."""
        tool_name = event.tool_use.get("name", "")
        if tool_name not in self.mutating_tool_names:
            return Proceed()
        if self._is_confirmed(tool_name):
            return Proceed()
        # Tool name only: the arguments may carry caller data and are never logged.
        logger.warning("mutating_tool_denied", tool_name=tool_name)
        return Deny(
            reason=(
                f"'{tool_name}' is a mutating COA operation and was not authorized by "
                "this call. To allow it, include its exact name in the run_workflow "
                f"'confirm_mutations' list (e.g. confirm_mutations=['{tool_name}']), or "
                "pass confirm_mutations=['*'] to authorize all mutating operations. "
                "Then retry."
            )
        )


def normalize_confirm_mutations(
    confirm_mutations: list[str] | None, mutating_tool_names: frozenset[str]
) -> frozenset[str]:
    """The gate set for ``confirm_mutations``: empty authorizes nothing, ``"*"`` everything.

    Raises:
        ValueError: an entry is neither ``"*"`` nor a known mutating tool name. A typo is
            refused rather than ignored, so the caller never believes they authorized a
            tool they did not.
    """
    if not confirm_mutations:
        return frozenset()
    requested = {name.strip() for name in confirm_mutations}
    if CONFIRM_ALL in requested:
        return frozenset({CONFIRM_ALL})
    unknown = requested - mutating_tool_names
    if unknown:
        known = ", ".join(sorted(mutating_tool_names)) or "none in this release"
        raise ValueError(
            f"confirm_mutations names unknown mutating tool(s): {sorted(unknown)}. "
            f"Valid mutating tool names are: {known}. Use '*' to authorize all of them."
        )
    return frozenset(requested)
