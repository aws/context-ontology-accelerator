# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A cross-account Glue source must not produce a Lake Formation grant here.

Two decisions used to be taken independently of each other:

* ``assert_namespace_may_catalog`` EXEMPTS a source whose ``catalogId`` names
  another account. Correctly so — that catalog is not ours to govern, and
  reaching it needs the customer's own ``crossAccountRoleArn``.
* ``grant_consumer_select_native`` grants in THIS account. The catalog is not a
  parameter of that call: Lake Formation permissions come from a grantor in the
  account that owns the resource, and the only grantor here is ours.

So a GLUE_DATABASE source naming a foreign ``catalogId`` and a ``databaseName``
that also exists locally skipped the ownership check and then had the federation
provisioner — a Lake Formation admin — grant the SHARED serve runtime role
DESCRIBE/SELECT on the LOCAL database of that name, and persist
``queryable: true``. One caller-supplied field, pointing somewhere else, bought a
read of local data nobody authorized. CWE-863 / CWE-345.

The gate is ``native_grant_target_error``: the declared catalog must be the one
the grant is made in. Refusing is also the truthful answer rather than a lost
feature — the local grant never made the FOREIGN database readable, so a
cross-account source it marked queryable was answering from a same-named local
database or not answering at all.
"""

from __future__ import annotations

import json
from unittest.mock import ANY, MagicMock, patch

import coa_sources.api.sources_handler  # noqa: F401

# Imported for its side effect: mock.patch() resolves dotted targets by
# attribute traversal, so these submodules must be bound on their parent
# packages before any patch(f"{MODULE}...") is evaluated.
import coa_sources.database.connectors.glue_connection_provisioner  # noqa: F401
import coa_sources.database.pipeline.federation_handler  # noqa: F401
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.constants import namespace_tag_key
from coa_common.dao.base import PaginatedResult

from tests.unit.conftest import dao_double

pytestmark = pytest.mark.unit

HANDLER = "coa_sources.database.pipeline.federation_handler"
PROVISIONER = "coa_sources.database.connectors.glue_connection_provisioner"
SOURCES = "coa_sources.api.sources_handler"

# The account this deployment runs in — and therefore the only account its Lake
# Formation grants can land in.
_LOCAL = "111111111111"
# The account a caller points at to exempt itself from the ownership check.
_FOREIGN = "999999999999"

_DATABASE = "retail_demo"
_CONSUMER = "arn:aws:iam::111111111111:role/serve-runtime"
# The owner tag parser accepts namespace ids only as UUIDs, so the cases that run
# the real ownership check need a well-formed one.
_NAMESPACE = "3f2b6a1c-7d4e-4a90-9b21-8c5e0f1a2d34"
_EVENT = {"datasourceId": "DS#abc", "sourceId": "abc", "namespaceId": _NAMESPACE}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SOURCES_TABLE", "sources")
    monkeypatch.setenv("NAMESPACES_TABLE", "namespaces")
    monkeypatch.setenv("SOURCE_SCAN_JOBS_TABLE", "scan-jobs")
    monkeypatch.setenv("SMUS_DOMAIN_ID", "dz-test")


@pytest.fixture(autouse=True)
def _local_account():
    """Pin the account the grant would be made in, in both modules that read it.

    ``glue_ownership`` resolves it to decide whether a target is somebody else's;
    the provisioner resolves it to decide where a grant can land. The real
    resolvers call STS, and the finding is precisely that these two readings were
    not tied together — so the tests pin them to ONE value and vary only the
    source's declared ``catalogId``.
    """
    import coa_sources.database.connectors.glue_connection_provisioner as prov
    import coa_sources.database.glue_ownership as ownership

    previous = prov._account_id
    prov._account_id = _LOCAL
    with patch.object(ownership, "_deployment_account", return_value=_LOCAL):
        yield
    prov._account_id = previous


@pytest.fixture
def _lf():
    """A Lake Formation client that would ACCEPT every grant.

    The local database of the attacker-chosen name existing is what makes the
    finding exploitable, so the mock must not be the reason nothing is granted.
    """
    lf = MagicMock()
    with patch(f"{PROVISIONER}._get_lakeformation", return_value=lf):
        yield lf


def _glue_item(*, catalog_id: str, cross_account_role_arn: str | None = None) -> dict:
    configuration = {"catalogId": catalog_id, "databaseName": _DATABASE, "region": "us-east-1"}
    if cross_account_role_arn:
        configuration["crossAccountRoleArn"] = cross_account_role_arn
    return {
        "sourceSubType": "GLUE_DATABASE",
        "athenaDatabase": _DATABASE,
        "configuration": json.dumps(configuration),
    }


class TestNativeGrantTargetError:
    """The predicate: may a native LF grant be made for this declared catalog?"""

    def test_a_catalog_in_this_account_may_be_granted(self):
        from coa_sources.database.connectors.glue_connection_provisioner import native_grant_target_error

        assert native_grant_target_error(_LOCAL) is None

    def test_an_unset_catalog_id_is_this_account(self):
        """Glue reads an absent catalog id as the caller's own Data Catalog."""
        from coa_sources.database.connectors.glue_connection_provisioner import native_grant_target_error

        assert native_grant_target_error("") is None

    def test_a_catalog_in_another_account_is_refused_naming_both(self):
        from coa_sources.database.connectors.glue_connection_provisioner import native_grant_target_error

        reason = native_grant_target_error(_FOREIGN)

        assert reason is not None
        # An operator reading the step output has to see which account was asked
        # for and which one we can actually grant in.
        assert _FOREIGN in reason
        assert _LOCAL in reason

    @pytest.mark.parametrize(
        "catalog_id",
        [
            f"{_FOREIGN}:coadevds_abc",  # a nested catalog in another account
            f" {_FOREIGN} ",  # padding does not change the account
            f"{_FOREIGN}:",
            f"arn:aws:glue:us-east-1:{_LOCAL}:catalog",  # an ARN is not a catalog id
            ":coadevds_abc",  # no account, but still not the root catalog
        ],
    )
    def test_the_account_is_read_from_every_catalog_id_shape(self, catalog_id: str):
        from coa_sources.database.connectors.glue_connection_provisioner import native_grant_target_error

        assert native_grant_target_error(catalog_id) is not None

    def test_a_nested_catalog_in_this_account_is_refused(self):
        """The other half of the mismatch, reached through the catalog name.

        ``account:catalogName`` addresses a NESTED catalog, whose databases are a
        separate namespace from the root catalog's — but the grant can only be
        made on the root. So the ownership check clears one resource and the
        grant lands on another, which is the same defect as the cross-account
        arm: ``assert_namespace_may_catalog`` passes a platform-provisioned
        nested catalog on the claim alone, never looking at ``database_name``, so
        a namespace owning its own federated catalog could name any schema in it
        and collect a grant on the root-catalog database of that name.
        """
        from coa_sources.database.connectors.glue_connection_provisioner import native_grant_target_error

        reason = native_grant_target_error(f"{_LOCAL}:coadevds_abc")

        assert reason is not None
        assert "coadevds_abc" in reason

    def test_an_unresolvable_account_refuses(self):
        """Not knowing our account is not evidence it is the one we were given."""
        import coa_sources.database.connectors.glue_connection_provisioner as prov
        from coa_sources.database.connectors.glue_connection_provisioner import native_grant_target_error

        prov._account_id = None
        with patch.object(prov, "_get_account_id", side_effect=BotoCoreError()):
            assert native_grant_target_error(_FOREIGN) is not None


