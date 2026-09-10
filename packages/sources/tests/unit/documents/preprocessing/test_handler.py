# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the preprocessing Lambda handler."""

import os
from unittest.mock import call, patch

import pytest

# Add preprocessing source to path for imports

# Set required env vars before importing handler (module-level validation runs at import)
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("DOC_SOURCES_TABLE", "test-table")

from coa_common.constants import bucket_namespace_tag_key, validate_id  # noqa: E402

# Common env vars required by handler
_ENV = {"BUCKET_NAME": "test-bucket", "DOC_SOURCES_TABLE": "test-table"}


# ---------------------------------------------------------------------------
# Input validation — validate_id (now in coa_common.constants)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateId:
    def test_valid_simple(self):
        validate_id("valid-id_123", "test")  # should not raise

    def test_valid_alphanumeric(self):
        validate_id("abc123", "test")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid test"):
            validate_id("", "test")

    def test_path_traversal_raises(self):
        with pytest.raises(ValueError):
            validate_id("../path/traversal", "test")

    def test_spaces_raise(self):
        with pytest.raises(ValueError):
            validate_id("has spaces", "test")

    def test_dots_raise(self):
        with pytest.raises(ValueError):
            validate_id("has.dots", "test")

    def test_slashes_raise(self):
        with pytest.raises(ValueError):
            validate_id("has/slash", "test")


