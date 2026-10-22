# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sources API error text neither reflects caller input nor carries raw exception text."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("SOURCES_TABLE", "test-sources")
os.environ.setdefault("SOURCE_SCAN_JOBS_TABLE", "test-scan-jobs")
os.environ.setdefault("NAMESPACES_TABLE", "test-namespaces")
os.environ.setdefault("SCAN_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/scan-queue")
os.environ.setdefault("SMUS_DOMAIN_ID", "test-domain-id")

import coa_sources.api.sources_handler  # noqa: E402, F401, I001
import coa_sources.api.database_routes as _dr  # noqa: E402
import coa_sources.api.document_routes as _docr  # noqa: E402
from coa_sources import utils  # noqa: E402
from coa_sources.database import glue_ownership  # noqa: E402

pytestmark = pytest.mark.unit

_NS = "550e8400-e29b-41d4-a716-446655440000"
_PROBE = "<script>alert(1)</script>"


def _body(resp: dict) -> dict:
    return json.loads(resp["body"])


def test_invalid_decision_lists_allowed_values_without_echo() -> None:
    _, err = _dr._parse_decision({"decision": _PROBE})
    assert err is not None and err["statusCode"] == 400
    message = _body(err)["error"]
    assert _PROBE not in message and "APPROVED" in message


def test_invalid_review_status_filter_not_echoed() -> None:
    event = {"queryStringParameters": {"reviewStatus": _PROBE}}
    with patch(f"{_dr.__name__}._SMUS_DOMAIN_ID", "dzd"):
        resp = _dr._handle_list_tables(event, _NS, "src-1")
    assert resp["statusCode"] == 400
    assert _PROBE not in _body(resp)["error"]


def test_unsupported_upload_content_type_not_echoed() -> None:
    event = {"body": json.dumps({"files": [{"filename": "a.pdf", "contentType": _PROBE}]})}
    with patch(f"{_docr.__name__}._BUCKET_NAME", "bucket"):
        resp = _docr._handle_upload_urls(event, _NS)
    assert resp["statusCode"] == 400
    message = _body(resp)["error"]
    assert _PROBE not in message and "supported types" in message


def test_malformed_bucket_arn_not_echoed() -> None:
    resp = _docr._validate_bucket_namespace_authorization(f"not-an-arn{_PROBE}", _NS)
    assert resp is not None and resp["statusCode"] == 400
    assert _PROBE not in _body(resp)["error"]


def test_cron_minute_error_not_echoed() -> None:
    message = _dr._schedule_expression_error(f"cron({_PROBE} * * * ? *)")
    assert message is not None and _PROBE not in message


def test_scheduler_validation_message_not_forwarded() -> None:
    dao = MagicMock()
    dao.get.return_value = {"sourceType": "DATABASE"}
    scheduler_error = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "arn:aws:scheduler:us-east-1:123456789012:x bad"}},
        "CreateSchedule",
    )
    event = {"body": json.dumps({"enabled": True, "scheduleExpression": "rate(1 day)"})}
    with (
        patch(f"{_dr.__name__}._get_dao", return_value=dao),
        patch(f"{_dr.__name__}._scheduling_configured", return_value=True),
        patch(f"{_dr.__name__}._upsert_rescan_schedule", side_effect=scheduler_error),
    ):
        resp = _dr._handle_put_rescan_schedule(event, _NS, "src-1")
    assert resp["statusCode"] == 400
    message = _body(resp)["error"]
    assert "123456789012" not in message and message.startswith("Invalid schedule")


def test_extraction_config_errors_do_not_echo_values() -> None:
    with pytest.raises(ValueError) as dup:
        utils.merge_extraction_config({"preferred_topics": [_PROBE, _PROBE.upper()]})
    assert _PROBE not in str(dup.value)
    with pytest.raises(ValueError) as mode:
        utils.merge_extraction_config({"extraction_mode": _PROBE})
    assert _PROBE not in str(mode.value)


def test_document_error_message_redacted_on_read() -> None:
    lambda_envelope = json.dumps(
        {
            "errorType": "ClientError",
            "errorMessage": "User arn:aws:sts::123456789012:assumed-role/coa-pre/x denied on bucket",
        }
    )
    detail = sys.modules["coa_sources.api.sources_handler"]._build_document_detail(
        {"errorMessage": lambda_envelope, "docSourceType": "s3"}
    )
    assert detail is not None
    assert "123456789012" not in detail["errorMessage"]
    assert "<arn>" in detail["errorMessage"]


def test_scan_job_error_message_redacted_on_read() -> None:
    entry = _dr._scan_job_to_entry(
        {"SK": "2026-01-01T00:00:00Z", "errorMessage": 'Traceback (most recent call last):\n  File "/var/task/x.py"'}
    )
    assert "Traceback" not in entry["errorMessage"]


@pytest.mark.real_glue_ownership
def test_glue_tag_lookup_failure_detail_is_error_code_only() -> None:
    glue = MagicMock()
    glue.get_tags.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "role arn:aws:iam::123456789012:role/r denied"}},
        "GetTags",
    )
    lookup = glue_ownership._tagged_namespaces("123456789012", "sales", "us-east-1", glue_client=glue)
    assert lookup.unreadable is True
    assert lookup.detail == "GetTags failed: AccessDeniedException"


def test_flagged_client_safe_scan_job_message_is_not_redacted_on_read() -> None:
    hint = glue_ownership._tag_hint("123456789012", "sales_db", _NS, "us-east-1")
    row = {"SK": "2026-01-01T00:00:00Z", "errorMessage": hint}
    assert _dr._scan_job_to_entry({**row, "errorMessageClientSafe": True})["errorMessage"] == hint
    # Without the flag (legacy rows) the same text is still redacted and bounded.
    redacted = _dr._scan_job_to_entry(row)["errorMessage"]
    assert "123456789012" not in redacted and len(redacted) < len(hint)
