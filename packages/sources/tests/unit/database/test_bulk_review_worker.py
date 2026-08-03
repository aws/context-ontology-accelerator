# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the bulk review worker logic.

Tests are scoped to the pure-logic helpers in worker.py (parse_message,
_apply_decision_to_table, process_bulk_review). The Lambda entry point
``handler`` is exercised indirectly via process_bulk_review.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# Environment must be set before importing the worker module
os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.database.bulk_review.worker as worker  # noqa: E402
from coa_common.domain_models import (  # noqa: E402
    BusinessMetadata,
    Column,
    ForeignKey,
    PrimaryKey,
    Table,
)
from coa_common.review_logic import apply_decision_to_table  # noqa: E402

_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
_SOURCE_ID = "src-db-001"


class _RecordingCall:
    """Thread-safe stand-in for a mocked client method.

    ``unittest.mock`` records ``call_count`` / ``call_args_list`` WITHOUT a
    lock. The worker fans ``create_asset_revision`` across a
    ``ThreadPoolExecutor``, so under real thread contention (a loaded CI
    runner) the mock's ``call_count += 1`` / ``call_args_list.append`` race and
    lose increments — the mock undercounts and tests asserting an EXACT call
    count flake (#853, ``AssertionError: assert 2433 == 2500``). This records
    every call under a lock and exposes the same ``call_count`` /
    ``call_args_list`` / ``call_args`` surface the assertions already use, so
    the count is exact regardless of contention. Production ``worker.py`` is
    unchanged — only the test's accounting is made atomic, and the assertion
    still fails (undercounts) if the worker genuinely drops a table.
    """

    def __init__(self, return_value: Any = None) -> None:
        self.return_value = return_value
        self.side_effect: Callable[..., Any] | None = None
        self._lock = threading.Lock()
        self.call_args_list: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Record before invoking the side_effect so a raising side_effect still
        # counts the call (matches unittest.mock semantics).
        with self._lock:
            self.call_args_list.append(call(*args, **kwargs))
        if self.side_effect is not None:
            return self.side_effect(*args, **kwargs)
        return self.return_value

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.call_args_list)

    @property
    def call_args(self) -> Any:
        with self._lock:
            return self.call_args_list[-1] if self.call_args_list else None


def _make_table(
    *,
    table_name: str = "orders",
    status: str = "PENDING_REVIEW",
    columns: list[tuple[str, str]] | None = None,
) -> Table:
    """Build a Table with the given column statuses."""
    if columns is None:
        columns = [("col_a", "PENDING_REVIEW"), ("col_b", "PENDING_REVIEW")]
    return Table(
        name=table_name,
        database="db",
        data_source_id=_SOURCE_ID,
        namespace_id=_NAMESPACE_ID,
        business_metadata=BusinessMetadata(review_status=status),
        columns=[
            Column(
                name=name,
                data_type="string",
                business_metadata=BusinessMetadata(review_status=col_status),
            )
            for name, col_status in columns
        ],
    )


# ===================================================================
# parse_message
# ===================================================================


@pytest.mark.unit
class TestParseMessage:
    def test_valid_json_string(self):
        body = json.dumps({"namespaceId": "ns", "sourceId": "src", "decision": "APPROVED"})
        msg = worker.parse_message(body)
        assert msg.namespace_id == "ns"
        assert msg.source_id == "src"
        assert msg.decision == "APPROVED"

    def test_valid_dict(self):
        body = {"namespaceId": "ns", "sourceId": "src", "decision": "REJECTED"}
        msg = worker.parse_message(body)
        assert msg.decision == "REJECTED"

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            worker.parse_message("not-json")

    def test_missing_field_raises(self):
        with pytest.raises(ValueError, match="missing required fields"):
            worker.parse_message({"namespaceId": "ns", "sourceId": "src"})

    def test_invalid_decision_raises(self):
        with pytest.raises(ValueError, match="Invalid decision"):
            worker.parse_message({"namespaceId": "ns", "sourceId": "src", "decision": "MAYBE"})


# ===================================================================
# apply_decision_to_table — cascade rules + change tracking
# (Helper moved to libs/common/coa_common.review_logic;
# tests imported via the explicit import at the top of this file so
# both the worker and the per-table review handler share one source.)
# ===================================================================


