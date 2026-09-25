# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bedrock-backed reasoning planner for the Agentic Retriever (Req 4.3, 5, 6.1-6.2).

:class:`BedrockStepPlanner` is the concrete
:class:`~coa_serve.tier3.agentic.controller.StepPlanner` the
:class:`ReasoningController` consults each Reasoning_Step. It adapts the existing
Bedrock :class:`~coa_serve.clients.base.LLMClient` (a Bedrock
Converse concern that is SEPARATE from final synthesis but runs under the same
client + region and never bypasses the synthesis-time guardrail) into the narrow
planner Protocol:

- :meth:`propose_steps` asks the planner LLM for one or more candidate next-step
  decisions over the accumulated context and the registry's Tool specs (Req 4.3),
  framed so the model prefers a Retrieval_Strategy_Tool or a prepared
  Query_Template over an ad-hoc graph query (Req 5). The controller still enforces
  that preference deterministically, so a misbehaving planner cannot defeat it.
- :meth:`check_sufficiency` asks the planner LLM for the explicit boolean
  Sufficiency_Check decision after a step (Req 6.1-6.2).

Both decisions are requested as small, structured JSON objects so they are
deterministic to parse and trivial to mock in unit tests (a mocked
``LLMClient.converse`` returning scripted JSON drives the planner with no live
Bedrock call). The planner is best-effort and NEVER raises: a Converse failure,
a guardrail block, or an unparseable / empty response degrades to "no candidate
selectable" for :meth:`propose_steps` (the loop then stops, Req 4.7) and to
``False`` for :meth:`check_sufficiency` (the loop continues under the budget/step
caps).

Import discipline (Requirement 12.3): this module imports only the dependency-
free sibling models / controller instruction string and ``structlog``; the
planner :class:`LLMClient`, :class:`AccumulatedContext`, and :class:`SubQuestion`
are referenced structurally and typed under ``TYPE_CHECKING``, so importing this
module never pulls ``graphrag_toolkit`` (or the concrete Bedrock client) into
``sys.modules``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from .controller import TOOL_PREFERENCE_INSTRUCTION
from .models import PlannerDecision

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime/graphrag import
    from ...clients.base import LLMClient
    from .context import AccumulatedContext
    from .models import SubQuestion

logger = structlog.get_logger(__name__)


def _relevance_ranked_context() -> bool:
    """Whether to surface carried chunks by relevance instead of recency (idea 8).

    Opt-in via ``AGENTIC_RELEVANCE_RANKED_CONTEXT=1`` (default off, preserving the
    recency behavior). Read at call time so it is easy to toggle per run/test.
    """
    return os.getenv("AGENTIC_RELEVANCE_RANKED_CONTEXT", "") == "1"


# Hard cap on how much of the sub-question is sent to the planner LLM, matching
# the decomposition planner / synthesizer query budgets so an oversized question
# cannot blow the prompt.
_MAX_QUESTION_CHARS = 2000

# Cap on how many edge types are enumerated in the context summary so a large
# ontology cannot blow the prompt.
_MAX_EDGE_TYPES_IN_SUMMARY = 50

# Cap on how many gathered entities are listed (with their ids) in the context
# summary so the planner can anchor an edge_typed traversal without the prompt
# growing unbounded as context accumulates.
_MAX_ENTITIES_IN_SUMMARY = 20

# Caps on the carried reasoning state echoed back into the planner prompt (ideas
# 2/3/4): how many recent attempts to list, and how many chunk snippets (and their
# width) to surface so the planner reasons about CONTENT, not just counts.
_MAX_ATTEMPTS_IN_SUMMARY = 8
_MAX_CHUNK_SNIPPETS_IN_SUMMARY = 3
_MAX_CHUNK_SNIPPETS_IN_ASSESS = 12
_CHUNK_SNIPPET_WIDTH = 280
_ASSESS_SNIPPET_WIDTH = 500
# Structured rows surfaced to the planner/assessor. Rows gathered by the
# NL->SQL / metric tools were previously invisible to both prompts (only
# chunks/entities were rendered), so a tabular sub-question that had ALREADY
# been answered was assessed as "no data" — the loop then wandered into
# unrelated tools and the working answer denied having any data.
_MAX_ROWS_IN_SUMMARY = 3
_MAX_ROWS_IN_ASSESS = 12
_ROW_SNIPPET_WIDTH = 200

