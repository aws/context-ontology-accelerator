# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB-backed store for import job tracking."""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

logger = structlog.get_logger(__name__)

_TABLE_NAME = os.environ.get("IMPORT_JOBS_TABLE", "coa-dev-metric-import-jobs")
_OFFSET_PLAN_ATTRIBUTE = "offsetPlan"
_OFFSET_PLAN_SCHEMA_VERSION = 2
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
UNVERIFIABLE_IMPORT_PROGRESS_ERROR = (
    "This import was started by an older release and cannot be resumed safely; submit the import again"
)
_dynamodb = None


class UnverifiableImportProgressError(ValueError):
    """Persisted counters exist without enough history for safe replay."""

    def __init__(self, frontier: int, reason: str) -> None:
        """Expose the validated frontier for a conditional terminal transition."""
        super().__init__(reason)
        self.frontier = frontier


class MetricDisposition(StrEnum):
    """The durable logical outcome selected for one metric in an import chunk."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    ERROR = "ERROR"


@dataclass(frozen=True)
class OffsetPlanEntry:
    """One metric's durable logical disposition and associated message."""

    name: str
    disposition: MetricDisposition
    warning: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        """Validate the message invariant for the selected disposition."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("OffsetPlanEntry name must be a non-empty string")
        if not isinstance(self.disposition, MetricDisposition):
            raise ValueError("OffsetPlanEntry disposition must be a MetricDisposition")

        if self.disposition == MetricDisposition.CREATE:
            if self.warning is not None or self.error is not None:
                raise ValueError("CREATE offset plan entry must not contain a warning or error")
            return

        if self.disposition == MetricDisposition.UPDATE:
            if not isinstance(self.warning, str) or not self.warning.strip():
                raise ValueError("UPDATE offset plan entry requires a non-empty warning")
            if self.error is not None:
                raise ValueError("UPDATE offset plan entry must not contain an error")
            return

        if not isinstance(self.error, str) or not self.error.strip():
            raise ValueError("ERROR offset plan entry requires a non-empty error")
        if self.warning is not None:
            raise ValueError("ERROR offset plan entry must not contain a warning")


@dataclass(frozen=True)
class OffsetPlan:
    """The immutable legacy logical dispositions selected for one import offset."""

    offset: int
    entries: tuple[OffsetPlanEntry, ...]

    def __post_init__(self) -> None:
        """Require a concrete non-negative offset and at least one typed entry."""
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("OffsetPlan offset must be a non-negative integer")
        if not isinstance(self.entries, tuple) or not self.entries:
            raise ValueError("OffsetPlan entries must be a non-empty tuple")
        if not all(isinstance(entry, OffsetPlanEntry) for entry in self.entries):
            raise ValueError("OffsetPlan entries must contain only OffsetPlanEntry values")


@dataclass(frozen=True)
class OffsetPlanReference:
    """Compact DynamoDB reference to an exact schema-v2 plan payload in versioned S3."""

    offset: int
    entry_count: int
    payload_key: str
    payload_version_id: str
    payload_sha256: str
    payload_bytes: int

    def __post_init__(self) -> None:
        """Validate every persisted reference field before it can drive replay."""
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("OffsetPlanReference offset must be a non-negative integer")
        if isinstance(self.entry_count, bool) or not isinstance(self.entry_count, int) or self.entry_count <= 0:
            raise ValueError("OffsetPlanReference entry_count must be a positive integer")
        if not isinstance(self.payload_key, str) or not self.payload_key.strip():
            raise ValueError("OffsetPlanReference payload_key must be a non-empty string")
        if not isinstance(self.payload_version_id, str) or not self.payload_version_id.strip():
            raise ValueError("OffsetPlanReference payload_version_id must be a non-empty string")
        if not isinstance(self.payload_sha256, str) or not _SHA256_PATTERN.fullmatch(self.payload_sha256):
            raise ValueError("OffsetPlanReference payload_sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.payload_bytes, bool) or not isinstance(self.payload_bytes, int) or self.payload_bytes <= 0:
            raise ValueError("OffsetPlanReference payload_bytes must be a positive integer")


StoredOffsetPlan = OffsetPlan | OffsetPlanReference


class JobOffsetState(StrEnum):
    """Relationship between one queue interval and the job's durable frontier."""

    CURRENT = "CURRENT"
    REPLAY = "REPLAY"
    STALE = "STALE"
    INVALID = "INVALID"


