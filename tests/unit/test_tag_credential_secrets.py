# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the credential-secret tagging migration (scripts/tag_credential_secrets.py).

The risk in this script is not crashing — it is **writing the wrong tag value**.
The value is a list of the namespaces entitled to a secret, so a bad write either
revokes a namespace that is working today (by dropping it) or grants one that
should not have it. So these tests pin classification, pin that `--apply`
*preserves* existing entries while appending, and pin the two cases it must never
write: a value it could not parse, and a union over the tag-value cap.
"""

from __future__ import annotations

import json

import pytest

from scripts import tag_credential_secrets as tcs

pytestmark = pytest.mark.unit

ACCT = "111122223333"
NS_A = "aaaaaaaa-0000-4000-8000-000000000001"
NS_B = "bbbbbbbb-0000-4000-8000-000000000002"
NS_C = "cccccccc-0000-4000-8000-000000000003"
TAG_KEY = "coa:namespace"


def _arn(name: str, account: str = ACCT, region: str = "us-east-1") -> str:
    return f"arn:aws:secretsmanager:{region}:{account}:secret:{name}-AbCdEf"


def _ref(arn: str, namespaces: list[str]) -> tcs.SecretRef:
    r = tcs.SecretRef(arn=arn)
    r.namespaces = set(namespaces)
    r.sources = [(ns, f"src-{ns[:4]}") for ns in namespaces]
    return r


def _classify(refs, tags_by_arn, monkeypatch):
    """Classify with DescribeSecret stubbed from ``tags_by_arn`` (absent arn -> raises)."""

    class _SM:
        def __init__(self, m):
            self._m = m

        def describe_secret(self, SecretId):  # noqa: N803 — boto3 kwarg name
            if SecretId not in self._m:
                raise tcs.ClientError(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "DescribeSecret"
                )
            return {"Tags": [{"Key": k, "Value": v} for k, v in self._m[SecretId].items()]}

    monkeypatch.setattr(tcs.boto3, "client", lambda *a, **k: _SM(tags_by_arn))
    tcs._classify(refs, ACCT, "us-east-1", TAG_KEY)


# --- classification ----------------------------------------------------------


def test_untagged_secret_needs_its_namespace_added(monkeypatch):
    a = _arn("solo")
    refs = {a: _ref(a, [NS_A])}
    _classify(refs, {a: {}}, monkeypatch)
    assert refs[a].status == "needs-tag"
    assert refs[a].new_value == NS_A


def test_tag_already_listing_every_namespace_is_ok(monkeypatch):
    a = _arn("bound")
    refs = {a: _ref(a, [NS_A, NS_B])}
    _classify(refs, {a: {TAG_KEY: f"{NS_A} {NS_B}"}}, monkeypatch)
    assert refs[a].status == "ok"


def test_shared_secret_appends_the_missing_namespace_preserving_the_existing_one(monkeypatch):
    """A secret used by two namespaces is supported — append, do not replace.

    Replacing would revoke the namespace already listed, which is the failure mode
    worth guarding: it takes down a source that works today.
    """
    a = _arn("shared")
    refs = {a: _ref(a, [NS_A, NS_B])}
    _classify(refs, {a: {TAG_KEY: NS_A}}, monkeypatch)
    assert refs[a].status == "needs-tag"
    assert refs[a].new_value == f"{NS_A} {NS_B}"


def test_existing_entry_not_used_by_any_source_is_preserved(monkeypatch):
    """An entry the sources table does not explain is still kept.

    The tag is owned by whoever owns the secret; a namespace may be listed ahead of
    registering its source. Dropping it would revoke access this script was never
    asked to touch.
    """
    a = _arn("extra")
    refs = {a: _ref(a, [NS_A])}
    _classify(refs, {a: {TAG_KEY: f"{NS_C} {NS_B}"}}, monkeypatch)
    assert refs[a].status == "needs-tag"
    assert refs[a].new_value == f"{NS_C} {NS_B} {NS_A}"


def test_malformed_existing_tag_is_never_overwritten(monkeypatch):
    a = _arn("bad")
    refs = {a: _ref(a, [NS_A])}
    _classify(refs, {a: {TAG_KEY: f"{NS_A},{NS_B}"}}, monkeypatch)
    assert refs[a].status == "malformed"
    assert refs[a].new_value == ""


def test_union_over_the_tag_value_cap_is_reported_not_truncated(monkeypatch):
    """Truncating would produce a value that does not parse, revoking everyone on it."""
    a = _arn("full")
    # Six valid v4 UUIDs: 6 x 37 - 1 = 221 chars, under the cap. Adding a seventh
    # takes it to 258, over it.
    existing = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(1, 7)]
    refs = {a: _ref(a, [NS_A])}
    _classify(refs, {a: {TAG_KEY: " ".join(existing)}}, monkeypatch)
    assert refs[a].status == "over-capacity"
    assert refs[a].new_value == ""


def test_cross_account_secret_is_out_of_scope(monkeypatch):
    a = _arn("theirs", account="999988887777")
    refs = {a: _ref(a, [NS_A])}
    _classify(refs, {}, monkeypatch)  # DescribeSecret must not be needed
    assert refs[a].status == "cross-account"


def test_unreadable_secret_is_reported_not_skipped(monkeypatch):
    a = _arn("gone")
    refs = {a: _ref(a, [NS_A])}
    _classify(refs, {}, monkeypatch)
    assert refs[a].status == "unreadable"


def test_malformed_arn_is_unreadable(monkeypatch):
    refs = {"not-an-arn": _ref("not-an-arn", [NS_A])}
    _classify(refs, {}, monkeypatch)
    assert refs["not-an-arn"].status == "unreadable"


# --- apply -------------------------------------------------------------------


def test_apply_writes_only_needs_tag(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _SM:
        def tag_resource(self, SecretId, Tags):  # noqa: N803 — boto3 kwarg names
            calls.append((SecretId, Tags[0]["Value"]))

    monkeypatch.setattr(tcs.boto3, "client", lambda *a, **k: _SM())

    needs, bad, full, ok = _arn("n"), _arn("b"), _arn("f"), _arn("o")
    refs = {x: _ref(x, [NS_A]) for x in (needs, bad, full, ok)}
    refs[needs].status, refs[needs].new_value = "needs-tag", f"{NS_B} {NS_A}"
    refs[bad].status = "malformed"
    refs[full].status = "over-capacity"
    refs[ok].status = "ok"

    tagged, failed = tcs._apply(refs, "us-east-1", TAG_KEY)
    assert (tagged, failed) == (1, 0)
    assert calls == [(needs, f"{NS_B} {NS_A}")]
    # The two it must never write keep their status, or a second run would "fix" them.
    assert refs[bad].status == "malformed"
    assert refs[full].status == "over-capacity"


def test_apply_is_idempotent(monkeypatch):
    monkeypatch.setattr(tcs.boto3, "client", lambda *a, **k: pytest.fail("must not call AWS"))
    a = _arn("done")
    refs = {a: _ref(a, [NS_A])}
    refs[a].status = "ok"
    assert tcs._apply(refs, "us-east-1", TAG_KEY) == (0, 0)


def test_apply_records_a_failed_tag_as_unreadable(monkeypatch):
    class _SM:
        def tag_resource(self, SecretId, Tags):  # noqa: N803
            raise tcs.ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "TagResource")

    monkeypatch.setattr(tcs.boto3, "client", lambda *a, **k: _SM())
    a = _arn("denied")
    refs = {a: _ref(a, [NS_A])}
    refs[a].status, refs[a].new_value = "needs-tag", NS_A
    assert tcs._apply(refs, "us-east-1", TAG_KEY) == (0, 1)
    assert refs[a].status == "unreadable"


# --- table scan --------------------------------------------------------------


def _paginator(items):
    class _P:
        def paginate(self, **_):
            return [{"Items": items}]

    class _DDB:
        def get_paginator(self, _):
            return _P()

    return _DDB()


def _ddb_item(sub_type, namespace_id, config, name="s"):
    raw = {
        "sourceSubType": {"S": sub_type},
        "namespaceId": {"S": namespace_id},
        "name": {"S": name},
        "PK": {"S": f"NS#{namespace_id}"},
    }
    if config is not None:
        raw["configuration"] = {"S": config if isinstance(config, str) else json.dumps(config)}
    return raw


def test_scan_collects_only_credential_holding_sources_with_a_secret(monkeypatch):
    a = _arn("keep")
    items = [
        _ddb_item("JDBC_DATABASE", NS_A, {"credentialSecretArn": a}),
        _ddb_item("GLUE_DATABASE", NS_A, {"databaseName": "d"}),  # wrong sub-type
        _ddb_item("CUSTOM_CONNECTOR", NS_A, {"databaseName": "d"}),  # holds no credential
        _ddb_item("JDBC_DATABASE", NS_A, {"host": "h"}),  # no secret
        _ddb_item("JDBC_DATABASE", NS_A, None),  # no configuration
        _ddb_item("JDBC_DATABASE", NS_A, "{not json"),  # unparsable blob
    ]
    monkeypatch.setattr(tcs.boto3, "client", lambda *a_, **k: _paginator(items))
    assert list(tcs._iter_credential_sources("t", "us-east-1")) == [(NS_A, "s", a)]


def test_scan_collects_a_databricks_source_too(monkeypatch):
    """A Databricks source's secret is under the same binding rule as a JDBC one.

    Its secrets are bound at registration, so this is repair rather than migration
    — but a tag edited out of band leaves the source unscannable in exactly the same
    way, and a sub-type filter that skipped it would mean the only tool for that
    could not see the source at all.
    """
    a = _arn("dbx")
    items = [
        _ddb_item(
            "DATABRICKS_SQL_WAREHOUSE",
            NS_A,
            {
                "workspaceHostname": "dbc-a1b2345c-d6e7.cloud.databricks.com",
                "httpPath": "/sql/1.0/warehouses/a1b234c567d8e9fa",
                "credentialSecretArn": a,
            },
        ),
    ]
    monkeypatch.setattr(tcs.boto3, "client", lambda *a_, **k: _paginator(items))
    assert list(tcs._iter_credential_sources("t", "us-east-1")) == [(NS_A, "s", a)]


def test_scan_groups_one_secret_used_by_two_namespaces(monkeypatch):
    a = _arn("shared")
    items = [
        _ddb_item("JDBC_DATABASE", NS_A, {"credentialSecretArn": a}, name="in-a"),
        _ddb_item("JDBC_DATABASE", NS_B, {"credentialSecretArn": a}, name="in-b"),
    ]
    monkeypatch.setattr(tcs.boto3, "client", lambda *a_, **k: _paginator(items))
    refs = tcs._collect("t", "us-east-1")
    assert set(refs) == {a}
    assert refs[a].namespaces == {NS_A, NS_B}


def test_scan_tolerates_a_map_shaped_configuration(monkeypatch):
    """A legacy `configuration` stored as a DynamoDB map, not a JSON string.

    Both pipeline handlers tolerate it, so the migration must too — a one-level
    unwrap leaves the ARN as `{"S": "arn:..."}` and the source is silently skipped,
    which is the worst failure mode for a migration tool: it reports "nothing to do".
    """
    a = _arn("legacy")
    raw = _ddb_item("JDBC_DATABASE", NS_A, {"credentialSecretArn": a})
    raw["configuration"] = {"M": {"credentialSecretArn": {"S": a}}}
    monkeypatch.setattr(tcs.boto3, "client", lambda *a_, **k: _paginator([raw]))
    assert list(tcs._iter_credential_sources("t", "us-east-1")) == [(NS_A, "s", a)]
