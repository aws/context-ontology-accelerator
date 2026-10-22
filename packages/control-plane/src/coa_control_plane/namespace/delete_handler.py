# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Delete Namespace handler (async via Step Functions).

Validates state, marks the namespace as DELETING, starts the deletion
pipeline state machine, and returns 202 Accepted. Clients poll
GET /namespaces/{id} for the final status (DELETED) or failure
(DELETE_FAILED).

Lifecycle: only ARCHIVED or DELETE_FAILED namespaces can be deleted.
  - ARCHIVED -> DELETING -> DELETED (happy path)
  - ARCHIVED -> DELETING -> DELETE_FAILED -> (retry) -> DELETING -> DELETED

Deletion is blocked only while an operation is actively running that could
race with the deletion pipeline's cleanup:
  - a source is actively scanning/enriching (SOURCE_ACTIVE_STATUSES)
  - an ontology induction job has not reached a terminal state
  - a metric import job is IN_PROGRESS
Completed/failed job records, along with all sources, ontology data, and
metrics themselves, are deleted by the pipeline — this handler no longer
blocks on their mere existence.

On Step Functions start failure, reverts status to DELETE_FAILED so the
caller can retry.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

import boto3
import structlog
from coa_common.constants import SOURCE_ACTIVE_STATUSES, validate_namespace_id
from coa_common.dao import DynamoDBDAO, QueryParams
from coa_common.response import api_response
from coa_control_plane_server.models.namespace_status import NamespaceStatus

logger = structlog.get_logger(__name__)

_ns_dao: DynamoDBDAO | None = None
_sfn_client: Any | None = None

# Namespace deletion is blocked only while a metric import job is actively
# running. Completed/failed jobs are stale records with no bearing on
# whether it's safe to delete the namespace — the deletion
# pipeline's delete_metrics step removes them along with everything else.
_BLOCKING_IMPORT_JOB_STATUSES = frozenset({"IN_PROGRESS"})

# A source actively scanning/enriching should not be deleted out from under
# itself by DeleteSources — that mirrors sources_handler._handle_delete's
# own check. DELETING is intentionally excluded: a source already being
# deleted individually is not a reason to block namespace deletion.
_BLOCKING_SOURCE_STATUSES = SOURCE_ACTIVE_STATUSES - {"DELETING"}


def _get_ns_dao() -> DynamoDBDAO:
    global _ns_dao  # noqa: PLW0603
    if _ns_dao is None:
        _ns_dao = DynamoDBDAO(os.environ["NAMESPACES_TABLE"], region=os.environ["AWS_REGION"])
    return _ns_dao


