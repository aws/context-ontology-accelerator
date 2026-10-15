# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for predicate normalization and XSD type inference.

Requirements: 19.1–19.4, 18.1–18.8
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.normalizers import (
    infer_xsd_type,
    normalize_predicate,
    select_canonical_label,
)


class TestNormalizePredicate:
    """Test predicate normalization."""

    def test_uppercase_to_lowercase(self):
        assert normalize_predicate("OPERATES_IN") == "operates_in"

    def test_mixed_case(self):
        assert normalize_predicate("Works_For") == "works_for"

    def test_spaces_to_underscores(self):
        assert normalize_predicate("works for") == "works_for"

    def test_all_caps_with_underscores(self):
        assert normalize_predicate("PART_OF") == "part_of"

    def test_strips_special_chars(self):
        assert normalize_predicate("has-age!") == "hasage"

    def test_collapses_consecutive_underscores(self):
        assert normalize_predicate("a__b___c") == "a_b_c"

    def test_strips_leading_trailing_underscores(self):
        assert normalize_predicate("_hello_") == "hello"
        assert normalize_predicate("___test___") == "test"

    def test_spaces_and_special_combined(self):
        assert normalize_predicate("HAS POLICY (v2)") == "has_policy_v2"

    def test_idempotent(self):
        """normalize_predicate(normalize_predicate(s)) == normalize_predicate(s)"""
        cases = [
            "OPERATES_IN",
            "works for",
            "HAS_POLICY",
            "PART OF",
            "a - b",
            "ABBREVIATION_OF",
        ]
        for s in cases:
            once = normalize_predicate(s)
            twice = normalize_predicate(once)
            assert once == twice, f"Not idempotent for {s!r}: {once!r} != {twice!r}"

    def test_empty_string(self):
        assert normalize_predicate("") == ""

    def test_symbol_survives_when_punctuation_is_removed(self):
        assert normalize_predicate("@#$%") == "$"

    def test_numbers_preserved(self):
        assert normalize_predicate("version_2") == "version_2"

    def test_real_graph_predicates(self):
        """Test with actual predicates from the dev graph."""
        assert normalize_predicate("ABBREVIATION_OF") == "abbreviation_of"
        assert normalize_predicate("ACCESSED_VIA") == "accessed_via"
        assert normalize_predicate("ACCOUNTED_AS") == "accounted_as"
        assert normalize_predicate("CEASED OPERATIONS IN") == "ceased_operations_in"


