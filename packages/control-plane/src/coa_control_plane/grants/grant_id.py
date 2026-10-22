# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grant ID encoding — derives external ID from DynamoDB PK+SK."""

from __future__ import annotations

import base64
import re

_SEPARATOR = "|"
# grant.smithy GrantId: @length(min: 1, max: 1024), @pattern("^[A-Za-z0-9_-]+$").
GRANT_ID_MAX_LENGTH = 1024
GRANT_ID_PATTERN = "^[A-Za-z0-9_-]+$"
_GRANT_ID_RE = re.compile(GRANT_ID_PATTERN)


def encode_grant_id(pk: str, sk: str) -> str:
    """Encode PK+SK into a URL-safe opaque grant identifier."""
    return base64.urlsafe_b64encode(f"{pk}{_SEPARATOR}{sk}".encode()).decode().rstrip("=")


def decode_grant_id(grant_id: str) -> tuple[str, str]:
    """Decode a grant identifier back into (PK, SK).

    Raises ValueError on any malformed input, including an identifier outside
    the GrantId contract (length or alphabet).
    """
    if not isinstance(grant_id, str) or len(grant_id) > GRANT_ID_MAX_LENGTH or not _GRANT_ID_RE.fullmatch(grant_id):
        raise ValueError("Invalid grant ID: cannot decode")
    try:
        padded = grant_id + "=" * (-len(grant_id) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid grant ID: cannot decode") from exc
    parts = decoded.split(_SEPARATOR, 1)
    if len(parts) != 2:
        raise ValueError("Invalid grant ID format: missing separator")
    return parts[0], parts[1]
