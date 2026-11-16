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

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import structlog
from coa_common import ontology_vector_index_name
from coa_common.constants import StructuredBackend

from ...clients.base import GraphClient, QueryExecutor
from ...exceptions import AccessDeniedError, AmbiguousReferenceError
from ...identity import display_principal
from ...query_utils import (
    SPARQL_PREFIXES,
    get_graph_uri_template,
    graph_scoped_body,
    namespace_graph_prefix,
    validate_namespace,
)
from ...request_caps import SQL_WRITER_EFFORT_LEVELS, token_cap
from ...step_ids import StepId
from ..guardrail_placeholders import masked_literal_in_sql
from ..sql_firewall import FirewallResult, SQLFirewall, UnsafeSQLError, tables_visible_to
from ..strategy import (
    EMPTY_RESULT_CONFIDENCE_FLOOR,
    StrategyContext,
    StrategyOption,
    StrategyResult,
    capped_max_rows,
)
from ..table_qualifier import PreparedSQL, SourceLookup, prepare_execution_sql
from ..tools import OntologyGraphTool
from .sql_generator import (
    DEFAULT_EVIDENCE_PROMPT_CHARS,
    NLtoSQLResult,
    SQLGenerator,
    WriterOptions,
    sql_table_routing,
    table_sources_need_preparation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...tier1.metric_resolver import DeclinedMetricContext

logger = structlog.get_logger(__name__)


# Bounds for ``options.evidenceMaxChars``, which the deep-reasoning loop sets when it
# hands its gathered document evidence to this strategy (``options.sqlWithEvidence``).
# Absent, the historical 500-char cap applies, so the prompt is unchanged.
_EVIDENCE_PROMPT_CHARS_MAX = 20_000


def _evidence_prompt_chars(options: dict) -> int:
    """How many characters of ``options.evidence`` reach the SQL prompt."""
    value = options.get("evidenceMaxChars")
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_EVIDENCE_PROMPT_CHARS
    return max(DEFAULT_EVIDENCE_PROMPT_CHARS, min(_EVIDENCE_PROMPT_CHARS_MAX, value))


def writer_options(options: dict) -> WriterOptions:
    """The per-request SQL-writer adjustments named in ``options`` (all off by default).

    * ``sqlOriginalQuestion`` — with the ``originalQuestion`` the deep-reasoning
      nl_to_sql tool adds, the writer also sees the user's original question.
    * ``tokenCaps.nl2sql`` — output-token cap for generation and correction.
    * ``sqlTimeRules`` — the opt-in time rules.
    * ``computeMetric`` — the compute-metric rule, set only by the deep-reasoning
      nl_to_sql tool under ``sqlComputeMetric``. Its extra input columns change the
      result's shape, which is fine for rows read by the answer step but not for a
      direct Tier-2 answer, so a caller's ``sqlComputeMetric`` is ignored here.
    * ``sqlFeedback`` — a prior attempt to revise (set by the deep-reasoning
      nl_to_sql tool under ``sqlRevision``).
    * ``sqlWriterEffort`` — the writer's model effort level (``low``/``medium``/``high``).
    """
    original = options.get("originalQuestion") if options.get("sqlOriginalQuestion") is True else ""
    return WriterOptions(
        original_question=original if isinstance(original, str) else "",
        max_tokens=token_cap(options, "nl2sql"),
        time_rules=options.get("sqlTimeRules") is True,
        compute_metric=options.get("computeMetric") is True,
        feedback=feedback if isinstance(feedback := options.get("sqlFeedback"), str) else "",
        effort=effort if (effort := options.get("sqlWriterEffort")) in SQL_WRITER_EFFORT_LEVELS else "",
    )


def _governed_metric_block(declined: DeclinedMetricContext | None, resolved_source_id: str) -> str:
    """The governed-metric block to forward to the SQL writer, or ``""`` when it must not be.

    Nothing is forwarded for a template-less metric (``prompt_block`` is empty).
    The block is also dropped when the metric's source
    (``declined.data_source_id``) and the resolved execution source are both known
    and differ: the resolved source's schema may not contain the formula's tables.
    When the resolved source is unknown the block is forwarded best-effort; the
    firewall and executor still enforce scope.
    """
    if declined is None:
        return ""
    block = declined.prompt_block
    if not block:
        return ""
    if declined.data_source_id and resolved_source_id and declined.data_source_id != resolved_source_id:
        logger.info(
            "governed_metric_suppressed_source_mismatch",
            metric=declined.metric_name,
            metric_source=declined.data_source_id,
            resolved_source=resolved_source_id,
        )
        return ""
    return block


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
# Default per-statement execution timeout (seconds), used only when no request
# deadline is threaded; with a deadline, _exec_timeout_seconds() clamps below it.
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


_TRUTHY = ("1", "true", "on", "yes")
_FALSY = ("0", "false", "off", "no")

# Expansion is on unless something turns it off: it raised execution accuracy on
# the measured Spider 2.0 benchmarks and was neutral on BIRD.
_GRAPH_EXPAND_DEFAULT = True


def _tri_state(raw: object) -> bool | None:
    """``True``/``False`` for a recognised flag value, ``None`` for anything else.

    ``None`` means "this layer said nothing" — unset, empty, or a typo — and the
    next layer decides. Distinguishing it from ``False`` is what lets a caller turn
    the walk OFF against a deployment that has it on; treating an unrecognised
    value as ``False`` would have silently overridden the default instead.
    """
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def _graph_expand_enabled(options: dict | None = None) -> bool:
    """True when ontology-graph expansion of the retrieved tables is on.

    When on (and a graph client is wired), the tables one FK hop from the retrieved
    classes are appended to the schema context with their columns.

    Three layers, most specific first: per-request ``options.flatGraphExpand``, then
    deployment-wide ``SERVE_NL2SQL_GRAPH_EXPAND``, then ``_GRAPH_EXPAND_DEFAULT``
    (on). Both explicit layers can say OFF as well as ON, so a control arm must pass
    ``flatGraphExpand: false`` rather than omit the option.
    """
    if options is not None:
        per_request = _tri_state(options.get("flatGraphExpand", ""))
        if per_request is not None:
            return per_request
    deployment = _tri_state(os.environ.get("SERVE_NL2SQL_GRAPH_EXPAND", ""))
    if deployment is not None:
        return deployment
    return _GRAPH_EXPAND_DEFAULT


_VOTE_K_MAX = 5
_VOTE_UNANIMOUS_CONFIDENCE = 0.6
_VOTE_MAJORITY_CONFIDENCE = 0.35
_NULL_SENTINEL = "\x00null"
_ZERO_ROW_FEEDBACK = (
    "The query executed successfully but returned ZERO rows, and no value from the "
    "user's question was used as a filter. The JOINs or non-value filters may be "
    "over-restrictive or incorrect — reconsider the JOINs and structural filters and "
    "produce a corrected query. Do NOT invent or alter a specific entity/identifier value."
)


def _resolve_vote_k(options: dict | None) -> int:
    raw = (options or {}).get("flatSqlVoteK")
    if raw is None or raw == "":
        raw = os.environ.get("SERVE_NL2SQL_VOTE_K", "1")
    try:
        return min(_VOTE_K_MAX, max(1, int(raw)))
    except (TypeError, ValueError):
        return 1


_CandidateStatus = Literal["pending", "executed", "exec_error", "no_sql", "refused"]


@dataclass(frozen=True)
class _Candidate:
    index: int
    sql: str
    self_confidence: float
    exec_sql: str = ""
    rows: list[dict] | None = None
    columns: list[str] | None = None
    row_count: int | None = None
    truncated: bool = False
    status: _CandidateStatus = "pending"
    error: str | None = None
    # A refusal that must reach the caller if every candidate ends in one.
    refusal: Exception | None = None


@dataclass(frozen=True)
class _VoteOutcome:
    winner: _Candidate | None
    lead: _Candidate
    votes: int
    confidence: float
    group_sizes: tuple[int, ...]
    executed: int
    errored: int
    tie_broken: bool


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.split()).rstrip(";").rstrip()


