# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for token authorizer ABC and implementations."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit
from coa_common.auth import (
    CognitoTokenAuthorizer,
    OIDCTokenAuthorizer,
    TokenAuthorizer,
    TokenType,
    TokenValidationResult,
)


@pytest.fixture()
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_numbers = public_key.public_numbers()

    def _int_to_base64url(n: int, length: int) -> str:
        return jwt.utils.base64url_encode(n.to_bytes(length, byteorder="big")).decode()

    jwk = {
        "kty": "RSA",
        "kid": "test-kid-1",
        "use": "sig",
        "alg": "RS256",
        "n": _int_to_base64url(public_numbers.n, 256),
        "e": _int_to_base64url(public_numbers.e, 3),
    }
    return private_key, jwk


@pytest.fixture()
def _make_token(rsa_keypair):
    private_key, _ = rsa_keypair

    def _factory(claims: dict[str, Any], headers: dict[str, Any] | None = None) -> str:
        default_headers = {"kid": "test-kid-1"}
        if headers:
            default_headers.update(headers)
        return jwt.encode(claims, private_key, algorithm="RS256", headers=default_headers)

    return _factory


@pytest.fixture()
def jwks_response(rsa_keypair):
    _, jwk = rsa_keypair
    return {"keys": [jwk]}


class TestExtractBearerToken:
    def test_valid_bearer(self):
        assert TokenAuthorizer._extract_bearer_token("Bearer abc123") == "abc123"

    def test_missing_header(self):
        with pytest.raises(ValueError, match="Missing or malformed"):
            TokenAuthorizer._extract_bearer_token("")

    def test_no_bearer_prefix(self):
        with pytest.raises(ValueError, match="Missing or malformed"):
            TokenAuthorizer._extract_bearer_token("Basic abc123")

    def test_empty_token(self):
        with pytest.raises(ValueError, match="Bearer token is empty"):
            TokenAuthorizer._extract_bearer_token("Bearer ")


