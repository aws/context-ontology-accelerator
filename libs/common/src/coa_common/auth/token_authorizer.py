# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Abstract base class for token authorization."""

from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import jwt
import requests
import structlog
from jwt.exceptions import PyJWTError

logger = structlog.get_logger(__name__)

_HTTP_TIMEOUT = 30

# Checked against the token's unverified header before any key lookup, so
# ``none`` or an HMAC-confusion attempt fails early with an error naming it.
_ALLOWED_ALGORITHMS = ("RS256",)

# After a failed JWKS fetch, callers re-raise that failure for this long instead of
# each refetching in turn behind the lock (a hung IdP would otherwise cost every
# queued caller its own HTTP timeout).
_JWKS_RETRY_AFTER_S = 5.0

# JWT ``typ`` header values of an OAuth 2.0 access token (RFC 9068).
_ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "application/at+jwt"})


def _normalize_audiences(audience: str | Collection[str] | None) -> frozenset[str]:
    """Accepted ``aud`` values as a set; empty means the audience is not checked.

    ``None`` or ``""`` opts out, as before. A collection with no non-empty entry is a
    configuration error (an unset value split into ``[""]``, say), not an opt-out.
    """
    if audience is None:
        return frozenset()
    if isinstance(audience, str):
        return frozenset({audience}) if audience else frozenset()
    audiences = frozenset(a for a in audience if a)
    if not audiences:
        raise ValueError("audience collection has no non-empty value; pass None to skip the audience check")
    return audiences


def extract_groups_claim(claims: dict[str, Any], group_claim_name: str = "groups") -> list[str]:
    """Normalize an IdP groups claim to list[str] (shared identity extraction).

    Accepts a list, or a comma/space-delimited string (IdPs differ). Used by the
    OIDC authorizer (control plane) and the serve WS TokenClaims so group
    parsing has exactly one implementation.
    """
    raw = claims.get(group_claim_name, [])
    if isinstance(raw, list):
        return [str(g) for g in raw]
    if isinstance(raw, str):
        return [g for g in raw.replace(",", " ").split() if g]
    return []


class TokenType(StrEnum):
    """Type of bearer token presented by the caller."""

    JWT = "JWT"
    AGENT_ACCESS_TOKEN = "AGENT_ACCESS_TOKEN"


@dataclass(frozen=True)
class TokenValidationResult:
    """Result of validating and decoding a bearer token."""

    principal_id: str  # email — used for role lookups
    sub: str  # JWT sub claim — for audit/logging
    email: str
    groups: list[str]
    token_type: TokenType
    claims: dict[str, Any] = field(default_factory=dict)
    agent_identity: str | None = None


