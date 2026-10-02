# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the bundled live-induction harness (``scripts/live_induction_run.py``).

Regression coverage for issue #175, whose two defects both let the harness
report success against behaviour the shipped API never exhibits:

1. **Accept step** — ``accept_proposal`` returns an ``AcceptProposalResponse``
   *pydantic model* (not a dict) with ``status="accepting"`` (async 202), and a
   background worker later flips the proposal to ``accepted`` / ``accept_failed``.
   The old harness subscripted the model (``a["status"]`` → ``TypeError``) and
   asserted a synchronous ``"accepted"`` / a non-existent ``"already_accepted"``
   status. The fix (``accept_proposal_direct``) polls the proposal to a terminal
   state and normalizes the result.
2. **Grounding** — the harness never populated ``grounding_ontology_ids`` and
   defaulted to the ``rigor_ontology`` strategy (which ``del``\\ s the grounding
   scope), so grounding was never attempted yet the run still "passed". The fix
   threads a grounding scope + a grounding-capable strategy and adds a fail-loud
   gate keyed on ``report.grounding_ontologies_used``.

The harness lives under ``scripts/`` (not an importable package), and its module
top-level does env validation + ``sys.exit`` on missing config, so it is loaded
by file path with the required env pre-seeded and its downstream deps stubbed.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_induction_run.py"


