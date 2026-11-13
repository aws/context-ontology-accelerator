# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG Reload Lambda — provisions or redeploys per-namespace VKG service on ontology publish.

On every reload, registers a new task definition revision with the latest container
image (from SSM, written by CDK deploy) before forcing a new deployment. This ensures
ontology reloads always pick up the latest VKG image, not a stale one from provision time.
"""

from __future__ import annotations

import contextlib
import json
import os
import re

import boto3

ecs = boto3.client("ecs")
ecr = boto3.client("ecr")
sd = boto3.client("servicediscovery")
ssm = boto3.client("ssm")
autoscaling = boto3.client("application-autoscaling")
cloudwatch = boto3.client("cloudwatch")
logs = boto3.client("logs")

_NS_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

# <registry>.dkr.ecr.<region>.amazonaws.com[.cn]/<repo>[:<tag>][@sha256:<digest>]
_ECR_IMAGE = re.compile(
    r"^(?P<registry>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/"
    r"(?P<repo>[a-z0-9][a-z0-9._/-]*?)(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
_DIGEST_SUFFIX = re.compile(r"@(?P<digest>sha256:[0-9a-f]{64})$")

# Reload-outcome metrics; the vkg stack alarms on ReloadFailed.
_METRIC_NAMESPACE = "COA/VKG"

# Binds Ontop's endpoint to loopback; translate-server is its only caller.
_ONTOP_LOOPBACK_ARG = "-Dserver.address=127.0.0.1"

_LOG_RETENTION_DAYS = 30

# Revision cleanup stops when less than this much invocation time is left, so a
# backlog of stale revisions can't time the Lambda out; the next reload goes on.
_CLEANUP_MIN_REMAINING_MS = 10_000

# Circuit breaker + rollback: paired with the container's functional /health
# probe, a broken reload never goes healthy and rolls back to the last-good task.
_DEPLOY_CONFIG = {
    "minimumHealthyPercent": 100,
    "maximumPercent": 200,
    "deploymentCircuitBreaker": {"enable": True, "rollback": True},
}


def _emit_metric(name, namespace):
    """Emit a reload-outcome metric. Never raises.

    Each metric is published twice: once dimensioned by Namespace (for
    per-namespace dashboards / drill-down) and once with no dimensions (a
    cluster-wide roll-up). The undimensioned series is what the vkg stack's
    ReloadFailedAlarm evaluates: CloudWatch metric alarms cannot be backed by a
    SEARCH() expression, so the alarm needs a single concrete series that
    actually receives data across all namespaces.
    """
    try:
        cloudwatch.put_metric_data(
            Namespace=_METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": name,
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "Namespace", "Value": namespace}],
                },
                {
                    "MetricName": name,
                    "Value": 1,
                    "Unit": "Count",
                },
            ],
        )
    except Exception as e:  # pragma: no cover - defensive
        print(json.dumps({"action": "metric_emit_failed", "metric": name, "error": str(e)}))


def handler(event, context):
    """Provision or redeploy per-namespace VKG service(s).

    Two entry modes:

    * **Per-namespace** (EventBridge ``ontology.published``): the event carries
      ``detail.namespace`` / ``detail.version`` and only that namespace is reconciled.
      An unexpected error is re-raised after emitting ``ReloadFailed``.
    * **Sweep** (``{"sweep": true}``, from the weekly rule and the deploy-time custom
      resource): every ``<prefix>-vkg-*`` service in the cluster is reconciled against the
      latest SSM image, so namespaces whose ontology is never republished still pick up
      base-image bumps. A service already on the latest image is left alone, and a single
      bad namespace never fails the sweep.
    """
    cluster = os.environ["CLUSTER_ARN"]
    prefix = os.environ["RESOURCE_PREFIX"]

    if event.get("sweep") is True:
        return _sweep(cluster, prefix, str(event.get("trigger") or "schedule"), context)

    ns = event.get("detail", {}).get("namespace", "unknown")
    version = event.get("detail", {}).get("version", "unknown")

    if ns == "unknown" or not _NS_PATTERN.match(ns):
        print(json.dumps({"action": "reload_skipped", "reason": "missing or invalid namespace"}))
        return {"status": "skipped", "reason": "missing or invalid namespace"}

    # The default event bus is shared by every deployment in the account and Region.
    # An event emitted from another deployment names that deployment's bucket, so a
    # namespace this one does not own is skipped instead of getting a service that
    # no teardown here would ever delete.
    event_bucket = event.get("detail", {}).get("s3Bucket")
    own_bucket = os.environ.get("ONTOLOGY_BUCKET", "")
    if event_bucket and own_bucket and event_bucket != own_bucket:
        print(json.dumps({"action": "reload_skipped", "reason": "event from another deployment", "namespace": ns}))
        return {"status": "skipped", "reason": "event from another deployment"}

    return _reload_one(ns, version, cluster, prefix, context=context)


def _sweep(cluster, prefix, trigger="schedule", context=None):
    """Reconcile every per-namespace VKG service in the cluster to the SSM image.

    Resolves the target image once, then reloads each ``<prefix>-vkg-*`` service
    that is not already running it. A service is skipped (no restart) only when
    its running tasks' image digest equals the digest the target image resolves
    to; a tag can be reused for a new build, so equal image URIs alone do not
    prove the image is current. A single namespace's failure is recorded and the
    sweep continues — one broken service must not stop the rest from being
    patched.
    """
    print(json.dumps({"action": "sweep_start", "cluster": cluster, "trigger": trigger}))
    container_image = _resolve_container_image()
    if not container_image:
        print(json.dumps({"action": "sweep_failed", "reason": "cannot resolve container image"}))
        _emit_metric("ReloadFailed", "sweep")
        return {"status": "failed", "reason": "cannot resolve container image"}

    try:
        namespaces = _list_vkg_namespaces(cluster, prefix)
    except Exception as e:
        # A failed ListServices (throttling, IAM denial, bad cluster) must not
        # raise uncaught — surface it like every other reload failure so the
        # ReloadFailed alarm fires and the run ends cleanly.
        print(json.dumps({"action": "sweep_failed", "reason": "list_services_failed", "error": str(e)}))
        _emit_metric("ReloadFailed", "sweep")
        return {"status": "failed", "reason": f"list services failed: {e}"}

    results = []
    # The target image is the same for every service, so look its digest up once.
    digest_cache: dict[str, str] = {}
    for ns in namespaces:
        try:
            outcome = _reload_one(
                ns,
                f"sweep-{trigger}",
                cluster,
                prefix,
                container_image,
                restart_if_current=False,
                digest_cache=digest_cache,
                context=context,
            )
            results.append({"namespace": ns, "status": outcome.get("status")})
        except Exception as e:  # one bad namespace must not abort the sweep
            print(json.dumps({"action": "sweep_item_failed", "namespace": ns, "error": str(e)}))
            results.append({"namespace": ns, "status": "failed", "error": str(e)})

    triggered = sum(1 for r in results if r["status"] in ("reload_triggered", "provisioned"))
    unchanged = sum(1 for r in results if r["status"] == "unchanged")
    print(
        json.dumps(
            {
                "action": "sweep_complete",
                "trigger": trigger,
                "total": len(results),
                "triggered": triggered,
                "unchanged": unchanged,
            }
        )
    )
    return {
        "status": "sweep_complete",
        "total": len(results),
        "triggered": triggered,
        "unchanged": unchanged,
        "results": results,
    }


def _list_vkg_namespaces(cluster, prefix):
    """Return the namespace id of every ``<prefix>-vkg-*`` service in the cluster."""
    marker = f"{prefix}-vkg-"
    namespaces = []
    paginator = ecs.get_paginator("list_services")
    for page in paginator.paginate(cluster=cluster):
        for arn in page.get("serviceArns", []):
            name = arn.split("/")[-1]
            if name.startswith(marker):
                namespaces.append(name[len(marker) :])
    return namespaces


def _reload_one(
    ns, version, cluster, prefix, container_image=None, restart_if_current=True, digest_cache=None, context=None
):
    """Reconcile a single namespace's VKG service to the latest image.

    Registers a fresh task-definition revision when the service names a different image.
    For the same image URI it forces a new deployment when ``restart_if_current`` is true
    (an ontology publish must reload mappings); otherwise (the sweep) it leaves the service
    alone only when every running task's digest equals the digest the URI resolves to now,
    restarting when either digest is unknown. Provisions the service if it does not exist,
    and re-raises after emitting ``ReloadFailed`` on an unexpected error.
    """
    service_name = f"{prefix}-vkg-{ns}"
    log = {"action": "reload_start", "namespace": ns, "version": version, "cluster": cluster, "service": service_name}
    print(json.dumps(log))

    try:
        if container_image is None:
            container_image = _resolve_container_image()
        if not container_image:
            print(json.dumps({"action": "reload_failed", "namespace": ns, "reason": "cannot resolve container image"}))
            _emit_metric("ReloadFailed", ns)
            return {"status": "failed", "reason": "cannot resolve container image"}

        current = _get_current_service(cluster, service_name)
        if current is None:
            # No ACTIVE service: provision it, registering its first revision once.
            raise _ServiceMissing(service_name)
        current_task_def, current_image, deployed_task_defs = current

        if current_image == container_image and not restart_if_current:
            if digest_cache is not None and container_image in digest_cache:
                target_digest = digest_cache[container_image]
            else:
                target_digest = _resolve_image_digest(container_image)
                if digest_cache is not None:
                    digest_cache[container_image] = target_digest
            running_digests = _running_image_digests(cluster, service_name)
            if target_digest and running_digests and running_digests == {target_digest}:
                print(
                    json.dumps(
                        {
                            "action": "image_current_skipped",
                            "namespace": ns,
                            "image": container_image,
                            "digest": target_digest,
                        }
                    )
                )
                return {"status": "unchanged", "namespace": ns}
            # Same URI but a different (or unknown) digest: the tag now points at another
            # build, or we cannot tell. Restart so ECS re-resolves the tag.
            known = bool(target_digest and running_digests)
            print(
                json.dumps(
                    {
                        "action": "image_digest_changed" if known else "image_digest_unknown",
                        "namespace": ns,
                        "image": container_image,
                        "target_digest": target_digest or "(unknown)",
                        "running_digests": sorted(running_digests) or ["(unknown)"],
                    }
                )
            )
        new_task_def = current_task_def
        if current_image == container_image:
            if restart_if_current:
                print(json.dumps({"action": "image_unchanged", "namespace": ns, "image": container_image}))
            resp = ecs.update_service(
                cluster=cluster,
                service=service_name,
                forceNewDeployment=True,
                deploymentConfiguration=_DEPLOY_CONFIG,
            )
        else:
            print(
                json.dumps(
                    {
                        "action": "image_updated",
                        "namespace": ns,
                        "old_image": current_image or "(unknown)",
                        "new_image": container_image,
                    }
                )
            )
            new_task_def = _register_task_definition(ns, prefix, container_image)
            resp = ecs.update_service(
                cluster=cluster,
                service=service_name,
                taskDefinition=new_task_def,
                forceNewDeployment=True,
                deploymentConfiguration=_DEPLOY_CONFIG,
            )
        deployment_id = resp["service"].get("deployments", [{}])[0].get("id", "unknown")
        print(
            json.dumps(
                {
                    "action": "reload_triggered",
                    "namespace": ns,
                    "version": version,
                    "service": service_name,
                    "deployment_id": deployment_id,
                    "image": container_image,
                    "image_changed": current_image != container_image,
                }
            )
        )
        _emit_metric("ReloadTriggered", ns)
        # Every revision a deployment still uses may be the circuit breaker's rollback
        # target (the last completed one), so all of them stay ACTIVE.
        if current_task_def:
            deployed_task_defs += _deployment_task_defs(resp.get("service") or {})
            _deregister_older_revisions([new_task_def, current_task_def, *deployed_task_defs], context)
        return {"status": "reload_triggered", "namespace": ns, "deployment_id": deployment_id}
    except (_ServiceMissing, ecs.exceptions.ServiceNotFoundException, ecs.exceptions.ServiceNotActiveException):
        print(json.dumps({"action": "provision_start", "namespace": ns, "service": service_name}))
        return _provision_and_deploy(ns, service_name, cluster, prefix, version, container_image, context)
    except Exception as e:
        print(
            json.dumps(
                {
                    "action": "reload_failed",
                    "namespace": ns,
                    "service": service_name,
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            )
        )
        _emit_metric("ReloadFailed", ns)
        raise


class _ServiceMissing(Exception):
    """The namespace has no ACTIVE VKG service yet."""


def _deployment_task_defs(service):
    """The task definitions of the service's PRIMARY and ACTIVE deployments."""
    return [
        d["taskDefinition"]
        for d in service.get("deployments") or []
        if d.get("taskDefinition") and d.get("status", "ACTIVE") in ("PRIMARY", "ACTIVE")
    ]


