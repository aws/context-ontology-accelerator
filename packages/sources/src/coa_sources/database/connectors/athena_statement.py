# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Execute a single Athena statement and return all of its rows.

Metadata discovery for a Lambda-backed (custom connector) Athena catalog is done
with Athena SQL — ``SHOW DATABASES``, ``SHOW TABLES``, ``DESCRIBE`` — because the
Athena metadata *API* drops the column comments this feature depends on. That
makes correct, complete, loud statement execution a discovery primitive rather
than a convenience, which is why this is a module of its own rather than a reuse
of :class:`~.athena_sampler.AthenaSampler`, which reads one bounded page and
returns ``[]`` on failure. ``DESCRIBE`` is the only source of columns for this
connector, so a short or empty answer would ship a table with no columns.

Hence the two rules this module holds to: **paginate to exhaustion**, and **raise
rather than return a short answer**. Every ``return`` of rows here is a statement
that Athena reported ``SUCCEEDED`` and that we read to completion.

Result shapes, verified live against a Lambda-backed catalog (they are not
documented, and they drive the two decisions below):

* ``SHOW``/``DESCRIBE`` report ``StatementType == "UTILITY"`` and return **no
  header row** — the first row is already data. ``SELECT`` reports ``"DML"`` and
  *does* repeat the column names in row 0. An unconditional ``rows[1:]`` strip
  (what the sampler does, correctly for its own ``SELECT``) would therefore eat
  the first database, the first table, or the first column here.
* ``DESCRIBE`` advertises three columns (``col_name``/``data_type``/``comment``)
  but returns each row as a **single** cell holding all three fields
  TAB-separated. Splitting that belongs to the caller; this module hands back
  cells verbatim so the shape stays visible to the code that has to tolerate it.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any, Literal

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError
from coa_common import resolve_region
from coa_common.aws_config import RetryPolicy, async_boto_config, is_throttling_error

from coa_sources.database.metrics import emit_metric

logger = structlog.get_logger(__name__)


# Wall-clock budget for one statement, including every throttle retry. A DESCRIBE
# pays the customer connector Lambda's cold start; the budget stays well under the
# discovery Lambda's 15-minute timeout.
DEFAULT_STATEMENT_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 1.0
# Floor on the poll interval. Zero would busy-loop `get_query_execution` as fast
# as the network allows for the whole budget — thousands of calls that would
# themselves cause the throttling this module then retries.
_MIN_POLL_INTERVAL_S = 0.05
_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
# Athena's documented maximum for get_query_results.
_RESULTS_PAGE_SIZE = 1000
# Statement types whose result set carries no header row (verified live).
_HEADERLESS_STATEMENT_TYPES = frozenset({"UTILITY", "DDL"})
# ...and those that repeat the column names in row 0.
_HEADERED_STATEMENT_TYPES = frozenset({"DML"})

# Athena's DML concurrency limit is ACCOUNT-wide (default ~20-25), not per-scan,
# so bounding our own fan-out is not enough: a second scan, or serve traffic
# alongside a scan, can exhaust it and throttle a subset of DESCRIBE statements.
# This is the only retry layer: the client is built with max_attempts=1 so
# botocore's retries do not multiply with these.
_THROTTLE_ATTEMPTS = 5
# Substrings that mark a terminal FAILED state as a throttle rather than a
# genuine statement error. Athena reports quota exhaustion both ways: as a
# TooManyRequestsException on submission, and — once queued — as a FAILED
# execution whose StateChangeReason names the limit. Kept narrow on purpose:
# StateChangeReason carries the customer connector's own message verbatim, so a
# loose marker turns their error text into five pointless re-executions.
_THROTTLING_REASON_MARKERS = (
    "rate exceeded",
    "too many requests",
    "throttl",
    "exceeded the limit of concurrent",
)

# Server-side failures that say nothing about the request, so the same request may
# well succeed next time. Athena declares InternalServerException on every operation
# this module calls; the rest are the generic spellings other AWS services use for
# the same thing, matched so a service-side rename does not silently make this fatal.
_RETRYABLE_SERVER_ERROR_CODES = frozenset(
    {
        "InternalServerException",
        "InternalServerError",
        "InternalFailure",
        "InternalError",
        "ServiceUnavailable",
        "ServiceUnavailableException",
    }
)

HeaderRowMode = Literal["auto", "present", "absent"]


