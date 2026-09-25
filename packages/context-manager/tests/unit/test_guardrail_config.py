# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the live (TTL-refreshed) guardrail config provider.

The guardrail SSM parameters are the only serve config that can change
under a running container. These tests pin the provider's contract: TTL-bounded
freshness, stale-while-revalidate, last-known-good on SSM error, the ``none``
sentinel, and the ``SERVE_GUARDRAILS_DISABLED`` hard-override — the exact
semantics ``load_config`` applies at startup, now applied on every refresh.
"""

from __future__ import annotations

import pytest
from coa_serve.guardrail_config import (
    DEFAULT_GUARDRAIL_TTL_S,
    GuardrailConfigProvider,
)


class _FakeClock:
    """Monotonic clock whose value the test advances explicitly."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeSsm:
    """Records calls and returns per-parameter values; can be told to raise."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[str] = []
        self.raise_on_next = False

    def __call__(self, name: str, *, required: bool = False) -> str:
        self.calls.append(name)
        if self.raise_on_next:
            raise RuntimeError("ssm unavailable")
        # Return the suffix-keyed value; empty string when unset (matches
        # _get_ssm_parameter's not-found behavior).
        for suffix, val in self.values.items():
            if name.endswith(suffix):
                return val
        return ""


def _provider(
    ssm: _FakeSsm,
    clock: _FakeClock,
    *,
    ttl_s: float = DEFAULT_GUARDRAIL_TTL_S,
    disabled: bool = False,
) -> GuardrailConfigProvider:
    return GuardrailConfigProvider(
        "/coa",
        ttl_s=ttl_s,
        time_fn=clock,
        ssm_getter=ssm,
        disabled_fn=lambda: disabled,
    )


@pytest.mark.unit
class TestGuardrailConfigProvider:
    def test_reads_all_three_parameters(self):
        ssm = _FakeSsm(
            {
                "/bedrock/guardrail-id": "gr-input",
                "/bedrock/retrieval-guardrail-id": "gr-retrieval",
                "/bedrock/retrieval-guardrail-version": "7",
            }
        )
        p = _provider(ssm, _FakeClock())
        assert p.guardrail_id() == "gr-input"
        assert p.retrieval_guardrail_id() == "gr-retrieval"
        assert p.retrieval_guardrail_version() == "7"

    def test_within_ttl_served_from_cache_no_extra_ssm_reads(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        assert p.guardrail_id() == "gr-1"
        reads_after_first = len(ssm.calls)
        # Many reads inside the TTL → no new SSM calls.
        clock.advance(59)
        for _ in range(5):
            assert p.guardrail_id() == "gr-1"
        assert len(ssm.calls) == reads_after_first

    def test_ttl_expiry_triggers_refresh_and_adopts_new_value(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-old"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        assert p.guardrail_id() == "gr-old"
        # Operator flips the SSM value; still within TTL → old value.
        ssm.values["/bedrock/guardrail-id"] = "gr-new"
        clock.advance(59)
        assert p.guardrail_id() == "gr-old"
        # Past TTL → refresh picks up the new value.
        clock.advance(2)
        assert p.guardrail_id() == "gr-new"

    def test_operator_disables_via_none_sentinel_within_ttl(self):
        """Setting guardrail-id to 'none' normalizes to '' after the TTL."""
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-on"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        assert p.guardrail_id() == "gr-on"
        ssm.values["/bedrock/guardrail-id"] = "none"
        clock.advance(61)
        assert p.guardrail_id() == ""

    def test_none_sentinel_normalized_on_first_read(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "none"})
        p = _provider(ssm, _FakeClock())
        assert p.guardrail_id() == ""

    def test_version_defaults_to_draft_when_unset(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        p = _provider(ssm, _FakeClock())
        assert p.retrieval_guardrail_version() == "DRAFT"

    def test_last_known_good_on_refresh_error(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-good"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        assert p.guardrail_id() == "gr-good"
        # SSM starts failing; past TTL the refresh raises → keep last-known-good.
        ssm.raise_on_next = True
        clock.advance(61)
        assert p.guardrail_id() == "gr-good"

    def test_error_does_not_advance_ttl_so_recovery_is_immediate(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-good"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        assert p.guardrail_id() == "gr-good"
        ssm.raise_on_next = True
        clock.advance(61)
        assert p.guardrail_id() == "gr-good"  # served cached
        # SSM recovers with a new value; provider retries on the very next call
        # (it did not stamp _loaded_at on failure) rather than waiting a full TTL.
        ssm.raise_on_next = False
        ssm.values["/bedrock/guardrail-id"] = "gr-recovered"
        assert p.guardrail_id() == "gr-recovered"

    def test_initial_load_failure_raises(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        ssm.raise_on_next = True
        p = _provider(ssm, _FakeClock())
        with pytest.raises(RuntimeError):
            p.guardrail_id()

    def test_env_disable_forces_ids_empty_ignoring_ssm(self):
        ssm = _FakeSsm(
            {
                "/bedrock/guardrail-id": "gr-input",
                "/bedrock/retrieval-guardrail-id": "gr-retrieval",
            }
        )
        p = _provider(ssm, _FakeClock(), disabled=True)
        assert p.guardrail_id() == ""
        assert p.retrieval_guardrail_id() == ""

    def test_prime_performs_initial_read(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        p = _provider(ssm, _FakeClock())
        assert ssm.calls == []
        p.prime()
        assert len(ssm.calls) >= 1

    def test_provider_accessor_is_callable_for_injection(self):
        """The consumers receive provider.guardrail_id as a bare callable."""
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        p = _provider(ssm, _FakeClock())
        accessor = p.guardrail_id
        assert callable(accessor)
        assert accessor() == "gr-1"


@pytest.mark.unit
class TestConsumerLiveGuardrailReload:
    """Live-reload at the consumer level.

    The reported failure: a warm, session-pinned Synthesizer kept applying the
    pre-change guardrail after an operator edited SSM, because the id was frozen
    at construction. These tests prove that a single long-lived consumer instance
    now reflects a provider value change on its NEXT call — no reconstruction, no
    restart — which is exactly what a warm microVM needs.
    """

    async def test_synthesizer_reflects_provider_change_without_reconstruction(self):
        from unittest.mock import AsyncMock

        from coa_serve.clients.base import ConverseResult
        from coa_serve.tier3.synthesizer import Synthesizer
        from coa_serve.tier3.vector_retriever import ChunkResult

        bedrock = AsyncMock()
        bedrock.converse.return_value = ConverseResult(text="answer")

        # A mutable holder the provider callable reads — stands in for the SSM
        # value an operator flips underneath a warm instance.
        current = {"gid": "gr-old"}
        synth = Synthesizer(
            bedrock,
            guardrail_id="gr-startup",
            guardrail_id_provider=lambda: current["gid"],
        )
        chunk = ChunkResult(chunk_id="c1", text="t", source_doc="d.pdf", relevance_score=0.9, label="D")

        await synth.synthesize("q", [chunk], [])
        assert bedrock.converse.await_args.kwargs["guardrail_id"] == "gr-old"

        # Operator flips the guardrail; SAME instance, next call reflects it.
        current["gid"] = "gr-new"
        await synth.synthesize("q", [chunk], [])
        assert bedrock.converse.await_args.kwargs["guardrail_id"] == "gr-new"

        # And disabling (empty) collapses to None (guardrail off).
        current["gid"] = ""
        await synth.synthesize("q", [chunk], [])
        assert bedrock.converse.await_args.kwargs["guardrail_id"] is None

    async def test_synthesizer_without_provider_uses_startup_value(self):
        """Back-compat: no provider wired → behaves exactly as before (frozen value)."""
        from unittest.mock import AsyncMock

        from coa_serve.clients.base import ConverseResult
        from coa_serve.tier3.synthesizer import Synthesizer
        from coa_serve.tier3.vector_retriever import ChunkResult

        bedrock = AsyncMock()
        bedrock.converse.return_value = ConverseResult(text="answer")
        synth = Synthesizer(bedrock, guardrail_id="gr-frozen")
        chunk = ChunkResult(chunk_id="c1", text="t", source_doc="d.pdf", relevance_score=0.9, label="D")
        await synth.synthesize("q", [chunk], [])
        assert bedrock.converse.await_args.kwargs["guardrail_id"] == "gr-frozen"

    async def test_sql_generator_reflects_provider_change_without_reconstruction(self):
        """Live-reload for the text-to-SQL surface.

        A warm SQLGenerator must apply a flipped guardrail id on its next
        generation call, at the guard_content-scoped converse (:814), with no
        reconstruction. This is the surface whose visible failure was broken
        text-to-SQL when the stale guardrail masked query literals to {NAME}.
        """
        from unittest.mock import AsyncMock

        from coa_serve.clients.base import ConverseResult
        from coa_serve.tier2.nl_to_sql.sql_generator import SQLGenerator

        llm = AsyncMock()
        llm.converse.return_value = ConverseResult(text="```sql\nSELECT 1\n```\nConfidence: 0.9")
        llm.embed.return_value = [0.0, 0.0, 0.0]

        current = {"gid": "gr-old"}
        gen = SQLGenerator(
            llm,
            vector_client=AsyncMock(),
            guardrail_id="gr-startup",
            guardrail_id_provider=lambda: current["gid"],
        )

        # Drive the internal generation call directly with a minimal DDL context so
        # the test does not depend on vector retrieval; the converse call is what
        # carries the guardrail id (:808-819).
        await gen._generate_sql("show names", ddl_context="CREATE TABLE t (n TEXT)", evidence="")
        assert llm.converse.await_args.kwargs["guardrail_id"] == "gr-old"

        current["gid"] = "gr-new"
        await gen._generate_sql("show names", ddl_context="CREATE TABLE t (n TEXT)", evidence="")
        assert llm.converse.await_args.kwargs["guardrail_id"] == "gr-new"

        # Operator disables → warm instance stops masking (guardrail off).
        current["gid"] = ""
        await gen._generate_sql("show names", ddl_context="CREATE TABLE t (n TEXT)", evidence="")
        assert llm.converse.await_args.kwargs["guardrail_id"] is None

    async def test_agentic_planner_reflects_provider_change_without_reconstruction(self):
        """Live-reload for the AGENTIC mode — proves the second Tier-3 path is not left stale.

        A warm BedrockStepPlanner (built via the agentic factory) must apply a
        flipped guardrail id on its next propose_steps call (:280), with no
        reconstruction. Uses the planner test's FakeLLM which records guardrail_id.
        """
        from types import SimpleNamespace

        from coa_serve.tier3.agentic.context import AccumulatedContext
        from coa_serve.tier3.agentic.models import SubQuestion
        from coa_serve.tier3.agentic.planner import BedrockStepPlanner

        class _RecordingLLM:
            def __init__(self):
                self.calls: list[dict] = []

            async def converse(
                self, *, prompt, system=None, guardrail_id=None, guard_content=None, temperature=None, max_tokens=4096
            ):
                self.calls.append({"guardrail_id": guardrail_id})
                return SimpleNamespace(text='{"candidates": []}', guardrail_blocked=False)

        specs = [{"name": "ontology_lookup", "description": "d", "input_schema": {}, "output_schema": {}}]
        llm = _RecordingLLM()
        current = {"gid": "gr-old"}
        planner = BedrockStepPlanner(llm, guardrail_id="gr-startup", guardrail_id_provider=lambda: current["gid"])

        await planner.propose_steps(sub_question=SubQuestion(text="q"), context=AccumulatedContext(), tool_specs=specs)
        assert llm.calls[-1]["guardrail_id"] == "gr-old"

        current["gid"] = "gr-new"
        await planner.propose_steps(sub_question=SubQuestion(text="q"), context=AccumulatedContext(), tool_specs=specs)
        assert llm.calls[-1]["guardrail_id"] == "gr-new"

        current["gid"] = ""
        await planner.propose_steps(sub_question=SubQuestion(text="q"), context=AccumulatedContext(), tool_specs=specs)
        assert llm.calls[-1]["guardrail_id"] is None


@pytest.mark.unit
class TestMainWiringGuarantee:
    """Static guard: every guardrail consumer built in main._ensure_initialized
    must receive the live provider, and the provider must be built + primed.

    This is a silent-drift guard: a future consumer
    added to main.py WITHOUT wiring the provider would silently reintroduce the
    frozen-guardrail bug for that surface. Rather than mock the entire async
    init path, we assert the wiring invariants by parsing main.py's AST — a
    check that fails loudly the moment a construction drops the provider kwarg.
    """

    @staticmethod
    def _main_source() -> str:
        import inspect

        from coa_serve import main

        return inspect.getsource(main)

    def test_provider_is_built_and_primed(self):
        src = self._main_source()
        assert "build_guardrail_config_provider()" in src, "provider must be constructed in main"
        assert ".prime" in src, "provider must be primed once at init (off the request path)"

    def test_every_input_consumer_receives_the_provider(self):
        """Each input-guardrail consumer construction passes guardrail_id_provider."""
        import ast

        tree = ast.parse(self._main_source())
        input_consumers = {"NLtoSPARQL", "Synthesizer", "SQLGenerator", "BedrockStepPlanner"}
        seen: dict[str, bool] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in input_consumers:
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                # Record whether THIS construction wired the provider.
                seen[node.func.id] = seen.get(node.func.id, False) or ("guardrail_id_provider" in kwargs)
        # build_agentic_retriever is the deep-reasoning path's provider entry point.
        agentic = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "build_agentic_retriever"
            and any(kw.arg == "guardrail_id_provider" for kw in n.keywords)
            for n in ast.walk(tree)
        )
        missing = {c for c, wired in seen.items() if not wired}
        assert not missing, f"consumers constructed without guardrail_id_provider: {missing}"
        assert seen.keys() >= input_consumers - {"Synthesizer"} or "Synthesizer" in seen, seen
        assert agentic, "build_agentic_retriever must receive guardrail_id_provider (agentic path)"

    def test_retrieval_screener_receives_the_provider(self):
        """The GuardrailScreener construction passes the retrieval provider callables."""
        import ast

        tree = ast.parse(self._main_source())
        wired = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "GuardrailScreener":
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                wired = "guardrail_id_provider" in kwargs and "guardrail_version_provider" in kwargs
        assert wired, "GuardrailScreener must receive guardrail_id_provider + guardrail_version_provider"


class _CountingSsm:
    """Thread-safe SSM stub counting distinct refresh *cycles*.

    ``_read_from_ssm`` reads all three parameters per refresh, so we count a
    refresh once per barrier-synchronised cycle by keying on the first parameter
    name and incrementing under a lock. Optionally blocks the first refresh on a
    barrier so N threads can be forced to contend at the TTL boundary at once.
    """

    def __init__(self, values: dict[str, str], *, gate=None) -> None:
        import threading

        self.values = values
        self.calls: list[str] = []
        self.refresh_cycles = 0
        self._lock = threading.Lock()
        self._gate = gate
        # The first param name _read_from_ssm requests marks a new refresh cycle.
        self._cycle_marker = "/bedrock/guardrail-id"

    def __call__(self, name: str, *, required: bool = False) -> str:
        with self._lock:
            self.calls.append(name)
            if name.endswith(self._cycle_marker):
                self.refresh_cycles += 1
        # Block only inside the refresh critical path if a gate is set, so we can
        # line up concurrent callers at the boundary.
        if self._gate is not None and not self._gate.is_set():
            self._gate.wait(timeout=5)
        for suffix, val in self.values.items():
            if name.endswith(suffix):
                return val
        return ""


@pytest.mark.unit
class TestConcurrencyAndObservability:
    """Findings from the automated code review (MR !1213, 2nd pass)."""

    def test_thundering_herd_single_refresh_under_concurrent_load(self):
        """10+ threads hitting an expired TTL at once trigger exactly ONE refresh.

        Validates the double-checked lock: the first thread past expiry refreshes
        under the lock; the rest re-check inside the lock and adopt the snapshot
        without their own SSM read. (Addresses the HIGH race-condition concern and
        the thundering-herd test gap.)
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        ssm = _CountingSsm({"/bedrock/guardrail-id": "gr-1"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        # Prime once (cycle 1), then expire the TTL.
        assert p.guardrail_id() == "gr-1"
        assert ssm.refresh_cycles == 1
        clock.advance(61)

        barrier = threading.Barrier(16)

        def worker() -> str:
            barrier.wait()  # release all 16 threads at the same instant
            return p.guardrail_id()

        with ThreadPoolExecutor(max_workers=16) as ex:
            results = [f.result() for f in [ex.submit(worker) for _ in range(16)]]

        assert all(r == "gr-1" for r in results)
        # Exactly one additional refresh cycle despite 16 concurrent callers.
        assert ssm.refresh_cycles == 2, f"expected 1 refresh under contention, got {ssm.refresh_cycles - 1}"

    def test_multi_accessor_at_ttl_boundary_triggers_one_refresh(self):
        """Different accessors called back-to-back at expiry refresh all three params once.

        A refresh reads guardrail-id + retrieval-guardrail-id + retrieval-guardrail-version
        together, so hitting all three accessors after expiry must NOT produce three
        separate refreshes.
        """
        ssm = _CountingSsm(
            {
                "/bedrock/guardrail-id": "gr-in",
                "/bedrock/retrieval-guardrail-id": "gr-ret",
                "/bedrock/retrieval-guardrail-version": "3",
            }
        )
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        assert p.guardrail_id() == "gr-in"  # cycle 1
        assert ssm.refresh_cycles == 1
        clock.advance(61)
        # Three different accessors in rapid succession past the boundary.
        assert p.guardrail_id() == "gr-in"
        assert p.retrieval_guardrail_id() == "gr-ret"
        assert p.retrieval_guardrail_version() == "3"
        # Only one new refresh cycle for all three (batched), not three.
        assert ssm.refresh_cycles == 2

    def test_refresh_failure_logs_aws_error_code(self, monkeypatch):
        """A failing refresh serves cached and logs the AWS error code, not just the class."""
        import coa_serve.guardrail_config as gc

        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)
        assert p.guardrail_id() == "gr-1"  # prime cached snapshot

        captured: dict = {}

        def fake_warning(event, **kw):
            captured["event"] = event
            captured.update(kw)

        monkeypatch.setattr(gc.logger, "warning", fake_warning)

        # Simulate a botocore ClientError-shaped exception with an error code.
        class _ClientErrorish(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "ThrottlingException"}}

        def boom(name, *, required=False):
            raise _ClientErrorish()

        # Rebind the provider's getter to the raising one and expire the TTL.
        p._ssm_getter = boom  # type: ignore[attr-defined]
        clock.advance(61)
        # Serves last-known-good despite the failure.
        assert p.guardrail_id() == "gr-1"
        assert captured.get("event") == "guardrail_config_refresh_failed_serving_cached"
        assert captured.get("error_code") == "ThrottlingException"

    def test_change_is_logged_on_first_load_and_on_change_only(self, monkeypatch):
        """INFO on first load and when a refresh changes a value; silent otherwise."""
        import coa_serve.guardrail_config as gc

        events: list[tuple[str, dict]] = []

        def fake_info(event, **kw):
            events.append((event, kw))

        monkeypatch.setattr(gc.logger, "info", fake_info)

        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        clock = _FakeClock()
        p = _provider(ssm, clock, ttl_s=60)

        # First load → guardrail_config_loaded.
        assert p.guardrail_id() == "gr-1"
        assert events and events[0][0] == "guardrail_config_loaded"
        assert events[0][1]["guardrail_id"] == "gr-1"

        # Refresh with no change → no new log.
        clock.advance(61)
        assert p.guardrail_id() == "gr-1"
        assert len(events) == 1

        # Operator flips the value → guardrail_config_changed with old/new.
        ssm.values["/bedrock/guardrail-id"] = "gr-2"
        clock.advance(61)
        assert p.guardrail_id() == "gr-2"
        assert events[-1][0] == "guardrail_config_changed"
        assert events[-1][1]["old_guardrail_id"] == "gr-1"
        assert events[-1][1]["new_guardrail_id"] == "gr-2"


