# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the grounding recall-ceiling fixes (issue 72).

Three independent mechanisms let the correct golden class reach the reranker
when dense embedding recall alone misses it:

  1. A widened, parameterized reranker window (was a hardcoded 8) so a class
     recalled at rank 9+ is still shown to the LLM.
  2. An additive token-overlap (lexical) retriever that surfaces a class
     sharing surface tokens with the subject even when dense ranked it out, and
     pins it into the reranker window.
  3. A flat-recall-spread guard that caps an undiscriminating recall at
     ``ambiguous`` so a high score off a near-arbitrary ranking routes to
     review instead of being auto-accepted.

All external I/O (Bedrock, AOSS) is mocked — no network.
"""

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer.services.grounding import (
    GroundingCandidate,
    GroundingService,
    _tokenize,
    classify_score_tier,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_eg():
    return MagicMock()


def _svc(catalog, eg):
    return GroundingService(
        ontology_catalog=catalog,
        embedding_generator=eg,
        llm_region="us-east-1",
        llm_model_id="us.anthropic.claude-haiku-4-5-20251001",
    )


# ── Fix 3: flat-spread guard ─────────────────────────────────────────────────


class TestFlatSpreadGuard:
    def test_flat_spread_caps_high_score_at_ambiguous(self):
        # A confident score off an undiscriminating recall (spread < 0.05) must
        # not auto-accept; it routes to review.
        assert classify_score_tier(0.90, has_rerank=True, recall_spread=0.02) == "ambiguous"

    def test_flat_spread_caps_high_confidence_too(self):
        assert classify_score_tier(0.70, has_rerank=True, recall_spread=0.01) == "ambiguous"

    def test_flat_spread_does_not_rescue_novel(self):
        # The guard only downgrades exact/high_confidence — a novel stays novel.
        assert classify_score_tier(0.20, has_rerank=True, recall_spread=0.0) == "novel"

    def test_wide_spread_keeps_exact(self):
        assert classify_score_tier(0.90, has_rerank=True, recall_spread=0.30) == "exact"

    def test_absent_spread_is_backward_compatible(self):
        # No spread supplied → original absolute-threshold behavior, unchanged.
        assert classify_score_tier(0.90, has_rerank=True) == "exact"
        assert classify_score_tier(0.70, has_rerank=True) == "high_confidence"
        assert classify_score_tier(0.50, has_rerank=True) == "ambiguous"

    def test_standard_ladder_also_guarded(self):
        assert classify_score_tier(0.96, has_rerank=False, recall_spread=0.01) == "ambiguous"
        assert classify_score_tier(0.96, has_rerank=False, recall_spread=0.30) == "exact"


# ── tokenizer ────────────────────────────────────────────────────────────────


class TestTokenize:
    def test_splits_camel_case(self):
        assert _tokenize("apaAreaGross") == {"apa", "area", "gross"}

    def test_splits_snake_case(self):
        assert _tokenize("tuf_petreg_licence") == {"tuf", "petreg", "licence"}

    def test_drops_single_char_tokens(self):
        # "a" is below _MIN_TOKEN_LEN; "id" (2 chars) survives.
        assert _tokenize("aXId") == {"id"}


# ── Fix 2: lexical (token-overlap) recall ────────────────────────────────────


class TestLexicalRecall:
    def _catalog(self, dense, listed):
        oc = MagicMock()
        oc.search_embeddings.return_value = dense
        oc.list_embeddings_for_ontology.return_value = listed
        return oc

    def test_surfaces_token_overlap_class_dense_missed(self, mock_eg):
        # Dense recall returns only unrelated classes; the correct class
        # (MainArea, sharing token "area" with apaAreaGross) is absent from dense
        # but present in the ontology listing. It must appear, flagged lexical.
        dense = [
            {"entity_uri": "http://npdv/Geometry", "ontology_id": "npd-v2", "score": 0.46},
            {"entity_uri": "http://npdv/Point", "ontology_id": "npd-v2", "score": 0.44},
        ]
        listed = [
            {"entity_uri": "http://npdv/MainArea", "ontology_id": "npd-v2", "text": "Main Area — a main area"},
            {"entity_uri": "http://npdv/Geometry", "ontology_id": "npd-v2", "text": "Geometry"},
            {"entity_uri": "http://npdv/Wellbore", "ontology_id": "npd-v2", "text": "Wellbore"},
        ]
        svc = _svc(self._catalog(dense, listed), mock_eg)
        cands = svc._recall([0.1] * 10, "model", ["npd-v2"], top_k=30, subject_name="apaAreaGross")
        by_uri = {c.entity_uri: c for c in cands}
        assert "http://npdv/MainArea" in by_uri
        assert by_uri["http://npdv/MainArea"].from_lexical is True

    def test_no_subject_name_skips_lexical(self, mock_eg):
        oc = self._catalog([{"entity_uri": "http://npdv/Point", "ontology_id": "npd-v2", "score": 0.44}], [])
        svc = _svc(oc, mock_eg)
        svc._recall([0.1] * 10, "model", ["npd-v2"], top_k=30, subject_name=None)
        oc.list_embeddings_for_ontology.assert_not_called()

    def test_degrades_to_dense_when_catalog_cannot_list(self, mock_eg):
        # A catalog without list_embeddings_for_ontology (e.g. the HTTP client)
        # must not break recall — dense results still return.
        oc = MagicMock(spec=["search_embeddings"])
        oc.search_embeddings.return_value = [
            {"entity_uri": "http://npdv/Point", "ontology_id": "npd-v2", "score": 0.44},
        ]
        svc = _svc(oc, mock_eg)
        cands = svc._recall([0.1] * 10, "model", ["npd-v2"], top_k=30, subject_name="apaAreaGross")
        assert [c.entity_uri for c in cands] == ["http://npdv/Point"]

    def test_lexical_does_not_duplicate_a_dense_hit(self, mock_eg):
        dense = [{"entity_uri": "http://npdv/MainArea", "ontology_id": "npd-v2", "score": 0.80}]
        listed = [{"entity_uri": "http://npdv/MainArea", "ontology_id": "npd-v2", "text": "Main Area"}]
        svc = _svc(self._catalog(dense, listed), mock_eg)
        cands = svc._recall([0.1] * 10, "model", ["npd-v2"], top_k=30, subject_name="apaAreaGross")
        assert sum(1 for c in cands if c.entity_uri == "http://npdv/MainArea") == 1


# ── Fix 1: reranker window ───────────────────────────────────────────────────


class TestRerankerWindow:
    def _capture_converse(self, captured):
        def fake_converse(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"][0]["text"]
            return {
                "output": {"message": {"content": [{"text": '{"choice": "NONE", "confidence": 0.0, "reason": "x"}'}]}}
            }

        return fake_converse

    def test_candidates_beyond_old_cap_reach_the_llm(self, mock_eg):
        # 12 dense candidates: the old hardcoded [:8] hid ranks 9-12. With the
        # widened window they must appear in the rerank prompt.
        cands = [
            GroundingCandidate(
                entity_uri=f"http://x/Class{i}",
                ontology_id="o",
                label=f"Class{i}",
                definition="d",
                lexical_sim=0.9 - i * 0.01,
            )
            for i in range(12)
        ]
        svc = _svc(MagicMock(), mock_eg)
        captured: dict = {}
        fake = MagicMock(converse=self._capture_converse(captured))
        with patch.object(type(svc), "bedrock", property(lambda self: fake)):
            svc._llm_rerank("tbl", "desc", [], cands)
        assert "Class9" in captured["prompt"]
        assert "Class11" in captured["prompt"]

    def test_lexical_candidate_past_window_is_pinned(self, mock_eg):
        # 25 dense (window is 20) + one lexical candidate at the very end. The
        # dense tail past the window is dropped, but the lexical one is pinned in.
        cands = [
            GroundingCandidate(
                entity_uri=f"http://x/Dense{i}",
                ontology_id="o",
                label=f"Dense{i}",
                definition="d",
                lexical_sim=0.9 - i * 0.01,
            )
            for i in range(25)
        ]
        cands.append(
            GroundingCandidate(
                entity_uri="http://x/MainArea",
                ontology_id="o",
                label="MainArea",
                definition="d",
                lexical_sim=0.0,
                from_lexical=True,
            )
        )
        svc = _svc(MagicMock(), mock_eg)
        captured: dict = {}
        fake = MagicMock(converse=self._capture_converse(captured))
        with patch.object(type(svc), "bedrock", property(lambda self: fake)):
            svc._llm_rerank("tbl", "desc", [], cands)
        # A dense candidate past the window is cut; the lexical one is pinned.
        assert "Dense24" not in captured["prompt"]
        assert "MainArea" in captured["prompt"]
