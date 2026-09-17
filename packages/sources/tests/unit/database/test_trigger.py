# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the scan trigger Lambda."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

os.environ["STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:test-pipeline"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Import after setting env var
import sys
from pathlib import Path

_trigger_dir = str(Path(__file__).resolve().parents[3] / "database/trigger")
if _trigger_dir not in sys.path:
    sys.path.insert(0, _trigger_dir)
from index import handler  # noqa: E402


@pytest.fixture
def valid_message():
    return {
        "datasourceId": "DS#ds-123",
        "scanJobId": "SCAN#scan-456",
        "namespaceId": "ns-test",
        "scanType": "full",
    }


@pytest.fixture
def sqs_event(valid_message):
    return {
        "Records": [
            {
                "messageId": "msg-001",
                "body": json.dumps(valid_message),
            }
        ]
    }


@patch("index.sfn_client")
def test_starts_execution_on_valid_message(mock_sfn, sqs_event, valid_message):
    handler(sqs_event, None)
    # `isRescan` and `hadOpenRescan` are always added (normalized to strings),
    # both defaulting to "false" for a scan whose message carries neither marker.
    mock_sfn.start_execution.assert_called_once_with(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name="scan-456",
        input=json.dumps({**valid_message, "isRescan": "false", "hadOpenRescan": "false"}),
    )


@patch("index.sfn_client")
def test_only_forwards_required_fields(mock_sfn):
    body = {
        "datasourceId": "DS#x",
        "scanJobId": "SCAN#y",
        "namespaceId": "ns-1",
        "scanType": "full",
        "extraField": "should-be-stripped",
    }
    event = {"Records": [{"messageId": "msg-extra", "body": json.dumps(body)}]}
    handler(event, None)
    call_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
    assert "extraField" not in call_input
    required = {k: body[k] for k in ("datasourceId", "scanJobId", "namespaceId", "scanType")}
    assert call_input == {**required, "isRescan": "false", "hadOpenRescan": "false"}


@patch("index.sfn_client")
def test_forwards_rescan_marker_as_string_true(mock_sfn):
    body = {
        "datasourceId": "DS#x",
        "scanJobId": "SCAN#y",
        "namespaceId": "ns-1",
        "scanType": "full",
        "isRescan": True,
    }
    event = {"Records": [{"messageId": "msg-rescan", "body": json.dumps(body)}]}
    handler(event, None)
    call_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
    assert call_input["isRescan"] == "true"


@patch("index.sfn_client")
def test_rescan_marker_defaults_to_string_false(mock_sfn, sqs_event):
    handler(sqs_event, None)
    call_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
    assert call_input["isRescan"] == "false"


@patch("index.sfn_client")
def test_forwards_had_open_rescan_marker_as_string_true(mock_sfn):
    # hadOpenRescan=true (source was in RESCAN_REVIEW) must reach the execution
    # input so discovery reconstructs the approved baseline from the backup blob.
    body = {
        "datasourceId": "DS#x",
        "scanJobId": "SCAN#y",
        "namespaceId": "ns-1",
        "scanType": "full",
        "isRescan": True,
        "hadOpenRescan": True,
    }
    event = {"Records": [{"messageId": "msg-open-rescan", "body": json.dumps(body)}]}
    handler(event, None)
    call_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
    assert call_input["hadOpenRescan"] == "true"


@patch("index.sfn_client")
def test_had_open_rescan_defaults_to_string_false(mock_sfn, sqs_event):
    # Absent marker (first scan, or a re-scan from APPROVED) => "false", so
    # discovery treats the live assets as the approved baseline and ignores any
    # stale backup.
    handler(sqs_event, None)
    call_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
    assert call_input["hadOpenRescan"] == "false"


@patch("index.sfn_client")
def test_drops_malformed_message_missing_fields(mock_sfn):
    event = {
        "Records": [
            {
                "messageId": "msg-bad",
                "body": json.dumps({"datasourceId": "DS#x"}),
            }
        ]
    }
    handler(event, None)
    mock_sfn.start_execution.assert_not_called()


@patch("index.sfn_client")
def test_drops_invalid_json(mock_sfn):
    event = {
        "Records": [
            {
                "messageId": "msg-json-err",
                "body": "not-json{{{",
            }
        ]
    }
    handler(event, None)
    mock_sfn.start_execution.assert_not_called()


@patch("index.sfn_client")
def test_reraises_client_error(mock_sfn, sqs_event):
    from botocore.exceptions import ClientError

    mock_sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
        "StartExecution",
    )
    with pytest.raises(ClientError):
        handler(sqs_event, None)


@patch("index.sfn_client")
def test_execution_already_exists_is_idempotent(mock_sfn, sqs_event):
    from botocore.exceptions import ClientError

    mock_sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "Already exists"}},
        "StartExecution",
    )
    # Should not raise — duplicate is handled gracefully
    handler(sqs_event, None)


@patch("index.sfn_client")
def test_unexpected_exception_reraises(mock_sfn, sqs_event):
    mock_sfn.start_execution.side_effect = ValueError("unexpected")
    with pytest.raises(ValueError):
        handler(sqs_event, None)


@patch("index.sfn_client")
def test_mixed_batch_processes_valid_after_invalid(mock_sfn):
    event = {
        "Records": [
            {"messageId": "msg-bad", "body": json.dumps({"datasourceId": "DS#x"})},
            {
                "messageId": "msg-good",
                "body": json.dumps(
                    {
                        "datasourceId": "DS#a",
                        "scanJobId": "SCAN#b",
                        "namespaceId": "ns-1",
                        "scanType": "full",
                    }
                ),
            },
        ]
    }
    handler(event, None)
    mock_sfn.start_execution.assert_called_once()
