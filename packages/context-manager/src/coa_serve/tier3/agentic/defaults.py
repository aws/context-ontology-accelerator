# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Recommended Deep Reasoning request options, applied when a request leaves them unset.

The values were measured together as one setting (``external-docs/content/serve.md``,
"Deep Reasoning request options"), so a request that names none of them gets that
setting. A request that sets an option keeps its own value, ``false`` included, so
every option can still be switched off per request. ``tokenCaps`` is filled per role.

``DEEP_REASONING_DEFAULT_OPTIONS=off`` (also ``false``, ``0`` or ``no``) on the serve
runtime restores the previous behaviour (every option off unless the request sets
it), as a deployment-wide rollback that needs no code change.
"""

from __future__ import annotations

import os
from typing import Any

RECOMMENDED_OPTIONS: dict[str, Any] = {
    "ontologyLookupMode": "preload",
    "readDocument": True,
    "readDocumentAnchor": True,
    "readDocumentMaxChars": 32000,
    "entityRouting": True,
    "prefetchMode": "assess",
    "loopFixes": True,
    "plannerParseRetry": True,
    "tokenCaps": {"planner": 4000, "synthesizer": 12000},
    "sufficiencyGuards": True,
    "fastPathSpeculation": True,
    "singleCallSteps": True,
    "keepSlowSql": True,
}


_OFF_VALUES = frozenset({"off", "false", "0", "no"})


def defaults_enabled() -> bool:
    """Whether the recommended options apply (``DEEP_REASONING_DEFAULT_OPTIONS`` is not off).

    ``off``, ``false``, ``0`` and ``no`` (any case) switch them off, so a ``false``
    written in ``cdk.json`` means what it says. Anything else, or unset, keeps them.
    """
    return os.environ.get("DEEP_REASONING_DEFAULT_OPTIONS", "recommended").strip().lower() not in _OFF_VALUES


def with_recommended_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``options`` with every unset recommended option filled in (a new dict).

    A key the request sets, to any value, is left as the request set it. For
    ``tokenCaps`` a role the request leaves out gets the recommended cap.
    """
    merged = dict(options or {})
    if not defaults_enabled():
        return merged
    for key, value in RECOMMENDED_OPTIONS.items():
        if key == "tokenCaps":
            caps = merged.get(key)
            if caps is None:
                merged[key] = dict(value)
            elif isinstance(caps, dict):
                merged[key] = {**value, **caps}
            continue
        merged.setdefault(key, value)
    return merged
