# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Retrying a failed re-scan must keep the merge path.

A re-scan of an APPROVED source that fails leaves the source SCAN_FAILED with
its curated assets still live. Re-scanning from SCAN_FAILED used to send
``isRescan=false``, so discovery took the first-scan path and revised every live
asset with fresh, uncurated metadata. v0.3.4's SCHEDULED and EVENT triggers made
that retry run with nobody watching.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from coa_control_plane_server.models.scan_trigger import ScanTrigger
from coa_sources.database.rescan_backup import BACKUP_SCAN_JOB_FIELD

from tests.unit.api.test_sources_handler_extended import (  # noqa: I001
    _NAMESPACE_ID,
    _SOURCE_ID,
    _current_sh,
    _db_source_item,
    _parse,
)

_SH = "coa_sources.api.sources_handler"
_PRE = "preScanStatus"
_QUEUE = "https://sqs.us-east-1.amazonaws.com/123/scan-queue"


def _rescan(item: dict[str, Any], trigger: str = ScanTrigger.MANUAL, event: dict | None = None):
    """Run _handle_rescan on ``item``; return (status, queue message, lock-write call)."""
    # Outside the EVENT cooldown, so the EVENT trigger is not debounced.
    item.setdefault("lastScanAt", (datetime.now(UTC) - timedelta(hours=2)).isoformat())
    mock_dao = MagicMock()
    mock_dao.get.return_value = item
    mock_sqs = MagicMock()
    with (
        patch(f"{_SH}._get_dao", return_value=mock_dao),
        patch(f"{_SH}._get_scan_dao", return_value=MagicMock()),
        patch(f"{_SH}._get_sqs", return_value=mock_sqs),
        patch(f"{_SH}._SCAN_QUEUE_URL", _QUEUE),
    ):
        status, _body = _parse(_current_sh()._handle_rescan(event or {}, _NAMESPACE_ID, _SOURCE_ID, trigger=trigger))
    message = None
    if mock_sqs.send_message.called:
        message = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
    lock = mock_dao.update.call_args_list[0] if mock_dao.update.call_args_list else None
    return status, message, lock


def _failed(**fields: Any) -> dict[str, Any]:
    item = _db_source_item("SCAN_FAILED")
    item.update(fields)
    return item


