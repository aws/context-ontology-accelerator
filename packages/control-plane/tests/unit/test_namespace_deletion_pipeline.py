# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the namespace deletion Step Functions pipeline Lambda tasks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from coa_common.constants import RESOURCE_PREFIX

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("SOURCES_API_FN_NAME", f"{RESOURCE_PREFIX}-dev-sources-api")
    monkeypatch.setenv("METRIC_API_FN_NAME", f"{RESOURCE_PREFIX}-dev-metric-api")
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")


@pytest.fixture(autouse=True)
def reset_singletons():
    yield
    import coa_control_plane.namespace.deletion_pipeline.delete_metrics as dm
    import coa_control_plane.namespace.deletion_pipeline.delete_sources as ds
    import coa_control_plane.namespace.deletion_pipeline.mark_failed as mf

    ds._lambda_client = None
    dm._lambda_client = None
    mf._ns_dao = None


def _lambda_payload(status_code: int, body: dict) -> dict:
    return {
        "Payload": MagicMock(read=lambda: json.dumps({"statusCode": status_code, "body": json.dumps(body)}).encode())
    }


class TestDeleteSources:
    def test_deletes_all_sources_across_pages(self):
        from coa_control_plane.namespace.deletion_pipeline import delete_sources

        mock_client = MagicMock()
        mock_client.invoke.side_effect = [
            _lambda_payload(200, {"items": [{"sourceId": "src-1"}], "nextToken": "page2"}),
            _lambda_payload(200, {"sourceId": "src-1"}),
            _lambda_payload(200, {"items": [{"sourceId": "src-2"}]}),
            _lambda_payload(200, {"sourceId": "src-2"}),
        ]

        with patch.object(delete_sources, "_get_lambda_client", return_value=mock_client):
            result = delete_sources.handler({"namespaceId": "ns-123"}, None)

        assert result["sourcesDeleted"] == 2
        assert mock_client.invoke.call_count == 4

    def test_no_sources_returns_zero(self):
        from coa_control_plane.namespace.deletion_pipeline import delete_sources

        mock_client = MagicMock()
        mock_client.invoke.return_value = _lambda_payload(200, {"items": []})

        with patch.object(delete_sources, "_get_lambda_client", return_value=mock_client):
            result = delete_sources.handler({"namespaceId": "ns-123"}, None)

        assert result["sourcesDeleted"] == 0

    def test_delete_404_is_treated_as_success(self):
        """A source deleted by a concurrent request (race) must not fail the step."""
        from coa_control_plane.namespace.deletion_pipeline import delete_sources

        mock_client = MagicMock()
        mock_client.invoke.side_effect = [
            _lambda_payload(200, {"items": [{"sourceId": "src-1"}]}),
            _lambda_payload(404, {"error": "not found"}),
        ]

        with patch.object(delete_sources, "_get_lambda_client", return_value=mock_client):
            result = delete_sources.handler({"namespaceId": "ns-123"}, None)

        assert result["sourcesDeleted"] == 1

    def test_delete_failure_raises(self):
        from coa_control_plane.namespace.deletion_pipeline import delete_sources

        mock_client = MagicMock()
        mock_client.invoke.side_effect = [
            _lambda_payload(200, {"items": [{"sourceId": "src-1"}]}),
            _lambda_payload(500, {"error": "boom"}),
        ]

        with patch.object(delete_sources, "_get_lambda_client", return_value=mock_client), pytest.raises(RuntimeError):
            delete_sources.handler({"namespaceId": "ns-123"}, None)

    def test_lambda_function_error_raises(self):
        from coa_control_plane.namespace.deletion_pipeline import delete_sources

        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            "Payload": MagicMock(read=lambda: json.dumps({"errorMessage": "boom"}).encode()),
            "FunctionError": "Unhandled",
        }

        with patch.object(delete_sources, "_get_lambda_client", return_value=mock_client), pytest.raises(RuntimeError):
            delete_sources.handler({"namespaceId": "ns-123"}, None)


