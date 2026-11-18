# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the db-scan reaper handler.

The reaper is the out-of-band backstop for db-scan executions that end
abnormally (ExecutionTimedOut / ABORTED / FAILED) with no in-machine
terminal-status write. It drives the source to SCAN_FAILED — but only when
the row is still active, so it is idempotent against redelivery and against
the in-machine error chain having already run.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from coa_common.dao import DynamoDBDAO
from coa_control_plane_server.models.source_status import SourceStatus
from coa_sources.database.pipeline.reaper_handler import handler as reap
from moto import mock_aws

from tests.unit.conftest import dao_double

MODULE = "coa_sources.database.pipeline.reaper_handler"


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    monkeypatch.setenv("SOURCES_TABLE", "test-sources")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset the module-level DAO singleton between tests."""
    import coa_sources.database.pipeline.reaper_handler as mod

    mod._dao = None
    yield
    mod._dao = None


def _event(status: str = "TIMED_OUT", input_payload: object = None) -> dict:
    """Build a Step Functions execution-status-change EventBridge event.

    ``input_payload`` is embedded as the raw ``detail.input`` string exactly as
    EventBridge delivers it (the original StartExecution input, JSON-encoded).
    """
    detail: dict[str, object] = {
        "executionArn": "arn:aws:states:us-east-1:123456789012:execution:sm:exec-1",
        "stateMachineArn": "arn:aws:states:us-east-1:123456789012:stateMachine:sm",
        "name": "exec-1",
        "status": status,
    }
    if input_payload is not None:
        detail["input"] = input_payload
    return {
        "detail-type": "Step Functions Execution Status Change",
        "source": "aws.states",
        "detail": detail,
    }


def _active_input() -> str:
    return json.dumps({"namespaceId": "ns-1", "sourceId": "src-1", "scanType": "full"})


class TestReaperHandler:
    @patch(f"{MODULE}._get_dao")
    def test_reaper_active_source_marks_scan_failed(self, mock_get_dao):
        """A source still in an active status (ENRICHING) is driven to SCAN_FAILED."""
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        dao.get.return_value = {"status": SourceStatus.ENRICHING}
        mock_get_dao.return_value = dao

        handler(_event(status="TIMED_OUT", input_payload=_active_input()), None)

        dao.update.assert_called_once()
        kwargs = dao.update.call_args.kwargs
        assert kwargs["key"] == {"PK": "NS#ns-1", "SK": "SRC#src-1"}
        assert kwargs["update_fields"]["status"] == SourceStatus.SCAN_FAILED
        assert "lastScanAt" in kwargs["update_fields"]
        assert "TIMED_OUT" in kwargs["update_fields"]["errorMessage"]
        assert kwargs["condition"] == "attribute_exists(PK)"

    @patch(f"{MODULE}._get_dao")
    def test_reaper_terminal_source_no_update(self, mock_get_dao):
        """A source already in a terminal status is left untouched (idempotent)."""
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        dao.get.return_value = {"status": SourceStatus.COMPLETED}
        mock_get_dao.return_value = dao

        handler(_event(status="ABORTED", input_payload=_active_input()), None)

        dao.update.assert_not_called()

    @patch(f"{MODULE}._get_dao")
    def test_reaper_missing_source_no_update_no_raise(self, mock_get_dao):
        """A missing source row (already deleted) is a no-op, never a raise."""
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        dao.get.return_value = None
        mock_get_dao.return_value = dao

        handler(_event(status="FAILED", input_payload=_active_input()), None)

        dao.update.assert_not_called()

    @patch(f"{MODULE}._get_dao")
    def test_reaper_unparseable_input_no_raise(self, mock_get_dao):
        """A malformed detail.input must be logged and ignored, never raised —
        a single bad event cannot poison the rule for subsequent ones."""
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        mock_get_dao.return_value = dao

        handler(_event(status="TIMED_OUT", input_payload="{not valid json"), None)

        dao.get.assert_not_called()
        dao.update.assert_not_called()

    @patch(f"{MODULE}._get_dao")
    def test_reaper_empty_input_no_raise(self, mock_get_dao):
        """A missing detail.input is a no-op (nothing to reap), never a raise."""
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        mock_get_dao.return_value = dao

        handler(_event(status="TIMED_OUT", input_payload=None), None)

        dao.get.assert_not_called()
        dao.update.assert_not_called()

    @patch(f"{MODULE}._get_dao")
    def test_reaper_input_missing_keys_no_update(self, mock_get_dao):
        """Parseable input that lacks namespaceId/sourceId is a no-op, never a raise."""
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        mock_get_dao.return_value = dao

        handler(_event(status="FAILED", input_payload=json.dumps({"scanType": "full"})), None)

        dao.get.assert_not_called()
        dao.update.assert_not_called()

    @pytest.mark.parametrize("payload", ["123", "[]", "null", '"just-a-string"'])
    @patch(f"{MODULE}._get_dao")
    def test_reaper_non_dict_json_input_no_update(self, mock_get_dao, payload):
        """Valid JSON that is not an object (number, list, null, bare string) is a
        no-op, never an AttributeError — it must not reach the .get() lookups."""
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        mock_get_dao.return_value = dao

        handler(_event(status="TIMED_OUT", input_payload=payload), None)

        dao.get.assert_not_called()
        dao.update.assert_not_called()

    @patch(f"{MODULE}._get_dao")
    def test_reaper_dao_get_client_error_no_raise(self, mock_get_dao):
        """A DynamoDB ClientError on the source lookup is logged and swallowed —
        the Lambda must not crash, or EventBridge would retry the event forever."""
        from botocore.exceptions import ClientError
        from coa_sources.database.pipeline.reaper_handler import handler

        dao = dao_double()
        dao.get.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "GetItem",
        )
        mock_get_dao.return_value = dao

        # Must not raise.
        handler(_event(status="TIMED_OUT", input_payload=_active_input()), None)

        dao.get.assert_called_once()
        dao.update.assert_not_called()


# Scan-job SKs: the failed execution's run, and a re-scan started after it failed.
_THIS_RUN = "2026-11-06T01:00:00.000000Z"
_NEWER_RUN = "2026-11-06T01:00:20.000000Z"
_KEY = {"PK": "NS#ns-1", "SK": "SRC#src-1"}


def _run_input(scan_job_sk: str) -> str:
    return json.dumps({"namespaceId": "ns-1", "sourceId": "src-1", "scanType": "full", "scanJobSK": scan_job_sk})


@pytest.fixture()
def sources() -> Iterator[tuple[Any, DynamoDBDAO]]:
    """A moto sources table behind the reaper's DAO, so its conditions are really evaluated."""
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="test-sources",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK")],
            BillingMode="PAY_PER_REQUEST",
        )
        dao = DynamoDBDAO("test-sources", region="us-east-1")
        with patch(f"{MODULE}._get_dao", return_value=dao):
            yield table, dao