class TestGrantRefusesAForeignCatalog:
    """The sink refuses on its own, not only because its caller checked first."""

    def test_no_lake_formation_call_is_made_for_a_foreign_catalog(self, _lf):
        from coa_sources.database.connectors.glue_connection_provisioner import grant_consumer_select_native

        granted = grant_consumer_select_native(
            database_name=_DATABASE,
            principal_arn=_CONSUMER,
            catalog_id=_FOREIGN,
        )

        assert granted is False
        # Not "grant and hope LF denies it": nothing is attempted, because the
        # call that would be attempted is the one that lands on local data.
        _lf.grant_permissions.assert_not_called()

    def test_a_local_catalog_is_still_granted(self, _lf):
        """The other half of the contract: same-account sources are unaffected."""
        from coa_sources.database.connectors.glue_connection_provisioner import grant_consumer_select_native

        granted = grant_consumer_select_native(
            database_name=_DATABASE,
            principal_arn=_CONSUMER,
            catalog_id=_LOCAL,
        )

        assert granted is True
        # Database DESCRIBE + Table SELECT/DESCRIBE, both in the local catalog.
        assert _lf.grant_permissions.call_count == 2
        for call in _lf.grant_permissions.call_args_list:
            assert next(iter(call.kwargs["Resource"].values()))["CatalogId"] == _LOCAL

    def test_an_undeclared_catalog_is_still_granted(self, _lf):
        """Callers with no catalog id to declare target the local catalog by definition."""
        from coa_sources.database.connectors.glue_connection_provisioner import grant_consumer_select_native

        assert grant_consumer_select_native(database_name=_DATABASE, principal_arn=_CONSUMER) is True
        assert _lf.grant_permissions.call_count == 2

    def test_no_lake_formation_call_is_made_for_a_local_nested_catalog(self, _lf):
        """A nested catalog in this account is refused at the sink too.

        The grant can only be made on the root catalog, so a declared nested
        catalog is the same declared-vs-granted mismatch as a foreign account.
        """
        from coa_sources.database.connectors.glue_connection_provisioner import grant_consumer_select_native

        granted = grant_consumer_select_native(
            database_name=_DATABASE,
            principal_arn=_CONSUMER,
            catalog_id=f"{_LOCAL}:coadevds_abc",
        )

        assert granted is False
        _lf.grant_permissions.assert_not_called()


