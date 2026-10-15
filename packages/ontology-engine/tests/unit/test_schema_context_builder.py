# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the per-class T-Box schema-context builder.

Tests cover:
- Outgoing/incoming projection from RelationRecord aggregates
- Predicate normalization before aggregation
- Aggregation across same (predicate, other_class) pairs
- Sort order: frequency descending, ties broken lexicographically
- CamelCase → space-separated canonical mapping
- Edges to/from classes outside the canonical set are still represented
- Self-loops contribute to BOTH outgoing and incoming
- Empty inputs
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.description_generator import (
    ClassSchemaContext,
    SchemaEdge,
)
from coa_ontology.inducer.unstructured.services.schema_context_builder import (
    build_class_schema_contexts,
    build_class_schema_contexts_from_schema_result,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassSchemaResult,
    RelationRecord,
)


def _r(source: str, predicate: str, target: str, count: int) -> RelationRecord:
    return RelationRecord(
        source_class=source,
        predicate=predicate,
        target_class=target,
        count=count,
    )


class TestBasicProjection:
    """Edges are split into outgoing and incoming for each class."""

    def test_single_outgoing_edge(self):
        rels = [_r("Company", "OPERATES_IN", "Location", 42)]
        result = build_class_schema_contexts(rels, ["Company"])

        assert "Company" in result
        ctx = result["Company"]
        assert ctx.outgoing == [SchemaEdge(predicate="operates_in", other_class="Location", frequency=42)]
        assert ctx.incoming == []

    def test_single_incoming_edge(self):
        rels = [_r("Person", "WORKS_FOR", "Company", 18)]
        result = build_class_schema_contexts(rels, ["Company"])

        ctx = result["Company"]
        assert ctx.outgoing == []
        assert ctx.incoming == [SchemaEdge(predicate="works_for", other_class="Person", frequency=18)]

    def test_multiple_classes_each_get_their_own_context(self):
        rels = [
            _r("Company", "OPERATES_IN", "Location", 10),
            _r("Person", "WORKS_FOR", "Company", 5),
        ]
        result = build_class_schema_contexts(rels, ["Company", "Person"])

        assert set(result) == {"Company", "Person"}
        # Company sees the OPERATES_IN as outgoing and WORKS_FOR as incoming.
        assert len(result["Company"].outgoing) == 1
        assert len(result["Company"].incoming) == 1
        # Person sees WORKS_FOR as outgoing and nothing else.
        assert len(result["Person"].outgoing) == 1
        assert len(result["Person"].incoming) == 0

    def test_classes_with_zero_edges_appear_with_empty_lists(self):
        rels: list[RelationRecord] = []
        result = build_class_schema_contexts(rels, ["Company", "Person"])

        for cls in ("Company", "Person"):
            assert result[cls].class_label == cls
            assert result[cls].outgoing == []
            assert result[cls].incoming == []


class TestPredicateNormalization:
    """Predicates are normalized before aggregation."""

    def test_case_variants_merge(self):
        rels = [
            _r("Company", "OPERATES_IN", "Location", 5),
            _r("Company", "operates_in", "Location", 3),
            _r("Company", "Operates_In", "Location", 1),
        ]
        result = build_class_schema_contexts(rels, ["Company"])
        edges = result["Company"].outgoing
        assert len(edges) == 1
        assert edges[0].predicate == "operates_in"
        assert edges[0].frequency == 9

    def test_predicate_normalizing_to_empty_skipped(self):
        rels = [
            _r("Company", "@#%", "Location", 5),  # normalizes to empty
            _r("Company", "valid", "Location", 3),
        ]
        result = build_class_schema_contexts(rels, ["Company"])
        edges = result["Company"].outgoing
        assert len(edges) == 1
        assert edges[0].predicate == "valid"


class TestAggregation:
    """Same (predicate, other_class) pairs are summed."""

    def test_same_target_class_counts_summed(self):
        rels = [
            _r("Company", "OPERATES_IN", "Location", 10),
            _r("Company", "OPERATES_IN", "Location", 5),
        ]
        result = build_class_schema_contexts(rels, ["Company"])
        edges = result["Company"].outgoing
        assert len(edges) == 1
        assert edges[0].frequency == 15

    def test_different_target_classes_stay_separate(self):
        rels = [
            _r("Company", "OPERATES_IN", "Location", 10),
            _r("Company", "OPERATES_IN", "Country", 4),
        ]
        result = build_class_schema_contexts(rels, ["Company"])
        edges = result["Company"].outgoing
        assert len(edges) == 2
        targets = {e.other_class for e in edges}
        assert targets == {"Location", "Country"}


