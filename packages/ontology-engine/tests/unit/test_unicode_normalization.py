# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Direct unit tests for the shared Unicode identity helpers.

The four helpers are also exercised through ``normalize_predicate``,
``_normalize_name_key`` and the structured path's ``to_pascal``, but those
callers compose them and then strip punctuation or capitalize, so a
helper-level regression can hide behind a caller-level assertion. Every case
below pins one helper's own contract, in particular the degenerate inputs that
never reach a caller with real table metadata: an empty string, a value that is
nothing but combining marks, a percent escape whose bytes are not UTF-8, and
format characters with no base to attach to.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Callable

import pytest
from coa_ontology.inducer.services.unicode_normalization import (
    filter_letters_marks_numbers,
    simple_case_identity,
    strict_percent_decode,
    width_only_nfkc,
)

pytestmark = pytest.mark.unit

# England and Scotland differ only in their TAG characters; the base emoji and
# the terminating CANCEL TAG are shared.
_ENGLAND_FLAG = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
_ORPHAN_TAGS = "\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"


def test_runtime_unicode_version_is_pinned() -> None:
    """NFC/NFKC still come from the runtime's Unicode character database.

    Category membership is read from the generated Unicode 15.0 ranges, but
    ``width_only_nfkc`` and the structured path's final NFC pass call
    :func:`unicodedata.normalize`. A Python upgrade that ships a newer Unicode
    release could recompose differently and so move an IRI. Bumping this pin
    must be a reviewed decision.
    """
    assert unicodedata.unidata_version == "15.0.0"


class TestEmptyInput:
    """Every helper must accept the empty string without raising."""

    @pytest.mark.parametrize(
        "normalize",
        [width_only_nfkc, simple_case_identity, filter_letters_marks_numbers, strict_percent_decode],
        ids=["width_only_nfkc", "simple_case_identity", "filter_letters_marks_numbers", "strict_percent_decode"],
    )
    def test_empty_string_round_trips(self, normalize: Callable[[str], str]) -> None:
        """A column with a blank name reaches these helpers before validation."""
        assert normalize("") == ""


