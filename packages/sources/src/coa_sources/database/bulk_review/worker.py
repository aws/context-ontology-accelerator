# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bulk approve/reject worker — applies a decision to every PENDING asset.

Applies a review decision to every PENDING table/column of a source
asynchronously.

Triggered by an SQS message produced by the Approve/RejectSource API
handlers. The handler transitions the source's status to APPROVING (for an
approve) or REJECTING (for a reject) via a conditional DDB update before
enqueuing, so a duplicate SQS delivery here will find a state that doesn't
match and bail out cleanly.

Cascade rules are imported from ``coa_common.review_logic`` so
the synchronous per-table review handler and this async worker stay in sync.

Write-only-when-changed: per-asset DataZone revisions are only written when
a status actually changes — this keeps the worker fast for sources that
already have most assets approved.

Concurrency: asset writes are parallelized with a ThreadPoolExecutor. The
worker iterates pages of search_assets sequentially (DataZone search has its
own per-account limits) and dispatches the expensive create_asset_revision
calls to the pool.

Idempotency at worker level: the worker validates that the source is still
in the matching transient state (APPROVING or REJECTING) before doing any
DataZone writes. If a duplicate SQS message arrives after the first
invocation has already finished and transitioned the source to a terminal
state, the duplicate exits early.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError
from coa_common import resolve_region
from coa_common.dao import DynamoDBDAO
from coa_common.datazone_forms import FORM_TYPE_NAME, build_forms_input, deserialize_form
from coa_common.domain_models import Table
from coa_common.metadata_store import SMUSClient
from coa_common.review_logic import apply_decision_to_table
from coa_common.s3 import get_s3_client, read_file_bytes
from coa_control_plane_server.models.review_decision import ReviewDecision
from coa_control_plane_server.models.review_status import ReviewStatus
from coa_control_plane_server.models.source_status import SourceStatus

from coa_sources.database.metrics import emit_metric
from coa_sources.database.rescan_backup import S3_ABSENT_CODES, backup_s3_key

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Per-invocation table budget. When a source has more assets than this, the
# worker processes one page of this many, re-enqueues a continuation carrying
# the search next_token + running approved count, and leaves the source in the
# transient state until the final page. This is a THROUGHPUT knob, not a silent
# ceiling: every table is eventually processed across the chained invocations
# (ceil(N / budget) of them), so a 50k-table source is fully approved with no
# silent drop. Kept well under what one 5-min Lambda can load+revise.
# ponytail: fixed per-invocation budget; if a single page's forms-fetch + revise
# still overruns the timeout, drop this or lower _PAGE_WALL_CLOCK_BUDGET_S.
_PAGE_TABLE_BUDGET = int(os.environ.get("BULK_REVIEW_PAGE_BUDGET", "1000"))

# Wall-clock budget per invocation (seconds). If loading+revising a page nears
# this, we stop after the current search page and continue in a fresh
# invocation — a second guard so a slow DataZone account can't push us past the
# 5-min Lambda timeout even if the table budget hasn't been hit.
# ponytail: 240s under the 5-min (300s) Lambda timeout; raise toward ~270 for
# fewer invocations if DataZone latency is low, lower if writes time out.
_PAGE_WALL_CLOCK_BUDGET_S = int(os.environ.get("BULK_REVIEW_WALL_CLOCK_BUDGET_S", "240"))

_PARALLELISM = int(os.environ.get("BULK_REVIEW_PARALLELISM", "10"))

_REVIEW_QUEUE_URL: str = os.environ.get("REVIEW_QUEUE_URL", "")

# S3 bucket holding the re-scan backup blob written by discovery. Read here to
# delete the re-scan's removed items on approve and to restore the pre-rescan
# state on reject. Only the re-scan approve/reject paths use it.
_BUCKET_NAME: str = os.environ.get("BUCKET_NAME", "")

_sqs = None


def _get_sqs() -> Any:
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=_AWS_REGION)
    return _sqs


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class BulkReviewMessage:
    """Validated SQS payload.

    ``next_token`` and ``tables_approved_so_far`` carry continuation state for
    a source too large to process in a single invocation. Both are absent on
    the first message (the API enqueues only namespace/source/decision) and are
    set by the worker when it re-enqueues to continue paging.
    """

    namespace_id: str
    source_id: str
    decision: str  # ReviewDecision value
    next_token: str | None = None
    tables_approved_so_far: int = 0
    # True when the source is in RESCAN_REVIEW (set by the API at enqueue time).
    # An approve then also deletes the re-scan's removed tables/columns; a reject
    # RESTORES the pre-rescan state from the S3 backup and lands the source back
    # on APPROVED (not REJECTED) — see process_bulk_review.
    is_rescan: bool = False


@dataclass
class BulkReviewResult:
    """Aggregated outcome for a single bulk review job."""

    tables_total: int
    tables_changed: int
    tables_failed: list[str]


