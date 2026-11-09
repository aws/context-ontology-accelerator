# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for deregistering a deleted namespace's VKG task definitions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

ENV = {
    "AWS_REGION": "us-east-1",
    "RESOURCE_PREFIX": "coa-dev",
    "VKG_CLUSTER_ARN": "arn:aws:ecs:us-east-1:111:cluster/vkg",
    "CLOUD_MAP_NAMESPACE_ID": "ns-abc123",
    "NAMESPACES_TABLE": "test-ns-table",
}
TD = "arn:aws:ecs:us-east-1:111:task-definition/"
FAMILY = "coa-dev-vkg-test-ns"


def _clients(mock_boto3, revisions):
    mock_ecs = MagicMock()
    mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
    mock_ecs.get_paginator.return_value.paginate.return_value = [{"taskDefinitionArns": [TD + r for r in revisions]}]
    mock_sd = MagicMock()
    mock_sd.get_paginator.return_value.paginate.return_value = [{"Services": []}]
    mock_boto3.client.side_effect = lambda service, **kw: mock_ecs if service == "ecs" else mock_sd
    return mock_ecs


@pytest.mark.unit
@patch.dict("os.environ", ENV)
@patch("coa_control_plane.namespace.cleanup.boto3")
def test_deregisters_task_definitions_when_the_service_is_not_found(mock_boto3):
    """A namespace whose service is already gone still loses its leftover revisions."""
    mock_ecs = _clients(mock_boto3, [f"{FAMILY}:1", f"{FAMILY}:2", f"{FAMILY}2:1"])
    mock_ecs.delete_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")

    from coa_control_plane.namespace.cleanup import teardown_vkg_service

    teardown_vkg_service("test-ns")

    deregistered = [c.kwargs["taskDefinition"] for c in mock_ecs.deregister_task_definition.call_args_list]
    assert deregistered == [f"{TD}{FAMILY}:1", f"{TD}{FAMILY}:2"]


@pytest.mark.unit
@patch.dict("os.environ", ENV)
@patch("coa_control_plane.namespace.cleanup.boto3")
def test_a_failed_deregistration_logs_the_family_and_how_far_it_got(mock_boto3):
    mock_ecs = _clients(mock_boto3, [f"{FAMILY}:1", f"{FAMILY}:2", f"{FAMILY}:3"])
    mock_ecs.deregister_task_definition.side_effect = [{}, RuntimeError("throttled"), {}]

    from coa_control_plane.namespace import cleanup

    with patch.object(cleanup, "logger") as logger:
        cleanup.teardown_vkg_service("test-ns")  # the deregistration is best effort: no raise

    logger.error.assert_called_once()
    event, fields = logger.error.call_args.args[0], logger.error.call_args.kwargs
    assert event == "vkg_task_definitions_deregister_failed"
    assert fields["family"] == FAMILY
    assert fields["deregistered"] == 1
    assert fields["listed"] == 3
