# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The induction entry points resolve the namespace language once, server-side.

Pins LLD section 2.2.8 (ontology induction row): the language comes from the
namespace item, never from the request; it reaches the structured strategy, the
unstructured worker and the rebuild path; default proposal titles follow it; and
a non-English ``rigor_ontology`` request is rejected before any job state exists.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import induce_catalog
from coa_ontology.induce_catalog import WorkbenchInductionRequest, start_induction
from coa_ontology.inducer.services.data_catalog import CatalogTable
from coa_ontology.namespace_language import (
    NamespaceLanguageNotFoundError,
    NamespaceLanguageUnconfiguredError,
    resolve_namespace_language,
)
from fastapi import HTTPException
from rdflib import Graph

pytestmark = pytest.mark.unit

_PREFIX = "http://example.org/ns/ontology/induced#"


@pytest.fixture(autouse=True)
def _isolate_induce_catalog_globals():
    jobs = dict(induce_catalog._jobs)
    namespaces = dict(induce_catalog._job_namespaces)
    try:
        yield
    finally:
        induce_catalog._jobs.clear()
        induce_catalog._jobs.update(jobs)
        induce_catalog._job_namespaces.clear()
        induce_catalog._job_namespaces.update(namespaces)


def _request(config: dict | None = None) -> MagicMock:
    request = MagicMock()
    request.app.state.config = config if config is not None else {}
    return request


def _body(strategy: str = "table_to_ontology", **extra) -> WorkbenchInductionRequest:
    return WorkbenchInductionRequest(datasource_ids=["ds-1"], ontology_uri_prefix=_PREFIX, strategy=strategy, **extra)


# ── resolve_namespace_language ──────────────────────────────────────────


class _FakeTable:
    def __init__(self, items: dict[tuple[str, str], dict]):
        self._items = items
        self.reads: list[dict] = []

    def get_item(self, Key: dict) -> dict:  # noqa: N803 — boto3 keyword
        self.reads.append(Key)
        item = self._items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item is not None else {}


def _resolve_with(items: dict[tuple[str, str], dict], namespace: str) -> str:
    table = _FakeTable(items)
    resource = MagicMock()
    resource.Table.return_value = table
    with patch("boto3.resource", return_value=resource):
        return resolve_namespace_language(namespace, {"namespaces_table": "ns-table", "smus_region": "us-east-1"})


class TestResolveNamespaceLanguage:
    def test_stored_language_is_returned_canonical(self) -> None:
        ns = str(uuid.uuid4())
        item = {"PK": f"NS#{ns}", "SK": "METADATA", "language": "ja"}
        assert _resolve_with({(f"NS#{ns}", "METADATA"): item}, ns) == "ja"

    def test_legacy_item_without_language_reads_as_en(self) -> None:
        ns = str(uuid.uuid4())
        assert _resolve_with({(f"NS#{ns}", "METADATA"): {"PK": f"NS#{ns}", "SK": "METADATA"}}, ns) == "en"

    def test_namespace_name_resolves_through_the_reservation(self) -> None:
        ns = str(uuid.uuid4())
        name = f"plant-docs-{ns[:8]}"
        items = {
            (f"NS_NAME#{name}", "RESERVATION"): {"namespaceId": ns},
            (f"NS#{ns}", "METADATA"): {"language": "ja-JP"},
        }
        assert _resolve_with(items, name) == "ja-JP"

    def test_missing_namespace_is_an_error_not_english(self) -> None:
        with pytest.raises(NamespaceLanguageNotFoundError):
            _resolve_with({}, str(uuid.uuid4()))

    def test_read_failure_propagates(self) -> None:
        resource = MagicMock()
        resource.Table.return_value.get_item.side_effect = RuntimeError("throttled")
        with patch("boto3.resource", return_value=resource), pytest.raises(RuntimeError, match="throttled"):
            resolve_namespace_language(str(uuid.uuid4()), {"namespaces_table": "ns-table"})

    def test_local_http_catalog_without_namespaces_table_induces_as_english(self, monkeypatch) -> None:
        monkeypatch.delenv("NAMESPACES_TABLE", raising=False)
        monkeypatch.delenv("CATALOG_SOURCE", raising=False)
        with patch("boto3.resource") as resource:
            assert resolve_namespace_language("local", {}) == "en"
            assert resolve_namespace_language("local", {"catalog_source": "http"}) == "en"
        resource.assert_not_called()

    def test_deployed_catalog_without_namespaces_table_fails(self, monkeypatch) -> None:
        monkeypatch.delenv("NAMESPACES_TABLE", raising=False)
        with pytest.raises(NamespaceLanguageUnconfiguredError):
            resolve_namespace_language("ns-1", {"catalog_source": "smus"})
        monkeypatch.setenv("CATALOG_SOURCE", "smus")
        with pytest.raises(NamespaceLanguageUnconfiguredError):
            resolve_namespace_language("ns-1", {})

    def test_route_maps_missing_namespaces_table_to_500(self, monkeypatch) -> None:
        monkeypatch.delenv("NAMESPACES_TABLE", raising=False)
        with patch.object(induce_catalog, "dynamo_store") as dynamo, pytest.raises(HTTPException) as err:
            start_induction(body=_body(), request=_request({"catalog_source": "smus"}), namespace="ns-1")
        assert err.value.status_code == 500
        assert dynamo.mock_calls == []


