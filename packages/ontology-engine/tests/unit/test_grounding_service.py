# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the centralized grounding service."""

from __future__ import annotations

import unicodedata
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer.services.grounding import (
    GroundingCandidate,
    GroundingRerankError,
    GroundingResult,
    GroundingService,
    _local_name,
    _normalize,
)

pytestmark = pytest.mark.unit

_ENGLAND_FLAG = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
_SCOTLAND_FLAG = "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"


@pytest.fixture
def mock_catalog():
    oc = MagicMock()
    oc.search_embeddings.return_value = [
        {"entity_uri": "http://fibo/Agreement", "ontology_id": "fibo-agreements", "vector": [0.5] * 10},
        {"entity_uri": "http://fibo/CreditAgreement", "ontology_id": "fibo-agreements", "vector": [0.4] * 10},
        {"entity_uri": "http://fibo/ContractDocument", "ontology_id": "fibo-contracts", "vector": [0.3] * 10},
    ]
    return oc


@pytest.fixture
def mock_eg():
    return MagicMock()


@pytest.fixture
def service(mock_catalog, mock_eg):
    return GroundingService(
        ontology_catalog=mock_catalog,
        embedding_generator=mock_eg,
        llm_region="us-east-1",
        llm_model_id="us.anthropic.claude-haiku-4-5-20251001",
    )


class TestNormalize:
    def test_basic(self):
        assert _normalize("Agreement") == "agreement"
        assert _normalize("credit_agreement") == "creditagreement"
        assert _normalize("CreditAgreement") == "creditagreement"
        assert _normalize("party_role") == "partyrole"

    def test_strips_special_chars(self):
        assert _normalize("hello-world_123") == "helloworld123"

    def test_does_not_casefold_distinct_sharp_s_words(self):
        assert _normalize("Maße") != _normalize("Masse")

    def test_preserves_compatibility_symbols_without_expanding_them(self):
        assert _normalize("℃") == "℃"
        assert _normalize("™") == "™"
        assert _normalize("℃") != _normalize("C")
        assert _normalize("™") != _normalize("TM")

    def test_simple_case_identity_unifies_sigma_without_expanding_sharp_s(self):
        assert _normalize("Σ") == _normalize("σ") == _normalize("ς")
        assert _normalize("Maße") != _normalize("Masse")

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("A￣B", "A¯B", id="fullwidth-macron"),
            pytest.param("ﾡￂ", "ㄱㅏ", id="halfwidth-hangul"),
            pytest.param("HO\u0308HE", "HÖHE", id="canonical-equivalence"),
        ],
    )
    def test_width_and_canonical_variants_share_fuzzy_identity(self, left: str, right: str) -> None:
        assert _normalize(left) == _normalize(right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("👩‍🔬", "👩🔬", id="zwj-sequence"),
            pytest.param("a‌b", "ab", id="zwnj-sequence"),
            pytest.param("µ", "μ", id="micro-sign-vs-greek-mu"),
            pytest.param("ϐ", "β", id="beta-symbol-vs-beta"),
            pytest.param("aͅ", "a", id="greek-combining-mark"),
            pytest.param("ſ", "s", id="long-s"),
        ],
    )
    def test_distinct_unicode_identities_do_not_share_fuzzy_key(self, left: str, right: str) -> None:
        assert _normalize(left) != _normalize(right)

    def test_attached_greek_mark_remains_significant_after_ascii_base(self) -> None:
        assert _normalize("aͅ") == "aͅ"

    def test_subdivision_flags_keep_attached_tags_and_orphans_are_dropped(self) -> None:
        assert _normalize(_ENGLAND_FLAG) == _ENGLAND_FLAG
        assert _normalize(_SCOTLAND_FLAG) == _SCOTLAND_FLAG
        assert _normalize(_ENGLAND_FLAG) != _normalize(_SCOTLAND_FLAG)
        assert _normalize("\U000e0067") == ""


