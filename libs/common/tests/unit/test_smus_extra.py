# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Additional unit tests for SMUSClient methods not covered elsewhere."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.metadata_store import MetadataStoreError, SMUSClient
from coa_common.metadata_store.base import AssetResult

pytestmark = pytest.mark.unit

DOMAIN = "dzd_123"


class TestSMUSClientInit:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_assume_role_builds_scoped_session(self, mock_boto3):
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AK",
                "SecretAccessKey": "SK",
                "SessionToken": "TOK",
            }
        }
        session = MagicMock()
        datazone = MagicMock()
        session.client.return_value = datazone
        mock_boto3.client.return_value = sts
        mock_boto3.Session.return_value = session

        client = SMUSClient(
            domain_id=DOMAIN,
            region_name="us-east-1",
            assume_role_arn="arn:aws:iam::123:role/r",
            session_name="my-session",
        )

        mock_boto3.client.assert_called_once_with("sts", region_name="us-east-1")
        sts.assume_role.assert_called_once_with(RoleArn="arn:aws:iam::123:role/r", RoleSessionName="my-session")
        mock_boto3.Session.assert_called_once_with(
            aws_access_key_id="AK",
            aws_secret_access_key="SK",
            aws_session_token="TOK",
            region_name="us-east-1",
        )
        # DataZone client comes from the scoped session, not the default chain
        assert client._client is datazone

    @patch("coa_common.metadata_store.smus.boto3")
    def test_no_assume_role_uses_default_client(self, mock_boto3):
        datazone = MagicMock()
        mock_boto3.client.return_value = datazone

        client = SMUSClient(domain_id=DOMAIN, region_name="eu-west-1")

        mock_boto3.client.assert_called_once()
        args, kwargs = mock_boto3.client.call_args
        assert args == ("datazone",)
        assert kwargs["region_name"] == "eu-west-1"
        # Adaptive retry config is attached (issue #857) — see TestDataZoneRetryConfig.
        assert kwargs.get("config") is not None
        assert client._client is datazone


class TestDataZoneRetryConfig:
    """The DataZone client must be built with an adaptive, high-attempt retry
    config so per-table CreateAsset writes survive sustained account-wide
    TooManyRequestsException throttling (issue #857) instead of exhausting the
    botocore default (~4 attempts) and dead-ending discovery."""

    @patch("coa_common.metadata_store.smus.boto3")
    def test_default_client_uses_adaptive_retry(self, mock_boto3):
        datazone = MagicMock()
        mock_boto3.client.return_value = datazone

        SMUSClient(domain_id=DOMAIN, region_name="us-east-1")

        _, kwargs = mock_boto3.client.call_args
        config = kwargs.get("config")
        assert config is not None, "DataZone client built with no botocore Config (no retry tuning)"
        assert config.retries.get("mode") == "adaptive"
        assert config.retries.get("max_attempts", 0) >= 10

    @patch("coa_common.metadata_store.smus.boto3")
    def test_assumed_role_client_uses_adaptive_retry(self, mock_boto3):
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "TOK"}
        }
        session = MagicMock()
        session.client.return_value = MagicMock()
        mock_boto3.client.return_value = sts
        mock_boto3.Session.return_value = session

        SMUSClient(domain_id=DOMAIN, region_name="us-east-1", assume_role_arn="arn:aws:iam::123:role/r")

        # DataZone client comes from the scoped session; it must carry the config.
        _, kwargs = session.client.call_args
        config = kwargs.get("config")
        assert config is not None, "Assumed-role DataZone client built with no botocore Config"
        assert config.retries.get("mode") == "adaptive"
        assert config.retries.get("max_attempts", 0) >= 10


class TestCreateProjectProfileId:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_passes_project_profile_id(self, mock_boto3):
        dz = MagicMock()
        dz.create_project.return_value = {"id": "p1", "name": "n"}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).create_project(name="n", project_profile_id="prof-1")

        _, kwargs = dz.create_project.call_args
        assert kwargs["projectProfileId"] == "prof-1"


