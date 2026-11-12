# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Withhold table/column overrides from grant summaries for schema-blind callers.

A grant's ``tableAllowlist`` and
``columnDenylist`` name physical tables and columns, which a caller without
``viewSource`` (e.g. ``data-analyst``) must not see. ``allowedMetrics`` names
governed metrics, which every role may read, so it is kept.

The rights come from the API Gateway authorizer context (flag names and parsing
are shared through :mod:`coa_authorization.schema_visibility`):

* namespace listing: ``canViewSource`` for that namespace;
* the caller's own listing (which spans namespaces): ``viewSourceNamespaces``, a
  comma-separated list of the namespace ids where the caller holds
  ``viewSource``; ``*`` means a platform-wide grant, so every namespace is
  visible.

Fail closed: a request whose context lacks the key is treated as schema-blind
and the overrides are stripped. The authorizer always emits the key on these
routes (see ``authorization/handler.py``).
"""

from __future__ import annotations

from typing import Any

from coa_authorization.schema_visibility import (
    PLATFORM_NAMESPACE,
    from_authorizer_context,
    from_namespace_list,
)

_SCHEMA_OVERRIDE_KEYS = ("tableAllowlist", "columnDenylist")


def _authorizer(event: dict[str, Any]) -> dict[str, Any]:
    return (event.get("requestContext") or {}).get("authorizer") or {}


def _strip(grant: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in grant.items() if k not in _SCHEMA_OVERRIDE_KEYS}


def withhold_namespace_grant_overrides(grants: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
    """Namespace listing: keep overrides only when ``canViewSource`` is ``"true"``."""
    if from_authorizer_context(_authorizer(event), missing="blind").source:
        return grants
    return [_strip(g) for g in grants]


def withhold_principal_grant_overrides(grants: list[dict[str, Any]], event: dict[str, Any]) -> list[dict[str, Any]]:
    """Own listing: keep overrides only on grants in a namespace named in ``viewSourceNamespaces``."""
    allowed = from_namespace_list(_authorizer(event))
    platform_wide = PLATFORM_NAMESPACE in allowed
    out = []
    for grant in grants:
        visible = platform_wide or str(grant.get("namespaceId", "")) in allowed
        out.append(grant if visible else _strip(grant))
    return out
