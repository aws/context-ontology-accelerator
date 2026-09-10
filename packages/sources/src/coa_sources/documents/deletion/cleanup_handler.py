# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Deletion Pipeline — Stage 1: S3 Cleanup Lambda.

Invoked by the deletion Step Functions state machine.
Removes S3 objects (raw + staging) for the doc source.

The DynamoDB record is deleted by the state machine itself (as a final
DynamoDeleteItem task) after all stages succeed, so that the record
remains available for status updates if any stage fails.
"""

from __future__ import annotations

import os
from typing import Any

import boto3
import structlog
from coa_common import resolve_region
from coa_common.logging import setup_logging

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_AWS_REGION: str = resolve_region()

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=_AWS_REGION)
    return _s3


def _delete_s3_prefix(bucket: str, prefix: str) -> int:
    """Delete all objects under a given S3 prefix. Returns count of deleted objects."""
    s3 = _get_s3()
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if objects:
            response = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            errors = response.get("Errors", [])
            if errors:
                raise RuntimeError(
                    f"S3 delete_objects partial failure: {len(errors)} objects failed "
                    f"(first error: {errors[0].get('Code')} {errors[0].get('Message')})"
                )
            deleted += len(response.get("Deleted", []))
    return deleted


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Step Functions task handler — cleanup S3 files only.

    Does NOT delete the DynamoDB record — that is handled by the state machine
    as a final step after all pipeline stages succeed, ensuring the record
    remains available for status updates if any stage fails.
    """
    namespace_id: str = event["namespace_id"]
    doc_source_id: str = event["doc_source_id"]
    bucket_name: str = event["bucket_name"]
    source_type: str = event.get("source_type", "upload")
    s3_prefixes: list[str] = event.get("s3_prefixes", [])

    logger.info(
        "s3_cleanup_started",
        namespace_id=namespace_id,
        doc_source_id=doc_source_id,
        source_type=source_type,
    )

    raw_deleted = 0
    staging_deleted = 0

    # Only delete raw files from the platform bucket for "upload" type sources.
    # For "s3" type, raw files live in the customer's bucket — we don't touch those.
    if source_type == "upload":
        for prefix in s3_prefixes:
            # Enforce namespace isolation: only delete objects scoped to this namespace.
            # Prefixes are stored from user input and must be validated before use.
            if not prefix.startswith(f"{namespace_id}/"):
                raise ValueError(
                    f"S3 prefix '{prefix}' is not scoped to namespace '{namespace_id}'. Refusing to delete."
                )
            raw_deleted += _delete_s3_prefix(bucket_name, prefix)

    # Staging files are always in our bucket: {namespaceId}/staging/{docSourceId}/
    staging_prefix = f"{namespace_id}/staging/{doc_source_id}/"
    staging_deleted = _delete_s3_prefix(bucket_name, staging_prefix)

    # Scan-result diagnostics reports written by preprocessing (issue 104):
    # {namespaceId}/scan-results/{docSourceId}/. They carry the customer's full
    # object keys for every failed file, so they must not outlive the source.
    scan_results_prefix = f"{namespace_id}/scan-results/{doc_source_id}/"
    scan_results_deleted = _delete_s3_prefix(bucket_name, scan_results_prefix)

    logger.info(
        "s3_cleanup_complete",
        namespace_id=namespace_id,
        doc_source_id=doc_source_id,
        raw_deleted=raw_deleted,
        staging_deleted=staging_deleted,
        scan_results_deleted=scan_results_deleted,
    )

    return {
        "namespace_id": namespace_id,
        "doc_source_id": doc_source_id,
        "tenant_id": event["tenant_id"],
        "raw_objects_deleted": raw_deleted,
        "staging_objects_deleted": staging_deleted,
        "scan_results_objects_deleted": scan_results_deleted,
    }
