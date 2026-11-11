# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""JWT claims extraction with independent signature verification.

Verifies the JWT signature here as defense in depth, in case the AgentCore
authorizer is misconfigured or bypassed. Reuses ``coa_common.auth``; the issuer
comes from ``JWT_ISSUER_URL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import structlog
from coa_common.auth import TokenAuthorizer, build_token_authorizer

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CallerIdentity:
    """Extracted identity from the JWT claims."""

    user_id: str
    agent_id: str | None
    namespaces: list[str]
    roles: list[str]
    raw_claims: dict
    email: str = ""


class ClaimsExtractionError(Exception):
    """Raised when the token is missing, invalid, or lacks required claims."""


@lru_cache(maxsize=1)
def _get_authorizer() -> TokenAuthorizer:
    """Build the token authorizer once per container.

    Missing configuration raises on first call: fail closed, never skip verification.
    """
    issuer = os.environ["JWT_ISSUER_URL"]
    return build_token_authorizer(
        issuer=issuer,
        region=os.environ["AWS_REGION"],
        client_id=os.environ.get("JWT_CLIENT_ID"),
        jwks_uri=os.environ.get("JWT_JWKS_URI") or None,
        group_claim_name=os.environ.get("GROUP_CLAIM_NAME", "groups"),
    )


def extract_caller_identity(authorization_header: str | None) -> CallerIdentity:
    """Verify the JWT's signature and extract the delegating user's identity.

    Raises:
        ClaimsExtractionError: If the header is missing, malformed, the
            signature/issuer/audience/expiry check fails, or required claims
            are absent.
    """
    if not authorization_header:
        raise ClaimsExtractionError("Missing Authorization header")

    authorizer = _get_authorizer()
    try:
        # verify_claims does the crypto verification (signature/issuer/audience/
        # expiry) but skips validate()'s email requirement — MCP callers are
        # keyed by "sub"/"username", never carry email.
        claims = authorizer.verify_claims(authorization_header)
    except ValueError as e:
        logger.warning("jwt_verification_failed", error=str(e))
        raise ClaimsExtractionError(f"JWT verification failed: {e}") from e

    user_id = claims.get("sub") or claims.get("user_id") or claims.get("username")
    if not user_id:
        raise ClaimsExtractionError("JWT lacks required 'sub' / 'user_id' claim for delegating user")

    agent_id = claims.get("agent_id") or claims.get("client_id") or claims.get("azp")

    namespaces = _extract_list_claim(claims, "namespaces", "custom:namespaces", "scl_namespaces")

    roles = authorizer.extract_groups(claims)

    logger.info(
        "caller_identity_extracted",
        user_id=user_id,
        agent_id=agent_id,
        namespace_count=len(namespaces),
    )

    return CallerIdentity(
        user_id=user_id,
        agent_id=agent_id,
        namespaces=namespaces,
        roles=roles,
        raw_claims=claims,
        email=claims.get("email", "") or "",
    )


def _extract_list_claim(claims: dict, *keys: str) -> list[str]:
    """Try multiple claim keys, return the first non-empty list found."""
    for key in keys:
        value = claims.get(key)
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str) and value:
            return [v.strip() for v in value.split(",") if v.strip()]
    return []
