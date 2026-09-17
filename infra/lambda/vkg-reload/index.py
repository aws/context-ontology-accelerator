# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""VKG Reload Lambda — provisions or redeploys per-namespace VKG service on ontology publish.

On every reload, registers a new task definition revision with the latest container
image (from SSM, written by CDK deploy) before forcing a new deployment. This ensures
ontology reloads always pick up the latest VKG image, not a stale one from provision time.
"""

import json
import os
import re

import boto3

ecs = boto3.client("ecs")
sd = boto3.client("servicediscovery")
ssm = boto3.client("ssm")
autoscaling = boto3.client("application-autoscaling")
cloudwatch = boto3.client("cloudwatch")

_NS_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

# Reload-outcome metrics; the vkg stack alarms on ReloadFailed.
_METRIC_NAMESPACE = "COA/VKG"

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
      ``detail.namespace`` / ``detail.version`` and only that namespace's VKG
      service is reconciled.
    * **Sweep** (scheduled rule with constant input ``{"sweep": true}``): every
      ``<prefix>-vkg-*`` service in the cluster is reconciled against the latest
      SSM image. This keeps long-lived namespaces current even when their
      ontology is never republished — otherwise a base-image digest bump never
      reaches a running task, which is how stale VKG images accumulate.

    Args:
        event: EventBridge event. ``{"sweep": true}`` selects sweep mode;
            otherwise ``detail.namespace`` / ``detail.version`` select the
            per-namespace reload.
        context: Lambda runtime context (unused).

    Returns:
        Per-namespace: a status dict (``reload_triggered`` / ``provisioned`` /
        ``skipped`` / ``failed``). Sweep: ``{"status": "sweep_complete", ...}``
        with a per-namespace ``results`` list, or ``failed`` if the image cannot
        be resolved at all.

    Raises:
        Exception: In per-namespace mode, re-raised after emitting
            ``ReloadFailed`` on an unexpected error. The sweep isolates
            per-namespace failures and never raises for a single bad namespace.
    """
    cluster = os.environ["CLUSTER_ARN"]
    prefix = os.environ["RESOURCE_PREFIX"]

    if event.get("sweep") is True:
        return _sweep(cluster, prefix)

    ns = event.get("detail", {}).get("namespace", "unknown")
    version = event.get("detail", {}).get("version", "unknown")

    if ns == "unknown" or not _NS_PATTERN.match(ns):
        print(json.dumps({"action": "reload_skipped", "reason": "missing or invalid namespace"}))
        return {"status": "skipped", "reason": "missing or invalid namespace"}

    return _reload_one(ns, version, cluster, prefix)


def _sweep(cluster, prefix):
    """Reconcile every per-namespace VKG service in the cluster to the SSM image.

    Resolves the target image once, then reloads each ``<prefix>-vkg-*`` service.
    A single namespace's failure is recorded and the sweep continues — one broken
    service must not stop the rest from being patched.
    """
    print(json.dumps({"action": "sweep_start", "cluster": cluster}))
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
    for ns in namespaces:
        try:
            outcome = _reload_one(ns, "scheduled-sweep", cluster, prefix, container_image)
            results.append({"namespace": ns, "status": outcome.get("status")})
        except Exception as e:  # one bad namespace must not abort the sweep
            print(json.dumps({"action": "sweep_item_failed", "namespace": ns, "error": str(e)}))
            results.append({"namespace": ns, "status": "failed", "error": str(e)})

    triggered = sum(1 for r in results if r["status"] in ("reload_triggered", "provisioned"))
    print(json.dumps({"action": "sweep_complete", "total": len(results), "triggered": triggered}))
    return {"status": "sweep_complete", "total": len(results), "triggered": triggered, "results": results}


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


