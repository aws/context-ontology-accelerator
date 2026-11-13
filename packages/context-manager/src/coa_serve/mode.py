# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Execution mode — the request-level policy axis, orthogonal to tiers.

* **MODE** — *how* to execute: single-shot (``standard``) or an iterative
  plan/act/assess loop (``deep-reasoning``). Caller-controllable.
* **COMPOSITION** — *what* the namespace holds (structured / unstructured /
  mixed). Derived from the sources registry, never caller-settable.
* **TIER** — a label for the *shape* of the answer that came back (1 = metric,
  2 = rows, 3 = prose).

A namespace with no documents cannot run a document retriever in either mode, and
a namespace with no database cannot run NL→SQL in either mode.
"""

from __future__ import annotations

from enum import StrEnum

import structlog

logger = structlog.get_logger(__name__)


class Mode(StrEnum):
    """How a request is executed. Orthogonal to which tier answers it."""

    STANDARD = "standard"
    """Single-shot: retrieve once, synthesize once. Lower latency."""

    DEEP_REASONING = "deep-reasoning"
    """Iterative: decompose, then loop plan → select tool → invoke → assess."""

    AUTO = "auto"
    """Reserved: pick per-query. Accepted by the parser but not yet dispatched;
    :func:`resolve_mode` never returns it (see ``_resolve_auto``)."""


LEGACY_MODE_ALIASES = {"agentic": Mode.DEEP_REASONING}
"""Pre-rename wire spellings still accepted on ``options.mode``.

``deep-reasoning`` was called ``agentic`` before the rebrand. ``options.mode`` is a
public request field (REST, MCP, benchmark drivers, notebooks), so dropping the old
spelling would 400 every caller pinned to it. Remove once those callers are updated.
"""


def resolve_mode(
    request_mode: str | None,
    *,
    deployment_default: Mode,
    deep_reasoning_available: bool,
) -> tuple[Mode, str]:
    """Resolve the effective mode, request winning over deployment default.

    Reproduces ``Orchestrator._is_deep_reasoning_engaged``'s precedence exactly:
    an explicit request value wins (either direction), absent falls through to
    the deployment default, and an unavailable deep-reasoning retriever forces
    ``standard`` regardless — the reasoning loop cannot run if it was never built.

    An unrecognized request value is treated as absent (warn + fall through)
    rather than raising, matching ``resolve_strategy``'s defensive posture; the
    model layer validates this field, so reaching here means something upstream
    changed.
    """
    mode, source = _requested_or_default(request_mode, deployment_default)

    if mode is Mode.AUTO:
        mode, source = _resolve_auto()

    if mode is Mode.DEEP_REASONING and not deep_reasoning_available:
        logger.warning("deep_reasoning_mode_unavailable", requested_source=source, fallback="standard")
        return Mode.STANDARD, source

    return mode, source


def _requested_or_default(request_mode: str | None, deployment_default: Mode) -> tuple[Mode, str]:
    if request_mode is None:
        return deployment_default, "deployment"
    if request_mode in LEGACY_MODE_ALIASES:
        return LEGACY_MODE_ALIASES[request_mode], "request"
    try:
        return Mode(request_mode), "request"
    except ValueError:
        logger.warning(
            "unrecognized_request_mode",
            request_mode=request_mode,
            action="treating as absent; falling back to deployment default",
        )
        return deployment_default, "deployment"


def _resolve_auto() -> tuple[Mode, str]:
    """Collapse :attr:`Mode.AUTO` to a concrete mode.

    Until per-query classification lands, auto resolves to ``deep-reasoning``: it is
    the higher-recall path, so auto never answers worse than an explicit request would
    — it only costs latency. Kept as its own function so the classifier has exactly
    one seam to replace.
    """
    return Mode.DEEP_REASONING, "auto"
