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
from typing import Any

import boto3
from botocore.exceptions import ClientError
from coa_common import resolve_region
from coa_common.dao import DynamoDBDAO
from coa_common.datazone_forms import FORM_TYPE_NAME, build_forms_input, deserialize_form
from coa_common.metadata_store import SMUSClient
from coa_common.review_logic import apply_decision_to_table
from coa_control_plane_server.models.review_decision import ReviewDecision
from coa_control_plane_server.models.review_status import ReviewStatus
from coa_control_plane_server.models.source_status import SourceStatus

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

    return BulkReviewMessage(
        namespace_id=str(namespace_id),
        source_id=str(source_id),
        decision=str(decision),
        next_token=str(next_token) if next_token else None,
        tables_approved_so_far=approved_so_far,
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


def _persist_terminal_state(
    sources_table: str,
    region: str,
    namespace_id: str,
    source_id: str,
    new_status: str,
    tables_approved: int,
) -> None:
    """Update the source record with final status and tablesApproved counter."""
    DynamoDBDAO(sources_table, region=region).update(
        {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
        {"status": new_status, "tablesApproved": tables_approved},
        raise_on_error=False,
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


def process_bulk_review(
    *,
    msg: BulkReviewMessage,
    client: SMUSClient,
    project_id: str,
    sources_table: str,
    region: str,
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
        )
        return BulkReviewResult(tables_total=0, tables_changed=0, tables_failed=[])

    # Apply decision in-memory (shared cascade helper, bulk semantics — see
    # libs/common/review_logic.py for the rules) and select assets that
    # need a write.
    to_write: list[dict[str, Any]] = []
    for asset in assets:
        if apply_decision_to_table(asset["table"], msg.decision, bulk=True):
            to_write.append(asset)

    failed: list[str] = []
    if to_write:
        with ThreadPoolExecutor(max_workers=_PARALLELISM) as pool:
            futures = [pool.submit(_write_revision, client, a) for a in to_write]
            for future in as_completed(futures):
                err_table_id = future.result()
                if err_table_id:
                    failed.append(err_table_id)

    tables_approved_this_page = sum(
        1
        for a in assets
        if a["table"].business_metadata.review_status == ReviewStatus.APPROVED and a["table"].table_id not in failed
    )
    # Fold in the count accumulated by earlier pages in the chain.
    tables_approved = msg.tables_approved_so_far + tables_approved_this_page

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

    new_status = success_terminal if not failed else failure_terminal
    _persist_terminal_state(sources_table, region, msg.namespace_id, msg.source_id, new_status, tables_approved)

    return BulkReviewResult(
        tables_total=len(assets),
        tables_changed=len(to_write) - len(failed),
        tables_failed=failed,
    )


# ---------------------------------------------------------------------------
# Lambda entry point (SQS-triggered)
# ---------------------------------------------------------------------------


_SOURCES_TABLE: str = os.environ.get("SOURCES_TABLE", "")
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
