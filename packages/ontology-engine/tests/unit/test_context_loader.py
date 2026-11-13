# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the enriched-context loader.

These tests use a mocked LexicalGraphStore with controlled fact responses.
No live AWS calls.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.context_loader import (
    _format_fact,
    load_class_contexts,
)
from coa_ontology.inducer.unstructured.services.description_generator import (
    EntityContext,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    FactRecord,
)


def _spo(subject: str, predicate: str, obj: str) -> FactRecord:
    return FactRecord(
        subject_value=subject,
        subject_class="X",
        predicate=predicate,
        object_value=obj,
        object_class="Y",
        complement=None,
    )


def _po(subject: str, predicate: str) -> FactRecord:
    """Predicate-only fact (no object entity)."""
    return FactRecord(
        subject_value=subject,
        subject_class="X",
        predicate=predicate,
        object_value=None,
        object_class=None,
        complement=None,
    )


class TestFormatFact:
    """Direct tests for the fact-rendering helper."""

    def test_spo_format(self):
        assert _format_fact(_spo("acme", "MAKES widgets", "Widget")) == "acme -- MAKES widgets --> Widget"

    def test_predicate_only_format(self):
        # The predicate text itself encodes the whole statement.
        assert _format_fact(_po("acme", "acme HEADQUARTERS Boston")) == "acme HEADQUARTERS Boston"


class TestLoadClassContextsBasic:
    """Core load_class_contexts behaviour."""

    def test_pairs_entities_with_their_facts(self):
        store = MagicMock()
        store.get_facts_for_entities.return_value = [
            _spo("acme", "MAKES widgets", "Widget"),
            _po("acme", "acme HEADQUARTERS Boston"),
            _spo("globex", "ACQUIRED initech", "Company"),
        ]
        sampled = {"Company": ["id-1", "id-2"]}
        label_by_id = {"id-1": "acme", "id-2": "globex"}

        result = load_class_contexts(sampled, label_by_id, store)

        # One context per entity, in input order.
        assert list(result.keys()) == ["Company"]
        ctxs = result["Company"]
        assert [c.label for c in ctxs] == ["acme", "globex"]
        assert ctxs[0].facts == [
            "acme -- MAKES widgets --> Widget",
            "acme HEADQUARTERS Boston",
        ]
        assert ctxs[1].facts == ["globex -- ACQUIRED initech --> Company"]

    def test_one_batched_store_call_per_class(self):
        store = MagicMock()
        store.get_facts_for_entities.return_value = []
        sampled = {
            "Company": ["c1", "c2"],
            "Person": ["p1"],
        }
        label_by_id = {"c1": "acme", "c2": "globex", "p1": "alice"}

        load_class_contexts(sampled, label_by_id, store)

        assert store.get_facts_for_entities.call_count == 2
        calls = store.get_facts_for_entities.call_args_list
        assert calls[0].args[0] == ["c1", "c2"]
        assert calls[1].args[0] == ["p1"]

    def test_entity_with_no_facts_still_present(self):
        """An entity returning no facts gets an EntityContext with facts=[]."""
        store = MagicMock()
        store.get_facts_for_entities.return_value = []
        sampled = {"Company": ["c1"]}
        label_by_id = {"c1": "acme"}

        result = load_class_contexts(sampled, label_by_id, store)

        assert result["Company"] == [EntityContext(label="acme", facts=[])]


