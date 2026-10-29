# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Reasoning controller: drives one Sub_Question's Reasoning_Loop (Req 4, 5, 6).

:class:`ReasoningController` owns the per-Sub_Question / per-Exploration_Branch
loop that sits at the heart of the Agentic Retriever. One iteration of the loop is
a single Reasoning_Step (Req 4.1): it consumes a step from the shared
:class:`~coa_serve.tier3.agentic.budget.BudgetClock`, asks the planner
for the next Tool + inputs, validates those inputs against the Tool's declared
input contract, invokes the Tool under the per-Tool timeout, incorporates the
result into the shared accumulated context, opens Exploration_Branches when a
result surfaces multiple directions, and runs the boolean Sufficiency_Check
(Req 4.2-4.3, 6.1-6.3).

The controller does NOT own the session: the wall-clock Time_Budget, the
aggregate step cap, and the fan-out cap all live on the single
:class:`BudgetClock` the session shares across every Sub_Question and
Exploration_Branch, and the queue of Sub_Questions is owned by the session too.
The controller merely draws from those shared budgets and appends branches to the
shared queue, which is what makes the caps aggregate for the whole request
(Req 13.7-13.9). The terminal session stop reason and the final synthesis are the
session's job (tasks 13-14); :meth:`ReasoningController.run_one_loop` returns the
per-Sub_Question outcome so the session can derive the session-level stop.

Tool preference (Req 5.1-5.3, 5.6). The planner proposes one or more candidate
decisions; the controller enforces the hard preference ordering **itself**,
deterministically, regardless of the order the planner returned them: a
Retrieval_Strategy_Tool is preferred over a Query_Template, which is preferred over
an Ad_Hoc_Query (strategy wins ties — Req 5.6). An Ad_Hoc_Query is therefore only
ever selected when it is the sole candidate (Req 5.3); when one is used the
controller records that no strategy tool / template fit (Req 5.4) and handles an
ad-hoc execution failure distinctly (Req 5.7).

Ontology-constrained traversal (Req 3.4, 3.9). When a selected decision traverses
edge types and the ontology has already been resolved for the request, the
controller filters the requested edge types down to the ontology's edge-type set,
excluding any absent ones, before the Tool is invoked.

Import discipline (Requirement 12.3): this module imports only the dependency-free
sibling models / context and ``structlog``; it never imports ``graphrag_toolkit``
nor any tool implementation (the registry and Tools are passed in at call time and
used structurally), so importing it never pulls the toolkit into ``sys.modules``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from ...exceptions import AccessDeniedError
from .models import ExplorationBranch, PlannerDecision, StopReason, SubQuestion

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime/graphrag import
    from .budget import BudgetClock
    from .context import AccumulatedContext
    from .tools.base import Tool, ToolResult
    from .tools.registry import ToolRegistry

logger = structlog.get_logger(__name__)


# The hard preference ordering the controller enforces over the planner's
# candidates (Req 5.1-5.3, 5.6). Lower rank = more preferred. A strategy tool
# beats a Query_Template, which beats an Ad_Hoc_Query; an ad-hoc query is selected
# only when it is the sole candidate. The sort is stable, so ties within a rank
# preserve the planner's own ordering.
_RANK_STRATEGY = 0
_RANK_TEMPLATE = 1
_RANK_AD_HOC = 2

# Prefix identifying a Retrieval_Strategy_Tool name (``strategy:<value>``).
_STRATEGY_PREFIX = "strategy:"

# Instruction the real (Bedrock Converse) planner is given so it proposes
# preference-ordered candidates; the controller still enforces the ordering below
# deterministically, so a misbehaving planner cannot defeat the preference.
TOOL_PREFERENCE_INSTRUCTION = (
    "Prefer a Retrieval_Strategy_Tool (name 'strategy:<value>') or a prepared "
    "Query_Template over generating an ad-hoc graph query. A strategy tool wins "
    "ties with a template. Only generate an ad-hoc query when no strategy tool and "
    "no template can be fully populated for the intended retrieval."
)


