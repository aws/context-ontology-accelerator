# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared retry/timeout policy for outbound clients.

Single source of truth for the retry and socket-timeout configuration applied
to AWS SDK (boto3) clients and non-boto3 HTTP clients.

- Synchronous customer-path calls fail fast and retry at most once.
- Asynchronous/background calls use capped exponential backoff with jitter.

botocore's ``standard`` retry mode natively implements capped exponential
backoff with full jitter plus a retry token bucket, so we express both presets
through it rather than hand-rolling backoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from botocore.config import Config

# A connection attempt is a TCP/TLS handshake; it should always be fast,
# regardless of sync vs async call path.
DEFAULT_CONNECT_TIMEOUT = 2
# Customer-facing (synchronous) reads fail fast so a hung dependency cannot tie
# up request threads. Comfortably above healthy in-region p99 for our deps.
DEFAULT_SYNC_READ_TIMEOUT = 8
# Background/async reads tolerate slower dependencies (ingestion, LLM, catalog).
DEFAULT_ASYNC_READ_TIMEOUT = 30


def sync_boto_config(
    *,
    read_timeout: int = DEFAULT_SYNC_READ_TIMEOUT,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    max_attempts: int = 2,
    **overrides: Any,
) -> Config:
    """Build a botocore ``Config`` for synchronous customer-path clients (fail-fast, <=1 retry)."""
    return Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"mode": "standard", "max_attempts": max_attempts},
        **overrides,
    )


# --- Non-boto3 HTTP client policy -------------------------------------------

SYNC_HTTP_READ_TIMEOUT = 8.0
ASYNC_HTTP_READ_TIMEOUT = 30.0


def opensearch_client_kwargs(*, sync: bool) -> dict[str, Any]:
    """Retry/timeout kwargs for an opensearch-py ``OpenSearch`` client.

    Uses opensearch-py's native retry (``max_retries`` + ``retry_on_timeout``),
    so no extra dependency is needed. Sync callers fail fast (1 retry); async
    callers retry more with a longer read budget.
    """
    if sync:
        return {"timeout": int(SYNC_HTTP_READ_TIMEOUT), "max_retries": 1, "retry_on_timeout": True}
    return {"timeout": int(ASYNC_HTTP_READ_TIMEOUT), "max_retries": 3, "retry_on_timeout": True}


@dataclass(frozen=True)
class RetryPolicy:
    """Backoff policy for non-boto3 HTTP retry decorators (httpx/urllib).

    ``attempts`` counts the initial call plus retries (matching botocore's
    ``max_attempts`` convention). ``base_delay``/``max_delay`` bound a capped
    exponential backoff; callers add jitter.
    """

    attempts: int
    base_delay: float
    max_delay: float

    @classmethod
    def for_async(cls) -> RetryPolicy:
        """Lenient preset for async/background retries (2 retries, wider backoff)."""
        return cls(attempts=3, base_delay=0.5, max_delay=10.0)


def async_boto_config(
    *,
    read_timeout: int = DEFAULT_ASYNC_READ_TIMEOUT,
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    max_attempts: int = 3,
    **overrides: Any,
) -> Config:
    """Build a botocore ``Config`` for async/background clients (capped exp backoff + jitter)."""
    return Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"mode": "standard", "max_attempts": max_attempts},
        **overrides,
    )


# --- Throttle classification -------------------------------------------------

# Error codes that mean "an AWS service is asking us to slow down". Spans the
# spelling variants AWS uses across services (Glue, Lake Formation, STS, Athena,
# S3), because there is no single canonical name: the same condition arrives as
# ThrottlingException from one service and RequestThrottled from another.
#
# Lives here rather than in a caller because a partial copy is worse than no
# copy: a missing code means a retryable condition is treated as fatal, which
# reads as an unexplained failure rather than as a throttle.
THROTTLE_ERROR_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "ThrottledException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "SlowDown",
    }
)


def is_throttling_error(exc: BaseException | None) -> bool:
    """Whether ``exc`` is an AWS throttle.

    Accepts ``BaseException | None`` so callers can pass ``__cause__`` directly.
    Falls back to the HTTP status when the code is unrecognised — 429 is
    unambiguous, and it catches a service whose code spelling is not in the set
    above.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    if (response.get("Error") or {}).get("Code") in THROTTLE_ERROR_CODES:
        return True
    return (response.get("ResponseMetadata") or {}).get("HTTPStatusCode") == 429


def apply_default_client_config(botocore_session: Any) -> None:
    """Ensure a botocore session has a default client config with our timeouts.

    Merges the async (lenient) preset UNDER any config the session already has,
    so callers who set their own default are untouched while sessions with none
    gain socket timeouts + bounded retries. Per-client ``Config`` objects passed
    at client creation still merge on top and win. Idempotent.
    """
    baseline = async_boto_config()
    existing = botocore_session.get_default_client_config()
    merged = baseline.merge(existing) if existing is not None else baseline
    botocore_session.set_default_client_config(merged)