def _get_current_service(cluster, service_name):
    """Return ``(task_def_arn, image, deployment_task_defs)`` of the ACTIVE service.

    Returns None when there is none. A failed lookup returns empty values, so the
    caller registers a revision and updates the service without any cleanup.
    """
    try:
        svc_resp = ecs.describe_services(cluster=cluster, services=[service_name])
        services = svc_resp.get("services", [])
        if not services or services[0].get("status", "ACTIVE") != "ACTIVE":
            return None
        task_def_arn = services[0]["taskDefinition"]
        td_resp = ecs.describe_task_definition(taskDefinition=task_def_arn)
        containers = td_resp["taskDefinition"].get("containerDefinitions", [])
        return task_def_arn, (containers[0].get("image", "") if containers else ""), _deployment_task_defs(services[0])
    except (ecs.exceptions.ServiceNotFoundException, ecs.exceptions.ServiceNotActiveException):
        return None
    except Exception as e:
        print(json.dumps({"action": "get_image_failed", "service": service_name, "error_type": type(e).__name__}))
        return "", "", []


def _deregister_older_revisions(keep_arns, context=None):
    """Deregister the family's ACTIVE revisions that are not in ``keep_arns``. Never raises.

    The family is the one of ``keep_arns[0]``. Revisions newer than every kept
    one are left alone, so a concurrent reload's fresh revision is never pulled
    from under it. One failed deregistration doesn't stop the rest. The cleanup
    stops when ``context`` has less than ``_CLEANUP_MIN_REMAINING_MS`` left; the
    remaining revisions are logged and left for the next reload.
    """
    family, _, _ = keep_arns[0].rpartition("/")[2].rpartition(":")
    keep = set()
    for arn in keep_arns:
        fam, _, rev = arn.rpartition("/")[2].rpartition(":")
        if fam == family and rev.isdigit():
            keep.add(int(rev))
    if not family or not keep:
        return
    try:
        stale = []
        paginator = ecs.get_paginator("list_task_definitions")
        # familyPrefix is a prefix match: compare the exact family on each ARN.
        for page in paginator.paginate(familyPrefix=family, status="ACTIVE"):
            for arn in page.get("taskDefinitionArns", []):
                fam, _, rev = arn.rpartition("/")[2].rpartition(":")
                if fam == family and rev.isdigit() and int(rev) < max(keep) and int(rev) not in keep:
                    stale.append(arn)
    except Exception as e:
        print(json.dumps({"action": "task_defs_list_failed", "family": family, "error": str(e)}))
        return
    done = failed = 0
    for i, arn in enumerate(stale):
        if context is not None and context.get_remaining_time_in_millis() < _CLEANUP_MIN_REMAINING_MS:
            print(json.dumps({"action": "task_defs_deregister_deferred", "family": family, "left": len(stale) - i}))
            break
        try:
            ecs.deregister_task_definition(taskDefinition=arn)
            done += 1
        except Exception as e:
            failed += 1
            print(json.dumps({"action": "task_def_deregister_failed", "task_def_arn": arn, "error": str(e)}))
    if done or failed:
        print(json.dumps({"action": "task_defs_deregistered", "family": family, "count": done, "failed": failed}))