# System instruction for next-step selection. All static guidance lives here (not
# in the user message) to avoid tripping PROMPT_ATTACK guardrail filters, matching
# the Synthesizer / DecompositionPlanner discipline. The controller re-enforces
# the preference ordering deterministically (see TOOL_PREFERENCE_INSTRUCTION).
_PROPOSE_SYSTEM = (
    "You are the reasoning planner for a server-side knowledge-retrieval agent. "
    "Given a sub-question, the context gathered so far, and the catalog of "
    "internal retrieval tools, decide which tool to invoke NEXT and with what "
    "inputs to make progress toward answering the sub-question.\n\n"
    "RECOMMENDED APPROACH — your standing strategy for questions about entities "
    "and their relationships (most questions). Apply it even when the question "
    "does NOT explicitly ask for it; it is your standing strategy, not something "
    "the user must request. Skip a step only when the gathered-context summary "
    "shows it is already done or clearly does not apply:\n"
    "1. RESOLVE THE ONTOLOGY FIRST. If the context summary says the ontology is "
    "not yet resolved, select the ontology lookup tool before any other "
    "retrieval, so you learn this namespace's node types and edge types and can "
    "follow real relationships instead of guessing.\n"
    "2. ANCHOR THE ENTITIES named in the sub-question. Use an entity-oriented "
    "retrieval strategy (or a keyword graph traversal) to locate the specific "
    "entities the question is about and obtain their ids.\n"
    "3. FOLLOW RELATIONSHIPS FROM THOSE ENTITIES. Use an edge-typed graph "
    "traversal FROM the anchored entity ids OVER the edge types that lead toward "
    "the answer, to discover the connected nodes and their supporting text. Use the "
    "entity ids listed in the context summary as 'entity_ids' — do NOT invent ids. "
    "PREFER edge types the resolved ontology lists when one fits, but the ontology "
    "is a PRUNED subset of the graph's real relationships — if no ontology edge type "
    "fits the question, first run a graph traversal in 'edge_types' mode on the "
    "anchored entity ids to DISCOVER the predicates actually present (e.g. "
    "'HAS SEGMENT', 'GENERATES REVENUE IN'), then follow the most relevant discovered "
    "predicate with an 'edge_typed' traversal. Do not restrict yourself to the "
    "ontology edge types when the graph has a better-fitting predicate.\n\n"
    "Focus your tool choices on this ontology-guided GRAPH EXPLORATION, which only "
    "you can direct. A standard semantic search of the original question is "
    "ALWAYS performed automatically at the END of the session, so you do not need "
    "to spend a reasoning step on a plain semantic/chunk search of the whole "
    "question — prioritize anchoring entities and following their relationships.\n\n"
    "ESCALATE — DO NOT REPEAT. The context summary includes a 'Recent attempts' "
    "list of the tools you already tried and whether each added new information. If "
    "your recent attempts added NOTHING new, do something DIFFERENT this step — do "
    "not re-run an attempt that already returned nothing:\n"
    "- switch to a different retrieval strategy or tool;\n"
    "- reframe the sub-question (different wording, a narrower or broader phrasing, "
    "or a specific entity/period from the question);\n"
    "- switch modality: if semantic/chunk retrieval found nothing useful, anchor "
    "the relevant entity (entity-based retrieval or a keyword graph traversal) and "
    "then follow its ontology relationships with a graph traversal, or vice-versa.\n\n"
    f"{TOOL_PREFERENCE_INSTRUCTION}\n\n"
    "Each tool spec lists its name, a description, and an input_schema declaring "
    "the accepted input fields and their types. Populate inputs that conform to "
    "the chosen tool's input_schema.\n\n"
    "Respond with ONLY a JSON object of this exact shape, and no prose outside "
    "it:\n"
    '{"candidates": [{"tool_name": "<registered tool name>", '
    '"inputs": {<field>: <value>, ...}, "reasoning": "<short why>", '
    '"is_ad_hoc": <true|false>}]}\n'
    'Set "is_ad_hoc": true ONLY for a generated-from-scratch graph query chosen '
    "because no strategy tool and no template fit. Return an empty candidates "
    "list ONLY when no tool can make further progress."
)

