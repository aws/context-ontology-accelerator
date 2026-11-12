# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for local-dev support utilities (CORS middleware)."""

from __future__ import annotations

import pytest
from coa_mcp.dev_support import add_cors_middleware


@pytest.mark.unit
class TestAddCorsMiddleware:
    def test_adds_cors_middleware_to_app(self):
        from starlette.applications import Starlette
        from starlette.middleware.cors import CORSMiddleware

        app = Starlette()
        returned = add_cors_middleware(app)
        assert returned is app
        assert any(mw.cls is CORSMiddleware for mw in app.user_middleware)
