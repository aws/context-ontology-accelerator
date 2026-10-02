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
from unittest.mock import patch

import pytest
from coa_control_plane_server.models.source_status import SourceStatus

from tests.unit.conftest import dao_double  # noqa: E402

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
        assert kwargs["raise_on_error"] is False

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
