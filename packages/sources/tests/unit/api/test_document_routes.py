# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for document_routes.py — DOCUMENTS source route handlers."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from coa_control_plane_server.models.source_type import SourceType

# ---------------------------------------------------------------------------
# Environment setup — must happen before module import
# ---------------------------------------------------------------------------
os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue")
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"
_SOURCE_ID = "src-doc-001"

# Import sources_handler FIRST to resolve circular import, then document_routes
from coa_common.constants import bucket_namespace_tag_key  # noqa: E402, I001
import coa_sources.api.sources_handler  # noqa: F401, I001
import coa_sources.api.document_routes as _docr  # noqa: E402, I001

_DOCR = "coa_sources.api.document_routes"
_SH = "coa_sources.api.sources_handler"


@pytest.fixture(autouse=True)
def reset_lazy_clients():
    """Reset module-level lazy client globals between tests to prevent cross-test pollution."""
    from unittest.mock import patch

    import coa_sources.api.namespace_counters as nc
    import coa_sources.api.sources_handler as sh

    sh._dao = None
    sh._sqs = None
    sh._s3 = None
    sh._sfn = None
    sh._scan_dao = None
    sh._ns_dao = None
    nc._ns_dao = None
    with patch("coa_sources.api.document_routes.adjust_namespace_source_count"):
        yield
    sh._dao = None
    sh._sqs = None
    sh._s3 = None
    sh._sfn = None
    sh._scan_dao = None
    sh._ns_dao = None
    nc._ns_dao = None


def _parse(result):
    return result["statusCode"], json.loads(result["body"]) if result.get("body") else {}


def _make_event(body=None):
    return {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/sources",
        "pathParameters": {"namespaceId": _NAMESPACE_ID},
        "queryStringParameters": {},
        "body": json.dumps(body) if body else None,
        "requestContext": {},
    }


def _make_doc_req(
    name="my-doc-source",
    source_bucket_arn=None,
    s3_prefixes=None,
    role_arn=None,
    extraction_config=None,
    upload_id="11111111-1111-4111-8111-111111111111",
):
    req = MagicMock()
    req.name = name
    req.source_bucket_arn = source_bucket_arn
    req.s3_prefixes = s3_prefixes or ["uploads/"]
    req.role_arn = role_arn
    req.extraction_config = extraction_config
    req.upload_id = upload_id
    return req


# ===================================================================
# _create_document_source
# ===================================================================


