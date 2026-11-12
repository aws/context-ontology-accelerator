# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Helpers for reshaping Smithy wire objects into what internal consumers expect.

The Smithy service contracts define request bodies that don't always line up
one-to-one with the internal representations the Context Manager consumes. These
helpers live at the surface-to-CM boundary — data-layer's ``handler.py`` and
MCP's ``execution.py`` both call them before forwarding — so a single source of
truth converts each Smithy shape to what CM actually reads.
"""

from __future__ import annotations

from typing import Any


def normalize_dimensions(dimensions: Any) -> dict[str, Any]:
    """Coerce a Smithy ``DimensionFilterList`` into the mapping CM's Tier-1 consumes.

    The Smithy wire form is a list of ``DimensionFilter`` objects; Tier-1's
    ``substitute_dimensions`` expects a ``{name: value}`` mapping. A list of
    ``{"name": ..., "value": ...}`` dicts becomes that mapping (entries without
    a ``name`` are dropped); a ``dict`` passes through unchanged; anything else
    returns ``{}``.
    """
    if isinstance(dimensions, dict):
        return dimensions
    if not isinstance(dimensions, list):
        return {}
    return {entry["name"]: entry.get("value") for entry in dimensions if isinstance(entry, dict) and "name" in entry}
