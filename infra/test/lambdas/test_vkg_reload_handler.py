# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for VKG reload Lambda handler."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambda" / "vkg-reload"))

ENV = {
    "CLUSTER_ARN": "arn:aws:ecs:us-west-2:111:cluster/vkg",
    "RESOURCE_PREFIX": "coa-dev",
    "CLOUD_MAP_NAMESPACE_ID": "ns-abc123",
    "VKG_TASK_ROLE_ARN": "arn:aws:iam::111:role/task",
    "VKG_EXECUTION_ROLE_ARN": "arn:aws:iam::111:role/exec",
    "ONTOLOGY_BUCKET": "ontology-bucket",
    "PRIVATE_SUBNET_IDS": "subnet-a,subnet-b",
    "ECS_SECURITY_GROUP_ID": "sg-123",
    "AWS_REGION": "us-west-2",
    "VKG_IMAGE_PARAM_NAME": "/coa/vkg/container-image",
}


def _make_event(namespace="test-ns", version="v1"):
    return {"detail": {"namespace": namespace, "version": version}}


class TestHandler:
    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_with_new_image_registers_task_def(self, mock_ssm, mock_sd, mock_ecs):
        """When SSM image differs from current, register new task def before deploying."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        mock_ecs.register_task_definition.assert_called_once()
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-test-ns",
            taskDefinition="arn:td:ns-rev2",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_same_image_skips_task_def_registration(self, mock_ssm, mock_sd, mock_ecs):
        """When SSM image matches current, just forceNewDeployment without new task def."""
        same_image = "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-same"
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": same_image}}
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": same_image}]}
        }
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        mock_ecs.register_task_definition.assert_not_called()
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-test-ns",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_uses_latest_image_from_ssm(self, mock_ssm, mock_sd, mock_ecs):
        """The reload path must read image from SSM, not from a running service."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-new123"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev3"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-2"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        index.handler(_make_event(), None)

        mock_ssm.get_parameter.assert_called_with(Name="/coa/vkg/container-image")
        td_call = mock_ecs.register_task_definition.call_args
        container_defs = td_call[1]["containerDefinitions"]
        assert container_defs[0]["image"] == "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-new123"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_when_service_not_found(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-abc"}
        }
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"
        mock_ecs.create_service.assert_called_once()
        assert mock_ecs.create_service.call_args[1]["serviceName"] == "coa-dev-vkg-test-ns"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_invalid_namespace_rejected(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(namespace="../evil"), None)
        assert result["status"] == "skipped"
        assert "invalid" in result["reason"]
        mock_ecs.update_service.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_missing_namespace_rejected(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"detail": {"version": "v1"}}, None)
        assert result["status"] == "skipped"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_race_condition_handles_not_idempotent(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = [
            mock_ecs.exceptions.ServiceNotFoundException("not found"),
            {"service": {"deployments": [{"id": "deploy-2"}]}},
        ]
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_ecs.create_service.side_effect = mock_ecs.exceptions.InvalidParameterException(
            "Creation of service was not idempotent."
        )
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_service_not_active_triggers_provision(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotActiveException("not active")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"

    @patch.dict(os.environ, {**ENV, "CLOUD_MAP_NAMESPACE_ID": "", "PRIVATE_SUBNET_IDS": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_skipped_when_env_vars_missing(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "skipped"
        assert "env vars" in result["reason"]

    @patch.dict(os.environ, {**ENV, "VKG_IMAGE_PARAM_NAME": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_fails_when_ssm_param_not_configured(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "failed"
        assert "container image" in result["reason"]

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_provision_configures_auto_scaling(self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs):
        """New namespace provision must configure auto-scaling (min=1, max=3, CPU 70%)."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"
        mock_autoscaling.register_scalable_target.assert_called_once()
        target_call = mock_autoscaling.register_scalable_target.call_args[1]
        assert target_call["MinCapacity"] == 1
        assert target_call["MaxCapacity"] == 3
        mock_autoscaling.put_scaling_policy.assert_called_once()
        policy_call = mock_autoscaling.put_scaling_policy.call_args[1]
        assert policy_call["TargetTrackingScalingPolicyConfiguration"]["TargetValue"] == 70.0

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    @patch("index.autoscaling")
    def test_provision_succeeds_even_if_autoscaling_fails(self, mock_autoscaling, mock_ssm, mock_sd, mock_ecs):
        """Auto-scaling failure must not prevent service provisioning."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})
        mock_autoscaling.register_scalable_target.side_effect = Exception("autoscaling API error")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm
        index.autoscaling = mock_autoscaling

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"

    # ── circuit breaker + reload-outcome metrics ────────────────

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_enables_deployment_circuit_breaker(self, mock_ssm, mock_sd, mock_ecs):
        """A reload deploys with the ECS circuit breaker + rollback so a load that
        never goes healthy rolls back instead of leaving the namespace broken."""
        same_image = "img:same"
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": same_image}}
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": same_image}]}
        }
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        index.handler(_make_event(), None)

        cfg = mock_ecs.update_service.call_args.kwargs["deploymentConfiguration"]
        assert cfg["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_successful_reload_emits_reload_triggered_metric(self, mock_ssm, mock_sd, mock_ecs):
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        index.handler(_make_event(), None)

        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadTriggered" in metrics

    @patch.dict(os.environ, {**ENV, "VKG_IMAGE_PARAM_NAME": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_unresolvable_image_emits_reload_failed_metric(self, mock_ssm, mock_sd, mock_ecs):
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "failed"
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_reload_exception_emits_reload_failed_and_reraises(self, mock_ssm, mock_sd, mock_ecs):
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }
        mock_ecs.update_service.side_effect = RuntimeError("ecs boom")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        import pytest

        with pytest.raises(RuntimeError, match="ecs boom"):
            index.handler(_make_event(), None)

        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_success_emits_reload_triggered_metric(self, mock_ssm, mock_sd, mock_ecs):
        """A first-time provision (create_service) must emit ReloadTriggered, like the update path."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:latest"}}
        mock_ecs.describe_services.return_value = {"services": []}
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:new"}}
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "provisioned"
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadTriggered" in metrics

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_provision_unresolvable_image_emits_reload_failed_metric(self, mock_ssm, mock_sd, mock_ecs):
        """The provision path's unresolvable-image failure must emit ReloadFailed, like its reload-path twin."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": ""}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index._provision_and_deploy("test-ns", "coa-dev-vkg-test-ns", ENV["CLUSTER_ARN"], "coa-dev", "v1")
        assert result["status"] == "failed"
        assert "cannot resolve container image" in result["reason"]
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    def test_emit_metric_publishes_dimensioned_and_undimensioned_series(self):
        """Each metric is published twice in one call: once dimensioned by
        Namespace (drill-down) and once undimensioned (the cluster-wide roll-up
        the ReloadFailedAlarm evaluates — a metric alarm can't use SEARCH())."""
        import importlib

        import index

        importlib.reload(index)
        index.cloudwatch = MagicMock()

        index._emit_metric("ReloadFailed", "some-ns")

        index.cloudwatch.put_metric_data.assert_called_once()
        call = index.cloudwatch.put_metric_data.call_args
        assert call.kwargs["Namespace"] == "COA/VKG"
        entries = call.kwargs["MetricData"]
        assert len(entries) == 2
        assert all(e["MetricName"] == "ReloadFailed" for e in entries)
        dimensioned = [e for e in entries if e.get("Dimensions")]
        undimensioned = [e for e in entries if not e.get("Dimensions")]
        assert len(dimensioned) == 1
        assert dimensioned[0]["Dimensions"] == [{"Name": "Namespace", "Value": "some-ns"}]
        # The alarm reads this undimensioned roll-up; it must exist.
        assert len(undimensioned) == 1


class TestSweep:
    """Scheduled-sweep mode: reconcile every per-namespace VKG service."""

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_reconciles_all_vkg_services(self, mock_ssm, mock_sd, mock_ecs):
        """{"sweep": true} reloads every <prefix>-vkg-* service, skipping non-vkg ones."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "serviceArns": [
                    "arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns1",
                    "arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns2",
                    "arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-notvkg",
                ]
            }
        ]
        mock_ecs.get_paginator.return_value = paginator
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)

        assert result["status"] == "sweep_complete"
        assert result["total"] == 2  # the non-vkg service is filtered out
        assert result["triggered"] == 2
        assert mock_ecs.update_service.call_count == 2
        assert {r["namespace"] for r in result["results"]} == {"ns1", "ns2"}
        # image resolved once for the whole sweep, not per namespace
        mock_ssm.get_parameter.assert_called_once_with(Name="/coa/vkg/container-image")

    @staticmethod
    def _load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr=None):
        import importlib

        import index

        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d"}]}}
        importlib.reload(index)
        index.ecs = mock_ecs
        index.ecr = mock_ecr if mock_ecr is not None else MagicMock()
        index.sd = mock_sd
        index.ssm = mock_ssm
        return index

    # A tag that CI pushes again for every build, as in `{repo}:{name}-latest`.
    REUSED_TAG = "111111111111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-latest"
    OLD_DIGEST = "sha256:" + "a" * 64
    NEW_DIGEST = "sha256:" + "b" * 64

    @staticmethod
    def _running(mock_ecs, digest_by_service):
        """Running tasks per service, each reporting the given image digest."""
        mock_ecs.list_tasks.side_effect = lambda cluster, serviceName, desiredStatus: {
            "taskArns": [f"arn:task/{serviceName}"] if serviceName in digest_by_service else []
        }
        mock_ecs.describe_tasks.side_effect = lambda cluster, tasks: {
            "tasks": [{"containers": [{"imageDigest": digest_by_service[t.split("/", 1)[1]]}]} for t in tasks]
        }

    @staticmethod
    def _tag_points_at(mock_ecr, digest):
        mock_ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": digest}]}

    @classmethod
    def _same_uri_everywhere(cls, mock_ssm, mock_ecs, image, *namespaces):
        """SSM and every service's task definition name the same image URI."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": image}}
        cls._services(mock_ecs, *namespaces)
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": image}]}
        }

    @staticmethod
    def _services(mock_ecs, *namespaces):
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"serviceArns": [f"arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-{ns}" for ns in namespaces]}
        ]
        mock_ecs.get_paginator.return_value = paginator

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_leaves_services_already_on_latest_image_alone(self, mock_ssm, mock_sd, mock_ecs):
        """A service already running the image's current digest is not restarted (no update, no new revision)."""
        self._same_uri_everywhere(mock_ssm, mock_ecs, self.REUSED_TAG, "ns1", "ns2")
        self._running(mock_ecs, {"coa-dev-vkg-ns1": self.NEW_DIGEST, "coa-dev-vkg-ns2": self.NEW_DIGEST})
        mock_ecr = MagicMock()
        self._tag_points_at(mock_ecr, self.NEW_DIGEST)
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert result["status"] == "sweep_complete"
        assert result["total"] == 2
        assert result["triggered"] == 0
        assert result["unchanged"] == 2
        assert {r["status"] for r in result["results"]} == {"unchanged"}
        mock_ecs.update_service.assert_not_called()
        mock_ecs.register_task_definition.assert_not_called()
        mock_ecs.create_service.assert_not_called()
        # One registry lookup for the whole sweep, not one per service.
        mock_ecr.describe_images.assert_called_once()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_moves_only_the_stale_services(self, mock_ssm, mock_sd, mock_ecs):
        """In a mixed cluster only the service on an old image gets a new revision and deployment."""
        new_image = self.REUSED_TAG.replace("vkg-latest", "vkg-new")
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": new_image}}
        self._services(mock_ecs, "fresh", "stale")
        mock_ecs.describe_services.side_effect = lambda cluster, services: {
            "services": [{"taskDefinition": f"arn:td:{services[0]}"}]
        }
        mock_ecs.describe_task_definition.side_effect = lambda taskDefinition: {
            "taskDefinition": {
                "containerDefinitions": [{"image": new_image if taskDefinition.endswith("fresh") else "img:old"}]
            }
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:stale-rev"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d"}]}}
        self._running(mock_ecs, {"coa-dev-vkg-fresh": self.NEW_DIGEST})
        mock_ecr = MagicMock()
        self._tag_points_at(mock_ecr, self.NEW_DIGEST)
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert {r["namespace"]: r["status"] for r in result["results"]} == {
            "fresh": "unchanged",
            "stale": "reload_triggered",
        }
        assert result["triggered"] == 1
        assert result["unchanged"] == 1
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-stale",
            taskDefinition="arn:td:stale-rev",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_deploy_trigger_runs_the_sweep_against_the_ssm_image(self, mock_ssm, mock_sd, mock_ecs):
        """The deploy-time payload runs the sweep; the target image still comes from SSM, not the payload."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:from-ssm"}}
        self._services(mock_ecs, "ns1")
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d"}]}}
        index = self._load_index(mock_ssm, mock_sd, mock_ecs)

        result = index.handler({"sweep": True, "trigger": "deploy", "image": "img:from-payload"}, None)

        assert result["status"] == "sweep_complete"
        assert result["triggered"] == 1
        assert result["unchanged"] == 0
        container = mock_ecs.register_task_definition.call_args.kwargs["containerDefinitions"][0]
        assert container["image"] == "img:from-ssm"
        mock_ssm.get_parameter.assert_called_once_with(Name="/coa/vkg/container-image")

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_deploy_trigger_with_unchanged_images_restarts_nothing(self, mock_ssm, mock_sd, mock_ecs):
        """A deploy-time sweep where every service already runs the tag's digest is a pure no-op."""
        self._same_uri_everywhere(mock_ssm, mock_ecs, self.REUSED_TAG, "ns1", "ns2", "ns3")
        self._running(mock_ecs, {f"coa-dev-vkg-ns{i}": self.NEW_DIGEST for i in (1, 2, 3)})
        mock_ecr = MagicMock()
        self._tag_points_at(mock_ecr, self.NEW_DIGEST)
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True, "trigger": "deploy"}, None)

        assert result["unchanged"] == 3
        mock_ecs.update_service.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_restarts_a_reused_tag_that_points_at_a_new_build(self, mock_ssm, mock_sd, mock_ecs):
        """Same URI text, new digest behind the tag: the service is restarted so ECS pulls the new build."""
        self._same_uri_everywhere(mock_ssm, mock_ecs, self.REUSED_TAG, "ns1")
        self._running(mock_ecs, {"coa-dev-vkg-ns1": self.OLD_DIGEST})
        mock_ecr = MagicMock()
        self._tag_points_at(mock_ecr, self.NEW_DIGEST)
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert result["results"] == [{"namespace": "ns1", "status": "reload_triggered"}]
        assert result["triggered"] == 1
        assert result["unchanged"] == 0
        mock_ecr.describe_images.assert_called_once_with(
            registryId="111111111111", repositoryName="scl", imageIds=[{"imageTag": "vkg-latest"}]
        )
        mock_ecs.list_tasks.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"], serviceName="coa-dev-vkg-ns1", desiredStatus="RUNNING"
        )
        # Same URI -> no new revision, just a fresh deployment that re-resolves the tag.
        mock_ecs.register_task_definition.assert_not_called()
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-ns1",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_restarts_when_any_running_task_has_another_digest(self, mock_ssm, mock_sd, mock_ecs):
        """One task on the old digest is enough to roll the service."""
        self._same_uri_everywhere(mock_ssm, mock_ecs, self.REUSED_TAG, "ns1")
        mock_ecs.list_tasks.return_value = {"taskArns": ["t1", "t2"]}
        mock_ecs.describe_tasks.return_value = {
            "tasks": [
                {"containers": [{"imageDigest": self.NEW_DIGEST}]},
                {"containers": [{"imageDigest": self.OLD_DIGEST}]},
            ]
        }
        mock_ecr = MagicMock()
        self._tag_points_at(mock_ecr, self.NEW_DIGEST)
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert result["triggered"] == 1
        mock_ecs.update_service.assert_called_once()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_digest_pinned_uri_needs_no_registry_lookup(self, mock_ssm, mock_sd, mock_ecs):
        """A URI pinned with @sha256: is its own digest: equal -> unchanged, without calling ECR."""
        pinned = f"111111111111.dkr.ecr.us-west-2.amazonaws.com/scl@{self.NEW_DIGEST}"
        self._same_uri_everywhere(mock_ssm, mock_ecs, pinned, "same", "other")
        self._running(mock_ecs, {"coa-dev-vkg-same": self.NEW_DIGEST, "coa-dev-vkg-other": self.OLD_DIGEST})
        mock_ecr = MagicMock()
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert {r["namespace"]: r["status"] for r in result["results"]} == {
            "same": "unchanged",
            "other": "reload_triggered",
        }
        mock_ecr.describe_images.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_falls_back_to_force_new_deployment_when_the_registry_lookup_fails(self, mock_ssm, mock_sd, mock_ecs):
        """ECR error -> digest unknown -> restart as before; a lookup failure never stops a rollout."""
        self._same_uri_everywhere(mock_ssm, mock_ecs, self.REUSED_TAG, "ns1")
        self._running(mock_ecs, {"coa-dev-vkg-ns1": self.NEW_DIGEST})
        mock_ecr = MagicMock()
        mock_ecr.describe_images.side_effect = Exception("AccessDeniedException")
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert result["results"] == [{"namespace": "ns1", "status": "reload_triggered"}]
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-ns1",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_falls_back_to_force_new_deployment_when_task_digests_are_unknown(self, mock_ssm, mock_sd, mock_ecs):
        """No running task, an ECS error, or a task without a digest -> restart as before."""
        self._same_uri_everywhere(mock_ssm, mock_ecs, self.REUSED_TAG, "none", "err", "nodigest")

        def list_tasks(cluster, serviceName, desiredStatus):
            if serviceName.endswith("err"):
                raise Exception("ThrottlingException")
            return {"taskArns": [] if serviceName.endswith("none") else ["t1"]}

        mock_ecs.list_tasks.side_effect = list_tasks
        mock_ecs.describe_tasks.return_value = {"tasks": [{"containers": [{"name": "ontop"}]}]}
        mock_ecr = MagicMock()
        self._tag_points_at(mock_ecr, self.NEW_DIGEST)
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert {r["status"] for r in result["results"]} == {"reload_triggered"}
        assert result["unchanged"] == 0
        assert mock_ecs.update_service.call_count == 3

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_restarts_when_the_image_is_not_in_ecr(self, mock_ssm, mock_sd, mock_ecs):
        """A non-ECR image's digest cannot be resolved, so the service is restarted as before."""
        self._same_uri_everywhere(mock_ssm, mock_ecs, "public.example/vkg:latest", "ns1")
        self._running(mock_ecs, {"coa-dev-vkg-ns1": self.NEW_DIGEST})
        mock_ecr = MagicMock()
        index = self._load_index(mock_ssm, mock_sd, mock_ecs, mock_ecr)

        result = index.handler({"sweep": True}, None)

        assert result["triggered"] == 1
        mock_ecr.describe_images.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_ontology_publish_still_restarts_a_service_on_the_latest_image(self, mock_ssm, mock_sd, mock_ecs):
        """The skip is sweep-only: a publish must restart the task so it loads the new mappings."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:same"}}
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:current"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:same"}]}
        }
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d"}]}}
        index = self._load_index(mock_ssm, mock_sd, mock_ecs)

        result = index.handler(_make_event(), None)

        assert result["status"] == "reload_triggered"
        mock_ecs.update_service.assert_called_once_with(
            cluster=ENV["CLUSTER_ARN"],
            service="coa-dev-vkg-test-ns",
            forceNewDeployment=True,
            deploymentConfiguration=index._DEPLOY_CONFIG,
        )

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_continues_after_single_namespace_failure(self, mock_ssm, mock_sd, mock_ecs):
        """One namespace failing must not abort the others."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "serviceArns": [
                    "arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns1",
                    "arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns2",
                ]
            }
        ]
        mock_ecs.get_paginator.return_value = paginator
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev"}}
        mock_ecs.update_service.side_effect = [
            {"service": {"deployments": [{"id": "d1"}]}},  # ns1 OK
            RuntimeError("ecs boom"),  # ns2 fails
        ]

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)

        assert result["status"] == "sweep_complete"
        assert result["total"] == 2
        assert result["triggered"] == 1
        assert sorted(r["status"] for r in result["results"]) == ["failed", "reload_triggered"]
        # the failed namespace still emitted a ReloadFailed metric
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(os.environ, {**ENV, "VKG_IMAGE_PARAM_NAME": ""})
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_fails_fast_when_image_unresolvable(self, mock_ssm, mock_sd, mock_ecs):
        """If the SSM image can't be resolved, the sweep fails without listing services."""
        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)

        assert result["status"] == "failed"
        assert "container image" in result["reason"]
        mock_ecs.get_paginator.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_no_services_is_a_clean_noop(self, mock_ssm, mock_sd, mock_ecs):
        """A cluster with no VKG services completes as an empty sweep (no reloads)."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [{"serviceArns": []}]
        mock_ecs.get_paginator.return_value = paginator

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)
        assert result["status"] == "sweep_complete"
        assert result["total"] == 0
        assert result["triggered"] == 0
        mock_ecs.update_service.assert_not_called()

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_walks_paginated_list_services(self, mock_ssm, mock_sd, mock_ecs):
        """_list_vkg_namespaces must reconcile services across every ListServices page."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"serviceArns": ["arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns1"]},
            {"serviceArns": ["arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns2"]},
        ]
        mock_ecs.get_paginator.return_value = paginator
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "d"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)
        assert result["total"] == 2
        assert {r["namespace"] for r in result["results"]} == {"ns1", "ns2"}
        assert mock_ecs.update_service.call_count == 2

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_reports_all_failures_without_raising(self, mock_ssm, mock_sd, mock_ecs):
        """When every namespace fails, the sweep still completes with all failures recorded."""
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "serviceArns": [
                    "arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns1",
                    "arn:aws:ecs:us-west-2:111:service/vkg/coa-dev-vkg-ns2",
                ]
            }
        ]
        mock_ecs.get_paginator.return_value = paginator
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "img:old"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:rev"}}
        mock_ecs.update_service.side_effect = RuntimeError("ecs boom")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)
        assert result["status"] == "sweep_complete"
        assert result["total"] == 2
        assert result["triggered"] == 0
        assert all(r["status"] == "failed" for r in result["results"])

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_sweep_handles_list_services_failure(self, mock_ssm, mock_sd, mock_ecs):
        """A ListServices failure ends the sweep cleanly (failed + ReloadFailed), not an uncaught raise."""
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "img:new"}}
        paginator = MagicMock()
        paginator.paginate.side_effect = RuntimeError("Throttling: Rate exceeded")
        mock_ecs.get_paginator.return_value = paginator

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler({"sweep": True}, None)
        assert result["status"] == "failed"
        assert "list services failed" in result["reason"]
        mock_ecs.update_service.assert_not_called()
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics

    @patch.dict(
        os.environ,
        {**ENV, "VKG_TASK_CPU": "2048", "VKG_TASK_MEMORY": "4096", "VKG_ONTOP_JAVA_ARGS": "-Xmx3072m -Xms1024m"},
    )
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_task_sizing_env_threaded_into_task_definition(self, mock_ssm, mock_sd, mock_ecs):
        """#149 cause B/C: reload reads VKG_TASK_CPU/MEMORY/ONTOP_JAVA_ARGS and

        threads them into the registered task def, so a reloaded task matches the
        CDK-provisioned initial one instead of the old hardcoded 512/1024 with a
        dead JAVA_OPTS. Guards against a regression that would silently
        under-provision and never apply the heap.
        """
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        td_kwargs = mock_ecs.register_task_definition.call_args.kwargs
        assert td_kwargs["cpu"] == "2048"
        assert td_kwargs["memory"] == "4096"
        env = {e["name"]: e["value"] for e in td_kwargs["containerDefinitions"][0]["environment"]}
        assert env["ONTOP_JAVA_ARGS"] == "-Xmx3072m -Xms1024m"
        # The dead variable must NOT be emitted — that was the #149 cause C no-op.
        assert "JAVA_OPTS" not in env

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_task_sizing_defaults_match_cdk_when_env_unset(self, mock_ssm, mock_sd, mock_ecs):
        """With the sizing env unset, the reload falls back to defaults that match

        the CDK stack (cpu=1024/memory=2048, -Xmx1536m -Xms512m), so an
        un-parameterised reload is still safe rather than the old 512/1024.
        """
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        td_kwargs = mock_ecs.register_task_definition.call_args.kwargs
        assert td_kwargs["cpu"] == "1024"
        assert td_kwargs["memory"] == "2048"
        env = {e["name"]: e["value"] for e in td_kwargs["containerDefinitions"][0]["environment"]}
        assert env["ONTOP_JAVA_ARGS"] == "-Xmx1536m -Xms512m"

    @patch.dict(os.environ, {**ENV, "VKG_TASK_MEMORY": "8192"}, clear=False)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_heap_derived_from_task_memory_when_java_args_unset(self, mock_ssm, mock_sd, mock_ecs):
        """When VKG_ONTOP_JAVA_ARGS is unset, the heap is derived from
        VKG_TASK_MEMORY (max heap ~=75%, initial ~=25%) so bumping memory alone
        scales the heap in step rather than leaving a stale hardcoded -Xmx.
        """
        # Ensure no explicit heap override leaks in from a broader ENV.
        os.environ.pop("VKG_ONTOP_JAVA_ARGS", None)
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-NEW"}
        }
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-OLD"}]}
        }
        mock_ecs.register_task_definition.return_value = {"taskDefinition": {"taskDefinitionArn": "arn:td:ns-rev2"}}
        mock_ecs.update_service.return_value = {"service": {"deployments": [{"id": "deploy-1"}]}}

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "reload_triggered"
        td_kwargs = mock_ecs.register_task_definition.call_args.kwargs
        assert td_kwargs["memory"] == "8192"
        env = {e["name"]: e["value"] for e in td_kwargs["containerDefinitions"][0]["environment"]}
        # 8192 * 3//4 = 6144, 8192 // 4 = 2048
        assert env["ONTOP_JAVA_ARGS"] == "-Xmx6144m -Xms2048m"

    @patch.dict(os.environ, ENV)
    @patch("index.ecs")
    @patch("index.sd")
    @patch("index.ssm")
    def test_register_task_definition_failure_fails_cleanly_without_creating_service(self, mock_ssm, mock_sd, mock_ecs):
        """If register_task_definition raises during a provision, the handler

        returns failed and never calls create_service, rather than letting the
        exception propagate and orphan the just-created Cloud Map entry.
        """
        mock_ecs.exceptions.ServiceNotFoundException = type("ServiceNotFoundException", (Exception,), {})
        mock_ecs.exceptions.ServiceNotActiveException = type("ServiceNotActiveException", (Exception,), {})
        mock_ecs.exceptions.InvalidParameterException = type("InvalidParameterException", (Exception,), {})
        same_image = "111.dkr.ecr.us-west-2.amazonaws.com/scl:vkg-abc"
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": same_image}}
        # Same image so the reload path takes the unchanged branch (no task-def
        # registration there); update_service then raises ServiceNotFound so the
        # handler routes to the provision path, where _register_task_definition
        # is called and we force it to fail.
        mock_ecs.describe_services.return_value = {"services": [{"taskDefinition": "arn:td:old"}]}
        mock_ecs.describe_task_definition.return_value = {
            "taskDefinition": {"containerDefinitions": [{"image": same_image}]}
        }
        mock_ecs.update_service.side_effect = mock_ecs.exceptions.ServiceNotFoundException("not found")
        mock_sd.create_service.return_value = {"Service": {"Arn": "arn:sd:svc"}}
        mock_sd.exceptions.ServiceAlreadyExists = type("ServiceAlreadyExists", (Exception,), {})
        mock_ecs.register_task_definition.side_effect = RuntimeError("ThrottlingException")

        import importlib

        import index

        importlib.reload(index)
        index.ecs = mock_ecs
        index.sd = mock_sd
        index.ssm = mock_ssm

        result = index.handler(_make_event(), None)
        assert result["status"] == "failed"
        assert "register_task_definition failed" in result["reason"]
        mock_ecs.create_service.assert_not_called()
        metrics = [c.kwargs["MetricData"][0]["MetricName"] for c in index.cloudwatch.put_metric_data.call_args_list]
        assert "ReloadFailed" in metrics
