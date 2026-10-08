# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for namespace ValidationError -> 400 message formatting."""

from __future__ import annotations

import pytest
from coa_control_plane.namespace.validation_errors import format_validation_error
from coa_control_plane_server.models.create_namespace_request_content import CreateNamespaceRequestContent
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _error(body: dict) -> str:
    with pytest.raises(ValidationError) as exc:
        CreateNamespaceRequestContent.model_validate(body)
    return format_validation_error(exc.value)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("owner", "a@b.com<script>", "owner must be a valid email address"),
        ("displayName", "x<y", "displayName may not contain '<', '>', or control characters"),
        (
            "description",
            "x>y",
            "description may not contain '<', '>', or control characters other than tab and newline",
        ),
    ],
)
def test_pattern_failures_get_field_message(field: str, value: str, expected: str) -> None:
    assert _error({"name": "sales", "owner": "a@b.com", field: value}) == expected


def test_non_pattern_failures_keep_pydantic_message() -> None:
    message = _error({"name": "sales", "owner": "a@b.com", "displayName": "x" * 257})
    assert "at most 256 characters" in message


def test_name_pattern_failure_is_not_rewritten() -> None:
    # Only the free-text fields are mapped; other fields keep the generated text.
    assert "regular expression" in _error({"name": "Bad Name!", "owner": "a@b.com"})