def _get_sfn_client() -> Any:
    global _sfn_client  # noqa: PLW0603
    if _sfn_client is None:
        _sfn_client = boto3.client("stepfunctions", region_name=os.environ["AWS_REGION"])
    return _sfn_client


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle DELETE /namespaces/{namespaceId}.

    Returns 202 Accepted on success (async deletion pipeline started).
    Returns 409 if deletion is already in progress, the namespace is not in
    a deletable state, or an active source scan, ontology induction job, or
    metric import job is blocking deletion.
    """
    path_params = event.get("pathParameters") or {}
    namespace_id = path_params.get("namespaceId", "")

    if not namespace_id:
        return api_response(400, {"message": "namespaceId is required"})
    try:
        validate_namespace_id(namespace_id)
    except ValueError as exc:
        return api_response(400, {"message": str(exc)})

    dao = _get_ns_dao()
    item = dao.get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    if not item:
        return api_response(404, {"message": "Namespace not found"})

    current_status = item.get("status")

    if current_status == NamespaceStatus.DELETED:
        return api_response(409, {"message": "Namespace is already deleted"})

    if current_status == NamespaceStatus.DELETING:
        return api_response(202, {"namespaceId": namespace_id, "status": current_status})

    if current_status not in (NamespaceStatus.ARCHIVED, NamespaceStatus.DELETE_FAILED):
        return api_response(
            409,
            {
                "message": (
                    f"Namespace must be ARCHIVED or DELETE_FAILED before deletion. Current status: {current_status}"
                )
            },
        )

    # ── Precondition: no active source scans, induction jobs, or import jobs ─
    blockers = _check_active_operations(namespace_id)
    if blockers:
        blocker_detail = "; ".join(blockers)
        return api_response(
            409,
            {
                "message": f"Cannot delete namespace: {blocker_detail}. Wait for these jobs to finish.",
                "blockers": blockers,
            },
        )

    # ── Start deletion pipeline, then mark DELETING ───────────────────
    # Order matters: start_execution FIRST, then update status. If the
    # Lambda crashes between these two operations, it's better to have a
    # running execution with status still ARCHIVED (re-entry will see
    # ExecutionAlreadyExists and return 202) than a DELETING status with
    # no execution running (permanently stuck, no recovery path).
    sfn_arn = os.environ["DELETION_STATE_MACHINE_ARN"]
    sfn_client = _get_sfn_client()

    # Use namespaceId as the execution name for idempotency on the happy
    # path. On retry from DELETE_FAILED, a prior execution with this name
    # already exists (Step Functions keeps execution history), so append a
    # uuid suffix to avoid ExecutionAlreadyExists.
    execution_name = f"delete-{namespace_id}"
    if current_status == NamespaceStatus.DELETE_FAILED:
        execution_name = f"delete-{namespace_id}-retry-{uuid.uuid4().hex[:8]}"

    sfn_input = json.dumps(
        {
            "namespaceId": namespace_id,
            "namespaceName": item.get("name", ""),
            "athenaWorkgroupName": item.get("athenaWorkgroupName"),
            "dataZoneProjectId": item.get("dataZoneProjectId"),
        }
    )

    try:
        sfn_client.start_execution(stateMachineArn=sfn_arn, name=execution_name, input=sfn_input)
    except sfn_client.exceptions.ExecutionAlreadyExists:
        logger.info("namespace_deletion_already_in_progress", namespace_id=namespace_id)
        # Ensure status is DELETING (covers crash-between-start-and-update recovery).
        dao.update(
            key={"PK": f"NS#{namespace_id}", "SK": "METADATA"},
            update_fields={"status": NamespaceStatus.DELETING},
        )
        return api_response(202, {"namespaceId": namespace_id, "status": NamespaceStatus.DELETING})
    except Exception:
        logger.exception("namespace_deletion_start_failed", namespace_id=namespace_id)
        return api_response(500, {"message": "Failed to initiate namespace deletion"})

    # Execution started successfully — now mark the namespace as DELETING.
    dao.update(
        key={"PK": f"NS#{namespace_id}", "SK": "METADATA"},
        update_fields={"status": NamespaceStatus.DELETING},
    )

    logger.info("namespace_deletion_started", namespace_id=namespace_id, execution_name=execution_name)
    return api_response(202, {"namespaceId": namespace_id, "status": NamespaceStatus.DELETING})


# ── Precondition checks ────────────────────────────────────────────────────


def _check_active_operations(namespace_id: str) -> list[str]:
    """Check for in-progress operations that should block deletion.

    Unlike the old MVP handler, this does NOT block on the mere existence of
    sources, ontology records, or completed/failed job records — those are
    all cleaned up by the deletion pipeline itself. It
    blocks only on operations actively running against this namespace right
    now, since the deletion pipeline's cleanup steps (DeleteSources,
    DeleteOntology, DeleteMetrics) could otherwise race with in-flight
    writes: deleting a source mid-scan, tearing down ontology graph data an
    induction job is still writing to, or deleting metrics an import job is
    still creating.
    """
    blockers: list[str] = []
    blockers.extend(_check_active_sources(namespace_id))
    blockers.extend(_check_active_induction_jobs(namespace_id))
    blockers.extend(_check_active_import_jobs(namespace_id))
    return blockers


def _check_active_sources(namespace_id: str) -> list[str]:
    """Count sources actively scanning/enriching (SOURCE_ACTIVE_STATUSES).

    Mirrors the check sources_handler._handle_delete already performs for a
    single source — done here too so the namespace-level precondition fails
    fast with one clear message instead of letting the deletion pipeline
    discover the same 409 mid-flight, retry it a few times, and eventually
    land the whole namespace in DELETE_FAILED for a source that was simply
    still finishing its scan.
    """
    table = os.environ.get("SOURCES_TABLE")
    if not table:
        return []

    region = os.environ["AWS_REGION"]
    dao = DynamoDBDAO(table, region=region)
    count = 0
    last_key = None
    for _ in range(1000):
        result = dao.query(
            QueryParams(
                key_condition="#pk = :ns",
                expression_names={"#pk": "namespaceId"},
                expression_values={":ns": namespace_id},
                index_name="ByNamespace",
                exclusive_start_key=last_key,
            )
        )
        count += sum(1 for i in result.items if i.get("status") in _BLOCKING_SOURCE_STATUSES)
        if not result.last_evaluated_key:
            break
        last_key = result.last_evaluated_key

    if count:
        return [f"{count} source(s) actively scanning or enriching"]
    return []


def _check_active_induction_jobs(namespace_id: str) -> list[str]:
    """Count in-flight ontology work still writing to the namespace.

    Covers induction jobs, ingest jobs, and proposal accepts that are not yet
    terminal, via the ontology-engine.

    Calls the ontology-engine's GET /induce/active-jobs endpoint rather than
    querying its DynamoDB table directly: that table has no GSI and the
    service's own job-status scan logic (terminal-status sets per job type,
    PK/SK conventions, structured-vs-unstructured source_type backfill
    rules) is non-trivial and already implemented there — duplicating it
    here would risk drifting out of sync.

    Reachability policy (see #DELETE-STUCK): the deletion path is only ever
    reached for an ARCHIVED (or DELETE_FAILED) namespace, and an archived
    namespace accepts no NEW induction work — so the only thing this check can
    legitimately be racing with is a job that was already running at archive
    time, which reaches a terminal state within minutes. A TRANSIENT inability
    to reach the ontology-engine must therefore NOT permanently block deletion:
    the previous "fail closed on the first error" behavior meant that whenever
    the delete Lambda couldn't reach the ontology-engine (VPC/config/transient),
    EVERY ontology namespace became permanently undeletable (409 "unable to
    verify ... try again shortly" forever, even a day later — the jobs had long
    finished). That broke self-contained test teardown and leaked ECS/VKG
    resources indefinitely.

    New behavior: retry the reachability call a few times with short backoff;
    if the endpoint returns, honor its answer (block only on a real activeJobs
    count). If it stays unreachable after all retries, LOG LOUDLY and fail OPEN
    (allow the delete) — the async deletion pipeline is itself race-tolerant
    (its DeleteSources/DeleteOntology steps handle in-flight state and a genuine
    race lands the namespace in DELETE_FAILED, which is retryable), so proceeding
    is strictly safer than bricking every delete on an unreachable checker.
    """
    endpoint = os.environ.get("ONTOLOGY_ENGINE_ENDPOINT", "").rstrip("/")
    if not endpoint:
        logger.warning("ontology_engine_endpoint_not_configured_skipping_check", namespace_id=namespace_id)
        return []

    qs = urllib_parse.urlencode({"namespace": namespace_id})
    url = f"{endpoint}/induce/active-jobs?{qs}"

    # Keep the whole check well inside the API Lambda's timeout (15s): a short
    # per-call timeout and a small attempt count. Defaults: 1 retry (2 attempts)
    # at 5s each + one short backoff ≈ under 12s worst case. Overridable via env.
    attempts = int(os.environ.get("DELETE_ACTIVE_JOBS_CHECK_ATTEMPTS", "2"))
    call_timeout = float(os.environ.get("DELETE_ACTIVE_JOBS_CHECK_TIMEOUT", "5"))
    last_error: Exception | None = None
    for attempt in range(attempts):
        req = urllib_request.Request(url, method="GET")
        try:
            with urllib_request.urlopen(req, timeout=call_timeout) as resp:  # noqa: S310
                body = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, OSError) as e:
            last_error = e
            logger.warning(
                "active_induction_jobs_check_attempt_failed",
                namespace_id=namespace_id,
                attempt=attempt + 1,
                attempts=attempts,
                error=str(e),
            )
            if attempt + 1 < attempts:
                time.sleep(1)  # brief backoff, kept small to respect the Lambda timeout
            continue

        active = body.get("activeJobs", 0)
        if active:
            return [f"{active} ontology induction/ingest job(s) or proposal accept(s) in progress"]
        return []

    # Exhausted retries without a successful response. Fail OPEN so an
    # unreachable checker can't permanently block deletion of an already-archived
    # namespace (which cannot start new induction work). Surface it loudly.
    logger.error(
        "active_induction_jobs_check_unreachable_failing_open",
        namespace_id=namespace_id,
        attempts=attempts,
        error=str(last_error) if last_error else "unknown",
    )
    return []


def _check_active_import_jobs(namespace_id: str) -> list[str]:
    """Check for in-progress metric import jobs that should block deletion."""
    table = os.environ.get("METRIC_IMPORT_JOBS_TABLE")
    if not table:
        return []

    region = os.environ["AWS_REGION"]
    dao = DynamoDBDAO(table, region=region)
    count = 0
    last_key = None
    for _ in range(1000):
        result = dao.query(
            QueryParams(
                key_condition="PK = :pk",
                expression_values={":pk": f"NS#{namespace_id}"},
                exclusive_start_key=last_key,
            )
        )
        count += sum(1 for i in result.items if i.get("status") in _BLOCKING_IMPORT_JOB_STATUSES)
        if not result.last_evaluated_key:
            break
        last_key = result.last_evaluated_key

    if count:
        return [f"{count} metric import job(s) in progress"]
    return []
