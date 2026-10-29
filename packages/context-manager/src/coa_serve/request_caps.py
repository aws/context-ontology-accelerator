# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Per-request output-token caps (``options.tokenCaps``), shared by every caller.

``options.tokenCaps`` is an optional object mapping a model-call role to an output
token cap for this request only — ``planner`` (deep-reasoning planner calls) and
``synthesizer`` (deep-reasoning answer writing). An absent or malformed entry
leaves that role on its configured default. Dependency-free so any layer can
import it.
"""

from __future__ import annotations

from typing import Any

# Bounds for any per-request cap: below 1024 a reasoning model has no room left to
# answer after it thinks; 16000 is a safe ceiling for a single non-streamed call.
TOKEN_CAP_RANGE = (1024, 16000)


def token_cap(options: dict[str, Any] | None, role: str) -> int | None:
    """``options.tokenCaps[role]`` clamped to :data:`TOKEN_CAP_RANGE`, or ``None`` when unset."""
    caps = (options or {}).get("tokenCaps")
    value = caps.get(role) if isinstance(caps, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(TOKEN_CAP_RANGE[0], min(TOKEN_CAP_RANGE[1], value))
