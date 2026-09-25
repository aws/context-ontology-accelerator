# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for S3-based OSI import/export operations."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_metrics.source_status import PERMISSIVE_ENV

pytestmark = pytest.mark.unit


# ── GetOsiUploadUrl tests ───────────────────────────────────────────────


class TestGetOsiUploadUrl:
    """Tests for the get_osi_upload_url handler."""

    def _make_event(self, body: dict | None = None, namespace: str = "test-ns") -> dict:
        return {
            "httpMethod": "POST",
            "pathParameters": {"namespaceId": namespace},
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"email": "test@example.com"}}},
        }

    @patch("coa_metrics.api.get_osi_upload_url._get_s3_client")
    def test_returns_upload_url(self, mock_s3: MagicMock) -> None:
        from coa_metrics.api.get_osi_upload_url import handler

        mock_s3.return_value.generate_presigned_url.return_value = "https://s3.example.com/presigned"

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(self._make_event(body={"filename": "metrics.yaml"}), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["uploadUrl"] == "https://s3.example.com/presigned"
        assert "s3Key" in body
        assert body["s3Key"].startswith("test-ns/imports/")
        assert body["s3Key"].endswith("/metrics.yaml")
        assert body["expiresIn"] == 900

    @patch("coa_metrics.api.get_osi_upload_url._get_s3_client")
    def test_rejects_invalid_extension(self, mock_s3: MagicMock) -> None:
        from coa_metrics.api.get_osi_upload_url import handler

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(self._make_event(body={"filename": "data.json"}), None)

        assert resp["statusCode"] == 400
        assert "extension" in json.loads(resp["body"])["message"].lower()

    @patch("coa_metrics.api.get_osi_upload_url._get_s3_client")
    def test_accepts_yml_extension(self, mock_s3: MagicMock) -> None:
        from coa_metrics.api.get_osi_upload_url import handler

        mock_s3.return_value.generate_presigned_url.return_value = "https://s3.example.com/presigned"

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(self._make_event(body={"filename": "export.yml"}), None)

        assert resp["statusCode"] == 200

    def test_missing_filename_returns_400(self) -> None:
        from coa_metrics.api.get_osi_upload_url import handler

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(self._make_event(body={}), None)

        assert resp["statusCode"] == 400
        assert "filename" in json.loads(resp["body"])["message"]

    def test_rejects_path_traversal_filename(self) -> None:
        from coa_metrics.api.get_osi_upload_url import handler

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(
                self._make_event(body={"filename": "../../other-ns/poison.yaml"}),
                None,
            )

        assert resp["statusCode"] == 400
        assert "filename" in json.loads(resp["body"])["message"].lower()

    def test_missing_namespace_returns_400(self) -> None:
        from coa_metrics.api.get_osi_upload_url import handler

        resp = handler(self._make_event(body={"filename": "test.yaml"}, namespace=""), None)

        assert resp["statusCode"] == 400

    def test_missing_bucket_env_returns_500(self) -> None:
        from coa_metrics.api.get_osi_upload_url import handler

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": ""}, clear=False):
            resp = handler(self._make_event(body={"filename": "test.yaml"}), None)

        assert resp["statusCode"] == 500


# ── Import with S3 key tests ───────────────────────────────────────────


@pytest.mark.parametrize(
    "s3_key",
    [
        "test-ns/imports/./metrics.yaml",
        "test-ns/imports/../metrics.yaml",
        "test-ns/imports//metrics.yaml",
        "test-ns/imports/%2e%2e/metrics.yaml",
    ],
)
def test_import_s3_key_scope_accepts_exact_prefix_with_opaque_suffix(s3_key: str) -> None:
    from coa_metrics.api.import_s3_key import is_import_s3_key_for_namespace

    assert is_import_s3_key_for_namespace(s3_key, "test-ns")


@pytest.mark.parametrize(
    "s3_key",
    [
        "other-ns/imports/metrics.yaml",
        "test-ns/imports-evil/metrics.yaml",
    ],
)
def test_import_s3_key_scope_rejects_foreign_and_lookalike_prefixes(s3_key: str) -> None:
    from coa_metrics.api.import_s3_key import is_import_s3_key_for_namespace

    assert not is_import_s3_key_for_namespace(s3_key, "test-ns")


class TestImportFromS3:
    """Tests for import_osi handler with s3Key parameter."""

    def _make_event(self, body: dict | None = None, namespace: str = "test-ns") -> dict:
        return {
            "httpMethod": "POST",
            "pathParameters": {"namespaceId": namespace},
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"email": "test@example.com"}}},
        }

    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi._get_s3_client")
    def test_reads_content_from_s3(
        self, mock_s3: MagicMock, mock_neptune: MagicMock, mock_opensearch: MagicMock
    ) -> None:
        from coa_metrics.api.import_osi import handler

        yaml_content = (
            'osi_spec_version: "1.0"\n'
            "metrics:\n"
            "  - name: test_metric\n"
            '    description: "Test"\n'
            "    expression:\n"
            "      dialects:\n"
            "        - dialect: ANSI_SQL\n"
            '          expression: "SELECT COUNT(*) FROM orders"\n'
            "    custom_extensions:\n"
            "      - vendor_name: COA\n"
            "        data:\n"
            "          data_source_id: ds-123\n"
            "          source_table: orders\n"
        )

        mock_body = MagicMock()
        mock_body.read.return_value = yaml_content.encode("utf-8")
        mock_s3.return_value.get_object.return_value = {"Body": mock_body}
        mock_neptune.return_value.get_metric.return_value = None
        mock_neptune.return_value.create_metric.return_value = None

        # Permissive lookup: this test covers the S3 read path, not the #564
        # APPROVED-source enforcement import applies per metric (which would
        # otherwise fail closed with a 503 — DATA_SOURCES_TABLE is unset here).
        env = {"OSI_BUCKET_NAME": "test-bucket", PERMISSIVE_ENV: "true"}
        with patch.dict("os.environ", env):
            resp = handler(self._make_event(body={"s3Key": "test-ns/imports/abc/file.yaml"}), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["metricsCreated"] == 1
        mock_s3.return_value.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="test-ns/imports/abc/file.yaml",
        )

    @patch("coa_metrics.api.import_osi._read_from_s3")
    def test_foreign_namespace_s3_key_returns_400_before_io(self, mock_read_from_s3: MagicMock) -> None:
        from coa_metrics.api.import_osi import handler
        from coa_metrics.api.import_s3_key import IMPORT_S3_KEY_SCOPE_ERROR

        resp = handler(
            self._make_event(body={"s3Key": "other-ns/imports/abc/missing.yaml"}),
            None,
        )

        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["message"] == IMPORT_S3_KEY_SCOPE_ERROR
        mock_read_from_s3.assert_not_called()

    @pytest.mark.parametrize("s3_key", [None, 42, [], {}])
    @patch("coa_metrics.api.import_osi._read_from_s3")
    def test_non_string_s3_key_returns_400_before_io(
        self,
        mock_read_from_s3: MagicMock,
        s3_key: object,
    ) -> None:
        from coa_metrics.api.import_osi import handler
        from coa_metrics.api.import_s3_key import IMPORT_S3_KEY_SCOPE_ERROR

        resp = handler(self._make_event(body={"s3Key": s3_key}), None)

        assert resp["statusCode"] == 400
        assert json.loads(resp["body"])["message"] == IMPORT_S3_KEY_SCOPE_ERROR
        mock_read_from_s3.assert_not_called()

    def test_content_and_s3key_mutually_exclusive(self) -> None:
        from coa_metrics.api.import_osi import handler

        resp = handler(
            self._make_event(body={"content": "some yaml", "s3Key": "some/key.yaml"}),
            None,
        )

        assert resp["statusCode"] == 400
        assert "mutually exclusive" in json.loads(resp["body"])["message"]

    def test_neither_content_nor_s3key_returns_400(self) -> None:
        from coa_metrics.api.import_osi import handler

        resp = handler(self._make_event(body={}), None)

        assert resp["statusCode"] == 400
        assert "required" in json.loads(resp["body"])["message"].lower()

    @patch("coa_metrics.api.import_osi._get_s3_client")
    def test_s3_missing_key_returns_404(self, mock_s3: MagicMock) -> None:
        from coa_metrics.api.import_osi import handler

        mock_s3.return_value.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject"
        )

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(self._make_event(body={"s3Key": "test-ns/imports/abc/missing.yaml"}), None)

        assert resp["statusCode"] == 404

    @patch("coa_metrics.api.import_osi._get_s3_client")
    def test_s3_service_error_returns_503(self, mock_s3: MagicMock) -> None:
        from coa_metrics.api.import_osi import handler

        mock_s3.return_value.get_object.side_effect = ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "slow down"}}, "GetObject"
        )

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(self._make_event(body={"s3Key": "test-ns/imports/abc/file.yaml"}), None)

        assert resp["statusCode"] == 503

    def test_inline_size_limit_enforced(self) -> None:
        from coa_metrics.api.import_osi import handler

        # 6MB of content (exceeds 5MB limit)
        large_content = "x" * (6 * 1024 * 1024)
        resp = handler(self._make_event(body={"content": large_content}), None)

        assert resp["statusCode"] == 413
        assert "5MB" in json.loads(resp["body"])["message"]