# ── structured route ────────────────────────────────────────────────────


class TestStructuredRoute:
    def test_non_english_rigor_is_rejected_before_any_job_state(self) -> None:
        jobs_before = dict(induce_catalog._jobs)
        with (
            patch.object(induce_catalog, "resolve_namespace_language", return_value="ja"),
            patch.object(induce_catalog, "dynamo_store") as dynamo,
            patch.object(induce_catalog, "Thread") as thread,
            pytest.raises(HTTPException) as err,
        ):
            start_induction(body=_body("rigor_ontology"), request=_request(), namespace="ns-1")
        assert err.value.status_code == 400
        assert "rigor_ontology" in str(err.value.detail)
        assert dynamo.mock_calls == []
        thread.assert_not_called()
        assert induce_catalog._jobs == jobs_before

    @pytest.mark.parametrize("language", ["ja-JP", "ko", "de", "zh-Hans"])
    def test_every_non_english_language_rejects_rigor(self, language: str) -> None:
        with (
            patch.object(induce_catalog, "resolve_namespace_language", return_value=language),
            patch.object(induce_catalog, "dynamo_store") as dynamo,
            pytest.raises(HTTPException) as err,
        ):
            start_induction(body=_body("rigor_ontology"), request=_request(), namespace="ns-1")
        assert err.value.status_code == 400
        assert dynamo.mock_calls == []

    def test_english_rigor_still_starts(self) -> None:
        with (
            patch.object(induce_catalog, "resolve_namespace_language", return_value="en-US"),
            patch.object(induce_catalog, "dynamo_store") as dynamo,
            patch.object(induce_catalog, "Thread") as thread,
            patch.object(induce_catalog, "_schemas_mod", __import__("coa_ontology.inducer.schemas", fromlist=["x"])),
        ):
            dynamo.list_proposals.return_value = []
            start_induction(body=_body("rigor_ontology"), request=_request(), namespace="ns-1")
        dynamo.acquire_induction_lock.assert_called_once()
        thread.assert_called_once()

    def test_missing_namespace_is_404_without_job_state(self) -> None:
        with (
            patch.object(
                induce_catalog, "resolve_namespace_language", side_effect=NamespaceLanguageNotFoundError("gone")
            ),
            patch.object(induce_catalog, "dynamo_store") as dynamo,
            pytest.raises(HTTPException) as err,
        ):
            start_induction(body=_body(), request=_request(), namespace="ns-1")
        assert err.value.status_code == 404
        assert dynamo.mock_calls == []

    def test_language_reaches_the_worker_and_default_title_is_japanese(self) -> None:
        with (
            patch.object(induce_catalog, "resolve_namespace_language", return_value="ja") as resolve,
            patch.object(induce_catalog, "dynamo_store") as dynamo,
            patch.object(induce_catalog, "Thread") as thread,
            patch.object(induce_catalog, "_schemas_mod", __import__("coa_ontology.inducer.schemas", fromlist=["x"])),
        ):
            dynamo.list_proposals.return_value = []
            start_induction(body=_body(), request=_request(), namespace="ns-1")
        resolve.assert_called_once_with("ns-1", {})
        assert thread.call_args.kwargs["args"][-1] == "ja"
        assert dynamo.create_proposal_stub.call_args.kwargs["label"] == "構造化データから生成したオントロジー"

    def test_explicit_label_is_kept(self) -> None:
        with (
            patch.object(induce_catalog, "resolve_namespace_language", return_value="ja"),
            patch.object(induce_catalog, "dynamo_store") as dynamo,
            patch.object(induce_catalog, "Thread"),
            patch.object(induce_catalog, "_schemas_mod", __import__("coa_ontology.inducer.schemas", fromlist=["x"])),
        ):
            dynamo.list_proposals.return_value = []
            start_induction(body=_body(label="My Plant Ontology"), request=_request(), namespace="ns-1")
        assert dynamo.create_proposal_stub.call_args.kwargs["label"] == "My Plant Ontology"

    def test_english_default_title_is_unchanged(self) -> None:
        with (
            patch.object(induce_catalog, "resolve_namespace_language", return_value="en"),
            patch.object(induce_catalog, "dynamo_store") as dynamo,
            patch.object(induce_catalog, "Thread"),
            patch.object(induce_catalog, "_schemas_mod", __import__("coa_ontology.inducer.schemas", fromlist=["x"])),
        ):
            dynamo.list_proposals.return_value = []
            start_induction(body=_body(), request=_request(), namespace="ns-1")
        assert dynamo.create_proposal_stub.call_args.kwargs["label"] == "Induced Structured Ontology"

    def test_unstructured_dispatch_forwards_the_resolved_language(self) -> None:
        with (
            patch.object(induce_catalog, "resolve_namespace_language", return_value="ja") as resolve,
            patch("coa_ontology.inducer.routers.induce_unstructured.start_unstructured_job") as start_job,
        ):
            start_job.return_value = SimpleNamespace(job_id="j", status="pending")
            start_induction(
                body=_body("unstructured_lexical_graph"),
                request=_request({"neptune_endpoint": "cluster.example:8182"}),
                namespace="ns-1",
            )
        resolve.assert_called_once()
        assert start_job.call_args.kwargs["namespace_language"] == "ja"
        assert start_job.call_args.kwargs["body"].label == "非構造化データから生成したオントロジー"


