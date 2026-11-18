# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Dataset resolution for OSI import.

Resolves OSI dataset references to onboarded data sources.
MVP rule: ALL datasets must resolve — error on any unresolved.

Resolution strategy:
1. If the dataset has a `data_source_id` in custom_extensions, verify it exists.
2. If not, attempt to match by dataset name against known data source names.
3. Unresolved datasets produce hard errors that block the import.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from .osi_parser import OsiDataset
from .validator import DataSourceLookup

logger = structlog.get_logger(__name__)


@dataclass
class ResolvedDataset:
    """A dataset successfully resolved to an onboarded data source."""

    dataset_name: str
    data_source_id: str
    description: str = ""
    synonyms: list[str] = field(default_factory=list)


@dataclass
class ResolutionError:
    """A dataset that could not be resolved."""

    dataset_name: str
    reason: str


@dataclass
class DatasetResolutionResult:
    """Result of resolving all datasets in an OSI document."""

    resolved: list[ResolvedDataset]
    errors: list[ResolutionError]

    @property
    def success(self) -> bool:
        """Whether every dataset resolved without errors."""
        return len(self.errors) == 0

    def get_data_source_id(self, dataset_name: str) -> str | None:
        """Look up the resolved data source ID for a dataset name."""
        for r in self.resolved:
            if r.dataset_name == dataset_name:
                return r.data_source_id
        return None


def resolve_datasets(
    datasets: list[OsiDataset],
    lookup: DataSourceLookup,
) -> DatasetResolutionResult:
    """Resolve OSI dataset references to onboarded data sources.

    MVP: all datasets must resolve to an existing data source.
    Resolution uses the dataset's `data_source_id` field if present,
    otherwise attempts name-based matching.
    """
    resolved: list[ResolvedDataset] = []
    errors: list[ResolutionError] = []

    for dataset in datasets:
        data_source_id = dataset.data_source_id

        if data_source_id:
            if lookup.data_source_exists(data_source_id):
                resolved.append(
                    ResolvedDataset(
                        dataset_name=dataset.name,
                        data_source_id=data_source_id,
                        description=dataset.description,
                        synonyms=dataset.synonyms,
                    )
                )
                logger.info(
                    "dataset_resolved",
                    dataset=dataset.name,
                    data_source_id=data_source_id,
                    method="explicit_id",
                )
            else:
                errors.append(
                    ResolutionError(
                        dataset_name=dataset.name,
                        reason=f"Data source '{data_source_id}' does not exist",
                    )
                )
                logger.warning(
                    "dataset_unresolved",
                    dataset=dataset.name,
                    data_source_id=data_source_id,
                    reason="not_found",
                )
        else:
            # Convention: dataset name may match a data source ID directly
            if lookup.data_source_exists(dataset.name):
                resolved.append(
                    ResolvedDataset(
                        dataset_name=dataset.name,
                        data_source_id=dataset.name,
                        description=dataset.description,
                        synonyms=dataset.synonyms,
                    )
                )
                logger.info(
                    "dataset_resolved",
                    dataset=dataset.name,
                    data_source_id=dataset.name,
                    method="name_match",
                )
            else:
                errors.append(
                    ResolutionError(
                        dataset_name=dataset.name,
                        reason=(
                            f"No data_source_id specified and dataset name "
                            f"'{dataset.name}' does not match any onboarded data source"
                        ),
                    )
                )
                logger.warning(
                    "dataset_unresolved",
                    dataset=dataset.name,
                    reason="no_id_no_name_match",
                )

    return DatasetResolutionResult(resolved=resolved, errors=errors)
