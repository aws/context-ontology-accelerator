# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Entrypoint for ``python -m coa_workflow_agents``: serve ``run_workflow`` over Streamable HTTP."""

from __future__ import annotations

import structlog
import uvicorn

from coa_workflow_agents.agents.supervisor import ALL_MUTATING_TOOL_NAMES
from coa_workflow_agents.log_policy import setup_service_logging
from coa_workflow_agents.server import build_http_app, config


def main() -> None:
    """Configure logging and run the MCP server."""
    # Third-party (stdlib) loggers stay at WARNING: at DEBUG botocore and the
    # HTTP libraries log request bodies and URLs that carry caller tokens.
    # Every line, ours and third-party, then passes the fail-closed content
    # policy (log_policy.py).
    setup_service_logging(config.log_level)
    logger = structlog.get_logger(__name__)
    # Fail at startup, not on the first call: a misconfigured server must not look healthy.
    missing = config.missing_required()
    if missing:
        logger.error("server_config_missing", missing=missing)
        config.validate_required()
    logger.info(
        "coa_workflow_agents_server_starting",
        host=config.mcp_host,
        port=config.mcp_port,
        cors_origins=list(config.cors_origins),
        mutating_tools=sorted(ALL_MUTATING_TOOL_NAMES),
    )
    # What FastMCP.run(transport="streamable-http") does, on the app
    # build_http_app returns (CORS only when SCL_ALLOW_CORS_ORIGINS is set).
    uvicorn.run(
        build_http_app(config.cors_origins),
        host=config.mcp_host,
        port=config.mcp_port,
        log_level=config.log_level.lower(),
        # No uvicorn logging config of its own: its loggers then propagate to
        # the root handler, so its lines (an ASGI exception's traceback
        # included) pass the same content policy as every other record.
        log_config=None,
        # The access line would carry only its template (no status, no path) under the log policy.
        access_log=False,
    )


if __name__ == "__main__":
    main()
