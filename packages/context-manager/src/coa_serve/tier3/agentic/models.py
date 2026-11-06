# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared, dependency-free data models for the Agentic Retriever.

These value objects are passed between the decomposition planner, the reasoning
controller, and the retriever session. They are intentionally free of any
``graphrag_toolkit`` (or other heavy) import so that importing this module — and
therefore the ``agentic`` subpackage — never pulls the toolkit into
``sys.modules`` (Requirement 12.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class StopReason(StrEnum):
    """Enumerated reason the Reasoning_Loop stopped retrieving (Req 6.6, 7).

    Recorded once per session in the terminal ``agentic_retrieval_stop`` trace
    step. ``queue_drained`` and ``time_budget_expired`` are session-level
    outcomes; the remaining values are per-sub-question loop outcomes that
    bubble up to the session stop reason.
    """

    sufficiency_satisfied = "sufficiency_satisfied"
    no_new_information = "no_new_information"
    max_steps_reached = "max_steps_reached"
    sufficiency_check_timeout = "sufficiency_check_timeout"
    time_budget_expired = "time_budget_expired"
    queue_drained = "queue_drained"
    # The working answer stopped changing (single-call answer-stability signal).
    answer_stable = "answer_stable"


@dataclass(frozen=True)
class SubQuestion:
    """A self-contained question the agent resolves independently (Req 13.2).

    Produced either by Query_Decomposition of the input question or by branching
    from a Reasoning_Step result. Findings from every Sub_Question contribute to
    the single shared accumulated context for the request.

    Attributes:
        text: The natural-language question to resolve.
        multi_entity: True when the question still refers to multiple/unresolved
            entities (e.g. "competitors") and may surface Exploration_Branches
            once those entities are anchored.
        origin: Where the Sub_Question came from — ``"decomposition"`` for an
            initial decomposition product, ``"branch"`` for one derived from an
            Exploration_Branch.
    """

    text: str
    multi_entity: bool = False
    origin: str = "decomposition"


@dataclass(frozen=True)
class ExplorationBranch:
    """A distinct direction of investigation surfaced during retrieval (Req 13.4).

    Originates from a Sub_Question or from a Reasoning_Step result that surfaces
    multiple directions worth exploring (e.g. one branch per discovered
    competitor). Opening a branch is subject to the aggregate fan-out cap.

    Attributes:
        seed: The seed question / direction text this branch will pursue.
        parent_question: The Sub_Question text this branch was spawned from, if
            any (for tracing the exploration tree).
        reason: Human-readable detail recorded in the ``exploration_branch``
            trace step describing why this direction was opened.
    """

    seed: str
    parent_question: str | None = None
    reason: str = ""

    def to_sub_question(self) -> SubQuestion:
        """Convert this branch into a Sub_Question for the processing queue."""
        return SubQuestion(text=self.seed, origin="branch")


@dataclass(frozen=True)
class PlannerDecision:
    """The planner LLM's choice for the next Reasoning_Step (Req 4.3, 5).

    Names the Tool to invoke next, the inputs to invoke it with (validated
    against the Tool's declared input contract before invocation), and the
    planner's reasoning (recorded in the ``next_step_select`` trace step). When
    the planner falls back to an ad-hoc graph query, ``is_ad_hoc`` is set so the
    controller can record that no strategy tool / template fit (Req 5.4).

    Attributes:
        tool_name: Registry name of the selected Tool (e.g. ``strategy:entity_based``).
        inputs: Keyword inputs for the Tool's ``invoke`` call.
        reasoning: Why this Tool/these inputs were selected.
        is_ad_hoc: True when this decision represents an Ad_Hoc_Query generated
            because no Retrieval_Strategy_Tool or Query_Template was fully
            populatable for the intended retrieval.
    """

    tool_name: str
    inputs: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    is_ad_hoc: bool = False


@dataclass(frozen=True)
class StopSignalConfig:
    """Service-scoped settings for the deep-reasoning early-stop signals.

    Built once from ``ServiceConfig`` at startup and passed by constructor to the
    planner and the controller (never stored on the per-request context, so a
    sub-question fork cannot drop it). The defaults turn every signal off.

    Attributes:
        min_confidence: Planner confidence a "that is enough" stop needs (0 = no bar).
        similarity_threshold: Working-answer cosine at which a step counts as
            stable (0 = signal off).
        stability_steps: Stable steps in a row that stop the loop.
        telemetry: Record the per-reply stop signals even with both thresholds at 0.
    """

    min_confidence: float = 0.0
    similarity_threshold: float = 0.0
    stability_steps: int = 2
    telemetry: bool = False

    @property
    def elicit(self) -> bool:
        """Whether the planner prompts ask for the stop signals (confidence, working answer)."""
        return self.min_confidence > 0 or self.similarity_threshold > 0 or self.telemetry


@dataclass(frozen=True)
class ReplySignals:
    """The stop signals the planner parsed from one reply (``ctx.last_stop_signals``).

    The controller clears the carrier before every planner call, and the planner
    writes it only after a parsed reply while the stop signals are elicited, so
    ``None`` always means "no signal" (a failed call, a fake planner, no tools).

    Attributes:
        confidence: The reported probability, in ``[0, 1]``, that the content
            gathered so far answers every part of the question; ``None`` when it
            was missing or not a plain number in range.
        working_answer: The reply's grounded best answer (``""`` when absent).
        repaired: The reply came from the parse-repair turn.
        reply_chars: Length of the reply text the signals were parsed from.
    """

    confidence: float | None = None
    working_answer: str = ""
    repaired: bool = False
    reply_chars: int = 0


# What counts as a "named term" in a question (options.entityRouting): shared by the
# term extraction, the find_documents tool description and the planner guidance.
NAMED_TERM_KINDS = (
    "specific identifiers (account or record numbers, ids), people, document or message "
    "subjects, product features, systems, field or table names, and short quoted phrases"
)
