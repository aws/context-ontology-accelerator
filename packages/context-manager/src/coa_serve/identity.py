# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Identity resolution for the invoke entrypoint.

Trust Model
-----------
There are two trusted sources of user identity, depending on the caller:

1. **JWT in Authorization header** (highest trust): AgentCore validates the JWT
   (signature, expiry, audience) before forwarding the request. We decode without
   signature check because AgentCore already did that. This gives us the 'sub'
   (user_id) and 'email' claims. The data-layer Lambda also forwards the original
   JWT when invoking via AgentCore, so JWT is always present for authenticated callers.

2. **profile.userId** (fallback only): For any future internal caller that invokes
   the runtime without forwarding a Bearer token. This path should NOT override
   a validated JWT since the request body is attacker-controlled for direct callers.

Priority: JWT sub > JWT email > profile.userId. The JWT takes precedence because
it is cryptographically validated by AgentCore before the request reaches this code.
The profile path is a fallback for edge cases where no JWT is forwarded.
"""

from __future__ import annotations

import structlog

from .auth import TokenClaims

logger = structlog.get_logger(__name__)


def extract_jwt_identity(context) -> tuple[str, str, list[str]]:
    """Extract user identity from the AgentCore request context.

    Returns (user_id, email, groups) from the JWT forwarded by AgentCore.
    AgentCore validates the token before forwarding, so we only decode.
    Returns empty strings/list if context is None or extraction fails.
    """
    if not context:
        return "", "", []

    try:
        claims = TokenClaims.from_request_context(context)
        if claims:
            return claims.user_id, claims.email, claims.groups
    except Exception as e:
        logger.warning("jwt_extraction_failed", error=str(e), context_type=type(context).__name__)

    return "", "", []


def resolve_user_id(payload: dict, jwt_user_id: str, jwt_email: str) -> str:
    """Resolve the authoritative user ID with explicit priority.

    Priority order (JWT is authoritative when present):
    1. JWT sub (user_id) - from AgentCore-validated Bearer token
    2. JWT email - fallback when sub is unavailable
    3. profile.userId - fallback for internal callers without JWT forwarding

    This function does not validate tokens. Validation is performed by
    AgentCore Runtime before the request reaches this code.
    """
    if jwt_user_id:
        return jwt_user_id
    if jwt_email:
        return jwt_email
    profile = payload.get("profile") or {}
    return profile.get("userId", "") if isinstance(profile, dict) else ""


def resolve_principal(
    profile: dict,
    jwt_user_id: str,
    jwt_email: str,
    jwt_groups: list[str],
) -> tuple[str, list[str]]:
    """Resolve the authoritative ``(user_id, groups)`` for authorization.

    JWT is authoritative; the body is attacker-controlled for direct Playground
    callers, so it must never override the AgentCore-validated token:

    * ``user_id``: JWT sub → JWT email → ``profile.userId`` → ``profile.email``.
    * ``groups``: the JWT groups whenever a JWT identity was extracted (even if the
      list is empty); only with NO JWT at all do we fall back to
      ``profile.groups``. A comma-joined string is normalized to a list, and any
      other type is discarded, so a malformed ``groups`` cannot violate the
      ``list[str]`` contract.
    """
    profile = profile or {}
    user_id = jwt_user_id or jwt_email or profile.get("userId") or profile.get("email") or ""
    groups = jwt_groups if (jwt_user_id or jwt_email) else (profile.get("groups") or [])
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    elif not isinstance(groups, list):
        # Fail closed on a malformed body value: no groups rather than a value the
        # grant resolver / Cedar would then have to interpret.
        logger.warning("resolve_principal_groups_malformed", groups_type=type(groups).__name__)
        groups = []
    else:
        # A list whose members are not strings would reach Cedar as-is; keep only
        # the usable entries so the return type is honoured element-wise too.
        groups = [g for g in groups if isinstance(g, str) and g.strip()]
    return user_id, groups


def display_principal(profile: dict | None) -> str | None:
    """Human-readable caller identity for response metadata and trace details.

    Prefers the ``email`` claim over ``profile["userId"]`` (the JWT ``sub``). A
    display label only — it never feeds authorization, which reads ``userId``.
    Only the JWT-derived email reaches the profile (``main.py`` strips a
    client-supplied ``email``), so this cannot be spoofed by the request body.
    """
    p = profile or {}
    return p.get("email") or p.get("userId")