class AthenaStatementError(RuntimeError):
    """Base for every way a discovery statement can fail to produce rows.

    Callers catch this to account for a failed table without having to
    distinguish submission failures from execution failures — but the subclasses
    stay distinct because "the connector rejected this table" and "we ran out of
    time" call for different operator action.
    """


class AthenaStatementFailed(AthenaStatementError):
    """The statement reached a terminal non-``SUCCEEDED`` state.

    ``state_change_reason`` is preserved verbatim: for a custom connector it is
    the only channel through which the customer's own Lambda reports why it
    refused, so paraphrasing it would destroy the diagnostic.
    """

    def __init__(self, statement: str, state: str, state_change_reason: str) -> None:
        """Record the terminal state and Athena's own explanation of it."""
        self.statement = statement
        self.state = state
        self.state_change_reason = state_change_reason
        detail = f": {state_change_reason}" if state_change_reason else ""
        super().__init__(f"Athena statement {state}{detail} — {statement}")


class AthenaStatementTimeout(AthenaStatementError):
    """The statement did not complete within its total budget."""

    def __init__(self, statement: str, timeout_seconds: float) -> None:
        """Record the budget that was exhausted, for the operator-facing message."""
        self.statement = statement
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Athena statement did not complete within {timeout_seconds:g}s — {statement}")


class AthenaStatementClientError(AthenaStatementError):
    """An AWS-side call (submit, poll, or fetch) failed, or answered unusably."""

    def __init__(self, statement: str, cause: Exception) -> None:
        """Wrap ``cause``, keeping it reachable on both ``cause`` and ``__cause__``."""
        self.statement = statement
        # Stored explicitly rather than relying on ``__cause__``: the throttle
        # retry classifies on this, and depending on every raise site remembering
        # ``from exc`` would make a missed one silently disable the retry.
        self.cause = cause
        super().__init__(f"Athena call failed for statement — {statement}: {cause}")


class AthenaStatementUnexpectedShape(AthenaStatementError):
    """Athena answered ``SUCCEEDED`` but the response could not be read safely.

    Separate from :class:`AthenaStatementClientError` because nothing failed —
    the shape was simply not one this module knows how to interpret without
    risking a silently wrong row set, which is the one outcome it exists to
    prevent.
    """


def _is_throttling_reason(reason: str) -> bool:
    """Whether a terminal FAILED reason describes a quota/rate limit."""
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in _THROTTLING_REASON_MARKERS)


def _is_retryable_call_error(exc: BaseException | None) -> bool:
    """Whether an AWS call failure is worth another attempt.

    Retries throttles, transient 5xx and transport errors (``BotoCoreError`` has no
    ``response``), which botocore would have retried had the client not been built
    with ``max_attempts=1``. 4xx other than 429 stays fatal.
    """
    if is_throttling_error(exc):
        return True
    # No `response` → a transport-level failure (read timeout, connect timeout,
    # endpoint resolution, closed connection). Retryable by nature: nothing about
    # the request itself was rejected.
    if isinstance(exc, BotoCoreError):
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    if (response.get("Error") or {}).get("Code") in _RETRYABLE_SERVER_ERROR_CODES:
        return True
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return isinstance(status, int) and status >= 500


