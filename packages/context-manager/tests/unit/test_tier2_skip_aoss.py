# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the AOSS mapped-class-provenance Tier-2 skip evaluator."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from coa_serve.clients.base import VectorHit
from coa_serve.clients.sources_registry import SourceComposition
from coa_serve.tier2.skip.aoss_provenance import AossProvenanceEvaluator
from coa_serve.tier2.skip.evaluator import Tier2SkipDecision
from coa_serve.trace import TraceCollector


def _hit(ds_id: str = "", *, score: float = 0.9, name: str = "C") -> VectorHit:
    md = {"entity_type": "class"}
    if ds_id:
        md["data_source_id"] = ds_id
    return VectorHit(id=name, text=f"Table: {name}", score=score, metadata=md)


def _make_evaluator(hits, *, composition="mixed"):
    """Build an evaluator with a mock vector client (returns `hits`) and registry."""
    vector = AsyncMock()
    vector.search.return_value = hits

    registry = MagicMock()
    if composition == "mixed":
        comp = SourceComposition(has_structured_source=True, has_unstructured_source=True)
    elif composition == "structured_only":
        comp = SourceComposition(has_structured_source=True, has_unstructured_source=False)
    elif composition == "doc_only":
        comp = SourceComposition(has_structured_source=False, has_unstructured_source=True)
    else:  # unknown
        comp = SourceComposition.unknown_composition()
    registry.get_source_composition = AsyncMock(return_value=comp)

    ev = AossProvenanceEvaluator(vector_client=vector, sources_registry=registry, oss_ontology_index="idx")
    return ev, vector, registry


async def _decide(ev, *, embedding=None) -> Tier2SkipDecision:
    return await ev.should_skip_tier2("q", "ns", embedding or [0.1] * 8, trace=TraceCollector())


@pytest.mark.unit
class TestAossProvenanceEvaluator:
    async def test_all_topk_unmapped_but_provenance_present_skips(self):
        """Top-k all unmapped AND a mapped class exists deeper in results → SKIP."""
        hits = [_hit("", name=f"D{i}") for i in range(10)] + [_hit("src-db", name="M")]
        ev, _, _ = _make_evaluator(hits)
        assert await _decide(ev) is Tier2SkipDecision.SKIP

    async def test_any_mapped_in_topk_runs(self):
        """A mapped class within the top-k decision window → RUN (Tier 2 may answer)."""
        hits = [_hit("", name="D1"), _hit("src-db", name="M"), _hit("", name="D2")]
        ev, _, _ = _make_evaluator(hits)
        assert await _decide(ev) is Tier2SkipDecision.RUN

    async def test_no_provenance_anywhere_abstains(self):
        """All hits empty data_source_id (field never stamped) → ABSTAIN, not SKIP.

        A namespace whose ontology predates data_source_id is all-empty too; we must
        not mistake that for "all unmapped" and drop a valid structured answer.
        """
        hits = [_hit("", name=f"C{i}") for i in range(12)]
        ev, _, _ = _make_evaluator(hits)
        assert await _decide(ev) is Tier2SkipDecision.ABSTAIN

    async def test_empty_results_abstains(self):
        ev, _, _ = _make_evaluator([])
        assert await _decide(ev) is Tier2SkipDecision.ABSTAIN

    async def test_non_mixed_namespace_abstains_without_search(self):
        """Pure namespaces are handled by namespace-level gating — evaluator must not even search."""
        for comp in ("structured_only", "doc_only", "unknown"):
            ev, vector, _ = _make_evaluator([_hit("", name="D")], composition=comp)
            assert await _decide(ev) is Tier2SkipDecision.ABSTAIN
            vector.search.assert_not_awaited()

    async def test_no_embedding_abstains_without_search(self):
        ev, vector, _ = _make_evaluator([_hit("", name="D")])
        assert await ev.should_skip_tier2("q", "ns", None, trace=TraceCollector()) is Tier2SkipDecision.ABSTAIN
        vector.search.assert_not_awaited()

    async def test_search_error_abstains(self):
        ev, vector, _ = _make_evaluator([])
        vector.search.side_effect = RuntimeError("aoss boom")
        assert await _decide(ev) is Tier2SkipDecision.ABSTAIN

    async def test_decision_cached_single_search(self):
        hits = [_hit("", name=f"D{i}") for i in range(10)] + [_hit("src-db", name="M")]
        ev, vector, _ = _make_evaluator(hits)
        assert await _decide(ev) is Tier2SkipDecision.SKIP
        assert await _decide(ev) is Tier2SkipDecision.SKIP
        vector.search.assert_awaited_once()

    async def test_skip_records_routing_trace(self):
        hits = [_hit("", name=f"D{i}") for i in range(10)] + [_hit("src-db", name="M")]
        ev, _, _ = _make_evaluator(hits)
        trace = TraceCollector()
        await ev.should_skip_tier2("q", "ns", [0.1] * 8, trace=trace)
        step = next((s for s in trace.steps if s.step == "routing.select"), None)
        assert step is not None
        assert step.detail["gating"] == "unmapped_classes"
        assert "tier2" in step.detail["skipped"]
        # Trace reports the executed probe size, not just the decision window.
        assert step.detail["probeK"] == 50
        assert step.detail["decisionK"] == 10

    async def test_decision_cache_is_bounded(self):
        """The decision cache is a bounded LRU under high query cardinality."""
        from coa_serve.tier2.skip import aoss_provenance as mod

        hits = [_hit("", name=f"D{i}") for i in range(10)] + [_hit("src-db", name="M")]
        ev, _, _ = _make_evaluator(hits)
        # Drive more distinct queries than the cap; cache must stay bounded.
        for i in range(mod._DECISION_CACHE_MAX + 50):
            await ev.should_skip_tier2(f"q{i}", "ns", [0.1] * 8, trace=TraceCollector())
        assert len(ev._decision_cache) <= mod._DECISION_CACHE_MAX
