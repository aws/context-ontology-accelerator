# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared Bedrock model builder.

Guards against regressing to every agent builder instantiating a bare
``BedrockModel()`` with zero explicit configuration — these tests confirm
the config values actually reach the constructed model, and that a retry
policy is set rather than left to whatever boto3 defaults.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from coa_workflow_agents.agents.model import build_bedrock_model
from coa_workflow_agents.config import WorkflowAgentsConfig

pytestmark = pytest.mark.unit


def test_build_bedrock_model_omits_model_id_when_config_is_empty():
    """Empty bedrock_model_id -> model_id is NOT passed to BedrockModel's
    constructor at all (verified via the kwargs build_bedrock_model
    assembles), so Strands' own provider default applies — matching the
    coa_serve.agents.sql_agent's convention (model_id=None)
    rather than this package hardcoding a specific Claude version."""
    config = WorkflowAgentsConfig(bedrock_model_id="", aws_region="us-east-1")
    with patch("coa_workflow_agents.agents.model.BedrockModel") as mock_bedrock_model:
        build_bedrock_model(config)
    _, kwargs = mock_bedrock_model.call_args
    assert "model_id" not in kwargs


def test_build_bedrock_model_sets_the_configured_model_id():
    config = WorkflowAgentsConfig(
        bedrock_model_id="us.anthropic.claude-3-5-sonnet-20241022-v2:0", aws_region="us-east-1"
    )
    model = build_bedrock_model(config)
    resolved = model.get_config()
    assert resolved.get("model_id") == "us.anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_build_bedrock_model_sets_the_configured_temperature():
    config = WorkflowAgentsConfig(bedrock_model_id="", bedrock_temperature=0.7, aws_region="us-east-1")
    model = build_bedrock_model(config)
    resolved = model.get_config()
    assert resolved.get("temperature") == 0.7


def test_build_bedrock_model_omits_temperature_when_unset():
    """Newer models (e.g. claude-sonnet-5) reject `temperature` with a ValidationException."""
    config = WorkflowAgentsConfig(bedrock_model_id="", aws_region="us-east-1")
    with patch("coa_workflow_agents.agents.model.BedrockModel") as mock_bedrock_model:
        build_bedrock_model(config)
    _, kwargs = mock_bedrock_model.call_args
    assert "temperature" not in kwargs


def test_build_bedrock_model_sets_a_bounded_retry_policy_not_the_implicit_default():
    config = WorkflowAgentsConfig(bedrock_model_id="", aws_region="us-east-1")
    model = build_bedrock_model(config)
    retries = model.client.meta.config.retries
    # botocore normalizes max_attempts (which counts the initial try) into
    # total_max_attempts internally — 5 retries configured -> 6 total tries.
    assert retries["mode"] == "adaptive"
    assert retries["total_max_attempts"] == 6


def test_build_bedrock_model_uses_the_configured_region():
    config = WorkflowAgentsConfig(bedrock_model_id="", aws_region="eu-west-1")
    model = build_bedrock_model(config)
    assert model.client.meta.region_name == "eu-west-1"


def test_build_bedrock_model_applies_the_configured_guardrail():
    config = WorkflowAgentsConfig(
        bedrock_model_id="", aws_region="us-east-1", guardrail_id="gr-123", guardrail_version="2"
    )
    resolved = build_bedrock_model(config).get_config()
    assert resolved.get("guardrail_id") == "gr-123"
    assert resolved.get("guardrail_version") == "2"
    assert resolved.get("guardrail_trace") == "enabled"


def test_build_bedrock_model_falls_back_to_the_draft_guardrail_version():
    config = WorkflowAgentsConfig(
        bedrock_model_id="", aws_region="us-east-1", guardrail_id="gr-123", guardrail_version=""
    )
    resolved = build_bedrock_model(config).get_config()
    assert resolved.get("guardrail_id") == "gr-123"
    assert resolved.get("guardrail_version") == "DRAFT"


def test_build_bedrock_model_sends_guardrail_config_on_converse():
    """The SDK only adds guardrailConfig when both id and version are set; check the request it builds."""
    config = WorkflowAgentsConfig(
        bedrock_model_id="m", aws_region="us-east-1", guardrail_id="gr-123", guardrail_version="2"
    )
    request = build_bedrock_model(config).format_request([{"role": "user", "content": [{"text": "hi"}]}])
    assert request["guardrailConfig"]["guardrailIdentifier"] == "gr-123"
    assert request["guardrailConfig"]["guardrailVersion"] == "2"


def test_build_bedrock_model_omits_guardrail_when_unset():
    config = WorkflowAgentsConfig(bedrock_model_id="", aws_region="us-east-1")
    with patch("coa_workflow_agents.agents.model.BedrockModel") as mock_bedrock_model:
        build_bedrock_model(config)
    _, kwargs = mock_bedrock_model.call_args
    assert not {"guardrail_id", "guardrail_version", "guardrail_trace"} & kwargs.keys()
