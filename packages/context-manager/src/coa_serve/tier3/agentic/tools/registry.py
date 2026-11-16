# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Internal catalog of Tools for the Agentic Retriever.

The :class:`ToolRegistry` is the single place the reasoning agent discovers the
Tools available for a request. It enforces the uniform Tool interface
on registration — rejecting any Tool missing one of the four required fields
and rejecting a duplicate name while keeping the already-registered
Tool unchanged — and exposes the registered Tools' specs to the
planner prompt via :meth:`specs`.

The registry and its Tools are internal to the Serve_Layer: nothing here is
reachable from a transport or route, so a caller outside the Serve_Layer can
neither enumerate nor invoke Tools.

Import discipline: this module imports only the dependency-
free :mod:`.base` contract plus ``structlog``; it never imports
``graphrag_toolkit``.
"""

from __future__ import annotations

import structlog

from .base import Tool

logger = structlog.get_logger(__name__)

# The four interface fields every Tool MUST present.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "input_schema",
    "output_schema",
)


def _missing_fields(tool: object) -> list[str]:
    """Return the required interface fields a Tool fails to satisfy (empty schema dicts are allowed)."""
    missing: list[str] = []

    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name.strip():
        missing.append("name")

    description = getattr(tool, "description", None)
    if not isinstance(description, str) or not description.strip():
        missing.append("description")

    input_schema = getattr(tool, "input_schema", None)
    if not isinstance(input_schema, dict):
        missing.append("input_schema")

    output_schema = getattr(tool, "output_schema", None)
    if not isinstance(output_schema, dict):
        missing.append("output_schema")

    return missing


class ToolRegistry:
    """Internal, Serve_Layer-only catalog of Tools.

    Tools are stored keyed by their unique ``name`` in registration order, which
    :meth:`names` and :meth:`specs` preserve so the planner prompt sees a stable
    ordering.
    """

    def __init__(self) -> None:
        """Create an empty registry keyed by Tool name in registration order."""
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register ``tool`` after enforcing the uniform interface.

        Rejects, by raising :class:`ValueError`, a Tool that is missing any of
        the four required interface fields or whose ``name`` duplicates
        an already-registered Tool. On a duplicate, the existing
        Tool is retained unchanged and is excluded from this registration.
        """
        missing = _missing_fields(tool)
        if missing:
            logger.warning(
                "tool_registration_rejected_missing_fields",
                missing_fields=missing,
            )
            raise ValueError(
                "Tool rejected: missing or invalid required interface "
                f"field(s): {', '.join(missing)}. A Tool must declare a "
                "non-empty name and description and dict input_schema and "
                "output_schema."
            )

        name = tool.name
        if name in self._tools:
            logger.warning(
                "tool_registration_rejected_duplicate_name",
                duplicate_name=name,
            )
            raise ValueError(
                f"Tool rejected: a Tool named {name!r} is already registered; the existing Tool is retained unchanged."
            )

        self._tools[name] = tool
        logger.debug("tool_registered", tool_name=name)

    def get(self, name: str) -> Tool | None:
        """Return the registered Tool named ``name``, or ``None`` if absent."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """Return the registered Tool names in registration order."""
        return list(self._tools)

    def specs(self, *, exclude: frozenset[str] | None = None) -> list[dict]:
        """Return name + description + schemas for each Tool (for the planner).

        Each entry is a plain dict carrying the Tool's ``name``, ``description``,
        ``input_schema``, and ``output_schema`` so the planner prompt can reason
        about which Tool to select and how to populate its inputs.
        Order matches :meth:`names`.

        ``exclude`` withholds the named tools from the returned specs so the
        planner never sees them for this request. This is how per-request tool
        gating works: the registry is process-wide, but a request over a namespace
        with no structured source excludes the structured tools (``nl_to_sql``) so
        they are not offered to the LLM — a tool the planner cannot see cannot be
        selected. Excluding an unregistered name is a harmless no-op.
        """
        exclude = exclude or frozenset()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
            }
            for tool in self._tools.values()
            if tool.name not in exclude
        ]
