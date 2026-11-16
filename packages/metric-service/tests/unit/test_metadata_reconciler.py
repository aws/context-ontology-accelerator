# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for dataset metadata reconciliation."""

from __future__ import annotations

import pytest
from coa_metrics.dataset_resolver import ResolvedDataset
from coa_metrics.metadata_reconciler import (
    DatasetMetadata,
    MetadataSource,
    MetadataStore,
    ReconciliationAction,
    reconcile_metadata,
)

pytestmark = pytest.mark.unit


class MockMetadataStore(MetadataStore):
    """In-memory metadata store for testing."""

    def __init__(self, data: dict[str, DatasetMetadata] | None = None) -> None:
        self._data: dict[str, DatasetMetadata] = data or {}
        self.updates: list[dict] = []  # Track calls for assertions

    def get_metadata(self, data_source_id: str) -> DatasetMetadata | None:
        return self._data.get(data_source_id)

    def update_metadata(
        self,
        data_source_id: str,
        description: str | None = None,
        description_source: MetadataSource | None = None,
        synonyms: list[str] | None = None,
    ) -> None:
        self.updates.append(
            {
                "data_source_id": data_source_id,
                "description": description,
                "description_source": description_source,
                "synonyms": synonyms,
            }
        )
        existing = self._data.get(data_source_id)
        if existing is None:
            existing = DatasetMetadata(data_source_id=data_source_id)
            self._data[data_source_id] = existing
        if description is not None:
            existing.description = description
        if description_source is not None:
            existing.description_source = description_source
        if synonyms is not None:
            existing.synonyms = synonyms


class TestReconcileMetadata:
    """Tests for reconcile_metadata function."""

    def test_new_dataset_imports_all_metadata(self) -> None:
        """When no existing metadata, import everything."""
        store = MockMetadataStore()
        datasets = [
            ResolvedDataset(
                dataset_name="orders",
                data_source_id="ds-123",
                description="Order database",
                synonyms=["sales", "transactions"],
            )
        ]

        result = reconcile_metadata(datasets, store)

        assert result.datasets_updated == 1
        assert len(store.updates) == 1
        assert store.updates[0]["description"] == "Order database"
        assert store.updates[0]["synonyms"] == ["sales", "transactions"]

    def test_ai_description_overwritten_by_osi(self) -> None:
        """OSI import overwrites AI-generated descriptions."""
        store = MockMetadataStore(
            data={
                "ds-123": DatasetMetadata(
                    data_source_id="ds-123",
                    description="AI generated desc",
                    description_source=MetadataSource.AI_GENERATED,
                    synonyms=[],
                )
            }
        )
        datasets = [
            ResolvedDataset(
                dataset_name="orders",
                data_source_id="ds-123",
                description="OSI imported desc",
            )
        ]

        result = reconcile_metadata(datasets, store)

        assert result.datasets_updated == 1
        assert store.updates[0]["description"] == "OSI imported desc"
        assert store.updates[0]["description_source"] == MetadataSource.OSI_IMPORTED

    def test_steward_description_never_overwritten(self) -> None:
        """Steward descriptions are never overwritten by OSI import."""
        store = MockMetadataStore(
            data={
                "ds-123": DatasetMetadata(
                    data_source_id="ds-123",
                    description="Steward curated desc",
                    description_source=MetadataSource.STEWARD,
                    synonyms=[],
                )
            }
        )
        datasets = [
            ResolvedDataset(
                dataset_name="orders",
                data_source_id="ds-123",
                description="OSI wants to overwrite",
            )
        ]

        result = reconcile_metadata(datasets, store)

        assert result.datasets_updated == 0
        assert len(result.warnings) == 1
        assert "steward" in result.warnings[0].lower()

    def test_osi_description_not_overwritten_by_osi(self) -> None:
        """Existing OSI-imported description is not overwritten by another OSI import."""
        store = MockMetadataStore(
            data={
                "ds-123": DatasetMetadata(
                    data_source_id="ds-123",
                    description="Previous OSI desc",
                    description_source=MetadataSource.OSI_IMPORTED,
                    synonyms=[],
                )
            }
        )
        datasets = [
            ResolvedDataset(
                dataset_name="orders",
                data_source_id="ds-123",
                description="New OSI desc",
            )
        ]

        result = reconcile_metadata(datasets, store)

        # Same priority — no overwrite
        assert result.datasets_updated == 0

    def test_synonyms_union_no_duplicates(self) -> None:
        """Synonyms are merged with case-insensitive deduplication."""
        store = MockMetadataStore(
            data={
                "ds-123": DatasetMetadata(
                    data_source_id="ds-123",
                    description="Existing",
                    description_source=MetadataSource.STEWARD,
                    synonyms=["Sales", "revenue"],
                )
            }
        )
        datasets = [
            ResolvedDataset(
                dataset_name="orders",
                data_source_id="ds-123",
                synonyms=["sales", "transactions", "Revenue", "orders"],
            )
        ]

        result = reconcile_metadata(datasets, store)

        assert result.datasets_updated == 1
        # Only "transactions" and "orders" are new (sales/Revenue already exist)
        action = result.actions[0]
        assert set(action.added_synonyms) == {"transactions", "orders"}

    def test_empty_incoming_no_changes(self) -> None:
        """No changes when incoming dataset has no description or synonyms."""
        store = MockMetadataStore(
            data={
                "ds-123": DatasetMetadata(
                    data_source_id="ds-123",
                    description="Existing",
                    description_source=MetadataSource.STEWARD,
                    synonyms=["existing_syn"],
                )
            }
        )
        datasets = [ResolvedDataset(dataset_name="orders", data_source_id="ds-123")]

        result = reconcile_metadata(datasets, store)

        assert result.datasets_updated == 0
        assert len(store.updates) == 0

    def test_multiple_datasets_reconciled(self) -> None:
        """Multiple datasets are reconciled independently."""
        store = MockMetadataStore(
            data={
                "ds-orders": DatasetMetadata(
                    data_source_id="ds-orders",
                    description="AI desc",
                    description_source=MetadataSource.AI_GENERATED,
                    synonyms=["sales"],
                ),
                "ds-customers": DatasetMetadata(
                    data_source_id="ds-customers",
                    description="Steward desc",
                    description_source=MetadataSource.STEWARD,
                    synonyms=[],
                ),
            }
        )
        datasets = [
            ResolvedDataset(
                dataset_name="orders",
                data_source_id="ds-orders",
                description="OSI orders desc",
                synonyms=["transactions"],
            ),
            ResolvedDataset(
                dataset_name="customers",
                data_source_id="ds-customers",
                description="OSI customers desc",
                synonyms=["clients"],
            ),
        ]

        result = reconcile_metadata(datasets, store)

        # orders: description updated (AI→OSI) + synonym added
        # customers: description NOT updated (steward) + synonym added
        assert result.datasets_updated == 2
        assert len(result.warnings) == 1  # steward warning for customers

    def test_reconciliation_action_has_changes(self) -> None:
        """ReconciliationAction.has_changes property works correctly."""
        no_changes = ReconciliationAction(data_source_id="ds-1")
        assert not no_changes.has_changes

        with_desc = ReconciliationAction(data_source_id="ds-1", updated_description="new")
        assert with_desc.has_changes

        with_syns = ReconciliationAction(data_source_id="ds-1", added_synonyms=["a"])
        assert with_syns.has_changes

    def test_empty_datasets_list(self) -> None:
        """Empty input produces empty result."""
        store = MockMetadataStore()
        result = reconcile_metadata([], store)

        assert result.datasets_updated == 0
        assert len(result.actions) == 0
        assert len(result.warnings) == 0
