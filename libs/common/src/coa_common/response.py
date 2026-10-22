# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared API Gateway Lambda proxy utilities."""

from __future__ import annotations

import calendar
import datetime
import json
import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# Returned by get_caller_identity when the event carries no caller identity.
UNKNOWN_CALLER = "unknown"


def _get_allowed_origin() -> str:
    origin = os.environ.get("ALLOWED_ORIGIN", "")
    if not origin:
        raise RuntimeError(
            "ALLOWED_ORIGIN environment variable must be set. "
            "This is required for any Lambda that returns HTTP responses."
        )
    return origin


def cors_headers() -> dict[str, str]:
    """CORS headers for an API Gateway proxy response (reads ``ALLOWED_ORIGIN``)."""
    return {
        "Access-Control-Allow-Origin": _get_allowed_origin(),
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    }


# Backward-compatible private alias for existing callers.
_cors_headers = cors_headers


# Backward-compatible module-level constant — evaluates lazily on first access
# via api_response(). Services that never call api_response() won't crash.
CORS_HEADERS: dict[str, str] = {}


def api_response(status: int, body: dict, extra_headers: dict | None = None) -> dict[str, Any]:
    """Build an API Gateway proxy response with CORS headers."""
    headers = {"Content-Type": "application/json", **cors_headers()}
    if extra_headers:
        headers.update(extra_headers)
    return {"statusCode": status, "headers": headers, "body": json.dumps(body, default=str)}


def iso_to_epoch(value: Any) -> int | Any:
    """Convert an ISO 8601 timestamp string to a Unix epoch integer.

    Returns the value unchanged if it is not a string or cannot be parsed.
    """
    if not isinstance(value, str):
        return value
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(calendar.timegm(dt.utctimetuple()))
    except (ValueError, AttributeError):
        return value


def get_caller_identity(event: dict[str, Any]) -> str:
    """Extract the authenticated caller identity from an API Gateway event.

    Resolution order:
    1. Custom Lambda authorizer context (flat keys: email, userId, principalId)
    2. Cognito authorizer claims (email, then sub)
    3. IAM identity (userArn)

    Returns :data:`UNKNOWN_CALLER` (``"unknown"``) if none of the above are present.
    """
    rc = event.get("requestContext", {})
    authorizer = rc.get("authorizer", {})

    # 1. Custom authorizer context (flat keys set by token authorizer)
    caller = authorizer.get("email") or authorizer.get("userId") or authorizer.get("principalId")
    if caller:
        return caller

    # 2. Cognito authorizer claims
    claims = authorizer.get("claims", {})
    if claims:
        caller = claims.get("email") or claims.get("sub")
        if caller:
            return caller

    # 3. IAM identity
    identity = rc.get("identity", {})
    if identity and identity.get("userArn"):
        return identity["userArn"]

    logger.warning("no_caller_identity_in_event")
    return UNKNOWN_CALLER
