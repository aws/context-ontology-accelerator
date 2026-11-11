# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Development-only support utilities.

This module contains CORS helpers that are only used in local development
(SCL_ALLOW_CORS=true). server.py imports from here only when that env var is set.
"""

from __future__ import annotations


def add_cors_middleware(app):
    """Add CORS middleware for local demo UI.

    Only called when SCL_ALLOW_CORS=true. Allows the local React
    dev server (port 3000) to talk to the MCP server.
    """
    from starlette.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    return app
