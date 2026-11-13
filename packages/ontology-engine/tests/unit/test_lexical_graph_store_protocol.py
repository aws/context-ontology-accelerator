# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for LexicalGraphStore protocol type-checking."""

import pytest

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
    ClassSchemaResult,
    EntityRecord,
    FactRecord,
    LexicalGraphStore,
    RelationRecord,
    TopicRecord,
)


class _CompleteMock:
    """Minimal mock that implements all LexicalGraphStore methods."""

    def get_class_nodes(self) -> list[ClassRecord]:
        return []

    def get_class_relations(self) -> list[RelationRecord]:
        return []

    def get_entities_by_class(
        self,
        classification: str,
        limit: int = 100,
    ) -> list[EntityRecord]:
        return []

    def get_facts_for_entities(
        self,
        entity_ids: list[str],
    ) -> list[FactRecord]:
        return []

    def get_topics(self) -> list[TopicRecord]:
        return []

    def get_graph_schema(self) -> ClassSchemaResult:
        return ClassSchemaResult(classes=[], relations=[])

    def health_check(self) -> dict:
        return {"status": "healthy", "backend": "mock"}


class _IncompleteMock:
    """Mock missing required protocol methods."""

    def get_class_nodes(self) -> list[ClassRecord]:
        return []


# ── Tests ──


class TestLexicalGraphStoreProtocol:
    """Verify LexicalGraphStore Protocol is runtime-checkable."""

    def test_protocol_is_runtime_checkable(self):
        """LexicalGraphStore should be usable with isinstance()."""
        # If the Protocol is not decorated with @runtime_checkable,
        # calling isinstance raises TypeError.
        assert isinstance(_CompleteMock(), LexicalGraphStore)

    def test_complete_mock_satisfies_protocol(self):
        """A class implementing all methods satisfies isinstance check."""
        store = _CompleteMock()
        assert isinstance(store, LexicalGraphStore)

    def test_incomplete_mock_does_not_satisfy_protocol(self):
        """A class missing methods does NOT satisfy isinstance check."""
        store = _IncompleteMock()
        assert not isinstance(store, LexicalGraphStore)