@dataclass(frozen=True)
class JobOffsetAssessment:
    """Validated queue-interval classification at one durable frontier."""

    state: JobOffsetState
    frontier: int


def _serialize_offset_plan(plan: OffsetPlan) -> dict[str, Any]:
    """Serialize a validated plan to the explicit map stored in DynamoDB."""
    if not isinstance(plan, OffsetPlan):
        raise ValueError("offset plan must be an OffsetPlan")

    entries: list[dict[str, str]] = []
    for entry in plan.entries:
        serialized_entry = {
            "name": entry.name,
            "disposition": entry.disposition.value,
        }
        if entry.warning is not None:
            serialized_entry["warning"] = entry.warning
        if entry.error is not None:
            serialized_entry["error"] = entry.error
        entries.append(serialized_entry)
    return {"offset": plan.offset, "entries": entries}


def _serialize_offset_plan_reference(reference: OffsetPlanReference) -> dict[str, Any]:
    """Serialize a validated schema-v2 checkpoint reference for DynamoDB."""
    if not isinstance(reference, OffsetPlanReference):
        raise ValueError("offset plan reference must be an OffsetPlanReference")
    return {
        "schemaVersion": _OFFSET_PLAN_SCHEMA_VERSION,
        "offset": reference.offset,
        "entryCount": reference.entry_count,
        "payloadKey": reference.payload_key,
        "payloadVersionId": reference.payload_version_id,
        "payloadSha256": reference.payload_sha256,
        "payloadBytes": reference.payload_bytes,
    }


def _serialize_stored_offset_plan(plan: StoredOffsetPlan) -> dict[str, Any]:
    """Serialize either a legacy inline plan or a schema-v2 S3 reference."""
    if isinstance(plan, OffsetPlanReference):
        return _serialize_offset_plan_reference(plan)
    if isinstance(plan, OffsetPlan):
        return _serialize_offset_plan(plan)
    raise ValueError("offset plan must be an OffsetPlan or OffsetPlanReference")


