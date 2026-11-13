# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

r"""Db-scan reaper — terminal-status safety net for abnormally-ended executions.

The db-scan Step Functions state machine writes ``SCAN_FAILED`` to the source
row via its in-machine ``dbErrorChain`` only on *catchable* task errors (a task
that raises ``States.Timeout`` / a service exception routed through
``.addCatch()``). Execution-level failures — the state-machine ``timeout``
firing as ``ExecutionTimedOut``, an operator ``StopExecution`` (``ABORTED``),
or an otherwise ``FAILED`` execution that no state caught — are NOT catchable
in-machine, so the source is stranded in an active status
(``SCANNING``/``ENRICHING``/...) with no terminal-status write, leaving it
undeletable and un-rescannable.

This handler is the out-of-band backstop. It is triggered by an EventBridge
rule on the state machine's *execution status change* and drives the source to
``SCAN_FAILED`` when — and only when — the row is still in an active status
(idempotent no-op otherwise, so it is safe against the in-machine write having
already run, or against redelivery).

EventBridge input shape ("Step Functions Execution Status Change",
``source == "aws.states"``)::

    {
        "detail-type": "Step Functions Execution Status Change",
        "source": "aws.states",
        "detail": {
            "executionArn": "arn:aws:states:...:execution:<sm>:<name>",
            "stateMachineArn": "arn:aws:states:...:stateMachine:<sm>",
            "name": "<execution-name>",
            "status": "TIMED_OUT" | "ABORTED" | "FAILED" | ...,
            "input": "{\"namespaceId\": \"...\", \"sourceId\": \"...\", ...}",
            ...
        }
    }

``detail.input`` is the ORIGINAL ``StartExecution`` input, verbatim, as a JSON
string. The db-scan trigger (``packages/sources/database/trigger/index.py``)
passes the bare ``sourceId`` (no ``DS#`` prefix) and ``namespaceId`` in that
input, which are the two keys this handler reads to rebuild the sources-table
key ``{"PK": "NS#<namespaceId>", "SK": "SRC#<sourceId>"}``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from coa_common.constants import SOURCE_ACTIVE_STATUSES
from coa_common.dao import DynamoDBDAO
from coa_control_plane_server.models.source_status import SourceStatus

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

SOURCES_TABLE = os.environ["SOURCES_TABLE"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

_dao: DynamoDBDAO | None = None


def _get_dao() -> DynamoDBDAO:
    global _dao
    if _dao is None:
        _dao = DynamoDBDAO(SOURCES_TABLE, region=AWS_REGION)
    return _dao


def handler(event: dict[str, Any], context: Any) -> None:
    """Lambda entry point — EventBridge Step Functions execution status change.

    Parses ``detail.input`` for ``namespaceId``/``sourceId`` and, if the source
    is still in an active status, marks it ``SCAN_FAILED``. A malformed event
    (unparseable input or missing keys) is logged and ignored — never raised —
    so a single bad event cannot poison the rule for every subsequent one.
    """
    detail = event.get("detail", {})
    status = detail.get("status", "UNKNOWN")
    execution_arn = detail.get("executionArn", "unknown")

    raw_input = detail.get("input")
    if not raw_input:
        logger.warning(
            "Reaper: execution %s ended %s but carried no detail.input; nothing to reap.",
            execution_arn,
            status,
        )
        return

    try:
        execution_input = json.loads(raw_input)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "Reaper: execution %s ended %s with unparseable detail.input (%s); skipping.",
            execution_arn,
            status,
            exc,
        )
        return

    # Valid JSON that is not an object (e.g. "123", [], null) would AttributeError
    # on the .get() calls below. Treat it like a malformed event: log and skip.
    if not isinstance(execution_input, dict):
        logger.warning(
            "Reaper: execution %s ended %s with non-object detail.input (type=%s); skipping.",
            execution_arn,
            status,
            type(execution_input).__name__,
        )
        return

    namespace_id = execution_input.get("namespaceId")
    source_id = execution_input.get("sourceId")
    if not namespace_id or not source_id:
        logger.warning(
            "Reaper: execution %s ended %s but input lacks namespaceId/sourceId "
            "(namespaceId=%r sourceId=%r); skipping.",
            execution_arn,
            status,
            namespace_id,
            source_id,
        )
        return

    # sources-table key schema: PK=NS#{namespaceId}, SK=SRC#{sourceId}. The
    # trigger passes the bare sourceId (no DS# prefix) in the execution input.
    source_key = {"PK": f"NS#{namespace_id}", "SK": f"SRC#{source_id}"}

    # DynamoDBDAO.get() re-raises any ClientError other than a missing table
    # (throttle, 5xx, ...). Catch it here so a transient DDB fault does not crash
    # the Lambda and make EventBridge retry the same event indefinitely — the
    # reaper is a best-effort backstop, and the state machine's own error chain
    # and a later redelivery both still cover the source.
    try:
        item = _get_dao().get(source_key)
    except ClientError as exc:
        logger.warning(
            "Reaper: DynamoDB get failed for source %s (execution %s, %s): %s; skipping.",
            source_key,
            execution_arn,
            status,
            exc,
        )
        return
    if not item:
        logger.info(
            "Reaper: source %s not found (already deleted?); no-op for execution %s (%s).",
            source_key,
            execution_arn,
            status,
        )
        return

    current_status = item.get("status")
    if current_status not in SOURCE_ACTIVE_STATUSES:
        logger.info(
            "Reaper: source %s already terminal (status=%s), no-op for execution %s (%s).",
            source_key,
            current_status,
            execution_arn,
            status,
        )
        return

    logger.warning(
        "Reaper: execution %s ended %s with source %s still active (status=%s); marking SCAN_FAILED.",
        execution_arn,
        status,
        source_key,
        current_status,
    )
    # Mirror discovery_handler's failure-path write: conditional on the row
    # still existing, non-raising so a concurrent delete does not turn cleanup
    # into an error.
    _get_dao().update(
        key=source_key,
        update_fields={
            "status": SourceStatus.SCAN_FAILED,
            "lastScanAt": datetime.now(UTC).isoformat(),
            "errorMessage": (
                f"Scan execution ended {status} with no terminal-status write; reaper marked SCAN_FAILED."
            ),
        },
        condition="attribute_exists(PK)",
        raise_on_error=False,
    )
