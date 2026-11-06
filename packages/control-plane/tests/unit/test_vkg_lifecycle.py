# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for VKG teardown on namespace delete.

Tests the actual production functions (cleanup.teardown_vkg_service, invoked
by the deletion pipeline's delete_platform step) with boto3 mocked at the
module level. Provisioning tests live in
infra/test/lambdas/test_vkg_reload_handler.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from coa_common.constants import RESOURCE_PREFIX
from coa_control_plane.namespace import cleanup


@pytest.mark.unit
class TestTeardownVkgService:
    """Tests for teardown_vkg_service in cleanup.py."""

    ENV = {
        "AWS_REGION": "us-east-1",
        "RESOURCE_PREFIX": "coa-dev",
        "VKG_CLUSTER_ARN": "arn:aws:ecs:us-east-1:111:cluster/vkg",
        "CLOUD_MAP_NAMESPACE_ID": "ns-abc123",
        "NAMESPACES_TABLE": "test-ns-table",
    }

    @patch.dict("os.environ", ENV)
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_deletes_service_and_cloud_map(self, mock_boto3):
        mock_ecs = MagicMock()
        mock_sd = MagicMock()
        mock_ecs.delete_service.return_value = {"service": {"status": "DRAINING"}}
        mock_sd.exceptions.ResourceInUse = type("ResourceInUse", (Exception,), {})
        mock_sd.exceptions.ServiceNotFound = type("ServiceNotFound", (Exception,), {})
        svc_paginator = MagicMock()
        svc_paginator.paginate.return_value = [{"Services": [{"Name": "vkg-test-ns", "Id": "svc-id-1"}]}]
        inst_paginator = MagicMock()
        inst_paginator.paginate.return_value = [{"Instances": [{"Id": "inst-1"}]}]
        mock_sd.get_paginator.side_effect = lambda name: svc_paginator if name == "list_services" else inst_paginator

        def client_fn(service, **kw):
            return mock_ecs if service == "ecs" else mock_sd

        mock_boto3.client.side_effect = client_fn

        from coa_control_plane.namespace.cleanup import (
            teardown_vkg_service,
        )

        teardown_vkg_service("test-ns")

        mock_ecs.delete_service.assert_called_once_with(
            cluster="arn:aws:ecs:us-east-1:111:cluster/vkg",
            service=f"{RESOURCE_PREFIX}-dev-vkg-test-ns",
            force=True,
        )
        mock_sd.deregister_instance.assert_called_once_with(ServiceId="svc-id-1", InstanceId="inst-1")
        mock_sd.delete_service.assert_called_once_with(Id="svc-id-1")

    @patch.dict("os.environ", ENV)
    @patch("coa_control_plane.namespace.cleanup.time")
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_retries_cloud_map_delete_on_resource_in_use(self, mock_boto3, mock_time):
        mock_ecs = MagicMock()
        mock_sd = MagicMock()
        mock_ecs.delete_service.return_value = {"service": {"status": "DRAINING"}}
        mock_sd.exceptions.ResourceInUse = type("ResourceInUse", (Exception,), {})
        mock_sd.exceptions.ServiceNotFound = type("ServiceNotFound", (Exception,), {})
        svc_paginator = MagicMock()
        svc_paginator.paginate.return_value = [{"Services": [{"Name": "vkg-test-ns", "Id": "svc-id-1"}]}]
        inst_paginator = MagicMock()
        inst_paginator.paginate.return_value = [{"Instances": []}]
        mock_sd.get_paginator.side_effect = lambda name: svc_paginator if name == "list_services" else inst_paginator
        mock_sd.delete_service.side_effect = [
            mock_sd.exceptions.ResourceInUse("in use"),
            mock_sd.exceptions.ResourceInUse("in use"),
            None,
        ]

        def client_fn(service, **kw):
            return mock_ecs if service == "ecs" else mock_sd

        mock_boto3.client.side_effect = client_fn

        from coa_control_plane.namespace.cleanup import (
            teardown_vkg_service,
        )

        teardown_vkg_service("test-ns")

        assert mock_sd.delete_service.call_count == 3
        assert mock_time.sleep.call_count == 2

    @patch.dict("os.environ", ENV)
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_handles_service_not_found(self, mock_boto3):
        mock_ecs = MagicMock()
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.delete_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")

        mock_sd = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Services": []}]
        mock_sd.get_paginator.return_value = paginator

        def client_fn(service, **kw):
            return mock_ecs if service == "ecs" else mock_sd

        mock_boto3.client.side_effect = client_fn

        from coa_control_plane.namespace.cleanup import (
            teardown_vkg_service,
        )

        # Should not raise
        teardown_vkg_service("nonexistent-ns")

    @patch.dict("os.environ", {"AWS_REGION": "us-east-1", "VKG_CLUSTER_ARN": "", "RESOURCE_PREFIX": ""})
    @patch("coa_control_plane.namespace.cleanup.boto3")
    def test_skips_when_cluster_not_configured(self, mock_boto3):
        from coa_control_plane.namespace.cleanup import (
            teardown_vkg_service,
        )

        teardown_vkg_service("skip-ns")
        mock_boto3.client.assert_not_called()


_CLUSTER_ARN = "arn:aws:ecs:us-east-1:111111111111:cluster/vkg"
_ECS_SERVICE = "coa-dev-vkg-ns1"
_CLOUD_MAP_NS = "ns-abc123"
_CLOUD_MAP_SERVICE_ID = "srv-1"


@pytest.fixture
def aws_stubs(monkeypatch):
    """Real botocore clients behind Stubbers, handed to cleanup in place of boto3.client."""
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    for key, value in {
        "AWS_REGION": "us-east-1",
        "RESOURCE_PREFIX": "coa-dev",
        "VKG_CLUSTER_ARN": _CLUSTER_ARN,
        "CLOUD_MAP_NAMESPACE_ID": _CLOUD_MAP_NS,
    }.items():
        monkeypatch.setenv(key, value)
    session = boto3.session.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1"
    )
    clients = {"ecs": session.client("ecs"), "servicediscovery": session.client("servicediscovery")}
    stubs = {name: Stubber(client) for name, client in clients.items()}
    monkeypatch.setattr(cleanup, "boto3", SimpleNamespace(client=lambda name, **_: clients[name]))
    monkeypatch.setattr(cleanup.time, "sleep", lambda _seconds: None)
    for stub in stubs.values():
        stub.activate()
    yield SimpleNamespace(ecs=stubs["ecs"], sd=stubs["servicediscovery"])
    for stub in stubs.values():
        stub.assert_no_pending_responses()
        stub.deactivate()