def _running_image_digests(cluster, service_name):
    """Return the set of image digests of the service's RUNNING tasks.

    An empty set means "unknown": no running task, a task that has not reported
    a digest yet, or an API error. The caller then restarts the service rather
    than skipping it.
    """
    try:
        arns = ecs.list_tasks(cluster=cluster, serviceName=service_name, desiredStatus="RUNNING").get("taskArns", [])
        if not arns:
            return set()
        digests = set()
        for i in range(0, len(arns), 100):
            for task in ecs.describe_tasks(cluster=cluster, tasks=arns[i : i + 100]).get("tasks", []):
                containers = task.get("containers") or []
                digest = containers[0].get("imageDigest", "") if containers else ""
                if not digest:
                    return set()
                digests.add(digest)
        return digests
    except Exception as e:
        print(json.dumps({"action": "running_digest_failed", "service": service_name, "error_type": type(e).__name__}))
        return set()


def _resolve_image_digest(image):
    """Return the digest an image URI points at now, or "" when it cannot be resolved.

    A URI pinned with ``@sha256:`` is its own digest. A tagged ECR URI is looked
    up with ``ecr:DescribeImages``, so a tag that was moved to a new build
    resolves to the new digest. Anything else (a non-ECR registry, no tag, an
    API error) is unknown.
    """
    pinned = _DIGEST_SUFFIX.search(image or "")
    if pinned:
        return pinned.group("digest")
    match = _ECR_IMAGE.match(image or "")
    if not match or not match.group("tag"):
        print(json.dumps({"action": "image_digest_unresolvable", "image": image, "reason": "not a tagged ECR image"}))
        return ""
    region = match.group("region")
    try:
        client = ecr if region == os.environ.get("AWS_REGION", region) else boto3.client("ecr", region_name=region)
        resp = client.describe_images(
            registryId=match.group("registry"),
            repositoryName=match.group("repo"),
            imageIds=[{"imageTag": match.group("tag")}],
        )
        details = resp.get("imageDetails") or []
        return details[0].get("imageDigest", "") if details else ""
    except Exception as e:
        print(json.dumps({"action": "image_digest_lookup_failed", "image": image, "error_type": type(e).__name__}))
        return ""


