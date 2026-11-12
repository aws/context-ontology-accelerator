# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Generic OIDC token authorizer (Okta, EntraID, Auth0, etc.)."""

from __future__ import annotations

from typing import Any

from coa_common.auth.token_authorizer import TokenAuthorizer, extract_groups_claim


class OIDCTokenAuthorizer(TokenAuthorizer):
    """Validates JWTs from any OIDC-compliant identity provider.

    Parameters
    ----------
    issuer:
        The expected ``iss`` claim value.
    audience:
        Optional audience for ``aud`` validation.
    jwks_uri:
        Explicit JWKS endpoint. Falls back to OIDC discovery if ``None``.
    group_claim_name:
        JWT claim containing group memberships. Defaults to ``"groups"``.
        Override when the IdP uses a non-standard claim name.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | None = None,
        jwks_uri: str | None = None,
        group_claim_name: str = "groups",
    ) -> None:
        """Configure OIDC validation and the group-claim name to read."""
        super().__init__(issuer=issuer, audience=audience, jwks_uri=jwks_uri)
        self._group_claim_name = group_claim_name

    def _extract_groups(self, claims: dict[str, Any]) -> list[str]:
        return extract_groups_claim(claims, self._group_claim_name)
