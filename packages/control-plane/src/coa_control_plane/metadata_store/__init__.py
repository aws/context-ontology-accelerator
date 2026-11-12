# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metadata store abstraction and implementations (re-exported from common)."""

from coa_common.metadata_store import (
    AssetResult,
    MetadataStoreClient,
    MetadataStoreError,
    ProjectResult,
    SMUSClient,
)

__all__ = [
    "AssetResult",
    "MetadataStoreClient",
    "MetadataStoreError",
    "ProjectResult",
    "SMUSClient",
]
