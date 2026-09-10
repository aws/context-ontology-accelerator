# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the per-language stop-word modules (tier1/stopwords)."""

from __future__ import annotations

import pytest
from coa_serve.tier1 import stopwords
from coa_serve.tier1.metric_resolver import _RESIDUAL_STOP_WORDS
from coa_serve.tier1.stopwords import en, ko


@pytest.mark.unit
class TestMergedSet:
    def test_merge_is_the_union_of_all_language_modules(self):
        expected = frozenset(w for g in (*en.GROUPS, *ko.GROUPS) for w in g.split())
        assert expected == stopwords.RESIDUAL_STOP_WORDS

    def test_resolver_consumes_the_merged_set(self):
        assert _RESIDUAL_STOP_WORDS is stopwords.RESIDUAL_STOP_WORDS

    def test_every_language_contributes(self):
        assert "please" in stopwords.RESIDUAL_STOP_WORDS  # en
        assert "알려줘" in stopwords.RESIDUAL_STOP_WORDS  # ko

    def test_groups_are_nonempty(self):
        for group in stopwords.RESIDUAL_STOP_WORD_GROUPS:
            assert group.split(), "empty stop-word group"


@pytest.mark.unit
class TestCurationPolicy:
    """Qualifier-bearing words must NEVER be stop-listed, in any language.

    A wrongly added stop word makes the gate blind to a real constraint: a
    fixed metric then answers a question it cannot express, at confidence 1.0.
    These canaries encode the policy so a future addition trips a test, not a
    customer.
    """

    @pytest.mark.parametrize(
        "word",
        # Aggregate modifiers change WHICH aggregate is asked for; time words
        # are windows a fixed SQL template cannot express.
        ["average", "avg", "mean", "net", "gross", "cumulative", "today", "current", "now", "yesterday"],
    )
    def test_english_qualifier_words_are_absent(self, word):
        assert word not in stopwords.RESIDUAL_STOP_WORDS

    @pytest.mark.parametrize(
        "word",
        ["평균", "누적", "순", "오늘", "어제", "지난주", "지난달", "올해", "작년"],
    )
    def test_korean_qualifier_words_are_absent(self, word):
        assert word not in stopwords.RESIDUAL_STOP_WORDS
