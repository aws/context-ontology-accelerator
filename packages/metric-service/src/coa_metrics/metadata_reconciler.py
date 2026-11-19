# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dataset metadata reconciliation for OSI import.

Implements additive merge of dataset metadata with priority:
  STEWARD > OSI_IMPORTED > AI_GENERATED

Rules:
- Synonyms: union of existing + incoming (no duplicates, case-insensitive dedup)
- Descriptions: only overwrite if current source priority is lower
- Steward edits are NEVER overwritten by import
- New metadata from OSI is additive — it enriches, never replaces
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

import structlog

from .dataset_resolver import ResolvedDataset

logger = structlog.get_logger(__name__)


class MetadataSource(IntEnum):
    """Priority of metadata sources. Higher value = higher priority."""

    AI_GENERATED = 1
    OSI_IMPORTED = 2
    STEWARD = 3


@dataclass
class DatasetMetadata:
    """Current metadata state for a dataset."""

    data_source_id: str
    description: str = ""
    description_source: MetadataSource = MetadataSource.AI_GENERATED
    synonyms: list[str] = field(default_factory=list)


@dataclass
class ReconciliationAction:
    """A single reconciliation action to apply."""

    data_source_id: str
    updated_description: str | None = None
    added_synonyms: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Whether this action updates the description or adds synonyms."""
        return self.updated_description is not None or len(self.added_synonyms) > 0


@dataclass
class ReconciliationResult:
    """Result of reconciling all resolved datasets."""

    actions: list[ReconciliationAction]
    warnings: list[str]

    @property
    def datasets_updated(self) -> int:
        """Number of reconciliation actions that carry a change."""
        return sum(1 for a in self.actions if a.has_changes)


class MetadataStore:
    """Interface for reading/writing dataset metadata.

    Implementations can use DynamoDB, SageMaker Catalog, or mock data.
    """

    def get_metadata(self, data_source_id: str) -> DatasetMetadata | None:
        """Get current metadata for a data source. Returns None if not found."""
        raise NotImplementedError

    def update_metadata(
        self,
        data_source_id: str,
        description: str | None = None,
        description_source: MetadataSource | None = None,
        synonyms: list[str] | None = None,
    ) -> None:
        """Update metadata for a data source."""
        raise NotImplementedError


def reconcile_metadata(
    resolved_datasets: list[ResolvedDataset],
    store: MetadataStore,
) -> ReconciliationResult:
    """Reconcile OSI dataset metadata with existing onboarded metadata.

    Applies additive merge with priority STEWARD > OSI_IMPORTED > AI_GENERATED:
    - Synonyms: union (case-insensitive dedup)
    - Description: only update if current priority < OSI_IMPORTED

    """
    actions: list[ReconciliationAction] = []
    warnings: list[str] = []

    for dataset in resolved_datasets:
        action = _reconcile_single(dataset, store, warnings)
        actions.append(action)

        if action.has_changes:
            _apply_action(action, dataset, store)

    return ReconciliationResult(actions=actions, warnings=warnings)


def _reconcile_single(
    dataset: ResolvedDataset,
    store: MetadataStore,
    warnings: list[str],
) -> ReconciliationAction:
    """Reconcile a single dataset's metadata."""
    existing = store.get_metadata(dataset.data_source_id)
    action = ReconciliationAction(data_source_id=dataset.data_source_id)

    if existing is None:
        if dataset.description:
            action.updated_description = dataset.description
        if dataset.synonyms:
            action.added_synonyms = list(dataset.synonyms)
        logger.info(
            "metadata_reconcile_new",
            data_source_id=dataset.data_source_id,
            dataset=dataset.dataset_name,
        )
        return action

    if dataset.description:
        if existing.description_source < MetadataSource.OSI_IMPORTED:
            action.updated_description = dataset.description
            logger.info(
                "metadata_description_updated",
                data_source_id=dataset.data_source_id,
                old_source=existing.description_source.name,
                new_source=MetadataSource.OSI_IMPORTED.name,
            )
        elif existing.description_source >= MetadataSource.STEWARD:
            warnings.append(
                f"Dataset '{dataset.dataset_name}': description not updated "
                f"(existing steward description takes priority)"
            )
            logger.info(
                "metadata_description_preserved",
                data_source_id=dataset.data_source_id,
                reason="steward_priority",
            )

    if dataset.synonyms:
        existing_synonyms = existing.synonyms
        existing_lower = {s.lower() for s in existing_synonyms}
        new_synonyms = [s for s in dataset.synonyms if s.lower() not in existing_lower]
        if new_synonyms:
            action.added_synonyms = new_synonyms
            logger.info(
                "metadata_synonyms_added",
                data_source_id=dataset.data_source_id,
                count=len(new_synonyms),
            )

    return action


def _apply_action(
    action: ReconciliationAction,
    dataset: ResolvedDataset,
    store: MetadataStore,
) -> None:
    """Apply a reconciliation action to the metadata store."""
    existing = store.get_metadata(action.data_source_id)

    updated_synonyms: list[str] | None = None
    if action.added_synonyms:
        current = existing.synonyms if existing else []
        updated_synonyms = current + action.added_synonyms

    description = action.updated_description
    description_source = MetadataSource.OSI_IMPORTED if description else None

    store.update_metadata(
        data_source_id=action.data_source_id,
        description=description,
        description_source=description_source,
        synonyms=updated_synonyms,
    )

    logger.info(
        "metadata_reconcile_applied",
        data_source_id=action.data_source_id,
        dataset=dataset.dataset_name,
        description_updated=description is not None,
        synonyms_added=len(action.added_synonyms),
    )
