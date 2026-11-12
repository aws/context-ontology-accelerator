# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for constructing DynamoDB principal key components.

Grants are stored in the ResourceRoleMappings table keyed by ``principalKey``
(e.g. ``User::alice@example.com``). Both the writers (grant-creation handlers)
and the readers (the API authorizer and the serve-layer role resolver) must
encode the principal identifier identically, or a grant written under one
encoding will never be found under another.

Centralizing the encoding here keeps every read and write site in lockstep, so
grants for emails with special characters (e.g. ``foo+123@example.com``) resolve.
"""

from __future__ import annotations

import urllib.parse


def sanitize_principal_key(value: str) -> str:
    """URL-encode a value used as a DynamoDB principal key component.

    Encodes characters that would otherwise corrupt the ``<type>::<id>`` key
    layout (e.g. ``+``, ``#``, ``/``) while leaving ``@`` and ``.`` intact so
    that email addresses remain human-readable in the stored keys.

    Identifiers containing ``@`` are treated as emails and are lowercased and
    stripped before encoding, so ``Alice@Example.com`` and ``alice@example.com``
    produce the same key. Other identifiers (``sub`` UUIDs, group names) are
    passed through verbatim because they are case-sensitive.
    """
    if "@" in value:
        value = value.strip().lower()
    return urllib.parse.quote(value, safe="@.")


def build_principal_keys(
    *,
    user_id: str,
    email: str,
    groups: list[str],
) -> list[str]:
    """Build the PrincipalIndex GSI lookup keys for a caller.

    Grants can be written under any identifier the caller presents (sub, email,
    group), so every known identifier is included; every reader of the
    PrincipalIndex GSI must use this helper. Duplicates are collapsed.
    Returns ``User::…`` keys followed by ``Group::…`` keys.
    """
    seen: set[str] = set()
    keys: list[str] = []
    for identifier in (user_id, email):
        if not identifier:
            continue
        key = f"User::{sanitize_principal_key(identifier)}"
        if key not in seen:
            seen.add(key)
            keys.append(key)
    for group in groups:
        key = f"Group::{sanitize_principal_key(group)}"
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys
