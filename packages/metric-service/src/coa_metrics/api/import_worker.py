# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQS worker Lambda for asynchronous metric imports."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import boto3
import structlog
from coa_common.logging import setup_logging

from coa_metrics.api.import_job_store import (
    UNVERIFIABLE_IMPORT_PROGRESS_ERROR,
    MetricDisposition,
    OffsetClaimState,
    OffsetPlan,
    OffsetPlanReference,
    UnverifiableImportProgressError,
    claim_job_offset,
    complete_job,
    fail_claimed_job,
    fail_job,
    finalize_job_offset,
    get_job,
    job_chunk_size,
    job_metrics_total,
    job_next_offset,
    job_progress_frontier,
    store_job_offset_plan,
)
from coa_metrics.api.import_osi import _get_lookup, _osi_metric_to_definition
from coa_metrics.api.import_plan_payload import (
    OffsetPlanPayload,
    PlannedMetric,
    canonical_plan_payload_bytes,
    parse_plan_payload,
    plan_payload_sha256,
)
from coa_metrics.api.import_queue_message import (
    INVALID_IMPORT_QUEUE_MESSAGE_ERROR,
    ImportQueueMessage,
    InvalidImportQueueMessage,
    message_attributes,
    parse_import_queue_record,
)
from coa_metrics.dataset_resolver import resolve_datasets
from coa_metrics.neptune_client import InvalidMetricDefinitionError, MetricDefinition, MetricNeptuneClient
from coa_metrics.osi_parser import OsiDocument, OsiMetric, parse_osi_yaml
from coa_metrics.source_status import (
    SourceValidationUnavailableError,
    check_source_approved,
    check_source_table_exists,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

_QUEUE_URL = os.environ.get("IMPORT_QUEUE_URL", "")
_BUCKET = os.environ.get("OSI_BUCKET_NAME", "")
_OFFSET_LEASE_SECONDS = int(os.environ.get("IMPORT_OFFSET_LEASE_SECONDS", "960"))

_neptune: MetricNeptuneClient | None = None
_s3 = None
_sqs = None


class OffsetLeaseHeldError(RuntimeError):
    """A retryable delivery encountered another active worker's offset lease."""


class InvalidImportSourceError(ValueError):
    """A pinned import source is deterministically invalid for its job."""


def _get_neptune() -> MetricNeptuneClient:
    global _neptune  # noqa: PLW0603
    if _neptune is None:
        _neptune = MetricNeptuneClient()
    return _neptune


def _get_s3():
    global _s3  # noqa: PLW0603
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _s3


def _get_sqs():
    global _sqs  # noqa: PLW0603
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _sqs


def _fail_invalid_message(identity, reason: str) -> None:
    if identity is None:
        logger.error("unidentifiable_import_queue_message", reason=reason)
        return

    if fail_job(
        identity.namespace_id,
        identity.job_id,
        INVALID_IMPORT_QUEUE_MESSAGE_ERROR,
        require_idle=True,
    ):
        logger.error("invalid_import_queue_message", job_id=identity.job_id, reason=reason)
        return

    job = get_job(identity.namespace_id, identity.job_id, consistent_read=True)
    if job and job.get("status") == "IN_PROGRESS":
        raise OffsetLeaseHeldError("cannot fail invalid queue message while an import offset lease is active")


def handler(event: dict[str, Any], context: Any) -> None:
    """Process validated SQS records one chunk at a time."""
    for record in event.get("Records", []):
        try:
            message = parse_import_queue_record(record)
        except InvalidImportQueueMessage as exc:
            _fail_invalid_message(exc.identity, str(exc))
            continue
        _process_chunk(message)


def _send_continuation(message: ImportQueueMessage, next_offset: int) -> None:
    continuation = message.model_copy(
        update={"offset": next_offset, "automatic_redrive_count": 0},
    )
    try:
        _get_sqs().send_message(
            QueueUrl=_QUEUE_URL,
            MessageBody=json.dumps(continuation.model_dump(by_alias=True)),
            MessageAttributes=message_attributes(message),
        )
    except Exception as exc:
        # Let the source queue retry this already-accounted offset. Its replay
        # resumes dispatch without repeating Neptune writes.
        logger.exception("import_continuation_failed", job_id=message.job_id, next_offset=next_offset, error=str(exc))
        raise
    logger.info("import_continuation_sent", job_id=message.job_id, next_offset=next_offset)


def _resume_post_accounting(message: ImportQueueMessage, metrics_total: int, end_offset: int) -> None:
    """Repeat only the idempotent transition after the latest accounted chunk."""
    if end_offset < metrics_total:
        _send_continuation(message, end_offset)
        return

    if complete_job(
        message.namespace_id,
        message.job_id,
        status="COMPLETED",
        expected_next_offset=end_offset,
    ):
        return

    job = get_job(message.namespace_id, message.job_id, consistent_read=True)
    observed_status = job.get("status") if job else "NOT_FOUND"
    logger.warning(
        "import_legacy_completion_state_observed",
        namespace=message.namespace_id,
        job_id=message.job_id,
        offset=message.offset,
        observed_status=observed_status,
    )
    if observed_status == "IN_PROGRESS":
        raise RuntimeError("import job remained IN_PROGRESS after its legacy completion transition")


def _resolve_ontology_concepts(
    namespace_id: str,
    metric_definition: MetricDefinition,
    neptune: MetricNeptuneClient,
) -> None:
    """Resolve a definition's ontology concepts before it is written."""
    if metric_definition.ontology_concepts:
        metric_definition.ontology_concepts = neptune.resolve_class_uris(
            namespace_id,
            metric_definition.ontology_concepts,
        )


def _overwrite_warning(metric_name: str, existing: MetricDefinition) -> str:
    """Return the durable warning for a metric that existed during planning."""
    author = existing.defined_by
    author_detail = f" authored by {author.strip()}" if isinstance(author, str) and author.strip() else ""
    return f"Metric '{metric_name}' overwritten (had existing metadata{author_detail})"


def _duplicate_overwrite_warning(metric_name: str) -> str:
    """Return the deterministic warning for an ordered duplicate definition."""
    return f"Metric '{metric_name}' overwritten by a later definition in the same import"


def _durable_error(index: int, osi_metric: OsiMetric, error: ValueError) -> PlannedMetric:
    message = f"{osi_metric.name}: {error}"
    logger.warning("metric_import_error", name=osi_metric.name, error=str(error))
    return PlannedMetric(
        source_index=index,
        name=osi_metric.name,
        disposition=MetricDisposition.ERROR,
        error=message,
    )


def _validate_source_reference(
    namespace_id: str,
    metric_definition: MetricDefinition,
    approval_results: dict[str, str | None],
    table_results: dict[tuple[str, str], str | None],
) -> None:
    """Enforce source approval and table existence once per chunk reference."""
    data_source_id = metric_definition.data_source_id
    if data_source_id not in approval_results:
        approval_results[data_source_id] = check_source_approved(namespace_id, data_source_id)
    source_error = approval_results[data_source_id]
    if source_error is None:
        table_key = (data_source_id, metric_definition.source_table.lower())
        if table_key not in table_results:
            table_results[table_key] = check_source_table_exists(
                namespace_id,
                data_source_id,
                metric_definition.source_table,
            )
        source_error = table_results[table_key]
    if source_error:
        raise ValueError(source_error)


def _build_offset_plan(
    namespace_id: str,
    offset: int,
    chunk: list[OsiMetric],
    document: OsiDocument,
    neptune: MetricNeptuneClient,
) -> OffsetPlanPayload:
    """Choose logical outcomes and exact write payloads before any mutation."""
    entries: list[PlannedMetric] = []
    planned_names: set[str] = set()
    approval_results: dict[str, str | None] = {}
    table_results: dict[tuple[str, str], str | None] = {}

    for relative_index, osi_metric in enumerate(chunk):
        source_index = offset + relative_index
        try:
            metric_definition = _osi_metric_to_definition(osi_metric, document, "import-worker")
            _validate_source_reference(namespace_id, metric_definition, approval_results, table_results)
        except ValueError as exc:
            entries.append(_durable_error(source_index, osi_metric, exc))
            continue

        try:
            # Validate the metric URI and any direct concept IRIs before the
            # first possible Neptune query. Resolve labels, then validate the
            # exact final write shape a second time.
            neptune.validate_metric_definition(namespace_id, metric_definition)
            _resolve_ontology_concepts(namespace_id, metric_definition, neptune)
            neptune.validate_metric_definition(namespace_id, metric_definition)
        except InvalidMetricDefinitionError as exc:
            entries.append(_durable_error(source_index, osi_metric, exc))
            continue

        if metric_definition.name in planned_names:
            disposition = MetricDisposition.UPDATE
            warning = _duplicate_overwrite_warning(metric_definition.name)
        else:
            existing = neptune.get_metric(namespace_id, metric_definition.name)
            if existing is None:
                disposition = MetricDisposition.CREATE
                warning = None
            else:
                disposition = MetricDisposition.UPDATE
                warning = _overwrite_warning(metric_definition.name, existing)
            planned_names.add(metric_definition.name)

        entries.append(
            PlannedMetric(
                source_index=source_index,
                name=metric_definition.name,
                disposition=disposition,
                definition=metric_definition,
                warning=warning,
            )
        )

    return OffsetPlanPayload(offset=offset, entries=tuple(entries))


def _validate_legacy_offset_plan(plan: OffsetPlan, offset: int, chunk: list[OsiMetric]) -> None:
    """Fail closed when a recovered v1 plan does not describe the pinned chunk."""
    if plan.offset != offset:
        raise RuntimeError(f"durable offset plan targets offset {plan.offset}, not current offset {offset}")
    if len(plan.entries) != len(chunk):
        raise RuntimeError(
            f"durable offset plan has {len(plan.entries)} entries for a current chunk of {len(chunk)} metrics"
        )
    for index, (entry, osi_metric) in enumerate(zip(plan.entries, chunk, strict=True)):
        if entry.name != osi_metric.name:
            raise RuntimeError(
                f"durable offset plan entry {index} names {entry.name!r}, not current metric {osi_metric.name!r}"
            )


def _rebuild_legacy_plan(
    namespace_id: str,
    plan: OffsetPlan,
    chunk: list[OsiMetric],
    document: OsiDocument,
    neptune: MetricNeptuneClient,
) -> OffsetPlanPayload:
    """Compatibility path for disposition-only checkpoints created before schema v2."""
    entries: list[PlannedMetric] = []
    approval_results: dict[str, str | None] = {}
    table_results: dict[tuple[str, str], str | None] = {}
    for relative_index, (entry, osi_metric) in enumerate(zip(plan.entries, chunk, strict=True)):
        source_index = plan.offset + relative_index
        if entry.disposition == MetricDisposition.ERROR:
            entries.append(
                PlannedMetric(
                    source_index=source_index,
                    name=entry.name,
                    disposition=entry.disposition,
                    error=entry.error,
                )
            )
            continue
        try:
            definition = _osi_metric_to_definition(osi_metric, document, "import-worker")
            _validate_source_reference(namespace_id, definition, approval_results, table_results)
        except ValueError as exc:
            raise RuntimeError(f"metric {entry.name!r} no longer matches its legacy durable offset plan") from exc
        try:
            neptune.validate_metric_definition(namespace_id, definition)
            _resolve_ontology_concepts(namespace_id, definition, neptune)
            neptune.validate_metric_definition(namespace_id, definition)
        except InvalidMetricDefinitionError as exc:
            raise RuntimeError(f"metric {entry.name!r} no longer matches its legacy durable offset plan") from exc
        entries.append(
            PlannedMetric(
                source_index=source_index,
                name=entry.name,
                disposition=entry.disposition,
                definition=definition,
                warning=entry.warning,
            )
        )
    logger.warning("legacy_offset_plan_rebuilt", namespace=namespace_id, offset=plan.offset)
    return OffsetPlanPayload(offset=plan.offset, entries=tuple(entries))


def _checkpoint_prefix(namespace_id: str, job_id: str) -> str:
    return f"{namespace_id}/imports/checkpoints/{job_id}/"


def _store_plan_payload(
    namespace_id: str,
    job_id: str,
    claim_token: str,
    plan: OffsetPlanPayload,
) -> OffsetPlanReference:
    """Store canonical plan bytes and return a compact version-pinned reference."""
    payload = canonical_plan_payload_bytes(plan)
    payload_key = f"{_checkpoint_prefix(namespace_id, job_id)}{plan.offset}/{claim_token}.json"
    result = _get_s3().put_object(
        Bucket=_BUCKET,
        Key=payload_key,
        Body=payload,
        ContentType="application/json",
    )
    version_id = result.get("VersionId") if isinstance(result, Mapping) else None
    if not isinstance(version_id, str) or not version_id.strip():
        raise RuntimeError("versioned offset-plan write did not return VersionId")
    return OffsetPlanReference(
        offset=plan.offset,
        entry_count=len(plan.entries),
        payload_key=payload_key,
        payload_version_id=version_id,
        payload_sha256=plan_payload_sha256(payload),
        payload_bytes=len(payload),
    )


def _checkpoint_offset_plan(
    namespace_id: str,
    job_id: str,
    *,
    offset: int,
    claim_token: str,
    plan: OffsetPlanPayload,
) -> bool:
    """Write the exact payload, then publish its compact fenced reference."""
    reference = _store_plan_payload(namespace_id, job_id, claim_token, plan)
    return store_job_offset_plan(
        namespace_id,
        job_id,
        offset=offset,
        claim_token=claim_token,
        plan=reference,
    )


def _load_plan_payload(namespace_id: str, job_id: str, reference: OffsetPlanReference) -> OffsetPlanPayload:
    """Load and verify the exact checkpoint bytes before any Neptune call."""
    expected_prefix = _checkpoint_prefix(namespace_id, job_id)
    if not reference.payload_key.startswith(expected_prefix):
        raise ValueError("offset plan payload key is outside the job checkpoint namespace")

    response = _get_s3().get_object(
        Bucket=_BUCKET,
        Key=reference.payload_key,
        VersionId=reference.payload_version_id,
    )
    response_version_id = response.get("VersionId") if isinstance(response, Mapping) else None
    if response_version_id != reference.payload_version_id:
        raise ValueError("offset plan payload VersionId does not match its durable reference")
    body = response.get("Body") if isinstance(response, Mapping) else None
    if body is None or not hasattr(body, "read"):
        raise ValueError("offset plan payload response did not contain a readable Body")
    payload = body.read()
    if not isinstance(payload, bytes):
        raise ValueError("offset plan payload body must be bytes")
    if len(payload) != reference.payload_bytes:
        raise ValueError("offset plan payload byte length does not match its durable reference")
    if plan_payload_sha256(payload) != reference.payload_sha256:
        raise ValueError("offset plan payload digest does not match its durable reference")

    plan = parse_plan_payload(payload)
    if plan.offset != reference.offset:
        raise ValueError("offset plan payload offset does not match its durable reference")
    if len(plan.entries) != reference.entry_count:
        raise ValueError("offset plan payload entry count does not match its durable reference")
    return plan


def _apply_offset_plan(
    namespace_id: str,
    plan: OffsetPlanPayload,
    neptune: MetricNeptuneClient,
) -> tuple[int, int, list[str], list[str]]:
    """Converge physical state while accounting from the immutable logical plan."""
    created = 0
    updated = 0
    errors: list[str] = []
    warnings: list[str] = []

    for entry in plan.entries:
        if entry.disposition == MetricDisposition.ERROR:
            if entry.error is None:
                raise RuntimeError(f"durable ERROR plan entry {entry.name!r} has no error")
            errors.append(entry.error)
            continue
        if entry.definition is None:
            raise RuntimeError(f"durable plan entry {entry.name!r} has no metric definition")

        existing = neptune.get_metric(namespace_id, entry.definition.name)
        if existing is None:
            neptune.create_metric(namespace_id, entry.definition)
        else:
            neptune.update_metric(namespace_id, entry.definition.name, entry.definition)

        if entry.disposition == MetricDisposition.CREATE:
            created += 1
        elif entry.disposition == MetricDisposition.UPDATE:
            if entry.warning is None:
                raise RuntimeError(f"durable UPDATE plan entry {entry.name!r} has no warning")
            updated += 1
            warnings.append(entry.warning)

    return created, updated, errors, warnings


def _expected_entry_count(offset: int, chunk_size: int, metrics_total: int) -> int:
    if offset >= metrics_total:
        raise ValueError("import queue offset is outside the job metric range")
    return min(chunk_size, metrics_total - offset)


def _load_source_document(job: Mapping[str, Any], s3_key: str, metrics_total: int) -> OsiDocument:
    """Load the job's exact source version and validate its immutable metric count."""
    get_kwargs: dict[str, str] = {"Bucket": _BUCKET, "Key": s3_key}
    source_version_id = job.get("s3VersionId")
    if source_version_id is not None:
        if not isinstance(source_version_id, str) or not source_version_id.strip():
            raise InvalidImportSourceError("import job s3VersionId must be a non-empty string")
        get_kwargs["VersionId"] = source_version_id
    else:
        logger.warning("legacy_import_source_unversioned", s3_key=s3_key)

    response = _get_s3().get_object(**get_kwargs)
    body = response.get("Body") if isinstance(response, Mapping) else None
    if body is None or not hasattr(body, "read"):
        raise InvalidImportSourceError("S3 source response did not contain a readable Body")
    raw_content = body.read()
    if not isinstance(raw_content, bytes):
        raise InvalidImportSourceError("S3 source body must be bytes")
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidImportSourceError("S3 source must contain UTF-8 text") from exc

    parse_result = parse_osi_yaml(content)
    if not parse_result.success:
        raise InvalidImportSourceError(f"OSI YAML parse errors: {parse_result.errors}")
    document = parse_result.document
    if document is None:
        raise InvalidImportSourceError("parsed import source did not contain an OSI document")
    if len(document.metrics) != metrics_total:
        raise InvalidImportSourceError(
            f"pinned import source contains {len(document.metrics)} metrics, expected {metrics_total}"
        )
    return document


def _validate_datasets(namespace_id: str, job_id: str, offset: int, document: OsiDocument) -> None:
    """Validate declared datasets once before planning the first offset."""
    if offset != 0 or not document.datasets:
        return
    lookup = _get_lookup(namespace_id)
    resolution = resolve_datasets(document.datasets, lookup)
    if not resolution.success:
        raise InvalidImportSourceError(f"dataset resolution failed: {resolution.errors}")
    logger.info("import_datasets_resolved", job_id=job_id, count=len(resolution.resolved))


def _fail_owned_import(namespace_id: str, job_id: str, offset: int, claim_token: str, error: str) -> None:
    """Make a deterministic source failure terminal without racing a replacement owner."""
    if fail_claimed_job(
        namespace_id,
        job_id,
        offset=offset,
        claim_token=claim_token,
        error=error,
    ):
        return
    observed = get_job(namespace_id, job_id, consistent_read=True)
    if observed and observed.get("status") == "IN_PROGRESS":
        raise OffsetLeaseHeldError("lost import offset fence before deterministic failure could be recorded")


def _fail_unverifiable_import_progress(namespace_id: str, job_id: str, frontier: int) -> None:
    """Make unsafe pre-upgrade progress terminal without racing a newer owner."""
    if fail_job(
        namespace_id,
        job_id,
        UNVERIFIABLE_IMPORT_PROGRESS_ERROR,
        require_idle=True,
        expected_next_offset=frontier,
    ):
        return
    observed = get_job(namespace_id, job_id, consistent_read=True)
    if not observed or observed.get("status") != "IN_PROGRESS":
        return
    if job_progress_frontier(observed) != frontier:
        return
    raise OffsetLeaseHeldError("cannot fail unverifiable import progress while its frontier is active")


def _process_chunk(msg: ImportQueueMessage | dict[str, Any]) -> None:
    """Process or safely resume a single validated import chunk."""
    message = msg if isinstance(msg, ImportQueueMessage) else ImportQueueMessage.model_validate(msg)
    namespace_id = message.namespace_id
    job_id = message.job_id
    s3_key = message.s3_key
    offset = message.offset

    logger.info(
        "import_chunk_start",
        job_id=job_id,
        namespace=namespace_id,
        offset=offset,
        chunk_size=message.chunk_size,
    )

    job = get_job(namespace_id, job_id, consistent_read=True)
    if not job or job.get("status") != "IN_PROGRESS":
        logger.warning("import_job_not_active", job_id=job_id, status=job.get("status") if job else "NOT_FOUND")
        return

    stored_s3_key = job.get("s3Key")
    if not isinstance(stored_s3_key, str) or stored_s3_key != s3_key:
        logger.error("import_message_s3_key_mismatch", job_id=job_id, s3_key=s3_key)
        if fail_job(namespace_id, job_id, INVALID_IMPORT_QUEUE_MESSAGE_ERROR, require_idle=True):
            return
        observed = get_job(namespace_id, job_id, consistent_read=True)
        if observed and observed.get("status") == "IN_PROGRESS":
            raise OffsetLeaseHeldError("cannot fail mismatched queue message while an import offset lease is active")
        return

    metrics_total = job_metrics_total(job)
    chunk_size = job_chunk_size(job, legacy_fallback=message.chunk_size)
    try:
        job_next_offset(job)
    except UnverifiableImportProgressError as exc:
        logger.error(
            "import_progress_unverifiable",
            job_id=job_id,
            frontier=exc.frontier,
            reason=str(exc),
        )
        _fail_unverifiable_import_progress(namespace_id, job_id, exc.frontier)
        return
    if message.chunk_size != chunk_size or offset >= metrics_total:
        logger.error(
            "import_message_interval_invalid",
            job_id=job_id,
            offset=offset,
            chunk_size=message.chunk_size,
        )
        return
    expected_entry_count = _expected_entry_count(offset, chunk_size, metrics_total)
    end_offset = offset + expected_entry_count
    claim = claim_job_offset(
        namespace_id,
        job_id,
        offset=offset,
        end_offset=end_offset,
        chunk_size=chunk_size,
        lease_seconds=_OFFSET_LEASE_SECONDS,
    )
    if claim.state == OffsetClaimState.NOT_ACTIVE:
        logger.warning("import_job_not_active", job_id=job_id)
        return
    if claim.state == OffsetClaimState.STALE:
        logger.info("import_chunk_stale", job_id=job_id, offset=offset)
        return
    if claim.state == OffsetClaimState.INVALID_OFFSET:
        logger.error("import_chunk_interval_rejected", job_id=job_id, offset=offset, end_offset=end_offset)
        return
    if claim.state == OffsetClaimState.LEASE_HELD:
        logger.warning("import_offset_lease_held", job_id=job_id, offset=offset)
        raise OffsetLeaseHeldError(f"offset {offset} is already being processed")
    if claim.state == OffsetClaimState.ALREADY_PROCESSED:
        logger.info("import_chunk_resuming_transition", job_id=job_id, offset=offset)
        _resume_post_accounting(message, metrics_total, end_offset)
        return
    if claim.token is None:
        logger.error(
            "import_offset_claim_missing_token",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            claim_state=claim.state,
        )
        raise RuntimeError("acquired import offset claim did not contain a fencing token")

    neptune: MetricNeptuneClient | None = None
    try:
        if isinstance(claim.plan, OffsetPlanReference):
            plan = _load_plan_payload(namespace_id, job_id, claim.plan)
        else:
            try:
                document = _load_source_document(job, s3_key, metrics_total)
                _validate_datasets(namespace_id, job_id, offset, document)
                chunk = document.metrics[offset : offset + expected_entry_count]
                if len(chunk) != expected_entry_count:
                    raise InvalidImportSourceError("pinned import source does not contain the claimed chunk")
            except InvalidImportSourceError as exc:
                logger.error("invalid_import_source", job_id=job_id, offset=offset, error=str(exc))
                _fail_owned_import(namespace_id, job_id, offset, claim.token, str(exc))
                return

            if isinstance(claim.plan, OffsetPlan):
                _validate_legacy_offset_plan(claim.plan, offset, chunk)
                neptune = _get_neptune()
                plan = _rebuild_legacy_plan(namespace_id, claim.plan, chunk, document, neptune)
            elif claim.plan is None:
                neptune = _get_neptune()
                plan = _build_offset_plan(namespace_id, offset, chunk, document, neptune)
                if not _checkpoint_offset_plan(
                    namespace_id,
                    job_id,
                    offset=offset,
                    claim_token=claim.token,
                    plan=plan,
                ):
                    logger.warning("import_offset_plan_claim_lost_or_terminal", job_id=job_id, offset=offset)
                    return
            else:
                raise RuntimeError("acquired import offset claim contained an unsupported plan type")
    except SourceValidationUnavailableError as exc:
        logger.error(
            "source_validation_unavailable",
            job_id=job_id,
            offset=offset,
            error=str(exc),
        )
        _fail_owned_import(namespace_id, job_id, offset, claim.token, str(exc))
        return

    if len(plan.entries) != expected_entry_count:
        raise ValueError(
            f"durable offset plan has {len(plan.entries)} entries, expected {expected_entry_count} for this message"
        )

    if neptune is None:
        neptune = _get_neptune()
    created, updated, errors, warnings = _apply_offset_plan(namespace_id, plan, neptune)
    next_offset = offset + len(plan.entries)
    is_final_chunk = next_offset == metrics_total

    progress_recorded = finalize_job_offset(
        namespace_id,
        job_id,
        offset=offset,
        claim_token=claim.token,
        metrics_processed=len(plan.entries),
        metrics_created=created,
        metrics_updated=updated,
        errors=errors or None,
        warnings=warnings or None,
        mark_job_completed=is_final_chunk,
    )
    if not progress_recorded:
        logger.warning("import_chunk_claim_lost_or_terminal", job_id=job_id, offset=offset)
        return

    logger.info(
        "import_chunk_done",
        job_id=job_id,
        offset=offset,
        processed=len(plan.entries),
        created=created,
        updated=updated,
        errors_count=len(errors),
        is_final_chunk=is_final_chunk,
    )
    if not is_final_chunk:
        _send_continuation(message, next_offset)
