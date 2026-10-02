# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Transient-error retry for AOSS operations.

OpenSearch Serverless surfaces transient OCU-pressure / node faults as 429
(throttle / circuit_breaking_exception) and 5xx. opensearch-py's transport only
retries a subset and not consistently, so a single blip fails a whole operation
(an accept's embedding write, a delete's teardown drain, a retrieval). We wrap
every op in capped exponential backoff over :data:`OSS_RETRY_STATUS`, and bulk
writes re-submit only the transiently-failed per-item docs.

Env-tunable: ``OSS_MAX_RETRIES`` (default 6), ``OSS_MAX_BACKOFF_S`` (default 30).
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Callable

from opensearchpy import OpenSearch
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import ConnectionTimeout, TransportError
from opensearchpy.helpers import bulk

log = logging.getLogger(__name__)


class PartialIndexError(Exception):
    """A bulk write did not durably index every action.

    Raised by :func:`bulk_with_retry` when, after transient-fault retries are
    exhausted, one or more documents still failed to index — OR when any
    document failed with a NON-transient (terminal) status. Carries the count
    and per-item errors so the caller can fail loud instead of silently
    reporting a write that only partially landed.

    This is the correctness contract #173 relies on: at min-OCU 0 the NEXTGEN
    circuit breaker sheds bulk-write load with 429s; opensearch-py's own
    per-item retry can exhaust and DROP those docs from the success stream
    without surfacing an error, so a naive caller reports "N written" when far
    fewer landed. Surfacing the shortfall as a raise turns silent data loss
    into a visible failure.
    """

    def __init__(self, message: str, *, failed: int, submitted: int, errors: list[dict]):
        """Record the shortfall so callers/tests can assert on it.

        Args:
            message: Human-readable summary.
            failed: Number of documents that never durably indexed.
            submitted: Number of documents handed to this bulk write.
            errors: The per-item error dicts (opensearch-py bulk error shape).
        """
        super().__init__(message)
        self.failed = failed
        self.submitted = submitted
        self.errors = errors


# 429 (throttle / circuit-breaker) + retryable 5xx. 4xx (400/403/404/409) are
# terminal and propagate immediately.
OSS_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
OSS_MAX_RETRIES = int(os.getenv("OSS_MAX_RETRIES", "6"))
OSS_MAX_BACKOFF_S = float(os.getenv("OSS_MAX_BACKOFF_S", "30"))


def is_transient(exc: Exception) -> bool:
    """Whether an opensearch-py error is a transient AOSS fault worth retrying."""
    if isinstance(exc, (OSConnectionError, ConnectionTimeout)):
        return True  # network blip / timeout — retry
    if isinstance(exc, TransportError):
        return getattr(exc, "status_code", None) in OSS_RETRY_STATUS
    return False


def _backoff(attempt: int, max_backoff_s: float = OSS_MAX_BACKOFF_S) -> float:
    """Capped exponential backoff with jitter: min(max_backoff_s, 2**attempt) + [0,1)."""
    return min(max_backoff_s, 2.0**attempt) + random.uniform(0, 1)


def _status(exc: Exception) -> object:
    """Best-effort status/label for the retry log line.

    httpx puts it on exc.response.status_code; opensearch-py on
    exc.status_code; else the type name.
    """
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if code is not None else type(exc).__name__


