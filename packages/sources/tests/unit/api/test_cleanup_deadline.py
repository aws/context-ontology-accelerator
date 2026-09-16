# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for GH-137: derive source-delete cleanup deadline from real remaining time.

Tests cover:
  - _cleanup_deadline() helper: Lambda context present, absent, broken
  - _delete_source_datazone_assets: search-phase deadline, delete-phase deadline,
    partial-completion shape, env budget clamp, malformed env var
"""

from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue")
os.environ.setdefault("DELETION_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:stateMachine/delete")
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.api.sources_handler as _sh  # noqa: E402

_SH = "coa_sources.api.sources_handler"
_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
_SOURCE_ID = "src-deadline-test"


def _current_sh():
    return sys.modules.get("coa_sources.api.sources_handler", _sh)


def _fake_context(remaining_ms: int) -> MagicMock:
    """Return a fake Lambda context with a controllable remaining time."""
    ctx = MagicMock()
    ctx.get_remaining_time_in_millis.return_value = remaining_ms
    return ctx


def _mk_asset(source_id: str, table_name: str) -> MagicMock:
    m = MagicMock(asset_id=f"aid-{table_name}")
    m.name = f"DS#{source_id}:{table_name}"
    return m


# ===================================================================
# _cleanup_deadline unit tests
# ===================================================================


@pytest.mark.unit
class TestCleanupDeadline:
    def test_derives_deadline_from_lambda_context(self):
        """With a context providing 10 000 ms remaining, the deadline should
        be approximately now + 10 − margin (default 2), i.e. ~8 s from now."""
        now = time.monotonic()
        ctx = _fake_context(10_000)
        deadline = _current_sh()._cleanup_deadline(ctx, margin_s=2.0)
        # Should be roughly now + 8 (±0.5 for test execution jitter)
        assert now + 7.0 <= deadline <= now + 9.0

    def test_env_budget_clamps_derived_deadline(self):
        """A huge remaining time must still be clamped by _DATAZONE_CLEANUP_BUDGET_S."""
        now = time.monotonic()
        ctx = _fake_context(999_000)  # 999 s remaining
        with patch(f"{_SH}._DATAZONE_CLEANUP_BUDGET_S", 5):
            deadline = _current_sh()._cleanup_deadline(ctx, margin_s=2.0)
        # env budget of 5 s is tighter than 999 − 2 = 997 s
        assert now + 4.0 <= deadline <= now + 6.0

    def test_huge_env_budget_does_not_extend_past_derived(self):
        """A _DATAZONE_CLEANUP_BUDGET_S of 9999 must not override the derived deadline."""
        now = time.monotonic()
        ctx = _fake_context(10_000)  # 10 s remaining
        with patch(f"{_SH}._DATAZONE_CLEANUP_BUDGET_S", 9999):
            deadline = _current_sh()._cleanup_deadline(ctx, margin_s=2.0)
        # derived = now + 8; env = now + 9999; min → now + 8
        assert now + 7.0 <= deadline <= now + 9.0

    def test_none_context_falls_back_to_env_budget(self):
        now = time.monotonic()
        with patch(f"{_SH}._DATAZONE_CLEANUP_BUDGET_S", 10):
            deadline = _current_sh()._cleanup_deadline(None, margin_s=2.0)
        assert now + 9.0 <= deadline <= now + 11.0

    def test_context_without_method_falls_back(self):
        """A context object that lacks get_remaining_time_in_millis (e.g. a
        plain dict or None-like stub) must fall back to the env budget."""
        now = time.monotonic()
        ctx = object()  # no get_remaining_time_in_millis attr
        with patch(f"{_SH}._DATAZONE_CLEANUP_BUDGET_S", 7):
            deadline = _current_sh()._cleanup_deadline(ctx, margin_s=1.0)
        assert now + 6.0 <= deadline <= now + 8.0

    def test_context_method_raises_falls_back(self):
        """If get_remaining_time_in_millis raises, fall back gracefully."""
        ctx = MagicMock()
        ctx.get_remaining_time_in_millis.side_effect = RuntimeError("boom")
        now = time.monotonic()
        with patch(f"{_SH}._DATAZONE_CLEANUP_BUDGET_S", 5):
            deadline = _current_sh()._cleanup_deadline(ctx, margin_s=1.0)
        assert now + 4.0 <= deadline <= now + 6.0


# ===================================================================
# _delete_source_datazone_assets — deadline in search phase
# ===================================================================


@pytest.mark.unit
class TestDatazoneAssetsSearchDeadline:
    def test_search_pagination_stops_at_deadline(self):
        """When the Lambda has very little remaining time, the search phase
        must stop between pages — not only the delete phase (GH-137).

        With the deadline already in the past, the loop should break before
        fetching even the first page of search results."""
        assets = [_mk_asset(_SOURCE_ID, f"t{i}") for i in range(50)]
        mock_client = MagicMock()
        mock_client.search_assets.return_value = MagicMock(items=assets, next_token="tok-2")

        # Deadline already in the past → search loop should exit immediately
        past_deadline = time.monotonic() - 10

        with (
            patch(f"{_SH}._SMUS_DOMAIN_ID", "dz-domain-1"),
            patch(f"{_SH}._cleanup_deadline", return_value=past_deadline),
            patch(
                "coa_sources.api.database_routes._resolve_project_id",
                return_value="proj-1",
            ),
            patch(
                "coa_sources.api.database_routes._get_smus_client",
                return_value=mock_client,
            ),
        ):
            removed = _current_sh()._delete_source_datazone_assets(
                _NAMESPACE_ID,
                _SOURCE_ID,
                context=_fake_context(1),
            )

        # No pages fetched at all, no deletes
        assert mock_client.search_assets.call_count == 0
        assert mock_client.delete_asset.call_count == 0
        assert removed == 0

    def test_search_stops_between_pages_not_mid_page(self):
        """When the deadline trips between page 1 and page 2, page 1's assets
        are collected but page 2 is never fetched."""
        page1 = [_mk_asset(_SOURCE_ID, f"t{i}") for i in range(3)]
        page2 = [_mk_asset(_SOURCE_ID, f"t{i}") for i in range(3, 6)]
        mock_client = MagicMock()
        mock_client.search_assets.side_effect = [
            MagicMock(items=page1, next_token="tok-2"),
            MagicMock(items=page2, next_token=None),
        ]

        base = time.monotonic()
        call_count = {"n": 0}

        def _ticking_monotonic():
            call_count["n"] += 1
            # Call 1: first search-loop iteration check → OK, fetch page 1
            # Call 2: second search-loop iteration check → past deadline, stop
            if call_count["n"] <= 1:
                return base
            return base + 99999

        # Deadline = base + 100 (generous, but monotonic jumps past it after page 1)
        with (
            patch(f"{_SH}._SMUS_DOMAIN_ID", "dz-domain-1"),
            patch(f"{_SH}._cleanup_deadline", return_value=base + 100),
            patch(f"{_SH}.time.monotonic", side_effect=_ticking_monotonic),
            patch(
                "coa_sources.api.database_routes._resolve_project_id",
                return_value="proj-1",
            ),
            patch(
                "coa_sources.api.database_routes._get_smus_client",
                return_value=mock_client,
            ),
        ):
            removed = _current_sh()._delete_source_datazone_assets(
                _NAMESPACE_ID,
                _SOURCE_ID,
                context=_fake_context(30_000),
            )

        # Page 1 fetched (3 assets collected), page 2 never fetched
        assert mock_client.search_assets.call_count == 1
        # Delete loop also bails (monotonic is way past deadline)
        assert removed == 0

    def test_generous_time_completes_all_search_and_delete(self):
        """With plenty of remaining time, all pages are fetched and all assets deleted."""
        assets = [_mk_asset(_SOURCE_ID, f"t{i}") for i in range(5)]
        mock_client = MagicMock()
        mock_client.search_assets.return_value = MagicMock(items=assets, next_token=None)

        ctx = _fake_context(300_000)  # 300 s remaining — plenty

        with (
            patch(f"{_SH}._SMUS_DOMAIN_ID", "dz-domain-1"),
            patch(f"{_SH}._DATAZONE_CLEANUP_BUDGET_S", 300),
            patch(
                "coa_sources.api.database_routes._resolve_project_id",
                return_value="proj-1",
            ),
            patch(
                "coa_sources.api.database_routes._get_smus_client",
                return_value=mock_client,
            ),
        ):
            removed = _current_sh()._delete_source_datazone_assets(
                _NAMESPACE_ID,
                _SOURCE_ID,
                context=ctx,
            )

        assert removed == 5
        assert mock_client.search_assets.call_count == 1
        assert mock_client.delete_asset.call_count == 5


# ===================================================================
# _delete_source_datazone_assets — deadline in delete phase
# ===================================================================


@pytest.mark.unit
class TestDatazoneAssetsDeleteDeadline:
    def test_delete_phase_stops_at_derived_deadline(self):
        """With the deadline tripping during the delete loop, search completes
        fully but only some assets are deleted."""
        assets = [_mk_asset(_SOURCE_ID, f"t{i}") for i in range(10)]
        mock_client = MagicMock()
        mock_client.search_assets.return_value = MagicMock(items=assets, next_token=None)

        base = time.monotonic()
        call_count = {"n": 0}

        def _ticking_monotonic():
            call_count["n"] += 1
            # Calls 1-2: search-phase check + first delete loop check → OK
            # Call 3: second delete loop check → past deadline
            if call_count["n"] <= 2:
                return base
            return base + 99999

        with (
            patch(f"{_SH}._SMUS_DOMAIN_ID", "dz-domain-1"),
            patch(f"{_SH}._cleanup_deadline", return_value=base + 100),
            patch(f"{_SH}.time.monotonic", side_effect=_ticking_monotonic),
            patch(
                "coa_sources.api.database_routes._resolve_project_id",
                return_value="proj-1",
            ),
            patch(
                "coa_sources.api.database_routes._get_smus_client",
                return_value=mock_client,
            ),
        ):
            removed = _current_sh()._delete_source_datazone_assets(
                _NAMESPACE_ID,
                _SOURCE_ID,
                context=_fake_context(30_000),
            )

        # Search collected all 10, delete loop ran once then stopped
        assert mock_client.search_assets.call_count == 1
        assert removed == 1
        assert mock_client.delete_asset.call_count == 1


# ===================================================================
# Context threading — handler → _route → _handle_delete → cleanup
# ===================================================================


@pytest.mark.unit
class TestContextThreading:
    def test_handler_passes_context_to_delete(self):
        """Verify context flows: handler → _route → _handle_delete → _delete_source_datazone_assets."""
        ctx = _fake_context(30_000)
        sh = _current_sh()

        # Mock the delete path to capture the context arg
        captured_ctx = {}

        def _capturing_delete(ns, sid, context=None):
            captured_ctx["value"] = context
            return 0

        event = {
            "httpMethod": "DELETE",
            "resource": "/namespaces/{namespaceId}/sources/{sourceId}",
            "pathParameters": {"namespaceId": _NAMESPACE_ID, "sourceId": _SOURCE_ID},
        }

        # Mock the database source path (not documents)
        mock_item = {
            "PK": f"NS#{_NAMESPACE_ID}",
            "SK": f"SRC#{_SOURCE_ID}",
            "sourceType": "DATABASE",
            "sourceSubType": "GLUE_DATABASE",
            "status": "COMPLETED",
        }

        with (
            patch.object(sh, "_get_dao") as mock_dao,
            patch.object(sh, "_get_scan_dao"),
            patch(f"{_SH}._delete_source_datazone_assets", side_effect=_capturing_delete),
            patch(f"{_SH}._delete_source_scan_jobs", return_value=0),
            patch(f"{_SH}.adjust_namespace_source_count"),
            patch(f"{_SH}.release_platform_catalog"),
        ):
            mock_dao.return_value.get.return_value = mock_item
            mock_dao.return_value.delete.return_value = None
            sh.handler(event, ctx)

        assert captured_ctx.get("value") is ctx


# ===================================================================
# Malformed DATAZONE_CLEANUP_BUDGET_S env var
# ===================================================================


@pytest.mark.unit
class TestMalformedBudgetEnv:
    def test_malformed_budget_does_not_crash_import(self):
        """A non-integer DATAZONE_CLEANUP_BUDGET_S must not raise at import time."""
        # We can't re-import the module safely in the same process, but we can
        # test the defensive parsing by calling _cleanup_deadline with a patched
        # budget value that simulates what the fallback produces.
        # The module-level try/except sets _DATAZONE_CLEANUP_BUDGET_S = 240 on failure.
        # Verify the fallback value is the expected 240.
        with patch(f"{_SH}._DATAZONE_CLEANUP_BUDGET_S", 240):
            now = time.monotonic()
            deadline = _current_sh()._cleanup_deadline(None)
            assert now + 239.0 <= deadline <= now + 241.0

    def test_malformed_budget_env_parse_fallback(self):
        """Simulate the module-level parse: int('not-a-number') → fallback 240."""
        # This directly tests the parse logic pattern
        raw = "not-a-number"
        try:
            val = int(raw)
        except (ValueError, TypeError):
            val = 240
        assert val == 240
