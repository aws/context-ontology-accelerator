# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for VKG health resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX

MODULE = "coa_control_plane.namespace.vkg_health"


@pytest.fixture(autouse=True)
def reset_client():
    import coa_control_plane.namespace.vkg_health as mod

    mod._ecs = None
    yield
    mod._ecs = None


def _running(running, desired=1, status="ACTIVE", deployments=None):
    svc = {"status": status, "runningCount": running, "desiredCount": desired}
    if deployments is not None:
        svc["deployments"] = deployments
    return {"services": [svc]}


def _tasks(*container_health, task_health=None):
    """Build a describe_tasks response with one task.

    container_health: healthStatus values for the task's containers.
    task_health: optional task-level rollup healthStatus.
    """
    task = {"containers": [{"healthStatus": h} for h in container_health]}
    if task_health is not None:
        task["healthStatus"] = task_health
    return {"tasks": [task]}


def _client_with(describe_services, list_task_arns=None, describe_tasks=None):
    """Wire a MagicMock ECS client for the three calls resolve_vkg_health makes."""
    client = MagicMock()
    client.describe_services.return_value = describe_services
    client.list_tasks.return_value = {"taskArns": list_task_arns or []}
    if describe_tasks is not None:
        client.describe_tasks.return_value = describe_tasks
    return client


