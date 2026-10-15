# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Document-source route handlers.

Handles all DOCUMENTS source operations:
  POST   /namespaces/{namespaceId}/sources             — create document source
  POST   /namespaces/{namespaceId}/sources/upload-urls — pre-signed S3 upload URLs
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import unquote

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from coa_common.constants import (
    SUPPORTED_UPLOAD_CONTENT_TYPES,
    bucket_grants_namespace,
    bucket_namespace_tag_key,
    to_graphrag_tenant_id,
    validate_id,
    validate_s3_prefix,
)
from coa_common.dao.base import QueryParams
from coa_common.response import api_response, get_caller_identity
from coa_common.s3 import get_bucket_tags, get_s3_client, parse_bucket_from_arn
from coa_control_plane_server.models.extraction_config import ExtractionConfig
from coa_control_plane_server.models.source_status import SourceStatus
from coa_control_plane_server.models.source_sub_type import SourceSubType
from coa_control_plane_server.models.source_type import SourceType

from coa_sources.utils import merge_extraction_config

from .namespace_counters import adjust_namespace_source_count
from .sources_handler import (
    _BUCKET_NAME,
    _INGESTION_QUEUE_URL,
    _MAX_UPLOAD_FILES,
    _UPLOAD_URL_EXPIRY_SECONDS,
    _get_dao,
    _get_s3,
    _get_sqs,
    _item_to_detail,
    _now_iso,
    _source_id_from_item,
)

_BY_NAME_GSI = "ByName"

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# CREATE DOCUMENT SOURCE
# ---------------------------------------------------------------------------


def _validate_bucket_namespace_authorization(source_bucket_arn: str | None, namespace_id: str) -> dict[str, Any] | None:
    """Bind an S3 document source's bucket to the registering namespace.

    The bucket must carry ``{tag_prefix}.namespace`` listing this namespace. Only a
    principal with ``s3:TagResource`` on the bucket can set that, so the tag is the
    bucket owner's consent — which is the evidence the request itself cannot
    provide, since holding ``manageSource`` on a namespace says nothing about who
    may read the bucket it names.

    Fail-closed on every uncertainty: a malformed ARN, an unreadable tag set, or a
    tag that does not list this namespace all refuse the request, so a bucket we
    cannot prove is authorized is never persisted. A transient client-side fault is
    distinguished from a refusal — it returns a retryable 503, because "retry" and
    "fix your tag" are different instructions.

    This is the fail-fast front door; the preprocessing handler re-checks because it
    is a separate entry point. Mirrors ``_validate_credential_secret_binding`` for
    JDBC credential secrets.

    Returns an error response, or ``None`` when the binding holds.
    """
    # The Smithy S3BucketArn pattern normally rejects a malformed ARN upstream, but
    # this must not depend on that — parse_bucket_from_arn raises rather than
    # returning empty, so an unguarded call would surface as a 500. An absent ARN is
    # refused here rather than narrowed away at the call site, so that a caller who
    # reaches this function can never skip the check by passing nothing.
    try:
        bucket = parse_bucket_from_arn(source_bucket_arn or "")
    except ValueError as exc:
        logger.warning("bucket_namespace_authorization_malformed_arn", namespace_id=namespace_id)
        return api_response(400, {"error": f"sourceBucketArn is malformed: {exc}"})

    tag_key = bucket_namespace_tag_key()
    try:
        tags = get_bucket_tags(get_s3_client(), bucket)
    except ClientError:
        # AWS answered and refused: AccessDenied, NoSuchBucket, and friends. That is
        # a request the caller can act on, so it is a 400 naming what to fix.
        logger.warning("bucket_namespace_authorization_unverifiable", namespace_id=namespace_id, exc_info=True)
        return api_response(
            400,
            {
                "error": (
                    f"sourceBucketArn could not be verified. The bucket must exist and be readable "
                    f"(s3:GetBucketTagging) and carry the tag '{tag_key}' listing this namespace."
                )
            },
        )
    except BotoCoreError:
        # Client-side: DNS, TLS, connection timeout. Nothing about the caller's
        # request is wrong, so a permissions message would misdirect them. Still
        # fails closed — we just say "retry" instead of "fix your tag".
        logger.warning("bucket_namespace_authorization_transient_fault", namespace_id=namespace_id, exc_info=True)
        return api_response(
            503,
            {"error": "Could not verify sourceBucketArn (transient fault reaching S3); retry."},
        )

    if not bucket_grants_namespace(tags, namespace_id):
        logger.warning(
            "bucket_namespace_authorization_rejected",
            namespace_id=namespace_id,
            has_tag=tag_key in tags,
        )
        return api_response(
            400,
            {
                "error": (
                    f"sourceBucketArn is not authorized for this namespace: tag the bucket with "
                    f"'{tag_key}={namespace_id}'. Separate several namespace ids with spaces."
                )
            },
        )
    return None


