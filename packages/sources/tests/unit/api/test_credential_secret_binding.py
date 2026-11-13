# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the credential-secret → namespace binding.

`_validate_credential_secret_binding` is the front-door check that a JDBC
source's `credentialSecretArn` is bound to the registering namespace: an
in-account secret must carry a ``<prefix>:namespace`` tag whose value LISTS the
registering namespace (one or more namespace UUIDs separated by a single space),
and anything unverifiable or unparseable fails closed with a 400.

Imports go through sources_handler first to resolve the documented circular
import with database_routes (see test_database_routes.py).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.api.sources_handler  # noqa: F401, I001
import coa_sources.api.database_routes as _dr  # noqa: E402, I001
import coa_sources.database.secret_binding as _sb  # noqa: E402, I001

_ACCOUNT = "111122223333"
_NS = "550e8400-e29b-41d4-a716-446655440000"
_OTHER_NS = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
_THIRD_NS = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"
# Under test the deployment prefix is unset, so the key falls back to the brand.
_TAG = _sb.NAMESPACE_TAG_KEY
_IN_ACCT_ARN = f"arn:aws:secretsmanager:us-east-1:{_ACCOUNT}:secret:ns-a/db-AbCdEf"
_CROSS_ACCT_ARN = "arn:aws:secretsmanager:us-east-1:999988887777:secret:ns-b/db-XyZ123"


@pytest.fixture(autouse=True)
def _pin_account(monkeypatch):
    """Pin the deployment account so no real STS call is made."""
    monkeypatch.setattr(_sb, "deployment_account_id", lambda: _ACCOUNT)


def _jdbc(secret_arn: str | None) -> SimpleNamespace:
    return SimpleNamespace(credential_secret_arn=secret_arn)


def test_in_account_matching_tag_is_allowed(monkeypatch):
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {_TAG: _NS})
    assert _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS) is None


def test_in_account_other_namespace_only_is_rejected(monkeypatch):
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {_TAG: _OTHER_NS})
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 400


def test_in_account_missing_tag_is_rejected(monkeypatch):
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {"env": "prod"})
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 400


def test_describe_secret_denied_fails_closed(monkeypatch):
    def _raise(arn, region):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DescribeSecret")

    monkeypatch.setattr(_sb, "describe_secret_tags", _raise)
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 400


def test_cross_account_arn_skips_tag_check(monkeypatch):
    # Cross-account secrets are gated by the customer's resource policy, not tags.
    # DescribeSecret must NOT be attempted (it would fail against another account).
    spy = MagicMock(side_effect=AssertionError("describe_secret must not be called cross-account"))
    monkeypatch.setattr(_sb, "describe_secret_tags", spy)
    assert _dr._validate_credential_secret_binding(_jdbc(_CROSS_ACCT_ARN), _NS) is None
    spy.assert_not_called()


def test_malformed_arn_fails_closed(monkeypatch):
    # An ARN with no parsable account must be rejected, never treated as
    # cross-account and skipped.
    spy = MagicMock(side_effect=AssertionError("describe_secret must not be called for a malformed ARN"))
    monkeypatch.setattr(_sb, "describe_secret_tags", spy)
    resp = _dr._validate_credential_secret_binding(_jdbc("not-an-arn"), _NS)
    assert resp is not None and resp["statusCode"] == 400
    spy.assert_not_called()


def test_account_resolution_failure_fails_closed(monkeypatch):
    # If STS can't resolve the deployment account we cannot tell in- from
    # cross-account, so we must fail closed (retryable), not skip the check.

    def _boom():
        raise ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "GetCallerIdentity")

    monkeypatch.setattr(_sb, "deployment_account_id", _boom)
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 503


def test_no_jdbc_config_is_noop():
    assert _dr._validate_credential_secret_binding(None, _NS) is None


def test_jdbc_without_secret_is_noop(monkeypatch):
    spy = MagicMock(side_effect=AssertionError("describe_secret must not be called without a secret ARN"))
    monkeypatch.setattr(_sb, "describe_secret_tags", spy)
    assert _dr._validate_credential_secret_binding(_jdbc(None), _NS) is None
    spy.assert_not_called()


#
# One credential can legitimately serve several namespaces (a shared read-only
# reporting login), so the tag value is a space-separated list. Registration must
# accept the request when this namespace is any entry, and reject it when the
# value is absent, unparseable, or simply does not list this namespace.


@pytest.mark.parametrize(
    "tag_value",
    [
        _NS,
        f"{_NS} {_OTHER_NS}",
        f"{_OTHER_NS} {_NS}",
        f"{_OTHER_NS} {_NS} {_THIRD_NS}",
    ],
)
def test_namespace_listed_anywhere_in_the_tag_is_allowed(monkeypatch, tag_value):
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {_TAG: tag_value})
    assert _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS) is None


def test_multi_namespace_tag_without_this_namespace_is_rejected(monkeypatch):
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {_TAG: f"{_OTHER_NS} {_THIRD_NS}"})
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 400


@pytest.mark.parametrize(
    "tag_value",
    [
        "",
        "   ",
        "not-a-uuid",
        f"{_NS} not-a-uuid",  # a valid entry does not excuse an invalid one
        f" {_NS}",  # leading whitespace IAM's space-anchored patterns can't match
        f"{_NS}  {_OTHER_NS}",  # doubled separator
        f"{_NS}\t{_OTHER_NS}",  # tab separator
    ],
)
def test_malformed_tag_value_is_rejected(monkeypatch, tag_value):
    """A tag we cannot parse is not a binding — reject rather than guess.

    Each of these would otherwise be accepted here and then fail at IAM, where
    the same binding is matched on literal single-space entry boundaries.
    """
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {_TAG: tag_value})
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 400


def test_substring_occurrence_does_not_bind(monkeypatch):
    """The namespace embedded in a longer token is not an entry (and not a UUID)."""
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {_TAG: f"prefixed{_NS}"})
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 400


def test_error_message_names_the_derived_tag_key(monkeypatch):
    """The 400 must tell the operator the exact key to tag, prefix included."""
    monkeypatch.setattr(_sb, "describe_secret_tags", lambda arn, region: {})
    resp = _dr._validate_credential_secret_binding(_jdbc(_IN_ACCT_ARN), _NS)
    assert resp is not None and resp["statusCode"] == 400
    assert _TAG in resp["body"] and _NS in resp["body"]


def test_tag_key_is_derived_from_the_deployment_prefix():
    """The key tracks the deployment prefix so co-located deployments don't share."""
    from coa_common.constants import namespace_tag_key

    assert namespace_tag_key() == _TAG
    assert namespace_tag_key("scl") == "scl:namespace"
    assert namespace_tag_key("scl") != namespace_tag_key("coa")
