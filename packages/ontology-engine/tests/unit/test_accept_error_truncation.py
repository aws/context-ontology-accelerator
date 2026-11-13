# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for proposal accept error truncation.

Verifies that _accept_fail truncates large error messages to prevent
DynamoDB ValidationException when writing to the accept_error attribute.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.unit
class TestAcceptFailErrorTruncation:
    """Verify that _accept_fail truncates error messages to avoid DynamoDB size limits."""

    @patch("coa_ontology.proposals.dynamo_store")
    def test_small_error_message_not_truncated(self, mock_dynamo_store):
        """Short error messages are passed through unchanged."""
        from coa_ontology.proposals import _accept_fail

        error_msg = "Connection timeout"
        _accept_fail("proposal-123", "test-ns", error_msg, step="ingest")

        mock_dynamo_store.update_proposal.assert_called_once_with(
            "proposal-123",
            namespace="test-ns",
            status="accept_failed",
            accept_error=error_msg,
            accept_progress={"step": "ingest", "state": "failed"},
            # Guard: a late step failure must never downgrade an ontology that is
            # already live and serving (see _accept_fail).
            expected_status_not="accepted",
        )

    @patch("coa_ontology.proposals.dynamo_store")
    def test_large_error_message_truncated_to_1000_chars(self, mock_dynamo_store):
        """Error messages longer than 1000 chars are truncated."""
        from coa_ontology.proposals import _accept_fail

        # Create a large error message (simulating BulkIndexError with embedded vectors)
        error_msg = "BulkIndexError: " + "x" * 2000

        _accept_fail("proposal-456", "test-ns", error_msg)

        call_args = mock_dynamo_store.update_proposal.call_args
        actual_error = call_args[1]["accept_error"]

        assert len(actual_error) == 1000
        assert actual_error.startswith("BulkIndexError: ")
        assert actual_error.endswith("x" * 100)  # ends with truncated 'x's

    @patch("coa_ontology.proposals.dynamo_store")
    def test_exactly_1000_chars_not_truncated(self, mock_dynamo_store):
        """Error message exactly 1000 chars long is not truncated."""
        from coa_ontology.proposals import _accept_fail

        error_msg = "Error: " + "y" * 993  # Exactly 1000 chars
        _accept_fail("proposal-789", "test-ns", error_msg)

        call_args = mock_dynamo_store.update_proposal.call_args
        actual_error = call_args[1]["accept_error"]

        assert len(actual_error) == 1000
        assert actual_error == error_msg

    @patch("coa_ontology.proposals._log")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_failure_is_logged_at_error_with_the_step(self, mock_dynamo_store, mock_log):
        """_accept_fail logs every terminal failure at ERROR with its step."""
        from coa_ontology.proposals import _accept_fail

        _accept_fail("proposal-log", "test-ns", "Invalid ontology Turtle: bad syntax", step="ingest")

        mock_log.error.assert_called_once()
        fmt = mock_log.error.call_args[0][0]
        args = mock_log.error.call_args[0][1:]
        rendered = fmt % args
        assert "proposal-log" in rendered
        assert "ingest" in rendered  # the failing step is named
        assert "bad syntax" in rendered  # the reason is carried

    @patch("coa_ontology.proposals._log")
    @patch("coa_ontology.proposals.dynamo_store")
    def test_dynamo_update_failure_logged_but_swallowed(self, mock_dynamo_store, mock_log):
        """If DynamoDB update fails, exception is logged but not raised."""
        from coa_ontology.proposals import _accept_fail

        mock_dynamo_store.update_proposal.side_effect = Exception("DynamoDB timeout")

        _accept_fail("proposal-xyz", "test-ns", "Some error")

        mock_log.exception.assert_called_once()
        assert "failed to record accept_failed status" in mock_log.exception.call_args[0][0]

    @patch("coa_ontology.proposals.dynamo_store")
    def test_non_string_error_converted_to_string(self, mock_dynamo_store):
        """Non-string error objects are converted to string before truncation."""
        from coa_ontology.proposals import _accept_fail

        error_obj = ValueError("Test error")
        _accept_fail("proposal-obj", "test-ns", error_obj)

        call_args = mock_dynamo_store.update_proposal.call_args
        actual_error = call_args[1]["accept_error"]

        assert isinstance(actual_error, str)
        assert "Test error" in actual_error