class _RecordingStrategy:
    calls: list[dict] = []

    def induce(self, **kwargs):
        type(self).calls.append(kwargs)
        return Graph(), set(), [], []

    def build_r2rml(self, **_kwargs):
        return Graph()


def test_run_induction_passes_language_to_strategy_and_records_it(tmp_path, monkeypatch) -> None:
    from coa_ontology.inducer.schemas import JobResponse, JobStatus

    monkeypatch.setenv("INDUCE_OUTPUT_DIR", str(tmp_path))
    _RecordingStrategy.calls = []
    job_id = "job-lang"
    induce_catalog._jobs[job_id] = JobResponse(
        job_id=job_id, status=JobStatus.PENDING, created_at="2026-01-01T00:00:00Z"
    )
    with (
        patch.object(
            induce_catalog,
            "_catalog_to_tables",
            return_value=[{"id": "t", "name": "orders", "fullyQualifiedName": "db.orders", "columns": []}],
        ),
        patch.object(induce_catalog, "_fetch_catalog_from_fixture", return_value={"databases": []}),
        patch.object(induce_catalog, "dynamo_store") as dynamo,
        patch.object(induce_catalog, "get_strategy", return_value=_RecordingStrategy),
        patch.object(induce_catalog, "_start_watchdog", return_value=None),
        patch.object(induce_catalog, "emit_induction_job_metrics"),
        patch("coa_ontology.stores.build_stores", return_value=(MagicMock(), MagicMock())),
        patch("coa_ontology.stores.adapters.StoreOntologyCatalogAdapter"),
    ):
        dynamo.list_proposals.return_value = []
        induce_catalog._run_induction(
            job_id,
            WorkbenchInductionRequest(datasource_ids=["ds-mock"], ontology_uri_prefix=_PREFIX),
            "ns-1",
            {},
            MagicMock(),
            __import__("coa_ontology.inducer.schemas", fromlist=["x"]),
            CatalogTable,
            "ja",
        )
    assert induce_catalog._jobs[job_id].status == JobStatus.COMPLETED, induce_catalog._jobs[job_id].error
    assert _RecordingStrategy.calls[0]["namespace_language"] == "ja"
    metadata = dynamo.create_proposal.call_args.kwargs["metadata"]
    assert metadata["label"] == "構造化データから生成したオントロジー"
    assert metadata["namespaceLanguage"] == "ja"


# ── unstructured route ──────────────────────────────────────────────────


def test_unstructured_route_resolves_language_for_the_worker() -> None:
    from coa_ontology.inducer.routers import induce_unstructured
    from coa_ontology.inducer.unstructured.schemas import UnstructuredInductionRequest

    body = UnstructuredInductionRequest(
        graph_arn="arn:aws:neptune-graph:us-east-1:123456789012:graph/g-abc123",
        name="docs",
        ontology_uri_prefix=_PREFIX,
    )
    with (
        patch.object(induce_catalog, "resolve_namespace_language", return_value="ja") as resolve,
        patch.object(induce_unstructured, "dynamo_store") as dynamo,
        patch.object(induce_unstructured, "Thread") as thread,
    ):
        dynamo.list_proposals.return_value = []
        induce_unstructured.start_induction(body=body, request=_request(), namespace="ns-1")
    resolve.assert_called_once_with("ns-1", {})
    assert thread.call_args.kwargs["args"][-1] == "ja"
    assert dynamo.create_proposal_stub.call_args.kwargs["label"] == "非構造化データから生成したオントロジー"