# ---------------------------------------------------------------------------
# Validation error responses (400)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerValidation:
    @patch.dict(os.environ, _ENV)
    def test_invalid_namespace_returns_400(self):
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "../bad", "doc_source_id": "ok"}, None)
        assert result["status"] == "SCAN_FAILED"
        assert len(result["issues_preview"]) > 0

    @patch.dict(os.environ, _ENV)
    def test_empty_ids_returns_400(self):
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "", "doc_source_id": ""}, None)
        assert result["status"] == "SCAN_FAILED"

    @patch.dict(os.environ, _ENV)
    def test_invalid_doc_source_returns_400(self):
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "ok", "doc_source_id": "bad/id"}, None)
        assert result["status"] == "SCAN_FAILED"
        assert result["issues_preview"][0]["reason"]


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerResponseShape:
    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_empty_listing_has_all_keys(self, mock_client, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        result = handler(
            {"namespace_id": "ns1", "doc_source_id": "ds1", "s3_prefixes": ["ns1/raw/ds1/"]},
            None,
        )
        assert result["status"] == "SCANNING"
        expected_keys = {
            "status",
            "staging_prefix",
            "files_total",
            "files_preprocessed",
            "files_skipped",
            "files_errored",
            "issues_preview",
            "issues_truncated",
            "issues_s3_key",
            "elapsed_seconds",
        }
        assert expected_keys.issubset(result.keys())
        assert isinstance(result["elapsed_seconds"], float)
        assert result["files_total"] == 0


# ---------------------------------------------------------------------------
# s3_prefixes — whole bucket and multi-prefix behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestS3PrefixBehaviour:
    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_empty_prefixes_lists_whole_bucket(self, mock_client, mock_list):
        """No s3_prefixes → list_objects called with empty prefix (whole bucket)."""
        from coa_sources.documents.preprocessing.handler import handler

        handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)
        mock_list.assert_called_once()
        args = mock_list.call_args[0]
        assert args[2] == ""  # empty prefix = whole bucket

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_single_prefix_used(self, mock_client, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        handler({"namespace_id": "ns1", "doc_source_id": "ds1", "s3_prefixes": ["ns1/custom/path/"]}, None)
        args = mock_list.call_args[0]
        assert args[2] == "ns1/custom/path/"

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_multiple_prefixes_calls_list_objects_per_prefix(self, mock_client, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        handler(
            {"namespace_id": "ns1", "doc_source_id": "ds1", "s3_prefixes": ["ns1/reports/2024/", "ns1/reports/2025/"]},
            None,
        )
        assert mock_list.call_count == 2
        prefixes_called = [c[0][2] for c in mock_list.call_args_list]
        assert "ns1/reports/2024/" in prefixes_called
        assert "ns1/reports/2025/" in prefixes_called

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_staging_prefix_always_derived(self, mock_client, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "myns", "doc_source_id": "myds"}, None)
        assert result["staging_prefix"] == "myns/staging/myds/"

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes", return_value=b"ok")
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_deduplicates_overlapping_prefixes(self, mock_client, mock_list, mock_read, mock_upload, mock_meta):
        """If two prefixes return the same key, it should only be processed once."""
        mock_list.return_value = [{"Key": "reports/doc.txt", "Size": 2}]
        from coa_sources.documents.preprocessing.handler import handler

        result = handler(
            {"namespace_id": "ns1", "doc_source_id": "ds1", "s3_prefixes": ["ns1/reports/", "ns1/reports/"]},
            None,
        )
        assert result["files_preprocessed"] == 1
        assert mock_upload.call_count == 1


# ---------------------------------------------------------------------------
# Upload-prefix namespace-scoping guard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUploadPrefixNamespaceGuard:
    """Upload-type jobs read from the shared platform bucket, so a prefix that
    is not scoped to the job's namespace must be refused before any listing —
    defense-in-depth behind the server-side prefix derivation in create."""

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_upload_foreign_namespace_prefix_refused(self, mock_client, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        result = handler(
            {"namespace_id": "ns1", "doc_source_id": "ds1", "s3_prefixes": ["ns2/raw/upload1/"]},
            None,
        )
        assert result["status"] == "SCAN_FAILED"
        # Nothing in the other namespace may be listed.
        mock_list.assert_not_called()
        # The offending prefix must not be echoed back to the caller.
        assert "ns2" not in str(result["issues_preview"])

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch(
        "coa_sources.documents.preprocessing.handler.parse_bucket_from_arn",
        return_value="cust-bucket",
    )
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    @patch(
        "coa_sources.documents.preprocessing.handler.get_bucket_tags",
        return_value={bucket_namespace_tag_key(): "ns1"},
    )
    def test_s3_type_prefix_not_namespace_scoped_is_allowed(self, mock_tags, mock_client, mock_parse, mock_list):
        """S3-type prefixes point into the customer's own bucket, so the guard
        does not apply — an arbitrary prefix is listed as-is."""
        from coa_sources.documents.preprocessing.handler import handler

        handler(
            {
                "namespace_id": "ns1",
                "doc_source_id": "ds1",
                "source_bucket_arn": "arn:aws:s3:::cust-bucket",
                "s3_prefixes": ["anything/"],
            },
            None,
        )
        mock_list.assert_called_once()
        assert mock_list.call_args[0][2] == "anything/"


@pytest.mark.unit
class TestHandlerErrorResponses:
    """Error paths must return a structured SCAN_FAILED, not crash, and must not
    leak internal detail in the user-facing reason."""

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.parse_bucket_from_arn", return_value="ext-bucket")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_bad_role_name_returns_scan_failed(self, mock_client, mock_parse, mock_list):
        """A role ARN that violates the naming convention returns SCAN_FAILED
        rather than raising uncaught and failing the Lambda."""
        from coa_sources.documents.preprocessing.handler import handler

        result = handler(
            {
                "namespace_id": "ns1",
                "doc_source_id": "ds1",
                "source_bucket_arn": "arn:aws:s3:::ext-bucket",
                "role_arn": "arn:aws:iam::123456789012:role/evil-role",
            },
            None,
        )
        assert result["status"] == "SCAN_FAILED"
        mock_list.assert_not_called()
        # The reason names the required prefix only, not the caller's role name.
        assert "evil-role" not in str(result["issues_preview"])

    @patch.dict(os.environ, _ENV)
    @patch(
        "coa_sources.documents.preprocessing.handler.list_objects",
        side_effect=RuntimeError("AccessDenied: arn:aws:s3:::secret-bucket internal detail"),
    )
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_listing_failure_reason_is_generic(self, mock_client, mock_list):
        """A listing failure surfaces a generic reason; the exception detail is
        logged for operators, not echoed to the caller via GetSource."""
        from coa_sources.documents.preprocessing.handler import handler

        result = handler(
            {"namespace_id": "ns1", "doc_source_id": "ds1", "s3_prefixes": ["ns1/raw/ds1/"]},
            None,
        )
        assert result["status"] == "SCAN_FAILED"
        reason = result["issues_preview"][0]["reason"]
        assert reason == "Failed to list source files"
        assert "secret-bucket" not in reason
        assert "AccessDenied" not in reason


# ---------------------------------------------------------------------------
# Cross-account S3 access
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCrossAccountAccess:
    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch(
        "coa_sources.documents.preprocessing.handler.parse_bucket_from_arn",
        return_value="external-bucket",
    )
    @patch(
        "coa_sources.documents.preprocessing.handler.get_bucket_tags",
        return_value={bucket_namespace_tag_key(): "ns1"},
    )
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_source_bucket_arn_uses_external_bucket(self, mock_client, mock_tags, mock_parse, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        handler(
            {
                "namespace_id": "ns1",
                "doc_source_id": "ds1",
                "source_bucket_arn": "arn:aws:s3:::external-bucket",
                "role_arn": "arn:aws:iam::123456789012:role/coa-cross-account-reader",
            },
            None,
        )
        mock_parse.assert_called_once_with("arn:aws:s3:::external-bucket")
        # One ambient client for the tag read, then the role-scoped source client.
        assert call(role_arn="arn:aws:iam::123456789012:role/coa-cross-account-reader") in mock_client.call_args_list
        list_call_args = mock_list.call_args[0]
        assert list_call_args[1] == "external-bucket"

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.parse_bucket_from_arn", return_value="ext-bucket")
    @patch(
        "coa_sources.documents.preprocessing.handler.get_bucket_tags",
        return_value={bucket_namespace_tag_key(): "ns1"},
    )
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_without_role_arn_an_authorized_bucket_is_read_with_the_ambient_client(
        self, mock_client, mock_tags, mock_parse, mock_list
    ):
        """roleArn stays optional — the bucket tag is what authorizes the read, so a
        same-account bucket the owner has tagged needs no role."""
        from coa_sources.documents.preprocessing.handler import handler

        handler(
            {
                "namespace_id": "ns1",
                "doc_source_id": "ds1",
                "source_bucket_arn": "arn:aws:s3:::ext-bucket",
            },
            None,
        )
        assert call(role_arn=None) in mock_client.call_args_list

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_no_source_bucket_uses_our_bucket(self, mock_client, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)
        assert mock_client.call_count == 2
        assert mock_client.call_args_list[0] == call()
        list_call_args = mock_list.call_args[0]
        assert list_call_args[1] == "test-bucket"


# ---------------------------------------------------------------------------
# Empty extraction — the 39-of-78 silent loss
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmptyExtractionIsReported:
    """A processor can return "" without raising, and used to be counted a success.

    `unstructured` abandons text extraction on drawing-heavy PDF pages and yields
    zero characters. That empty string was uploaded to staging as a 0-byte object
    and counted in files_preprocessed, so the source reported Skipped=0/Errored=0
    while KG build silently dropped every empty file — 39 of 78 PDFs vanished with
    nothing in the UI to show it.
    """

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes", return_value=b"%PDF-1.7")
    @patch("coa_sources.documents.preprocessing.handler.get_page_count", return_value=30)
    @patch("coa_sources.documents.preprocessing.handler.process_pdf", return_value=("", ".md"))
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_empty_pdf_is_skipped_not_staged(
        self, mock_client, mock_list, mock_pdf, mock_pages, mock_read, mock_upload, mock_meta
    ):
        mock_list.return_value = [{"Key": "raw/drawing.pdf", "Size": 5000}]
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        assert result["files_preprocessed"] == 0, "an empty extraction is not a success"
        assert result["files_skipped"] == 1
        assert result["files_errored"] == 0, "empty is skipped, not an error"
        assert mock_upload.call_count == 0, "no 0-byte object may reach staging"
        assert mock_meta.call_count == 0
        assert len(result["issues_preview"]) == 1
        issue = result["issues_preview"][0]
        assert issue["filename"] == "raw/drawing.pdf"
        assert issue["type"] == "skipped"
        assert "No text extracted" in issue["reason"]

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes", return_value=b"%PDF-1.7")
    @patch("coa_sources.documents.preprocessing.handler.get_page_count", return_value=1)
    @patch("coa_sources.documents.preprocessing.handler.process_pdf", return_value=("| a | b |", ".md"))
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_enable_table_extraction_flag_reaches_process_pdf(
        self, mock_client, mock_list, mock_pdf, mock_pages, mock_read, mock_upload, mock_meta
    ):
        """The flag arrives on the state-machine input as a string in
        extraction_config and must be forwarded as a bool kwarg."""
        mock_list.return_value = [{"Key": "policy.pdf", "Size": 5000}]
        from coa_sources.documents.preprocessing.handler import handler

        result = handler(
            {
                "namespace_id": "ns1",
                "doc_source_id": "ds1",
                "extraction_config": {"enable_table_extraction": "true"},
            },
            None,
        )
        assert result["files_preprocessed"] == 1
        _, kwargs = mock_pdf.call_args
        assert kwargs.get("enable_table_extraction") is True
        # Metadata records which processing method ran, so operators can grep.
        uploaded_metadata = mock_meta.call_args.args[3]
        assert uploaded_metadata["processing_method"] == "textract_analyze_document_tables"

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes", return_value=b"%PDF-1.7")
    @patch("coa_sources.documents.preprocessing.handler.get_page_count", return_value=1)
    @patch("coa_sources.documents.preprocessing.handler.process_pdf", return_value=("prose", ".md"))
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_enable_table_extraction_defaults_to_false(
        self, mock_client, mock_list, mock_pdf, mock_pages, mock_read, mock_upload, mock_meta
    ):
        """Missing extraction_config → flag is False (backward compatible)."""
        mock_list.return_value = [{"Key": "prose.pdf", "Size": 5000}]
        from coa_sources.documents.preprocessing.handler import handler

        handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)
        _, kwargs = mock_pdf.call_args
        assert kwargs.get("enable_table_extraction") is False

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes")
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_whitespace_only_is_empty(self, mock_client, mock_list, mock_read, mock_upload, mock_meta):
        """A file of only whitespace stages nothing — KG build would skip it anyway."""
        mock_list.return_value = [{"Key": "blank.txt", "Size": 6}]
        mock_read.return_value = b"  \n\t \n"
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        assert result["files_skipped"] == 1
        assert result["files_preprocessed"] == 0
        assert mock_upload.call_count == 0

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes")
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_empty_files_do_not_hide_the_ones_that_worked(
        self, mock_client, mock_list, mock_read, mock_upload, mock_meta
    ):
        """The guard is per-file: a real file still stages, and the counts split."""
        mock_list.return_value = [{"Key": "good.txt", "Size": 12}, {"Key": "empty.txt", "Size": 0}]
        mock_read.side_effect = [b"real content", b""]
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        assert result["files_total"] == 2
        assert result["files_preprocessed"] == 1
        assert result["files_skipped"] == 1
        assert mock_upload.call_count == 1
        assert mock_upload.call_args[0][3] == "real content"

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes", return_value=b"text")
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_every_file_empty_is_a_scan_failure(self, mock_client, mock_list, mock_read, mock_upload, mock_meta):
        """Nothing extracted from anything is SCAN_FAILED, not a completed scan."""
        mock_list.return_value = [{"Key": "a.txt", "Size": 1}, {"Key": "b.txt", "Size": 1}]
        mock_read.side_effect = [b"", b"   "]
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        assert result["status"] == "SCAN_FAILED"
        assert result["files_skipped"] == 2
        assert result["files_preprocessed"] == 0

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.upload_metadata")
    @patch("coa_sources.documents.preprocessing.handler.upload_file")
    @patch("coa_sources.documents.preprocessing.handler.read_file_bytes", return_value=b"hello world")
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_success_path_is_untouched(self, mock_client, mock_list, mock_read, mock_upload, mock_meta):
        """A normal extraction is unaffected — same counts, same staging key."""
        mock_list.return_value = [{"Key": "reports/notes.txt", "Size": 11}]
        from coa_sources.documents.preprocessing.handler import handler

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        assert result["files_preprocessed"] == 1
        assert result["files_skipped"] == 0
        assert result["issues_preview"] == []
        assert mock_upload.call_count == 1
        assert mock_upload.call_args[0][2].endswith("reports/notes.txt")


class TestBucketNamespaceAuthorization:
    """A caller-named bucket is read only when its own tag authorizes the job's
    namespace. Holding manageSource on a namespace is not evidence that the caller
    may read the bucket it names, so the bucket owner's tag is the authorization.
    """

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.parse_bucket_from_arn", return_value="other-bucket")
    @patch("coa_sources.documents.preprocessing.handler.get_bucket_tags", return_value={})
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_untagged_bucket_is_refused(self, mock_client, mock_tags, mock_parse, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        with pytest.raises(ValueError, match="does not authorize namespace"):
            handler(
                {
                    "namespace_id": "ns1",
                    "doc_source_id": "ds1",
                    "source_bucket_arn": "arn:aws:s3:::other-bucket",
                },
                None,
            )
        mock_list.assert_not_called()

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.parse_bucket_from_arn", return_value="other-bucket")
    @patch(
        "coa_sources.documents.preprocessing.handler.get_bucket_tags",
        return_value={bucket_namespace_tag_key(): "ns-other"},
    )
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_bucket_tagged_for_another_namespace_is_refused(self, mock_client, mock_tags, mock_parse, mock_list):
        """The cross-namespace case: a bucket another namespace was granted."""
        from coa_sources.documents.preprocessing.handler import handler

        with pytest.raises(ValueError, match="does not authorize namespace"):
            handler(
                {
                    "namespace_id": "ns1",
                    "doc_source_id": "ds1",
                    "source_bucket_arn": "arn:aws:s3:::other-bucket",
                },
                None,
            )
        mock_list.assert_not_called()

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.parse_bucket_from_arn", return_value="shared-bucket")
    @patch(
        "coa_sources.documents.preprocessing.handler.get_bucket_tags",
        return_value={bucket_namespace_tag_key(): "ns-other ns1 ns-third"},
    )
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_bucket_shared_across_namespaces_is_allowed(self, mock_client, mock_tags, mock_parse, mock_list):
        """One bucket may serve several namespaces — that is why the tag is a list."""
        from coa_sources.documents.preprocessing.handler import handler

        handler(
            {
                "namespace_id": "ns1",
                "doc_source_id": "ds1",
                "source_bucket_arn": "arn:aws:s3:::shared-bucket",
            },
            None,
        )
        mock_list.assert_called()

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.get_bucket_tags")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_upload_sources_are_not_tag_checked(self, mock_client, mock_tags, mock_list):
        """Upload sources read the platform's own bucket, which no customer tags."""
        from coa_sources.documents.preprocessing.handler import handler

        handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)
        mock_tags.assert_not_called()

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects", return_value=[])
    @patch("coa_sources.documents.preprocessing.handler.parse_bucket_from_arn", return_value="b")
    @patch("coa_sources.documents.preprocessing.handler.get_bucket_tags")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_a_tag_read_fault_is_not_swallowed(self, mock_client, mock_tags, mock_parse, mock_list):
        """An AccessDenied reading tags must not degrade into "unauthorized" silently."""
        from botocore.exceptions import ClientError
        from coa_sources.documents.preprocessing.handler import handler

        mock_tags.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetBucketTagging"
        )
        with pytest.raises(ClientError):
            handler(
                {
                    "namespace_id": "ns1",
                    "doc_source_id": "ds1",
                    "source_bucket_arn": "arn:aws:s3:::b",
                },
                None,
            )
        mock_list.assert_not_called()


# ---------------------------------------------------------------------------
# issue 104 — unbounded issues array must not blow the SFN 256 KB payload limit
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIssuesPayloadBounding:
    """A source full of unprocessable objects generates one issue per file. That
    list is unbounded and used to be returned whole, pushing the Lambda result
    past the Step Functions 256 KB limit and failing the whole execution. The
    result must stay bounded: a capped preview inline, a truncated flag, and an
    S3 pointer to the complete report."""

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_many_issues_are_bounded_and_full_report_persisted(self, mock_client, mock_list):
        import json as _json

        from coa_sources.documents.preprocessing.handler import _ISSUES_PREVIEW_MAX, handler

        # 500 unsupported objects with long keys — the raw issues list would be
        # hundreds of KB (each entry carries the full key + a verbose reason).
        long_key = "raw/" + "d" * 300
        mock_list.return_value = [{"Key": f"{long_key}/file_{i}.bin", "Size": 10} for i in range(500)]

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        # Bounded shape, no raw `issues` array.
        assert "issues" not in result
        assert len(result["issues_preview"]) == _ISSUES_PREVIEW_MAX
        assert result["issues_truncated"] is True
        assert result["issues_s3_key"].startswith("ns1/scan-results/ds1/")
        assert result["files_skipped"] == 500

        # The returned payload — what Step Functions size-checks — is well under 256 KB.
        assert len(_json.dumps(result).encode("utf-8")) < 256 * 1024

        # The complete 500-entry report was persisted to the platform bucket.
        put_calls = [c for c in mock_client.return_value.put_object.call_args_list]
        assert len(put_calls) == 1
        kwargs = put_calls[0].kwargs
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"] == result["issues_s3_key"]
        assert len(_json.loads(kwargs["Body"])) == 500

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_few_issues_stay_inline_with_no_s3_write(self, mock_client, mock_list):
        from coa_sources.documents.preprocessing.handler import handler

        mock_list.return_value = [{"Key": f"raw/file_{i}.bin", "Size": 10} for i in range(3)]

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        assert len(result["issues_preview"]) == 3
        assert result["issues_truncated"] is False
        assert result["issues_s3_key"] == ""
        mock_client.return_value.put_object.assert_not_called()

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.list_objects")
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_s3_persist_failure_is_best_effort_job_still_succeeds(self, mock_client, mock_list):
        # Best-effort persistence: a put_object failure must NOT fail the job.
        # The payload stays bounded (preview only), truncated stays True, and the
        # s3 key is empty so consumers know the full report is unavailable.
        from botocore.exceptions import ClientError
        from coa_sources.documents.preprocessing.handler import _ISSUES_PREVIEW_MAX, handler

        mock_client.return_value.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject"
        )
        mock_list.return_value = [{"Key": f"raw/file_{i}.bin", "Size": 10} for i in range(60)]

        result = handler({"namespace_id": "ns1", "doc_source_id": "ds1"}, None)

        # Job did not raise; bounded result returned with no usable S3 pointer.
        assert result["status"] == "SCAN_FAILED"  # all skipped → no success
        assert len(result["issues_preview"]) == _ISSUES_PREVIEW_MAX
        assert result["issues_truncated"] is True
        assert result["issues_s3_key"] == ""
        mock_client.return_value.put_object.assert_called_once()

    @patch.dict(os.environ, _ENV)
    @patch("coa_sources.documents.preprocessing.handler.get_s3_client")
    def test_preview_is_byte_bounded_even_with_huge_reasons(self, mock_client):
        # Entry-count cap alone is not enough: an error entry's reason is
        # str(exc) (unbounded) and filename is the full S3 key. 20 entries with
        # multi-KB reasons would still cross 256 KB. The preview must cap both
        # fields; the full text stays in the S3 report.
        import json as _json

        from coa_sources.documents.preprocessing.handler import (
            _ISSUES_PREVIEW_MAX,
            _PREVIEW_FILENAME_MAX,
            _PREVIEW_REASON_MAX,
            _bounded_issues,
        )

        huge = "\u00e9" * 100_000  # 100 KB of non-ASCII (escapes to \uXXXX in JSON)
        issues = [{"filename": "raw/" + "k" * 2000 + f"_{i}.bin", "type": "error", "reason": huge} for i in range(25)]

        result = _bounded_issues(issues, namespace_id="ns1", doc_source_id="ds1")

        assert len(result["issues_preview"]) == _ISSUES_PREVIEW_MAX
        for entry in result["issues_preview"]:
            assert len(entry["filename"]) <= _PREVIEW_FILENAME_MAX
            assert len(entry["reason"]) <= _PREVIEW_REASON_MAX
        # The state payload (preview + flags) is provably under the SFN limit.
        assert len(_json.dumps(result).encode("utf-8")) < 256 * 1024
        # The full untruncated report still went to S3 with all 25 entries.
        body = mock_client.return_value.put_object.call_args.kwargs["Body"]
        full = _json.loads(body)
        assert len(full) == 25
        assert len(full[0]["reason"]) == 100_000