def _create_document_source(
    doc_req: Any, namespace_id: str, event: dict[str, Any], *, namespace_language: str
) -> dict[str, Any]:
    """Create a DOCUMENTS source. doc_req is a CreateDocumentSourceInput model instance.

    ``namespace_language`` is the canonical language the caller resolved from the
    namespace item; it rides in the ingestion job input as the KG-build snapshot.
    """
    name: str = doc_req.name.strip()
    if not name:
        return api_response(400, {"error": "name is required"})

    source_bucket_arn: str | None = doc_req.source_bucket_arn or None
    role_arn: str | None = doc_req.role_arn or None

    # Infer sub-type: sourceBucketArn → S3, else → LOCAL_UPLOAD
    if source_bucket_arn:
        sub_type = SourceSubType.S3
        doc_source_type = "s3"
    else:
        sub_type = SourceSubType.LOCAL_UPLOAD
        doc_source_type = "upload"

    if doc_source_type == "s3":
        # S3 sub-type: prefixes point into the caller-owned sourceBucketArn, so
        # they are legitimately caller-supplied. Drop empty/whitespace-only
        # entries (a stray "" would otherwise list the whole bucket), then
        # validate character set / traversal only.
        s3_prefixes: list[str] = [s for s in (p.strip() for p in (doc_req.s3_prefixes or [])) if s]
        for prefix in s3_prefixes:
            try:
                validate_s3_prefix(prefix, "s3Prefixes")
            except ValueError as exc:
                return api_response(400, {"error": str(exc)})
        # A caller-named bucket must be authorized by its own owner. Runs after the
        # local prefix checks so a malformed request costs no S3 call. Upload
        # sources read the platform's own bucket, so no customer tag applies.
        error = _validate_bucket_namespace_authorization(source_bucket_arn, namespace_id)
        if error:
            return error
    else:
        # Upload sub-type: files live in the shared platform bucket under a
        # server-issued, namespace-scoped prefix. Reconstruct that prefix from
        # the uploadId minted by GetSourceUploadUrls and ignore any
        # caller-supplied s3Prefixes — otherwise a steward with manageSource on
        # one namespace could point ingestion at another namespace's objects
        # in the shared bucket. uploadId is a UUID-v4-constrained model field;
        # validate_id re-checks it here so the derived prefix can never contain
        # a path separator or traversal segment.
        upload_id: str = (doc_req.upload_id or "").strip()
        if not upload_id:
            return api_response(400, {"error": "uploadId is required for upload sources"})
        try:
            validate_id(upload_id, "uploadId")
        except ValueError as exc:
            return api_response(400, {"error": str(exc)})
        s3_prefixes = [f"{namespace_id}/raw/{upload_id}/"]

    try:
        extraction_config = merge_extraction_config(doc_req.extraction_config)
    except ValueError as exc:
        return api_response(400, {"error": str(exc)})

    # Name uniqueness check via ByName GSI — O(1) lookup matching existing unstructured pattern
    try:
        gsi_result = _get_dao().query(
            QueryParams(
                key_condition="namespaceId = :ns AND #n = :name",
                expression_values={":ns": namespace_id, ":name": name},
                expression_names={"#n": "name"},
                index_name=_BY_NAME_GSI,
                limit=1,
            )
        )
        if gsi_result.items:
            existing = gsi_result.items[0]
            current_status = existing.get("status", "")
            existing_id = _source_id_from_item(existing)
            return api_response(
                409,
                {
                    "error": (f"A document source named '{name}' already exists (status: '{current_status}')."),
                    "sourceId": existing_id,
                    "status": current_status,
                },
            )
    except ClientError:
        logger.exception("ddb_gsi_query_failed")
        return api_response(500, {"error": "Internal server error"})

    source_id = str(uuid.uuid4())
    tenant_id = to_graphrag_tenant_id(namespace_id, source_id)
    now = _now_iso()

    extraction_config_stored = ExtractionConfig.model_validate(extraction_config).model_dump(
        by_alias=True, exclude_none=True
    )

    item: dict[str, Any] = {
        "PK": f"NS#{namespace_id}",
        "SK": f"SRC#{source_id}",
        "sourceId": source_id,
        "namespaceId": namespace_id,
        "name": name,
        "sourceType": SourceType.DOCUMENTS,
        "sourceSubType": sub_type,
        "docSourceType": doc_source_type,
        "status": SourceStatus.REGISTERED,
        "tenantId": tenant_id,
        "s3Prefixes": s3_prefixes,
        "extractionConfig": extraction_config_stored,
        "createdBy": get_caller_identity(event),
        "createdAt": now,
        "updatedAt": now,
        "sourceTypeCreatedAt": f"{SourceType.DOCUMENTS.value}#{now}",
    }
    if source_bucket_arn:
        item["sourceBucketArn"] = source_bucket_arn
    if role_arn:
        item["roleArn"] = role_arn

    try:
        _get_dao().put(item)
    except ClientError:
        logger.exception("ddb_put_failed")
        return api_response(500, {"error": "Internal server error"})

    # Increment the namespace sourceCount. Best-effort: counter drift
    # is undesirable but must never block source creation.
    adjust_namespace_source_count(namespace_id, SourceType.DOCUMENTS, 1)

    if _INGESTION_QUEUE_URL:
        sqs_body: dict[str, Any] = {
            "namespace_id": namespace_id,
            "doc_source_id": source_id,
            "tenant_id": tenant_id,
            "source_type": doc_source_type,
            "s3_prefixes": s3_prefixes,
            "extraction_config": extraction_config,
            "namespace_language": namespace_language,
        }
        if source_bucket_arn:
            sqs_body["source_bucket_arn"] = source_bucket_arn
        if role_arn:
            sqs_body["role_arn"] = role_arn
        try:
            _get_sqs().send_message(
                QueueUrl=_INGESTION_QUEUE_URL,
                MessageBody=json.dumps(sqs_body, default=str),
            )
        except ClientError:
            logger.exception("sqs_send_failed", source_id=source_id)
            try:
                _get_dao().update(
                    {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
                    {"status": SourceStatus.SCAN_FAILED, "errorMessage": "Failed to enqueue ingestion job"},
                )
            except ClientError:
                logger.exception("ddb_update_scan_failed_status_failed", source_id=source_id)
            return api_response(500, {"error": "Internal server error"})

    logger.info("document_source_created", source_id=source_id, namespace_id=namespace_id)
    return api_response(201, _item_to_detail(item))


# ---------------------------------------------------------------------------
# UPLOAD URLS
# ---------------------------------------------------------------------------


def _handle_upload_urls(event: dict[str, Any], namespace_id: str) -> dict[str, Any]:
    """POST /namespaces/{namespaceId}/sources/upload-urls — generate pre-signed S3 PUT URLs."""
    if not _BUCKET_NAME:
        return api_response(500, {"error": "Internal server error"})

    try:
        body: dict[str, Any] = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return api_response(400, {"error": "Invalid JSON in request body"})

    files: list[Any] = body.get("files") or []
    if not files:
        return api_response(400, {"error": "files must not be empty"})
    if len(files) > _MAX_UPLOAD_FILES:
        return api_response(400, {"error": f"Too many files: maximum {_MAX_UPLOAD_FILES} per request"})

    validated: list[dict[str, str]] = []
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            return api_response(400, {"error": f"files[{i}] must be an object with filename and contentType"})
        filename = (f.get("filename") or "").strip()
        content_type = (f.get("contentType") or "").strip()
        if not filename:
            return api_response(400, {"error": f"files[{i}].filename must not be blank"})
        # Decode percent-encoding before checking for path traversal
        decoded = unquote(filename)
        if any(c in decoded for c in ["/", "\\", "\0"]) or ".." in decoded or decoded.startswith("."):
            return api_response(400, {"error": f"files[{i}].filename contains invalid characters"})
        if not content_type:
            return api_response(400, {"error": f"files[{i}].contentType must not be blank"})
        if content_type not in SUPPORTED_UPLOAD_CONTENT_TYPES:
            return api_response(400, {"error": f"files[{i}].contentType '{content_type}' is not supported"})
        validated.append({"filename": filename, "contentType": content_type})

    upload_id = str(uuid.uuid4())
    s3_prefix = f"{namespace_id}/raw/{upload_id}/"
    upload_urls: list[dict[str, str]] = []
    try:
        for entry in validated:
            s3_key = f"{s3_prefix}{entry['filename']}"
            url = _get_s3().generate_presigned_url(
                "put_object",
                Params={"Bucket": _BUCKET_NAME, "Key": s3_key, "ContentType": entry["contentType"]},
                ExpiresIn=_UPLOAD_URL_EXPIRY_SECONDS,
            )
            upload_urls.append({"filename": entry["filename"], "uploadUrl": url})
    except ClientError:
        logger.exception("presigned_url_generation_failed", namespace_id=namespace_id)
        return api_response(500, {"error": "Internal server error"})

    return api_response(
        200,
        {
            "uploadId": upload_id,
            "s3Prefix": s3_prefix,
            "uploadUrls": upload_urls,
            "expiresIn": _UPLOAD_URL_EXPIRY_SECONDS,
        },
    )