# ---------------------------------------------------------------------------
# Per-decision lifecycle
# ---------------------------------------------------------------------------


def _lifecycle(decision: str) -> tuple[str, str, str]:
    """Return (transient_status, success_terminal, failure_terminal) for a decision.

    APPROVE: APPROVING → APPROVED on success; APPROVAL_FAILED on error.
    REJECT:  REJECTING → REJECTED on success; REJECTION_FAILED on error.

    A successful reject is terminal: the source lands in REJECTED and no
    further review transitions are permitted (the API blocks them) — the
    steward must re-onboard a new source.
    """
    if decision == ReviewDecision.APPROVED:
        return SourceStatus.APPROVING, SourceStatus.APPROVED, SourceStatus.APPROVAL_FAILED
    if decision == ReviewDecision.REJECTED:
        return SourceStatus.REJECTING, SourceStatus.REJECTED, SourceStatus.REJECTION_FAILED
    raise ValueError(f"Unsupported decision: {decision}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def parse_message(body: str | dict[str, Any]) -> BulkReviewMessage:
    """Parse and validate an SQS message body. Raises ValueError on malformed input."""
    if isinstance(body, str):
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"Invalid JSON message body: {exc}") from exc
    else:
        payload = body

    namespace_id = payload.get("namespaceId", "")
    source_id = payload.get("sourceId", "")
    decision = payload.get("decision", "")

    if not namespace_id or not source_id or not decision:
        raise ValueError("Message missing required fields: namespaceId, sourceId, decision")

    try:
        ReviewDecision(decision)
    except ValueError as exc:
        raise ValueError(f"Invalid decision value: {decision}") from exc

    next_token = payload.get("nextToken") or None
    try:
        approved_so_far = int(payload.get("tablesApprovedSoFar") or 0)
    except (TypeError, ValueError):
        approved_so_far = 0

    # Accept both a JSON bool and the string form (the scan pipeline threads
    # isRescan as a string; the API enqueues a bool).
    raw_rescan = payload.get("isRescan")
    is_rescan = raw_rescan is True or str(raw_rescan).strip().lower() == "true"

    return BulkReviewMessage(
        namespace_id=str(namespace_id),
        source_id=str(source_id),
        decision=str(decision),
        next_token=str(next_token) if next_token else None,
        tables_approved_so_far=approved_so_far,
        is_rescan=is_rescan,
    )


# ---------------------------------------------------------------------------
# Worker pipeline
# ---------------------------------------------------------------------------


def _load_one_asset(client: SMUSClient, asset: Any, source_id: str) -> dict[str, Any] | None:
    """Fetch + deserialize a single asset's table form. Returns None on skip.

    I/O-bound (one get_asset_forms round-trip); dispatched to a thread pool so a
    page's forms fetch runs concurrently instead of the old serial N+1.
    """
    try:
        detail = client.get_asset_forms(asset_id=asset.asset_id)
    except Exception:
        logger.warning(
            "load_assets_skipping_asset_get_forms_failed",
            extra={"asset_id": asset.asset_id, "source_id": source_id},
        )
        return None
    for form in detail.get("formsOutput", []):
        if form.get("formName") != FORM_TYPE_NAME:
            continue
        try:
            payload = json.loads(form["content"])
            table = deserialize_form(payload, data_source_id=source_id)
        except Exception:
            logger.exception(
                "load_assets_deserialize_failed",
                extra={"asset_id": asset.asset_id, "source_id": source_id},
            )
            return None
        return {"asset_id": asset.asset_id, "asset_name": asset.name, "table": table}
    return None


