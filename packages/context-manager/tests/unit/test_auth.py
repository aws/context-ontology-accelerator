# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for TokenClaims (auth.py)."""

from unittest.mock import MagicMock

import jwt
import pytest
from coa_serve.auth import TokenClaims

pytestmark = pytest.mark.unit


def _make_token(claims: dict) -> str:
    """JWT signed with a throwaway key (the parser does not verify)."""
    return jwt.encode(claims, "secret", algorithm="HS256")


class TestTokenClaimsFromAuthHeader:
    """Tests for _from_auth_header (shared parsing logic)."""

    def test_extracts_sub_from_valid_token(self):
        token = _make_token({"sub": "user-123", "email": "a@b.com"})
        claims = TokenClaims._from_auth_header(f"Bearer {token}")
        assert claims is not None
        assert claims.user_id == "user-123"

    def test_returns_none_when_no_auth_header(self):
        assert TokenClaims._from_auth_header("") is None

    def test_returns_none_when_not_bearer(self):
        assert TokenClaims._from_auth_header("Basic abc123") is None

    def test_returns_none_when_sub_missing(self):
        token = _make_token({"email": "a@b.com"})
        assert TokenClaims._from_auth_header(f"Bearer {token}") is None

    def test_handles_cognito_access_token(self):
        """Cognito access tokens have sub + client_id + username."""
        token = _make_token(
            {
                "sub": "e3345862-5061-7049-51c4-1498357d4c52",
                "client_id": "2scin4gj5kcpl11q3mn5akaal",
                "token_use": "access",
                "username": "e3345862-5061-7049-51c4-1498357d4c52",
            }
        )
        claims = TokenClaims._from_auth_header(f"Bearer {token}")
        assert claims is not None
        assert claims.user_id == "e3345862-5061-7049-51c4-1498357d4c52"

    def test_handles_azure_ad_token(self):
        """Azure AD tokens have sub + oid + preferred_username."""
        token = _make_token(
            {
                "sub": "opaque-azure-sub",
                "oid": "azure-object-id",
                "preferred_username": "user@contoso.com",
            }
        )
        claims = TokenClaims._from_auth_header(f"Bearer {token}")
        assert claims is not None
        assert claims.user_id == "opaque-azure-sub"


class TestTokenClaimsFromRequestContext:
    """Tests for from_request_context (AgentCore HTTP path)."""

    def test_extracts_from_request_headers_dict(self):
        token = _make_token({"sub": "user-abc", "email": "u@x.com"})
        ctx = MagicMock()
        ctx.request_headers = {"authorization": f"Bearer {token}"}
        claims = TokenClaims.from_request_context(ctx)
        assert claims is not None
        assert claims.user_id == "user-abc"
        assert claims.email == "u@x.com"

    def test_extracts_from_request_headers_capitalized(self):
        token = _make_token({"sub": "user-cap"})
        ctx = MagicMock()
        ctx.request_headers = {"Authorization": f"Bearer {token}"}
        claims = TokenClaims.from_request_context(ctx)
        assert claims is not None
        assert claims.user_id == "user-cap"

    def test_returns_none_when_context_is_none(self):
        assert TokenClaims.from_request_context(None) is None

    def test_returns_none_when_no_auth_header_found(self):
        ctx = MagicMock()
        ctx.request_headers = {"content-type": "application/json"}
        assert TokenClaims.from_request_context(ctx) is None

    def test_returns_none_when_request_headers_not_dict(self):
        ctx = MagicMock()
        ctx.request_headers = "not-a-dict"
        assert TokenClaims.from_request_context(ctx) is None

    def test_returns_none_when_no_request_headers_attr(self):
        ctx = MagicMock(spec=[])
        assert TokenClaims.from_request_context(ctx) is None

    def test_extracts_groups_from_jwt(self):
        token = _make_token({"sub": "user-g", "groups": ["admin", "viewer"]})
        ctx = MagicMock()
        ctx.request_headers = {"authorization": f"Bearer {token}"}
        claims = TokenClaims.from_request_context(ctx)
        assert claims is not None
        assert "admin" in claims.groups
        assert "viewer" in claims.groups