@pytest.mark.unit
class TestApplyDecisionToTable:
    def test_pending_to_approved_flips_table_when_all_columns_terminal(self):
        # Per-asset APPROVE now requires all columns to be terminal first.
        table = _make_table(status="PENDING_REVIEW", columns=[("c1", "APPROVED"), ("c2", "REJECTED")])
        changed = apply_decision_to_table(table, "APPROVED")
        assert changed is True
        assert table.business_metadata.review_status == "APPROVED"
        # Terminal column decisions are preserved
        assert table.columns[0].business_metadata.review_status == "APPROVED"
        assert table.columns[1].business_metadata.review_status == "REJECTED"

    def test_approve_cascades_pending_columns(self):
        table = _make_table(status="PENDING_REVIEW", columns=[("c1", "PENDING_REVIEW"), ("c2", "APPROVED")])
        apply_decision_to_table(table, "APPROVED")
        assert table.business_metadata.review_status == "APPROVED"
        statuses = {c.name: c.business_metadata.review_status for c in table.columns}
        assert statuses == {"c1": "APPROVED", "c2": "APPROVED"}

    def test_approve_preserves_terminal_column_decisions(self):
        table = _make_table(
            status="PENDING_REVIEW",
            columns=[("c1", "APPROVED"), ("c2", "REJECTED"), ("c3", "APPROVED")],
        )
        apply_decision_to_table(table, "APPROVED")
        statuses = {c.name: c.business_metadata.review_status for c in table.columns}
        assert statuses == {"c1": "APPROVED", "c2": "REJECTED", "c3": "APPROVED"}

    def test_reject_cascades_to_all_non_rejected(self):
        table = _make_table(
            status="APPROVED",
            columns=[("c1", "APPROVED"), ("c2", "REJECTED"), ("c3", "PENDING_REVIEW")],
        )
        apply_decision_to_table(table, "REJECTED")
        statuses = {c.name: c.business_metadata.review_status for c in table.columns}
        assert all(s == "REJECTED" for s in statuses.values())

    def test_no_change_when_already_in_target_state(self):
        table = _make_table(status="APPROVED", columns=[("c1", "APPROVED")])
        changed = apply_decision_to_table(table, "APPROVED")
        assert changed is False

    def test_reapprove_cascades_pending_column(self):
        # Re-approving cascades a newly-PENDING column to APPROVED.
        table = _make_table(status="APPROVED", columns=[("c1", "PENDING_REVIEW")])
        assert apply_decision_to_table(table, "APPROVED") is True
        assert table.columns[0].business_metadata.review_status == "APPROVED"

    def test_invalid_decision_raises(self):
        table = _make_table()
        with pytest.raises(ValueError):
            apply_decision_to_table(table, "MAYBE")


# ===================================================================
# process_bulk_review — full pipeline (mocked SMUSClient + DAO)
# ===================================================================


def _mock_smus_with_assets(tables: list[Table]) -> MagicMock:
    """Build a mocked SMUSClient that returns the given tables across one search page."""
    from coa_common.datazone_forms import serialize_form

    mock_client = MagicMock()
    items = []
    forms = {}
    for i, table in enumerate(tables):
        asset = MagicMock()
        asset.name = f"DS#{_SOURCE_ID}:{table.table_id}"
        asset.asset_id = f"asset-{i}"
        items.append(asset)
        forms[asset.asset_id] = {
            "formsOutput": [{"formName": "CoaTableMetadata", "content": json.dumps(serialize_form(table))}]
        }
    mock_client.search_assets.return_value = MagicMock(items=items, next_token=None)
    # get_asset_forms and create_asset_revision are fanned across a
    # ThreadPoolExecutor in the worker; back them with a lock-guarded recorder
    # so call counts / call_args stay exact under contention (see _RecordingCall).
    get_forms = _RecordingCall()
    get_forms.side_effect = lambda asset_id: forms[asset_id]
    mock_client.get_asset_forms = get_forms
    mock_client.create_asset_revision = _RecordingCall(return_value=MagicMock(asset_id="written"))
    return mock_client


