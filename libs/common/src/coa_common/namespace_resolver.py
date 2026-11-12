# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace name ↔ UUID resolution via DynamoDB.

The control plane stores namespace metadata keyed by UUID (``NS#{uuid}``),
with a secondary name-reservation record (``NS_NAME#{name}``). API consumers
typically pass namespace names, but DDB lookups require the UUID.

This module provides ``resolve_namespace_id()`` — a reusable resolver that
accepts either a name or UUID and always returns the canonical UUID.
"""

from __future__ import annotations

import re

import structlog

logger = structlog.get_logger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


_RESOLVE_CACHE: dict[str, str] = {}


def resolve_namespace_id(namespace: str, table) -> str:
    """Resolve a namespace name or UUID to the canonical UUID.

    Raises:
        ValueError: If the namespace cannot be resolved.
    """
    if namespace in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[namespace]

    if _UUID_RE.fullmatch(namespace):
        resp = table.get_item(Key={"PK": f"NS#{namespace}", "SK": "METADATA"})
        if resp.get("Item"):
            _RESOLVE_CACHE[namespace] = namespace
            return namespace

    # Treat as name — resolve via NS_NAME# reservation record
    name_resp = table.get_item(Key={"PK": f"NS_NAME#{namespace}", "SK": "RESERVATION"})
    name_item = name_resp.get("Item")
    if name_item and name_item.get("namespaceId"):
        resolved = name_item["namespaceId"]
        _RESOLVE_CACHE[namespace] = resolved
        logger.debug("namespace_name_resolved", name=namespace, uuid=resolved)
        return resolved

    # Last attempt: maybe it IS a UUID that doesn't have a METADATA record yet
    if _UUID_RE.fullmatch(namespace):
        _RESOLVE_CACHE[namespace] = namespace
        return namespace

    raise ValueError(f"Cannot resolve namespace '{namespace}' — not found as UUID or name in namespaces table")
