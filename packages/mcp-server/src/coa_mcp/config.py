# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""MCP Server configuration."""

import os

from coa_common.config import resolve_region
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class MCPConfig(BaseSettings):
    """Configuration for the MCP Server.

    Values are loaded from environment variables with the ``SCL_`` prefix.
    """

    model_config = {"env_prefix": "SCL_"}

    # General
    environment: str = "dev"
    log_level: str = "INFO"

    # AWS — SCL_AWS_REGION wins when set; otherwise resolve from the runtime
    # environment (AWS_REGION, then AWS_DEFAULT_REGION, then us-east-1).
    # Nothing in infra/ sets SCL_AWS_REGION — the MCP stack sets AWS_REGION —
    # so a hardcoded default here pinned every deployment's downstream calls
    # (Lambda invokes, Context Manager endpoint) to us-east-1 and broke every
    # MCP tool in any other region. default_factory keeps the pydantic
    # env lookup precedence intact while making the fallback region-aware.
    aws_region: str = Field(default_factory=resolve_region)

    # MCP Server
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # Auth
    auth_enabled: bool = True

    @model_validator(mode="after")
    def _refuse_auth_disabled_in_container(self) -> "MCPConfig":
        # SCL_MCP_MODE=true is what makes the shared container image start the MCP
        # server (packages/context-manager/Dockerfile), so it marks every deployed run.
        if not self.auth_enabled and os.environ.get("SCL_MCP_MODE") == "true":
            raise ValueError(
                "SCL_AUTH_ENABLED=false is for local development only and cannot be used "
                "with SCL_MCP_MODE=true (the deployed MCP runtime)."
            )
        return self
