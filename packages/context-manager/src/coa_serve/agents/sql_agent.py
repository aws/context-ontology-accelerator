# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL→SQL agent — a bounded, self-correcting Strands agent over the Tier-2 tools.

Where the single-shot Tier-2 path does a fixed retrieve → generate → (correct)
pipeline, this agent *iteratively* discovers the right tables (searching, then
inspecting candidate schemas and their FKs) before writing SQL, then executes and
self-corrects. On the multi-source Spider 2.0 regime that iterative schema
inspection is the main lever over single-shot retrieval.

This module is only the WIRING: it owns the prompt and the turn-by-turn
translation between the model and the capabilities it may use. It owns no
retrieval, no SQL generation and no execution of its own —

  * ``search_tables`` / ``get_table_schema`` → :class:`TableCatalog` (Tier 2)
  * ``generate_sql``                        → :class:`SqlAuthoringTool` (Tier 2)
  * ``explore_graph`` (opt-in)              → :class:`OntologyGraphTool` (Tier 2)
  * ``execute_sql``                         → :class:`SqlExecutionService`

— so the agent is one consumer of the shared tools rather than their owner, and
the same tools back any other consumer (an MCP tool, an HTTP route) unchanged.

**No arbitrary SQL execution.** The agent never gets a "run this SQL" tool. Every
statement it can execute must have come from ``generate_sql``, which returns an
opaque HANDLE; ``execute_sql`` takes only that handle and runs the corresponding
statement through the shared execute-with-authz primitive (firewall + executor).
Self-correction is unaffected: the execute step returns rows or the engine error,
which feeds the next ``generate_sql`` as revise-feedback.

It runs natively async (Strands ``Agent.invoke_async``) inside the orchestrator's
event loop, with async tools backed by the SAME serve clients the other Tier-2
strategies use — no sources-API, no separate infra.

**What bounds the loop.** Not a turn count (the pinned Strands ignores one): a cap
on statement executions (:data:`_DEFAULT_MAX_EXECUTIONS`, enforced inside the SQL
tools), a stop just before the request deadline that keeps the last statement that
executed successfully, and the per-statement execution timeout
(:data:`_DEFAULT_EXEC_TIMEOUT_S`) — see the note on ``invoke_async`` below.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from ..config import env_with_legacy_name
from ..deadline import Deadline
from ..exceptions import AccessDeniedError
from ..sql_execution import SqlExecutionService, SqlExecutionStatus
from ..tier2.tools import (
    DEFAULT_NODE_LIMIT,
    OntologyGraphTool,
    SqlAuthoringTool,
    TableCatalog,
    UnknownTablesError,
)

logger = structlog.get_logger(__name__)

# Per-tool-call SQL execution timeout inside the agent loop. Kept low by default
# so one slow query can't consume the whole request; overridable for hard DBs
# (complex_oracle/oracle_sql) that legitimately need longer.
_DEFAULT_EXEC_TIMEOUT_S = 35

# Statement executions the agent may run per request. On the COA-100 BIRD nights
# that first ran this agent, runs that finished needed a median of 1 execution
# (p90 4), while every run that hit the request deadline had looped through 5-29
# executions of already-valid SQL. Past the cap, generate_sql/execute_sql stop
# running and tell the model to answer with what it has.
_DEFAULT_MAX_EXECUTIONS = 6

# Time kept back from the request deadline so the agent's best verified result
# can still be returned (strategy result, response assembly) before the
# orchestrator's outer timeout cancels the request and discards it.
_DEADLINE_MARGIN_S = 10.0

_BUDGET_EXHAUSTED = (
    "budget exhausted: no more SQL will be generated or executed. "
    "Stop calling tools and give your final answer from the results you already have."
)

# Rows echoed back to the model as the execution observation.
_PREVIEW_ROWS = 5

# Truncation caps for the trajectory log lines (CloudWatch-side error analysis).
_LOG_SQL_CHARS = 1500
_LOG_OBS_CHARS = 400
_LOG_QUESTION_CHARS = 160

