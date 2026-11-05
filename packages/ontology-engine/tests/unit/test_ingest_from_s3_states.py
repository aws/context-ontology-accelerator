# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The async ingest-from-s3 worker exposes an EMBEDDINGS_SYNC phase.

``_run_ingest_from_s3`` transitions RUNNING → EMBEDDINGS_SYNC (set via the
``on_embeddings_sync`` hook of ``ingest_ontology_and_wait``, right before it
waits for the just-written embeddings to become searchable) → COMPLETED. A
poller therefore sees a distinct "syncing embeddings" phase rather than a
stalled-looking RUNNING.

The worker routes through the shared ``ingest_ontology_and_wait`` helper, so
these tests patch the *inner* seams (``ingest_ontology`` and
``wait_for_embeddings_searchable``) on the ``catalog.ingest`` module and let
the real helper run — that's what fires the ``on_embeddings_sync`` callback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _run(monkeypatch, *, wait_spy):
    """Drive the worker with all I/O collaborators mocked; return the ordered
    list of ingest-job states the worker wrote."""
    import coa_ontology.catalog.ingest as ingest_mod
    import coa_ontology.catalog.routers.ontologies as mod

    transitions: list[str] = []

    def _update_ingest_job(job_id, state, namespace=None, **kw):
        # State may be the IngestJobState enum — normalize to its wire value.
        transitions.append(getattr(state, "value", state))

    monkeypatch.setattr(mod.dynamo_store, "update_ingest_job", _update_ingest_job)
    monkeypatch.setattr(
        mod.dynamo_store,
        "get_ontology_registry",
        lambda ns, oid: {"class_count": 2, "embedding_count": 2},
    )

    # S3 get_object → small turtle payload.
    s3 = MagicMock()
    s3.get_object.return_value = {
        "ContentLength": 64,
        "Body": MagicMock(read=lambda: b"@prefix ex: <http://ex/> . ex:A a <http://www.w3.org/2002/07/owl#Class> ."),
    }
    monkeypatch.setattr("boto3.client", lambda *a, **k: s3)

    monkeypatch.setattr(mod, "_stores", lambda ns: (MagicMock(), MagicMock()))
    # Patch the inner seams the shared helper calls, NOT the helper itself, so
    # ingest_ontology_and_wait runs for real and fires on_embeddings_sync.
    monkeypatch.setattr(
        ingest_mod,
        "ingest_ontology",
        lambda **kw: {
            "ontology_id": "http://ex/onto",
            "embeddings": {"entity_uris": ["http://ex/A"]},
        },
    )
    monkeypatch.setattr(ingest_mod, "wait_for_embeddings_searchable", wait_spy)

    mod._run_ingest_from_s3(
        job_id="j-1",
        ontology_id="http://ex/onto",
        s3_key="ontology-uploads/ns-a/u1/my.ttl",
        fmt="turtle",
        title="My Ontology",
        ontology_type="user_created",
        namespace="ns-a",
    )
    return transitions


def test_worker_transition_sequence_is_running_sync_completed(monkeypatch):
    """The worker writes RUNNING → EMBEDDINGS_SYNC → COMPLETED in order."""
    transitions = _run(monkeypatch, wait_spy=lambda **kw: True)
    assert transitions == ["running", "embeddings_sync", "completed"]


def test_worker_enters_wait_only_after_embeddings_sync(monkeypatch):
    """The EMBEDDINGS_SYNC transition must be recorded BEFORE the wait runs."""
    captured: dict[str, list[str]] = {}

    import coa_ontology.catalog.ingest as ingest_mod
    import coa_ontology.catalog.routers.ontologies as mod

    transitions: list[str] = []

    def _update_ingest_job(job_id, state, namespace=None, **kw):
        transitions.append(getattr(state, "value", state))

    def _wait_spy(**kw):
        captured["at_wait"] = list(transitions)
        return True

    s3 = MagicMock()
    s3.get_object.return_value = {
        "ContentLength": 64,
        "Body": MagicMock(read=lambda: b"@prefix ex: <http://ex/> . ex:A a <http://www.w3.org/2002/07/owl#Class> ."),
    }

    with (
        patch.object(mod.dynamo_store, "update_ingest_job", _update_ingest_job),
        patch.object(mod.dynamo_store, "get_ontology_registry", lambda ns, oid: {}),
        patch("boto3.client", lambda *a, **k: s3),
        patch.object(mod, "_stores", lambda ns: (MagicMock(), MagicMock())),
        patch.object(
            ingest_mod,
            "ingest_ontology",
            lambda **kw: {"ontology_id": "http://ex/onto", "embeddings": {"entity_uris": ["http://ex/A"]}},
        ),
        patch.object(ingest_mod, "wait_for_embeddings_searchable", _wait_spy),
    ):
        mod._run_ingest_from_s3(
            job_id="j-1",
            ontology_id="http://ex/onto",
            s3_key="ontology-uploads/ns-a/u1/my.ttl",
            fmt="turtle",
            title="My Ontology",
            ontology_type="user_created",
            namespace="ns-a",
        )

    # At the instant the wait fired, the last recorded state was embeddings_sync
    # (and completed had not yet been written).
    assert captured["at_wait"][-1] == "embeddings_sync"
    assert "completed" not in captured["at_wait"]


def test_worker_reports_validation_failures_to_the_author(monkeypatch):
    """A rejected graph binding must reach the job, not just its exception class."""
    import coa_ontology.catalog.ingest as ingest_mod
    import coa_ontology.catalog.routers.ontologies as mod

    errors: list[str] = []
    stub_failures: list[str] = []

    def _update_ingest_job(job_id, state, namespace=None, **kw):
        if "error" in kw:
            errors.append(kw["error"])

    def _reject(**kw):
        raise ingest_mod.IngestValidationError('Class http://ex/A: No data source named "g"', findings=[])

    s3 = MagicMock()
    s3.get_object.return_value = {"ContentLength": 8, "Body": MagicMock(read=lambda: b"@prefix ex: <http://ex/> .")}
    with (
        patch.object(mod.dynamo_store, "update_ingest_job", _update_ingest_job),
        patch.object(mod, "_fail_ingest_stub", lambda ns, oid, msg: stub_failures.append(msg)),
        patch("boto3.client", lambda *a, **k: s3),
        patch.object(mod, "_stores", lambda ns: (MagicMock(), MagicMock())),
        patch.object(ingest_mod, "ingest_ontology", _reject),
    ):
        mod._run_ingest_from_s3(
            job_id="j-1",
            ontology_id="http://ex/onto",
            s3_key="ontology-uploads/ns-a/u1/my.ttl",
            fmt="turtle",
            title="My Ontology",
            ontology_type="user_created",
            namespace="ns-a",
        )
    assert errors == ['Validation failed: Class http://ex/A: No data source named "g"']
    assert stub_failures == errors
