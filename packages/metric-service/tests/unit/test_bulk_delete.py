# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for bulk delete metrics handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestBulkDeleteByNames:
    """Tests for delete by explicit name list."""

    @patch("coa_metrics.api.bulk_delete_metrics._get_opensearch")
    @patch("coa_metrics.api.bulk_delete_metrics._get_neptune")
    def test_deletes_existing_metrics(self, mock_neptune, mock_opensearch):
        from coa_metrics.api.bulk_delete_metrics import handler

        neptune = MagicMock()
        neptune.delete_metric.return_value = True
        mock_neptune.return_value = neptune

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"names": ["metric_a", "metric_b", "metric_c"]}),
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == 3
        assert "notFound" not in body

    @patch("coa_metrics.api.bulk_delete_metrics._get_opensearch")
    @patch("coa_metrics.api.bulk_delete_metrics._get_neptune")
    def test_reports_not_found(self, mock_neptune, mock_opensearch):
        from coa_metrics.api.bulk_delete_metrics import handler

        neptune = MagicMock()
        neptune.delete_metric.side_effect = [True, False, True]
        mock_neptune.return_value = neptune

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"names": ["exists_1", "missing", "exists_2"]}),
        }
        result = handler(event, None)

        body = json.loads(result["body"])
        assert body["deleted"] == 2
        assert body["notFound"] == ["missing"]


class TestBulkDeleteByFilter:
    """Tests for delete by filter criteria."""

    @patch("coa_metrics.api.bulk_delete_metrics._get_opensearch")
    @patch("coa_metrics.api.bulk_delete_metrics._get_neptune")
    def test_deletes_by_name_prefix(self, mock_neptune, mock_opensearch):
        from coa_metrics.api.bulk_delete_metrics import handler

        neptune = MagicMock()
        metric_a = MagicMock()
        metric_a.name = "bulk_001"
        metric_b = MagicMock()
        metric_b.name = "bulk_002"
        metric_c = MagicMock()
        metric_c.name = "other_001"
        neptune.list_metrics.return_value = [metric_a, metric_b, metric_c]
        neptune.delete_metric.return_value = True
        mock_neptune.return_value = neptune

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"filter": {"namePrefix": "bulk_"}}),
        }
        result = handler(event, None)

        body = json.loads(result["body"])
        assert body["deleted"] == 2  # only bulk_ prefix matches

    def test_rejects_empty_filter(self):
        from coa_metrics.api.bulk_delete_metrics import handler

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"filter": {}}),
        }
        result = handler(event, None)

        assert result["statusCode"] == 400


class TestBulkDeleteAll:
    """Tests for deleteAll: true (namespace-scoped deletion)."""

    @patch("coa_metrics.api.bulk_delete_metrics._get_opensearch")
    @patch("coa_metrics.api.bulk_delete_metrics._get_neptune")
    def test_deletes_all_metrics_for_namespace(self, mock_neptune, mock_opensearch):
        from coa_metrics.api.bulk_delete_metrics import handler

        neptune = MagicMock()
        neptune.delete_all_metrics_for_namespace.return_value = 5
        mock_neptune.return_value = neptune

        event = {
            "pathParameters": {"namespaceId": "test-ns"},
            "body": json.dumps({"deleteAll": True}),
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == 5
        neptune.delete_all_metrics_for_namespace.assert_called_once_with("test-ns")

    @patch("coa_metrics.api.bulk_delete_metrics._get_opensearch")
    @patch("coa_metrics.api.bulk_delete_metrics._get_neptune")
    def test_returns_zero_when_no_metrics(self, mock_neptune, mock_opensearch):
        from coa_metrics.api.bulk_delete_metrics import handler

        neptune = MagicMock()
        neptune.delete_all_metrics_for_namespace.return_value = 0
        mock_neptune.return_value = neptune

        event = {
            "pathParameters": {"namespaceId": "empty-ns"},
            "body": json.dumps({"deleteAll": True}),
        }
        result = handler(event, None)

        body = json.loads(result["body"])
        assert body["deleted"] == 0


class TestBulkDeleteValidation:
    """Tests for input validation."""

    def test_rejects_both_names_and_filter(self):
        from coa_metrics.api.bulk_delete_metrics import handler

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"names": ["a"], "filter": {"namePrefix": "b"}}),
        }
        result = handler(event, None)

        assert result["statusCode"] == 400

    def test_rejects_names_and_delete_all(self):
        from coa_metrics.api.bulk_delete_metrics import handler

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"names": ["a"], "deleteAll": True}),
        }
        result = handler(event, None)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "mutually exclusive" in body["message"]

    def test_rejects_filter_and_delete_all(self):
        from coa_metrics.api.bulk_delete_metrics import handler

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({"filter": {"namePrefix": "x"}, "deleteAll": True}),
        }
        result = handler(event, None)

        assert result["statusCode"] == 400

    def test_rejects_empty_body(self):
        from coa_metrics.api.bulk_delete_metrics import handler

        event = {
            "pathParameters": {"namespaceId": "ns-1"},
            "body": json.dumps({}),
        }
        result = handler(event, None)

        assert result["statusCode"] == 400