@pytest.mark.unit
class TestResolveVkgHealth:
    def test_unknown_when_cluster_not_configured(self, monkeypatch):
        monkeypatch.delenv("VKG_CLUSTER_ARN", raising=False)
        from coa_control_plane.namespace.vkg_health import resolve_vkg_health

        assert resolve_vkg_health("ns-1") == "UNKNOWN"

    def test_unknown_when_namespace_empty(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        from coa_control_plane.namespace.vkg_health import resolve_vkg_health

        assert resolve_vkg_health("") == "UNKNOWN"

    def test_healthy_when_running_and_container_healthy(self, monkeypatch):
        """A running task whose container reports HEALTHY can translate."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(1, 1),
                list_task_arns=["arn:task/1"],
                describe_tasks=_tasks("HEALTHY"),
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            client = mock.return_value
            assert resolve_vkg_health("ns-1") == "HEALTHY"
            client.describe_services.assert_called_once_with(
                cluster="arn:cluster", services=[f"{RESOURCE_PREFIX}-dev-vkg-ns-1"]
            )

    def test_degraded_when_running_but_container_unhealthy(self, monkeypatch):
        """#170: a task running at 1/1 whose container health check is failing
        must NOT report HEALTHY — it cannot answer queries."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(1, 1),
                list_task_arns=["arn:task/1"],
                describe_tasks=_tasks("UNHEALTHY"),
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "DEGRADED"

    def test_degraded_uses_task_level_rollup(self, monkeypatch):
        """The task-level healthStatus rollup is honored when present."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(1, 1),
                list_task_arns=["arn:task/1"],
                describe_tasks=_tasks("UNKNOWN", task_health="UNHEALTHY"),
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "DEGRADED"

    def test_provisioning_when_container_health_unknown(self, monkeypatch):
        """A running task whose container health is still UNKNOWN (start period)
        is treated as still starting up."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(1, 1),
                list_task_arns=["arn:task/1"],
                describe_tasks=_tasks("UNKNOWN"),
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "PROVISIONING"

    def test_degraded_when_running_but_task_lookup_empty(self, monkeypatch):
        """Running >= 1 but no running task returned and not rolling out: we
        cannot confirm health, so report DEGRADED rather than a false HEALTHY."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(1, 1, deployments=[{"status": "PRIMARY", "rolloutState": "COMPLETED"}]),
                list_task_arns=[],  # no task arns -> _container_health returns None
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "DEGRADED"

    def test_healthy_when_multiple_tasks_and_at_least_one_healthy(self, monkeypatch):
        """Auto-scaled deployment: 2 tasks, one HEALTHY + one UNHEALTHY. The

        priority is HEALTHY > UNHEALTHY, so the service can still answer queries
        and must report HEALTHY, not DEGRADED (a false alarm during a rolling
        replacement or a single flapping task).
        """
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(2, 2, deployments=[{"status": "PRIMARY", "rolloutState": "COMPLETED"}]),
                list_task_arns=["arn:task/1", "arn:task/2"],
                describe_tasks={
                    "tasks": [
                        {"containers": [{"healthStatus": "UNHEALTHY"}]},
                        {"containers": [{"healthStatus": "HEALTHY"}]},
                    ]
                },
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "HEALTHY"

    def test_degraded_when_multiple_tasks_and_none_healthy(self, monkeypatch):
        """2 tasks, one UNHEALTHY + one still UNKNOWN and none HEALTHY: no task

        can serve queries, so UNHEALTHY wins over UNKNOWN and the service reports
        DEGRADED.
        """
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(2, 2, deployments=[{"status": "PRIMARY", "rolloutState": "COMPLETED"}]),
                list_task_arns=["arn:task/1", "arn:task/2"],
                describe_tasks={
                    "tasks": [
                        {"containers": [{"healthStatus": "UNKNOWN"}]},
                        {"containers": [{"healthStatus": "UNHEALTHY"}]},
                    ]
                },
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "DEGRADED"

    def test_provisioning_when_running_unconfirmed_but_rolling_out(self, monkeypatch):
        """Running but container health unreadable while a deployment rolls out:
        starting up wins over DEGRADED."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            client = _client_with(
                _running(1, 1, deployments=[{"status": "PRIMARY", "rolloutState": "IN_PROGRESS"}]),
                list_task_arns=["arn:task/1"],
                describe_tasks={"tasks": [{"containers": [{}]}]},  # no healthStatus
            )
            mock.return_value = client

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "PROVISIONING"

    def test_healthy_survives_describe_tasks_error_via_no_signal(self, monkeypatch):
        """If describe_tasks raises, _container_health returns None; with no
        rollout in progress and a running task, we report DEGRADED (not a false
        HEALTHY, and not UNAVAILABLE which would mean 'no task')."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            client = _client_with(_running(1, 1), list_task_arns=["arn:task/1"])
            client.describe_tasks.side_effect = RuntimeError("throttled")
            mock.return_value = client

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "DEGRADED"

    def test_unavailable_when_no_running_tasks(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(_running(0, 1))

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "UNAVAILABLE"

    def test_provisioning_when_starting_up(self, monkeypatch):
        """A service that is not yet serving but whose primary deployment is rolling
        out is starting up, not broken."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(0, 1, deployments=[{"status": "PRIMARY", "rolloutState": "IN_PROGRESS"}])
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "PROVISIONING"

    def test_unavailable_when_not_running_and_rollout_completed(self, monkeypatch):
        """A service that finished rolling out but still has no running tasks is down,
        not merely starting — so it must be UNAVAILABLE, not PROVISIONING."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(0, 1, deployments=[{"status": "PRIMARY", "rolloutState": "COMPLETED"}])
            )

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "UNAVAILABLE"

    def test_unknown_when_service_missing(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            # A non-existent service comes back under `failures`, not `services`.
            mock.return_value = _client_with({"services": [], "failures": [{"reason": "MISSING"}]})

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "UNKNOWN"

    def test_inactive_service_is_unknown(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(_running(1, 1, status="INACTIVE"))

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "UNKNOWN"

    def test_unknown_on_ecs_error(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            client = MagicMock()
            mock.return_value = client
            client.describe_services.side_effect = RuntimeError("ecs boom")

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "UNKNOWN"

    def test_prefix_trailing_dash_normalized(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", f"{RESOURCE_PREFIX}-dev-")
        with patch(f"{MODULE}._ecs_client") as mock:
            client = _client_with(
                _running(1, 1),
                list_task_arns=["arn:task/1"],
                describe_tasks=_tasks("HEALTHY"),
            )
            mock.return_value = client

            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            resolve_vkg_health("ns-1")
            client.describe_services.assert_called_once_with(
                cluster="arn:cluster", services=[f"{RESOURCE_PREFIX}-dev-vkg-ns-1"]
            )


@pytest.mark.unit
class TestResolveVkgHealthWithReason:
    """#149 Fix 4: GetNamespace surfaces a coarse, always-available reason for a
    non-HEALTHY status, derived from ECS signals (no extra network hop)."""

    def test_healthy_has_no_reason(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(1, 1),
                list_task_arns=["arn:task/1"],
                describe_tasks=_tasks("HEALTHY"),
            )
            from coa_control_plane.namespace.vkg_health import resolve_vkg_health_with_reason

            status, reason = resolve_vkg_health_with_reason("ns-1")
            assert status == "HEALTHY"
            assert reason is None

    def test_degraded_reason_points_to_container_health(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(
                _running(1, 1),
                list_task_arns=["arn:task/1"],
                describe_tasks=_tasks("UNHEALTHY"),
            )
            from coa_control_plane.namespace.vkg_health import resolve_vkg_health_with_reason

            status, reason = resolve_vkg_health_with_reason("ns-1")
            assert status == "DEGRADED"
            assert reason is not None
            assert "health check" in reason.lower()

    def test_unavailable_reason_says_no_task(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(_running(0, 1))
            from coa_control_plane.namespace.vkg_health import resolve_vkg_health_with_reason

            status, reason = resolve_vkg_health_with_reason("ns-1")
            assert status == "UNAVAILABLE"
            assert reason is not None and "no running task" in reason.lower()

    def test_unknown_has_reason(self, monkeypatch):
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            client = MagicMock()
            client.describe_services.side_effect = RuntimeError("boom")
            mock.return_value = client
            from coa_control_plane.namespace.vkg_health import resolve_vkg_health_with_reason

            status, reason = resolve_vkg_health_with_reason("ns-1")
            assert status == "UNKNOWN"
            assert reason is not None

    def test_back_compat_wrapper_returns_status_string(self, monkeypatch):
        """resolve_vkg_health still returns the bare status string."""
        monkeypatch.setenv("VKG_CLUSTER_ARN", "arn:cluster")
        monkeypatch.setenv("RESOURCE_PREFIX", "coa-dev")
        with patch(f"{MODULE}._ecs_client") as mock:
            mock.return_value = _client_with(_running(0, 1))
            from coa_control_plane.namespace.vkg_health import resolve_vkg_health

            assert resolve_vkg_health("ns-1") == "UNAVAILABLE"
