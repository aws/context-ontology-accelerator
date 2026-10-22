# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""CreateSource is retry-safe with a caller-supplied ``clientToken``.

Without the token a retried database create registers a second source, a
second scan job and a second scan message. With it, the retry replays the
first response and nothing is written twice.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from coa_common import idempotency
from coa_common.testing import mock_idempotency_table

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import coa_sources.api.sources_handler  # noqa: E402, F401, I001
from tests.unit.conftest import dao_double  # noqa: E402

_DR = "coa_sources.api.database_routes"
_SH = "coa_sources.api.sources_handler"
_NAMESPACE_ID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def idem_table(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("IDEMPOTENCY_TABLE", "test-idempotency")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_idempotency_table("test-idempotency"):
        yield


def _handler() -> object:
    """The live sources_handler module (other test modules re-import it)."""
    return sys.modules[_SH].handler


def _event(body: dict) -> dict:
    return {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/sources",
        "pathParameters": {"namespaceId": _NAMESPACE_ID},
        "requestContext": {"authorizer": {"email": "steward@example.com"}},
        "body": json.dumps(body),
    }


def _glue_body(token: str | None, name: str = "my-glue-db") -> dict:
    body: dict = {
        "sourceType": "DATABASE",
        "databaseSource": {
            "name": name,
            "glueConfiguration": {"databaseName": "mydb", "region": "us-east-1", "catalogId": "123456789012"},
        },
    }
    if token is not None:
        body["clientToken"] = token
    return body


@pytest.mark.unit
def test_create_source_database_retry_with_token_creates_one_source_and_one_scan(idem_table: None) -> None:
    mock_dao = dao_double()
    mock_scan_dao = dao_double()
    ns_dao = dao_double()
    ns_dao.get.return_value = {"PK": f"NS#{_NAMESPACE_ID}", "SK": "METADATA"}
    mock_sqs = MagicMock()

    with (
        patch(f"{_SH}._get_ns_dao", return_value=ns_dao),
        patch(f"{_DR}._get_dao", return_value=mock_dao),
        patch(f"{_DR}._get_scan_dao", return_value=mock_scan_dao),
        patch(f"{_DR}._get_sqs", return_value=mock_sqs),
        patch(f"{_DR}.adjust_namespace_source_count") as counter,
        patch(f"{_DR}._validate_credential_secret_binding", return_value=None),
        patch(f"{_DR}._BUCKET_NAME", ""),
    ):
        first = _handler()(_event(_glue_body("src-token-1")), None)
        second = _handler()(_event(_glue_body("src-token-1")), None)
        reused = _handler()(_event(_glue_body("src-token-1", name="other-db")), None)

    assert first["statusCode"] == 202, first["body"]
    assert (second["statusCode"], second["body"]) == (first["statusCode"], first["body"])
    assert reused["statusCode"] == 409
    assert "error" in json.loads(reused["body"])  # sources error envelope

    source_rows = [c.args[0] for c in mock_dao.put.call_args_list if str(c.args[0].get("SK", "")).startswith("SRC#")]
    assert len(source_rows) == 1
    assert "clientToken" not in json.dumps(source_rows[0], default=str)
    mock_sqs.send_message.assert_called_once()
    assert mock_scan_dao.put.call_count == 1
    assert counter.call_count == 1


@pytest.mark.unit
def test_create_source_without_token_skips_idempotency_store() -> None:
    with (
        patch.object(idempotency, "_get_store") as get_store,
        patch(f"{_SH}._handle_create", return_value={"statusCode": 202, "body": "{}"}) as create,
    ):
        _handler()(_event(_glue_body(None)), None)
    get_store.assert_not_called()
    create.assert_called_once()


def _rescan_event(body: dict | None) -> dict:
    return {
        "httpMethod": "POST",
        "resource": "/namespaces/{namespaceId}/sources/{sourceId}/rescan",
        "pathParameters": {"namespaceId": _NAMESPACE_ID, "sourceId": "src-1"},
        "requestContext": {"authorizer": {"email": "steward@example.com"}},
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.mark.unit
def test_rescan_retry_with_token_starts_one_scan_and_replays(idem_table: None) -> None:
    accepted = {"statusCode": 202, "headers": {}, "body": json.dumps({"sourceId": "src-1", "status": "IN_PROGRESS"})}
    with patch(f"{_SH}._handle_rescan", return_value=accepted) as rescan:
        first = _handler()(_rescan_event({"clientToken": "rescan-1"}), None)
        second = _handler()(_rescan_event({"clientToken": "rescan-1"}), None)
        changed = _handler()(_rescan_event({"clientToken": "rescan-1", "confirmDiscardOpenReview": True}), None)

    assert first["statusCode"] == 202
    assert (second["statusCode"], second["body"]) == (202, first["body"])
    assert second["headers"]["X-Idempotent-Replay"] == "true"
    assert changed["statusCode"] == 409
    rescan.assert_called_once()
    # The token never reaches the handler's body validation.
    assert json.loads(rescan.call_args.args[0]["body"]) == {}


@pytest.mark.unit
def test_rescan_without_token_or_body_skips_idempotency_store() -> None:
    accepted = {"statusCode": 202, "headers": {}, "body": "{}"}
    with (
        patch.object(idempotency, "_get_store") as get_store,
        patch(f"{_SH}._handle_rescan", return_value=accepted) as rescan,
    ):
        _handler()(_rescan_event(None), None)
        _handler()(_rescan_event({"confirmDiscardOpenReview": True}), None)
    get_store.assert_not_called()
    assert rescan.call_count == 2
