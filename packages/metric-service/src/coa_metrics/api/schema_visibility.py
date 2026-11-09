# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Schema visibility for metric reads (ADR: decompose-viewnamespace-schema-visibility).

A metric definition names physical schema: its SQL ``expression`` and
``sourceTable`` disclose table/column names (``viewSource``), and its
``ontologyConcepts`` disclose ontology IRIs (``viewOntology``). A schema-blind
caller (``data-analyst``) may still list and read metrics by name, description
and AI context, but those fields are withheld.

The rights arrive as ``canViewSource`` / ``canViewOntology`` in the authorizer
context (names and parsing shared through
:mod:`coa_authorization.schema_visibility`): the API Gateway authorizer sets both
on every ``readMetric`` request (``"false"`` when it cannot evaluate them), and
the MCP server sets both on its direct invocations. Only an explicit
non-``"true"`` value withholds.

Deliberate exception to fail-closed (``missing="full"``): a request that carries
NEITHER flag came through a path that predates them, i.e. the deploy window
before the new authorizer is live (metric-service deploys first) or dev mode
without a roles table; withholding there would blank definitions for every
caller, admins included. Such a request keeps the pre-flag behaviour. A request
carrying only one flag withholds the other.
"""

from __future__ import annotations

from typing import Any

from coa_authorization.schema_visibility import SchemaVisibility, from_authorizer_context

# Kept as a name for this service's callers; the shape is the shared one.
MetricSchemaVisibility = SchemaVisibility


def visibility_from_event(event: dict[str, Any]) -> SchemaVisibility:
    """Read the caller's schema rights from the authorizer context (missing both = full)."""
    ctx = (event.get("requestContext") or {}).get("authorizer") or {}
    return from_authorizer_context(ctx, missing="full")


def withhold_metric_schema(metric: dict[str, Any], vis: SchemaVisibility) -> dict[str, Any]:
    """Blank the schema-bearing fields of one serialized metric (returns a new dict).

    ``expression`` and ``sourceTable`` are required by the Smithy output shape,
    so they are emptied rather than dropped.
    """
    out = dict(metric)
    if not vis.source:
        if "expression" in out:
            out["expression"] = {"dialects": []}
        if "sourceTable" in out:
            out["sourceTable"] = ""
    if not vis.ontology:
        out.pop("ontologyConcepts", None)
    return out
