# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Security tests for TraceStep serialization — CWE-209 detail truncation.

Validates that trace steps with exception details are properly sanitized
when serialized via model_dump(). The @field_serializer on TraceStep.detail
truncates long strings to prevent infrastructure secrets from leaking to
clients via SSE streaming events.
"""

from __future__ import annotations

import pytest
from coa_serve.models import TraceStep

pytestmark = pytest.mark.unit


class TestTraceStepDetailTruncation:
    """CWE-209: TraceStep detail must be truncated on serialization to prevent leaks."""

    def test_long_error_message_truncated_to_500_chars(self):
        """TraceStep with a long exception message (e.g. boto3 ClientError with ARN)
        must be truncated to 500 chars when serialized via model_dump()."""
        long_error_message = (
            "User: arn:aws:iam::123456789012:assumed-role/OrchestratorRole/session "
            "is not authorized to perform: bedrock:InvokeModel on resource: "
            "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0. "
            "Additional context: " + ("x" * 600)
        )

        step = TraceStep(
            step="query.embed",
            status="error",
            durationMs=120,
            detail={"error": "ClientError", "message": long_error_message},
            toolUsed="bedrock",
        )

        assert len(step.detail["message"]) > 500

        serialized = step.model_dump(by_alias=True, exclude_none=True)

        assert len(serialized["detail"]["message"]) <= 503  # 500 + "..."
        assert serialized["detail"]["message"].endswith("...")
        assert long_error_message not in str(serialized)

    def test_string_detail_truncated(self):
        """Plain string detail (not dict) is also truncated."""
        long_detail = (
            "Error: connection to https://neptune-cluster-abc123.us-east-1.neptune.amazonaws.com:8182 failed. "
            + ("x" * 600)
        )
        step = TraceStep(
            step="t3.vector_search",
            status="error",
            durationMs=200,
            detail=long_detail,
        )

        serialized = step.model_dump(by_alias=True, exclude_none=True)

        assert len(serialized["detail"]) <= 503
        assert serialized["detail"].endswith("...")
        assert "abc123" not in serialized["detail"][500:]

    def test_short_detail_unchanged(self):
        """Detail under 500 chars passes through unmodified."""
        short_detail = "Query timed out after 30s"
        step = TraceStep(step="t2.sql", status="error", durationMs=30000, detail=short_detail)

        serialized = step.model_dump(by_alias=True, exclude_none=True)

        assert serialized["detail"] == short_detail

    def test_none_detail_excluded(self):
        """None detail is excluded from serialized output."""
        step = TraceStep(step="t1.resolve", status="done", durationMs=5)

        serialized = step.model_dump(by_alias=True, exclude_none=True)

        assert "detail" not in serialized


class TestClientErrorDetail:
    """CWE-209: the client-facing detail for a caught exception carries its type only."""

    def test_omits_exception_message(self):
        from coa_serve.trace import client_error_detail

        exc = RuntimeError("User: arn:aws:iam::123456789012:assumed-role/X is not authorized")
        assert client_error_detail(exc) == {"error": "RuntimeError"}

    def test_merges_extra_fields(self):
        from coa_serve.trace import client_error_detail

        assert client_error_detail(TimeoutError("db-host:5432"), timeoutSeconds=30) == {
            "error": "TimeoutError",
            "timeoutSeconds": 30,
        }
