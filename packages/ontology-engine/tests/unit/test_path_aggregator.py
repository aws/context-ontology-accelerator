# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Path Aggregator.

Tests cover:
- Normalization of predicate labels (case, spaces, special chars)
- Merging duplicate (domain, predicate, range) triples by summing counts
- Distinct (domain, predicate, range) triples remain separate
- Frequency-descending sort
- Lexicographic tie-breaking on (domain, predicate, range)
- min_frequency filter (excludes strictly below; includes at threshold)
- Empty input returns empty list
- Predicates that normalize to empty string are skipped (defensive guard)

Requirements: 5.1–5.6
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.path_aggregator import (
    PropertyProposal,
    aggregate,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    RelationRecord,
)


def _rel(
    source_class: str,
    predicate: str,
    target_class: str,
    count: int,
) -> RelationRecord:
    """Helper to construct a RelationRecord."""
    return RelationRecord(
        source_class=source_class,
        predicate=predicate,
        target_class=target_class,
        count=count,
    )


class TestPredicateNormalization:
    """Variants of the same predicate must collapse to the same key.

    Validates: Requirement 5.2 — Path_Aggregator normalizes predicate
    labels (lowercasing, spaces → underscores, stripping non-alphanumeric,
    collapsing consecutive underscores) before producing proposals.
    """

    def test_case_variants_collapse(self):
        relations = [
            _rel("Company", "WORKS_FOR", "Person", 3),
            _rel("Company", "works_for", "Person", 2),
            _rel("Company", "Works_For", "Person", 1),
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 1
        assert proposals[0] == PropertyProposal(
            domain="Company",
            predicate="works_for",
            range="Person",
            frequency=6,
        )

    def test_spaces_collapse_to_underscore_form(self):
        """Spaced and underscored variants must collapse.

        Per Requirement 5.2, normalization replaces spaces with
        underscores; non-alphanumeric characters other than underscores
        are stripped (not converted). So "Works For" → "works_for"
        merges with "works_for", but "WORKS-FOR" → "worksfor" is a
        distinct key (covered separately below).
        """
        relations = [
            _rel("Company", "Works For", "Person", 4),
            _rel("Company", "works_for", "Person", 3),
            _rel("Company", "WORKS  FOR", "Person", 2),  # double space
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 1
        assert proposals[0].predicate == "works_for"
        assert proposals[0].frequency == 9

    def test_hyphens_stripped_not_converted(self):
        """Per Requirement 5.2, hyphens are stripped (not turned into
        underscores) so "WORKS-FOR" normalizes to "worksfor", which is
        a distinct key from "works_for"."""
        relations = [
            _rel("Company", "works_for", "Person", 3),
            _rel("Company", "WORKS-FOR", "Person", 2),
        ]
        proposals = aggregate(relations)

        # Two distinct normalized predicates → two proposals.
        predicates = {p.predicate for p in proposals}
        assert predicates == {"works_for", "worksfor"}

    def test_special_chars_stripped_before_merge(self):
        # "HAS POLICY (v2)" → "has_policy_v2"
        # "has_policy_v2"   → "has_policy_v2"
        relations = [
            _rel("Org", "HAS POLICY (v2)", "Policy", 5),
            _rel("Org", "has_policy_v2", "Policy", 7),
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 1
        assert proposals[0].predicate == "has_policy_v2"
        assert proposals[0].frequency == 12

    def test_predicate_normalizing_to_empty_is_skipped(self):
        """Predicates that normalize to "" must be skipped, not crash.

        Defensive guard: ``normalize_predicate("@#%")`` returns "" and
        cannot produce a valid property IRI downstream.
        """
        relations = [
            _rel("A", "@#%", "B", 5),  # normalizes to ""
            _rel("A", "valid", "B", 3),
        ]
        proposals = aggregate(relations)

        # Only the "valid" relation makes it through.
        assert len(proposals) == 1
        assert proposals[0].predicate == "valid"
        assert proposals[0].frequency == 3


class TestMerging:
    """Merging behaviour for the (domain, predicate, range) key.

    Validates: Requirement 5.3 — Edges sharing the same normalized
    predicate between the same class pair merge with summed counts;
    different class pairs produce distinct proposals.
    """

    def test_same_triple_counts_sum(self):
        relations = [
            _rel("Company", "operates_in", "Country", 10),
            _rel("Company", "operates_in", "Country", 5),
            _rel("Company", "operates_in", "Country", 1),
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 1
        assert proposals[0].frequency == 16

    def test_different_domains_stay_separate(self):
        relations = [
            _rel("Company", "operates_in", "Country", 10),
            _rel("Person", "operates_in", "Country", 4),
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 2
        domains = {p.domain for p in proposals}
        assert domains == {"Company", "Person"}

    def test_different_ranges_stay_separate(self):
        relations = [
            _rel("Company", "operates_in", "Country", 10),
            _rel("Company", "operates_in", "Region", 4),
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 2
        ranges = {p.range for p in proposals}
        assert ranges == {"Country", "Region"}

    def test_same_predicate_different_class_pairs(self):
        """One proposal per distinct (domain, predicate, range) triple."""
        relations = [
            _rel("Company", "owns", "Asset", 7),
            _rel("Person", "owns", "Asset", 3),
            _rel("Company", "owns", "Subsidiary", 2),
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 3
        triples = {(p.domain, p.predicate, p.range) for p in proposals}
        assert triples == {
            ("Company", "owns", "Asset"),
            ("Person", "owns", "Asset"),
            ("Company", "owns", "Subsidiary"),
        }

    def test_normalization_then_merge(self):
        """Variants must normalize first, then merge by triple."""
        relations = [
            _rel("Company", "OPERATES IN", "Country", 6),
            _rel("Company", "operates_in", "Country", 4),
        ]
        proposals = aggregate(relations)

        assert len(proposals) == 1
        assert proposals[0] == PropertyProposal(
            domain="Company",
            predicate="operates_in",
            range="Country",
            frequency=10,
        )


class TestSorting:
    """Frequency-descending sort with lexicographic tie-breaking.

    Validates: Requirement 5.4 — Sorted by frequency descending; ties
    broken lexicographically by (domain, predicate, range).
    """

    def test_sorted_by_frequency_descending(self):
        relations = [
            _rel("Company", "owns", "Asset", 1),
            _rel("Company", "operates_in", "Country", 100),
            _rel("Person", "lives_in", "City", 25),
        ]
        proposals = aggregate(relations)

        frequencies = [p.frequency for p in proposals]
        assert frequencies == [100, 25, 1]

    def test_tie_break_by_domain(self):
        """Equal frequency → break ties by domain ascending."""
        relations = [
            _rel("Zebra", "p", "X", 5),
            _rel("Apple", "p", "X", 5),
            _rel("Mango", "p", "X", 5),
        ]
        proposals = aggregate(relations)

        domains = [p.domain for p in proposals]
        assert domains == ["Apple", "Mango", "Zebra"]

    def test_tie_break_by_predicate_when_domain_equal(self):
        """Equal frequency, equal domain → break ties by predicate ascending."""
        relations = [
            _rel("Org", "zeta_rel", "Thing", 5),
            _rel("Org", "alpha_rel", "Thing", 5),
            _rel("Org", "mu_rel", "Thing", 5),
        ]
        proposals = aggregate(relations)

        predicates = [p.predicate for p in proposals]
        assert predicates == ["alpha_rel", "mu_rel", "zeta_rel"]

    def test_tie_break_by_range_when_domain_and_predicate_equal(self):
        """Equal frequency, equal domain+predicate → break ties by range ascending."""
        relations = [
            _rel("Org", "owns", "Zebra", 5),
            _rel("Org", "owns", "Apple", 5),
            _rel("Org", "owns", "Mango", 5),
        ]
        proposals = aggregate(relations)

        ranges = [p.range for p in proposals]
        assert ranges == ["Apple", "Mango", "Zebra"]

    def test_combined_sort_order(self):
        """Frequency-desc beats lexicographic; lex breaks ties at each tier."""
        relations = [
            _rel("Person", "knows", "Person", 50),  # high freq
            _rel("Apple", "x", "Y", 10),  # tied at 10
            _rel("Banana", "x", "Y", 10),  # tied at 10
            _rel("Cherry", "x", "Y", 10),  # tied at 10
            _rel("Org", "owns", "Asset", 5),  # low freq
        ]
        proposals = aggregate(relations)

        triples = [(p.domain, p.predicate, p.range, p.frequency) for p in proposals]
        assert triples == [
            ("Person", "knows", "Person", 50),
            ("Apple", "x", "Y", 10),
            ("Banana", "x", "Y", 10),
            ("Cherry", "x", "Y", 10),
            ("Org", "owns", "Asset", 5),
        ]


class TestMinFrequencyFilter:
    """min_frequency filter behaviour.

    Validates: Requirement 5.6 — When ``min_frequency`` is configured,
    proposals with frequency below that threshold are excluded.
    """

    def test_default_min_frequency_includes_count_one(self):
        """Default min_frequency=1 should include proposals with count 1."""
        relations = [_rel("A", "p", "B", 1)]
        proposals = aggregate(relations)

        assert len(proposals) == 1
        assert proposals[0].frequency == 1

    def test_excludes_strictly_below_threshold(self):
        relations = [
            _rel("A", "p", "B", 2),  # below 5 → excluded
            _rel("A", "q", "B", 4),  # below 5 → excluded
            _rel("A", "r", "B", 5),  # equal to 5 → kept
            _rel("A", "s", "B", 9),  # above 5 → kept
        ]
        proposals = aggregate(relations, min_frequency=5)

        kept_predicates = {p.predicate for p in proposals}
        assert kept_predicates == {"r", "s"}

    def test_includes_at_threshold(self):
        """Proposals AT the min_frequency threshold must be included."""
        relations = [_rel("A", "p", "B", 7)]
        proposals = aggregate(relations, min_frequency=7)

        assert len(proposals) == 1
        assert proposals[0].frequency == 7

    def test_filter_applied_after_merge(self):
        """Filter is on the merged frequency, not per-edge counts.

        Two edges with count=2 each merge to frequency=4 — that is what
        is compared against min_frequency.
        """
        relations = [
            _rel("Company", "owns", "Asset", 2),
            _rel("Company", "owns", "Asset", 2),
        ]
        proposals = aggregate(relations, min_frequency=4)

        # Merged frequency is 4, which is >= threshold → kept.
        assert len(proposals) == 1
        assert proposals[0].frequency == 4

    def test_high_threshold_yields_empty_list(self):
        relations = [
            _rel("A", "p", "B", 1),
            _rel("C", "q", "D", 2),
        ]
        proposals = aggregate(relations, min_frequency=100)

        assert proposals == []


class TestEmptyInput:
    """Empty input returns empty list without error.

    Validates: Requirement 5.5 — Zero ``__SYS_RELATION__`` edges yields
    an empty list (no exception).
    """

    def test_empty_list_returns_empty(self):
        assert aggregate([]) == []

    def test_empty_list_with_min_frequency(self):
        assert aggregate([], min_frequency=10) == []

    def test_only_empty_normalized_predicates_returns_empty(self):
        """If every predicate normalizes to "", result is still []."""
        relations = [
            _rel("A", "@#", "B", 5),
            _rel("C", "!!!", "D", 7),
        ]
        proposals = aggregate(relations)

        assert proposals == []