# ── Export to S3 tests ──────────────────────────────────────────────────


class TestExportToS3:
    """Tests for export_osi handler with format=s3."""

    def _make_event(self, namespace: str = "test-ns", format: str = "s3") -> dict:
        return {
            "httpMethod": "GET",
            "pathParameters": {"namespaceId": namespace},
            "queryStringParameters": {"format": format},
            "requestContext": {"authorizer": {"email": "test@example.com"}},
        }

    @patch("coa_metrics.api.export_osi._get_s3_client")
    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_export_s3_returns_download_url(self, mock_neptune: MagicMock, mock_s3: MagicMock) -> None:
        from coa_metrics.api.export_osi import handler
        from coa_metrics.neptune_client import MetricDefinition, MetricDialect

        mock_neptune.return_value.get_metrics_bulk.return_value = [
            MetricDefinition(
                name="revenue",
                description="Total revenue",
                expression_dialects=[MetricDialect(dialect="POSTGRESQL", expression="SELECT SUM(x) FROM t")],
                data_source_id="ds-1",
                source_table="orders",
            )
        ]
        mock_s3.return_value.put_object.return_value = {}
        mock_s3.return_value.generate_presigned_url.return_value = "https://s3.example.com/download"

        with patch.dict("os.environ", {"OSI_BUCKET_NAME": "test-bucket"}):
            resp = handler(self._make_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["downloadUrl"] == "https://s3.example.com/download"
        assert body["expiresIn"] == 900
        assert "content" not in body

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_export_inline_still_works(self, mock_neptune: MagicMock) -> None:
        from coa_metrics.api.export_osi import handler
        from coa_metrics.neptune_client import MetricDefinition, MetricDialect

        mock_neptune.return_value.get_metrics_bulk.return_value = [
            MetricDefinition(
                name="revenue",
                description="Total revenue",
                expression_dialects=[MetricDialect(dialect="POSTGRESQL", expression="SELECT SUM(x) FROM t")],
                data_source_id="ds-1",
                source_table="orders",
            )
        ]

        resp = handler(self._make_event(format="inline"), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert "content" in body
        assert "revenue" in body["content"]

    @patch("coa_metrics.api.export_osi._get_neptune")
    def test_invalid_format_returns_400(self, mock_neptune: MagicMock) -> None:
        from coa_metrics.api.export_osi import handler

        resp = handler(self._make_event(format="xml"), None)

        assert resp["statusCode"] == 400
        assert "format" in json.loads(resp["body"])["message"].lower()