def _resolve_container_image():
    """Read latest VKG image URI from SSM (written by CDK on every deploy)."""
    param_name = os.environ.get("VKG_IMAGE_PARAM_NAME", "")
    if not param_name:
        print(json.dumps({"action": "image_resolve_failed", "reason": "VKG_IMAGE_PARAM_NAME not set"}))
        return ""
    try:
        resp = ssm.get_parameter(Name=param_name)
        image = resp["Parameter"]["Value"]
        print(json.dumps({"action": "image_resolved", "image": image, "source": "ssm"}))
        return image
    except Exception as e:
        print(json.dumps({"action": "image_resolve_failed", "reason": str(e)}))
        return ""


def _register_task_definition(ns, prefix, container_image):
    """Register a new task def revision with the latest image for this namespace."""
    task_role_arn = os.environ.get("VKG_TASK_ROLE_ARN", "")
    execution_role_arn = os.environ.get("VKG_EXECUTION_ROLE_ARN", "")
    ontology_bucket = os.environ.get("ONTOLOGY_BUCKET", "")
    region = os.environ.get("AWS_REGION", "us-west-2")

    # Task sizing and heap are read from the environment (set on the reload Lambda by
    # VkgStack) so this path matches the CDK-provisioned service. Defaults below match the
    # CDK stack defaults.
    task_cpu = os.environ.get("VKG_TASK_CPU", "1024")
    task_memory = os.environ.get("VKG_TASK_MEMORY", "2048")
    # The Ontop launcher reads ONTOP_JAVA_ARGS (NOT JAVA_OPTS). When VKG_ONTOP_JAVA_ARGS is
    # not set, derive the heap from task memory (max heap ~= 75%, initial ~= 25%).
    ontop_java_args = os.environ.get("VKG_ONTOP_JAVA_ARGS", "").strip()
    if not ontop_java_args:
        try:
            mem_mib = int(task_memory)
        except (TypeError, ValueError):
            mem_mib = 2048
        xmx = max(512, mem_mib * 3 // 4)
        xms = max(256, mem_mib // 4)
        ontop_java_args = f"-Xmx{xmx}m -Xms{xms}m"
    # Bind Ontop to loopback whatever the env says (VkgStack already appends it):
    # only translate-server calls Ontop. Appended last, it supersedes any other
    # -Dserver.address, since Java keeps the last -D for a property.
    if ontop_java_args.split()[-1:] != [_ONTOP_LOOPBACK_ARG]:
        ontop_java_args = f"{ontop_java_args} {_ONTOP_LOOPBACK_ARG}"

    family = f"{prefix}-vkg-{ns}" if prefix else f"vkg-{ns}"
    log_group = f"/ecs/{family}"
    with contextlib.suppress(logs.exceptions.ResourceAlreadyExistsException):
        logs.create_log_group(logGroupName=log_group)
    logs.put_retention_policy(logGroupName=log_group, retentionInDays=_LOG_RETENTION_DAYS)
    # The unprefixed group of a revision registered before the prefix gets no new
    # writes once this revision runs; retention lets its history expire. Best-effort.
    legacy_group = f"/ecs/vkg-{ns}"
    try:
        logs.put_retention_policy(logGroupName=legacy_group, retentionInDays=_LOG_RETENTION_DAYS)
    except logs.exceptions.ResourceNotFoundException:
        pass
    except Exception as e:
        print(
            json.dumps(
                {"action": "legacy_log_retention_failed", "namespace": ns, "log_group": legacy_group, "error": str(e)}
            )
        )
    td_resp = ecs.register_task_definition(
        family=family,
        taskRoleArn=task_role_arn,
        executionRoleArn=execution_role_arn,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=task_cpu,
        memory=task_memory,
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        containerDefinitions=[
            {
                "name": "ontop",
                "image": container_image,
                "essential": True,
                "environment": [
                    {"name": "ONTOLOGY_BUCKET", "value": ontology_bucket},
                    {"name": "ONTOLOGY_PREFIX", "value": "ontologies/"},
                    {"name": "NAMESPACE", "value": ns},
                    {"name": "NAMESPACE_ID", "value": ns},
                    {"name": "ENDPOINT_PORT", "value": "8080"},
                    {"name": "ONTOP_JAVA_ARGS", "value": ontop_java_args},
                ],
                "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
                "healthCheck": {
                    "command": ["CMD-SHELL", "curl -sf http://localhost:8080/health || exit 1"],
                    "interval": 30,
                    "timeout": 10,
                    "retries": 5,
                    "startPeriod": 180,
                },
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "vkg",
                        # Recreates the group if it was deleted: an unchanged image
                        # reuses this revision without calling create_log_group.
                        "awslogs-create-group": "true",
                    },
                },
            }
        ],
    )
    task_def_arn = td_resp["taskDefinition"]["taskDefinitionArn"]
    print(json.dumps({"action": "task_def_registered", "namespace": ns, "task_def_arn": task_def_arn}))
    return task_def_arn


