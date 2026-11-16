# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The induction Store/completed DDB writes must never carry an unbounded
per-table list inline (DynamoDB 400 KB item cap).

Drives the real ``_run_induction`` and asserts on the kwargs it hands
``create_proposal`` / ``update_job_status``: the ``datasources`` manifest is
offloaded to S3 and ``novel_tables`` is a scalar count.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer import schemas as schemas_mod
from coa_ontology.inducer.services.data_catalog import CatalogTable

pytestmark = pytest.mark.unit

# A large novel-table set so the would-be-inline ``sorted()`` list is a real blob
# (documents the ceiling the count-only write avoids: a 20k list is ~365 KB).
_NOVEL_TABLES = {f"schema.novel_table_{i:05d}" for i in range(500)}


@pytest.fixture(autouse=True)
def _isolate_induce_catalog_globals():
    """Snapshot/restore ``induce_catalog`` process globals per test.

    ``_run_induction`` mutates the module-global ``_jobs`` / ``_job_namespaces``
    dicts; teardown keeps unrelated tests hermetic under any collection order.
    Mirrors the guard in ``test_induce_catalog_cost_metrics.py``.
    """
    from coa_ontology import induce_catalog

    jobs_snapshot = dict(induce_catalog._jobs)
    namespaces_snapshot = dict(induce_catalog._job_namespaces)
    try:
        yield
    finally:
        induce_catalog._jobs.clear()
        induce_catalog._jobs.update(jobs_snapshot)
        induce_catalog._job_namespaces.clear()
        induce_catalog._job_namespaces.update(namespaces_snapshot)


@pytest.fixture()
def captured_writes():
    """Drive the REAL success-path ``_run_induction`` once; return the patched
    ``dynamo_store`` MagicMock whose ``create_proposal`` / ``update_job_status``
    calls were recorded at the true write sites."""
    from coa_ontology.induce_catalog import (
        WorkbenchInductionRequest,
        _jobs,
        _run_induction,
    )
    from coa_ontology.inducer.schemas import ConceptMatch, JobResponse, JobStatus

    job = JobResponse(job_id="job-size", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
    _jobs["job-size"] = job
    body = WorkbenchInductionRequest(
        datasource_ids=["ds-1"],
        ontology_uri_prefix="http://test.org/ontology/induced#",
    )
    # One novel class-level match (source_column == "" marks a class-level match).
    matches = [
        ConceptMatch(
            source_column="",
            source_table="orders",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
        )
    ]

    class _FakeStrategy:
        def induce(self, **kwargs):
            fake_graph = MagicMock()
            fake_graph.serialize.return_value = "@prefix : <#> ."
            return fake_graph, set(_NOVEL_TABLES), matches, []

        def build_r2rml(self, **kwargs):
            fake_graph = MagicMock()
            fake_graph.serialize.return_value = "@prefix rr: <#> ."
            return fake_graph

    fake_constraint = MagicMock()
    fake_constraint.model_dump.return_value = {}

    with (
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            return_value={"databases": [{"name": "db"}], "sourceType": "POSTGRES"},
        ),
        patch(
            "coa_ontology.induce_catalog._catalog_to_tables",
            return_value=[
                {
                    "id": "t-orders",
                    "name": "orders",
                    "fullyQualifiedName": "db.orders",
                    "columns": [{"name": "id", "dataType": "INTEGER"}],
                }
            ],
        ),
        patch("coa_ontology.induce_catalog.get_strategy", return_value=_FakeStrategy),
        patch("coa_ontology.stores.build_stores", return_value=(MagicMock(), MagicMock())),
        patch("coa_ontology.stores.adapters.StoreOntologyCatalogAdapter", return_value=MagicMock()),
        patch(
            "coa_ontology.validation.shapes.config.generate_config_from_db",
            return_value=fake_constraint,
        ),
        patch("coa_ontology.induce_catalog.os.makedirs"),
        patch("builtins.open", MagicMock()),
        patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
        patch("coa_ontology.induce_catalog.emit_induction_job_metrics"),
    ):
        mock_dynamo.list_proposals.return_value = []
        mock_dynamo.list_ontologies_registry.return_value = []

        _run_induction(
            job_id="job-size",
            body=body,
            namespace="size-ns",
            config={"catalog_source": "smus"},
            build_pipeline_fn=lambda _config: MagicMock(),
            schemas_mod=schemas_mod,
            catalog_table_cls=CatalogTable,
        )

    assert job.status == JobStatus.COMPLETED, "harness must reach the completed/store write path"
    return mock_dynamo


def _completed_call(mock_dynamo):
    for call in mock_dynamo.update_job_status.call_args_list:
        if len(call.args) >= 2 and call.args[1] == "completed":
            return call
    raise AssertionError("no completed update_job_status call was captured")


def _datasources_call(mock_dynamo):
    for call in mock_dynamo.update_job_status.call_args_list:
        if "datasources_s3_key" in call.kwargs:
            return call
    raise AssertionError("no datasources_s3_key update_job_status call was captured")


def test_novel_tables_not_written_inline_at_completed_status(captured_writes):
    """The terminal ``completed`` job write must carry only a count, never the
    unbounded ``novel_tables`` list (else the 400 KB item cap re-fires at 20k)."""
    call = _completed_call(captured_writes)
    assert "novel_tables" not in call.kwargs, "unbounded novel_tables list must not be written inline"
    assert isinstance(call.kwargs.get("novel_tables_count"), int)
    assert call.kwargs["novel_tables_count"] == len(_NOVEL_TABLES)


def test_create_proposal_drops_novel_tables_keeps_count(captured_writes):
    """The proposal ``metadata`` written by ``create_proposal`` must not embed
    the unbounded ``novel_tables`` list — only the scalar count."""
    metadata = captured_writes.create_proposal.call_args.kwargs["metadata"]
    assert "novel_tables" not in metadata, "unbounded novel_tables list must not land in proposal metadata"
    assert isinstance(metadata.get("novel_tables_count"), int)
    assert metadata["novel_tables_count"] == len(_NOVEL_TABLES)


def test_datasources_offloaded_not_inline_at_write_site(captured_writes):
    """The real write site must pass the S3 key, never the inline manifest."""
    call = _datasources_call(captured_writes)
    assert "datasources" not in call.kwargs, "datasources manifest must be offloaded to S3, not written inline"
    assert "datasources_s3_key" in call.kwargs
