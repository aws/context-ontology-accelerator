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

    The Smithy wire form is a list of ``DimensionFilter`` objects. The
    Tier-1 :func:`substitute_dimensions` in ``metric_resolver.py`` expects a
    mapping and calls ``.items()`` on it — passing the list verbatim raises
    ``AttributeError`` (not ``ValueError``, so the orchestrator's fail-open
    catch does not fire) and every dimensioned Tier-1 query 500s.

    Behavior:

    - A ``list`` of ``{"name": ..., "value": ...}`` dicts becomes
      ``{name: value}``. Entries without a ``name`` key are dropped silently
      rather than raising, so a malformed entry cannot 500 the whole query.
    - An ``operator`` field (if the Smithy shape grows one) is ignored: the
      current metric resolver supports equality only. When operator support
      lands in ``substitute_dimensions``, this helper should evolve alongside
      it — not the call sites, which stay contract-shaped.
    - A ``dict`` is passed through unchanged so a caller that has already
      normalized (e.g. an internal test) is idempotent.
    - Anything else — ``None``, a scalar, a set — returns ``{}``. The caller
      is expected to short-circuit on an empty result rather than forward
      it into ``options``.

    Args:
        dimensions: A Smithy ``DimensionFilterList``, a pre-normalized dict,
            or an empty/None value.

    Returns:
        The ``{name: value}`` mapping CM's Tier-1 metric resolver consumes.
        Empty when the input has no usable dimensions.
    """
    if isinstance(dimensions, dict):
        return dimensions
    if not isinstance(dimensions, list):
        return {}
    return {entry["name"]: entry.get("value") for entry in dimensions if isinstance(entry, dict) and "name" in entry}
