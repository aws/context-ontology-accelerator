# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Checks for a caller-supplied DynamoDB resume key (a decoded ``nextToken``).

List handlers mint ``nextToken`` from DynamoDB's ``LastEvaluatedKey`` and,
on the next request, hand the decoded value back as ``ExclusiveStartKey``.
The token is caller-controlled, so anything that decodes (``{"foo": "bar"}``,
``[1, 2]``, a key minted for another namespace) would otherwise reach
DynamoDB, fail there with a ``ValidationException`` and surface as a 500.

:func:`check_start_key` rejects every value that is not exactly a key of the
listing's index with the expected partition, and :func:`is_rejected_start_key`
recognises DynamoDB's own refusal of a key that passed it (e.g. one outside the
query's sort-key range), so a handler can answer both with a 400.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from botocore.exceptions import ClientError

__all__ = ["check_start_key", "is_rejected_start_key"]


def check_start_key(
    key: Any,
    attributes: Collection[str],
    *,
    expected: Mapping[str, str] | None = None,
    prefixes: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return ``key`` if it is a well-formed resume key for the listing, else raise ``ValueError``.

    Args:
        key: The decoded token.
        attributes: The exact key attributes of the queried table or index
            (base-table keys plus the index's own keys).
        expected: Attributes whose value is fixed by the request (the
            partition the query reads), e.g. ``{"namespaceKey": "NS#<id>"}``.
        prefixes: Attributes whose value must start with a given prefix (a
            ``begins_with`` sort-key condition).

    Raises:
        ValueError: With a generic message that never echoes the token.
    """
    if (
        not isinstance(key, dict)
        or set(key) != set(attributes)
        or not all(isinstance(v, str) and v for v in key.values())
    ):
        raise ValueError("not a key for this listing")
    for attr, value in (expected or {}).items():
        if key.get(attr) != value:
            raise ValueError("not a key for this listing")
    for attr, prefix in (prefixes or {}).items():
        if not key.get(attr, "").startswith(prefix):
            raise ValueError("not a key for this listing")
    return key


def is_rejected_start_key(exc: BaseException) -> bool:
    """True if ``exc`` is DynamoDB refusing the request as invalid (``ValidationException``).

    Only meaningful when the request carried a caller-supplied
    ``ExclusiveStartKey``: the handler's own parameters are fixed, so the
    caller's key is the only input DynamoDB could be rejecting.
    """
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ValidationException"
