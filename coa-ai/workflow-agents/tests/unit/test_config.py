# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""WorkflowAgentsConfig: env var names, defaults and the required-settings check."""

from __future__ import annotations

import pytest
from coa_workflow_agents.config import WorkflowAgentsConfig
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_ENV = (
    "SCL_API_ENDPOINT",
    "JWT_ISSUER_URL",
    "JWT_CLIENT_ID",
    "GROUP_CLAIM_NAME",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "SCL_MCP_HOST",
    "SCL_MCP_PORT",
    "SCL_RUN_DEADLINE_S",
    "SCL_BEDROCK_MODEL_ID",
    "SCL_BEDROCK_TEMPERATURE",
    "SCL_GUARDRAIL_ID",
    "SCL_GUARDRAIL_VERSION",
    "SCL_ALLOW_CORS_ORIGINS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def test_validate_required_lists_every_missing_variable():
    with pytest.raises(RuntimeError) as exc:
        WorkflowAgentsConfig().validate_required()
    for name in ("SCL_API_ENDPOINT", "JWT_ISSUER_URL", "JWT_CLIENT_ID"):
        assert name in str(exc.value)


def test_validate_required_passes_when_set(monkeypatch):
    monkeypatch.setenv("SCL_API_ENDPOINT", "https://coa.example.com")
    monkeypatch.setenv("JWT_ISSUER_URL", "https://issuer.example.com")
    monkeypatch.setenv("JWT_CLIENT_ID", "mcp-client")
    WorkflowAgentsConfig().validate_required()


def test_validate_required_lists_only_missing(monkeypatch):
    monkeypatch.setenv("SCL_API_ENDPOINT", "https://coa.example.com")
    monkeypatch.setenv("JWT_ISSUER_URL", "https://issuer.example.com")
    with pytest.raises(RuntimeError) as exc:
        WorkflowAgentsConfig().validate_required()
    assert "JWT_CLIENT_ID" in str(exc.value)
    assert "SCL_API_ENDPOINT" not in str(exc.value)


def test_env_var_names_follow_the_contract(monkeypatch):
    env = {
        "SCL_API_ENDPOINT": "https://coa.example.com",
        "JWT_ISSUER_URL": "https://issuer.example.com",
        "JWT_CLIENT_ID": "mcp-client",
        "GROUP_CLAIM_NAME": "roles",
        "AWS_REGION": "eu-west-1",
        "SCL_MCP_HOST": "0.0.0.0",
        "SCL_MCP_PORT": "9000",
        "SCL_RUN_DEADLINE_S": "600",
        "SCL_BEDROCK_MODEL_ID": "model-x",
        "SCL_BEDROCK_TEMPERATURE": "0.5",
        "SCL_GUARDRAIL_ID": "gr-1",
        "SCL_GUARDRAIL_VERSION": "3",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config = WorkflowAgentsConfig()
    assert (config.api_endpoint, config.jwt_issuer_url, config.jwt_client_id) == (
        "https://coa.example.com",
        "https://issuer.example.com",
        "mcp-client",
    )
    assert (config.group_claim_name, config.aws_region) == ("roles", "eu-west-1")
    assert (config.mcp_host, config.mcp_port, config.run_deadline_s) == ("0.0.0.0", 9000, 600.0)
    assert (config.bedrock_model_id, config.bedrock_temperature) == ("model-x", 0.5)
    assert (config.guardrail_id, config.guardrail_version) == ("gr-1", "3")


def test_defaults():
    config = WorkflowAgentsConfig()
    assert config.aws_region == "us-east-1"
    assert (config.mcp_host, config.mcp_port) == ("127.0.0.1", 8000)
    assert config.run_deadline_s == 3300.0
    assert config.log_level == "INFO"
    assert config.group_claim_name == "groups"
    assert (config.bedrock_model_id, config.bedrock_temperature) == ("", None)
    assert (config.guardrail_id, config.guardrail_version) == ("", "")
    assert config.cors_origins == ()


def test_aws_region_falls_back_to_aws_default_region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    assert WorkflowAgentsConfig().aws_region == "ap-south-1"


@pytest.mark.parametrize(("given", "level"), [("debug", "DEBUG"), ("WARN", "WARNING"), ("fatal", "CRITICAL")])
def test_log_level_is_read_from_bare_log_level_and_normalised(monkeypatch, given, level):
    monkeypatch.setenv("LOG_LEVEL", given)
    assert WorkflowAgentsConfig().log_level == level


def test_an_unknown_log_level_is_refused(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    with pytest.raises(ValidationError):
        WorkflowAgentsConfig()


def test_cors_origins_are_read_split_and_trimmed(monkeypatch):
    monkeypatch.setenv("SCL_ALLOW_CORS_ORIGINS", " http://127.0.0.1:3000 , http://localhost:5173,http://[::1]:8080,")
    assert WorkflowAgentsConfig().cors_origins == (
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://[::1]:8080",
    )


@pytest.mark.parametrize("value", ["*", "http://127.0.0.1:3000,*", "http://*.example.com"])
def test_a_wildcard_cors_origin_is_refused(monkeypatch, value):
    monkeypatch.setenv("SCL_ALLOW_CORS_ORIGINS", value)
    with pytest.raises(ValidationError, match="'\\*' is refused"):
        WorkflowAgentsConfig()


@pytest.mark.parametrize(
    "value",
    [
        # Not an origin at all.
        "null",
        "127.0.0.1:3000",
        "file://",
        "ftp://host",
        # FastMCP's DNS-rebinding check refuses these on a loopback bind.
        "https://127.0.0.1:3000",
        "https://dev.example.com",
        "http://dev.example.com:3000",
        "http://127.0.0.1.example.com:3000",
        # Never what a browser sends, so it could never match.
        "http://LOCALHOST:3000",
        "HTTP://127.0.0.1:3000",
        "http://127.0.0.1",
        "http://127.0.0.1:80",
        "http://127.0.0.1:0",
        "http://127.0.0.1:03000",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:abc",
        "http://127.0.0.1:３０００",
        "http://u:p@127.0.0.1:3000",
        "http://::1:3000",
        "http://127.0.0.1:3000/",
        "http://127.0.0.1:3000?q",
        "http://127.0.0.1:3000#f",
    ],
)
def test_a_cors_entry_that_is_not_a_loopback_origin_is_refused(monkeypatch, value):
    monkeypatch.setenv("SCL_ALLOW_CORS_ORIGINS", value)
    with pytest.raises(ValidationError, match="is not a loopback origin"):
        WorkflowAgentsConfig()
