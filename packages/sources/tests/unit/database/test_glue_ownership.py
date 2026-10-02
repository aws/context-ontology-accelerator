# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Glue namespace-ownership check (finding F-8).

The vulnerability these cover: ``manageSource`` authorizes a steward against a
NAMESPACE, while ``glueConfiguration.{catalogId,databaseName}`` name a database.
Nothing bound the two, so a steward with ``manageSource`` in any namespace could
catalog, sample and — in strict-LF accounts — self-grant access to any Glue
database in the platform account.

Every test here carries ``real_glue_ownership`` so the sources conftest's
allow-all stub steps aside and the actual decision runs.
"""

from __future__ import annotations

import os
import re
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from coa_common.constants import namespace_tag_key  # noqa: E402

_TAG_KEY = namespace_tag_key()
from coa_sources.database import glue_ownership as go  # noqa: E402

from tests.unit.conftest import dao_double  # noqa: E402

pytestmark = pytest.mark.real_glue_ownership

# Real UUIDs: the shared `parse_namespace_tag` validates every entry as a namespace
# id, so a readable-but-fake id like "ns-owner" is now correctly rejected.
_NS = "550e8400-e29b-41d4-a716-446655440000"
_OTHER_NS = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
_ACCOUNT = "111122223333"
_FOREIGN_ACCOUNT = "999988887777"
_REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Clear the cached account id and Glue client between tests."""
    go._account_id = None
    go._glue_client = None
    yield
    go._account_id = None
    go._glue_client = None


def _glue_with_tags(tags: dict[str, str]) -> MagicMock:
    glue = MagicMock()
    glue.get_tags.return_value = {"Tags": tags}
    return glue


def _dao_with_claim(namespace_id: str | None) -> MagicMock:
    dao = dao_double()
    dao.get.return_value = {"namespaceId": namespace_id} if namespace_id else None
    return dao


def _glue_missing_database() -> MagicMock:
    glue = MagicMock()
    glue.get_tags.side_effect = ClientError({"Error": {"Code": "EntityNotFoundException"}}, "GetTags")
    return glue


def _check(
    dao, glue, *, namespace_id=_NS, catalog_id=_ACCOUNT, database="sales", role=None, allow_missing_database=False
):
    return go.assert_namespace_may_catalog(
        dao,
        namespace_id=namespace_id,
        catalog_id=catalog_id,
        database_name=database,
        region=_REGION,
        cross_account_role_arn=role,
        allow_missing_database=allow_missing_database,
        glue_client=glue,
    )


# ---------------------------------------------------------------------------
# Native databases — the owner's tag is the authorization
# ---------------------------------------------------------------------------


