# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests: ``_run_induction`` emits Bedrock cost metrics exactly once."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_common.bedrock_metrics import CostTracker
from coa_ontology.inducer.services.data_catalog import CatalogColumn, CatalogTable

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_induce_catalog_globals():
    """Guarantee cross-test hermeticity of ``induce_catalog`` module globals."""
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


def _tbl(name: str, cols: list[CatalogColumn]) -> CatalogTable:
    return CatalogTable(id=f"t-{name}", name=name, fullyQualifiedName=f"db.{name}", columns=cols)


def _schemas_mod():
    return __import__(
        "coa_ontology.inducer.schemas",
        fromlist=["JobStatus", "InductionReport"],
    )


def test_emit_called_once_on_duplicate_shortcircuit() -> None:
    """The duplicate-fingerprint path returns early — metrics must still emit once."""
    from coa_ontology.induce_catalog import (
        WorkbenchInductionRequest,
        _compute_structural_fingerprint,
        _jobs,
        _run_induction,
    )
    from coa_ontology.inducer.schemas import JobResponse, JobStatus

    job = JobResponse(job_id="job-dup-metrics", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
    _jobs["job-dup-metrics"] = job

    tables = [_tbl("customers", [CatalogColumn(name="id", dataType="INTEGER")])]
    fingerprint = _compute_structural_fingerprint(tables)
    existing_accepted = {
        "proposal_id": "accepted-proposal-123",
        "ontology_id": "http://test.org/ontology/induced#",
        "metadata": {"structural_fingerprint": fingerprint},
    }
    body = WorkbenchInductionRequest(
        datasource_ids=["ds-1"],
        ontology_uri_prefix="http://test.org/ontology/induced#",
    )

    with (
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            return_value={"databases": [{"name": "db"}], "sourceType": "POSTGRES"},
        ),
        patch(
            "coa_ontology.induce_catalog._catalog_to_tables",
            return_value=[
                {
                    "id": "t-customers",
                    "name": "customers",
                    "fullyQualifiedName": "db.customers",
                    "columns": [{"name": "id", "dataType": "INTEGER"}],
                }
            ],
        ),
        patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
        patch("coa_ontology.induce_catalog.emit_induction_job_metrics") as mock_emit,
    ):
        mock_dynamo.list_proposals.return_value = [existing_accepted]

        _run_induction(
            job_id="job-dup-metrics",
            body=body,
            namespace="test-ns",
            config={"catalog_source": "smus", "llm_region": "us-west-2"},
            build_pipeline_fn=MagicMock(),
            schemas_mod=_schemas_mod(),
            catalog_table_cls=CatalogTable,
        )

    assert job.status == JobStatus.COMPLETED
    # Emitted exactly once, for this namespace, with the configured Bedrock region.
    mock_emit.assert_called_once()
    args, kwargs = mock_emit.call_args
    assert isinstance(args[0], CostTracker)
    assert kwargs["namespace_id"] == "test-ns"
    assert kwargs["region"] == "us-west-2"
    # The dedup short-circuit is a completing path: the tracker carries the job
    # wall-clock (recorded in the finally) + the input-volume trio.
    tracker = args[0]
    assert tracker.job_duration_ms is not None
    assert tracker.volume_counts.get("TablesProcessed") == 1
    assert "ColumnsProcessed" in tracker.volume_counts
    # No induction ran on a dedup hit — novel/grounded counts are NOT recorded.
    assert "NovelClassesCreated" not in tracker.volume_counts
    assert "InductionJobErrors" not in tracker.volume_counts


def test_emit_called_once_on_failure() -> None:
    """A job that fails mid-way must still emit metrics exactly once."""
    from coa_ontology.induce_catalog import (
        WorkbenchInductionRequest,
        _jobs,
        _run_induction,
    )
    from coa_ontology.inducer.schemas import JobResponse, JobStatus

    job = JobResponse(job_id="job-fail-metrics", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
    _jobs["job-fail-metrics"] = job

    body = WorkbenchInductionRequest(
        datasource_ids=["ds-1"],
        ontology_uri_prefix="http://test.org/ontology/induced#",
    )

    with (
        # Blow up early inside the try so the except+finally paths both run.
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            side_effect=RuntimeError("boom"),
        ),
        patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
        patch("coa_ontology.induce_catalog.emit_induction_job_metrics") as mock_emit,
    ):
        mock_dynamo.list_proposals.return_value = []

        _run_induction(
            job_id="job-fail-metrics",
            body=body,
            namespace="fail-ns",
            config={"catalog_source": "smus"},
            build_pipeline_fn=MagicMock(),
            schemas_mod=_schemas_mod(),
            catalog_table_cls=CatalogTable,
        )

    assert job.status == JobStatus.FAILED
    mock_emit.assert_called_once()
    args, kwargs = mock_emit.call_args
    assert isinstance(args[0], CostTracker)
    assert kwargs["namespace_id"] == "fail-ns"
    # region falls back to config.get("llm_region") -> None (lib resolves AWS_REGION).
    assert kwargs["region"] is None
    # Failure path records InductionJobErrors + still records the job wall-clock
    # in the finally (so a dead job reports its duration too).
    tracker = args[0]
    assert tracker.volume_counts.get("InductionJobErrors") == 1
    assert tracker.job_duration_ms is not None


