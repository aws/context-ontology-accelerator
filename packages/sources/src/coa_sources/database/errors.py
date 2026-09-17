# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Classified scan failures for discovery retry control.

The DB scan state machine retries the discovery step, but only *transient*
failures (connectivity, throttling, transient service errors) benefit from a
retry. *Permanent* failures — bad configuration, denied credentials, a missing
database, an unsupported source type — fail identically on every attempt, so
retrying them just wastes ~15s (three attempts with backoff) before the source
lands in SCAN_FAILED.

Discovery raises one of these so the state machine can scope its retry to
``TransientScanError`` (AWS Lambda reports the raised exception's class name as
the Step Functions error name, which the retry policy matches on). Both subclass
``RuntimeError`` so existing callers and tests that catch/expect ``RuntimeError``
are unaffected.
"""

from __future__ import annotations

from botocore.exceptions import ClientError


class ScanError(RuntimeError):
    """Base class for a classified discovery scan failure."""


class TransientScanError(ScanError):
    """A retryable scan failure (connectivity, throttling, transient service errors)."""


class PermanentScanError(ScanError):
    """A non-retryable scan failure (bad config, denied auth, missing resource, unsupported type)."""


# botocore error codes that mean the request can never succeed as sent — auth,
# missing resource, or malformed input — so retrying is futile. Anything else
# (throttling, timeouts, 5xx, connectivity, unknown) is treated as transient.
_PERMANENT_CLIENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "UnauthorizedException",
        "EntityNotFoundException",
        "ResourceNotFoundException",
        "ValidationException",
        "InvalidInputException",
        "InvalidRequestException",
    }
)


def is_permanent_scan_error(exc: Exception) -> bool:
    """Classify a discovery failure as permanent (no retry) vs transient.

    Permanent: our own validation (``ValueError`` — unsupported type, source not
    found, too many tables) and botocore auth / not-found / validation errors.
    Everything else defaults to transient so a genuine blip still gets retried.
    """
    if isinstance(exc, ValueError):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        return code in _PERMANENT_CLIENT_ERROR_CODES or "AccessDenied" in code or "NotFound" in code
    return False
