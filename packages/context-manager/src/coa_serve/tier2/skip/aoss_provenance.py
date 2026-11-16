# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AOSS mapped-class-provenance Tier-2 skip evaluator.

Prunes the Tier-2 structured-query path for a **document-flavored query inside a
MIXED namespace** — the one gap namespace-level source-composition gating leaves
open (a mixed namespace has a DATABASE source, so that gating runs Tier 2).

Signal (subtractive, capability-based — NOT intent classification):
    An ontology class carries a ``data_source_id`` in AOSS *iff* it is R2RML-mapped
    to a relational source (the field is stamped only on the structured induction
    path). So ``data_source_id`` presence == "Tier-2-capable". If the classes a
    query retrieves are ALL unmapped (empty ``data_source_id``), Tier 2 cannot
    generate SQL from them → prune it.

Guardrails (fail-open — see ``Tier2SkipDecision``):
  * Only runs for MIXED namespaces (≥1 DATABASE and ≥1 DOCUMENTS source). Pure
    namespaces are already decided deterministically by namespace-level gating;
    running here would add an AOSS search with nothing to gain.
  * ``tierOverride`` / no embedding / no vector client / unconfigured index →
    ABSTAIN.
  * "All top-k unmapped" is only trusted when there is positive evidence the
    namespace actually stamps provenance — i.e. at least one class in the
    (over-fetched) result set IS mapped. Otherwise the all-empty result is
    ambiguous (a namespace whose ontology predates the ``data_source_id`` field is
    also all-empty) and we ABSTAIN rather than risk dropping a valid structured
    answer.
  * Any error is swallowed → ABSTAIN.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from coa_common import ontology_vector_index_name

from ...clients.base import VectorHit
from ...clients.ttl_cache import TtlLruCache
from ...step_ids import StepId
from .evaluator import Tier2SkipDecision

if TYPE_CHECKING:
    from ...clients.base import VectorClient
    from ...clients.sources_registry import SourcesRegistry
    from ...trace import TraceCollector

logger = structlog.get_logger(__name__)

# Classes examined for the SKIP decision. Sized to cover Tier 2's own retrieval
# window (NL→SQL uses 7, Ontop 10) so we never decide "all unmapped" on a NARROWER
# view than Tier 2 would actually use.
_DECISION_TOP_K = 10
# Larger window used only to prove provenance is populated in this namespace (the
# fail-open guard). k-NN cost scales with k, not index size, and ontology indexes
# are small (~10^2 entities), so this stays cheap. Invariant: must be >= the
# decision window (the guard is meaningless on a window smaller than the decision).
_PROVENANCE_PROBE_K = 50
assert _PROVENANCE_PROBE_K >= _DECISION_TOP_K
_CACHE_TTL_S = 120.0
# Upper bound on cached decisions. Bounds memory under high query cardinality — a
# long-lived process would otherwise accumulate one entry per distinct
# (namespace, query) forever (TTL alone only invalidates on read, never evicts).
_DECISION_CACHE_MAX = 1024
# Trace discriminator for this gating decision. Names the SIGNAL (top-k classes
# all unmapped), consistent with the "source_composition" gating step — not the
# backend or an inferred intent.
_GATING_KIND = "unmapped_classes"


class AossProvenanceEvaluator:
    """Skip Tier 2 when a query's top-k ontology classes are all unmapped.

    Reuses the query embedding the orchestrator already computed — it issues at
    most one k-NN search per evaluation (only for MIXED namespaces). Composition
    lookups reuse the shared ``SourcesRegistry`` cache.
    """

    def __init__(
        self,
        vector_client: VectorClient,
        sources_registry: SourcesRegistry,
        oss_ontology_index: str,
    ):
        """Wire the vector client, sources registry, and ontology index."""
        self._vector = vector_client
        self._sources_registry = sources_registry
        self._oss_ontology_index = oss_ontology_index
        # Per-(namespace, query) decision cache with a short TTL and LRU bound.
        self._decision_cache: TtlLruCache[tuple[str, str], Tier2SkipDecision] = TtlLruCache(
            ttl_s=_CACHE_TTL_S, max_size=_DECISION_CACHE_MAX
        )

    async def should_skip_tier2(
        self,
        query: str,
        namespace: str,
        embedding: list[float] | None,
        *,
        trace: TraceCollector,
    ) -> Tier2SkipDecision:
        """Decide whether Tier 2 should be skipped for this query, failing open on error."""
        try:
            return await self._evaluate(query, namespace, embedding, trace=trace)
        except Exception as e:  # defensive: an evaluator must never break resolve()
            logger.warning("tier2_skip_evaluator_failed", namespace=namespace, error=type(e).__name__)
            return Tier2SkipDecision.ABSTAIN

    async def _evaluate(
        self,
        query: str,
        namespace: str,
        embedding: list[float] | None,
        *,
        trace: TraceCollector,
    ) -> Tier2SkipDecision:
        # Precondition — no embedding means no query to route; fail open.
        # (oss_ontology_index is guaranteed non-empty: main.py only constructs this
        # evaluator when it is set.)
        if embedding is None:
            return Tier2SkipDecision.ABSTAIN

        # MIXED-only gate. Pure namespaces are handled by namespace-level gating;
        # skip evaluation entirely (and thus the AOSS search) for anything not mixed.
        composition = await self._sources_registry.get_source_composition(namespace)
        if composition.unknown or not (composition.has_structured_source and composition.has_unstructured_source):
            return Tier2SkipDecision.ABSTAIN

        cache_key = (namespace, query)
        cached = self._decision_cache.get(cache_key)
        if cached is not None:
            return cached

        index = ontology_vector_index_name(self._oss_ontology_index, namespace)
        hits = await self._vector.search(
            embedding,
            namespace=namespace,
            top_k=_PROVENANCE_PROBE_K,
            index=index,
            entity_type="class",
        )
        decision = self._decide(hits)
        self._decision_cache.set(cache_key, decision)

        if decision is Tier2SkipDecision.SKIP:
            trace.record(
                StepId.ROUTING_SELECT,
                "success",
                0,
                # probeK = classes actually fetched; decisionK = window the SKIP is
                # based on. Reporting both avoids masking the over-fetch cost.
                detail={
                    "gating": _GATING_KIND,
                    "skipped": ["tier2"],
                    "decisionK": _DECISION_TOP_K,
                    "probeK": _PROVENANCE_PROBE_K,
                },
            )
        return decision

    @staticmethod
    def _decide(hits: list[VectorHit]) -> Tier2SkipDecision:
        """Apply the subtractive, fail-open rule to class hits.

        ``hits`` is the over-fetched (``_PROVENANCE_PROBE_K``) class result set;
        the SKIP decision looks only at the top ``_DECISION_TOP_K`` of it, while the
        full set is used to prove provenance is populated in this namespace.
        """
        if not hits:
            return Tier2SkipDecision.ABSTAIN  # no retrieval signal → run Tier 2

        # A class is Tier-2-capable iff it carries provenance (data_source_id).
        # Provenance must be populated somewhere in this namespace, else "all empty"
        # is ambiguous.
        if not any(h.data_source_id for h in hits):
            return Tier2SkipDecision.ABSTAIN

        decision_window = hits[:_DECISION_TOP_K]
        # If any class the query actually retrieved is mapped, Tier 2 might answer.
        if any(h.data_source_id for h in decision_window):
            return Tier2SkipDecision.RUN

        # Top-k are all unmapped while the namespace stamps provenance elsewhere.
        return Tier2SkipDecision.SKIP