def retry_call[T](
    op: str,
    fn: Callable[[], T],
    *,
    is_transient: Callable[[Exception], bool],
    max_retries: int,
    max_backoff_s: float,
) -> T:
    """Generic capped-exponential-backoff retry loop.

    Retries `fn` while `is_transient(exc)` and attempts remain; re-raises on
    exhaustion or a non-transient error. `oss_retry` and the httpx catalog
    client both build on this — inject the predicate + limits, share the loop.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — re-raised below unless transient
            if not is_transient(exc) or attempt >= max_retries:
                raise
            backoff = _backoff(attempt, max_backoff_s)
            log.warning(
                "retry %s transient error (%s); attempt %d/%d in %.1fs",
                op,
                _status(exc),
                attempt + 1,
                max_retries,
                backoff,
            )
            time.sleep(backoff)
            attempt += 1


def oss_retry[T](op: str, fn: Callable[[], T]) -> T:
    """Run an AOSS operation with capped exponential backoff on transient faults.

    Retries 429/5xx (TransportError) and connection errors/timeouts up to
    :data:`OSS_MAX_RETRIES`; on exhaustion re-raises the real error. Non-transient
    errors (400/404/409, RequestError, auth) propagate immediately.
    """
    return retry_call(
        op,
        fn,
        is_transient=is_transient,
        max_retries=OSS_MAX_RETRIES,
        max_backoff_s=OSS_MAX_BACKOFF_S,
    )


def bulk_with_retry(client: OpenSearch, actions: list[dict]) -> None:
    """Bulk-index ``actions``, retrying transient per-item failures, then verify.

    Contract: this returns ONLY when every action durably indexed. Any residual
    failure — transient errors that survive :data:`OSS_MAX_RETRIES` re-submits,
    or a NON-transient (terminal 4xx) per-item error — raises
    :class:`PartialIndexError` carrying the shortfall. It never returns cleanly
    with docs still un-indexed.

    Why not rely on ``BulkIndexError`` (the previous design): opensearch-py's
    ``helpers.bulk`` handles per-item 429s with its own internal retry, and when
    those retries exhaust it can DROP the item from the success stream WITHOUT
    raising ``BulkIndexError`` (the HTTP request returned 200; the failure is
    per-document and got swallowed). Under min-OCU-0 circuit-breaker load
    shedding that produced the #173 symptom: "101 written" reported while far
    fewer were searchable, with no error to the caller.

    So we call ``bulk`` with ``raise_on_error=False`` and inspect the returned
    ``(ok_count, errors)`` DIRECTLY — the errors list is authoritative, whether
    or not a ``BulkIndexError`` would have been raised. We re-submit ONLY the
    transiently-failed docs (re-sending the whole batch would duplicate the
    succeeded ones, since AOSS auto-assigns ``_id``), and on exhaustion raise.
    """
    if not actions:
        return
    pending = actions
    attempt = 0
    while True:
        # raise_on_error=False → returns (success_count, errors) instead of
        # raising, so an exhausted per-item 429 that opensearch-py would
        # otherwise silently drop is visible to us as an error entry.
        # max_retries here is opensearch-py's own inner 429 retry; our outer
        # loop adds the capped-exponential re-submit over OSS_RETRY_STATUS.
        _ok, errors = bulk(
            client,
            pending,
            chunk_size=10,
            max_retries=3,
            initial_backoff=2,
            max_backoff=30,
            raise_on_error=False,
            stats_only=False,
        )
        if not errors:
            return

        # errors is a list of per-item result dicts, e.g.
        # [{"index": {"status": 500, "error": {...}, "data": {...}}}].
        retryable_data: list[dict] = []
        terminal: list[dict] = []
        for item in errors:
            (op_result,) = item.values()  # single-key dict keyed by op type
            status = op_result.get("status")
            data = op_result.get("data")
            if status in OSS_RETRY_STATUS and data is not None:
                retryable_data.append(data)
            else:
                terminal.append(item)

        # A terminal (non-transient) per-item failure can never be fixed by
        # retrying — surface it immediately rather than looping.
        if terminal:
            raise PartialIndexError(
                f"OSS bulk: {len(terminal)} doc(s) failed with a terminal (non-retryable) status "
                f"out of {len(pending)} submitted",
                failed=len(terminal),
                submitted=len(pending),
                errors=errors,
            )

        # Only transient failures remain. If we're out of re-submit budget, the
        # docs did NOT land — fail loud instead of returning a partial write.
        if attempt >= OSS_MAX_RETRIES:
            raise PartialIndexError(
                f"OSS bulk: {len(retryable_data)} doc(s) still failing transiently after "
                f"{OSS_MAX_RETRIES} re-submit(s); index is incomplete",
                failed=len(retryable_data),
                submitted=len(pending),
                errors=errors,
            )

        backoff = _backoff(attempt)
        log.warning(
            "OSS bulk: %d of %d docs failed transiently on this attempt; re-submit %d/%d in %.1fs",
            len(retryable_data),
            len(pending),
            attempt + 1,
            OSS_MAX_RETRIES,
            backoff,
        )
        time.sleep(backoff)
        # Re-submit only the failed docs. ``data`` is the source document; it
        # carries neither _index nor _op_type, so rebuild the action envelope
        # from the original actions (all share the same index within a batch).
        index = pending[0]["_index"]
        pending = [{"_op_type": "index", "_index": index, **d} for d in retryable_data]
        attempt += 1