def _load_asset_page(
    client: SMUSClient,
    project_id: str,
    source_id: str,
    *,
    start_token: str | None,
    budget: int,
    deadline: float | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Load one bounded page of assets, resuming from ``start_token``.

    Returns ``(assets, remaining_token)``. ``remaining_token`` is ``None`` ONLY
    when the source's asset list is fully exhausted; otherwise it is the search
    ``next_token`` a continuation invocation should resume from. Each search
    page (up to 50 assets) is fully processed before the budget/deadline is
    checked, so the returned token always points past every asset we collected
    — no asset is skipped across the invocation boundary.
    """
    ds_key = f"DS#{source_id}"
    prefix = f"{ds_key}:"
    assets: list[dict[str, Any]] = []
    next_token = start_token

    while True:
        result = client.search_assets(
            project_id=project_id,
            search_text=ds_key,
            max_results=50,
            next_token=next_token,
        )
        matching = [a for a in result.items if a.name.startswith(prefix)]
        if matching:
            with ThreadPoolExecutor(max_workers=_PARALLELISM) as pool:
                for loaded in pool.map(lambda a: _load_one_asset(client, a, source_id), matching):
                    if loaded is not None:
                        assets.append(loaded)

        next_token = result.next_token
        if not next_token:
            return assets, None  # fully exhausted
        if len(assets) >= budget:
            return assets, next_token
        if deadline is not None and time.monotonic() >= deadline:
            return assets, next_token


def _enqueue_continuation(msg: BulkReviewMessage, next_token: str, tables_approved: int) -> None:
    """Re-enqueue a continuation so the next chunk runs in a fresh invocation.

    Keeps the source in its transient state (the idempotency guard passes)
    until the final page writes the terminal state.
    """
    if not _REVIEW_QUEUE_URL:
        # No queue configured (e.g. a misconfigured env) — fail loud rather than
        # silently drop the remaining tables.
        raise RuntimeError("REVIEW_QUEUE_URL not configured; cannot continue bulk review paging")
    _get_sqs().send_message(
        QueueUrl=_REVIEW_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "namespaceId": msg.namespace_id,
                "sourceId": msg.source_id,
                "decision": msg.decision,
                "nextToken": next_token,
                "tablesApprovedSoFar": tables_approved,
            }
        ),
    )


def _write_revision(client: SMUSClient, asset: dict[str, Any]) -> str | None:
    """Write a single asset revision. Returns table_id on failure, else None."""
    table = asset["table"]
    try:
        client.create_asset_revision(
            asset_id=asset["asset_id"],
            name=asset["asset_name"],
            description=table.business_metadata.description or "",
            forms_input=build_forms_input(table),
        )
        return None
    except Exception:
        logger.exception(
            "bulk_review_revision_failed",
            extra={"asset_id": asset["asset_id"], "asset_name": asset["asset_name"]},
        )
        return table.table_id


def _write_review_scan_job(
    scan_jobs_table: str,
    region: str,
    namespace_id: str,
    source_id: str,
    decision: str,
    is_rescan: bool,
    tables_approved: int,
    status: str,
) -> None:
    """Append a REVIEW event row to the source-scan-jobs table (best-effort).

    Records the approve/reject outcome so the Scan History tab can show a real
    audit row instead of a client-derived guess. Shares the scan-jobs table with
    discovery scan rows (PK=SRC#{sourceId}, SK=ISO timestamp); readers treat a
    row with no ``eventType`` as a SCAN, so REVIEW rows are tagged explicitly.

    Failure here MUST NOT fail the review — the terminal source state is already
    written by the time we get here — so any error is logged and swallowed.
    """
    if not scan_jobs_table:
        # No scan-jobs table configured (e.g. a misconfigured env): skip the
        # audit row rather than raising over a non-critical write.
        return
    now = datetime.now(tz=UTC).isoformat()
    try:
        DynamoDBDAO(scan_jobs_table, region=region).put(
            {
                "PK": f"SRC#{source_id}",
                "SK": now,
                "sourceId": source_id,
                "namespaceId": namespace_id,
                "eventType": "REVIEW",
                "decision": decision,
                "isRescan": bool(is_rescan),
                "tablesApproved": tables_approved,
                "status": status,
                "createdAt": now,
                "startedAt": now,
            }
        )
    except Exception:
        logger.warning(
            "review_scan_job_write_failed",
            extra={"source_id": source_id, "namespace_id": namespace_id, "decision": decision},
        )


def _persist_terminal_state(
    sources_table: str,
    region: str,
    namespace_id: str,
    source_id: str,
    new_status: str,
    tables_approved: int,
    extra_fields: dict[str, Any] | None = None,
    *,
    scan_jobs_table: str = "",
    decision: str | None = None,
    is_rescan: bool = False,
) -> None:
    """Update the source record with final status and tablesApproved counter.

    ``extra_fields`` merges additional source-record fields — used by a re-scan
    reject to restore the pre-rescan summary counts alongside the status.

    When ``scan_jobs_table`` and ``decision`` are supplied, a REVIEW audit row
    is appended to the scan-jobs table AFTER the source update succeeds. That
    write is best-effort and never fails the review.
    """
    fields: dict[str, Any] = {"status": new_status, "tablesApproved": tables_approved}
    if extra_fields:
        fields.update(extra_fields)
    DynamoDBDAO(sources_table, region=region).update(
        {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
        fields,
        raise_on_error=False,
    )
    if decision is not None:
        _write_review_scan_job(
            scan_jobs_table,
            region,
            namespace_id,
            source_id,
            decision,
            is_rescan,
            tables_approved,
            new_status,
        )


def _verify_in_transient(
    sources_table: str,
    region: str,
    namespace_id: str,
    source_id: str,
    expected_transient: str,
) -> bool:
    """Return True iff the source is still in the expected transient state.

    Worker idempotency guard: if the source has already advanced to a terminal
    state (an earlier worker invocation finished) we skip the duplicate.
    """
    item = DynamoDBDAO(sources_table, region=region).get(
        {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
        projection=["status"],
    )
    return bool(item and item.get("status") == expected_transient)


def _rejected_key_column_reason(assets: list[dict[str, Any]], source_id: str) -> str | None:
    """Return a denial reason if an approved table would ship without its key columns.

    Runs AFTER the cascade, which is fine — and necessary — because the condition
    it tests is one the cascade cannot manufacture: a bulk APPROVE never turns a
    column REJECTED (it flips only ``PENDING_REVIEW`` → ``APPROVED`` and
    deliberately preserves explicit REJECTED decisions, see
    ``coa_common.review_logic``). So a REJECTED key column here was rejected by a
    human, and survives the cascade untouched.

    Why this is the condition that matters: ``catalog_reader`` projects only
    ``APPROVED`` columns into the induction payload
    (``_table_to_induction_format``). A REJECTED primary-key column therefore
    disappears from the catalog while the table itself is APPROVED, and induction
    mints a subject template with no key to build from — surfacing later as a
    PK-column-missing error in Ontop, far from the review that caused it. A
    REJECTED foreign-key column silently drops the relationship instead, so the
    ontology loses an object property the schema does have.

    This replaces a gate that looked for ``PENDING_REVIEW`` columns *after* the
    cascade had already flipped every one of them to ``APPROVED`` — structurally
    unreachable dead code. Checking pending columns pre-cascade is not the fix
    either: the enricher leaves ALL columns ``PENDING_REVIEW``
    (``table_enricher.py``), and clearing them is precisely what bulk approve is
    for, so blocking on pending would reject every normal bulk approval.

    Args:
        assets: Loaded assets for this page, post-cascade.
        source_id: Source id, for logging.

    Returns:
        A human-readable reason when the approval must be blocked, else ``None``.
    """
    offenders: list[tuple[str, str, str]] = []  # (table, column, key kind)
    for asset in assets:
        table = asset["table"]
        if table.business_metadata.review_status != ReviewStatus.APPROVED:
            continue  # not shipping, so its column states are moot

        key_kinds: dict[str, str] = {}
        for pk_col in table.primary_key.columns or []:
            key_kinds[pk_col] = "primary key"
        for fk in table.foreign_keys or []:
            if fk.column and fk.column not in key_kinds:
                key_kinds[fk.column] = "foreign key"

        for col in table.columns:
            kind = key_kinds.get(col.name)
            if kind and col.business_metadata.review_status == ReviewStatus.REJECTED:
                offenders.append((table.name, col.name, kind))

    if not offenders:
        return None

    sample = offenders[:10]
    logger.warning(
        "bulk_approve_blocked_rejected_key_columns",
        extra={"source_id": source_id, "rejected_key_count": len(offenders), "sample": sample},
    )
    return (
        f"Cannot approve: {len(offenders)} key column(s) across "
        f"{len({t for t, _, _ in offenders})} table(s) are REJECTED, but their tables are approved. "
        "A rejected key column is dropped from the induction catalog, leaving the table without a "
        "usable key. Un-reject these columns or reject the whole table. Examples: "
        + ", ".join(f"{t}.{c} ({kind})" for t, c, kind in sample)
    )


def _read_backup(source_id: str) -> dict[str, Any] | None:
    """Read the re-scan backup blob for a source, or None if there is none.

    A no-drift re-scan writes no backup, so a missing object (NoSuchKey) is a
    legitimate empty change-set and returns None. Any OTHER S3 error is
    re-raised so the worker fails the job rather than silently skipping the
    restore/delete an approve or reject depends on.
    """
    if not _BUCKET_NAME:
        raise RuntimeError("BUCKET_NAME not set; cannot read the re-scan backup for approve/reject")
    key = backup_s3_key(source_id)
    try:
        raw = read_file_bytes(get_s3_client(), _BUCKET_NAME, key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in S3_ABSENT_CODES:
            return None
        raise
    return json.loads(raw)


def _delete_backup(source_id: str) -> None:
    """Delete the re-scan backup blob once a re-scan review is resolved.

    The blob's PRESENCE is what a later re-scan uses to detect that an
    un-approved re-scan is still open — it reconstructs the approved baseline
    from it (see ``rescan.reconstruct_approved_baseline``). Clearing it on both
    approve and reject keeps "backup present == open un-approved re-scan"
    reliable, so the next re-scan of a now-approved source diffs against the
    live assets directly. Best-effort: S3 delete is idempotent (a missing key
    is a no-op) and any error is swallowed so a resolved review is not blocked
    on cleanup.
    """
    if not _BUCKET_NAME:
        return
    try:
        get_s3_client().delete_object(Bucket=_BUCKET_NAME, Key=backup_s3_key(source_id))
    except Exception:
        logger.warning("rescan_backup_delete_failed", extra={"source_id": source_id})


def _resolve_asset_ids(client: SMUSClient, project_id: str, source_id: str, wanted_names: set[str]) -> dict[str, str]:
    """Map DataZone asset names to ids for a bounded set of re-scan targets.

    Pages ``search_assets`` (the same search the cascade uses) only until every
    wanted name is found or the source is exhausted — the change-set is small,
    so this rarely walks past the first page.
    """
    if not wanted_names:
        return {}
    found: dict[str, str] = {}
    token: str | None = None
    while wanted_names - set(found):
        result = client.search_assets(
            project_id=project_id, search_text=f"DS#{source_id}", max_results=50, next_token=token
        )
        for a in result.items:
            if a.name in wanted_names:
                found[a.name] = a.asset_id
        token = result.next_token
        if not token:
            break
    return found


def _extract_table(detail: dict[str, Any], source_id: str) -> Table | None:
    """Deserialize the CoaTableMetadata form out of a get_asset_forms response."""
    for form in detail.get("formsOutput", []):
        if form.get("formName") != FORM_TYPE_NAME:
            continue
        try:
            return deserialize_form(json.loads(form["content"]), data_source_id=source_id)
        except Exception:
            return None
    return None


def _apply_rescan_removals(
    client: SMUSClient, project_id: str, source_id: str, backup: dict[str, Any] | None
) -> tuple[list[str], int]:
    """Delete a re-scan's removed tables and drop its removed columns (approve).

    Returns ``(failures, deleted_table_count)``: ``failures`` are table_ids whose
    delete/column-drop raised; ``deleted_table_count`` is how many removed tables
    were actually deleted, so the caller can keep ``tablesApproved`` honest. A
    missing backup (no-drift re-scan) is a no-op.
    """
    if not backup:
        return [], 0
    removed_tables: list[str] = list(backup.get("removed_tables", []))
    removed_columns: dict[str, list[str]] = dict(backup.get("removed_columns", {}))
    if not removed_tables and not removed_columns:
        return [], 0

    wanted = {f"DS#{source_id}:{tid}" for tid in removed_tables + list(removed_columns)}
    asset_ids = _resolve_asset_ids(client, project_id, source_id, wanted)
    failures: list[str] = []
    deleted = 0

    for tid in removed_tables:
        asset_id = asset_ids.get(f"DS#{source_id}:{tid}")
        if not asset_id:
            continue  # already absent — nothing to delete
        try:
            client.delete_asset(asset_id=asset_id)
            deleted += 1
        except Exception:
            logger.exception("rescan_delete_removed_table_failed", extra={"source_id": source_id, "table_id": tid})
            failures.append(tid)

    for tid, cols in removed_columns.items():
        name = f"DS#{source_id}:{tid}"
        asset_id = asset_ids.get(name)
        if not asset_id:
            continue
        try:
            table = _extract_table(client.get_asset_forms(asset_id=asset_id), source_id)
            if table is None:
                failures.append(tid)
                continue
            drop = set(cols)
            table.columns = [c for c in table.columns if c.name not in drop]
            client.create_asset_revision(
                asset_id=asset_id,
                name=name,
                description=table.business_metadata.description or "",
                forms_input=build_forms_input(table),
            )
        except Exception:
            logger.exception("rescan_drop_removed_columns_failed", extra={"source_id": source_id, "table_id": tid})
            failures.append(tid)

    return failures, deleted


def _process_rescan_reject(
    client: SMUSClient, project_id: str, source_id: str, backup: dict[str, Any] | None
) -> list[str]:
    """Restore the pre-rescan state on a re-scan reject; returns failed table_ids.

    Modified tables are re-written from their backed-up form, added tables are
    deleted, and removed tables are left untouched (still their approved selves).
    A missing backup (no-drift re-scan) is a no-op. This deliberately does NOT
    run the reject cascade — a re-scan reject discards the fresh scan and keeps
    what was already approved.
    """
    if not backup:
        return []
    modified: dict[str, Any] = dict(backup.get("modified_backup", {}))
    added: list[str] = list(backup.get("added_tables", []))
    if not modified and not added:
        return []

    wanted = {f"DS#{source_id}:{tid}" for tid in list(modified) + added}
    asset_ids = _resolve_asset_ids(client, project_id, source_id, wanted)
    failures: list[str] = []

    for tid, stored_form in modified.items():
        name = f"DS#{source_id}:{tid}"
        asset_id = asset_ids.get(name)
        if not asset_id:
            # The overwritten asset should still exist; a miss means we cannot
            # restore it — flag rather than silently leave the rejected scan live.
            failures.append(tid)
            continue
        try:
            table = deserialize_form(stored_form, data_source_id=source_id)
            client.create_asset_revision(
                asset_id=asset_id,
                name=name,
                description=table.business_metadata.description or "",
                forms_input=build_forms_input(table),
            )
        except Exception:
            logger.exception("rescan_restore_modified_failed", extra={"source_id": source_id, "table_id": tid})
            failures.append(tid)

    for tid in added:
        asset_id = asset_ids.get(f"DS#{source_id}:{tid}")
        if not asset_id:
            continue  # never created or already gone
        try:
            client.delete_asset(asset_id=asset_id)
        except Exception:
            logger.exception("rescan_delete_added_failed", extra={"source_id": source_id, "table_id": tid})
            failures.append(tid)

    return failures


def process_bulk_review(
    *,
    msg: BulkReviewMessage,
    client: SMUSClient,
    project_id: str,
    sources_table: str,
    region: str,
    scan_jobs_table: str,
) -> BulkReviewResult:
    """Run one page of the bulk review pipeline for a source, paging via SQS.

    Steps:
      1. Verify source is still in the matching transient state (idempotency).
         Continuation messages pass this because the source stays transient
         until the final page.
      2. Load ONE bounded page of assets (resume from msg.next_token), bounded
         by a per-invocation table budget AND a wall-clock budget under the
         Lambda timeout — parallelized get_asset_forms fetch.
      3. Apply decision in memory using the shared cascade helper; write
         revisions for changed assets (parallel ThreadPoolExecutor).
      4. If a search next_token remains (more assets): re-enqueue a
         continuation carrying that token + the running approved count and
         leave the source transient — do NOT write terminal yet.
      5. On the final page (no next_token): write the terminal state with the
         FULL accumulated tablesApproved.

    This replaces the old silent 1000-table cap: every table is processed
    across ceil(N / budget) chained invocations, so a 50k-table source is
    fully approved with no silent drop (#853).
    """
    transient, success_terminal, failure_terminal = _lifecycle(msg.decision)
    # A re-scan reject discards the fresh scan and keeps what was already
    # approved, so it lands the source back on APPROVED — not REJECTED.
    if msg.is_rescan and msg.decision == ReviewDecision.REJECTED:
        success_terminal = SourceStatus.APPROVED

    if not _verify_in_transient(sources_table, region, msg.namespace_id, msg.source_id, transient):
        logger.info(
            "bulk_review_skipping_not_in_transient",
            extra={
                "namespace_id": msg.namespace_id,
                "source_id": msg.source_id,
                "expected_transient": transient,
            },
        )
        return BulkReviewResult(tables_total=0, tables_changed=0, tables_failed=[])

    # Re-scan REJECT is a RESTORE, not a cascade: re-write the modified assets
    # from the S3 backup, delete the added ones, leave the removed ones, and
    # return the source to APPROVED. Bounded by the change-set, so it does not
    # page like the approve cascade.
    if msg.is_rescan and msg.decision == ReviewDecision.REJECTED:
        backup = _read_backup(msg.source_id)
        reject_failed = _process_rescan_reject(client, project_id, msg.source_id, backup)
        if reject_failed:
            # Leave the source in REJECTION_FAILED for manual intervention; don't
            # rewrite counts onto a half-restored state.
            _persist_terminal_state(
                sources_table,
                region,
                msg.namespace_id,
                msg.source_id,
                failure_terminal,
                msg.tables_approved_so_far,
                scan_jobs_table=scan_jobs_table,
                decision=msg.decision,
                is_rescan=msg.is_rescan,
            )
        else:
            # Restore the pre-rescan summary counts so the source's numbers match
            # the restored (pre-rescan) assets.
            prior = (backup or {}).get("source_summary") or {}
            restore: dict[str, Any] = {
                k: prior[k] for k in ("discoveredSchemas", "lastScanAt", "lastScanJobId") if k in prior
            }
            # tablesDiscovered is numeric on the source record and the GetSource
            # response model requires a number. The backup stores it as a JSON
            # string, so coerce it back to int (as tablesApproved already is
            # below); copying the string through makes the detail endpoint fail
            # model validation and the source page 500s.
            if "tablesDiscovered" in prior:
                # A corrupt or non-numeric backup value must not fail the whole
                # reject — that would strand the source in REJECTION_FAILED with
                # no way back. Skip the restore instead, leaving the live count
                # in place: slightly stale beats both a crash and a wrong zero.
                try:
                    restore["tablesDiscovered"] = int(prior["tablesDiscovered"])
                except (TypeError, ValueError):
                    logger.warning(
                        "rescan_reject_bad_tables_discovered_skipped",
                        extra={
                            "source_id": msg.source_id,
                            "value": repr(prior["tablesDiscovered"]),
                        },
                    )
            _persist_terminal_state(
                sources_table,
                region,
                msg.namespace_id,
                msg.source_id,
                success_terminal,
                int(prior.get("tablesApproved", msg.tables_approved_so_far) or 0),
                extra_fields=restore,
                scan_jobs_table=scan_jobs_table,
                decision=msg.decision,
                is_rescan=msg.is_rescan,
            )
            # Review resolved (reject applied cleanly) — clear the backup so its
            # presence reliably signals an OPEN un-approved re-scan to any later
            # re-scan. Skipped on the failure branch above (state left for manual
            # intervention; the backup is still needed to retry the restore).
            _delete_backup(msg.source_id)
        return BulkReviewResult(tables_total=len(reject_failed), tables_changed=0, tables_failed=reject_failed)

    deadline = time.monotonic() + _PAGE_WALL_CLOCK_BUDGET_S if _PAGE_WALL_CLOCK_BUDGET_S > 0 else None
    assets, remaining_token = _load_asset_page(
        client,
        project_id,
        msg.source_id,
        start_token=msg.next_token,
        budget=_PAGE_TABLE_BUDGET,
        deadline=deadline,
    )
    if not assets and remaining_token is None:
        # No (more) tables to process. Advance to the terminal state so the
        # source isn't stuck in the transient state forever, carrying any count
        # accumulated by earlier pages.
        _persist_terminal_state(
            sources_table,
            region,
            msg.namespace_id,
            msg.source_id,
            success_terminal,
            tables_approved=msg.tables_approved_so_far,
            scan_jobs_table=scan_jobs_table,
            decision=msg.decision,
            is_rescan=msg.is_rescan,
        )
        return BulkReviewResult(tables_total=0, tables_changed=0, tables_failed=[])

    # Apply decision in-memory (shared cascade helper, bulk semantics — see
    # libs/common/review_logic.py for the rules) and select assets that
    # need a write.
    to_write: list[dict[str, Any]] = []
    # table_ids whose own review_status flipped on this call. Only these are
    # human decisions for the acceptance-rate metric below — a table that was
    # already APPROVED (repeat bulk run, or a mixed selection) still lands in
    # to_write when its columns cascade, but it is not a new decision.
    transitioned: list[str] = []
    for asset in assets:
        table = asset["table"]
        status_before = table.business_metadata.review_status
        if apply_decision_to_table(table, msg.decision, bulk=True):
            to_write.append(asset)
        if table.business_metadata.review_status != status_before:
            transitioned.append(table.table_id)

    failed: list[str] = []
    if to_write:
        with ThreadPoolExecutor(max_workers=_PARALLELISM) as pool:
            futures = [pool.submit(_write_revision, client, a) for a in to_write]
            for future in as_completed(futures):
                err_table_id = future.result()
                if err_table_id:
                    failed.append(err_table_id)

    # Absolute counter folded into the source record — every APPROVED table on
    # this page, transitioned on this call or earlier.
    tables_approved_this_page = sum(
        1
        for a in assets
        if a["table"].business_metadata.review_status == ReviewStatus.APPROVED and a["table"].table_id not in failed
    )
    # Fold in the count accumulated by earlier pages in the chain.
    tables_approved = msg.tables_approved_so_far + tables_approved_this_page

    # Enrichment acceptance rate: how much of what the enricher proposed a human
    # kept. Emitted once per bulk decision (aggregate counts, not per table).
    # Only transitions count, matching the single-table half in
    # api/database_routes.py — a re-approve of an already-APPROVED table is not
    # a new decision, so it must not inflate the rate.
    decided = sum(1 for tid in transitioned if tid not in failed)
    if msg.decision == ReviewDecision.APPROVED:
        emit_metric("TablesApprovedByReview", decided, "Count", ReviewScope="Bulk")
    else:
        emit_metric("TablesRejectedByReview", decided, "Count", ReviewScope="Bulk")

    # Gate: refuse an approval that would ship a table whose PK/FK column is
    # REJECTED. See _rejected_key_column_reason for why this is the condition
    # that actually breaks downstream, and why the previous check could not fire.
    if msg.decision == ReviewDecision.APPROVED and not failed:
        reason = _rejected_key_column_reason(assets, msg.source_id)
        if reason is not None:
            failed.append(reason)

    # More assets remain AND this page had no failures → continue in a fresh
    # invocation. Leave the source transient (the idempotency guard lets the
    # continuation through) and carry the running approved count forward. A
    # failure short-circuits the chain to the failure-terminal state below so
    # the source doesn't silently keep paging past a broken write.
    if remaining_token is not None and not failed:
        _enqueue_continuation(msg, remaining_token, tables_approved)
        return BulkReviewResult(
            tables_total=len(assets),
            tables_changed=len(to_write),
            tables_failed=[],
        )

    # Final page of a re-scan APPROVE: every changed item is now APPROVED, so
    # delete the tables/columns the re-scan flagged as removed (recorded in the
    # S3 backup). Runs once — only on the last page and only if the cascade had
    # no failures — and keeps the approved count honest for the deleted tables.
    if msg.is_rescan and msg.decision == ReviewDecision.APPROVED and remaining_token is None and not failed:
        removal_failures, deleted_removed = _apply_rescan_removals(
            client, project_id, msg.source_id, _read_backup(msg.source_id)
        )
        failed.extend(removal_failures)
        tables_approved = max(0, tables_approved - deleted_removed)

    new_status = success_terminal if not failed else failure_terminal
    _persist_terminal_state(
        sources_table,
        region,
        msg.namespace_id,
        msg.source_id,
        new_status,
        tables_approved,
        scan_jobs_table=scan_jobs_table,
        decision=msg.decision,
        is_rescan=msg.is_rescan,
    )

    # Terminal page of a successful re-scan APPROVE (removals done above with no
    # failures): the review is resolved, so clear the backup — same signal
    # hygiene as the reject path. Only here: continuation pages return earlier,
    # and a failed page (failed non-empty) is excluded so the backup survives
    # for retry/inspection.
    if msg.is_rescan and msg.decision == ReviewDecision.APPROVED and remaining_token is None and not failed:
        _delete_backup(msg.source_id)

    return BulkReviewResult(
        tables_total=len(assets),
        tables_changed=len(to_write) - len(failed),
        tables_failed=failed,
    )


# ---------------------------------------------------------------------------
# Lambda entry point (SQS-triggered)
# ---------------------------------------------------------------------------


_SOURCES_TABLE: str = os.environ.get("SOURCES_TABLE", "")
# Scan-history store. A REVIEW audit row is appended here on each terminal
# approve/reject so the console can show the real history (best-effort).
_SOURCE_SCAN_JOBS_TABLE: str = os.environ.get("SOURCE_SCAN_JOBS_TABLE", "")
_NAMESPACES_TABLE: str = os.environ.get("NAMESPACES_TABLE", "")
_SMUS_DOMAIN_ID: str = os.environ.get("SMUS_DOMAIN_ID", "")
_PROJECT_ACCESS_ROLE_ARN: str = os.environ.get("PROJECT_ACCESS_ROLE_ARN", "")
_AWS_REGION: str = resolve_region()


def _build_smus_client() -> SMUSClient:
    return SMUSClient(
        domain_id=_SMUS_DOMAIN_ID,
        region_name=_AWS_REGION,
        assume_role_arn=_PROJECT_ACCESS_ROLE_ARN or None,
        session_name="bulk-review-worker",
    )


def _resolve_project_id(namespace_id: str) -> str | None:
    item = DynamoDBDAO(_NAMESPACES_TABLE, region=_AWS_REGION).get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    return item.get("dataZoneProjectId") if item else None


def _mark_failed(namespace_id: str, source_id: str, decision: str | None) -> None:
    """Best-effort: flip the source to the appropriate failure state for the decision."""
    if decision == ReviewDecision.REJECTED:
        failure_status = SourceStatus.REJECTION_FAILED
    else:
        # Default to APPROVAL_FAILED when decision is unknown/missing.
        failure_status = SourceStatus.APPROVAL_FAILED
    try:
        DynamoDBDAO(_SOURCES_TABLE, region=_AWS_REGION).update(
            {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
            {"status": failure_status},
            raise_on_error=False,
        )
    except Exception:
        logger.exception(
            "mark_failed_failed",
            extra={"namespace_id": namespace_id, "source_id": source_id},
        )


def handler(event: dict[str, Any], context: object) -> None:
    """Lambda entry point — process one SQS record per invocation (batchSize=1)."""
    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            msg = parse_message(record["body"])
        except (KeyError, ValueError):
            logger.exception("bulk_review_invalid_message_dropping", extra={"message_id": message_id})
            # Don't re-raise — SQS will eventually DLQ malformed messages, but
            # there's no point retrying invalid input.
            continue

        try:
            project_id = _resolve_project_id(msg.namespace_id)
            if not project_id:
                logger.error(
                    "bulk_review_namespace_not_found",
                    extra={"namespace_id": msg.namespace_id, "source_id": msg.source_id},
                )
                _mark_failed(msg.namespace_id, msg.source_id, msg.decision)
                continue

            client = _build_smus_client()
            result = process_bulk_review(
                msg=msg,
                client=client,
                project_id=project_id,
                sources_table=_SOURCES_TABLE,
                region=_AWS_REGION,
                scan_jobs_table=_SOURCE_SCAN_JOBS_TABLE,
            )
            logger.info(
                "bulk_review_complete",
                extra={
                    "namespace_id": msg.namespace_id,
                    "source_id": msg.source_id,
                    "decision": msg.decision,
                    "tables_total": result.tables_total,
                    "tables_changed": result.tables_changed,
                    "tables_failed": len(result.tables_failed),
                    "message_id": message_id,
                },
            )
        except ClientError:
            # Transient AWS errors should be retried via SQS redelivery.
            logger.exception(
                "bulk_review_transient_error",
                extra={"namespace_id": msg.namespace_id, "source_id": msg.source_id},
            )
            raise
        except Exception:
            logger.exception(
                "bulk_review_unexpected_error",
                extra={"namespace_id": msg.namespace_id, "source_id": msg.source_id},
            )
            _mark_failed(msg.namespace_id, msg.source_id, msg.decision)
            # Re-raise so SQS marks it failed → goes to DLQ after retries.
            raise


# Keep boto3 reference live to avoid linter complaints when only used indirectly.
_ = boto3