class TestOwnerTag:
    def test_untagged_database_is_refused(self):
        """The finding itself: an arbitrary database in the platform account.

        Nothing about it says the caller's namespace may read it, so nothing may
        be read from it — the platform does not get to grant itself access to data
        nobody handed it.
        """
        with pytest.raises(go.GlueOwnershipError) as exc:
            _check(MagicMock(), _glue_with_tags({}))
        # The message has to be actionable, or the fix is a support ticket.
        assert "aws glue tag-resource" in str(exc.value)
        assert _TAG_KEY in str(exc.value)

    def test_database_tagged_for_another_namespace_is_refused(self):
        """The cross-namespace read: tagged, but not for the caller."""
        glue = _glue_with_tags({_TAG_KEY: _OTHER_NS})
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), glue, namespace_id=_NS)

    def test_database_tagged_for_the_caller_is_allowed(self):
        glue = _glue_with_tags({_TAG_KEY: _NS})
        _check(MagicMock(), glue)

    @pytest.mark.parametrize(
        "raw",
        [
            # A single space is the canonical form and the only separator Glue accepts
            # (it rejects `,` in a tag value) — matching NAMESPACE_TAG_SEPARATOR.
            f"{_OTHER_NS} {_NS}",
            f"{_NS} {_OTHER_NS}",
        ],
    )
    def test_a_list_of_namespaces_shares_the_database(self, raw):
        """A database may be shared, and the separator must not decide who reads it."""
        _check(MagicMock(), _glue_with_tags({_TAG_KEY: raw}))

    def test_a_comma_separated_value_does_not_grant(self):
        """Glue rejects `,` in a tag value, so a comma form can only arrive from a
        non-Glue writer — and the IAM entry-boundary patterns a shared value needs
        match on space boundaries. Reading it as a list would accept a binding the
        platform layer could not enforce."""
        glue = _glue_with_tags({_TAG_KEY: f"{_OTHER_NS},{_NS}"})
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), glue)

    @pytest.mark.parametrize("raw", ["ALL", "all", "All", " ALL "])
    def test_the_share_all_sentinel_shares_with_every_namespace(self, raw):
        """``ALL``, case-insensitively, as the WHOLE value.

        Not ``*``: Glue rejects it in a tag value (InvalidInputException), so a
        ``*`` sentinel could never be applied to a database in the first place.
        """
        _check(MagicMock(), _glue_with_tags({_TAG_KEY: raw}))

    def test_the_sentinel_is_not_accepted_inside_a_list(self):
        """`ALL` is Glue-only and not a namespace id, so it may not ride along in a
        list — the shared validator rejects non-UUID entries, and "everyone plus these"
        says nothing that "everyone" does not."""
        glue = _glue_with_tags({_TAG_KEY: f"{_NS} ALL"})
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), glue)

    def test_a_non_canonical_value_is_reported_as_a_value_problem(self):
        """Padded or double-spaced values fail the shared canonical-form check. The
        owner must fix the tag they wrote, so the message must not tell them to add
        another one."""
        glue = _glue_with_tags({_TAG_KEY: f"  {_NS}   {_OTHER_NS}  "})
        with pytest.raises(go.GlueOwnershipError) as exc:
            _check(MagicMock(), glue)
        assert "not usable" in str(exc.value)
        assert "aws glue tag-resource" not in str(exc.value)

    def test_the_sentinel_is_a_value_glue_accepts(self):
        """Guards the regression directly: ``*`` was unusable as a tag value."""
        assert go.SHARED_WITH_ALL == "ALL"
        assert re.fullmatch(r"[\w\s.:/=+\-@]*", go.SHARED_WITH_ALL)

    def test_unrelated_tags_do_not_grant(self):
        glue = _glue_with_tags({"Owner": _NS, "team": _NS, "namespace": _NS})
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), glue)

    def test_an_unreadable_tag_says_so_instead_of_blaming_the_tag(self):
        """The message must not send an operator to fix the wrong thing.

        During this fix's own rollout, `GetTags` without `GetDatabase` presented as
        "not registered to namespace" against a database that WAS correctly tagged,
        and cost a deploy cycle to diagnose. Denial is unchanged; the message is
        what tells them where to look.
        """
        glue = MagicMock()
        glue.get_tags.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to perform: glue:GetDatabase"}},
            "GetTags",
        )
        with pytest.raises(go.GlueOwnershipError) as exc:
            _check(MagicMock(), glue)

        msg = str(exc.value)
        assert "Could not verify" in msg
        assert "glue:GetTags and glue:GetDatabase" in msg
        # The underlying AWS error is surfaced, not swallowed into a generic denial.
        assert "glue:GetDatabase" in msg
        # And it must NOT tell them to apply a tag they may already have applied.
        assert "aws glue tag-resource" not in msg

    def test_lake_formation_denial_points_at_the_describe_grant(self):
        glue = MagicMock()
        glue.get_tags.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Insufficient Lake Formation permission(s)"}},
            "GetTags",
        )
        with pytest.raises(go.GlueOwnershipError) as exc:
            _check(MagicMock(), glue)
        assert "lakeformation grant-permissions" in str(exc.value)

    def test_unreadable_tags_fail_closed(self):
        """No `glue:GetTags`, database gone, throttle — all mean "not authorized".

        Failing open here would let an IAM regression silently restore the hole
        this check closes, and the symptom would be invisible.
        """
        glue = MagicMock()
        glue.get_tags.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetTags")
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), glue)

    def test_blank_database_name_is_refused(self):
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), _glue_with_tags({_TAG_KEY: _NS}), database="")


