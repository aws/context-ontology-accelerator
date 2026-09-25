# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""NL-to-SQL strategy — retrieval-grounded LLM SQL generation + firewall + execution.

Two-shot self-correction: the first shot generates SQL from retrieved schema and
executes it. If execution raises a database error (e.g. a type mismatch or an
unknown column), the strategy hands the verbatim engine error back to the LLM for
ONE corrective regeneration against the same retrieved schema, then re-executes.
The retry is capped (``SERVE_NL2SQL_MAX_SHOTS``, default 2) so worst-case latency
is bounded at two LLM calls + two executions — recovering the accuracy of an
agentic self-correction loop without its open-ended turn count. Retrieval and
embedding happen once (first shot only); the correction reuses that context.

A clean execution that returns ZERO rows is handled by WHERE the filter value
came from. If a WHERE literal was taken straight from the user's question, the
empty result is the correct "no such entity" ANSWER and is returned as-is — the
correction shot is NOT allowed to loosen/re-case/substitute it, because that
turns a correct "no such entity → 0 rows" into fabricated rows for a DIFFERENT,
real value (a silent wrong answer that flips nondeterministically across runs).
If no user-supplied literal is involved, a zero-row result may instead be a
genuinely wrong query (bad JOIN, over-restrictive derived filter) and still gets
one correction shot. Correction likewise fires on a true execution error.
"""

from __future__ import annotations

import os
import re
import time

import structlog
from coa_common import ontology_vector_index_name

from ...clients.base import GraphClient, QueryExecutor
from ...exceptions import AccessDeniedError, AmbiguousReferenceError
from ...identity import display_principal
from ...step_ids import StepId
from ..sql_firewall import FirewallResult, SQLFirewall, UnsafeSQLError
from ..strategy import StrategyContext, StrategyOption, StrategyResult, capped_max_rows
from ..table_qualifier import PreparedSQL, SourceLookup, prepare_execution_sql
from ..tools import OntologyGraphTool
from .sql_generator import NLtoSQLResult, SQLGenerator, sql_table_routing, table_sources_need_preparation

logger = structlog.get_logger(__name__)

# Total NL→SQL attempts per query, INCLUDING the first shot. 2 = one generate +
# one execution-error-driven correction. 1 disables self-correction (pure
# one-shot). Capped so latency stays bounded (see module docstring).
_DEFAULT_MAX_SHOTS = 2


def _resolve_max_shots() -> int:
    try:
        return max(1, int(os.environ.get("SERVE_NL2SQL_MAX_SHOTS", str(_DEFAULT_MAX_SHOTS))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SHOTS


# Single-quoted SQL string literals, handling the '' escape (e.g. 'O''Brien').
_SQL_STRING_LITERAL = re.compile(r"'((?:[^']|'')*)'")
# Shortest literal we treat as a "user value". A 1-char filter is too likely to
# be an incidental substring of the question to protect from correction.
_MIN_USER_LITERAL_LEN = 2
# Default per-statement execution timeout (seconds) — the NON-REST fallback used
# only when no request deadline is threaded (the long streaming/AgentCore path,
# RESOLVE_TIMEOUT_S ~170s, or an unwired caller). Preserves the historical fixed
# budget for that path. On the REST path a deadline IS threaded, so
# _exec_timeout_seconds() clamps below this to min(35, remaining - margin) — on a
# 29s REST budget that is always < 29s; this 35 never applies there.
_DEFAULT_EXEC_TIMEOUT_S = 35
# Floor for a deadline-derived execution timeout. Below this a query has no
# realistic chance of completing, so we do not shrink the executor timeout past
# it — the request will simply exhaust its budget and the outer wait_for handles
# the timeout, rather than us handing the engine an unusable 0–1s window.
_MIN_EXEC_TIMEOUT_S = 5.0
# Safety margin (seconds) subtracted from "remaining budget" when deciding
# whether a correction shot fits. Covers firewall re-auth, response assembly and
# serialization AFTER the shot returns, so we do not approve a shot that finishes
# with no time left to actually deliver its result.
_CORRECTION_MARGIN_S = 2.0


def _graph_expand_enabled(options: dict | None = None) -> bool:
    """True when ontology-graph expansion of the retrieved tables is on.

    When on (and a graph client is wired), the tables one FK hop from the retrieved
    classes are appended to the schema context with their columns — this path's only
    route to a table vector retrieval did not rank, since it has one shot and no
    tools. Where the agentic arm's ``explore_graph`` is a tool the model may or may
    not call, this fires on every generation, so the measured effect is the graph's,
    not the model's tool adherence.

    Two toggles, matching the agentic flags: env ``SERVE_NL2SQL_GRAPH_EXPAND``
    (deployment-wide, off) and per-request ``options.flatGraphExpand``. The
    per-request form is what lets expansion-on and expansion-off run PAIRED against
    ONE image; without it the two arms need two deployments and the delta is
    confounded with everything else that differed between them.
    """
    if options and str(options.get("flatGraphExpand", "")).strip().lower() in ("1", "true", "on", "yes"):
        return True
    return os.environ.get("SERVE_NL2SQL_GRAPH_EXPAND", "").strip().lower() in ("1", "true", "on", "yes")


def _user_supplied_literal_in_sql(sql: str, query: str) -> bool:
    """True if a string literal filtered on in ``sql`` came from the user's ``query``.

    Used to decide, on a ZERO-row result, whether the filter value was taken
    straight from the user's question. If so, an empty result is the correct
    "no such entity" answer and the self-correction shot must NOT be allowed to
    loosen/re-case/substitute that value — doing so fabricates rows for a
    different, real entity (the reported bug). If no user-supplied literal is
    involved, an empty result may be a genuinely wrong query (bad JOIN,
    over-restrictive derived filter) that still warrants correction.

    Scope: single-quoted STRING literals only — the reported failure is
    proper-noun / categorical entity values. Numeric literals are intentionally
    not protected (they are far more likely to be incidental and rarely cause a
    fabricated-entity substitution). Match is case-insensitive substring, since
    the model may re-case a value (e.g. user "acme" → SQL 'ACME').
    """
    if not sql or not query:
        return False
    q = query.lower()
    for raw in _SQL_STRING_LITERAL.findall(sql):
        value = raw.replace("''", "'").strip()
        if len(value) >= _MIN_USER_LITERAL_LEN and value.lower() in q:
            return True
    return False


class NLtoSQLStrategy:
    """StructuredQueryStrategy implementation for direct NL-to-SQL generation."""

    name: str = StrategyOption.NL_TO_SQL

    def __init__(
        self,
        sql_generator: SQLGenerator,
        firewall: SQLFirewall,
        query_executor: QueryExecutor | None,
        oss_ontology_index: str = "",
        max_shots: int | None = None,
        graph_client: GraphClient | None = None,
        sources_registry: SourceLookup | None = None,
    ):
        """Wire the SQL generator, firewall, executor, and ontology index.

        Args:
            sql_generator: Retrieval-grounded LLM SQL generator.
            firewall: SQLFirewall enforcing safety and authorization.
            query_executor: Executor that runs authorized SQL; None disables execution.
            oss_ontology_index: OpenSearch ontology index used for class retrieval.
            max_shots: Maximum generation+execution attempts (1 = no self-correction).
                Defaults to the NL_TO_SQL_MAX_SHOTS env var or 2.
            graph_client: Optional SPARQL client backing the opt-in ontology-graph
                expansion (``SERVE_NL2SQL_GRAPH_EXPAND`` /
                ``options.flatGraphExpand``). Unused unless one of those is on, so
                passing it is inert on the default path.
            sources_registry: Optional registry used ONLY to look up each
                datasource's Athena catalog/schema when a generated statement spans
                more than one source (see :mod:`..table_qualifier`). Without it, a
                cross-source statement keeps its previous behaviour. Typed as the
                narrow :class:`~..table_qualifier.SourceLookup` Protocol so a
                wiring mistake fails type-check rather than surfacing as a runtime
                AttributeError on the first cross-source statement.
        """
        self._sql_generator = sql_generator
        self._firewall = firewall
        self._query_executor = query_executor
        self._oss_ontology_index = oss_ontology_index
        self._graph_client = graph_client
        self._sources = sources_registry
        self._max_shots = max_shots if max_shots is not None else _resolve_max_shots()

    def _exec_timeout_seconds(self, context: StrategyContext) -> int:
        """Per-statement execution timeout, derived from the request deadline (A4/A6).

        With a deadline threaded, cap the executor at the time actually left in the
        request budget (leaving the correction margin) so a single statement cannot
        run past the point where its result could still be delivered — but never
        below ``_MIN_EXEC_TIMEOUT_S`` and never above the historical
        ``_DEFAULT_EXEC_TIMEOUT_S``. With no deadline (e.g. the long AgentCore path
        or an unwired caller) the fixed 35s default is preserved exactly. Rounded
        DOWN to whole seconds — conservative, never rounds up past the budget.
        """
        if context.deadline is None:
            return _DEFAULT_EXEC_TIMEOUT_S
        remaining = context.deadline.remaining_s() - _CORRECTION_MARGIN_S
        # Floor at _MIN_EXEC_TIMEOUT_S even when the remaining budget is smaller: the
        # outer asyncio.wait_for owns request cancellation, so a too-generous executor
        # timeout can never cause an actual overrun (the request is cancelled first).
        # Handing the engine a floored 5s vs a literal sub-2s window changes nothing
        # about correctness but avoids a degenerate 0-1s timeout the engine cannot use.
        clamped = min(float(_DEFAULT_EXEC_TIMEOUT_S), max(_MIN_EXEC_TIMEOUT_S, remaining))
        result = int(clamped)
        if result < _DEFAULT_EXEC_TIMEOUT_S:
            # Budget forced the executor below its historical default. Surface it so
            # a mis-tuned _CORRECTION_MARGIN_S / _MIN_EXEC_TIMEOUT_S is visible rather
            # than silently starving the engine; the normal (unclamped) path stays quiet.
            logger.info(
                "nl_to_sql_exec_timeout_clamped",
                exec_timeout_s=result,
                remaining_s=round(context.deadline.remaining_s(), 2),
                floored=result == int(_MIN_EXEC_TIMEOUT_S),
            )
        return result

    def _correction_fits_deadline(self, context: StrategyContext, estimated_shot_cost_s: float) -> bool:
        """True when another correction shot fits the remaining request budget.

        No deadline threaded → always True (pre-A0 behaviour: the correction shot
        runs unconditionally, as it always did on the long-budget path). With a
        deadline, the next shot is approved only when the time left (minus a margin
        for post-shot assembly/serialization) covers its estimated cost — estimated
        as what the shot just completed took.
        """
        if context.deadline is None:
            return True
        return context.deadline.remaining_s() >= (estimated_shot_cost_s + _CORRECTION_MARGIN_S)

    async def resolve(self, query: str, namespace: str, context: StrategyContext) -> StrategyResult | None:
        """Generate SQL from the query, enforce the firewall, and execute it.

        Args:
            query: The natural-language query to answer.
            namespace: Namespace the query targets.
            context: Shared strategy context (embedding, profile, options, trace).

        Returns:
            A StrategyResult on success, or None when this strategy should be
            skipped so the next strategy can try.

        Raises:
            AccessDeniedError: If the firewall denies the generated SQL (terminal).
        """
        trace = context.trace
        profile = context.profile
        options = context.options
        embedding = context.embedding

        t_start = time.perf_counter()
        index_name = (
            ontology_vector_index_name(self._oss_ontology_index, namespace) if self._oss_ontology_index else None
        )

        # Resolve the target SQL dialect from the namespace's sources so the LLM
        # generates SQL in the correct dialect for the target engine. This avoids
        # generating Trino SQL for a PostgreSQL-only source, which would fail or
        # produce incorrect results after transpilation.
        dialect = "athena"
        data_source_id = options.get("dataSourceId", "")
        if self._query_executor and hasattr(self._query_executor, "resolve_target_dialect"):
            try:
                dialect = await self._query_executor.resolve_target_dialect(namespace, data_source_id)
            except Exception as e:
                logger.warning("dialect_resolution_failed", error=str(e), fallback="athena")

        evidence = options.get("evidence", "")[:500]
        # Request-scoped: the tool caches this namespace's FK edges and named graphs
        # for the life of the object, and that cache must not outlive a request (a
        # namespace can be re-ingested between two of them). No catalog is passed —
        # there is no agent here, so seeds are the retrieved classes' IRIs rather
        # than a table name needing resolution.
        graph_expander = (
            OntologyGraphTool(self._graph_client, namespace=namespace)
            if (_graph_expand_enabled(options) and self._graph_client is not None)
            else None
        )
        nl_to_sql_result = await self._sql_generator.generate(
            query,
            namespace=namespace,
            index=index_name,
            evidence=evidence,
            embedding=embedding,
            model_id=context.model_id,
            dialect=dialect,
            graph_expander=graph_expander,
        )

        t_ms = int((time.perf_counter() - t_start) * 1000)

        if nl_to_sql_result.error or not nl_to_sql_result.sql:
            # Consolidate all sub-steps into a single t2.sql.generate step (E10).
            # A retrieval-backend failure (e.g. the vector proxy/index being
            # unavailable) means this strategy could not even start — that's a
            # graceful SKIP with fallthrough to the next strategy (VKG/Ontop),
            # not an error. Reserve "error" for a genuine generation failure so
            # the trace doesn't show a red step on every query when only the
            # optional NL→SQL retrieval path is degraded.
            err = nl_to_sql_result.error or "empty_sql"
            skipped = isinstance(err, str) and err.startswith("retrieve_failed")
            trace.record(
                StepId.T2_SQL_GENERATE,
                "skipped" if skipped else "error",
                t_ms,
                detail=err,
                tool_used="bedrock",
            )
            logger.warning("nl_to_sql_failed", error=nl_to_sql_result.error, duration_ms=t_ms)
            return None

        # Consolidate sub-steps into single t2.sql.generate/success step (E10)
        confidence = nl_to_sql_result.confidence
        tables = nl_to_sql_result.expanded_tables or nl_to_sql_result.retrieved_tables or []
        trace.record(
            StepId.T2_SQL_GENERATE,
            "success",
            t_ms,
            detail={"confidence": confidence, "tables": tables},
            tool_used="bedrock",
        )

        if not self._query_executor:
            # Firewall-check the SQL so an unsafe/denied first shot is still
            # surfaced, then skip (no executor configured — e.g. Tier-3 reuse).
            self._firewall_check(nl_to_sql_result.sql, profile, namespace, trace)
            trace.record(StepId.T2_SQL_EXECUTE, "skipped", 0, detail="no query_executor configured")
            return None

        # ── Two-shot loop: generate → firewall → execute; regenerate ONCE on an
        # execution ERROR, or on a zero-row result whose filter value did NOT come
        # from the user's question. A zero-row result that filters on a
        # user-supplied literal is a legitimate "no such entity" answer and is
        # returned as-is — see the empty-result handling below and the docstring. ─
        sql = nl_to_sql_result.sql
        confidence = nl_to_sql_result.confidence
        data_source_id = nl_to_sql_result.data_source_id or options.get("dataSourceId", "")
        last_error: str | None = None

        # Best clean-but-empty result seen so far. A zero-row result is NEVER
        # downgraded to a miss: if a correction shot also comes back empty (or
        # fails), we return this instead of None.
        empty_fallback: StrategyResult | None = None

        for shot in range(1, self._max_shots + 1):
            shot_start = time.perf_counter()
            # Firewall (unsafe → skip strategy; denied → 403). Re-run per shot
            # because each shot produces distinct SQL that must be authorized.
            fw_result = self._firewall_check(sql, profile, namespace, trace)
            if fw_result is None:
                return None  # unsafe SQL — abandon the strategy

            # Cross-source qualification, per shot: a corrected statement may
            # reference a different set of tables than the first one. Runs AFTER
            # the firewall for the same reason as the VKG path — Cedar sees
            # extract_tables() output verbatim (see ..table_qualifier). A
            # single-source statement is returned untouched, so the direct-JDBC
            # fast path is unaffected.
            prepared = await self._prepare_sql(fw_result.authorized_sql, nl_to_sql_result, namespace)
            if prepared.error:
                # A reference that cannot be attributed to ONE physical table — the
                # same bare name in two sources/schemas. Refuse the query with a
                # client-visible ambiguity error instead of returning None: a bare
                # None is masked downstream as an incidental miss (NoResult), which
                # hides a mis-route behind a generic "no data". Raising a terminal
                # ServeError surfaces the explicit reason ("...is ambiguous...") to
                # the caller and tells them how to proceed (qualify the reference or
                # pass a source). Not retried (a correction shot hits the same
                # ambiguity) and not executed (it could read a different source's
                # table than the one authorized) — the same fail-closed treatment as
                # unsafe SQL, but reported rather than silently skipped.
                trace.record(StepId.T2_SQL_EXECUTE, "error", 0, detail=prepared.error)
                logger.warning("nl_to_sql_ambiguous_reference", namespace=namespace, error=prepared.error)
                raise AmbiguousReferenceError(prepared.error)
            exec_sql = prepared.sql

            exec_start = time.perf_counter()
            try:
                exec_result = await self._query_executor.execute(
                    exec_sql,
                    namespace=namespace,
                    data_source_id=data_source_id,
                    max_rows=capped_max_rows(options),
                    timeout_seconds=self._exec_timeout_seconds(context),
                )
                exec_ms = int((time.perf_counter() - exec_start) * 1000)
                result = StrategyResult(
                    # The SQL that actually ran (qualified when cross-source).
                    sql=exec_sql,
                    rows=exec_result.rows,
                    columns=exec_result.columns,
                    confidence=confidence,
                    strategy_name=StrategyOption.NL_TO_SQL,
                    trace_steps=nl_to_sql_result.trace_steps,
                    row_count=exec_result.row_count,
                    truncated=getattr(exec_result, "truncated", False),
                    retrieved_tables=nl_to_sql_result.retrieved_tables,
                    expanded_tables=nl_to_sql_result.expanded_tables,
                    data_source_id=nl_to_sql_result.data_source_id or "",
                )
                tables = nl_to_sql_result.expanded_tables or nl_to_sql_result.retrieved_tables or []

                # Decide whether a ZERO-row result is a legitimate, deterministic
                # answer or a signal worth one corrective regeneration. A
                # non-empty result is always final. For zero rows the question is
                # WHERE the filter value came from: if a WHERE literal was taken
                # straight from the user's question, an empty result is the CORRECT
                # "no such entity" answer — coaxing the model to loosen/re-case it
                # only fabricates rows for a DIFFERENT, real value. If NO
                # user-supplied literal is involved, an empty result may instead be
                # a genuinely wrong query (bad JOIN / over-restrictive derived
                # filter), which still gets the one correction shot.
                user_value_filter = exec_result.row_count == 0 and _user_supplied_literal_in_sql(
                    fw_result.authorized_sql, query
                )
                final_empty = exec_result.row_count == 0 and (user_value_filter or shot >= self._max_shots)
                trace.record(
                    StepId.T2_SQL_EXECUTE,
                    "success",
                    exec_ms,
                    detail={
                        "rowCount": exec_result.row_count,
                        "tables": tables,
                        "truncated": getattr(exec_result, "truncated", False),
                        "shot": shot,
                        # Executing engine — athena | redshift | jdbc.
                        "engine": getattr(exec_result, "engine", "") or "unknown",
                        # A verified-empty result returned AS the answer (not a
                        # retry trigger) — surfaced so the decision is observable.
                        "deterministicEmpty": final_empty,
                        # Empty result protected because a WHERE literal came from
                        # the user's question (vs. simply out of correction shots).
                        "userValueFilter": user_value_filter,
                    },
                )
                if exec_result.row_count > 0 or final_empty:
                    return result if exec_result.row_count > 0 else (empty_fallback or result)
                # Zero rows, no user-supplied literal, a shot remains: keep the
                # clean empty result as a fallback (never downgraded to a miss) and
                # ask for ONE correction — steered at JOINs/structure, explicitly
                # NOT at inventing or altering a specific entity value.
                empty_fallback = empty_fallback or result
                last_error = (
                    "The query executed successfully but returned ZERO rows, and no value from the "
                    "user's question was used as a filter. The JOINs or non-value filters may be "
                    "over-restrictive or incorrect — reconsider the JOINs and structural filters and "
                    "produce a corrected query. Do NOT invent or alter a specific entity/identifier value."
                )
            except Exception as e:
                exec_ms = int((time.perf_counter() - exec_start) * 1000)
                last_error = f"{type(e).__name__}: {e}"
                trace.record(
                    StepId.T2_SQL_EXECUTE,
                    "error",
                    exec_ms,
                    detail={"error": type(e).__name__, "message": str(e)[:120], "shot": shot},
                )
                logger.warning(
                    "nl_to_sql_execute_failed",
                    error=type(e).__name__,
                    duration_ms=exec_ms,
                    shot=shot,
                    max_shots=self._max_shots,
                )
                if shot >= self._max_shots:
                    break

            # A shot remains (reached on an execution ERROR, or a zero-row result
            # with no user-supplied filter value): ask the LLM to correct the SQL
            # from the feedback in ``last_error`` — model-driven, no rule
            # catalogue — reusing the already-retrieved schema context.
            #
            # A4 — deadline-aware correction: before spending another full
            # generate→firewall→execute cycle, check it can FIT the remaining
            # request budget. The correction shot is a latency-doubling accuracy
            # feature; over a short transport (REST, ~29s) the second shot pushes
            # total latency past the ceiling and the caller 504s AFTER serve has
            # already computed a usable first-shot answer. Rather than start work
            # that cannot return, skip the correction and return the best result in
            # hand. Estimated cost of the next shot = what THIS shot just took
            # (generate was already paid once; a correction is generate+execute of
            # comparable magnitude). No deadline threaded → estimate is None →
            # never skip (pre-A0 behaviour preserved, e.g. the 170s AgentCore path
            # where the second shot always fits).
            shot_cost_s = time.perf_counter() - shot_start
            if not self._correction_fits_deadline(context, shot_cost_s):
                # Record ONLY the decision — not a latency. Per the trace model,
                # step latency is the true elapsed time of a step that ran; this
                # step represents a correction that did NOT run, so its duration is
                # genuinely 0. The shot that DID run already recorded its own
                # latency on its t2.sql.generate/t2.sql.execute steps — we do not
                # restate a duration here. The deadline is an independent dimension
                # surfaced at request level (see main.py), so the detail carries
                # only the boolean reason for the skip, never seconds.
                trace.record(
                    StepId.T2_SQL_GENERATE,
                    "skipped",
                    0,
                    detail={
                        "reason": "deadline_insufficient_for_correction",
                        "shot": shot,
                    },
                    tool_used="bedrock",
                )
                logger.info(
                    "nl_to_sql_correction_skipped_deadline",
                    shot=shot,
                    last_shot_s=round(shot_cost_s, 2),
                    remaining_s=round(context.deadline.remaining_s(), 2) if context.deadline else None,
                )
                # Return the best clean result we have. For the zero-row branch this
                # is the verified-empty first-shot result (never a miss); for the
                # execution-error branch there is no clean result, so fall through
                # to the post-loop miss handling by breaking.
                if empty_fallback is not None:
                    return empty_fallback
                break

            correct_start = time.perf_counter()
            try:
                sql, confidence = await self._sql_generator.correct(
                    query,
                    nl_to_sql_result.ddl_context,
                    failed_sql=fw_result.authorized_sql,
                    execution_error=last_error,
                    evidence=evidence,
                    model_id=context.model_id,
                )
                correct_ms = int((time.perf_counter() - correct_start) * 1000)
                if not sql:
                    logger.warning("nl_to_sql_correction_empty", shot=shot)
                    trace.record(
                        StepId.T2_SQL_GENERATE,
                        "error",
                        correct_ms,
                        detail={"correction_shot": shot + 1, "error": "empty SQL returned"},
                        tool_used="bedrock",
                    )
                    break
                trace.record(
                    StepId.T2_SQL_GENERATE,
                    "success",
                    correct_ms,
                    detail={"confidence": confidence, "correction_shot": shot + 1},
                    tool_used="bedrock",
                )
            except Exception as e:
                correct_ms = int((time.perf_counter() - correct_start) * 1000)
                logger.warning("nl_to_sql_correction_failed", error=type(e).__name__, shot=shot)
                trace.record(
                    StepId.T2_SQL_GENERATE,
                    "error",
                    correct_ms,
                    detail={"correction_shot": shot + 1, "error": type(e).__name__},
                    tool_used="bedrock",
                )
                break

        # Every remaining shot ended in an execution ERROR (a clean result — rows,
        # or a verified/exhausted empty — returns inside the loop). Return any
        # clean empty result we saw; otherwise None so the next strategy can try.
        return empty_fallback

    async def _prepare_sql(self, sql: str, nl_result: NLtoSQLResult, namespace: str) -> PreparedSQL:
        """Make ``sql`` executable, qualifying to ``catalog.schema.table`` if cross-source.

        Delegates to the shared :func:`~..table_qualifier.prepare_execution_sql`
        so this path and the VKG path cannot diverge on how they rewrite or on how
        they react to an unattributable reference. Returns ``sql`` unchanged for a
        single-source statement, when no registry is wired, or when the rewrite
        cannot be completed.

        An ambiguity error IS reachable from here: when the retrieval hits
        attribute one bare name to two classes (e.g. a source exposing
        ``customers`` in two schemas), ``sql_table_routing`` emits an ambiguity
        marker and ``prepare_execution_sql`` refuses the query rather than run it
        against a guessed schema.
        """
        # Cheap short-circuit that skips three sqlglot parses (routing extraction,
        # restore, qualify) on the common single-source path. Preparation is a
        # no-op UNLESS the hits span two real sources or mark a name ambiguous —
        # ``table_sources_need_preparation`` distinguishes those from the single
        # unambiguous source that needs nothing done.
        if not table_sources_need_preparation(nl_result.table_sources):
            return PreparedSQL(sql=sql)
        routing = sql_table_routing(sql, nl_result.table_sources)
        return await prepare_execution_sql(sql, routing, self._sources, namespace=namespace)

    def _firewall_check(self, sql: str, profile: dict, namespace: str, trace) -> FirewallResult | None:
        """Run the SQL firewall + Cedar gate for one candidate statement.

        Returns the FirewallResult on allow. Returns None (caller abandons the
        strategy) when the SQL is structurally unsafe. Raises AccessDeniedError
        on a policy denial (terminal 403) — matching the original behavior.
        """
        fw_start = time.perf_counter()
        try:
            fw_result = self._firewall.evaluate(sql, profile, namespace=namespace)
        except UnsafeSQLError as e:
            fw_ms = int((time.perf_counter() - fw_start) * 1000)
            trace.record(
                StepId.T2_SQL_FIREWALL,
                "error",
                fw_ms,
                detail={"error": "unsafe_sql", "message": str(e)[:200]},
                tool_used="sql-firewall",
            )
            logger.warning("nl_to_sql_unsafe_sql", message=str(e), security_event="unsafe_sql_attempt")
            return None
        fw_ms = int((time.perf_counter() - fw_start) * 1000)
        principal_id = display_principal(profile) or "unknown"
        if fw_result.denied:
            trace.record(
                StepId.T2_SQL_AUTHORIZE,
                "denied",
                fw_ms,
                detail={"principal": principal_id, "decision": "deny"},
                tool_used="cedar",
            )
            trace.record(
                StepId.T2_SQL_FIREWALL,
                "denied",
                fw_ms,
                detail={"reason": fw_result.reason},
                tool_used="sql-firewall",
            )
            logger.warning("nl_to_sql_firewall_denied", namespace=namespace, reason=fw_result.reason)
            raise AccessDeniedError(fw_result.reason)
        trace.record(
            StepId.T2_SQL_AUTHORIZE,
            "allow",
            fw_ms,
            detail={"principal": principal_id, "decision": "allow"},
            tool_used="cedar",
        )
        trace.record(
            StepId.T2_SQL_FIREWALL,
            "success",
            fw_ms,
            detail={"decision": "authorized"},
            tool_used="sql-firewall",
        )
        return fw_result
