# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier-2 tool layer — the reusable query-authoring capabilities Tier 2 owns.

Tier 2 owns the tools; everything else consumes them. Each capability here is a
plain async method over the shared serve clients, so the same code backs the
in-process agent (:mod:`coa_serve.agents.sql_agent`) and any future transport
(an MCP tool, an HTTP route) without a rewrite:

  * ``search_tables`` / ``get_table_schema`` — :class:`TableCatalog`
  * ``generate_sql`` — :class:`SqlAuthoringTool`
  * ``explore_graph`` — :class:`OntologyGraphTool` (opt-in; needs a graph client)

Execution is deliberately NOT a tool here. Running a statement is the shared
execute-with-authz primitive (:mod:`coa_serve.sql_execution`), which sits under
the tiers — so a consumer executes what ``generate_sql`` produced rather than
calling back into Tier 2 with arbitrary SQL.
"""

from __future__ import annotations

from .graph_tools import DEFAULT_NODE_LIMIT, MAX_HOPS, OntologyGraphTool
from .sql_tools import SqlAuthoringTool, UnknownTablesError
from .table_tools import MAX_TOP_K, TableCatalog

__all__ = [
    "DEFAULT_NODE_LIMIT",
    "MAX_HOPS",
    "MAX_TOP_K",
    "OntologyGraphTool",
    "SqlAuthoringTool",
    "TableCatalog",
    "UnknownTablesError",
]