@pytest.mark.unit
class TestUnattendedRetryOfFailedRescan:
    """The reported case: a SCAN_FAILED row written before this fix (no recorded
    origin) whose ``tablesApproved`` shows it was approved. SCHEDULED and EVENT
    retries must keep the merge path."""

    @pytest.mark.parametrize("trigger_name", ["SCHEDULED", "EVENT"])
    def test_automated_retry_of_previously_approved_source_keeps_merge_path(self, trigger_name):
        item = _db_source_item("SCAN_FAILED")
        item["tablesApproved"] = 42  # the source was approved and curated before the re-scan failed
        item["lastScanAt"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

        status, message, _lock = _rescan(item, trigger=getattr(ScanTrigger, trigger_name))

        assert status == 202
        assert message is not None
        assert message["isRescan"] is True, (
            f"{trigger_name} retry of a previously-approved SCAN_FAILED source enqueued isRescan="
            f"{message['isRescan']}: discovery will overwrite curated assets"
        )
        # A legacy row cannot say whether the failed re-scan had merged, so it is
        # treated as a re-scan from APPROVED.
        assert message["hadOpenRescan"] is False


@pytest.mark.unit
class TestRetryUsesRecordedOrigin:
    @pytest.mark.parametrize("trigger", [ScanTrigger.MANUAL, ScanTrigger.SCHEDULED, ScanTrigger.EVENT])
    def test_failed_rescan_from_approved_retries_as_rescan(self, trigger):
        status, message, _lock = _rescan(_failed(**{_PRE: "APPROVED"}), trigger=trigger)

        assert status == 202
        assert message["isRescan"] is True
        assert message["hadOpenRescan"] is False

    def test_failed_rescan_from_open_review_rebuilds_from_the_backup(self):
        # The steward already confirmed discarding the open review when they started
        # the run that failed, so the retry needs no second confirmation, and the
        # blob is still the approved pre-image.
        status, message, _lock = _rescan(_failed(**{_PRE: "RESCAN_REVIEW"}), trigger=ScanTrigger.SCHEDULED)

        assert status == 202
        assert message["isRescan"] is True
        assert message["hadOpenRescan"] is True

    def test_failed_rescan_that_had_written_its_backup_rebuilds_from_the_backup(self):
        # The failed run got past its backup write, so the live assets may already be
        # its unreviewed merge. Treating them as the baseline would lose the approved
        # pre-image of every table that run changed.
        item = _failed(**{_PRE: "APPROVED", BACKUP_SCAN_JOB_FIELD: "2026-10-03T07:27:05.000000Z"})

        status, message, _lock = _rescan(item, trigger=ScanTrigger.EVENT)

        assert status == 202
        assert message["isRescan"] is True
        assert message["hadOpenRescan"] is True

    def test_failed_first_scan_still_retries_as_first_scan(self):
        # Origin recorded as SCAN_FAILED means the chain began with a failed first
        # scan: nothing was ever approved, even if per-table counters say otherwise.
        status, message, _lock = _rescan(_failed(**{_PRE: "SCAN_FAILED", "tablesApproved": 3}))

        assert status == 202
        assert message["isRescan"] is False
        assert message["hadOpenRescan"] is False

    @pytest.mark.parametrize("tables_approved", [None, 0, "0", "not-a-number"])
    def test_legacy_row_without_approvals_retries_as_first_scan(self, tables_approved):
        item = _failed()
        if tables_approved is not None:
            item["tablesApproved"] = tables_approved

        status, message, _lock = _rescan(item)

        assert status == 202
        assert message["isRescan"] is False
        assert message["hadOpenRescan"] is False

    def test_backup_marker_alone_does_not_make_a_first_scan_a_rescan(self):
        status, message, _lock = _rescan(_failed(**{_PRE: "SCAN_FAILED", BACKUP_SCAN_JOB_FIELD: "x"}))

        assert message["isRescan"] is False
        assert message["hadOpenRescan"] is False


@pytest.mark.unit
class TestLockWriteRecordsOrigin:
    @pytest.mark.parametrize("entry_status", ["APPROVED", "RESCAN_REVIEW"])
    def test_rescan_records_its_starting_status(self, entry_status):
        event = {"body": json.dumps({"confirmDiscardOpenReview": True})} if entry_status == "RESCAN_REVIEW" else {}

        status, _message, lock = _rescan(_db_source_item(entry_status), event=event)

        assert status == 202
        fields = lock.args[1]
        assert fields["status"] == "SCANNING"
        assert fields[_PRE] == entry_status
        # Still the conditional lock on the status just read.
        assert lock.kwargs["condition_values"] == {":prev": entry_status}

    def test_retry_carries_the_origin_forward(self):
        # A retry that also fails must still remember the chain began at APPROVED,
        # not record SCAN_FAILED as its own origin.
        _status, _message, lock = _rescan(_failed(**{_PRE: "APPROVED"}))

        assert lock.args[1][_PRE] == "APPROVED"

    def test_legacy_retry_records_the_inferred_origin(self):
        _status, _message, lock = _rescan(_failed(tablesApproved=5))

        assert lock.args[1][_PRE] == "APPROVED"

    def test_first_scan_retry_records_scan_failed(self):
        _status, _message, lock = _rescan(_failed())

        assert lock.args[1][_PRE] == "SCAN_FAILED"

    def test_rescan_from_approved_drops_a_stale_backup_marker(self):
        # From APPROVED the live assets are the baseline, so a marker left by an
        # earlier re-scan is stale; if THIS run fails before writing its own backup,
        # the retry must not rebuild from the old blob.
        item = _db_source_item("APPROVED")
        item[BACKUP_SCAN_JOB_FIELD] = "2026-09-01T00:00:00.000000Z"

        _status, message, lock = _rescan(item)

        assert lock.kwargs["remove_fields"] == [BACKUP_SCAN_JOB_FIELD]
        assert message["hadOpenRescan"] is False

    @pytest.mark.parametrize("entry", [{"status": "SCAN_FAILED", _PRE: "APPROVED"}, {"status": "RESCAN_REVIEW"}])
    def test_other_starts_keep_the_backup_marker(self, entry):
        item = _db_source_item(entry["status"])
        item.update({k: v for k, v in entry.items() if k != "status"})
        item[BACKUP_SCAN_JOB_FIELD] = "2026-10-03T07:27:05.000000Z"
        event = {"body": json.dumps({"confirmDiscardOpenReview": True})} if entry["status"] == "RESCAN_REVIEW" else {}

        _status, _message, lock = _rescan(item, event=event)

        assert lock.kwargs["remove_fields"] is None
