# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""IdP-agnostic token authorizer construction.

Picks ``CognitoTokenAuthorizer`` for a Cognito user-pool issuer, or
``OIDCTokenAuthorizer`` for any other OIDC-compliant IdP (Okta, EntraID,
Auth0, ...). Single implementation shared by every caller that needs to
verify a bearer JWT against an issuer supplied via configuration, so a new
caller never has to re-derive the Cognito-vs-generic-OIDC dispatch.
"""

from __future__ import annotations

from collections.abc import Collection
from urllib.parse import urlparse

from coa_common.auth.cognito_authorizer import CognitoTokenAuthorizer
from coa_common.auth.oidc_authorizer import OIDCTokenAuthorizer
from coa_common.auth.token_authorizer import TokenAuthorizer


def is_cognito_issuer(issuer: str) -> bool:
    """True when *issuer* is an Amazon Cognito user-pool issuer URL.

    Parses the URL and matches on the host label rather than searching the raw
    string. A substring test such as ``"cognito-idp" in issuer`` would also
    match an attacker-controlled host that merely embeds the token — e.g.
    ``https://cognito-idp.amazonaws.com.evil.test/pool`` or a URL carrying it
    in the path or query — and would pick the Cognito authorizer for a
    non-Cognito identity provider.
    """
    parsed = urlparse(issuer)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    # Canonical form: cognito-idp.<region>.amazonaws.com (or .amazonaws.com.cn
    # in the China partition).
    labels = host.split(".")
    return (
        len(labels) >= 4
        and labels[0] == "cognito-idp"
        and (host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn"))
    )


def build_token_authorizer(
    *,
    issuer: str,
    region: str,
    client_id: str | Collection[str] | None = None,
    jwks_uri: str | None = None,
    group_claim_name: str = "groups",
    require_id_token: bool = False,
) -> TokenAuthorizer:
    """Construct the right ``TokenAuthorizer`` subclass for *issuer*.

    Args:
        issuer: The expected ``iss`` claim value (a Cognito user-pool issuer
            URL, or any other OIDC issuer URL).
        region: AWS region — only used when *issuer* is a Cognito issuer, to
            derive the user pool's JWKS URI.
        client_id: Optional audience/client ID, or a collection of accepted
            IDs (any one matches), for ``aud``/``client_id`` validation.
        jwks_uri: Explicit JWKS endpoint for non-Cognito IdPs. Ignored for
            Cognito, which derives its JWKS URI from the issuer. Falls back
            to OIDC discovery (``/.well-known/openid-configuration``) if
            unset.
        group_claim_name: JWT claim holding group memberships, for non-Cognito
            IdPs (Cognito always uses ``cognito:groups``). Defaults to
            ``"groups"`` — override for IdPs using a non-standard claim name
            (e.g. EntraID's ``roles``).
        require_id_token: Accept only ID tokens (see ``TokenAuthorizer``).
    """
    if is_cognito_issuer(issuer):
        user_pool_id = issuer.rstrip("/").rsplit("/", maxsplit=1)[-1]
        return CognitoTokenAuthorizer(
            user_pool_id=user_pool_id, region=region, client_id=client_id, require_id_token=require_id_token
        )
    return OIDCTokenAuthorizer(
        issuer=issuer,
        audience=client_id,
        jwks_uri=jwks_uri,
        group_claim_name=group_claim_name,
        require_id_token=require_id_token,
    )