class TestNormalizeIsScriptAgnostic:
    """The key must survive in every script, because equal keys mean "same concept".

    ``_normalize`` is only ever used as ``_normalize(a) == _normalize(b)``. The
    previous ``[^a-z0-9]`` filter judged by ASCII range, so a label in any
    non-Latin script normalized to ``""`` — and ``"" == ""`` is True, so every
    pair of non-Latin labels matched each other. That is worse than failing to
    match: it grounded distinct concepts onto one another silently, and the more
    of a corpus was non-Latin the more of it collapsed.
    """

    @pytest.mark.parametrize(
        "label",
        ["設置場所", "メーカー", "エレベーター", "安装位置", "제조사", "ИЗГОТОВИТЕЛЬ"],
    )
    def test_a_non_latin_label_does_not_normalize_away(self, label):
        assert _normalize(label) != ""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("設置場所", "メーカー", id="ja-unrelated-pair"),
            pytest.param("エレベーター", "エスカレーター", id="ja-elevator-vs-escalator"),
            pytest.param("제조사", "설치장소", id="ko-unrelated-pair"),
            pytest.param("安装位置", "制造商", id="zh-unrelated-pair"),
        ],
    )
    def test_two_unrelated_non_latin_labels_are_not_equal(self, left, right):
        """The defect itself: unrelated concepts compared equal via the empty string."""
        assert _normalize(left) != _normalize(right)

    @pytest.mark.parametrize(
        ("voiced", "unvoiced"),
        [
            pytest.param("ベース", "ヘース", id="katakana-dakuten"),
            pytest.param("ゲージ", "ケージ", id="katakana-gauge-vs-cage"),
            pytest.param("ぱす", "はす", id="hiragana-handakuten"),
        ],
    )
    def test_japanese_voicing_marks_are_not_stripped(self, voiced, unvoiced):
        """Dakuten and handakuten are combining marks, but they are part of the letter.

        NFKD splits ベ into ヘ + U+3099, so a blanket combining-mark strip — the
        obvious way to fold Latin accents — turns エレベーター into エレヘーター and
        ぱ into は. Those are different words, and folding them together matches
        the wrong concept. Marks are dropped only when the base letter is ASCII.
        """
        assert _normalize(voiced) != _normalize(unvoiced)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("कि", "की", id="devanagari-vowel-signs"),
            pytest.param("กิ", "กี", id="thai-vowel-signs"),
        ],
    )
    def test_script_vowel_marks_keep_labels_distinct(self, left: str, right: str) -> None:
        """Spacing and nonspacing vowel marks are part of the comparison key."""
        assert _normalize(left) == left
        assert _normalize(right) == right
        assert _normalize(left) != _normalize(right)

    @pytest.mark.parametrize("label", ["❤️", "✈️", "☀️", "⚙️"])
    def test_symbol_variation_selectors_remain_attached(self, label: str) -> None:
        """Symbols are identity bases; their variation selectors stay attached."""
        assert _normalize(label) == label

    def test_enclosing_mark_attached_to_a_letter_is_retained(self) -> None:
        assert _normalize("क⃝") == "क⃝"

    @pytest.mark.parametrize(
        ("accented", "plain"),
        [
            pytest.param("HÖHE", "HOHE", id="de-umlaut"),
            pytest.param("MODÈLE", "MODELE", id="fr-grave"),
            pytest.param("TAMAÑO", "TAMANO", id="es-tilde"),
            pytest.param("AÇÃO", "ACAO", id="pt-cedilla-tilde"),
        ],
    )
    def test_latin_accents_still_fold_for_matching(self, accented, plain):
        """Folding Latin diacritics is deliberate here and must not regress.

        Unlike ``normalize_predicate``, this value is a comparison key that is
        never persisted, so making ``Höhe`` match ``HOHE`` is the point rather
        than data loss.
        """
        assert _normalize(accented) == _normalize(plain)

    @pytest.mark.parametrize("label", ["HÖHE", "エレベーター", "설치장소"])
    def test_key_is_independent_of_input_normalization_form(self, label):
        """Composed and decomposed input must produce the same key.

        Labels arrive from an LLM, a Turtle file, and a graph store, which need not
        agree on NFC vs NFD. If the key depended on the form, the same label from
        two sources would fail to match itself.
        """
        assert _normalize(unicodedata.normalize("NFC", label)) == _normalize(unicodedata.normalize("NFD", label))


