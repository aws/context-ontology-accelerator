# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace-language label preference for the catalog graph and schema readers.

The graph routers name each vertex, search hit, overview entry and schema class
with one label chosen from the namespace's immutable language
(namespace-language LLD §2.2.5), the same ranking the serve readers use
(:mod:`coa_common.label_language`). The language is read from the namespace item
on the server, once per request; no request parameter can change it.

A missing namespace item or a failed read is an error, never an English
default: only an existing item without a ``language`` attribute (a namespace
created before the field) reads as ``en``. A deployment with no namespaces
table configured has no namespace metadata to read and uses
:meth:`LabelPreference.untagged_first`.
"""

from __future__ import annotations

import logging
import os
import threading

import boto3
from coa_common import resolve_namespace_id, resolve_region
from coa_common.label_language import LabelPreference
from coa_common.language_tags import LanguageTagError, canonicalize_language_tag, effective_namespace_language
from fastapi import HTTPException

log = logging.getLogger(__name__)

# Namespace language is immutable once the namespace exists, so a cached value
# never goes stale. Bounded so a long-lived container cannot grow without limit.
_CACHE_MAX = 1024
_language_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def _namespaces_table():
    table_name = os.environ.get("NAMESPACES_TABLE", "")
    if not table_name:
        return None
    region = os.environ.get("DYNAMODB_REGION") or resolve_region()
    return boto3.resource("dynamodb", region_name=region).Table(table_name)


def namespace_language(namespace: str) -> str | None:
    """Return the namespace's canonical effective language.

    Returns:
        The language, or ``None`` when no namespaces table is configured.

    Raises:
        HTTPException: 404 when the namespace does not exist; 503 when the item
            cannot be read or holds an invalid language.
    """
    table = _namespaces_table()
    if table is None:
        return None
    try:
        namespace_id = resolve_namespace_id(namespace, table)
    except ValueError as exc:
        raise HTTPException(404, f"Namespace '{namespace}' not found.") from exc
    except Exception as exc:
        log.warning("namespace language lookup failed for %s: %s", namespace, type(exc).__name__)
        raise HTTPException(503, "Unable to read namespace language.") from exc
    # Keyed by the resolved UUID, never the caller's name: a name can be reused
    # by a later namespace, while the UUID's language is immutable.
    with _cache_lock:
        cached = _language_cache.get(namespace_id)
    if cached is not None:
        return cached
    try:
        item = table.get_item(Key={"PK": f"NS#{namespace_id}", "SK": "METADATA"}).get("Item")
    except Exception as exc:
        log.warning("namespace language read failed for %s: %s", namespace, type(exc).__name__)
        raise HTTPException(503, "Unable to read namespace language.") from exc
    if not item:
        raise HTTPException(404, f"Namespace '{namespace}' not found.")
    try:
        language = canonicalize_language_tag(effective_namespace_language(item))
    except (LanguageTagError, TypeError, AttributeError) as exc:
        log.warning("namespace %s has an invalid stored language", namespace)
        raise HTTPException(503, "Unable to read namespace language.") from exc
    with _cache_lock:
        if len(_language_cache) >= _CACHE_MAX:
            _language_cache.pop(next(iter(_language_cache)))
        _language_cache[namespace_id] = language
    return language


def label_preference_for(namespace: str) -> LabelPreference:
    """Return the label preference for ``namespace`` (see :func:`namespace_language`)."""
    language = namespace_language(namespace)
    if language is None:
        return LabelPreference.untagged_first()
    return LabelPreference.for_namespace(language)