@pytest.mark.unit
class TestProcessBulkReview:
    _DAO_PATCH = "coa_sources.database.bulk_review.worker.DynamoDBDAO"

    def _msg(self, decision: str) -> worker.BulkReviewMessage:
        return worker.BulkReviewMessage(
            namespace_id=_NAMESPACE_ID,
            source_id=_SOURCE_ID,
            decision=decision,
        )

    def test_skips_when_source_not_in_approving_state(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVED"}
        mock_client = MagicMock()
        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )
        assert result.tables_total == 0
        mock_client.search_assets.assert_not_called()

    def test_approves_all_pending_tables(self):
        tables = [
            _make_table(table_name="t1", status="PENDING_REVIEW"),
            _make_table(table_name="t2", status="PENDING_REVIEW"),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _mock_smus_with_assets(tables)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        assert result.tables_total == 2
        assert result.tables_changed == 2
        assert result.tables_failed == []
        assert mock_client.create_asset_revision.call_count == 2
        # Final status update sets APPROVED + tablesApproved=2
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        assert final_update.args[1] == {"status": "APPROVED", "tablesApproved": 2}

    def test_skips_already_approved_tables_no_redundant_writes(self):
        tables = [
            _make_table(
                table_name="t1",
                status="APPROVED",
                columns=[("c1", "APPROVED"), ("c2", "APPROVED")],
            ),
            _make_table(
                table_name="t2",
                status="PENDING_REVIEW",
                columns=[("c1", "PENDING_REVIEW")],
            ),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _mock_smus_with_assets(tables)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        assert result.tables_total == 2
        assert result.tables_changed == 1  # Only t2 needed a write (t1 already fully terminal)
        assert mock_client.create_asset_revision.call_count == 1

    def test_cascades_pending_columns_on_already_approved_table(self):
        """Bulk approve cascades PENDING columns even on APPROVED tables."""
        tables = [
            _make_table(
                table_name="t1",
                status="APPROVED",
                columns=[("c1", "APPROVED"), ("c2", "PENDING_REVIEW")],
            ),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _mock_smus_with_assets(tables)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        assert result.tables_total == 1
        assert result.tables_changed == 1
        assert mock_client.create_asset_revision.call_count == 1

    def test_reject_aggressively_flips_approved_tables(self):
        """Bulk reject is aggressive (same as per-asset): it flips a
        previously APPROVED table to REJECTED, and the source becomes
        terminal-REJECTED."""
        from coa_common.datazone_forms import deserialize_form

        tables = [
            _make_table(table_name="t1", status="APPROVED"),
            _make_table(table_name="t2", status="PENDING_REVIEW"),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "REJECTING"}
        mock_client = _mock_smus_with_assets(tables)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("REJECTED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        # Both tables flip to REJECTED (t1 APPROVED clobbered, t2 PENDING).
        assert result.tables_total == 2
        assert result.tables_changed == 2
        assert mock_client.create_asset_revision.call_count == 2
        # No tables remain approved; source lands in terminal REJECTED.
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        assert final_update.args[1] == {"status": "REJECTED", "tablesApproved": 0}
        written_names = {
            deserialize_form(json.loads(c.kwargs["forms_input"][0]["content"]), data_source_id=_SOURCE_ID).name
            for c in mock_client.create_asset_revision.call_args_list
        }
        assert written_names == {"t1", "t2"}

    def test_approve_preserves_explicitly_rejected_tables(self):
        """Symmetric guard: bulk approve must NOT overwrite a table the
        steward already explicitly rejected."""
        from coa_common.datazone_forms import deserialize_form

        tables = [
            _make_table(table_name="t1", status="REJECTED"),
            _make_table(table_name="t2", status="PENDING_REVIEW"),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _mock_smus_with_assets(tables)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        # Only t2 (PENDING) flipped to APPROVED. t1 stays REJECTED.
        assert result.tables_changed == 1
        assert mock_client.create_asset_revision.call_count == 1
        written_form = mock_client.create_asset_revision.call_args.kwargs["forms_input"][0]
        written = deserialize_form(json.loads(written_form["content"]), data_source_id=_SOURCE_ID)
        assert written.name == "t2"
        assert written.business_metadata.review_status == "APPROVED"

        # Terminal status — not all tables approved (t1 still REJECTED), so
        # the source goes to APPROVAL_FAILED rather than APPROVED... actually
        # no: failure-terminal is only on per-asset write failure, not on
        # surviving REJECTED tables. The terminal is APPROVED with
        # tablesApproved = 1 (just t2).
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        assert final_update.args[1] == {"status": "APPROVED", "tablesApproved": 1}

    def test_bulk_approve_preserves_explicitly_rejected_columns(self):
        """Within tables touched by bulk approve, REJECTED columns are preserved."""
        from coa_common.datazone_forms import deserialize_form

        tables = [
            _make_table(
                table_name="t",
                status="PENDING_REVIEW",
                columns=[
                    ("c1", "REJECTED"),  # explicit reject — preserve
                    ("c2", "PENDING_REVIEW"),  # flip to APPROVED
                    ("c3", "APPROVED"),  # already approved — no change
                ],
            ),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _mock_smus_with_assets(tables)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        written_form = mock_client.create_asset_revision.call_args.kwargs["forms_input"][0]
        written = deserialize_form(json.loads(written_form["content"]), data_source_id=_SOURCE_ID)
        col_status = {c.name: c.business_metadata.review_status for c in written.columns}
        assert col_status == {"c1": "REJECTED", "c2": "APPROVED", "c3": "APPROVED"}

    def test_bulk_reject_clobbers_approved_columns(self):
        """Bulk reject is aggressive (same as per-asset): every non-REJECTED
        column — including explicitly APPROVED ones — flips to REJECTED."""
        from coa_common.datazone_forms import deserialize_form

        tables = [
            _make_table(
                table_name="t",
                status="PENDING_REVIEW",
                columns=[
                    ("c1", "APPROVED"),  # explicit approve — clobbered to REJECTED
                    ("c2", "PENDING_REVIEW"),  # flip to REJECTED
                    ("c3", "REJECTED"),  # already rejected — no change
                ],
            ),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "REJECTING"}
        mock_client = _mock_smus_with_assets(tables)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            worker.process_bulk_review(
                msg=self._msg("REJECTED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        written_form = mock_client.create_asset_revision.call_args.kwargs["forms_input"][0]
        written = deserialize_form(json.loads(written_form["content"]), data_source_id=_SOURCE_ID)
        col_status = {c.name: c.business_metadata.review_status for c in written.columns}
        assert col_status == {"c1": "REJECTED", "c2": "REJECTED", "c3": "REJECTED"}

    def test_per_asset_failure_marks_approval_failed(self):
        tables = [
            _make_table(table_name="t1", status="PENDING_REVIEW"),
            _make_table(table_name="t2", status="PENDING_REVIEW"),
        ]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _mock_smus_with_assets(tables)
        # Make second create_asset_revision fail
        call_count = {"n": 0}

        def fail_second(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("simulated")
            return MagicMock(asset_id="ok")

        mock_client.create_asset_revision.side_effect = fail_second

        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        assert len(result.tables_failed) == 1
        # When at least one write fails, terminal state is APPROVAL_FAILED
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        assert final_update.args[1]["status"] == "APPROVAL_FAILED"

    def test_empty_source_advances_to_terminal_status(self):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = MagicMock()
        mock_client.search_assets.return_value = MagicMock(items=[], next_token=None)

        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        assert result.tables_total == 0
        # Empty source should still leave APPROVING — advance to APPROVED for approve
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        assert final_update.args[1] == {"status": "APPROVED", "tablesApproved": 0}


# ===================================================================
# Continuation paging — no silent drop past the old 1000-table cap (#853)
# ===================================================================


def _make_paginated_client(tables: list[Table]) -> MagicMock:
    """Mocked SMUSClient that pages assets 50-at-a-time via next_token.

    next_token is the string offset into the full asset list; search_assets
    honours max_results and returns None once the list is exhausted. This lets
    a test drive the worker across many search pages (and, with a small page
    budget, across many chained invocations).
    """
    from coa_common.datazone_forms import FORM_TYPE_NAME, serialize_form

    mock_client = MagicMock()
    all_items: list[MagicMock] = []
    forms: dict[str, dict] = {}
    for i, table in enumerate(tables):
        asset = MagicMock()
        asset.name = f"DS#{_SOURCE_ID}:t{i}"
        asset.asset_id = f"asset-{i}"
        all_items.append(asset)
        forms[asset.asset_id] = {
            "formsOutput": [{"formName": FORM_TYPE_NAME, "content": json.dumps(serialize_form(table))}]
        }

    def _search(*, project_id, search_text, max_results=50, next_token=None):  # noqa: ANN001, ANN202
        start = int(next_token) if next_token else 0
        end = start + max_results
        page = all_items[start:end]
        nxt = str(end) if end < len(all_items) else None
        return MagicMock(items=page, next_token=nxt)

    mock_client.search_assets.side_effect = _search
    # get_asset_forms and create_asset_revision are fanned across a
    # ThreadPoolExecutor in the worker; back them with a lock-guarded recorder
    # so call counts stay exact under contention (see _RecordingCall).
    get_forms = _RecordingCall()
    get_forms.side_effect = lambda asset_id: forms[asset_id]
    mock_client.get_asset_forms = get_forms
    mock_client.create_asset_revision = _RecordingCall(return_value=MagicMock(asset_id="written"))
    return mock_client


@pytest.mark.unit
class TestBulkReviewPaging:
    _DAO_PATCH = "coa_sources.database.bulk_review.worker.DynamoDBDAO"

    def _msg(self, decision: str = "APPROVED") -> worker.BulkReviewMessage:
        return worker.BulkReviewMessage(
            namespace_id=_NAMESPACE_ID,
            source_id=_SOURCE_ID,
            decision=decision,
        )

    def _drive_chain(
        self,
        msg: worker.BulkReviewMessage,
        mock_client: MagicMock,
        mock_dao: MagicMock,
        mock_sqs: MagicMock,
    ) -> list:
        """Run process_bulk_review, following each re-enqueued continuation
        (parsed back through the real parse_message) until no continuation is
        enqueued. Returns the per-invocation results."""
        results = []
        current = msg
        with (
            patch(self._DAO_PATCH, return_value=mock_dao),
            patch.object(worker, "_get_sqs", return_value=mock_sqs),
            patch.object(worker, "_REVIEW_QUEUE_URL", "test-review-queue"),
        ):
            for _ in range(200):  # safety bound
                before = mock_sqs.send_message.call_count
                results.append(
                    worker.process_bulk_review(
                        msg=current,
                        client=mock_client,
                        project_id="proj-123",
                        sources_table="test-sources",
                        region="us-east-1",
                    )
                )
                if mock_sqs.send_message.call_count > before:
                    body = mock_sqs.send_message.call_args.kwargs["MessageBody"]
                    current = worker.parse_message(body)
                else:
                    break
        return results

    def test_no_silent_drop_past_cap_full_chain_approves_all(self):
        """A source with 2500 tables (≫ old 1000 cap) is FULLY approved across
        the continuation chain — no silent drop."""
        tables = [_make_table(table_name=f"t{i}", status="PENDING_REVIEW") for i in range(2500)]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _make_paginated_client(tables)
        mock_sqs = MagicMock()

        with patch.object(worker, "_PAGE_TABLE_BUDGET", 1000):
            self._drive_chain(self._msg("APPROVED"), mock_client, mock_dao, mock_sqs)

        # Every one of the 2500 tables got a revision write (nothing dropped).
        assert mock_client.create_asset_revision.call_count == 2500
        # The final terminal write carries the FULL accumulated approved count.
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        assert final_update.args[1] == {"status": "APPROVED", "tablesApproved": 2500}

    def test_terminal_state_written_only_on_last_page(self):
        """With assets remaining, the intermediate invocation enqueues a
        continuation and does NOT write a terminal state."""
        tables = [_make_table(table_name=f"t{i}", status="PENDING_REVIEW") for i in range(150)]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _make_paginated_client(tables)
        mock_sqs = MagicMock()

        with (
            patch.object(worker, "_PAGE_TABLE_BUDGET", 100),
            patch(self._DAO_PATCH, return_value=mock_dao),
            patch.object(worker, "_get_sqs", return_value=mock_sqs),
            patch.object(worker, "_REVIEW_QUEUE_URL", "test-review-queue"),
        ):
            # First (intermediate) invocation: 100 of 150 → continuation, no terminal.
            worker.process_bulk_review(
                msg=self._msg("APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        assert mock_sqs.send_message.call_count == 1
        # No terminal write on the intermediate page.
        assert not any("tablesApproved" in c.args[1] for c in mock_dao.update.call_args_list)

    def test_final_accumulated_count_spans_all_pages(self):
        """Final tablesApproved is the sum across pages, not just the last page."""
        # 120 tables, budget 50 → pages of ~50/50/20 across 3 invocations.
        tables = [_make_table(table_name=f"t{i}", status="PENDING_REVIEW") for i in range(120)]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _make_paginated_client(tables)
        mock_sqs = MagicMock()

        with patch.object(worker, "_PAGE_TABLE_BUDGET", 50):
            results = self._drive_chain(self._msg("APPROVED"), mock_client, mock_dao, mock_sqs)

        # Multiple invocations happened (chain, not one shot).
        assert len(results) >= 2
        assert mock_client.create_asset_revision.call_count == 120
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        assert final_update.args[1] == {"status": "APPROVED", "tablesApproved": 120}

    def test_continuation_processed_while_transient(self):
        """A continuation message (next_token set) for a source still in the
        transient state is processed, not skipped by the idempotency guard."""
        tables = [_make_table(table_name=f"t{i}", status="PENDING_REVIEW") for i in range(10)]
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _make_paginated_client(tables)
        mock_sqs = MagicMock()

        cont = worker.BulkReviewMessage(
            namespace_id=_NAMESPACE_ID,
            source_id=_SOURCE_ID,
            decision="APPROVED",
            next_token="0",
            tables_approved_so_far=5,
        )
        with (
            patch(self._DAO_PATCH, return_value=mock_dao),
            patch.object(worker, "_get_sqs", return_value=mock_sqs),
            patch.object(worker, "_REVIEW_QUEUE_URL", "test-review-queue"),
        ):
            worker.process_bulk_review(
                msg=cont,
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

        # It resumed the search (processed), and folded the carried count in.
        mock_client.search_assets.assert_called()
        final_update = next(c for c in mock_dao.update.call_args_list if "tablesApproved" in c.args[1])
        # 5 carried + 10 approved this page = 15
        assert final_update.args[1] == {"status": "APPROVED", "tablesApproved": 15}

    def test_continuation_skipped_when_source_terminal(self):
        """A continuation message for an already-terminal source is skipped."""
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVED"}
        mock_client = MagicMock()
        cont = worker.BulkReviewMessage(
            namespace_id=_NAMESPACE_ID,
            source_id=_SOURCE_ID,
            decision="APPROVED",
            next_token="500",
            tables_approved_so_far=500,
        )
        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=cont,
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )
        assert result.tables_total == 0
        mock_client.search_assets.assert_not_called()


# ===================================================================
# Rejected key-column gate
# ===================================================================


@pytest.mark.unit
class TestRejectedKeyColumnGate:
    """An approved table must not ship with a REJECTED primary/foreign key column.

    `catalog_reader._table_to_induction_format` projects only APPROVED columns, so
    a rejected PK vanishes from the induction catalog while its table stays
    APPROVED — induction then has no key to build a subject template from and the
    failure surfaces as a PK-column-missing error in Ontop, far from the review
    that caused it.

    This gate replaced one that scanned for PENDING_REVIEW columns *after* the
    bulk cascade had already flipped every one of them to APPROVED (unreachable),
    and which could not be moved pre-cascade either: the enricher leaves all
    columns PENDING_REVIEW, so blocking on pending would reject every normal bulk
    approval.
    """

    _DAO_PATCH = "coa_sources.database.bulk_review.worker.DynamoDBDAO"

    @staticmethod
    def _table_with_keys(
        *,
        table_name: str = "orders",
        table_status: str = "APPROVED",
        columns: list[tuple[str, str]],
        pk_columns: list[str] | None = None,
        fk_column: str | None = None,
    ) -> Table:
        return Table(
            name=table_name,
            database="db",
            data_source_id=_SOURCE_ID,
            namespace_id=_NAMESPACE_ID,
            business_metadata=BusinessMetadata(review_status=table_status),
            primary_key=PrimaryKey(columns=pk_columns or []),
            foreign_keys=(
                [ForeignKey(column=fk_column, target_table="other", target_column="id")] if fk_column else []
            ),
            columns=[
                Column(name=name, data_type="string", business_metadata=BusinessMetadata(review_status=status))
                for name, status in columns
            ],
        )

    def _run(self, tables: list[Table]):
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "APPROVING"}
        mock_client = _mock_smus_with_assets(tables)
        with patch(self._DAO_PATCH, return_value=mock_dao):
            return worker.process_bulk_review(
                msg=worker.BulkReviewMessage(namespace_id=_NAMESPACE_ID, source_id=_SOURCE_ID, decision="APPROVED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )

    def test_rejected_primary_key_column_blocks_approval(self):
        result = self._run(
            [
                self._table_with_keys(
                    columns=[("id", "REJECTED"), ("name", "APPROVED")],
                    pk_columns=["id"],
                )
            ]
        )
        assert result.tables_failed, "approval should have been blocked"
        assert "orders.id (primary key)" in result.tables_failed[0]

    def test_rejected_foreign_key_column_blocks_approval(self):
        result = self._run(
            [
                self._table_with_keys(
                    columns=[("id", "APPROVED"), ("customer_id", "REJECTED")],
                    pk_columns=["id"],
                    fk_column="customer_id",
                )
            ]
        )
        assert result.tables_failed
        assert "orders.customer_id (foreign key)" in result.tables_failed[0]

    def test_rejected_non_key_column_does_not_block(self):
        """Rejecting an ordinary column is a legitimate review decision."""
        result = self._run(
            [
                self._table_with_keys(
                    columns=[("id", "APPROVED"), ("notes", "REJECTED")],
                    pk_columns=["id"],
                )
            ]
        )
        assert result.tables_failed == []

    def test_all_approved_keys_do_not_block(self):
        result = self._run(
            [
                self._table_with_keys(
                    columns=[("id", "APPROVED"), ("customer_id", "APPROVED")],
                    pk_columns=["id"],
                    fk_column="customer_id",
                )
            ]
        )
        assert result.tables_failed == []

    def test_pending_columns_do_not_block_the_normal_case(self):
        """The regression the previous gate would have caused if moved pre-cascade.

        The enricher leaves every column PENDING_REVIEW; clearing them is the
        entire point of bulk approve.
        """
        result = self._run(
            [
                self._table_with_keys(
                    table_status="PENDING_REVIEW",
                    columns=[("id", "PENDING_REVIEW"), ("name", "PENDING_REVIEW")],
                    pk_columns=["id"],
                )
            ]
        )
        assert result.tables_failed == []
        assert result.tables_changed == 1

    def test_rejected_key_on_a_rejected_table_does_not_block(self):
        """A table that is not shipping cannot break induction."""
        result = self._run(
            [
                self._table_with_keys(
                    table_status="REJECTED",
                    columns=[("id", "REJECTED")],
                    pk_columns=["id"],
                )
            ]
        )
        assert result.tables_failed == []

    def test_composite_primary_key_partially_rejected_blocks(self):
        result = self._run(
            [
                self._table_with_keys(
                    columns=[("day", "APPROVED"), ("hour", "REJECTED")],
                    pk_columns=["day", "hour"],
                )
            ]
        )
        assert result.tables_failed
        assert "orders.hour (primary key)" in result.tables_failed[0]

    def test_reject_decision_never_runs_the_gate(self):
        """The gate guards approvals only; a bulk REJECT is always allowed."""
        mock_dao = MagicMock()
        mock_dao.get.return_value = {"status": "REJECTING"}
        tables = [
            self._table_with_keys(
                table_status="APPROVED",
                columns=[("id", "REJECTED")],
                pk_columns=["id"],
            )
        ]
        mock_client = _mock_smus_with_assets(tables)
        with patch(self._DAO_PATCH, return_value=mock_dao):
            result = worker.process_bulk_review(
                msg=worker.BulkReviewMessage(namespace_id=_NAMESPACE_ID, source_id=_SOURCE_ID, decision="REJECTED"),
                client=mock_client,
                project_id="proj-123",
                sources_table="test-sources",
                region="us-east-1",
            )
        assert result.tables_failed == []