def _provision_and_deploy(ns, service_name, cluster, prefix, version, container_image=None, context=None):
    """Create Cloud Map entry and launch ECS service for a new namespace."""
    cloud_map_ns_id = os.environ.get("CLOUD_MAP_NAMESPACE_ID", "")
    ontology_bucket = os.environ.get("ONTOLOGY_BUCKET", "")
    subnet_ids = [s for s in os.environ.get("PRIVATE_SUBNET_IDS", "").split(",") if s]
    sg_id = os.environ.get("ECS_SECURITY_GROUP_ID", "")

    if not all([cloud_map_ns_id, ontology_bucket, subnet_ids]):
        print(json.dumps({"action": "provision_skipped", "namespace": ns, "reason": "missing env vars"}))
        return {"status": "skipped", "reason": "provision env vars not configured"}

    if not container_image:
        container_image = _resolve_container_image()
    if not container_image:
        print(json.dumps({"action": "provision_failed", "namespace": ns, "reason": "cannot resolve container image"}))
        _emit_metric("ReloadFailed", ns)
        return {"status": "failed", "reason": "cannot resolve container image"}

    registry_arn = _ensure_cloud_map(ns, cloud_map_ns_id)
    try:
        task_def_arn = _register_task_definition(ns, prefix, container_image)
    except Exception as e:
        # The Cloud Map entry was just created (line above); without a task def
        # the service can never be created, leaving it orphaned. Log with the
        # dangling registry so an operator can reconcile, then fail the reload.
        print(
            json.dumps(
                {
                    "action": "register_task_definition_failed",
                    "namespace": ns,
                    "registry_arn": registry_arn,
                    "error": str(e),
                }
            )
        )
        _emit_metric("ReloadFailed", ns)
        return {"status": "failed", "reason": f"register_task_definition failed: {e}"}

    try:
        ecs.create_service(
            cluster=cluster,
            serviceName=service_name,
            taskDefinition=task_def_arn,
            desiredCount=1,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": subnet_ids,
                    "securityGroups": [sg_id] if sg_id else [],
                    "assignPublicIp": "DISABLED",
                }
            },
            serviceRegistries=[{"registryArn": registry_arn}],
            deploymentConfiguration={
                "minimumHealthyPercent": 100,
                "maximumPercent": 200,
                "deploymentCircuitBreaker": {"enable": True, "rollback": True},
            },
        )
        print(json.dumps({"action": "service_created", "namespace": ns, "service": service_name}))
        _configure_auto_scaling(cluster, service_name)
        _emit_metric("ReloadTriggered", ns)
        # A revision left by a failed earlier attempt is older, so this removes it too.
        _deregister_older_revisions([task_def_arn], context)
        return {"status": "provisioned", "namespace": ns, "service": service_name}
    except ecs.exceptions.InvalidParameterException as e:
        if "already exists" in str(e).lower() or "not idempotent" in str(e).lower():
            resp = ecs.update_service(
                cluster=cluster,
                service=service_name,
                taskDefinition=task_def_arn,
                forceNewDeployment=True,
                deploymentConfiguration=_DEPLOY_CONFIG,
            )
            _configure_auto_scaling(cluster, service_name)
            print(json.dumps({"action": "reload_triggered", "namespace": ns, "service": service_name}))
            _emit_metric("ReloadTriggered", ns)
            # The winner's revision stays while its deployment is listed.
            _deregister_older_revisions([task_def_arn, *_deployment_task_defs(resp.get("service") or {})], context)
            return {"status": "reload_triggered", "namespace": ns}
        raise