class TestFederationHandlerCrossAccountSource:
    """End to end through the step that holds Lake Formation admin."""

    @staticmethod
    def _run(item: dict, lf: MagicMock, claim: dict | None = None):
        from coa_sources.database.pipeline.federation_handler import handler

        dao = dao_double()
        if claim is None:
            dao.get.return_value = item
        else:
            # Two different rows come back through one dao: the source row, and
            # the platform-catalog claim `assert_namespace_may_catalog` reads to
            # decide whether this namespace owns a nested catalog.
            dao.get.side_effect = lambda key, **_: claim if str(key.get("PK", "")).startswith("GLUECAT#") else item
        with (
            patch(f"{HANDLER}._get_dao", return_value=dao),
            patch(f"{HANDLER}._consumer_role_arn", return_value=_CONSUMER),
            patch(f"{HANDLER}.provision_federated_catalog") as provision,
        ):
            out = handler(_EVENT)
        provision.assert_not_called()
        return out, dao, lf

    @pytest.mark.real_glue_ownership
    def test_a_foreign_catalog_with_a_same_named_local_database_grants_nothing(self, _lf):
        """The finding, end to end: exempt from the check, and now also not granted.

        The real ownership check runs here, not a stub — the exemption firing is
        half of what made this reachable, so a stub would test the fix against a
        precondition the fix exists to survive.
        """
        item = _glue_item(catalog_id=_FOREIGN, cross_account_role_arn=f"arn:aws:iam::{_FOREIGN}:role/coa-reader")

        out, dao, lf = self._run(item, _lf)

        lf.grant_permissions.assert_not_called()
        assert out["queryable"] is False
        assert out["reason"].startswith("declared-catalog-not-grantable: ")
        # Persisted, not just returned: serve gates its SQL scope on this flag
        # (sources_registry.sql_namespace_scope skips `queryable is False`), so a
        # row left True from an earlier scan would keep the database in scope.
        dao.update.assert_called_once()
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": False}

    @pytest.mark.real_glue_ownership
    def test_an_owner_tagged_foreign_database_still_grants_nothing(self, _lf):
        """Even a foreign database whose owner DID tag us in gets no local grant.

        Without ``crossAccountRoleArn`` the exemption does not fire and the owner
        tag is honoured, so the ownership check passes. It passed on the FOREIGN
        database; the grant would still have landed on the local one.
        """
        import coa_sources.database.glue_ownership as ownership

        glue = MagicMock()
        glue.get_tags.return_value = {"Tags": {namespace_tag_key(): _EVENT["namespaceId"]}}
        with patch.object(ownership, "_get_glue", return_value=glue):
            out, dao, lf = self._run(_glue_item(catalog_id=_FOREIGN), _lf)

        lf.grant_permissions.assert_not_called()
        assert out["queryable"] is False
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": False}

    @pytest.mark.real_glue_ownership
    def test_a_local_catalog_is_granted_and_marked_queryable(self, _lf):
        """The regression guard: same-account Glue sources keep working.

        ``stub_glue_ownership`` is opted out of here too, so the ownership check
        is the real one — the local database is shared with this namespace by the
        tag, which is how a native Glue source is onboarded.
        """
        import coa_sources.database.glue_ownership as ownership

        glue = MagicMock()
        glue.get_tags.return_value = {"Tags": {namespace_tag_key(): _EVENT["namespaceId"]}}
        with patch.object(ownership, "_get_glue", return_value=glue):
            out, dao, lf = self._run(_glue_item(catalog_id=_LOCAL), _lf)

        assert lf.grant_permissions.call_count == 2
        assert out == {"provisioned": False, "reason": "glue-native", "queryable": True}
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": True}

    @pytest.mark.real_glue_ownership
    def test_a_claimed_nested_catalog_in_this_account_grants_nothing(self, _lf):
        """The second route to the same mismatch, end to end.

        The namespace genuinely owns the nested catalog ``coadevds_abc`` — the
        platform claim says so, and ``assert_namespace_may_catalog`` passes on
        that claim alone, never looking at ``database_name``. It therefore
        authorizes nothing about the ROOT-catalog database of that name, which is
        the only thing the grant can land on. So the ownership check passes and
        the grant must still be refused.
        """
        claim = {"PK": "GLUECAT#coadevds_abc", "SK": "CLAIM", "namespaceId": _NAMESPACE}

        out, dao, lf = self._run(_glue_item(catalog_id=f"{_LOCAL}:coadevds_abc"), _lf, claim=claim)

        lf.grant_permissions.assert_not_called()
        assert out["queryable"] is False
        assert out["reason"].startswith("declared-catalog-not-grantable: ")
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": False}

    @pytest.mark.real_glue_ownership
    def test_the_declared_catalog_reaches_the_grant(self, _lf):
        """The gate is not the only thing carrying the catalog id to the sink.

        The sink refuses a foreign catalog itself, which it can only do if the
        handler tells it which catalog the source declared.
        """
        import coa_sources.database.glue_ownership as ownership

        glue = MagicMock()
        glue.get_tags.return_value = {"Tags": {namespace_tag_key(): _EVENT["namespaceId"]}}
        with (
            patch.object(ownership, "_get_glue", return_value=glue),
            patch(f"{HANDLER}.grant_consumer_select_native", return_value=True) as grant,
        ):
            self._run(_glue_item(catalog_id=_LOCAL), _lf)

        assert grant.call_args.kwargs["catalog_id"] == _LOCAL


