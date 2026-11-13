# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 3 — Agentic Retriever subpackage.

A server-side reasoning agent that iteratively explores the graphrag-toolkit
knowledge graph through an internal-only tool set, then synthesizes a
best-effort answer via the existing guardrailed ``Synthesizer``.

Import discipline: module load of this subpackage MUST NOT
import ``graphrag_toolkit``. All toolkit access is delegated to the already-lazy
``BaselineLexicalRetriever`` / ``STRATEGY_REGISTRY`` ``build_engine`` callables,
imported lazily inside the tool ``invoke`` methods — never at module load. This
``__init__`` therefore exports only the dependency-free shared data models; the
heavier components (controller, tools, retriever) are imported directly from
their own modules by the factory to keep this package's load cheap and lazy.
"""

from __future__ import annotations

from .budget import AgenticBudgetConfig, BudgetClock
from .context import AccumulatedContext
from .models import ExplorationBranch, PlannerDecision, StopReason, SubQuestion

__all__ = [
    "SubQuestion",
    "ExplorationBranch",
    "PlannerDecision",
    "StopReason",
    "AgenticBudgetConfig",
    "BudgetClock",
    "AccumulatedContext",
]
