# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opaque ``nextToken`` codec for the ontology engine's paginated list routes.

A token is the base64url (unpadded) encoding of the JSON DynamoDB key a listing
resumes from, tagged with the listing it belongs to:
``{"PK": ..., "SK": ..., "scope": ...}``. It is opaque to callers: the Smithy
``PaginationToken`` contract only promises "pass it back unchanged".

The key is either the last item a page returned or, when a request stopped
early on its read budget, DynamoDB's own ``LastEvaluatedKey`` — which may be a
row the listing filters out (another namespace's row in a table scan). That is
why the token is bound to a ``scope`` (e.g. ``jobs:{namespace}``) rather than
to the key's ``PK``: every token a route mints decodes for the same route and
namespace, whatever row the scan stopped on.

Decoding is strict because the token is caller-controlled and is handed straight
to DynamoDB as ``ExclusiveStartKey``. Anything that is not exactly a two-string
key plus the expected scope is rejected with :class:`InvalidNextToken` (the
routes answer 400) instead of reaching DynamoDB and surfacing as a 500. The
scope check stops a token minted for one namespace or listing being accepted by
another and resuming at an arbitrary point. It is not an access control: the
listing's own filter still selects only the caller's rows, so a hand-edited key
can at worst move the resume point within the caller's own listing.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# Upper bound on an incoming token, checked before decoding so an oversized
# value is never base64-decoded or JSON-parsed. Equal to the Smithy
# ``PaginationToken`` @length max (8192, common.smithy) — the ontology listing's
# key embeds an ontology id of up to 2048 characters, so a tighter bound could
# refuse a token this module minted itself.
MAX_TOKEN_LENGTH = 8192

_TOKEN_ATTRIBUTES = frozenset({"PK", "SK", "scope"})


class InvalidNextToken(ValueError):
    """The caller's ``nextToken`` is malformed or was not minted for this listing.

    The message is always the generic "Invalid nextToken." so a caller cannot use
    it to probe the token format; ``reason`` says which check failed and is only
    logged.
    """

    def __init__(self, reason: str) -> None:
        """Record which check failed (``reason``) behind the generic message."""
        super().__init__("Invalid nextToken.")
        self.reason = reason


def _reject(reason: str, *, scope: str) -> InvalidNextToken:
    logger.info("invalid_next_token reason=%s scope=%s", reason, scope)
    return InvalidNextToken(reason)


def encode_next_token(key: dict[str, Any] | None, *, scope: str) -> str | None:
    """Encode a DynamoDB resume key for listing ``scope`` (``None`` passes through)."""
    if not key:
        return None
    raw = json.dumps(
        {"PK": key["PK"], "SK": key["SK"], "scope": scope},
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_next_token(token: str | None, *, scope: str, pk: str | None = None) -> dict[str, str] | None:
    """Decode ``token`` back to a DynamoDB key, or raise :class:`InvalidNextToken`.

    Args:
        token: The caller's ``nextToken``; ``None``/empty means "first page".
        scope: The listing the token must have been minted for.
        pk: When set, the key's ``PK`` must equal it. For single-partition
            ``Query`` listings, where DynamoDB rejects a start key outside the
            queried partition.

    Returns:
        ``{"PK": ..., "SK": ...}``, or ``None`` for the first page.
    """
    if not token:
        return None
    if len(token) > MAX_TOKEN_LENGTH:
        raise _reject("too_long", scope=scope)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        decoded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _reject("undecodable", scope=scope) from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != _TOKEN_ATTRIBUTES
        or not all(isinstance(v, str) and v for v in decoded.values())
    ):
        raise _reject("wrong_shape", scope=scope)
    if decoded["scope"] != scope:
        raise _reject("wrong_scope", scope=scope)
    if pk is not None and decoded["PK"] != pk:
        raise _reject("wrong_partition", scope=scope)
    return {"PK": decoded["PK"], "SK": decoded["SK"]}


def json_native_numbers(value: Any) -> Any:
    """Return ``value`` with every ``Decimal`` replaced by an ``int`` (whole) or ``float``.

    boto3 reads every DynamoDB number back as ``Decimal``. The list routes carry a
    return annotation, so FastAPI serializes them through Pydantic's JSON mode,
    which writes a ``Decimal`` as a JSON string (``"2"``) where the contract types
    an ``Integer``. Walks dicts and lists/tuples (a tuple comes back as a list, as
    it would on the wire); every other value, including Pydantic models, is
    returned unchanged.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: json_native_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_native_numbers(v) for v in value]
    return value


def page_body(key: str, items: list, next_key: dict[str, Any] | None, *, scope: str) -> dict[str, Any]:
    """A list operation's output envelope: ``{key: items}`` plus ``nextToken`` when more may follow.

    The items are usually raw DynamoDB rows, so their ``Decimal`` numbers are
    converted to JSON numbers here (see :func:`json_native_numbers`).
    """
    body: dict[str, Any] = {key: json_native_numbers(items)}
    token = encode_next_token(next_key, scope=scope)
    if token:
        body["nextToken"] = token
    return body