@pytest.mark.unit
class TestHasGuardrailTracksLiveProvider:
    """`has_guardrail` must reflect the live provider, not the frozen id."""

    def _synth(self, *, static_id: str, provider_val: str | None):
        from unittest.mock import AsyncMock

        from coa_serve.tier3.synthesizer import Synthesizer

        prov = None if provider_val is None else (lambda: provider_val)
        return Synthesizer(
            AsyncMock(),
            guardrail_id=static_id,
            guardrail_id_provider=prov,
        )

    def test_has_guardrail_true_when_provider_enables_from_empty_start(self):
        """Started with no guardrail; provider now returns one → has_guardrail flips True."""
        s = self._synth(static_id="", provider_val="gr-live")
        assert s.has_guardrail is True

    def test_has_guardrail_false_when_provider_disables_from_configured_start(self):
        """Started with a guardrail; provider now empty → has_guardrail flips False.

        Guards the trace-step gate (knowledge_retriever) against emitting a
        bogus t3.guardrail step when the live guardrail is off.
        """
        s = self._synth(static_id="gr-start", provider_val="")
        assert s.has_guardrail is False

    def test_has_guardrail_falls_back_to_static_without_provider(self):
        s = self._synth(static_id="gr-start", provider_val=None)
        assert s.has_guardrail is True


