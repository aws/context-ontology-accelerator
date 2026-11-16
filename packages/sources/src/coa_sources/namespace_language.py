# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace language for source processing.

The namespace owns one immutable language. Sources never carry their own:

* the Sources API resolves it from the namespace item after namespace
  authorization, once, when a scan or ingestion job is triggered, and puts the
  canonical tag into the job input;
* workers read that snapshot and never re-read the namespace, so a retried or
  resumed step uses the value its job started with.

A missing namespace item or a failed read is an error, never an English
default. Only an item that exists without a ``language`` attribute (a namespace
created before the field existed) reads as ``en``.
"""

from __future__ import annotations

from typing import Any

from coa_common.dao import DynamoDBDAO
from coa_common.language_tags import (
    LanguageTagError,
    canonicalize_language_tag,
    effective_namespace_language,
)

__all__ = [
    "NAMESPACE_LANGUAGE_ENV",
    "NamespaceNotFoundError",
    "namespace_language_from_item",
    "require_language_snapshot",
    "resolve_namespace_language",
]

NAMESPACE_LANGUAGE_ENV = "NAMESPACE_LANGUAGE"
"""Worker environment variable carrying the job's namespace-language snapshot."""


class NamespaceNotFoundError(LookupError):
    """Raised when the namespace metadata item does not exist."""


def namespace_language_from_item(namespace_item: dict[str, Any] | None, namespace_id: str) -> str:
    """Return the canonical language of an already-loaded namespace item.

    Raises:
        NamespaceNotFoundError: If the item is missing.
        LanguageTagError: If the stored value is not a supported tag.
    """
    if not namespace_item:
        raise NamespaceNotFoundError(f"Namespace not found: {namespace_id}")
    return canonicalize_language_tag(effective_namespace_language(namespace_item))


def resolve_namespace_language(ns_dao: DynamoDBDAO, namespace_id: str) -> str:
    """Read the namespace item and return its canonical language.

    A read error propagates unchanged; the caller fails the request.

    Raises:
        NamespaceNotFoundError: If the item is missing, or the namespaces table
            does not exist (``DynamoDBDAO.get`` returns ``None`` for that).
        LanguageTagError: If the stored value is not a supported tag.
        botocore.exceptions.ClientError: For any other DynamoDB error, which
            ``DynamoDBDAO.get`` re-raises.
        botocore.exceptions.BotoCoreError: For a client-side DynamoDB failure
            such as a connection error.
    """
    item = ns_dao.get({"PK": f"NS#{namespace_id}", "SK": "METADATA"})
    return namespace_language_from_item(item, namespace_id)


def require_language_snapshot(value: str | None) -> str:
    """Validate the namespace-language snapshot a worker received in its job input.

    A job input without the snapshot was enqueued by an older API revision or
    tampered with. The worker fails rather than defaulting or re-reading the
    namespace: re-triggering the job resolves the language again.

    Raises:
        ValueError: If the snapshot is absent or not a supported tag.
    """
    if not value:
        raise ValueError(f"{NAMESPACE_LANGUAGE_ENV} is required: the job input carries no namespace language")
    try:
        return canonicalize_language_tag(value)
    except LanguageTagError as exc:
        raise ValueError(f"{NAMESPACE_LANGUAGE_ENV} is not a supported language tag: {exc}") from exc
