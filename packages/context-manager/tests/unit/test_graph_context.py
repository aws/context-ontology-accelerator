# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Direct unit tests for the graph-context statement helpers.

The response assembler, the agentic context and session restore all route
``supportingStatements`` through these helpers, so a regression here changes
every Tier-3 response at once while the caller-level tests may still pass.
"""

from __future__ import annotations

import json

import pytest
from coa_serve.graph_context import (
    MAX_SUPPORTING_STATEMENT_BYTES,
    MAX_SUPPORTING_STATEMENT_CHARS,
    MAX_SUPPORTING_STATEMENTS_BYTES,
    MAX_SUPPORTING_STATEMENTS_CHARS,
    MAX_SUPPORTING_STATEMENTS_PER_ENTITY,
    bound_supporting_statements,
    decode_supporting_statements,
    encode_supporting_statements,
    fair_bound_statement_groups,
    truncate_utf8,
)

pytestmark = pytest.mark.unit

_ELLIPSIS = "…"


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


class TestTruncateUtf8:
    def test_text_within_both_bounds_is_returned_unchanged(self):
        assert truncate_utf8("設備台帳", max_chars=4, max_bytes=12) == "設備台帳"

    @pytest.mark.parametrize(("max_chars", "max_bytes"), [(0, 10), (10, 0), (-1, 10)])
    def test_a_non_positive_bound_yields_empty(self, max_chars, max_bytes):
        assert truncate_utf8("abc", max_chars=max_chars, max_bytes=max_bytes) == ""

    def test_character_bound_keeps_room_for_the_ellipsis(self):
        assert truncate_utf8("abcdef", max_chars=4, max_bytes=100) == "abc…"

    def test_byte_bound_never_splits_a_multibyte_character(self):
        # Each kanji is 3 bytes and the ellipsis is 3 bytes, so 10 bytes leave
        # room for two whole kanji (6) plus the marker (3), not a partial third.
        result = truncate_utf8("設備台帳管理", max_chars=100, max_bytes=10)

        assert result == "設備…"
        assert _utf8_len(result) <= 10
        result.encode("utf-8").decode("utf-8")  # well-formed UTF-8

    def test_four_byte_characters_are_kept_whole(self):
        result = truncate_utf8("😀😀😀", max_chars=100, max_bytes=8)

        assert result == "😀…"
        assert _utf8_len(result) <= 8

    def test_bound_too_small_for_the_ellipsis_cuts_without_it(self):
        # One character, or bytes no larger than the marker, leave no room for it.
        assert truncate_utf8("abcdef", max_chars=1, max_bytes=100) == "a"
        assert truncate_utf8("abcdef", max_chars=100, max_bytes=3) == "abc"
        assert truncate_utf8("設備台帳", max_chars=100, max_bytes=3) == "設"

    def test_byte_bound_that_fits_no_prefix_returns_only_the_ellipsis(self):
        # 4 bytes: the ellipsis (3) fits, but no 3-byte kanji fits beside it.
        assert truncate_utf8("設備", max_chars=100, max_bytes=4) == _ELLIPSIS

    def test_truncation_is_deterministic(self):
        text = "温度計測 " * 400
        first = truncate_utf8(text, max_chars=50, max_bytes=60)
        assert first == truncate_utf8(text, max_chars=50, max_bytes=60)
        assert len(first) <= 50 and _utf8_len(first) <= 60


class TestDecodeSupportingStatements:
    def test_canonical_json_array_decodes_in_order(self):
        assert decode_supporting_statements(json.dumps(["b", "a"])) == ["b", "a"]

    def test_a_restored_python_list_decodes_without_json(self):
        assert decode_supporting_statements(["x", "y"]) == ["x", "y"]

    def test_a_tuple_decodes_like_a_list(self):
        assert decode_supporting_statements(("x",)) == ["x"]

    def test_legacy_plain_text_is_one_statement(self):
        assert decode_supporting_statements("plain statement, not JSON") == ["plain statement, not JSON"]

    def test_a_json_string_literal_is_one_statement(self):
        assert decode_supporting_statements(json.dumps("one")) == ["one"]

    @pytest.mark.parametrize(
        "value",
        [None, 3, {"a": 1}, json.dumps({"a": 1}), json.dumps(3), "", "   "],
        ids=["none", "int", "dict", "json-object", "json-number", "empty", "blank"],
    )
    def test_unusable_values_decode_to_nothing(self, value):
        assert decode_supporting_statements(value) == []

    def test_non_string_and_blank_members_are_dropped(self):
        assert decode_supporting_statements(json.dumps(["a", 1, None, "  ", "b"])) == ["a", "b"]

    def test_malformed_json_falls_back_to_plain_text(self):
        assert decode_supporting_statements('["unterminated') == ['["unterminated']


class TestBoundSupportingStatements:
    def test_duplicates_are_removed_keeping_first_occurrence_order(self):
        assert bound_supporting_statements(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]

    def test_statements_that_clip_to_the_same_text_are_deduplicated(self):
        long_a = "x" * (MAX_SUPPORTING_STATEMENT_CHARS + 5) + "A"
        long_b = "x" * (MAX_SUPPORTING_STATEMENT_CHARS + 5) + "B"

        bounded = bound_supporting_statements([long_a, long_b])

        assert len(bounded) == 1
        assert len(bounded[0]) == MAX_SUPPORTING_STATEMENT_CHARS

    def test_count_bound_stops_after_max_count(self):
        statements = [f"s{i}" for i in range(MAX_SUPPORTING_STATEMENTS_PER_ENTITY + 20)]

        bounded = bound_supporting_statements(statements)

        assert bounded == statements[:MAX_SUPPORTING_STATEMENTS_PER_ENTITY]

    def test_each_statement_is_clipped_to_the_per_statement_bound(self):
        bounded = bound_supporting_statements(["a" * (MAX_SUPPORTING_STATEMENT_CHARS * 2)])

        assert len(bounded[0]) == MAX_SUPPORTING_STATEMENT_CHARS
        assert bounded[0].endswith(_ELLIPSIS)

    def test_total_character_budget_is_enforced(self):
        statements = [f"{i:03d}" + "a" * 997 for i in range(10)]  # exactly 1,000 chars each, all distinct

        bounded = bound_supporting_statements(statements)

        assert bounded == statements[: MAX_SUPPORTING_STATEMENTS_CHARS // 1_000]
        assert sum(len(s) for s in bounded) == MAX_SUPPORTING_STATEMENTS_CHARS

    def test_the_statement_that_crosses_the_budget_is_clipped_into_what_remains(self):
        statements = [f"{i:03d}" + "a" * 996 for i in range(10)]  # 999 chars each

        bounded = bound_supporting_statements(statements)

        # Four whole statements use 3,996 chars; the fifth gets the last 4.
        assert bounded[:4] == statements[:4]
        assert bounded[4] == "004…"
        assert sum(len(s) for s in bounded) == MAX_SUPPORTING_STATEMENTS_CHARS

    def test_total_byte_budget_is_enforced_for_multibyte_text(self):
        # 600 kanji = 1,800 bytes each, under the per-statement 2,000-byte bound.
        statements = [f"{i:02d}" + "設" * 598 for i in range(10)]

        bounded = bound_supporting_statements(statements)

        assert sum(_utf8_len(s) for s in bounded) <= MAX_SUPPORTING_STATEMENTS_BYTES
        assert all(_utf8_len(s) <= MAX_SUPPORTING_STATEMENT_BYTES for s in bounded)
        assert all(s.encode("utf-8").decode("utf-8") == s for s in bounded)

    def test_custom_bounds_are_honoured(self):
        assert bound_supporting_statements(["a", "b", "c"], max_count=2) == ["a", "b"]
        assert bound_supporting_statements(["aaaa", "bbbb"], max_total_chars=6) == ["aaaa", "b…"]

    def test_encoded_input_is_accepted(self):
        assert bound_supporting_statements(encode_supporting_statements(["a", "a", "b"])) == ["a", "b"]


class TestFairBoundStatementGroups:
    def test_small_groups_are_returned_whole_and_in_place(self):
        assert fair_bound_statement_groups([["a", "b"], [], ["c"]]) == [["a", "b"], [], ["c"]]

    def test_one_large_entity_cannot_starve_the_others(self):
        heavy = [f"{i:03d}" + "h" * 996 for i in range(10)]  # 10 x 1,000 chars
        groups = [heavy, ["light one"], ["light two"]]

        bounded = fair_bound_statement_groups(groups)

        assert bounded[1] == ["light one"]
        assert bounded[2] == ["light two"]
        assert bounded[0], "the heavy entity still gets its share"
        total_chars = sum(len(s) for group in bounded for s in group)
        assert total_chars <= MAX_SUPPORTING_STATEMENTS_CHARS

    def test_more_large_entities_than_the_budget_fits_all_get_a_share(self):
        # Six 1,000-char statements against a 4,000-char budget. Taking whole
        # statements in turn would fill the budget after four entities; the
        # fair share clips each so all six are represented.
        groups = [[f"{g}" + "x" * 999] for g in "abcdef"]

        bounded = fair_bound_statement_groups(groups)

        assert all(len(group) == 1 for group in bounded)
        assert [group[0][0] for group in bounded] == list("abcdef")
        assert all(len(group[0]) < 1_000 for group in bounded)
        assert sum(len(group[0]) for group in bounded) <= MAX_SUPPORTING_STATEMENTS_CHARS

    def test_global_byte_budget_holds_across_groups(self):
        groups = [[f"{g}{i:02d}" + "設" * 590 for i in range(5)] for g in "abcd"]

        bounded = fair_bound_statement_groups(groups)

        total_bytes = sum(_utf8_len(s) for group in bounded for s in group)
        assert total_bytes <= MAX_SUPPORTING_STATEMENTS_BYTES
        assert all(group for group in bounded), "every active entity gets at least one statement"

    def test_each_group_is_deduplicated_before_sharing(self):
        assert fair_bound_statement_groups([["a", "a", "b"]]) == [["a", "b"]]

    def test_result_has_one_list_per_input_group(self):
        assert fair_bound_statement_groups([]) == []
        assert fair_bound_statement_groups([[], []]) == [[], []]

    def test_output_order_follows_each_group_order(self):
        bounded = fair_bound_statement_groups([["a1", "a2", "a3"], ["b1", "b2"]])

        assert bounded == [["a1", "a2", "a3"], ["b1", "b2"]]


class TestEncodeSupportingStatements:
    def test_round_trips_through_decode_without_ascii_escapes(self):
        statements = ["設備台帳の点検周期は 6 か月", 'quote " and \\ backslash']

        encoded = encode_supporting_statements(statements)

        assert "設備台帳" in encoded  # ensure_ascii=False keeps the text readable
        assert decode_supporting_statements(encoded) == statements

    def test_accepts_any_sequence(self):
        assert encode_supporting_statements(("a", "b")) == '["a", "b"]'