class TestMissingDatabase:
    """A database that does not exist has no owner to have opted in.

    Create has never required the target to exist — it persists the config and the
    async scan reports a bad target — so refusing there would be a 403 telling the
    caller to tag something that isn't there. The pipeline must NOT tolerate it:
    "absent" is one CreateDatabase away from "present and readable".
    """

    def test_create_defers_a_missing_database(self):
        _check(MagicMock(), _glue_missing_database(), allow_missing_database=True)

    def test_the_pipeline_refuses_a_missing_database(self):
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), _glue_missing_database(), allow_missing_database=False)

    def test_missing_is_the_default(self):
        """The strict reading is what a caller gets for free."""
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), _glue_missing_database())

    def test_an_existing_untagged_database_is_refused_even_at_create(self):
        """The distinction is real: absent defers, present-and-untagged refuses.

        Folding the two together is what would turn the create-time tolerance into
        a hole — every unowned database in the account is "present and untagged".
        """
        with pytest.raises(go.GlueOwnershipError):
            _check(MagicMock(), _glue_with_tags({}), allow_missing_database=True)

    def test_an_unreadable_tag_set_is_not_treated_as_missing(self):
        """AccessDenied is an infrastructure failure, not evidence of absence.

        `allow_missing_database` must not extend to it: a tag we cannot read is no
        evidence the database is not there, so create stays refused.
        """
        glue = MagicMock()
        glue.get_tags.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetTags")
        with pytest.raises(go.GlueOwnershipError) as exc:
            _check(MagicMock(), glue, allow_missing_database=True)
        assert "Could not verify" in str(exc.value)


# ---------------------------------------------------------------------------
# Platform-provisioned catalogs — the claim is the authorization
# ---------------------------------------------------------------------------


class TestPlatformCatalogs:
    @pytest.fixture(autouse=True)
    def _prefix(self, monkeypatch):
        monkeypatch.setenv("RESOURCE_PREFIX", "scl-dev-")

    def _catalog_id(self, name: str = "scldevds_deadbeefdeadbeef") -> str:
        return f"{_ACCOUNT}:{name}"

    def test_recognises_its_own_catalog_shape(self):
        assert go.is_platform_provisioned(self._catalog_id()) is True
        assert go.is_platform_provisioned(_ACCOUNT) is False
        assert go.is_platform_provisioned(f"{_ACCOUNT}:customer_own_catalog") is False

    def test_another_namespaces_federated_catalog_is_refused(self):
        """The impact named in F-8: a JDBC source's catalog, read as a Glue source.

        Its name is visible to the owning namespace's viewers via
        ``GetSource.athenaDataCatalogName``, so it is not a secret — the claim is
        what stops it being used.
        """
        glue = _glue_with_tags({_TAG_KEY: _OTHER_NS})
        with pytest.raises(go.GlueOwnershipError) as exc:
            _check(_dao_with_claim(_OTHER_NS), glue, namespace_id=_NS, catalog_id=self._catalog_id())
        assert "does not own" in str(exc.value)

    def test_a_tag_cannot_override_the_claim(self):
        """A tag on one of our own catalogs is a forgeable second opinion.

        Anyone with `glue:TagResource` in the account could otherwise hand
        themselves another namespace's federated catalog, so the claim wins and the
        tag is never consulted.
        """
        glue = _glue_with_tags({_TAG_KEY: _NS})
        with pytest.raises(go.GlueOwnershipError):
            _check(_dao_with_claim(_OTHER_NS), glue, namespace_id=_NS, catalog_id=self._catalog_id())
        glue.get_tags.assert_not_called()

    def test_the_owning_namespace_is_allowed(self):
        glue = MagicMock()
        _check(_dao_with_claim(_NS), glue, namespace_id=_NS, catalog_id=self._catalog_id())
        glue.get_tags.assert_not_called()

    def test_an_unclaimed_catalog_is_refused_for_everyone(self):
        """Including the namespace that would own it. A missing claim is not consent.

        The claim write at source-create is best-effort, so this is the state a
        failed claim leaves behind — refusing it is what makes that failure safe.
        """
        with pytest.raises(go.GlueOwnershipError):
            _check(_dao_with_claim(None), MagicMock(), namespace_id=_NS, catalog_id=self._catalog_id())

    def test_claim_round_trip(self):
        dao = dao_double()
        go.claim_platform_catalog(dao, catalog_name="scldevds_abc", namespace_id=_NS, source_id="src-1")
        item = dao.put.call_args.args[0]
        assert item["PK"] == "GLUECAT#scldevds_abc"
        assert item["SK"] == "CLAIM"
        assert item["namespaceId"] == _NS

        go.release_platform_catalog(dao, catalog_name="scldevds_abc")
        assert dao.delete.call_args.args[0] == {"PK": "GLUECAT#scldevds_abc", "SK": "CLAIM"}

    def test_claims_live_outside_the_namespace_key_space(self):
        """A claim must never come back from a namespace's own source query.

        Two mechanisms, both asserted, because the PK check ALONE passed while
        ``GET /sources`` 500d for every namespace holding a claim (job 10897199):
        the ``ByNamespace`` GSI is keyed on the ATTRIBUTES
        ``(namespaceId, createdAt)``, so staying out of the ``NS#`` PK space does
        nothing to keep a claim out of the index.

        The dao here stamps timestamps exactly as ``DynamoDbDao.put`` does, so this
        asserts the item that would REACH DynamoDB rather than the kwarg used to get
        there — a claim carrying both GSI keys is in the index however that happened.
        """
        written: dict[str, object] = {}

        class _StampingDao:
            """Mirrors DynamoDbDao.put's auto_timestamp contract."""

            def put(self, item, *, condition=None, auto_timestamp=True):  # noqa: ARG002
                if auto_timestamp:
                    item.setdefault("createdAt", "2026-01-01T00:00:00Z")
                    item["updatedAt"] = "2026-01-01T00:00:00Z"
                written.update(item)

        go.claim_platform_catalog(_StampingDao(), catalog_name="scldevds_abc", namespace_id=_NS, source_id="src-1")
        assert not written["PK"].startswith("NS#")
        # Both ByNamespace key attributes present ⇒ the claim joins the index ⇒ 500.
        assert written["namespaceId"] == _NS
        assert "createdAt" not in written


