# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for database/metadata_writer.py — DataZone asset persistence."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def _make_table(table_id="mydb.mytable", name="mytable", database="mydb", description="A table"):
    table = MagicMock()
    table.table_id = table_id
    table.name = name
    table.database = database
    table.business_metadata = MagicMock()
    table.business_metadata.description = description
    table.technical_metadata = MagicMock()
    table.technical_metadata.format = "parquet"
    table.columns = []
    table.primary_key = None
    table.foreign_keys = []
    return table


def _make_metadata(tables=None):
    meta = MagicMock()
    meta.tables = tables if tables is not None else [_make_table()]
    return meta


def _search_page(items, next_token=None):
    """Build a single search-result page. next_token MUST be explicit — a bare
    MagicMock has a truthy .next_token that would loop the paginator forever."""
    return MagicMock(items=items, next_token=next_token)


def _existing_asset(name, asset_id):
    a = MagicMock()
    a.name = name
    a.asset_id = asset_id
    return a


# ===================================================================
# write_to_datazone
# ===================================================================


@pytest.mark.unit
class TestWriteToDatazone:
    def test_creates_new_asset_when_not_exists(self):
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([])
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(),
                data_source_id="DS#src-001",
            )

        assert result["assets_created"] == 1
        assert result["assets_revised"] == 0
        mock_client.create_asset.assert_called_once()
        mock_client.create_asset_revision.assert_not_called()

    def test_revises_existing_asset(self):
        mock_client = MagicMock()
        # Prefetch returns the existing asset for this source.
        mock_client.search_assets.return_value = _search_page([_existing_asset("DS#src-001:mydb.mytable", "asset-abc")])
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(),
                data_source_id="DS#src-001",
            )

        assert result["assets_revised"] == 1
        assert result["assets_created"] == 0
        mock_client.create_asset_revision.assert_called_once()
        # De-N+1: only the bulk prefetch search runs, not a per-table search.
        mock_client.search_assets.assert_called_once()

    def test_prefetch_paginates_all_pages(self):
        """The existing-asset map must follow next_token across pages."""
        mock_client = MagicMock()
        mock_client.search_assets.side_effect = [
            _search_page([_existing_asset("DS#src-001:db.t0", "a0")], next_token="page2"),
            _search_page([_existing_asset("DS#src-001:db.t1", "a1")], next_token=None),
        ]
        mock_client_cls = MagicMock(return_value=mock_client)
        tables = [_make_table("db.t0", "t0"), _make_table("db.t1", "t1")]

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(tables),
                data_source_id="DS#src-001",
            )

        assert mock_client.search_assets.call_count == 2
        assert result["assets_revised"] == 2
        assert result["assets_created"] == 0

    def test_prefetch_ignores_other_source_assets(self):
        """Assets not prefixed with this source's DS# key must be ignored."""
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([_existing_asset("DS#OTHER:db.mytable", "other-1")])
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(),
                data_source_id="DS#src-001",
            )

        # Foreign-source asset ignored → treated as new → create.
        assert result["assets_created"] == 1
        mock_client.create_asset.assert_called_once()

    def test_multiple_tables_all_created(self):
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([])
        mock_client_cls = MagicMock(return_value=mock_client)

        tables = [_make_table(f"db.table{i}", f"table{i}") for i in range(3)]

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(tables),
                data_source_id="DS#src-001",
            )

        assert result["assets_created"] == 3
        assert result["assets_revised"] == 0
        assert mock_client.create_asset.call_count == 3

    def test_parallel_writes_complete_all_tables(self):
        """With parallelism > 1, every table is still written exactly once."""
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([])
        mock_client_cls = MagicMock(return_value=mock_client)

        tables = [_make_table(f"db.table{i}", f"table{i}") for i in range(50)]

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
            patch.dict(os.environ, {"DATAZONE_WRITE_PARALLELISM": "8"}),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(tables),
                data_source_id="DS#src-001",
            )

        assert result["assets_created"] == 50
        assert mock_client.create_asset.call_count == 50

    def test_missing_data_source_id_raises(self):
        from coa_sources.database.metadata_writer import write_to_datazone

        with pytest.raises(ValueError, match="data_source_id"):
            write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(),
                data_source_id="",
            )

    def test_asset_write_failure_raises_runtime_error(self):
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([])
        mock_client.create_asset.side_effect = Exception("DataZone error")
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            with pytest.raises(RuntimeError, match="Failed to write"):
                write_to_datazone(
                    domain_id="domain-123",
                    project_id="proj-123",
                    metadata=_make_metadata(),
                    data_source_id="DS#src-001",
                )

    def test_partial_failure_raises_with_count(self):
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([])

        # Deterministically fail exactly one table by name (thread-safe, unlike
        # a shared call counter under the ThreadPoolExecutor).
        def create_asset_side_effect(**kwargs):
            if kwargs.get("name") == "DS#src-001:db.table1":
                raise Exception("write failed")

        mock_client.create_asset.side_effect = create_asset_side_effect
        mock_client_cls = MagicMock(return_value=mock_client)

        tables = [_make_table(f"db.table{i}", f"table{i}") for i in range(3)]

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            with pytest.raises(RuntimeError, match="Failed to write 1"):
                write_to_datazone(
                    domain_id="domain-123",
                    project_id="proj-123",
                    metadata=_make_metadata(tables),
                    data_source_id="DS#src-001",
                )

    def test_empty_tables_returns_zero_counts(self):
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([])
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            result = write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata([]),
                data_source_id="DS#src-001",
            )

        assert result["assets_created"] == 0
        assert result["assets_revised"] == 0
        mock_client.create_asset.assert_not_called()
        # No tables → no prefetch search either.
        mock_client.search_assets.assert_not_called()

    def test_uses_project_access_role_arn_when_set(self):
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page([])
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
            patch.dict(os.environ, {"PROJECT_ACCESS_ROLE_ARN": "arn:aws:iam::123:role/test-role"}),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            write_to_datazone(
                domain_id="domain-123",
                project_id="proj-123",
                metadata=_make_metadata(),
                data_source_id="DS#src-001",
            )

        call_kwargs = mock_client_cls.call_args[1]
        assert call_kwargs["assume_role_arn"] == "arn:aws:iam::123:role/test-role"