def _parse_dynamodb_integer(value: object, description: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"{description} must be an integer")
    if isinstance(value, Decimal) and (not value.is_finite() or value != value.to_integral_value()):
        raise ValueError(f"{description} must be an integer")
    parsed = int(value)
    if (positive and parsed <= 0) or (not positive and parsed < 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{description} must be {qualifier}")
    return parsed


def _parse_offset_plan_reference(value: object) -> OffsetPlanReference:
    """Strictly parse a schema-v2 S3 checkpoint reference from DynamoDB."""
    if not isinstance(value, Mapping):
        raise ValueError("offset plan reference must be a DynamoDB map")
    expected_keys = {
        "schemaVersion",
        "offset",
        "entryCount",
        "payloadKey",
        "payloadVersionId",
        "payloadSha256",
        "payloadBytes",
    }
    if set(value) != expected_keys:
        raise ValueError(f"offset plan reference must contain exactly {sorted(expected_keys)}")
    schema_version = _parse_dynamodb_integer(value["schemaVersion"], "offset plan schemaVersion")
    if schema_version != _OFFSET_PLAN_SCHEMA_VERSION:
        raise ValueError(f"unsupported offset plan schema version {schema_version}")

    payload_key = value["payloadKey"]
    payload_version_id = value["payloadVersionId"]
    payload_sha256 = value["payloadSha256"]
    if not isinstance(payload_key, str):
        raise ValueError("offset plan payloadKey must be a string")
    if not isinstance(payload_version_id, str):
        raise ValueError("offset plan payloadVersionId must be a string")
    if not isinstance(payload_sha256, str):
        raise ValueError("offset plan payloadSha256 must be a string")

    try:
        return OffsetPlanReference(
            offset=_parse_dynamodb_integer(value["offset"], "offset plan offset"),
            entry_count=_parse_dynamodb_integer(value["entryCount"], "offset plan entryCount", positive=True),
            payload_key=payload_key,
            payload_version_id=payload_version_id,
            payload_sha256=payload_sha256,
            payload_bytes=_parse_dynamodb_integer(value["payloadBytes"], "offset plan payloadBytes", positive=True),
        )
    except ValueError as exc:
        raise ValueError(f"offset plan reference is invalid: {exc}") from exc


def _parse_offset_plan(value: object) -> OffsetPlan:
    """Parse and strictly validate a DynamoDB offset-plan map."""
    if not isinstance(value, Mapping):
        raise ValueError("offset plan must be a DynamoDB map")
    if set(value) != {"offset", "entries"}:
        raise ValueError("offset plan must contain exactly 'offset' and 'entries'")

    raw_offset = value["offset"]
    if isinstance(raw_offset, bool) or not isinstance(raw_offset, (int, Decimal)):
        raise ValueError("offset plan offset must be a non-negative integer")
    if isinstance(raw_offset, Decimal) and (not raw_offset.is_finite() or raw_offset != raw_offset.to_integral_value()):
        raise ValueError("offset plan offset must be a non-negative integer")
    offset = int(raw_offset)

    raw_entries = value["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("offset plan entries must be a non-empty DynamoDB list")

    entries: list[OffsetPlanEntry] = []
    required_entry_keys = {"name", "disposition"}
    allowed_entry_keys = required_entry_keys | {"warning", "error"}
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping):
            raise ValueError(f"offset plan entry {index} must be a DynamoDB map")
        entry_keys = set(raw_entry)
        if not required_entry_keys.issubset(entry_keys) or not entry_keys.issubset(allowed_entry_keys):
            raise ValueError(
                f"offset plan entry {index} must contain 'name' and 'disposition' and only optional message fields"
            )

        name = raw_entry["name"]
        raw_disposition = raw_entry["disposition"]
        if not isinstance(name, str):
            raise ValueError(f"offset plan entry {index} name must be a string")
        if not isinstance(raw_disposition, str):
            raise ValueError(f"offset plan entry {index} disposition must be a string")
        try:
            disposition = MetricDisposition(raw_disposition)
        except ValueError as exc:
            raise ValueError(f"offset plan entry {index} has unknown disposition {raw_disposition!r}") from exc

        warning = raw_entry.get("warning")
        error = raw_entry.get("error")
        try:
            entries.append(
                OffsetPlanEntry(
                    name=name,
                    disposition=disposition,
                    warning=warning,
                    error=error,
                )
            )
        except ValueError as exc:
            raise ValueError(f"offset plan entry {index} is invalid: {exc}") from exc

    return OffsetPlan(offset=offset, entries=tuple(entries))


def _parse_item_offset_plan(item: Mapping[str, Any], *, expected_offset: int) -> StoredOffsetPlan | None:
    """Read an optional persisted plan and reject malformed plans or another offset."""
    if _OFFSET_PLAN_ATTRIBUTE not in item:
        return None
    raw_plan = item[_OFFSET_PLAN_ATTRIBUTE]
    if isinstance(raw_plan, Mapping) and "schemaVersion" in raw_plan:
        plan: StoredOffsetPlan = _parse_offset_plan_reference(raw_plan)
    else:
        plan = _parse_offset_plan(raw_plan)
    if plan.offset != expected_offset:
        raise ValueError(
            f"persisted offset plan offset {plan.offset} does not match requested offset {expected_offset}"
        )
    return plan


class OffsetClaimState(StrEnum):
    """Result of trying to claim one import offset."""

    ACQUIRED = "ACQUIRED"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    STALE = "STALE"
    INVALID_OFFSET = "INVALID_OFFSET"
    LEASE_HELD = "LEASE_HELD"
    NOT_ACTIVE = "NOT_ACTIVE"


@dataclass(frozen=True)
class OffsetClaim:
    """A claim outcome with its fencing token and any durable plan when acquired."""

    state: OffsetClaimState
    token: str | None = None
    plan: StoredOffsetPlan | None = None

    def __post_init__(self) -> None:
        """Require a non-empty fencing token exactly when the claim was acquired."""
        if self.state == OffsetClaimState.ACQUIRED:
            if not self.token:
                raise ValueError("ACQUIRED offset claim requires a non-empty token")
            if self.plan is not None and not isinstance(self.plan, (OffsetPlan, OffsetPlanReference)):
                raise ValueError("ACQUIRED offset claim plan must be an OffsetPlan or OffsetPlanReference")
            return
        if self.token is not None:
            raise ValueError(f"{self.state} offset claim must not contain a token")
        if self.plan is not None:
            raise ValueError(f"{self.state} offset claim must not contain a plan")


def _get_table():
    global _dynamodb  # noqa: PLW0603
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _dynamodb.Table(_TABLE_NAME)


def _job_key(namespace_id: str, job_id: str) -> dict[str, str]:
    return {"PK": f"NS#{namespace_id}", "SK": f"IMPORT#{job_id}"}


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def create_job(
    namespace_id: str,
    s3_key: str,
    metrics_total: int,
    *,
    source_version_id: str | None = None,
    chunk_size: int = 50,
) -> dict[str, Any]:
    """Create a new import job record. Returns the job dict."""
    metrics_total = _parse_dynamodb_integer(metrics_total, "metrics_total", positive=True)
    chunk_size = _parse_dynamodb_integer(chunk_size, "chunk_size", positive=True)
    if source_version_id is not None and (not isinstance(source_version_id, str) or not source_version_id.strip()):
        raise ValueError("source_version_id must be a non-empty string when provided")
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    item = {
        **_job_key(namespace_id, job_id),
        "jobId": job_id,
        "namespaceId": namespace_id,
        "status": "IN_PROGRESS",
        "s3Key": s3_key,
        "metricsTotal": metrics_total,
        "metricsProcessed": 0,
        "nextOffset": 0,
        "chunkSize": chunk_size,
        "metricsCreated": 0,
        "metricsUpdated": 0,
        "processedOffsets": [],
        "errors": [],
        "warnings": [],
        "createdAt": now,
        "updatedAt": now,
    }
    if source_version_id is not None:
        item["s3VersionId"] = source_version_id
    _get_table().put_item(Item=item)
    logger.info("import_job_created", job_id=job_id, namespace=namespace_id, total=metrics_total)
    return item


def get_job(namespace_id: str, job_id: str, *, consistent_read: bool = False) -> dict[str, Any] | None:
    """Fetch a job by ID. Returns None if not found."""
    result = _get_table().get_item(
        Key=_job_key(namespace_id, job_id),
        ConsistentRead=consistent_read,
    )
    return result.get("Item")


def job_metrics_total(job: Mapping[str, Any]) -> int:
    """Read the authoritative positive metric count from a durable job."""
    return _parse_dynamodb_integer(job.get("metricsTotal"), "import job metricsTotal", positive=True)


def job_chunk_size(job: Mapping[str, Any], *, legacy_fallback: int | None = None) -> int:
    """Read the fixed chunk size, using a queue value only for a legacy job."""
    value = job.get("chunkSize", legacy_fallback)
    return _parse_dynamodb_integer(value, "import job chunkSize", positive=True)


def _processed_offsets(job: Mapping[str, Any], frontier: int) -> list[int]:
    raw_offsets = job.get("processedOffsets", [])
    if not isinstance(raw_offsets, list):
        raise UnverifiableImportProgressError(frontier, "import job processedOffsets must be a list")
    try:
        offsets = [
            _parse_dynamodb_integer(value, f"import job processedOffsets[{index}]")
            for index, value in enumerate(raw_offsets)
        ]
    except ValueError as exc:
        raise UnverifiableImportProgressError(frontier, str(exc)) from exc
    if offsets != sorted(set(offsets)):
        raise UnverifiableImportProgressError(frontier, "import job processedOffsets must be strictly increasing")
    if frontier == 0:
        if offsets:
            raise UnverifiableImportProgressError(frontier, "unstarted import job must not contain processed offsets")
        return offsets
    if not offsets or offsets[0] != 0 or offsets[-1] >= frontier:
        raise UnverifiableImportProgressError(
            frontier,
            "import job processedOffsets do not describe a contiguous prefix",
        )
    return offsets


def job_progress_frontier(job: Mapping[str, Any]) -> int:
    """Read the counter frontier without requiring replay history."""
    metrics_processed = _parse_dynamodb_integer(job.get("metricsProcessed"), "import job metricsProcessed")
    raw_frontier = job.get("nextOffset", metrics_processed)
    frontier = _parse_dynamodb_integer(raw_frontier, "import job nextOffset")
    if frontier != metrics_processed:
        raise ValueError("import job nextOffset must equal metricsProcessed")
    if frontier > job_metrics_total(job):
        raise ValueError("import job nextOffset exceeds metricsTotal")
    return frontier


def job_next_offset(job: Mapping[str, Any]) -> int:
    """Read and validate the durable contiguous frontier, with legacy fallback."""
    frontier = job_progress_frontier(job)
    _processed_offsets(job, frontier)
    return frontier


def assess_job_offset(
    job: Mapping[str, Any],
    *,
    offset: int,
    end_offset: int,
) -> JobOffsetAssessment:
    """Classify a queue interval against the job's validated contiguous frontier."""
    frontier = job_next_offset(job)
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or isinstance(end_offset, bool)
        or not isinstance(end_offset, int)
        or offset < 0
        or end_offset <= offset
        or end_offset > job_metrics_total(job)
    ):
        return JobOffsetAssessment(JobOffsetState.INVALID, frontier)
    if offset == frontier:
        return JobOffsetAssessment(JobOffsetState.CURRENT, frontier)
    if offset > frontier:
        return JobOffsetAssessment(JobOffsetState.INVALID, frontier)

    offsets = _processed_offsets(job, frontier)
    try:
        index = offsets.index(offset)
    except ValueError:
        return JobOffsetAssessment(JobOffsetState.INVALID, frontier)
    durable_end = offsets[index + 1] if index + 1 < len(offsets) else frontier
    if end_offset != durable_end:
        return JobOffsetAssessment(JobOffsetState.INVALID, frontier)
    state = JobOffsetState.REPLAY if durable_end == frontier else JobOffsetState.STALE
    return JobOffsetAssessment(state, frontier)


