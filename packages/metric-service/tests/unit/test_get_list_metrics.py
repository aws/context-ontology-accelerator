# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Get and List Metric handlers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_metrics.api.get_metric import handler as get_handler
from coa_metrics.api.list_metrics import handler as list_handler
from coa_metrics.neptune_client import MetricDefinition, MetricDialect

pytestmark = pytest.mark.unit


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_get_event(namespace: str = "test-ns", name: str = "revenue") -> dict:
    return {
        "httpMethod": "GET",
        "pathParameters": {"namespaceId": namespace, "name": name},
        "requestContext": {"authorizer": {"claims": {"email": "user@example.com"}}},
    }


def _make_list_event(
    namespace: str = "test-ns",
    query_params: dict | None = None,
) -> dict:
    return {
        "httpMethod": "GET",
        "pathParameters": {"namespaceId": namespace},
        "queryStringParameters": query_params,
        "requestContext": {"authorizer": {"claims": {"email": "user@example.com"}}},
    }


def _sample_metric(name: str = "revenue") -> MetricDefinition:
    return MetricDefinition(
        name=name,
        description="Test metric",
        expression_dialects=[MetricDialect(dialect="trino", expression="SUM(x)")],
        data_source_id="ds-1",
        source_table="orders",
        defined_by="steward@example.com",
        effective_from="2026-05-01",
    )


# ── Get Metric tests ───────────────────────────────────────────────────


class TestGetMetric:
    @patch("coa_metrics.api.get_metric._get_neptune")
    def test_get_found_returns_200(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.get_metric.return_value = _sample_metric()

        resp = get_handler(_make_get_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["metric"]["name"] == "revenue"
        assert body["metric"]["dataSourceId"] == "ds-1"

    @patch("coa_metrics.api.get_metric._get_neptune")
    def test_get_not_found_returns_404(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.get_metric.return_value = None

        resp = get_handler(_make_get_event(name="nonexistent"), None)

        assert resp["statusCode"] == 404
        assert "not found" in json.loads(resp["body"])["message"]

    @patch("coa_metrics.api.get_metric._get_neptune")
    def test_get_missing_name_returns_400(self, mock_neptune: MagicMock) -> None:
        event = _make_get_event(name="")

        resp = get_handler(event, None)

        assert resp["statusCode"] == 400

    @patch("coa_metrics.api.get_metric._get_neptune")
    def test_get_scoped_to_path_namespace(self, mock_neptune: MagicMock) -> None:
        """The read must be scoped to the path namespace, not a hardcoded one."""
        mock_neptune.return_value.get_metric.return_value = _sample_metric()

        get_handler(_make_get_event(namespace="ns-alpha"), None)

        assert mock_neptune.return_value.get_metric.call_args[0][0] == "ns-alpha"


# ── List Metrics tests ──────────────────────────────────────────────────


class TestListMetrics:
    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_returns_metrics(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.list_metrics.return_value = [
            _sample_metric("revenue"),
            _sample_metric("cost"),
        ]

        resp = list_handler(_make_list_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["metrics"]) == 2
        assert body["metrics"][0]["name"] == "revenue"
        assert body["metrics"][1]["name"] == "cost"

    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_empty_returns_empty_array(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.list_metrics.return_value = []

        resp = list_handler(_make_list_event(), None)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["metrics"] == []
        assert "nextToken" not in body

    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_passes_filters(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.list_metrics.return_value = []

        list_handler(
            _make_list_event(query_params={"dataSourceId": "ds-x", "sourceTable": "orders"}),
            None,
        )

        call_kwargs = mock_neptune.return_value.list_metrics.call_args[1]
        assert call_kwargs["data_source_id"] == "ds-x"
        assert call_kwargs["source_table"] == "orders"

    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_scoped_to_path_namespace(self, mock_neptune: MagicMock) -> None:
        """The listing must be scoped to the path namespace, not a hardcoded one."""
        mock_neptune.return_value.list_metrics.return_value = []

        list_handler(_make_list_event(namespace="ns-alpha"), None)

        assert mock_neptune.return_value.list_metrics.call_args[1]["namespace"] == "ns-alpha"

    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_pagination_with_next_token(self, mock_neptune: MagicMock) -> None:
        # Return limit+1 items to trigger pagination
        mock_neptune.return_value.list_metrics.return_value = [
            _sample_metric(f"metric_{i}")
            for i in range(4)  # 3+1 extra
        ]

        resp = list_handler(
            _make_list_event(query_params={"maxResults": "3"}),
            None,
        )

        body = json.loads(resp["body"])
        assert len(body["metrics"]) == 3
        assert body["nextToken"] == "3"

    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_no_next_token_when_last_page(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.list_metrics.return_value = [
            _sample_metric("only_one"),
        ]

        resp = list_handler(
            _make_list_event(query_params={"maxResults": "10"}),
            None,
        )

        body = json.loads(resp["body"])
        assert len(body["metrics"]) == 1
        assert "nextToken" not in body

    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_uses_next_token_as_offset(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.list_metrics.return_value = []

        list_handler(
            _make_list_event(query_params={"nextToken": "50", "maxResults": "25"}),
            None,
        )

        call_kwargs = mock_neptune.return_value.list_metrics.call_args[1]
        assert call_kwargs["offset"] == 50
        assert call_kwargs["limit"] == 26  # 25 + 1 for pagination check

    @pytest.mark.parametrize(
        "token",
        [
            "2147483648",  # one past the largest accepted offset
            "9" * 5000,  # past int()'s digit limit; was a 500
            "-1",
            "eyJvZmZzZXQiOjI1fQ==",
            "１２",  # non-ASCII digits
        ],
    )
    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_out_of_range_or_malformed_token_returns_400(self, mock_neptune: MagicMock, token: str) -> None:
        resp = list_handler(_make_list_event(query_params={"nextToken": token}), None)

        assert resp["statusCode"] == 400
        assert json.loads(resp["body"]) == {"message": "Invalid nextToken"}
        mock_neptune.return_value.list_metrics.assert_not_called()

    @patch("coa_metrics.api.list_metrics._get_neptune")
    def test_list_largest_offset_token_is_accepted(self, mock_neptune: MagicMock) -> None:
        mock_neptune.return_value.list_metrics.return_value = []

        resp = list_handler(_make_list_event(query_params={"nextToken": "2147483647"}), None)

        assert resp["statusCode"] == 200
        assert mock_neptune.return_value.list_metrics.call_args[1]["offset"] == 2147483647
