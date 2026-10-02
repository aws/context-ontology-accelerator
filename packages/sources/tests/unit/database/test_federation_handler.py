# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dedicated federation provisioner Lambda."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

# Imported for its side effect: mock.patch() resolves dotted targets by
# attribute traversal, so this submodule must be bound on its parent
# package before any patch(f"{MODULE}...") is evaluated.
import coa_sources.database.pipeline.federation_handler  # noqa: F401
import pytest
from coa_sources.database import glue_ownership as _go

from tests.unit.conftest import dao_double  # noqa: E402

MODULE = "coa_sources.database.pipeline.federation_handler"
PROVISIONER = "coa_sources.database.connectors.glue_connection_provisioner"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SOURCES_TABLE", "sources")
    monkeypatch.setenv("SOURCE_SCAN_JOBS_TABLE", "scan-jobs")
    monkeypatch.setenv("SMUS_DOMAIN_ID", "dz-test")
    monkeypatch.setenv("NAMESPACES_TABLE", "namespaces")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def _reset():
    import coa_sources.database.pipeline.federation_handler as mod

    mod._dao = None
    yield
    mod._dao = None


@pytest.fixture(autouse=True)
def _secret_bound():
    """Default the namespace-binding re-check to "bound".

    Every JDBC case below is about provisioning, not binding, and the real check
    would reach STS/Secrets Manager. The cases that are about binding patch this
    themselves — see TestScanTimeNamespaceBinding.
    """
    with patch(f"{MODULE}.require_secret_namespace_binding") as require:
        yield require


_EVENT = {
    "datasourceId": "DS#abc",
    "sourceId": "abc",
    "namespaceId": "ns-1",
}
_JDBC_ITEM = {
    "sourceSubType": "JDBC_DATABASE",
    "configuration": {
        "engine": "POSTGRESQL",
        "host": "db.example.com",
        "port": 5432,
        "databaseName": "bird",
        "credentialSecretArn": "arn:aws:secretsmanager:us-east-1:123:secret:s-AbCdEf",
    },
}


def _patch_dao(item):
    dao = dao_double()
    dao.get.return_value = item
    return patch(f"{MODULE}._get_dao", return_value=dao), dao