# Token ceiling for the agent's own reasoning turns.
_AGENT_MAX_TOKENS = 4096

_DEFAULT_REGION = "us-east-1"

# Candidate tables searched for, and schemas inlined, BEFORE the first model turn
# (see :meth:`SqlAgent._prefetch_context`). The question alone already identifies
# the likely tables well enough to hand the agent the schema up front instead of
# making it spend two tool turns asking for it; supplying the authoritative schema
# rather than leaving the agent to discover it is the largest measured lever on
# this arm. 0 disables the prefetch and restores pure tool-driven discovery.
_DEFAULT_PREFETCH_SCHEMAS = 3
_PREFETCH_SEARCH_K = 7
_PREFETCH_PREVIEW_CHARS = 160


def _resolve_prefetch_schemas() -> int:
    try:
        return max(
            0,
            int(env_with_legacy_name("SERVE_DEEP_REASONING_PREFETCH_SCHEMAS", str(_DEFAULT_PREFETCH_SCHEMAS))),
        )
    except (TypeError, ValueError):
        return _DEFAULT_PREFETCH_SCHEMAS


def _resolve_exec_timeout() -> int:
    try:
        return max(5, int(env_with_legacy_name("SERVE_DEEP_REASONING_EXEC_TIMEOUT_S", str(_DEFAULT_EXEC_TIMEOUT_S))))
    except (TypeError, ValueError):
        return _DEFAULT_EXEC_TIMEOUT_S


