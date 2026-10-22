# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests: GET /induce/jobs is durable and returns ``InductionJobSummary`` entries.

Two defects are pinned here.

**Wrong data source.** The handler read the module-level ``_jobs`` dict, which only
holds jobs started by the task serving the request and is wiped on every deploy.
Against the deployed dev environment that meant a 200 with ``[]`` for every
namespace while dozens of job rows sat in DynamoDB. ``count_active_jobs`` in the
same module already read durably for exactly this reason.

**Wrong shape.** ``ListInductionJobs`` declares ``InductionJobSummaryList`` —
scalars only, no ``report``. Returning whole ``JobResponse`` objects both
over-returned against the contract and serialized the full ``report.matches[]``
per job; one report carries ~836k match entries at 50k tables, which risks the
API-proxy Lambda's 6MB response cap.

``test_summary_keys_match_declared_contract`` is the guard that matters most: it
pins the key set exactly, so re-adding ``report`` (or leaking ``PK``/``SK`` from
the DynamoDB item) fails rather than silently reintroducing an unbounded response.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# Exactly the members of `structure InductionJobSummary` in
# models/src/main/smithy/ontology-induction.smithy, in this service's snake_case
# wire casing. Update this set ONLY alongside the Smithy model — it is the
# contract, not an implementation detail.
_DECLARED_SUMMARY_KEYS = {
    "job_id",
    "status",
    "created_at",
    "source_type",
    "tables_processed",
    "novel_classes_created",
    "error",
}


def _item(**over):
    """A persisted job row shaped like the real DynamoDB item.

    Field names and the ``Decimal`` numerics mirror what was observed on the
    deployed environment, including the storage-internal keys that must not reach
    the client.
    """
    item = {
        "PK": "ns-a#JOB#job-1",
        "SK": "STATUS",
        "job_id": "job-1",
        "namespace": "ns-a",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00Z",
        "source_type": "STRUCTURED",
        "tables_processed": Decimal("7"),
        "novel_classes_created": Decimal("3"),
        "columns_processed": Decimal("42"),
        "datasources_s3_key": "jobs/job-1/datasources.json",
        "proposal_id": "prop-1",
    }
    item.update(over)
    return item


def _list_jobs(namespace="ns-a", **kw):
    """Call the route function directly and return its ``jobs`` page.

    The FastAPI ``Query(...)`` defaults only resolve through the app, so a direct
    call passes them explicitly.
    """
    from coa_ontology import induce_catalog

    kw.setdefault("max_results", 50)
    kw.setdefault("next_token", None)
    return induce_catalog.list_jobs(namespace=namespace, **kw)["jobs"]


def test_reads_from_dynamodb_not_in_memory_jobs() -> None:
    """The durable store is the source of truth, even when ``_jobs`` is empty.

    This is the defect that made the endpoint useless in practice: it returned
    ``[]`` on every namespace because nothing had run on the task being hit.
    """
    from coa_ontology import induce_catalog

    assert not induce_catalog._jobs, "precondition: no in-memory jobs"

    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([_item()], None)) as m:
        result = _list_jobs()

    assert len(result) == 1
    assert result[0]["job_id"] == "job-1"
    m.assert_called_once()


def test_summary_keys_match_declared_contract() -> None:
    """Key set is exactly InductionJobSummary — no report, no DynamoDB internals."""
    from coa_ontology import induce_catalog

    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([_item()], None)):
        (entry,) = _list_jobs()

    assert set(entry) == _DECLARED_SUMMARY_KEYS
    # Called out individually because these are the ones that actually leaked.
    for leaked in ("PK", "SK", "datasources_s3_key", "report", "matches"):
        assert leaked not in entry


def test_numeric_attributes_are_ints_not_decimals() -> None:
    """The contract types these Integer; a raw Decimal serializes as "7.0"."""
    from coa_ontology import induce_catalog

    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([_item()], None)):
        (entry,) = _list_jobs()

    assert entry["tables_processed"] == 7
    assert isinstance(entry["tables_processed"], int)
    assert entry["novel_classes_created"] == 3
    assert isinstance(entry["novel_classes_created"], int)


def test_missing_source_type_defaults_to_structured() -> None:
    """Contract: items lacking the discriminator SHALL read as STRUCTURED."""
    from coa_ontology import induce_catalog

    row = _item()
    del row["source_type"]
    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([row], None)):
        (entry,) = _list_jobs()

    assert entry["source_type"] == "STRUCTURED"