def _configure_auto_scaling(cluster, service_name):
    """Register auto-scaling for a per-namespace VKG service (min=1, max=3, CPU 70%)."""
    cluster_name = cluster.split("/")[-1]
    resource_id = f"service/{cluster_name}/{service_name}"
    try:
        autoscaling.register_scalable_target(
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            MinCapacity=1,
            MaxCapacity=3,
        )
        autoscaling.put_scaling_policy(
            PolicyName=f"{service_name}-cpu-scaling",
            ServiceNamespace="ecs",
            ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            PolicyType="TargetTrackingScaling",
            TargetTrackingScalingPolicyConfiguration={
                "TargetValue": 70.0,
                "PredefinedMetricSpecification": {"PredefinedMetricType": "ECSServiceAverageCPUUtilization"},
                "ScaleInCooldown": 300,
                "ScaleOutCooldown": 60,
            },
        )
        print(json.dumps({"action": "autoscaling_configured", "service": service_name}))
    except Exception as e:
        print(json.dumps({"action": "autoscaling_failed", "service": service_name, "error": str(e)}))


def _ensure_cloud_map(ns, cloud_map_ns_id):
    """Create or look up Cloud Map service entry. Returns registry ARN."""
    try:
        resp = sd.create_service(
            Name=f"vkg-{ns}",
            NamespaceId=cloud_map_ns_id,
            DnsConfig={"DnsRecords": [{"Type": "A", "TTL": 10}], "RoutingPolicy": "MULTIVALUE"},
            HealthCheckCustomConfig={"FailureThreshold": 1},
        )
        return resp["Service"]["Arn"]
    except sd.exceptions.ServiceAlreadyExists:
        pass
    paginator = sd.get_paginator("list_services")
    for page in paginator.paginate(Filters=[{"Name": "NAMESPACE_ID", "Values": [cloud_map_ns_id], "Condition": "EQ"}]):
        for svc in page.get("Services", []):
            if svc["Name"] == f"vkg-{ns}":
                return svc["Arn"]
    raise RuntimeError(f"Cloud Map service vkg-{ns} not found after create reported it exists")