class TestRevokeConsumerSelectNative:
    """The grant's mirror image, used by the delete path."""

    def test_both_resources_are_revoked_in_the_local_catalog(self):
        import coa_sources.database.connectors.glue_connection_provisioner as prov
        from coa_sources.database.connectors.glue_connection_provisioner import revoke_consumer_select_native

        lf = MagicMock()
        session = MagicMock()
        session.client.return_value = lf

        assert (
            revoke_consumer_select_native(
                database_name=_DATABASE,
                principal_arn=_CONSUMER,
                session=session,
            )
            is True
        )

        # The session's client, not the module's: permissions are revoked by the
        # grantor, and the caller is not it.
        session.client.assert_called_once_with("lakeformation", region_name=prov.AWS_REGION)
        resources = [c.kwargs["Resource"] for c in lf.revoke_permissions.call_args_list]
        assert resources == [
            {"Table": {"CatalogId": _LOCAL, "DatabaseName": _DATABASE, "TableWildcard": {}}},
            {"Database": {"CatalogId": _LOCAL, "Name": _DATABASE}},
        ]

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            ("EntityNotFoundException", "no such grant"),
            # What Lake Formation actually answers for a permission that was never
            # held — the common case on a re-delete, and the reason the code alone
            # was not enough to go on.
            ("InvalidInputException", "No permissions revoked. Grantee has no permissions on resource"),
        ],
    )
    def test_a_permission_that_is_not_held_counts_as_revoked(self, code: str, message: str):
        """Otherwise a delete retries forever over a grant that is not there."""
        from coa_sources.database.connectors.glue_connection_provisioner import revoke_consumer_select_native

        lf = MagicMock()
        lf.revoke_permissions.side_effect = ClientError(
            {"Error": {"Code": code, "Message": message}}, "RevokePermissions"
        )
        session = MagicMock()
        session.client.return_value = lf

        assert revoke_consumer_select_native(database_name=_DATABASE, principal_arn=_CONSUMER, session=session) is True

    def test_a_malformed_request_is_not_mistaken_for_an_absent_grant(self):
        """``InvalidInputException`` is also how LF rejects a bad request.

        Treating the code alone as success would report a revoke that never
        happened and leave the permission in place with nothing in the logs. The
        message is what separates "there was nothing to revoke" from "the call
        was wrong", and an unrecognised message has to fail.
        """
        from coa_sources.database.connectors.glue_connection_provisioner import revoke_consumer_select_native

        lf = MagicMock()
        lf.revoke_permissions.side_effect = ClientError(
            {"Error": {"Code": "InvalidInputException", "Message": "Invalid principal identifier"}},
            "RevokePermissions",
        )
        session = MagicMock()
        session.client.return_value = lf

        assert revoke_consumer_select_native(database_name=_DATABASE, principal_arn=_CONSUMER, session=session) is False

    def test_a_real_failure_is_reported(self):
        from coa_sources.database.connectors.glue_connection_provisioner import revoke_consumer_select_native

        lf = MagicMock()
        lf.revoke_permissions.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not an admin"}}, "RevokePermissions"
        )
        session = MagicMock()
        session.client.return_value = lf

        assert revoke_consumer_select_native(database_name=_DATABASE, principal_arn=_CONSUMER, session=session) is False

    @pytest.mark.parametrize(
        ("database_name", "principal_arn"),
        [
            ("", _CONSUMER),
            ("bad name; drop", _CONSUMER),
            (_DATABASE, ""),
            (_DATABASE, "not-an-arn"),
        ],
    )
    def test_malformed_inputs_revoke_nothing(self, database_name: str, principal_arn: str):
        """A wildcard revoke is as dangerous as a wildcard grant — same validation."""
        from coa_sources.database.connectors.glue_connection_provisioner import revoke_consumer_select_native

        lf = MagicMock()
        session = MagicMock()
        session.client.return_value = lf

        assert (
            revoke_consumer_select_native(
                database_name=database_name,
                principal_arn=principal_arn,
                session=session,
            )
            is False
        )
        lf.revoke_permissions.assert_not_called()


