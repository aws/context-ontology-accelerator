# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client-safe failure text: no ARNs, account ids, endpoints or tracebacks reach a caller."""

from __future__ import annotations

import zipfile

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from coa_sources.client_errors import (
    GENERIC_FAILURE,
    MAX_CLIENT_ERROR_CHARS,
    ClientSafeError,
    client_safe_error,
    is_client_safe,
    redact,
)
from coa_sources.database import glue_ownership

pytestmark = pytest.mark.unit


def test_client_error_keeps_operation_and_code_only() -> None:
    exc = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "arn:aws:iam::123456789012:role/x denied"}},
        "GetTags",
    )
    assert client_safe_error(exc) == "GetTags failed: AccessDeniedException"


def test_botocore_fault_names_only_its_type() -> None:
    exc = EndpointConnectionError(endpoint_url="https://glue.us-east-1.amazonaws.com")
    msg = client_safe_error(exc)
    assert "EndpointConnectionError" in msg and "amazonaws" not in msg


def test_own_explanatory_message_is_kept_but_redacted() -> None:
    exc = RuntimeError(
        "Connection test failed: role arn:aws:iam::123456789012:role/reader cannot reach "
        "db.cluster-abc.us-east-1.rds.amazonaws.com:5432 in account 123456789012"
    )
    msg = client_safe_error(exc)
    assert msg.startswith("Connection test failed: role <arn> cannot reach <aws-endpoint>")
    assert "123456789012" not in msg


def test_library_error_keeps_type_and_redacted_reason() -> None:
    """A per-file reason such as a corrupt archive or an unreadable PDF is what the caller acts on."""
    assert client_safe_error(zipfile.BadZipFile("File is not a zip file")) == "BadZipFile: File is not a zip file"

    class PdfReadError(Exception):
        pass

    msg = client_safe_error(PdfReadError("EOF marker not found in s3://bucket/123456789012/a.pdf"))
    assert msg == "PdfReadError: EOF marker not found in s3://bucket/<account>/a.pdf"


def test_library_error_without_safe_text_names_only_its_type() -> None:
    assert client_safe_error(KeyError()) == "Unexpected KeyError; see the service logs for details."
    tb = OSError('Traceback (most recent call last):\n  File "/var/task/x.py", line 1')
    assert client_safe_error(tb) == "Unexpected OSError; see the service logs for details."


def test_uuid_with_all_digit_last_group_is_not_redacted() -> None:
    uuid = "0b8f0e8e-1111-4222-8333-444455556666"
    assert redact(f"namespace '{uuid}' in account 123456789012") == f"namespace '{uuid}' in account <account>"
    assert redact("9b8f0e8e-1111-4222-8333-123456789012") == "9b8f0e8e-1111-4222-8333-123456789012"


def test_client_safe_error_is_returned_verbatim() -> None:
    message = "x" * (MAX_CLIENT_ERROR_CHARS + 100) + " arn:aws:glue:us-east-1:123456789012:database/db"
    exc = ClientSafeError(message)
    assert is_client_safe(exc)
    assert client_safe_error(exc) == message
    assert redact(message, client_safe=True) == message
    assert not is_client_safe(ClientSafeError("  "))
    assert not is_client_safe(RuntimeError(message))


def test_glue_tag_hint_reaches_the_steward_unchanged() -> None:
    """The tag command carries the caller's own database ARN, account and namespace; it must survive intact."""
    namespace = "0b8f0e8e-1111-4222-8333-444455556666"
    hint = glue_ownership._tag_hint("123456789012", "sales_db", namespace, "us-east-1")
    exc = glue_ownership.GlueOwnershipError(hint)
    assert len(hint) > MAX_CLIENT_ERROR_CHARS
    assert client_safe_error(exc) == hint
    assert "arn:aws:glue:us-east-1:123456789012:database/sales_db" in hint
    assert f'"{namespace}"' in client_safe_error(exc)


def test_redact_replaces_tracebacks_and_bounds_length() -> None:
    assert redact('Traceback (most recent call last):\n  File "/var/task/x.py", line 1') == GENERIC_FAILURE
    assert len(redact("x" * 5000)) == MAX_CLIENT_ERROR_CHARS
    assert redact("   ") == GENERIC_FAILURE
