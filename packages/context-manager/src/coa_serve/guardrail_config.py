# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Live (TTL-refreshed) provider for the serve guardrail configuration.

Background
----------
``ServiceConfig`` is loaded once at process start and frozen for the life of the
container (``config.load_config`` — "called once at startup"). Every value it
carries is therefore fixed until the process is replaced. That is correct for
the ~22 environment-backed fields: an environment variable cannot change without
a new task definition, i.e. a redeploy that replaces the container.

It is NOT correct for the guardrail, which is the one piece of serve config that
can change *underneath a running container*: an operator edits the SSM parameter
``<prefix>/bedrock/guardrail-id`` (no redeploy). After such an edit a
warm, session-pinned playground instance kept applying the pre-edit guardrail
(PII masking rewrote query literals to ``{NAME}``, breaking text-to-SQL) until
the instance eventually cycled — up to the AgentCore microVM lifetime (idle 15m
/ max 8h) later — while freshly-started API/MCP instances already had the new
value. The three SSM guardrail parameters are the *only* serve config that can
drift this way; every other field is environment-backed and cannot.

This provider makes those three parameters live: it re-reads them from SSM on a
short TTL instead of baking them in at startup, so an operator change converges
on every instance within the TTL regardless of how long the instance has been
warm or which microVM a session is pinned to.

Design
------
* **Scope — guardrail only.** ``_get_ssm_parameter`` is the sole SSM read in
  serve and is called for exactly three parameters (``guardrail-id``,
  ``retrieval-guardrail-id``, ``retrieval-guardrail-version``). This provider
  owns those three and nothing else. It is deliberately NOT named
  ``ConfigProvider``: it does not (and must not) front the environment-backed
  fields, for which a TTL re-read would only ever return the same value.
* **TTL + stale-while-revalidate.** A single ``ttl_s`` (default 60s) bounds
  worst-case staleness. Reads are served from cache; when the cache is older
  than the TTL the *first* caller past expiry triggers a refresh and, on
  success, adopts the new value. Guardrail changes are rare, operator-initiated
  events, so 60s is comfortably fresh while collapsing SSM traffic to ~1
  ``GetParameter``/parameter/minute/instance regardless of request volume.
* **Last-known-good on error.** A failing SSM read never fails an invoke and
  never flips the guardrail off: the read raises (see
  ``_read_ssm_parameter_strict``), the refresh is abandoned, and the previously
  cached value is retained without advancing the TTL so the next call retries.
  A genuinely *unset* parameter (``ParameterNotFound``) is not an error — it
  maps to ``""`` (guardrail off), same as ``load_config``. The first load is the
  one case with no fallback — see ``ALLOW_NO_GUARDRAIL`` handling in the
  consumers, which is unchanged.
* **``SERVE_GUARDRAILS_DISABLED`` hard-override.** When the deploy-scoped env
  flag is set, both ids are forced empty and the SSM value is ignored — exactly
  as ``load_config`` does today. An operator "off" must never be silently
  re-enabled by an SSM read. The flag is read live too, but since it is
  environment-backed it cannot actually change without a redeploy; reading it on
  each refresh simply keeps the override authoritative.
* **``none`` sentinel.** ``guardrail-id == "none"`` normalizes to ``""``,
  matching ``load_config`` (``config.py``). ``cdk deploy`` writes ``none`` to
  disable; this provider treats it identically to "unset".

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

# Default TTL for the guardrail SSM parameters. 60s bounds worst-case staleness
# to <=1 minute after an operator edits the parameter (vs. up to the microVM
# lifetime today) while keeping SSM traffic to ~1 GetParameter/param/min/instance
# — far under the account GetParameter throttle. Guardrail changes are rare,
# operator-initiated events, so no finer granularity is warranted.
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
        ssm_prefix: The SSM parameter prefix (e.g. ``/coa``), same value
            ``load_config`` derives from ``SSM_PREFIX``.
        ttl_s: Cache time-to-live in seconds. Defaults to
            :data:`DEFAULT_GUARDRAIL_TTL_S`.
        time_fn: Monotonic clock, injectable for tests. Defaults to
            :func:`time.monotonic`.
        ssm_getter: SSM parameter reader, injectable for tests. Defaults to
            :func:`coa_serve.config._read_ssm_parameter_strict`, which raises on a
            real read failure (so last-known-good engages) and maps only a missing
            parameter to ``""``.
        disabled_fn: Predicate for the ``SERVE_GUARDRAILS_DISABLED`` override,
            injectable for tests. Defaults to
            :func:`coa_serve.config._guardrails_disabled`.
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

    # -- parameter paths ----------------------------------------------------

    @property
    def _guardrail_id_param(self) -> str:
        return f"{self._ssm_prefix}/bedrock/guardrail-id"

    @property
    def _retrieval_guardrail_id_param(self) -> str:
        return f"{self._ssm_prefix}/bedrock/retrieval-guardrail-id"

    @property
    def _retrieval_guardrail_version_param(self) -> str:
        return f"{self._ssm_prefix}/bedrock/retrieval-guardrail-version"

    # -- refresh ------------------------------------------------------------

    def _read_from_ssm(self) -> _GuardrailSnapshot:
        """Read the three parameters and apply the ``none`` + env-disable rules.

        Mirrors ``load_config``'s handling exactly so the live path and the
        startup path can never disagree on how a raw SSM value maps to an
        effective guardrail id.
        """
        if self._disabled_fn():
            # Deploy-scoped kill switch: both ids forced empty, SSM ignored. An
            # operator "off" must never be silently re-enabled by an SSM read, so
            # short-circuit BEFORE any GetParameter call — reading the three
            # parameters only to discard them would be pure waste when disabled.
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
        snapshot is retained (last-known-good) so a transient SSM failure can
        neither fail an invoke nor flip the guardrail off. Only the very first
        load has no fallback; there an SSM failure yields empty ids, which the
        consumers' existing ``ALLOW_NO_GUARDRAIL`` / non-local guards handle.
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
                # Keep the broad catch (any failure must fall back to
                # last-known-good), but surface the AWS error code so operators
                # can distinguish throttling from IAM/permission from network in
                # production, not just the exception class.
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

        This is the primary troubleshooting signal: it confirms that
        an operator's SSM edit was picked up live (and when), and shows the
        effective before/after ids. Logging only on change keeps the steady-state
        60s refresh silent (no per-TTL spam). Guardrail ids are non-secret config
        identifiers (they already appear in request traces), so logging them is
        safe and necessary to answer "did my new guardrail take effect?".
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

    # -- accessors (call these on the hot path) -----------------------------

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