class TestCreateAssetExtra:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_passes_description_and_forms(self, mock_boto3):
        dz = MagicMock()
        dz.create_asset.return_value = {"id": "a1", "name": "n", "owningProjectId": "p"}
        mock_boto3.client.return_value = dz
        forms = [{"formName": "F", "content": "{}", "typeIdentifier": "F"}]

        SMUSClient(domain_id=DOMAIN).create_asset(
            project_id="p", name="n", type_identifier="t", description="hello", forms_input=forms
        )

        _, kwargs = dz.create_asset.call_args
        assert kwargs["description"] == "hello"
        assert kwargs["formsInput"] == forms

    @patch("coa_common.metadata_store.smus.boto3")
    def test_truncates_long_description(self, mock_boto3):
        dz = MagicMock()
        dz.create_asset.return_value = {"id": "a1", "name": "n", "owningProjectId": "p"}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).create_asset(project_id="p", name="n", type_identifier="t", description="x" * 3000)

        _, kwargs = dz.create_asset.call_args
        assert len(kwargs["description"]) == 2048

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_when_no_asset_id(self, mock_boto3):
        dz = MagicMock()
        dz.create_asset.return_value = {"name": "n"}
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError, match="returned no asset ID"):
            SMUSClient(domain_id=DOMAIN).create_asset(project_id="p", name="n", type_identifier="t")


class TestCreateAssetRevision:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_returns_asset_result(self, mock_boto3):
        dz = MagicMock()
        dz.create_asset_revision.return_value = {"id": "a2", "name": "n2", "owningProjectId": "p"}
        mock_boto3.client.return_value = dz

        result = SMUSClient(domain_id=DOMAIN).create_asset_revision(asset_id="a1", name="n2")

        assert result == AssetResult(asset_id="a2", name="n2", project_id="p")

    @patch("coa_common.metadata_store.smus.boto3")
    def test_truncates_long_description(self, mock_boto3):
        dz = MagicMock()
        dz.create_asset_revision.return_value = {"id": "a2", "name": "n2"}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).create_asset_revision(asset_id="a1", name="n2", description="y" * 5000)

        _, kwargs = dz.create_asset_revision.call_args
        assert len(kwargs["description"]) == 2048

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_when_no_asset_id(self, mock_boto3):
        dz = MagicMock()
        dz.create_asset_revision.return_value = {"name": "n2"}
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError, match="returned no asset ID"):
            SMUSClient(domain_id=DOMAIN).create_asset_revision(asset_id="a1", name="n2")

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_api_failure(self, mock_boto3):
        dz = MagicMock()
        dz.create_asset_revision.side_effect = Exception("boom")
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError, match="Failed to create asset revision"):
            SMUSClient(domain_id=DOMAIN).create_asset_revision(asset_id="a1", name="n2")


class TestDeleteAssetFailure:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_api_failure(self, mock_boto3):
        dz = MagicMock()
        dz.delete_asset.side_effect = Exception("boom")
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError, match="Failed to delete asset"):
            SMUSClient(domain_id=DOMAIN).delete_asset(asset_id="a1")


class TestSearchAssets:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_maps_items_and_next_token(self, mock_boto3):
        dz = MagicMock()
        dz.search.return_value = {
            "items": [
                {"assetItem": {"identifier": "a1", "name": "n1", "owningProjectId": "p1"}},
                {"assetItem": {"identifier": "a2"}},
                {"notAnAsset": {}},  # skipped
                {"assetItem": {"name": "no-id"}},  # skipped (no identifier)
            ],
            "nextToken": "tok",
        }
        mock_boto3.client.return_value = dz

        result = SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="DS#ds-1")

        assert result.next_token == "tok"
        assert [i.asset_id for i in result.items] == ["a1", "a2"]
        assert result.items[0].name == "n1"
        # Falls back to the passed project_id when owningProjectId absent
        assert result.items[1].project_id == "p1"

    @patch("coa_common.metadata_store.smus.boto3")
    def test_passes_next_token_when_provided(self, mock_boto3):
        dz = MagicMock()
        dz.search.return_value = {"items": []}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q", next_token="page2")

        _, kwargs = dz.search.call_args
        assert kwargs["nextToken"] == "page2"
        assert kwargs["searchScope"] == "ASSET"

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_api_failure(self, mock_boto3):
        dz = MagicMock()
        dz.search.side_effect = Exception("boom")
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError, match="Failed to search assets"):
            SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q")


