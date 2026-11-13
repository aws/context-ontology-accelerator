# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for count_active_jobs in induce_catalog.py.

Backs the namespace-deletion precondition check (control-plane
delete_handler._check_active_induction_jobs) — counts three independent
sources of in-flight background work against a namespace's ontology data:
  - induction jobs (structured + unstructured) not yet terminal
  - async ontology-ingest jobs (upload-a-custom-ontology-file) not yet
    terminal
  - proposals currently in the "accepting" state (background ingest writer
    in flight)
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

_MODULE = "coa_ontology.induce_catalog.dynamo_store"


def _patch_dynamo_store(jobs=None, ingest_jobs=None, proposals=None):
    """Patch all three dynamo_store calls count_active_jobs makes, with
    sane defaults (empty) for whichever the test doesn't care about."""
    stack = ExitStack()
    mocks = {
        "list_jobs": stack.enter_context(patch(f"{_MODULE}.list_jobs", return_value=jobs or [])),
        "list_ingest_jobs": stack.enter_context(patch(f"{_MODULE}.list_ingest_jobs", return_value=ingest_jobs or [])),
        "list_proposals": stack.enter_context(patch(f"{_MODULE}.list_proposals", return_value=proposals or [])),
    }
    return stack, mocks


@pytest.mark.unit
class TestCountActiveJobs:
    def test_nothing_active_returns_zero(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store()
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_only_terminal_induction_jobs_returns_zero(self):
        from coa_ontology.induce_catalog import count_active_jobs

        jobs = [{"status": "completed"}, {"status": "failed"}, {"status": "ingested"}]
        stack, _ = _patch_dynamo_store(jobs=jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_non_terminal_induction_jobs_are_counted(self):
        from coa_ontology.induce_catalog import count_active_jobs

        jobs = [
            {"status": "pending"},
            {"status": "generating_embeddings"},
            {"status": "completed"},
            {"status": "querying_neptune"},
        ]
        stack, _ = _patch_dynamo_store(jobs=jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 3}

    def test_induction_job_with_missing_status_counts_as_active(self):
        """Defensive: a job record with no status attribute (malformed / very old
        data) should not be silently treated as terminal — err toward blocking
        deletion rather than risking a race against unknown job state."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store(jobs=[{}])
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 1}

    def test_queries_both_structured_and_unstructured_induction_jobs(self):
        """source_type=None must be passed through so both job families are
        included in the scan (dynamo_store.list_jobs treats None as no filter)."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store()
        with stack:
            count_active_jobs("ns-1")

        mocks["list_jobs"].assert_called_once_with(namespace="ns-1", source_type=None, limit=1000)

    def test_accepting_proposals_are_counted(self):
        """A proposal mid-accept has a background thread actively writing to
        the namespace's ontology data (catalog projection, Turtle bulk load,
        embeddings, S3 persist) — this must block deletion even though the
        induction job that produced the proposal is long since terminal."""
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store(proposals=[{"status": "accepting"}, {"status": "accepting"}])
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 2}
        mocks["list_proposals"].assert_called_once_with(namespace="ns-1", status="accepting", limit=1000)

    def test_only_terminal_ingest_jobs_returns_zero(self):
        from coa_ontology.induce_catalog import count_active_jobs

        ingest_jobs = [{"status": "completed"}, {"status": "failed"}]
        stack, _ = _patch_dynamo_store(ingest_jobs=ingest_jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 0}

    def test_non_terminal_ingest_jobs_are_counted(self):
        """An in-progress "upload custom ontology" job (ingest-from-S3) has a
        background writer doing Turtle load + embeddings just like an
        induction job — pending/running/embeddings_sync must all block."""
        from coa_ontology.induce_catalog import count_active_jobs

        ingest_jobs = [
            {"status": "pending"},
            {"status": "running"},
            {"status": "embeddings_sync"},
            {"status": "completed"},
        ]
        stack, _ = _patch_dynamo_store(ingest_jobs=ingest_jobs)
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 3}

    def test_queries_ingest_jobs_for_the_namespace(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, mocks = _patch_dynamo_store()
        with stack:
            count_active_jobs("ns-1")

        mocks["list_ingest_jobs"].assert_called_once_with(namespace="ns-1", limit=1000)

    def test_all_three_sources_are_combined(self):
        from coa_ontology.induce_catalog import count_active_jobs

        stack, _ = _patch_dynamo_store(
            jobs=[{"status": "pending"}],
            ingest_jobs=[{"status": "running"}, {"status": "completed"}],
            proposals=[{"status": "accepting"}],
        )
        with stack:
            result = count_active_jobs("ns-1")

        assert result == {"activeJobs": 3}
