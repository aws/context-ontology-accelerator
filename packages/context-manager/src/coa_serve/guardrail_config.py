# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live (TTL-refreshed) provider for the serve guardrail configuration.

The guardrail is the one piece of serve config that can change under a running
container: an operator edits the SSM parameter ``<prefix>/bedrock/guardrail-id``
with no redeploy, while every other ``ServiceConfig`` field is environment-backed
and fixed for the container's life. This provider re-reads the three guardrail
parameters (``guardrail-id``, ``retrieval-guardrail-id``,
``retrieval-guardrail-version``) on a short TTL so an operator change reaches
every instance within the TTL. It fronts nothing else.

* **TTL.** Reads are served from cache; the first caller past expiry refreshes.
* **Last-known-good on error.** A failing SSM read never fails an invoke and
  never flips the guardrail off: the previously cached value is retained without
  advancing the TTL, so the next call retries. ``ParameterNotFound`` maps to
  ``""`` (guardrail off), as in ``load_config``. A failure on the first load
  raises.
* **``SERVE_GUARDRAILS_DISABLED`` hard-override.** Both ids are forced empty and
  SSM is ignored, as in ``load_config``.
* **``none`` sentinel.** ``guardrail-id == "none"`` normalizes to ``""``.

The provider is safe to share across threads: refreshes take a lock, and readers
that lose the race return the current cached snapshot without blocking.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import structlog

from .config import (
    _GUARDRAILS_DISABLED_ENV,
    _guardrails_disabled,
    _read_ssm_parameter_strict,
)

logger = structlog.get_logger(__name__)

# Default TTL for the guardrail SSM parameters.
DEFAULT_GUARDRAIL_TTL_S = 60.0


@dataclass(frozen=True)
class _GuardrailSnapshot:
    """Immutable snapshot of the three guardrail parameters at one point in time."""

    guardrail_id: str
    retrieval_guardrail_id: str
    retrieval_guardrail_version: str