# ===================================================================
# DataZone Search AccessDenied → actionable membership error
# ===================================================================


@pytest.mark.unit
class TestSearchAccessDeniedSurfacing:
    def test_access_denied_search_raises_actionable_error(self):
        from coa_common.metadata_store.exceptions import MetadataStoreError

        mock_client = MagicMock()
        mock_client.search_assets.side_effect = MetadataStoreError(
            "Failed to search assets: An error occurred (AccessDeniedException) when calling "
            "the Search operation: User is not permitted to perform operation: Search"
        )
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
            patch.dict(os.environ, {"PROJECT_ACCESS_ROLE_ARN": "arn:aws:iam::123:role/scan"}),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            with pytest.raises(MetadataStoreError, match="not a member of the namespace's DataZone project") as excinfo:
                write_to_datazone(
                    domain_id="domain-123",
                    project_id="proj-123",
                    metadata=_make_metadata(),
                    data_source_id="DS#src-001",
                )

        # User-facing message must not leak the role ARN or project id.
        message = str(excinfo.value)
        assert "arn:aws:iam::123:role/scan" not in message
        assert "proj-123" not in message

    def test_non_access_denied_search_error_propagates_unchanged(self):
        from coa_common.metadata_store.exceptions import MetadataStoreError

        mock_client = MagicMock()
        mock_client.search_assets.side_effect = MetadataStoreError("Failed to search assets: some transient boom")
        mock_client_cls = MagicMock(return_value=mock_client)

        with (
            patch("coa_sources.database.metadata_writer.SMUSClient", mock_client_cls),
            patch("coa_sources.database.metadata_writer.build_forms_input", return_value=[]),
        ):
            from coa_sources.database.metadata_writer import write_to_datazone

            with pytest.raises(MetadataStoreError, match="some transient boom"):
                write_to_datazone(
                    domain_id="domain-123",
                    project_id="proj-123",
                    metadata=_make_metadata(),
                    data_source_id="DS#src-001",
                )


# ===================================================================
# _find_existing_asset (retained single-asset lookup)
# ===================================================================


@pytest.mark.unit
class TestFindExistingAsset:
    # Looks the asset up by exact name via find_asset_by_name rather than a
    # top-1 relevance search: search scores every searchable attribute, so a full
    # asset name ranks siblings of the same source alongside the exact one and a
    # top-1 miss would re-create an asset that already exists.
    def test_returns_asset_id_when_found(self):
        mock_client = MagicMock()
        mock_client.find_asset_by_name.return_value = _existing_asset("DS#src-001:mydb.mytable", "asset-xyz")

        from coa_sources.database.metadata_writer import _find_existing_asset

        result = _find_existing_asset(mock_client, "proj-123", "DS#src-001:mydb.mytable")
        assert result == "asset-xyz"
        mock_client.find_asset_by_name.assert_called_once_with(project_id="proj-123", name="DS#src-001:mydb.mytable")

    def test_returns_none_when_not_found(self):
        mock_client = MagicMock()
        mock_client.find_asset_by_name.return_value = None

        from coa_sources.database.metadata_writer import _find_existing_asset

        result = _find_existing_asset(mock_client, "proj-123", "DS#src-001:mydb.mytable")
        assert result is None

    def test_returns_none_on_search_exception(self):
        mock_client = MagicMock()
        mock_client.find_asset_by_name.side_effect = Exception("search failed")

        from coa_sources.database.metadata_writer import _find_existing_asset

        result = _find_existing_asset(mock_client, "proj-123", "DS#src-001:mydb.mytable")
        assert result is None


# ===================================================================
# _build_existing_asset_map
# ===================================================================


@pytest.mark.unit
class TestBuildExistingAssetMap:
    def test_builds_map_filtering_by_prefix(self):
        mock_client = MagicMock()
        mock_client.search_assets.return_value = _search_page(
            [
                _existing_asset("DS#src-001:db.t0", "a0"),
                _existing_asset("DS#src-001:db.t1", "a1"),
                _existing_asset("DS#OTHER:db.t2", "a2"),  # different source, dropped
            ]
        )

        from coa_sources.database.metadata_writer import _build_existing_asset_map

        result = _build_existing_asset_map(mock_client, "proj-123", "DS#src-001")
        assert result == {"DS#src-001:db.t0": "a0", "DS#src-001:db.t1": "a1"}
