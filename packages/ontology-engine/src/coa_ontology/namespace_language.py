# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace language for ontology induction.

The namespace owns one immutable language (``internal-docs/adrs/namespace-language-adr.md``).
Induction entry points resolve it once, from the namespace item, after the route
has bound the authorized namespace, and pass the canonical value to the worker.
Requests and source records never carry a language.

A missing namespace item or a failed read is an error, never an English default.
Only an item without a ``language`` attribute (a namespace created before the
field existed) reads as ``en``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from coa_common.language_tags import (
    DEFAULT_LANGUAGE,
    canonicalize_language_tag,
    effective_namespace_language,
)

log = logging.getLogger(__name__)


class NamespaceLanguageNotFoundError(LookupError):
    """Raised when the namespace metadata item does not exist."""


class NamespaceLanguageUnconfiguredError(RuntimeError):
    """Raised when a deployment with namespace records has no namespaces table configured."""


def resolve_namespace_language(namespace: str, config: dict[str, Any]) -> str:
    """Read the namespace item and return its canonical language.

    ``namespace`` may be the namespace UUID or its name, as elsewhere in the
    engine. A read error propagates unchanged so the caller fails the request.

    Only the local engine run against the HTTP data catalog
    (``catalog_source == "http"``) may lack a namespaces table: it has no
    namespace records and therefore no language authority, so it induces as
    ``en``, the effective language of a namespace that never stored one, and
    logs that it did so. Any other catalog source (``smus`` in every deployed
    stack) without a namespaces table is a configuration error.

    Args:
        namespace: The authorized namespace UUID or name.
        config: The app config (``namespaces_table``, ``smus_region``).

    Returns:
        The canonical namespace language.

    Raises:
        NamespaceLanguageNotFoundError: If the namespace item does not exist.
        NamespaceLanguageUnconfiguredError: If no namespaces table is configured
            and the catalog source is not the local HTTP catalog.
        coa_common.language_tags.LanguageTagError: If the stored value is not a
            supported tag.
        botocore.exceptions.ClientError: If the DynamoDB read fails; not caught
            here, so the caller fails the request instead of guessing a language.
        botocore.exceptions.BotoCoreError: For a client-side DynamoDB failure
            such as a connection error, same rule.
    """
    table_name = config.get("namespaces_table") or os.environ.get("NAMESPACES_TABLE", "")
    if not table_name:
        catalog_source = config.get("catalog_source") or os.environ.get("CATALOG_SOURCE", "http")
        if catalog_source != "http":
            raise NamespaceLanguageUnconfiguredError("NAMESPACES_TABLE env var not configured")
        log.warning(
            "namespace_language_unconfigured namespace=%s language=%s: no namespaces table is configured",
            namespace,
            DEFAULT_LANGUAGE,
        )
        return DEFAULT_LANGUAGE

    import boto3
    from coa_common import resolve_namespace_id

    region = config.get("smus_region") or os.environ.get("AWS_REGION", "us-east-1")
    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    try:
        namespace_id = resolve_namespace_id(namespace, table)
    except ValueError as e:
        raise NamespaceLanguageNotFoundError(str(e)) from e
    item = table.get_item(Key={"PK": f"NS#{namespace_id}", "SK": "METADATA"}).get("Item")
    if not item:
        raise NamespaceLanguageNotFoundError(f"Namespace not found: {namespace}")
    language = canonicalize_language_tag(effective_namespace_language(item))
    log.info("namespace_language_resolved namespace=%s language=%s", namespace_id, language)
    return language