class GuardrailConfigProvider:
    """Live, TTL-refreshed view of the serve guardrail SSM parameters.

    Exposes per-field accessors (:meth:`guardrail_id`,
    :meth:`retrieval_guardrail_id`, :meth:`retrieval_guardrail_version`) that
    consumers call *on the hot path* instead of capturing a value at
    construction. Each call returns the current cached value, refreshing from
    SSM in place when the cache is older than the TTL.

    Args:
        ssm_getter: SSM parameter reader. The default raises on a real read
            failure (so last-known-good engages) and maps only a missing
            parameter to ``""``.
    """

    def __init__(
        self,
        ssm_prefix: str,
        *,
        ttl_s: float = DEFAULT_GUARDRAIL_TTL_S,
        time_fn=time.monotonic,
        ssm_getter=_read_ssm_parameter_strict,
        disabled_fn=_guardrails_disabled,
    ) -> None:
        """Bind the SSM prefix, TTL, and injectable clock/reader/override hooks."""
        self._ssm_prefix = ssm_prefix.rstrip("/")
        self._ttl_s = ttl_s
        self._time_fn = time_fn
        self._ssm_getter = ssm_getter
        self._disabled_fn = disabled_fn
        self._lock = threading.Lock()
        self._snapshot: _GuardrailSnapshot | None = None
        self._loaded_at: float = 0.0

    @property
    def _guardrail_id_param(self) -> str:
        return f"{self._ssm_prefix}/bedrock/guardrail-id"

    @property
    def _retrieval_guardrail_id_param(self) -> str:
        return f"{self._ssm_prefix}/bedrock/retrieval-guardrail-id"

    @property
    def _retrieval_guardrail_version_param(self) -> str:
        return f"{self._ssm_prefix}/bedrock/retrieval-guardrail-version"

    def _read_from_ssm(self) -> _GuardrailSnapshot:
        """Read the three parameters and apply the ``none`` + env-disable rules.

        Mirrors ``load_config``'s handling exactly so the live path and the
        startup path can never disagree on how a raw SSM value maps to an
        effective guardrail id.
        """
        if self._disabled_fn():
            # Deploy-scoped kill switch: short-circuit before any GetParameter call.
            logger.error(
                "guardrails_disabled_by_configuration",
                reason=f"{_GUARDRAILS_DISABLED_ENV} is set",
            )
            return _GuardrailSnapshot(
                guardrail_id="",
                retrieval_guardrail_id="",
                retrieval_guardrail_version="DRAFT",
            )

        guardrail_id = self._ssm_getter(self._guardrail_id_param, required=False)
        if guardrail_id == "none":
            guardrail_id = ""
        retrieval_guardrail_id = self._ssm_getter(self._retrieval_guardrail_id_param, required=False)
        retrieval_guardrail_version = (
            self._ssm_getter(self._retrieval_guardrail_version_param, required=False) or "DRAFT"
        )

        return _GuardrailSnapshot(
            guardrail_id=guardrail_id,
            retrieval_guardrail_id=retrieval_guardrail_id,
            retrieval_guardrail_version=retrieval_guardrail_version,
        )

    def _current(self) -> _GuardrailSnapshot:
        """Return the current snapshot, refreshing if the cache has expired.

        Stale-while-revalidate: if a refresh raises, the previously cached
        snapshot is retained (last-known-good). A failure on the very first load
        re-raises.
        """
        now = self._time_fn()
        snapshot = self._snapshot
        if snapshot is not None and (now - self._loaded_at) < self._ttl_s:
            return snapshot

        with self._lock:
            # Re-check under the lock: another thread may have refreshed while we
            # waited. Reuse its result rather than issue a redundant SSM read.
            now = self._time_fn()
            if self._snapshot is not None and (now - self._loaded_at) < self._ttl_s:
                return self._snapshot
            prev = self._snapshot
            try:
                fresh = self._read_from_ssm()
            except Exception as exc:  # noqa: BLE001 — last-known-good is the whole point
                # Surface the AWS error code to tell throttling from IAM from network.
                error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if self._snapshot is not None:
                    logger.warning(
                        "guardrail_config_refresh_failed_serving_cached",
                        error=type(exc).__name__,
                        error_code=error_code,
                        error_msg=str(exc)[:200],
                    )
                    # Do NOT advance _loaded_at: retry on the next call rather
                    # than hold a stale value for a full TTL after a failure.
                    return self._snapshot
                logger.error(
                    "guardrail_config_initial_load_failed",
                    error=type(exc).__name__,
                    error_code=error_code,
                    error_msg=str(exc)[:200],
                )
                raise
            self._snapshot = fresh
            self._loaded_at = now
            self._log_change(prev, fresh)
            return fresh

    def _log_change(self, prev: _GuardrailSnapshot | None, fresh: _GuardrailSnapshot) -> None:
        """Emit an INFO line only when a refresh actually changes the config.

        Guardrail ids are non-secret config identifiers, so logging them is safe.
        """
        if prev is None:
            logger.info(
                "guardrail_config_loaded",
                guardrail_id=fresh.guardrail_id or None,
                retrieval_guardrail_id=fresh.retrieval_guardrail_id or None,
                retrieval_guardrail_version=fresh.retrieval_guardrail_version,
            )
            return
        if (
            prev.guardrail_id != fresh.guardrail_id
            or prev.retrieval_guardrail_id != fresh.retrieval_guardrail_id
            or prev.retrieval_guardrail_version != fresh.retrieval_guardrail_version
        ):
            logger.info(
                "guardrail_config_changed",
                old_guardrail_id=prev.guardrail_id or None,
                new_guardrail_id=fresh.guardrail_id or None,
                old_retrieval_guardrail_id=prev.retrieval_guardrail_id or None,
                new_retrieval_guardrail_id=fresh.retrieval_guardrail_id or None,
                old_retrieval_guardrail_version=prev.retrieval_guardrail_version,
                new_retrieval_guardrail_version=fresh.retrieval_guardrail_version,
                ttl_s=self._ttl_s,
            )

    def guardrail_id(self) -> str:
        """Current input guardrail id ("" when unset/none/disabled)."""
        return self._current().guardrail_id

    def retrieval_guardrail_id(self) -> str:
        """Current retrieval (chunk-screening) guardrail id ("" when unset/disabled)."""
        return self._current().retrieval_guardrail_id

    def retrieval_guardrail_version(self) -> str:
        """Current retrieval guardrail version (defaults to "DRAFT")."""
        return self._current().retrieval_guardrail_version

    def prime(self) -> None:
        """Eagerly perform the first SSM read (optional; accessors do it lazily)."""
        self._current()


def build_guardrail_config_provider(ttl_s: float = DEFAULT_GUARDRAIL_TTL_S) -> GuardrailConfigProvider:
    """Construct a provider using the same ``SSM_PREFIX`` resolution as ``load_config``."""
    ssm_prefix = os.environ.get("SSM_PREFIX", "/coa")
    return GuardrailConfigProvider(ssm_prefix, ttl_s=ttl_s)