def _resolve_max_executions() -> int:
    try:
        return max(1, int(env_with_legacy_name("SERVE_DEEP_REASONING_MAX_EXECUTIONS", str(_DEFAULT_MAX_EXECUTIONS))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_EXECUTIONS


def _intent_review_enabled() -> bool:
    """True when SERVE_DEEP_REASONING_INTENT_REVIEW is set to a truthy value (env-only)."""
    return env_with_legacy_name("SERVE_DEEP_REASONING_INTENT_REVIEW").strip().lower() in ("1", "true", "on", "yes")


AGENT_SYSTEM_PROMPT = """\
You are a SQL expert answering a question against a relational database you must \
discover through tools. Workflow:
1. The prompt may already list candidate tables and inline the schema of the most \
likely ones. START FROM WHAT IS GIVEN — call search_tables only when the question \
needs a table that is not there, or the given candidates are clearly wrong.
2. Call get_table_schema on any table you will use whose schema is not already \
inlined (inspect columns, types, foreign-key relationships, and any 'allowed \
values'). Do NOT guess columns.
3. Call generate_sql with the exact table names you chose — a focused SQL writer \
produces the query from those tables' schema. Do NOT write SQL yourself.
4. Call execute_sql with the handle generate_sql returned to run that query.
5. If execute_sql returns an error, call generate_sql again (optionally inspect more \
schema first) and execute the new handle. If it returns zero rows and the question \
expects data, reconsider which tables/filters are right and try again.
6. When execute_sql succeeds with the intended result, output the FINAL SQL in <sql>...</sql> tags.

Rules:
- ALWAYS get SQL from generate_sql — never hand-write SQL in your reasoning.
- ALWAYS verify with execute_sql before finishing.
- execute_sql runs ONLY a query generate_sql produced: pass the handle, not SQL text.
- PRECISION: most questions need 1-3 tables. Don't over-retrieve; pick the minimal set.
- The candidate tables may span several unrelated databases; only join tables that \
share a real foreign key. Ignore irrelevant candidates.
- Output the final verified SQL in <sql>...</sql>."""


# Appended to the system prompt ONLY when SERVE_DEEP_REASONING_INTENT_REVIEW is on. Targets
# the confidently-wrong-on-plausible-rows failure mode: the agent runs SQL, sees
# non-empty rows, and finalizes without questioning whether its reading of the
# question's ambiguous terms is the intended one. This adds a mandatory
# interpretation-reconciliation turn BEFORE finalizing — model-driven, no rule
# catalogue. Safe by construction on this path: the last successful SQL is sticky,
# so a re-run during review cannot downgrade an already-good answer to empty.
INTENT_REVIEW_BLOCK = """

BEFORE you output the final <sql>, run this interpretation check (it is NOT optional):
- Re-read the question and list every term that could be read more than one way: \
quantifiers ("highest X, Y and Z" — does it mean rows maximal in ALL of them, in ANY \
one, or ranked by a combination?), metric definitions (how EXACTLY is the rate / \
span / lifespan / average computed, and over what grain — per row, per group, \
distinct?), filters implied but not stated (status, date window, de-duplication), \
and the requested output shape (one number vs a list, rounding, units).
- For the reading your current SQL encodes, state in one line why it is the reading \
the question intends. If a DIFFERENT reading is at least as plausible, generate_sql \
for that reading, execute_sql it, and compare the two results before choosing.
- Only then output the final verified SQL in <sql>...</sql>."""


# NO PROMPT BLOCK FOR explore_graph — deliberately.
#
# An earlier revision appended a paragraph describing the tool and MANDATING it
# ("you MUST call explore_graph before generate_sql"). Strands already passes each
# tool's docstring to the model, so that was a SECOND description of the same tool,
# and repetition in a prompt is itself a nudge: the agent called it on 81 of 135
# Spider 2.0 questions for +1.1pp EX (p=0.69), i.e. it fired everywhere and paid for
# itself nowhere. Trajectories show why — hop-1 FK neighbours are already in the text
# ``get_table_schema`` returns, so most calls re-fetched context the agent had.
#
# Keeping the prompt byte-identical whether or not the tool is wired also makes the
# flag a clean A/B: the only difference between arms is the tool's availability, and
# whether the model reaches for it on its own merits is then a real signal. If call
# volume drops to zero, the follow-up is one line on WHEN it earns its cost (a join
# the search missed), never a re-description of what it does.


def build_system_prompt(intent_review: bool | None = None) -> str:
    """Return the agent system prompt with the opt-in blocks the run enables.

    Args:
        intent_review: Force the interpretation-reconciliation block on/off;
            ``None`` reads ``SERVE_DEEP_REASONING_INTENT_REVIEW``.
    """
    enabled = _intent_review_enabled() if intent_review is None else intent_review
    return AGENT_SYSTEM_PROMPT + (INTENT_REVIEW_BLOCK if enabled else "")


def _record_strands_usage(agent: object) -> None:
    """Fold the Strands agent's reasoning-turn token usage into the request total.

    Best-effort. The agent drives its turns through Strands' own Bedrock model
    provider (not ``BedrockClient.converse``), so those turns are otherwise
    invisible to the request's token accounting. The deployed ``coa_serve`` has no
    ``token_usage`` accumulator module (unlike the reference build), so this is a
    no-op hook kept for parity + a future accumulator — never let telemetry break
    the query path.
    """
    try:
        metrics = getattr(agent, "event_loop_metrics", None)
        usage = getattr(metrics, "accumulated_usage", None) if metrics is not None else None
        if usage:
            logger.debug(
                "agentic_token_usage",
                input_tokens=usage.get("inputTokens"),
                output_tokens=usage.get("outputTokens"),
                total_tokens=usage.get("totalTokens"),
            )
    except Exception:
        logger.debug("agentic_usage_capture_failed")


@dataclass
class SqlAgentOutcome:
    """What one agent run produced.

    Attributes:
        executed_sql: The last statement that executed successfully; empty when
            the agent never got a query to run.
        rows: Rows from that statement.
        columns: Its column names.
        row_count: Its row count.
        confidence: The SQL writer's self-reported confidence (0.0-1.0) for the
            statement that executed — the same self-rating the single-shot
            NL→SQL path reports, carried through so this arm's confidence is a
            real signal on the same scale rather than a constant. 0.0 when
            nothing executed.
        data_source_id: Source the statement was routed to.
        tables: Every table the agent inspected (sorted).
        denied_reason: Set when the firewall/Cedar gate refused a statement —
            terminal for the request, which the caller surfaces as a 403.
        unavailable: True when the Strands dependency is missing, so no run
            happened at all (distinct from a run that produced nothing).
        duration_ms: Wall time of the run.
    """

    executed_sql: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    confidence: float = 0.0
    data_source_id: str = ""
    tables: list[str] = field(default_factory=list)
    denied_reason: str = ""
    unavailable: bool = False
    duration_ms: int = 0

    @property
    def succeeded(self) -> bool:
        """True when the agent verified a statement by executing it."""
        return bool(self.executed_sql)


class SqlAgent:
    """Bounded tool-use agent that answers a question with verified SQL."""

    def __init__(
        self,
        *,
        catalog: TableCatalog,
        authoring: SqlAuthoringTool,
        execution: SqlExecutionService,
        graph: OntologyGraphTool | None = None,
        region: str = _DEFAULT_REGION,
        model_id: str | None = None,
        exec_timeout_s: int | None = None,
        intent_review: bool | None = None,
        prefetch_schemas: int | None = None,
        max_executions: int | None = None,
    ):
        """Wire the agent to the Tier-2 tools and the execution primitive.

        Args:
            catalog: Tier-2 table discovery/schema tools.
            authoring: Tier-2 SQL authoring tool.
            execution: Shared execute-with-authz primitive.
            graph: Tier-2 FK-traversal tool. When None (the default) the agent is
                offered the same four tools and the same prompt as baseline; when
                given, it also gets ``explore_graph`` plus the block describing it.
            region: Bedrock region for the agent's own model provider.
            model_id: Model the agent reasons with; None uses the provider default.
            exec_timeout_s: Per-statement execution timeout; None reads
                ``SERVE_DEEP_REASONING_EXEC_TIMEOUT_S``.
            intent_review: Force the intent-review prompt block; None reads
                ``SERVE_DEEP_REASONING_INTENT_REVIEW``.
            prefetch_schemas: Table schemas to inline before the first turn;
                None reads ``SERVE_DEEP_REASONING_PREFETCH_SCHEMAS``, 0 disables.
            max_executions: Statement executions allowed per run; None reads
                ``SERVE_DEEP_REASONING_MAX_EXECUTIONS``.
        """
        self._catalog = catalog
        self._authoring = authoring
        self._execution = execution
        self._graph = graph
        self._region = region
        self._model_id = model_id
        self._exec_timeout_s = exec_timeout_s if exec_timeout_s is not None else _resolve_exec_timeout()
        self._intent_review = intent_review
        self._prefetch_schemas = prefetch_schemas if prefetch_schemas is not None else _resolve_prefetch_schemas()
        self._max_executions = max_executions if max_executions is not None else _resolve_max_executions()

    async def _prefetch_context(self, question: str) -> str:
        """Search once on the raw question and inline the top tables' schemas.

        Hands the agent the schema it would otherwise spend its first two turns
        asking for: every mapped candidate as ``table — preview``, plus the full
        stored schema (columns, types, FKs, allowed values) of the top
        ``prefetch_schemas`` of them. The candidates also land in the catalog's
        cache, so ``generate_sql`` can resolve those names and the data source is
        already pinned even if the agent never calls a discovery tool.

        Best-effort: a retrieval failure returns "" and the agent falls back to
        tool-driven discovery, exactly as before.

        Args:
            question: The natural-language question, used verbatim as the search.

        Returns:
            A prompt block, or "" when prefetch is disabled or found nothing.
        """
        if self._prefetch_schemas <= 0:
            return ""
        try:
            candidates = await self._catalog.search_tables(question, _PREFETCH_SEARCH_K)
        except Exception as e:
            logger.warning("agentic_prefetch_failed", error=type(e).__name__, message=str(e)[:200])
            return ""
        if not candidates:
            return ""

        schemas: list[str] = []
        for candidate in candidates[: self._prefetch_schemas]:
            try:
                schema = await self._catalog.get_table_schema(candidate.table)
            except Exception as e:
                logger.warning("agentic_prefetch_schema_failed", table=candidate.table, error=type(e).__name__)
                continue
            if schema:
                schemas.append(f"### {candidate.table}\n{schema}")

        listing = "\n".join(f"- {c.table} — {c.preview[:_PREFETCH_PREVIEW_CHARS]}" for c in candidates)
        block = f"\n\nCandidate tables for this question:\n{listing}"
        if schemas:
            block += "\n\nSchemas already retrieved for you (do NOT re-request these):\n\n" + "\n\n".join(schemas)
        logger.info(
            "agentic_prefetch",
            candidates=len(candidates),
            schemas=len(schemas),
            tables=[c.table for c in candidates],
        )
        return block

    async def run(
        self,
        question: str,
        *,
        namespace: str,
        profile: dict[str, Any] | None = None,
        evidence: str = "",
        max_rows: int = 1000,
        data_source_id: str = "",
        deadline: Deadline | None = None,
    ) -> SqlAgentOutcome:
        """Run the bounded tool-use loop and return what it verified.

        Args:
            question: The natural-language question to answer.
            namespace: Namespace the query targets.
            profile: Caller's authorization profile, passed to the firewall.
            evidence: Optional caller hints (already capped by the caller).
            max_rows: Row cap for each execution.
            data_source_id: Source to fall back to when the tables the SQL
                references don't pin one.
            deadline: Request deadline. The loop is stopped ``_DEADLINE_MARGIN_S``
                before it, keeping the last statement that executed successfully,
                rather than letting the outer timeout discard the run.

        Returns:
            A :class:`SqlAgentOutcome`. Never raises for a model/tool failure —
            a run that produced nothing returns an outcome with no SQL.

        Raises:
            AccessDeniedError: Propagated if raised beneath the agent loop.
        """
        t_start = time.perf_counter()
        try:
            from strands import Agent, tool
            from strands.models import BedrockModel
        except ImportError as e:  # pragma: no cover - dependency always present in the runtime image
            logger.warning("agentic_strands_unavailable", error=str(e))
            return SqlAgentOutcome(unavailable=True, duration_ms=int((time.perf_counter() - t_start) * 1000))

        # Capture request_id from the bound contextvars HERE (we're on the
        # entrypoint's asyncio task, where main.py bound it) and stamp it
        # explicitly on every agentic_turn line. merge_contextvars already adds it
        # to in-loop async tools, but Strands may run a tool in a thread executor
        # (contextvars are NOT copied across run_in_executor), so we stamp it
        # explicitly to make trajectory join-by-request_id robust either way.
        req_id = structlog.contextvars.get_contextvars().get("request_id", "")
        outcome = SqlAgentOutcome()

        # SQL the agent is allowed to execute, keyed by the handle generate_sql
        # handed back. This registry IS the no-arbitrary-execution guarantee:
        # execute_sql can only reach statements the Tier-2 writer produced in
        # this run, so the model has no channel for SQL of its own invention.
        pending_sql: dict[str, str] = {}
        # The writer's self-reported confidence per handle, so the outcome can
        # report the rating of the statement that actually executed instead of a
        # fixed score. Same key space as pending_sql.
        pending_confidence: dict[str, float] = {}
        # Most recent (sql, observation) the agent executed — fed to generate_sql
        # as revise-feedback so a re-generation revises rather than repeats
        # identical SQL. Empty until the first execution.
        last_attempt: dict[str, str] = {"sql": "", "obs": ""}
        # Statement executions so far; past self._max_executions the SQL tools
        # refuse and tell the model to answer (see _BUDGET_EXHAUSTED).
        executions = {"count": 0, "generations": 0}

        @tool
        async def search_tables(search: str, top_k: int = 7) -> str:
            """Find database tables semantically related to a search phrase.

            Args:
                search: Natural-language description of the data/tables you need.
                top_k: Number of candidate tables to return (default 7, max 12).
            """
            try:
                candidates = await self._catalog.search_tables(search, top_k)
            except Exception as e:
                return json.dumps({"error": str(e)[:200]})
            return json.dumps({"tables": [{"table": c.table, "preview": c.preview} for c in candidates]})

        @tool
        async def get_table_schema(table_name: str) -> str:
            """Get full schema (columns, types, FK relationships, allowed values) for a table.

            Args:
                table_name: Exact table name (lowercase) from search_tables.
            """
            try:
                schema = await self._catalog.get_table_schema(table_name)
            except Exception as e:
                return f"-- Schema lookup failed for '{table_name}': {str(e)[:150]}"
            return schema or f"-- Table '{table_name}' not found among mapped classes"

        @tool
        async def explore_graph(
            table_name: str, hops: int = 1, node_types: list[str] | None = None, limit: int = DEFAULT_NODE_LIMIT
        ) -> str:
            """Walk the ontology's foreign-key graph outward from a table to find joins.

            Complements search_tables (which finds tables by meaning): this follows
            the foreign-key edges the ontology encodes, so you can discover tables
            that join to a known one — including tables several hops away that a
            single semantic search will not surface. Returns neighbor tables
            (nearest hop first) and, optionally, their columns; the result is
            capped and ordered tables-first. ``table_label`` / ``column_label``
            and ``(label: …)`` / ``[label: …]`` hints are display-only; write SQL
            with ``table`` / ``column``.

            Args:
                table_name: Exact table name (lowercase), ideally one you already
                    found via search_tables so its exact identity is known.
                hops: Foreign-key hops to expand outward (default 1, max 4).
                node_types: Which nodes to return — any of "table", "column".
                    Omit to return all node types within k hops.
                limit: Max nodes to return (default 50); tables fill the budget first.
            """
            if self._graph is None:  # pragma: no cover - tool is not offered in this case
                return json.dumps({"error": "graph traversal not enabled"})
            seed = str(table_name).strip().lower()
            # Log the call BEFORE the query so an invocation is always observable —
            # even one that errors or comes back empty — which is what lets us tell
            # "the agent never called it" apart from "called it and it failed".
            logger.info(
                "agentic_turn",
                phase="explore_graph_call",
                request_id=req_id,
                question=question[:_LOG_QUESTION_CHARS],
                seed=seed,
                hops=hops,
                node_types=node_types,
            )
            try:
                result = await self._graph.explore_graph(seed, hops, node_types, limit)
            except Exception as e:
                logger.info(
                    "agentic_turn",
                    phase="explore_graph_error",
                    request_id=req_id,
                    seed=seed,
                    error=f"{type(e).__name__}: {str(e)[:160]}",
                )
                return json.dumps({"error": f"graph_traversal_failed: {type(e).__name__}: {str(e)[:160]}"})
            logger.info(
                "agentic_turn",
                phase="explore_graph",
                request_id=req_id,
                seed=seed,
                hops=result.get("hops"),
                tables=len(result.get("tables", [])),
                columns=len(result.get("columns", [])),
                truncated=result.get("truncated"),
            )
            return json.dumps(result, default=str)

        @tool
        async def generate_sql(table_names: list) -> str:
            """Generate a SQL query using a focused writer over ONLY the given tables' schema.

            Call this after you've chosen the tables (via search_tables/get_table_schema).
            A dedicated SQL writer produces the simplest correct query — do not write SQL
            yourself. Returns a handle to pass to execute_sql.

            Args:
                table_names: Exact table names (lowercase) to use as context.
            """
            # Self-correction is the whole point of this arm: if a prior execution
            # exists, hand the writer that SQL + its observation so a
            # re-generation REVISES the failed/wrong query instead of returning
            # byte-identical SQL (the writer is a stateless temp-0 function of
            # (question, schema), so without this the retry loop is a no-op).
            # Empty on the FIRST generate → identical to single-shot.
            # Generations are capped too (twice the executions), so a model that keeps
            # regenerating without executing cannot spend writer calls until the deadline.
            if executions["count"] >= self._max_executions or executions["generations"] >= 2 * self._max_executions:
                return json.dumps({"error": _BUDGET_EXHAUSTED})
            executions["generations"] += 1
            feedback = ""
            if last_attempt["sql"]:
                feedback = f"```sql\n{last_attempt['sql']}\n```\nObservation from running it: {last_attempt['obs']}"
            try:
                generated = await self._authoring.generate_sql(
                    question, list(table_names or []), evidence=evidence, feedback=feedback
                )
            except UnknownTablesError as e:
                return json.dumps({"error": f"{e}; call search_tables first"})
            except Exception as e:
                return json.dumps({"error": f"generation_failed: {str(e)[:160]}"})
            if not generated.sql:
                return json.dumps({"error": "generator produced no SQL"})

            handle = f"sql-{len(pending_sql) + 1}"
            pending_sql[handle] = generated.sql
            pending_confidence[handle] = generated.confidence
            note = f" (note: no schema for {generated.missing_tables}, ignored)" if generated.missing_tables else ""
            # Trajectory log (for offline error analysis): the candidate SQL the
            # writer produced this turn, the tables it saw, and whether a prior
            # attempt was fed back. Stamped with the question so turns group per
            # question in CloudWatch.
            logger.info(
                "agentic_turn",
                phase="generate",
                request_id=req_id,
                question=question[:_LOG_QUESTION_CHARS],
                tables=generated.tables_used,
                revised=bool(feedback),
                sql=generated.sql[:_LOG_SQL_CHARS],
            )
            return json.dumps(
                {"handle": handle, "sql": generated.sql, "tables_used": generated.tables_used, "note": note}
            )

        @tool
        async def execute_sql(handle: str) -> str:
            """Execute a query generate_sql produced and return its rows or the error.

            Args:
                handle: The handle returned by generate_sql. Only a generated
                    query can be executed — SQL text is not accepted.
            """
            # Tolerate the model quoting the handle, or echoing back the exact
            # statement generate_sql returned — but nothing else: unknown text is
            # never executed, which is what keeps this from being a run_sql tool.
            key = str(handle).strip().strip("\"'`")
            sql = pending_sql.get(key)
            confidence = pending_confidence.get(key, 0.0)
            if not sql and key in pending_sql.values():
                sql = key
                confidence = next((pending_confidence.get(h, 0.0) for h, s in pending_sql.items() if s == key), 0.0)
            if not sql:
                return json.dumps(
                    {"error": f"unknown handle '{handle}'; call generate_sql and pass the handle it returns"}
                )
            if executions["count"] >= self._max_executions:
                return json.dumps({"error": _BUDGET_EXHAUSTED})
            executions["count"] += 1
            result = await self._execution.execute(
                sql,
                namespace=namespace,
                profile=profile,
                data_source_id=data_source_id,
                data_source_resolver=self._catalog.resolve_data_source,
                max_rows=max_rows,
                timeout_seconds=self._exec_timeout_s,
            )
            if result.status is SqlExecutionStatus.UNSAFE:
                return json.dumps({"error": f"unsafe_sql: {result.reason[:150]}"})
            if result.denied:
                # A deny is terminal for the whole request; the caller turns this
                # into a 403 once the loop unwinds.
                outcome.denied_reason = result.reason
                return json.dumps({"error": f"access_denied: {result.reason}"})
            if result.status is not SqlExecutionStatus.OK:
                # Record the failed attempt for revise-feedback on the next
                # generate_sql, and surface the engine error to the agent. The
                # AUTHORIZED statement is what actually failed, so that (not the
                # pre-firewall text) is what the writer must revise.
                attempted = result.authorized_sql or sql
                last_attempt["sql"] = attempted
                last_attempt["obs"] = f"execution error: {result.reason}"
                logger.info(
                    "agentic_turn",
                    phase="run",
                    request_id=req_id,
                    question=question[:_LOG_QUESTION_CHARS],
                    outcome="error",
                    sql=attempted[:_LOG_SQL_CHARS],
                    obs=f"execution error: {result.reason}",
                )
                return json.dumps({"error": result.reason})

            outcome.executed_sql = result.authorized_sql
            outcome.rows = result.rows
            outcome.columns = result.columns
            outcome.row_count = result.row_count
            outcome.confidence = confidence
            outcome.data_source_id = result.data_source_id
            preview = result.rows[:_PREVIEW_ROWS]
            # Record the executed attempt + a compact observation so that if the
            # agent judges these rows wrong and re-calls generate_sql, the writer
            # can revise from what actually came back (not just from re-reading
            # the schema). Empty/zero-row results are the strongest wrong-signal.
            obs = (
                f"executed OK, returned {result.row_count} row(s); "
                f"columns={result.columns}; preview={json.dumps(preview, default=str)[:_LOG_OBS_CHARS]}"
            )
            last_attempt["sql"] = result.authorized_sql
            last_attempt["obs"] = obs
            logger.info(
                "agentic_turn",
                phase="run",
                request_id=req_id,
                question=question[:_LOG_QUESTION_CHARS],
                outcome="ok",
                row_count=result.row_count,
                sql=result.authorized_sql[:_LOG_SQL_CHARS],
                obs=obs[:_LOG_OBS_CHARS],
            )
            return json.dumps(
                {"row_count": result.row_count, "columns": result.columns, "preview": preview}, default=str
            )

        # BedrockModel uses the same region as the serve LLM client + the request's model.
        model_kwargs: dict[str, Any] = {"region_name": self._region, "max_tokens": _AGENT_MAX_TOKENS}
        if self._model_id:
            model_kwargs["model_id"] = self._model_id
        # The graph tool is offered ONLY when one is wired: with no graph client the
        # agent sees the same four tools and the same prompt text as baseline.
        tools = [search_tables, get_table_schema, generate_sql, execute_sql]
        if self._graph is not None:
            tools.insert(2, explore_graph)
        agent = Agent(
            model=BedrockModel(**model_kwargs),
            tools=tools,
            system_prompt=build_system_prompt(self._intent_review),
        )
        agent.callback_handler = lambda **kw: None

        evidence_block = f"\n\nHint: {evidence}" if evidence else ""
        # Hand over the schema up front rather than making the agent ask for it.
        prefetch_block = await self._prefetch_context(question)
        user_prompt = (
            f"Question: {question}{evidence_block}{prefetch_block}"
            "\n\nWrite SQL over these tables (search for more only if they are insufficient) "
            "and verify it with execute_sql."
        )

        # No turn cap is passed: on the pinned strands-agents, invoke_async takes a
        # max_iterations kwarg into **kwargs and ignores it. The loop is bounded by
        # the execution cap in the SQL tools above and by stopping it just before
        # the request deadline, which keeps ``outcome`` (the last statement that
        # executed successfully) instead of losing it to the outer timeout.
        loop_budget_s = deadline.remaining_s() - _DEADLINE_MARGIN_S if deadline is not None else None
        try:
            if loop_budget_s is not None and loop_budget_s <= 0:
                # Not enough time left to run the loop and still return a result.
                logger.warning(
                    "agentic_deadline_stop", budget_s=round(loop_budget_s, 1), executions=0, has_result=False
                )
            else:
                async with asyncio.timeout(loop_budget_s) as budget:
                    await agent.invoke_async(user_prompt)
        except AccessDeniedError:
            raise
        except TimeoutError as e:
            if budget.expired():
                logger.warning(
                    "agentic_deadline_stop",
                    budget_s=round(loop_budget_s or 0.0, 1),
                    executions=executions["count"],
                    has_result=outcome.succeeded,
                )
            else:  # a TimeoutError raised inside the loop, not our stop
                logger.warning("agentic_agent_failed", error=type(e).__name__, message=str(e)[:200])
        except Exception as e:
            logger.warning("agentic_agent_failed", error=type(e).__name__, message=str(e)[:200])
        if executions["count"] >= self._max_executions:
            logger.info("agentic_execution_cap_reached", executions=executions["count"], has_result=outcome.succeeded)

        # Telemetry: fold the agent's reasoning-turn usage into the request total
        # (best-effort — see _record_strands_usage). Disjoint from generate_sql's
        # calls (already counted via converse).
        _record_strands_usage(agent)

        outcome.tables = self._catalog.known_tables
        outcome.duration_ms = int((time.perf_counter() - t_start) * 1000)
        return outcome