def _ecs_delete_accepted(ecs: Stubber, status: str = "DRAINING") -> None:
    ecs.add_response(
        "delete_service",
        {"service": {"serviceName": _ECS_SERVICE, "status": status}},
        {"cluster": _CLUSTER_ARN, "service": _ECS_SERVICE, "force": True},
    )


def _cloud_map_lists_service(sd: Stubber, instance_ids: list[str]) -> None:
    sd.add_response(
        "list_services",
        {"Services": [{"Id": _CLOUD_MAP_SERVICE_ID, "Name": "vkg-ns1"}]},
        {"Filters": [{"Name": "NAMESPACE_ID", "Values": [_CLOUD_MAP_NS], "Condition": "EQ"}]},
    )
    sd.add_response(
        "list_instances",
        {"Instances": [{"Id": instance_id} for instance_id in instance_ids]},
        {"ServiceId": _CLOUD_MAP_SERVICE_ID},
    )


@pytest.mark.unit
class TestTeardownVkgServiceSurfacesFailures:
    """A failed or unconfirmed teardown must raise so DeletePlatform fails retryably."""

    @pytest.mark.parametrize("error_code", ["AccessDeniedException", "ThrottlingException"])
    def test_teardown_vkg_service_ecs_delete_error_raises(self, aws_stubs, error_code):
        aws_stubs.ecs.add_client_error("delete_service", service_error_code=error_code)

        with pytest.raises(ClientError) as exc_info:
            cleanup.teardown_vkg_service("ns1")

        assert exc_info.value.response["Error"]["Code"] == error_code

    def test_teardown_vkg_service_still_active_after_delete_raises(self, aws_stubs):
        _ecs_delete_accepted(aws_stubs.ecs, "ACTIVE")

        with pytest.raises(RuntimeError, match="ACTIVE"):
            cleanup.teardown_vkg_service("ns1")

    def test_teardown_vkg_service_cloud_map_delete_error_raises(self, aws_stubs):
        _ecs_delete_accepted(aws_stubs.ecs)
        _cloud_map_lists_service(aws_stubs.sd, [])
        aws_stubs.sd.add_client_error("delete_service", service_error_code="AccessDeniedException")

        with pytest.raises(ClientError):
            cleanup.teardown_vkg_service("ns1")

    def test_teardown_vkg_service_cloud_map_in_use_after_retries_raises(self, aws_stubs):
        _ecs_delete_accepted(aws_stubs.ecs)
        _cloud_map_lists_service(aws_stubs.sd, [])
        for _ in range(4):
            aws_stubs.sd.add_client_error("delete_service", service_error_code="ResourceInUse")

        with pytest.raises(ClientError) as exc_info:
            cleanup.teardown_vkg_service("ns1")

        assert exc_info.value.response["Error"]["Code"] == "ResourceInUse"


