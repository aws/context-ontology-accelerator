# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for shared namespace-teardown cleanup primitives (cleanup.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX

MODULE = "coa_control_plane.namespace.cleanup"


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    monkeypatch.setenv("RESOURCE_ROLE_MAPPINGS_TABLE", "test-rrm")
    monkeypatch.setenv("ROLES_TABLE", "test-roles")
    monkeypatch.setenv("DATAZONE_DOMAIN_ID", "dz-domain-123")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.mark.unit
class TestDeleteNamespaceRecordsTransaction:
    """Tests for the atomic DDB transaction step."""

    def test_transact_write_includes_namespace_and_reservation(self):
        with patch(f"{MODULE}.boto3") as mock_boto:
            mock_ddb = MagicMock()
            mock_boto.client.return_value = mock_ddb

            from coa_control_plane.namespace.cleanup import delete_namespace_records

            delete_namespace_records("ns-123", "sales")

        mock_ddb.transact_write_items.assert_called_once()
        items = mock_ddb.transact_write_items.call_args.kwargs["TransactItems"]
        assert len(items) == 2
        assert items[0]["Delete"]["Key"]["PK"]["S"] == "NS#ns-123"
        assert items[1]["Delete"]["Key"]["PK"]["S"] == "NS_NAME#sales"

    def test_transact_write_without_name(self):
        with patch(f"{MODULE}.boto3") as mock_boto:
            mock_ddb = MagicMock()
            mock_boto.client.return_value = mock_ddb

            from coa_control_plane.namespace.cleanup import delete_namespace_records

            delete_namespace_records("ns-123", None)

        items = mock_ddb.transact_write_items.call_args.kwargs["TransactItems"]
        assert len(items) == 1  # Only namespace record, no reservation


