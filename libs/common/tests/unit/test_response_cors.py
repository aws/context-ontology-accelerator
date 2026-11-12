# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for response.py CORS configuration."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestCorsConfiguration:
    """Test that CORS headers require explicit ALLOWED_ORIGIN configuration."""

    def test_allowed_origin_env_var_is_used(self):
        """Test that ALLOWED_ORIGIN from environment is used in api_response."""
        with patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://prod.example.com"}, clear=True):
            import importlib

            from coa_common import response

            importlib.reload(response)

            result = response.api_response(200, {"message": "ok"})
            assert result["headers"]["Access-Control-Allow-Origin"] == "https://prod.example.com"

    def test_missing_allowed_origin_raises_on_use(self):
        """Missing ALLOWED_ORIGIN raises RuntimeError when api_response is called, not at import."""
        with patch.dict(os.environ, {}, clear=True):
            import importlib

            from coa_common import response

            # Import succeeds (no crash)
            importlib.reload(response)

            # But calling api_response fails
            with pytest.raises(RuntimeError, match="ALLOWED_ORIGIN environment variable must be set"):
                response.api_response(200, {"message": "ok"})

    def test_api_response_includes_cors_headers(self):
        """Test that api_response function includes configured CORS headers."""
        with patch.dict(os.environ, {"ALLOWED_ORIGIN": "https://test.example.com"}, clear=True):
            import importlib

            from coa_common import response

            importlib.reload(response)

            result = response.api_response(200, {"message": "ok"})

            assert result["statusCode"] == 200
            assert result["headers"]["Access-Control-Allow-Origin"] == "https://test.example.com"
            assert result["headers"]["Access-Control-Allow-Headers"] == "Content-Type,Authorization"
            assert result["headers"]["Access-Control-Allow-Methods"] == "GET,POST,PUT,DELETE,OPTIONS"
            assert result["headers"]["Content-Type"] == "application/json"