def test_in_progress_row_without_counts_does_not_crash() -> None:
    """A pending job has no counts yet; they read as None, not an error."""
    from coa_ontology import induce_catalog

    row = _item(status="pending")
    del row["tables_processed"]
    del row["novel_classes_created"]
    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([row], None)):
        (entry,) = _list_jobs()

    assert entry["status"] == "pending"
    assert entry["tables_processed"] is None
    assert entry["novel_classes_created"] is None


def test_malformed_numeric_degrades_to_none() -> None:
    """One bad row must not 500 the whole list read."""
    from coa_ontology import induce_catalog

    rows = [_item(tables_processed="not-a-number")]
    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=(rows, None)):
        (entry,) = _list_jobs()

    assert entry["tables_processed"] is None


def test_dynamo_scan_failure_is_503_not_an_empty_page() -> None:
    """A DynamoDB scan failure (throttling, network, permissions) must not look
    like "no more jobs": an empty page without ``nextToken`` silently truncates a
    paging client. Surface a retryable 503 with a fixed message instead."""
    from coa_ontology import induce_catalog
    from fastapi import HTTPException

    with (
        patch.object(induce_catalog.dynamo_store, "list_jobs_page", side_effect=RuntimeError("scan failed")),
        pytest.raises(HTTPException) as exc,
    ):
        _list_jobs()

    assert exc.value.status_code == 503
    assert "scan failed" not in str(exc.value.detail)


def test_filters_and_limit_are_passed_through() -> None:
    """``status`` and ``maxResults`` are declared on the contract — wire them."""
    from coa_ontology import induce_catalog

    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([], None)) as m:
        _list_jobs(namespace="ns-b", status="failed", max_results=5)

    m.assert_called_once_with(namespace="ns-b", status="failed", limit=5, exclusive_start_key=None)


def test_newest_first() -> None:
    """Ordering is deterministic so a caller's first page is the recent one."""
    from coa_ontology import induce_catalog

    rows = [
        _item(job_id="old", created_at="2026-01-01T00:00:00Z"),
        _item(job_id="new", created_at="2026-06-01T00:00:00Z"),
        _item(job_id="mid", created_at="2026-03-01T00:00:00Z"),
    ]
    with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=(rows, None)):
        result = _list_jobs()

    assert [e["job_id"] for e in result] == ["new", "mid", "old"]


class TestOverHttp:
    """Route-level checks — the direct-call tests above bypass FastAPI binding.

    The contract spells the parameter ``maxResults``, but Python cannot use that
    name idiomatically, so the handler declares ``max_results`` with an alias.
    Only a request through the app proves the alias is wired: calling
    ``list_jobs(max_results=5)`` directly would pass even if the query parameter
    were spelled something else entirely.
    """

    def test_max_results_query_alias_binds(self) -> None:
        from coa_ontology import induce_catalog
        from coa_ontology.main import app
        from fastapi.testclient import TestClient

        with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([], None)) as m:
            resp = TestClient(app).get("/induce/jobs?namespace=ns-a&status=failed&maxResults=5")

        assert resp.status_code == 200
        m.assert_called_once_with(namespace="ns-a", status="failed", limit=5, exclusive_start_key=None)

    def test_out_of_range_max_results_is_rejected(self) -> None:
        """Bounded so a caller cannot ask for an unbounded scan."""
        from coa_ontology.main import app
        from fastapi.testclient import TestClient

        resp = TestClient(app).get("/induce/jobs?namespace=ns-a&maxResults=99999")

        assert resp.status_code == 422

    def test_response_body_carries_only_contract_keys(self) -> None:
        """End-to-end shape check on the serialized JSON, not the return value."""
        from coa_ontology import induce_catalog
        from coa_ontology.main import app
        from fastapi.testclient import TestClient

        with patch.object(induce_catalog.dynamo_store, "list_jobs_page", return_value=([_item()], None)):
            resp = TestClient(app).get("/induce/jobs?namespace=ns-a")

        assert resp.status_code == 200
        (entry,) = resp.json()["jobs"]
        assert set(entry) == _DECLARED_SUMMARY_KEYS
        assert entry["tables_processed"] == 7  # Decimal survived JSON as an int

    def test_dynamo_scan_failure_returns_503(self) -> None:
        """Transient DynamoDB issues are a retryable 503, never a 500 or a truncating empty 200."""
        from coa_ontology import induce_catalog
        from coa_ontology.main import app
        from fastapi.testclient import TestClient

        with patch.object(induce_catalog.dynamo_store, "list_jobs_page", side_effect=RuntimeError("throttled")):
            resp = TestClient(app).get("/induce/jobs?namespace=ns-a")

        assert resp.status_code == 503
        assert "throttled" not in resp.text
