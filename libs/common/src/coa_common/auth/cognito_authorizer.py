# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cognito-specific token authorizer."""

from __future__ import annotations

from typing import Any

from coa_common.auth.token_authorizer import TokenAuthorizer


class CognitoTokenAuthorizer(TokenAuthorizer):
    """Validates JWTs issued by Amazon Cognito User Pools.

    Cognito tokens use ``cognito:groups`` for group membership.

    Parameters
    ----------
    user_pool_id:
        The Cognito User Pool ID (e.g. ``us-east-1_ABC123``).
    region:
        AWS region where the user pool is deployed. **Required**.
    client_id:
        Optional app client ID for audience validation.
    """

    def __init__(
        self,
        *,
        user_pool_id: str,
        region: str,
        client_id: str | None = None,
    ) -> None:
        """Derive the Cognito issuer and JWKS URI from the pool coordinates."""
        issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        jwks_uri = f"{issuer}/.well-known/jwks.json"
        super().__init__(issuer=issuer, audience=client_id, jwks_uri=jwks_uri)

    def _extract_email(self, claims: dict[str, Any]) -> str:
        """Extract email — falls back to username for access tokens."""
        return str(claims.get("email") or claims.get("username", ""))

    def _extract_groups(self, claims: dict[str, Any]) -> list[str]:
        groups = claims.get("cognito:groups", [])
        return list(groups) if isinstance(groups, list) else []
