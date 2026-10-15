# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Lake Formation self-grant helper (strict-LF onboarding)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from coa_sources.database.connectors import lf_grant

pytestmark = pytest.mark.unit


def _access_denied() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "explicit deny"}},
        "GetDatabase",
    )


class TestIsAccessDenied:
    def test_client_error_access_denied(self):
        assert lf_grant.is_access_denied(_access_denied()) is True

    def test_client_error_other(self):
        err = ClientError({"Error": {"Code": "EntityNotFound"}}, "GetDatabase")
        assert lf_grant.is_access_denied(err) is False

    def test_plain_exception_explicit_deny(self):
        assert lf_grant.is_access_denied(Exception("explicit deny in policy")) is True

    def test_plain_exception_unrelated(self):
        assert lf_grant.is_access_denied(Exception("timeout")) is False


class TestAttemptSelfGrant:
    def test_no_grantor_configured_returns_false(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "")
        assert lf_grant.attempt_self_grant("db", "", "us-east-1", owner_verified=True) is False

    def test_owner_unverified_grants_nothing(self, monkeypatch):
        """F-8: the self-grant must not fire for a database whose owner is unknown.

        It assumes a Lake Formation admin role and grants SELECT to the SHARED
        serve role, so an unverified target here is cross-namespace data access —
        the grantor must never even be assumed.
        """
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "arn:aws:iam::111:role/serve")
        mock_lf = MagicMock()
        with patch.object(lf_grant, "_lf_client_via_grantor", return_value=mock_lf) as via_grantor:
            assert lf_grant.attempt_self_grant("someone_elses_db", "", "us-east-1", owner_verified=False) is False

        via_grantor.assert_not_called()
        mock_lf.grant_permissions.assert_not_called()

    def test_grants_describe_to_connector(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")

        mock_lf = MagicMock()
        with (
            patch.object(lf_grant, "_lf_client_via_grantor", return_value=mock_lf),
            patch.object(
                lf_grant,
                "_caller_role_arn",
                return_value="arn:aws:iam::111:role/connector",
            ),
        ):
            result = lf_grant.attempt_self_grant("hcp360", "", "us-east-1", owner_verified=True)

        assert result is True
        # DB DESCRIBE + table-wildcard DESCRIBE = 2 grant calls
        assert mock_lf.grant_permissions.call_count == 2

    def test_grants_consumer_select_when_configured(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "arn:aws:iam::111:role/serve")
        mock_lf = MagicMock()
        with (
            patch.object(lf_grant, "_lf_client_via_grantor", return_value=mock_lf),
            patch.object(
                lf_grant,
                "_caller_role_arn",
                return_value="arn:aws:iam::111:role/connector",
            ),
        ):
            result = lf_grant.attempt_self_grant("hcp360", "cat1", "us-east-1", owner_verified=True)

        assert result is True
        # connector (2) + serve (2) = 4 grant calls
        assert mock_lf.grant_permissions.call_count == 4

    def test_skips_consumer_grant_when_not_configured(self, monkeypatch):
        """Regression test for #678: serve role grant was silently skipped when env var unset."""
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        mock_lf = MagicMock()
        with (
            patch.object(lf_grant, "_lf_client_via_grantor", return_value=mock_lf),
            patch.object(
                lf_grant,
                "_caller_role_arn",
                return_value="arn:aws:iam::111:role/connector",
            ),
        ):
            result = lf_grant.attempt_self_grant("hcp360", "", "us-east-1", owner_verified=True)

        assert result is True
        # Only connector grants (2), no serve grants when CONSUMER_QUERY_ROLE_ARN is empty
        assert mock_lf.grant_permissions.call_count == 2

    def test_assume_failure_returns_false(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        with patch.object(lf_grant, "_lf_client_via_grantor", return_value=None):
            assert lf_grant.attempt_self_grant("db", "", "us-east-1", owner_verified=True) is False

    def test_grant_clienterror_falls_back(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        mock_lf = MagicMock()
        mock_lf.grant_permissions.side_effect = _access_denied()
        with (
            patch.object(lf_grant, "_lf_client_via_grantor", return_value=mock_lf),
            patch.object(
                lf_grant,
                "_caller_role_arn",
                return_value="arn:aws:iam::111:role/connector",
            ),
        ):
            # First grant raises → caught → returns granted_any (False)
            assert lf_grant.attempt_self_grant("db", "", "us-east-1", owner_verified=True) is False


class TestOnboardingHint:
    def test_includes_database_and_grant_commands(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        with patch.object(lf_grant, "_caller_role_arn", return_value="arn:aws:iam::111:role/connector"):
            hint = lf_grant.lf_onboarding_hint("hcp360", "")
        assert "hcp360" in hint
        assert "lakeformation grant-permissions" in hint
        assert "arn:aws:iam::111:role/connector" in hint


class TestConsumerQueryRoleArn:
    """The serve runtime role ARN is read from SSM at runtime, so SourcesStack
    does not have to wait for ServeStack at deploy time."""

    _PARAM = "/coa/dev/serve/runtime-role-arn"

    def _ssm(self, value: str = "arn:aws:iam::111:role/serve") -> MagicMock:
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": value}}
        return ssm

    def test_env_value_wins_without_ssm_read(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "arn:aws:iam::111:role/from-env")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", self._PARAM)
        with patch.object(lf_grant.boto3, "client") as client:
            assert lf_grant._consumer_query_role_arn() == "arn:aws:iam::111:role/from-env"
        client.assert_not_called()

    def test_no_param_configured_returns_empty(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", "")
        with patch.object(lf_grant.boto3, "client") as client:
            assert lf_grant._consumer_query_role_arn() == ""
        client.assert_not_called()

    def test_reads_ssm_once_and_caches(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", self._PARAM)
        ssm = self._ssm()
        with patch.object(lf_grant.boto3, "client", return_value=ssm):
            assert lf_grant._consumer_query_role_arn() == "arn:aws:iam::111:role/serve"
            assert lf_grant._consumer_query_role_arn() == "arn:aws:iam::111:role/serve"
        ssm.get_parameter.assert_called_once_with(Name=self._PARAM)

    def test_ssm_failure_returns_empty_and_retries_next_call(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", self._PARAM)
        ssm = self._ssm()
        ssm.get_parameter.side_effect = [
            ClientError({"Error": {"Code": "ParameterNotFound", "Message": "x"}}, "GetParameter"),
            {"Parameter": {"Value": "arn:aws:iam::111:role/serve"}},
        ]
        with patch.object(lf_grant.boto3, "client", return_value=ssm):
            assert lf_grant._consumer_query_role_arn() == ""
            assert lf_grant._consumer_query_role_arn() == "arn:aws:iam::111:role/serve"

    def test_self_grant_grants_select_to_ssm_resolved_role(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", self._PARAM)
        mock_lf = MagicMock()
        with (
            patch.object(lf_grant.boto3, "client", return_value=self._ssm()),
            patch.object(lf_grant, "_lf_client_via_grantor", return_value=mock_lf),
            patch.object(lf_grant, "_caller_role_arn", return_value="arn:aws:iam::111:role/connector"),
        ):
            result = lf_grant.attempt_self_grant("hcp360", "cat1", "us-east-1", owner_verified=True)

        assert result is True
        # connector (2) + serve (2) = 4 grant calls
        assert mock_lf.grant_permissions.call_count == 4

    def test_ssm_connection_error_is_handled_like_a_failed_read(self, monkeypatch):
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", self._PARAM)
        ssm = self._ssm()
        ssm.get_parameter.side_effect = EndpointConnectionError(endpoint_url="https://ssm.us-east-1.amazonaws.com")
        with patch.object(lf_grant.boto3, "client", return_value=ssm):
            assert lf_grant._consumer_query_role_arn() == ""

    def test_self_grant_fails_without_grants_when_configured_role_is_unreadable(self, monkeypatch):
        """A configured but unreadable serve role must fail onboarding, not skip its SELECT grant."""
        monkeypatch.setattr(lf_grant, "LF_GRANTOR_ROLE_ARN", "arn:aws:iam::111:role/grantor")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", self._PARAM)
        ssm = self._ssm()
        ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetParameter"
        )
        mock_lf = MagicMock()
        with (
            patch.object(lf_grant.boto3, "client", return_value=ssm),
            patch.object(lf_grant, "_lf_client_via_grantor", return_value=mock_lf),
            patch.object(lf_grant, "_caller_role_arn", return_value="arn:aws:iam::111:role/connector"),
        ):
            result = lf_grant.attempt_self_grant("hcp360", "cat1", "us-east-1", owner_verified=True)

        assert result is False
        mock_lf.grant_permissions.assert_not_called()

    def test_onboarding_hint_names_the_unreadable_parameter(self, monkeypatch):
        """When the serve role can't be resolved, the hint must say so, not suggest a cross-account grant."""
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_ARN", "")
        monkeypatch.setattr(lf_grant, "CONSUMER_QUERY_ROLE_SSM_PARAM", self._PARAM)
        ssm = self._ssm()
        ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetParameter"
        )
        with patch.object(lf_grant.boto3, "client", return_value=ssm):
            hint = lf_grant.lf_onboarding_hint("hcp360", "")
        assert self._PARAM in hint
        assert "cross-account" not in hint
        assert "<coa-serve-runtime-role-arn>" not in hint