# Optional chunk-window guidance, appended to the propose prompt ONLY when the
# chunk_window tool is actually registered (it is gated off by default via
# AGENTIC_ENABLE_CHUNK_WINDOW). Advertising it unconditionally caused the planner
# to select an unregistered tool (unknown_tool:chunk_window) and waste a step.
_CHUNK_WINDOW_INSTRUCTION = (
    "\n\nEXPAND AROUND A RELEVANT CHUNK. If a retrieved chunk is on-topic but seems "
    "to be missing an adjacent detail (a specific figure, period, or label that "
    "likely sits just before or after it in the document), use the chunk-window "
    "tool with that chunk's id (from the gathered-context summary) to pull its "
    "neighboring chunks."
)

# System instruction for the combined Sufficiency_Check + grounded best-guess
# answer (Req 6.1-6.2; improvement idea 2). The planner both decides whether the
# gathered context is enough AND produces its current best answer grounded ONLY in
# that context — never its own parametric/prior knowledge — so the running guess
# handed to the synthesizer cannot smuggle in ungrounded facts.
_ASSESS_SYSTEM = (
    "You are the reasoning planner for a server-side knowledge-retrieval agent. "
    "Given a sub-question and the context gathered so far (document-chunk snippets, "
    "graph entities, and prior findings), do BOTH of the following:\n"
    "1. Decide whether the accumulated context is SUFFICIENT to answer the "
    "sub-question accurately.\n"
    "2. Write your CURRENT BEST ANSWER to the sub-question.\n\n"
    "GROUNDING — this is critical. Base your best answer ONLY on the provided "
    "context (the chunk snippets, entities, and findings shown to you). Do NOT use "
    "any outside or prior knowledge, and do NOT invent figures, names, dates, or "
    "facts that are not present in the context. Prefer specific figures and "
    "quotations that appear in the context. If the context is insufficient or only "
    "partially answers the question, say what IS supported and state plainly what "
    "is still missing — do not fill gaps from memory. Answer the WHOLE question as "
    "asked (e.g. every period/segment requested), not just one sub-part.\n\n"
    "LANGUAGE: Write your working_answer in the SAME language as the sub-question. The "
    "gathered context may be in other languages; ignore their language and match the "
    "sub-question's language (this answer is handed to the final synthesizer, so a "
    "consistent language prevents it from drifting to the wrong one — work item #901).\n\n"
    "Respond with ONLY a JSON object of this exact shape, and no prose outside it:\n"
    '{"sufficient": <true|false>, "working_answer": "<your grounded best answer>"}'
)


