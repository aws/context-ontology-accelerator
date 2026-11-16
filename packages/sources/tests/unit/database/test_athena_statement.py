# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AthenaStatementRunner — the discovery statement primitive.

The behaviours that separate this from ``AthenaSampler._run`` are the ones tested
hardest, because each replaces a silent-wrong-answer path: pagination to
exhaustion (vs. truncation at one page), raising (vs. returning ``[]`` on every
failure), and header handling driven by ``StatementType`` (vs. an unconditional
``rows[1:]`` that would eat the first row of a ``SHOW``).

The fake Athena client models the response shapes observed live against a
Lambda-backed catalog, including ``DESCRIBE``'s three advertised columns against
one tab-packed cell.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from coa_sources.database.connectors.athena_statement import (
    AthenaStatementClientError,
    AthenaStatementError,
    AthenaStatementFailed,
    AthenaStatementRunner,
    AthenaStatementTimeout,
    AthenaStatementUnexpectedShape,
)

pytestmark = pytest.mark.unit

_MODULE = "coa_sources.database.connectors.athena_statement"


def _row(*cells: str | None) -> dict:
    """An Athena result row; a ``None`` cell is an absent VarCharValue."""
    return {"Data": [({} if c is None else {"VarCharValue": c}) for c in cells]}


class FakeAthena:
    """Minimal Athena client double.

    ``pages`` is a list of ``(rows, next_token)`` returned in order by
    ``get_query_results``; ``states`` is the sequence of states reported by
    successive ``get_query_execution`` polls. Both clamp to their last entry so a
    test need not enumerate every poll.
    """

    def __init__(
        self,
        *,
        pages: list[tuple[list[dict], str | None]] | None = None,
        states: list[str] | None = None,
        statement_type: str = "UTILITY",
        state_change_reason: str = "",
        column_names: tuple[str, ...] = ("col_name", "data_type", "comment"),
        start_error: Exception | None = None,
        start_response: dict | None = None,
        execution_response: dict | None = None,
        results_error: Exception | None = None,
        results_response: dict | None = None,
    ) -> None:
        self.pages = pages if pages is not None else [([], None)]
        self.states = states or ["SUCCEEDED"]
        self.statement_type = statement_type
        self.state_change_reason = state_change_reason
        self.column_names = column_names
        self.start_error = start_error
        self.start_response = start_response
        self.execution_response = execution_response
        self.results_error = results_error
        self.results_response = results_response
        self.start_calls: list[dict] = []
        self.execution_calls = 0
        self.results_calls: list[dict] = []
        self.stop_calls: list[str] = []
        self._poll = 0
        self._page = 0

    def start_query_execution(self, **kwargs):
        self.start_calls.append(kwargs)
        if self.start_error is not None:
            raise self.start_error
        if self.start_response is not None:
            return self.start_response
        return {"QueryExecutionId": f"qid-{len(self.start_calls)}"}

    def get_query_execution(self, QueryExecutionId):  # noqa: N803 - boto3 casing
        self.execution_calls += 1
        if self.execution_response is not None:
            return self.execution_response
        state = self.states[min(self._poll, len(self.states) - 1)]
        self._poll += 1
        return {
            "QueryExecution": {
                "StatementType": self.statement_type,
                "Status": {"State": state, "StateChangeReason": self.state_change_reason},
            }
        }

    def get_query_results(self, **kwargs):
        self.results_calls.append(kwargs)
        if self.results_error is not None:
            raise self.results_error
        if self.results_response is not None:
            return self.results_response
        rows, token = self.pages[min(self._page, len(self.pages) - 1)]
        self._page += 1
        out: dict = {
            "ResultSet": {
                "Rows": rows,
                "ResultSetMetadata": {"ColumnInfo": [{"Name": n} for n in self.column_names]},
            }
        }
        if token:
            out["NextToken"] = token
        return out

    def stop_query_execution(self, QueryExecutionId):  # noqa: N803 - boto3 casing
        self.stop_calls.append(QueryExecutionId)
        return {}


