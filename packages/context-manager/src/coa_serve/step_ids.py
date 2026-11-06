# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Canonical trace step IDs — single source of truth for the Python serve layer.

Keep in sync with packages/web-app/src/components/Chat/step-labels.ts.
"""

from __future__ import annotations

from enum import StrEnum


class StepId(StrEnum):
    """Canonical string identifiers for each trace step across the resolution tiers."""

    # Tier 1 — metric match
    T1_METRIC_MATCH = "t1.metric_match"
    T1_AUTHORIZE = "t1.authorize"
    T1_FIREWALL = "t1.firewall"
    T1_EXECUTE = "t1.execute"

    # Query embedding (shared across tiers)
    QUERY_EMBED = "query.embed"

    # Routing
    ROUTING_SELECT = "routing.select"

    # Tier 2 — NL-to-SQL
    T2_SQL_GENERATE = "t2.sql.generate"
    T2_SQL_AUTHORIZE = "t2.sql.authorize"
    T2_SQL_FIREWALL = "t2.sql.firewall"
    T2_SQL_EXECUTE = "t2.sql.execute"
    T2_SQL_FAILED = "t2.sql.failed"

    # Tier 2 — VKG (Ontop)
    T2_VKG_CONTEXT = "t2.vkg.context"
    T2_VKG_TRANSLATE = "t2.vkg.translate"
    T2_VKG_VALIDATE = "t2.vkg.validate"
    T2_VKG_COMPILE = "t2.vkg.compile"
    T2_VKG_AUTHORIZE = "t2.vkg.authorize"
    T2_VKG_FIREWALL = "t2.vkg.firewall"
    T2_VKG_EXECUTE = "t2.vkg.execute"
    T2_VKG_RETRY = "t2.vkg.retry"
    T2_VKG_FAILED = "t2.vkg.failed"

    # Tier 2 — native SPARQL (RDF_SPARQL sources). Separate from VKG so a graph
    # answer is never mistaken for an Ontop one in a trace.
    T2_SPARQL_CONTEXT = "t2.sparql.context"
    T2_SPARQL_TRANSLATE = "t2.sparql.translate"
    T2_SPARQL_VALIDATE = "t2.sparql.validate"
    T2_SPARQL_FIREWALL = "t2.sparql.firewall"
    T2_SPARQL_EXECUTE = "t2.sparql.execute"
    T2_SPARQL_FAILED = "t2.sparql.failed"

    # Tier 2 — strategy selection (parallel mode)
    T2_STRATEGY_SELECTED = "t2.strategy_selected"

    # Tier 3 — knowledge retrieval
    T3_RETRIEVE = "t3.retrieve"
    T3_VECTOR_SEARCH = "t3.vector_search"
    T3_GRAPH_TRAVERSE = "t3.graph_traverse"
    T3_CATALOG_CONTEXT = "t3.catalog_context"
    T3_SYNTHESIZE = "t3.synthesize"
    T3_GUARDRAIL = "t3.guardrail"
    LEXICAL_BASELINE_RETRIEVE = "lexical_baseline_retrieve"

    # Response
    RESPONSE_ASSEMBLE = "response.assemble"


TOOL_FOR_STEP: dict[str, str] = {
    StepId.T1_AUTHORIZE: "cedar",
    StepId.T1_FIREWALL: "sql-firewall",
    StepId.T1_EXECUTE: "sql-engine",
    StepId.QUERY_EMBED: "bedrock",
    StepId.T2_SQL_GENERATE: "bedrock",
    StepId.T2_SQL_AUTHORIZE: "cedar",
    StepId.T2_SQL_FIREWALL: "sql-firewall",
    StepId.T2_SQL_EXECUTE: "sql-engine",
    StepId.T2_VKG_CONTEXT: "tbox-builder",
    StepId.T2_VKG_TRANSLATE: "bedrock",
    StepId.T2_VKG_VALIDATE: "sparql-validator",
    StepId.T2_VKG_COMPILE: "ontop",
    StepId.T2_VKG_AUTHORIZE: "cedar",
    StepId.T2_VKG_FIREWALL: "sql-firewall",
    StepId.T2_VKG_EXECUTE: "sql-engine",
    StepId.T2_SPARQL_CONTEXT: "tbox-builder",
    StepId.T2_SPARQL_TRANSLATE: "bedrock",
    StepId.T2_SPARQL_VALIDATE: "sparql-validator",
    StepId.T2_SPARQL_FIREWALL: "sparql-firewall",
    StepId.T2_SPARQL_EXECUTE: "sparql-endpoint",
    StepId.T3_VECTOR_SEARCH: "opensearch",
    StepId.T3_GRAPH_TRAVERSE: "neptune",
    StepId.T3_SYNTHESIZE: "bedrock",
    StepId.T3_GUARDRAIL: "bedrock-guardrail",
}