class TestCognitoTokenAuthorizer:
    def test_validate_success(self, _make_token, jwks_response):
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "cognito:groups": ["Admins", "Viewers"],
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert isinstance(result, TokenValidationResult)
        assert result.principal_id == "alice@example.com"
        assert result.sub == "user-123"
        assert result.email == "alice@example.com"
        assert result.groups == ["Admins", "Viewers"]
        assert result.token_type == TokenType.JWT

    def test_expired_token_rejected(self, _make_token, jwks_response):
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) - 100,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_recently_expired_token_accepted_within_leeway(self, _make_token, jwks_response):
        """Token expired 10s ago is accepted due to 30s leeway."""
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "cognito:groups": ["Admin"],
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) - 10,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.principal_id == "alice@example.com"

    def test_token_expired_beyond_leeway_rejected(self, _make_token, jwks_response):
        """Token expired 31s ago (beyond 30s leeway) is rejected."""
        claims = {
            "sub": "user-123",
            "email": "alice@example.com",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) - 31,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_missing_email_denied(self, _make_token, jwks_response):
        claims = {
            "sub": "user-456",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="missing required email"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_no_groups_returns_empty(self, _make_token, jwks_response):
        claims = {
            "sub": "user-456",
            "email": "bob@example.com",
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.groups == []


class TestOIDCTokenAuthorizer:
    def test_validate_success(self, _make_token, jwks_response):
        claims = {
            "sub": "okta-user-1",
            "email": "carol@corp.com",
            "groups": ["Engineering", "Platform"],
            "iss": "https://login.corp.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://login.corp.com", jwks_uri="https://login.corp.com/jwks")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.principal_id == "carol@corp.com"
        assert result.groups == ["Engineering", "Platform"]

    def test_custom_group_claim_name(self, _make_token, jwks_response):
        claims = {
            "sub": "u1",
            "email": "user@corp.com",
            "memberOf": ["TeamA", "TeamB"],
            "iss": "https://idp.example.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", group_claim_name="memberOf")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.groups == ["TeamA", "TeamB"]

    def test_groups_as_csv_string(self, _make_token, jwks_response):
        claims = {
            "sub": "u1",
            "email": "user@corp.com",
            "groups": "A,B,C",
            "iss": "https://idp.example.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.validate(f"Bearer {token}")

        assert result.groups == ["A", "B", "C"]

    def test_issuer_mismatch_rejected(self, _make_token, jwks_response):
        claims = {
            "sub": "u1",
            "email": "user@evil.com",
            "iss": "https://evil.com",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = OIDCTokenAuthorizer(issuer="https://legit.com")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.validate(f"Bearer {token}")


class TestTokenType:
    def test_token_type_is_enum(self):
        assert TokenType.JWT == "JWT"
        assert TokenType.AGENT_ACCESS_TOKEN == "AGENT_ACCESS_TOKEN"


class TestVerifyClaimsAndExtractGroups:
    """verify_claims/extract_groups: the email-agnostic path used by callers
    (e.g. MCP server) whose identity model isn't email-keyed."""

    def test_verify_claims_returns_raw_claims_without_email_requirement(self, _make_token, jwks_response):
        claims = {
            "sub": "agent-user-1",
            "cognito:groups": ["admin"],
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
            "exp": int(time.time()) + 3600,
        }
        token = _make_token(claims)
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            result = authorizer.verify_claims(f"Bearer {token}")

        assert result["sub"] == "agent-user-1"

    def test_verify_claims_still_rejects_forged_signature(self, jwks_response):
        forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        forged = jwt.encode(
            {
                "sub": "attacker",
                "cognito:groups": ["admin"],
                "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool",
                "exp": int(time.time()) + 3600,
            },
            forged_key,
            algorithm="RS256",
            headers={"kid": "test-kid-1"},
        )
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="Invalid token"),
        ):
            authorizer.verify_claims(f"Bearer {forged}")

    def test_extract_groups_delegates_to_idp_specific_hook(self):
        cognito = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")
        assert cognito.extract_groups({"cognito:groups": ["a", "b"]}) == ["a", "b"]

        oidc = OIDCTokenAuthorizer(issuer="https://idp.example.com", group_claim_name="memberOf")
        assert oidc.extract_groups({"memberOf": ["c", "d"]}) == ["c", "d"]


class TestJWKSCaching:
    def test_jwks_cached_within_ttl(self, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
        with patch("coa_common.auth.token_authorizer.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = jwks_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            authorizer._get_jwks()
            authorizer._get_jwks()
            assert mock_get.call_count == 1

    def test_jwks_refetched_after_ttl(self, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
        authorizer._jwks_cache_ttl = 0

        with patch("coa_common.auth.token_authorizer.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = jwks_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            authorizer._get_jwks()
            authorizer._get_jwks()
            assert mock_get.call_count == 2

    def test_a_failed_refresh_raises_keeps_the_old_cache_and_the_next_call_refetches(self, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
        ok = MagicMock()
        ok.json.return_value = jwks_response
        rotated = {"keys": [{"kid": "rotated"}]}
        ok_rotated = MagicMock()
        ok_rotated.json.return_value = rotated

        with patch("coa_common.auth.token_authorizer.requests.get", return_value=ok):
            authorizer._get_jwks()  # warm the cache
        cached = authorizer._jwks_cache
        authorizer._jwks_cache_ttl = 0  # expire it

        with (
            patch(
                "coa_common.auth.token_authorizer.requests.get",
                side_effect=requests.ConnectionError("idp down"),
            ),
            pytest.raises(requests.ConnectionError),
        ):
            authorizer._get_jwks()
        assert authorizer._jwks_cache == cached

        with (
            patch("coa_common.auth.token_authorizer._JWKS_RETRY_AFTER_S", 0.0),
            patch("coa_common.auth.token_authorizer.requests.get", return_value=ok_rotated) as mock_get,
        ):
            assert authorizer._get_jwks() == rotated
        assert mock_get.call_count == 1


_POOL_ISS = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TestPool"


def _cognito_claims(**extra: Any) -> dict[str, Any]:
    return {"sub": "u1", "email": "a@example.com", "iss": _POOL_ISS, "exp": int(time.time()) + 3600, **extra}


def _unsigned_token(claims: dict[str, Any]) -> str:
    """An ``alg: none`` token: header and payload, empty signature."""
    header = jwt.utils.base64url_encode(b'{"alg":"none","typ":"JWT","kid":"test-kid-1"}').decode()
    payload = jwt.utils.base64url_encode(jwt.api_jws.json.dumps(claims).encode()).decode()
    return f"{header}.{payload}."


class TestAlgorithmAllowlist:
    @pytest.mark.parametrize("alg", ["none", "HS256", "RS512"])
    def test_unexpected_algorithm_is_rejected_before_any_key_lookup(self, rsa_keypair, alg):
        """``none`` and HMAC key confusion (HS256 keyed with the public key's PEM) are what the
        guard exists for; RS512 is valid for PyJWT but not allowed. Each fails early, by name."""
        private_key, _ = rsa_keypair
        if alg == "none":
            token = _unsigned_token(_cognito_claims())
        elif alg == "HS256":
            pem = private_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            token = _hmac_token(_cognito_claims(), pem)
        else:
            token = jwt.encode(_cognito_claims(), private_key, algorithm="RS512", headers={"kid": "test-kid-1"})
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1")

        with (
            patch.object(authorizer, "_get_jwks") as get_jwks,
            pytest.raises(ValueError, match=f"Unsupported token algorithm: '{alg}'"),
        ):
            authorizer.validate(f"Bearer {token}")
        get_jwks.assert_not_called()


def _hmac_token(claims: dict[str, Any], key: bytes) -> str:
    """HS256 signed with ``key`` by hand: PyJWT refuses a PEM as an HMAC key."""
    header = jwt.utils.base64url_encode(b'{"alg":"HS256","typ":"JWT","kid":"test-kid-1"}')
    payload = jwt.utils.base64url_encode(jwt.api_jws.json.dumps(claims).encode())
    signing_input = header + b"." + payload
    signature = jwt.utils.base64url_encode(hmac.new(key, signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + signature).decode()


class TestMultipleAudiences:
    def test_any_listed_audience_is_accepted(self, _make_token, jwks_response):
        authorizer = CognitoTokenAuthorizer(
            user_pool_id="us-east-1_TestPool", region="us-east-1", client_id=["web-client", "mcp-client"]
        )
        for aud in ("web-client", "mcp-client"):
            token = _make_token(_cognito_claims(aud=aud))
            with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
                assert authorizer.validate(f"Bearer {token}").claims["aud"] == aud

    def test_an_unlisted_audience_is_rejected(self, _make_token, jwks_response):
        authorizer = CognitoTokenAuthorizer(
            user_pool_id="us-east-1_TestPool", region="us-east-1", client_id=["web-client", "mcp-client"]
        )
        token = _make_token(_cognito_claims(aud="other-client"))
        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="audience mismatch"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_a_single_string_audience_still_works(self, _make_token, jwks_response):
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1", client_id="web")
        token = _make_token(_cognito_claims(aud="web"))
        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            assert authorizer.validate(f"Bearer {token}").email == "a@example.com"

    def test_an_access_tokens_client_id_matches_by_default(self, _make_token, jwks_response):
        authorizer = CognitoTokenAuthorizer(
            user_pool_id="us-east-1_TestPool", region="us-east-1", client_id=["web", "mcp"]
        )
        token = _make_token(_cognito_claims(client_id="mcp", token_use="access"))
        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            assert authorizer.verify_claims(f"Bearer {token}")["client_id"] == "mcp"

    def test_non_string_entries_in_an_aud_list_are_skipped(self, _make_token, jwks_response):
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1", client_id="web")
        token = _make_token(_cognito_claims(aud=["x", {"k": 1}, "web"]))
        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            assert authorizer.validate(f"Bearer {token}").email == "a@example.com"

    @pytest.mark.parametrize("aud", [5, {"web": 1}], ids=["number", "dict"])
    def test_an_aud_that_is_neither_string_nor_list_is_a_clean_mismatch(self, _make_token, jwks_response, aud):
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1", client_id="web")
        token = _make_token(_cognito_claims(aud=aud))
        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="audience mismatch"),
        ):
            authorizer.validate(f"Bearer {token}")

    @pytest.mark.parametrize("aud", ["", []], ids=["empty-string", "empty-list"])
    def test_an_empty_aud_does_not_fall_back_to_client_id(self, _make_token, jwks_response, aud):
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1", client_id="web")
        token = _make_token(_cognito_claims(aud=aud, client_id="web"))
        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="audience mismatch"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_a_cognito_authorizer_refuses_an_all_empty_audience_collection(self):
        with pytest.raises(ValueError, match="no non-empty value"):
            CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1", client_id=["", ""])


class TestRequireIdToken:
    def _authorizer(self) -> CognitoTokenAuthorizer:
        return CognitoTokenAuthorizer(
            user_pool_id="us-east-1_TestPool", region="us-east-1", client_id=["mcp"], require_id_token=True
        )

    def test_an_id_token_is_accepted(self, _make_token, jwks_response):
        authorizer = self._authorizer()
        token = _make_token(_cognito_claims(aud="mcp", token_use="id"))
        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            assert authorizer.validate(f"Bearer {token}").email == "a@example.com"

    def test_an_access_token_is_rejected(self, _make_token, jwks_response):
        authorizer = self._authorizer()
        token = _make_token(_cognito_claims(client_id="mcp", token_use="access"))
        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="only an ID token"),
        ):
            authorizer.verify_claims(f"Bearer {token}")

    def test_a_plain_oidc_id_token_without_token_use_is_accepted(self, _make_token, jwks_response):
        """Generic IdPs send no ``token_use``; an ID token with the right ``aud`` and a plain ``typ`` passes."""
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", audience="mcp", require_id_token=True)
        token = _make_token({**_cognito_claims(aud="mcp"), "iss": "https://idp.example.com"})
        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            assert authorizer.validate(f"Bearer {token}").email == "a@example.com"

    def test_a_plain_oidc_id_token_for_another_audience_is_rejected(self, _make_token, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", audience="mcp", require_id_token=True)
        token = _make_token({**_cognito_claims(aud="other"), "iss": "https://idp.example.com"})
        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="audience mismatch"),
        ):
            authorizer.validate(f"Bearer {token}")

    def test_client_id_does_not_stand_in_for_aud(self, _make_token, jwks_response):
        """Without token_use, an access-token-shaped claim set (client_id, no aud) still fails."""
        authorizer = self._authorizer()
        token = _make_token(_cognito_claims(client_id="mcp"))
        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="audience mismatch"),
        ):
            authorizer.verify_claims(f"Bearer {token}")

    @pytest.mark.parametrize("typ", ["at+jwt", "AT+JWT", "application/at+jwt"])
    def test_an_rfc9068_access_token_without_token_use_is_rejected(self, _make_token, jwks_response, typ):
        """Non-Cognito IdPs emit no token_use; their JWT access tokens are typed at+jwt."""
        authorizer = self._authorizer()
        token = _make_token(_cognito_claims(aud="mcp"), headers={"typ": typ})
        with (
            patch.object(authorizer, "_get_jwks", return_value=jwks_response),
            pytest.raises(ValueError, match="only an ID token"),
        ):
            authorizer.verify_claims(f"Bearer {token}")

    def test_an_access_token_type_is_accepted_when_id_tokens_are_not_required(self, _make_token, jwks_response):
        authorizer = CognitoTokenAuthorizer(user_pool_id="us-east-1_TestPool", region="us-east-1", client_id=["mcp"])
        token = _make_token(_cognito_claims(aud="mcp"), headers={"typ": "at+jwt"})
        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            assert authorizer.verify_claims(f"Bearer {token}")["aud"] == "mcp"


class TestJWKSLock:
    def test_concurrent_cold_cache_fetches_the_jwks_once(self, jwks_response):
        """Several threads hitting a cold cache at once must share one fetch."""
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
        started = threading.Barrier(8)

        def slow_get(*_args: Any, **_kwargs: Any) -> MagicMock:
            time.sleep(0.05)
            resp = MagicMock()
            resp.json.return_value = jwks_response
            return resp

        results: list[dict[str, Any]] = []

        def worker() -> None:
            started.wait()
            results.append(authorizer._get_jwks())

        with patch("coa_common.auth.token_authorizer.requests.get", side_effect=slow_get) as mock_get:
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert mock_get.call_count == 1
        assert results == [jwks_response] * 8

    @pytest.mark.parametrize("jwks_uri", ["https://idp.example.com/jwks", None], ids=["jwks", "discovery"])
    def test_callers_queued_behind_a_hung_fetch_reraise_its_failure_instead_of_refetching(self, jwks_uri):
        """A hung IdP costs one fetch, whether the key set or the discovery document hangs."""
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri=jwks_uri)
        started = threading.Barrier(4)

        def hung_get(*_args: Any, **_kwargs: Any) -> MagicMock:
            time.sleep(0.1)  # long enough for the other callers to queue on the lock
            raise requests.Timeout("idp hung")

        def worker() -> None:
            started.wait()
            with contextlib.suppress(requests.RequestException):
                authorizer._get_jwks()

        with patch("coa_common.auth.token_authorizer.requests.get", side_effect=hung_get) as mock_get:
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert mock_get.call_count == 1

    def test_a_recent_failure_is_reraised_and_a_later_call_retries(self, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
        ok = MagicMock()
        ok.json.return_value = jwks_response

        with (
            patch("coa_common.auth.token_authorizer.requests.get", side_effect=requests.ConnectionError("down")),
            pytest.raises(requests.ConnectionError),
        ):
            authorizer._get_jwks()
        with (
            patch("coa_common.auth.token_authorizer.requests.get") as mock_get,
            capture_logs() as logs,
            pytest.raises(requests.RequestException, match="failed recently") as exc_info,
        ):
            authorizer._get_jwks()
        mock_get.assert_not_called()
        assert isinstance(exc_info.value.__cause__, requests.ConnectionError)
        # Each refused caller is logged, so an outage's reach shows in the logs.
        (refused,) = [e for e in logs if e["event"] == "JWKS fetch failed recently; not retrying yet"]
        assert refused["log_level"] == "warning" and 0 < refused["retry_in_s"] <= 5.0

        with (
            patch("coa_common.auth.token_authorizer._JWKS_RETRY_AFTER_S", 0.0),
            patch("coa_common.auth.token_authorizer.requests.get", return_value=ok) as mock_get,
        ):
            assert authorizer._get_jwks() == jwks_response
        assert mock_get.call_count == 1
        assert authorizer._jwks_failure is None


class TestAudienceConfiguration:
    @pytest.mark.parametrize("audience", [[], [""], ("",), {""}])
    def test_an_audience_collection_with_no_value_is_a_configuration_error(self, audience):
        with pytest.raises(ValueError, match="no non-empty value"):
            OIDCTokenAuthorizer(issuer="https://idp.example.com", audience=audience)

    @pytest.mark.parametrize("audience", [None, ""])
    def test_none_or_an_empty_string_still_skips_the_audience_check(self, audience, _make_token, jwks_response):
        authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", audience=audience)
        token = _make_token({**_cognito_claims(aud="anything"), "iss": "https://idp.example.com"})
        with patch.object(authorizer, "_get_jwks", return_value=jwks_response):
            assert authorizer.validate(f"Bearer {token}").email == "a@example.com"


def test_the_jwks_cache_is_stamped_after_the_fetch(jwks_response):
    """A slow fetch must not shorten the cache's life: the timestamp is taken when the keys arrive."""
    authorizer = OIDCTokenAuthorizer(issuer="https://idp.example.com", jwks_uri="https://idp.example.com/jwks")
    clock = iter([100.0, 130.0])  # the locked check, then after the fetch
    with (
        patch("coa_common.auth.token_authorizer.time.time", side_effect=lambda: next(clock)),
        patch.object(authorizer, "_fetch_jwks", return_value=jwks_response),
    ):
        authorizer._get_jwks()
    assert authorizer._jwks_cache == (jwks_response, 130.0)
