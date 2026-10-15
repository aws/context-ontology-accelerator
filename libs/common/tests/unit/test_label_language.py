# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for namespace-language label selection (LLD §2.2.5)."""

from __future__ import annotations

import itertools

import pytest
from coa_common.label_language import LabelPreference, asserted_language_tag
from coa_common.language_tags import LanguageTagError

pytestmark = pytest.mark.unit


class TestAssertedLanguageTag:
    @pytest.mark.parametrize(
        ("namespace_language", "expected"),
        [
            ("ja", "ja"),
            ("JA-jp", "ja-JP"),
            ("ja-Jpan", "ja-Jpan"),
            ("ja-Jpan-JP", "ja-Jpan-JP"),
            ("ko", "ko"),
            ("ko-Kore-KR", "ko-Kore-KR"),
            ("zh", "zh"),
            # English asserts no tags, so untagged wins.
            ("en", None),
            ("en-US", None),
            # Chinese qualifiers stay untagged in v1.
            ("zh-Hans", None),
            ("zh-Hant", None),
            ("zh-TW", None),
            # Mismatched script qualifiers stay untagged.
            ("ja-Latn", None),
            ("ko-Latn", None),
            # Languages without a reviewed entry assert nothing.
            ("de", None),
            ("pt-BR", None),
        ],
    )
    def test_matches_the_generation_rules(self, namespace_language: str, expected: str | None) -> None:
        assert asserted_language_tag(namespace_language) == expected

    def test_invalid_stored_language_raises(self) -> None:
        with pytest.raises(LanguageTagError):
            LabelPreference.for_namespace("ja_JP")


class TestSelectionOrder:
    def test_japanese_namespace_prefers_ja_then_untagged_then_other(self) -> None:
        pref = LabelPreference.for_namespace("ja")
        assert pref.pick([("Air Conditioner", "en"), ("aircon", ""), ("空調設備", "ja")]) == "空調設備"
        assert pref.pick([("Air Conditioner", "en"), ("aircon", "")]) == "aircon"
        assert pref.pick([("Air Conditioner", "en")]) == "Air Conditioner"

    def test_japanese_region_namespace_prefers_its_exact_tag(self) -> None:
        pref = LabelPreference.for_namespace("ja-JP")
        assert pref.pick([("空調", "ja"), ("空調設備", "ja-jp"), ("aircon", "")]) == "空調設備"
        # A bare ``ja`` label is not the asserted tag; untagged outranks it.
        assert pref.pick([("空調", "ja"), ("aircon", "")]) == "aircon"

    def test_english_namespace_prefers_untagged(self) -> None:
        pref = LabelPreference.for_namespace("en")
        assert pref.pick([("Air Conditioner", "en"), ("aircon", ""), ("空調設備", "ja")]) == "aircon"

    def test_zh_hans_namespace_prefers_untagged(self) -> None:
        pref = LabelPreference.for_namespace("zh-Hans")
        assert pref.pick([("空调", "zh"), ("空調", "zh-Hans"), ("aircon", "")]) == "aircon"

    def test_zh_namespace_prefers_zh(self) -> None:
        pref = LabelPreference.for_namespace("zh")
        assert pref.pick([("aircon", ""), ("空调", "zh")]) == "空调"

    def test_tag_comparison_is_case_insensitive(self) -> None:
        assert LabelPreference.for_namespace("ko-KR").pick([("x", ""), ("공조", "KO-kr")]) == "공조"


class TestVisibility:
    def test_legacy_untagged_only_resource_keeps_its_label(self) -> None:
        assert LabelPreference.for_namespace("ja").pick([("Legacy", "")]) == "Legacy"

    def test_differently_tagged_resource_keeps_its_label(self) -> None:
        assert LabelPreference.for_namespace("ja").pick([("Klimagerät", "de")]) == "Klimagerät"

    def test_pick_by_resource_never_drops_a_resource(self) -> None:
        pref = LabelPreference.for_namespace("ja")
        rows = [
            ("urn:a", "Legacy", ""),
            ("urn:b", "Klimagerät", "de"),
            ("urn:c", "", ""),
            ("urn:d", "Equipment", ""),
            ("urn:d", "設備", "ja"),
        ]
        assert pref.pick_by_resource(rows) == {
            "urn:a": "Legacy",
            "urn:b": "Klimagerät",
            "urn:c": "",
            "urn:d": "設備",
        }

    def test_empty_input(self) -> None:
        assert LabelPreference.for_namespace("ja").pick([]) == ""
        assert LabelPreference.for_namespace("ja").pick([("", "ja")]) == ""


class TestDeterminism:
    CANDIDATES = [("Beta", "de"), ("Alpha", "fr"), ("Gamma", ""), ("Delta", "")]

    @pytest.mark.parametrize("namespace_language", ["ja", "en", "zh-Hans"])
    def test_order_independent(self, namespace_language: str) -> None:
        pref = LabelPreference.for_namespace(namespace_language)
        results = {pref.pick(list(p)) for p in itertools.permutations(self.CANDIDATES)}
        assert results == {"Delta"}

    def test_remaining_literals_in_lexical_order(self) -> None:
        pref = LabelPreference.for_namespace("ja")
        assert pref.pick([("Beta", "de"), ("Alpha", "fr")]) == "Alpha"
        # Same text under two tags resolves on the tag.
        assert pref.sort_key("x", "de") < pref.sort_key("x", "fr")

    def test_order_lists_every_label_best_first(self) -> None:
        pref = LabelPreference.for_namespace("ja")
        ordered = pref.order([("Beta", "de"), ("Legacy", ""), ("設備", "ja"), ("Alpha", "fr"), ("Legacy", "")])
        assert ordered == ["設備", "Legacy", "Alpha", "Beta"]

    def test_untagged_first_has_no_preferred_tag(self) -> None:
        pref = LabelPreference.untagged_first()
        assert pref.preferred_tag is None
        assert pref.pick([("設備", "ja"), ("Equipment", "")]) == "Equipment"
