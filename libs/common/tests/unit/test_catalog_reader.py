# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for catalog_reader.read_approved_catalog — new sources table key schema."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX


@pytest.mark.unit
class TestReadApprovedCatalogKeySchema:
    """Tests that read_approved_catalog uses the new PK=NS#{ns}, SK=SRC#{id} schema."""

    def _make_dao(self, item: dict | None):
        dao = MagicMock()
        dao.get.return_value = item
        return dao

    @patch("coa_common.metadata_store.catalog_reader.read_assets_for_datasource")
    @patch("coa_common.metadata_store.catalog_reader.DynamoDBDAO")
    def test_looks_up_new_key_schema(self, mock_dao_cls, mock_read_assets):
        """DynamoDB lookup uses PK=NS#{namespaceId}, SK=SRC#{sourceId}."""
        from coa_common.metadata_store.catalog_reader import read_approved_catalog

        mock_dao = self._make_dao({"sourceType": "GLUE_DATABASE", "status": "APPROVED"})
        mock_dao_cls.return_value = mock_dao
        mock_read_assets.return_value = []

        read_approved_catalog(
            domain_id="dzd-123",
            project_id="proj-abc",
            namespace_id="ns-uuid",
            data_source_ids=["source-uuid"],
            datasources_table=f"{RESOURCE_PREFIX}-dev-sources",
        )

        mock_dao.get.assert_called_once_with({"PK": "NS#ns-uuid", "SK": "SRC#source-uuid"})

    @patch("coa_common.metadata_store.catalog_reader.read_assets_for_datasource")
    @patch("coa_common.metadata_store.catalog_reader.DynamoDBDAO")
    def test_strips_ds_prefix_from_id(self, mock_dao_cls, mock_read_assets):
        """DS# prefix is stripped before constructing the SRC# key."""
        from coa_common.metadata_store.catalog_reader import read_approved_catalog

        mock_dao = self._make_dao({"sourceType": "GLUE_DATABASE", "status": "APPROVED"})
        mock_dao_cls.return_value = mock_dao
        mock_read_assets.return_value = []

        read_approved_catalog(
            domain_id="dzd-123",
            project_id="proj-abc",
            namespace_id="ns-uuid",
            data_source_ids=["DS#source-uuid"],
            datasources_table=f"{RESOURCE_PREFIX}-dev-sources",
        )

        mock_dao.get.assert_called_once_with({"PK": "NS#ns-uuid", "SK": "SRC#source-uuid"})

    @patch("coa_common.metadata_store.catalog_reader.read_assets_for_datasource")
    @patch("coa_common.metadata_store.catalog_reader.DynamoDBDAO")
    def test_strips_src_prefix_from_id(self, mock_dao_cls, mock_read_assets):
        """SRC# prefix is stripped before constructing the SRC# key (avoids double prefix)."""
        from coa_common.metadata_store.catalog_reader import read_approved_catalog

        mock_dao = self._make_dao({"sourceType": "GLUE_DATABASE", "status": "APPROVED"})
        mock_dao_cls.return_value = mock_dao
        mock_read_assets.return_value = []

        read_approved_catalog(
            domain_id="dzd-123",
            project_id="proj-abc",
            namespace_id="ns-uuid",
            data_source_ids=["SRC#source-uuid"],
            datasources_table=f"{RESOURCE_PREFIX}-dev-sources",
        )

        mock_dao.get.assert_called_once_with({"PK": "NS#ns-uuid", "SK": "SRC#source-uuid"})

    @patch("coa_common.metadata_store.catalog_reader.read_assets_for_datasource")
    @patch("coa_common.metadata_store.catalog_reader.DynamoDBDAO")
    def test_skips_missing_source(self, mock_dao_cls, mock_read_assets):
        """Source not found in DynamoDB is skipped, not raised."""
        from coa_common.metadata_store.catalog_reader import read_approved_catalog

        mock_dao = self._make_dao(None)
        mock_dao_cls.return_value = mock_dao
        mock_read_assets.return_value = []

        result = read_approved_catalog(
            domain_id="dzd-123",
            project_id="proj-abc",
            namespace_id="ns-uuid",
            data_source_ids=["missing-uuid"],
            datasources_table=f"{RESOURCE_PREFIX}-dev-sources",
        )

        assert result == {"namespaceId": "ns-uuid", "sources": []}
        mock_read_assets.assert_not_called()

    @patch("coa_common.metadata_store.catalog_reader.read_assets_for_datasource")
    @patch("coa_common.metadata_store.catalog_reader.DynamoDBDAO")
    def test_returns_source_type_from_item(self, mock_dao_cls, mock_read_assets):
        """sourceType is read from the DynamoDB item and included in the output."""
        from coa_common.domain_models import ReviewStatus
        from coa_common.metadata_store.catalog_reader import read_approved_catalog

        mock_dao = self._make_dao({"sourceType": "JDBC_DATABASE", "status": "APPROVED"})
        mock_dao_cls.return_value = mock_dao

        col = MagicMock()
        col.business_metadata.review_status = ReviewStatus.APPROVED
        col.name = "id"
        col.data_type = "BIGINT"
        col.nullable = False

        table = MagicMock()
        table.business_metadata.review_status = ReviewStatus.APPROVED
        table.database = "mydb"
        table.database_description = ""
        table.name = "claims"
        table.columns = [col]
        table.primary_key.columns = []
        table.foreign_keys = []
        table.technical_metadata.column_count = 1
        table.technical_metadata.partition_keys = []
        table.technical_metadata.format = "parquet"
        table.technical_metadata.location = "s3://bucket/path"

        mock_read_assets.return_value = [table]

        result = read_approved_catalog(
            domain_id="dzd-123",
            project_id="proj-abc",
            namespace_id="ns-uuid",
            data_source_ids=["source-uuid"],
            datasources_table=f"{RESOURCE_PREFIX}-dev-sources",
        )

        assert len(result["sources"]) == 1
        assert result["sources"][0]["sourceType"] == "JDBC_DATABASE"
        assert result["sources"][0]["datasourceId"] == "source-uuid"

    @patch("coa_common.metadata_store.catalog_reader.read_assets_for_datasource")
    @patch("coa_common.metadata_store.catalog_reader.DynamoDBDAO")
    def test_skips_non_approved_source(self, mock_dao_cls, mock_read_assets):
        """Sources not in APPROVED status are skipped — no DataZone fetch."""
        from coa_common.metadata_store.catalog_reader import read_approved_catalog

        mock_dao = self._make_dao({"sourceType": "GLUE_DATABASE", "status": "PENDING_REVIEW"})
        mock_dao_cls.return_value = mock_dao

        result = read_approved_catalog(
            domain_id="dzd-123",
            project_id="proj-abc",
            namespace_id="ns-uuid",
            data_source_ids=["source-uuid"],
            datasources_table=f"{RESOURCE_PREFIX}-dev-sources",
        )

        assert result == {"namespaceId": "ns-uuid", "sources": []}
        mock_read_assets.assert_not_called()


