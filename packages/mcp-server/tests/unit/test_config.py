# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for MCPConfig environment-variable loading.

MCPConfig is a pydantic BaseSettings that reads SCL_-prefixed env vars.
These tests verify defaults and that env vars override them.
"""

from __future__ import annotations

import pytest
from coa_mcp.config import MCPConfig


@pytest.mark.unit
class TestMCPConfig:
    def test_defaults_when_no_env(self, monkeypatch):
        for var in (
            "SCL_ENVIRONMENT",
            "SCL_LOG_LEVEL",
            "SCL_AWS_REGION",
            "SCL_MCP_HOST",
            "SCL_MCP_PORT",
            "SCL_AUTH_ENABLED",
            "SCL_USE_FUSEKI",
            # aws_region now falls back to the runtime environment
            # (resolve_region), so these must be cleared too or the test
            # inherits the host machine's region.
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
        ):
            monkeypatch.delenv(var, raising=False)
        config = MCPConfig()
        assert config.environment == "dev"
        assert config.log_level == "INFO"
        assert config.aws_region == "us-east-1"
        assert config.mcp_host == "0.0.0.0"
        assert config.mcp_port == 8000
        assert config.auth_enabled is True
        assert config.use_fuseki is False

    def test_aws_region_env_override(self, monkeypatch):
        monkeypatch.setenv("SCL_AWS_REGION", "eu-central-1")
        assert MCPConfig().aws_region == "eu-central-1"

    def test_aws_region_follows_runtime_environment(self, monkeypatch):
        # #92 regression: the MCP stack sets AWS_REGION (not SCL_AWS_REGION),
        # so the config must resolve the deploy region from the runtime
        # environment. Before the fix this returned the hardcoded us-east-1
        # and every MCP tool failed in any other region.
        monkeypatch.delenv("SCL_AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert MCPConfig().aws_region == "us-west-2"

    def test_aws_region_falls_back_to_aws_default_region(self, monkeypatch):
        monkeypatch.delenv("SCL_AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
        assert MCPConfig().aws_region == "ap-northeast-1"

    def test_scl_aws_region_takes_precedence_over_runtime(self, monkeypatch):
        # The SCL_ prefixed var stays an explicit operator override (the
        # documented workaround for #92 keeps working after the fix).
        monkeypatch.setenv("SCL_AWS_REGION", "eu-west-1")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        assert MCPConfig().aws_region == "eu-west-1"

    def test_auth_enabled_env_override(self, monkeypatch):
        monkeypatch.delenv("SCL_MCP_MODE", raising=False)
        monkeypatch.setenv("SCL_AUTH_ENABLED", "false")
        assert MCPConfig().auth_enabled is False

    def test_mcp_port_env_override_coerced_to_int(self, monkeypatch):
        monkeypatch.setenv("SCL_MCP_PORT", "9001")
        config = MCPConfig()
        assert config.mcp_port == 9001
        assert isinstance(config.mcp_port, int)


@pytest.mark.unit
class TestAuthDisabledGuard:
    """SCL_AUTH_ENABLED=false is a local-dev switch: it must never take effect in the container."""

    @pytest.mark.parametrize("disabled", ["false", "0", "off", "FALSE"])
    def test_auth_disabled_in_mcp_container_refuses_to_start(self, monkeypatch, disabled):
        # The container entrypoint runs the MCP server only when SCL_MCP_MODE=true, so
        # this pair is a deployed runtime with auth switched off.
        monkeypatch.setenv("SCL_MCP_MODE", "true")
        monkeypatch.setenv("SCL_AUTH_ENABLED", disabled)
        with pytest.raises(ValueError, match="SCL_AUTH_ENABLED=false"):
            MCPConfig()

    def test_auth_enabled_in_mcp_container_is_accepted(self, monkeypatch):
        monkeypatch.setenv("SCL_MCP_MODE", "true")
        monkeypatch.delenv("SCL_AUTH_ENABLED", raising=False)
        assert MCPConfig().auth_enabled is True

    @pytest.mark.parametrize("mcp_mode", [None, "false", ""])
    def test_auth_disabled_outside_container_still_allowed(self, monkeypatch, mcp_mode):
        if mcp_mode is None:
            monkeypatch.delenv("SCL_MCP_MODE", raising=False)
        else:
            monkeypatch.setenv("SCL_MCP_MODE", mcp_mode)
        monkeypatch.setenv("SCL_AUTH_ENABLED", "false")
        assert MCPConfig().auth_enabled is False
