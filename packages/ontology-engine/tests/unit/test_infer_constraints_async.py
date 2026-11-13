# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Async infer-constraints endpoint contract.

The Bedrock call can exceed API Gateway's 29s integration timeout, so
``POST /proposals/{id}/infer-constraints`` returns 202 + a job handle and
runs the inference on a background thread.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_VALID_TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.org/o> a owl:Ontology .
"""


class _StubConfig:
    """Stand-in for the ConstraintConfig the LLM helper returns.

    Only ``model_dump`` is exercised by the worker."""

    def model_dump(self):
        return {"classes": [{"class_uri": "https://example.org/Policy", "constraints": []}]}


def _stub_proposal(pid, namespace="default"):
    return {"proposal_id": pid, "status": "pending", "ontology_turtle": _VALID_TTL, "metadata": {}}


@pytest.fixture(autouse=True)
def persisted(monkeypatch) -> list[str]:
    """Statuses the worker writes to DynamoDB, in order.

    The DynamoDB copy is best-effort. Unstubbed, each write waits out botocore's
    retries against an unreachable endpoint, which can outlast the 5 s poll."""
    from coa_ontology import proposals

    written: list[str] = []
    monkeypatch.setattr(
        proposals.dynamo_store,
        "put_async_job",
        lambda kind, job_id, item, namespace="default": written.append(item["status"]),
    )
    return written


def _poll_until_terminal(proposals, proposal_id, job_id, namespace, timeout_s=5.0):
    deadline = time.time() + timeout_s
    job = None
    while time.time() < deadline:
        job = proposals.get_infer_constraints_job(proposal_id, job_id, namespace=namespace)
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    return job


def test_infer_constraints_returns_202_pending_job(monkeypatch):
    """The POST endpoint registers a PENDING job + spawns the worker on a
    background thread, and returns the handle with no inline result."""
    from coa_ontology import proposals

    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", _stub_proposal)

    # Patch Thread so the worker never runs — the returned job stays PENDING,
    # proving the handshake doesn't block on the LLM call.
    with patch("coa_ontology.proposals.Thread") as thread:
        resp = proposals.infer_constraints("p-1", namespace="ns")

    thread.assert_called_once()
    thread.return_value.start.assert_called_once()
    assert resp["status"] == "pending"
    assert resp["job_id"]
    assert resp["proposal_id"] == "p-1"
    assert resp["result"] is None


def test_infer_constraints_job_polls_to_completed(monkeypatch, persisted):
    """End-to-end: POST spawns the real worker thread; the job GET
    eventually returns ``completed`` with ``inferred_constraints``."""
    from coa_ontology import proposals

    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", _stub_proposal)
    # Mock the LLM call at its import site inside nl_generator so the worker's
    # local import resolves to the stub.
    monkeypatch.setattr(
        "coa_ontology.validation.shapes.nl_generator.infer_constraints_from_ontology",
        lambda turtle, existing_config=None: _StubConfig(),
    )
    resp = proposals.infer_constraints("p-1", namespace="ns")

    job = _poll_until_terminal(proposals, "p-1", resp["job_id"], "ns")
    # The poll can see the job completed before the worker writes the terminal
    # record, so wait for that write before asserting on it.
    deadline = time.monotonic() + 5
    while persisted[-1:] != ["completed"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job["status"] == "completed"
    assert job["result"]["inferred_constraints"]["classes"][0]["class_uri"] == "https://example.org/Policy"
    assert job["error"] is None
    assert persisted == ["pending", "running", "completed"]


def test_infer_constraints_worker_marks_failed_on_llm_error(monkeypatch):
    from coa_ontology import proposals

    def _boom(turtle, existing_config=None):
        raise RuntimeError("Constraint inference failed: Bedrock throttled")

    monkeypatch.setattr(
        "coa_ontology.validation.shapes.nl_generator.infer_constraints_from_ontology",
        _boom,
    )

    job_id = "job-fail"
    proposals._infer_jobs[job_id] = {
        "job_id": job_id,
        "proposal_id": "p-2",
        "status": "pending",
        "created_at": "now",
        "result": None,
        "error": None,
    }
    # Run the worker directly (deterministic — no thread race).
    proposals._run_infer_constraints(job_id, _VALID_TTL, None, "p-2", "default")

    job = proposals._infer_jobs[job_id]
    assert job["status"] == "failed"
    # RuntimeError message is surfaced verbatim (scrubbed at the source).
    assert "Constraint inference failed" in job["error"]
    assert job["result"] is None


def test_infer_constraints_404_on_unknown_proposal(monkeypatch):
    from coa_ontology import proposals

    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", lambda pid, namespace="default": None)
    with pytest.raises(HTTPException) as exc:
        proposals.infer_constraints("missing", namespace="ns")
    assert exc.value.status_code == 404


def test_infer_constraints_409_when_no_turtle(monkeypatch):
    from coa_ontology import proposals

    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending", "ontology_turtle": ""},
    )
    with pytest.raises(HTTPException) as exc:
        proposals.infer_constraints("p-3", namespace="ns")
    assert exc.value.status_code == 409


def test_infer_constraints_job_get_404_on_mismatch(monkeypatch):
    from coa_ontology import proposals

    proposals._infer_jobs["job-x"] = {
        "job_id": "job-x",
        "proposal_id": "p-owner",
        "status": "pending",
        "created_at": "now",
        "result": None,
        "error": None,
    }
    # Same job id but a different proposal_id must 404 (ownership check).
    with pytest.raises(HTTPException) as exc:
        proposals.get_infer_constraints_job("p-other", "job-x", namespace="ns")
    assert exc.value.status_code == 404


class TestEvictTerminalJobs:
    """The in-memory job cap must never evict a still-running job."""

    def test_eviction_skips_running_jobs(self):
        from coa_ontology.proposals import _evict_terminal_jobs

        jobs = {
            "a": {"job_id": "a", "status": "running"},
            "b": {"job_id": "b", "status": "completed"},
            "c": {"job_id": "c", "status": "running"},
            "d": {"job_id": "d", "status": "failed"},
        }
        # Cap of 3 → dropping terminal b/d alone (→ 2 jobs) gets under cap, so
        # both running jobs are preserved without touching the hard ceiling.
        _evict_terminal_jobs(jobs, cap=3)
        assert set(jobs) == {"a", "c"}
        assert all(j["status"] == "running" for j in jobs.values())

    def test_no_eviction_below_cap(self):
        from coa_ontology.proposals import _evict_terminal_jobs

        jobs = {"a": {"job_id": "a", "status": "completed"}}
        _evict_terminal_jobs(jobs, cap=200)
        assert set(jobs) == {"a"}

    def test_prefers_terminal_over_running_when_terminal_suffices(self):
        from coa_ontology.proposals import _evict_terminal_jobs

        # With enough terminal jobs to get strictly below cap, running jobs are
        # never touched — terminal ones are evicted oldest-first.
        jobs = {
            "t1": {"job_id": "t1", "status": "completed"},
            "t2": {"job_id": "t2", "status": "failed"},
            "run": {"job_id": "run", "status": "running"},
        }
        _evict_terminal_jobs(jobs, cap=3)
        # cap=3, len=3 → must get to <3. Dropping one terminal (oldest "t1")
        # suffices; "run" and one terminal remain.
        assert "t1" not in jobs
        assert "run" in jobs
        assert len(jobs) < 3

    def test_hard_ceiling_force_evicts_when_all_nonterminal(self):
        from coa_ontology.proposals import _evict_terminal_jobs

        # All jobs non-terminal at cap → terminal-eviction can't help, so the
        # hard ceiling force-evicts the oldest (its status lives in DynamoDB, so
        # the poll still resolves via fallback). Prevents unbounded growth.
        jobs = {
            "old": {"job_id": "old", "status": "running"},
            "mid": {"job_id": "mid", "status": "running"},
            "new": {"job_id": "new", "status": "pending"},
        }
        _evict_terminal_jobs(jobs, cap=2)
        # Oldest ("old") force-evicted to get under cap; dict is bounded.
        assert len(jobs) < 3
        assert "old" not in jobs