class TestPerEntityFactCap:
    """The max_facts_per_entity cap is enforced per entity, not total."""

    def test_cap_is_per_entity(self):
        store = MagicMock()
        store.get_facts_for_entities.return_value = [_po("acme", f"acme FACT-{i}") for i in range(20)]
        sampled = {"Company": ["c1"]}
        label_by_id = {"c1": "acme"}

        result = load_class_contexts(sampled, label_by_id, store, max_facts_per_entity=5)

        assert len(result["Company"][0].facts) == 5
        # Order preserved (first 5 of the 20 returned).
        assert result["Company"][0].facts == [f"acme FACT-{i}" for i in range(5)]

    def test_default_cap_is_max_facts_per_entity_constant(self):
        from coa_ontology.inducer.unstructured.services.description_generator import (
            MAX_FACTS_PER_ENTITY,
        )

        store = MagicMock()
        # Many more facts than the default cap.
        store.get_facts_for_entities.return_value = [
            _po("acme", f"acme FACT-{i}") for i in range(MAX_FACTS_PER_ENTITY + 5)
        ]
        sampled = {"Company": ["c1"]}
        label_by_id = {"c1": "acme"}

        result = load_class_contexts(sampled, label_by_id, store)

        assert len(result["Company"][0].facts) == MAX_FACTS_PER_ENTITY

    def test_cap_zero_means_no_facts_kept(self):
        store = MagicMock()
        store.get_facts_for_entities.return_value = [
            _po("acme", "acme FACT-1"),
        ]
        sampled = {"Company": ["c1"]}
        label_by_id = {"c1": "acme"}

        result = load_class_contexts(sampled, label_by_id, store, max_facts_per_entity=0)

        assert result["Company"][0].facts == []

    def test_negative_cap_raises(self):
        store = MagicMock()
        with pytest.raises(ValueError, match=">= 0"):
            load_class_contexts(
                {"X": ["i1"]},
                {"i1": "label"},
                store,
                max_facts_per_entity=-1,
            )


class TestMissingLabel:
    """Entities whose ID is missing from label_by_id are dropped with a warning."""

    def test_missing_id_dropped_with_warning(self, caplog):
        store = MagicMock()
        store.get_facts_for_entities.return_value = []
        sampled = {"Company": ["good", "missing"]}
        label_by_id = {"good": "acme"}  # "missing" deliberately absent.

        with caplog.at_level(logging.WARNING):
            result = load_class_contexts(sampled, label_by_id, store)

        assert [c.label for c in result["Company"]] == ["acme"]
        assert "missing" in caplog.text
        # Store should only be called with the kept IDs.
        store.get_facts_for_entities.assert_called_once_with(["good"])

    def test_all_missing_ids_yields_empty_class(self, caplog):
        store = MagicMock()
        sampled = {"Empty": ["id1", "id2"]}
        label_by_id: dict[str, str] = {}

        with caplog.at_level(logging.WARNING):
            result = load_class_contexts(sampled, label_by_id, store)

        assert result == {"Empty": []}
        # Store should NOT be called at all when no IDs survive filtering.
        store.get_facts_for_entities.assert_not_called()


class TestEmptyInputs:
    """Edge cases on empty/zero-class inputs."""

    def test_empty_sampled_returns_empty(self):
        store = MagicMock()
        result = load_class_contexts({}, {}, store)
        assert result == {}
        store.get_facts_for_entities.assert_not_called()

    def test_class_with_empty_id_list_returns_empty_list_for_class(self):
        store = MagicMock()
        result = load_class_contexts({"Empty": []}, {}, store)
        assert result == {"Empty": []}
        store.get_facts_for_entities.assert_not_called()


class TestFactGroupingByLabel:
    """Facts are grouped to entities by subject_value (entity label)."""

    def test_facts_with_unrelated_subject_are_skipped(self):
        """Facts whose subject doesn't match any kept label are not attached."""
        store = MagicMock()
        store.get_facts_for_entities.return_value = [
            _po("acme", "acme FACT-1"),
            # Subject doesn't match any entity in our sampled set.
            _po("unknown_other_entity", "unknown FACT-1"),
        ]
        sampled = {"Company": ["c1"]}
        label_by_id = {"c1": "acme"}

        result = load_class_contexts(sampled, label_by_id, store)

        # "acme" gets its fact; the orphan fact is dropped silently because
        # it doesn't match any kept entity label.
        assert len(result["Company"]) == 1
        assert result["Company"][0].label == "acme"
        assert result["Company"][0].facts == ["acme FACT-1"]
