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
    assert msg == "PdfReadError: EOF marker not found in <s3-uri>"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Failed to list staged files under s3://example-staging-bucket/ns-1/staging/",
            "Failed to list staged files under <s3-uri>",
        ),
        (
            "Unable to read input data from s3://example-batch-bucket/batch/in/records.jsonl. Check the role's access.",
            "Unable to read input data from <s3-uri>. Check the role's access.",
        ),
    ],
    ids=["kg-build-staging", "bedrock-batch"],
)
def test_s3_uris_are_not_returned(message: str, expected: str) -> None:
    assert redact(message) == expected


def test_toolkit_message_naming_a_bucket_is_redacted() -> None:
    message = "No output files found in bucket example-sources-bucket under prefix ns-1/batch-inference/ds-1/"
    assert redact(message) == "No output files found in bucket <bucket> under prefix ns-1/batch-inference/ds-1/"
    assert redact("Output not found in bucket example-sources-bucket.") == "Output not found in bucket <bucket>."


@pytest.mark.parametrize(
    "message",
    ["Check the bucket policy.", "No such bucket exists.", "The S3 bucket specified."],
    ids=["policy", "exists", "specified"],
)
def test_ordinary_word_after_bucket_is_kept(message: str) -> None:
    assert redact(message) == message


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Access denied for bucket coa-spill-bkt: check policy", "Access denied for bucket <bucket>: check policy"),
        ("in bucket 'coa-data-1': Access Denied", "in bucket <bucket>: Access Denied"),
    ],
    ids=["unquoted", "quoted"],
)
def test_bucket_name_followed_by_a_colon_is_redacted(message: str, expected: str) -> None:
    assert redact(message) == expected


def test_bare_service_bucket_names_are_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUCKET_NAME", "example-sources-bucket")
    monkeypatch.setenv("ATHENA_SPILL_BUCKET", "example-spill-bucket")
    message = "Cannot read example-sources-bucket/ns-1/in.jsonl or write example-spill-bucket/ns-1/out.csv"
    assert "example-" not in redact(message)


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.us-east-1.amazonaws.com/example-other-bucket/ns-1/in.jsonl",
        "https://example-other-bucket.s3.us-east-1.amazonaws.com/ns-1/in.jsonl",
    ],
    ids=["path-style", "virtual-hosted"],
)
def test_s3_https_urls_are_not_returned(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    monkeypatch.delenv("ATHENA_SPILL_BUCKET", raising=False)
    assert redact(f"Access denied: {url}") == "Access denied: <s3-uri>"


def test_tag_guidance_keeps_the_tag_key() -> None:
    message = (
        "bucket 'example-customer-bucket' does not authorize namespace ns-1: "
        "tag the bucket my-coa:namespace=<namespaceId> (space-separated for several namespaces)"
    )
    assert "tag the bucket my-coa:namespace=<namespaceId>" in redact(message)
    assert redact("tag the bucket 'my-coa:namespace=x'") == "tag the bucket 'my-coa:namespace=x'"


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
