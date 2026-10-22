# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The Agentic Retriever session orchestration (Req 1, 4, 6, 7, 8, 9, 13).

:class:`AgenticRetriever` is the Tier 3 agentic path. It owns one **reasoning
session** per request and returns the existing frozen
:class:`~coa_serve.tier3.knowledge_retriever.Tier3Result`, so
``Orchestrator._run_tier3`` and ``ResponseAssembler.assemble_tier3`` consume its
output unchanged (design tenet 1). A session:

1. starts a single :class:`BudgetClock` and a single
   :class:`AccumulatedContext`, both **shared** across every Sub_Question and
   Exploration_Branch so the Time_Budget, the max-step cap, and the fan-out cap
   are enforced in aggregate for the whole request (Req 13.9);
2. decomposes the input question into one or more Sub_Questions via the
   :class:`DecompositionPlanner` (Req 13.1-13.3);
3. drives the Sub_Question / Exploration_Branch FIFO queue through the
   :class:`ReasoningController`, one loop per queued item, until the queue drains
   or the wall-clock Time_Budget expires (Req 4, 6, 7.4-7.5);
4. derives exactly one terminal stop reason from the :class:`StopReason` enum
   (Req 6.6) and records it; and
5. synthesizes once over the merged accumulated context via the existing
   guardrailed :class:`Synthesizer` (Req 8.1).

The session sets ``partial`` on the time-budget-expiry, all-tools-fail, and
degraded-source paths (Req 7.5-7.8, 9.6-9.7); populates ``supporting_content`` /
``graph_context`` from the accumulated context (empty when nothing was gathered,
Req 8.2); honors a guardrail block (Req 8.3-8.4) and a synthesis failure
(Req 8.6); and scopes every Tool invocation, Sub_Question, and Exploration_Branch
to the originating request Namespace, blocking and tracing any cross-Namespace
access (Req 1.5, 1.7).

Import discipline (Requirement 12.3): this module imports only dependency-free
siblings (the budget clock, the accumulated context, the shared models) and the
graphrag-free :class:`Tier3Result` at load. The
controller, decomposer, synthesizer, registry, embed client, and trace collector
are referenced structurally and typed under ``TYPE_CHECKING``, so importing this
module never pulls ``graphrag_toolkit`` into ``sys.modules``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ...namespace_language import current_label_preference
from ...query_utils import validate_namespace
from ...trace import client_error_detail
from ..knowledge_retriever import Tier3Result
from .budget import BudgetClock
from .context import AccumulatedContext
from .models import StopReason, SubQuestion
from .tools.structured_tool import NL_TO_SPARQL_TOOL, NL_TO_SQL_TOOL

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime/graphrag import
    from ...clients.base import LLMClient
    from ...trace import TraceCollector
    from ..synthesizer import SynthesisResult, Synthesizer
    from .budget import AgenticBudgetConfig
    from .controller import ReasoningController
    from .decompose import DecompositionPlanner
    from .tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)

# Cap on graph-entity relationships surfaced per entity in ``graph_context``,
# matching the hand-rolled / lexical Tier 3 paths.
_MAX_RELATIONSHIPS_PER_ENTITY = 10

# Floor for the synthesis timeout. A session that has already consumed its whole
# Time_Budget must still get a real synthesis attempt — without a floor,
# ``remaining_s()`` clamps to 0.0 and every overrunning session would time out
# instantly, turning a slow answer into no answer.
#
# MUST clear real synthesis latency, or the timeout kills GOOD answers and
# substitutes the cruder working answer. Measured on the SEC-10-Q eval
# (195 questions): answer_ms p50 17.4s / p90 32.4s / p99 40.1s. An earlier 20s
# floor sat between p50 and p90, fired on 45/195 sessions (23%), and cost 40
# correct→incorrect flips against only 15 the other way — accuracy fell 51.35% →
# 38.50%. 60s clears p99 with headroom, so the fallback is reserved for a genuinely
# hung synthesis (its actual purpose) instead of routinely pre-empting a slow one.
_MIN_SYNTHESIS_TIMEOUT_S = 60.0

# Tools withheld from the planner on a namespace with no structured source. Only
# the structured (DATABASE-backed) tools belong here — retrieval over document
# chunks / the graph is always applicable. Kept as a module constant so the gate
# and the factory agree on exactly which tools are "structured".
_STRUCTURED_TOOL_NAMES: frozenset[str] = frozenset({NL_TO_SQL_TOOL, NL_TO_SPARQL_TOOL})

