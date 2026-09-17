# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ontology-validation router orchestration."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from coa_ontology.validation.routers import validate as vr
from coa_ontology.validation.schemas import (
    JobResponse,
    JobStatus,
    ValidationRequest,
    ValidationTier,
)
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_TURTLE = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://ex.org/> .
ex:Agreement a owl:Class ; rdfs:label "Agreement" ; rdfs:comment "A deal" .
ex:Party a owl:Class ; rdfs:label "Party" ; rdfs:comment "A party" .
ex:hasParty a owl:ObjectProperty ; rdfs:domain ex:Agreement ; rdfs:range ex:Party .
"""


@pytest.fixture(autouse=True)
def _clean_state():
    vr._jobs.clear()
    vr._job_namespaces.clear()
    yield
    vr._jobs.clear()
    vr._job_namespaces.clear()


def _job(job_id: str = "j1") -> JobResponse:
    return JobResponse(job_id=job_id, status=JobStatus.PENDING, created_at=datetime.utcnow())


# ── _fetch_ontology_turtle ───────────────────────────────────────────


def _httpx_client(get_map: dict[str, MagicMock]) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)

    def _get(url: str, **kwargs):
        for key, resp in get_map.items():
            if url.endswith(key):
                return resp
        raise AssertionError(f"unexpected GET {url}")

    client.get.side_effect = _get
    return client


def test_fetch_ontology_turtle_follows_presigned_download_url() -> None:
    """#143: /download returns {"downloadUrl"}; the Turtle is fetched out-of-band."""
    meta = MagicMock()
    meta.json.return_value = {"ontology_type": "induced"}
    meta.raise_for_status = MagicMock()
    dl = MagicMock()
    dl.json.return_value = {
        "downloadUrl": "https://s3.example/presigned/ontology.ttl",
        "expiresInSeconds": 3600,
    }
    # If the JSON envelope were parsed as Turtle this would be the bug — assert we
    # never fall back to it when a downloadUrl is present.
    dl.text = '{"downloadUrl": "https://s3.example/presigned/ontology.ttl"}'
    dl.raise_for_status = MagicMock()
    obj = MagicMock()
    obj.text = "@prefix x: <http://x> ."
    obj.raise_for_status = MagicMock()
    client = _httpx_client({"presigned/ontology.ttl": obj, "/download": dl, "ont-1": meta})
    with patch.object(httpx, "Client", return_value=client):
        out = vr._fetch_ontology_turtle("http://oc", "ont-1", namespace="ns1")
    assert out == "@prefix x: <http://x> ."


def test_fetch_ontology_turtle_falls_back_to_inline_body() -> None:
    """Rollout fallback: an ontology-engine that still inlines the Turtle body
    (no downloadUrl in the response) is still read correctly."""
    meta = MagicMock()
    meta.json.return_value = {"ontology_type": "induced"}
    meta.raise_for_status = MagicMock()
    dl = MagicMock()
    dl.json.side_effect = ValueError("not json")
    dl.text = "@prefix x: <http://x> ."
    dl.raise_for_status = MagicMock()
    client = _httpx_client({"/download": dl, "ont-1": meta})
    with patch.object(httpx, "Client", return_value=client):
        out = vr._fetch_ontology_turtle("http://oc", "ont-1", namespace="ns1")
    assert out == "@prefix x: <http://x> ."


def test_fetch_ontology_turtle_rejects_foundational() -> None:
    meta = MagicMock()
    meta.json.return_value = {"ontology_type": "foundational"}
    meta.raise_for_status = MagicMock()
    client = _httpx_client({"ont-1": meta})
    with (
        patch.object(httpx, "Client", return_value=client),
        pytest.raises(ValueError, match="Validation is only supported"),
    ):
        vr._fetch_ontology_turtle("http://oc", "ont-1")


# ── _run_validation ──────────────────────────────────────────────────


def test_run_validation_inline_turtle_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    vr._jobs["j1"] = _job()
    body = ValidationRequest(
        ontology_id="ont-1",
        ontology_turtle=_TURTLE,
        tiers=[ValidationTier.tier1_blocking, ValidationTier.tier2_scoring],
    )
    with patch.object(vr, "dynamo_store") as mock_ds:
        mock_ds.get_async_job.return_value = None
        vr._run_validation("j1", body, namespace="ns1", config={"ontology_catalog_url": "http://oc"})
    job = vr._jobs["j1"]
    assert job.status == JobStatus.COMPLETED
    assert job.report is not None
    assert job.report.metrics is not None
    assert job.report.metrics.class_count == 2


def test_run_validation_fetches_when_no_inline_turtle() -> None:
    vr._jobs["j1"] = _job()
    body = ValidationRequest(ontology_id="ont-1", tiers=[ValidationTier.tier1_blocking])
    with (
        patch.object(vr, "dynamo_store") as mock_ds,
        patch.object(vr, "_fetch_ontology_turtle", return_value=_TURTLE) as mock_fetch,
    ):
        mock_ds.get_async_job.return_value = None
        vr._run_validation("j1", body, namespace="ns1", config={"ontology_catalog_url": "http://oc"})
    mock_fetch.assert_called_once()
    assert vr._jobs["j1"].status == JobStatus.COMPLETED


