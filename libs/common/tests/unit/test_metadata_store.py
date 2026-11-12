# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for coa_common.metadata_store."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_common.metadata_store import (
    MetadataStoreClient,
    MetadataStoreError,
    ProjectResult,
    SMUSClient,
)

DOMAIN = "dzd_123"


# ---------------------------------------------------------------------------
# Abstract base class contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetadataStoreClientIsAbstract:
    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            MetadataStoreClient()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# SMUSClient — Projects
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateProject:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_returns_project_result(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.create_project.return_value = {"id": "proj_abc", "name": "550e8400-e29b-41d4-a716-446655440000"}
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        result = client.create_project(name="550e8400-e29b-41d4-a716-446655440000")

        assert result == ProjectResult(project_id="proj_abc", name="550e8400-e29b-41d4-a716-446655440000")
        mock_dz.create_project.assert_called_once_with(
            domainIdentifier=DOMAIN, name="550e8400-e29b-41d4-a716-446655440000"
        )

    @patch("coa_common.metadata_store.smus.boto3")
    def test_passes_description(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.create_project.return_value = {"id": "proj_abc", "name": "550e8400-e29b-41d4-a716-446655440000"}
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        client.create_project(name="550e8400-e29b-41d4-a716-446655440000", description="desc")

        mock_dz.create_project.assert_called_once_with(
            domainIdentifier=DOMAIN, name="550e8400-e29b-41d4-a716-446655440000", description="desc"
        )

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_api_failure(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.create_project.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "CreateProject",
        )
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="Failed to create"):
            client.create_project(name="550e8400-e29b-41d4-a716-446655440000")

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_missing_id(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.create_project.return_value = {"name": "550e8400-e29b-41d4-a716-446655440000"}
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="returned no project ID"):
            client.create_project(name="550e8400-e29b-41d4-a716-446655440000")

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_throttling(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.create_project.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "CreateProject",
        )
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="Failed to create"):
            client.create_project(name="550e8400-e29b-41d4-a716-446655440000")


