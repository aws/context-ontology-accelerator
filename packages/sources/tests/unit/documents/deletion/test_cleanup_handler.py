# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Deletion Pipeline Stage 1: cleanup_handler.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
_DOC_SOURCE_ID = "01AAAAAAAAAAAAAAAAAAAAAA01"
_BUCKET = "test-bucket"
_TENANT_ID = "550e8400e29b41d4a71644665"

_BASE_EVENT = {
    "namespace_id": _NAMESPACE_ID,
    "doc_source_id": _DOC_SOURCE_ID,
    "tenant_id": _TENANT_ID,
    "bucket_name": _BUCKET,
    "source_type": "upload",
    "s3_prefixes": [f"{_NAMESPACE_ID}/raw/upload-id/"],
}


def _make_page(*keys: str) -> dict:
    return {"Contents": [{"Key": k} for k in keys]}


def _mock_s3(pages_by_prefix: dict[str, list[dict]]):
    """Build a mock S3 client where list_objects_v2 returns given pages per prefix."""
    s3 = MagicMock()
    paginator = MagicMock()

    def paginate_side_effect(Bucket, Prefix):
        return iter(pages_by_prefix.get(Prefix, [{}]))

    paginator.paginate.side_effect = paginate_side_effect
    s3.get_paginator.return_value = paginator
    s3.delete_objects.return_value = {"Deleted": [], "Errors": []}
    return s3


# ---------------------------------------------------------------------------
# _delete_s3_prefix
# ---------------------------------------------------------------------------


class TestDeleteS3Prefix:
    def test_deletes_all_objects_in_prefix(self):
        from coa_sources.documents.deletion import cleanup_handler

        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [_make_page("a/1.txt", "a/2.txt")]
        s3.get_paginator.return_value = paginator
        s3.delete_objects.return_value = {
            "Deleted": [{"Key": "a/1.txt"}, {"Key": "a/2.txt"}],
            "Errors": [],
        }

        with patch.object(cleanup_handler, "_s3", s3):
            count = cleanup_handler._delete_s3_prefix(_BUCKET, "a/")

        assert count == 2
        s3.delete_objects.assert_called_once_with(
            Bucket=_BUCKET,
            Delete={"Objects": [{"Key": "a/1.txt"}, {"Key": "a/2.txt"}]},
        )

    def test_empty_prefix_returns_zero(self):
        from coa_sources.documents.deletion import cleanup_handler

        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]  # no Contents key
        s3.get_paginator.return_value = paginator

        with patch.object(cleanup_handler, "_s3", s3):
            count = cleanup_handler._delete_s3_prefix(_BUCKET, "empty/")

        assert count == 0
        s3.delete_objects.assert_not_called()

    def test_paginated_deletion(self):
        from coa_sources.documents.deletion import cleanup_handler

        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            _make_page("a/1.txt", "a/2.txt"),
            _make_page("a/3.txt"),
        ]
        s3.get_paginator.return_value = paginator
        s3.delete_objects.side_effect = [
            {"Deleted": [{"Key": "a/1.txt"}, {"Key": "a/2.txt"}], "Errors": []},
            {"Deleted": [{"Key": "a/3.txt"}], "Errors": []},
        ]

        with patch.object(cleanup_handler, "_s3", s3):
            count = cleanup_handler._delete_s3_prefix(_BUCKET, "a/")

        assert count == 3
        assert s3.delete_objects.call_count == 2

    def test_partial_delete_failure_raises(self):
        """delete_objects returning Errors → RuntimeError raised."""
        from coa_sources.documents.deletion import cleanup_handler

        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [_make_page("a/1.txt")]
        s3.get_paginator.return_value = paginator
        s3.delete_objects.return_value = {
            "Deleted": [],
            "Errors": [{"Key": "a/1.txt", "Code": "AccessDenied", "Message": "denied"}],
        }

        with patch.object(cleanup_handler, "_s3", s3), pytest.raises(RuntimeError, match="partial failure"):
            cleanup_handler._delete_s3_prefix(_BUCKET, "a/")


# ---------------------------------------------------------------------------
# handler() — upload type
# ---------------------------------------------------------------------------


