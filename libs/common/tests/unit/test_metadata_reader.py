# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.metadata_store.reader."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_common.datazone_forms import FORM_TYPE_NAME, serialize_form
from coa_common.domain_models import Column, Table
from coa_common.metadata_store.base import AssetResult, SearchResult
from coa_common.metadata_store.reader import (
    _parse_asset,
    _qualified_name_from_asset,
    read_asset_names_for_datasource,
    read_assets_for_datasource,
)

pytestmark = pytest.mark.unit


def _make_table(name: str = "orders", ds: str = "ds-1") -> Table:
    return Table(
        name=name,
        database="sales",
        data_source_id=ds,
        columns=[Column(name="id", data_type="int", nullable=False)],
    )


class TestReadAssetsForDatasource:
    def test_raises_without_data_source_id(self):
        with pytest.raises(ValueError, match="data_source_id is required"):
            read_assets_for_datasource("dom-1", "proj-1", "")

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_filters_assets_by_datasource_prefix_and_parses(self, mock_client_cls):
        client = MagicMock()
        # One matching asset, one non-matching (wrong prefix)
        client.search_assets.return_value = SearchResult(
            items=[
                AssetResult(asset_id="a1", name="DS#ds-1:orders", project_id="proj-1"),
                AssetResult(asset_id="a2", name="DS#other:widgets", project_id="proj-1"),
            ],
            next_token=None,
        )
        table = _make_table()
        client.get_asset_forms.return_value = {
            "formsOutput": [{"formName": FORM_TYPE_NAME, "content": json.dumps(serialize_form(table))}]
        }
        mock_client_cls.return_value = client

        tables = read_assets_for_datasource("dom-1", "proj-1", "ds-1")

        assert len(tables) == 1
        assert tables[0].name == "orders"
        # Only the matching asset's forms are fetched
        client.get_asset_forms.assert_called_once_with(asset_id="a1")

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_paginates_across_next_token(self, mock_client_cls):
        client = MagicMock()
        client.search_assets.side_effect = [
            SearchResult(items=[AssetResult(asset_id="a1", name="DS#ds-1:t1", project_id="p")], next_token="tok"),
            SearchResult(items=[AssetResult(asset_id="a2", name="DS#ds-1:t2", project_id="p")], next_token=None),
        ]
        client.get_asset_forms.side_effect = [
            {"formsOutput": [{"formName": FORM_TYPE_NAME, "content": json.dumps(serialize_form(_make_table("t1")))}]},
            {"formsOutput": [{"formName": FORM_TYPE_NAME, "content": json.dumps(serialize_form(_make_table("t2")))}]},
        ]
        mock_client_cls.return_value = client

        tables = read_assets_for_datasource("dom-1", "proj-1", "ds-1")

        assert {t.name for t in tables} == {"t1", "t2"}
        assert client.search_assets.call_count == 2

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_skips_assets_that_fail_to_parse(self, mock_client_cls):
        client = MagicMock()
        client.search_assets.return_value = SearchResult(
            items=[AssetResult(asset_id="a1", name="DS#ds-1:broken", project_id="p")],
            next_token=None,
        )
        # No matching form -> _parse_asset returns None
        client.get_asset_forms.return_value = {"formsOutput": []}
        mock_client_cls.return_value = client

        tables = read_assets_for_datasource("dom-1", "proj-1", "ds-1")

        assert tables == []

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_search_failure_propagates(self, mock_client_cls):
        client = MagicMock()
        client.search_assets.side_effect = RuntimeError("boom")
        mock_client_cls.return_value = client

        with pytest.raises(RuntimeError, match="boom"):
            read_assets_for_datasource("dom-1", "proj-1", "ds-1")

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_accepts_data_source_id_already_prefixed(self, mock_client_cls):
        client = MagicMock()
        client.search_assets.return_value = SearchResult(items=[], next_token=None)
        mock_client_cls.return_value = client

        read_assets_for_datasource("dom-1", "proj-1", "DS#ds-1")

        # ds_key passed as search_text should not be double-prefixed
        _, kwargs = client.search_assets.call_args
        assert kwargs["search_text"] == "DS#ds-1"