@pytest.mark.unit
class TestTeardownVkgServiceIdempotentRedelete:
    """A re-run after a partial or completed delete must succeed."""

    @pytest.mark.parametrize("status", ["DRAINING", "INACTIVE"])
    def test_teardown_vkg_service_delete_accepted_and_not_active_succeeds(self, aws_stubs, status):
        _ecs_delete_accepted(aws_stubs.ecs, status)
        _cloud_map_lists_service(aws_stubs.sd, ["inst-1"])
        aws_stubs.sd.add_response(
            "deregister_instance", {"OperationId": "op-1"}, {"ServiceId": _CLOUD_MAP_SERVICE_ID, "InstanceId": "inst-1"}
        )
        aws_stubs.sd.add_response("delete_service", {}, {"Id": _CLOUD_MAP_SERVICE_ID})

        cleanup.teardown_vkg_service("ns1")

    def test_teardown_vkg_service_already_inactive_succeeds(self, aws_stubs):
        aws_stubs.ecs.add_client_error("delete_service", service_error_code="ServiceNotActiveException")
        _cloud_map_lists_service(aws_stubs.sd, [])
        aws_stubs.sd.add_response("delete_service", {}, {"Id": _CLOUD_MAP_SERVICE_ID})

        cleanup.teardown_vkg_service("ns1")

    def test_teardown_vkg_service_everything_already_gone_succeeds(self, aws_stubs):
        aws_stubs.ecs.add_client_error("delete_service", service_error_code="ServiceNotFoundException")
        aws_stubs.sd.add_response("list_services", {"Services": []})

        cleanup.teardown_vkg_service("ns1")

    def test_teardown_vkg_service_cluster_already_deleted_counts_as_gone(self, aws_stubs):
        # A deleted cluster has no services left, so a namespace delete after a
        # cluster teardown must not end in DELETE_FAILED on every retry.
        aws_stubs.ecs.add_client_error("delete_service", service_error_code="ClusterNotFoundException")
        _cloud_map_lists_service(aws_stubs.sd, [])
        aws_stubs.sd.add_response("delete_service", {}, {"Id": _CLOUD_MAP_SERVICE_ID})

        cleanup.teardown_vkg_service("ns1")

    def test_teardown_vkg_service_deregister_racing_ecs_succeeds(self, aws_stubs):
        _ecs_delete_accepted(aws_stubs.ecs)
        _cloud_map_lists_service(aws_stubs.sd, ["inst-1"])
        aws_stubs.sd.add_client_error("deregister_instance", service_error_code="DuplicateRequest")
        aws_stubs.sd.add_response("delete_service", {}, {"Id": _CLOUD_MAP_SERVICE_ID})

        cleanup.teardown_vkg_service("ns1")

    def test_teardown_vkg_service_cloud_map_removed_concurrently_succeeds(self, aws_stubs):
        _ecs_delete_accepted(aws_stubs.ecs)
        _cloud_map_lists_service(aws_stubs.sd, ["inst-1"])
        aws_stubs.sd.add_client_error("deregister_instance", service_error_code="InstanceNotFound")
        aws_stubs.sd.add_client_error("delete_service", service_error_code="ServiceNotFound")

        cleanup.teardown_vkg_service("ns1")
