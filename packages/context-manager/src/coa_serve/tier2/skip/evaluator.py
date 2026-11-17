# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier-2 skip evaluator interface and decision type.

A ``Tier2SkipEvaluator`` answers ONE narrow question for a single query:

    "Can Tier 2 (the structured NL→SQL/SPARQL path) be pruned for this query
     because it provably cannot contribute — routing it AWAY, not routing it TO?"

The contract is deliberately conservative and **fail-open**: an evaluator returns
``SKIP`` only on positive proof that Tier 2 is useless, ``RUN`` when there is
positive evidence it could contribute, and ``ABSTAIN`` on ANY uncertainty. The
orchestrator treats both ``RUN`` and ``ABSTAIN`` as "run Tier 2 as normal" — so a
mistaken or unavailable evaluator can never drop a tier that would have answered.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ...trace import TraceCollector


class Tier2SkipDecision(Enum):
    """Outcome of a Tier-2 skip evaluation.

    ``SKIP``    — proven that Tier 2 cannot contribute; prune it (→ fall to Tier 3).
    ``RUN``     — positive evidence Tier 2 could contribute; run it.
    ``ABSTAIN`` — undetermined (no signal / error / disabled); fail open → run Tier 2.
    """

    SKIP = "skip"
    RUN = "run"
    ABSTAIN = "abstain"


@runtime_checkable
class Tier2SkipEvaluator(Protocol):
    """Decides whether Tier 2 can be pruned for a single query.

    Implementations own all detection logic and any I/O (vector search, registry
    lookups, caching) so the orchestrator stays free of routing internals. They
    MUST honor the fail-open contract: never raise to the caller, and return
    ``ABSTAIN`` on any doubt.
    """

    async def should_skip_tier2(
        self,
        query: str,
        namespace: str,
        embedding: list[float] | None,
        *,
        trace: TraceCollector,
    ) -> Tier2SkipDecision:
        """Return whether Tier 2 should be skipped for this query. Never raises."""
        ...
