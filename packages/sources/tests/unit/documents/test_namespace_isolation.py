# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace isolation tests for the document source pipeline.

Verifies that cross-namespace access is rejected at every enforcement point:
  1. S3 deletion cleanup — rejects prefixes not scoped to the caller's namespace
  2. Upload URL generation — upload keys are scoped to the caller's namespace
  3. Document source creation — source is bound to its namespace in DDB
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from coa_common.constants import RESOURCE_PREFIX

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Environment setup — must happen before module import to avoid missing-env
# errors at cold-start time in sources_handler.py / document_routes.py.
# Import sources_handler FIRST to resolve the circular import between
# sources_handler and document_routes (sources_handler does a deferred
# ``from .document_routes import ...`` at module body level).
# ---------------------------------------------------------------------------
os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("INGESTION_QUEUE_URL", "")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.api.sources_handler  # noqa: F401, E402, I001
import coa_sources.api.document_routes as _docr  # noqa: E402, I001

_NS_A = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
_NS_B = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"
_DOC_SOURCE_ID = "src-test-001"
_TENANT_ID = "aaaaaaaaaaaaaaaaaaaaa"
_BUCKET = f"{RESOURCE_PREFIX}-dev-sources-data-123456789012"


# ─────────────────────────────────────────────────────────────────────────────
# 1. S3 Deletion Cleanup — namespace prefix isolation
# ─────────────────────────────────────────────────────────────────────────────


