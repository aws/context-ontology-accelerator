# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""MCP Server configuration — extends shared SCLConfig."""

import os

from coa_common.config import resolve_region
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


class MCPConfig(BaseSettings):
    """Configuration for the MCP Server.

    Values loaded from environment variables with ``SCL_`` prefix.
    Pydantic BaseSettings reads env vars automatically — for example,
    ``aws_region`` is resolved from the runtime environment only when the
    ``SCL_AWS_REGION`` environment variable is not set. When the env var is
    present, its value takes precedence over the default.
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
    # MCP tool in any other region (#92). default_factory keeps the pydantic
    # env lookup precedence intact while making the fallback region-aware.
    aws_region: str = Field(default_factory=resolve_region)

    # Neptune (for discovery tools)
    neptune_endpoint: str = ""
    neptune_port: int = 8182

    # Fuseki local dev mode (bypasses SigV4, uses plain HTTP)
    use_fuseki: bool = False
    fuseki_url: str = "http://localhost:3030/coa/query"

    # MCP Server
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    # Auth. False is a local-dev switch only; the validator below refuses it
    # inside the deployed container.
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
