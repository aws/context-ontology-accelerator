# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Glue-event → rescan consumer (#683 R8)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

_MODULE = "coa_sources.database.glue_event_rescan_handler"
_SH = "coa_sources.api.sources_handler"


def _sqs_event(*bodies: dict) -> dict:
    """SQS batch where message ids are m0, m1, ... in order."""
    return {"Records": [{"messageId": f"m{i}", "body": json.dumps(b)} for i, b in enumerate(bodies)]}


def _failed_ids(result: dict) -> list[str]:
    return [f["itemIdentifier"] for f in result["batchItemFailures"]]


@pytest.mark.unit
class TestGlueEventRescanHandler:
    def test_fires_event_rescan_per_record(self):
        from coa_sources.database import glue_event_rescan_handler as mod

        # autospec: a plain MagicMock accepts any args and hid a dropped argument.
        with patch(f"{_MODULE}._handle_rescan", autospec=True, return_value={"statusCode": 202}) as mock_rescan:
            result = mod.handler(
                _sqs_event(
                    {"namespaceId": "ns-1", "sourceId": "src-1", "trigger": "EVENT"},
                    {"namespaceId": "ns-1", "sourceId": "src-2", "trigger": "EVENT"},
                ),
                None,
            )

        assert result == {"batchItemFailures": []}
        assert mock_rescan.call_count == 2
        # Each call fires an EVENT-trigger rescan for the message's source.
        from coa_control_plane_server.models.scan_trigger import ScanTrigger

        called_sources = {c.args[2] for c in mock_rescan.call_args_list}
        assert called_sources == {"src-1", "src-2"}
        for c in mock_rescan.call_args_list:
            assert c.kwargs["trigger"] == ScanTrigger.EVENT
            # Empty event keeps confirmDiscardOpenReview false.
            assert c.args[0] == {}

    def test_records_missing_ids_are_reported_not_dropped(self):
        """A payload with no ids means the rule's input transformer is wrong.
        Reporting it redrives to the DLQ, where it is visible."""
        from coa_sources.database import glue_event_rescan_handler as mod

        with patch(f"{_MODULE}._handle_rescan", autospec=True, return_value={"statusCode": 202}) as mock_rescan:
            result = mod.handler(_sqs_event({"trigger": "EVENT"}), None)

        assert _failed_ids(result) == ["m0"]
        mock_rescan.assert_not_called()

    def test_bad_body_is_reported_not_dropped(self):
        from coa_sources.database import glue_event_rescan_handler as mod

        with patch(f"{_MODULE}._handle_rescan", autospec=True) as mock_rescan:
            result = mod.handler({"Records": [{"messageId": "m0", "body": "not-json"}]}, None)

        assert _failed_ids(result) == ["m0"]
        mock_rescan.assert_not_called()

    def test_per_record_failure_is_reported_for_retry(self):
        from coa_sources.database import glue_event_rescan_handler as mod

        with patch(f"{_MODULE}._handle_rescan", autospec=True, side_effect=RuntimeError("boom")):
            # Must not raise: raising would redrive the whole batch and re-run
            # the rescans that already succeeded.
            result = mod.handler(
                _sqs_event({"namespaceId": "ns-1", "sourceId": "src-1", "trigger": "EVENT"}),
                None,
            )

        assert _failed_ids(result) == ["m0"]

    def test_one_bad_message_does_not_take_the_batch_with_it(self):
        """The whole point of the partial response: the nine good messages are
        deleted and only the failure is retried."""
        from coa_sources.database import glue_event_rescan_handler as mod

        def _rescan(event, namespace_id, source_id, trigger=None):
            if source_id == "src-bad":
                raise RuntimeError("boom")
            return {"statusCode": 202}

        with patch(f"{_MODULE}._handle_rescan", autospec=True, side_effect=_rescan):
            result = mod.handler(
                _sqs_event(
                    {"namespaceId": "ns-1", "sourceId": "src-ok-1"},
                    {"namespaceId": "ns-1", "sourceId": "src-bad"},
                    {"namespaceId": "ns-1", "sourceId": "src-ok-2"},
                ),
                None,
            )

        assert _failed_ids(result) == ["m1"]

    @pytest.mark.parametrize("status", [200, 202, 400, 409])
    def test_non_server_status_is_not_a_failure(self, status):
        """A 409 for an open review is the designed outcome, not an error, so it
        must not be retried."""
        from coa_sources.database import glue_event_rescan_handler as mod

        with patch(f"{_MODULE}._handle_rescan", autospec=True, return_value={"statusCode": status}):
            result = mod.handler(
                _sqs_event({"namespaceId": "ns-1", "sourceId": "src-1"}),
                None,
            )

        assert result == {"batchItemFailures": []}

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_server_error_status_is_reported_for_retry(self, status):
        """The rescan path returns its faults rather than raising, so a 5xx would
        otherwise look like success and the message would be deleted without ever
        reaching the DLQ."""
        from coa_sources.database import glue_event_rescan_handler as mod

        with patch(f"{_MODULE}._handle_rescan", autospec=True, return_value={"statusCode": status}):
            result = mod.handler(
                _sqs_event({"namespaceId": "ns-1", "sourceId": "src-1"}),
                None,
            )

        assert _failed_ids(result) == ["m0"]

    def test_missing_message_id_cannot_be_reported(self):
        """Nothing to identify to SQS, so it must not emit a malformed entry."""
        from coa_sources.database import glue_event_rescan_handler as mod

        with patch(f"{_MODULE}._handle_rescan", autospec=True, side_effect=RuntimeError("boom")):
            result = mod.handler({"Records": [{"body": json.dumps({"namespaceId": "n", "sourceId": "s"})}]}, None)

        assert result == {"batchItemFailures": []}
