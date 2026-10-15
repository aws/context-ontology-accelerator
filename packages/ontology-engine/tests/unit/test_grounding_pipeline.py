# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests that drive the REAL grounding pipeline end-to-end.

Unlike ``test_grounding_service.py`` (which patches ``_recall`` and
``_llm_rerank`` to isolate the classification branches), these tests exercise
the recall + dedup + exact-name + LLM-rerank code paths for real, mocking only
the external boundaries: the ontology-catalog ``search_embeddings`` call and the
Bedrock ``converse`` client. This asserts observable behavior (the returned
``GroundingResult`` / ``ConceptMatch``), not internal state.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from coa_ontology.inducer.services.grounding import (
    GroundingRerankError,
    GroundingService,
    classify_score_tier,
)

pytestmark = pytest.mark.unit


def _make_service(hits: list[dict]) -> GroundingService:
    """Build a GroundingService whose catalog returns ``hits`` from recall."""
    catalog = MagicMock()
    catalog.search_embeddings.return_value = hits
    svc = GroundingService(
        ontology_catalog=catalog,
        embedding_generator=MagicMock(),
        llm_region="us-east-1",
        llm_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        namespace="ns1",
    )
    return svc


def _bedrock_returning(text: str) -> MagicMock:
    client = MagicMock()
    client.converse.return_value = {"output": {"message": {"content": [{"text": text}]}}}
    return client


# ── STANDARD mode: real recall + exact-name match ───────────────────────


class TestStandardExactMatch:
    def test_single_exact_name_match_auto_grounds_with_confidence_one(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.88, "text": "Agreement a deal"},
            {"entity_uri": "http://fibo/Party", "ontology_id": "fibo", "score": 0.40, "text": "Party a person"},
        ]
        svc = _make_service(hits)
        result = svc.ground_table(
            table_name="agreement",
            table_description="a contract",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo"],
        )
        assert result.match_type == "exact"
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.confidence == 1.0
        assert result.relationship == "exactMatch"
        assert result.mode == "STANDARD"

    def test_multiple_exact_matches_picks_highest_embedding_score(self):
        # Two candidates share local name "Agreement" but come from different
        # ontologies (so they survive dedup); STANDARD picks the higher score.
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.91, "text": "Agreement deal"},
            {"entity_uri": "http://dc/Agreement", "ontology_id": "dublincore", "score": 0.72, "text": "Agreement x"},
        ]
        svc = _make_service(hits)
        result = svc.ground_table(
            table_name="Agreement",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo"],
        )
        assert result.match_type == "high_confidence"
        assert result.relationship == "closeMatch"
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.confidence == pytest.approx(0.91)

    def test_no_exact_match_classifies_on_embedding_score(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.55, "text": "Agreement deal"},
        ]
        svc = _make_service(hits)
        result = svc.ground_table(
            table_name="policy",
            table_description="insurance policy",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo"],
        )
        # 0.55 → ambiguous tier (>=0.50, <0.80) in STANDARD (no rerank)
        assert result.match_type == "ambiguous"
        assert result.relationship == "relatedMatch"
        assert result.mode == "STANDARD"


# ── Recall internals: dedup, cosine fallback, definition extraction ─────


