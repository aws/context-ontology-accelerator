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

The message is a trigger, not an authority. Before acting, the worker reads the
source row and proceeds only when an accepted ``DELETE`` has put it in a delete
state (``DELETING``, or ``DELETE_FAILED`` on a retry). Everything the teardown
works from comes from the row and the source id, never from the message body,
so a message for a live source, a missing source, or naming another source's
catalog does nothing.
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
from coa_sources.database.connectors.athena_catalog import derive_catalog_name

logger = structlog.get_logger(__name__)

# The statuses the API leaves a database source in once it has accepted a
# DELETE and handed the tail to this worker: DELETING on the hand-off, and
# DELETE_FAILED when the enqueue or an earlier worker attempt failed (the SQS
# redrive then retries it). Any other status means no delete was accepted.
_DELETE_STATUSES = frozenset({SourceStatus.DELETING, SourceStatus.DELETE_FAILED})

# Guard for the DELETE_FAILED write: without it, an update for a source whose
# row is already gone would create a key-only row that lists as a phantom.
_ROW_EXISTS_GUARD: dict[str, Any] = {"condition": "attribute_exists(PK)"}


def _source_key(namespace_id: str, source_id: str) -> dict[str, str]:
    return {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"}


def _mark_failed(namespace_id: str, source_id: str, reason: str) -> None:
    """Flag the source ``DELETE_FAILED`` so a stuck delete is visible.

    Best-effort: if this write fails too the source stays ``DELETING``, which the
    SQS redrive will retry. Never raises — it runs on the failure path, and
    masking the original error with a bookkeeping error would lose the diagnosis.

    Conditional on the row existing: if it is already gone there is nothing to
    flag, and an unconditional update would write a phantom row back.
    """
    try:
        _get_dao().update(
            _source_key(namespace_id, source_id),
            {"status": SourceStatus.DELETE_FAILED, "errorMessage": reason},
            **_ROW_EXISTS_GUARD,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("delete_failed_status_skipped_row_gone", source_id=source_id)
            return
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

        # Read the row before touching anything. A read error is retried via the
        # redrive; nothing has been changed yet, so there is nothing to flag.
        try:
            item = _get_dao().get(_source_key(namespace_id, source_id))
        except ClientError:
            logger.exception("source_delete_row_read_failed", source_id=source_id, namespace_id=namespace_id)
            failures.append({"itemIdentifier": message_id})
            continue

        status = (item or {}).get("status", "")
        if not item or status not in _DELETE_STATUSES:
            # No accepted DELETE behind this message: the source is gone (a
            # redelivery after success) or still live. Drop it without side
            # effects; reporting it as a failure would only redrive it.
            logger.warning(
                "source_delete_message_dropped_not_deleting",
                message_id=message_id,
                source_id=source_id,
                namespace_id=namespace_id,
                row_found=bool(item),
                status=status or None,
            )
            continue

        # Sub-type from the row, catalog name derived from the source id the
        # same way the API derives it. The message's own fields are ignored, so
        # messages already queued keep working while their content is not trusted.
        sub_type = item.get("sourceSubType", "")
        try:
            catalog_name = derive_catalog_name(source_id)
        except RuntimeError:
            logger.exception("source_delete_catalog_name_unresolved", source_id=source_id, namespace_id=namespace_id)
            _mark_failed(namespace_id, source_id, "Deletion cleanup could not resolve the catalog name")
            failures.append({"itemIdentifier": message_id})
            continue

        try:
            ok = finish_database_source_deletion(namespace_id, source_id, sub_type, catalog_name, context)
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
