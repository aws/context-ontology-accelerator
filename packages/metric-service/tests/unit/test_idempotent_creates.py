# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CreateMetric and ImportOsi are retry-safe with a caller-supplied ``clientToken``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from coa_common.testing import mock_idempotency_table
from coa_metrics.api import import_osi
from coa_metrics.api.metric_api_handler import handler as metric_api_handler
from coa_metrics.source_status import PERMISSIVE_ENV

pytestmark = pytest.mark.unit


@pytest.fixture
def idem_table(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(PERMISSIVE_ENV, "true")
    monkeypatch.setenv("IDEMPOTENCY_TABLE", "test-idempotency")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with mock_idempotency_table("test-idempotency"):
        yield


def _event(resource: str, body: dict) -> dict:
    return {
        "httpMethod": "POST",
        "resource": resource,
        "pathParameters": {"namespaceId": "test-ns"},
        "requestContext": {"authorizer": {"claims": {"email": "test@example.com"}}},
        "body": json.dumps(body),
    }


def _metric_body(token: str) -> dict:
    return {
        "name": "monthly_revenue",
        "description": "Total revenue",
        "expression": {"dialects": [{"dialect": "TRINO", "expression": "SELECT SUM(total) AS r FROM orders"}]},
        "dataSourceId": "ds-abc123",
        "sourceTable": "orders",
        "clientToken": token,
    }


@patch("coa_metrics.api.create_metric._validate_soft", return_value=[])
@patch("coa_metrics.api.create_metric._get_eventbridge")
@patch("coa_metrics.api.create_metric._get_opensearch")
@patch("coa_metrics.api.create_metric._get_neptune")
def test_create_metric_retry_with_token_writes_once_and_replays(
    mock_neptune: MagicMock,
    mock_opensearch: MagicMock,
    mock_eventbridge: MagicMock,
    _soft: MagicMock,
    idem_table: None,
) -> None:
    neptune = mock_neptune.return_value
    neptune.get_metric.return_value = None

    first = metric_api_handler(_event("/namespaces/{namespaceId}/metrics", _metric_body("m-1")), None)
    # The non-atomic existence check would now see the metric and 409 a plain retry.
    neptune.get_metric.return_value = {"name": "monthly_revenue"}
    second = metric_api_handler(_event("/namespaces/{namespaceId}/metrics", _metric_body("m-1")), None)

    assert first["statusCode"] == 201, first["body"]
    assert (second["statusCode"], second["body"]) == (201, first["body"])
    neptune.create_metric.assert_called_once()
    mock_eventbridge.return_value.put_events.assert_called_once()
    assert "clientToken" not in json.loads(first["body"])["metric"]


def test_import_osi_retry_with_token_starts_one_job(idem_table: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retried async import replays the 202 and enqueues exactly one job."""
    staged: list[str] = []
    monkeypatch.setattr(import_osi, "_IMPORT_QUEUE_URL", "https://sqs.example/queue")
    sqs = MagicMock()
    content = (
        'osi_spec_version: "1.0"\n'
        "metrics:\n"
        "  - name: revenue\n"
        "    description: Total revenue\n"
        "    expression:\n"
        "      dialects:\n"
        "        - dialect: ANSI_SQL\n"
        "          expression: SELECT SUM(total) AS r FROM orders\n"
    )

    def stage(text: str, namespace: str) -> import_osi.StagedImportSource:
        staged.append(text)
        return import_osi.StagedImportSource(key=f"{namespace}/imports/jobs/x.yaml", version_id="v1")

    with (
        patch.object(import_osi, "_write_to_s3", side_effect=stage),
        patch.object(import_osi, "_get_sqs", return_value=sqs),
        patch("coa_metrics.api.import_job_store.create_job", return_value={"jobId": "j-1"}) as create_job,
    ):
        body = {"content": content, "clientToken": "osi-1"}
        first = metric_api_handler(_event("/namespaces/{namespaceId}/metrics/import-osi", body), None)
        second = metric_api_handler(_event("/namespaces/{namespaceId}/metrics/import-osi", body), None)
        changed = metric_api_handler(
            _event("/namespaces/{namespaceId}/metrics/import-osi", {**body, "content": content + "# v2\n"}), None
        )

    assert first["statusCode"] == 202, first["body"]
    assert (second["statusCode"], second["body"]) == (202, first["body"])
    assert changed["statusCode"] == 409
    create_job.assert_called_once()
    sqs.send_message.assert_called_once()
    # The token never reaches the import itself: the staged bytes are the content alone.
    assert staged == [content]
