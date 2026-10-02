# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Glue event → rescan consumer (event-driven rescan, #683 R8).

Drains the Glue-event SQS queue, which is fed by per-source EventBridge rules
matching Glue Data Catalog state changes for a source's database. Each message
carries ``{namespaceId, sourceId, trigger}`` (a constant emitted by the rule's
input transformer). For each, fire an EVENT-trigger rescan via the shared
rescan path — which applies the EVENT cooldown and the active-scan guard, so a
burst of upstream change events coalesces into a single rescan.

Failures are reported per message via ``batchItemFailures`` rather than raising,
so only the messages that actually failed are retried and eventually reach the
DLQ. Raising would re-drive the whole batch and re-run rescans that already
succeeded; returning success for everything would delete the failures silently
and make the queue's DLQ unreachable for this path.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog
from coa_common.logging import setup_logging
from coa_control_plane_server.models.scan_trigger import ScanTrigger

from coa_sources.api.sources_handler import _handle_rescan

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """SQS batch entry point for Glue-event-driven rescans.

    Returns the partial-batch-failure response. The event source is configured
    with ``reportBatchItemFailures``, so SQS deletes only the messages absent
    from ``batchItemFailures``.
    """
    records = event.get("Records", [])
    processed = 0
    failures: list[dict[str, str]] = []

    def fail(record: dict[str, Any]) -> None:
        message_id = record.get("messageId")
        if message_id:
            failures.append({"itemIdentifier": message_id})

    for record in records:
        try:
            body = json.loads(record.get("body") or "{}")
        except (json.JSONDecodeError, TypeError):
            # Malformed body means the rule's input transformer is wrong, so let
            # it redrive into the DLQ where it is visible instead of vanishing.
            logger.warning("glue_event_bad_body")
            fail(record)
            continue

        namespace_id = body.get("namespaceId")
        source_id = body.get("sourceId")
        if not namespace_id or not source_id:
            logger.warning("glue_event_missing_ids", body_keys=sorted(body.keys()))
            fail(record)
            continue

        try:
            # Empty event so confirmDiscardOpenReview stays false: an upstream
            # change must not discard a steward's open review.
            response = _handle_rescan({}, namespace_id, source_id, trigger=ScanTrigger.EVENT)
            status_code = int(response.get("statusCode") or 0)
            logger.info(
                "glue_event_rescan",
                namespace_id=namespace_id,
                source_id=source_id,
                status_code=status_code,
            )
            # The rescan path returns its faults instead of raising, so a 5xx has
            # to be read off the response. 4xx is a decision (409 = open review)
            # and must not retry.
            if status_code >= 500:
                fail(record)
            else:
                processed += 1
        except Exception:
            logger.exception("glue_event_rescan_failed", source_id=source_id)
            fail(record)

    logger.info("glue_event_batch_complete", processed=processed, failed=len(failures))
    return {"batchItemFailures": failures}
