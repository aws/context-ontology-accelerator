# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Agentic NL-to-SQL strategy — the Tier-2 adapter for the NL→SQL agent.

Where :class:`NLtoSQLStrategy` does a fixed retrieve → generate → (correct)
pipeline, this strategy delegates to :class:`SqlAgent`, which iteratively
discovers the right tables (inspecting candidate schemas, following FKs) before
writing SQL, then executes and self-corrects. The strategy holds no prompt, tools
or agent loop: it builds the tools from the request context, runs the agent, and
maps the outcome onto trace records and a :class:`StrategyResult`.

Worst-case latency and token spend are bounded by a per-request cap on statement
executions, a stop just before the request deadline, and the per-statement
execution timeout (see :mod:`coa_serve.agents.sql_agent`). It runs only when the
request pins ``options.strategy = "deep-reasoning"`` (see
``StructuredQueryTier._strategies_for``), never as an implicit fallback.
"""

from __future__ import annotations

import structlog
from coa_common import ontology_vector_index_name
from coa_common.constants import StructuredBackend

from ...agents import SqlAgent
from ...clients.base import GraphClient, QueryExecutor, VectorClient
from ...config import env_with_legacy_name
from ...exceptions import AccessDeniedError
from ...sql_execution import SqlExecutionService
from ...step_ids import StepId
from ..sql_firewall import SQLFirewall, tables_visible_to
from ..strategy import (
    EMPTY_RESULT_CONFIDENCE_FLOOR,
    StrategyContext,
    StrategyOption,
    StrategyResult,
    capped_evidence,
    capped_max_rows,
)
from ..tools import OntologyGraphTool, SqlAuthoringTool, TableCatalog
from .sql_generator import SQLGenerator

logger = structlog.get_logger(__name__)

_DEFAULT_REGION = "us-east-1"

# The truthy set every serve boolean flag accepts (matches config._guardrails_disabled).
_TRUTHY = ("1", "true", "on", "yes")


def _reported_confidence(outcome_confidence: float) -> float:
    """Confidence for a statement the agent verified by executing it.

    The score is the SQL writer's own self-rating, floored at
    :data:`EMPTY_RESULT_CONFIDENCE_FLOOR` because the statement ran, so a verified
    result is not discarded as a low-confidence empty.
    """
    return max(outcome_confidence, EMPTY_RESULT_CONFIDENCE_FLOOR)


def _graph_traversal_enabled(options: dict | None = None) -> bool:
    """Whether this request gets the opt-in ``explore_graph`` tool.

    Three toggles, in precedence order:

    1. ``options.excludeTools = ["explore_graph"]`` withholds it for one request
       — the ablation key the Tier-3 deep-reasoning path already honours, and a veto
       so it wins over the two enables below.
    2. ``options.deepReasoningGraphTraversal`` turns it on for one request, the same
       per-request shape as the FK-evidence, reranker and sample-row flags.
       ``options.agenticGraphTraversal`` is the accepted pre-rename spelling.
    3. ``SERVE_DEEP_REASONING_GRAPH_TRAVERSAL`` sets the deployment default (off);
       ``SERVE_AGENTIC_GRAPH_TRAVERSAL`` is still read.
    """
    if "explore_graph" in ((options or {}).get("excludeTools") or ()):
        return False
    if options and any(
        str(options.get(key, "")).strip().lower() in _TRUTHY
        for key in ("deepReasoningGraphTraversal", "agenticGraphTraversal")
    ):
        return True
    return env_with_legacy_name("SERVE_DEEP_REASONING_GRAPH_TRAVERSAL").strip().lower() in _TRUTHY


class AgenticStrategy:
    """StructuredQueryStrategy: bounded tool-use agent over the serve clients."""

    name: str = StrategyOption.DEEP_REASONING
    # Backends this strategy answers; the runner keeps it only for a namespace that
    # has one of them (see StructuredQueryTier._strategies_for).
    supported_backends: frozenset[StructuredBackend] = frozenset({StructuredBackend.SQL})

    def __init__(
        self,
        sql_generator: SQLGenerator,
        firewall: SQLFirewall,
        query_executor: QueryExecutor | None,
        vector_client: VectorClient,
        oss_ontology_index: str = "",
        prefetch_schemas: int | None = None,
        graph_client: GraphClient | None = None,
    ):
        """Wire the strategy to the shared serve clients (see module docstring).

        ``graph_client`` is optional: it backs the opt-in ``explore_graph`` tool
        (flag ``SERVE_DEEP_REASONING_GRAPH_TRAVERSAL``). With it unset or the flag off, no
        graph tool is built.
        """
        self._sql_generator = sql_generator
        self._firewall = firewall
        self._query_executor = query_executor
        self._vector = vector_client
        self._oss_ontology_index = oss_ontology_index
        self._prefetch_schemas = prefetch_schemas
        self._graph_client = graph_client

    async def resolve(self, query: str, namespace: str, context: StrategyContext) -> StrategyResult | None:
        """Run the bounded tool-use agent; return a StrategyResult or None on miss.

        Scope: this path is SINGLE-SOURCE. It executes through
        ``SqlExecutionService`` and does NOT run
        ``table_qualifier.prepare_execution_sql``, so bare table names are not
        rewritten to ``catalog.schema.table`` and a cross-source query fails
        (Athena TABLE_NOT_FOUND). The ``ontop`` and ``nl_to_sql`` strategies cover
        cross-source.
        """
        if not self._query_executor:
            context.trace.record(StepId.T2_SQL_EXECUTE, "skipped", 0, detail="no query_executor configured")
            return None

        trace = context.trace
        options = context.options
        index_name = (
            ontology_vector_index_name(self._oss_ontology_index, namespace) if self._oss_ontology_index else None
        )

        # Request-scoped tool set: the catalog accumulates every hit the agent
        # sees (its cache is also what pins the data source), the authoring tool
        # writes SQL over that cache, and the execution primitive is the ONLY way
        # a statement reaches an engine.
        catalog = TableCatalog(self._vector, self._sql_generator.llm, namespace=namespace, index=index_name)
        authoring = SqlAuthoringTool(self._sql_generator, catalog, model_id=context.model_id)
        graph = (
            OntologyGraphTool(self._graph_client, catalog, namespace=namespace)
            if (self._graph_client is not None and _graph_traversal_enabled(options))
            else None
        )
        agent = SqlAgent(
            catalog=catalog,
            authoring=authoring,
            execution=SqlExecutionService(self._firewall, self._query_executor),
            graph=graph,
            region=getattr(self._sql_generator.llm, "_region", None) or _DEFAULT_REGION,
            model_id=context.model_id,
            prefetch_schemas=self._prefetch_schemas,
        )

        outcome = await agent.run(
            query,
            namespace=namespace,
            profile=context.profile,
            evidence=capped_evidence(options.get("evidence", "")),
            max_rows=capped_max_rows(options),
            data_source_id=options.get("dataSourceId", ""),
            deadline=context.deadline,
        )
        t_ms = outcome.duration_ms

        # A firewall denial anywhere in the loop is a terminal 403 (mirrors NL→SQL).
        if outcome.denied_reason:
            trace.record(
                StepId.T2_SQL_FIREWALL,
                "denied",
                t_ms,
                detail={"reason": outcome.denied_reason},
                tool_used="sql-firewall",
            )
            raise AccessDeniedError(outcome.denied_reason)

        if not outcome.succeeded:
            trace.record(
                StepId.T2_SQL_GENERATE, "error", t_ms, detail="agent_produced_no_executed_sql", tool_used="bedrock"
            )
            logger.info("agentic_no_result", namespace=namespace, duration_ms=t_ms, unavailable=outcome.unavailable)
            return None

        confidence = _reported_confidence(outcome.confidence)
        # ``outcome.tables`` is every table the agent inspected, not only the ones
        # its SQL read; keep names outside the grant's ``tableAllowlist`` out of
        # the client-visible trace and response.
        visible_tables = tables_visible_to(outcome.tables, context.profile)
        # The execute step reports the tables the executed statement read; what the
        # agent inspected stays in ``retrieved_tables`` / ``expanded_tables``.
        executed_tables = tables_visible_to(SQLFirewall.referenced_tables(outcome.executed_sql), context.profile)
        trace.record(
            StepId.T2_SQL_EXECUTE,
            "success",
            t_ms,
            detail={
                "rowCount": outcome.row_count,
                "tables": executed_tables,
                "deepReasoning": True,
                "confidence": confidence,
            },
            tool_used="bedrock",
        )
        return StrategyResult(
            sql=outcome.executed_sql,
            rows=outcome.rows,
            columns=outcome.columns,
            confidence=confidence,
            strategy_name=StrategyOption.DEEP_REASONING,
            trace_steps=[],
            row_count=outcome.row_count,
            truncated=False,
            retrieved_tables=visible_tables,
            expanded_tables=visible_tables,
            data_source_id=outcome.data_source_id,
        )