class TestRevokeOnDelete:
    """The grant does not outlive the last source that justified it.

    The permission is keyed on (consumer principal, database), not on a source,
    so the delete has to establish that nobody else still catalogs the database
    before it takes the grant back.
    """

    # `queryable: True` is the record of the federation step having granted FOR
    # THIS ROW, which is what the revoke is gated on — see the test below for the
    # row that has no grant to take back.
    _ITEM = {"athenaDatabase": _DATABASE, "sourceSubType": "GLUE_DATABASE", "queryable": True}
    _ARGS = {"namespace_id": _NAMESPACE, "source_id": "abc"}

    @staticmethod
    def _run(item: dict, scan_pages: list[PaginatedResult] | Exception):
        import coa_sources.api.sources_handler as handler

        dao = dao_double()
        dao.scan.side_effect = scan_pages
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": _CONSUMER}}
        session = MagicMock()
        session.client.return_value = ssm
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}
        }
        with (
            patch.object(handler, "_get_dao", return_value=dao),
            patch.object(handler, "_get_sts", return_value=sts),
            patch.object(handler, "_FEDERATION_PROVISIONER_ROLE_ARN", "arn:aws:iam::111111111111:role/fed"),
            patch.object(handler, "_CONSUMER_QUERY_ROLE_SSM_PARAM", "/coa/serve/runtime-role-arn"),
            patch.object(handler.boto3, "Session", return_value=session),
            patch(f"{SOURCES}.revoke_consumer_select_native", return_value=True) as revoke,
        ):
            handler._revoke_native_lf_grant(item=item, **TestRevokeOnDelete._ARGS)
        return revoke, dao

    def test_the_last_source_referencing_the_database_revokes(self):
        revoke, _ = self._run(self._ITEM, [PaginatedResult(items=[])])

        revoke.assert_called_once_with(database_name=_DATABASE, principal_arn=_CONSUMER, session=ANY)

    def test_a_co_owning_namespace_keeps_the_grant(self):
        """Two namespaces may legitimately catalog one database and share one grant."""
        other = {"PK": "NS#other", "SK": "SRC#xyz", "athenaDatabase": _DATABASE}

        revoke, _ = self._run(self._ITEM, [PaginatedResult(items=[other])])

        revoke.assert_not_called()

    def test_the_source_being_deleted_is_excluded_from_the_count(self):
        """Its own row is still in the table — it is deleted after this runs."""
        revoke, dao = self._run(self._ITEM, [PaginatedResult(items=[])])

        values = dao.scan.call_args.kwargs["expression_values"]
        assert values[":pk"] == f"NS#{_NAMESPACE}"
        assert values[":sk"] == "SRC#abc"
        assert "NOT (PK = :pk AND SK = :sk)" in dao.scan.call_args.kwargs["filter_expression"]

    def test_an_empty_page_with_more_to_read_keeps_looking(self):
        """A filtered scan returns empty pages; stopping at the first would over-revoke."""
        pages = [
            PaginatedResult(items=[], last_evaluated_key={"PK": "NS#a", "SK": "SRC#b"}),
            PaginatedResult(items=[{"PK": "NS#other", "SK": "SRC#xyz"}]),
        ]

        revoke, dao = self._run(self._ITEM, pages)

        assert dao.scan.call_count == 2
        revoke.assert_not_called()

    def test_an_unanswerable_reference_count_keeps_the_grant(self):
        """Unknown is not zero. Breaking a live co-owner is worse than a stale grant."""
        revoke, _ = self._run(self._ITEM, ClientError({"Error": {"Code": "ThrottlingException"}}, "Scan"))

        revoke.assert_not_called()

    def test_running_out_of_scan_pages_keeps_the_grant(self):
        """The page budget is a bound on work, and exhausting it means "unknown".

        Guards the fall-through specifically: if it ever returned False, or the
        loop became ``while start_key``, a table large enough to exceed the
        budget would start revoking grants a co-owning namespace still uses.
        """
        import coa_sources.api.sources_handler as handler

        pages = [PaginatedResult(items=[], last_evaluated_key={"PK": "NS#a", "SK": f"SRC#{n}"}) for n in range(5)]
        with patch.object(handler, "_GLUE_DB_REFERENCE_SCAN_MAX_PAGES", 2):
            revoke, dao = self._run(self._ITEM, pages)

        assert dao.scan.call_count == 2
        revoke.assert_not_called()

    def test_a_legacy_cross_account_row_still_has_its_local_grant_revoked(self):
        """Rows written before the grant was gated are the ones worth cleaning up.

        Such a row names a foreign catalog and carries a real grant on the LOCAL
        database of the same name — its ``queryable: True`` is the evidence that
        the grant was made — so the revoke must not be gated on the declared
        catalog the way the grant is.
        """
        item = dict(self._ITEM, configuration=json.dumps({"catalogId": _FOREIGN, "databaseName": _DATABASE}))

        revoke, _ = self._run(item, [PaginatedResult(items=[])])

        revoke.assert_called_once_with(database_name=_DATABASE, principal_arn=_CONSUMER, session=ANY)

    @pytest.mark.parametrize("queryable", [False, None, "true"])
    def test_a_row_that_never_got_a_grant_revokes_nothing(self, queryable):
        """Without this gate the fix hands out a denial-of-access primitive.

        A source created AFTER the grant was gated, declaring a catalog it does
        not own, produces no grant and is recorded ``queryable: False``. If its
        delete still revoked, any namespace could strip the shared serve runtime
        role's permissions off any local database — including ones an operator
        granted by hand — just by naming it in a source and deleting it. So the
        revoke follows the row's own record of a grant, not the caller's word.
        """
        item = {"athenaDatabase": _DATABASE, "sourceSubType": "GLUE_DATABASE"}
        if queryable is not None:
            item["queryable"] = queryable

        revoke, dao = self._run(item, [PaginatedResult(items=[])])

        revoke.assert_not_called()
        # Not even the scan: there is nothing to decide about.
        dao.scan.assert_not_called()

    def test_a_row_with_no_database_does_nothing(self):
        revoke, dao = self._run({"sourceSubType": "GLUE_DATABASE", "queryable": True}, [PaginatedResult(items=[])])

        revoke.assert_not_called()
        dao.scan.assert_not_called()

    def test_a_revoke_failure_does_not_raise(self):
        """The delete must not be blocked by Lake Formation having a bad minute."""
        import coa_sources.api.sources_handler as handler

        dao = dao_double()
        dao.scan.return_value = PaginatedResult(items=[])
        sts = MagicMock()
        sts.assume_role.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole")
        with (
            patch.object(handler, "_get_dao", return_value=dao),
            patch.object(handler, "_get_sts", return_value=sts),
            patch.object(handler, "_FEDERATION_PROVISIONER_ROLE_ARN", "arn:aws:iam::111111111111:role/fed"),
        ):
            handler._revoke_native_lf_grant(item=self._ITEM, **self._ARGS)

    def test_a_revoke_that_left_the_grant_behind_warns(self):
        """The row is about to go, so this log line is the only surviving record.

        At INFO it reads as routine output and alerts on nothing, which is the
        one case where a lingering permission goes unnoticed.
        """
        import coa_sources.api.sources_handler as handler

        dao = dao_double()
        dao.scan.return_value = PaginatedResult(items=[])
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": _CONSUMER}}
        session = MagicMock()
        session.client.return_value = ssm
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}
        }
        with (
            patch.object(handler, "_get_dao", return_value=dao),
            patch.object(handler, "_get_sts", return_value=sts),
            patch.object(handler, "_FEDERATION_PROVISIONER_ROLE_ARN", "arn:aws:iam::111111111111:role/fed"),
            patch.object(handler, "_CONSUMER_QUERY_ROLE_SSM_PARAM", "/coa/serve/runtime-role-arn"),
            patch.object(handler.boto3, "Session", return_value=session),
            patch(f"{SOURCES}.revoke_consumer_select_native", return_value=False),
            patch.object(handler, "logger") as logger,
        ):
            handler._revoke_native_lf_grant(item=self._ITEM, **self._ARGS)

        logger.warning.assert_called_once_with(
            "lf_native_revoke_on_delete_incomplete",
            source_id="abc",
            database=_DATABASE,
        )

    def test_an_unresolved_consumer_role_revokes_nothing(self):
        """A revoke needs the principal it was granted to; a wildcard is not available."""
        import coa_sources.api.sources_handler as handler

        dao = dao_double()
        dao.scan.return_value = PaginatedResult(items=[])
        sts = MagicMock()
        sts.assume_role.return_value = {
            "Credentials": {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T"}
        }
        with (
            patch.object(handler, "_get_dao", return_value=dao),
            patch.object(handler, "_get_sts", return_value=sts),
            patch.object(handler, "_FEDERATION_PROVISIONER_ROLE_ARN", "arn:aws:iam::111111111111:role/fed"),
            patch.object(handler, "_CONSUMER_QUERY_ROLE_SSM_PARAM", ""),
            patch(f"{SOURCES}.revoke_consumer_select_native") as revoke,
        ):
            handler._revoke_native_lf_grant(item=self._ITEM, **self._ARGS)

        revoke.assert_not_called()


class TestDiscoveryWithholdsQueryable:
    """Discovery must not mark a source queryable that the federation step will refuse.

    Discovery runs first and used to write ``queryable: true`` for every scanned
    GLUE_DATABASE source. For a foreign or nested ``catalogId`` that opened
    serve's SQL scope to the same-named local database until the federation
    step wrote ``false`` — and left it open if that step failed — and it made
    the delete-time revoke, gated on ``queryable``, act on a database this
    deployment never granted for.
    """

    DISCOVERY = "coa_sources.database.pipeline.discovery_handler"

    def _queryable_writes(self, item: dict) -> list:
        from coa_common.domain_models import Column, DiscoveredMetadata, Table
        from coa_sources.database.connectors.base import ConnectionTestResult
        from coa_sources.database.pipeline import discovery_handler

        ds_dao = MagicMock()
        ds_dao.get.return_value = item
        ns_dao = dao_double()
        ns_dao.get.return_value = {"dataZoneProjectId": "proj-123"}
        connector = MagicMock()
        connector.test_connection.return_value = ConnectionTestResult(success=True, message="OK", checks=[])
        connector.discover_metadata.return_value = DiscoveredMetadata(
            tables=[Table(name="orders", database=_DATABASE, columns=[Column(name="id", data_type="bigint")])]
        )
        with (
            patch.object(discovery_handler, "_get_ds_dao", return_value=ds_dao),
            patch.object(discovery_handler, "_get_scan_dao", return_value=dao_double()),
            patch.object(discovery_handler, "_get_ns_dao", return_value=ns_dao),
            patch.object(discovery_handler, "get_connector", return_value=connector),
            patch.object(
                discovery_handler, "write_to_datazone", return_value={"assets_created": 1, "assets_revised": 0}
            ),
        ):
            discovery_handler.handler(
                {"datasourceId": "DS#abc", "scanJobId": "SCAN#s1", "namespaceId": _NAMESPACE, "scanType": "full"},
                None,
            )
        return [
            c.kwargs["update_fields"]["queryable"]
            for c in ds_dao.update.call_args_list
            if "queryable" in c.kwargs.get("update_fields", {})
        ]

    def test_a_local_catalog_is_marked_queryable(self):
        assert self._queryable_writes(_glue_item(catalog_id=_LOCAL)) == [True]

    def test_a_foreign_catalog_is_not_marked_queryable(self):
        item = _glue_item(catalog_id=_FOREIGN, cross_account_role_arn=f"arn:aws:iam::{_FOREIGN}:role/reader")
        assert self._queryable_writes(item) == []

    def test_a_nested_catalog_in_this_account_is_not_marked_queryable(self):
        assert self._queryable_writes(_glue_item(catalog_id=f"{_LOCAL}:some_catalog")) == []

    def test_an_unresolvable_account_is_not_marked_queryable(self):
        import coa_sources.database.connectors.glue_connection_provisioner as prov

        with patch.object(prov, "_get_account_id", side_effect=BotoCoreError()):
            assert self._queryable_writes(_glue_item(catalog_id=_LOCAL)) == []