class TestSortOrder:
    """Edges sorted by frequency desc, ties by (predicate, other_class)."""

    def test_sorted_by_frequency_descending(self):
        rels = [
            _r("Company", "P_LOW", "X", 1),
            _r("Company", "P_HIGH", "X", 100),
            _r("Company", "P_MID", "X", 50),
        ]
        result = build_class_schema_contexts(rels, ["Company"])
        freqs = [e.frequency for e in result["Company"].outgoing]
        assert freqs == [100, 50, 1]

    def test_tie_break_by_predicate_then_other_class(self):
        rels = [
            _r("Company", "BETA", "Z", 5),
            _r("Company", "ALPHA", "Z", 5),  # same freq + same other; alphabetical predicate first
            _r("Company", "ALPHA", "Y", 5),  # same freq + same predicate; alphabetical other first
        ]
        result = build_class_schema_contexts(rels, ["Company"])
        order = [(e.predicate, e.other_class) for e in result["Company"].outgoing]
        assert order == [
            ("alpha", "Y"),
            ("alpha", "Z"),
            ("beta", "Z"),
        ]


class TestCanonicalMapping:
    """CamelCase → space-separated canonicalization works as documented."""

    def test_class_label_to_canonical_redirects_match(self):
        rels = [
            _r("FinancialInstrument", "ISSUED_BY", "Company", 5),
        ]
        result = build_class_schema_contexts(
            rels,
            class_labels=["Financial Instrument", "Company"],
            class_label_to_canonical={
                "FinancialInstrument": "Financial Instrument",
                "Company": "Company",
            },
        )

        assert "Financial Instrument" in result
        assert len(result["Financial Instrument"].outgoing) == 1
        assert result["Financial Instrument"].outgoing[0].other_class == "Company"
        # And Company sees the incoming.
        assert len(result["Company"].incoming) == 1
        assert result["Company"].incoming[0].other_class == "Financial Instrument"

    def test_default_mapping_treats_classes_as_already_canonical(self):
        rels = [_r("Company", "OPERATES_IN", "Location", 5)]
        result = build_class_schema_contexts(rels, ["Company"])
        # Without an explicit mapping, "Company" matches "Company".
        assert len(result["Company"].outgoing) == 1


class TestOtherClassPreservation:
    """Other-side classes outside the canonical set are still surfaced."""

    def test_target_class_outside_set_kept_as_other(self):
        rels = [
            _r("Company", "OPERATES_IN", "Location", 5),  # only Company in set
        ]
        result = build_class_schema_contexts(rels, ["Company"])
        assert result["Company"].outgoing[0].other_class == "Location"


class TestSelfLoops:
    """Edges where source == target appear as both outgoing and incoming."""

    def test_self_loop_counted_in_both_directions(self):
        rels = [_r("Person", "KNOWS", "Person", 7)]
        result = build_class_schema_contexts(rels, ["Person"])

        assert len(result["Person"].outgoing) == 1
        assert len(result["Person"].incoming) == 1
        assert result["Person"].outgoing[0].other_class == "Person"
        assert result["Person"].incoming[0].other_class == "Person"

    def test_inter_canonical_class_edge_appears_for_both(self):
        """When source AND target are both in the canonical set, both classes
        get the edge from their own perspective."""
        rels = [_r("Person", "WORKS_FOR", "Company", 12)]
        result = build_class_schema_contexts(rels, ["Person", "Company"])

        # Person sees it as outgoing.
        assert len(result["Person"].outgoing) == 1
        assert result["Person"].outgoing[0].other_class == "Company"
        # Company sees it as incoming.
        assert len(result["Company"].incoming) == 1
        assert result["Company"].incoming[0].other_class == "Person"


class TestSchemaResultWrapper:
    """The convenience wrapper accepts ClassSchemaResult directly."""

    def test_passes_relations_through(self):
        schema_result = ClassSchemaResult(
            classes=[],
            relations=[_r("Company", "OPERATES_IN", "Location", 5)],
        )
        result = build_class_schema_contexts_from_schema_result(
            schema_result,
            class_labels=["Company"],
        )
        assert len(result["Company"].outgoing) == 1


class TestEmptyInputs:
    """Empty / zero-edge inputs return empty contexts cleanly."""

    def test_empty_relations_yields_empty_contexts(self):
        result = build_class_schema_contexts([], ["Company"])
        assert isinstance(result["Company"], ClassSchemaContext)
        assert result["Company"].outgoing == []
        assert result["Company"].incoming == []

    def test_empty_class_labels_returns_empty_dict(self):
        result = build_class_schema_contexts([_r("A", "p", "B", 1)], [])
        assert result == {}