@runtime_checkable
class StepPlanner(Protocol):
    """The reasoning planner the controller consults each Reasoning_Step.

    A separate Bedrock Converse concern from final synthesis (task 12 builds the
    concrete LLM planner); the controller depends only on this narrow Protocol so
    unit tests can drive the loop with a scripted, deterministic fake.

    ``propose_steps`` returns the candidate next-step decisions for the current
    accumulated context (Req 4.3); the controller selects among them by the hard
    preference ordering (Req 5). Returning an empty list signals that no Tool can
    be selected (Req 4.7). ``check_sufficiency`` returns the explicit boolean
    Sufficiency_Check decision after a step (Req 6.1-6.2).
    """

    async def propose_steps(
        self,
        *,
        sub_question: SubQuestion,
        context: AccumulatedContext,
        tool_specs: list[dict],
    ) -> list[PlannerDecision]:
        """Return candidate next-step decisions (empty = no Tool selectable)."""
        ...

    async def check_sufficiency(self, *, sub_question: SubQuestion, context: AccumulatedContext) -> bool:
        """Return the boolean Sufficiency_Check decision for the context so far."""
        ...


def _schema_base_type(type_hint: str) -> type | tuple[type, ...] | None:
    """Map an input-schema type-hint string to the Python type(s) to check.

    The Tool input schemas declare field types as short hint strings (e.g.
    ``"str"``, ``"int"``, ``"list[str]"``, ``"list"``). This maps the leading
    token to a concrete type for a lightweight conformance check; an unrecognized
    hint maps to ``None`` (no type check, only presence/known-field is enforced).
    """
    hint = type_hint.strip().lower()
    if hint.startswith("str"):
        return str
    if hint.startswith("bool"):
        return bool
    if hint.startswith("int"):
        # bool is a subclass of int; exclude it so an int field rejects a bool.
        return int
    if hint.startswith("float"):
        return (int, float)
    if hint.startswith("list") or hint.startswith("tuple"):
        return (list, tuple)
    if hint.startswith("dict"):
        return dict
    return None


def validate_inputs(inputs: dict[str, Any], input_schema: dict[str, Any]) -> list[str]:
    """Return the ways ``inputs`` fail ``input_schema``'s declared contract (Req 2.5).

    Conformance here means: every supplied key is a field the Tool declares, and
    every supplied value matches the declared field type. A Tool may declare more
    fields than a given call supplies (most fields are optional / mode-specific),
    so missing declared fields are NOT a violation — only unknown fields and type
    mismatches are. An empty ``input_schema`` (a Tool that accepts no planner
    inputs, e.g. the ontology tool) therefore rejects any supplied field.

    Args:
        inputs: The keyword inputs the planner prepared for the Tool.
        input_schema: The Tool's declared ``input_schema``.

    Returns:
        A list of human-readable violation strings; empty when ``inputs`` conform.
    """
    violations: list[str] = []
    for key, value in inputs.items():
        if key not in input_schema:
            violations.append(f"unknown field {key!r}")
            continue
        hint = input_schema[key]
        if not isinstance(hint, str):
            continue
        expected = _schema_base_type(hint)
        if expected is None:
            continue
        # bool is a subclass of int — guard the int case explicitly.
        if expected is int and isinstance(value, bool):
            violations.append(f"field {key!r} expected {hint!r}, got bool")
            continue
        if not isinstance(value, expected):
            violations.append(f"field {key!r} expected {hint!r}, got {type(value).__name__}")
    return violations


