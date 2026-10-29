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
import contextlib
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ...exceptions import AccessDeniedError
from ...namespace_language import current_label_preference
from ...query_utils import validate_namespace
from ...request_caps import token_cap
from ...trace import client_error_detail
from ..knowledge_retriever import Tier3Result, _has_document_provenance, _supporting_content_from_chunk
from ..synthesizer import fit_chunks_to_budget, synth_max_context_chars
from .budget import BudgetClock
from .context import AccumulatedContext
from .controller import PendingSql, insufficiency_reason, take_pending_sql
from .models import StopReason, SubQuestion
from .tools.base import ToolResult
from .tools.document_tool import DEFAULT_MAX_CHARS, DOCUMENT_CHUNK_PREFIX, rank_source_documents
from .tools.document_tool import TOOL_NAME as READ_DOCUMENT_TOOL
from .tools.find_documents_tool import TOOL_NAME as FIND_DOCUMENTS_TOOL
from .tools.ontology_tool import TOOL_NAME as ONTOLOGY_LOOKUP_TOOL
from .tools.structured_tool import NL_TO_SPARQL_TOOL, NL_TO_SQL_TOOL

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime/graphrag import
    from ...clients.base import LLMClient
    from ...trace import TraceCollector
    from ..synthesizer import SynthesisResult, Synthesizer
    from ..vector_retriever import ChunkResult
    from .budget import AgenticBudgetConfig
    from .controller import ReasoningController
    from .decompose import DecompositionPlanner
    from .ontology import Ontology
    from .tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)


def _last_executed_queries(ctx: AccumulatedContext) -> tuple[str | None, str | None]:
    """Return the last executed (SQL, SPARQL) statements from the session (#1166).

    Deep Reasoning may run several structured statements across its sub-questions;
    Standard mode exposes a single ``queryUsed`` / ``sparqlGenerated``. To match
    that shape we surface the LAST statement of each kind — the one that produced
    the numbers closest to the final answer. Returns ``(None, None)`` when the
    session ran no structured tool (a document/graph-only answer).
    """
    sql: str | None = None
    sparql: str | None = None
    for eq in ctx.executed_queries:
        if eq.kind == "sql":
            sql = eq.query
        elif eq.kind == "sparql":
            sparql = eq.query
    return sql, sparql


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


# Tools that exist in the registry but are offered to the planner only when the
# request opts in through ``options``. Withholding them by default keeps a request
# that does not ask for them on exactly the pre-existing tool set and prompt.
#   • ``read_document`` — opt in with ``options.readDocument = true``.
#   • ``find_documents`` — opt in with ``options.entityRouting = true``.
_OPT_IN_TOOL_NAMES: frozenset[str] = frozenset({READ_DOCUMENT_TOOL, FIND_DOCUMENTS_TOOL})

# ``options.ontologyLookupMode`` values. ``planner`` (the default, also used for an
# absent or unrecognized value) leaves ontology_lookup as a planner-selectable tool.
# ``preload`` resolves the ontology once at session start, concurrently with
# decomposition and OUTSIDE the step budget, then withholds the tool from the
# planner: the planner still sees the ontology's edge types in its context summary,
# but no reasoning step (nor the sufficiency check that follows a successful step)
# is spent on fetching it — once per parallel sub-question in the planner mode.
ONTOLOGY_MODE_PRELOAD = "preload"

# A guardrail privacy-mask placeholder left in the question (e.g. "{NAME}").
_MASK_PLACEHOLDER_RE = re.compile(r"\{[A-Z][A-Z_]*\}")

# options.prefetchMode (see ``_prefetch``). "assess" is the only value: gather the
# obvious evidence before the loop, check sufficiency once, and skip the loop when it
# is enough. The prefetch reads this many top source documents, adds at most this
# much full-document text, skips a window smaller than the minimum, and does not
# start the reads with less usable time (after the synthesis reserve) than this.
PREFETCH_ASSESS = "assess"
_PREFETCH_READ_DOCS = 2
_PREFETCH_TEXT_BUDGET = 60_000
_PREFETCH_MIN_WINDOW = 2_000
# Below this much time left, the prefetch does not start (a call could not finish).
_PREFETCH_MIN_TIME_S = 1.0