def test_unstructured_request_cannot_carry_a_language() -> None:
    from coa_ontology.inducer.unstructured.schemas import UnstructuredInductionRequest

    assert "namespace_language" not in UnstructuredInductionRequest.model_fields
    assert "language" not in UnstructuredInductionRequest.model_fields
    assert "language" not in WorkbenchInductionRequest.model_fields
    assert "namespace_language" not in WorkbenchInductionRequest.model_fields


# ── grounding-override rebuild ──────────────────────────────────────────


def test_grounding_override_rebuild_keeps_the_namespace_language(monkeypatch) -> None:
    """Re-grounding regenerates the structured ontology; it must not drop tags."""
    from coa_ontology import proposals
    from coa_ontology.inducer.strategies.table_to_ontology import TableToOntologyStrategy

    monkeypatch.setattr("coa_ontology.induce_catalog._fetch_catalog", lambda *a, **k: {})
    monkeypatch.setattr(
        "coa_ontology.induce_catalog._catalog_to_tables",
        lambda catalog: [
            {
                "id": "t-orders",
                "name": "orders",
                "fullyQualifiedName": "db.orders",
                "description": "購入注文",
                "generatedFields": ["description"],
                "columns": [{"name": "id", "dataType": "INT"}],
            }
        ],
    )
    turtles: dict[str, str] = {}
    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", lambda *a, **k: "matches-key")
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": turtles.setdefault(artifact, content),
    )
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", lambda *a, **k: None)
    item = {
        "proposal_id": "p-1",
        "ontology_id": _PREFIX,
        "status": "pending",
        "metadata": {
            "datasource_ids": ["ds-1"],
            "matches": [
                {
                    "source_table": "orders",
                    "source_column": "",
                    "matched_class_uri": None,
                    "matched_ontology_id": None,
                    "similarity": None,
                    "match_type": "novel",
                    "candidates": [],
                }
            ],
        },
    }
    build = TableToOntologyStrategy._build_proposal_ontology
    with (
        patch.object(induce_catalog, "resolve_namespace_language", return_value="ja") as resolve,
        patch.object(TableToOntologyStrategy, "_build_proposal_ontology", autospec=True, side_effect=build) as spy,
    ):
        proposals._apply_grounding_overrides("p-1", {"orders": None}, item, namespace="ns-1")
    resolve.assert_called_once()
    assert spy.call_args.kwargs["namespace_language"] == "ja"
    assert '"購入注文"@ja' in turtles["ontology"]


# ── accept-time fallback title ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("source_type", "language", "expected"),
    [
        ("STRUCTURED", "ja", "構造化データから生成したオントロジー"),
        ("UNSTRUCTURED", "ja", "非構造化データから生成したオントロジー"),
        ("STRUCTURED", "en", "Induced Structured Ontology"),
        ("UNSTRUCTURED", "en", "Induced Unstructured Ontology"),
    ],
)
def test_accept_fallback_label_follows_the_namespace_language(source_type, language, expected) -> None:
    from coa_ontology import proposals

    item = {
        "proposal_id": "p-1",
        "job_id": "p-1",
        "status": "pending",
        "source_type": source_type,
        "ontology_id": _PREFIX,
        "ontology_turtle": "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "metadata": {},
    }
    with (
        patch.object(proposals, "dynamo_store") as dynamo,
        patch.object(proposals, "Thread") as thread,
        patch.object(induce_catalog, "resolve_namespace_language", return_value=language) as resolve,
    ):
        dynamo.get_proposal_by_id.return_value = item
        dynamo.get_reviewed_proposal.return_value = None
        dynamo.list_ontologies_registry.return_value = []
        proposals.accept_proposal("p-1", namespace="ns-1")
    resolve.assert_called_once()
    assert expected in thread.call_args.kwargs["args"]


def test_accept_with_stored_label_does_not_read_the_namespace() -> None:
    from coa_ontology import proposals

    item = {
        "proposal_id": "p-1",
        "job_id": "p-1",
        "status": "pending",
        "ontology_id": _PREFIX,
        "ontology_turtle": "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "metadata": {"label": "Plant"},
    }
    with (
        patch.object(proposals, "dynamo_store") as dynamo,
        patch.object(proposals, "Thread") as thread,
        patch.object(induce_catalog, "resolve_namespace_language") as resolve,
    ):
        dynamo.get_proposal_by_id.return_value = item
        dynamo.get_reviewed_proposal.return_value = None
        dynamo.list_ontologies_registry.return_value = []
        proposals.accept_proposal("p-1", namespace="ns-1")
    resolve.assert_not_called()
    assert "Plant" in thread.call_args.kwargs["args"]