# ---------------------------------------------------------------------------
# Cross-account
# ---------------------------------------------------------------------------


class TestCrossAccount:
    def test_a_foreign_catalog_reached_by_assumed_role_is_exempt(self):
        """The customer's role trust policy is the authorization, and their tags
        are theirs to set — not something this deployment can require."""
        glue = MagicMock()
        with patch.object(go, "_deployment_account", return_value=_ACCOUNT):
            _check(
                MagicMock(),
                glue,
                catalog_id=_FOREIGN_ACCOUNT,
                role=f"arn:aws:iam::{_FOREIGN_ACCOUNT}:role/reader",
            )
        glue.get_tags.assert_not_called()

    def test_a_role_arn_cannot_exempt_a_local_database(self):
        """The bypass the exemption would otherwise be: pass a
        `crossAccountRoleArn` alongside THIS account's `catalogId` and the check
        switches itself off. The exemption turns on the ACCOUNT differing, not on
        the field being present."""
        glue = _glue_with_tags({})
        with (
            patch.object(go, "_deployment_account", return_value=_ACCOUNT),
            pytest.raises(go.GlueOwnershipError),
        ):
            _check(MagicMock(), glue, catalog_id=_ACCOUNT, role=f"arn:aws:iam::{_ACCOUNT}:role/mine")

    def test_an_unresolvable_own_account_denies_the_exemption(self):
        """Not knowing our own account means we cannot establish that a target is
        somebody else's, so the tag check still has to pass."""
        glue = _glue_with_tags({})
        with (
            patch.object(go, "_deployment_account", return_value=None),
            pytest.raises(go.GlueOwnershipError),
        ):
            _check(MagicMock(), glue, catalog_id=_FOREIGN_ACCOUNT, role="arn:aws:iam::999988887777:role/reader")

    def test_caller_identity_failure_is_not_fatal(self):
        go._account_id = None
        with patch.object(go.boto3, "client") as mock_client:
            mock_client.return_value.get_caller_identity.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied"}}, "GetCallerIdentity"
            )
            assert go._deployment_account() is None


# ---------------------------------------------------------------------------
# ARN construction — the tag is read off the resource, so the ARN must be right
# ---------------------------------------------------------------------------


class TestDatabaseArn:
    def test_root_catalog_database(self):
        assert go._database_arn(_ACCOUNT, "sales", _REGION) == f"arn:aws:glue:{_REGION}:{_ACCOUNT}:database/sales"

    def test_nested_catalog_database(self):
        arn = go._database_arn(f"{_ACCOUNT}:mycat", "sales", _REGION)
        assert arn == f"arn:aws:glue:{_REGION}:{_ACCOUNT}:database/mycat/sales"

    @pytest.mark.parametrize(
        ("region", "partition"),
        [("us-east-1", "aws"), ("us-gov-west-1", "aws-us-gov"), ("cn-north-1", "aws-cn")],
    )
    def test_partition_follows_the_region(self, region, partition):
        assert go._database_arn(_ACCOUNT, "sales", region).startswith(f"arn:{partition}:glue:")