class _RequestTools:
    """A request-scoped view of the registry with some tools replaced by variants.

    The registry is process-wide; a per-request option that changes how a tool
    behaves and is described (``options.readDocumentAnchor``) swaps in a variant
    for this request only. ``get`` / ``specs`` / ``names`` mirror
    :class:`ToolRegistry`, which is all the controller uses.
    """

    def __init__(self, registry: Any, overrides: dict[str, Any]) -> None:
        """Wrap ``registry``, answering ``overrides`` names with their variant tools."""
        self._registry = registry
        self._overrides = overrides

    def get(self, name: str) -> Any:
        """The variant for ``name`` when overridden, else the registered tool."""
        return self._overrides.get(name) or self._registry.get(name)

    def names(self) -> list[str]:
        """Registered tool names (variants keep their tool's name)."""
        return self._registry.names()

    def specs(self, *, exclude: frozenset[str] | None = None) -> list[dict]:
        """The registry's specs with overridden tools described by their variant."""
        specs = self._registry.specs(exclude=exclude)
        for spec in specs:
            variant = self._overrides.get(spec["name"])
            if variant is not None:
                spec.update(
                    description=variant.description,
                    input_schema=variant.input_schema,
                    output_schema=variant.output_schema,
                )
        return specs


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

        opts = options or {}
        clock = BudgetClock(self._budget, clock=self._clock_source)
        clock.started()
        ctx = AccumulatedContext()
        trace.record("agentic_session_start", "success", 0, detail=query[:80])
        # options.tokenCaps.planner / options.plannerParseRetry ride on the context
        # (the planner is stateless); unset leaves the planner's own defaults.
        ctx.planner_max_tokens = token_cap(opts, "planner")
        ctx.planner_parse_retry = opts.get("plannerParseRetry") is True
        # options.sufficiencyGuards: hold back a positive sufficiency check while a top
        # name-matched document has been read only in part.
        ctx.sufficiency_guards = opts.get("sufficiencyGuards") is True
        # options.singleCallSteps: one model call per loop step (the next-step call
        # also decides when to stop) instead of a step call plus a sufficiency call.
        ctx.single_call_steps = opts.get("singleCallSteps") is True

        # Composition-gate the tools BOTH ways, so the planner is only ever offered
        # tools that can actually produce a result on this namespace:
        #   • no structured (DATABASE) source  → withhold ``nl_to_sql``
        #   • no unstructured (DOCUMENTS) source → withhold the chunk/topic vector
        #     retrievers (all ``strategy:*``, ``vector_search``, ``chunk_window``)
        # A MIXED namespace withholds nothing — either kind of tool might answer, so
        # the planner chooses. Fail-open: both flags default True, so an unknown
        # composition never hides a tool that might answer.
        composition_gated: frozenset[str] = frozenset()
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
        requested_gated = composition_gated | exclude_tools

        # Option-gated tools stay withheld unless the request's options turn them on
        # (the recommended defaults do, see ``defaults.py``). Not traced: withholding
        # them is not a gating decision. read_document has
        # nothing to read on a namespace with no DOCUMENTS source.
        option_gated = _OPT_IN_TOOL_NAMES
        tools: Any = self._registry
        # options.entityRouting: offer find_documents (names -> source documents),
        # extract the question's named terms during decomposition and route them
        # once up front.
        entity_routing = opts.get("entityRouting") is True and has_unstructured_source
        if entity_routing:
            option_gated -= {FIND_DOCUMENTS_TOOL}
        if opts.get("readDocument") is True and has_unstructured_source:
            option_gated -= {READ_DOCUMENT_TOOL}
            # options.readDocumentAnchor: offer the anchored reader (windows centred
            # on a passage the loop retrieved, or a phrase) instead of offset paging.
            if opts.get("readDocumentAnchor") is True:
                variant = getattr(self._registry.get(READ_DOCUMENT_TOOL), "anchored_variant", None)
                if callable(variant):
                    tools = _RequestTools(self._registry, {READ_DOCUMENT_TOOL: variant()})

        # options.ontologyLookupMode == "preload": resolve the ontology up front and
        # take the tool away from the planner. Only when the planner could have used
        # it (registered and not already withheld for this request).
        ontology_tool = None
        if opts.get("ontologyLookupMode") == ONTOLOGY_MODE_PRELOAD and ONTOLOGY_LOOKUP_TOOL not in requested_gated:
            ontology_tool = self._registry.get(ONTOLOGY_LOOKUP_TOOL)
            if ontology_tool is not None:
                option_gated |= {ONTOLOGY_LOOKUP_TOOL}
        excluded_tools = requested_gated | option_gated

        # Out-of-band per-request context handed to every Tool invocation, NOT routed
        # through the planner's tool inputs (``validate_inputs`` rejects keys outside a
        # tool's input_schema). Structured tools need the profile: SQLFirewall/Cedar
        # fail CLOSED on an empty profile, so without it every nl_to_sql / nl_to_sparql
        # call is an AccessDeniedError silently rendered as "0 rows".
        tool_context = {"profile": profile or {}, "options": opts, "model_id": model_id}
        if requested_gated:
            trace.record(
                "tools_gated",
                "success",
                clock.elapsed_ms(),
                detail=(
                    f"withholding {sorted(requested_gated)}"
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

        # Ontology preload runs concurrently with decomposition (both are needed
        # before the loops start: forked sub-contexts copy the resolved ontology).
        ontology_preload = (
            asyncio.ensure_future(self._preload_ontology(ontology_tool, namespace, clock, trace))
            if ontology_tool is not None
            else None
        )

        # 1. Decompose the question into >= 1 Sub_Questions (Req 13.1-13.3). The
        # decomposer is best-effort and never raises (it degrades to a single
        # Sub_Question), so no guard is needed here. The same call reports the
        # question's language, resolved once, so the planner and synthesizer are
        # told the language rather than each guessing it.
        named_terms: list[str] = []
        decompose_with_language = getattr(self._decomposer, "decompose_with_language", None)
        decompose_with_terms = getattr(self._decomposer, "decompose_with_terms", None)
        try:
            if entity_routing and callable(decompose_with_terms):
                sub_questions, detected, named_terms = await decompose_with_terms(query, trace)
            elif callable(decompose_with_language):
                sub_questions, detected = await decompose_with_language(query, trace)
            else:
                sub_questions, detected = await self._decomposer.decompose(query, trace), ""
        except BaseException:
            # A cancelled request must not leave the ontology preload running.
            if ontology_preload is not None:
                ontology_preload.cancel()
            raise
        ctx.answer_language = detected or (current_label_preference().preferred_tag or "")
        trace.record(
            "answer_language",
            "success" if ctx.answer_language else "skipped",
            clock.elapsed_ms(),
            detail=ctx.answer_language or "unresolved; prompts match the question's language",
        )
        if ontology_preload is not None:
            preloaded = await ontology_preload
            if preloaded is not None:
                ctx.ontology = preloaded
        if entity_routing:
            if opts.get("loopFixes") is True:
                # A privacy-mask placeholder ({NAME}, {EMAIL}, ...) is not a searchable name.
                named_terms = [t for t in named_terms if not _MASK_PLACEHOLDER_RE.search(t)]
            await self._route_named_terms(named_terms, ctx, clock, trace, namespace=namespace)

        # options.prefetchMode = "assess": gather the obvious evidence before the loop,
        # in parallel and outside the step budget — the baseline semantic search, the
        # SQL writer on the whole question, and reads of the top documents — so the
        # planner does not spend its first steps (two LLM calls each) finding them.
        # One sufficiency check follows; the loop is skipped when the evidence
        # already answers the question and runs as usual otherwise.
        skip_loop = False
        prefetched = False
        speculative: list[asyncio.Future] = []
        speculate: Callable[[], Awaitable[Tier3Result]] | None = None
        fast_path = opts.get("prefetchMode") == PREFETCH_ASSESS and opts.get("fastPathSpeculation") is True
        # options.fastPathSpeculation: write the answer while the "enough?" check runs,
        # and keep it only if the check passes (blocking responses only; a streamed
        # answer must not start before the decision).
        if fast_path and on_token is None:

            def speculate() -> Awaitable[Tier3Result]:
                return self._synthesize_and_build(
                    query,
                    ctx,
                    clock,
                    trace,
                    catalog_summary=catalog_summary,
                    conversation_history=conversation_history,
                    on_token=None,
                    synth_max_tokens=token_cap(opts, "synthesizer"),
                )

        # One try/finally from the prefetch to the end of the loop, so a prefetch SQL
        # call kept running (options.keepSlowSql) is never left behind, whatever
        # returns or raises in between.
        try:
            if opts.get("prefetchMode") == PREFETCH_ASSESS:
                await self._finish_final_semantic_search(final_search, ctx, trace, clock)
                final_search = None
                prefetched = True
                skip_loop = await self._prefetch(
                    query,
                    ctx,
                    clock,
                    trace,
                    namespace=namespace,
                    tools=tools,
                    sql_allowed=has_structured_source and NL_TO_SQL_TOOL not in excluded_tools,
                    read_allowed=has_unstructured_source and READ_DOCUMENT_TOOL not in excluded_tools,
                    tool_context=tool_context,
                    speculate=speculate,
                    speculative=speculative,
                    early_guard=fast_path,
                )
                if speculative:
                    trace.record(
                        "agentic_retrieval_stop",
                        "success",
                        clock.elapsed_ms(),
                        detail=str(StopReason.sufficiency_satisfied),
                    )
                    return await speculative[0]

            # 2. Run the initial (independent) Sub_Questions concurrently: each gets a
            # private forked sub-context (own seen_keys, own local branch queue) while the
            # shared BudgetClock enforces the aggregate step/fan-out caps across all of
            # them. Sub-contexts are merged back into ``ctx`` (re-deduped) in deterministic
            # sub-question order, so the gathered set matches sequential accumulation.
            last_stop = (
                StopReason.sufficiency_satisfied
                if skip_loop
                else await self._run_subquestions_parallel(
                    sub_questions,
                    ctx,
                    clock,
                    trace,
                    namespace=namespace,
                    excluded_tools=excluded_tools,
                    tool_context=tool_context,
                    tools=tools,
                )
            )
            # options.keepSlowSql: last chance for a late prefetch SQL; then stop waiting.
            await take_pending_sql(ctx, trace)
        finally:
            if ctx.pending_sql is not None:
                ctx.pending_sql.cancel()

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
        # retrieval. Recorded as a degraded source on failure; never raises. Already
        # done when the prefetch ran.
        if not prefetched:
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
            synth_max_tokens=token_cap(opts, "synthesizer"),
        )

    # ── entity routing (options.entityRouting) ─────────────────────────

    async def _route_named_terms(
        self,
        terms: list[str],
        ctx: AccumulatedContext,
        clock: BudgetClock,
        trace: TraceCollector,
        *,
        namespace: str,
    ) -> None:
        """Run find_documents once on the question's named terms, outside the step budget.

        The terms come from the decomposition call itself (no extra model round
        trip). The matches go onto the context (shared with every sub-question's
        fork, so each planner starts from them) and the mention chunks become
        evidence. Skipped — and traced — when there are no terms or no tool; a
        failure or timeout degrades to a planner that can still call the tool.
        """
        tool = self._registry.get(FIND_DOCUMENTS_TOOL)
        if tool is None or not terms:
            trace.record(
                "find_documents_preload",
                "skipped",
                clock.elapsed_ms(),
                detail="no find_documents tool" if tool is None else "no named terms in the question",
            )
            return
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                tool.invoke(namespace=namespace, terms=list(terms)),
                timeout=clock.tool_deadline_s(self._budget.per_tool_timeout_s),
            )
        except Exception as exc:  # noqa: BLE001 - routing must never raise (Req 9.1)
            ms = int((time.perf_counter() - start) * 1000)
            logger.warning(
                "agentic_find_documents_preload_failed",
                namespace=namespace,
                error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            trace.record(
                "find_documents_preload", "error", ms, detail=type(exc).__name__, tool_used=FIND_DOCUMENTS_TOOL
            )
            return
        ms = int((time.perf_counter() - start) * 1000)
        ctx.add(result)
        matches = [i for i in result.items if isinstance(i, dict) and i.get("type") == "document_match"]
        ctx.add_document_matches(matches)
        trace.record(
            "find_documents_preload",
            "success",
            ms,
            detail=f"terms={terms}; {result.detail}"[:1500],
            tool_used=FIND_DOCUMENTS_TOOL,
        )

    # ── evidence prefetch (options.prefetchMode) ───────────────────────

    async def _prefetch(
        self,
        query: str,
        ctx: AccumulatedContext,
        clock: BudgetClock,
        trace: TraceCollector,
        *,
        namespace: str,
        tools: Any,
        sql_allowed: bool,
        read_allowed: bool,
        tool_context: dict,
        speculate: Callable[[], Awaitable[Tier3Result]] | None = None,
        speculative: list[asyncio.Future] | None = None,
        early_guard: bool = False,
    ) -> bool:
        """Run the SQL writer on the whole question and read the top documents, concurrently.

        Both start from what is already on the context (the routed name matches and
        the baseline semantic search), outside the step budget. Then one sufficiency
        check: returns whether the loop should be skipped (the check's working answer
        is kept for synthesis either way). A failed SQL call or read just leaves
        less evidence; the one exception that propagates is an SQL
        :class:`AccessDeniedError` (as it does from the loop), after the document
        reads have been cancelled.

        ``options.fastPathSpeculation``: with ``speculate`` the answer is written
        while the check runs; on a passing check its task is appended to
        ``speculative`` (the caller returns it), otherwise it is cancelled. With
        ``early_guard`` a sufficiency guard that already holds the check back skips
        the check, and the loop starts at once.
        """

        async def _sql() -> None:
            tool = tools.get(NL_TO_SQL_TOOL) if sql_allowed else None
            if tool is None:
                return
            start = time.perf_counter()
            extra = {"accumulated_context": ctx, "sub_question": query} if getattr(tool, "wants_context", False) else {}
            timeout_s = clock.tool_deadline_s(self._budget.per_tool_timeout_s)
            call = tool.invoke(namespace=namespace, **tool_context, **extra, query=query)
            try:
                if (tool_context.get("options") or {}).get("keepSlowSql") is True:
                    # options.keepSlowSql: a call that outlives its timeout keeps running
                    # for the loop instead of being cancelled.
                    task = asyncio.ensure_future(call)
                    try:
                        done, _ = await asyncio.wait({task}, timeout=timeout_s)
                    except asyncio.CancelledError:
                        task.cancel()
                        raise
                    if not done:
                        ctx.pending_sql = PendingSql(task, query, start)
                        trace.record(
                            "prefetch_sql",
                            "pending",
                            int(timeout_s * 1000),
                            detail="still running; kept for the loop",
                            tool_used=NL_TO_SQL_TOOL,
                        )
                        return
                    result = task.result()
                else:
                    result = await asyncio.wait_for(call, timeout=timeout_s)
            except AccessDeniedError:
                raise
            except Exception as exc:  # noqa: BLE001 - prefetch must never raise (Req 9.1)
                ms = int((time.perf_counter() - start) * 1000)
                ctx.record_degraded(NL_TO_SQL_TOOL, type(exc).__name__)
                trace.record("prefetch_sql", "error", ms, detail=type(exc).__name__, tool_used=NL_TO_SQL_TOOL)
                return
            ms = int((time.perf_counter() - start) * 1000)
            new_count = ctx.add(result)
            ctx.record_attempt(
                NL_TO_SQL_TOOL, {"query": query}, new_count, "succeeded", "prefetch on the whole question"
            )
            trace.record("prefetch_sql", "success", ms, detail=result.detail, tool_used=NL_TO_SQL_TOOL)

        async def _read() -> None:
            if read_allowed:
                await self._read_top_documents(
                    ctx,
                    clock,
                    trace,
                    namespace=namespace,
                    query=query,
                    k=_PREFETCH_READ_DOCS,
                    text_budget=_PREFETCH_TEXT_BUDGET,
                    tools=tools,
                    tool_context=tool_context,
                )

        sql_task, read_task = asyncio.ensure_future(_sql()), asyncio.ensure_future(_read())
        try:
            await asyncio.gather(sql_task, read_task)
        except AccessDeniedError:
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)
            trace.record(
                "prefetch_sql", "error", clock.elapsed_ms(), detail="AccessDeniedError", tool_used=NL_TO_SQL_TOOL
            )
            raise
        early = insufficiency_reason(ctx) if early_guard else ""
        if early:
            trace.record("sufficiency_guard", "success", clock.elapsed_ms(), detail=early[:300])
            trace.record("prefetch_done", "success", clock.elapsed_ms(), detail="guard held; running the loop")
            return False
        spec_task = asyncio.ensure_future(speculate()) if speculate is not None else None
        try:
            sufficient, working_answer = await self._controller.assess(SubQuestion(text=query), ctx)
            if working_answer:
                ctx.set_working_answer(query, working_answer)
            reason = insufficiency_reason(ctx) if sufficient else ""
            if reason:
                sufficient = False
                trace.record("sufficiency_guard", "success", clock.elapsed_ms(), detail=reason[:300])
        except BaseException:
            if spec_task is not None:
                spec_task.cancel()
            raise
        trace.record(
            "prefetch_done",
            "success",
            clock.elapsed_ms(),
            detail="sufficient; loop skipped" if sufficient else "not sufficient; running the loop",
        )
        if spec_task is not None:
            if sufficient and speculative is not None:
                speculative.append(spec_task)
            else:
                spec_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await spec_task
        return sufficient

    # ── reading the best-supported documents (prefetch) ────────────────

    async def _read_top_documents(
        self,
        ctx: AccumulatedContext,
        clock: BudgetClock,
        trace: TraceCollector,
        *,
        namespace: str,
        query: str,
        k: int,
        text_budget: int,
        tools: Any,
        tool_context: dict,
    ) -> None:
        """Read the top-``k`` unread source documents into the context (prefetch).

        Runs after the baseline semantic search (and the routed name matches), so it
        ranks everything gathered so far with :func:`rank_source_documents`. The
        reads run concurrently, through this request's read_document tool (anchored
        on each document's own retrieved chunks when the request offers the
        anchored variant, else from the start), so they carry the same namespace
        scoping. Bounded twice: by the wall
        clock left before the synthesis reserve (skipped cleanly when there is
        none), and by ``text_budget`` characters of full-document text — windows
        already read count against it, and the remainder is allotted to the
        documents in rank order. Finally the full-document windows are moved to the
        FRONT of the context, in document-rank order, so the assess call and the
        synthesizer read them before the short chunks. Never raises.
        """
        tool = tools.get(READ_DOCUMENT_TOOL)
        if tool is None:
            trace.record(
                "prefetch_read_documents", "skipped", clock.elapsed_ms(), detail="read_document not registered"
            )
            return
        anchored = getattr(tool, "wants_context", False) is True

        ranked = rank_source_documents(ctx.chunks)
        targets = [d for d in ranked if not d.read][:k]
        if not targets:
            trace.record("prefetch_read_documents", "skipped", clock.elapsed_ms(), detail="no unread source documents")
            return
        usable_s = clock.remaining_s() - self._budget.synthesis_reserve_s
        if usable_s < _PREFETCH_MIN_TIME_S:
            trace.record(
                "prefetch_read_documents",
                "skipped",
                clock.elapsed_ms(),
                detail=f"no time left before the synthesis reserve ({usable_s:.1f}s usable)",
            )
            return

        already = sum(len(c.text or "") for c in ctx.chunks if (c.chunk_id or "").startswith(DOCUMENT_CHUNK_PREFIX))
        left = text_budget - already
        plan: list[tuple[Any, int]] = []
        for doc in targets:
            size = min(self._default_read_window(tool, tool_context), left)
            if size < _PREFETCH_MIN_WINDOW:
                break
            plan.append((doc, size))
            left -= size
        if not plan:
            trace.record(
                "prefetch_read_documents",
                "skipped",
                clock.elapsed_ms(),
                detail=f"full-document text budget spent ({already} of {text_budget} chars already read)",
            )
            return

        async def _read(doc: Any, size: int) -> ToolResult:
            inputs: dict[str, Any] = {"max_chars": size}
            if doc.source_key:
                inputs["source_key"] = doc.source_key
            else:
                inputs["doc_id"] = doc.doc_id
            extra = {"accumulated_context": ctx, "sub_question": query} if anchored else {"offset": 0}
            return await tool.invoke(namespace=namespace, **tool_context, **extra, **inputs)

        start = time.perf_counter()
        timeout_s = clock.tool_deadline_s(self._budget.per_tool_timeout_s)
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*(_read(doc, size) for doc, size in plan), return_exceptions=True),
                timeout=timeout_s,
            )
        except TimeoutError:
            ms = int((time.perf_counter() - start) * 1000)
            logger.warning("agentic_prefetch_read_timeout", documents=len(plan))
            trace.record("prefetch_read_documents", "timeout", ms, detail=f"{len(plan)} read(s) after {timeout_s:.1f}s")
            return
        ms = int((time.perf_counter() - start) * 1000)

        outcomes: list[str] = []
        read_keys: list[str] = []
        for (doc, _), result in zip(plan, results, strict=True):
            ref = doc.source_key or doc.doc_id
            if isinstance(result, BaseException) or not getattr(result, "chunks", ()):
                reason = type(result).__name__ if isinstance(result, BaseException) else result.detail
                outcomes.append(f"{ref[:100]}: not read ({str(reason)[:80]})")
                continue
            ctx.add(result)
            read_keys.extend(c.source_doc for c in result.chunks)
            outcomes.append(f"{ref[:100]}: {result.detail.split(': ', 1)[-1][:120]}")

        self._front_load_document_windows(ctx, [d.source_key for d in ranked] + read_keys)
        logger.info("agentic_prefetch_read", attempted=len(plan), read=len(read_keys))
        trace.record(
            "prefetch_read_documents",
            "success" if read_keys else "error",
            ms,
            detail="; ".join(outcomes)[:1500],
            tool_used=READ_DOCUMENT_TOOL,
        )

    @staticmethod
    def _default_read_window(tool: Any, tool_context: dict) -> int:
        """The window size the read tool would use by default for this request."""
        sizer = getattr(tool, "window_size", None)
        if callable(sizer):
            return int(sizer(None, tool_context.get("options")))
        return DEFAULT_MAX_CHARS

    @staticmethod
    def _front_load_document_windows(ctx: AccumulatedContext, key_order: list[str]) -> None:
        """Move full-document windows to the front of ``ctx.chunks``, in document-rank order.

        Stable within a document (window order preserved); every other chunk keeps
        its relative order after them. Only the order changes — nothing is dropped.
        """
        rank = {}
        for key in key_order:
            if key and key not in rank:
                rank[key] = len(rank)
        windows = [c for c in ctx.chunks if (c.chunk_id or "").startswith(DOCUMENT_CHUNK_PREFIX)]
        others = [c for c in ctx.chunks if not (c.chunk_id or "").startswith(DOCUMENT_CHUNK_PREFIX)]
        windows.sort(key=lambda c: rank.get(c.source_doc, len(rank)))
        ctx.chunks[:] = windows + others

    # ── ontology preload (options.ontologyLookupMode) ──────────────────

    async def _preload_ontology(
        self, tool: Any, namespace: str, clock: BudgetClock, trace: TraceCollector
    ) -> Ontology | None:
        """Resolve the ontology once, outside the step budget (``ontologyLookupMode=preload``).

        Invokes the ontology lookup tool directly under the deadline-aware per-tool
        timeout and returns the resolved :class:`Ontology` (possibly empty), or
        ``None`` on a timeout / failure, in which case the planner simply runs
        without one — the same as a namespace whose lookup failed. Never raises.
        """
        from .tools.ontology_tool import ontology_from_result

        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                tool.invoke(namespace=namespace),
                timeout=clock.tool_deadline_s(self._budget.per_tool_timeout_s),
            )
        except Exception as exc:  # noqa: BLE001 - preload must never raise (Req 9.1)
            ms = int((time.perf_counter() - start) * 1000)
            logger.warning("agentic_ontology_preload_failed", error_type=type(exc).__name__)
            trace.record("ontology_preload", "error", ms, detail=type(exc).__name__, tool_used=ONTOLOGY_LOOKUP_TOOL)
            return None
        ms = int((time.perf_counter() - start) * 1000)
        ontology = ontology_from_result(result)
        status = "success" if ontology is not None else "error"
        trace.record("ontology_preload", status, ms, detail=result.detail, tool_used=ONTOLOGY_LOOKUP_TOOL)
        return ontology

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
        tools: Any | None = None,
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
                    tools if tools is not None else self._registry,
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
        if tool is None:
            trace.record(
                "agentic_final_semantic_search",
                "skipped",
                clock.elapsed_ms(),
                detail="semantic retrieval task has no tool",
            )
            return
        try:
            result = await final_search.task
            ms = int((time.perf_counter() - final_search.start) * 1000)
            ctx.add(result)
            result_degraded_reason = getattr(result, "degraded_reason", "")
            if result_degraded_reason:
                ctx.record_degraded(tool.name, result_degraded_reason)
            trace.record(
                "agentic_final_semantic_search",
                "degraded" if result_degraded_reason else "succeeded",
                ms,
                detail=result.detail,
                tool_used=tool.name,
            )
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
        synth_max_tokens: int | None = None,
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

        # Bound the answer prompt's document context (synth_max_context_chars). Under
        # the budget nothing changes; over it the trim is traced and logged, and the
        # response's supporting content is what the answer was actually given.
        prompt_chunks, left_out = fit_chunks_to_budget(ctx.chunks, synth_max_context_chars())
        if left_out:
            detail = (
                f"kept {len(prompt_chunks)} of {len(ctx.chunks)} chunks within "
                f"{synth_max_context_chars():,} prompt characters; {left_out} left out"
            )
            trace.record("synthesis_context_capped", "capped", clock.elapsed_ms(), detail=detail)
            logger.warning(
                "agentic_synthesis_context_capped", kept=len(prompt_chunks), left_out=left_out, total=len(ctx.chunks)
            )

        supporting_content, graph_only_chunks, anonymous_chunks = self._supporting_content_with_drops(
            ctx, prompt_chunks
        )
        graph_context = self._graph_context(ctx)
        if graph_only_chunks or anonymous_chunks:
            detail = (
                f"{len(supporting_content)} document citation(s); "
                f"{graph_only_chunks} graph-only statement chunk(s) surfaced as graph context; "
                f"{anonymous_chunks} chunk(s) dropped with neither document nor graph provenance"
            )
            logger.info(
                "agentic_supporting_content_filtered",
                citations=len(supporting_content),
                graph_only_chunks=graph_only_chunks,
                anonymous_chunks=anonymous_chunks,
            )
            # Only the unattributable chunks are a degradation: a graph statement
            # legitimately has no document to cite and is represented in graphContext.
            trace.record(
                "agentic_supporting_content",
                "degraded" if anonymous_chunks else "succeeded",
                clock.elapsed_ms(),
                detail=detail,
            )
            if anonymous_chunks:
                degraded.append(
                    {
                        "source": "agentic_supporting_content",
                        "reason": "missing_document_provenance",
                        "detail": detail,
                    }
                )

        # Partial on time-budget expiry (Req 7.7-7.8), any degraded source / all
        # tools failing with context (Req 9.6), or no context gathered (Req 7.8).
        partial = clock.expired() or bool(degraded) or no_context

        # Query artifacts the loop executed (#1166): the LAST SQL and LAST SPARQL
        # the structured tools ran, mirroring Standard mode's single queryUsed /
        # sparqlGenerated. Empty when no structured tool ran (document/graph-only
        # answer), leaving both None. Threaded through every _build_result exit
        # that carries a synthesized answer; the no-source (Req 9.7) and
        # guardrail-blocked (Req 8.3) exits return an empty answer and so leave
        # the artifacts unset by design.
        sql_used, sparql_used = _last_executed_queries(ctx)

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
                    prompt_chunks,
                    catalog_summary=catalog_summary,
                    conversation_history=conversation_history,
                    on_token=on_token,
                    synth_max_tokens=synth_max_tokens,
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
                query_used=sql_used,
                sparql_generated=sparql_used,
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
                query_used=sql_used,
                sparql_generated=sparql_used,
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
                query_used=sql_used,
                sparql_generated=sparql_used,
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
            query_used=sql_used,
            sparql_generated=sparql_used,
        )

    async def _synthesize(
        self,
        query: str,
        ctx: AccumulatedContext,
        prompt_chunks: list[ChunkResult],
        *,
        catalog_summary: list[dict[str, Any]] | None,
        conversation_history: list[dict[str, str]] | None,
        on_token: OnTokenCallback | None,
        synth_max_tokens: int | None = None,
    ) -> SynthesisResult:
        """Run the single Synthesizer call over the accumulated context (Req 8.1).

        Streams when an ``on_token`` callback is supplied and the Synthesizer
        supports it, mirroring ``KnowledgeRetriever``; otherwise performs a
        single-shot synthesis. Returns the :class:`SynthesisResult` (or raises,
        which the caller turns into the Req 8.6 partial result).

        ``prompt_chunks`` is ``ctx.chunks`` fitted to the document-context budget.
        ``synth_max_tokens`` (``options.tokenCaps.synthesizer``) caps the answer's
        output tokens; it is passed only when set, so the default call is unchanged.
        """
        cap_kw: dict[str, Any] = {"max_tokens": synth_max_tokens} if synth_max_tokens else {}
        # idea-2 ablation: feed synthesis ONLY the grounded running best-guess,
        # withholding the gathered chunks/entities, to isolate the interim answer.
        chunks = [] if self._synthesize_working_answer_only else prompt_chunks
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
                **cap_kw,
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
            **cap_kw,
        )

    # ── context → result mappers (Req 8.2) ─────────────────────────────

    @staticmethod
    def _supporting_content(ctx: AccumulatedContext) -> tuple[dict, ...]:
        """Map document-backed chunks to ``supporting_content`` (empty when none).

        Graph traversal can gather statement snippets with no source-document
        provenance. They remain available to synthesis, while their entities and
        relationships surface through ``graph_context``; they must not masquerade
        as document citations. Every accumulated document chunk is returned with
        no ``MAX_PROMPT_CHUNKS`` slice (Req 8.2).
        """
        return AgenticRetriever._supporting_content_with_drops(ctx)[0]

    @staticmethod
    def _supporting_content_with_drops(
        ctx: AccumulatedContext, chunks: list[ChunkResult] | None = None
    ) -> tuple[tuple[dict, ...], int, int]:
        """Split accumulated chunks into citations, graph-only text, and anonymous text.

        The hand-rolled and lexical paths both count the chunks they filter for
        missing provenance, mark the step degraded and add a ``degradedSources``
        entry. This path filtered silently, so a chunk that should have carried a
        document id but did not simply vanished from the citation list.

        The two counts are kept apart because they mean different things. A
        graph-only statement chunk (``uri`` set, no ``source_doc``) is EXPECTED: the
        graph tool produces it, and it surfaces as ``graphContext`` evidence, so it
        is reported but not degraded. A chunk with neither a document id nor a graph
        uri is unattributable evidence that the answer may have used and the
        response cannot show — that one is a genuine degradation.

        ``chunks`` (default: every accumulated chunk) is what to split: the answer
        step passes the chunks fitted to its document-context budget, so the
        response cites only what the answer was given.

        Returns:
            ``(supporting_content, graph_only_count, anonymous_count)``.
        """
        supporting_content: list[dict] = []
        graph_only = 0
        anonymous = 0
        for chunk in ctx.chunks if chunks is None else chunks:
            if _has_document_provenance(chunk):
                supporting_content.append(_supporting_content_from_chunk(chunk))
            elif getattr(chunk, "uri", ""):
                graph_only += 1
            else:
                anonymous += 1
        return tuple(supporting_content), graph_only, anonymous

    @staticmethod
    def _graph_context(ctx: AccumulatedContext) -> tuple[dict, ...]:
        """Map the accumulated entities to ``graph_context`` (empty when none, Req 8.2)."""
        return tuple(
            {
                "uri": e.uri,
                "label": e.label,
                "type": e.type,
                "relationships": e.relationships,
                "properties": getattr(e, "properties", {}),
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
        query_used: str | None = None,
        sparql_generated: str | None = None,
    ) -> Tier3Result:
        """Assemble the frozen :class:`Tier3Result` for the session.

        Attaches all trace steps recorded up to this point, ordered by invocation
        sequence (Req 10.4), and carries the ``partial`` flag and ``degraded_sources``
        through unchanged (Req 1.4, 7.7, 9.5). ``query_used`` / ``sparql_generated``
        carry the structured query artifacts the loop executed (#1166); they
        default to ``None`` for paths that ran no structured tool.
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
            query_used=query_used,
            sparql_generated=sparql_generated,
        )