def test_run_validation_marks_failed_on_error() -> None:
    vr._jobs["j1"] = _job()
    body = ValidationRequest(ontology_id="ont-1", tiers=[ValidationTier.tier1_blocking])
    with (
        patch.object(vr, "dynamo_store") as mock_ds,
        patch.object(vr, "_fetch_ontology_turtle", side_effect=ValueError("boom")),
    ):
        mock_ds.get_async_job.return_value = None
        vr._run_validation("j1", body, namespace="ns1", config={"ontology_catalog_url": "http://oc"})
    job = vr._jobs["j1"]
    assert job.status == JobStatus.FAILED
    assert job.error == "boom"


def test_run_validation_empty_fetch_result_fails() -> None:
    vr._jobs["j1"] = _job()
    body = ValidationRequest(ontology_id="ont-1", tiers=[ValidationTier.tier1_blocking])
    with (
        patch.object(vr, "dynamo_store") as mock_ds,
        patch.object(vr, "_fetch_ontology_turtle", return_value=""),
    ):
        mock_ds.get_async_job.return_value = None
        vr._run_validation("j1", body, namespace="ns1", config={"ontology_catalog_url": "http://oc"})
    assert vr._jobs["j1"].status == JobStatus.FAILED
    assert "No content available" in vr._jobs["j1"].error


# ── _persist ─────────────────────────────────────────────────────────


def test_persist_inserts_new_job() -> None:
    job = _job()
    with patch.object(vr, "dynamo_store") as mock_ds:
        mock_ds.get_async_job.return_value = None
        vr._persist(job, namespace="ns1")
    mock_ds.put_async_job.assert_called_once()
    mock_ds.update_async_job.assert_not_called()


def test_persist_updates_existing_job() -> None:
    job = _job()
    with patch.object(vr, "dynamo_store") as mock_ds:
        mock_ds.get_async_job.return_value = {"job_id": "j1"}
        vr._persist(job, namespace="ns1")
    mock_ds.update_async_job.assert_called_once()
    mock_ds.put_async_job.assert_not_called()


def test_persist_swallows_dynamo_errors() -> None:
    job = _job()
    with patch.object(vr, "dynamo_store") as mock_ds:
        mock_ds.get_async_job.side_effect = RuntimeError("dynamo down")
        # Must not raise — durability is best-effort.
        vr._persist(job, namespace="ns1")


# ── get_job / list_jobs ──────────────────────────────────────────────


def test_get_job_returns_in_memory_job_for_matching_namespace() -> None:
    vr._jobs["j1"] = _job()
    vr._job_namespaces["j1"] = "ns1"
    assert vr.get_job("j1", namespace="ns1").job_id == "j1"


def test_get_job_404_on_namespace_mismatch() -> None:
    vr._jobs["j1"] = _job()
    vr._job_namespaces["j1"] = "ns1"
    with pytest.raises(HTTPException) as exc:
        vr.get_job("j1", namespace="other-ns")
    assert exc.value.status_code == 404


def test_get_job_falls_back_to_dynamo() -> None:
    persisted = {
        "job_id": "j2",
        "status": "completed",
        "created_at": datetime.utcnow().isoformat(),
        "report": None,
        "error": None,
    }
    with patch.object(vr, "dynamo_store") as mock_ds:
        mock_ds.get_async_job.return_value = persisted
        out = vr.get_job("j2", namespace="ns1")
    assert out.job_id == "j2"
    assert out.status == JobStatus.COMPLETED


def test_get_job_404_when_absent_everywhere() -> None:
    with patch.object(vr, "dynamo_store") as mock_ds:
        mock_ds.get_async_job.return_value = None
        with pytest.raises(HTTPException) as exc:
            vr.get_job("missing", namespace="ns1")
    assert exc.value.status_code == 404


def test_get_job_404_when_dynamo_unreachable() -> None:
    with patch.object(vr, "dynamo_store") as mock_ds:
        mock_ds.get_async_job.side_effect = RuntimeError("dynamo down")
        with pytest.raises(HTTPException) as exc:
            vr.get_job("missing", namespace="ns1")
    assert exc.value.status_code == 404


def test_list_jobs_filters_by_namespace() -> None:
    vr._jobs["a"] = _job("a")
    vr._jobs["b"] = _job("b")
    vr._job_namespaces["a"] = "ns1"
    vr._job_namespaces["b"] = "ns2"
    out = vr.list_jobs(namespace="ns1")
    assert [j.job_id for j in out] == ["a"]


# ── start_validation ─────────────────────────────────────────────────


def test_start_validation_registers_job_and_starts_thread() -> None:
    body = ValidationRequest(ontology_id="ont-1", ontology_turtle=_TURTLE)
    request = MagicMock()
    request.app.state.config = {"ontology_catalog_url": "http://oc"}
    with (
        patch.object(vr, "dynamo_store") as mock_ds,
        patch.object(vr, "Thread") as mock_thread,
    ):
        mock_ds.get_async_job.return_value = None
        resp = vr.start_validation(body, request, namespace="ns1")
    assert resp.status == JobStatus.PENDING
    assert vr._job_namespaces[resp.job_id] == "ns1"
    mock_thread.assert_called_once()
    mock_thread.return_value.start.assert_called_once()
