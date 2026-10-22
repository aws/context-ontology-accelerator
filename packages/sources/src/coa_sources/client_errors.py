# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client-safe failure text for messages a caller reads back through the API.

A scan or ingestion failure is stored on the source / scan-job row and returned
by GetSource, GetSourceScanJob and ListSourceScanJobs. Storing ``str(exc)``
there hands the caller whatever the failing library put in its message: AWS
error text naming role and resource ARNs and account ids, service endpoints,
and — for the document pipeline — whole tracebacks. These helpers turn an
exception (or an already-stored message) into text that keeps what the caller
can act on (our own explanatory messages, the AWS error code and operation)
and drops the internals. The full exception is still logged at the failure site.
"""

from __future__ import annotations

import re

from botocore.exceptions import BotoCoreError, ClientError

# Long enough for every explanatory message the pipeline raises on purpose.
MAX_CLIENT_ERROR_CHARS = 500

GENERIC_FAILURE = "The operation failed; see the service logs for details."

_ARN_RE = re.compile(r"\barn:aws[a-z-]*:[^\s'\"`,;)\]}]+")
# A UUID is matched first, as its own alternative, so the account-id rule never
# fires on a UUID whose last group happens to be twelve digits.
_ACCOUNT_RE = re.compile(
    r"(?P<uuid>\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b)|(?<!\d)\d{12}(?!\d)"
)
# AWS service endpoints and VPC endpoint / cluster hostnames.
_AWS_HOST_RE = re.compile(r"\b[\w.-]+\.(?:amazonaws\.com(?:\.cn)?|on\.aws)\b(?::\d+)?")
_TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "/')


class ClientSafeError(Exception):
    """An exception whose message this package wrote for the caller to read.

    Its text names only what the caller supplied or owns (their database, their
    namespace, their account) plus the command that fixes the problem, so
    :func:`client_safe_error` returns it verbatim: no truncation and no ARN or
    account-id redaction, either of which would break the copy-paste fix it
    carries. Never put library or AWS service text in such a message; pass that
    through :func:`client_safe_error` first.

    Rows that store this text must also record that it is client-safe (see
    :data:`CLIENT_SAFE_FLAG`), so the read path does not redact it again.
    """


# Stored alongside an ``errorMessage`` written from a :class:`ClientSafeError`;
# readers hand such a message back unchanged instead of re-redacting it.
CLIENT_SAFE_FLAG = "errorMessageClientSafe"


def _keep_uuid(match: re.Match[str]) -> str:
    return match.group("uuid") or "<account>"


def _redacted_or_empty(text: str) -> str:
    """:func:`redact` without the generic fallback: ``""`` when nothing safe is left."""
    if any(marker in text for marker in _TRACEBACK_MARKERS):
        return ""
    text = _ARN_RE.sub("<arn>", text)
    text = _AWS_HOST_RE.sub("<aws-endpoint>", text)
    text = _ACCOUNT_RE.sub(_keep_uuid, text)
    text = text.strip()
    if len(text) > MAX_CLIENT_ERROR_CHARS:
        text = text[: MAX_CLIENT_ERROR_CHARS - 1].rstrip() + "…"
    return text


def redact(text: str, *, client_safe: bool = False) -> str:
    """Strip ARNs, account ids and AWS hostnames from ``text`` and bound its length.

    A traceback is never returned: it is replaced by :data:`GENERIC_FAILURE`.
    ``client_safe=True`` marks text written from a :class:`ClientSafeError`
    (see :data:`CLIENT_SAFE_FLAG`); it is returned unchanged.
    """
    if client_safe and text.strip():
        return text
    return _redacted_or_empty(text) or GENERIC_FAILURE


def client_safe_error(exc: BaseException) -> str:
    """Caller-facing description of ``exc``.

    * AWS service errors become ``"<Operation> failed: <ErrorCode>"`` — the code
      (e.g. ``AccessDeniedException``) is what the caller needs; the message is
      where ARNs and account ids live.
    * Client-side botocore faults (DNS, TLS, timeouts) name only their type.
    * A :class:`ClientSafeError` was written for the caller: returned verbatim.
    * ``ValueError`` / ``RuntimeError`` and their subclasses are what this
      package raises on purpose with an explanatory message (an over-large
      source, a failed connection test); their text is kept but redacted.
    * Anything else (a library fault such as an unreadable PDF or a corrupt
      archive) keeps its type and its redacted text, which is usually the only
      actionable reason the caller gets; its type alone when no text survives
      redaction.
    """
    if isinstance(exc, ClientSafeError) and str(exc).strip():
        return str(exc)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code") or "UnknownError"
        operation = getattr(exc, "operation_name", "") or "AWS request"
        return f"{operation} failed: {code}"
    if isinstance(exc, BotoCoreError):
        return f"Could not reach an AWS service ({type(exc).__name__}); retry the operation."
    text = _redacted_or_empty(str(exc))
    if isinstance(exc, (ValueError, RuntimeError)):
        return text or GENERIC_FAILURE
    if text:
        return f"{type(exc).__name__}: {text}"
    return f"Unexpected {type(exc).__name__}; see the service logs for details."


def is_client_safe(exc: BaseException) -> bool:
    """Whether :func:`client_safe_error` returns ``exc``'s message verbatim."""
    return isinstance(exc, ClientSafeError) and bool(str(exc).strip())
