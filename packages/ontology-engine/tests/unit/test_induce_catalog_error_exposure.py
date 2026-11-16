# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit test: induction job error field does not expose tracebacks (CWE-200)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
def test_induction_failure_exposes_only_type_and_message() -> None:
    """The job error field must be 'ExceptionType: message' — no traceback."""
    from coa_ontology.induce_catalog import WorkbenchInductionRequest, _jobs, _run_induction
    from coa_ontology.inducer.schemas import JobResponse, JobStatus

    job = JobResponse(job_id="test-1", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
    _jobs["test-1"] = job

    body = WorkbenchInductionRequest(
        datasource_ids=["nonexistent"],
        ontology_uri_prefix="http://test.org/",
    )

    with (
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            side_effect=ValueError("bad datasource"),
        ),
        patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
    ):
        mock_dynamo.update_job_status.return_value = None

        _run_induction(
            job_id="test-1",
            body=body,
            namespace="test-ns",
            config={"catalog_source": "smus"},
            build_pipeline_fn=MagicMock(),
            schemas_mod=MagicMock(JobStatus=JobStatus),
            catalog_table_cls=MagicMock(),
        )

    assert job.status == JobStatus.FAILED
    assert job.error == "ValueError: bad datasource"
    _jobs.pop("test-1", None)