class TestSearchAssetsIncludeForms:
    """Tests for the include_forms kwarg on search_assets."""

    @patch("coa_common.metadata_store.smus.boto3")
    def test_additional_attributes_present_when_include_forms_true(self, mock_boto3):
        dz = MagicMock()
        dz.search.return_value = {"items": []}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q", include_forms=True)

        _, kwargs = dz.search.call_args
        assert kwargs["additionalAttributes"] == ["FORMS"]

    @patch("coa_common.metadata_store.smus.boto3")
    def test_additional_attributes_absent_when_include_forms_false(self, mock_boto3):
        dz = MagicMock()
        dz.search.return_value = {"items": []}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q", include_forms=False)

        _, kwargs = dz.search.call_args
        assert "additionalAttributes" not in kwargs

    @patch("coa_common.metadata_store.smus.boto3")
    def test_additional_attributes_absent_by_default(self, mock_boto3):
        dz = MagicMock()
        dz.search.return_value = {"items": []}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q")

        _, kwargs = dz.search.call_args
        assert "additionalAttributes" not in kwargs

    @patch("coa_common.metadata_store.smus.boto3")
    def test_forms_output_populated_from_response(self, mock_boto3):
        forms = [{"formName": "MyForm", "content": '{"col": "val"}'}]
        dz = MagicMock()
        dz.search.return_value = {
            "items": [
                {
                    "assetItem": {
                        "identifier": "a1",
                        "name": "n1",
                        "owningProjectId": "p1",
                        "additionalAttributes": {"formsOutput": forms},
                    }
                }
            ]
        }
        mock_boto3.client.return_value = dz

        result = SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q", include_forms=True)

        assert result.items[0].forms_output == forms

    @patch("coa_common.metadata_store.smus.boto3")
    def test_forms_output_none_when_include_forms_false(self, mock_boto3):
        forms = [{"formName": "MyForm", "content": '{"col": "val"}'}]
        dz = MagicMock()
        dz.search.return_value = {
            "items": [
                {
                    "assetItem": {
                        "identifier": "a1",
                        "name": "n1",
                        "owningProjectId": "p1",
                        "additionalAttributes": {"formsOutput": forms},
                    }
                }
            ]
        }
        mock_boto3.client.return_value = dz

        result = SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q", include_forms=False)

        assert result.items[0].forms_output is None

    @patch("coa_common.metadata_store.smus.boto3")
    def test_no_additional_attributes_key_tolerated(self, mock_boto3):
        """When the response item has no additionalAttributes key at all,
        forms_output should be None and no exception raised."""
        dz = MagicMock()
        dz.search.return_value = {
            "items": [
                {
                    "assetItem": {
                        "identifier": "a1",
                        "name": "n1",
                        "owningProjectId": "p1",
                    }
                }
            ]
        }
        mock_boto3.client.return_value = dz

        result = SMUSClient(domain_id=DOMAIN).search_assets(project_id="p1", search_text="q", include_forms=True)

        assert result.items[0].forms_output is None
        assert result.items[0].asset_id == "a1"


class TestGetAssetForms:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_returns_full_response_dict(self, mock_boto3):
        dz = MagicMock()
        dz.get_asset.return_value = {"id": "a1", "formsOutput": [{"formName": "F"}]}
        mock_boto3.client.return_value = dz

        result = SMUSClient(domain_id=DOMAIN).get_asset_forms(asset_id="a1")

        assert result["formsOutput"] == [{"formName": "F"}]

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_api_failure(self, mock_boto3):
        dz = MagicMock()
        dz.get_asset.side_effect = Exception("boom")
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError, match="Failed to get asset forms"):
            SMUSClient(domain_id=DOMAIN).get_asset_forms(asset_id="a1")


class TestGetProjectFailure:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_create_project_returns_error_message_from_args(self, mock_boto3):
        dz = MagicMock()
        dz.create_project.side_effect = Exception("underlying reason")
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError, match="underlying reason"):
            SMUSClient(domain_id=DOMAIN).create_project(name="n")


def _asset(name: str, asset_id: str) -> dict:
    return {"assetItem": {"identifier": asset_id, "name": name, "owningProjectId": "p1"}}


