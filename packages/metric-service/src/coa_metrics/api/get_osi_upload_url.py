# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Get OSI Upload URL Lambda handler.

POST /namespaces/{namespaceId}/import-osi/upload-url → 200 OK

Generates a pre-signed S3 PUT URL for uploading large OSI YAML files.
After uploading, the client calls POST /import-osi with the returned s3Key.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import boto3
import structlog
from botocore.config import Config
from coa_common.logging import setup_logging
from coa_common.response import api_response, get_caller_identity

from coa_metrics.api.contract import FILENAME_MAX_LENGTH, FILENAME_MIN_LENGTH, check_string

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────────────

MAX_UPLOAD_SIZE_MB = 10
PRESIGN_EXPIRY_SECONDS = 900  # 15 minutes
ALLOWED_EXTENSIONS = {".yaml", ".yml"}

# ── Singleton S3 client ─────────────────────────────────────────────────

_s3_client: Any = None


def _get_s3_client() -> Any:
    global _s3_client  # noqa: PLW0603
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(signature_version="s3v4"),
        )
    return _s3_client


# ── Handler ─────────────────────────────────────────────────────────────


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Generate a pre-signed S3 PUT URL for OSI YAML upload."""
    namespace = event.get("pathParameters", {}).get("namespaceId", "")
    caller = get_caller_identity(event)
    logger.info("get_osi_upload_url", namespace=namespace, caller=caller)

    if not namespace:
        return api_response(400, {"message": "namespaceId is required"})

    # Parse request body
    try:
        body = json.loads(event.get("body") or "null")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"message": "Invalid JSON in request body"})

    if body is None or not isinstance(body, dict):
        return api_response(400, {"message": "Request body is required"})

    filename = body.get("filename", "")
    if not filename:
        return api_response(400, {"message": "'filename' is required"})
    filename_error = check_string(filename, "filename", min_length=FILENAME_MIN_LENGTH, max_length=FILENAME_MAX_LENGTH)
    if filename_error:
        return api_response(400, {"message": filename_error})

    # Reject path traversal — filename must be a bare name with no separators
    if "/" in filename or "\\" in filename or filename != os.path.basename(filename):
        return api_response(400, {"message": "Invalid filename: must not contain path separators"})

    # Validate file extension
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return api_response(
            400,
            {"message": f"Invalid file extension. Allowed: {sorted(ALLOWED_EXTENSIONS)}"},
        )

    # Generate unique S3 key
    bucket_name = os.environ.get("OSI_BUCKET_NAME", "")
    if not bucket_name:
        logger.error("OSI_BUCKET_NAME not configured")
        return api_response(500, {"message": "S3 bucket not configured"})

    upload_id = str(uuid.uuid4())
    s3_key = f"{namespace}/imports/{upload_id}/{filename}"

    # Generate pre-signed PUT URL
    try:
        s3_client = _get_s3_client()
        upload_url = s3_client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket_name,
                "Key": s3_key,
                "ContentType": "application/x-yaml",
            },
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
        )
    except Exception as exc:
        logger.exception("presign_failed", error=str(exc))
        return api_response(500, {"message": "Failed to generate upload URL"})

    logger.info(
        "upload_url_generated",
        namespace=namespace,
        s3_key=s3_key,
        expires_in=PRESIGN_EXPIRY_SECONDS,
    )

    return api_response(
        200,
        {
            "uploadUrl": upload_url,
            "s3Key": s3_key,
            "expiresIn": PRESIGN_EXPIRY_SECONDS,
        },
    )