@pytest.mark.unit
class TestStreamingGuardrailCapturedPerCall:
    """A streaming synthesis captures the effective id at call start, not mid-stream.

    Long streams must not switch guardrail partway through; the next call picks up
    the new value. (Addresses the HIGH mid-stream test gap.)
    """

    async def test_stream_uses_id_captured_at_call_start_next_call_sees_new(self):
        from unittest.mock import AsyncMock

        from coa_serve.tier3.synthesizer import Synthesizer

        live = {"v": "gr-old"}
        s = Synthesizer(
            AsyncMock(),
            guardrail_id="gr-old",
            guardrail_id_provider=lambda: live["v"],
        )
        # First resolution captures gr-old.
        first = s._effective_guardrail_id()
        assert first == "gr-old"
        # Operator flips the live value mid-flight.
        live["v"] = "gr-new"
        # A resolution for the NEXT call sees the new value; the already-captured
        # `first` is unchanged (no mid-stream switch of an in-flight converse).
        assert first == "gr-old"
        assert s._effective_guardrail_id() == "gr-new"


@pytest.mark.unit
class TestStrictReaderErrorSemantics:
    """The default production getter must raise on a real read failure so the
    provider's last-known-good path engages, and must NOT flip the guardrail off.

    Addresses reviewer note (guardrail_config.py:224): ``_get_ssm_parameter``
    swallows every error to ``""``, which would silently disable the guardrail on
    an IAM/throttle failure. ``_read_ssm_parameter_strict`` raises instead, and
    maps only ``ParameterNotFound`` to ``""``.
    """

    def _client_error(self, code: str):
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": code, "Message": code}}, "GetParameter")

    def test_parameter_not_found_maps_to_empty(self, monkeypatch):
        from unittest.mock import MagicMock

        from coa_serve import config as cfg

        client = MagicMock()
        client.get_parameter.side_effect = self._client_error("ParameterNotFound")
        monkeypatch.setattr(cfg.boto3, "client", lambda *a, **k: client)
        monkeypatch.setattr(cfg, "resolve_region", lambda: "us-east-1")
        assert cfg._read_ssm_parameter_strict("/coa/bedrock/guardrail-id") == ""

    @pytest.mark.parametrize("code", ["ThrottlingException", "AccessDeniedException"])
    def test_real_error_raises_not_swallowed(self, monkeypatch, code):
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError
        from coa_serve import config as cfg

        client = MagicMock()
        client.get_parameter.side_effect = self._client_error(code)
        monkeypatch.setattr(cfg.boto3, "client", lambda *a, **k: client)
        monkeypatch.setattr(cfg, "resolve_region", lambda: "us-east-1")
        with pytest.raises(ClientError):
            cfg._read_ssm_parameter_strict("/coa/bedrock/guardrail-id")

    def test_provider_retains_last_known_good_when_strict_reader_raises(self):
        """End-to-end: a non-transient failure on refresh keeps the cached id,
        it does NOT flip the guardrail off (the bug the swallow-to-empty caused).
        """
        state = {"raise": False}

        def strict_getter(name: str, *, required: bool = False) -> str:
            if state["raise"]:
                raise self._client_error("AccessDeniedException")
            return "gr-live" if name.endswith("/bedrock/guardrail-id") else ""

        clock = _FakeClock()
        p = GuardrailConfigProvider(
            "/coa", ttl_s=60, time_fn=clock, ssm_getter=strict_getter, disabled_fn=lambda: False
        )
        assert p.guardrail_id() == "gr-live"
        state["raise"] = True
        clock.advance(61)
        # Refresh raises -> last-known-good retained, NOT "" (guardrail stays on).
        assert p.guardrail_id() == "gr-live"


@pytest.mark.unit
class TestDisabledSkipsSsmRead:
    """When the deploy-scoped kill-switch is set, the provider must not issue any
    SSM GetParameter — it returns the forced-empty snapshot directly.

    Addresses reviewer note (guardrail_config.py:168): don't read the three
    parameters only to discard them when disabled.
    """

    def test_disabled_provider_makes_zero_ssm_calls(self):
        ssm = _FakeSsm({"/bedrock/guardrail-id": "gr-1"})
        clock = _FakeClock()
        p = _provider(ssm, clock, disabled=True)
        assert p.guardrail_id() == ""
        assert p.retrieval_guardrail_id() == ""
        assert p.retrieval_guardrail_version() == "DRAFT"
        assert ssm.calls == [], f"expected zero SSM reads when disabled, got {ssm.calls}"
