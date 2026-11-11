# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for JWT claims extraction with independent signature verification."""

import time

import jwt
import pytest
from coa_mcp.auth.claims import (
    ClaimsExtractionError,
    extract_caller_identity,
)

from .conftest import _TEST_AUDIENCE, _TEST_ISSUER, _TEST_KID, _private_key, create_test_token


@pytest.mark.unit
class TestExtractCallerIdentity:
    """Properly-signed tokens are accepted and claims extracted."""

    def test_valid_token_extracts_user_id(self):
        token = create_test_token(user_id="alice", namespaces=["sales", "hr"])
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.user_id == "alice"

    def test_valid_token_extracts_agent_id(self):
        token = create_test_token(user_id="alice", agent_id="claude-agent-1")
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.agent_id == "claude-agent-1"

    def test_valid_token_extracts_namespaces(self):
        token = create_test_token(user_id="alice", namespaces=["sales", "hr"])
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.namespaces == ["sales", "hr"]

    def test_valid_token_extracts_roles_from_cognito_groups(self):
        token = create_test_token(user_id="alice", roles=["admin", "viewer"])
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.roles == ["admin", "viewer"]

    def test_missing_authorization_header_raises(self):
        with pytest.raises(ClaimsExtractionError, match="Missing Authorization header"):
            extract_caller_identity(None)

    def test_empty_authorization_header_raises(self):
        with pytest.raises(ClaimsExtractionError, match="Missing Authorization header"):
            extract_caller_identity("")

    def test_malformed_bearer_prefix_raises(self):
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity("Basic dXNlcjpwYXNz")

    def test_garbage_token_raises(self):
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity("Bearer not.a.valid.jwt")

    def test_expired_token_raises(self):
        token = create_test_token(user_id="alice", exp_offset=-100)
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity(f"Bearer {token}")

    def test_comma_separated_string_namespaces_parsed_to_list(self):
        """A scope-style comma-separated string claim is split into a list."""
        token = jwt.encode(
            {
                "sub": "alice",
                "namespaces": "sales, hr ,  finance",
                "iss": _TEST_ISSUER,
                "aud": _TEST_AUDIENCE,
                "exp": int(time.time()) + 3600,
            },
            _private_key,
            algorithm="RS256",
            headers={"kid": _TEST_KID},
        )
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.namespaces == ["sales", "hr", "finance"]

    def test_user_id_from_username_claim_when_no_sub(self):
        """Falls back to 'username' claim for the delegating user id."""
        token = jwt.encode(
            {
                "username": "carol",
                "iss": _TEST_ISSUER,
                "aud": _TEST_AUDIENCE,
                "exp": int(time.time()) + 3600,
            },
            _private_key,
            algorithm="RS256",
            headers={"kid": _TEST_KID},
        )
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.user_id == "carol"


@pytest.mark.unit
class TestSignatureVerification:
    """The core defense-in-depth guarantee: a forged token must be rejected here,
    independent of whether AgentCore's own authorizer would have caught it."""

    def test_forged_admin_token_with_wrong_key_rejected(self):
        """Self-signed with a different RSA key than the test JWKS: a forged admin JWT must fail the signature check."""
        from cryptography.hazmat.primitives.asymmetric import rsa

        attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(
            {
                "sub": "attacker",
                "cognito:groups": ["admin"],
                "iss": _TEST_ISSUER,
                "aud": _TEST_AUDIENCE,
                "exp": int(time.time()) + 3600,
            },
            attacker_key,
            algorithm="RS256",
            # Real kid, so the signature check itself rejects this, not an unknown kid.
            headers={"kid": _TEST_KID},
        )
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity(f"Bearer {forged}")

    def test_unsigned_none_algorithm_token_rejected(self):
        """The classic alg=none forgery — no signature at all."""
        forged = jwt.encode(
            {"sub": "attacker", "cognito:groups": ["admin"]},
            key=None,
            algorithm="none",
        )
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity(f"Bearer {forged}")

    def test_wrong_issuer_rejected(self):
        token = create_test_token(user_id="attacker", issuer="https://evil.test/pool")
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity(f"Bearer {token}")

    def test_wrong_audience_rejected(self):
        token = create_test_token(user_id="attacker", audience="some-other-client")
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity(f"Bearer {token}")

    def test_unknown_kid_rejected(self):
        token = jwt.encode(
            {
                "sub": "attacker",
                "iss": _TEST_ISSUER,
                "aud": _TEST_AUDIENCE,
                "exp": int(time.time()) + 3600,
            },
            _private_key,
            algorithm="RS256",
            headers={"kid": "not-in-jwks"},
        )
        with pytest.raises(ClaimsExtractionError, match="JWT verification failed"):
            extract_caller_identity(f"Bearer {token}")


@pytest.mark.unit
class TestGetAuthorizerConfiguration:
    def test_missing_issuer_env_var_raises(self, monkeypatch):
        """Fail closed: no JWT_ISSUER_URL means no authorizer can be built,
        never a silent fall-through to unverified decoding."""
        from coa_mcp.auth import claims

        monkeypatch.delenv("JWT_ISSUER_URL", raising=False)
        claims._get_authorizer.cache_clear()
        with pytest.raises(KeyError, match="JWT_ISSUER_URL"):
            extract_caller_identity("Bearer irrelevant")
        claims._get_authorizer.cache_clear()


@pytest.mark.unit
class TestCreateTestToken:
    def test_creates_verifiable_token(self):
        token = create_test_token(user_id="test-user")
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.user_id == "test-user"

    def test_default_values(self):
        token = create_test_token()
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.user_id == "test-user"
        assert identity.agent_id == "test-agent"
        assert identity.namespaces == ["default"]
        assert identity.roles == ["viewer"]


@pytest.mark.unit
class TestEmailExtraction:
    """Email is threaded from the verified JWT into CallerIdentity so grants
    keyed on ``User::<email>`` (the UI-created default) resolve for a caller
    whose ``sub`` is an opaque Cognito UUID.
    """

    def test_valid_token_extracts_email(self):
        token = create_test_token(user_id="uuid-1234", email="alice@example.com")
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.email == "alice@example.com"

    def test_missing_email_defaults_to_empty_string(self):
        # Cognito access tokens carry no email claim; absence must yield ``""``, not ``None``.
        token = create_test_token(user_id="uuid-1234")  # no email kwarg
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.email == ""

    def test_none_email_coalesced_to_empty_string(self):
        """An explicit ``"email": None`` in the JWT is coalesced to ``""``."""
        token = jwt.encode(
            {
                "sub": "uuid-1234",
                "email": None,
                "iss": _TEST_ISSUER,
                "aud": _TEST_AUDIENCE,
                "exp": int(time.time()) + 3600,
            },
            _private_key,
            algorithm="RS256",
            headers={"kid": _TEST_KID},
        )
        identity = extract_caller_identity(f"Bearer {token}")
        assert identity.email == ""