# Tools withheld on a namespace with no UNSTRUCTURED (DOCUMENTS) source — the
# mirror of the above. These all read the DOCUMENTS-derived chunk/topic vector
# indexes, which do not exist on a structured-only namespace, so offering them
# there wastes reasoning steps on retrievals that can only come back empty.
#
# Passing ``embedding=None`` (what the standard path relies on) is NOT sufficient
# here: ``vector_tool`` accepts EITHER a precomputed embedding OR raw query text
# and re-embeds in the latter case, so it would still search a nonexistent index.
# Withholding the tools from the planner is the only effective gate.
#
# ``graph_traversal`` is deliberately EXCLUDED from this set: it walks the graph,
# which exists for structured namespaces too.
_UNSTRUCTURED_TOOL_NAMES: frozenset[str] = frozenset(
    {"vector_search", "chunk_window"}
    | {
        f"strategy:{name}"
        for name in (
            "chunk_based",
            "chunk_based_semantic",
            "entity_based",
            "entity_context",
            "entity_network",
            "topic_based",
            "traversal",
            "topic_beam",
        )
    }
)


@dataclass(frozen=True)
class _FinalSearch:
    """Handle for the P1 guaranteed-final-search task launched alongside the loop.

    ``tool`` is the resolved semantic retrieval tool (``None`` when none was
    registered); ``task`` is the in-flight ``asyncio`` future for its invocation
    (``None`` when there was no tool to invoke); ``start`` is the launch
    ``perf_counter`` used to report the incorporated step's duration.
    """

    tool: Any | None
    task: Any | None
    start: float


OnTokenCallback = Callable[[str], Awaitable[None]]


