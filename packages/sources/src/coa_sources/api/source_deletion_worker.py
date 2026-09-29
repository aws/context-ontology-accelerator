# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SQS worker that finishes deleting a database source.

Why this exists: a database source's teardown includes one DataZone
``delete_asset`` per discovered table. That is work proportional to source size,
and it used to run inside the ``DELETE`` request on a Lambda with a 30-second
timeout. An 860-table source could not finish, so the request was killed
mid-cleanup — and because the row was dropped regardless of progress, the
remaining assets were orphaned with no handle left to retry them.

The API now validates, does the bounded teardown whose only handle is the row
(catalog/federation deregistration), flips the source to ``DELETING`` and
enqueues here. This Lambda gets a 15-minute envelope and, critically, deletes
the row LAST — so an incomplete cleanup leaves a retryable ``DELETING`` /
``DELETE_FAILED`` source rather than invisible orphans.

Idempotent by construction: every step is a delete, and the shared
``finish_database_source_deletion`` tolerates already-absent assets, scan jobs
and rows, so an SQS redelivery is safe.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from botocore.exceptions import ClientError
from coa_common.constants import validate_namespace_id, validate_source_id
from coa_control_plane_server.models.source_status import SourceStatus

from coa_sources.api.sources_handler import (
    _get_dao,
    finish_database_source_deletion,
)

logger = structlog.get_logger(__name__)


def _mark_failed(namespace_id: str, source_id: str, reason: str) -> None:
    """Flag the source ``DELETE_FAILED`` so a stuck delete is visible.

    Best-effort: if this write fails too the source stays ``DELETING``, which the
    SQS redrive will retry. Never raises — it runs on the failure path, and
    masking the original error with a bookkeeping error would lose the diagnosis.
    """
    try:
        _get_dao().update(
            {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"},
            {"status": SourceStatus.DELETE_FAILED, "errorMessage": reason},
        )
    except ClientError:
        logger.exception("delete_failed_status_write_failed", source_id=source_id)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Process deletion messages, reporting per-message failures to SQS.

    Uses partial batch response (``batchItemFailures``) so one source's failure
    redrives only its own message. Without it a single bad message would redeliver
    the whole batch and re-run cleanup for sources that already finished — safe,
    since every step is idempotent, but wasteful and noisy.
    """
    failures: list[dict[str, str]] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record.get("body") or "{}")
        except (json.JSONDecodeError, ValueError):
            # Can never succeed, but report it as a failure anyway: a record left
            # out of batchItemFailures counts as success and SQS deletes it. Failing
            # it lands it in the DLQ after maxReceiveCount, payload kept and alarmed.
            logger.exception("source_delete_message_unparseable", message_id=message_id)
            failures.append({"itemIdentifier": message_id})
            continue

        namespace_id = body.get("namespace_id", "")
        source_id = body.get("source_id", "")
        if not namespace_id or not source_id:
            logger.warning("source_delete_message_incomplete", message_id=message_id, body=body)
            failures.append({"itemIdentifier": message_id})  # to the DLQ, as above
            continue

        # Defense-in-depth at the queue trust boundary: the ids should be the
        # server-generated UUIDs the API enqueued, and they flow into DDB keys,
        # a DataZone search prefix and a derived catalog name. Never act on a
        # malformed id; fail it to the DLQ, as above.
        try:
            validate_namespace_id(namespace_id, "namespace_id")
            validate_source_id(source_id, "source_id")
        except ValueError:
            logger.exception("source_delete_message_invalid_ids", message_id=message_id)
            failures.append({"itemIdentifier": message_id})
            continue

        try:
            ok = finish_database_source_deletion(
                namespace_id,
                source_id,
                body.get("sub_type", ""),
                body.get("catalog_name", ""),
                context,
            )
        except Exception:
            logger.exception("source_delete_worker_failed", source_id=source_id, namespace_id=namespace_id)
            _mark_failed(namespace_id, source_id, "Deletion cleanup raised an unexpected error")
            failures.append({"itemIdentifier": message_id})
            continue

        if not ok:
            # The row survived, so the source is still there and must be retried.
            logger.warning("source_delete_incomplete", source_id=source_id, namespace_id=namespace_id)
            _mark_failed(namespace_id, source_id, "Deletion cleanup did not complete")
            failures.append({"itemIdentifier": message_id})
            continue

        logger.info("source_delete_completed", source_id=source_id, namespace_id=namespace_id)

    return {"batchItemFailures": failures}
