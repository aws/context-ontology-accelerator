# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pre-Processing Lambda - first step in the Context Ontology Accelerator unstructured ingestion pipeline.

Reads files from one or more source S3 prefixes (our bucket or cross-account),
converts each to clean text based on format, writes the processed content plus
a JSON metadata sidecar to the staging prefix in our bucket, and returns summary
statistics to the Step Functions state machine.

Staging key layout preserves the full source key path to avoid collisions when
multiple prefixes contain files with the same name:
  source key:  reports/2024/summary.pdf
  staging key: <ns>/staging/<ds>/reports/2024/summary.txt
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import boto3
from botocore.exceptions import ClientError
from coa_common import async_boto_config
from coa_common.constants import (
    DEFAULT_MAX_FILE_SIZE_MB,
    SAFE_ID_RE,
    SUPPORTED_EXTENSIONS,
    bucket_grants_namespace,
    bucket_namespace_tag_key,
    validate_id,
    validate_s3_prefix,
)
from coa_common.s3 import (
    get_bucket_tags,
    get_object_metadata_and_tags,
    get_s3_client,
    list_objects,
    parse_bucket_from_arn,
    read_file_bytes,
    upload_file,
)
from coa_common.s3 import (
    upload_json as upload_metadata,
)
from coa_control_plane_server.models.source_status import SourceStatus

from coa_sources.documents.preprocessing.processors import (
    _PROCESSORS,
    get_page_count,
    process_pdf,
)

# ---------------------------------------------------------------------------
# Logging - structured JSON output for CloudWatch
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        for key in (
            "namespace_id",
            "doc_source_id",
            "doc_filename",
            "s3_key",
            "extension",
            "bucket",
            "key",
            "page",
            "total_pages",
            "page_count",
            "total_chars",
            "avg_chars_per_page",
            "threshold",
            "elements",
            "char_count",
            "processing_method",
            "error",
            "size_bytes",
            "limit_mb",
        ):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())

logger = logging.getLogger()
logger.handlers.clear()
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Environment variable validation — fail fast at cold start
# ---------------------------------------------------------------------------


def _require_env(key: str) -> str:
    """Return the value of a required environment variable or raise at cold start."""
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key}. Ensure the Lambda function is configured correctly."
        )
    return value


# ---------------------------------------------------------------------------
# File size limit
# ---------------------------------------------------------------------------

try:
    _MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", str(DEFAULT_MAX_FILE_SIZE_MB)))
    if _MAX_FILE_SIZE_MB <= 0:
        raise ValueError("MAX_FILE_SIZE_MB must be a positive integer")
except ValueError as _e:
    logger.warning(
        "Invalid MAX_FILE_SIZE_MB value, falling back to default %d MB: %s",
        DEFAULT_MAX_FILE_SIZE_MB,
        _e,
    )
    _MAX_FILE_SIZE_MB = DEFAULT_MAX_FILE_SIZE_MB

MAX_FILE_SIZE_BYTES = _MAX_FILE_SIZE_MB * 1024 * 1024

# Validate required env vars at cold start so misconfiguration fails immediately
_BUCKET_NAME = _require_env("BUCKET_NAME")
_DOC_SOURCES_TABLE = _require_env("DOC_SOURCES_TABLE")
_ROLE_PREFIX = os.environ.get("CROSS_ACCOUNT_ROLE_PREFIX", "coa")

# Cap on the per-file issue entries carried inline in the Step Functions state
# payload. The full list is one entry per skipped/errored object and is
# unbounded, so a heterogeneous source can push the Lambda's returned result
# past the Step Functions 256 KB limit and fail the whole execution (issue 104).
_ISSUES_PREVIEW_MAX = 20

# The preview bounds per-entry BYTES too, not just the entry count. An error
# entry's reason is str(exc) (unbounded) and filename is the full S3 key (up to
# 1 KB, more once json escapes non-ASCII to \uXXXX), so 20 entries with multi-KB
# reasons would still cross the 256 KB state limit from the byte axis. Cap both
# fields; the full untruncated text stays in the S3 report. Worst case with
# all-non-ASCII escaping is ~(256*6 + 500*6) ≈ 4.5 KB/entry → ~90 KB for 20,
# provably under the limit.
_PREVIEW_FILENAME_MAX = 256
_PREVIEW_REASON_MAX = 500