def _signature_value(value: object) -> str:
    if value is None:
        return _NULL_SENTINEL
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        # Twelve digits absorb float noise between equivalent aggregates; ints and
        # Decimals stay exact, so distinct IDs or totals never share a vote.
        value = Decimal(f"{value:.12g}")
    if isinstance(value, (int, Decimal)):
        return str(Decimal(value).normalize())
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _result_signature(rows: list, truncated: bool) -> str:
    normalized = sorted(
        sorted(_signature_value(v) for v in (row.values() if isinstance(row, dict) else row)) for row in rows
    )
    return hashlib.sha256(json.dumps([normalized, truncated]).encode()).hexdigest()


def _pick_winner(cands: Sequence[_Candidate], k: int) -> _VoteOutcome:
    ordered = sorted(cands, key=lambda c: c.index)
    groups: dict[str, list[_Candidate]] = {}
    for cand in ordered:
        if cand.status == "executed":
            groups.setdefault(_result_signature(cand.rows or [], cand.truncated), []).append(cand)
    executed = len({c.exec_sql for c in cands if c.exec_sql})
    errored = sum(1 for c in cands if c.status == "exec_error")
    lead = ordered[0]
    if not groups:
        return _VoteOutcome(None, lead, 0, _vote_confidence(0, k), (), executed, errored, tie_broken=False)
    ranked = sorted(groups.values(), key=lambda g: (-len(g), not g[0].row_count, g[0].index))
    top = ranked[0]
    tied = sum(1 for g in ranked if len(g) == len(top))
    return _VoteOutcome(
        top[0],
        lead,
        len(top),
        _vote_confidence(len(top), k),
        tuple(len(g) for g in ranked),
        executed,
        errored,
        tie_broken=tied > 1,
    )