@pytest.mark.unit
class TestDeleteProject:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_succeeds(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.delete_project.return_value = {}
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        client.delete_project(project_id="proj_abc")

        mock_dz.delete_project.assert_called_once_with(
            domainIdentifier=DOMAIN,
            identifier="proj_abc",
            skipDeletionCheck=False,
        )

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_active_assets(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.delete_project.side_effect = ClientError(
            {"Error": {"Code": "ConflictException", "Message": "active assets"}},
            "DeleteProject",
        )
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="Failed to delete"):
            client.delete_project(project_id="proj_abc")


@pytest.mark.unit
class TestCreateAsset:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_returns_asset_result(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.create_asset.return_value = {
            "id": "asset_1",
            "name": "my-asset",
            "owningProjectId": "proj_abc",
        }
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        result = client.create_asset(
            project_id="proj_abc",
            name="my-asset",
            type_identifier="custom-type",
        )

        assert result.asset_id == "asset_1"
        assert result.name == "my-asset"
        assert result.project_id == "proj_abc"

    @patch("coa_common.metadata_store.smus.boto3")
    def test_raises_on_api_failure(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.create_asset.side_effect = Exception("boom")
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="Failed to create.*asset"):
            client.create_asset(project_id="proj_abc", name="a", type_identifier="t")


@pytest.mark.unit
class TestDeleteAsset:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_succeeds(self, mock_boto3):
        mock_dz = MagicMock()
        mock_dz.delete_asset.return_value = {}
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        client.delete_asset(asset_id="asset_1")

        mock_dz.delete_asset.assert_called_once_with(domainIdentifier=DOMAIN, identifier="asset_1")


@pytest.mark.unit
class TestSleepMembershipBackoff:
    """Verify the exponential backoff + jitter calculation in _sleep_membership_backoff.

    The function sleeps: min(BASE * 2^(attempt-1), CAP) + uniform(0, 0.5).
    With BASE=0.5 and CAP=2.0:
      attempt 1 → 0.5 + [0, 0.5]  = [0.5, 1.0]
      attempt 2 → 1.0 + [0, 0.5]  = [1.0, 1.5]
      attempt 3 → 2.0 + [0, 0.5]  = [2.0, 2.5]  (hits cap)
      attempt 4 → 2.0 + [0, 0.5]  = [2.0, 2.5]  (still capped)
    """

    @patch("coa_common.metadata_store.smus.time.sleep")
    @patch("coa_common.metadata_store.smus.random.uniform", return_value=0.25)
    def test_attempt_1_exponential(self, _mock_rand, mock_sleep):
        from coa_common.metadata_store.smus import _sleep_membership_backoff

        _sleep_membership_backoff(1)
        mock_sleep.assert_called_once_with(0.75)  # 0.5 * 2^0 + 0.25

    @patch("coa_common.metadata_store.smus.time.sleep")
    @patch("coa_common.metadata_store.smus.random.uniform", return_value=0.25)
    def test_attempt_2_exponential(self, _mock_rand, mock_sleep):
        from coa_common.metadata_store.smus import _sleep_membership_backoff

        _sleep_membership_backoff(2)
        mock_sleep.assert_called_once_with(1.25)  # 0.5 * 2^1 + 0.25

    @patch("coa_common.metadata_store.smus.time.sleep")
    @patch("coa_common.metadata_store.smus.random.uniform", return_value=0.0)
    def test_attempt_3_capped_at_max(self, _mock_rand, mock_sleep):
        from coa_common.metadata_store.smus import _sleep_membership_backoff

        _sleep_membership_backoff(3)
        mock_sleep.assert_called_once_with(2.0)  # min(0.5*4, 2.0) + 0.0

    @patch("coa_common.metadata_store.smus.time.sleep")
    @patch("coa_common.metadata_store.smus.random.uniform", return_value=0.5)
    def test_attempt_3_cap_plus_max_jitter(self, _mock_rand, mock_sleep):
        from coa_common.metadata_store.smus import _sleep_membership_backoff

        _sleep_membership_backoff(3)
        mock_sleep.assert_called_once_with(2.5)  # min(0.5*4, 2.0) + 0.5

    @patch("coa_common.metadata_store.smus.time.sleep")
    @patch("coa_common.metadata_store.smus.random.uniform", return_value=0.5)
    def test_high_attempt_stays_capped(self, _mock_rand, mock_sleep):
        from coa_common.metadata_store.smus import _sleep_membership_backoff

        _sleep_membership_backoff(10)
        mock_sleep.assert_called_once_with(2.5)  # still capped at 2.0 + 0.5

    @patch("coa_common.metadata_store.smus.time.sleep")
    def test_jitter_within_bounds(self, mock_sleep):
        """Run multiple calls and verify jitter stays in [0, 0.5]."""
        from coa_common.metadata_store.smus import _sleep_membership_backoff

        for _ in range(50):
            _sleep_membership_backoff(1)

        for call in mock_sleep.call_args_list:
            delay = call[0][0]
            # attempt 1 base = 0.5; with jitter in [0, 0.5] → delay in [0.5, 1.0]
            assert 0.5 <= delay <= 1.0, f"delay {delay} out of expected range"


# ---------------------------------------------------------------------------
# SMUSClient — create_project_membership
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCreateProjectMembership:
    @patch("coa_common.metadata_store.smus.boto3")
    def test_success(self, mock_boto3):
        mock_dz = MagicMock()
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        client.create_project_membership(project_id="proj_1", user_identifier="arn:aws:iam::123:role/test")

        mock_dz.create_project_membership.assert_called_once()

    @patch("time.sleep")
    @patch("coa_common.metadata_store.smus.boto3")
    def test_persistent_conflict_raises(self, mock_boto3, mock_sleep):
        """A ConflictException that survives retries is raised, never assumed
        to mean 'already a member' — the DataZone API documents no such
        outcome, so we must not silently treat it as success."""
        mock_dz = MagicMock()
        ConflictExc = type("ConflictException", (ClientError,), {})
        mock_dz.exceptions.ConflictException = ConflictExc
        mock_dz.create_project_membership.side_effect = ConflictExc(
            {"Error": {"Code": "ConflictException", "Message": "Member already exists"}}, "CreateProjectMembership"
        )
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="Conflict persisted"):
            client.create_project_membership(project_id="proj_1", user_identifier="arn:aws:iam::123:role/test")

        assert mock_dz.create_project_membership.call_count == 4

    @patch("time.sleep")
    @patch("coa_common.metadata_store.smus.boto3")
    def test_conflict_retries_then_succeeds(self, mock_boto3, mock_sleep):
        mock_dz = MagicMock()
        ConflictExc = type("ConflictException", (ClientError,), {})
        mock_dz.exceptions.ConflictException = ConflictExc
        conflict = ConflictExc(
            {"Error": {"Code": "ConflictException", "Message": "There is a conflict while performing this action"}},
            "CreateProjectMembership",
        )
        # Fail twice with a transient conflict, then succeed.
        mock_dz.create_project_membership.side_effect = [conflict, conflict, None]
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        client.create_project_membership(project_id="proj_1", user_identifier="arn:aws:iam::123:role/test")

        assert mock_dz.create_project_membership.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("time.sleep")
    @patch("coa_common.metadata_store.smus.boto3")
    def test_conflict_exhausts_retries(self, mock_boto3, mock_sleep):
        mock_dz = MagicMock()
        ConflictExc = type("ConflictException", (ClientError,), {})
        mock_dz.exceptions.ConflictException = ConflictExc
        conflict = ConflictExc(
            {"Error": {"Code": "ConflictException", "Message": "There is a conflict while performing this action"}},
            "CreateProjectMembership",
        )
        mock_dz.create_project_membership.side_effect = conflict
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="Conflict persisted"):
            client.create_project_membership(project_id="proj_1", user_identifier="arn:aws:iam::123:role/test")

        assert mock_dz.create_project_membership.call_count == 4

    @patch("coa_common.metadata_store.smus._sleep_membership_backoff")
    @patch("coa_common.metadata_store.smus.boto3")
    def test_throttling_retries_then_succeeds(self, mock_boto3, mock_sleep):
        from botocore.exceptions import ClientError as BotoClientError

        mock_dz = MagicMock()
        # ConflictException must be a real class so the earlier except clause
        # is evaluable; throttling arrives as a plain ClientError.
        mock_dz.exceptions.ConflictException = type("ConflictException", (BotoClientError,), {})
        throttle = BotoClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "CreateProjectMembership",
        )
        mock_dz.create_project_membership.side_effect = [throttle, None]
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        client.create_project_membership(project_id="proj_1", user_identifier="arn:aws:iam::123:role/test")

        assert mock_dz.create_project_membership.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("coa_common.metadata_store.smus._sleep_membership_backoff")
    @patch("coa_common.metadata_store.smus.boto3")
    def test_access_denied_raises_without_retry(self, mock_boto3, mock_sleep):
        from botocore.exceptions import ClientError as BotoClientError

        mock_dz = MagicMock()
        mock_dz.exceptions.ConflictException = type("ConflictException", (BotoClientError,), {})
        denied = BotoClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not permitted"}},
            "CreateProjectMembership",
        )
        mock_dz.create_project_membership.side_effect = denied
        mock_boto3.client.return_value = mock_dz

        client = SMUSClient(domain_id=DOMAIN)
        with pytest.raises(MetadataStoreError, match="Failed to create project membership"):
            client.create_project_membership(project_id="proj_1", user_identifier="arn:aws:iam::123:role/test")

        # AccessDenied is not retryable — single attempt, no backoff.
        assert mock_dz.create_project_membership.call_count == 1
        assert mock_sleep.call_count == 0