class TestHandlerUploadType:
    def test_upload_deletes_raw_and_staging(self):
        from coa_sources.documents.deletion import cleanup_handler

        raw_prefix = f"{_NAMESPACE_ID}/raw/upload-id/"
        staging_prefix = f"{_NAMESPACE_ID}/staging/{_DOC_SOURCE_ID}/"

        s3 = _mock_s3(
            {
                raw_prefix: [_make_page(f"{raw_prefix}doc.pdf")],
                staging_prefix: [_make_page(f"{staging_prefix}doc.md")],
            }
        )
        s3.delete_objects.side_effect = [
            {"Deleted": [{"Key": f"{raw_prefix}doc.pdf"}], "Errors": []},
            {"Deleted": [{"Key": f"{staging_prefix}doc.md"}], "Errors": []},
        ]

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler({**_BASE_EVENT, "s3_prefixes": [raw_prefix]}, None)

        assert result["raw_objects_deleted"] == 1
        assert result["staging_objects_deleted"] == 1
        assert result["namespace_id"] == _NAMESPACE_ID
        assert result["doc_source_id"] == _DOC_SOURCE_ID
        assert result["tenant_id"] == _TENANT_ID

    def test_upload_empty_s3_prefixes_only_deletes_staging(self):
        from coa_sources.documents.deletion import cleanup_handler

        staging_prefix = f"{_NAMESPACE_ID}/staging/{_DOC_SOURCE_ID}/"
        s3 = _mock_s3({staging_prefix: [_make_page(f"{staging_prefix}doc.md")]})
        s3.delete_objects.return_value = {
            "Deleted": [{"Key": f"{staging_prefix}doc.md"}],
            "Errors": [],
        }

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler({**_BASE_EVENT, "s3_prefixes": []}, None)

        assert result["raw_objects_deleted"] == 0
        assert result["staging_objects_deleted"] == 1

    def test_upload_prefix_wrong_namespace_raises(self):
        """Prefix not scoped to namespace_id → ValueError."""
        from coa_sources.documents.deletion import cleanup_handler

        s3 = _mock_s3({})
        with patch.object(cleanup_handler, "_s3", s3), pytest.raises(ValueError, match="not scoped to namespace"):
            cleanup_handler.handler(
                {**_BASE_EVENT, "s3_prefixes": ["other-namespace/raw/upload/"]},
                None,
            )

    def test_upload_multiple_prefixes_all_validated(self):
        """All prefixes must be namespace-scoped; first bad one raises."""
        from coa_sources.documents.deletion import cleanup_handler

        good = f"{_NAMESPACE_ID}/raw/upload-a/"
        bad = "other-ns/raw/upload-b/"
        s3 = _mock_s3({good: [{}]})
        s3.delete_objects.return_value = {"Deleted": [], "Errors": []}

        with patch.object(cleanup_handler, "_s3", s3), pytest.raises(ValueError, match="not scoped to namespace"):
            cleanup_handler.handler(
                {**_BASE_EVENT, "s3_prefixes": [good, bad]},
                None,
            )


# ---------------------------------------------------------------------------
# handler() — s3 type
# ---------------------------------------------------------------------------


class TestHandlerS3Type:
    def test_s3_type_only_deletes_staging(self):
        """For s3 source type, raw files are in customer bucket — skip them."""
        from coa_sources.documents.deletion import cleanup_handler

        staging_prefix = f"{_NAMESPACE_ID}/staging/{_DOC_SOURCE_ID}/"
        s3 = _mock_s3({staging_prefix: [_make_page(f"{staging_prefix}doc.md")]})
        s3.delete_objects.return_value = {
            "Deleted": [{"Key": f"{staging_prefix}doc.md"}],
            "Errors": [],
        }

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler(
                {
                    **_BASE_EVENT,
                    "source_type": "s3",
                    "s3_prefixes": ["customer-bucket/some/prefix/"],
                },
                None,
            )

        assert result["raw_objects_deleted"] == 0
        assert result["staging_objects_deleted"] == 1

    def test_s3_type_skips_prefix_validation(self):
        """s3 type never iterates s3_prefixes, so no namespace check runs."""
        from coa_sources.documents.deletion import cleanup_handler

        staging_prefix = f"{_NAMESPACE_ID}/staging/{_DOC_SOURCE_ID}/"
        s3 = _mock_s3({staging_prefix: [{}]})

        with patch.object(cleanup_handler, "_s3", s3):
            # Would raise ValueError if prefix validation ran for s3 type
            result = cleanup_handler.handler(
                {
                    **_BASE_EVENT,
                    "source_type": "s3",
                    "s3_prefixes": ["completely-different-namespace/raw/"],
                },
                None,
            )

        assert result["raw_objects_deleted"] == 0


# ---------------------------------------------------------------------------
# handler() — return value structure
# ---------------------------------------------------------------------------


class TestHandlerReturnValue:
    def test_return_value_has_all_required_keys(self):
        from coa_sources.documents.deletion import cleanup_handler

        staging_prefix = f"{_NAMESPACE_ID}/staging/{_DOC_SOURCE_ID}/"
        s3 = _mock_s3({staging_prefix: [{}]})

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler({**_BASE_EVENT, "s3_prefixes": []}, None)

        assert set(result.keys()) == {
            "namespace_id",
            "doc_source_id",
            "tenant_id",
            "raw_objects_deleted",
            "staging_objects_deleted",
            "scan_results_objects_deleted",
        }

    def test_tenant_id_passed_through(self):
        from coa_sources.documents.deletion import cleanup_handler

        staging_prefix = f"{_NAMESPACE_ID}/staging/{_DOC_SOURCE_ID}/"
        s3 = _mock_s3({staging_prefix: [{}]})

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler({**_BASE_EVENT, "s3_prefixes": []}, None)

        assert result["tenant_id"] == _TENANT_ID


@pytest.mark.unit
class TestScanResultsCleanup:
    def test_deletes_scan_results_reports(self):
        """The preprocessing diagnostics reports under {ns}/scan-results/{ds}/
        (issue 104) must be deleted with the source — they carry the customer's
        full object keys for every failed file."""
        from coa_sources.documents.deletion import cleanup_handler

        staging_prefix = f"{_NAMESPACE_ID}/staging/{_DOC_SOURCE_ID}/"
        scan_results_prefix = f"{_NAMESPACE_ID}/scan-results/{_DOC_SOURCE_ID}/"
        s3 = _mock_s3(
            {
                staging_prefix: [_make_page(f"{staging_prefix}doc.md")],
                scan_results_prefix: [_make_page(f"{scan_results_prefix}1-issues.json")],
            }
        )
        s3.delete_objects.side_effect = [
            {"Deleted": [{"Key": f"{staging_prefix}doc.md"}], "Errors": []},
            {"Deleted": [{"Key": f"{scan_results_prefix}1-issues.json"}], "Errors": []},
        ]

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler({**_BASE_EVENT, "source_type": "s3", "s3_prefixes": []}, None)

        assert result["scan_results_objects_deleted"] == 1
