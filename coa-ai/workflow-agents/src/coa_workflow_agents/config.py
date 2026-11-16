# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Configuration for the workflow-agents MCP server, read from the environment.

The server's own settings use the ``SCL_`` prefix. The identity and platform settings
(``JWT_ISSUER_URL``, ``JWT_CLIENT_ID``, ``GROUP_CLAIM_NAME``, ``AWS_REGION``, ``LOG_LEVEL``)
use their bare names, as the other COA services do.
"""

from __future__ import annotations

from typing import Literal

from coa_common.config import resolve_region
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_LOOPBACK_NETLOCS = ("127.0.0.1", "localhost", "[::1]")


def _is_loopback_origin(origin: str) -> bool:
    """True for exactly ``http://{127.0.0.1|localhost|[::1]}:<port>``, port 1-65535 and not 80."""
    scheme, _, rest = origin.partition("://")
    host, _, port = rest.rpartition(":")
    return (
        scheme == "http"
        and host in _LOOPBACK_NETLOCS
        and port.isascii()
        and port.isdigit()
        and not port.startswith("0")
        and 1 <= int(port) <= 65535
        and port != "80"
    )


class WorkflowAgentsConfig(BaseSettings):
    """Runtime configuration for the workflow-agents server."""

    model_config = {"env_prefix": "SCL_", "populate_by_name": True}

    # ── COA ──────────────────────────────────────────────────────────
    # Base URL of the COA Control Plane REST API.
    api_endpoint: str = ""

    # ── Inbound identity ─────────────────────────────────────────────
    # The IdP issuer every caller token must carry.
    jwt_issuer_url: str = Field(default="", validation_alias="JWT_ISSUER_URL")
    # The COA web app client id (SSM /<prefix>/userpool-client-id): the only accepted
    # inbound audience, because the REST API accepts only that client's tokens.
    jwt_client_id: str = Field(default="", validation_alias="JWT_CLIENT_ID")
    # Group claim for non-Cognito IdPs (Cognito always uses cognito:groups).
    group_claim_name: str = Field(default="groups", validation_alias="GROUP_CLAIM_NAME")

    # ── AWS ──────────────────────────────────────────────────────────
    aws_region: str = Field(default_factory=resolve_region, validation_alias="AWS_REGION")

    # ── MCP server ───────────────────────────────────────────────────
    # Loopback by default. The container sets 0.0.0.0, so the published port is the
    # boundary there: publish it to 127.0.0.1 only (see the Dockerfile).
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    # Comma-separated loopback browser origins allowed to call the server cross-origin
    # (a local browser MCP client). Empty -> no CORS middleware. Anything else is refused.
    allow_cors_origins: str = ""
    # Case-insensitive; WARN and FATAL mean WARNING and CRITICAL.
    log_level: LogLevel = Field(default="INFO", validation_alias="LOG_LEVEL")

    # ── Run limits ───────────────────────────────────────────────────
    # The run's deadline for polls; the call's backstop fires BACKSTOP_GRACE_S later (server.py).
    run_deadline_s: float = 3300.0

    # ── Bedrock ──────────────────────────────────────────────────────
    # Empty -> the Strands/SDK provider default model.
    bedrock_model_id: str = ""
    # Unset -> not sent: newer models (e.g. claude-sonnet-5) reject
    # `temperature` with a ValidationException instead of ignoring it.
    bedrock_temperature: float | None = None
    # An optional Bedrock guardrail of your own; empty -> none.
    guardrail_id: str = ""
    guardrail_version: str = ""

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        """Upper-case the level and map the stdlib's WARN/FATAL spellings."""
        if not isinstance(value, str):
            return value
        level = value.strip().upper()
        return {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(level, level)

    @field_validator("allow_cors_origins")
    @classmethod
    def _check_cors_origins(cls, value: str) -> str:
        """Refuse anything but exact loopback ``http://host:port`` origins, at startup.

        Only these pass FastMCP's DNS-rebinding protection on a loopback bind, and this
        exact form is what a browser sends as ``Origin``.
        """
        origins = [o.strip() for o in value.split(",") if o.strip()]
        for origin in origins:
            if "*" in origin:
                raise ValueError("SCL_ALLOW_CORS_ORIGINS must list exact origins; '*' is refused")
            if not _is_loopback_origin(origin):
                raise ValueError(
                    f"SCL_ALLOW_CORS_ORIGINS entry {origin!r} is not a loopback origin "
                    f"(http://127.0.0.1:<port>, http://localhost:<port> or http://[::1]:<port>)"
                )
        return ",".join(origins)

    @property
    def cors_origins(self) -> tuple[str, ...]:
        """The configured CORS origins; empty when CORS is off."""
        return tuple(self.allow_cors_origins.split(",")) if self.allow_cors_origins else ()

    def missing_required(self) -> list[str]:
        """Names of the unset settings needed to verify callers or call COA.

        An empty ``JWT_CLIENT_ID`` would turn off the audience check, so it is required.
        """
        return [
            name
            for name, value in (
                ("SCL_API_ENDPOINT", self.api_endpoint),
                ("JWT_ISSUER_URL", self.jwt_issuer_url),
                ("JWT_CLIENT_ID", self.jwt_client_id),
            )
            if not value
        ]

    def validate_required(self) -> None:
        """Raise if a required setting is unset. The entrypoint checks at startup; importing needs none."""
        missing = self.missing_required()
        if missing:
            raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")