@pytest.mark.unit
class TestCreateDocumentSource:
    def test_create_upload_source_happy_path(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, body = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        assert "sourceId" in body
        mock_dao.put.assert_called_once()
        mock_sqs.send_message.assert_called_once()
        # Prefix is derived server-side from the uploadId, never taken from s3Prefixes.
        put_item = mock_dao.put.call_args[0][0]
        assert put_item["s3Prefixes"] == [f"{_NAMESPACE_ID}/raw/11111111-1111-4111-8111-111111111111/"]

    def test_create_snapshots_namespace_language_into_ingestion_input(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="ja")
            )

        assert status == 201
        message = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert message["namespace_language"] == "ja"
        # The source record does not duplicate the namespace's language.
        assert "language" not in mock_dao.put.call_args[0][0]
        assert "namespaceLanguage" not in mock_dao.put.call_args[0][0]

    def test_create_s3_source_happy_path(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        req = _make_doc_req(
            name="s3-source",
            source_bucket_arn="arn:aws:s3:::my-bucket",
            s3_prefixes=["data/"],
        )

        with (
            patch(f"{_DOCR}.get_bucket_tags", return_value={bucket_namespace_tag_key(): _NAMESPACE_ID}),
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, body = _parse(
                _docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        put_item = mock_dao.put.call_args[0][0]
        assert put_item["sourceSubType"] == "S3"
        assert put_item["sourceBucketArn"] == "arn:aws:s3:::my-bucket"

    def test_create_blank_name_returns_400(self):
        status, body = _parse(
            _docr._create_document_source(
                _make_doc_req(name="  "), _NAMESPACE_ID, _make_event(), namespace_language="en"
            )
        )
        assert status == 400
        assert "name" in body["error"]

    def test_create_duplicate_name_returns_409(self):
        existing = MagicMock()
        existing.get = lambda k, d=None: {
            "name": "my-doc-source",
            "status": "COMPLETED",
            "SK": "SRC#existing-id",
        }.get(k, d)

        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[existing], last_evaluated_key=None)

        with patch(f"{_DOCR}._get_dao", return_value=mock_dao):
            status, body = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 409
        assert "already exists" in body["error"]

    def test_create_ddb_put_fails_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_dao.put.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "PutItem")

        with patch(f"{_DOCR}._get_dao", return_value=mock_dao):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 500

    def test_create_sqs_fails_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError({"Error": {"Code": "SQSError"}}, "SendMessage")

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 500

    def test_create_no_ingestion_queue_skips_sqs(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", ""),
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201

    def test_create_with_role_arn_stored(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        req = _make_doc_req(
            name="s3-with-role",
            source_bucket_arn="arn:aws:s3:::my-bucket",
            role_arn="arn:aws:iam::123456789012:role/my-role",
        )

        with (
            patch(f"{_DOCR}.get_bucket_tags", return_value={bucket_namespace_tag_key(): _NAMESPACE_ID}),
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, body = _parse(
                _docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        put_item = mock_dao.put.call_args[0][0]
        assert put_item["roleArn"] == "arn:aws:iam::123456789012:role/my-role"

    def test_create_gsi_query_fails_returns_500(self):
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.query.side_effect = ClientError({"Error": {"Code": "InternalError"}}, "Query")

        with patch(f"{_DOCR}._get_dao", return_value=mock_dao):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 500

    def test_create_stores_tenant_id(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        put_item = mock_dao.put.call_args[0][0]
        assert "tenantId" in put_item

    def test_create_stores_extraction_config(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        put_item = mock_dao.put.call_args[0][0]
        assert "extractionConfig" in put_item

    def test_create_local_upload_sub_type(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        put_item = mock_dao.put.call_args[0][0]
        assert put_item["sourceSubType"] == "LOCAL_UPLOAD"
        assert put_item["docSourceType"] == "upload"

    def test_upload_ignores_caller_supplied_prefix(self):
        """A caller-supplied s3Prefixes pointing at another
        namespace is ignored; the stored and enqueued prefix is derived
        server-side from the uploadId under the caller's own namespace."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()

        foreign = "99999999-9999-4999-8999-999999999999"
        req = _make_doc_req(
            s3_prefixes=[f"{foreign}/raw/"],
            upload_id="22222222-2222-4222-8222-222222222222",
        )

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
        ):
            status, _ = _parse(
                _docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        derived = f"{_NAMESPACE_ID}/raw/22222222-2222-4222-8222-222222222222/"
        put_item = mock_dao.put.call_args[0][0]
        assert put_item["s3Prefixes"] == [derived]
        # The foreign namespace prefix must never reach the ingestion job.
        sqs_body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert sqs_body["s3_prefixes"] == [derived]
        assert foreign not in json.dumps(sqs_body)

    def test_upload_missing_upload_id_returns_400(self):
        req = _make_doc_req(upload_id=None)
        status, body = _parse(_docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en"))
        assert status == 400
        assert "uploadId" in body["error"]

    def test_upload_id_with_path_traversal_rejected(self):
        """A malformed uploadId that could smuggle a path separator or traversal
        segment is rejected, so the derived prefix can never escape the
        namespace. Guards the validate_id() check from silent removal."""
        for bad in ("../other-ns", "ns/path", "..", "ns\\path", "a b"):
            req = _make_doc_req(upload_id=bad)
            status, body = _parse(
                _docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
            assert status == 400, f"expected 400 for uploadId={bad!r}, got {status}"
            assert "uploadId" in body["error"]

    def test_s3_whitespace_only_prefix_filtered(self):
        """Whitespace-only S3 prefixes are dropped rather than becoming an empty
        (whole-bucket) prefix; real prefixes are kept."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()
        req = _make_doc_req(
            name="s3-ws",
            source_bucket_arn="arn:aws:s3:::my-bucket",
            s3_prefixes=["  ", "data/"],
        )
        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
            # S3 sources are authorized by the bucket's own namespace tag.
            patch(f"{_DOCR}.get_bucket_tags", return_value={bucket_namespace_tag_key(): _NAMESPACE_ID}),
        ):
            status, _ = _parse(
                _docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 201
        put_item = mock_dao.put.call_args[0][0]
        assert put_item["s3Prefixes"] == ["data/"]


# ===================================================================
# _create_document_source — namespace sourceCount maintenance
# ===================================================================


@pytest.mark.unit
class TestCreateDocumentSourceCounter:
    """The namespace ``sourceCount`` must be incremented when a DOCUMENTS
    source is created.

    The autouse ``reset_lazy_clients`` fixture patches
    ``adjust_namespace_source_count`` globally; each test re-patches it with a
    local handle so the call can be asserted.
    """

    def test_create_increments_namespace_source_count(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=MagicMock()),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/ingestion-queue"),
            patch(f"{_DOCR}.adjust_namespace_source_count") as mock_counter,
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 201
        mock_counter.assert_called_once_with(_NAMESPACE_ID, SourceType.DOCUMENTS, 1)

    def test_create_does_not_count_when_ddb_put_fails(self):
        """If the source row never persists the counter must not be touched."""
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_dao.put.side_effect = ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "PutItem")

        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=MagicMock()),
            patch(f"{_DOCR}.adjust_namespace_source_count") as mock_counter,
        ):
            status, _ = _parse(
                _docr._create_document_source(_make_doc_req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )

        assert status == 500
        mock_counter.assert_not_called()


# ===================================================================
# _handle_upload_urls
# ===================================================================


@pytest.mark.unit
class TestHandleUploadUrls:
    def _make_upload_event(self, body=None):
        return {
            "body": json.dumps(body) if body is not None else None,
            "pathParameters": {"namespaceId": _NAMESPACE_ID},
        }

    def test_upload_urls_happy_path(self):
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://s3.amazonaws.com/presigned-url"

        with (
            patch(f"{_DOCR}._get_s3", return_value=mock_s3),
            patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"),
        ):
            event = self._make_upload_event({"files": [{"filename": "doc.pdf", "contentType": "application/pdf"}]})
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))

        assert status == 200
        assert "uploadId" in body
        assert "uploadUrls" in body
        assert len(body["uploadUrls"]) == 1
        assert body["uploadUrls"][0]["filename"] == "doc.pdf"
        assert "uploadUrl" in body["uploadUrls"][0]
        # Regression: never sign ContentLength — under SigV4 it forces the
        # browser to PUT exactly that many bytes or get a 403 (see !1018).
        assert "ContentLength" not in mock_s3.generate_presigned_url.call_args.kwargs["Params"]

    def test_upload_urls_multiple_files(self):
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://s3.amazonaws.com/presigned-url"

        with (
            patch(f"{_DOCR}._get_s3", return_value=mock_s3),
            patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"),
        ):
            event = self._make_upload_event(
                {
                    "files": [
                        {"filename": "doc1.pdf", "contentType": "application/pdf"},
                        {"filename": "doc2.txt", "contentType": "text/plain"},
                    ]
                }
            )
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))

        assert status == 200
        assert len(body["uploadUrls"]) == 2

    def test_upload_urls_no_bucket_returns_500(self):
        with patch(f"{_DOCR}._BUCKET_NAME", ""):
            status, _ = _parse(_docr._handle_upload_urls(self._make_upload_event({"files": []}), _NAMESPACE_ID))
        assert status == 500

    def test_upload_urls_empty_files_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            status, body = _parse(_docr._handle_upload_urls(self._make_upload_event({"files": []}), _NAMESPACE_ID))
        assert status == 400
        assert "files" in body["error"]

    def test_upload_urls_invalid_json_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            status, _ = _parse(_docr._handle_upload_urls({"body": "not-json"}, _NAMESPACE_ID))
        assert status == 400

    def test_upload_urls_too_many_files_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            files = [{"filename": f"doc{i}.pdf", "contentType": "application/pdf"} for i in range(101)]
            status, body = _parse(_docr._handle_upload_urls(self._make_upload_event({"files": files}), _NAMESPACE_ID))
        assert status == 400
        assert "Too many files" in body["error"]

    def test_upload_urls_blank_filename_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            event = self._make_upload_event({"files": [{"filename": "", "contentType": "application/pdf"}]})
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))
        assert status == 400
        assert "filename" in body["error"]

    def test_upload_urls_invalid_filename_chars_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            event = self._make_upload_event({"files": [{"filename": "../evil.pdf", "contentType": "application/pdf"}]})
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))
        assert status == 400

    def test_upload_urls_unsupported_content_type_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            event = self._make_upload_event(
                {"files": [{"filename": "script.exe", "contentType": "application/x-msdownload"}]}
            )
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))
        assert status == 400
        assert "not supported" in body["error"]

    def test_upload_urls_blank_content_type_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            event = self._make_upload_event({"files": [{"filename": "doc.pdf", "contentType": ""}]})
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))
        assert status == 400

    def test_upload_urls_non_dict_file_entry_returns_400(self):
        with patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"):
            event = self._make_upload_event({"files": ["not-a-dict"]})
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))
        assert status == 400

    def test_upload_urls_s3_error_returns_500(self):
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.side_effect = ClientError({"Error": {"Code": "S3Error"}}, "GeneratePresignedUrl")

        with (
            patch(f"{_DOCR}._get_s3", return_value=mock_s3),
            patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"),
        ):
            event = self._make_upload_event({"files": [{"filename": "doc.pdf", "contentType": "application/pdf"}]})
            status, _ = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))

        assert status == 500

    def test_upload_urls_response_has_s3_prefix(self):
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://s3.amazonaws.com/presigned-url"

        with (
            patch(f"{_DOCR}._get_s3", return_value=mock_s3),
            patch(f"{_DOCR}._BUCKET_NAME", "test-bucket"),
        ):
            event = self._make_upload_event({"files": [{"filename": "doc.pdf", "contentType": "application/pdf"}]})
            status, body = _parse(_docr._handle_upload_urls(event, _NAMESPACE_ID))

        assert status == 200
        assert "s3Prefix" in body
        assert _NAMESPACE_ID in body["s3Prefix"]
        assert body["expiresIn"] == 900