class TestNormalizePredicateIsScriptAgnostic:
    """A predicate must survive normalization in every script, not just Latin.

    The previous implementation stripped by ASCII range (``[^a-z0-9_]``), which
    failed in two different ways depending on the script — and only one of them
    was noisy. Every test here fails against that implementation.

    Nothing about the IRI required it: the normalized predicate is handed to
    ``iri_resolver.resolve``, which builds an IRI (RFC 3987), where a Unicode
    letter is legal in a path segment and passes through unencoded.
    """

    @pytest.mark.parametrize(
        ("predicate", "expected"),
        [
            pytest.param("設置場所", "設置場所", id="ja-kanji"),
            pytest.param("メーカー", "メーカー", id="ja-katakana"),
            pytest.param("対応機種", "対応機種", id="ja-compound"),
            pytest.param("安装位置", "安装位置", id="zh-hans"),
            pytest.param("제조사", "제조사", id="ko-hangul"),
            pytest.param("ИЗГОТОВИТЕЛЬ", "изготовитель", id="ru-cyrillic"),
        ],
    )
    def test_a_non_latin_predicate_is_not_normalized_away(self, predicate, expected):
        """It normalized to the empty string, and the caller then dropped the fact.

        ``induce_object_properties`` cannot mint an IRI from an empty string, so
        it logged ``skipped_empty_predicate`` and discarded the fact — meaning
        every predicate of a non-Latin-script corpus was lost. Loud, at least,
        unlike the collision below.
        """
        assert normalize_predicate(predicate) == expected

    @pytest.mark.parametrize(
        ("predicate", "expected"),
        [
            pytest.param("GRÖSSE", "grösse", id="de-size"),
            pytest.param("HÖHE", "höhe", id="de-height"),
            pytest.param("FÜR", "für", id="de-for"),
            pytest.param("MODÈLE", "modèle", id="fr-model"),
            pytest.param("SITUÉ", "situé", id="fr-located"),
            pytest.param("TAMAÑO", "tamaño", id="es-size"),
            pytest.param("AÇÃO", "ação", id="pt-action"),
            pytest.param("ÓRGÃO", "órgão", id="pt-body"),
            pytest.param("ǰ", "ǰ", id="precomposed-caron"),
        ],
    )
    def test_a_latin_predicate_keeps_its_diacritics(self, predicate, expected):
        """The silent half of the defect, and the reason this is not a CJK fix.

        Stripping by ASCII range removed the accented letter itself rather than
        just its case, so a Latin-script predicate came out misspelled with no
        warning: ``HÖHE`` → ``hhe``, ``FÜR`` → ``fr``, ``MODÈLE`` → ``modle``,
        ``ÓRGÃO`` → ``rgo``. The property IRI and its label both carried the
        mangled form. Unlike the non-Latin case, which normalized to empty and
        was at least logged, this looked like a spelling the corpus had used.

        This is why the defect is general rather than a CJK one: it hit
        de / fr / es / pt-BR, whose output is plausible enough that nobody
        re-reads it.
        """
        assert normalize_predicate(predicate) == expected

    def test_stripping_accents_could_merge_two_distinct_predicates(self):
        """Worst case of the same bug: two real predicates became one property.

        Spanish ``AÑO`` (year) and Portuguese ``AÇÃO`` (action) both normalized to
        ``ao``, so the aggregation key collided: one property IRI, one surviving
        ``rdfs:label``, and the other predicate's facts silently folded into it.
        A cross-language corpus is not exotic — a spec sheet and its translated
        annex are one document set.
        """
        assert normalize_predicate("AÑO") != normalize_predicate("AÇÃO")

    def test_lowercasing_does_not_merge_distinct_german_predicates(self):
        """A persisted identity key must not full-casefold ``ß`` to ``ss``."""
        assert normalize_predicate("Maße") != normalize_predicate("Masse")
        assert normalize_predicate("MAẞE") == normalize_predicate("Maße")

    def test_simple_case_identity_unifies_all_sigma_forms(self):
        """One-code-point folds unify ordinary and final Greek sigma."""
        assert normalize_predicate("Σ") == normalize_predicate("σ") == normalize_predicate("ς")
        assert normalize_predicate("ΟΣ") == normalize_predicate("οσ") == normalize_predicate("ος")

    def test_ideographic_space_separates_words(self):
        """U+3000 is an ordinary word separator in Japanese and Chinese.

        ``str.replace(" ", "_")`` only knew about U+0020, so an ideographic space
        was stripped as punctuation and two words were run together.
        """
        assert normalize_predicate("設置　場所") == "設置_場所"

    @pytest.mark.parametrize(
        ("predicate", "expected"),
        [
            pytest.param("ﾒｰｶｰ", "メーカー", id="halfwidth-katakana"),
            pytest.param("ｶﾞｲﾄﾞ･名", "ガイド名", id="dakuten-and-middle-dot"),
            pytest.param("ﾊﾟｰﾄ", "パート", id="handakuten"),
        ],
    )
    def test_nfkc_unifies_halfwidth_katakana(self, predicate, expected):
        """Compatibility-equivalent width forms must share one property key.

        NFKC runs before filtering so a base plus dakuten/handakuten composes,
        while punctuation such as the halfwidth middle dot is still removed.
        """
        assert normalize_predicate(predicate) == expected
        assert normalize_predicate(expected) == expected

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("कि", "की", id="devanagari-vowel-signs"),
            pytest.param("กิ", "กี", id="thai-vowel-signs"),
        ],
    )
    def test_combining_marks_keep_predicates_distinct(self, left: str, right: str) -> None:
        """Script vowel marks carry lexical meaning and must survive filtering."""
        assert normalize_predicate(left) == left
        assert normalize_predicate(right) == right
        assert normalize_predicate(left) != normalize_predicate(right)

    @pytest.mark.parametrize("predicate", ["❤️", "✈️", "☀️", "⚙️", "℃", "™"])
    def test_symbols_and_attached_variation_selectors_are_preserved(self, predicate: str) -> None:
        """Symbols remain literal and are never compatibility-expanded to text."""
        assert normalize_predicate(predicate) == predicate

    def test_subdivision_flag_tag_sequences_remain_distinct(self) -> None:
        """Attached TAG characters distinguish Unicode subdivision flags."""
        england = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
        scotland = "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"

        assert normalize_predicate(england) == england
        assert normalize_predicate(scotland) == scotland
        assert normalize_predicate(england) != normalize_predicate(scotland)

    def test_compatibility_symbols_do_not_merge_with_text(self) -> None:
        assert normalize_predicate("℃") != normalize_predicate("C")
        assert normalize_predicate("™") != normalize_predicate("TM")

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("A￣B", "A¯B", id="fullwidth-macron"),
            pytest.param("ﾡￂ", "ㄱㅏ", id="halfwidth-hangul"),
            pytest.param("HO\u0308HE", "HÖHE", id="canonical-equivalence"),
        ],
    )
    def test_width_and_canonical_variants_share_predicate_identity(self, left: str, right: str) -> None:
        assert normalize_predicate(left) == normalize_predicate(right)

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
    def test_distinct_unicode_identities_do_not_merge(self, left: str, right: str) -> None:
        assert normalize_predicate(left) != normalize_predicate(right)

    def test_enclosing_mark_attached_to_a_letter_is_retained(self) -> None:
        assert normalize_predicate("क⃝") == "क⃝"

    def test_punctuation_separated_hangul_jamo_is_idempotent(self) -> None:
        """Removing punctuation must recompose adjacent Jamo in the same pass."""
        once = normalize_predicate("ᄀ-ᅡ")
        assert once == "가"
        assert normalize_predicate(once) == once

    @pytest.mark.parametrize(
        "predicate",
        ["設置場所", "GRÖSSE", "AÇÃO", "ǰ", "제조사", "設置　場所", "OPERATES_IN"],
    )
    def test_still_idempotent_outside_ascii(self, predicate):
        """The aggregation key is the normalized form, so a second pass must be a no-op.

        If normalization were not idempotent outside ASCII, re-normalizing an
        already-normalized predicate would mint a second property for the same
        relationship.
        """
        once = normalize_predicate(predicate)
        assert normalize_predicate(once) == once

    def test_punctuation_is_still_stripped_in_any_script(self):
        """Widening to Unicode letters must not start admitting punctuation.

        Full-width punctuation is punctuation, not a letter — the change is
        letters-and-digits-in-every-script, not keep-everything.
        """
        assert normalize_predicate("設置場所（屋内）") == "設置場所屋内"
        assert normalize_predicate("año!") == "año"


