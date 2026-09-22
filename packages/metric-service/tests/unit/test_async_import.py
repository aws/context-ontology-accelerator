# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for async import: job store, import worker, get_import_job handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.source_status import PERMISSIVE_ENV, SourceValidationUnavailableError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _permissive_source_lookup(monkeypatch: pytest.MonkeyPatch):
    """Keep unrelated worker tests independent from deployed source metadata."""
    monkeypatch.setenv(PERMISSIVE_ENV, "true")
    yield


# ── import_job_store tests ──────────────────────────────────────────────


class TestImportJobStore:
    """Tests for the DynamoDB job store."""

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_create_job(self, mock_table):
        from coa_metrics.api.import_job_store import create_job

        table = MagicMock()
        mock_table.return_value = table

        job = create_job("ns-1", "imports/test.yaml", 100)

        assert job["namespaceId"] == "ns-1"
        assert job["s3Key"] == "imports/test.yaml"
        assert job["metricsTotal"] == 100
        assert job["status"] == "IN_PROGRESS"
        assert job["metricsProcessed"] == 0
        assert "jobId" in job
        table.put_item.assert_called_once()

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_get_job_found(self, mock_table):
        from coa_metrics.api.import_job_store import get_job

        table = MagicMock()
        table.get_item.return_value = {"Item": {"jobId": "j-1", "status": "IN_PROGRESS"}}
        mock_table.return_value = table

        result = get_job("ns-1", "j-1")

        assert result is not None
        assert result["jobId"] == "j-1"

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_get_job_not_found(self, mock_table):
        from coa_metrics.api.import_job_store import get_job

        table = MagicMock()
        table.get_item.return_value = {}
        mock_table.return_value = table

        result = get_job("ns-1", "j-missing")

        assert result is None

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_complete_job(self, mock_table):
        from coa_metrics.api.import_job_store import complete_job

        table = MagicMock()
        mock_table.return_value = table

        complete_job("ns-1", "j-1", status="COMPLETED")

        table.update_item.assert_called_once()
        call_kwargs = table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":s"] == "COMPLETED"

    @patch("coa_metrics.api.import_job_store._get_table")
    def test_update_job_progress_uses_atomic_add(self, mock_table):
        from coa_metrics.api.import_job_store import update_job_progress

        table = MagicMock()
        mock_table.return_value = table

        update_job_progress("ns-1", "j-1", metrics_processed=5, metrics_created=3, metrics_updated=2, errors=["e1"])

        table.update_item.assert_called_once()
        kwargs = table.update_item.call_args[1]
        expr = kwargs["UpdateExpression"]
        assert expr.startswith("ADD metricsProcessed :mp, metricsCreated :mc, metricsUpdated :mu")
        assert "list_append(errors, :errs)" in expr
        vals = kwargs["ExpressionAttributeValues"]
        assert (vals[":mp"], vals[":mc"], vals[":mu"]) == (5, 3, 2)
        assert vals[":errs"] == ["e1"]


# ── get_import_job handler tests ────────────────────────────────────────


