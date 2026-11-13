# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The one Bedrock model configuration every agent in this package uses."""

from __future__ import annotations

from botocore.config import Config as BotoConfig
from strands.models import BedrockModel

from coa_workflow_agents.config import WorkflowAgentsConfig

# Bounded, adaptive retries, so a Bedrock outage fails within a few attempts under the run deadline.
_BEDROCK_BOTO_CONFIG = BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"})


def build_bedrock_model(config: WorkflowAgentsConfig) -> BedrockModel:
    """Build a ``BedrockModel`` from ``config``.

    Unset settings are omitted, not sent as ``None``: the SDK applies its default model only
    when ``model_id`` is absent, and newer models reject ``temperature``. A configured
    guardrail screens the messages sent to the model, not tool results, so it is not the
    control on what a run may do; the ``confirm_mutations`` gate is.
    """
    model_config: BedrockModel.BedrockConfig = {}
    if config.bedrock_temperature is not None:
        model_config["temperature"] = config.bedrock_temperature
    if config.bedrock_model_id:
        model_config["model_id"] = config.bedrock_model_id
    if config.guardrail_id:
        model_config["guardrail_id"] = config.guardrail_id
        model_config["guardrail_version"] = config.guardrail_version or "DRAFT"
        model_config["guardrail_trace"] = "enabled"
    return BedrockModel(region_name=config.aws_region, boto_client_config=_BEDROCK_BOTO_CONFIG, **model_config)
