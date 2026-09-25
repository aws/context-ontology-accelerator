# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared validation for namespace-scoped OSI import object keys."""

from __future__ import annotations

from typing import TypeGuard

IMPORT_S3_KEY_SCOPE_ERROR = "s3Key must be under the namespace import prefix"


def is_import_s3_key_for_namespace(value: object, namespace_id: str) -> TypeGuard[str]:
    """Return whether an opaque S3 key is under the namespace import prefix."""
    return bool(namespace_id) and isinstance(value, str) and value.startswith(f"{namespace_id}/imports/")
