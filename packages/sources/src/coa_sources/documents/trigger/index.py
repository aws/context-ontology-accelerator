# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Ingestion Trigger Lambda.

Reads SQS messages and starts a Step Functions execution for each one.
Malformed messages are dropped (logged and deleted from the queue).
Transient AWS errors are re-raised so SQS retries the message.
"""

from __future__ import annotations

import json
import os

import boto3
import structlog
from botocore.exceptions import ClientError
from coa_common.constants import EXTRACTION_DEFAULTS
from coa_common.logging import setup_logging

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = structlog.get_logger(__name__)

sfn_client = boto3.client("stepfunctions")

_STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]
_BEDROCK_MODEL_ARN = os.environ.get("BEDROCK_MODEL_ARN", "")

_REQUIRED_FIELDS = ("namespace_id", "doc_source_id")

# Merge static defaults with the runtime Bedrock model ARN.
_EXTRACTION_DEFAULTS = {
    **EXTRACTION_DEFAULTS,
    "bedrock_model_arn": _BEDROCK_MODEL_ARN,
}


def _stringify_config_value(v: object) -> object:
    """Coerce an extraction_config value to its SFN container-override string form.

    Step Functions container-override JsonPath cannot inline a JSON object or
    array and ECS env-var values must be strings, so: bools -> lower-case
    ``"true"``/``"false"``, lists -> JSON (``preferred_entity_classifications``
    and ``preferred_topics``; the container json.loads them back at boot),
    ints -> ``str``. Anything already a string (or an unexpected type) passes
    through unchanged.

    NOTE: bool is checked before int because ``isinstance(True, int)`` is True.
    """
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, list):
        return json.dumps(v)
    if isinstance(v, int):
        return str(v)
    return v


def handler(event: dict, context: object) -> None:
    """Lambda entry-point — process one SQS record per invocation (batchSize=1)."""
    for record in event["Records"]:
        message_id: str = record.get("messageId", "unknown")
        try:
            body = json.loads(record["body"])

            missing = [f for f in _REQUIRED_FIELDS if f not in body]
            if missing:
                logger.error("malformed_sqs_message", missing_fields=missing, message_id=message_id)
                continue

            body["extraction_config"] = {
                **_EXTRACTION_DEFAULTS,
                **{k: v for k, v in body.get("extraction_config", {}).items() if v is not None},
            }
            # ECS container environment variable values must be strings, and
            # Step Functions container-override JsonPath cannot inline a JSON
            # object/array — see _stringify_config_value for the per-type rules.
            ec = body["extraction_config"]
            body["extraction_config"] = {k: _stringify_config_value(v) for k, v in ec.items()}

            logger.info(
                "starting_execution",
                namespace_id=body["namespace_id"],
                doc_source_id=body["doc_source_id"],
                message_id=message_id,
            )
            sfn_client.start_execution(
                stateMachineArn=_STATE_MACHINE_ARN,
                input=json.dumps(body),
            )

        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("malformed_message_body", message_id=message_id, error=str(exc))

        except ClientError:
            logger.exception("transient_aws_error", message_id=message_id)
            raise

        except Exception:
            logger.exception("unexpected_error", message_id=message_id)
            raise
