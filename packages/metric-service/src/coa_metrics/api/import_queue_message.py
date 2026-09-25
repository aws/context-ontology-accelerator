# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Validated queue-message contract for asynchronous metric imports."""

from __future__ import annotations

import contextlib
import json
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator

from coa_metrics.api.import_s3_key import IMPORT_S3_KEY_SCOPE_ERROR, is_import_s3_key_for_namespace

INVALID_IMPORT_QUEUE_MESSAGE_ERROR = "Import recovery failed because its queue message was invalid"


class ImportMessageIdentity(BaseModel):
    """Minimum trusted identity needed to attribute a malformed message."""

    model_config = ConfigDict(populate_by_name=True)

    namespace_id: StrictStr = Field(alias="namespaceId", min_length=1)
    job_id: StrictStr = Field(alias="jobId", min_length=1)


class ImportQueueMessage(ImportMessageIdentity):
    """Strict SQS message shared by the worker and DLQ recovery boundaries."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    s3_key: StrictStr = Field(alias="s3Key", min_length=1)
    offset: StrictInt = Field(default=0, ge=0)
    chunk_size: StrictInt = Field(default=50, alias="chunkSize", gt=0)
    automatic_redrive_count: StrictInt = Field(default=0, alias="automaticRedriveCount", ge=0)

    @model_validator(mode="after")
    def validate_s3_key_scope(self) -> Self:
        """Keep queue messages scoped to the owning namespace's import prefix."""
        if not is_import_s3_key_for_namespace(self.s3_key, self.namespace_id):
            raise ValueError(IMPORT_S3_KEY_SCOPE_ERROR)
        return self


class InvalidImportQueueMessage(ValueError):
    """A malformed queue record, optionally attributable to a trusted identity."""

    def __init__(self, reason: str, identity: ImportMessageIdentity | None = None) -> None:
        """Retain the trusted identity while exposing the validation reason."""
        super().__init__(reason)
        self.identity = identity


def identity_from_message_attributes(record: dict[str, Any]) -> ImportMessageIdentity | None:
    """Read authoritative identity attributes, or None for a legacy record without them."""
    attributes = record.get("messageAttributes")
    if attributes is None:
        return None
    if not isinstance(attributes, dict):
        raise ValueError("messageAttributes must be an object")

    identity_values: dict[str, Any] = {}
    for field in ("namespaceId", "jobId"):
        attribute = attributes.get(field)
        if not isinstance(attribute, dict):
            raise ValueError(f"messageAttributes.{field} is required")
        value = attribute.get("stringValue", attribute.get("StringValue"))
        if value is None:
            raise ValueError(f"messageAttributes.{field} must contain a string value")
        identity_values[field] = value

    return ImportMessageIdentity.model_validate(identity_values)


def message_attributes(identity: ImportMessageIdentity) -> dict[str, dict[str, str]]:
    """Build identity attributes that survive body corruption and queue redrive."""
    return {
        "namespaceId": {"DataType": "String", "StringValue": identity.namespace_id},
        "jobId": {"DataType": "String", "StringValue": identity.job_id},
    }


def parse_import_queue_record(record: dict[str, Any]) -> ImportQueueMessage:
    """Parse one Lambda SQS record and reject body/attribute identity mismatches."""
    try:
        attribute_identity = identity_from_message_attributes(record)
    except (TypeError, ValueError, ValidationError) as exc:
        raise InvalidImportQueueMessage(str(exc)) from exc

    try:
        raw_message = json.loads(record["body"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidImportQueueMessage(str(exc), attribute_identity) from exc

    body_identity: ImportMessageIdentity | None = None
    with contextlib.suppress(ValidationError):
        body_identity = ImportMessageIdentity.model_validate(raw_message)

    trusted_identity = attribute_identity or body_identity
    if attribute_identity is not None and body_identity is not None and attribute_identity != body_identity:
        raise InvalidImportQueueMessage("message body identity does not match message attributes", attribute_identity)

    try:
        return ImportQueueMessage.model_validate(raw_message)
    except ValidationError as exc:
        raise InvalidImportQueueMessage(str(exc), trusted_identity) from exc