def job_has_active_offset_claim(job: Mapping[str, Any], *, now_epoch: int | None = None) -> bool:
    """Return whether a valid current-frontier worker claim has not expired."""
    now = int(time.time()) if now_epoch is None else now_epoch
    has_offset = "claimOffset" in job
    has_expiry = "claimLeaseExpiresAt" in job
    if not has_offset and not has_expiry:
        return False
    if has_offset != has_expiry:
        raise ValueError("import job offset claim fields must be present together")
    claim_offset = _parse_dynamodb_integer(job["claimOffset"], "import job claimOffset")
    claim_expires = _parse_dynamodb_integer(job["claimLeaseExpiresAt"], "import job claimLeaseExpiresAt")
    if claim_offset != job_next_offset(job):
        raise ValueError("import job claimOffset must equal nextOffset")
    return claim_expires > now


def claim_job_offset(
    namespace_id: str,
    job_id: str,
    *,
    offset: int,
    end_offset: int,
    chunk_size: int,
    lease_seconds: int,
) -> OffsetClaim:
    """Claim exactly the current durable interval and recover its saved plan."""
    offset = _parse_dynamodb_integer(offset, "offset")
    end_offset = _parse_dynamodb_integer(end_offset, "end_offset", positive=True)
    chunk_size = _parse_dynamodb_integer(chunk_size, "chunk_size", positive=True)
    lease_seconds = _parse_dynamodb_integer(lease_seconds, "lease_seconds", positive=True)
    if end_offset <= offset or end_offset - offset > chunk_size:
        raise ValueError("claimed interval must be non-empty and no larger than chunk_size")

    now_epoch = int(time.time())
    token = str(uuid.uuid4())
    values = {
        ":offset": offset,
        ":end": end_offset,
        ":chunk_size": chunk_size,
        ":token": token,
        ":expires": now_epoch + lease_seconds,
        ":now": now_epoch,
        ":ts": datetime.now(UTC).isoformat(),
        ":in_progress": "IN_PROGRESS",
    }
    names = {
        "#s": "status",
        "#total": "metricsTotal",
        "#metrics_processed": "metricsProcessed",
        "#next": "nextOffset",
        "#chunk_size": "chunkSize",
        "#processed": "processedOffsets",
        "#plan": _OFFSET_PLAN_ATTRIBUTE,
        "#plan_offset": "offset",
        "#claim_offset": "claimOffset",
        "#claim_token": "claimToken",
        "#claim_expires": "claimLeaseExpiresAt",
        "#updated": "updatedAt",
    }
    try:
        table = _get_table()
        try:
            result = table.update_item(
                Key=_job_key(namespace_id, job_id),
                UpdateExpression=(
                    "SET #next = if_not_exists(#next, :offset), "
                    "#chunk_size = if_not_exists(#chunk_size, :chunk_size), "
                    "#claim_offset = :offset, #claim_token = :token, "
                    "#claim_expires = :expires, #updated = :ts"
                ),
                ConditionExpression=(
                    "#s = :in_progress AND #metrics_processed = :offset "
                    "AND (attribute_not_exists(#next) OR #next = :offset) "
                    "AND (attribute_not_exists(#chunk_size) OR #chunk_size = :chunk_size) "
                    "AND :end <= #total "
                    "AND (attribute_not_exists(#processed) OR NOT contains(#processed, :offset)) "
                    "AND (attribute_not_exists(#plan) OR #plan.#plan_offset = :offset) "
                    "AND (attribute_not_exists(#claim_offset) OR attribute_not_exists(#claim_expires) "
                    "OR #claim_expires <= :now)"
                ),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise

            job = get_job(namespace_id, job_id, consistent_read=True)
            if not job or job.get("status") != "IN_PROGRESS":
                return OffsetClaim(OffsetClaimState.NOT_ACTIVE)
            if job_chunk_size(job, legacy_fallback=chunk_size) != chunk_size:
                return OffsetClaim(OffsetClaimState.INVALID_OFFSET)
            assessment = assess_job_offset(job, offset=offset, end_offset=end_offset)
            if assessment.state == JobOffsetState.REPLAY:
                return OffsetClaim(OffsetClaimState.ALREADY_PROCESSED)
            if assessment.state == JobOffsetState.STALE:
                return OffsetClaim(OffsetClaimState.STALE)
            if assessment.state == JobOffsetState.INVALID:
                return OffsetClaim(OffsetClaimState.INVALID_OFFSET)
            _parse_item_offset_plan(job, expected_offset=offset)
            return OffsetClaim(OffsetClaimState.LEASE_HELD)
    except ClientError as exc:
        logger.exception(
            "import_offset_claim_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=exc.response.get("Error", {}).get("Code", "UNKNOWN"),
        )
        raise
    except BotoCoreError as exc:
        logger.exception(
            "import_offset_claim_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=type(exc).__name__,
        )
        raise

    if not isinstance(result, Mapping) or "Attributes" not in result:
        raise ValueError("claim update response did not contain Attributes")
    attributes = result["Attributes"]
    if not isinstance(attributes, Mapping):
        raise ValueError("claim update response Attributes must be a DynamoDB map")
    plan = _parse_item_offset_plan(attributes, expected_offset=offset)

    logger.info("import_offset_claimed", job_id=job_id, offset=offset, lease_seconds=lease_seconds)
    return OffsetClaim(OffsetClaimState.ACQUIRED, token, plan)


def store_job_offset_plan(
    namespace_id: str,
    job_id: str,
    *,
    offset: int,
    claim_token: str,
    plan: StoredOffsetPlan,
) -> bool:
    """Persist a claimed offset's immutable plan or payload reference once."""
    if not isinstance(plan, (OffsetPlan, OffsetPlanReference)):
        raise ValueError("plan must be an OffsetPlan or OffsetPlanReference")
    if plan.offset != offset:
        raise ValueError(f"plan offset {plan.offset} does not match claimed offset {offset}")

    names = {
        "#s": "status",
        "#next": "nextOffset",
        "#processed": "processedOffsets",
        "#plan": _OFFSET_PLAN_ATTRIBUTE,
        "#claim_offset": "claimOffset",
        "#claim_token": "claimToken",
        "#updated": "updatedAt",
    }
    values = {
        ":in_progress": "IN_PROGRESS",
        ":offset": offset,
        ":claim_token": claim_token,
        ":plan": _serialize_stored_offset_plan(plan),
        ":ts": datetime.now(UTC).isoformat(),
    }
    try:
        _get_table().update_item(
            Key=_job_key(namespace_id, job_id),
            UpdateExpression="SET #plan = :plan, #updated = :ts",
            ConditionExpression=(
                "#s = :in_progress AND #next = :offset "
                "AND #claim_offset = :offset AND #claim_token = :claim_token "
                "AND (attribute_not_exists(#processed) OR NOT contains(#processed, :offset)) "
                "AND attribute_not_exists(#plan)"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        logger.exception(
            "import_offset_plan_store_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=exc.response.get("Error", {}).get("Code", "UNKNOWN"),
        )
        raise
    except BotoCoreError as exc:
        logger.exception(
            "import_offset_plan_store_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=type(exc).__name__,
        )
        raise

    logger.info("import_offset_plan_stored", namespace=namespace_id, job_id=job_id, offset=offset)
    return True


def fail_claimed_job(
    namespace_id: str,
    job_id: str,
    *,
    offset: int,
    claim_token: str,
    error: str,
) -> bool:
    """Fail a job only while the caller still owns the active offset fence."""
    if not isinstance(error, str) or not error.strip():
        raise ValueError("error must be a non-empty string")
    try:
        _get_table().update_item(
            Key=_job_key(namespace_id, job_id),
            UpdateExpression=(
                "SET #s = :failed, #updated = :ts, "
                "#errors = list_append(if_not_exists(#errors, :empty), :errors) "
                "REMOVE #plan, #claim_offset, #claim_token, #claim_expires"
            ),
            ConditionExpression=(
                "#s = :in_progress AND #next = :offset AND #claim_offset = :offset AND #claim_token = :claim_token"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#next": "nextOffset",
                "#updated": "updatedAt",
                "#errors": "errors",
                "#plan": _OFFSET_PLAN_ATTRIBUTE,
                "#claim_offset": "claimOffset",
                "#claim_token": "claimToken",
                "#claim_expires": "claimLeaseExpiresAt",
            },
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":in_progress": "IN_PROGRESS",
                ":offset": offset,
                ":claim_token": claim_token,
                ":ts": datetime.now(UTC).isoformat(),
                ":empty": [],
                ":errors": [error],
            },
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        logger.exception(
            "import_claimed_failure_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=exc.response.get("Error", {}).get("Code", "UNKNOWN"),
        )
        raise
    except BotoCoreError as exc:
        logger.exception(
            "import_claimed_failure_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=type(exc).__name__,
        )
        raise

    logger.error("import_claimed_job_failed", namespace=namespace_id, job_id=job_id, offset=offset, error=error)
    return True


def finalize_job_offset(
    namespace_id: str,
    job_id: str,
    *,
    offset: int,
    claim_token: str,
    metrics_processed: int,
    metrics_created: int,
    metrics_updated: int,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    mark_job_completed: bool = False,
) -> bool:
    """Atomically account for a planned chunk, optionally complete its job, and release its fenced lease."""
    offset = _parse_dynamodb_integer(offset, "offset")
    metrics_processed = _parse_dynamodb_integer(metrics_processed, "metrics_processed", positive=True)
    metrics_created = _parse_dynamodb_integer(metrics_created, "metrics_created")
    metrics_updated = _parse_dynamodb_integer(metrics_updated, "metrics_updated")
    next_offset = offset + metrics_processed
    set_parts = [
        "#updated = :ts",
        "#next = :next",
        "#processed = list_append(if_not_exists(#processed, :empty), :offsets)",
    ]
    values: dict[str, Any] = {
        ":mp": metrics_processed,
        ":mc": metrics_created,
        ":mu": metrics_updated,
        ":ts": datetime.now(UTC).isoformat(),
        ":empty": [],
        ":offset": offset,
        ":next": next_offset,
        ":offsets": [offset],
        ":claim_token": claim_token,
        ":in_progress": "IN_PROGRESS",
    }
    names = {
        "#s": "status",
        "#total": "metricsTotal",
        "#metrics_processed": "metricsProcessed",
        "#next": "nextOffset",
        "#processed": "processedOffsets",
        "#updated": "updatedAt",
        "#plan": _OFFSET_PLAN_ATTRIBUTE,
        "#plan_offset": "offset",
        "#claim_offset": "claimOffset",
        "#claim_token": "claimToken",
        "#claim_expires": "claimLeaseExpiresAt",
    }
    if errors:
        set_parts.append("#errors = list_append(if_not_exists(#errors, :empty), :errors)")
        names["#errors"] = "errors"
        values[":errors"] = errors
    if warnings:
        set_parts.append("#warnings = list_append(if_not_exists(#warnings, :empty), :warnings)")
        names["#warnings"] = "warnings"
        values[":warnings"] = warnings
    if mark_job_completed:
        set_parts.append("#s = :completed")
        values[":completed"] = "COMPLETED"

    update_expression = (
        "SET "
        + ", ".join(set_parts)
        + " REMOVE #plan, #claim_offset, #claim_token, #claim_expires"
        + " ADD metricsProcessed :mp, metricsCreated :mc, metricsUpdated :mu"
    )
    completion_condition = ":next = #total" if mark_job_completed else ":next < #total"
    try:
        _get_table().update_item(
            Key=_job_key(namespace_id, job_id),
            UpdateExpression=update_expression,
            ConditionExpression=(
                "#s = :in_progress AND #metrics_processed = :offset AND #next = :offset "
                "AND #claim_offset = :offset AND #claim_token = :claim_token "
                "AND #plan.#plan_offset = :offset "
                "AND (attribute_not_exists(#processed) OR NOT contains(#processed, :offset)) "
                f"AND {completion_condition}"
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        logger.exception(
            "import_offset_finalize_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=exc.response.get("Error", {}).get("Code", "UNKNOWN"),
        )
        raise
    except BotoCoreError as exc:
        logger.exception(
            "import_offset_finalize_dynamodb_failed",
            namespace=namespace_id,
            job_id=job_id,
            offset=offset,
            error_code=type(exc).__name__,
        )
        raise

    logger.info(
        "import_offset_finalized",
        namespace=namespace_id,
        job_id=job_id,
        offset=offset,
        job_completed=mark_job_completed,
    )
    return True


def complete_job(
    namespace_id: str,
    job_id: str,
    status: str = "COMPLETED",
    *,
    expected_next_offset: int | None = None,
) -> bool:
    """Make an active job terminal, optionally under a durable-frontier CAS."""
    condition = "#s = :in_progress"
    names = {
        "#s": "status",
        "#updated": "updatedAt",
        "#plan": _OFFSET_PLAN_ATTRIBUTE,
        "#claim_offset": "claimOffset",
        "#claim_token": "claimToken",
        "#claim_expires": "claimLeaseExpiresAt",
    }
    values: dict[str, Any] = {
        ":s": status,
        ":in_progress": "IN_PROGRESS",
        ":ts": datetime.now(UTC).isoformat(),
    }
    if expected_next_offset is not None:
        expected_next_offset = _parse_dynamodb_integer(expected_next_offset, "expected_next_offset")
        condition += (
            " AND (#next = :expected_next OR (attribute_not_exists(#next) AND #metrics_processed = :expected_next))"
        )
        names["#next"] = "nextOffset"
        names["#metrics_processed"] = "metricsProcessed"
        values[":expected_next"] = expected_next_offset

    try:
        _get_table().update_item(
            Key=_job_key(namespace_id, job_id),
            UpdateExpression=("SET #s = :s, #updated = :ts REMOVE #plan, #claim_offset, #claim_token, #claim_expires"),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise
    logger.info("import_job_completed", job_id=job_id, status=status)
    return True


def fail_job(
    namespace_id: str,
    job_id: str,
    error: str,
    *,
    require_idle: bool = False,
    expected_next_offset: int | None = None,
) -> bool:
    """Fail an active job, optionally fencing both lease and progress."""
    condition = "#s = :in_progress"
    names = {
        "#s": "status",
        "#updated": "updatedAt",
        "#errors": "errors",
        "#plan": _OFFSET_PLAN_ATTRIBUTE,
        "#claim_offset": "claimOffset",
        "#claim_token": "claimToken",
        "#claim_expires": "claimLeaseExpiresAt",
    }
    values: dict[str, Any] = {
        ":failed": "FAILED",
        ":in_progress": "IN_PROGRESS",
        ":ts": datetime.now(UTC).isoformat(),
        ":empty": [],
        ":errors": [error],
    }
    if require_idle:
        condition += (
            " AND (attribute_not_exists(#claim_offset) OR attribute_not_exists(#claim_expires) "
            "OR #claim_expires <= :now)"
        )
        values[":now"] = int(time.time())
    if expected_next_offset is not None:
        expected_next_offset = _parse_dynamodb_integer(expected_next_offset, "expected_next_offset")
        condition += (
            " AND (#next = :expected_next OR (attribute_not_exists(#next) AND #metrics_processed = :expected_next))"
        )
        names["#next"] = "nextOffset"
        names["#metrics_processed"] = "metricsProcessed"
        values[":expected_next"] = expected_next_offset

    try:
        _get_table().update_item(
            Key=_job_key(namespace_id, job_id),
            UpdateExpression=(
                "SET #s = :failed, #updated = :ts, "
                "#errors = list_append(if_not_exists(#errors, :empty), :errors) "
                "REMOVE #plan, #claim_offset, #claim_token, #claim_expires"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise
    logger.error("import_job_failed", job_id=job_id, error=error)
    return True