def test_success_path_records_durations_and_volume() -> None:
    """A full successful run records per-stage durations, the job wall-clock, and
    the volume counts (tables/columns/novel/grounded) on the SAME tracker emit
    receives — the percentile-capable duration datums the dashboard reads."""
    from coa_ontology.induce_catalog import (
        WorkbenchInductionRequest,
        _jobs,
        _run_induction,
    )
    from coa_ontology.inducer.schemas import JobResponse, JobStatus

    job = JobResponse(job_id="job-ok", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
    _jobs["job-ok"] = job

    from coa_ontology.inducer.schemas import ConceptMatch

    body = WorkbenchInductionRequest(
        datasource_ids=["ds-1"],
        ontology_uri_prefix="http://test.org/ontology/induced#",
    )

    # One grounded (high_confidence) class-level match, one novel class-level match
    # (source_column == "" marks a class-level, not column-level, match).
    matches = [
        ConceptMatch(
            source_column="",
            source_table="customers",
            matched_class_uri="http://ex.org/onto#Customer",
            matched_ontology_id="http://ex.org/onto",
            similarity=0.9,
            match_type="high_confidence",
        ),
        ConceptMatch(
            source_column="",
            source_table="orders",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
        ),
    ]

    class _FakeStrategy:
        def induce(self, **kwargs):
            fake_graph = MagicMock()
            fake_graph.serialize.return_value = "@prefix : <#> ."
            return fake_graph, {"orders"}, matches, []

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
                    "id": "t-customers",
                    "name": "customers",
                    "fullyQualifiedName": "db.customers",
                    "columns": [
                        {"name": "id", "dataType": "INTEGER"},
                        {"name": "email", "dataType": "TEXT"},
                    ],
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
        patch("coa_ontology.induce_catalog.emit_induction_job_metrics") as mock_emit,
    ):
        mock_dynamo.list_proposals.return_value = []
        mock_dynamo.list_ontologies_registry.return_value = []

        _run_induction(
            job_id="job-ok",
            body=body,
            namespace="ok-ns",
            config={"catalog_source": "smus"},
            build_pipeline_fn=lambda _config: MagicMock(),
            schemas_mod=_schemas_mod(),
            catalog_table_cls=CatalogTable,
        )

    assert job.status == JobStatus.COMPLETED
    mock_emit.assert_called_once()
    tracker = mock_emit.call_args.args[0]
    assert isinstance(tracker, CostTracker)

    # Job wall-clock recorded (finally) + the four success-path stage durations.
    assert tracker.job_duration_ms is not None
    assert set(tracker.stage_durations_ms) == {"FetchMetadata", "GroundingLoad", "Induce", "Store"}

    # Volume counts: 1 table, 2 columns, 1 novel class, 1 grounded class.
    vols = tracker.volume_counts
    assert vols.get("TablesProcessed") == 1
    assert vols.get("ColumnsProcessed") == 2
    assert vols.get("NovelClassesCreated") == 1
    assert vols.get("GroundedClassesMatched") == 1
    assert "InductionJobErrors" not in vols


def test_tracker_threaded_into_pipeline_embedder() -> None:
    """The job's tracker is attached to the pipeline's embedder (``pipeline.eg``)
    via ``set_cost_tracker`` — so embedding token usage lands on the SAME
    accumulator that emit publishes. The embedder built for a structured job is
    ``BedrockEmbeddingClient``, which exposes ``set_cost_tracker``."""
    from coa_ontology.induce_catalog import (
        WorkbenchInductionRequest,
        _jobs,
        _run_induction,
    )
    from coa_ontology.inducer.schemas import JobResponse, JobStatus

    job = JobResponse(job_id="job-eg", status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z")
    _jobs["job-eg"] = job

    body = WorkbenchInductionRequest(
        datasource_ids=["ds-1"],
        ontology_uri_prefix="http://test.org/ontology/induced#",
    )

    # A pipeline whose embedder records set_cost_tracker calls (mirrors the real
    # BedrockEmbeddingClient contract without importing boto3).
    fake_eg = MagicMock()
    fake_pipeline = MagicMock()
    fake_pipeline.eg = fake_eg
    captured: dict = {}

    class _FakeStrategy:
        def induce(self, **kwargs):
            captured["cost_tracker"] = kwargs.get("cost_tracker")
            raise RuntimeError("stop-after-capture")

        def build_r2rml(self, **kwargs):  # pragma: no cover - not reached
            raise AssertionError("should not reach build_r2rml")

    with (
        patch(
            "coa_ontology.induce_catalog._fetch_catalog_from_smus",
            return_value={"databases": [{"name": "db"}], "sourceType": "POSTGRES"},
        ),
        patch(
            "coa_ontology.induce_catalog._catalog_to_tables",
            return_value=[
                {
                    "id": "t-customers",
                    "name": "customers",
                    "fullyQualifiedName": "db.customers",
                    "columns": [{"name": "id", "dataType": "INTEGER"}],
                }
            ],
        ),
        patch("coa_ontology.induce_catalog.get_strategy", return_value=_FakeStrategy),
        patch("coa_ontology.stores.build_stores", return_value=(MagicMock(), MagicMock())),
        patch("coa_ontology.stores.adapters.StoreOntologyCatalogAdapter", return_value=MagicMock()),
        patch("coa_ontology.induce_catalog.dynamo_store") as mock_dynamo,
        patch("coa_ontology.induce_catalog.emit_induction_job_metrics") as mock_emit,
    ):
        mock_dynamo.list_proposals.return_value = []
        mock_dynamo.list_ontologies_registry.return_value = []

        _run_induction(
            job_id="job-eg",
            body=body,
            namespace="eg-ns",
            config={"catalog_source": "smus"},
            build_pipeline_fn=lambda _config: fake_pipeline,
            schemas_mod=_schemas_mod(),
            catalog_table_cls=CatalogTable,
        )

    # set_cost_tracker was called once, with the exact tracker instance the
    # strategy received and emit published.
    fake_eg.set_cost_tracker.assert_called_once()
    threaded = fake_eg.set_cost_tracker.call_args.args[0]
    assert isinstance(threaded, CostTracker)
    assert threaded is captured["cost_tracker"]
    assert mock_emit.call_args.args[0] is threaded
