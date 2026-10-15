# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compatibility vectors for ``languageTagCompatibilityV1`` (LLD section 2.2.4)."""

from __future__ import annotations

import pytest
from coa_ontology.inducer.services.literal_language import (
    catalog_literal,
    compatible_language_tag,
    generated_literal,
)
from rdflib import XSD, Literal

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "namespace_language", "expected"),
    [
        # Japanese: Han, Hiragana and Katakana are evidence and permitted.
        ("制御盤", "ja", "ja"),
        ("ひらがな", "ja", "ja"),
        ("カタカナ", "ja", "ja"),
        ("ｶﾀｶﾅ", "ja", "ja"),
        ("設備の点検記録", "ja", "ja"),
        ("サーバー", "ja", "ja"),  # prolonged sound mark is Katakana
        ("人々", "ja", "ja"),  # iteration mark is Han
        # Digits, punctuation and spaces are script-neutral.
        ("2024年度の売上（税込）", "ja", "ja"),
        ("第３四半期・売上", "ja", "ja"),
        # Canonical forms of reviewed entries; the canonical tag is returned.
        ("制御盤", "ja-JP", "ja-JP"),
        ("制御盤", "ja-Jpan", "ja-Jpan"),
        ("制御盤", "ja-Jpan-JP", "ja-Jpan-JP"),
        ("制御盤", " JA-jp ", "ja-JP"),
        # Latin is not permitted for ja: an embedded identifier leaves it untagged.
        ("PLC制御盤", "ja", None),
        ("ＰＬＣ制御盤", "ja", None),  # fullwidth Latin is Latin
        ("制御盤 model X-100", "ja", None),
        # No positive evidence: ASCII identifiers, units, codes, digits.
        ("customer_id", "ja", None),
        ("kWh", "ja", None),
        ("2024-01", "ja", None),
        ("", "ja", None),
        ("   ", "ja", None),
        # A strong script incompatible with ja.
        ("제어반", "ja", None),
        ("Привет", "ja", None),
        # Mismatched script qualifiers are never tagged.
        ("制御盤", "ja-Latn", None),
        ("制御盤", "ja-Hani", None),
        # Korean: Hangul is required; Han is permitted alongside it.
        ("제어반", "ko", "ko"),
        ("제어반", "ko-KR", "ko-KR"),
        ("제어반", "ko-Kore", "ko-Kore"),
        ("韓國 제어반", "ko", "ko"),
        ("韓國", "ko", None),  # Han alone is not Korean evidence
        ("カタカナ", "ko", None),
        ("제어반", "ko-Latn", None),
        ("PLC 제어반", "ko", None),
        # Chinese: exactly zh, Han only.
        ("控制柜", "zh", "zh"),
        ("控制柜", "zh-Hans", None),  # the script qualifier cannot be inferred
        ("控制櫃", "zh-Hant", None),
        ("控制柜", "zh-CN", None),
        ("控制柜", "zh-Hans-CN", None),
        ("カタカナ", "zh", None),
        # English namespaces assert no new tags, whatever the text.
        ("Purchase orders", "en", None),
        ("Purchase orders", "en-US", None),
        ("制御盤", "en", None),
        # Languages without a reviewed entry stay untagged.
        ("Bestellungen", "de", None),
        ("Привет", "ru", None),
        # An invalid tag never produces a tag.
        ("制御盤", "ja_JP", None),
        ("制御盤", "", None),
    ],
)
def test_compatibility_vectors(text: str, namespace_language: str, expected: str | None) -> None:
    assert compatible_language_tag(text, namespace_language) == expected


def test_compatible_text_becomes_a_language_tagged_literal() -> None:
    literal = generated_literal("制御盤", "ja", datatype=XSD.string)
    assert literal == Literal("制御盤", lang="ja")
    assert literal.datatype is None


def test_incompatible_text_keeps_its_untagged_shape() -> None:
    assert generated_literal("PLC制御盤", "ja", datatype=XSD.string) == Literal("PLC制御盤", datatype=XSD.string)
    assert generated_literal("PLC制御盤", "ja") == Literal("PLC制御盤")
    assert generated_literal("Purchase orders", "en") == Literal("Purchase orders")


def test_catalog_value_needs_generated_field_proof() -> None:
    assert catalog_literal("注文", "ja", generated=True) == Literal("注文", lang="ja")
    # Without the proof a Japanese value may be steward- or source-authored.
    assert catalog_literal("注文", "ja", generated=False) == Literal("注文")
    assert catalog_literal("注文", "ja", generated=False).language is None