def _bounded_issues(issues: list[dict[str, str]], *, namespace_id: str, doc_source_id: str) -> dict[str, Any]:
    """Bound the issues list for the Step Functions state payload (issue 104).

    Returns a capped ``issues_preview`` inline, an ``issues_truncated`` flag, and
    — only when truncated — an ``issues_s3_key`` pointing at the complete report
    persisted under the platform bucket. Persistence is best-effort: a failure is
    logged and leaves ``issues_s3_key`` empty, but the payload stays bounded and
    the job never fails over a diagnostics-write miss. ``issues_s3_key`` is an
    empty string (never null) when absent, so the state machine's DynamoDB write
    can treat it as a plain string.
    """
    preview = [
        {
            "filename": i.get("filename", "")[:_PREVIEW_FILENAME_MAX],
            "type": i.get("type", ""),
            "reason": i.get("reason", "")[:_PREVIEW_REASON_MAX],
        }
        for i in issues[:_ISSUES_PREVIEW_MAX]
    ]
    truncated = len(issues) > _ISSUES_PREVIEW_MAX
    s3_key = ""
    # Persist the full report only when truncated AND the identifiers are safe
    # for an S3 key. The large-list path is only reached after the ids passed
    # validate_id, but this guard makes id-safety non-negotiable rather than a
    # caller invariant: an id carrying a path separator (a would-be traversal)
    # can never reach put_object, whatever future caller passes it.
    if truncated and SAFE_ID_RE.match(namespace_id) and SAFE_ID_RE.match(doc_source_id):
        # Millisecond precision + a short random suffix so two scans of one
        # source in the same second cannot overwrite each other's report. (The
        # Step Functions execution id would work too if the state machine passed
        # it into the Lambda payload.)
        key = (
            f"{namespace_id}/scan-results/{doc_source_id}/{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}-issues.json"
        )
        try:
            get_s3_client().put_object(
                Bucket=_BUCKET_NAME,
                Key=key,
                Body=json.dumps(issues).encode("utf-8"),
                ContentType="application/json",
            )
            s3_key = key
        except Exception as exc:
            # Best-effort: never fail the job over a diagnostics write. Catch
            # broadly (a ClientError like AccessDenied AND a BotoCoreError like a
            # connection timeout must both be swallowed), but surface the error
            # code so operators can tell a retryable throttle from a permanent
            # permissions/config fault.
            code = (
                exc.response.get("Error", {}).get("Code", "Unknown")
                if isinstance(exc, ClientError)
                else type(exc).__name__
            )
            logger.error("Failed to persist full issues report to s3://%s/%s [%s]: %s", _BUCKET_NAME, key, code, exc)
    return {
        "issues_preview": preview,
        "issues_truncated": truncated,
        "issues_s3_key": s3_key,
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """Lambda entry-point invoked by the Step Functions state machine."""
    start_ts = time.time()

    # -- Extract & validate inputs -----------------------------------------
    namespace_id: str = event.get("namespace_id", "")
    doc_source_id: str = event.get("doc_source_id", "")
    # s3_prefixes: list of prefixes to ingest. Empty list means whole bucket.
    s3_prefixes: list[str] = event.get("s3_prefixes") or []
    source_bucket_arn: str | None = event.get("source_bucket_arn")
    role_arn: str | None = event.get("role_arn")
    # extraction_config is stringified by the trigger Lambda so it can ride the
    # state-machine → Lambda-invoke pipe; only booleans need parsing here (the
    # rest are consumed downstream by the KG-build container).
    ec = event.get("extraction_config") or {}
    enable_table_extraction = str(ec.get("enable_table_extraction", "false")).lower() == "true"

    try:
        validate_id(namespace_id, "namespace_id")
        validate_id(doc_source_id, "doc_source_id")
        for prefix in s3_prefixes:
            validate_s3_prefix(prefix, "s3_prefixes")
        # Defense-in-depth: upload-type sources read from the
        # shared platform bucket, so a prefix must be scoped to this job's
        # namespace. The create path already reconstructs the prefix server-side
        # as {namespace_id}/raw/{uploadId}/; this guard is a second line that
        # refuses a foreign-namespace prefix if a job ever reaches here without
        # going through create. S3-type prefixes live in the customer's own
        # bucket (source_bucket_arn set) and are exempt. Mirrors
        # deletion/cleanup_handler.py. The reason is intentionally generic so it
        # does not echo the offending prefix back through GetSource.
        # (An empty prefix — whole shared bucket — cannot occur for upload type:
        # create always derives a concrete non-empty prefix, so it is not a
        # reachable path and is not special-cased here.)
        if not source_bucket_arn:
            for prefix in s3_prefixes:
                if not prefix.startswith(f"{namespace_id}/"):
                    raise ValueError("source prefix is not scoped to the namespace")
        # Cross-account role names must follow the deployment's naming
        # convention. Validated here (inside the try) so a non-conforming name
        # returns a structured SCAN_FAILED instead of raising uncaught and
        # failing the Lambda. The message names the required prefix only, not
        # the caller-supplied role name.
        if source_bucket_arn and role_arn:
            role_name = role_arn.rsplit("/", 1)[-1] if "/" in role_arn else ""
            if not role_name.startswith(f"{_ROLE_PREFIX}-"):
                raise ValueError(f"role_arn role name must start with '{_ROLE_PREFIX}-'")
    except ValueError as exc:
        logger.error("Input validation failed: %s", exc)
        return {
            "status": SourceStatus.SCAN_FAILED,
            "doc_source_id": doc_source_id,
            "namespace_id": namespace_id,
            "staging_prefix": "",
            "files_total": 0,
            "files_preprocessed": 0,
            "files_skipped": 0,
            "files_errored": 0,
            **_bounded_issues(
                [{"filename": "", "type": "error", "reason": str(exc)}],
                namespace_id=namespace_id,
                doc_source_id=doc_source_id,
            ),
            "elapsed_seconds": 0,
        }

    staging_prefix = f"{namespace_id}/staging/{doc_source_id}/"
    our_bucket = _BUCKET_NAME

    logger.info(
        "Preprocessing started",
        extra={
            "namespace_id": namespace_id,
            "doc_source_id": doc_source_id,
            "s3_prefixes": s3_prefixes,
            "staging_prefix": staging_prefix,
            "cross_account": bool(source_bucket_arn),
        },
    )

    # -- Determine source bucket & S3 clients ------------------------------
    if source_bucket_arn:
        source_bucket = parse_bucket_from_arn(source_bucket_arn)
        # Role-name convention is validated above (inside the input-validation
        # try block) so a bad name returns a structured error, not a crash.
        #
        # The bucket is caller-named, and holding manageSource on a namespace says
        # nothing about whether the caller may read it. The bucket's own tag is the
        # authorization: setting it takes a bucket-level write permission, so its
        # presence is the owner's consent. Read it with the ambient client — we
        # cannot know whether we may assume anything until we know the bucket is
        # authorized at all, and this reads metadata, not data.
        #
        # Checked here as well as at registration because this handler is a
        # separate entry point; the sibling deletion path guards the same way
        # (cleanup_handler.py refuses a prefix outside its own namespace).
        #
        # Deliberately NOT moved into the input-validation try above, unlike the
        # role-name check: this makes an S3 call, and a throttle or transport fault
        # there must stay retryable rather than being converted into a terminal
        # SCAN_FAILED. An unauthorized bucket raises, which the state machine's
        # Catch turns into SCAN_FAILED.
        bucket_tags = get_bucket_tags(get_s3_client(), source_bucket)
        if not bucket_grants_namespace(bucket_tags, namespace_id):
            raise ValueError(
                f"bucket {source_bucket!r} does not authorize namespace {namespace_id}: "
                f"tag the bucket {bucket_namespace_tag_key()}=<namespaceId> "
                f"(space-separated for several namespaces)"
            )
        source_s3 = get_s3_client(role_arn=role_arn)
    else:
        source_bucket = our_bucket
        source_s3 = get_s3_client()

    dest_s3 = get_s3_client()
    textract_client: Any | None = None

    # -- List source files across all prefixes -----------------------------
    # Empty s3_prefixes means ingest the whole bucket (prefix="").
    effective_prefixes = s3_prefixes if s3_prefixes else [""]
    source_files: list[dict] = []
    for prefix in effective_prefixes:
        try:
            source_files.extend(list_objects(source_s3, source_bucket, prefix))
        except Exception as exc:
            logger.error("Failed to list source files for prefix %r: %s", prefix, exc, exc_info=True)
            return {
                "status": SourceStatus.SCAN_FAILED,
                "doc_source_id": doc_source_id,
                "namespace_id": namespace_id,
                "staging_prefix": staging_prefix,
                "files_total": 0,
                "files_preprocessed": 0,
                "files_skipped": 0,
                "files_errored": 0,
                **_bounded_issues(
                    [{"filename": "", "type": "error", "reason": "Failed to list source files"}],
                    namespace_id=namespace_id,
                    doc_source_id=doc_source_id,
                ),
                "elapsed_seconds": round(time.time() - start_ts, 2),
            }

    # Deduplicate in case prefixes overlap (e.g. "" and "reports/")
    seen_keys: set[str] = set()
    unique_files: list[dict] = []
    for f in source_files:
        if f["Key"] not in seen_keys:
            seen_keys.add(f["Key"])
            unique_files.append(f)
    source_files = unique_files

    logger.info(
        "Found %d files across %d prefix(es)",
        len(source_files),
        len(effective_prefixes),
        extra={"namespace_id": namespace_id, "doc_source_id": doc_source_id},
    )

    # -- Process each file --------------------------------------------------
    files_preprocessed = 0
    files_skipped = 0
    issues: list[dict[str, str]] = []
    files_total = len(source_files)

    for file_obj in source_files:
        source_key: str = file_obj["Key"]
        filename = PurePosixPath(source_key).name
        ext = PurePosixPath(filename).suffix.lower()

        log_extra = {
            "namespace_id": namespace_id,
            "doc_source_id": doc_source_id,
            "doc_filename": filename,
        }

        try:
            # 0. Check supported extension -------------------------
            if ext not in SUPPORTED_EXTENSIONS:
                logger.info("Unsupported extension, skipping", extra={**log_extra, "extension": ext})
                files_skipped += 1
                issues.append(
                    {
                        "filename": source_key,
                        "type": "skipped",
                        "reason": f"Unsupported format: {ext}. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
                    }
                )
                continue

            # 1. Check file size BEFORE reading ------------------------
            file_size = file_obj.get("Size", 0)
            if file_size > MAX_FILE_SIZE_BYTES:
                reason = f"Exceeds {_MAX_FILE_SIZE_MB} MB file size limit (size: {file_size / (1024 * 1024):.1f} MB)"
                logger.warning(
                    "File exceeds size limit, skipping",
                    extra={**log_extra, "size_bytes": file_size, "limit_mb": _MAX_FILE_SIZE_MB},
                )
                files_skipped += 1
                issues.append({"filename": source_key, "type": "skipped", "reason": reason})
                continue

            # 2. Read --------------------------------------------------
            logger.info("Read file", extra=log_extra)
            content_bytes = read_file_bytes(source_s3, source_bucket, source_key)

            # 3. Get page count (PDF only) ---------------------------------
            page_count = get_page_count(content_bytes, ext)

            # 4. Process ---------------------------------------------------
            if ext == ".pdf":
                if textract_client is None:
                    textract_client = boto3.client("textract", config=async_boto_config())
                processed_text, out_ext = process_pdf(
                    content_bytes,
                    filename,
                    textract_client,
                    enable_table_extraction=enable_table_extraction,
                )
                if enable_table_extraction:
                    processing_method = "textract_analyze_document_tables"
                else:
                    processing_method = "textract" if out_ext == ".txt" else "unstructured_partition_pdf"
            elif ext in _PROCESSORS:
                processed_text, out_ext = _PROCESSORS[ext](content_bytes, filename)
                processing_method = (
                    "passthrough" if ext in {".txt", ".md"} else f"unstructured_partition_{ext.lstrip('.')}"
                )
            else:
                # Defensive: shouldn't happen
                logger.warning("No processor for supported extension, skipping", extra={**log_extra, "extension": ext})
                files_skipped += 1
                issues.append({"filename": source_key, "type": "skipped", "reason": f"No processor for {ext}"})
                continue

            # 4b. Refuse to stage an empty extraction.
            #
            # Every processor above can return "" while raising nothing:
            # `unstructured` abandons text extraction on drawing-heavy pages
            # ("exceeds thresholds ... falling back to hi_res strategy without
            # text extraction") and yields zero characters, and the passthrough
            # and Textract paths do the same for an empty upload or a page with
            # no LINE blocks. That used to be written to staging as a 0-byte file
            # and counted as a success, so the source reported Skipped=0/Errored=0
            # and Completed while KG build silently dropped it — 39 of 78 files
            # vanished that way with nothing in the UI to show it. Count it as
            # skipped and say which file and how it was read.
            if not processed_text.strip():
                logger.warning(
                    "Extraction produced no text, skipping file",
                    extra={**log_extra, "processing_method": processing_method},
                )
                files_skipped += 1
                issues.append(
                    {
                        "filename": source_key,
                        "type": "skipped",
                        "reason": f"No text extracted ({processing_method}).",
                    }
                )
                continue

            # 5. Determine output key — preserve full source path to avoid
            #    collisions when multiple prefixes contain same-named files.
            #    e.g. reports/2024/doc.pdf → <staging>/reports/2024/doc.txt
            source_path = PurePosixPath(source_key)
            stem = source_path.stem
            out_filename = f"{stem}{out_ext}"
            # Reconstruct the relative path with the new extension
            relative_dir = str(source_path.parent) if str(source_path.parent) != "." else ""
            if relative_dir:
                staging_key = f"{staging_prefix}{relative_dir}/{out_filename}"
                metadata_key = f"{staging_prefix}{relative_dir}/{stem}.metadata.json"
            else:
                staging_key = f"{staging_prefix}{out_filename}"
                metadata_key = f"{staging_prefix}{stem}.metadata.json"

            # 6. Upload processed content ----------------------------------
            upload_file(dest_s3, our_bucket, staging_key, processed_text)

            # 7. Build & upload metadata sidecar ---------------------------
            char_count = len(processed_text)
            s3_user_metadata, s3_tags = get_object_metadata_and_tags(source_s3, source_bucket, source_key)
            metadata = {
                "original_format": ext,
                "staging_format": out_ext,
                "source_s3_key": source_key,
                "doc_source_id": doc_source_id,
                "namespace_id": namespace_id,
                "page_count": page_count,
                "char_count": char_count,
                "processing_method": processing_method,
                # Provenance fields (SDO-188 §4 data card/BOM)
                "provenance": {
                    "source_s3_key": f"s3://{source_bucket}/{source_key}",
                    "uploader_identity": s3_tags.get("coa:uploader", s3_user_metadata.get("uploader", "unknown")),
                    "ingestion_timestamp": datetime.now(UTC).isoformat(),
                    "content_hash_sha256": hashlib.sha256(content_bytes).hexdigest(),
                },
            }
            # Flatten S3 user metadata and tags with prefixed keys so the
            # sidecar structure matches what gets passed to GraphRAG directly.
            # Use underscores (not dots) so keys are valid OpenCypher property names.
            for k, v in s3_user_metadata.items():
                safe_k = k.replace("-", "_")
                metadata[f"s3_metadata_{safe_k}"] = str(v)
            for k, v in s3_tags.items():
                safe_k = k.replace("-", "_")
                metadata[f"s3_tags_{safe_k}"] = str(v)
            upload_metadata(dest_s3, our_bucket, metadata_key, metadata)

            files_preprocessed += 1
            logger.info(
                "File processed successfully",
                extra={**log_extra, "char_count": char_count},
            )

        except Exception as exc:
            error_msg = f"Error processing {filename}: {exc}"
            logger.error(error_msg, extra=log_extra, exc_info=True)
            issues.append({"filename": source_key, "type": "error", "reason": str(exc)})
            # Continue with remaining files

    elapsed = round(time.time() - start_ts, 2)
    files_errored = sum(1 for i in issues if i["type"] == "error")

    # Determine outcome: SCAN_FAILED if files existed but none succeeded,
    # SCANNING if some succeeded (KG Build still to run).
    # SavePreprocessingResults writes this status directly to DDB.
    all_failed = files_total > 0 and files_preprocessed == 0
    status = SourceStatus.SCAN_FAILED if all_failed else SourceStatus.SCANNING

    logger.info(
        "Preprocessing complete",
        extra={
            "namespace_id": namespace_id,
            "doc_source_id": doc_source_id,
            "files_total": files_total,
            "files_preprocessed": files_preprocessed,
            "files_skipped": files_skipped,
            "files_errored": files_errored,
            "status": status,
            "elapsed_seconds": elapsed,
        },
    )

    return {
        "status": status,
        "doc_source_id": doc_source_id,
        "namespace_id": namespace_id,
        "staging_prefix": staging_prefix,
        "files_total": files_total,
        "files_preprocessed": files_preprocessed,
        "files_skipped": files_skipped,
        "files_errored": files_errored,
        **_bounded_issues(issues, namespace_id=namespace_id, doc_source_id=doc_source_id),
        "elapsed_seconds": elapsed,
    }
