# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared credential-secret → namespace binding rule.

`check_secret_namespace_binding` is the one implementation of the rule, used by
the registration front door and by the scan-pipeline re-check. These tests cover
the verdicts themselves; the API status-code mapping is covered in
tests/unit/api/test_credential_secret_binding.py.
"""

from __future__ import annotations

import os

import pytest
from botocore.exceptions import BotoCoreError, ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.database.secret_binding as _sb

_ACCOUNT = "111122223333"
_NS = "550e8400-e29b-41d4-a716-446655440000"
_OTHER_NS = "660e8400-e29b-41d4-a716-446655440001"
_THIRD_NS = "770e8400-e29b-41d4-a716-446655440002"
_IN_ACCT_ARN = f"arn:aws:secretsmanager:us-east-1:{_ACCOUNT}:secret:ns-a/db-AbCdEf"
_OTHER_REGION_ARN = f"arn:aws:secretsmanager:eu-west-1:{_ACCOUNT}:secret:ns-a/db-AbCdEf"
_CROSS_ACCT_ARN = "arn:aws:secretsmanager:us-east-1:999988887777:secret:ns-b/db-XyZ123"


@pytest.fixture(autouse=True)
def _pin_account(monkeypatch):
    monkeypatch.setattr(_sb, "deployment_account_id", lambda: _ACCOUNT)


def _tags(monkeypatch, tags: dict[str, str]) -> list[tuple[str, str]]:
    """Patch the tag lookup, returning a list the caller can assert calls against."""
    calls: list[tuple[str, str]] = []

    def _describe(arn: str, region: str) -> dict[str, str]:
        calls.append((arn, region))
        return tags

    monkeypatch.setattr(_sb, "describe_secret_tags", _describe)
    return calls


def test_tag_listing_only_this_namespace_is_bound(monkeypatch):
    _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: _NS})
    verdict = _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS)
    assert verdict.ok and verdict.reason == "bound"


@pytest.mark.parametrize(
    "value",
    [
        f"{_NS} {_OTHER_NS}",  # first
        f"{_OTHER_NS} {_NS}",  # last
        f"{_OTHER_NS} {_NS} {_THIRD_NS}",  # middle
    ],
    ids=["first", "last", "middle"],
)
def test_tag_listing_several_namespaces_binds_each_entry(monkeypatch, value):
    """A shared credential is a supported configuration, not a conflict.

    Position must not matter — the entry is what binds, and the IAM StringLike
    patterns enumerate exactly these four positions for the same reason.
    """
    _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: value})
    assert _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS).ok


def test_substring_of_another_entry_does_not_bind(monkeypatch):
    """`<other><id>` with no space must NOT match — entry, not substring.

    This is the property the IAM patterns are anchored on; the code check has to
    agree or a value one layer accepts is refused by the other.
    """
    _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: f"{_OTHER_NS}{_NS}"})
    verdict = _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS)
    assert not verdict.ok
    # Concatenated UUIDs are not a parsable entry at all.
    assert verdict.reason == "malformed-tag"


def test_tag_listing_only_another_namespace_is_refused(monkeypatch):
    _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: _OTHER_NS})
    verdict = _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS)
    assert not verdict.ok
    assert verdict.reason == "not-listed"
    assert not verdict.retryable


def test_untagged_secret_is_refused(monkeypatch):
    _tags(monkeypatch, {"env": "prod"})
    verdict = _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS)
    assert not verdict.ok and verdict.reason == "tag-absent"


@pytest.mark.parametrize(
    "value",
    [f" {_NS}", f"{_NS} ", f"{_NS}  {_OTHER_NS}", f"{_NS},{_OTHER_NS}", "not-a-uuid", ""],
    ids=["leading-space", "trailing-space", "double-space", "comma", "not-uuid", "empty"],
)
def test_non_canonical_or_unparsable_tag_is_refused(monkeypatch, value):
    """Canonical form is required, not preferred.

    The IAM conditions match entries on literal space boundaries, so a value
    accepted here but unmatchable by IAM would pass this check and then fail every
    read — a source that registers and then cannot be scanned.
    """
    _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: value})
    verdict = _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS)
    assert not verdict.ok and verdict.reason == "malformed-tag"


def test_tag_is_queried_in_the_secrets_own_region(monkeypatch):
    # A same-account secret may live in another region; DescribeSecret must be
    # issued there or it 404s and the check fails closed for the wrong reason.
    calls = _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: _NS})
    assert _sb.check_secret_namespace_binding(_OTHER_REGION_ARN, _NS).ok
    assert calls == [(_OTHER_REGION_ARN, "eu-west-1")]


def test_unreadable_secret_fails_closed(monkeypatch):
    def _raise(arn: str, region: str) -> dict[str, str]:
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DescribeSecret")

    monkeypatch.setattr(_sb, "describe_secret_tags", _raise)
    verdict = _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS)
    assert not verdict.ok and verdict.reason == "unverifiable"


def test_botocore_error_fails_closed(monkeypatch):
    def _raise(arn: str, region: str) -> dict[str, str]:
        raise BotoCoreError()

    monkeypatch.setattr(_sb, "describe_secret_tags", _raise)
    assert not _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS).ok


def test_cross_account_skips_the_tag_lookup(monkeypatch):
    calls = _tags(monkeypatch, {})
    verdict = _sb.check_secret_namespace_binding(_CROSS_ACCT_ARN, _NS)
    assert verdict.ok and verdict.reason == "cross-account"
    assert calls == []


def test_malformed_arn_is_refused_not_treated_as_cross_account(monkeypatch):
    calls = _tags(monkeypatch, {})
    verdict = _sb.check_secret_namespace_binding("not-an-arn", _NS)
    assert not verdict.ok and verdict.reason == "malformed-arn"
    assert calls == []


def test_account_resolution_failure_is_retryable(monkeypatch):
    def _boom() -> str:
        raise ClientError({"Error": {"Code": "Throttling", "Message": "slow"}}, "GetCallerIdentity")

    monkeypatch.setattr(_sb, "deployment_account_id", _boom)
    verdict = _sb.check_secret_namespace_binding(_IN_ACCT_ARN, _NS)
    assert not verdict.ok
    assert verdict.reason == "account-unresolved"
    assert verdict.retryable


def test_absent_secret_is_a_noop(monkeypatch):
    calls = _tags(monkeypatch, {})
    assert _sb.check_secret_namespace_binding(None, _NS).reason == "no-secret"
    assert _sb.check_secret_namespace_binding("", _NS).ok
    assert calls == []


def test_require_raises_on_refusal(monkeypatch):
    _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: _OTHER_NS})
    with pytest.raises(RuntimeError, match="not bound to namespace"):
        _sb.require_secret_namespace_binding(_IN_ACCT_ARN, _NS, "DS#abc")


def test_require_is_silent_when_bound(monkeypatch):
    _tags(monkeypatch, {_sb.NAMESPACE_TAG_KEY: _NS})
    assert _sb.require_secret_namespace_binding(_IN_ACCT_ARN, _NS, "DS#abc") is None
