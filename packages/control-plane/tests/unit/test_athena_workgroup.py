# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Athena workgroup lifecycle in namespace create/delete."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX

MODULE_SERVICE = "coa_control_plane.namespace.service"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("NAMESPACES_TABLE", "test-namespaces")
    monkeypatch.setenv("ROLES_TABLE", "test-roles")
    monkeypatch.setenv("RESOURCE_ROLE_MAPPINGS_TABLE", "test-mappings")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ALLOWED_ORIGIN", "*")
    monkeypatch.setenv("RESOURCE_PREFIX", f"{RESOURCE_PREFIX}-dev-")
    monkeypatch.setenv("ATHENA_RESULTS_BUCKET_SSM", "/coa/query/athena-results-bucket")
    monkeypatch.setenv("TAG_PREFIX", "scl")
    monkeypatch.setenv("PROJECT_ACCESS_ROLE_ARN_SSM", "/coa/smus/dz-project-access-role-arn")


class TestAthenaWorkgroupCreation:
    def _make_service(self):
        from coa_common.dao import PaginatedResult
        from coa_common.metadata_store.base import ProjectResult
        from coa_control_plane.namespace.service import NamespaceService

        mock_metadata = MagicMock()
        mock_metadata.create_project.return_value = ProjectResult(project_id="proj-abc", name="test-ns")

        ns_dao = MagicMock()
        ns_dao._table_name = "test-namespaces"
        ns_dao.query.return_value = PaginatedResult(items=[])

        roles_dao = MagicMock()
        roles_dao._table_name = "test-roles"
        roles_dao.query.return_value = PaginatedResult(
            items=[
                {"PK": "NAMESPACE_TEMPLATE", "SK": "ROLE#namespace-owner", "name": "Owner", "scope": "NAMESPACE"},
            ]
        )

        mappings_dao = MagicMock()
        mappings_dao._table_name = "test-role-mappings"

        return NamespaceService(
            namespaces_dao=ns_dao,
            roles_dao=roles_dao,
            mappings_dao=mappings_dao,
            metadata_store=mock_metadata,
        )

    @patch(f"{MODULE_SERVICE}.boto3")
    def test_creates_workgroup_with_correct_name(self, mock_boto3):
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": f"{RESOURCE_PREFIX}-dev-athena-results-123"}}
        mock_athena = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ssm if svc == "ssm" else mock_athena

        svc = self._make_service()
        from coa_control_plane_server.models.create_namespace_request_content import (
            CreateNamespaceRequestContent,
        )

        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        svc.create(req)

        # Workgroup should be created with prefix + namespace UUID
        mock_athena.create_work_group.assert_called_once()
        call_kwargs = mock_athena.create_work_group.call_args.kwargs
        assert call_kwargs["Name"].startswith(f"{RESOURCE_PREFIX}-dev-")
        assert (
            f"{RESOURCE_PREFIX}-dev-athena-results-123"
            in call_kwargs["Configuration"]["ResultConfiguration"]["OutputLocation"]
        )

    @patch(f"{MODULE_SERVICE}.boto3")
    def test_workgroup_stored_in_ddb_record(self, mock_boto3):
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": f"{RESOURCE_PREFIX}-dev-athena-results-123"}}
        mock_athena = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ssm if svc == "ssm" else mock_athena

        svc = self._make_service()
        from coa_control_plane_server.models.create_namespace_request_content import (
            CreateNamespaceRequestContent,
        )

        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        svc.create(req)

        # Check the namespace DDB put includes athenaWorkgroupName
        ns_put_calls = [c for c in svc._ns_dao.put.call_args_list if "athenaWorkgroupName" in c.args[0]]
        assert len(ns_put_calls) == 1
        assert ns_put_calls[0].args[0]["athenaWorkgroupName"].startswith(f"{RESOURCE_PREFIX}-dev-")

    @patch(f"{MODULE_SERVICE}.boto3")
    def test_workgroup_already_exists_is_idempotent(self, mock_boto3):
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": f"{RESOURCE_PREFIX}-dev-athena-results-123"}}
        mock_athena = MagicMock()
        mock_athena.exceptions.InvalidRequestException = type("InvalidRequestException", (Exception,), {})
        mock_athena.create_work_group.side_effect = mock_athena.exceptions.InvalidRequestException(
            "WorkGroup already exists"
        )
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ssm if svc == "ssm" else mock_athena

        svc = self._make_service()
        from coa_control_plane_server.models.create_namespace_request_content import (
            CreateNamespaceRequestContent,
        )

        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        # Should not raise
        svc.create(req)

    @patch(f"{MODULE_SERVICE}.boto3")
    def test_missing_ssm_param_skips_workgroup_creation(self, mock_boto3):
        from botocore.exceptions import ClientError

        mock_ssm = MagicMock()

        # Mock SSM to return role ARN but fail on Athena bucket lookup
        def get_parameter_side_effect(Name):
            if "dz-project-access-role-arn" in Name:
                return {"Parameter": {"Value": "arn:aws:iam::123:role/project-access"}}
            # Athena results bucket SSM param is missing
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "not found"}}, "GetParameter")

        mock_ssm.get_parameter.side_effect = get_parameter_side_effect
        mock_athena = MagicMock()
        mock_boto3.client.side_effect = lambda svc, **kw: mock_ssm if svc == "ssm" else mock_athena

        svc = self._make_service()
        from coa_control_plane_server.models.create_namespace_request_content import (
            CreateNamespaceRequestContent,
        )

        req = CreateNamespaceRequestContent(name="sales", owner="alice@co.com")
        svc.create(req)
        # Athena create_work_group should NOT be called (SSM param missing)
        mock_athena.create_work_group.assert_not_called()


class TestAthenaWorkgroupDeletion:
    def test_delete_athena_workgroup_handles_not_found(self):
        from coa_control_plane.namespace.cleanup import delete_athena_workgroup

        with patch("coa_control_plane.namespace.cleanup.boto3") as mock_boto3:
            mock_athena = MagicMock()
            mock_athena.exceptions.InvalidRequestException = type("InvalidRequestException", (Exception,), {})
            mock_athena.delete_work_group.side_effect = mock_athena.exceptions.InvalidRequestException(
                "WorkGroup not found"
            )
            mock_boto3.client.return_value = mock_athena

            # Should not raise
            delete_athena_workgroup(f"{RESOURCE_PREFIX}-dev-ns-123")

    def test_delete_athena_workgroup_success(self):
        from coa_control_plane.namespace.cleanup import delete_athena_workgroup

        with patch("coa_control_plane.namespace.cleanup.boto3") as mock_boto3:
            mock_athena = MagicMock()
            mock_boto3.client.return_value = mock_athena

            delete_athena_workgroup(f"{RESOURCE_PREFIX}-dev-ns-123")

            mock_athena.delete_work_group.assert_called_once_with(
                WorkGroup=f"{RESOURCE_PREFIX}-dev-ns-123", RecursiveDeleteOption=True
            )