def _vote_confidence(votes: int, k: int) -> float:
    if votes == k:
        return _VOTE_UNANIMOUS_CONFIDENCE
    if 2 * votes > k:
        return _VOTE_MAJORITY_CONFIDENCE
    return EMPTY_RESULT_CONFIDENCE_FLOOR


def _user_supplied_literal_in_sql(sql: str, query: str) -> bool:
    """True if a string literal filtered on in ``sql`` came from the user's ``query``.

    Used to decide, on a ZERO-row result, whether the filter value was taken
    straight from the user's question. If so, an empty result is the correct
    "no such entity" answer and the self-correction shot must NOT be allowed to
    loosen/re-case/substitute that value — doing so fabricates rows for a
    different, real entity. If no user-supplied literal is involved, an empty
    result may be a genuinely wrong query (bad JOIN, over-restrictive derived
    filter) that still warrants correction.

    Scope: single-quoted STRING literals only (proper-noun / categorical entity
    values); numeric literals are not protected. Match is case-insensitive
    substring, since the model may re-case a value (e.g. user "acme" → SQL 'ACME').
    """
    if not sql or not query:
        return False
    q = query.lower()
    for raw in _SQL_STRING_LITERAL.findall(sql):
        value = raw.replace("''", "'").strip()
        if len(value) >= _MIN_USER_LITERAL_LEN and value.lower() in q:
            return True
    return False


# Written by ontology-engine ingest on each ontology node (NeptuneDBGraphStore.
# create_ontology), from the OntologyType contract enum. User-loaded ontologies
# are "user_created" (API default) or "user_uploaded" (the web app's upload flow);
# induction proposals are "induced" and foundational imports "foundational".
_ONTOLOGY_TYPE_IRI = "https://ontology-workbench.local/vocab#ontology_type"
_USER_ONTOLOGY_TYPES = ("user_created", "user_uploaded")


async def uploaded_ontology_iris(graph: GraphClient, namespace: str, graph_uri_template: str) -> list[str]:
    """IRIs of the namespace's user-loaded (``user_created`` / ``user_uploaded``) ontologies.

    These match the ``ontology_id`` of their classes' vector docs. The pattern is
    one ``owl:Ontology`` triple per ontology, so even the prefix-filter scope is cheap.
    """
    validate_namespace(namespace)
    types = " ".join(f'"{t}"' for t in _USER_ONTOLOGY_TYPES)
    body = f"            VALUES ?type {{ {types} }}\n            ?o a owl:Ontology ; <{_ONTOLOGY_TYPE_IRI}> ?type ."
    prefix = namespace_graph_prefix(graph_uri_template, namespace)
    sparql = f"{SPARQL_PREFIXES}SELECT DISTINCT ?o WHERE {{\n{graph_scoped_body(body, prefix)}\n}}"
    rows = await graph.query(sparql)
    return [str(r["o"]) for r in rows if r.get("o")]