class TestRecall:
    def test_dedupes_http_variants_keeping_highest_score(self):
        # Same URI up to trailing slash → collapse to one, keep higher score.
        hits = [
            {"entity_uri": "http://fibo/Claim", "ontology_id": "fibo", "score": 0.60, "text": "Claim demand"},
            {"entity_uri": "http://fibo/Claim/", "ontology_id": "fibo", "score": 0.80, "text": "Claim demand"},
        ]
        svc = _make_service(hits)
        result = svc.ground_table(
            table_name="ledger",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo"],
        )
        # Only one candidate survives dedup, carrying the higher score.
        assert len(result.candidates) == 1
        assert result.candidates[0].lexical_sim == pytest.approx(0.80)

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param("A￣B", "A¯B", id="fullwidth-macron"),
            pytest.param("ﾡￂ", "ㄱㅏ", id="halfwidth-hangul"),
            pytest.param("Ꭰ", "ꭰ", id="cherokee-case"),
            pytest.param("HO\u0308HE", "HÖHE", id="canonical-equivalence"),
        ],
    )
    def test_secondary_dedup_collapses_only_lossless_identity_variants(self, first: str, second: str):
        hits = [
            {"entity_uri": f"http://example.test/{first}", "ontology_id": "test", "score": 0.60},
            {"entity_uri": f"http://example.test/{second}", "ontology_id": "test", "score": 0.80},
        ]
        result = _make_service(hits).ground_table(
            table_name="unrelated",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["test"],
        )
        assert len(result.candidates) == 1
        assert result.candidates[0].lexical_sim == pytest.approx(0.80)

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param("Hohe", "H%C3%B6he", id="accent"),
            pytest.param("A-B", "AB", id="punctuation"),
            pytest.param("👩‍🔬", "👩🔬", id="zwj-sequence"),
            pytest.param("a‌b", "ab", id="zwnj-sequence"),
            pytest.param("µ", "μ", id="micro-sign-vs-greek-mu"),
            pytest.param("ϐ", "β", id="beta-symbol-vs-beta"),
            pytest.param("aͅ", "a", id="greek-combining-mark"),
            pytest.param("ſ", "s", id="long-s"),
        ],
    )
    def test_secondary_dedup_preserves_distinct_local_names(self, first: str, second: str):
        hits = [
            {"entity_uri": f"http://example.test/{first}", "ontology_id": "test", "score": 0.60},
            {"entity_uri": f"http://example.test/{second}", "ontology_id": "test", "score": 0.80},
        ]
        result = _make_service(hits).ground_table(
            table_name="unrelated",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["test"],
        )
        assert len(result.candidates) == 2

    def test_cosine_fallback_used_when_score_absent(self):
        # No 'score' key but a raw vector present → local cosine is computed.
        hits = [
            {"entity_uri": "http://fibo/Thing", "ontology_id": "fibo", "vector": [1.0] * 8, "text": "Thing"},
        ]
        svc = _make_service(hits)
        result = svc.ground_table(
            table_name="whatever",
            table_description="",
            columns=[],
            concept_vector=[1.0] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo"],
        )
        # Identical unit vectors → cosine 1.0.
        assert result.candidates[0].lexical_sim == pytest.approx(1.0)

    def test_definition_strips_label_prefix_from_text(self):
        hits = [
            {
                "entity_uri": "http://fibo/Agreement",
                "ontology_id": "fibo",
                "score": 0.9,
                "text": "Agreement a negotiated understanding between parties",
            },
        ]
        svc = _make_service(hits)
        result = svc.ground_table(
            table_name="ledger",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo"],
        )
        cand = result.candidates[0]
        assert cand.label == "Agreement"
        # Label prefix stripped from the embedded text to yield the definition.
        assert cand.definition == "a negotiated understanding between parties"

    def test_recall_propagates_per_ontology_search_failure(self):
        """Corrected contract: a per-ontology recall failure must RE-RAISE,
        not be logged-and-skipped into a partial candidate set. Continuing with
        partial recall silently drops the broken ontology's classes → the subject
        is misclassified 'novel' (reason "No candidates returned from embedding
        search") with no signal. A loud, re-runnable failure beats a silently
        incomplete all-novel grounding. Previously this swallowed the raise and
        returned only the surviving ontology's hit."""
        catalog = MagicMock()
        catalog.search_embeddings.side_effect = [
            RuntimeError("AOSS down"),
            [{"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.9, "text": "Agreement"}],
        ]
        svc = GroundingService(ontology_catalog=catalog, embedding_generator=MagicMock())
        with pytest.raises(RuntimeError, match="AOSS down"):
            svc.ground_table(
                table_name="ledger",
                table_description="",
                columns=[],
                concept_vector=[0.5] * 8,
                model_id="titan",
                grounding_mode="STANDARD",
                ontology_ids=["broken-ont", "fibo"],
            )

    def test_exclude_entity_uri_drops_self_from_candidates(self):
        hits = [
            {"entity_uri": "http://ns/Foo", "ontology_id": "ns", "score": 0.99, "text": "Foo"},
            {"entity_uri": "http://ns/Bar", "ontology_id": "ns", "score": 0.70, "text": "Bar"},
        ]
        svc = _make_service(hits)
        result = svc.ground_table(
            table_name="something",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["ns"],
            exclude_entity_uri="http://ns/Foo",
        )
        uris = [c.entity_uri for c in result.candidates]
        assert "http://ns/Foo" not in uris
        assert "http://ns/Bar" in uris


# ── ENHANCED mode: real _llm_rerank via mocked Bedrock converse ─────────


class TestEnhancedRerank:
    def test_llm_picks_candidate_and_parses_confidence(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement a deal"},
            {"entity_uri": "http://fibo/Party", "ontology_id": "fibo", "score": 0.5, "text": "Party a person"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning(
            json.dumps({"choice": "Agreement", "relationship": "closeMatch", "confidence": 0.9, "reason": "matches"})
        )
        result = svc.ground_table(
            table_name="agr_records",
            table_description="agreement records",
            columns=[{"name": "id", "dataType": "int", "description": "pk", "constraint": "PK"}],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.relationship == "closeMatch"
        assert result.confidence == pytest.approx(0.9)
        assert result.match_type == "exact"  # 0.9 >= 0.85 with rerank
        # The chosen candidate carries the reranker annotations.
        assert result.chosen.rerank_score == pytest.approx(0.9)
        assert result.chosen.rerank_reason == "matches"

    def test_llm_abstains_none_returns_novel(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning(
            json.dumps({"choice": "NONE", "relationship": "none", "confidence": 0.0, "reason": "no fit"})
        )
        result = svc.ground_table(
            table_name="occurrence",
            table_description="events",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        assert result.match_type == "novel"
        assert result.chosen is None
        assert result.reason == "no fit"

    def test_llm_choice_not_in_candidates_returns_novel(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning(
            json.dumps({"choice": "Nonexistent", "relationship": "exactMatch", "confidence": 0.9, "reason": "x"})
        )
        result = svc.ground_table(
            table_name="ledger",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        assert result.match_type == "novel"
        assert result.chosen is None
        assert "not in candidates" in result.reason

    def test_llm_missing_confidence_and_relationship_use_defaults(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        # Neither confidence nor relationship keys present in the LLM response.
        svc._bedrock = _bedrock_returning(json.dumps({"choice": "Agreement", "reason": "x"}))
        result = svc.ground_table(
            table_name="agr_x",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        # Missing confidence → 0.5 → ambiguous tier; missing relationship → relatedMatch.
        assert result.confidence == pytest.approx(0.5)
        assert result.match_type == "ambiguous"
        assert result.relationship == "relatedMatch"

    def test_rerank_parses_markdown_fenced_json(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        fenced = (
            "```json\n"
            + json.dumps({"choice": "Agreement", "relationship": "exactMatch", "confidence": 0.95, "reason": "y"})
            + "\n```"
        )
        svc._bedrock = _bedrock_returning(fenced)
        result = svc.ground_table(
            table_name="agr_x",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.confidence == pytest.approx(0.95)

    def test_rerank_non_json_response_fails_loud(self):
        # The call succeeded but the model returned prose instead of JSON — a
        # model-contract failure, not an abstention (a real abstention is the
        # parseable {"choice": "NONE"}). It must fail loud, never silently
        # ground novel — the enhanced-grounding defect (#59).
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning("this is not json at all")
        with pytest.raises(GroundingRerankError, match="non-JSON"):
            svc.ground_table(
                table_name="agr_x",
                table_description="",
                columns=[],
                concept_vector=[0.5] * 8,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo"],
            )

    def test_rerank_converse_error_fails_loud_not_novel(self):
        # A Bedrock error surfacing past botocore's retries is an INFRASTRUCTURE
        # failure, not an abstention. It must raise (so the job fails loud),
        # never silently ground novel — the enhanced-grounding defect (#59).
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        broken = MagicMock()
        broken.converse.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "temperature is deprecated"}}, "Converse"
        )
        svc._bedrock = broken
        with pytest.raises(GroundingRerankError):
            svc.ground_table(
                table_name="agr_x",
                table_description="",
                columns=[],
                concept_vector=[0.5] * 8,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo"],
            )

    def test_rerank_reasoning_model_response_parses(self):
        # A reasoning model returns a reasoningContent block (no "text" key) at
        # index 0 and the JSON answer at index 1. Grounding must read the answer,
        # not KeyError on content[0] and degrade to novel.
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.9, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        reasoning_resp = MagicMock()
        reasoning_resp.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "thinking", "signature": "s"}}},
                        {
                            "text": json.dumps(
                                {"choice": "Agreement", "relationship": "exactMatch", "confidence": 0.95, "reason": "x"}
                            )
                        },
                    ]
                }
            },
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
        svc._bedrock = reasoning_resp
        result = svc.ground_table(
            table_name="agreement",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        assert result.chosen is not None
        assert result.chosen.label == "Agreement"
        assert result.match_type != "novel"

    @pytest.mark.parametrize(
        ("stop_reason", "match_pattern"),
        [
            # Token-budget exhaustion: the answer was cut off. Fail loud AND name
            # the knob so the operator knows how to fix it.
            ("max_tokens", "rerank_max_tokens"),
            ("model_context_window_exceeded", "rerank_max_tokens"),
            # Model-side unusable output: no knob helps, but still an
            # infrastructure failure that must not parse into a silent novel.
            ("malformed_model_output", "unusable"),
            ("content_filtered", "unusable"),
        ],
        ids=["max_tokens", "model_context_window_exceeded", "malformed_model_output", "content_filtered"],
    )
    def test_rerank_loud_stop_reason_fails_loud(self, stop_reason, match_pattern):
        # A truncating/blocked stopReason is an infrastructure failure, not an
        # abstention — do not parse a partial/absent body into a silent novel.
        # All four are doc-confirmed loud-failure Converse stopReason values.
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        bad = MagicMock()
        bad.converse.return_value = {
            # Even a body that would parse cleanly must not be trusted once the
            # stopReason flags the response unusable — the guard fires first.
            "output": {"message": {"content": [{"text": '{"choice": "Agree'}]}},
            "stopReason": stop_reason,
            "usage": {"inputTokens": 10, "outputTokens": 200},
        }
        svc._bedrock = bad
        with pytest.raises(GroundingRerankError, match=match_pattern):
            svc.ground_table(
                table_name="agr_x",
                table_description="",
                columns=[],
                concept_vector=[0.5] * 8,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo"],
            )

    def test_rerank_omits_temperature_from_inference_config(self):
        # Defect 1 of issue #59: sending inferenceConfig.temperature made newer
        # models (Opus 5 / Sonnet 5) reject the call with a ValidationException,
        # which the old code swallowed to a silent novel. We only ever wanted 0
        # (the model default), so the key must NOT be sent at all. Capture the
        # converse kwargs and assert temperature is absent.
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning(
            json.dumps({"choice": "Agreement", "relationship": "exactMatch", "confidence": 0.9, "reason": "y"})
        )
        svc.ground_table(
            table_name="agr_x",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        _, kwargs = svc._bedrock.converse.call_args
        assert "temperature" not in kwargs["inferenceConfig"]

    def test_rerank_max_tokens_reaches_converse_inference_config(self):
        # The rerank_max_tokens knob must land on the Bedrock converse call's
        # inferenceConfig.maxTokens — that's the value that gives reasoning
        # models JSON headroom. Capture the kwargs and assert the custom value.
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning(
            json.dumps({"choice": "Agreement", "relationship": "exactMatch", "confidence": 0.9, "reason": "y"})
        )
        svc.ground_table(
            table_name="agr_x",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
            rerank_max_tokens=1500,
        )
        _, kwargs = svc._bedrock.converse.call_args
        assert kwargs["inferenceConfig"]["maxTokens"] == 1500

    def test_rerank_max_tokens_defaults_to_1000(self):
        # Omitting the knob uses the 1000 default (above the broken 200, below
        # the 4096 general default), so a reasoning model's thinking doesn't
        # truncate the JSON answer on the happy path.
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning(
            json.dumps({"choice": "Agreement", "relationship": "exactMatch", "confidence": 0.9, "reason": "y"})
        )
        svc.ground_table(
            table_name="agr_x",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        _, kwargs = svc._bedrock.converse.call_args
        assert kwargs["inferenceConfig"]["maxTokens"] == 1000

    def test_to_concept_match_after_real_pipeline(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement a deal"},
        ]
        svc = _make_service(hits)
        svc._bedrock = _bedrock_returning(
            json.dumps({"choice": "Agreement", "relationship": "exactMatch", "confidence": 0.92, "reason": "z"})
        )
        result = svc.ground_table(
            table_name="agr_x",
            table_description="",
            columns=[],
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        cm = svc.to_concept_match(result)
        assert cm.matched_class_uri == "http://fibo/Agreement"
        assert cm.matched_ontology_id == "fibo"
        assert cm.similarity == pytest.approx(0.92)
        assert cm.scoring_strategy == "grounding_enhanced"
        assert cm.candidates and cm.candidates[0].entity_uri == "http://fibo/Agreement"


class TestGroundClassPromptFraming:
    def test_ground_class_omits_columns_and_labels_source_class(self):
        hits = [
            {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo", "score": 0.6, "text": "Agreement"},
        ]
        svc = _make_service(hits)
        captured: dict[str, str] = {}

        def _converse(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"][0]["text"]
            captured["system"] = kwargs["system"][0]["text"]
            return {
                "output": {
                    "message": {"content": [{"text": json.dumps({"choice": "NONE", "confidence": 0.0, "reason": "x"})}]}
                }
            }

        svc._bedrock = MagicMock(converse=_converse)
        svc.ground_class(
            class_label="agreement",
            class_description="a mutual understanding",
            concept_vector=[0.5] * 8,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["fibo"],
        )
        assert "SOURCE CLASS:" in captured["prompt"]
        assert "Columns:" not in captured["prompt"]
        assert "source ontology class" in captured["system"].lower()


class TestClassifyScoreTier:
    def test_public_wrapper_reranked_ladder(self):
        assert classify_score_tier(0.9) == "exact"
        assert classify_score_tier(0.7) == "high_confidence"
        assert classify_score_tier(0.5) == "ambiguous"
        assert classify_score_tier(0.2) == "novel"

    def test_none_score_is_novel(self):
        assert classify_score_tier(None) == "novel"

    def test_embedding_ladder_when_no_rerank(self):
        assert classify_score_tier(0.97, has_rerank=False) == "exact"
        assert classify_score_tier(0.85, has_rerank=False, confidence_threshold=0.80) == "high_confidence"
