# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Athena's own run-time limit is a timeout at the client, decided from Athena's structured error.

A FAILED query whose ``AthenaError.ErrorType`` is one of Athena's "Query timed out" types raises
``AthenaTimeoutError`` (a ``TimeoutError``) with ``remote_cancelled=True``: the engine ended the query itself, so
the caller can distinguish an engine timeout from its own polling deadline. Free text is never read, so
a cast or column error that echoes the token from a filter value stays ``AthenaQueryError``.
The client's own poll deadline cancels nothing remotely, so it stays unproven.

The Athena error catalog (https://docs.aws.amazon.com/athena/latest/ug/error-reference.html, "Error type reference")
reads "206 | Query timed out" and "210 | Query timed out"; no other type is described so.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_serve.clients.athena import AthenaQueryError, AthenaQueryExecutor, AthenaTimeoutError

pytestmark = pytest.mark.unit

LIMIT = "EXCEEDED_TIME_LIMIT: Query exceeded the maximum execution time limit of 30.00s"


def _executor(status: dict[str, Any]) -> AthenaQueryExecutor:
    with patch("boto3.client") as client:
        athena = MagicMock()
        client.return_value = athena
        athena.get_query_execution.return_value = {"QueryExecution": {"Status": status}}
        executor = AthenaQueryExecutor()
    executor._athena = athena
    return executor


def _failed(reason: str, error_type: int | None, category: int = 1) -> dict[str, Any]:
    status: dict[str, Any] = {"State": "FAILED", "StateChangeReason": reason}
    if error_type is not None:
        status["AthenaError"] = {"ErrorCategory": category, "ErrorType": error_type, "ErrorMessage": reason}
    return status


@pytest.mark.parametrize("error_type", [206, 210])
@pytest.mark.parametrize("reason", [LIMIT, "Query timed out"])
async def test_athena_timed_out_types_are_an_observed_timeout(error_type: int, reason: str) -> None:
    with pytest.raises(AthenaTimeoutError) as raised:
        await _executor(_failed(reason, error_type))._wait_for_completion("qid-1", 30)
    assert isinstance(raised.value, TimeoutError)
    assert raised.value.remote_cancelled is True


@pytest.mark.parametrize(
    ("reason", "error_type"),
    [
        ("INVALID_CAST_ARGUMENT: Cannot cast 'EXCEEDED_TIME_LIMIT' to DECIMAL(3, 2)", 1001),
        ("COLUMN_NOT_FOUND: line 1:8: Column 'EXCEEDED_TIME_LIMIT' cannot be resolved", 1004),
        ("SYNTAX_ERROR: line 1:8: mismatched input", 1006),
        (LIMIT, None),
        (LIMIT, 1009),
        ("Unknown error", 100),
    ],
    ids=["echoed-cast", "echoed-column", "syntax", "text-without-type", "text-with-other-type", "internal"],
)
async def test_any_other_failure_stays_a_query_error(reason: str, error_type: int | None) -> None:
    with pytest.raises(AthenaQueryError):
        await _executor(_failed(reason, error_type))._wait_for_completion("qid-1", 30)


@pytest.mark.parametrize("error_type", [1011, 206], ids=["cancelled-by-user", "cancelled-with-a-timed-out-type"])
async def test_a_cancelled_query_stays_a_query_error(error_type: int) -> None:
    """Only a FAILED query is Athena's own timeout; a CANCELLED one stays a query error whatever its type."""
    status = {
        "State": "CANCELLED",
        "StateChangeReason": LIMIT,
        "AthenaError": {"ErrorCategory": 2, "ErrorType": error_type, "ErrorMessage": "Query canceled by user"},
    }
    with pytest.raises(AthenaQueryError):
        await _executor(status)._wait_for_completion("qid-1", 30)


async def test_the_client_poll_deadline_is_an_unproven_timeout() -> None:
    with pytest.raises(AthenaTimeoutError) as raised:
        await _executor({"State": "RUNNING"})._wait_for_completion("qid-1", 0)
    assert raised.value.remote_cancelled is False