class TestParseAsset:
    def test_returns_none_when_get_forms_raises(self):
        client = MagicMock()
        client.get_asset_forms.side_effect = RuntimeError("nope")

        result = _parse_asset(client, "a1", "DS#ds-1:orders", "ds-1")

        assert result is None

    def test_returns_none_on_invalid_json_content(self):
        client = MagicMock()
        client.get_asset_forms.return_value = {"formsOutput": [{"formName": FORM_TYPE_NAME, "content": "{not json"}]}

        result = _parse_asset(client, "a1", "DS#ds-1:orders", "ds-1")

        assert result is None

    def test_returns_none_when_form_type_absent(self):
        client = MagicMock()
        client.get_asset_forms.return_value = {"formsOutput": [{"formName": "OtherForm", "content": "{}"}]}

        result = _parse_asset(client, "a1", "DS#ds-1:orders", "ds-1")

        assert result is None

    def test_parses_matching_form_into_table(self):
        client = MagicMock()
        table = _make_table("customers")
        client.get_asset_forms.return_value = {
            "formsOutput": [{"formName": FORM_TYPE_NAME, "content": json.dumps(serialize_form(table))}]
        }

        result = _parse_asset(client, "a1", "DS#ds-1:customers", "ds-1")

        assert result is not None
        assert result.name == "customers"
        assert result.data_source_id == "ds-1"


class TestReadAssetNamesForDatasource:
    """The cheap half: table names from search pages, zero per-asset calls.

    ``read_assets_for_datasource`` must fetch one form per asset to build ``Table``
    objects. Callers that only need to know WHICH tables exist do not, and paying
    the per-asset cost for them put ``POST /metrics`` over API Gateway's 29s limit
    on an 88-table source.
    """

    def test_raises_without_data_source_id(self):
        with pytest.raises(ValueError, match="data_source_id is required"):
            read_asset_names_for_datasource("dom-1", "proj-1", "")

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_indexes_names_without_fetching_any_form(self, mock_client_cls):
        client = MagicMock()
        client.search_assets.return_value = SearchResult(
            items=[
                AssetResult(asset_id="a1", name="DS#ds-1:sales.orders", project_id="proj-1"),
                AssetResult(asset_id="a2", name="DS#ds-1:sales.customers", project_id="proj-1"),
                AssetResult(asset_id="a3", name="DS#other:widgets", project_id="proj-1"),
            ],
            next_token=None,
        )
        mock_client_cls.return_value = client

        names = read_asset_names_for_datasource("dom-1", "proj-1", "ds-1")

        assert names == {"sales.orders": "a1", "sales.customers": "a2"}
        client.get_asset_forms.assert_not_called()
        assert client.search_assets.call_count == 1

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_paginates_and_still_fetches_no_forms(self, mock_client_cls):
        client = MagicMock()
        client.search_assets.side_effect = [
            SearchResult(
                items=[AssetResult(asset_id=f"a{i}", name=f"DS#ds-1:public.t{i}", project_id="p") for i in range(50)],
                next_token="tok",
            ),
            SearchResult(
                items=[AssetResult(asset_id="a50", name="DS#ds-1:public.t50", project_id="p")],
                next_token=None,
            ),
        ]
        mock_client_cls.return_value = client

        names = read_asset_names_for_datasource("dom-1", "proj-1", "ds-1")

        assert len(names) == 51
        # 51 assets, 2 search calls, still zero per-asset calls.
        assert client.search_assets.call_count == 2
        client.get_asset_forms.assert_not_called()

    @patch("coa_common.metadata_store.reader.SMUSClient")
    def test_search_failure_propagates(self, mock_client_cls):
        """Callers must be able to tell "no tables" from "could not look" — an
        empty mapping on failure would make absence look provable."""
        client = MagicMock()
        client.search_assets.side_effect = RuntimeError("datazone down")
        mock_client_cls.return_value = client

        with pytest.raises(RuntimeError, match="datazone down"):
            read_asset_names_for_datasource("dom-1", "proj-1", "ds-1")

    def test_accepts_an_already_prefixed_data_source_id(self):
        with patch("coa_common.metadata_store.reader.SMUSClient") as cls:
            client = MagicMock()
            client.search_assets.return_value = SearchResult(
                items=[AssetResult(asset_id="a1", name="DS#ds-1:public.orders", project_id="p")],
                next_token=None,
            )
            cls.return_value = client

            assert read_asset_names_for_datasource("dom-1", "proj-1", "DS#ds-1") == {"public.orders": "a1"}


class TestQualifiedNameFromAsset:
    """Asset names are ``DS#{sourceId}:{database}.{table}``; the index key is the
    lower-cased ``{database}.{table}`` so same-named tables in different databases
    stay distinct."""

    @pytest.mark.parametrize(
        ("asset_name", "expected"),
        [
            ("DS#ds-1:public.orders", "public.orders"),
            # Lower-cased so lookups are case-insensitive.
            ("DS#ds-1:PC_Insurance.Claim_Amount", "pc_insurance.claim_amount"),
            # A dot in the table name is preserved in the qualified key.
            ("DS#ds-1:public.odd.name", "public.odd.name"),
            # No database segment: the bare table is the whole remainder.
            ("DS#ds-1:orders", "orders"),
        ],
    )
    def test_parses_qualified_name(self, asset_name, expected):
        assert _qualified_name_from_asset(asset_name, "DS#ds-1") == expected