class TestDeletionCleanupNamespaceIsolation:
    """cleanup_handler.handler must refuse to delete objects outside the caller's namespace."""

    _BASE_EVENT = {
        "namespace_id": _NS_A,
        "doc_source_id": _DOC_SOURCE_ID,
        "tenant_id": _TENANT_ID,
        "bucket_name": _BUCKET,
        "source_type": "upload",
        "s3_prefixes": [],
    }

    def _make_s3(self):
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]
        s3.get_paginator.return_value = paginator
        return s3

    def test_rejects_prefix_belonging_to_different_namespace(self):
        """An upload prefix scoped to NS_B must be rejected when NS_A is the caller."""
        from coa_sources.documents.deletion import cleanup_handler

        s3 = self._make_s3()
        cross_ns_prefix = f"{_NS_B}/raw/upload-x/"

        with patch.object(cleanup_handler, "_s3", s3), pytest.raises(ValueError, match="not scoped to namespace"):
            cleanup_handler.handler(
                {**self._BASE_EVENT, "s3_prefixes": [cross_ns_prefix]},
                None,
            )

    def test_rejects_prefix_with_namespace_as_substring(self):
        """A prefix that contains NS_A as a substring but doesn't start with it is rejected."""
        from coa_sources.documents.deletion import cleanup_handler

        # Attacker tries to escape via a path that embeds NS_A after an injection point
        malicious_prefix = f"evil/{_NS_A}/raw/"

        s3 = self._make_s3()
        with patch.object(cleanup_handler, "_s3", s3), pytest.raises(ValueError, match="not scoped to namespace"):
            cleanup_handler.handler(
                {**self._BASE_EVENT, "s3_prefixes": [malicious_prefix]},
                None,
            )

    def test_rejects_mixed_prefixes_when_one_is_cross_namespace(self):
        """If any prefix in the list is cross-namespace, the entire operation fails."""
        from coa_sources.documents.deletion import cleanup_handler

        good_prefix = f"{_NS_A}/raw/upload-a/"
        bad_prefix = f"{_NS_B}/raw/upload-b/"

        s3 = self._make_s3()
        with patch.object(cleanup_handler, "_s3", s3), pytest.raises(ValueError, match="not scoped to namespace"):
            cleanup_handler.handler(
                {**self._BASE_EVENT, "s3_prefixes": [good_prefix, bad_prefix]},
                None,
            )

    def test_accepts_prefix_correctly_scoped_to_namespace(self):
        """A valid prefix scoped to NS_A is accepted."""
        from coa_sources.documents.deletion import cleanup_handler

        valid_prefix = f"{_NS_A}/raw/upload-valid/"

        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{}]  # empty pages — no objects to delete
        s3.get_paginator.return_value = paginator

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler(
                {**self._BASE_EVENT, "s3_prefixes": [valid_prefix]},
                None,
            )

        assert result["namespace_id"] == _NS_A
        assert result["raw_objects_deleted"] == 0
        assert result["staging_objects_deleted"] == 0

    def test_staging_prefix_always_scoped_to_caller_namespace(self):
        """Staging objects are always deleted from the caller's namespace path."""
        from coa_sources.documents.deletion import cleanup_handler

        staging_prefix = f"{_NS_A}/staging/{_DOC_SOURCE_ID}/"

        s3 = MagicMock()
        paginator = MagicMock()
        # Simulate one staging object present
        paginator.paginate.return_value = [{"Contents": [{"Key": f"{staging_prefix}doc.md"}]}]
        s3.get_paginator.return_value = paginator
        s3.delete_objects.return_value = {
            "Deleted": [{"Key": f"{staging_prefix}doc.md"}],
            "Errors": [],
        }

        with patch.object(cleanup_handler, "_s3", s3):
            result = cleanup_handler.handler(
                {**self._BASE_EVENT, "s3_prefixes": [], "source_type": "upload"},
                None,
            )

        # Verify the paginator was called with the correct namespace-scoped staging prefix
        call_args = paginator.paginate.call_args_list
        prefixes_listed = [c.kwargs.get("Prefix", "") for c in call_args]
        assert staging_prefix in prefixes_listed, (
            f"Expected staging prefix {staging_prefix!r} to be listed; got {prefixes_listed}"
        )
        assert result["staging_objects_deleted"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. Upload URL generation — key scoping
# ─────────────────────────────────────────────────────────────────────────────


class TestUploadUrlNamespaceIsolation:
    """Upload pre-signed URLs must be scoped to the caller's namespace."""

    def _make_upload_event(self, namespace_id: str, files: list[dict]) -> dict:
        import json

        return {
            "httpMethod": "POST",
            "resource": "/namespaces/{namespaceId}/sources/upload-urls",
            "pathParameters": {"namespaceId": namespace_id},
            "body": json.dumps({"files": files}),
        }

    def test_upload_key_starts_with_caller_namespace(self):
        """Generated S3 keys must start with the caller's namespaceId."""
        captured_keys: list[str] = []

        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: (
            captured_keys.append(Params["Key"]) or "https://s3.example.com/presigned"
        )

        with (
            patch.object(_docr, "_get_s3", return_value=mock_s3),
            patch.object(_docr, "_BUCKET_NAME", _BUCKET),
        ):
            result = _docr._handle_upload_urls(
                self._make_upload_event(
                    _NS_A,
                    [{"filename": "report.pdf", "contentType": "application/pdf"}],
                ),
                _NS_A,
            )

        assert result["statusCode"] == 200
        for key in captured_keys:
            assert key.startswith(f"{_NS_A}/"), f"Key {key!r} does not start with namespace {_NS_A!r}"

    def test_upload_key_never_starts_with_different_namespace(self):
        """A request for NS_A must never produce a key rooted at NS_B."""
        captured_keys: list[str] = []

        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.side_effect = lambda op, Params, ExpiresIn: (
            captured_keys.append(Params["Key"]) or "https://s3.example.com/presigned"
        )

        with (
            patch.object(_docr, "_get_s3", return_value=mock_s3),
            patch.object(_docr, "_BUCKET_NAME", _BUCKET),
        ):
            _docr._handle_upload_urls(
                self._make_upload_event(
                    _NS_A,
                    [{"filename": "doc.txt", "contentType": "text/plain"}],
                ),
                _NS_A,
            )

        for key in captured_keys:
            assert not key.startswith(f"{_NS_B}/"), f"Key {key!r} must not belong to namespace {_NS_B!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Document source creation — namespace binding
# ─────────────────────────────────────────────────────────────────────────────


class TestDocSourceCreationNamespaceBinding:
    """A document source must be stored under its creation namespace."""

    def test_ddb_item_pk_matches_creator_namespace(self):
        """The DDB PK written on source creation must be NS#{namespace_id}."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[])
        written_items: list[dict] = []
        mock_dao.put.side_effect = lambda item, **kw: written_items.append(item)

        with (
            patch.object(_docr, "_get_dao", return_value=mock_dao),
            patch.object(_docr, "_get_sqs", return_value=MagicMock()),
            patch.object(_docr, "_INGESTION_QUEUE_URL", ""),
            patch("coa_sources.api.document_routes.adjust_namespace_source_count"),
        ):
            from coa_control_plane_server.models.create_document_source_input import (
                CreateDocumentSourceInput,
            )

            req = CreateDocumentSourceInput(
                name="test-source",
                uploadId="11111111-1111-4111-8111-111111111111",
            )
            result = _docr._create_document_source(req, _NS_A, {}, namespace_language="en")

        assert result["statusCode"] == 201
        assert len(written_items) == 1
        item = written_items[0]
        assert item["PK"] == f"NS#{_NS_A}", f"Expected PK 'NS#{_NS_A}', got {item['PK']!r}"
        assert item["namespaceId"] == _NS_A

    def test_source_cannot_be_placed_in_different_namespace(self):
        """Creating a source under NS_A must not write a record under NS_B."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[])
        written_items: list[dict] = []
        mock_dao.put.side_effect = lambda item, **kw: written_items.append(item)

        with (
            patch.object(_docr, "_get_dao", return_value=mock_dao),
            patch.object(_docr, "_get_sqs", return_value=MagicMock()),
            patch.object(_docr, "_INGESTION_QUEUE_URL", ""),
            patch("coa_sources.api.document_routes.adjust_namespace_source_count"),
        ):
            from coa_control_plane_server.models.create_document_source_input import (
                CreateDocumentSourceInput,
            )

            req = CreateDocumentSourceInput(
                name="another-source",
                uploadId="22222222-2222-4222-8222-222222222222",
            )
            _docr._create_document_source(req, _NS_A, {}, namespace_language="en")

        for item in written_items:
            assert item.get("PK") != f"NS#{_NS_B}", f"Source was incorrectly written under namespace {_NS_B!r}"
            assert item.get("namespaceId") != _NS_B