class TestInferXsdType:
    """Test XSD type inference."""

    def test_all_integers(self):
        assert infer_xsd_type(["42", "7", "100", "-5"]) == "xsd:integer"

    def test_all_decimals(self):
        assert infer_xsd_type(["3.14", "2.71", "0.5"]) == "xsd:decimal"

    def test_all_booleans(self):
        assert infer_xsd_type(["true", "false", "True", "FALSE"]) == "xsd:boolean"

    def test_all_dates(self):
        assert infer_xsd_type(["2023-04-30", "2024-01-15"]) == "xsd:date"

    def test_all_datetimes(self):
        assert infer_xsd_type(["2023-04-30T10:00:00Z", "2024-01-15T08:30:00+05:00"]) == "xsd:dateTime"

    def test_datetime_without_seconds(self):
        assert infer_xsd_type(["2023-04-30T10:00Z"]) == "xsd:dateTime"

    def test_mixed_types_returns_string(self):
        assert infer_xsd_type(["42", "hello", "true"]) == "xsd:string"

    def test_mixed_integer_and_decimal(self):
        assert infer_xsd_type(["42", "3.14"]) == "xsd:string"

    def test_empty_list(self):
        assert infer_xsd_type([]) == "xsd:string"

    def test_all_empty_strings(self):
        assert infer_xsd_type(["", "  ", ""]) == "xsd:string"

    def test_natural_language_is_string(self):
        """Real fact values from the dev graph should be xsd:string."""
        assert (
            infer_xsd_type(
                [
                    "7.27 billion",
                    "April 30, 2023",
                    "44%",
                    "$6,291 thousand",
                ]
            )
            == "xsd:string"
        )

    def test_single_value(self):
        assert infer_xsd_type(["42"]) == "xsd:integer"

    def test_positive_sign_integer(self):
        assert infer_xsd_type(["+42"]) == "xsd:integer"

    def test_whitespace_stripped(self):
        assert infer_xsd_type(["  42  ", " 7 "]) == "xsd:integer"


class TestSelectCanonicalLabel:
    """Test canonical label selection."""

    def test_picks_highest_count(self):
        assert select_canonical_label({"OPERATES_IN": 5, "operates_in": 2}) == "OPERATES_IN"

    def test_breaks_ties_lexicographically(self):
        # Same count → pick lexicographically smallest
        assert select_canonical_label({"beta": 3, "alpha": 3}) == "alpha"

    def test_single_entry(self):
        assert select_canonical_label({"PART_OF": 10}) == "PART_OF"

    def test_raises_on_empty(self):
        with pytest.raises(ValueError, match="empty"):
            select_canonical_label({})

    def test_many_variants(self):
        originals = {
            "OPERATES IN": 1,
            "OPERATES_IN": 10,
            "operates_in": 5,
            "Operates In": 2,
        }
        assert select_canonical_label(originals) == "OPERATES_IN"


@pytest.mark.unit
class TestNormalizePredicateJapanese:
    """Japanese predicates keep their letters in every namespace.

    Namespace-language LLD 2.2.6: IRIs minted by unstructured induction keep
    non-ASCII letters. Since !922 the normalizer is the same for every
    namespace language, so these hold for an ``en`` namespace too.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("製造する", "製造する"),
            ("PLCを 制御する", "plcを_制御する"),
            ("設置・運用（予定）", "設置運用予定"),
            # "か" + combining voiced sound mark → precomposed "が" (NFC).
            ("\u304b\u3099", "\u304c"),
            ("OPERATES_IN", "operates_in"),
        ],
    )
    def test_japanese_predicate_keeps_its_letters(self, raw, expected):
        assert normalize_predicate(raw) == expected

    def test_idempotent_on_japanese(self):
        once = normalize_predicate("製造 する")
        assert once == "製造_する"
        assert normalize_predicate(once) == once