class ReasoningController:
    """Drives one Sub_Question's Reasoning_Loop (Req 4, 5, 6).

    A single controller instance is reused across every Sub_Question and
    Exploration_Branch of a session; it is stateless between loops (all mutable
    state lives on the shared :class:`AccumulatedContext` and :class:`BudgetClock`
    passed in), so it is safe to reuse.

    Args:
        planner: The :class:`StepPlanner` consulted for next-step selection and
            the Sufficiency_Check. Mocked with scripted decisions in unit tests.
        per_tool_timeout_s: Per-Tool invocation timeout in seconds (Req 9.3-9.4,
            default 30). A Tool whose ``invoke`` raises or does not return within
            this window is recorded as a degraded source and the loop continues
            (Req 9.1-9.2).
    """

    def __init__(
        self,
        planner: StepPlanner,
        *,
        per_tool_timeout_s: float = 30.0,
        max_no_progress_steps: int = 3,
        ontology_edge_mode: str = "soft_prior",
    ) -> None:
        """Bind the planner and per-session loop tuning (timeout, escalation, edge mode)."""
        self._planner = planner
        self._per_tool_timeout_s = per_tool_timeout_s
        # How the resolved ontology constrains an edge-typed traversal's edge_types
        # (Req 3.4/3.9). "soft_prior" (default): the ontology is a PREFERENCE —
        # ontology-known edge types are ordered first, but edge types the planner
        # proposes beyond the ontology are still traversed. The induced ontology is
        # a heavily-pruned subset of the graph's ~1.7k predicates, so a hard
        # allowlist strips the majority of real edges (and can starve the traversal
        # to zero, tripping the tool's MIN_EDGE_TYPES bound). "strict": the legacy
        # allowlist (ontology edges only), but guarded so it never removes the LAST
        # edge type (an over-broad traversal is self-limiting; an empty one is a bug).
        self._ontology_edge_mode = (
            ontology_edge_mode if ontology_edge_mode in ("soft_prior", "strict") else "soft_prior"
        )
        # Idea 4 (escalation): how many CONSECUTIVE no-progress steps (a step that
        # adds no new chunks/entities and does not resolve the ontology) to tolerate
        # before stopping the loop with ``no_new_information``. Instead of stopping
        # on the FIRST fruitless step, the loop keeps going so the planner — which
        # can see the attempt history — can escalate to a different tool, reframe the
        # sub-question, or switch modality (e.g. semantic-search-empty → anchor the
        # entity → traverse the graph). Still bounded by the aggregate step/time
        # budgets. ``1`` reproduces the old stop-on-first-no-progress behavior.
        self._max_no_progress_steps = max(1, max_no_progress_steps)

    async def run_one_loop(
        self,
        sub_question: SubQuestion,
        ctx: AccumulatedContext,
        registry: ToolRegistry,
        clock: BudgetClock,
        queue: deque,
        trace: Any,
        *,
        namespace: str,
        excluded_tools: frozenset[str] | None = None,
        tool_context: dict | None = None,
    ) -> StopReason | None:
        """Run the Reasoning_Loop for ``sub_question`` until it stops.

        Each iteration is one Reasoning_Step: consume a step from the shared clock
        (Req 4.5/6.4), ask the planner for candidate next steps and select the
        most-preferred (Req 5), validate its inputs against the Tool contract
        (Req 2.5), constrain any traversal edge types to the resolved ontology
        (Req 3.4/3.9), invoke the Tool under the per-Tool timeout (Req 9.1-9.2),
        incorporate the result before the next selection (Req 4.2), open
        Exploration_Branches under the fan-out cap (Req 13.4), then stop on no new
        information (Req 6.3) or a positive Sufficiency_Check (Req 6.1-6.2).

        Args:
            sub_question: The Sub_Question this loop resolves.
            ctx: The shared accumulated context (working memory) for the session.
            registry: The Tool registry the planner selects from.
            clock: The shared aggregate budget clock.
            queue: The session's FIFO queue of pending Sub_Questions; opened
                Exploration_Branches are appended here (Req 13.4).
            trace: The :class:`TraceCollector` (used structurally via ``record``).
            namespace: The originating request namespace; every Tool invocation in
                this loop is scoped to it (Req 1.5).
            excluded_tools: Tool names withheld from the planner for this request
                (source-composition gating); ``None`` exposes the full registry.
            tool_context: Out-of-band per-request context (e.g. profile, options,
                model_id) merged into every Tool invocation's inputs.

        Returns:
            The per-Sub_Question stop reason — ``max_steps_reached`` (Req 4.5/6.4),
            ``time_budget_expired`` (Req 7.4), ``no_new_information`` (Req 6.3), or
            ``sufficiency_satisfied`` (Req 6.1-6.2) — or ``None`` when the loop
            ended because no Tool could be selected (Req 4.7). The session
            aggregates these into the terminal stop reason.
        """
        trace.record("subquestion_start", "success", clock.elapsed_ms(), detail=sub_question.text[:80])

        # Consecutive no-progress steps tolerated before stopping (reset on progress).
        # A no-progress step that REPEATS an already-tried approach counts against the
        # streak (the planner is stuck); one that tries something NOVEL does not (that
        # is escalation, which we encourage). ``tried`` holds the attempted-approach
        # signatures; the aggregate step/time budgets bound it regardless.
        no_progress = 0
        tried: set[str] = set()

        while True:
            # Time_Budget bounds the loop in aggregate (Req 7.4): stop before a new
            # step once the wall clock is spent.
            if clock.expired():
                return StopReason.time_budget_expired

            # Aggregate max-step cap (Req 4.5, 6.4).
            if not clock.try_consume_step():
                return StopReason.max_steps_reached

            # Next-step selection over the accumulated context (Req 4.3).
            candidates = await self._planner.propose_steps(
                sub_question=sub_question, context=ctx, tool_specs=registry.specs(exclude=excluded_tools)
            )
            decision = self._select_decision(candidates)
            if decision is None:
                # No Tool can be selected — stop initiating steps (Req 4.7).
                trace.record("next_step_select", "success", clock.elapsed_ms(), detail="no_tool_selectable")
                return None

            tool = registry.get(decision.tool_name)
            if tool is None:
                # The planner named a Tool that is not registered; treat as
                # nothing-selectable for this step and continue.
                ctx.record_attempt(decision.tool_name, decision.inputs, 0, "unknown_tool", decision.reasoning)
                trace.record(
                    "next_step_select",
                    "error",
                    clock.elapsed_ms(),
                    detail=f"unknown_tool:{decision.tool_name}",
                )
                continue

            # Constrain traversal edge types to the resolved ontology (Req 3.4/3.9)
            # BEFORE validating/invoking, so an absent edge type is never traversed.
            decision = self._apply_ontology_edge_filter(decision, ctx, clock, trace)

            trace.record(
                "next_step_select",
                "success",
                clock.elapsed_ms(),
                detail=f"{decision.tool_name}: {decision.reasoning}"[:200],
                tool_used=decision.tool_name,
            )

            # An Ad_Hoc_Query was selected only because no strategy tool / template
            # fit (the preference sort guarantees this) — record that (Req 5.4).
            if decision.is_ad_hoc:
                trace.record(
                    "ad_hoc_query",
                    "success",
                    clock.elapsed_ms(),
                    detail=(
                        "ad-hoc query generated: no Retrieval_Strategy_Tool and no Query_Template was fully populatable"
                    ),
                    tool_used=decision.tool_name,
                )

            # Input-contract validation (Req 2.5): do NOT invoke a non-conforming
            # Tool; record the violation and continue the loop.
            violations = validate_inputs(decision.inputs, tool.input_schema)
            if violations:
                ctx.record_attempt(decision.tool_name, decision.inputs, 0, "invalid_input", decision.reasoning)
                trace.record(
                    decision.tool_name,
                    "error",
                    0,
                    detail=f"input_contract_violation: {'; '.join(violations)}",
                    tool_used=decision.tool_name,
                )
                continue

            # Invoke under the deadline-aware per-Tool timeout (Req 9.1-9.2): the
            # effective timeout is clamped to the wall-clock time left minus the
            # synthesis reserve, so a late tool cannot overrun the Time_Budget.
            result, status, ms = await self._invoke_with_timeout(
                tool, decision.inputs, namespace, clock, tool_context=tool_context
            )

            if status != "success":
                # Tool raised or timed out: record the degraded source and continue
                # without terminating retrieval (Req 9.1-9.2). An ad-hoc execution
                # failure additionally retains no partial results (Req 5.7) — which
                # holds naturally since a failed invoke yields no result to add.
                ctx.record_degraded(decision.tool_name, status)
                ctx.record_attempt(decision.tool_name, decision.inputs, 0, status, decision.reasoning)
                detail = f"{decision.tool_name} {status}"
                if decision.is_ad_hoc:
                    detail = f"ad-hoc query execution {status}: no results retained"
                trace.record(decision.tool_name, status, ms, detail=detail, tool_used=decision.tool_name)
                continue

            assert result is not None  # status == "success" guarantees a result

            # Incorporate the result BEFORE selecting the next step (Req 4.2).
            new_count = ctx.add(result)
            result_degraded_reason = result.degraded_reason
            if result_degraded_reason:
                ctx.record_degraded(decision.tool_name, result_degraded_reason)
            # If the result carried the determined ontology (an Ontology_Lookup_Tool
            # success), cache it on the shared context so the ontology-constrained
            # edge filter (Req 3.4/3.9) and the planner's context summary can use it
            # on later steps this request (Req 3.8). Resolving a not-yet-known
            # ontology is preparatory progress, not "no new information".
            ontology_resolved = self._maybe_cache_ontology(result, ctx, clock, trace)
            # Similarly, a graph ``edge_types`` discovery probe surfaces the real
            # predicates present on the anchored entities (idea 5 soft-prior). It
            # gathers no chunks/entities (new_count==0) but learning traversable
            # predicates the pruned ontology omits is preparatory PROGRESS, not
            # "no new information" — cache them for the planner's next step.
            edges_discovered = self._maybe_cache_present_edges(result, ctx, clock, trace)
            # Record the attempt so the planner can see what it tried and whether it
            # produced anything (idea 3/4 — informs escalation / avoids repeats).
            attempt_status = "degraded" if result_degraded_reason else "succeeded"
            ctx.record_attempt(decision.tool_name, decision.inputs, new_count, attempt_status, decision.reasoning)
            trace.record(
                decision.tool_name,
                attempt_status,
                ms,
                detail=result.detail,
                tool_used=decision.tool_name,
            )

            # Open Exploration_Branches when the result surfaces multiple directions,
            # subject to the aggregate fan-out cap (Req 13.4, 13.7-13.8).
            self._enqueue_branches(result, sub_question, queue, clock, trace)

            # Escalation instead of immediate stop: a no-progress step (no new info,
            # no ontology resolution) does not end the loop on its own. Count
            # consecutive no-progress steps and stop only once the tolerance is spent,
            # so the planner can escalate to a different tool / reframe / switch
            # modality in between. Bounded by the aggregate step/time budgets.
            if new_count == 0 and not ontology_resolved and not edges_discovered:
                # A NOVEL approach (tool+inputs not tried before this sub-question)
                # RESETS the streak first, so a diversifying planner earns fresh
                # tolerance while one stuck REPEATING the same approach exhausts it at
                # ``max_no_progress_steps``.
                signature = self._attempt_signature(decision)
                is_repeat = signature in tried
                tried.add(signature)
                if not is_repeat:
                    no_progress = 0  # novel escalation — earn a fresh streak
                no_progress += 1
                trace.record(
                    "no_progress_step",
                    "success",
                    clock.elapsed_ms(),
                    detail=(
                        f"no new information ({'REPEAT' if is_repeat else 'novel escalation'}; "
                        f"streak {no_progress}/{self._max_no_progress_steps}) — "
                        "planner should try a different tool, reframe, or switch modality"
                    ),
                    tool_used=decision.tool_name,
                )
                if no_progress >= self._max_no_progress_steps:
                    return StopReason.no_new_information
                continue

            # Progress was made this step — reset the no-progress streak.
            no_progress = 0

            # Boolean Sufficiency_Check + the agent's running grounded best-guess
            # answer (Req 6.1-6.2; idea 2). ``_assess`` returns both in one planner
            # call; the working answer (grounded ONLY in gathered context) is stored
            # so it carries across steps and is handed to the synthesizer.
            sufficient, working_answer = await self._assess(sub_question, ctx)
            if working_answer:
                ctx.set_working_answer(sub_question.text, working_answer)
            if sufficient:
                return StopReason.sufficiency_satisfied

    # ── selection & preference (Req 5) ─────────────────────────────────

    async def _assess(self, sub_question: SubQuestion, ctx: AccumulatedContext) -> tuple[bool, str]:
        """Run the Sufficiency_Check and obtain the agent's grounded best-guess (idea 2).

        Prefers the planner's richer ``assess`` method, which returns
        ``(sufficient, working_answer)`` in one call — the working answer is the
        decision agent's current best answer grounded ONLY in the gathered context.
        Falls back to the bool-only ``check_sufficiency`` (with an empty working
        answer) for planners that do not implement ``assess`` (e.g. scripted test
        fakes). Never raises: any planner error degrades to ``(False, "")`` so the
        loop continues under the budget/step caps.
        """
        planner = self._planner
        assess = getattr(planner, "assess", None)
        if callable(assess):
            try:
                sufficient, working_answer = await assess(sub_question=sub_question, context=ctx)
                return bool(sufficient), working_answer or ""
            except Exception as exc:  # noqa: BLE001 - controller must never raise (Req 9.1)
                logger.warning("planner_assess_error", error=str(exc), error_type=type(exc).__name__)
                return False, ""
        try:
            return bool(await planner.check_sufficiency(sub_question=sub_question, context=ctx)), ""
        except Exception as exc:  # noqa: BLE001 - controller must never raise (Req 9.1)
            logger.warning("planner_sufficiency_error", error=str(exc), error_type=type(exc).__name__)
            return False, ""

    def _select_decision(self, candidates: list[PlannerDecision]) -> PlannerDecision | None:
        """Select the most-preferred candidate, enforcing the Req 5 ordering.

        Returns ``None`` when the planner proposed nothing (no Tool selectable,
        Req 4.7). Otherwise sorts the candidates by the hard preference rank
        (strategy < template < ad-hoc) with a STABLE sort so ties preserve the
        planner's own ordering, and returns the first. Because the sort is the
        controller's own, the preference holds regardless of the order the planner
        returned the candidates (Req 5.1-5.3, 5.6).
        """
        if not candidates:
            return None
        return sorted(candidates, key=self._preference_rank)[0]

    @staticmethod
    def _attempt_signature(decision: PlannerDecision) -> str:
        """A stable signature of an approach, for escalation-aware repeat detection.

        Two steps count as "the same approach" when they invoke the same tool with
        the same inputs. Inputs are serialized deterministically (sorted keys) so a
        re-proposed identical step matches regardless of dict ordering. This is used
        only to decide whether a fruitless step is a REPEAT (counts against the
        no-progress streak) or a NOVEL escalation (does not).
        """
        import json

        try:
            inputs_key = json.dumps(decision.inputs, sort_keys=True, default=str)
        except (TypeError, ValueError):
            inputs_key = repr(sorted((str(k), str(v)) for k, v in (decision.inputs or {}).items()))
        return f"{decision.tool_name}|{inputs_key}"

    @staticmethod
    def _preference_rank(decision: PlannerDecision) -> int:
        """Rank a decision for the preference sort (lower = more preferred)."""
        if decision.is_ad_hoc:
            return _RANK_AD_HOC
        if decision.tool_name.startswith(_STRATEGY_PREFIX):
            return _RANK_STRATEGY
        return _RANK_TEMPLATE

    # ── ontology-constrained traversal (Req 3.4, 3.9) ──────────────────

    def _apply_ontology_edge_filter(
        self, decision: PlannerDecision, ctx: AccumulatedContext, clock: BudgetClock, trace: Any
    ) -> PlannerDecision:
        """Constrain a decision's traversal edge types by the resolved ontology (Req 3.4/3.9).

        When the ontology is resolved (cached on ``ctx``) and the decision carries a
        non-empty ``edge_types`` list, apply the configured mode:

        - ``soft_prior`` (default): keep ALL requested edge types but REORDER them so
          ontology-known ones come first (the ontology is a preference, not a gate).
          Nothing is dropped, so edge types beyond the ontology's pruned subset — the
          majority of the graph's real predicates — still traverse. Returns a new
          decision only when the ordering actually changes.
        - ``strict``: keep only ontology edge types (the legacy allowlist), EXCEPT
          never strip to empty — if no requested edge type is in the ontology, keep
          the request unchanged (an empty traversal is a bug; an over-broad one is
          self-limiting).

        Membership is tested on the shared normalized form (``normalize_edge_type``),
        matching the traversal's own ``r.value`` normalization, so casing/separator
        differences never cause a spurious miss. Unchanged when the ontology is
        unresolved or the decision has no ``edge_types``.
        """
        if ctx.ontology is None:
            return decision
        edge_types = decision.inputs.get("edge_types")
        if not isinstance(edge_types, list) or not edge_types:
            return decision

        # Normalize both sides identically to the traversal template's r.value
        # normalization so membership is byte-for-byte reliable (lazy import keeps
        # module load toolkit-free; templates.py is dependency-free).
        from .templates import normalize_edge_type

        allowed_norm = {normalize_edge_type(e) for e in ctx.ontology.edge_types}
        in_ontology = [e for e in edge_types if normalize_edge_type(e) in allowed_norm]
        off_ontology = [e for e in edge_types if normalize_edge_type(e) not in allowed_norm]

        if self._ontology_edge_mode == "strict":
            # Allowlist, guarded against starvation: if nothing matched, keep the
            # original request rather than emptying it (which would trip the tool's
            # MIN_EDGE_TYPES bound and return a non-degraded empty result).
            if not in_ontology:
                trace.record(
                    "ontology_edge_filter",
                    "success",
                    clock.elapsed_ms(),
                    detail=f"strict: no requested edge in ontology; kept request (anti-starvation): {off_ontology}",
                    tool_used=decision.tool_name,
                )
                return decision
            if in_ontology == edge_types:
                return decision
            trace.record(
                "ontology_edge_filter",
                "success",
                clock.elapsed_ms(),
                detail=f"strict: excluded edge types absent from ontology: {off_ontology}",
                tool_used=decision.tool_name,
            )
            return replace(decision, inputs={**decision.inputs, "edge_types": in_ontology})

        # soft_prior: reorder ontology-known edges first, keep the rest. Nothing dropped.
        reordered = in_ontology + off_ontology
        if reordered == edge_types:
            return decision
        trace.record(
            "ontology_edge_filter",
            "success",
            clock.elapsed_ms(),
            detail=(
                f"soft_prior: {len(in_ontology)} ontology-preferred edge type(s) ordered first; "
                f"{len(off_ontology)} off-ontology edge type(s) retained: {off_ontology}"
            ),
            tool_used=decision.tool_name,
        )
        return replace(decision, inputs={**decision.inputs, "edge_types": reordered})

    # ── ontology caching (Req 3.8) ─────────────────────────────────────

    def _maybe_cache_ontology(
        self, result: ToolResult, ctx: AccumulatedContext, clock: BudgetClock, trace: Any
    ) -> bool:
        """Cache a freshly-resolved ontology on the context (Req 3.8); return newly-set.

        When ``result`` carries the determined ontology (an Ontology_Lookup_Tool
        success) and the request's ontology has not yet been resolved, store it on
        the shared context so the ontology-constrained edge filter (Req 3.4/3.9)
        and the planner's context summary can use it for the rest of the request,
        and record an ``ontology_resolved`` trace step. Returns ``True`` only when
        this call set a not-yet-known ontology, so the controller can treat that as
        progress rather than ``no_new_information``. A non-ontology result, or a
        repeat lookup once the ontology is already cached, returns ``False``.
        """
        if ctx.ontology is not None:
            return False
        # Lazy import keeps module load free of any tool implementation (and of
        # graphrag_toolkit), matching this module's import discipline.
        from .tools.ontology_tool import ontology_from_result

        ontology = ontology_from_result(result)
        if ontology is None:
            return False
        ctx.ontology = ontology
        trace.record(
            "ontology_resolved",
            "success",
            clock.elapsed_ms(),
            detail=f"{len(ontology.node_types)} node types, {len(ontology.edge_types)} edge types",
        )
        return True

    # ── discovered-predicate caching (idea 5 soft-prior) ───────────────

    def _maybe_cache_present_edges(
        self, result: ToolResult, ctx: AccumulatedContext, clock: BudgetClock, trace: Any
    ) -> bool:
        """Cache edge-type predicates surfaced by a graph ``edge_types`` probe.

        When ``result`` carries ``edge_type`` items (from the graph tool's
        ``edge_types`` discovery mode), append the newly-seen raw predicate labels to
        ``ctx.present_edge_types`` (preserving frequency order, dropping dups) so the
        planner can propose them for an ``edge_typed`` traversal even when the pruned
        ontology omits them. Returns ``True`` when at least one NEW predicate was
        learned (so the controller treats the probe as progress). A result with no
        edge-type items, or only already-known predicates, returns ``False``.
        """
        edges = [
            item.get("edge", "")
            for item in getattr(result, "items", ()) or ()
            if isinstance(item, dict) and item.get("type") == "edge_type" and item.get("edge")
        ]
        if not edges:
            return False
        known = set(ctx.present_edge_types)
        fresh = [e for e in edges if e not in known]
        if not fresh:
            return False
        ctx.present_edge_types.extend(fresh)
        trace.record(
            "present_edges_discovered",
            "success",
            clock.elapsed_ms(),
            detail=f"{len(fresh)} new graph predicate(s) available for traversal: {fresh[:10]}",
        )
        return True

    # ── branching (Req 13.4) ───────────────────────────────────────────
    def _enqueue_branches(
        self,
        result: ToolResult,
        sub_question: SubQuestion,
        queue: deque,
        clock: BudgetClock,
        trace: Any,
    ) -> None:
        """Open an Exploration_Branch per surfaced direction, under the fan-out cap.

        Each ``produced_directions`` entry is a candidate direction the result
        surfaced (e.g. one discovered competitor). The controller opens a branch
        for each, appending the derived Sub_Question to the shared session queue,
        but only while the aggregate fan-out cap has budget — ``try_open_branch``
        returns ``False`` once the cap is reached, at which point no further
        branches are opened this step (Req 13.4, 13.7-13.8).
        """
        for direction in result.produced_directions:
            if not direction:
                continue
            if not clock.try_open_branch():
                # Fan-out cap reached — stop opening branches (Req 13.7-13.8).
                trace.record(
                    "exploration_branch",
                    "skipped",
                    clock.elapsed_ms(),
                    detail=f"fan-out cap reached; not opening branch {direction!r}",
                )
                break
            branch = ExplorationBranch(
                seed=direction,
                parent_question=sub_question.text,
                reason=f"surfaced from {sub_question.text[:60]!r}",
            )
            queue.append(branch.to_sub_question())
            trace.record("exploration_branch", "success", clock.elapsed_ms(), detail=direction[:120])

    # ── tool invocation (Req 9.1-9.2) ──────────────────────────────────

    async def _invoke_with_timeout(
        self,
        tool: Tool,
        inputs: dict[str, Any],
        namespace: str,
        clock: BudgetClock,
        *,
        tool_context: dict | None = None,
    ) -> tuple[ToolResult | None, str, int]:
        """Invoke ``tool`` under the deadline-aware per-Tool timeout, never raising (Req 9.1-9.2).

        The effective timeout is ``clock.tool_deadline_s()`` — the configured
        per-tool timeout clamped to the wall-clock time left minus the synthesis
        reserve — so a tool started late in the session cannot overrun the
        Time_Budget by a full ``per_tool_timeout_s`` nor eat the slice reserved for
        final synthesis. Returns ``(result, status, duration_ms)`` where ``status``
        is ``"success"`` with a :class:`ToolResult`, ``"timeout"`` when the
        invocation did not return within that window, or ``"error"`` when it raised.
        A non-success status yields a ``None`` result; the caller records the
        degraded source and continues the loop. Scoped to ``namespace`` (Req 1.5).
        """
        start = time.perf_counter()
        timeout_s = clock.tool_deadline_s(self._per_tool_timeout_s)
        try:
            result = await asyncio.wait_for(
                tool.invoke(namespace=namespace, **(tool_context or {}), **inputs),
                timeout=timeout_s,
            )
            ms = int((time.perf_counter() - start) * 1000)
            return result, "success", ms
        except TimeoutError:
            ms = int((time.perf_counter() - start) * 1000)
            logger.warning("tool_invocation_timeout", tool=tool.name, timeout_s=timeout_s)
            return None, "timeout", ms
        except AccessDeniedError:
            # The ONE exception to "the controller never raises" (Req 9.1): an
            # authorization denial is not a retrieval failure. Degrading a 403 into a
            # degraded-source "error" would let the session answer 200 with an empty
            # result, hiding a misconfigured grant behind a plausible "no data found".
            # Every other consumer of the same engine propagates it, so we do too.
            raise
        except Exception as exc:  # noqa: BLE001 - controller must never raise (Req 9.1)
            ms = int((time.perf_counter() - start) * 1000)
            logger.warning(
                "tool_invocation_error",
                tool=tool.name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None, "error", ms