class NLtoSQLStrategy:
    """StructuredQueryStrategy implementation for direct NL-to-SQL generation."""

    name: str = StrategyOption.NL_TO_SQL
    # Backends this strategy answers; the runner keeps it only for a namespace that
    # has one of them (see StructuredQueryTier._strategies_for).
    supported_backends: frozenset[StructuredBackend] = frozenset({StructuredBackend.SQL})

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
                Defaults to the SERVE_NL2SQL_MAX_SHOTS env var or 2.
            graph_client: SPARQL client backing the ontology-graph expansion, which
                is on by default (``SERVE_NL2SQL_GRAPH_EXPAND`` /
                ``options.flatGraphExpand`` turn it off). None disables the walk
                entirely — the generator then runs on retrieval alone, so a
                deployment with no graph wired degrades rather than fails.
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
        """Per-statement execution timeout, derived from the request deadline.

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

    def _fits_deadline(self, context: StrategyContext, estimated_shot_cost_s: float) -> bool:
        """True when another shot (a correction or the vote) fits the remaining request budget.

        No deadline threaded → always True (the shot runs unconditionally). With a
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

        evidence_limit = _evidence_prompt_chars(options)
        writer = writer_options(options)
        evidence = options.get("evidence", "")[:evidence_limit]
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
        # Which source and database each retrieved table lives in, for a namespace
        # pooling several Glue databases; the executor owns the sources registry.
        table_placements = None
        if self._query_executor and hasattr(self._query_executor, "resolve_table_placements"):
            executor = self._query_executor

            async def table_placements(table_sources: dict[str, str]) -> dict[str, tuple[str, str]]:
                return await executor.resolve_table_placements(namespace, table_sources)

        # The namespace's uploaded ontologies, for the top-down fallback when no
        # class carries a data_source_id (see SQLGenerator step 2b).
        uploaded_ontologies = None
        template = get_graph_uri_template()
        if self._graph_client is not None and template:
            graph = self._graph_client

            async def uploaded_ontologies() -> list[str]:
                return await uploaded_ontology_iris(graph, namespace, template)

        # Every database of the namespace holding a retrieved table's name, so one
        # retrieved copy of a shared name is not read as the whole answer.
        same_name_databases = None
        if self._query_executor and hasattr(self._query_executor, "resolve_same_name_databases"):
            owners_executor = self._query_executor

            async def same_name_databases(tables: list[str]) -> dict[str, dict[str, str]]:
                return await owners_executor.resolve_same_name_databases(namespace, tables)

        # Forward the declined governed definition — but only when it is safe to
        # (a real formula, and a source that does not contradict the one resolved
        # here). ``data_source_id`` at this point is the request pin (empty when
        # unpinned, so pre-retrieval we forward best-effort). Empty on every
        # non-declining path, keeping the prompt byte-identical to the baseline.
        governed_metric = _governed_metric_block(context.declined_metric, data_source_id)
        vote_k = _resolve_vote_k(options)
        nl_to_sql_result = await self._sql_generator.generate(
            query,
            namespace=namespace,
            index=index_name,
            evidence=evidence,
            embedding=embedding,
            model_id=context.model_id,
            dialect=dialect,
            graph_expander=graph_expander,
            governed_metric=governed_metric,
            table_placements=table_placements,
            same_name_databases=same_name_databases,
            uploaded_ontologies=uploaded_ontologies,
            evidence_max_chars=evidence_limit,
            writer=writer,
        )

        t_ms = int((time.perf_counter() - t_start) * 1000)

        if nl_to_sql_result.error or not nl_to_sql_result.sql:
            # A retrieval-backend failure or a namespace with no SQL-backed class
            # (``no_tables_retrieved``) is a SKIP with fallthrough to the next
            # strategy; "error" is reserved for a genuine generation failure.
            err = nl_to_sql_result.error or "empty_sql"
            skipped = isinstance(err, str) and (err.startswith("retrieve_failed") or err == "no_tables_retrieved")
            trace.record(
                StepId.T2_SQL_GENERATE,
                "skipped" if skipped else "error",
                t_ms,
                detail=err,
                tool_used="bedrock",
            )
            logger.warning("nl_to_sql_failed", error=nl_to_sql_result.error, duration_ms=t_ms)
            return None

        confidence = nl_to_sql_result.confidence
        # List the tables the generated SQL reads, not the retrieval context:
        # retrieval and the FK walk put tables in front of the SQL writer that the
        # statement may never touch (the context stays in the response metadata as
        # ``retrieved_tables`` / ``expanded_tables``). The grant filter still applies
        # so a name outside ``tableAllowlist`` never reaches the caller.
        tables = tables_visible_to(SQLFirewall.referenced_tables(nl_to_sql_result.sql), profile)
        generate_detail: dict = {"confidence": confidence, "tables": tables}
        if vote_k > 1:
            generate_detail["candidate"] = 0
        trace.record(
            StepId.T2_SQL_GENERATE,
            "success",
            t_ms,
            detail=generate_detail,
            tool_used="bedrock",
        )

        if not self._query_executor:
            # Firewall-check the SQL so an unsafe/denied first shot is still
            # surfaced, then skip (no executor configured — e.g. Tier-3 reuse).
            self._firewall_check(nl_to_sql_result.sql, profile, namespace, trace)
            trace.record(StepId.T2_SQL_EXECUTE, "skipped", 0, detail="no query_executor configured")
            return None

        sql = nl_to_sql_result.sql
        confidence = nl_to_sql_result.confidence
        data_source_id = nl_to_sql_result.data_source_id or options.get("dataSourceId", "")

        if vote_k > 1 and not self._fits_deadline(context, t_ms / 1000):
            trace.record(StepId.T2_SQL_VOTE, "skipped", 0, detail={"k": vote_k, "skippedReason": "deadline"})
            logger.info("nl_to_sql_vote", k=vote_k, skippedReason="deadline")
            vote_k = 1
        last_error: str | None = None

        # Best clean-but-empty result seen so far. A zero-row result is NEVER
        # downgraded to a miss: if a correction shot also comes back empty (or
        # fails), we return this instead of None.
        empty_fallback: StrategyResult | None = None
        first_shot = 1

        if vote_k > 1:
            vote_start = time.perf_counter()
            vote = await self._vote_first_shot(
                query,
                namespace,
                context,
                nl_to_sql_result,
                vote_k,
                dialect=dialect,
                evidence=evidence,
                evidence_limit=evidence_limit,
                writer=writer,
                governed_metric=governed_metric,
                data_source_id=data_source_id,
            )
            confidence = vote.confidence
            # The vote IS shot 1. An empty winner, or a candidate 0 that failed to
            # execute, goes straight to the correction shot with that outcome as
            # feedback rather than re-running the statement. A refused candidate 0
            # (and no winner) enters the loop, whose gate re-raises its refusal
            # without executing anything.
            seed = vote.winner or vote.lead
            if seed.status in ("executed", "exec_error"):
                if seed.status == "executed":
                    result = StrategyResult(
                        sql=seed.exec_sql,
                        rows=seed.rows or [],
                        columns=seed.columns or [],
                        confidence=confidence,
                        strategy_name=StrategyOption.NL_TO_SQL,
                        trace_steps=nl_to_sql_result.trace_steps,
                        row_count=seed.row_count or 0,
                        truncated=seed.truncated,
                        retrieved_tables=nl_to_sql_result.retrieved_tables,
                        expanded_tables=nl_to_sql_result.expanded_tables,
                        data_source_id=nl_to_sql_result.data_source_id or "",
                    )
                    if seed.row_count or self._max_shots <= 1 or _user_supplied_literal_in_sql(seed.exec_sql, query):
                        return result
                    empty_fallback = result
                    last_error = _ZERO_ROW_FEEDBACK
                else:
                    last_error = seed.error
                corrected = None
                if self._max_shots > 1:
                    corrected = await self._correction_shot(
                        query,
                        context,
                        nl_to_sql_result,
                        shot=1,
                        shot_cost_s=time.perf_counter() - vote_start,
                        failed_sql=seed.exec_sql,
                        last_error=last_error,
                        evidence=evidence,
                        evidence_limit=evidence_limit,
                        writer=writer,
                        data_source_id=data_source_id,
                    )
                if corrected is None:
                    return empty_fallback
                sql, confidence = corrected
                first_shot = 2

        for shot in range(first_shot, self._max_shots + 1):
            shot_start = time.perf_counter()
            # Gate + firewall (masked literal or unsafe → abandon, keeping an earlier
            # clean empty result; denied → 403). Re-run per shot because each shot
            # produces distinct SQL that must be authorized.
            fw_result = self._gate_statement(sql, profile, namespace, trace)
            if fw_result is None:
                # Unsafe SQL or a guardrail-masked literal: abandon the strategy, keeping
                # a clean empty result from an earlier shot as the answer.
                return empty_fallback

            # Cross-source qualification, per shot: a corrected statement may
            # reference a different set of tables than the first one. Runs AFTER
            # the firewall for the same reason as the VKG path — Cedar sees
            # extract_tables() output verbatim (see ..table_qualifier). A
            # single-source statement is returned untouched, so the direct-JDBC
            # fast path is unaffected.
            prepared = await self._prepare_sql(fw_result.authorized_sql, nl_to_sql_result, namespace)
            if prepared.error:
                # A reference that cannot be attributed to ONE physical table: refuse
                # with a client-visible ambiguity error (None would read as a miss).
                # Never retried or executed.
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
                # The tables THIS shot's statement read (a correction shot can read
                # different ones than the first), grant-filtered like the generate step.
                tables = tables_visible_to(SQLFirewall.referenced_tables(fw_result.authorized_sql), profile)

                # Zero rows filtering on a user-supplied literal is a final answer
                # (see _user_supplied_literal_in_sql); a non-empty result is final.
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
                last_error = _ZERO_ROW_FEEDBACK
            except Exception as e:
                exec_ms = int((time.perf_counter() - exec_start) * 1000)
                last_error = f"{type(e).__name__}: {e}"
                trace.record(
                    StepId.T2_SQL_EXECUTE,
                    "error",
                    exec_ms,
                    detail={"error": type(e).__name__, "shot": shot},
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

            corrected = await self._correction_shot(
                query,
                context,
                nl_to_sql_result,
                shot=shot,
                shot_cost_s=time.perf_counter() - shot_start,
                failed_sql=exec_sql,
                last_error=last_error,
                evidence=evidence,
                evidence_limit=evidence_limit,
                writer=writer,
                data_source_id=data_source_id,
            )
            if corrected is None:
                break
            sql, confidence = corrected

        # Every remaining shot ended in an execution ERROR (a clean result — rows,
        # or a verified/exhausted empty — returns inside the loop). Return any
        # clean empty result we saw; otherwise None so the next strategy can try.
        return empty_fallback

    async def _correction_shot(
        self,
        query: str,
        context: StrategyContext,
        nl_result: NLtoSQLResult,
        *,
        shot: int,
        shot_cost_s: float,
        failed_sql: str,
        last_error: str | None,
        evidence: str,
        evidence_limit: int,
        writer: WriterOptions,
        data_source_id: str,
    ) -> tuple[str, float] | None:
        """Regenerate the SQL from ``last_error`` when the deadline fits; None means stop."""
        trace = context.trace
        # A shot remains: ask the LLM to correct the SQL from ``last_error``,
        # reusing the already-retrieved schema context. Skip the correction when
        # its estimated cost (what this shot took) plus a margin exceeds the
        # remaining request budget; with no deadline threaded, never skip.
        if not self._fits_deadline(context, shot_cost_s):
            # Record only the decision: the correction did not run, so duration is 0.
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
            # The caller returns the best clean result it has: the verified-empty
            # first-shot result (never a miss), or None after an execution error.
            return None

        correct_start = time.perf_counter()
        try:
            sql, confidence = await self._sql_generator.correct(
                query,
                nl_result.ddl_context,
                # The statement handed to the executor (post-qualification), which
                # is what the engine reported its line/column positions against.
                failed_sql=failed_sql,
                execution_error=last_error,
                evidence=evidence,
                evidence_max_chars=evidence_limit,
                writer=writer,
                model_id=context.model_id,
                # Forward on the retry too — but now the source is KNOWN
                # (``data_source_id`` here is the resolved source, not just the
                # pin), so the mismatch suppression actually bites: a formula
                # authored against a different source is dropped rather than
                # fed into the correction, which would otherwise burn the shot.
                governed_metric=_governed_metric_block(context.declined_metric, data_source_id),
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
                return None
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
            return None
        return sql, confidence

    async def _vote_first_shot(
        self,
        query: str,
        namespace: str,
        context: StrategyContext,
        nl_result: NLtoSQLResult,
        k: int,
        *,
        dialect: str,
        evidence: str,
        evidence_limit: int,
        writer: WriterOptions,
        governed_metric: str,
        data_source_id: str,
    ) -> _VoteOutcome:
        trace = context.trace
        profile = context.profile
        vote_start = time.perf_counter()

        async def extra_candidate(index: int) -> _Candidate:
            start = time.perf_counter()
            try:
                sql, self_confidence = await self._sql_generator.generate_from_context(
                    query,
                    nl_result.ddl_context,
                    evidence=evidence,
                    model_id=context.model_id,
                    dialect=dialect,
                    governed_metric=governed_metric,
                    # The same writer options as candidate 0, so the vote compares
                    # candidates written under one prompt.
                    evidence_max_chars=evidence_limit,
                    writer=writer,
                )
                failure = None if sql else "empty SQL returned"
            except Exception as e:
                # One failed extra candidate only shrinks the vote, like a failed
                # correction shot; log it so a broken vote path is not silent.
                logger.warning(
                    "nl_to_sql_vote_candidate_failed", candidate=index, error=type(e).__name__, exc_info=True
                )
                sql, self_confidence, failure = "", 0.0, type(e).__name__
            ms = int((time.perf_counter() - start) * 1000)
            if failure:
                trace.record(
                    StepId.T2_SQL_GENERATE,
                    "error",
                    ms,
                    detail={"candidate": index, "error": failure},
                    tool_used="bedrock",
                )
                return _Candidate(index, "", 0.0, status="no_sql", error=failure)
            trace.record(
                StepId.T2_SQL_GENERATE,
                "success",
                ms,
                detail={"confidence": self_confidence, "candidate": index},
                tool_used="bedrock",
            )
            return _Candidate(index, sql, self_confidence)

        generated = [
            _Candidate(0, nl_result.sql, nl_result.confidence),
            *await asyncio.gather(*(extra_candidate(i) for i in range(1, k))),
        ]

        by_statement: dict[str, list[_Candidate]] = {}
        for cand in generated:
            if cand.sql:
                by_statement.setdefault(_normalized_sql(cand.sql), []).append(cand)

        settled = [c for c in generated if not c.sql]

        def refuse(group: list[_Candidate], error: Exception | None) -> None:
            settled.extend(replace(c, status="refused", refusal=error) for c in group)

        async def run(group: list[_Candidate], authorized_sql: str) -> None:
            lead = group[0].index
            prepared = await self._prepare_sql(authorized_sql, nl_result, namespace)
            if prepared.error:
                trace.record(StepId.T2_SQL_EXECUTE, "error", 0, detail=prepared.error)
                logger.warning("nl_to_sql_ambiguous_reference", namespace=namespace, error=prepared.error)
                refuse(group, AmbiguousReferenceError(prepared.error))
                return
            exec_start = time.perf_counter()
            try:
                exec_result = await self._query_executor.execute(
                    prepared.sql,
                    namespace=namespace,
                    data_source_id=data_source_id,
                    max_rows=capped_max_rows(context.options),
                    timeout_seconds=self._exec_timeout_seconds(context),
                )
            except Exception as e:
                exec_ms = int((time.perf_counter() - exec_start) * 1000)
                trace.record(
                    StepId.T2_SQL_EXECUTE,
                    "error",
                    exec_ms,
                    detail={"error": type(e).__name__, "shot": 1, "candidate": lead},
                )
                logger.warning("nl_to_sql_execute_failed", error=type(e).__name__, duration_ms=exec_ms, candidate=lead)
                # The full message, as the single-shot loop keeps it: the correction
                # shot is fed this error instead of re-running the statement.
                error = f"{type(e).__name__}: {e}"
                settled.extend(replace(c, exec_sql=prepared.sql, status="exec_error", error=error) for c in group)
                return
            exec_ms = int((time.perf_counter() - exec_start) * 1000)
            truncated = getattr(exec_result, "truncated", False)
            trace.record(
                StepId.T2_SQL_EXECUTE,
                "success",
                exec_ms,
                detail={
                    "rowCount": exec_result.row_count,
                    "tables": tables_visible_to(SQLFirewall.referenced_tables(authorized_sql), profile),
                    "truncated": truncated,
                    "shot": 1,
                    "engine": getattr(exec_result, "engine", "") or "unknown",
                    "candidate": lead,
                },
            )
            settled.extend(
                replace(
                    c,
                    exec_sql=prepared.sql,
                    rows=exec_result.rows,
                    columns=exec_result.columns,
                    row_count=exec_result.row_count,
                    truncated=truncated,
                    status="executed",
                )
                for c in group
            )

        runs = []
        for group in by_statement.values():
            try:
                fw_result = self._gate_statement(group[0].sql, profile, namespace, trace)
            except (AccessDeniedError, AmbiguousReferenceError) as e:
                refuse(group, e)
                continue
            if fw_result is None:
                refuse(group, None)
                continue
            runs.append(run(group, fw_result.authorized_sql))
        # Let every execution finish before an unexpected failure propagates, so no
        # sibling is left running in the background.
        failures = [r for r in await asyncio.gather(*runs, return_exceptions=True) if isinstance(r, BaseException)]
        if failures:
            logger.warning("nl_to_sql_vote_failed", error=type(failures[0]).__name__, failed=len(failures))
            raise failures[0]

        outcome = _pick_winner(settled, k)
        reported = outcome.winner or generated[0]
        detail = {
            "k": k,
            "votes": outcome.votes,
            "agreement": round(outcome.votes / k, 3),
            "groupSizes": list(outcome.group_sizes),
            "distinctSql": len(by_statement),
            "executed": outcome.executed,
            "errored": outcome.errored,
            "dropped": sum(1 for c in settled if c.status in ("no_sql", "refused")),
            "winnerIndex": reported.index,
            "tieBroken": outcome.tie_broken,
            "selfConfidence": reported.self_confidence,
            "confidence": outcome.confidence,
            "skippedReason": None,
        }
        trace.record(StepId.T2_SQL_VOTE, "success", int((time.perf_counter() - vote_start) * 1000), detail=detail)
        logger.info("nl_to_sql_vote", **detail)
        refused = [c for c in settled if c.refusal is not None]
        if len(refused) == k:
            raise min(refused, key=lambda c: c.index).refusal
        return outcome

    async def _prepare_sql(self, sql: str, nl_result: NLtoSQLResult, namespace: str) -> PreparedSQL:
        """Make ``sql`` executable, qualifying to ``catalog.schema.table`` if cross-source.

        Delegates to the shared :func:`~..table_qualifier.prepare_execution_sql`
        so this path and the VKG path cannot diverge on how they rewrite or on how
        they react to an unattributable reference. Returns ``sql`` unchanged for a
        single-source statement. When a cross-source rewrite is required but cannot
        be completed (no registry, an unknown datasource, a missing schema, ...),
        the result carries ``error`` and the caller refuses the query instead of
        executing it bare.

        An ambiguity error IS reachable from here: when the retrieval hits
        attribute one bare name to two classes (e.g. a source exposing
        ``customers`` in two schemas), ``sql_table_routing`` emits an ambiguity
        marker and ``prepare_execution_sql`` refuses the query rather than run it
        against a guessed schema.
        """
        # Skip three sqlglot parses on the single-source path.
        if not table_sources_need_preparation(nl_result.table_sources):
            return PreparedSQL(sql=sql)
        routing = sql_table_routing(sql, nl_result.table_sources, nl_result.table_candidates)
        return await prepare_execution_sql(sql, routing, self._sources, namespace=namespace)

    def _gate_statement(self, sql: str, profile: dict, namespace: str, trace) -> FirewallResult | None:
        """Refuse SQL that filters on a guardrail-masked value, then run the firewall.

        A question naming a value the guardrail anonymized reaches the writer as
        ``{NAME}``; SQL comparing against it answers a different question, so it is
        never run.
        """
        if masked_literal_in_sql(sql):
            trace.record(
                StepId.T2_SQL_FAILED,
                "error",
                0,
                detail={
                    "reason": "guardrail_masked_literal",
                    "message": "The question contained a value the privacy filter masked, so the SQL was not run.",
                },
            )
            logger.warning("nl_to_sql_guardrail_masked_literal", namespace=namespace)
            return None
        return self._firewall_check(sql, profile, namespace, trace)

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
                # Fixed code only: the firewall message quotes the offending SQL.
                detail={"error": "unsafe_sql"},
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
