# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Recover asynchronous imports whose messages exhausted worker retries."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import structlog
from coa_common.logging import setup_logging

from coa_metrics.api.import_job_store import (
    UNVERIFIABLE_IMPORT_PROGRESS_ERROR,
    JobOffsetState,
    UnverifiableImportProgressError,
    assess_job_offset,
    fail_job,
    get_job,
    job_chunk_size,
    job_has_active_offset_claim,
    job_metrics_total,
    job_progress_frontier,
)
from coa_metrics.api.import_queue_message import (
    INVALID_IMPORT_QUEUE_MESSAGE_ERROR,
    ImportMessageIdentity,
    InvalidImportQueueMessage,
    message_attributes,
    parse_import_queue_record,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_QUEUE_URL = os.environ.get("IMPORT_QUEUE_URL", "")
_REDRIVE_DELAY_SECONDS = int(os.environ.get("IMPORT_REDRIVE_DELAY_SECONDS", "60"))
_RECOVERY_EXHAUSTED_ERROR = "Import failed after exhausting queue retries and automatic recovery"
_sqs: Any = None


class ActiveImportOffsetError(RuntimeError):
    """DLQ recovery must retry rather than race an active worker lease."""


def _get_sqs() -> Any:
    global _sqs  # noqa: PLW0603
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _sqs


def _fail_attributed_job(
    identity: ImportMessageIdentity,
    error: str,
    *,
    expected_next_offset: int | None = None,
) -> None:
    """Fail only the still-relevant idle frontier observed by this delivery."""
    if expected_next_offset is None:
        failed = fail_job(identity.namespace_id, identity.job_id, error, require_idle=True)
    else:
        failed = fail_job(
            identity.namespace_id,
            identity.job_id,
            error,
            require_idle=True,
            expected_next_offset=expected_next_offset,
        )
    if failed:
        return

    job = get_job(identity.namespace_id, identity.job_id, consistent_read=True)
    if not job or job.get("status") != "IN_PROGRESS":
        return
    if expected_next_offset is not None and job_progress_frontier(job) != expected_next_offset:
        return
    raise ActiveImportOffsetError("cannot fail import job while an offset lease is active for the relevant frontier")


def handler(event: dict[str, Any], context: Any) -> None:
    """Redrive each failed chunk once, then fail its still-active import job."""
    for record in event.get("Records", []):
        try:
            message = parse_import_queue_record(record)
        except InvalidImportQueueMessage as exc:
            if exc.identity is None:
                logger.error("unidentifiable_import_dlq_message", reason=str(exc))
            else:
                logger.error("invalid_import_dlq_message", job_id=exc.identity.job_id, reason=str(exc))
                _fail_attributed_job(exc.identity, INVALID_IMPORT_QUEUE_MESSAGE_ERROR)
            continue

        job = get_job(message.namespace_id, message.job_id, consistent_read=True)
        if not job or job.get("status") != "IN_PROGRESS":
            logger.info("import_dlq_job_not_active", job_id=message.job_id)
            continue
        stored_s3_key = job.get("s3Key")
        if not isinstance(stored_s3_key, str) or stored_s3_key != message.s3_key:
            logger.error("import_dlq_s3_key_mismatch", job_id=message.job_id, s3_key=message.s3_key)
            continue

        metrics_total = job_metrics_total(job)
        chunk_size = job_chunk_size(job, legacy_fallback=message.chunk_size)
        if message.chunk_size != chunk_size:
            logger.error(
                "import_dlq_chunk_size_mismatch",
                job_id=message.job_id,
                chunk_size=message.chunk_size,
            )
            continue
        end_offset = min(message.offset + chunk_size, metrics_total)
        try:
            assessment = assess_job_offset(job, offset=message.offset, end_offset=end_offset)
        except UnverifiableImportProgressError as exc:
            logger.error(
                "import_progress_unverifiable",
                job_id=message.job_id,
                frontier=exc.frontier,
                reason=str(exc),
            )
            _fail_attributed_job(
                message,
                UNVERIFIABLE_IMPORT_PROGRESS_ERROR,
                expected_next_offset=exc.frontier,
            )
            continue
        if assessment.state in (JobOffsetState.STALE, JobOffsetState.INVALID):
            logger.warning(
                "import_dlq_interval_ignored",
                job_id=message.job_id,
                offset=message.offset,
                state=assessment.state,
            )
            continue
        if job_has_active_offset_claim(job):
            raise ActiveImportOffsetError("cannot recover import chunk while an offset lease is active")

        if message.automatic_redrive_count >= 1:
            _fail_attributed_job(
                message,
                _RECOVERY_EXHAUSTED_ERROR,
                expected_next_offset=assessment.frontier,
            )
            continue

        redriven_message = message.model_copy(update={"automatic_redrive_count": 1})
        try:
            _get_sqs().send_message(
                QueueUrl=_QUEUE_URL,
                MessageBody=json.dumps(redriven_message.model_dump(by_alias=True)),
                MessageAttributes=message_attributes(message),
                DelaySeconds=_REDRIVE_DELAY_SECONDS,
            )
        except Exception as exc:
            logger.exception(
                "import_dlq_redrive_send_failed",
                namespace=message.namespace_id,
                job_id=message.job_id,
                offset=message.offset,
                error=str(exc),
            )
            raise
        logger.warning("import_chunk_redriven", job_id=message.job_id, delay_seconds=_REDRIVE_DELAY_SECONDS)
