# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared unit-test fixtures: an RS256 token factory and an autouse JWKS mock."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

_TEST_KID = "test-kid-1"
_TEST_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"
_TEST_AUDIENCE = "test-audience"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk() -> dict[str, Any]:
    public_numbers = _private_key.public_key().public_numbers()

    def _int_to_base64url(n: int, length: int) -> str:
        return jwt.utils.base64url_encode(n.to_bytes(length, byteorder="big")).decode()

    return {
        "kty": "RSA",
        "kid": _TEST_KID,
        "use": "sig",
        "alg": "RS256",
        "n": _int_to_base64url(public_numbers.n, 256),
        "e": _int_to_base64url(public_numbers.e, 3),
    }


def create_test_token(
    user_id: str = "test-user",
    agent_id: str = "test-agent",
    namespaces: list[str] | None = None,
    roles: list[str] | None = None,
    exp_offset: int = 3600,
    *,
    issuer: str = _TEST_ISSUER,
    audience: str = _TEST_AUDIENCE,
    email: str | None = None,
) -> str:
    """Create an RS256-signed test JWT matching the key served by ``_mock_jwks``.

    ``roles`` map to ``cognito:groups``. ``email`` is omitted when ``None``,
    as in Cognito access tokens.
    """
    payload = {
        "sub": user_id,
        "agent_id": agent_id,
        "namespaces": namespaces or ["default"],
        "cognito:groups": roles or ["viewer"],
        "iss": issuer,
        "aud": audience,
        "exp": int(time.time()) + exp_offset,
    }
    if email is not None:
        payload["email"] = email
    return jwt.encode(payload, _private_key, algorithm="RS256", headers={"kid": _TEST_KID})


@pytest.fixture(autouse=True)
def _mock_jwks(monkeypatch):
    """Point claims.py's authorizer at the test JWKS instead of a real network call."""
    from coa_mcp.auth import claims

    monkeypatch.setenv("JWT_ISSUER_URL", _TEST_ISSUER)
    monkeypatch.setenv("JWT_CLIENT_ID", _TEST_AUDIENCE)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    claims._get_authorizer.cache_clear()

    authorizer = claims._get_authorizer()
    monkeypatch.setattr(authorizer, "_get_jwks", lambda: {"keys": [_jwk()]})

    yield

    claims._get_authorizer.cache_clear()