class TestBucketNamespaceAuthorizationAtCreate:
    """An S3 source is persisted only when the bucket's own tag authorizes the
    namespace. Checked here so the customer learns at create time rather than from
    a SCAN_FAILED an hour later; the preprocessing handler re-checks independently.
    """

    def _req(self, **kw):
        return _make_doc_req(
            name=kw.pop("name", "s3-src"),
            source_bucket_arn="arn:aws:s3:::customer-bucket",
            s3_prefixes=["data/"],
            **kw,
        )

    def test_untagged_bucket_returns_400_and_persists_nothing(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}.get_bucket_tags", return_value={}),
        ):
            status, body = _parse(
                _docr._create_document_source(self._req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 400
        assert bucket_namespace_tag_key() in body["error"]
        mock_dao.put.assert_not_called()

    def test_bucket_tagged_for_another_namespace_returns_400(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}.get_bucket_tags", return_value={bucket_namespace_tag_key(): "some-other-namespace"}),
        ):
            status, body = _parse(
                _docr._create_document_source(self._req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 400
        mock_dao.put.assert_not_called()

    def test_unreadable_tags_fail_closed(self):
        """We never persist a bucket we cannot prove is authorized."""
        from botocore.exceptions import ClientError

        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(
                f"{_DOCR}.get_bucket_tags",
                side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetBucketTagging"),
            ),
        ):
            status, body = _parse(
                _docr._create_document_source(self._req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 400
        assert "s3:GetBucketTagging" in body["error"]
        mock_dao.put.assert_not_called()

    def test_malformed_arn_returns_400_not_500(self):
        """parse_bucket_from_arn raises rather than returning empty, so an unguarded
        call would surface as a 500. The Smithy pattern normally rejects this
        upstream; this must not depend on that."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        req = _make_doc_req(name="bad-arn", source_bucket_arn="not-an-arn", s3_prefixes=["data/"])
        with patch(f"{_DOCR}._get_dao", return_value=mock_dao):
            status, body = _parse(
                _docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 400
        assert "malformed" in body["error"]
        mock_dao.put.assert_not_called()

    def test_transient_fault_returns_retryable_503_not_a_permissions_400(self):
        """A DNS/TLS/timeout fault is not the caller's request being wrong — telling
        them to fix a tag would misdirect them. Still fails closed."""
        from botocore.exceptions import EndpointConnectionError

        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(
                f"{_DOCR}.get_bucket_tags",
                side_effect=EndpointConnectionError(endpoint_url="https://s3.amazonaws.com"),
            ),
        ):
            status, body = _parse(
                _docr._create_document_source(self._req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 503
        assert "retry" in body["error"].lower()
        assert "GetBucketTagging" not in body["error"]
        mock_dao.put.assert_not_called()

    def test_bucket_shared_across_namespaces_is_accepted(self):
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()
        shared = f"other-ns {_NAMESPACE_ID} third-ns"
        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q"),
            patch(f"{_DOCR}.get_bucket_tags", return_value={bucket_namespace_tag_key(): shared}),
        ):
            status, _ = _parse(
                _docr._create_document_source(self._req(), _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 201

    def test_upload_sources_are_not_tag_checked(self):
        """Upload sources read the platform's own bucket, which no customer tags."""
        mock_dao = MagicMock()
        mock_dao.query.return_value = MagicMock(items=[], last_evaluated_key=None)
        mock_sqs = MagicMock()
        req = _make_doc_req(name="upload-src", s3_prefixes=[f"{_NAMESPACE_ID}/raw/abc/"])
        with (
            patch(f"{_DOCR}._get_dao", return_value=mock_dao),
            patch(f"{_DOCR}._get_sqs", return_value=mock_sqs),
            patch(f"{_DOCR}._INGESTION_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/q"),
            patch(f"{_DOCR}.get_bucket_tags") as mock_tags,
        ):
            status, _ = _parse(
                _docr._create_document_source(req, _NAMESPACE_ID, _make_event(), namespace_language="en")
            )
        assert status == 201
        mock_tags.assert_not_called()