def _row(table: Any) -> dict[str, Any]:
    return table.get_item(Key=_KEY)["Item"]


class TestReaperOnlyFailsItsOwnRun:
    def test_reaper_source_taken_by_newer_run_left_scanning(self, sources):
        """The failed run's execution ends after a re-scan took the source: the newer scan keeps running."""
        table, _dao = sources
        table.put_item(Item={**_KEY, "status": SourceStatus.SCANNING, "currentScanJobId": _NEWER_RUN})

        reap(_event(status="FAILED", input_payload=_run_input(_THIS_RUN)), None)

        assert _row(table)["status"] == SourceStatus.SCANNING

    def test_reaper_newer_run_taking_source_after_the_read_left_scanning(self, sources):
        """A re-scan that takes the source between the reaper's read and its write still wins."""
        table, dao = sources
        table.put_item(Item={**_KEY, "status": SourceStatus.SCANNING, "currentScanJobId": _NEWER_RUN})
        read_before_the_rescan = {**_KEY, "status": SourceStatus.SCANNING, "currentScanJobId": _THIS_RUN}

        with patch.object(dao, "get", return_value=read_before_the_rescan):
            reap(_event(status="TIMED_OUT", input_payload=_run_input(_THIS_RUN)), None)

        assert _row(table)["status"] == SourceStatus.SCANNING

    @patch(f"{MODULE}._get_dao")
    def test_reaper_newer_run_recorded_no_write_attempted(self, mock_get_dao):
        """Read-side check: an expected skip makes no doomed write (and no failed-write warning)."""
        dao = dao_double()
        dao.get.return_value = {"status": SourceStatus.SCANNING, "currentScanJobId": _NEWER_RUN}
        mock_get_dao.return_value = dao

        reap(_event(status="FAILED", input_payload=_run_input(_THIS_RUN)), None)

        dao.update.assert_not_called()

    def test_reaper_own_run_still_active_marks_scan_failed(self, sources):
        table, _dao = sources
        table.put_item(Item={**_KEY, "status": SourceStatus.ENRICHING, "currentScanJobId": _THIS_RUN})

        reap(_event(status="TIMED_OUT", input_payload=_run_input(_THIS_RUN)), None)

        row = _row(table)
        assert row["status"] == SourceStatus.SCAN_FAILED
        assert "TIMED_OUT" in row["errorMessage"]

    def test_reaper_row_without_recorded_run_marks_scan_failed(self, sources):
        """No run recorded: not fenced, so the source is reaped."""
        table, _dao = sources
        table.put_item(Item={**_KEY, "status": SourceStatus.SCANNING})

        reap(_event(status="ABORTED", input_payload=_run_input(_THIS_RUN)), None)

        assert _row(table)["status"] == SourceStatus.SCAN_FAILED

    @pytest.mark.parametrize(
        ("code", "level"), [("ConditionalCheckFailedException", logging.INFO), ("ThrottlingException", logging.ERROR)]
    )
    @patch(f"{MODULE}._get_dao")
    def test_reaper_write_error_is_logged_by_cause_without_raising(self, mock_get_dao, caplog, code, level):
        """A failed fence is expected; any other DynamoDB error is logged as an error with the run."""
        dao = dao_double()
        dao.get.return_value = {"status": SourceStatus.SCANNING, "currentScanJobId": _THIS_RUN}
        dao.update.side_effect = ClientError({"Error": {"Code": code}}, "UpdateItem")
        mock_get_dao.return_value = dao

        with caplog.at_level(logging.INFO, logger=MODULE):
            reap(_event(status="TIMED_OUT", input_payload=_run_input(_THIS_RUN)), None)

        record = caplog.records[-1]
        assert record.levelno == level
        assert "src-1" in record.getMessage()