class BedrockStepPlanner:
    """Concrete :class:`StepPlanner` over a Bedrock :class:`LLMClient` (Req 4.3, 5, 6).

    A single instance is built once at startup (task 18 wiring) and reused across
    requests; it is stateless. Unit tests inject a mocked :class:`LLMClient` whose
    ``converse`` returns scripted JSON, so both planner decisions are fully
    deterministic without a live Bedrock call.

    Args:
        planner_client: The Bedrock :class:`LLMClient` used for the planner
            Converse calls (the same client class used for synthesis; a distinct
            *concern*, not necessarily a distinct instance).
        guardrail_id: Optional guardrail id applied to the user-supplied
            sub-question via ``guard_content`` so the planner never bypasses the
            guardrail used at synthesis time. ``None``/empty disables guard tagging.
        max_tokens: Upper bound on planner output tokens (structured JSON is small).
        max_question_chars: Upper bound on the sub-question text sent to the planner.
    """

    def __init__(
        self,
        planner_client: LLMClient,
        *,
        guardrail_id: str | None = None,
        guardrail_id_provider: Callable[[], str] | None = None,
        max_tokens: int = 1024,
        max_question_chars: int = _MAX_QUESTION_CHARS,
    ) -> None:
        """Bind the planner LLM client and its guardrail / token / length bounds."""
        self._planner = planner_client
        self._guardrail_id = guardrail_id or None
        self._guardrail_id_provider = guardrail_id_provider
        self._max_tokens = max_tokens
        self._max_question_chars = max_question_chars

    def _effective_guardrail_id(self) -> str | None:
        """Resolve the guardrail id LIVE via the provider when injected.

        Falls back to the construction-time ``guardrail_id`` (already normalized
        to ``None`` when empty) otherwise. Returns ``None`` for an empty provider
        value to preserve the disabled-guardrail semantics of the call sites.
        """
        if self._guardrail_id_provider is not None:
            gid = self._guardrail_id_provider()
            return gid or None
        return self._guardrail_id

    # ── next-step selection (Req 4.3, 5) ───────────────────────────────

    async def propose_steps(
        self,
        *,
        sub_question: SubQuestion,
        context: AccumulatedContext,
        tool_specs: list[dict],
    ) -> list[PlannerDecision]:
        """Return candidate next-step decisions for the current context (Req 4.3).

        Consults the planner LLM with the sub-question, a compact summary of the
        accumulated context, and the registry's Tool specs. Returns the parsed
        :class:`PlannerDecision` candidates (the controller selects among them by
        the hard preference ordering, Req 5). Returns an empty list — signalling
        no Tool can be selected (Req 4.7) — when there are no tools, the Converse
        call fails, the guardrail blocks, or the response is unparseable.
        """
        if not tool_specs:
            return []

        question = sub_question.text[: self._max_question_chars]
        prompt = self._build_propose_prompt(question, context, tool_specs)
        # Only advertise the chunk-window tool when it is actually registered
        # (gated off by default), so the planner never selects an unregistered tool.
        system = _PROPOSE_SYSTEM
        if any((spec.get("name") if isinstance(spec, dict) else "") == "chunk_window" for spec in tool_specs):
            system += _CHUNK_WINDOW_INSTRUCTION
        try:
            result = await self._planner.converse(
                prompt=prompt,
                system=system,
                guardrail_id=self._effective_guardrail_id(),
                guard_content=f"Question: {question}",
                temperature=0.0,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - planner must never raise (Req 4.7)
            logger.warning("planner_propose_error", error=str(exc), error_type=type(exc).__name__)
            return []

        if getattr(result, "guardrail_blocked", False):
            logger.warning("planner_propose_guardrail_blocked")
            return []

        return self._parse_candidates(result.text)

    # ── sufficiency + grounded best-guess (Req 6.1-6.2; idea 2) ─────────

    async def assess(self, *, sub_question: SubQuestion, context: AccumulatedContext) -> tuple[bool, str]:
        """Return ``(sufficient, working_answer)`` for the gathered context (idea 2).

        One Converse call that both runs the boolean Sufficiency_Check and produces
        the agent's current best answer grounded ONLY in the gathered context (no
        parametric/prior knowledge). Degrades to ``(False, "")`` — keep retrieving,
        no guess — on any Converse failure, guardrail block, or unparseable
        response, so the loop continues under the budget/step caps.
        """
        question = sub_question.text[: self._max_question_chars]
        prompt = self._build_assess_prompt(question, context)
        try:
            result = await self._planner.converse(
                prompt=prompt,
                system=_ASSESS_SYSTEM,
                guardrail_id=self._effective_guardrail_id(),
                guard_content=f"Question: {question}",
                temperature=0.0,
                max_tokens=self._max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - planner must never raise.
            logger.warning("planner_assess_error", error=str(exc), error_type=type(exc).__name__)
            return False, ""

        if getattr(result, "guardrail_blocked", False):
            logger.warning("planner_assess_guardrail_blocked")
            return False, ""

        return self._parse_assessment(result.text)

    async def check_sufficiency(self, *, sub_question: SubQuestion, context: AccumulatedContext) -> bool:
        """Return only the boolean Sufficiency_Check decision (Req 6.1-6.2).

        Retained for the :class:`StepPlanner` Protocol; delegates to :meth:`assess`
        and discards the working answer. The controller prefers :meth:`assess`
        directly so the grounded best-guess is captured.
        """
        sufficient, _ = await self.assess(sub_question=sub_question, context=context)
        return sufficient

    # ── prompt construction ─────────────────────────────────────────────

    def _build_propose_prompt(self, question: str, context: AccumulatedContext, tool_specs: list[dict]) -> str:
        """Build the next-step-selection user prompt."""
        specs_json = json.dumps(tool_specs, default=str)
        return (
            f"Sub-question: {question}\n\n"
            f"{self._context_summary(context)}\n\n"
            f"Available tools:\n{specs_json}\n\n"
            "Select the next tool(s) to invoke."
        )

    def _build_assess_prompt(self, question: str, context: AccumulatedContext) -> str:
        """Build the combined sufficiency + grounded-best-answer prompt (idea 2).

        Includes a content-bearing view of the gathered context — actual chunk
        snippets (not just counts), the anchored entities, and any prior grounded
        findings — so both the sufficiency decision and the best answer are
        grounded in what was actually retrieved.
        """
        return (
            f"Sub-question: {question}\n\n"
            f"{self._content_view(context)}\n\n"
            "Decide whether this context is sufficient, and write your current best "
            "answer grounded ONLY in the context above."
        )

    @staticmethod
    def _content_view(context: AccumulatedContext) -> str:
        """Content-bearing context view for the assess call (chunk text + entities + findings)."""
        parts: list[str] = []
        snippets: list[str] = []
        use_rel = _relevance_ranked_context()
        getter = getattr(context, "relevant_chunk_snippets", None) if use_rel else None
        if getter is None:
            getter = getattr(context, "recent_chunk_snippets", None)
        if callable(getter):
            snippets = getter(limit=_MAX_CHUNK_SNIPPETS_IN_ASSESS, width=_ASSESS_SNIPPET_WIDTH)
        if snippets:
            joined = "\n".join(f"  [chunk] {s}" for s in snippets)
            parts.append(f"Document chunk snippets gathered ({len(snippets)} shown):\n{joined}")
        else:
            parts.append("No document chunks gathered yet.")

        rows = getattr(context, "rows", []) or []
        row_getter = getattr(context, "recent_row_snippets", None)
        if rows and callable(row_getter):
            row_snips = row_getter(limit=_MAX_ROWS_IN_ASSESS, width=_ROW_SNIPPET_WIDTH)
            if row_snips:
                joined = "\n".join(f"  [row] {r}" for r in row_snips)
                parts.append(
                    f"Structured data rows gathered ({len(rows)} total, {len(row_snips)} shown) — "
                    f"these ARE retrieved data and may fully answer the sub-question:\n{joined}"
                )

        entities = getattr(context, "entities", []) or []
        if entities:
            labels = []
            for entity in entities[:_MAX_ENTITIES_IN_SUMMARY]:
                label = getattr(entity, "label", "") or getattr(entity, "uri", "") or ""
                if label:
                    labels.append(label)
            if labels:
                parts.append("Graph entities gathered: " + "; ".join(labels))

        prior = getattr(context, "working_answers", []) or []
        for entry in prior:
            ans = entry.get("answer") if isinstance(entry, dict) else None
            if ans:
                parts.append(f"Prior grounded finding: {ans}")
        return "\n\n".join(parts)

    @staticmethod
    def _context_summary(context: AccumulatedContext) -> str:
        """Summarize the accumulated context for the planner prompt.

        Reports the count of gathered chunks/entities and, when the ontology has
        been resolved this request, its edge-type set (capped) so the planner can
        choose meaningful traversals. Also lists the gathered entities with their
        ids so the planner can anchor an ``edge_typed`` graph traversal on them
        (the traversal tool takes ``entity_ids``; without surfacing the ids here
        the planner has no way to populate them). Read structurally so no toolkit
        import is needed.
        """
        entities = getattr(context, "entities", []) or []
        n_chunks = len(getattr(context, "chunks", []) or [])
        n_entities = len(entities)
        ontology = getattr(context, "ontology", None)
        if ontology is None:
            onto_line = "Ontology not yet resolved for this namespace."
        else:
            edge_types = list(getattr(ontology, "edge_types", ()) or ())
            shown = ", ".join(edge_types[:_MAX_EDGE_TYPES_IN_SUMMARY]) or "(none)"
            onto_line = f"Ontology resolved. Edge types available for traversal: {shown}"
        rows = getattr(context, "rows", []) or []
        summary = (
            f"Context gathered so far: {n_chunks} document chunk(s), {n_entities} graph "
            f"entit(y/ies), {len(rows)} structured data row(s). {onto_line}"
        )
        if rows:
            row_getter = getattr(context, "recent_row_snippets", None)
            row_snips = row_getter(limit=_MAX_ROWS_IN_SUMMARY, width=_ROW_SNIPPET_WIDTH) if callable(row_getter) else []
            if row_snips:
                joined = "\n".join(f"  [row] {r}" for r in row_snips)
                summary += (
                    f"\nStructured rows already retrieved (sample of {len(rows)}) — if these answer "
                    f"the sub-question, no further tools are needed:\n{joined}"
                )
        # Predicates discovered present on anchored entities via an 'edge_types'
        # probe (idea 5 soft-prior). These are REAL graph relationships the pruned
        # ontology may omit — the planner may traverse them with 'edge_typed'.
        present_edges = list(getattr(context, "present_edge_types", ()) or ())
        if present_edges:
            shown = ", ".join(present_edges[:_MAX_EDGE_TYPES_IN_SUMMARY]) or "(none)"
            summary += (
                "\nEdge types discovered present on the anchored entities (usable for an "
                f"'edge_typed' traversal even if absent from the ontology): {shown}"
            )
        if entities:
            listed = []
            for entity in entities[:_MAX_ENTITIES_IN_SUMMARY]:
                uri = getattr(entity, "uri", "") or ""
                if not uri:
                    continue
                label = getattr(entity, "label", "") or ""
                etype = getattr(entity, "type", "") or ""
                listed.append(f"{uri} ({label}; {etype})")
            if listed:
                summary += (
                    "\nGraph entities anchored so far — pass their ids as "
                    "'entity_ids' to an edge_typed graph traversal: " + "; ".join(listed)
                )

        # Recent attempts (idea 3/4): so the planner can SEE what it already tried
        # and whether it produced anything, and deliberately escalate / not repeat.
        attempts = getattr(context, "attempt_history", []) or []
        if attempts:
            recent = attempts[-_MAX_ATTEMPTS_IN_SUMMARY:]
            lines = []
            for a in recent:
                tool = a.get("tool", "?")
                status = a.get("status", "?")
                new_count = a.get("new_count", 0)
                inputs = a.get("inputs", {})
                outcome = (
                    f"+{new_count} new"
                    if status == "succeeded" and new_count
                    else "no new info"
                    if status == "succeeded"
                    else status
                )
                # Carry the planner's own rationale for the step (E-cheap) so the
                # next step builds on prior intent rather than re-deriving it.
                why = a.get("reasoning", "")
                why_suffix = f"  [why: {why}]" if why else ""
                lines.append(f"  - {tool} {inputs} -> {outcome}{why_suffix}")
            summary += "\nRecent attempts (most recent last):\n" + "\n".join(lines)

        # Snippets of the gathered chunks so tool selection can reason about CONTENT,
        # not just counts — and their ids so the planner can expand around a relevant
        # chunk via the chunk-window tool (idea 6). Ranked by relevance rather than
        # recency when idea 8 is enabled.
        use_rel = _relevance_ranked_context()
        getter = getattr(context, "relevant_chunk_refs", None) if use_rel else None
        if getter is None:
            getter = getattr(context, "recent_chunk_refs", None)
        if callable(getter):
            refs = getter(limit=_MAX_CHUNK_SNIPPETS_IN_SUMMARY, width=_CHUNK_SNIPPET_WIDTH)
            if refs:
                joined = "\n".join(f"  [chunk id={cid}] {snippet}" for cid, snippet in refs)
                summary += (
                    "\nMost relevant chunks so far (pass an id as 'chunk_ids' to the chunk-window "
                    f"tool to pull the chunks before/after it in the document):\n{joined}"
                )
        else:
            # Fallback: text-only snippets when chunk refs are unavailable.
            snip_getter = getattr(context, "recent_chunk_snippets", None)
            if callable(snip_getter):
                snippets = snip_getter(limit=_MAX_CHUNK_SNIPPETS_IN_SUMMARY, width=_CHUNK_SNIPPET_WIDTH)
                if snippets:
                    joined = "\n".join(f"  [chunk] {s}" for s in snippets)
                    summary += f"\nMost recent chunk snippets:\n{joined}"

        # The agent's running grounded best-guess so far (idea 2) — so a later step
        # builds on it rather than restarting.
        working = getattr(context, "working_answer", "") or ""
        if working:
            summary += f"\nCurrent grounded best-guess answer so far: {working[:600]}"

        return summary

    # ── parsing ──────────────────────────────────────────────────────────

    def _parse_candidates(self, text: str) -> list[PlannerDecision]:
        """Parse the planner's JSON decision into candidate :class:`PlannerDecision`s.

        Tolerates markdown fences / surrounding prose by extracting the first
        ``{ … }`` JSON object. Returns an empty list when no usable
        ``candidates`` list can be recovered or when every entry is unusable.
        """
        payload = self._extract_json_object(text)
        if payload is None:
            logger.warning("planner_propose_unparseable")
            return []
        raw = payload.get("candidates")
        if not isinstance(raw, list):
            return []

        decisions: list[PlannerDecision] = []
        for item in raw:
            decision = self._coerce_decision(item)
            if decision is not None:
                decisions.append(decision)
        return decisions

    @staticmethod
    def _coerce_decision(item: Any) -> PlannerDecision | None:
        """Coerce one planner candidate entry into a :class:`PlannerDecision`."""
        if not isinstance(item, dict):
            return None
        tool_name = item.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            return None
        inputs = item.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        reasoning = item.get("reasoning")
        reasoning = reasoning if isinstance(reasoning, str) else ""
        is_ad_hoc = bool(item.get("is_ad_hoc", False))
        return PlannerDecision(
            tool_name=tool_name.strip(),
            inputs=inputs,
            reasoning=reasoning,
            is_ad_hoc=is_ad_hoc,
        )

    def _parse_assessment(self, text: str) -> tuple[bool, str]:
        """Parse ``{"sufficient": <bool>, "working_answer": "..."}``; default ``(False, "")``.

        Tolerates fences / surrounding prose. The working answer is optional — a
        missing or non-string value yields ``""`` (no guess), while sufficiency
        defaults to ``False`` so the loop keeps retrieving on an unparseable
        response.
        """
        payload = self._extract_json_object(text)
        if payload is None:
            return False, ""
        sufficient = payload.get("sufficient") is True
        working = payload.get("working_answer")
        working = working.strip() if isinstance(working, str) else ""
        return sufficient, working

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        """Extract and parse the first JSON object from ``text``.

        Handles a bare JSON object, one wrapped in fences, or one embedded in
        surrounding prose by slicing from the first ``{`` to the last ``}``.
        Returns ``None`` on any parse failure.
        """
        if not text:
            return None
        candidate = text.strip()
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            pass
        first = candidate.find("{")
        last = candidate.rfind("}")
        if first == -1 or last == -1 or last <= first:
            return None
        try:
            parsed = json.loads(candidate[first : last + 1])
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