class AthenaStatementRunner:
    """Runs one Athena statement to completion and returns every row.

    One instance per discovery pass, shared across the connector's ``DESCRIBE``
    fan-out. The boto3 client is built in ``__init__`` rather than lazily on
    first use: ``boto3.client()`` is not safe to call concurrently (AWS documents
    response-ordering and SSL-module failures), so building it once up front and
    dispatching the ready instance into the pool is the supported pattern.
    Nothing else on the instance is mutated after construction.
    """

    def __init__(
        self,
        *,
        region: str | None = None,
        workgroup: str | None = None,
        output_location: str | None = None,
        timeout_seconds: float = DEFAULT_STATEMENT_TIMEOUT_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
        client: Any | None = None,
    ) -> None:
        """Configure the Athena target and build the client.

        Args:
            region: Region for the Athena client; defaults to ``resolve_region()``.
            workgroup: Workgroup to run in. Falls back to ``ATHENA_WORKGROUP``,
                and when that is unset the statement runs in the account's
                ``primary`` workgroup — see :meth:`_execution_kwargs`.
            output_location: S3 URI for results. Falls back to a path under
                ``ATHENA_SPILL_BUCKET``. Required unless the workgroup enforces
                its own result configuration.
            timeout_seconds: Default TOTAL wall-clock budget per statement.
            poll_interval_s: Delay between ``get_query_execution`` polls; floored
                at ``_MIN_POLL_INTERVAL_S``.
            client: Pre-built Athena client, for tests.
        """
        self._region = region or resolve_region()
        self._workgroup = workgroup if workgroup is not None else os.getenv("ATHENA_WORKGROUP", "")
        if output_location is not None:
            self._output_location = output_location
        else:
            spill_bucket = os.getenv("ATHENA_SPILL_BUCKET")
            self._output_location = f"s3://{spill_bucket}/discovery-statements/" if spill_bucket else ""
        self._timeout_seconds = timeout_seconds
        self._poll_interval_s = max(_MIN_POLL_INTERVAL_S, poll_interval_s)
        # max_attempts=1: the statement-level throttle retry below is the only
        # retry layer, so `_THROTTLE_ATTEMPTS` is the whole story.
        self._client = client or boto3.client(
            "athena", region_name=self._region, config=async_boto_config(max_attempts=1)
        )

    def _execution_kwargs(self, statement: str, database: str | None, catalog: str | None) -> dict:
        """Build ``start_query_execution`` kwargs.

        ``WorkGroup`` is omitted when unset rather than defaulted to a name,
        which lands the statement in the account's ``primary`` workgroup. That is
        deliberate for discovery: the Lambda serves every namespace in the
        deployment, so there is no single per-namespace workgroup it could pin,
        and ``OutputLocation`` is supplied explicitly so ``primary`` needs no
        result configuration of its own. The discovery role's Athena permissions
        are scoped to ``workgroup/*``, so this is permitted by design.
        """
        kwargs: dict = {"QueryString": statement}
        context: dict[str, str] = {}
        if catalog:
            context["Catalog"] = catalog
        if database:
            context["Database"] = database
        if context:
            kwargs["QueryExecutionContext"] = context
        if self._workgroup:
            kwargs["WorkGroup"] = self._workgroup
        if self._output_location:
            kwargs["ResultConfiguration"] = {"OutputLocation": self._output_location}
        return kwargs

    def run(
        self,
        statement: str,
        *,
        database: str | None = None,
        catalog: str | None = None,
        header_row: HeaderRowMode = "auto",
        timeout_seconds: float | None = None,
    ) -> list[list[str | None]]:
        """Execute ``statement`` and return every result row.

        Args:
            statement: The SQL to run. Athena's two parsers disagree on quoting
                (``SHOW``/``DESCRIBE`` reject double quotes, ``SELECT`` rejects
                backticks), so callers pass identifiers **unquoted** — the only
                form valid in both.
            database: ``QueryExecutionContext.Database``, when the statement
                relies on a default namespace.
            catalog: ``QueryExecutionContext.Catalog``.
            header_row: Whether row 0 repeats the column names. ``"auto"``
                decides from the reported ``StatementType``; pass an explicit
                mode when the caller already knows the shape.
            timeout_seconds: Overrides the instance default. This is the TOTAL
                budget: throttle retries share it rather than each getting a
                fresh one.

        Returns:
            One list of cell values per row, header excluded, in Athena's order.
            A cell absent from the response is ``None`` (distinct from ``""``).

        Raises:
            AthenaStatementFailed: terminal state other than ``SUCCEEDED``.
            AthenaStatementTimeout: the total budget ran out.
            AthenaStatementClientError: an AWS call failed or answered unusably.
            AthenaStatementUnexpectedShape: the result set could not be read
                without risking a wrong row set.
        """
        budget = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        overall_deadline = time.monotonic() + budget
        policy = RetryPolicy.for_async()

        # Phase state carried ACROSS attempts, so a throttle resumes rather than
        # restarts. A throttle can be raised from any of the three phases, and
        # re-submitting on a poll- or fetch-phase throttle would:
        #   * orphan the in-flight execution, which keeps holding a slot in the
        #     account-wide DML quota — the very contention being backed off from;
        #   * re-invoke the customer's connector Lambda, billed to them, once per
        #     attempt; and
        #   * for a fetch throttle, re-run a statement that already reported
        #     SUCCEEDED with its rows one cheap call away.
        # Only a submit-phase throttle leaves query_id unset and so re-submits.
        query_id: str | None = None
        statement_type: str | None = None

        for attempt in range(1, _THROTTLE_ATTEMPTS + 1):
            try:
                if query_id is None:
                    query_id = self._submit(
                        statement,
                        database=database,
                        catalog=catalog,
                        deadline=overall_deadline,
                        timeout_seconds=budget,
                    )
                if statement_type is None:
                    statement_type = self._await_completion(self._client, query_id, statement, overall_deadline, budget)
                return self._fetch_rows(
                    self._client, query_id, statement, statement_type, header_row, overall_deadline, budget
                )
            except AthenaStatementClientError as exc:
                if not _is_retryable_call_error(exc.cause):
                    raise
                last_error: AthenaStatementError = exc
            except AthenaStatementFailed as exc:
                if not _is_throttling_reason(exc.state_change_reason):
                    raise
                # A throttling FAILED state is terminal for that execution, so the
                # next attempt must submit a new one rather than resume this id.
                query_id, statement_type = None, None
                last_error = exc

            if attempt == _THROTTLE_ATTEMPTS:
                emit_metric("AthenaStatementThrottleExhausted", 1, "Count")
                self._release(query_id)
                raise last_error
            # Capped exponential backoff with full jitter, per the repo's async
            # retry policy. Full jitter matters here specifically: the fan-out
            # throttles many statements at once, and a fixed delay would have
            # them all retry in the same instant and throttle again.
            delay = min(policy.base_delay * 2 ** (attempt - 1), policy.max_delay)
            sleep_for = random.uniform(0, delay)  # noqa: S311 - jitter, not crypto
            # Do not sleep past the budget only to fail on the next attempt.
            if time.monotonic() + sleep_for >= overall_deadline:
                emit_metric("AthenaStatementTimeout", 1, "Count")
                self._release(query_id)
                raise AthenaStatementTimeout(statement, budget) from last_error
            logger.warning(
                "athena_statement_throttled_retrying",
                statement=statement,
                attempt=attempt,
                max_attempts=_THROTTLE_ATTEMPTS,
                sleep_seconds=round(sleep_for, 3),
            )
            time.sleep(sleep_for)

        # Unreachable: the loop either returns, raises, or raises at the final
        # attempt. Present so the function has no implicit None path.
        raise AthenaStatementTimeout(statement, budget)

    def _submit(
        self,
        statement: str,
        *,
        database: str | None,
        catalog: str | None,
        deadline: float,
        timeout_seconds: float,
    ) -> str:
        """Start one execution and return its id. The submit phase, on its own.

        ``timeout_seconds`` is reported on the timeout rather than read from the
        instance, because ``run()`` accepts a per-call budget that overrides it.
        """
        if time.monotonic() >= deadline:
            emit_metric("AthenaStatementTimeout", 1, "Count")
            raise AthenaStatementTimeout(statement, timeout_seconds)

        try:
            response = self._client.start_query_execution(**self._execution_kwargs(statement, database, catalog))
        except (ClientError, BotoCoreError) as exc:
            raise AthenaStatementClientError(statement, exc) from exc
        query_id = (response or {}).get("QueryExecutionId")
        if not query_id:
            raise AthenaStatementClientError(
                statement, ValueError("start_query_execution returned no QueryExecutionId")
            )
        return query_id

    def _release(self, query_id: str | None) -> None:
        """Cancel an execution this runner is about to stop waiting on.

        Called on every giving-up path, so an abandoned execution does not keep a
        slot in the account-wide DML quota. Harmless when it already reached a
        terminal state — Athena treats stopping a finished execution as a no-op —
        and best-effort, since the caller is already failing and a failure to cancel
        must not replace the error being reported.
        """
        if query_id:
            self._stop_quietly(self._client, query_id)

    def _await_completion(
        self,
        athena,
        query_id: str,
        statement: str,
        deadline: float,
        timeout_seconds: float,
    ) -> str:
        """Poll until terminal, returning the reported ``StatementType``.

        Raises on a non-``SUCCEEDED`` terminal state or on the deadline, so a
        caller can never mistake a failure for an empty result.
        """
        while True:
            try:
                execution = (athena.get_query_execution(QueryExecutionId=query_id) or {}).get("QueryExecution")
            except (ClientError, BotoCoreError) as exc:
                raise AthenaStatementClientError(statement, exc) from exc
            if execution is None:
                raise AthenaStatementClientError(
                    statement, ValueError("get_query_execution returned no QueryExecution")
                )

            status = execution.get("Status") or {}
            state = status.get("State", "")
            if state in _TERMINAL_STATES:
                if state != "SUCCEEDED":
                    reason = status.get("StateChangeReason", "") or ""
                    if not _is_throttling_reason(reason):
                        # Throttles are retried, so only a real failure counts.
                        emit_metric("AthenaStatementFailed", 1, "Count", State=state)
                    raise AthenaStatementFailed(statement, state, reason)
                return execution.get("StatementType", "") or ""

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop_quietly(athena, query_id)
                emit_metric("AthenaStatementTimeout", 1, "Count")
                raise AthenaStatementTimeout(statement, timeout_seconds)
            time.sleep(min(self._poll_interval_s, remaining))

    @staticmethod
    def _stop_quietly(athena, query_id: str) -> None:
        """Best-effort cancel; a failure here must not mask the timeout."""
        try:
            athena.stop_query_execution(QueryExecutionId=query_id)
        except Exception:  # noqa: BLE001 - the timeout is the error worth raising
            logger.warning("athena_stop_query_failed", query_execution_id=query_id, exc_info=True)

    def _fetch_rows(
        self,
        athena,
        query_id: str,
        statement: str,
        statement_type: str,
        header_row: HeaderRowMode,
        deadline: float,
        timeout_seconds: float,
    ) -> list[list[str | None]]:
        """Page through the full result set, dropping the header row if present.

        Pagination runs to exhaustion. The deadline is enforced from the *second* page
        onward: by the time the statement has SUCCEEDED the expensive work (the
        connector's cold start and its own source scan) is already paid and the
        rows are one cheap call away, so discarding them over a deadline the last
        poll happened to cross would waste the work and report a timeout for a
        statement that did complete. Later pages are still bounded, so a
        pathologically wide table cannot outlive the Lambda.
        """
        drop_header = self._should_drop_header(statement, statement_type, header_row)
        rows: list[list[str | None]] = []
        next_token: str | None = None
        # Tracked separately from "first page" so an EMPTY first page does not
        # consume the strip and leak page 2's header row in as data.
        header_pending = drop_header
        saw_result_set = False

        while True:
            kwargs: dict = {"QueryExecutionId": query_id, "MaxResults": _RESULTS_PAGE_SIZE}
            if next_token:
                kwargs["NextToken"] = next_token
            try:
                response = athena.get_query_results(**kwargs) or {}
            except (ClientError, BotoCoreError) as exc:
                raise AthenaStatementClientError(statement, exc) from exc

            if "ResultSet" in response:
                saw_result_set = True
            page = (response.get("ResultSet") or {}).get("Rows") or []
            if header_pending and page:
                page = page[1:]
                header_pending = False
            rows.extend([cell.get("VarCharValue") for cell in (row.get("Data") or [])] for row in page)

            next_token = response.get("NextToken")
            if not next_token:
                break
            if time.monotonic() >= deadline:
                emit_metric("AthenaStatementTimeout", 1, "Count")
                raise AthenaStatementTimeout(statement, timeout_seconds)

        if not saw_result_set:
            # A SUCCEEDED statement always carries a ResultSet. Without one there
            # is no way to tell "no rows" from "not a result set", and returning
            # [] here is precisely the ambiguity this module exists to remove.
            raise AthenaStatementUnexpectedShape(
                f"get_query_results returned no ResultSet for a SUCCEEDED statement — {statement}"
            )
        return rows

    @staticmethod
    def _should_drop_header(statement: str, statement_type: str, header_row: HeaderRowMode) -> bool:
        """Resolve the header-row question for this result set.

        ``auto`` keys on ``StatementType`` (shapes in the module docstring). An absent
        or unrecognised type raises rather than guessing, because a wrong guess eats
        the first database, table or column.
        """
        if header_row == "present":
            return True
        if header_row == "absent":
            return False
        normalized = statement_type.upper()
        if normalized in _HEADERLESS_STATEMENT_TYPES:
            return False
        if normalized in _HEADERED_STATEMENT_TYPES:
            return True
        raise AthenaStatementUnexpectedShape(
            f"Athena reported StatementType {statement_type!r}, so whether row 0 is a header "
            f"is unknown; pass header_row explicitly — {statement}"
        )