class TestWidthOnlyNfkc:
    """Only U+FFxx width forms fold, and exceptional targets stop at one step."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("￣", "¯", id="fullwidth-macron-to-macron"),
            pytest.param("ﾠ", "ㅤ", id="halfwidth-hangul-filler"),
            pytest.param("ﾡ", "ㄱ", id="halfwidth-kiyeok-to-compatibility-jamo"),
            pytest.param("ￂ", "ㅏ", id="halfwidth-a-to-compatibility-jamo"),
            pytest.param("ￊ", "ㅕ", id="halfwidth-yeo-to-compatibility-jamo"),
            pytest.param("ￒ", "ㅛ", id="halfwidth-yo-to-compatibility-jamo"),
            pytest.param("ￚ", "ㅡ", id="halfwidth-eu-to-compatibility-jamo"),
        ],
    )
    def test_exceptional_targets_stop_at_the_direct_mapping(self, value: str, expected: str) -> None:
        """These are the code points where plain NFKC overshoots.

        NFKC decomposes FULLWIDTH MACRON into SPACE plus COMBINING MACRON, which
        the subsequent letter/mark filter would then discard as an orphan mark,
        and it maps halfwidth Hangul to conjoining Jamo that compose with a
        neighbouring letter into a different syllable. Both would change
        identity, so the fold stops one step earlier.
        """
        assert width_only_nfkc(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("￣", "¯", id="fullwidth-macron"),
            pytest.param("ﾡ", "ㄱ", id="halfwidth-kiyeok"),
        ],
    )
    def test_exceptional_targets_differ_from_plain_nfkc(self, value: str, expected: str) -> None:
        """Pin the divergence itself, so a rewrite to plain NFKC fails loudly."""
        assert unicodedata.normalize("NFKC", value) != expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("Ａ", "A", id="fullwidth-latin-capital"),
            pytest.param("￠", "¢", id="fullwidth-cent-sign"),
            pytest.param("顧客ＩＤ", "顧客ID", id="fullwidth-ascii-in-a-japanese-name"),
            pytest.param("ｶﾞ", "ガ", id="halfwidth-ka-plus-dakuten-composes"),
        ],
    )
    def test_ordinary_width_forms_use_nfkc(self, value: str, expected: str) -> None:
        """The remaining U+FFxx block folds by NFKC, then NFC recomposes marks."""
        assert width_only_nfkc(value) == expected

    @pytest.mark.parametrize("value", ["℃", "™", "ﬁ"])
    def test_compatibility_forms_outside_the_width_block_survive(self, value: str) -> None:
        """DEGREE CELSIUS, TRADE MARK, and the fi ligature are not width forms.

        Unrestricted NFKC would expand them into ordinary text and merge
        ``temperature ℃`` with ``temperature °C``.
        """
        assert width_only_nfkc(value) == value

    def test_ascii_is_unchanged(self) -> None:
        ascii_chars = "".join(chr(c) for c in range(128))
        assert width_only_nfkc(ascii_chars) == ascii_chars


class TestSimpleCaseIdentity:
    """Lowercase, plus exactly one contextual fold for Greek final sigma."""

    def test_final_sigma_shares_sigma_identity(self) -> None:
        """``ς`` is a positional variant of ``σ``, not a separate letter."""
        assert simple_case_identity("Σ") == simple_case_identity("ς") == "σ"

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("Maße", "Masse", id="eszett-vs-ss"),
            pytest.param("µ", "Μ", id="micro-sign-vs-greek-mu"),
            pytest.param("ﬁle", "file", id="fi-ligature-vs-letters"),
        ],
    )
    def test_full_case_folding_substitutions_are_not_applied(self, left: str, right: str) -> None:
        """``str.casefold`` would merge these distinct persisted identities."""
        assert simple_case_identity(left) != simple_case_identity(right)
        assert left.casefold() == right.casefold()

    def test_capital_eszett_lowercases_to_eszett(self) -> None:
        """U+1E9E is an ordinary uppercase pair, so plain lowercase suffices."""
        assert simple_case_identity("MAẞE") == "maße"


class TestFilterLettersMarksNumbers:
    """Marks and format characters are kept only behind a retained base."""

    @pytest.mark.parametrize("value", ["設備id", "メールアドレス", "고객이름", "Название", "कर्मचारी", "อุณหภูมิ"])
    def test_letters_numbers_and_attached_marks_are_retained(self, value: str) -> None:
        assert filter_letters_marks_numbers(value) == value

    def test_ascii_keeps_exactly_alphanumerics(self) -> None:
        ascii_chars = "".join(chr(c) for c in range(128))
        expected = "".join(c for c in ascii_chars if c.isascii() and c.isalnum())
        assert filter_letters_marks_numbers(ascii_chars) == expected
        assert filter_letters_marks_numbers(ascii_chars, keep_underscore=True) == "".join(
            c for c in ascii_chars if c.isalnum() or c == "_"
        )

    def test_symbols_and_punctuation_are_dropped(self) -> None:
        assert filter_letters_marks_numbers("温度℃・（有効）%") == "温度有効"

    def test_a_value_of_only_marks_becomes_empty(self) -> None:
        """Three orphan combining marks have no base, so nothing is retained.

        Retaining them would make every mark-only name collapse to the same
        format-only key, which is exactly the collision the filter exists to
        prevent. The caller sees an empty key and rejects the name instead.
        """
        assert filter_letters_marks_numbers("\u0300\u0301\u0302") == ""

    def test_a_mark_behind_a_letter_is_retained(self) -> None:
        """An attached mark carries lexical meaning in many scripts."""
        assert filter_letters_marks_numbers("a\u0300") == "a\u0300"

    def test_orphan_tag_characters_are_discarded(self) -> None:
        """TAG characters only mean anything as a suffix of a base emoji."""
        assert filter_letters_marks_numbers(_ORPHAN_TAGS, keep_symbols=True) == ""

    def test_tag_characters_behind_a_retained_base_survive(self) -> None:
        """The subdivision flag is one grapheme; dropping its tags merges flags."""
        assert filter_letters_marks_numbers(_ENGLAND_FLAG, keep_symbols=True) == _ENGLAND_FLAG

    def test_tag_characters_go_with_a_base_that_was_not_retained(self) -> None:
        """Without ``keep_symbols`` the flag's base is dropped, so its tags are too.

        Half a flag sequence is not a meaningful key, so the whole grapheme has
        to leave together.
        """
        assert filter_letters_marks_numbers(_ENGLAND_FLAG) == ""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("\u200d", "", id="orphan-zwj"),
            pytest.param("\u200c", "", id="orphan-zwnj"),
            pytest.param("a\u200db", "a\u200db", id="zwj-between-letters"),
        ],
    )
    def test_join_controls_need_a_retained_base(self, value: str, expected: str) -> None:
        """ZWJ/ZWNJ distinguish joined graphemes but carry nothing on their own."""
        assert filter_letters_marks_numbers(value) == expected

    def test_symbols_are_retained_only_when_requested(self) -> None:
        """``keep_symbols`` is what separates label identity from property keys."""
        assert filter_letters_marks_numbers("℃", keep_symbols=True) == "℃"
        assert filter_letters_marks_numbers("℃") == ""

    def test_underscore_is_not_a_base_for_an_attached_mark(self) -> None:
        """``keep_underscore`` retains the separator without making it a base.

        A mark directly after a retained underscore is still orphaned, because
        an underscore is a separator rather than an identity-bearing character.
        Asserting it keeps the ``has_base`` reset on the underscore branch from
        being read as an oversight and 'fixed'.
        """
        assert filter_letters_marks_numbers("a_\u0300", keep_underscore=True) == "a_"


class TestStrictPercentDecode:
    """Decode only when every escape and every decoded byte is valid."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("A%20B", "A B", id="valid-space"),
            pytest.param("%E3%81%82", "あ", id="valid-multibyte-utf8"),
            pytest.param("a%2Fb", "a/b", id="valid-reserved-character"),
            pytest.param("%2541", "%41", id="single-decoding-pass-only"),
        ],
    )
    def test_fully_valid_values_decode(self, value: str, expected: str) -> None:
        """A well-formed value decodes exactly once, like ``decodeURIComponent``."""
        assert strict_percent_decode(value) == expected

    def test_invalid_utf8_bytes_are_left_encoded(self) -> None:
        """``%FF%FE`` is syntactically valid but decodes to no UTF-8 sequence.

        ``unquote`` in its default mode would replace the bytes with U+FFFD,
        which silently merges distinct malformed names; the browser rejects the
        whole value. Returning the original keeps both runtimes agreeing.
        """
        assert strict_percent_decode("%FF%FE") == "%FF%FE"

    @pytest.mark.parametrize(
        "value",
        ["A%20B%ZZ", "100%", "%2", "%GG", "trailing%"],
        ids=["non-hex-escape", "bare-percent", "truncated-escape", "non-hex-pair", "trailing-percent"],
    )
    def test_a_malformed_escape_rejects_the_whole_value(self, value: str) -> None:
        """``unquote`` decodes the valid escapes and keeps the broken ones.

        That partial result is the divergence from ``decodeURIComponent``, so
        the syntax check runs before any decoding.
        """
        assert strict_percent_decode(value) == value

    def test_a_value_with_no_escapes_is_unchanged(self) -> None:
        """The common case must not depend on the decoder at all."""
        assert strict_percent_decode("plain_name") == "plain_name"

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            pytest.param("%FF%FE", "invalid UTF-8", id="invalid-utf8"),
            pytest.param("A%20B%ZZ", "malformed", id="malformed-escape"),
        ],
    )
    def test_preserved_values_emit_bounded_debug_diagnostics(
        self,
        value: str,
        reason: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(
            logging.DEBUG,
            logger="coa_ontology.inducer.services.unicode_normalization",
        ):
            assert strict_percent_decode(value) == value

        assert reason in caplog.text
        assert "sha256=" in caplog.text
        assert value not in caplog.text
