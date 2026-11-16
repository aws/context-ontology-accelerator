# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests: GET /induce/jobs/{id} is poll-shaped — report.matches[] dropped by default.

At large table counts ``report.matches`` exceeds the API-proxy Lambda's 6MB
response cap, so ``get_job`` must drop
``report.matches[]`` by default (keeping scalar counts + terminal state) and
only return the full body behind an explicit ``include=report`` opt-in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


def _make_report(schemas, n_matches: int):
    """Build an InductionReport whose matches list has ``n_matches`` entries."""
    matches = [
        schemas.ConceptMatch(
            source_column=f"col{i}",
            source_table=f"tbl{i}",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
        )
        for i in range(n_matches)
    ]
    return schemas.InductionReport(
        job_id="job-poll",
        ontology_uri_prefix="http://test.org/o#",
        tables_processed=13,
        columns_processed=97,
        matches=matches,
        novel_classes_created=7,
        grounding_ontologies_used=[],
        induced_ontology_id="prop-42",
    )


def test_default_poll_drops_matches_but_keeps_counts() -> None:
    """No include param: report.matches emptied, scalar counts + status preserved."""
    from coa_ontology import induce_catalog
    from coa_ontology.inducer import schemas

    N = 5000
    job = schemas.JobResponse(
        job_id="job-poll",
        status=schemas.JobStatus.COMPLETED,
        created_at=datetime.now(UTC),
        report=_make_report(schemas, N),
        duplicate_of=None,
    )
    induce_catalog._jobs["job-poll"] = job
    induce_catalog._job_namespaces["job-poll"] = "ns-1"
    try:
        result = induce_catalog.get_job("job-poll", "ns-1")

        # matches dropped regardless of N (the whole point — bounds the payload).
        assert result.report.matches == []
        # scalar counts survive so existing clients reading them still work.
        assert result.report.tables_processed == 13
        assert result.report.columns_processed == 97
        assert result.report.novel_classes_created == 7
        assert result.report.induced_ontology_id == "prop-42"
        # terminal state still observable.
        assert result.status == schemas.JobStatus.COMPLETED
        # canonical in-memory object MUST NOT be mutated (immutable copy) — an
        # include=report re-poll must still see all N matches.
        assert len(induce_catalog._jobs["job-poll"].report.matches) == N
    finally:
        induce_catalog._jobs.pop("job-poll", None)
        induce_catalog._job_namespaces.pop("job-poll", None)


def test_include_report_returns_full_matches() -> None:
    """include=report returns the full report incl. every match (opt-in)."""
    from coa_ontology import induce_catalog
    from coa_ontology.inducer import schemas

    N = 5000
    job = schemas.JobResponse(
        job_id="job-poll",
        status=schemas.JobStatus.COMPLETED,
        created_at=datetime.now(UTC),
        report=_make_report(schemas, N),
    )
    induce_catalog._jobs["job-poll"] = job
    induce_catalog._job_namespaces["job-poll"] = "ns-1"
    try:
        result = induce_catalog.get_job("job-poll", "ns-1", include="report")
        assert len(result.report.matches) == N
        # include=full is the accepted synonym.
        result_full = induce_catalog.get_job("job-poll", "ns-1", include="full")
        assert len(result_full.report.matches) == N
    finally:
        induce_catalog._jobs.pop("job-poll", None)
        induce_catalog._job_namespaces.pop("job-poll", None)


def test_dynamodb_fallback_path_also_slimmed() -> None:
    """The DDB-hydration branch (no in-memory job) must ALSO drop report.matches[].

    This is the post-restart path. The structured DDB item is flat/scalar today,
    but ``_poll_shape`` guards the dict return shape too so any fat report that
    reaches this branch (hydrated / future / unstructured) is slimmed. Slimming
    only the in-memory path would leave this assertion failing.
    """
    from coa_ontology import induce_catalog

    fat_matches = [{"source_column": f"c{i}", "match_type": "novel"} for i in range(5000)]
    item = {
        "job_id": "ddb-1",
        "namespace": "ns-1",
        "status": "completed",
        "updated_at": datetime.now(UTC).isoformat(),
        "proposal_id": "prop-9",
        "novel_classes_created": 3,
        "report": {"matches": fat_matches, "novel_classes_created": 3, "tables_processed": 5},
    }
    # No in-memory _jobs entry → forces the DynamoDB fallback branch.
    induce_catalog._jobs.pop("ddb-1", None)
    with patch.object(induce_catalog.dynamo_store, "get_job", return_value=item):
        result = induce_catalog.get_job("ddb-1", "ns-1")
        assert result["report"]["matches"] == []
        # scalar counts + terminal state + proposal id survive for the poller.
        assert result["report"]["novel_classes_created"] == 3
        assert result["status"] == "completed"
        assert result["proposal_id"] == "prop-9"

        # include=report keeps the full matches on the DDB path too.
        full = induce_catalog.get_job("ddb-1", "ns-1", include="report")
        assert len(full["report"]["matches"]) == 5000


def test_include_report_binds_over_http() -> None:
    """``?include=report`` must bind through the HTTP layer, not just the Python call.

    The other tests here call ``get_job`` directly, so they cannot catch a query
    param that never binds (FastAPI silently falls back to the default for names
    it does not declare — the same class of bug the API proxy's ``_QUERY_ALIASES``
    exists to fix). Clients that need the matches — the grounding e2e integ test,
    any debugging caller — go over the wire, so pin the wire contract.
    """
    from coa_ontology import induce_catalog
    from coa_ontology.inducer import schemas

    app = FastAPI()
    app.include_router(induce_catalog.router, prefix="/induce")
    client = TestClient(app)

    induce_catalog._jobs["job-http"] = schemas.JobResponse(
        job_id="job-http",
        status=schemas.JobStatus.COMPLETED,
        created_at=datetime.now(UTC),
        report=_make_report(schemas, 3),
    )
    induce_catalog._job_namespaces["job-http"] = "ns-1"
    try:
        slim = client.get("/induce/jobs/job-http", params={"namespace": "ns-1"})
        assert slim.status_code == 200, slim.text
        assert slim.json()["report"]["matches"] == []

        full = client.get("/induce/jobs/job-http", params={"namespace": "ns-1", "include": "report"})
        assert full.status_code == 200, full.text
        assert len(full.json()["report"]["matches"]) == 3
    finally:
        induce_catalog._jobs.pop("job-http", None)
        induce_catalog._job_namespaces.pop("job-http", None)


def test_terminal_state_observable_on_default_poll() -> None:
    """Default poll still carries status + proposal/error so clients detect terminal state."""
    from coa_ontology import induce_catalog
    from coa_ontology.inducer import schemas

    job = schemas.JobResponse(
        job_id="job-term",
        status=schemas.JobStatus.COMPLETED,
        created_at=datetime.now(UTC),
        report=_make_report(schemas, 100),
        duplicate_of="prop-dup",
    )
    induce_catalog._jobs["job-term"] = job
    induce_catalog._job_namespaces["job-term"] = "ns-1"
    try:
        result = induce_catalog.get_job("job-term", "ns-1")
        assert result.status == schemas.JobStatus.COMPLETED
        assert result.duplicate_of == "prop-dup"
        assert result.report.induced_ontology_id == "prop-42"
    finally:
        induce_catalog._jobs.pop("job-term", None)
        induce_catalog._job_namespaces.pop("job-term", None)