@pytest.mark.unit
class TestDeleteSmuProject:
    """delete_smu_project must force-delete the DataZone project (skipDeletionCheck)."""

    def test_delete_project_passes_skip_deletion_check(self, monkeypatch):
        """delete_project must pass skipDeletionCheck=True or namespaces that own DataZone assets fail to delete."""
        from coa_control_plane.namespace import cleanup

        mock_dz = MagicMock()
        monkeypatch.setattr(cleanup.boto3, "client", lambda *a, **k: mock_dz)

        cleanup.delete_smu_project("ns-123", {"dataZoneProjectId": "proj-abc"})

        mock_dz.delete_project.assert_called_once_with(
            domainIdentifier="dz-domain-123", identifier="proj-abc", skipDeletionCheck=True
        )

    def test_skips_when_no_project_or_domain(self, monkeypatch):
        from coa_control_plane.namespace import cleanup

        mock_dz = MagicMock()
        monkeypatch.setattr(cleanup.boto3, "client", lambda *a, **k: mock_dz)
        # No dataZoneProjectId → early return, no DataZone call.
        cleanup.delete_smu_project("ns-123", {})
        mock_dz.delete_project.assert_not_called()

    def test_assumes_project_access_role_when_configured(self, monkeypatch):
        """When PROJECT_ACCESS_ROLE_ARN is set, the project access role is assumed for DeleteProject."""
        from coa_control_plane.namespace import cleanup

        monkeypatch.setenv("PROJECT_ACCESS_ROLE_ARN", "arn:aws:iam::123456789012:role/dz-project-access")

        mock_sts = MagicMock()
        mock_sts.assume_role.return_value = {
            "Credentials": {
                "AccessKeyId": "AKIAFAKE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }
        mock_dz = MagicMock()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_dz

        def _client(service, **kwargs):
            assert service == "sts"
            return mock_sts

        monkeypatch.setattr(cleanup.boto3, "client", _client)
        monkeypatch.setattr(cleanup.boto3, "Session", lambda **kwargs: mock_session)

        cleanup.delete_smu_project("ns-123", {"dataZoneProjectId": "proj-abc"})

        mock_sts.assume_role.assert_called_once_with(
            RoleArn="arn:aws:iam::123456789012:role/dz-project-access",
            RoleSessionName="ns-deletion-platform-dz-access",
        )
        mock_dz.delete_project.assert_called_once_with(
            domainIdentifier="dz-domain-123", identifier="proj-abc", skipDeletionCheck=True
        )

    def test_falls_back_to_execution_role_when_no_role_arn_configured(self, monkeypatch):
        """Without PROJECT_ACCESS_ROLE_ARN, nothing is assumed and the caller's own credentials are used."""
        from coa_control_plane.namespace import cleanup

        monkeypatch.delenv("PROJECT_ACCESS_ROLE_ARN", raising=False)

        mock_dz = MagicMock()
        mock_sts = MagicMock()

        def _client(service, **kwargs):
            return mock_sts if service == "sts" else mock_dz

        monkeypatch.setattr(cleanup.boto3, "client", _client)

        cleanup.delete_smu_project("ns-123", {"dataZoneProjectId": "proj-abc"})

        mock_sts.assume_role.assert_not_called()
        mock_dz.delete_project.assert_called_once_with(
            domainIdentifier="dz-domain-123", identifier="proj-abc", skipDeletionCheck=True
        )


@pytest.mark.unit
class TestDeleteAthenaWorkgroup:
    def test_skips_when_no_workgroup_name(self, monkeypatch):
        from coa_control_plane.namespace import cleanup

        mock_athena = MagicMock()
        monkeypatch.setattr(cleanup.boto3, "client", lambda *a, **k: mock_athena)

        cleanup.delete_athena_workgroup(None)

        mock_athena.delete_work_group.assert_not_called()

    def test_deletes_workgroup(self, monkeypatch):
        from coa_control_plane.namespace import cleanup

        mock_athena = MagicMock()
        monkeypatch.setattr(cleanup.boto3, "client", lambda *a, **k: mock_athena)

        cleanup.delete_athena_workgroup(f"{RESOURCE_PREFIX}-dev-sales")

        mock_athena.delete_work_group.assert_called_once_with(
            WorkGroup=f"{RESOURCE_PREFIX}-dev-sales", RecursiveDeleteOption=True
        )


@pytest.mark.unit
class TestDeleteRoleMappingsAndRoles:
    def test_delete_role_mappings_no_table_configured_noop(self, monkeypatch):
        monkeypatch.delenv("RESOURCE_ROLE_MAPPINGS_TABLE", raising=False)
        from coa_control_plane.namespace import cleanup

        with patch(f"{MODULE}._delete_all_by_index") as mock_delete:
            cleanup.delete_role_mappings("ns-123")

        mock_delete.assert_not_called()

    def test_delete_role_mappings_queries_namespace_grants_index(self):
        from coa_control_plane.namespace import cleanup

        with patch(f"{MODULE}._delete_all_by_index") as mock_delete:
            cleanup.delete_role_mappings("ns-123")

        mock_delete.assert_called_once_with("test-rrm", "NamespaceGrantsIndex", "namespaceKey", "NS#ns-123")

    def test_delete_roles_queries_by_pk(self):
        from coa_control_plane.namespace import cleanup

        with patch(f"{MODULE}._delete_all_by_pk") as mock_delete:
            cleanup.delete_roles("ns-123")

        mock_delete.assert_called_once_with("test-roles", "NS#ns-123")


@pytest.mark.unit
class TestDeleteAllOntologies:
    """Ontology teardown failures propagate so the pipeline step retries."""

    def test_http_error_propagates(self, monkeypatch):
        from urllib.error import HTTPError

        from coa_control_plane.namespace import cleanup

        monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology.internal")

        def _raise(*_a, **_k):
            raise HTTPError("http://ontology.internal", 500, "boom", {}, None)

        monkeypatch.setattr(cleanup.urllib_request, "urlopen", _raise)

        with pytest.raises(HTTPError):
            cleanup.delete_all_ontologies("ns-123")

    def test_connection_error_propagates(self, monkeypatch):
        from urllib.error import URLError

        from coa_control_plane.namespace import cleanup

        monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology.internal")

        def _raise(*_a, **_k):
            raise URLError("unreachable")

        monkeypatch.setattr(cleanup.urllib_request, "urlopen", _raise)

        with pytest.raises(URLError):
            cleanup.delete_all_ontologies("ns-123")

    def test_unconfigured_endpoint_is_still_a_noop(self, monkeypatch):
        """No endpoint configured is a deployment shape, not a teardown failure."""
        from coa_control_plane.namespace import cleanup

        monkeypatch.delenv("ONTOLOGY_ENGINE_ENDPOINT", raising=False)

        cleanup.delete_all_ontologies("ns-123")  # must not raise


@pytest.mark.unit
class TestDeleteOntologyStepPropagates:
    """The pipeline step must not catch what cleanup.py now raises."""

    def test_handler_propagates_ontology_failure(self, monkeypatch):
        from coa_control_plane.namespace.deletion_pipeline import delete_ontology

        with (
            patch.object(delete_ontology, "delete_all_ontologies", side_effect=RuntimeError("AOSS throttled")),
            patch.object(delete_ontology, "delete_ontology_artifacts") as mock_s3,
            pytest.raises(RuntimeError, match="AOSS throttled"),
        ):
            delete_ontology.handler({"namespaceId": "ns-123"}, None)

        # S3 sweep must not run — the step is being retried from the top.
        mock_s3.assert_not_called()


@pytest.mark.unit
class TestDeleteGraphragIndexes:
    """GraphRAG doc-KG indexes are namespace-scoped and dropped at namespace teardown."""

    def test_deletes_all_three_index_names_for_the_namespace(self, monkeypatch):
        from coa_common.constants import to_graphrag_tenant_id
        from coa_control_plane.namespace import cleanup

        monkeypatch.setenv("OSS_ENDPOINT", "https://x.aoss.us-east-1.on.aws")
        mock_client = MagicMock()
        mock_client.delete_index.return_value = True
        monkeypatch.setattr(cleanup, "AossVectorClient", lambda **_k: mock_client)

        ns = "550e8400-e29b-41d4-a716-446655440000"
        removed = cleanup.delete_graphrag_indexes(ns)

        tenant = to_graphrag_tenant_id(ns)
        assert removed == [f"chunk_{tenant}", f"topic_{tenant}", f"statement_{tenant}"]

    def test_absent_indexes_are_not_reported_as_removed(self, monkeypatch):
        """Deleting an absent index is a no-op, which keeps this idempotent."""
        from coa_control_plane.namespace import cleanup

        monkeypatch.setenv("OSS_ENDPOINT", "https://x.aoss.us-east-1.on.aws")
        mock_client = MagicMock()
        mock_client.delete_index.return_value = False  # nothing existed
        monkeypatch.setattr(cleanup, "AossVectorClient", lambda **_k: mock_client)

        assert cleanup.delete_graphrag_indexes("550e8400-e29b-41d4-a716-446655440000") == []

    def test_noop_when_endpoint_unconfigured(self, monkeypatch):
        """A deployment without an AOSS collection must not fail teardown."""
        from coa_control_plane.namespace import cleanup

        monkeypatch.delenv("OSS_ENDPOINT", raising=False)
        called = MagicMock()
        monkeypatch.setattr(cleanup, "AossVectorClient", called)

        assert cleanup.delete_graphrag_indexes("ns-1") == []
        called.assert_not_called()

    def test_failure_propagates(self, monkeypatch):
        """Same hard-gate rationale as the ontology index: a swallowed failure
        here is an unreclaimable leak once the namespace record is gone."""
        from coa_control_plane.namespace import cleanup

        monkeypatch.setenv("OSS_ENDPOINT", "https://x.aoss.us-east-1.on.aws")
        mock_client = MagicMock()
        mock_client.delete_index.side_effect = RuntimeError("AOSS throttled")
        monkeypatch.setattr(cleanup, "AossVectorClient", lambda **_k: mock_client)

        with pytest.raises(RuntimeError, match="AOSS throttled"):
            cleanup.delete_graphrag_indexes("550e8400-e29b-41d4-a716-446655440000")


@pytest.mark.unit
class TestDeleteSourcesStepDropsGraphragIndexes:
    """The sources step is where 'no sources remain in this namespace' becomes true."""

    def test_indexes_dropped_after_sources(self, monkeypatch):
        from coa_control_plane.namespace.deletion_pipeline import delete_sources

        order: list[str] = []

        def _fake_invoke(event):
            if event["httpMethod"] == "GET":
                return {"statusCode": 200, "body": json.dumps({"items": [{"sourceId": "s-1"}]})}
            order.append("delete_source")
            return {"statusCode": 202}

        monkeypatch.setattr(delete_sources, "_invoke_sources_api", _fake_invoke)
        monkeypatch.setattr(
            delete_sources,
            "delete_graphrag_indexes",
            lambda ns: (order.append("drop_indexes"), ["chunk_x"])[1],
        )

        result = delete_sources.handler({"namespaceId": "ns-1"}, None)

        assert order == ["delete_source", "drop_indexes"], "indexes must drop AFTER sources"
        assert result["sourcesDeleted"] == 1
        assert result["graphragIndexesDeleted"] == ["chunk_x"]