class TestGetImportJobHandler:
    """Tests for the GET /import-jobs/{jobId} handler."""

    @patch("coa_metrics.api.get_import_job.get_job")
    def test_returns_job(self, mock_get_job):
        from coa_metrics.api.get_import_job import handler

        mock_get_job.return_value = {
            "jobId": "j-1",
            "status": "IN_PROGRESS",
            "metricsTotal": 100,
            "metricsProcessed": 45,
            "metricsCreated": 30,
            "metricsUpdated": 15,
            "errors": [],
            "warnings": [],
        }

        event = {
            "pathParameters": {"namespaceId": "ns-1", "jobId": "j-1"},
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["jobId"] == "j-1"
        assert body["status"] == "IN_PROGRESS"
        assert body["metricsProcessed"] == 45

    @patch("coa_metrics.api.get_import_job.get_job")
    def test_returns_404_when_not_found(self, mock_get_job):
        from coa_metrics.api.get_import_job import handler

        mock_get_job.return_value = None

        event = {
            "pathParameters": {"namespaceId": "ns-1", "jobId": "j-missing"},
        }
        result = handler(event, None)

        assert result["statusCode"] == 404

    def test_returns_400_when_no_job_id(self):
        from coa_metrics.api.get_import_job import handler

        event = {
            "pathParameters": {"namespaceId": "ns-1", "jobId": ""},
        }
        result = handler(event, None)

        assert result["statusCode"] == 400


# ── import_osi async decision tests ─────────────────────────────────────


class TestImportOsiAsyncDecision:
    """Tests for the sync/async threshold in import_osi handler."""

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._read_from_s3")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_large_import_returns_202(self, mock_parse, mock_s3_read, mock_sqs):
        from coa_metrics.api.import_osi import handler

        # Mock parse result with >30 metrics
        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3_read.return_value = "yaml content"
        mock_sqs_client = MagicMock()
        mock_sqs.return_value = mock_sqs_client

        with patch("coa_metrics.api.import_job_store.create_job") as mock_create:
            mock_create.return_value = {"jobId": "j-async", "status": "IN_PROGRESS"}

            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"s3Key": "imports/big-file.yaml"}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["jobId"] == "j-async"
        assert body["status"] == "IN_PROGRESS"
        mock_sqs_client.send_message.assert_called_once()

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._read_from_s3")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_enqueue_failure_returns_500_and_fails_job(self, mock_parse, mock_s3_read, mock_sqs):
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3_read.return_value = "yaml content"
        mock_sqs.return_value.send_message.side_effect = Exception("sqs down")

        with (
            patch("coa_metrics.api.import_job_store.create_job") as mock_create,
            patch("coa_metrics.api.import_job_store.complete_job") as mock_complete,
        ):
            mock_create.return_value = {"jobId": "j-async", "status": "IN_PROGRESS"}
            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"s3Key": "ns-1/imports/big.yaml"}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 500
        mock_complete.assert_called_once_with("ns-1", "j-async", status="FAILED")

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._get_s3_client")
    @patch("coa_metrics.api.import_osi._get_bucket", return_value="test-bucket")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_inline_content_above_threshold_writes_s3_and_returns_202(self, mock_parse, mock_bucket, mock_s3, mock_sqs):
        """Inline content with >30 metrics is staged to S3 and processed async."""
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3_client = MagicMock()
        mock_s3.return_value = mock_s3_client
        mock_sqs_client = MagicMock()
        mock_sqs.return_value = mock_sqs_client

        with patch("coa_metrics.api.import_job_store.create_job") as mock_create:
            mock_create.return_value = {"jobId": "j-inline-async", "status": "IN_PROGRESS"}

            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"content": "metrics:\n" + "  - name: m\n" * 50}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["jobId"] == "j-inline-async"
        assert body["status"] == "IN_PROGRESS"
        # Verify content was written to S3 under the correct prefix
        put_call = mock_s3_client.put_object.call_args
        assert put_call[1]["Bucket"] == "test-bucket"
        assert put_call[1]["Key"].startswith("ns-1/imports/")
        mock_sqs_client.send_message.assert_called_once()

    @patch("coa_metrics.api.import_osi._get_s3_client")
    @patch("coa_metrics.api.import_osi._get_bucket", return_value="test-bucket")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_inline_s3_write_failure_returns_500(self, mock_parse, mock_bucket, mock_s3):
        """If S3 write fails for inline async, return 500 instead of processing sync."""
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock() for _ in range(50)]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        mock_s3.return_value.put_object.side_effect = Exception("S3 throttled")

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"content": "metrics:\n" + "  - name: m\n" * 50}),
            "headers": {},
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "stage content" in body["message"].lower()

    @patch("coa_metrics.api.import_osi._get_neptune")
    @patch("coa_metrics.api.import_osi._get_opensearch")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_empty_import_processes_sync(self, mock_parse, mock_os, mock_neptune):
        """An import with NO metrics has nothing to embed, so it stays synchronous (200).

        SYNC_THRESHOLD == 0, so this (count == 0) is the only remaining sync case.
        """
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = []
        mock_doc.datasets = []
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"content": "metrics: []"}),
            "headers": {},
        }
        result = handler(event, None)

        assert result["statusCode"] == 200

    @patch("coa_metrics.api.import_osi._get_sqs")
    @patch("coa_metrics.api.import_osi._get_s3_client")
    @patch("coa_metrics.api.import_osi._get_bucket", return_value="test-bucket")
    @patch("coa_metrics.api.import_osi.parse_osi_yaml")
    @patch("coa_metrics.api.import_osi._IMPORT_QUEUE_URL", "https://sqs.example.com/queue")
    def test_single_metric_import_is_async(self, mock_parse, mock_bucket, mock_s3, mock_sqs):
        """Even a ONE-metric import goes async (202).

        Regression guard for the API Gateway 504s: the inline path's dominant cost
        is the first embed on a cold container (~25s observed) against a ~29s
        integration timeout, so it is per-container, not per-metric. Any non-zero
        SYNC_THRESHOLD — including 1 — leaves the timeout reachable.
        """
        from coa_metrics.api.import_osi import handler

        mock_doc = MagicMock()
        mock_doc.metrics = [MagicMock()]
        mock_parse_result = MagicMock()
        mock_parse_result.success = True
        mock_parse_result.document = mock_doc
        mock_parse.return_value = mock_parse_result
        mock_s3.return_value = MagicMock()

        with patch("coa_metrics.api.import_job_store.create_job") as mock_create:
            mock_create.return_value = {"jobId": "j-single", "status": "IN_PROGRESS"}
            event = {
                "pathParameters": {"namespaceId": "ns-1"},
                "body": json.dumps({"content": "metrics:\n  - name: m\n"}),
                "headers": {},
            }
            result = handler(event, None)

        assert result["statusCode"] == 202
        assert json.loads(result["body"])["jobId"] == "j-single"


