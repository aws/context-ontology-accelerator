# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for build_data_source_lookup and namespace project resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

_FULL_ENV = {
    "DATA_SOURCES_TABLE": "sources-tbl",
    "NAMESPACES_TABLE": "ns-tbl",
    "SMUS_DOMAIN_ID": "dzd-123",
    "AWS_REGION": "us-east-1",
}


class TestBuildDataSourceLookup:
    """Tests for build_data_source_lookup config gating and resolution."""

    def test_missing_config_returns_none(self):
        from coa_metrics.data_source_lookup_factory import build_data_source_lookup

        # No env vars set → configuration incomplete → None.
        with patch.dict("os.environ", {}, clear=True):
            assert build_data_source_lookup("ns-1") is None

    def test_partial_config_returns_none(self):
        from coa_metrics.data_source_lookup_factory import build_data_source_lookup

        partial = {"DATA_SOURCES_TABLE": "sources-tbl"}  # missing NAMESPACES_TABLE + SMUS_DOMAIN_ID
        with patch.dict("os.environ", partial, clear=True):
            assert build_data_source_lookup("ns-1") is None

    @patch("coa_metrics.data_source_lookup_factory._resolve_project_id", return_value=None)
    def test_unresolved_project_returns_none(self, mock_resolve):
        from coa_metrics.data_source_lookup_factory import build_data_source_lookup

        with patch.dict("os.environ", _FULL_ENV, clear=True):
            assert build_data_source_lookup("ns-1") is None
        mock_resolve.assert_called_once_with("ns-1", "ns-tbl", "us-east-1")

    @patch("coa_metrics.data_source_lookup_factory._resolve_project_id", return_value="prj-9")
    def test_returns_smus_lookup_when_configured(self, mock_resolve):
        from coa_metrics.data_source_lookup_factory import build_data_source_lookup
        from coa_metrics.lookups import SmusCatalogDataSourceLookup

        with patch.dict("os.environ", _FULL_ENV, clear=True):
            lookup = build_data_source_lookup("ns-1")

        assert isinstance(lookup, SmusCatalogDataSourceLookup)


class TestResolveProjectId:
    """Tests for unprovisioned namespaces versus registry failures."""

    @patch("coa_metrics.data_source_lookup_factory.DynamoDBDAO")
    def test_returns_project_id_when_present(self, mock_dao_cls):
        from coa_metrics.data_source_lookup_factory import _resolve_project_id

        dao = MagicMock()
        dao.get.return_value = {"dataZoneProjectId": "prj-42"}
        mock_dao_cls.return_value = dao

        assert _resolve_project_id("ns-1", "ns-tbl", "us-east-1") == "prj-42"
        dao.get.assert_called_once_with({"PK": "NS#ns-1", "SK": "METADATA"})

    @patch("coa_metrics.data_source_lookup_factory.DynamoDBDAO")
    def test_missing_item_returns_none(self, mock_dao_cls):
        from coa_metrics.data_source_lookup_factory import _resolve_project_id

        dao = MagicMock()
        dao.get.return_value = None
        mock_dao_cls.return_value = dao

        assert _resolve_project_id("ns-1", "ns-tbl", "us-east-1") is None

    @patch("coa_metrics.data_source_lookup_factory.DynamoDBDAO")
    def test_item_without_project_id_returns_none(self, mock_dao_cls):
        from coa_metrics.data_source_lookup_factory import _resolve_project_id

        dao = MagicMock()
        dao.get.return_value = {"PK": "NS#ns-1", "SK": "METADATA"}  # no dataZoneProjectId
        mock_dao_cls.return_value = dao

        assert _resolve_project_id("ns-1", "ns-tbl", "us-east-1") is None

    @patch("coa_metrics.data_source_lookup_factory.DynamoDBDAO")
    def test_client_error_propagates(self, mock_dao_cls):
        from coa_metrics.data_source_lookup_factory import _resolve_project_id

        dao = MagicMock()
        dao.get.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}}, "GetItem"
        )
        mock_dao_cls.return_value = dao

        with pytest.raises(ClientError):
            _resolve_project_id("ns-1", "ns-tbl", "us-east-1")

    @patch("coa_metrics.data_source_lookup_factory.DynamoDBDAO")
    def test_unexpected_exception_propagates(self, mock_dao_cls):
        from coa_metrics.data_source_lookup_factory import _resolve_project_id

        dao = MagicMock()
        dao.get.side_effect = RuntimeError("boom")
        mock_dao_cls.return_value = dao

        with pytest.raises(RuntimeError, match="boom"):
            _resolve_project_id("ns-1", "ns-tbl", "us-east-1")