class TestFindAssetByName:
    """Exact-name asset lookup.

    A top-1 relevance search is not an exact lookup: DataZone scores every
    searchable attribute, so a full asset name such as ``DS#<id>:<db>.<table>``
    contributes terms shared by every asset of that source and the exact match
    is not reliably ranked first. These cover the resulting behaviours.
    """

    @patch("coa_common.metadata_store.smus.boto3")
    def test_restricts_matching_to_the_name_attribute(self, mock_boto3):
        # searchIn is what removes the ranking lottery; without it the other
        # searchable attributes (description, synonyms, glossary terms, ids) all
        # contribute to the score.
        dz = MagicMock()
        dz.search.return_value = {"items": [_asset("DS#s:db.orders", "a1")]}
        mock_boto3.client.return_value = dz

        SMUSClient(domain_id=DOMAIN).find_asset_by_name(project_id="p1", name="DS#s:db.orders")

        _, kwargs = dz.search.call_args
        assert kwargs["searchIn"] == [{"attribute": "name"}]

    @patch("coa_common.metadata_store.smus.boto3")
    def test_finds_the_exact_match_when_it_is_not_the_first_hit(self, mock_boto3):
        # The bug this replaces: a top-1 lookup returned the sibling and the
        # caller reported the table as missing.
        dz = MagicMock()
        dz.search.return_value = {
            "items": [
                _asset("DS#s:db.categories", "a-cat"),
                _asset("DS#s:db.order_items", "a-items"),
                _asset("DS#s:db.orders", "a-orders"),
            ]
        }
        mock_boto3.client.return_value = dz

        found = SMUSClient(domain_id=DOMAIN).find_asset_by_name(project_id="p1", name="DS#s:db.orders")

        assert found is not None
        assert found.asset_id == "a-orders"

    @patch("coa_common.metadata_store.smus.boto3")
    def test_pages_until_the_exact_match_is_found(self, mock_boto3):
        # Restricting to the name attribute is still not an exact match, so the
        # wanted asset can sit behind a page boundary.
        dz = MagicMock()
        dz.search.side_effect = [
            {"items": [_asset("DS#s:db.order_items", "a-items")], "nextToken": "p2"},
            {"items": [_asset("DS#s:db.orders", "a-orders")]},
        ]
        mock_boto3.client.return_value = dz

        found = SMUSClient(domain_id=DOMAIN).find_asset_by_name(project_id="p1", name="DS#s:db.orders")

        assert found is not None and found.asset_id == "a-orders"
        assert dz.search.call_count == 2
        assert dz.search.call_args_list[1][1]["nextToken"] == "p2"

    @patch("coa_common.metadata_store.smus.boto3")
    def test_returns_none_when_only_near_names_exist(self, mock_boto3):
        # A tokenised name matches relatives; only an exact name may be returned.
        dz = MagicMock()
        dz.search.return_value = {"items": [_asset("DS#s:db.orders_v2", "a-v2"), _asset("DS#s:db.order_items", "a-i")]}
        mock_boto3.client.return_value = dz

        assert SMUSClient(domain_id=DOMAIN).find_asset_by_name(project_id="p1", name="DS#s:db.orders") is None

    @patch("coa_common.metadata_store.smus.boto3")
    def test_stops_at_the_page_bound_rather_than_looping_forever(self, mock_boto3):
        # A name that prefixes many others can match widely; the bound keeps a
        # never-matching lookup from paging indefinitely.
        dz = MagicMock()
        dz.search.return_value = {"items": [_asset("DS#s:db.other", "a-o")], "nextToken": "more"}
        mock_boto3.client.return_value = dz

        found = SMUSClient(domain_id=DOMAIN).find_asset_by_name(project_id="p1", name="DS#s:db.orders", max_pages=3)

        assert found is None
        assert dz.search.call_count == 3

    @patch("coa_common.metadata_store.smus.boto3")
    def test_a_failed_search_raises_rather_than_reading_as_not_found(self, mock_boto3):
        # "Not found" must never be produced by a broken lookup: callers turn it
        # into a 404, or into skipping an enrichment write.
        dz = MagicMock()
        dz.search.side_effect = RuntimeError("throttled")
        mock_boto3.client.return_value = dz

        with pytest.raises(MetadataStoreError):
            SMUSClient(domain_id=DOMAIN).find_asset_by_name(project_id="p1", name="DS#s:db.orders")
