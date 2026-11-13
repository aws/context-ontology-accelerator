# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the IdP-agnostic token authorizer factory."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.unit

from coa_common.auth import CognitoTokenAuthorizer, OIDCTokenAuthorizer
from coa_common.auth.factory import build_token_authorizer, is_cognito_issuer


class TestIsCognitoIssuer:
    @pytest.mark.parametrize(
        "issuer",
        [
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_XyZ789",
            "https://cognito-idp.cn-north-1.amazonaws.com.cn/cn-north-1_ABC",
        ],
    )
    def test_real_cognito_issuers_detected(self, issuer):
        assert is_cognito_issuer(issuer) is True

    @pytest.mark.parametrize(
        "issuer",
        [
            # Look-alike host that merely ends with the Cognito service name.
            "https://cognito-idp.us-east-1.amazonaws.com.evil.test/us-east-1_ABC123",
            # Cognito host embedded in the path rather than the host.
            "https://evil.test/cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            # Cognito host smuggled into the query string.
            "https://evil.test/pool?x=cognito-idp.us-east-1.amazonaws.com/",
            # Userinfo trick: the real host is evil.test.
            "https://cognito-idp.us-east-1.amazonaws.com@evil.test/us-east-1_ABC",
            # Right shape but plaintext — must not be treated as Cognito.
            "http://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            # Generic OIDC issuers.
            "https://login.okta.com/oauth2/default",
            "https://login.microsoftonline.com/tenant-id/v2.0",
        ],
    )
    def test_non_cognito_issuers_rejected(self, issuer):
        assert is_cognito_issuer(issuer) is False


class TestBuildTokenAuthorizer:
    def test_cognito_issuer_builds_cognito_authorizer(self):
        authorizer = build_token_authorizer(
            issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            region="us-east-1",
            client_id="my-client-id",
        )
        assert isinstance(authorizer, CognitoTokenAuthorizer)

    def test_okta_issuer_builds_oidc_authorizer(self):
        authorizer = build_token_authorizer(
            issuer="https://login.okta.com/oauth2/default",
            region="us-east-1",
            jwks_uri="https://login.okta.com/oauth2/default/v1/keys",
        )
        assert isinstance(authorizer, OIDCTokenAuthorizer)

    def test_entra_id_issuer_builds_oidc_authorizer_with_custom_group_claim(self):
        # EntraID (Azure AD) commonly puts group memberships under "roles"
        # rather than the OIDC-conventional "groups" claim.
        authorizer = build_token_authorizer(
            issuer="https://login.microsoftonline.com/tenant-id/v2.0",
            region="us-east-1",
            jwks_uri="https://login.microsoftonline.com/tenant-id/discovery/v2.0/keys",
            group_claim_name="roles",
        )
        assert isinstance(authorizer, OIDCTokenAuthorizer)
        assert authorizer._group_claim_name == "roles"

    def test_oidc_falls_back_to_discovery_when_jwks_uri_omitted(self):
        authorizer = build_token_authorizer(
            issuer="https://auth0-tenant.us.auth0.com/",
            region="us-east-1",
        )
        assert isinstance(authorizer, OIDCTokenAuthorizer)
        # No explicit jwks_uri configured — falls back to OIDC discovery at
        # request time rather than raising eagerly.
        assert authorizer._configured_jwks_uri is None


class TestBuildTokenAuthorizerAudienceOptions:
    @pytest.mark.parametrize(
        "issuer",
        ["https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123", "https://login.okta.com/oauth2/default"],
    )
    def test_passes_several_audiences_and_require_id_token_through(self, issuer):
        """Behaviour, not storage: both audiences pass, and an access token is refused."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = key.public_key().public_numbers()

        def b64(n: int, length: int) -> str:
            return jwt.utils.base64url_encode(n.to_bytes(length, byteorder="big")).decode()

        jwks = {"keys": [{"kty": "RSA", "kid": "k1", "alg": "RS256", "n": b64(numbers.n, 256), "e": b64(numbers.e, 3)}]}
        authorizer = build_token_authorizer(
            issuer=issuer, region="us-east-1", client_id=["web", "mcp"], require_id_token=True
        )
        base = {"sub": "u1", "email": "a@example.com", "iss": issuer.rstrip("/"), "exp": int(time.time()) + 3600}

        def token(**claims: object) -> str:
            return "Bearer " + jwt.encode({**base, **claims}, key, algorithm="RS256", headers={"kid": "k1"})

        with patch.object(authorizer, "_get_jwks", return_value=jwks):
            for aud in ("web", "mcp"):
                assert authorizer.validate(token(aud=aud, token_use="id")).email == "a@example.com"
            with pytest.raises(ValueError, match="only an ID token"):
                authorizer.validate(token(aud="mcp", token_use="access"))