# ── import worker tests ─────────────────────────────────────────────────


class TestImportWorker:
    """Tests for the SQS import worker _process_chunk."""

    def _msg(self, **overrides):
        msg = {
            "namespaceId": "ns-1",
            "jobId": "j-1",
            "s3Key": "ns-1/imports/abc/file.yaml",
            "offset": 0,
            "chunkSize": 50,
        }
        msg.update(overrides)
        return msg

    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.get_job")
    @patch("coa_metrics.api.import_worker.complete_job")
    def test_rejects_s3_key_outside_namespace(self, mock_complete, mock_get_job, mock_s3):
        from coa_metrics.api.import_worker import _process_chunk

        _process_chunk(self._msg(s3Key="other-ns/imports/abc/poison.yaml"))

        mock_complete.assert_called_once_with("ns-1", "j-1", status="FAILED")
        mock_get_job.assert_not_called()
        mock_s3.assert_not_called()

    @patch("coa_metrics.api.import_worker._get_lookup")
    @patch("coa_metrics.api.import_worker.resolve_datasets")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_dataset_resolution_failure_marks_failed(
        self, mock_get_job, mock_complete, mock_s3, mock_parse, mock_resolve, mock_lookup
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = {"status": "IN_PROGRESS"}
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        doc = MagicMock()
        doc.datasets = [MagicMock()]
        doc.metrics = [MagicMock()]
        mock_parse.return_value = MagicMock(success=True, document=doc)
        mock_resolve.return_value = MagicMock(success=False, errors=["bad"])

        _process_chunk(self._msg())

        mock_complete.assert_called_once_with("ns-1", "j-1", status="FAILED")

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.update_job_progress")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_processes_chunk_with_delta_counts(
        self, mock_get_job, mock_complete, mock_s3, mock_parse, mock_update, mock_neptune, mock_to_def
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = {"status": "IN_PROGRESS"}
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        doc = MagicMock()
        doc.datasets = []
        doc.metrics = [MagicMock(), MagicMock()]
        mock_parse.return_value = MagicMock(success=True, document=doc)
        mock_neptune.return_value.get_metric.return_value = None
        mock_to_def.return_value = MagicMock()

        _process_chunk(self._msg(chunkSize=50))

        mock_update.assert_called_once()
        kwargs = mock_update.call_args[1]
        assert kwargs["metrics_processed"] == 2
        assert kwargs["metrics_created"] == 2
        assert kwargs["metrics_updated"] == 0
        mock_complete.assert_called_once_with("ns-1", "j-1", status="COMPLETED")

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.update_job_progress")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_unapproved_source_is_recorded_and_not_persisted(
        self, mock_get_job, mock_complete, mock_s3, mock_parse, mock_update, mock_neptune, mock_to_def
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = {"status": "IN_PROGRESS"}
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        doc = MagicMock(datasets=[], metrics=[MagicMock(name="blocked_metric")])
        mock_parse.return_value = MagicMock(success=True, document=doc)
        mock_to_def.return_value = MagicMock(
            name="blocked_metric",
            data_source_id="ds-pending",
            source_table="orders",
            ontology_concepts=[],
        )

        with (
            patch(
                "coa_metrics.api.import_worker.check_source_approved",
                return_value="Data source 'ds-pending' has status 'PENDING'",
            ) as mock_approved,
            patch("coa_metrics.api.import_worker.check_source_table_exists") as mock_table,
        ):
            _process_chunk(self._msg())

        mock_approved.assert_called_once_with("ns-1", "ds-pending")
        mock_table.assert_not_called()
        mock_neptune.return_value.get_metric.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        kwargs = mock_update.call_args.kwargs
        assert kwargs["metrics_processed"] == 1
        assert kwargs["metrics_created"] == 0
        assert "PENDING" in kwargs["errors"][0]
        mock_complete.assert_called_once_with("ns-1", "j-1", status="COMPLETED")

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.update_job_progress")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_source_table_is_checked_before_persistence(
        self, mock_get_job, mock_complete, mock_s3, mock_parse, mock_update, mock_neptune, mock_to_def
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = {"status": "IN_PROGRESS"}
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        doc = MagicMock(datasets=[], metrics=[MagicMock(name="bad_table_metric")])
        mock_parse.return_value = MagicMock(success=True, document=doc)
        mock_to_def.return_value = MagicMock(
            name="bad_table_metric",
            data_source_id="ds-approved",
            source_table="missing_table",
            ontology_concepts=[],
        )

        with (
            patch("coa_metrics.api.import_worker.check_source_approved", return_value=None),
            patch(
                "coa_metrics.api.import_worker.check_source_table_exists",
                return_value="Source table 'missing_table' not found",
            ) as mock_table,
        ):
            _process_chunk(self._msg())

        mock_table.assert_called_once_with("ns-1", "ds-approved", "missing_table")
        mock_neptune.return_value.create_metric.assert_not_called()
        kwargs = mock_update.call_args.kwargs
        assert kwargs["metrics_created"] == 0
        assert "missing_table" in kwargs["errors"][0]
        mock_complete.assert_called_once_with("ns-1", "j-1", status="COMPLETED")

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.update_job_progress")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_duplicate_source_references_are_validated_once_per_chunk(
        self, mock_get_job, mock_complete, mock_s3, mock_parse, mock_update, mock_neptune, mock_to_def
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = {"status": "IN_PROGRESS"}
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        doc = MagicMock(datasets=[], metrics=[MagicMock(name="one"), MagicMock(name="two")])
        mock_parse.return_value = MagicMock(success=True, document=doc)
        mock_neptune.return_value.get_metric.return_value = None
        mock_to_def.side_effect = [
            MagicMock(
                name="one",
                data_source_id="ds-approved",
                source_table="Orders",
                ontology_concepts=[],
            ),
            MagicMock(
                name="two",
                data_source_id="ds-approved",
                source_table="orders",
                ontology_concepts=[],
            ),
        ]

        with (
            patch("coa_metrics.api.import_worker.check_source_approved", return_value=None) as mock_approved,
            patch("coa_metrics.api.import_worker.check_source_table_exists", return_value=None) as mock_table,
        ):
            _process_chunk(self._msg())

        mock_approved.assert_called_once_with("ns-1", "ds-approved")
        mock_table.assert_called_once_with("ns-1", "ds-approved", "Orders")
        assert mock_neptune.return_value.create_metric.call_count == 2
        mock_complete.assert_called_once_with("ns-1", "j-1", status="COMPLETED")

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.update_job_progress")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_source_validation_outage_fails_job_and_records_progress(
        self, mock_get_job, mock_complete, mock_s3, mock_parse, mock_update, mock_neptune, mock_to_def
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = {"status": "IN_PROGRESS"}
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        doc = MagicMock(datasets=[], metrics=[MagicMock(name="metric")])
        mock_parse.return_value = MagicMock(success=True, document=doc)
        mock_to_def.return_value = MagicMock(
            name="metric",
            data_source_id="ds-approved",
            source_table="orders",
            ontology_concepts=[],
        )

        with patch(
            "coa_metrics.api.import_worker.check_source_approved",
            side_effect=SourceValidationUnavailableError("sources table unavailable"),
        ):
            _process_chunk(self._msg())

        mock_neptune.return_value.get_metric.assert_not_called()
        mock_neptune.return_value.create_metric.assert_not_called()
        kwargs = mock_update.call_args.kwargs
        assert kwargs["metrics_processed"] == 1
        assert kwargs["metrics_created"] == 0
        assert "sources table unavailable" in kwargs["errors"][0]
        mock_complete.assert_called_once_with("ns-1", "j-1", status="FAILED")

    @patch("coa_metrics.api.import_worker._osi_metric_to_definition")
    @patch("coa_metrics.api.import_worker._get_sqs")
    @patch("coa_metrics.api.import_worker._get_neptune")
    @patch("coa_metrics.api.import_worker.update_job_progress")
    @patch("coa_metrics.api.import_worker.parse_osi_yaml")
    @patch("coa_metrics.api.import_worker._get_s3")
    @patch("coa_metrics.api.import_worker.complete_job")
    @patch("coa_metrics.api.import_worker.get_job")
    def test_continuation_send_failure_marks_failed(
        self, mock_get_job, mock_complete, mock_s3, mock_parse, mock_update, mock_neptune, mock_sqs, mock_to_def
    ):
        from coa_metrics.api.import_worker import _process_chunk

        mock_get_job.return_value = {"status": "IN_PROGRESS"}
        body = MagicMock()
        body.read.return_value = b"yaml"
        mock_s3.return_value.get_object.return_value = {"Body": body}
        doc = MagicMock()
        doc.datasets = []
        doc.metrics = [MagicMock(), MagicMock(), MagicMock()]
        mock_parse.return_value = MagicMock(success=True, document=doc)
        mock_neptune.return_value.get_metric.return_value = None
        mock_to_def.return_value = MagicMock()
        mock_sqs.return_value.send_message.side_effect = Exception("sqs down")

        _process_chunk(self._msg(chunkSize=2))

        mock_complete.assert_called_once_with("ns-1", "j-1", status="FAILED")
