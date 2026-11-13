# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scan Trigger Lambda.

Reads SQS messages and starts a Step Functions execution for each one.
Malformed messages are dropped (logged and deleted from the queue).
Transient AWS errors are re-raised so SQS retries the message.
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn_client = boto3.client("stepfunctions")

_STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

_REQUIRED_FIELDS = ("datasourceId", "scanJobId", "namespaceId", "scanType")


def handler(event: dict, context: object) -> None:
    """Lambda entry-point — process one SQS record per invocation (batchSize=1)."""
    for record in event["Records"]:
        message_id: str = record.get("messageId", "unknown")
        try:
            body = json.loads(record["body"])

            missing = [f for f in _REQUIRED_FIELDS if f not in body]
            if missing:
                logger.error(
                    "Malformed SQS message, dropping (missing fields: %s): messageId=%s",
                    missing,
                    message_id,
                )
                continue

            execution_input = {k: body[k] for k in _REQUIRED_FIELDS}
            # Pass scanJobPK/scanJobSK through so the state machine can update the correct
            # source-scan-jobs record (PK=SRC#{sourceId}, SK=scanJobSK).
            if "scanJobPK" in body:
                execution_input["scanJobPK"] = body["scanJobPK"]
            if "scanJobSK" in body:
                execution_input["scanJobSK"] = body["scanJobSK"]
            # Pass bare sourceId (without DS# prefix) for sources-table status updates.
            # Messages sent before re-scans carried it (still queued, or redriven
            # from the DLQ) have only datasourceId, so derive it from that.
            if "sourceId" in body:
                execution_input["sourceId"] = body["sourceId"]
            else:
                datasource_id = str(body["datasourceId"])
                if datasource_id.startswith("DS#") and len(datasource_id) > len("DS#"):
                    execution_input["sourceId"] = datasource_id[len("DS#") :]
            # These three fields are always present as strings: the state machine
            # maps them with JsonPath.stringAt and the enrichment step reads them
            # as env vars. isRescan is true for a drift re-scan of an approved source.
            execution_input["isRescan"] = "true" if body.get("isRescan") else "false"
            # True when the live assets are an un-approved merge (a prior re-scan is
            # still open), so discovery rebuilds the approved baseline from the S3 backup.
            execution_input["hadOpenRescan"] = "true" if body.get("hadOpenRescan") else "false"
            # Language the API resolved at enqueue time; "" when absent. The enrichment
            # worker then resolves it from the namespace item and never defaults to English.
            execution_input["namespaceLanguage"] = str(body.get("namespaceLanguage") or "")
            execution_name = body["scanJobId"].replace("#", "-").replace("SCAN-", "")[:80]

            logger.info(
                "Starting scan execution: datasourceId=%s scanJobId=%s messageId=%s",
                body["datasourceId"],
                body["scanJobId"],
                message_id,
            )
            sfn_client.start_execution(
                stateMachineArn=_STATE_MACHINE_ARN,
                name=execution_name,
                input=json.dumps(execution_input),
            )

        except (json.JSONDecodeError, KeyError) as exc:
            logger.error(
                "Malformed SQS message body, dropping: messageId=%s error=%s",
                message_id,
                exc,
            )

        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ExecutionAlreadyExists":
                logger.info(
                    "Execution already exists (idempotent), skipping: messageId=%s",
                    message_id,
                )
            else:
                logger.exception(
                    "Transient AWS error starting execution, will retry: messageId=%s",
                    message_id,
                )
                raise

        except Exception:
            logger.exception(
                "Unexpected error processing SQS record: messageId=%s",
                message_id,
            )
            raise
