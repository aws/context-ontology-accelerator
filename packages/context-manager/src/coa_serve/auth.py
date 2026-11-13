# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""JWT claims extraction for authenticated requests.

AgentCore validates the JWT (signature, expiry, client_id) before the
entrypoint runs. This module only decodes the payload to extract
user identity, no cryptographic verification needed.

Requires `requestHeaderAllowlist: ["Authorization"]` on the AgentCore
Runtime so the original Bearer token is forwarded via context.request_headers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import jwt
import structlog
from coa_common.auth.token_authorizer import extract_groups_claim

logger = structlog.get_logger(__name__)

# IdP group claim name (mirrors the control-plane authorizer's GROUP_CLAIM_NAME).
_GROUP_CLAIM_NAME = os.environ.get("GROUP_CLAIM_NAME", "groups")


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Normalized identity claims extracted from a validated JWT."""

    user_id: str  # OIDC 'sub' claim — unique across all IdPs
    email: str = ""  # 'email' claim when present (principal id for grants)
    groups: list[str] = field(default_factory=list)  # IdP group memberships (Cedar input)

    @classmethod
    def from_request_context(cls, context) -> TokenClaims | None:
        """Extract claims from the AgentCore HTTP request context.

        AgentCore passes allowlisted headers via context.request_headers (dict).

        Returns None if no Authorization header or missing 'sub' claim.
        """
        if context is None:
            return None
        headers = getattr(context, "request_headers", None)
        if not isinstance(headers, dict):
            return None
        auth_header = headers.get("authorization", "") or headers.get("Authorization", "")
        return cls._from_auth_header(auth_header) if auth_header else None

    @classmethod
    def _from_auth_header(cls, auth_header: str) -> TokenClaims | None:
        """Parse a Bearer token from an Authorization header value."""
        if not auth_header.startswith("Bearer "):
            return None

        token = auth_header[7:]
        # Skip signature validation — AgentCore already verified the token.
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.DecodeError:
            logger.warning("jwt_decode_failed", exc_info=True)
            return None

        sub = claims.get("sub")
        if not sub:
            # Log only the claim NAMES present (not values) — the decoded JWT
            # can contain email/phone/name/custom IdP attrs (PII). Names preserve
            # diagnostics without leaking PII into logs.
            logger.warning("jwt_missing_sub", present_claims=sorted(claims.keys()))
            return None

        # groups feed Cedar namespace authorization.
        groups = extract_groups_claim(claims, _GROUP_CLAIM_NAME)

        return cls(user_id=sub, email=str(claims.get("email", "")), groups=groups)