def _reload_one(ns, version, cluster, prefix, container_image=None):
    """Reconcile a single namespace's VKG service to the latest image.

    Resolves the target image from SSM when not supplied (the sweep resolves it
    once and passes it in for every namespace). If the running service already
    uses that image, forces a new deployment; otherwise registers a fresh
    task-definition revision first. If the service does not yet exist, provisions
    it. Reload outcomes are published as ``ReloadTriggered``/``ReloadFailed``
    CloudWatch metrics.

    Raises:
        Exception: Re-raised after emitting ``ReloadFailed`` when an unexpected
            error occurs during reload of an existing service.
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

        current_image = _get_current_service_image(cluster, service_name)

        if current_image == container_image:
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
            task_def_arn = _register_task_definition(ns, prefix, container_image)
            resp = ecs.update_service(
                cluster=cluster,
                service=service_name,
                taskDefinition=task_def_arn,
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
        return {"status": "reload_triggered", "namespace": ns, "deployment_id": deployment_id}
    except (ecs.exceptions.ServiceNotFoundException, ecs.exceptions.ServiceNotActiveException):
        print(json.dumps({"action": "provision_start", "namespace": ns, "service": service_name}))
        return _provision_and_deploy(ns, service_name, cluster, prefix, version, container_image)
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


def _get_current_service_image(cluster, service_name):
    """Get the container image currently used by the service's task definition."""
    try:
        svc_resp = ecs.describe_services(cluster=cluster, services=[service_name])
        services = svc_resp.get("services", [])
        if not services:
            return ""
        task_def_arn = services[0]["taskDefinition"]
        td_resp = ecs.describe_task_definition(taskDefinition=task_def_arn)
        containers = td_resp["taskDefinition"].get("containerDefinitions", [])
        return containers[0].get("image", "") if containers else ""
    except (ecs.exceptions.ServiceNotFoundException, ecs.exceptions.ServiceNotActiveException):
        return ""
    except Exception as e:
        print(json.dumps({"action": "get_image_failed", "service": service_name, "error_type": type(e).__name__}))
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

    # Task sizing and heap are read from the environment so this reload path and
    # the CDK-provisioned initial service stay in sync from a single source of
    # truth (VkgStack sets these on the reload Lambda). Previously this path
    # hardcoded cpu=512/memory=1024 while the CDK service defaulted to 1024/2048,
    # so per-namespace reloads silently ran under-provisioned and the OWL2QL
    # translation pass could never finish (#149 cause B). Defaults below match
    # the CDK stack defaults so behaviour is safe even if the env is unset.
    task_cpu = os.environ.get("VKG_TASK_CPU", "1024")
    task_memory = os.environ.get("VKG_TASK_MEMORY", "2048")
    # The Ontop launcher reads ONTOP_JAVA_ARGS (NOT JAVA_OPTS). Setting JAVA_OPTS
    # was a silent no-op (#149 cause C). When VKG_ONTOP_JAVA_ARGS is not set
    # explicitly, derive the heap from task memory (max heap ~= 75% of task
    # memory, initial heap ~= 25%) so bumping VKG_TASK_MEMORY alone scales the
    # heap in step rather than leaving a stale hardcoded -Xmx.
    ontop_java_args = os.environ.get("VKG_ONTOP_JAVA_ARGS", "")
    if not ontop_java_args:
        try:
            mem_mib = int(task_memory)
        except (TypeError, ValueError):
            mem_mib = 2048
        xmx = max(512, mem_mib * 3 // 4)
        xms = max(256, mem_mib // 4)
        ontop_java_args = f"-Xmx{xmx}m -Xms{xms}m"

    family = f"{prefix}-vkg-{ns}" if prefix else f"vkg-{ns}"
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
                        "awslogs-group": f"/ecs/vkg-{ns}",
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "vkg",
                        "awslogs-create-group": "true",
                    },
                },
            }
        ],
    )
    task_def_arn = td_resp["taskDefinition"]["taskDefinitionArn"]
    print(json.dumps({"action": "task_def_registered", "namespace": ns, "task_def_arn": task_def_arn}))
    return task_def_arn


def _provision_and_deploy(ns, service_name, cluster, prefix, version, container_image=None):
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
        return {"status": "provisioned", "namespace": ns, "service": service_name}
    except ecs.exceptions.InvalidParameterException as e:
        if "already exists" in str(e).lower() or "not idempotent" in str(e).lower():
            ecs.update_service(
                cluster=cluster,
                service=service_name,
                taskDefinition=task_def_arn,
                forceNewDeployment=True,
                deploymentConfiguration=_DEPLOY_CONFIG,
            )
            _configure_auto_scaling(cluster, service_name)
            print(json.dumps({"action": "reload_triggered", "namespace": ns, "service": service_name}))
            _emit_metric("ReloadTriggered", ns)
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