def _runner(client, **kwargs) -> AthenaStatementRunner:
    defaults = {"output_location": "s3://results/discovery/", "poll_interval_s": 0.0}
    return AthenaStatementRunner(client=client, **{**defaults, **kwargs})


def _throttle_error(code: str = "TooManyRequestsException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "slow down"}}, "StartQueryExecution")


def _clock(*readings: float):
    """A ``time.monotonic`` stub returning ``readings`` in order, then repeating the last.

    Needed because the runner refuses to submit a statement it has no time left
    to await, so a zero budget fails before reaching the phase under test.
    Driving the clock explicitly pins WHICH phase notices the deadline.
    """
    remaining = list(readings)

    def monotonic() -> float:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return monotonic


class TestRowsAndHeader:
    # SHOW/DESCRIBE report UTILITY and return NO header row. The sampler's
    # unconditional rows[1:] would drop the first database here.
    def test_utility_statement_keeps_every_row(self):
        client = FakeAthena(pages=[([_row("sales"), _row("hr")], None)], statement_type="UTILITY")
        assert _runner(client).run("SHOW DATABASES IN cat") == [["sales"], ["hr"]]

    # A SELECT is DML and does repeat its column names in row 0.
    def test_dml_statement_drops_the_header_row(self):
        client = FakeAthena(pages=[([_row("a", "b"), _row("1", "x")], None)], statement_type="DML")
        assert _runner(client).run("SELECT 1 AS a, 'x' AS b") == [["1", "x"]]

    def test_ddl_statement_keeps_every_row(self):
        client = FakeAthena(pages=[([_row("only")], None)], statement_type="DDL")
        assert _runner(client).run("SHOW TABLES IN cat.db") == [["only"]]

    # Guessing "has a header" on an unknown type would eat the first database,
    # table, or column — and losing the first database loses everything beneath
    # it, silently. A loud failure is the better trade for this module.
    @pytest.mark.parametrize("statement_type", ["", "SOMETHING_NEW", "utility_v2"])
    def test_unknown_statement_type_raises_rather_than_guessing(self, statement_type):
        client = FakeAthena(pages=[([_row("h"), _row("v")], None)], statement_type=statement_type)
        with pytest.raises(AthenaStatementUnexpectedShape):
            _runner(client).run("SOMETHING")

    # ...and an explicit mode is the escape hatch, so a caller that knows the
    # shape is never blocked by the type being unrecognised.
    def test_explicit_mode_overrides_an_unknown_type(self):
        client = FakeAthena(pages=[([_row("a"), _row("1")], None)], statement_type="")
        assert _runner(client).run("SHOW X", header_row="absent") == [["a"], ["1"]]

    def test_explicit_absent_overrides_a_dml_type(self):
        client = FakeAthena(pages=[([_row("a"), _row("1")], None)], statement_type="DML")
        assert _runner(client).run("SELECT 1", header_row="absent") == [["a"], ["1"]]

    def test_explicit_present_overrides_a_utility_type(self):
        client = FakeAthena(pages=[([_row("a"), _row("1")], None)], statement_type="UTILITY")
        assert _runner(client).run("SHOW X", header_row="present") == [["1"]]

    def test_empty_result_set_is_an_empty_list_not_an_error(self):
        client = FakeAthena(pages=[([], None)], statement_type="UTILITY")
        assert _runner(client).run("SHOW TABLES IN cat.db") == []

    # A header strip on an empty first page must not raise or mis-slice.
    def test_dml_with_no_rows_at_all(self):
        client = FakeAthena(pages=[([], None)], statement_type="DML")
        assert _runner(client).run("SELECT 1") == []

    # DESCRIBE advertises 3 columns but packs all three fields into ONE
    # tab-separated cell. The runner hands the cell back verbatim; splitting is
    # the connector's job, so this pins the contract between them.
    def test_describe_cell_is_returned_verbatim_including_tabs(self):
        packed = "customer_id\tbigint\tSurrogate key @pk"
        client = FakeAthena(pages=[([_row(packed)], None)], statement_type="UTILITY")
        assert _runner(client).run("DESCRIBE cat.db.t") == [[packed]]

    # An absent VarCharValue is None, not "" — a NULL comment and an empty
    # comment are different signals to the tag parser.
    def test_absent_cell_value_is_none(self):
        client = FakeAthena(pages=[([_row("a", None)], None)], statement_type="UTILITY")
        assert _runner(client).run("DESCRIBE cat.db.t") == [["a", None]]


class TestPagination:
    # The truncation this module exists to fix: a table wider than one page.
    def test_follows_next_token_to_exhaustion(self):
        client = FakeAthena(
            pages=[
                ([_row("c1"), _row("c2")], "tok-1"),
                ([_row("c3")], "tok-2"),
                ([_row("c4")], None),
            ],
            statement_type="UTILITY",
        )
        assert _runner(client).run("DESCRIBE cat.db.wide") == [["c1"], ["c2"], ["c3"], ["c4"]]
        assert [c.get("NextToken") for c in client.results_calls] == [None, "tok-1", "tok-2"]

    # The header lives on the first page only. Stripping per page would eat one
    # real row from every subsequent page — silently, and only on the wide
    # tables that paginate, which is the worst possible place for it.
    def test_header_is_dropped_only_once(self):
        client = FakeAthena(
            pages=[([_row("hdr"), _row("v1")], "tok-1"), ([_row("v2"), _row("v3")], None)],
            statement_type="DML",
        )
        assert _runner(client).run("SELECT x") == [["v1"], ["v2"], ["v3"]]

    # An EMPTY first page must not consume the strip, or page 2's header leaks in
    # as a data row.
    def test_empty_first_page_does_not_consume_the_header_strip(self):
        client = FakeAthena(
            pages=[([], "tok-1"), ([_row("hdr"), _row("v1")], None)],
            statement_type="DML",
        )
        assert _runner(client).run("SELECT x") == [["v1"]]

    def test_requests_the_maximum_page_size(self):
        client = FakeAthena(pages=[([_row("a")], None)])
        _runner(client).run("SHOW DATABASES IN cat")
        assert client.results_calls[0]["MaxResults"] == 1000


class TestFailuresAreLoud:
    # The sampler returns [] here. For DESCRIBE that is indistinguishable from a
    # table with no columns, which would ship a silently gutted ontology.
    def test_failed_state_raises_with_the_reason_preserved(self):
        client = FakeAthena(
            states=["FAILED"],
            state_change_reason="AccessDeniedException on lambda:InvokeFunction",
        )
        with pytest.raises(AthenaStatementFailed) as exc:
            _runner(client).run("DESCRIBE cat.db.t")
        assert exc.value.state == "FAILED"
        # Verbatim: for a custom connector this is the customer's own Lambda
        # explaining itself, and it is the only channel that carries it.
        assert exc.value.state_change_reason == "AccessDeniedException on lambda:InvokeFunction"
        assert "DESCRIBE cat.db.t" in str(exc.value)

    def test_cancelled_state_raises(self):
        client = FakeAthena(states=["CANCELLED"])
        with pytest.raises(AthenaStatementFailed):
            _runner(client).run("DESCRIBE cat.db.t")

    # A SUCCEEDED statement always carries a ResultSet. Returning [] without one
    # is precisely the "no columns vs. no answer" ambiguity this module removes.
    @pytest.mark.parametrize("response", [{}, {"NotAResultSet": {}}])
    def test_missing_result_set_raises_rather_than_returning_empty(self, response):
        client = FakeAthena(results_response=response)
        with pytest.raises(AthenaStatementUnexpectedShape):
            _runner(client).run("DESCRIBE cat.db.t")

    def test_timeout_raises_and_cancels_the_statement(self):
        client = FakeAthena(states=["RUNNING"])
        # deadline=10; submit at t=0; the first poll finds t=20.
        with (
            patch(f"{_MODULE}.time.monotonic", side_effect=_clock(0.0, 0.0, 20.0)),
            pytest.raises(AthenaStatementTimeout),
        ):
            _runner(client, timeout_seconds=10.0).run("DESCRIBE cat.db.t")
        # Leaving it running would keep holding a slot in the account-wide DML
        # quota that the rest of the fan-out is contending for.
        assert client.stop_calls == ["qid-1"]

    def test_a_failed_cancel_does_not_mask_the_timeout(self):
        client = FakeAthena(states=["RUNNING"])
        with (
            patch(f"{_MODULE}.time.monotonic", side_effect=_clock(0.0, 0.0, 20.0)),
            patch.object(client, "stop_query_execution", side_effect=RuntimeError("boom")),
            pytest.raises(AthenaStatementTimeout),
        ):
            _runner(client, timeout_seconds=10.0).run("DESCRIBE cat.db.t")

    def test_submit_error_wraps_the_cause(self):
        cause = ClientError({"Error": {"Code": "InvalidRequestException"}}, "StartQueryExecution")
        client = FakeAthena(start_error=cause)
        with pytest.raises(AthenaStatementClientError) as exc:
            _runner(client).run("DESCRIBE cat.db.t")
        assert exc.value.__cause__ is cause
        # Stored explicitly: the throttle retry classifies on `.cause`, so it must
        # not depend on every raise site remembering `from exc`.
        assert exc.value.cause is cause

    def test_results_error_wraps_the_cause(self):
        cause = EndpointConnectionError(endpoint_url="https://athena")
        client = FakeAthena(results_error=cause)
        # sleep patched because a transport error is retryable: without it this
        # test pays five real backoff sleeps to reach the same assertion.
        with patch(f"{_MODULE}.time.sleep"), pytest.raises(AthenaStatementClientError):
            _runner(client).run("DESCRIBE cat.db.t")

    # Poll-phase failures must be wrapped too — a raw ClientError escaping here
    # aborts the whole discovery pass instead of accounting one table.
    def test_poll_error_is_wrapped(self):
        cause = ClientError({"Error": {"Code": "InvalidRequestException"}}, "GetQueryExecution")
        client = FakeAthena()
        with (
            patch.object(client, "get_query_execution", side_effect=cause),
            pytest.raises(AthenaStatementClientError),
        ):
            _runner(client).run("DESCRIBE cat.db.t")

    # A malformed-but-successful AWS response must not escape as KeyError: the
    # documented contract is that every failure is an AthenaStatementError.
    def test_missing_query_execution_id_raises_a_statement_error(self):
        client = FakeAthena(start_response={})
        with pytest.raises(AthenaStatementClientError):
            _runner(client).run("DESCRIBE cat.db.t")

    def test_missing_query_execution_body_raises_a_statement_error(self):
        client = FakeAthena(execution_response={})
        with pytest.raises(AthenaStatementClientError):
            _runner(client).run("DESCRIBE cat.db.t")

    # The caller contract is a single except clause, so the hierarchy matters.
    @pytest.mark.parametrize(
        "exc_type",
        [
            AthenaStatementFailed,
            AthenaStatementTimeout,
            AthenaStatementClientError,
            AthenaStatementUnexpectedShape,
        ],
    )
    def test_every_error_is_an_athena_statement_error(self, exc_type):
        assert issubclass(exc_type, AthenaStatementError)

    # A non-throttling failure must not be retried: re-running a statement the
    # connector rejected just multiplies the cost of the same error.
    def test_non_throttling_failure_is_not_retried(self):
        client = FakeAthena(states=["FAILED"], state_change_reason="SYNTAX_ERROR: no such table")
        with pytest.raises(AthenaStatementFailed):
            _runner(client).run("DESCRIBE cat.db.t")
        assert len(client.start_calls) == 1


class TestSucceededResultsAreNotDiscarded:
    # The expensive work — the connector's cold start and its own source scan —
    # is already paid once the statement SUCCEEDs, and the rows are one cheap call
    # away. Failing on a deadline the last poll happened to cross would waste all
    # of it and report a timeout for a statement that did complete.
    # Later pages are still bounded, so a pathologically wide table cannot
    # outlive the Lambda. Both halves in one test: the first page IS fetched with
    # the deadline already blown, and the second page is NOT.
    def test_first_page_is_fetched_past_the_deadline_but_later_pages_are_not(self):
        client = FakeAthena(pages=[([_row("c1")], "tok-1"), ([_row("c2")], None)])
        # deadline=10; submit at t=0; SUCCEEDED on the first poll (no clock read);
        # the post-page-1 check finds t=20.
        with (
            patch(f"{_MODULE}.time.monotonic", side_effect=_clock(0.0, 0.0, 20.0)),
            pytest.raises(AthenaStatementTimeout),
        ):
            _runner(client, timeout_seconds=10.0).run("DESCRIBE cat.db.wide")
        assert len(client.results_calls) == 1

    # ...and a single-page result set never consults the clock after SUCCEEDED at
    # all, so it cannot be discarded.
    def test_a_single_page_result_is_never_discarded(self):
        client = FakeAthena(pages=[([_row("sales")], None)], states=["SUCCEEDED"])
        with patch(f"{_MODULE}.time.monotonic", side_effect=_clock(0.0, 0.0, 20.0)):
            assert _runner(client, timeout_seconds=10.0).run("SHOW DATABASES IN cat") == [["sales"]]


class TestThrottleRetry:
    # Athena's DML concurrency limit is account-wide, so our own bounded fan-out
    # can still be throttled by a concurrent scan or by serve traffic.
    def test_retries_a_throttled_submission_then_succeeds(self):
        client = FakeAthena(pages=[([_row("sales")], None)])
        calls = {"n": 0}
        real_start = client.start_query_execution

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _throttle_error()
            return real_start(**kwargs)

        with (
            patch.object(client, "start_query_execution", side_effect=flaky),
            patch(f"{_MODULE}.time.sleep") as sleep,
        ):
            assert _runner(client).run("SHOW DATABASES IN cat") == [["sales"]]
        assert calls["n"] == 3
        assert sleep.call_count == 2

    # Quota exhaustion also arrives as a terminal FAILED whose reason names the
    # limit, not only as an exception on submission.
    def test_retries_a_failed_state_whose_reason_is_a_throttle(self):
        client = FakeAthena(states=["FAILED"], state_change_reason="Rate exceeded (Service: Athena)")
        with patch(f"{_MODULE}.time.sleep"), pytest.raises(AthenaStatementFailed):
            _runner(client).run("DESCRIBE cat.db.t")
        assert len(client.start_calls) == 5

    def test_gives_up_after_the_attempt_budget_and_raises_the_last_error(self):
        client = FakeAthena(start_error=_throttle_error("ThrottlingException"))
        with patch(f"{_MODULE}.time.sleep"), pytest.raises(AthenaStatementClientError):
            _runner(client).run("SHOW DATABASES IN cat")
        assert len(client.start_calls) == 5

    # A throttle can be raised from any of the three phases, and only the submit
    # phase may legitimately re-submit. Re-submitting on a poll or fetch throttle
    # orphans the in-flight execution — which keeps holding a slot in the
    # account-wide DML quota, the exact contention being backed off from — and
    # re-invokes the customer's connector Lambda, billed to them, once per attempt.
    def test_a_poll_throttle_resumes_the_same_execution(self):
        client = FakeAthena(states=["RUNNING", "SUCCEEDED"], pages=[([_row("sales")], None)])
        polls = {"n": 0}
        real_poll = client.get_query_execution

        def flaky(**kwargs):
            polls["n"] += 1
            if polls["n"] == 1:
                raise _throttle_error()
            return real_poll(**kwargs)

        with patch.object(client, "get_query_execution", side_effect=flaky), patch(f"{_MODULE}.time.sleep"):
            assert _runner(client).run("SHOW DATABASES IN cat") == [["sales"]]
        # One submission only — the retry resumed polling instead of restarting.
        assert len(client.start_calls) == 1

    def test_a_fetch_throttle_does_not_re_run_the_statement(self):
        client = FakeAthena(pages=[([_row("sales")], None)])
        fetches = {"n": 0}
        real_fetch = client.get_query_results

        def flaky(**kwargs):
            fetches["n"] += 1
            if fetches["n"] == 1:
                raise _throttle_error()
            return real_fetch(**kwargs)

        with patch.object(client, "get_query_results", side_effect=flaky), patch(f"{_MODULE}.time.sleep"):
            assert _runner(client).run("SHOW DATABASES IN cat") == [["sales"]]
        # The rows were one cheap call away; re-running would bill the customer's
        # Lambda again for a statement that already reported SUCCEEDED.
        assert len(client.start_calls) == 1

    # Giving up must not leave the execution running: an abandoned one holds its
    # slot in the account-wide DML quota until Athena times it out.
    def test_exhausting_the_budget_cancels_the_in_flight_execution(self):
        client = FakeAthena(states=["RUNNING"], pages=[([_row("sales")], None)])
        with (
            patch.object(client, "get_query_execution", side_effect=_throttle_error()),
            patch(f"{_MODULE}.time.sleep"),
            pytest.raises(AthenaStatementClientError),
        ):
            _runner(client).run("SHOW DATABASES IN cat")
        assert client.stop_calls == ["qid-1"]

    # A throttling FAILED state is terminal for that execution, so the next attempt
    # must start a NEW one — resuming a dead id would poll a finished execution.
    def test_a_throttling_failed_state_starts_a_new_execution(self):
        client = FakeAthena(
            states=["FAILED", "SUCCEEDED"],
            state_change_reason="Rate exceeded (Service: Athena)",
            pages=[([_row("sales")], None)],
        )
        with patch(f"{_MODULE}.time.sleep"):
            assert _runner(client).run("SHOW DATABASES IN cat") == [["sales"]]
        assert len(client.start_calls) == 2

    # Retrying is not limited to throttles. The client is built with
    # max_attempts=1 to stop botocore's retries multiplying with these, which also
    # gave up the two classes botocore WOULD have retried. Each one otherwise lost a
    # whole table's schema: it fail-softs into `failed` with no columns and no
    # @pk/@fk, and enrichment then invents descriptions over the gap.
    def test_a_transient_5xx_is_retried_then_succeeds(self):
        client = FakeAthena(pages=[([_row("sales")], None)])
        calls = {"n": 0}
        real_start = client.start_query_execution

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ClientError({"Error": {"Code": "InternalServerException"}}, "StartQueryExecution")
            return real_start(**kwargs)

        with patch.object(client, "start_query_execution", side_effect=flaky), patch(f"{_MODULE}.time.sleep"):
            assert _runner(client).run("SHOW DATABASES IN cat") == [["sales"]]
        assert calls["n"] == 2

    def test_a_read_timeout_is_retried_then_succeeds(self):
        # A BotoCoreError carries no `response`, so the throttle check — which reads
        # exc.response — could never have matched it.
        client = FakeAthena(pages=[([_row("sales")], None)])
        calls = {"n": 0}
        real_start = client.start_query_execution

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ReadTimeoutError(endpoint_url="https://athena")
            return real_start(**kwargs)

        with patch.object(client, "start_query_execution", side_effect=flaky), patch(f"{_MODULE}.time.sleep"):
            assert _runner(client).run("SHOW DATABASES IN cat") == [["sales"]]
        assert calls["n"] == 2

    # Retrying stays narrow: a 4xx that is not 429 describes the request, so it will
    # fail identically on every attempt and must surface at once.
    def test_a_client_side_4xx_is_not_retried(self):
        client = FakeAthena(
            start_error=ClientError(
                {"Error": {"Code": "InvalidRequestException"}, "ResponseMetadata": {"HTTPStatusCode": 400}},
                "StartQueryExecution",
            )
        )
        with patch(f"{_MODULE}.time.sleep") as sleep, pytest.raises(AthenaStatementClientError):
            _runner(client).run("SHOW DATABASES IN cat")
        assert len(client.start_calls) == 1
        assert sleep.call_count == 0

    # A 429 with an unrecognised code string is still a throttle.
    def test_http_429_is_treated_as_a_throttle(self):
        err = ClientError(
            {"Error": {"Code": "SomeNewName"}, "ResponseMetadata": {"HTTPStatusCode": 429}},
            "StartQueryExecution",
        )
        client = FakeAthena(start_error=err)
        with patch(f"{_MODULE}.time.sleep"), pytest.raises(AthenaStatementClientError):
            _runner(client).run("SHOW DATABASES IN cat")
        assert len(client.start_calls) == 5

    # Full jitter: sleep is drawn from [0, delay), not the delay itself. A fixed
    # delay would have the whole throttled fan-out retry in the same instant.
    def test_backoff_is_full_jitter_over_a_capped_exponential(self):
        client = FakeAthena(start_error=_throttle_error())
        with (
            patch(f"{_MODULE}.time.sleep"),
            patch(f"{_MODULE}.random.uniform", return_value=0.0) as uniform,
            pytest.raises(AthenaStatementClientError),
        ):
            _runner(client).run("SHOW DATABASES IN cat")
        # RetryPolicy.for_async(): base 0.5, doubling. Lower bound pinned at 0 —
        # that is what makes it full jitter rather than none.
        assert uniform.call_args_list == [call(0, 0.5), call(0, 1.0), call(0, 2.0), call(0, 4.0)]

    # The cap has to actually engage somewhere, or removing min() would go
    # unnoticed. RetryPolicy.for_async() caps at 10.0.
    def test_backoff_is_capped(self):
        client = FakeAthena(start_error=_throttle_error())
        with (
            patch(f"{_MODULE}._THROTTLE_ATTEMPTS", 8),
            patch(f"{_MODULE}.time.sleep"),
            patch(f"{_MODULE}.random.uniform", return_value=0.0) as uniform,
            pytest.raises(AthenaStatementClientError),
        ):
            _runner(client).run("SHOW DATABASES IN cat")
        assert [c.args[1] for c in uniform.call_args_list] == [0.5, 1.0, 2.0, 4.0, 8.0, 10.0, 10.0]

    # The budget is TOTAL, not per-attempt. Five attempts each getting a fresh
    # 120s would put one statement at ~10 minutes against a 15-minute Lambda that
    # still has every other table to do — and a Lambda hard-timeout produces no
    # per-table accounting at all.
    def test_the_timeout_budget_is_total_not_per_attempt(self):
        client = FakeAthena(start_error=_throttle_error())
        # deadline=5; submit at t=0 and is throttled; by the backoff check t=10,
        # so the budget is gone and there is no second attempt.
        with (
            patch(f"{_MODULE}.time.monotonic", side_effect=_clock(0.0, 0.0, 10.0)),
            patch(f"{_MODULE}.time.sleep"),
            pytest.raises(AthenaStatementTimeout),
        ):
            _runner(client, timeout_seconds=5.0).run("DESCRIBE cat.db.t")
        assert len(client.start_calls) == 1

    # Sleeping past the budget only to fail on the next attempt is pure waiting.
    def test_does_not_sleep_past_the_budget(self):
        client = FakeAthena(start_error=_throttle_error())
        with (
            patch(f"{_MODULE}.time.monotonic", side_effect=_clock(0.0, 0.0, 1.0)),
            patch(f"{_MODULE}.time.sleep") as sleep,
            patch(f"{_MODULE}.random.uniform", return_value=999.0),
            pytest.raises(AthenaStatementTimeout),
        ):
            _runner(client, timeout_seconds=5.0).run("SHOW DATABASES IN cat")
        sleep.assert_not_called()


class TestExecutionContext:
    def test_passes_catalog_and_database_context(self):
        client = FakeAthena(pages=[([_row("t")], None)])
        _runner(client).run("SHOW TABLES IN cat.db", catalog="cat", database="db")
        assert client.start_calls[0]["QueryExecutionContext"] == {"Catalog": "cat", "Database": "db"}

    def test_omits_the_context_entirely_when_neither_is_given(self):
        client = FakeAthena(pages=[([_row("t")], None)])
        _runner(client).run("SHOW DATABASES IN cat")
        assert "QueryExecutionContext" not in client.start_calls[0]

    # Omitted, not defaulted to a name: that lands the statement in the account's
    # `primary` workgroup, which is the deliberate choice for a discovery Lambda
    # that serves every namespace and so has no single workgroup to pin.
    def test_omits_workgroup_when_unset(self):
        client = FakeAthena(pages=[([_row("t")], None)])
        _runner(client, workgroup="").run("SHOW DATABASES IN cat")
        assert "WorkGroup" not in client.start_calls[0]

    def test_passes_workgroup_when_set(self):
        client = FakeAthena(pages=[([_row("t")], None)])
        _runner(client, workgroup="wg-1").run("SHOW DATABASES IN cat")
        assert client.start_calls[0]["WorkGroup"] == "wg-1"

    def test_passes_the_output_location(self):
        client = FakeAthena(pages=[([_row("t")], None)])
        _runner(client).run("SHOW DATABASES IN cat")
        assert client.start_calls[0]["ResultConfiguration"] == {"OutputLocation": "s3://results/discovery/"}

    def test_omits_result_configuration_when_no_location_is_available(self):
        client = FakeAthena(pages=[([_row("t")], None)])
        # A workgroup may enforce its own result configuration, in which case
        # sending ours would be rejected as a conflicting override.
        AthenaStatementRunner(client=client, output_location="", workgroup="wg-1").run("SHOW X")
        assert "ResultConfiguration" not in client.start_calls[0]

    def test_derives_the_output_location_from_the_spill_bucket_env(self, monkeypatch):
        monkeypatch.setenv("ATHENA_SPILL_BUCKET", "my-spill")
        client = FakeAthena(pages=[([_row("t")], None)])
        AthenaStatementRunner(client=client).run("SHOW X")
        assert client.start_calls[0]["ResultConfiguration"]["OutputLocation"] == "s3://my-spill/discovery-statements/"

    def test_polls_until_terminal(self):
        client = FakeAthena(states=["QUEUED", "RUNNING", "SUCCEEDED"], pages=[([_row("t")], None)])
        with patch(f"{_MODULE}.time.sleep"):
            assert _runner(client).run("SHOW DATABASES IN cat") == [["t"]]
        assert client.execution_calls == 3

    def test_per_call_timeout_overrides_the_instance_default(self):
        client = FakeAthena(states=["RUNNING"])
        with pytest.raises(AthenaStatementTimeout) as exc:
            _runner(client, timeout_seconds=99.0).run("DESCRIBE cat.db.t", timeout_seconds=0.0)
        assert exc.value.timeout_seconds == 0.0


class TestClientConstruction:
    # Built once in __init__, not lazily: boto3.client() is not safe to call
    # concurrently, and this instance is shared across the DESCRIBE fan-out.
    def test_builds_one_region_pinned_client_up_front(self):
        with patch(f"{_MODULE}.boto3.client", return_value=MagicMock()) as factory:
            runner = AthenaStatementRunner(region="eu-west-1", output_location="s3://x/")
        factory.assert_called_once()
        assert factory.call_args.kwargs["region_name"] == "eu-west-1"
        assert runner._client is factory.return_value  # noqa: SLF001 - construction is under test

    # max_attempts=1: the statement-level throttle retry is the only retry layer.
    # Leaving botocore's standard mode on would make one statement issue up to
    # 5 x 3 submissions, deepening the account-wide queue it is waiting on.
    def test_disables_the_transport_retry_layer(self):
        with patch(f"{_MODULE}.boto3.client", return_value=MagicMock()) as factory:
            AthenaStatementRunner(output_location="s3://x/")
        assert factory.call_args.kwargs["config"].retries["max_attempts"] == 1

    def test_an_injected_client_is_used_as_is(self):
        client = FakeAthena(pages=[([_row("t")], None)])
        with patch(f"{_MODULE}.boto3.client") as factory:
            _runner(client).run("SHOW DATABASES IN cat")
        factory.assert_not_called()

    # Zero would busy-loop get_query_execution for the whole budget — thousands
    # of calls that would themselves cause the throttling this module retries.
    def test_poll_interval_is_floored(self):
        runner = AthenaStatementRunner(client=FakeAthena(), poll_interval_s=0.0)
        assert runner._poll_interval_s > 0  # noqa: SLF001 - the floor is under test
