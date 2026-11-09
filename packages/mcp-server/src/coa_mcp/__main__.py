# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Entrypoint for `python -m coa_mcp`.

The shared container image runs this module instead of the Context Manager when
SCL_MCP_MODE=true (packages/context-manager/Dockerfile CMD); mcp-stack.ts sets it.
"""

from __future__ import annotations

from .server import config, mcp

if __name__ == "__main__":
    from coa_common.logging import setup_logging

    setup_logging(config.log_level)

    import structlog

    logger = structlog.get_logger(__name__)
    logger.info(
        "mcp_server_starting",
        host=config.mcp_host,
        port=config.mcp_port,
        auth_enabled=config.auth_enabled,
        tools=[
            "list_metrics",
            "describe_schema",
            "query",
            "translate_sparql",
            "rag_retrieval",
            "graph_traversal",
        ],
    )

    mcp.run(transport="streamable-http")
