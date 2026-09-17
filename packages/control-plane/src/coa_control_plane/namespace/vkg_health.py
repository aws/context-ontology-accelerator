# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve a namespace's VKG service health from ECS service + task state.

The health signal must reflect whether the container can actually answer
queries, not merely whether a task is running. ECS keeps ``runningCount`` at 1
for a task whose container health check is failing (it only replaces the task
when the deployment circuit breaker trips), so ``runningCount`` alone reports a
degraded container as HEALTHY (#170). We therefore read the container's ECS
health check status (which runs the functional ``/health`` probe — see
``vkg-stack.ts`` / the reload Lambda health check) via ``describe_tasks``.

State machine:

- ``HEALTHY``      — a running task whose container ``healthStatus`` is HEALTHY (can translate).
- ``DEGRADED``     — a running task whose container ``healthStatus`` is UNHEALTHY (up, cannot
  translate — e.g. published R2RML/schema will not load). Distinct from UNAVAILABLE.
- ``PROVISIONING`` — not yet serving (running < desired) with an IN_PROGRESS primary deployment,
  OR a running task whose container health is still UNKNOWN during its start period.
- ``UNAVAILABLE``  — the service exists but no task is running and it is not rolling out.
- ``UNKNOWN``      — no service / missing config / any API error.

Read on demand from GetNamespace; any failure degrades to UNKNOWN.
"""

from __future__ import annotations

import os

import boto3
import structlog
from coa_control_plane_server.models import VkgHealthStatus

logger = structlog.get_logger(__name__)

_ecs = None

# The VKG task definition has a single application container. Its ECS health
# check runs `curl -sf localhost:8080/health` (the functional reformulation
# probe), so its healthStatus is the authoritative "can translate" signal.


def _ecs_client():
    global _ecs
    if _ecs is None:
        _ecs = boto3.client("ecs", region_name=os.environ.get("AWS_REGION"))
    return _ecs


def _service_name(namespace_id: str) -> str:
    # Mirrors the reload Lambda's name; rstrip guards a trailing-dash prefix.
    prefix = os.environ.get("RESOURCE_PREFIX", "").rstrip("-")
    return f"{prefix}-vkg-{namespace_id}"


def _container_health(cluster: str, service_name: str) -> str | None:
    """Return the container health of the service's running task(s).

    Returns ``"HEALTHY"`` if any running task's container is HEALTHY,
    ``"UNHEALTHY"`` if running task(s) exist but none are HEALTHY and at least
    one is UNHEALTHY, ``"UNKNOWN"`` if running task(s) exist but their health is
    still UNKNOWN (start period), or ``None`` when no running task is found or
    the task lookup fails (caller falls back to the service-count logic).
    """
    try:
        listed = _ecs_client().list_tasks(cluster=cluster, serviceName=service_name, desiredStatus="RUNNING")
        task_arns = listed.get("taskArns", [])
        if not task_arns:
            return None
        described = _ecs_client().describe_tasks(cluster=cluster, tasks=task_arns)
    except Exception as e:
        logger.warning("vkg_health_describe_tasks_failed", service=service_name, error=str(e))
        return None

    statuses: list[str] = []
    for task in described.get("tasks", []):
        # Prefer the task-level rollup; fall back to the container's own status.
        task_health = task.get("healthStatus")
        if task_health and task_health != "UNKNOWN":
            statuses.append(task_health)
            continue
        for container in task.get("containers", []):
            cs = container.get("healthStatus")
            if cs:
                statuses.append(cs)
    if not statuses:
        return None
    if "HEALTHY" in statuses:
        return "HEALTHY"
    if "UNHEALTHY" in statuses:
        return "UNHEALTHY"
    return "UNKNOWN"


def resolve_vkg_health(namespace_id: str) -> str:
    """Return a VkgHealthStatus value for the namespace's VKG service.

    Convenience wrapper around :func:`resolve_vkg_health_with_reason` for the
    callers that only need the status enum and not the human-readable reason
    (e.g. status-only checks and the existing test surface). The GET namespace
    handler uses :func:`resolve_vkg_health_with_reason` directly and surfaces
    both the status and the reason.
    """
    return resolve_vkg_health_with_reason(namespace_id)[0]


def _reason_for(status: str) -> str | None:
    """Coarse, always-available reason for a non-HEALTHY status.

    Derived purely from ECS signals the control plane already holds — no extra
    network hop to the container. The precise container-side cause (e.g. R2RML
    failed to reformulate) is surfaced in the VKG /health 503 body + CloudWatch.
    """
    return {
        VkgHealthStatus.HEALTHY.value: None,
        VkgHealthStatus.DEGRADED.value: (
            "VKG task is running but its container health check is failing — "
            "it cannot answer queries (check the VKG service /health and its logs)"
        ),
        VkgHealthStatus.UNAVAILABLE.value: "VKG service has no running task",
        VkgHealthStatus.PROVISIONING.value: "VKG service is starting up",
        VkgHealthStatus.UNKNOWN.value: "VKG service state could not be determined",
    }.get(status)


def resolve_vkg_health_with_reason(namespace_id: str) -> tuple[str, str | None]:
    """Return ``(status, reason)`` for the namespace's VKG service.

    ``reason`` is None when HEALTHY, else a coarse human-readable explanation.
    """
    status = _resolve_status(namespace_id)
    return status, _reason_for(status)


def _resolve_status(namespace_id: str) -> str:
    """Return a VkgHealthStatus value for the namespace's VKG service."""
    cluster = os.environ.get("VKG_CLUSTER_ARN")
    if not cluster or not namespace_id:
        return VkgHealthStatus.UNKNOWN.value

    service_name = _service_name(namespace_id)
    try:
        resp = _ecs_client().describe_services(cluster=cluster, services=[service_name])
    except Exception as e:
        logger.warning("vkg_health_resolve_failed", namespace_id=namespace_id, error=str(e))
        return VkgHealthStatus.UNKNOWN.value

    # A missing service comes back under `failures`, not `services`.
    active = [s for s in resp.get("services", []) if s.get("status") == "ACTIVE"]
    if not active:
        return VkgHealthStatus.UNKNOWN.value

    svc = active[0]
    running = svc.get("runningCount", 0)
    desired = svc.get("desiredCount", 0)

    if running >= 1 and running >= desired:
        # A task is running, but "running" does NOT mean "can translate" — read
        # the container's functional-probe health before trusting it (#170).
        container_health = _container_health(cluster, service_name)
        if container_health == "HEALTHY":
            return VkgHealthStatus.HEALTHY.value
        if container_health == "UNHEALTHY":
            # Up but failing its /health probe — cannot answer queries.
            return VkgHealthStatus.DEGRADED.value
        if container_health == "UNKNOWN":
            # Health check still within its start period — treat as starting up.
            return VkgHealthStatus.PROVISIONING.value
        # Could not read task health (transient API issue / race). Fall back to
        # the deployment-state logic below rather than asserting HEALTHY.

    # Not yet serving, or task health unreadable. Distinguish a service that is
    # still starting up (primary deployment rolling out) from one that is
    # genuinely down, so the UI can set the expectation that it will become
    # healthy shortly rather than showing an error. First-time provisioning
    # starts with runningCount 0 < desired and an IN_PROGRESS primary
    # deployment; reloads keep the old task serving (minimumHealthyPercent 100).
    deployments = svc.get("deployments", [])
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if primary is not None and primary.get("rolloutState") == "IN_PROGRESS":
        return VkgHealthStatus.PROVISIONING.value
    if running >= 1:
        # A task is running but we could not confirm its container health and it
        # is not rolling out — report DEGRADED (up, unconfirmed) rather than the
        # old false HEALTHY or a misleading UNAVAILABLE (which means "no task").
        return VkgHealthStatus.DEGRADED.value
    return VkgHealthStatus.UNAVAILABLE.value