@pytest.mark.unit
class TestFederationHandler:
    def test_glue_native_grants_lf_and_marks_queryable(self):
        """GLUE_DATABASE sources get an LF native grant instead of catalog provisioning.

        Regression test: accounts with strict LF mode (IAM_ALLOWED_PRINCIPALS removed)
        received PERMISSION_DENIED from Athena because no LF data permission was ever
        granted to the serve runtime role for native Glue tables.
        """
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao({"sourceSubType": "GLUE_DATABASE", "athenaDatabase": "retail_demo"})
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/serve"),
            patch(f"{MODULE}.grant_consumer_select_native", return_value=True) as grant,
        ):
            out = handler(_EVENT)

        prov.assert_not_called()
        grant.assert_called_once_with(
            database_name="retail_demo",
            principal_arn="arn:aws:iam::123:role/serve",
        )
        dao.update.assert_called_once()
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": True}
        assert out == {"provisioned": False, "reason": "glue-native", "queryable": True}

    def test_glue_native_grant_is_refused_for_an_unowned_database(self):
        """F-8: this function runs as a Lake Formation admin and the principal it
        grants is the SHARED serve runtime role, so a grant here makes the database
        readable by every namespace's queries. It verifies ownership rather than
        trusting that source-create did — this step is reached from the stored row.
        """
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(
            {
                "sourceSubType": "GLUE_DATABASE",
                "athenaDatabase": "someone_elses_db",
                "configuration": json.dumps({"catalogId": "123456789012", "databaseName": "someone_elses_db"}),
            }
        )
        with (
            ctx,
            patch(
                f"{MODULE}.assert_namespace_may_catalog",
                side_effect=_go.GlueOwnershipError("not registered to namespace"),
            ),
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/serve"),
            patch(f"{MODULE}.grant_consumer_select_native") as grant,
        ):
            out = handler(_EVENT)

        grant.assert_not_called()
        # Left not-queryable rather than failing the step: discovery already refused
        # the same target, so a source reaching here unverified is a legacy row or
        # one whose tag was removed after onboarding.
        assert out["provisioned"] is False
        assert out["queryable"] is False
        # The reason carries the actionable message, not just a code: this dict is
        # the Step Functions output an operator reads first.
        assert out["reason"].startswith("namespace-not-owner: ")
        assert "not registered to namespace" in out["reason"]
        dao.update.assert_not_called()

    def test_glue_native_grant_checks_the_stored_target(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao(
            {
                "sourceSubType": "GLUE_DATABASE",
                "athenaDatabase": "retail_demo",
                "configuration": json.dumps(
                    {"catalogId": "123456789012", "databaseName": "retail_demo", "region": "eu-west-1"}
                ),
            }
        )
        with (
            ctx,
            patch(f"{MODULE}.assert_namespace_may_catalog") as check,
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/serve"),
            patch(f"{MODULE}.grant_consumer_select_native", return_value=True),
        ):
            handler(_EVENT)

        kwargs = check.call_args.kwargs
        assert kwargs["namespace_id"] == _EVENT["namespaceId"]
        assert kwargs["catalog_id"] == "123456789012"
        assert kwargs["database_name"] == "retail_demo"
        assert kwargs["region"] == "eu-west-1"

    def test_skips_non_jdbc_non_glue_source(self):
        """Other source types (e.g. DOCUMENT_SOURCE) are still skipped entirely."""
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao({"sourceSubType": "DOCUMENT_SOURCE"})
        with ctx, patch(f"{MODULE}.provision_federated_catalog") as prov:
            out = handler(_EVENT)
        assert out == {"provisioned": False, "reason": "not-jdbc"}
        prov.assert_not_called()

    def test_glue_native_skips_when_no_athena_database(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao({"sourceSubType": "GLUE_DATABASE"})
        with ctx, patch(f"{MODULE}.grant_consumer_select_native") as grant:
            out = handler(_EVENT)
        assert out == {"provisioned": False, "reason": "no-athena-database"}
        grant.assert_not_called()

    def test_glue_native_grant_failure_writes_queryable_false(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao({"sourceSubType": "GLUE_DATABASE", "athenaDatabase": "retail_demo"})
        with (
            ctx,
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/serve"),
            patch(f"{MODULE}.grant_consumer_select_native", return_value=False),
        ):
            out = handler(_EVENT)

        # Grant failed → always write queryable=False so stale True from discovery is cleared.
        dao.update.assert_called_once()
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": False}
        assert out["queryable"] is False

    def test_glue_native_ddb_write_failure_is_swallowed(self):
        """DDB update failure after a successful LF grant does not raise.

        The LF grant is idempotent — even if the DDB write races with a
        concurrent source deletion, the permission persists and a re-scan
        will retry the write. The handler must not raise so the scan pipeline
        does not mark the source SCAN_FAILED.
        """
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao({"sourceSubType": "GLUE_DATABASE", "athenaDatabase": "retail_demo"})
        dao.update.side_effect = RuntimeError("ddb gone")
        with (
            ctx,
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/serve"),
            patch(f"{MODULE}.grant_consumer_select_native", return_value=True),
        ):
            # Must not raise despite DDB failure.
            out = handler(_EVENT)
        assert out == {"provisioned": False, "reason": "glue-native", "queryable": True}

    def test_skips_incomplete_jdbc_config(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao({"sourceSubType": "JDBC_DATABASE", "configuration": {"engine": "POSTGRESQL"}})
        with ctx, patch(f"{MODULE}.provision_federated_catalog") as prov:
            out = handler(_EVENT)
        assert out["provisioned"] is False
        prov.assert_not_called()

    def test_grant_with_no_discovered_schemas(self):
        from coa_sources.database.pipeline.federation_handler import handler

        # No discoveredSchemas on the record → grant is called with an empty list,
        # which grant_consumer_select treats as a no-op → source stays not-queryable.
        ctx, dao = _patch_dao(_JDBC_ITEM)
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/consumer"),
            patch(f"{MODULE}._grant_secret_read_to_consumer"),
            patch(f"{MODULE}.grant_consumer_select", return_value=False) as grant,
        ):
            prov.return_value = {"glueConnectionName": "c", "athenaDataCatalogName": "cat"}
            out = handler(_EVENT)
        grant.assert_called_once_with(catalog_name="cat", schemas=[], principal_arn="arn:aws:iam::123:role/consumer")
        assert dao.update.call_args.kwargs["update_fields"]["queryable"] is False
        assert out["queryable"] is False

    def test_skips_when_connector_cannot_read_secret(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao(_JDBC_ITEM)
        with (
            ctx,
            patch(f"{MODULE}._secret_readable_by_connector", return_value=False),
            patch(f"{MODULE}.provision_federated_catalog") as prov,
        ):
            out = handler(_EVENT)
        assert out == {"provisioned": False, "reason": "secret-unreadable"}
        prov.assert_not_called()

    def test_provisions_and_persists(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(_JDBC_ITEM)
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}.grant_consumer_select", return_value=True),
        ):
            prov.return_value = {"glueConnectionName": "c", "athenaDataCatalogName": "cat"}
            out = handler(_EVENT)
        assert out == {
            "provisioned": True,
            "queryable": True,
            "glueConnectionName": "c",
            "athenaDataCatalogName": "cat",
        }
        prov.assert_called_once()
        dao.update.assert_called_once()
        assert dao.update.call_args.kwargs["update_fields"]["queryable"] is True

    def test_provision_failure_raises(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(_JDBC_ITEM)
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            handler(_EVENT)
        dao.update.assert_not_called()

    def test_persist_failure_rolls_back_and_raises(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(_JDBC_ITEM)
        dao.update.side_effect = RuntimeError("ddb down")
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}.grant_consumer_select", return_value=True),
            patch(f"{MODULE}.cleanup_federated_resources") as cleanup,
        ):
            prov.return_value = {"glueConnectionName": "c", "athenaDataCatalogName": "cat"}
            with pytest.raises(RuntimeError, match="ddb down"):
                handler(_EVENT)
        cleanup.assert_called_once_with(glue_connection_name="c", athena_catalog_name="cat")

    def test_grants_lf_select_to_consumer_role(self):
        from coa_sources.database.pipeline.federation_handler import handler

        # Schemas come from discovery's discoveredSchemas, lowercased to match the
        # federated catalog's DB names (CATALOG_CASING_FILTER=LOWERCASE_ONLY).
        ctx, dao = _patch_dao({**_JDBC_ITEM, "discoveredSchemas": ["Public", "Sales"]})
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/consumer"),
            patch(f"{MODULE}._grant_secret_read_to_consumer"),
            patch(f"{MODULE}.grant_iam_allowed_principals", return_value=True) as iam_grant,
            patch(f"{MODULE}.grant_consumer_select", return_value=True) as grant,
        ):
            prov.return_value = {"glueConnectionName": "c", "athenaDataCatalogName": "cat"}
            handler(_EVENT)
        grant.assert_called_once_with(
            catalog_name="cat", schemas=["public", "sales"], principal_arn="arn:aws:iam::123:role/consumer"
        )
        # Tables inherit access from a database-level IAM_ALLOWED_PRINCIPALS grant,
        # which must target the DISCOVERED schemas — granting only the hardcoded
        # "public" governs nothing on engines that have no "public" database.
        iam_grant.assert_called_once_with(catalog_name="cat", schemas=["public", "sales"])
        assert dao.update.call_args.kwargs["update_fields"]["queryable"] is True

    def test_not_queryable_when_grant_fails(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao({**_JDBC_ITEM, "discoveredSchemas": ["public"]})
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}._consumer_role_arn", return_value=""),
            patch(f"{MODULE}.grant_iam_allowed_principals", return_value=True),
            patch(f"{MODULE}.grant_consumer_select", return_value=False) as grant,
        ):
            prov.return_value = {"glueConnectionName": "c", "athenaDataCatalogName": "cat"}
            out = handler(_EVENT)
        # No grant (no consumer principal) → provisioned but NOT queryable.
        grant.assert_called_once_with(catalog_name="cat", schemas=["public"], principal_arn="")
        assert dao.update.call_args.kwargs["update_fields"]["queryable"] is False
        assert out["queryable"] is False


class TestCustomConnectorBranch:
    """A custom-connector source needs no provisioning here — its Lambda-backed
    Athena data catalog was registered at source-create, because this sub-type's
    discovery queries it and discovery runs first. All that remains is marking the
    source queryable."""

    _ITEM = {"sourceSubType": "CUSTOM_CONNECTOR", "athenaDataCatalogName": "coadevds_abc123"}

    def test_marks_the_source_queryable(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(dict(self._ITEM))
        with ctx:
            out = handler(_EVENT)
        assert out == {"provisioned": False, "reason": "connector-backed", "queryable": True}
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": True}
        # Guards against a concurrently-deleted row being resurrected.
        assert dao.update.call_args.kwargs["condition"] == "attribute_exists(PK)"

    def test_provisions_no_glue_or_lake_formation_resources(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao(dict(self._ITEM))
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}.grant_consumer_select") as grant,
            patch(f"{MODULE}.grant_consumer_select_native") as grant_native,
            patch(f"{MODULE}.grant_iam_allowed_principals") as iam_grant,
        ):
            handler(_EVENT)
        # There is no Glue object behind a Lambda catalog, so there is nothing to
        # provision and nothing for Lake Formation to govern.
        prov.assert_not_called()
        grant.assert_not_called()
        grant_native.assert_not_called()
        iam_grant.assert_not_called()

    def test_a_failed_write_raises_rather_than_leaving_it_unqueryable(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(dict(self._ITEM))
        dao.update.side_effect = RuntimeError("ddb down")
        # Silently leaving queryable False would present as a source that scanned
        # cleanly and then answers nothing. Nothing needs rolling back, and a
        # re-scan retries.
        with ctx, pytest.raises(RuntimeError):
            handler(_EVENT)


class TestDatabricksBranch:
    """The same branch as CUSTOM_CONNECTOR, reached through the shared set.

    This is one of the two switches that FAILED OPEN. Adding the sub-type to
    ``_HANDLED_SUB_TYPES`` only silences the tripwire below; the write that flips
    ``queryable`` was a separate exact-equality comparison, and anything else fell
    through to a ``not JDBC_DATABASE`` no-op returning ``not-jdbc``. Create persists
    ``queryable: False`` and discovery writes it only for GLUE_DATABASE — so a source
    that missed this branch would scan cleanly, resolve no catalog at serve, and be
    skipped by ``sql_namespace_scope``, which also silently defeats the namespace-scope
    requirement this sub-type depends on.
    """

    _ITEM = {"sourceSubType": "DATABRICKS_SQL_WAREHOUSE", "athenaDataCatalogName": "coadevds_abc123"}

    def test_marks_the_source_queryable(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(dict(self._ITEM))
        with ctx:
            out = handler(_EVENT)
        assert out == {"provisioned": False, "reason": "connector-backed", "queryable": True}
        assert dao.update.call_args.kwargs["update_fields"] == {"queryable": True}

    def test_makes_no_grant_of_any_kind(self):
        """Not because there is nothing to grant on — there is a credential — but
        because COA never holds a grant on it. The connector assumes the CUSTOMER's role
        at request time, so there is nothing to grant here and nothing to revoke at
        delete."""
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao(dict(self._ITEM))
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}.grant_consumer_select") as grant,
            patch(f"{MODULE}.grant_consumer_select_native") as grant_native,
            patch(f"{MODULE}.grant_iam_allowed_principals") as iam_grant,
            patch(f"{MODULE}._grant_secret_read_to_consumer") as secret_grant,
        ):
            handler(_EVENT)
        prov.assert_not_called()
        grant.assert_not_called()
        grant_native.assert_not_called()
        iam_grant.assert_not_called()
        secret_grant.assert_not_called()

    def test_does_not_fall_through_to_the_not_jdbc_no_op(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, dao = _patch_dao(dict(self._ITEM))
        with ctx:
            out = handler(_EVENT)
        assert out["reason"] != "not-jdbc"
        dao.update.assert_called_once()


class TestUnhandledSubType:
    """An absent sub-type must stay a no-op; a recognised DATABASE sub-type with
    no branch here must not."""

    def test_an_absent_sub_type_is_a_no_op(self):
        from coa_sources.database.pipeline.federation_handler import handler

        # A source deleted concurrently with its scan reads back as an empty dict.
        ctx, dao = _patch_dao({})
        with ctx:
            out = handler(_EVENT)
        assert out == {"provisioned": False, "reason": "not-jdbc"}
        dao.update.assert_not_called()

    def test_an_unrecognised_sub_type_is_a_no_op(self):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao({"sourceSubType": "SOMETHING_LEGACY"})
        with ctx:
            assert handler(_EVENT)["reason"] == "not-jdbc"

    def test_a_documents_sub_type_is_a_no_op(self):
        from coa_sources.database.pipeline.federation_handler import handler

        # These never reach this pipeline; a row carrying one is mis-stored, and
        # policing that is not this handler's job.
        ctx, _ = _patch_dao({"sourceSubType": "S3"})
        with ctx:
            assert handler(_EVENT)["reason"] == "not-jdbc"

    def test_a_recognised_database_sub_type_with_no_branch_raises(self):
        from coa_sources.database.pipeline.federation_handler import handler

        # Simulates a new DATABASE sub-type shipping without its branch. Without
        # the raise, every source of that type would scan cleanly and then stay
        # queryable=False with no other signal.
        ctx, _ = _patch_dao({"sourceSubType": "FUTURE_DATABASE"})
        with (
            ctx,
            patch(f"{MODULE}._UNHANDLED_DATABASE_SUB_TYPES", frozenset({"FUTURE_DATABASE"})),
            pytest.raises(RuntimeError, match="No federation branch"),
        ):
            handler(_EVENT)

    def test_the_set_is_empty_while_every_database_sub_type_has_a_branch(self):
        from coa_sources.database.pipeline.federation_handler import _UNHANDLED_DATABASE_SUB_TYPES

        # This is the tripwire: adding a DATABASE sub-type to the Smithy enum
        # without a branch above makes this fail, here, rather than in production.
        assert not _UNHANDLED_DATABASE_SUB_TYPES


@pytest.mark.unit
class TestGrantSecretReadToConsumer:
    """The serve runtime's read grant must be namespace-bound."""

    def _run(self, namespace_id="ns-1"):
        import json

        from botocore.exceptions import ClientError
        from coa_sources.database.pipeline.federation_handler import _grant_secret_read_to_consumer

        sm = MagicMock()
        sm.get_resource_policy.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "none"}}, "GetResourcePolicy"
        )
        with (
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/consumer"),
            patch(f"{MODULE}.boto3.client", return_value=sm),
        ):
            _grant_secret_read_to_consumer("arn:aws:secretsmanager:us-east-1:123:secret:s-AbCdEf", namespace_id)
        assert sm.put_resource_policy.called
        policy = json.loads(sm.put_resource_policy.call_args.kwargs["ResourcePolicy"])
        return next(s for s in policy["Statement"] if s.get("Sid") == "SCLRuntimeSecretRead")

    def test_grant_is_conditioned_on_the_namespace_tag(self):
        from coa_common.constants import namespace_tag_condition_patterns, namespace_tag_key

        ns = "550e8400-e29b-41d4-a716-446655440000"
        stmt = self._run(namespace_id=ns)
        # StringLike, not StringEquals: the tag value may list several namespaces,
        # so the grant matches this namespace as an ENTRY. StringEquals would never
        # match a shared secret and would silently kill the direct-JDBC read path.
        assert stmt["Condition"] == {
            "StringLike": {f"secretsmanager:ResourceTag/{namespace_tag_key()}": namespace_tag_condition_patterns(ns)}
        }
        assert stmt["Action"] == "secretsmanager:GetSecretValue"

    def test_condition_matches_a_shared_multi_namespace_tag(self):
        """Evaluate the emitted patterns the way IAM would, against real values."""
        import re

        from coa_common.constants import namespace_tag_key

        ns = "550e8400-e29b-41d4-a716-446655440000"
        other = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        patterns = self._run(namespace_id=ns)["Condition"]["StringLike"][
            f"secretsmanager:ResourceTag/{namespace_tag_key()}"
        ]

        def binds(value: str) -> bool:
            return any(re.fullmatch(".*".join(re.escape(part) for part in p.split("*")), value) for p in patterns)

        assert binds(ns)
        assert binds(f"{ns} {other}")
        assert binds(f"{other} {ns}")
        assert not binds(other)
        assert not binds(f"prefixed{ns}")

    def test_no_grant_without_consumer_arn(self):
        # No consumer principal → nothing to grant; must not touch the policy.
        from coa_sources.database.pipeline.federation_handler import _grant_secret_read_to_consumer

        sm = MagicMock()
        with (
            patch(f"{MODULE}._consumer_role_arn", return_value=""),
            patch(f"{MODULE}.boto3.client", return_value=sm),
        ):
            _grant_secret_read_to_consumer("arn:aws:secretsmanager:us-east-1:123:secret:s-AbCdEf", "ns-1")
        sm.put_resource_policy.assert_not_called()


