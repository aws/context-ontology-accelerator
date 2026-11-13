# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for dataset resolution module."""

from __future__ import annotations

import pytest
from coa_metrics.dataset_resolver import (
    DatasetResolutionResult,
    ResolvedDataset,
    resolve_datasets,
)
from coa_metrics.osi_parser import OsiDataset
from coa_metrics.validator import DataSourceLookup

pytestmark = pytest.mark.unit


class MockDataSourceLookup(DataSourceLookup):
    """Mock lookup with configurable data sources."""

    def __init__(self, sources: set[str] | None = None) -> None:
        self._sources = sources or set()

    def data_source_exists(self, data_source_id: str) -> bool:
        return data_source_id in self._sources

    def table_exists(self, data_source_id: str, table_name: str) -> bool:
        return False

    def get_table_columns(self, data_source_id: str, table_name: str) -> list[str] | None:
        return None


class TestResolveDatasets:
    """Tests for resolve_datasets function."""

    def test_resolve_with_explicit_id_success(self) -> None:
        datasets = [
            OsiDataset(name="orders_db", data_source_id="ds-abc123", description="Orders"),
        ]
        lookup = MockDataSourceLookup(sources={"ds-abc123"})

        result = resolve_datasets(datasets, lookup)

        assert result.success
        assert len(result.resolved) == 1
        assert result.resolved[0].dataset_name == "orders_db"
        assert result.resolved[0].data_source_id == "ds-abc123"
        assert result.resolved[0].description == "Orders"

    def test_resolve_with_explicit_id_not_found(self) -> None:
        datasets = [
            OsiDataset(name="orders_db", data_source_id="ds-nonexistent"),
        ]
        lookup = MockDataSourceLookup(sources={"ds-abc123"})

        result = resolve_datasets(datasets, lookup)

        assert not result.success
        assert len(result.errors) == 1
        assert result.errors[0].dataset_name == "orders_db"
        assert "does not exist" in result.errors[0].reason

    def test_resolve_by_name_match(self) -> None:
        datasets = [
            OsiDataset(name="my_data_source", description="Matched by name"),
        ]
        lookup = MockDataSourceLookup(sources={"my_data_source"})

        result = resolve_datasets(datasets, lookup)

        assert result.success
        assert len(result.resolved) == 1
        assert result.resolved[0].data_source_id == "my_data_source"

    def test_resolve_by_name_no_match(self) -> None:
        datasets = [
            OsiDataset(name="unknown_source"),
        ]
        lookup = MockDataSourceLookup(sources={"ds-abc123"})

        result = resolve_datasets(datasets, lookup)

        assert not result.success
        assert len(result.errors) == 1
        assert "does not match any onboarded data source" in result.errors[0].reason

    def test_resolve_multiple_datasets_all_success(self) -> None:
        datasets = [
            OsiDataset(name="orders", data_source_id="ds-orders"),
            OsiDataset(name="customers", data_source_id="ds-customers"),
            OsiDataset(name="products", data_source_id="ds-products"),
        ]
        lookup = MockDataSourceLookup(sources={"ds-orders", "ds-customers", "ds-products"})

        result = resolve_datasets(datasets, lookup)

        assert result.success
        assert len(result.resolved) == 3

    def test_resolve_multiple_datasets_partial_failure(self) -> None:
        datasets = [
            OsiDataset(name="orders", data_source_id="ds-orders"),
            OsiDataset(name="missing", data_source_id="ds-missing"),
        ]
        lookup = MockDataSourceLookup(sources={"ds-orders"})

        result = resolve_datasets(datasets, lookup)

        assert not result.success
        assert len(result.resolved) == 1
        assert len(result.errors) == 1
        assert result.resolved[0].dataset_name == "orders"
        assert result.errors[0].dataset_name == "missing"

    def test_resolve_empty_datasets_list(self) -> None:
        result = resolve_datasets([], MockDataSourceLookup())

        assert result.success
        assert len(result.resolved) == 0
        assert len(result.errors) == 0

    def test_get_data_source_id_found(self) -> None:
        result = DatasetResolutionResult(
            resolved=[
                ResolvedDataset(dataset_name="orders", data_source_id="ds-123"),
                ResolvedDataset(dataset_name="customers", data_source_id="ds-456"),
            ],
            errors=[],
        )

        assert result.get_data_source_id("orders") == "ds-123"
        assert result.get_data_source_id("customers") == "ds-456"

    def test_get_data_source_id_not_found(self) -> None:
        result = DatasetResolutionResult(resolved=[], errors=[])
        assert result.get_data_source_id("nonexistent") is None

    def test_preserves_synonyms(self) -> None:
        datasets = [
            OsiDataset(
                name="orders_db",
                data_source_id="ds-abc",
                synonyms=["sales_db", "order_system"],
            ),
        ]
        lookup = MockDataSourceLookup(sources={"ds-abc"})

        result = resolve_datasets(datasets, lookup)

        assert result.success
        assert result.resolved[0].synonyms == ["sales_db", "order_system"]
