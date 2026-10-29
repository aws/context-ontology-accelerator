# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Shared S3 utilities for the Context Ontology Accelerator.

Provides cross-account access via STS AssumeRole, basic S3 operations
(list, download, upload), and ARN parsing.  Used by preprocessing Lambda,
KG Build ECS, deletion pipeline, and the API layer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from coa_common.aws_config import async_boto_config

logger = logging.getLogger(__name__)

_STS_DURATION_SECONDS = 3600

_ROLE_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/.+$")


def regional_s3_client(region: str) -> Any:
    """Return a SigV4 S3 client that addresses buckets by their regional hostname.

    Use it for presigned URLs a browser follows. They name
    ``<bucket>.s3.<region>.amazonaws.com``, where boto3's default names the global
    ``<bucket>.s3.amazonaws.com``. S3 answers the global name with a 307 redirect for
    a bucket created outside us-east-1 until its DNS propagates (up to about 24
    hours), and a browser does not follow that redirect on a cross-origin request.

    Virtual addressing is what stops botocore rewriting a presigned URL onto the
    global name. The endpoint itself still comes from botocore's resolver, so every
    partition keeps its own DNS suffix (``amazonaws.com.cn`` in China). In us-east-1
    the resolver's endpoint is the global name, which serves that region directly.
    """
    return boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def get_s3_client(
    role_arn: str | None = None,
    session_name: str = "coa-cross-account",
    config: Any | None = None,
) -> Any:
    """Return an S3 client, optionally via STS AssumeRole for cross-account.

    Args:
        role_arn: If provided, assumes this IAM role via STS before creating
            the S3 client (cross-account access).
        session_name: STS session name for auditing.
        config: Optional botocore Config. If not provided, the shared async
            preset (socket timeouts + bounded retries) is used.
    """
    resolved = config if config is not None else async_boto_config()
    if role_arn:
        if not _ROLE_ARN_RE.match(role_arn):
            raise ValueError(
                f"Invalid IAM role ARN format: {role_arn!r}. Expected: arn:aws:iam::<account-id>:role/<role-name>"
            )
        logger.info("Assuming cross-account role: %s", role_arn)
        try:
            sts = boto3.client("sts")
            creds = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=session_name,
                DurationSeconds=_STS_DURATION_SECONDS,
            )["Credentials"]
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            logger.error(
                "Failed to assume role %s: %s (%s)",
                role_arn,
                exc.response["Error"]["Message"],
                error_code,
            )
            raise ValueError(f"Cannot assume cross-account role {role_arn}: {error_code}") from exc
        return boto3.client(
            "s3",
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            config=resolved,
        )
    return boto3.client("s3", config=resolved)


def list_objects(
    s3_client: Any,
    bucket: str,
    prefix: str,
    extensions: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """List S3 objects under *prefix*, optionally filtered by extension."""
    paginator = s3_client.get_paginator("list_objects_v2")
    files: list[dict] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key: str = obj["Key"]
            if key.endswith("/"):
                continue
            if extensions is not None:
                ext = _extension(key)
                if ext not in extensions:
                    continue
            files.append({"Key": key, "Size": obj["Size"]})

    return files


def read_file_bytes(s3_client: Any, bucket: str, key: str) -> bytes:
    """Download an S3 object and return its body as bytes."""
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def upload_file(
    s3_client: Any,
    bucket: str,
    key: str,
    content: str | bytes,
) -> None:
    """Upload content to S3.  Strings are UTF-8 encoded automatically."""
    body = content.encode("utf-8") if isinstance(content, str) else content
    s3_client.put_object(Bucket=bucket, Key=key, Body=body)
    logger.info("Uploaded file", extra={"bucket": bucket, "key": key})


def upload_json(
    s3_client: Any,
    bucket: str,
    key: str,
    data: dict,
) -> None:
    """Upload a JSON object to S3 with ``application/json`` content type."""
    body = json.dumps(data, indent=2, default=str).encode("utf-8")
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    logger.info("Uploaded JSON", extra={"bucket": bucket, "key": key})


def get_object_metadata_and_tags(
    s3_client: Any,
    bucket: str,
    key: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (user_metadata, tags) for an S3 object.

    user_metadata — the ``x-amz-meta-*`` headers stored on the object,
        returned as a plain ``{key: value}`` dict (keys are lowercased,
        the ``x-amz-meta-`` prefix is stripped by boto3 automatically).
    tags — the object's tag set as a plain ``{key: value}`` dict.

    Both dicts are empty if the object has no metadata / tags or if the
    calls fail (errors are logged but not re-raised so a missing tag
    permission doesn't abort the whole pipeline).
    """
    user_metadata: dict[str, str] = {}
    tags: dict[str, str] = {}

    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
        user_metadata = head.get("Metadata", {})
    except ClientError as exc:
        logger.warning("head_object failed for %s/%s: %s", bucket, key, exc)

    try:
        tag_resp = s3_client.get_object_tagging(Bucket=bucket, Key=key)
        tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
    except ClientError as exc:
        logger.warning("get_object_tagging failed for %s/%s: %s", bucket, key, exc)

    return user_metadata, tags


def get_bucket_tags(s3_client: Any, bucket: str) -> dict[str, str]:
    """Return *bucket*'s tag set as a plain ``{key: value}`` dict.

    Unlike :func:`get_object_metadata_and_tags` above, this does **not** swallow
    errors. That function's tags are enrichment, so losing them degrades a result;
    these tags are an authorization input, so losing them must not read as "no
    tags" and therefore "not authorized for anything" by accident — an
    ``AccessDenied`` on the tag read is an operational fault, not a decision. Every
    error except a genuinely absent tag set propagates so the caller fails closed
    on a fault rather than silently treating it as a denial it can report.

    ``NoSuchTagSet`` is the one exception: it means the bucket really carries no
    tags, which is a legitimate answer, and an empty dict authorizes nothing.
    """
    try:
        resp = s3_client.get_bucket_tagging(Bucket=bucket)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchTagSet":
            return {}
        raise
    return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}


def parse_bucket_from_arn(arn: str) -> str:
    """Extract the bucket name from an S3 bucket ARN.

    S3 ARN format: ``arn:aws:s3:::bucket-name`` (no region, no account).
    """
    if not arn or not arn.startswith("arn:"):
        raise ValueError(f"Invalid S3 bucket ARN: {arn}")

    parts = arn.split(":")
    # S3 ARN has 6 colon-separated parts: arn, aws, s3, "", "", bucket-name
    if len(parts) < 6 or parts[2] != "s3":
        raise ValueError(f"Cannot parse bucket from ARN: {arn}")

    bucket_name = parts[5].split("/")[0]
    if not bucket_name:
        raise ValueError(f"Cannot parse bucket from ARN: {arn}")

    return bucket_name


def _extension(key: str) -> str:
    """Return the lowercased file extension for an S3 key."""
    dot = key.rfind(".")
    if dot == -1:
        return ""
    return key[dot:].lower()