@pytest.mark.unit
class TestScanTimeNamespaceBinding:
    """The provisioner re-checks the STORED secret ARN before it touches it.

    Registration validates the ARN a caller supplies; this handler reads the ARN
    back out of the sources row. Rows written before the binding rule existed, and
    secrets re-tagged after registration, are only caught here.
    """

    def test_binding_is_checked_against_the_stored_arn_and_row_namespace(self, _secret_bound):
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao(_JDBC_ITEM)
        with (
            ctx,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}.grant_consumer_select", return_value=True),
        ):
            prov.return_value = {"glueConnectionName": "c", "athenaDataCatalogName": "cat"}
            handler(_EVENT)

        _secret_bound.assert_called_once_with(
            _JDBC_ITEM["configuration"]["credentialSecretArn"],
            "ns-1",
            "DS#abc",
        )

    def test_unbound_secret_raises_before_anything_reads_it(self, _secret_bound):
        """A refusal must abort the step, not fall through to a not-queryable no-op.

        The two things that follow both act on the named secret: the readability
        precheck reads it, and the consumer grant rewrites its resource policy.
        Neither may run.
        """
        from coa_sources.database.pipeline.federation_handler import handler

        _secret_bound.side_effect = RuntimeError("Credential secret is not bound to namespace ns-1")
        ctx, _ = _patch_dao(_JDBC_ITEM)
        with (
            ctx,
            patch(f"{MODULE}._secret_readable_by_connector") as readable,
            patch(f"{MODULE}.provision_federated_catalog") as prov,
            patch(f"{MODULE}._grant_secret_read_to_consumer") as grant,
            pytest.raises(RuntimeError, match="not bound to namespace"),
        ):
            handler(_EVENT)

        readable.assert_not_called()
        prov.assert_not_called()
        grant.assert_not_called()

    def test_binding_is_not_checked_for_glue_native_sources(self, _secret_bound):
        # GLUE_DATABASE sources have no credential secret; the check must not run.
        from coa_sources.database.pipeline.federation_handler import handler

        ctx, _ = _patch_dao({"sourceSubType": "GLUE_DATABASE", "athenaDatabase": "retail_demo"})
        with (
            ctx,
            patch(f"{MODULE}._consumer_role_arn", return_value="arn:aws:iam::123:role/serve"),
            patch(f"{MODULE}.grant_consumer_select_native", return_value=True),
        ):
            handler(_EVENT)
        _secret_bound.assert_not_called()