@pytest.fixture
def harness(monkeypatch):
    """Import ``live_induction_run`` by path with a minimal in-memory backend.

    Uses the ``na_only`` backend so the module's import-time REQUIRED check is
    satisfied with two trivial env vars and no AWS clients are constructed at
    import (the store/proposal calls the tests exercise are all monkeypatched).
    """
    monkeypatch.setenv("WORKBENCH_BACKEND", "na_only")
    monkeypatch.setenv("NA_GRAPH_ID", "g-test")
    monkeypatch.setenv("DYNAMODB_TABLE", "t-test")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    spec = importlib.util.spec_from_file_location("live_induction_run_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses/type hints resolving __module__ work.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    try:
        yield mod
    finally:
        sys.modules.pop(spec.name, None)


def _install_fake_proposals(monkeypatch, *, accept_status, poll_statuses):
    """Stub the ``coa_ontology.proposals`` + ``dynamo_store`` surface the harness calls.

    ``accept_status`` is what the (async) ``accept_proposal`` 202 returns;
    ``poll_statuses`` is the sequence of ``status`` values ``get_proposal_by_id``
    returns on successive polls (last one is the terminal state).
    """

    # Minimal AcceptProposalResponse stand-in: a pydantic-ish object exposing
    # ``.status`` (NOT subscriptable) — exactly the shape that broke the old
    # ``a["status"]`` harness.
    class _AcceptResp:
        def __init__(self, proposal_id, status):
            self.proposal_id = proposal_id
            self.status = status

        def __getitem__(self, key):  # pragma: no cover - proves non-subscriptable contract
            raise TypeError("'AcceptProposalResponse' object is not subscriptable")

    class _AcceptReq:
        def __init__(self, ontology_id=None):
            self.ontology_id = ontology_id

    accept_calls = []

    def _accept_proposal(proposal_id, body=None, namespace="default"):
        accept_calls.append((proposal_id, namespace, getattr(body, "ontology_id", None)))
        return _AcceptResp(proposal_id, accept_status)

    # ProposalStatus enum stand-in (values match the Smithy-generated enum).
    proposal_status = types.SimpleNamespace(
        ACCEPTING=types.SimpleNamespace(value="accepting"),
        ACCEPTED=types.SimpleNamespace(value="accepted"),
        EMBEDDINGS_SYNC=types.SimpleNamespace(value="embeddings_sync"),
        ACCEPT_FAILED=types.SimpleNamespace(value="accept_failed"),
    )
    ps_mod = types.ModuleType("coa_control_plane_server.models.proposal_status")
    ps_mod.ProposalStatus = proposal_status
    cps = types.ModuleType("coa_control_plane_server")
    cps_models = types.ModuleType("coa_control_plane_server.models")
    monkeypatch.setitem(sys.modules, "coa_control_plane_server", cps)
    monkeypatch.setitem(sys.modules, "coa_control_plane_server.models", cps_models)
    monkeypatch.setitem(sys.modules, "coa_control_plane_server.models.proposal_status", ps_mod)

    proposals_mod = types.ModuleType("coa_ontology.proposals")
    proposals_mod.accept_proposal = _accept_proposal
    proposals_mod.AcceptProposalRequest = _AcceptReq
    proposals_mod.PROPOSAL_STATUS_ACCEPT_FAILED = "accept_failed"
    proposals_mod._ACCEPT_IN_PROGRESS_STATUSES = ("accepting", "embeddings_sync")
    monkeypatch.setitem(sys.modules, "coa_ontology.proposals", proposals_mod)

    poll_iter = iter(poll_statuses)
    last = {"status": poll_statuses[-1]}

    def _get_proposal_by_id(proposal_id, namespace="default", **kw):
        try:
            status = next(poll_iter)
        except StopIteration:
            status = last["status"]
        return {
            "status": status,
            "ontology_id": "https://ont/x",
            "accept_error": "boom step 'ingest'" if status == "accept_failed" else "",
        }

    dynamo_mod = types.ModuleType("coa_ontology.dynamo_store")
    dynamo_mod.get_proposal_by_id = _get_proposal_by_id
    dynamo_mod.list_ontologies_registry = lambda namespace=None, **kw: [
        {"ontology_id": "https://ont/x", "embedding_count": 7}
    ]
    monkeypatch.setitem(sys.modules, "coa_ontology.dynamo_store", dynamo_mod)

    coa_ontology_pkg = sys.modules.get("coa_ontology") or types.ModuleType("coa_ontology")
    # Use monkeypatch.setattr (NOT direct assignment) so the real coa_ontology
    # package's .dynamo_store / .proposals attributes are RESTORED on teardown.
    # A direct `pkg.dynamo_store = fake` is untracked and leaks the stub into
    # sys.modules for the rest of the pytest session, breaking every later test
    # that patches the real dynamo_store (e.g. AttributeError: no attribute '_get_table').
    monkeypatch.setattr(coa_ontology_pkg, "dynamo_store", dynamo_mod, raising=False)
    monkeypatch.setattr(coa_ontology_pkg, "proposals", proposals_mod, raising=False)
    monkeypatch.setitem(sys.modules, "coa_ontology", coa_ontology_pkg)

    ndg_mod = types.ModuleType("coa_ontology.stores.neptune_db_graph")
    ndg_mod._ontology_graph_uri = lambda oid, ns: f"urn:graph:{ns}:{oid}"
    stores_pkg = types.ModuleType("coa_ontology.stores")
    monkeypatch.setitem(sys.modules, "coa_ontology.stores", stores_pkg)
    monkeypatch.setitem(sys.modules, "coa_ontology.stores.neptune_db_graph", ndg_mod)

    return accept_calls


# ── Fix A: accept polls the async worker to a terminal state ──────────────


def test_accept_polls_accepting_to_accepted(harness, monkeypatch):
    """202 'accepting' → poll → terminal 'accepted'; returns a normalized dict."""
    monkeypatch.setattr(harness, "_ACCEPT_POLL_INTERVAL_S", 0, raising=False)
    _install_fake_proposals(
        monkeypatch, accept_status="accepting", poll_statuses=["accepting", "embeddings_sync", "accepted"]
    )

    result = harness.accept_proposal_direct("p-1", namespace="ns")

    # A dict (subscriptable) — the old harness's a["status"] would have raised
    # TypeError on the underlying pydantic model; the fix normalizes to a dict.
    assert result["status"] == "accepted"
    assert result["already_accepted"] is False
    assert result["ontology_id"] == "https://ont/x"
    assert result["graph_uri"] == "urn:graph:ns:https://ont/x"
    assert result["embeddings"]["count"] == 7


def test_accept_already_accepted_short_circuits(harness, monkeypatch):
    """A re-accept of an accepted proposal returns 'accepted' + already_accepted=True (no 'already_accepted' status)."""
    monkeypatch.setattr(harness, "_ACCEPT_POLL_INTERVAL_S", 0, raising=False)
    _install_fake_proposals(monkeypatch, accept_status="accepted", poll_statuses=["accepted"])

    result = harness.accept_proposal_direct("p-1", namespace="ns")

    assert result["status"] == "accepted"
    assert result["already_accepted"] is True


def test_accept_failed_raises(harness, monkeypatch):
    """A worker that lands accept_failed makes the harness FAIL (not silently pass)."""
    monkeypatch.setattr(harness, "_ACCEPT_POLL_INTERVAL_S", 0, raising=False)
    _install_fake_proposals(monkeypatch, accept_status="accepting", poll_statuses=["accepting", "accept_failed"])

    with pytest.raises(RuntimeError, match="accept failed"):
        harness.accept_proposal_direct("p-1", namespace="ns")


def test_accept_times_out_when_never_terminal(harness, monkeypatch):
    """Still accepting past the deadline → TimeoutError, never a false success."""
    monkeypatch.setattr(harness, "_ACCEPT_POLL_INTERVAL_S", 0, raising=False)
    monkeypatch.setattr(harness, "_ACCEPT_POLL_TIMEOUT_S", 0, raising=False)
    _install_fake_proposals(monkeypatch, accept_status="accepting", poll_statuses=["accepting"])

    with pytest.raises(TimeoutError, match="still 'accepting'"):
        harness.accept_proposal_direct("p-1", namespace="ns")


def test_accept_missing_status_field_raises(harness, monkeypatch):
    """A proposal record with no 'status' key fails loudly (not a cryptic later error)."""
    monkeypatch.setattr(harness, "_ACCEPT_POLL_INTERVAL_S", 0, raising=False)
    _install_fake_proposals(monkeypatch, accept_status="accepting", poll_statuses=["accepting"])
    # Override get_proposal_by_id to return a record with no status.
    import coa_ontology.dynamo_store as ds

    monkeypatch.setattr(ds, "get_proposal_by_id", lambda pid, namespace="default", **kw: {"ontology_id": "x"})
    with pytest.raises(RuntimeError, match="no status field"):
        harness.accept_proposal_direct("p-1", namespace="ns")


def test_positive_int_env_rejects_non_numeric(harness, monkeypatch):
    """Non-numeric poll-timeout env fails loudly at parse (issue #175 fail-loud)."""
    monkeypatch.setenv("INDUCTION_ACCEPT_POLL_TIMEOUT_S", "not-a-number")
    with pytest.raises(RuntimeError, match="must be a valid integer"):
        harness._positive_int_env("INDUCTION_ACCEPT_POLL_TIMEOUT_S", 300, maximum=3600)


def test_positive_int_env_clamps_to_maximum(harness, monkeypatch):
    """An over-large timeout is clamped to the maximum rather than accepted unbounded."""
    monkeypatch.setenv("INDUCTION_ACCEPT_POLL_TIMEOUT_S", "999999")
    assert harness._positive_int_env("INDUCTION_ACCEPT_POLL_TIMEOUT_S", 300, maximum=3600) == 3600


# ── Fix B: grounding fail-loud gate ───────────────────────────────────────


def test_grounding_gate_fires_when_requested_but_nothing_grounded(harness):
    """Requested grounding + capable strategy + empty grounding_ontologies_used → gate trips."""
    assert (
        harness._grounding_gate_should_fail(
            grounding_keys=["fibo-agreements"], strategy="table_to_ontology", grounding_used=[]
        )
        is True
    )


def test_grounding_gate_silent_when_grounding_took_effect(harness):
    """Requested grounding that produced at least one grounded ontology → no failure."""
    assert (
        harness._grounding_gate_should_fail(
            grounding_keys=["fibo-agreements"],
            strategy="table_to_ontology",
            grounding_used=["https://fibo/agreements"],
        )
        is False
    )


def test_grounding_gate_silent_for_all_novel_run(harness):
    """No grounding requested → all-novel run is a legitimate mode, gate never fires."""
    assert (
        harness._grounding_gate_should_fail(grounding_keys=[], strategy="table_to_ontology", grounding_used=[]) is False
    )


def test_grounding_gate_silent_for_non_grounding_strategy(harness):
    """rigor_ontology deletes the scope by design → gate must not fire even with keys."""
    assert (
        harness._grounding_gate_should_fail(
            grounding_keys=["fibo-agreements"], strategy="rigor_ontology", grounding_used=[]
        )
        is False
    )