class TestDeleteMetrics:
    def test_bulk_deletes_all_with_delete_all_flag(self):
        """Uses deleteAll: true for authoritative namespace-scoped deletion."""
        from coa_control_plane.namespace.deletion_pipeline import delete_metrics

        mock_client = MagicMock()
        mock_client.invoke.return_value = _lambda_payload(200, {"deleted": 5})

        with patch.object(delete_metrics, "_get_lambda_client", return_value=mock_client):
            result = delete_metrics.handler({"namespaceId": "ns-123"}, None)

        assert result == {"metricsDeleted": 5}
        assert mock_client.invoke.call_count == 1  # single bulk-delete call, no list

        delete_event = json.loads(mock_client.invoke.call_args.kwargs["Payload"])
        assert delete_event["resource"] == "/namespaces/{namespaceId}/bulk-delete-metrics"
        body = json.loads(delete_event["body"])
        # Uses deleteAll: true, NOT explicit names list
        assert body == {"deleteAll": True}

    def test_returns_zero_when_no_metrics_deleted(self):
        """Idempotent: works even when namespace has no metrics."""
        from coa_control_plane.namespace.deletion_pipeline import delete_metrics

        mock_client = MagicMock()
        mock_client.invoke.return_value = _lambda_payload(200, {"deleted": 0})

        with patch.object(delete_metrics, "_get_lambda_client", return_value=mock_client):
            result = delete_metrics.handler({"namespaceId": "empty-ns"}, None)

        assert result == {"metricsDeleted": 0}

    def test_delete_failure_raises(self):
        from coa_control_plane.namespace.deletion_pipeline import delete_metrics

        mock_client = MagicMock()
        mock_client.invoke.return_value = _lambda_payload(500, {"error": "boom"})

        with patch.object(delete_metrics, "_get_lambda_client", return_value=mock_client), pytest.raises(RuntimeError):
            delete_metrics.handler({"namespaceId": "ns-123"}, None)


class TestDeleteOntology:
    def test_calls_both_cleanup_functions(self):
        from coa_control_plane.namespace.deletion_pipeline import delete_ontology

        with (
            patch.object(delete_ontology, "delete_all_ontologies") as mock_ont,
            patch.object(delete_ontology, "delete_ontology_artifacts") as mock_s3,
        ):
            result = delete_ontology.handler({"namespaceId": "ns-123"}, None)

        mock_ont.assert_called_once_with("ns-123")
        mock_s3.assert_called_once_with("ns-123")
        assert result == {"namespaceId": "ns-123"}


class TestDeletePlatform:
    def test_calls_all_platform_cleanup_steps(self):
        from coa_control_plane.namespace.deletion_pipeline import delete_platform

        with (
            patch.object(delete_platform, "teardown_vkg_service") as mock_vkg,
            patch.object(delete_platform, "delete_athena_workgroup") as mock_athena,
            patch.object(delete_platform, "delete_smu_project") as mock_smu,
            patch.object(delete_platform, "delete_role_mappings") as mock_rrm,
            patch.object(delete_platform, "delete_roles") as mock_roles,
        ):
            result = delete_platform.handler(
                {"namespaceId": "ns-123", "athenaWorkgroupName": "wg-1", "dataZoneProjectId": "proj-1"}, None
            )

        mock_vkg.assert_called_once_with("ns-123")
        mock_athena.assert_called_once_with("wg-1")
        mock_smu.assert_called_once_with("ns-123", {"dataZoneProjectId": "proj-1"})
        mock_rrm.assert_called_once_with("ns-123")
        mock_roles.assert_called_once_with("ns-123")
        assert result == {"namespaceId": "ns-123"}

    def test_delete_platform_vkg_delete_denied_fails_step(self, monkeypatch):
        from coa_control_plane.namespace import cleanup
        from coa_control_plane.namespace.deletion_pipeline import delete_platform

        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:aws:ecs:us-east-1:111111111111:cluster/vkg")
        ecs = boto3.session.Session(
            aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1"
        ).client("ecs")
        monkeypatch.setattr(cleanup, "boto3", SimpleNamespace(client=lambda name, **_: ecs))

        with (
            Stubber(ecs) as ecs_stub,
            patch.object(delete_platform, "delete_athena_workgroup"),
            patch.object(delete_platform, "delete_smu_project"),
            patch.object(delete_platform, "delete_role_mappings"),
            patch.object(delete_platform, "delete_roles"),
            pytest.raises(ClientError),
        ):
            ecs_stub.add_client_error("delete_service", service_error_code="AccessDeniedException")
            delete_platform.handler({"namespaceId": "ns-123"}, None)


class TestFinalize:
    def test_deletes_namespace_records_and_returns_deleted(self):
        from coa_control_plane.namespace.deletion_pipeline import finalize

        with patch.object(finalize, "delete_namespace_records") as mock_delete:
            result = finalize.handler({"namespaceId": "ns-123", "namespaceName": "sales"}, None)

        mock_delete.assert_called_once_with("ns-123", "sales")
        assert result == {"namespaceId": "ns-123", "status": "DELETED"}


class TestMarkFailed:
    def test_sets_delete_failed_status(self):
        from coa_control_plane.namespace.deletion_pipeline import mark_failed

        mock_dao = MagicMock()

        with patch.object(mark_failed, "_get_ns_dao", return_value=mock_dao):
            result = mark_failed.handler({"namespaceId": "ns-123", "error": {"Error": "States.TaskFailed"}}, None)

        mock_dao.update.assert_called_once_with(
            key={"PK": "NS#ns-123", "SK": "METADATA"}, update_fields={"status": "DELETE_FAILED"}
        )
        assert result == {"namespaceId": "ns-123", "status": "DELETE_FAILED"}
