# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared formatting for namespace request ValidationError -> API 400 message.

The Smithy-generated models report a ``@pattern`` mismatch as
``"Value error, must validate the regular expression /.../"``, which is
unreadable in the UI. Replace it with a per-field message for the
free-text fields whose patterns reject markup.
"""

from __future__ import annotations

from pydantic import ValidationError

_PATTERN_MESSAGES: dict[str, str] = {
    "owner": "owner must be a valid email address",
    "displayName": "displayName may not contain '<', '>', or control characters",
    "description": "description may not contain '<', '>', or control characters other than tab and newline",
}


def format_validation_error(exc: ValidationError) -> str:
    """Return a 400 message for the first Pydantic validation error."""
    errors = exc.errors()
    if not errors:
        return str(exc)

    first = errors[0]
    loc = first.get("loc", ())
    field = loc[0] if loc else None
    if first.get("type") == "value_error" and isinstance(field, str) and field in _PATTERN_MESSAGES:
        return _PATTERN_MESSAGES[field]
    return first.get("msg", str(exc))
