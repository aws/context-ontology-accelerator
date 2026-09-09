# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serve-layer orchestration exceptions.

Neutral home for cross-tier exceptions raised at the orchestration boundary.
Tier-specific safety exceptions (e.g. ``UnsafeSQLError``) stay with their
module (``tier2/sql_firewall.py``).

Each exception maps to a Smithy error shape in ``models/src/main/smithy/serve.smithy``
and carries the HTTP status code for handler-boundary serialization.
"""

from __future__ import annotations


class ServeError(Exception):
    """Base for all serve-layer typed errors."""

    status_code: int = 500
    error_type: str = "InternalError"

    def __init__(self, message: str):
        """Store the human-readable message and pass it to the base Exception.

        Args:
            message: Human-readable error description.
        """
        self.message = message
        super().__init__(message)

    def to_dict(self, request_id: str) -> dict:
        """Serialize the error into the API's JSON error shape.

        Args:
            request_id: Request identifier echoed back to the caller.

        Returns:
            A dict with the error type, message, request id, and HTTP status code.
        """
        return {
            "error": self.error_type,
            "message": self.message,
            "requestId": request_id,
            "statusCode": self.status_code,
        }


class AccessDeniedError(ServeError):
    """SQL firewall data-level deny — HTTP 403."""

    status_code = 403
    error_type = "AccessDeniedError"

    def __init__(self, reason: str | None = None):
        """Record the internal deny reason without leaking it in the client message.

        Args:
            reason: Internal explanation for the denial (not exposed to callers).
        """
        self.reason = reason
        super().__init__("Access denied")


class NamespaceScopeDeniedError(AccessDeniedError):
    """A SQL reference names an object outside the requested namespace — HTTP 403.

    Subclass of :class:`AccessDeniedError` (same 403 / ``AccessDeniedError`` client
    error type), but this is a *policy* denial that states policy rather than data,
    so — unlike a data-level deny — its reason IS surfaced in the client message.
    """

    def __init__(self, reason: str):
        """Surface the policy reason as the client message.

        Bypasses :class:`AccessDeniedError`'s reason-hiding (which exists for
        data-level denies where the reason can be sensitive); a cross-namespace
        SQL reference is a policy statement, safe to return to the caller.
        """
        ServeError.__init__(self, reason)
        self.reason = reason


class QueryTranslationError(ServeError):
    """NL-to-SPARQL translation failed — HTTP 400."""

    status_code = 400
    error_type = "QueryTranslationError"


class DataSourceUnavailableError(ServeError):
    """Backend data source (OpenSearch, Neptune, Bedrock) unreachable — HTTP 502."""

    status_code = 502
    error_type = "DataSourceUnavailableError"


class NamespaceNotFoundError(ServeError):
    """Namespace UUID does not exist — HTTP 404."""

    status_code = 404
    error_type = "NamespaceNotFoundError"


class NoResultError(ServeError):
    """A pinned resolution tier produced no result — HTTP 404.

    A metric/tier miss is a normal "no data found" outcome, NOT a server fault.
    When a caller pins a tier (``tierOverride``) and that tier — having exhausted
    any permitted fall-through — yields nothing, the orchestrator raises this
    instead of letting a bare error surface as ``InternalError`` (500). Clients
    can then distinguish "nothing matched" from "server broke" and must not retry
    a deterministic miss. Mirrors the ``AccessDeniedError`` (403) terminal-outcome
    precedent; confirmed as the desired contract by the serve owners.
    """

    status_code = 404
    error_type = "NoResultError"

    def __init__(self, message: str = "No result for the requested tier", tier: int | None = None):
        """Record which pinned tier produced no result.

        Args:
            message: Human-readable error description.
            tier: The pinned resolution tier that yielded nothing, if known.
        """
        self.tier = tier
        super().__init__(message)
