# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared contract for the schema-visibility authorizer flags.

A caller's right to see
physical source schema (``viewSource``) and ontology schema (``viewOntology``)
is decided by Cedar and forwarded to downstream handlers as string flags in the
API Gateway authorizer context. The control-plane authorizer and the MCP server
produce them; the grant handlers and the metric service consume them.

This module is the single definition of the flag names, the Cedar action names
behind them and the parsing rule, so a producer and a consumer cannot drift
apart. Which response fields each service withholds stays in that service,
since it depends on the service's own response shape.

Every consumer states its missing-flag rule explicitly through ``missing``:

* ``"blind"`` -- an absent flag withholds (fail closed). The default rule.
* ``"full"`` -- if NEITHER flag is present the caller keeps full visibility;
  any flag that is present must still be ``"true"``. This is the deliberate
  metric-read rule for the deploy window in which the metric service is live
  before the authorizer that sets the flags.

Only the string ``"true"`` (case-insensitive) grants; every other value
withholds.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

CAN_VIEW_SOURCE = "canViewSource"
CAN_VIEW_ONTOLOGY = "canViewOntology"
VIEW_SOURCE_NAMESPACES = "viewSourceNamespaces"

VIEW_SOURCE_ACTION = "viewSource"
VIEW_ONTOLOGY_ACTION = "viewOntology"

# Wildcard entry in ``viewSourceNamespaces``: the caller holds ``viewSource``
# platform-wide (a global role), so every namespace is visible.
PLATFORM_NAMESPACE = "*"

MissingRule = Literal["full", "blind"]


@dataclass(frozen=True)
class SchemaVisibility:
    """Whether the caller may see source schema and ontology schema."""

    source: bool
    ontology: bool

    @property
    def full(self) -> bool:
        """True when the caller may see both source and ontology schema."""
        return self.source and self.ontology


FULL_VISIBILITY = SchemaVisibility(source=True, ontology=True)
NO_VISIBILITY = SchemaVisibility(source=False, ontology=False)


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _is_true(value: Any) -> bool:
    return isinstance(value, str | bool) and str(value).lower() == "true"


def to_authorizer_flags(vis: SchemaVisibility) -> dict[str, str]:
    """Serialize ``vis`` as authorizer-context flags (string values, as API Gateway requires)."""
    return {CAN_VIEW_SOURCE: _flag(vis.source), CAN_VIEW_ONTOLOGY: _flag(vis.ontology)}


def from_authorizer_context(ctx: Mapping[str, Any] | None, *, missing: MissingRule) -> SchemaVisibility:
    """Parse the flags from an authorizer context under the given missing-flag rule."""
    ctx = ctx or {}
    if missing == "full" and CAN_VIEW_SOURCE not in ctx and CAN_VIEW_ONTOLOGY not in ctx:
        return FULL_VISIBILITY
    return SchemaVisibility(
        source=_is_true(ctx.get(CAN_VIEW_SOURCE)),
        ontology=_is_true(ctx.get(CAN_VIEW_ONTOLOGY)),
    )


def to_namespace_list(namespace_ids: Iterable[str]) -> str:
    """Serialize the namespaces where the caller holds ``viewSource`` (comma-separated)."""
    return ",".join(ns for ns in namespace_ids if ns)


def from_namespace_list(ctx: Mapping[str, Any] | None) -> frozenset[str]:
    """Namespaces named by ``viewSourceNamespaces``; empty when the key is absent (fail closed)."""
    raw = (ctx or {}).get(VIEW_SOURCE_NAMESPACES)
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(ns.strip() for ns in raw.split(",") if ns.strip())
