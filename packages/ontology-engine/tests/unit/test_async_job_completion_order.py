# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""A poll that sees an async proposal job finish also sees its output or error."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_VALID_TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.org/o> a owl:Ontology .
"""


class _PolledOnCompletion(dict):
    """Job dict that polls itself the moment its status becomes terminal."""

    def __init__(self, poll, **job):
        super().__init__(**job)
        self._poll = poll
        self.seen: dict | None = None

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "status" and value in ("completed", "failed"):
            self.seen = dict(self._poll())


@pytest.fixture(autouse=True)
def _no_dynamo(monkeypatch):
    from coa_ontology import proposals

    monkeypatch.setattr(proposals.dynamo_store, "put_async_job", lambda *a, **k: None)


def test_infer_constraints_poll_never_sees_completed_without_result(monkeypatch):
    from coa_ontology import proposals

    class _StubConfig:
        def model_dump(self):
            return {"classes": []}

    monkeypatch.setattr(
        "coa_ontology.validation.shapes.nl_generator.infer_constraints_from_ontology",
        lambda turtle, existing_config=None: _StubConfig(),
    )
    job = _PolledOnCompletion(
        lambda: proposals.get_infer_constraints_job("p-1", "job-infer", namespace="ns"),
        job_id="job-infer",
        proposal_id="p-1",
        status="pending",
        created_at="now",
        result=None,
        error=None,
    )
    monkeypatch.setitem(proposals._infer_jobs, "job-infer", job)

    proposals._run_infer_constraints("job-infer", _VALID_TTL, None, "p-1", "ns")

    assert job.seen is not None
    assert job.seen["result"] == {"inferred_constraints": {"classes": []}}


def test_proposal_validation_poll_never_sees_completed_without_report(monkeypatch):
    from coa_ontology import proposals

    job = _PolledOnCompletion(
        lambda: proposals.get_proposal_validation_job("p-1", "job-validate", namespace="ns"),
        job_id="job-validate",
        proposal_id="p-1",
        status="pending",
        created_at="now",
        report=None,
        error=None,
    )
    monkeypatch.setitem(proposals._validation_jobs, "job-validate", job)
    body = proposals.ValidateProposalRequest(tiers=["tier2_scoring"])

    proposals._run_proposal_validation("job-validate", _VALID_TTL, body, "p-1", "ns")

    assert job.seen is not None
    assert job.seen["report"]["job_id"] == "job-validate"


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (
            RuntimeError("Constraint inference failed: Bedrock throttled"),
            "Constraint inference failed: Bedrock throttled",
        ),
        (ValueError("boom"), "Constraint inference failed: ValueError"),
    ],
)
def test_infer_constraints_poll_never_sees_failed_without_error(monkeypatch, raised, error):
    from coa_ontology import proposals

    def _boom(turtle, existing_config=None):
        raise raised

    monkeypatch.setattr("coa_ontology.validation.shapes.nl_generator.infer_constraints_from_ontology", _boom)
    job = _PolledOnCompletion(
        lambda: proposals.get_infer_constraints_job("p-1", "job-infer-fail", namespace="ns"),
        job_id="job-infer-fail",
        proposal_id="p-1",
        status="pending",
        created_at="now",
        result=None,
        error=None,
    )
    monkeypatch.setitem(proposals._infer_jobs, "job-infer-fail", job)

    proposals._run_infer_constraints("job-infer-fail", _VALID_TTL, None, "p-1", "ns")

    assert job.seen is not None
    assert job.seen["status"] == "failed"
    assert job.seen["error"] == error


def test_proposal_validation_poll_never_sees_failed_without_error(monkeypatch):
    from coa_ontology import proposals

    job = _PolledOnCompletion(
        lambda: proposals.get_proposal_validation_job("p-1", "job-validate-fail", namespace="ns"),
        job_id="job-validate-fail",
        proposal_id="p-1",
        status="pending",
        created_at="now",
        report=None,
        error=None,
    )
    monkeypatch.setitem(proposals._validation_jobs, "job-validate-fail", job)
    body = proposals.ValidateProposalRequest(tiers=["tier2_scoring"])

    proposals._run_proposal_validation("job-validate-fail", "not turtle {", body, "p-1", "ns")

    assert job.seen is not None
    assert job.seen["status"] == "failed"
    assert job.seen["error"].startswith("Validation failed: ")
