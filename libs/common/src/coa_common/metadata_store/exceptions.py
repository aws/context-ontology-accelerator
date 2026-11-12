# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metadata store exceptions."""

from coa_common.exceptions import SCLError


class MetadataStoreError(SCLError):
    """Raised when a metadata store operation fails."""

    def __init__(self, message: str) -> None:
        """Initialize with ``message`` and the METADATA_STORE_ERROR code."""
        super().__init__(message, code="METADATA_STORE_ERROR")