@pytest.mark.unit
class TestInductionFormatGeneratedFields:
    """The induction projection carries the field-level generated-text proof."""

    def test_table_and_column_generated_fields_are_projected(self):
        from coa_common.domain_models import BusinessMetadata, Column, ReviewStatus, Table
        from coa_common.metadata_store.catalog_reader import _table_to_induction_format

        table = Table(
            name="orders",
            database="shop",
            business_metadata=BusinessMetadata(
                description="注文",
                synonyms=["受注"],
                review_status=ReviewStatus.APPROVED,
                generated_fields=["description", "synonyms"],
            ),
            columns=[
                Column(
                    name="amount",
                    data_type="int",
                    business_metadata=BusinessMetadata(
                        description="金額",
                        review_status=ReviewStatus.APPROVED,
                        generated_fields=["description"],
                    ),
                ),
                Column(
                    name="note",
                    data_type="varchar",
                    business_metadata=BusinessMetadata(description="steward text", review_status=ReviewStatus.APPROVED),
                ),
            ],
        )

        projected = _table_to_induction_format(table)

        assert projected["businessMetadata"]["generatedFields"] == ["description", "synonyms"]
        by_name = {c["name"]: c for c in projected["columns"]}
        assert by_name["amount"]["businessMetadata"]["generatedFields"] == ["description"]
        # No proof recorded: an empty set, which proves nothing.
        assert by_name["note"]["businessMetadata"]["generatedFields"] == []