class TestLocalName:
    def test_hash_fragment(self):
        assert _local_name("http://ex.org/ont#Foo") == "Foo"

    def test_slash_path(self):
        assert _local_name("http://fibo/Agreements/Agreement") == "Agreement"

    def test_percent_encoded_combining_mark_is_decoded(self):
        assert _local_name("http://example.test/class/क%E0%A4%BF") == "कि"

    @pytest.mark.parametrize("local_name", ["%FF", "A%20B%ZZ", "%E0%A4%BF%ZZ", "%", "%2"])
    def test_malformed_percent_encoding_is_preserved_whole(self, local_name: str):
        assert _local_name(f"http://example.test/class/{local_name}") == local_name


class TestGroundingModeNone:
    def test_returns_novel_immediately(self, service):
        result = service.ground_table(
            table_name="agreement",
            table_description="test",
            columns=[],
            concept_vector=[0.1] * 10,
            model_id="titan",
            grounding_mode="NONE",
        )
        assert result.match_type == "novel"
        assert result.mode == "NONE"
        assert result.candidates == []


class TestExactNameMatch:
    def test_symbol_only_names_do_not_exact_match(self, service):
        candidate = GroundingCandidate(
            entity_uri="http://example.test/Airplane",
            ontology_id="test",
            label="✈️",
            definition="",
            lexical_sim=1.0,
        )

        assert service._exact_name_matches("❤️", [candidate]) == []

    def test_subdivision_flags_do_not_exact_match(self, service):
        candidate = GroundingCandidate(
            entity_uri="http://example.test/Scotland",
            ontology_id="test",
            label=_SCOTLAND_FLAG,
            definition="Scotland flag",
            lexical_sim=1.0,
        )

        assert service._exact_name_matches(_ENGLAND_FLAG, [candidate]) == []

    def test_distinct_sharp_s_words_do_not_exact_match(self, service):
        candidate = GroundingCandidate(
            entity_uri="http://example.test/Masse",
            ontology_id="test",
            label="Masse",
            definition="Mass.",
            lexical_sim=1.0,
        )
        assert service._exact_name_matches("Maße", [candidate]) == []

    def test_percent_encoded_iri_local_name_exact_matches(self, service):
        candidate = GroundingCandidate(
            entity_uri="http://example.test/क%E0%A4%BF",
            ontology_id="test",
            label=_local_name("http://example.test/क%E0%A4%BF"),
            definition="",
            lexical_sim=1.0,
        )
        assert service._exact_name_matches("कि", [candidate]) == [candidate]

    def test_exact_match_auto_grounds_in_standard_mode(self, service, mock_catalog):
        # In STANDARD mode (no LLM), exact-name match auto-grounds
        result = service.ground_table(
            table_name="agreement",
            table_description="insurance agreements",
            columns=[],
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=["fibo-agreements"],
        )
        assert result.match_type == "exact"
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.confidence == 1.0
        assert result.relationship == "exactMatch"
        assert result.reason.startswith("Exact name match")

    def test_exact_match_defers_to_llm_in_enhanced_mode(self, service, mock_catalog):
        # In ENHANCED mode, exact-name matches still go through LLM for domain verification
        llm_response = {
            "choice": "Agreement",
            "relationship": "exactMatch",
            "confidence": 0.95,
            "reason": "Table represents agreements matching FIBO Agreement",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response):
            result = service.ground_table(
                table_name="agreement",
                table_description="insurance agreements",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.mode == "ENHANCED"

    def test_no_exact_match_when_names_differ(self, service, mock_catalog):
        # "policy" doesn't match "Agreement" / "CreditAgreement" / "ContractDocument"
        with patch.object(
            service, "_llm_rerank", return_value={"choice": "NONE", "confidence": 0, "reason": "no match"}
        ):
            result = service.ground_table(
                table_name="policy",
                table_description="insurance policies",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.match_type == "novel"


class TestStandardMode:
    def test_equal_score_duplicate_exact_names_abstain(self, service):
        candidates = [
            GroundingCandidate(
                entity_uri=f"http://ontology-{suffix}.test/Duplicate",
                ontology_id=f"ontology-{suffix}",
                label="Duplicate",
                definition="",
                lexical_sim=0.9,
            )
            for suffix in ("a", "b")
        ]
        with patch.object(service, "_recall", return_value=candidates):
            result = service.ground_table(
                table_name="duplicate",
                table_description="",
                columns=[],
                concept_vector=[0.1],
                model_id="titan",
                grounding_mode="STANDARD",
                ontology_ids=["ontology-a", "ontology-b"],
            )
        assert result.chosen is None
        assert result.match_type == "novel"
        assert "share the top embedding score" in (result.reason or "")

    def test_uses_embedding_score_no_llm(self, service, mock_catalog):
        with patch.object(service, "_llm_rerank") as mock_llm:
            result = service.ground_table(
                table_name="policy",
                table_description="test",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="STANDARD",
                ontology_ids=["fibo-agreements"],
            )
            mock_llm.assert_not_called()
        # Cosine of [0.3]*10 vs [0.5]*10 is < 0.95, check it classified
        assert result.mode == "STANDARD"
        assert result.match_type in ("exact", "high_confidence", "ambiguous", "novel")


class TestEnhancedMode:
    def test_llm_picks_correct_candidate(self, service):
        llm_response = {
            "choice": "Agreement",
            "relationship": "exactMatch",
            "confidence": 0.92,
            "reason": "Table represents agreements matching FIBO Agreement",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response):
            # Use a name that won't trigger exact-name-match (so LLM runs)
            result = service.ground_table(
                table_name="agr_records",
                table_description="insurance agreement records",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        assert result.relationship == "exactMatch"
        assert result.confidence == 0.92
        assert result.mode == "ENHANCED"

    def test_llm_exact_spelling_wins_over_normalized_collision(self, service):
        candidates = [
            GroundingCandidate(
                entity_uri="http://example.test/Hohe",
                ontology_id="test",
                label="Hohe",
                definition="Height without umlaut.",
                lexical_sim=0.9,
            ),
            GroundingCandidate(
                entity_uri="http://example.test/H%C3%B6he",
                ontology_id="test",
                label="Höhe",
                definition="Height.",
                lexical_sim=0.8,
            ),
        ]
        response = {
            "choice": "Höhe",
            "relationship": "exactMatch",
            "confidence": 0.95,
            "reason": "Exact spelling selected",
        }
        with (
            patch.object(service, "_recall", return_value=candidates),
            patch.object(service, "_llm_rerank", return_value=response),
        ):
            result = service.ground_table(
                table_name="height_record",
                table_description="",
                columns=[],
                concept_vector=[0.1],
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["test"],
            )
        assert result.chosen is candidates[1]

    @pytest.mark.parametrize("reverse", [False, True])
    def test_llm_duplicate_exact_choice_abstains_independent_of_recall_order(self, service, reverse: bool):
        candidates = [
            GroundingCandidate(
                entity_uri="http://ontology-a.test/Duplicate",
                ontology_id="ontology-a",
                label="Duplicate",
                definition="First domain.",
                lexical_sim=0.9,
            ),
            GroundingCandidate(
                entity_uri="http://ontology-b.test/Duplicate",
                ontology_id="ontology-b",
                label="Duplicate",
                definition="Second domain.",
                lexical_sim=0.8,
            ),
        ]
        if reverse:
            candidates.reverse()
        response = {
            "choice": "Duplicate",
            "relationship": "exactMatch",
            "confidence": 0.95,
            "reason": "Selected by local name",
        }
        with (
            patch.object(service, "_recall", return_value=candidates),
            patch.object(service, "_llm_rerank", return_value=response),
        ):
            result = service.ground_table(
                table_name="source_record",
                table_description="",
                columns=[],
                concept_vector=[0.1],
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["ontology-a", "ontology-b"],
            )
        assert result.chosen is None
        assert result.match_type == "novel"
        assert "did not uniquely identify" in (result.reason or "")
        assert "http://ontology-a.test/Duplicate" in (result.reason or "")
        assert "http://ontology-b.test/Duplicate" in (result.reason or "")

    def test_llm_abstains(self, service):
        llm_response = {
            "choice": "NONE",
            "relationship": "none",
            "confidence": 0.0,
            "reason": "No candidate matches insurance claims",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response):
            result = service.ground_table(
                table_name="claim",
                table_description="insurance claims",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.match_type == "novel"
        assert result.chosen is None
        assert "No candidate matches" in result.reason

    def test_llm_rerank_failure_propagates_not_novel(self, service):
        # A rerank INFRASTRUCTURE failure raises GroundingRerankError; it must
        # propagate so the induction job fails loud, NOT degrade to a silent
        # novel result (the enhanced-grounding defect, issue #59). Contrast
        # test_llm_abstains above: a genuine {"choice": "NONE"} abstention still
        # yields novel.
        with (
            patch.object(service, "_llm_rerank", side_effect=GroundingRerankError("bedrock exploded")),
            pytest.raises(GroundingRerankError),
        ):
            service.ground_table(
                table_name="occurrence",
                table_description="events",
                columns=[],
                concept_vector=[0.3] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )


class TestToConceptMatch:
    def test_conversion(self, service):
        candidate = GroundingCandidate(
            entity_uri="http://fibo/Agreement",
            ontology_id="fibo-agreements",
            label="Agreement",
            definition="a negotiated understanding",
            lexical_sim=0.51,
            rerank_score=0.92,
        )
        result = GroundingResult(
            source_table="agreement",
            source_column="",
            candidates=[candidate],
            chosen=candidate,
            match_type="exact",
            relationship="exactMatch",
            confidence=0.92,
            reason="direct match",
            mode="ENHANCED",
        )
        cm = service.to_concept_match(result)
        assert cm.source_table == "agreement"
        assert cm.matched_class_uri == "http://fibo/Agreement"
        assert cm.match_type == "exact"
        assert cm.similarity == 0.92
        assert cm.scoring_strategy == "grounding_enhanced"
        assert len(cm.candidates) == 1


class TestNoCandidates:
    def test_empty_search_returns_novel(self, service, mock_catalog):
        mock_catalog.search_embeddings.return_value = []
        result = service.ground_table(
            table_name="xyz",
            table_description="",
            columns=[],
            concept_vector=[0.1] * 10,
            model_id="titan",
            grounding_mode="ENHANCED",
        )
        assert result.match_type == "novel"
        assert "No candidates" in result.reason


class TestEmptyGroundingScope:
    """An empty ``ontology_ids`` pool (``[]`` or ``None``) means the caller
    resolved a grounding pool and it is empty (user picked no ontologies and the
    namespace has no accepted induced ontologies). Recall must return no
    candidates — NOT fall back to a namespace-wide search that would match a
    loaded-but-unselected ontology. There is no unscoped/global recall.
    """

    def test_empty_list_returns_novel_without_searching(self, service, mock_catalog):
        result = service.ground_table(
            table_name="agreement",
            table_description="insurance agreements",
            columns=[],
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=[],  # explicit empty scope
        )
        assert result.match_type == "novel"
        # The empty-scope guard short-circuits before any embedding search.
        mock_catalog.search_embeddings.assert_not_called()

    def test_empty_list_on_ground_class_returns_novel(self, service, mock_catalog):
        result = service.ground_class(
            class_label="agreement",
            class_description="a mutual understanding",
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=[],
        )
        assert result.match_type == "novel"
        mock_catalog.search_embeddings.assert_not_called()

    def test_none_treated_as_empty_scope_returns_novel(self, service, mock_catalog):
        # ``None`` is an empty grounding pool, identical to ``[]``: recall
        # returns no candidates and the subject is novel. There is no
        # unscoped/global recall to fall back to.
        result = service.ground_table(
            table_name="agreement",
            table_description="insurance agreements",
            columns=[],
            concept_vector=[0.5] * 10,
            model_id="titan",
            grounding_mode="STANDARD",
            ontology_ids=None,
        )
        assert result.match_type == "novel"
        mock_catalog.search_embeddings.assert_not_called()


class TestGroundClass:
    """ground_class() shares the ground_table() pipeline but frames the LLM
    reranker as class-vs-class (used by the unstructured induction path)."""

    def test_grounds_a_class_via_shared_pipeline(self, service, mock_catalog):
        llm_response = {
            "choice": "Agreement",
            "relationship": "exactMatch",
            "confidence": 0.95,
            "reason": "The class IS a FIBO Agreement",
        }
        with patch.object(service, "_llm_rerank", return_value=llm_response) as mock_rerank:
            result = service.ground_class(
                class_label="agreement",
                class_description="a mutual understanding between parties",
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert result.chosen is not None
        assert result.chosen.entity_uri == "http://fibo/Agreement"
        # ground_class must pass the class-flavored system prompt + subject kind.
        _, kwargs = mock_rerank.call_args
        assert kwargs["subject_kind"] == "class"
        assert "source ontology class" in kwargs["rerank_system"].lower()
        # And it always calls with empty columns (a class has none).
        called_columns = mock_rerank.call_args[0][2]
        assert called_columns == []

    def test_none_mode_short_circuits(self, service):
        result = service.ground_class(
            class_label="agreement",
            class_description="",
            concept_vector=[0.1] * 10,
            model_id="titan",
            grounding_mode="NONE",
        )
        assert result.match_type == "novel"

    def test_class_rerank_prompt_omits_columns_section(self, service, mock_catalog):
        # Drive the REAL _llm_rerank (patching only bedrock.converse) to verify
        # the class prompt has no "Columns:" block and labels the source a CLASS.
        captured = {}

        def fake_converse(**kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"][0]["text"]
            captured["system"] = kwargs["system"][0]["text"]
            return {
                "output": {
                    "message": {
                        "content": [
                            {"text": '{"choice": "NONE", "relationship": "none", "confidence": 0.0, "reason": "x"}'}
                        ]
                    }
                }
            }

        with patch.object(type(service), "bedrock", property(lambda self: MagicMock(converse=fake_converse))):
            service.ground_class(
                class_label="agreement",
                class_description="a mutual understanding",
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert "SOURCE CLASS:" in captured["prompt"]
        assert "Columns:" not in captured["prompt"]
        assert "source ontology class" in captured["system"].lower()


def _fake_bedrock_converse(usage: dict, choice: str = "NONE"):
    """A fake bedrock client whose converse returns a valid JSON body + usage."""
    body = f'{{"choice": "{choice}", "relationship": "none", "confidence": 0.0, "reason": "x"}}'

    def converse(**_kwargs):
        return {"output": {"message": {"content": [{"text": body}]}}, "usage": usage}

    return MagicMock(converse=converse)


class TestCostTrackerIntegration:
    """The rerank converse call records token usage when a CostTracker is
    injected, and is a no-op (no error) when the tracker is None."""

    def test_rerank_records_tokens_when_tracker_present(self, mock_catalog, mock_eg):
        from coa_common.bedrock_metrics import CostTracker

        tracker = CostTracker()
        service = GroundingService(
            ontology_catalog=mock_catalog,
            embedding_generator=mock_eg,
            llm_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            cost_tracker=tracker,
        )
        fake = _fake_bedrock_converse({"inputTokens": 42, "outputTokens": 13})
        with patch.object(type(service), "bedrock", property(lambda self: fake)):
            # Scope grounding to a non-empty pool so recall returns candidates, and
            # use a name that does NOT exact-match a candidate so the LLM rerank runs.
            service.ground_table(
                table_name="agr_records",
                table_description="insurance agreement records",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        assert tracker.input_tokens_by_stage == {"rerank": 42}
        assert tracker.output_tokens_by_stage == {"rerank": 13}
        assert tracker.rerank_invocations == 1
        # rerank latency is captured (a StatisticSet with one sample).
        stats = tracker.rerank_latency_stats
        assert stats is not None and stats["SampleCount"] == 1.0

    def test_no_tracker_records_nothing_and_does_not_error(self, mock_catalog, mock_eg):
        service = GroundingService(
            ontology_catalog=mock_catalog,
            embedding_generator=mock_eg,
            cost_tracker=None,
        )
        fake = _fake_bedrock_converse({"inputTokens": 42, "outputTokens": 13})
        with patch.object(type(service), "bedrock", property(lambda self: fake)):
            result = service.ground_table(
                table_name="agr_records",
                table_description="insurance agreement records",
                columns=[],
                concept_vector=[0.5] * 10,
                model_id="titan",
                grounding_mode="ENHANCED",
                ontology_ids=["fibo-agreements"],
            )
        # LLM abstained (choice NONE) → novel, and no crash without a tracker.
        assert result.match_type == "novel"