class TokenAuthorizer(ABC):
    """ABC for token validation. Subclass for Cognito or generic OIDC.

    Parameters
    ----------
    issuer:
        The expected ``iss`` claim value.
    audience:
        The accepted ``aud`` claim value, or a collection of accepted values
        (any one matches). If ``None`` or empty, audience is not verified.
    jwks_uri:
        Explicit JWKS endpoint. If ``None``, discovered via OIDC well-known.
    jwks_cache_ttl:
        Seconds to cache the JWKS key set (default 300 = 5 min).
    require_id_token:
        Accept only ID tokens. When an audience is configured it must match
        ``aud`` (an access token's ``client_id`` does not count); a
        ``token_use`` claim, when present, must be ``"id"``; and a header
        ``typ`` of ``at+jwt`` or ``application/at+jwt`` (an RFC 9068 access
        token) is refused. Default ``False``.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | Collection[str] | None = None,
        jwks_uri: str | None = None,
        jwks_cache_ttl: int = 300,
        require_id_token: bool = False,
    ) -> None:
        """Store issuer/audience config and initialize the JWKS cache."""
        self._issuer = issuer.rstrip("/")
        self._audiences = _normalize_audiences(audience)
        self._require_id_token = require_id_token
        self._configured_jwks_uri = jwks_uri
        self._jwks_cache_ttl = jwks_cache_ttl

        # (key set, fetched-at) as one reference, so a reader never pairs one
        # fetch's keys with another fetch's timestamp.
        self._jwks_cache: tuple[dict[str, Any], float] | None = None
        # (error, failed-at) of the last failed fetch, cleared by a success.
        self._jwks_failure: tuple[requests.RequestException, float] | None = None
        self._discovered_jwks_uri: str | None = None
        # One instance is commonly shared process-wide across threads: without
        # the lock two threads can both see a stale cache and refetch.
        self._jwks_lock = threading.Lock()

    def validate(self, authorization_header: str) -> TokenValidationResult:
        """Validate a ``Bearer <token>`` header and return structured claims.

        Raises ``ValueError`` if the token is missing, malformed, expired,
        or has an invalid signature.
        """
        claims = self.verify_claims(authorization_header)
        return self._build_result(claims)

    def verify_claims(self, authorization_header: str) -> dict[str, Any]:
        """Verify a ``Bearer <token>`` header's signature/issuer/audience/expiry and return raw claims.

        Skips the ``email`` requirement ``validate`` imposes; the cryptographic
        checks are identical.

        Raises ``ValueError`` if the token is missing, malformed, expired,
        or has an invalid signature.
        """
        token = self._extract_bearer_token(authorization_header)
        return self._verify_and_decode(token)

    def extract_groups(self, claims: dict[str, Any]) -> list[str]:
        """Extract groups from verified claims via the subclass hook."""
        return self._extract_groups(claims)

    @abstractmethod
    def _extract_groups(self, claims: dict[str, Any]) -> list[str]:
        """Extract group memberships from decoded JWT claims."""

    def _extract_email(self, claims: dict[str, Any]) -> str:
        """Extract email from claims. Override if the claim name differs."""
        return str(claims.get("email", ""))

    def _get_token_type(self, claims: dict[str, Any]) -> TokenType:  # noqa: ARG002
        """Determine the token type. Override for agent access token support.

        Future: inspect claims to distinguish agent delegation tokens from
        standard JWTs and return ``TokenType.AGENT_ACCESS_TOKEN`` when the
        token represents a delegated agent identity.
        """
        return TokenType.JWT

    def _get_agent_identity(self, claims: dict[str, Any]) -> str | None:  # noqa: ARG002
        """Extract the agent workload identity for audit logging.

        Future: when agent access tokens are supported, extract the agent's
        ARN or identifier from the token claims so it can be logged alongside
        the delegating user's authorization decision.
        """
        return None

    def _get_jwks_uri(self) -> str:
        if self._configured_jwks_uri:
            return self._configured_jwks_uri
        if self._discovered_jwks_uri:
            return self._discovered_jwks_uri

        discovery_url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            resp = requests.get(discovery_url, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            config = resp.json()
            discovered_issuer = config.get("issuer", "").rstrip("/")
            if discovered_issuer != self._issuer:
                raise ValueError(f"Issuer mismatch: expected {self._issuer}, got {discovered_issuer}")
            jwks_uri = config.get("jwks_uri")
            if not jwks_uri:
                raise ValueError("OIDC discovery missing jwks_uri")
            self._discovered_jwks_uri = jwks_uri
            return jwks_uri
        except requests.RequestException:
            logger.exception("Failed to fetch OIDC discovery document", url=discovery_url)
            raise

    def _get_jwks(self) -> dict[str, Any]:
        # Fast path without the lock, re-checked under it.
        cache = self._jwks_cache
        if cache and (time.time() - cache[1]) < self._jwks_cache_ttl:
            return cache[0]
        with self._jwks_lock:
            now = time.time()
            cache = self._jwks_cache
            if cache and (now - cache[1]) < self._jwks_cache_ttl:
                return cache[0]
            failure = self._jwks_failure
            if failure and (now - failure[1]) < _JWKS_RETRY_AFTER_S:
                retry_in_s = round(_JWKS_RETRY_AFTER_S - (now - failure[1]), 1)
                logger.warning("JWKS fetch failed recently; not retrying yet", retry_in_s=retry_in_s)
                raise requests.RequestException("JWKS fetch failed recently; not retrying yet") from failure[0]
            try:
                fetched = self._fetch_jwks()
            except requests.RequestException as exc:
                self._jwks_failure = (exc, time.time())
                raise
            # Stamped after the fetch, so a slow fetch does not shorten the cache's life.
            self._jwks_cache = (fetched, time.time())
            self._jwks_failure = None
            return fetched

    def _fetch_jwks(self) -> dict[str, Any]:
        """Fetch the key set, discovering its URI first if needed (both network calls)."""
        jwks_url = self._get_jwks_uri()
        try:
            resp = requests.get(jwks_url, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            fetched: dict[str, Any] = resp.json()
        except requests.RequestException:
            logger.exception("Failed to fetch JWKS", url=jwks_url)
            raise
        return fetched

    def _get_public_key(self, kid: str) -> Any:
        jwks = self._get_jwks()
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if not key:
            raise ValueError(f"Key ID {kid} not found in JWKS")
        return jwt.api_jwk.PyJWK.from_json(json.dumps(key)).key

    def _verify_and_decode(self, token: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            alg = header.get("alg")
            if alg not in _ALLOWED_ALGORITHMS:
                raise ValueError(f"Unsupported token algorithm: {alg!r}")
            kid = header.get("kid")
            if not kid:
                raise ValueError("No kid in token header")

            public_key = self._get_public_key(kid)

            # Cognito access tokens have client_id but no aud claim, so the audience
            # is checked manually after decoding.
            claims = jwt.decode(
                token,
                public_key,
                algorithms=list(_ALLOWED_ALGORITHMS),
                options={"verify_exp": True, "verify_iss": True, "verify_aud": False},
                issuer=self._issuer,
                leeway=30,
            )

            # Cognito marks access tokens with token_use; other IdPs use the RFC 9068 header type.
            is_access_token = (
                claims.get("token_use", "id") != "id" or str(header.get("typ", "")).lower() in _ACCESS_TOKEN_TYPES
            )
            if self._require_id_token and is_access_token:
                raise ValueError("Token rejected: only an ID token is accepted")
            if self._audiences:
                aud = claims.get("aud")
                if aud is None and not self._require_id_token:
                    aud = claims.get("client_id")
                if isinstance(aud, str):
                    aud_list = [aud]
                elif isinstance(aud, list):
                    aud_list = [a for a in aud if isinstance(a, str)]
                else:
                    aud_list = []  # a number, dict or other shape is not a valid aud
                if not self._audiences.intersection(aud_list):
                    raise ValueError(f"Token audience mismatch: expected one of {sorted(self._audiences)}, got {aud}")

            return claims
        except PyJWTError as exc:
            logger.warning("JWT validation failed", error=str(exc))
            raise ValueError(f"Invalid token: {exc}") from exc

    @staticmethod
    def _extract_bearer_token(authorization_header: str) -> str:
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise ValueError("Missing or malformed Authorization header")
        token = authorization_header.removeprefix("Bearer ")
        if not token:
            raise ValueError("Bearer token is empty")
        return token

    def _build_result(self, claims: dict[str, Any]) -> TokenValidationResult:
        email = self._extract_email(claims)
        if not email:
            raise ValueError("Token missing required email claim")
        return TokenValidationResult(
            principal_id=email,
            sub=str(claims.get("sub", "")),
            email=email,
            groups=self._extract_groups(claims),
            token_type=self._get_token_type(claims),
            claims=claims,
            agent_identity=self._get_agent_identity(claims),
        )