class AgenticRetriever:
    """Tier 3 agentic path — same ``resolve`` contract as ``KnowledgeRetriever``.

    A single instance is built once per process by the registry factory (task 17)
    and reused across requests; per-request state lives entirely on the
    :class:`BudgetClock` and :class:`AccumulatedContext` created inside
    :meth:`resolve`, so the retriever itself is stateless and safe to share.

    Args:
        registry: The internal :class:`ToolRegistry` the controller selects Tools
            from. Never exposed outside the Serve_Layer (Req 2.7).
        controller: The :class:`ReasoningController` that drives one
            Sub_Question's Reasoning_Loop. Reused across every queued item.
        decomposer: The :class:`DecompositionPlanner` that splits the input
            question into Sub_Questions (Req 13.1-13.3).
        synthesizer: The existing guardrailed :class:`Synthesizer` used for the
            single final synthesis call (Req 8.1).
        budget: The :class:`AgenticBudgetConfig` whose four budgets bound every
            session; a fresh :class:`BudgetClock` is built from it per request.
        embed_client: Optional planner/embedding :class:`LLMClient`. Held for
            parity with the design and for tools that embed sub-question text; the
            session orchestration itself does not call it directly.
        clock_source: Monotonic time source (seconds) injected into the per-request
            :class:`BudgetClock`. Defaults to :func:`time.monotonic`; tests inject
            a manually advanced source to drive time deterministically.
        final_semantic_search: When ``True`` (default), the session ALWAYS runs a
            standard ``strategy:chunk_based_semantic`` search of the original query
            as a final step (after the reasoning loop, before synthesis), so every
            session combines the ontology-guided graph exploration with the
            baseline semantic retrieval. Disabled in tests that isolate the
            planner-driven loop.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        controller: ReasoningController,
        decomposer: DecompositionPlanner,
        synthesizer: Synthesizer,
        budget: AgenticBudgetConfig,
        *,
        embed_client: LLMClient | None = None,
        clock_source: Callable[[], float] = time.monotonic,
        final_semantic_search: bool = True,
        synthesize_working_answer_only: bool = False,
        synthesize_without_working_answer: bool = False,
    ) -> None:
        """Wire the registry, controller, decomposer, synthesizer, budget, and flags."""
        self._registry = registry
        self._controller = controller
        self._decomposer = decomposer
        self._synthesizer = synthesizer
        self._budget = budget
        self._embed_client = embed_client
        self._clock_source = clock_source
        self._final_semantic_search = final_semantic_search
        # Experiment flag (idea-2 ablation): when True, the final synthesis is
        # given ONLY the agent's grounded running best-guess (working_answer) and
        # NONE of the gathered chunks/entities — to measure how good the interim
        # grounded answer alone is, versus full synthesis over the chunk set. The
        # reasoning loop still runs identically; only what synthesis SEES changes.
        self._synthesize_working_answer_only = synthesize_working_answer_only
        # Experiment flag (idea-2 ablation, complement of the above): when True, the
        # final synthesis is given the full chunks/entities but NOT the working_answer
        # (no <agent_preliminary_findings> block). Ideas 3 and 4 — and the working
        # answer's influence on the reasoning LOOP (it is still computed by assess()
        # and fed back into the planner each step) — are unchanged. Isolates whether
        # injecting the interim answer into the FINAL synthesis helps/hurts/no-ops.
        self._synthesize_without_working_answer = synthesize_without_working_answer

    async def resolve(
        self,
        query: str,
        namespace: str,
        *,
        embedding: list[float] | None = None,
        entity_uris: list[str] | None = None,
        tier2_sparql_hint: str | None = None,
        catalog_summary: list[dict[str, Any]] | None = None,
        structured_data: list[dict[str, Any]] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        on_token: OnTokenCallback | None = None,
        trace: TraceCollector | None = None,
        retriever_strategy: Any | None = None,
        has_structured_source: bool = True,
        has_unstructured_source: bool = True,
        exclude_tools: frozenset[str] = frozenset(),
        profile: dict | None = None,
        options: dict | None = None,
        model_id: str | None = None,
        **_: Any,
    ) -> Tier3Result:
        """Run one agentic reasoning session and synthesize a best-effort answer.

        Mirrors ``KnowledgeRetriever.resolve`` so the orchestrator can call either
        path interchangeably; the agentic-irrelevant keyword arguments
        (``entity_uris``, ``tier2_sparql_hint``, ``structured_data``,
        ``retriever_strategy``) are accepted for signature parity and ignored.

        Args:
            query: The natural-language question to resolve.
            namespace: The originating request Namespace; every Tool invocation,
                Sub_Question, and Exploration_Branch in the session is scoped to it
                (Req 1.5).
            embedding: Optional pre-computed query embedding (passed through to the
                planner / tools; the session does not require it).
            entity_uris: Accepted for ``KnowledgeRetriever.resolve`` signature
                parity; ignored by the agentic path.
            tier2_sparql_hint: Accepted for signature parity; ignored.
            catalog_summary: Optional metric definitions supplied to synthesis.
            structured_data: Accepted for signature parity; ignored (the agentic
                path gathers its own structured rows via the structured tools).
            conversation_history: Optional prior turns supplied to synthesis.
            on_token: Optional streaming callback for synthesis tokens.
            trace: The session :class:`TraceCollector`; one is created when absent.
            retriever_strategy: Accepted for signature parity; ignored.
            has_structured_source: Whether the namespace has a DATABASE source; when
                False the structured (SQL/SPARQL) tools are withheld from the planner.
            has_unstructured_source: Whether the namespace has a DOCUMENTS source;
                when False the chunk/topic vector tools are withheld from the planner.
            exclude_tools: Additional tool names to withhold from the planner for
                this request (per-request ablation via ``options.excludeTools``).
            profile: Optional authenticated caller profile threaded to the tools
                out-of-band (e.g. for structured-query authorization).
            options: Optional per-request options bag threaded to the tools
                out-of-band.
            model_id: Optional per-request model id override for synthesis / tools.

        Returns:
            A :class:`Tier3Result` with ``synthesized_answer``,
            ``supporting_content``, ``graph_context``, ``confidence``,
            ``guardrail_blocked``, ``degraded_sources``, and ``partial`` populated
            per Requirements 1.4, 7, 8, and 9.
        """
        if trace is None:
            from ...trace import TraceCollector as _TraceCollector

            trace = _TraceCollector()

        # Namespace scoping (Req 1.5, 1.7): validate the request Namespace and use
        # it — and only it — to scope every Tool invocation below. There is no path
        # by which a Sub_Question or branch can target a different Namespace; the
        # request Namespace is threaded into the controller for every queued item.
        validate_namespace(namespace)

        clock = BudgetClock(self._budget, clock=self._clock_source)
        clock.started()
        ctx = AccumulatedContext()
        trace.record("agentic_session_start", "success", 0, detail=query[:80])

        # Composition-gate the tools BOTH ways, so the planner is only ever offered
        # tools that can actually produce a result on this namespace:
        #   • no structured (DATABASE) source  → withhold ``nl_to_sql``
        #   • no unstructured (DOCUMENTS) source → withhold the chunk/topic vector
        #     retrievers (all ``strategy:*``, ``vector_search``, ``chunk_window``)
        # A MIXED namespace withholds nothing — either kind of tool might answer, so
        # the planner chooses. Fail-open: both flags default True, so an unknown
        # composition never hides a tool that might answer.
        composition_gated = frozenset()
        if not has_structured_source:
            composition_gated |= _STRUCTURED_TOOL_NAMES
        if not has_unstructured_source:
            composition_gated |= _UNSTRUCTURED_TOOL_NAMES
        # Union: automatic composition gating PLUS any per-request ablation
        # (options.excludeTools). Both withhold tools from the planner's spec list.
        #
        # Scope note: exclusion affects only what the PLANNER can select. The
        # guaranteed final semantic search resolves its own tool directly
        # (``strategy:chunk_based_semantic``, falling back to ``vector_search``) and
        # is deliberately NOT gated — it is the session's floor, not a planner
        # choice. So excluding ``vector_search`` measures the planner's mid-loop use
        # of it, which is the redundancy question worth asking (the final search
        # still runs via the chunk-based strategy).
        excluded_tools = composition_gated | exclude_tools

        # Out-of-band per-request context handed to every Tool invocation, NOT routed
        # through the planner's tool inputs (``validate_inputs`` rejects keys outside a
        # tool's input_schema). Structured tools need the profile: SQLFirewall/Cedar
        # fail CLOSED on an empty profile, so without it every nl_to_sql / nl_to_sparql
        # call is an AccessDeniedError silently rendered as "0 rows".
        tool_context = {"profile": profile or {}, "options": options or {}, "model_id": model_id}
        if excluded_tools:
            trace.record(
                "tools_gated",
                "success",
                clock.elapsed_ms(),
                detail=(
                    f"withholding {sorted(excluded_tools)}"
                    f"{' (composition)' if composition_gated else ''}"
                    f"{' (request ablation)' if exclude_tools else ''}"
                ),
            )

        # Launch the guaranteed final semantic search concurrently with the reasoning
        # loop: it runs over the original query and reads nothing from the accumulated
        # context, so its I/O overlaps decompose + the loop, then is incorporated
        # deterministically after the loop by ``_finish_final_semantic_search``.
        # Skipped when the namespace has no DOCUMENTS source (no chunk index to search).
        final_search = (
            self._start_final_semantic_search(query, namespace, clock, embedding=embedding)
            if has_unstructured_source
            else None
        )

        # 1. Decompose the question into >= 1 Sub_Questions (Req 13.1-13.3). The
        # decomposer is best-effort and never raises (it degrades to a single
        # Sub_Question), so no guard is needed here. The same call reports the
        # question's language, resolved once, so the planner and synthesizer are
        # told the language rather than each guessing it.
        decompose_with_language = getattr(self._decomposer, "decompose_with_language", None)
        if callable(decompose_with_language):
            sub_questions, detected = await decompose_with_language(query, trace)
        else:
            sub_questions, detected = await self._decomposer.decompose(query, trace), ""
        ctx.answer_language = detected or (current_label_preference().preferred_tag or "")
        trace.record(
            "answer_language",
            "success" if ctx.answer_language else "skipped",
            clock.elapsed_ms(),
            detail=ctx.answer_language or "unresolved; prompts match the question's language",
        )

        # 2. Run the initial (independent) Sub_Questions concurrently: each gets a
        # private forked sub-context (own seen_keys, own local branch queue) while the
        # shared BudgetClock enforces the aggregate step/fan-out caps across all of
        # them. Sub-contexts are merged back into ``ctx`` (re-deduped) in deterministic
        # sub-question order, so the gathered set matches sequential accumulation.
        last_stop = await self._run_subquestions_parallel(
            sub_questions,
            ctx,
            clock,
            trace,
            namespace=namespace,
            excluded_tools=excluded_tools,
            tool_context=tool_context,
        )

        # 3. Derive exactly one terminal stop reason (Req 6.6) and record it with
        # the total elapsed time (Req 10.3). The queue is drained per sub-question
        # (each owned its local branch queue), so pass an empty queue here.
        stop_reason = self._derive_stop_reason(clock, deque(), last_stop)
        trace.record(
            "agentic_retrieval_stop",
            "success",
            clock.elapsed_ms(),
            detail=str(stop_reason),
        )

        # 4. Await + incorporate the final semantic search launched above (bounded by
        # a deadline-aware timeout so it cannot eat the synthesis reserve), so the
        # answer is grounded in both the graph exploration and baseline semantic
        # retrieval. Recorded as a degraded source on failure; never raises.
        await self._finish_final_semantic_search(final_search, ctx, trace, clock)

        # 5. Synthesize once over the merged context and build the Tier3Result.
        return await self._synthesize_and_build(
            query,
            ctx,
            clock,
            trace,
            catalog_summary=catalog_summary,
            conversation_history=conversation_history,
            on_token=on_token,
        )

    # ── parallel sub-question execution (P2) ───────────────────────────

    async def _run_subquestions_parallel(
        self,
        sub_questions: list[SubQuestion],
        ctx: AccumulatedContext,
        clock: BudgetClock,
        trace: TraceCollector,
        *,
        namespace: str,
        excluded_tools: frozenset[str] = frozenset(),
        tool_context: dict | None = None,
    ) -> StopReason | None:
        """Run the initial Sub_Questions concurrently, merging results back (P2).

        Each Sub_Question runs in its own forked sub-context (private ``seen_keys``
        so a sibling's overlapping retrieval cannot spoof its ``no_new_information``
        signal) with its own local branch queue (surfaced Exploration_Branches stay
        within their parent Sub_Question). The single shared :class:`BudgetClock` is
        passed to every loop, so the aggregate step and fan-out caps hold across all
        of them (its counters increment atomically — no ``await`` between the check
        and the increment). Forked contexts are merged back into ``ctx`` in
        deterministic Sub_Question order, re-running dedup so the gathered set equals
        sequential accumulation. Returns the last per-Sub_Question stop reason for
        the session-level stop derivation. A single Sub_Question runs inline (no
        fan-out overhead).
        """

        async def _run_one(sub_question: SubQuestion) -> tuple[AccumulatedContext, StopReason | None]:
            sub_ctx = ctx.fork()
            local_queue: deque[SubQuestion] = deque([sub_question])
            last: StopReason | None = None
            # Drain this Sub_Question and any branches it surfaces, under the shared
            # aggregate budget (the clock is global; expiry/step caps stop all loops).
            while local_queue and not clock.expired():
                item = local_queue.popleft()
                last = await self._controller.run_one_loop(
                    item,
                    sub_ctx,
                    self._registry,
                    clock,
                    local_queue,
                    trace,
                    namespace=namespace,
                    excluded_tools=excluded_tools,
                    tool_context=tool_context,
                )
            return sub_ctx, last

        if not sub_questions:
            return None
        if len(sub_questions) == 1:
            sub_ctx, last = await _run_one(sub_questions[0])
            ctx.merge(sub_ctx)
            return last

        results = await asyncio.gather(*(_run_one(sq) for sq in sub_questions))
        # Merge in deterministic Sub_Question order (not completion order) so dedup
        # and the accumulated set are reproducible run to run.
        last_stop: StopReason | None = None
        for sub_ctx, last in results:
            ctx.merge(sub_ctx)
            if last is not None:
                last_stop = last
        return last_stop

    # ── guaranteed final semantic search (graph exploration + semantic) ─

    # The graphrag chunk-based semantic retriever the final search prefers, with a
    # vector-search fallback when the strategy tool is not registered.
    _SEMANTIC_STRATEGY_TOOL = "strategy:chunk_based_semantic"
    _VECTOR_TOOL = "vector_search"

    def _start_final_semantic_search(
        self,
        query: str,
        namespace: str,
        clock: BudgetClock,
        *,
        embedding: list[float] | None,
    ) -> _FinalSearch | None:
        """Launch the guaranteed final semantic search as a background task (P1).

        Resolves the graphrag ``strategy:chunk_based_semantic`` retriever (falling
        back to the vector tool), starts its invocation as an ``asyncio.Task`` so
        its I/O overlaps decompose + the reasoning loop, and returns a
        :class:`_FinalSearch` handle for :meth:`_finish_final_semantic_search` to
        await and incorporate after the loop. The search runs over the ORIGINAL
        query and reads nothing from the accumulated context, so overlapping it is
        safe (its chunks are merged, and deduped, only at finish time). Returns
        ``None`` when the final search is disabled or no semantic tool is
        registered (the finish step then records the appropriate trace).

        Args:
            query: The original request query the semantic search runs over.
            namespace: The request namespace the search is scoped to (Req 1.5).
            clock: The session budget clock (its deadline-aware timeout bounds the
                invocation so it cannot eat the synthesis reserve).
            embedding: Optional precomputed query embedding handed to the vector
                fallback to avoid a redundant embed call.
        """
        if not self._final_semantic_search:
            return None
        tool = self._registry.get(self._SEMANTIC_STRATEGY_TOOL) or self._registry.get(self._VECTOR_TOOL)
        if tool is None:
            return _FinalSearch(tool=None, task=None, start=0.0)

        inputs: dict[str, Any] = {"query": query}
        if embedding is not None and tool.name == self._VECTOR_TOOL:
            inputs["embedding"] = embedding

        start = time.perf_counter()
        # Bound the invocation by the deadline-aware timeout captured at LAUNCH:
        # even overlapped, the final search must not run past the budget minus the
        # synthesis reserve. asyncio.wait_for wraps the coroutine in a task, so the
        # tool I/O proceeds concurrently with the loop we return to.
        timeout_s = clock.tool_deadline_s(self._budget.per_tool_timeout_s)
        task = asyncio.ensure_future(asyncio.wait_for(tool.invoke(namespace=namespace, **inputs), timeout=timeout_s))
        return _FinalSearch(tool=tool, task=task, start=start)

    async def _finish_final_semantic_search(
        self,
        final_search: _FinalSearch | None,
        ctx: AccumulatedContext,
        trace: TraceCollector,
        clock: BudgetClock,
    ) -> None:
        """Await the P1 final search and incorporate its result (never raises, Req 9.1).

        Merges the search's chunks into the accumulated context and records the
        trace step AFTER the loop, so dedup (``ctx.add``) and trace ordering are
        identical to the pre-P1 sequential behavior — only the tool I/O latency was
        overlapped. A missing tool / disabled search records a ``skipped`` step; a
        timeout or error records a degraded source. The awaited task was already
        bounded by the deadline-aware timeout at launch.
        """
        if final_search is None:
            if self._final_semantic_search:
                trace.record(
                    "agentic_final_semantic_search", "skipped", clock.elapsed_ms(), detail="final search disabled"
                )
            return
        if final_search.task is None:
            trace.record(
                "agentic_final_semantic_search",
                "skipped",
                clock.elapsed_ms(),
                detail="no semantic retrieval tool registered",
            )
            return

        tool = final_search.tool
        try:
            result = await final_search.task
            ms = int((time.perf_counter() - final_search.start) * 1000)
            ctx.add(result)
            trace.record("agentic_final_semantic_search", "succeeded", ms, detail=result.detail, tool_used=tool.name)
        except TimeoutError:
            ms = int((time.perf_counter() - final_search.start) * 1000)
            logger.warning("agentic_final_semantic_timeout", tool=getattr(tool, "name", "?"))
            ctx.record_degraded(tool.name, "timeout")
            trace.record(
                "agentic_final_semantic_search", "timeout", ms, detail=f"{tool.name} timeout", tool_used=tool.name
            )
        except Exception as exc:  # noqa: BLE001 - final search must never raise out
            ms = int((time.perf_counter() - final_search.start) * 1000)
            logger.warning(
                "agentic_final_semantic_error",
                tool=getattr(tool, "name", "?"),
                error=str(exc),
                error_type=type(exc).__name__,
            )
            ctx.record_degraded(tool.name, "error")
            trace.record("agentic_final_semantic_search", "error", ms, detail=f"{tool.name} error", tool_used=tool.name)

    # ── stop-reason derivation (Req 6.6) ───────────────────────────────

    def _derive_stop_reason(self, clock: BudgetClock, queue: deque, last_stop: StopReason | None) -> StopReason:
        """Derive the single session-level stop reason from the enum (Req 6.6).

        Precedence: the wall-clock Time_Budget (Req 7.4) outranks the aggregate
        step cap (Req 4.5/6.4), which outranks a drained queue. When the queue
        drained without hitting either aggregate cap, the per-Sub_Question outcome
        (a positive Sufficiency_Check or no-new-information) is reported when one
        was produced, otherwise ``queue_drained``.
        """
        if clock.expired():
            return StopReason.time_budget_expired
        if clock.steps_consumed >= self._budget.max_steps:
            return StopReason.max_steps_reached
        if not queue:
            return last_stop or StopReason.queue_drained
        return last_stop or StopReason.queue_drained

    # ── synthesis & result assembly (Req 7.5-7.8, 8, 9.6-9.7) ──────────

    async def _synthesize_and_build(
        self,
        query: str,
        ctx: AccumulatedContext,
        clock: BudgetClock,
        trace: TraceCollector,
        *,
        catalog_summary: list[dict[str, Any]] | None,
        conversation_history: list[dict[str, str]] | None,
        on_token: OnTokenCallback | None,
    ) -> Tier3Result:
        """Synthesize once over ``ctx`` and assemble the partial-aware Tier3Result.

        Computes the ``partial`` flag (time-budget expiry, any degraded source, or
        no context gathered — Req 7.5-7.8, 9.6), handles the all-tools-failed /
        no-context case by returning no Best_Effort_Answer (Req 9.7), runs the
        single guardrailed synthesis call otherwise, and maps the outcome onto a
        :class:`Tier3Result` (guardrail block Req 8.3-8.4, synthesis failure
        Req 8.6, confidence Req 8.5, supporting content Req 8.2).
        """
        degraded = list(ctx.degraded_sources)
        # Structured rows count as context: a namespace answered purely by NL→SQL /
        # metric tools has no chunks or entities but is NOT a no-context case.
        no_context = not ctx.chunks and not ctx.entities and not ctx.rows
        # Partial on time-budget expiry (Req 7.7-7.8), any degraded source / all
        # tools failing with context (Req 9.6), or no context gathered (Req 7.8).
        partial = clock.expired() or bool(degraded) or no_context

        supporting_content = self._supporting_content(ctx)
        graph_context = self._graph_context(ctx)

        # Req 9.7: every Tool invocation failed AND no context was gathered. Return
        # a partial result with NO Best_Effort_Answer and indicate that no source
        # succeeded — do NOT call the Synthesizer (there is nothing to synthesize).
        if no_context and degraded:
            trace.record(
                "llm_synthesize",
                "skipped",
                clock.elapsed_ms(),
                detail="no source succeeded; no context retrieved",
                tool_used="bedrock",
            )
            return self._build_result(
                synthesized_answer="",
                confidence=0.0,
                supporting_content=supporting_content,
                graph_context=graph_context,
                guardrail_blocked=False,
                trace=trace,
                degraded=degraded,
                partial=True,
            )

        # Single guardrailed synthesis over the accumulated context (Req 8.1).
        # Instrumentation: record the carried grounded best-guess handed to the
        # synthesizer so it can be compared against the final synthesized answer
        # (measuring how often synthesis refines/corrects the agent's interim guess).
        if ctx.working_answer:
            trace.record(
                "agentic_working_answer",
                "success",
                clock.elapsed_ms(),
                detail=ctx.working_answer[:2000],
            )
        try:
            # Bound synthesis by the wall-clock actually left. synthesis_reserve_s
            # only SHRINKS tool timeouts (budget.tool_deadline_s) — it reserves a
            # slice but never bounded the synthesis call itself, so a slow LLM ran
            # unbounded past the Time_Budget and pushed total wall time into the
            # AgentCore invocation ceiling (~131s), where the endpoint returns HTTP
            # 200 with an EMPTY envelope and the answer is lost entirely. Measured on
            # the SEC-10-Q eval: p50 96.9s / p90 117.2s / max 131.8s, with the 131.8s
            # query coming back empty. Floored so a session that has already overrun
            # still gets a real attempt rather than an instant timeout.
            synthesis = await asyncio.wait_for(
                self._synthesize(
                    query,
                    ctx,
                    catalog_summary=catalog_summary,
                    conversation_history=conversation_history,
                    on_token=on_token,
                ),
                timeout=max(_MIN_SYNTHESIS_TIMEOUT_S, clock.remaining_s() + self._budget.synthesis_reserve_s),
            )
        except TimeoutError:
            # The loop's grounded working answer is a strictly better degraded result
            # than an empty string: it is derived from the same retrieved context the
            # synthesizer would have summarized. Mark partial so callers can tell.
            logger.warning(
                "agentic_synthesis_timeout",
                elapsed_ms=clock.elapsed_ms(),
                had_working_answer=bool(ctx.working_answer),
            )
            trace.record(
                "llm_synthesize",
                "error",
                clock.elapsed_ms(),
                detail={"error": "TimeoutError", "fallback": "working_answer" if ctx.working_answer else "empty"},
                tool_used="bedrock",
            )
            return self._build_result(
                synthesized_answer=ctx.working_answer or "",
                confidence=0.3 if ctx.working_answer else 0.0,
                supporting_content=supporting_content,
                graph_context=graph_context,
                guardrail_blocked=False,
                trace=trace,
                degraded=degraded,
                partial=True,
            )
        except Exception as exc:  # noqa: BLE001 - synthesis failure must not raise out
            # Req 8.6: synthesizer raised → partial, empty answer, confidence 0.0,
            # trace the failure.
            logger.error(
                "agentic_synthesis_failure",
                error_type=type(exc).__name__,
                exc_info=True,
            )
            trace.record(
                "llm_synthesize",
                "error",
                clock.elapsed_ms(),
                detail=client_error_detail(exc),
                tool_used="bedrock",
            )
            return self._build_result(
                synthesized_answer="",
                confidence=0.0,
                supporting_content=supporting_content,
                graph_context=graph_context,
                guardrail_blocked=False,
                trace=trace,
                degraded=degraded,
                partial=True,
            )

        # Req 8.3: a guardrail block excludes the blocked content from the answer.
        if synthesis.guardrail_blocked:
            trace.record(
                "llm_synthesize",
                "success",
                synthesis.duration_ms,
                detail="guardrail_blocked",
                tool_used="bedrock",
            )
            return self._build_result(
                synthesized_answer="",
                confidence=0.0,
                supporting_content=supporting_content,
                graph_context=graph_context,
                guardrail_blocked=True,
                trace=trace,
                degraded=degraded,
                partial=partial,
            )

        # Req 8.6: a successful call that returns no usable answer is treated as a
        # synthesis failure — partial, empty answer, confidence 0.0, traced.
        if not synthesis.answer or not synthesis.answer.strip():
            trace.record(
                "llm_synthesize",
                "error",
                synthesis.duration_ms,
                detail="synthesis returned no answer",
                tool_used="bedrock",
            )
            return self._build_result(
                synthesized_answer="",
                confidence=0.0,
                supporting_content=supporting_content,
                graph_context=graph_context,
                guardrail_blocked=False,
                trace=trace,
                degraded=degraded,
                partial=True,
            )

        trace.record("llm_synthesize", "success", synthesis.duration_ms, tool_used="bedrock")
        return self._build_result(
            synthesized_answer=synthesis.answer,
            confidence=min(1.0, max(0.0, synthesis.confidence)),
            supporting_content=supporting_content,
            graph_context=graph_context,
            guardrail_blocked=False,
            trace=trace,
            degraded=degraded,
            partial=partial,
        )

    async def _synthesize(
        self,
        query: str,
        ctx: AccumulatedContext,
        *,
        catalog_summary: list[dict[str, Any]] | None,
        conversation_history: list[dict[str, str]] | None,
        on_token: OnTokenCallback | None,
    ) -> SynthesisResult:
        """Run the single Synthesizer call over the accumulated context (Req 8.1).

        Streams when an ``on_token`` callback is supplied and the Synthesizer
        supports it, mirroring ``KnowledgeRetriever``; otherwise performs a
        single-shot synthesis. Returns the :class:`SynthesisResult` (or raises,
        which the caller turns into the Req 8.6 partial result).
        """
        # idea-2 ablation: feed synthesis ONLY the grounded running best-guess,
        # withholding the gathered chunks/entities, to isolate the interim answer.
        chunks = [] if self._synthesize_working_answer_only else ctx.chunks
        entities = [] if self._synthesize_working_answer_only else ctx.entities
        # Structured rows (NL→SQL / metric tools) flow to synthesis as
        # ``structured_data`` — the synthesizer already renders a structured-data
        # section. Withheld under the working-answer-only ablation, like chunks.
        structured_data = None if self._synthesize_working_answer_only else (ctx.rows or None)
        # idea-2 ablation (complement): withhold the working answer from synthesis
        # while keeping the chunks — isolates idea 2's synthesis contribution.
        working_answer = "" if self._synthesize_without_working_answer else ctx.working_answer
        # Only pass the resolved language when there is one, so a synthesizer that
        # predates the parameter keeps working on an unresolved session.
        language = {"answer_language": ctx.answer_language} if ctx.answer_language else {}
        if on_token is not None and hasattr(self._synthesizer, "synthesize_stream"):
            return await self._synthesizer.synthesize_stream(
                query,
                chunks,
                entities,
                structured_data=structured_data,
                catalog_summary=catalog_summary,
                conversation_history=conversation_history,
                on_token=on_token,
                working_answer=working_answer,
                **language,
            )
        return await self._synthesizer.synthesize(
            query,
            chunks,
            entities,
            structured_data=structured_data,
            catalog_summary=catalog_summary,
            conversation_history=conversation_history,
            working_answer=working_answer,
            **language,
        )

    # ── context → result mappers (Req 8.2) ─────────────────────────────

    @staticmethod
    def _supporting_content(ctx: AccumulatedContext) -> tuple[dict, ...]:
        """Map ALL accumulated chunks to ``supporting_content`` (empty when none).

        The agentic path surfaces every chunk its multi-step session gathered (no
        ``MAX_PROMPT_CHUNKS`` slice) so the returned result — and the synthesizer,
        which is built with ``max_prompt_chunks=None`` for this path — both reflect
        the full gathered context (Req 8.2).
        """
        return tuple(
            {
                "chunkId": c.chunk_id,
                "text": c.text,
                "sourceDoc": c.source_doc,
                "label": c.label,
                "relevanceScore": c.relevance_score,
            }
            for c in ctx.chunks
        )

    @staticmethod
    def _graph_context(ctx: AccumulatedContext) -> tuple[dict, ...]:
        """Map the accumulated entities to ``graph_context`` (empty when none, Req 8.2)."""
        return tuple(
            {
                "uri": e.uri,
                "label": e.label,
                "type": e.type,
                "relationships": e.relationships[:_MAX_RELATIONSHIPS_PER_ENTITY],
            }
            for e in ctx.entities
        )

    @staticmethod
    def _build_result(
        *,
        synthesized_answer: str,
        confidence: float,
        supporting_content: tuple[dict, ...],
        graph_context: tuple[dict, ...],
        guardrail_blocked: bool,
        trace: TraceCollector,
        degraded: list[dict],
        partial: bool,
    ) -> Tier3Result:
        """Assemble the frozen :class:`Tier3Result` for the session.

        Attaches all trace steps recorded up to this point, ordered by invocation
        sequence (Req 10.4), and carries the ``partial`` flag and ``degraded_sources``
        through unchanged (Req 1.4, 7.7, 9.5).
        """
        trace_steps = tuple(
            {
                "step": s.step,
                "status": s.status,
                "durationMs": s.duration_ms,
                "detail": s.detail,
                "toolUsed": s.tool_used,
            }
            for s in trace.steps
        )
        return Tier3Result(
            synthesized_answer=synthesized_answer,
            supporting_content=supporting_content,
            graph_context=graph_context,
            confidence=confidence,
            guardrail_blocked=guardrail_blocked,
            trace_steps=trace_steps,
            degraded_sources=tuple(degraded),
            partial=partial,
        )
