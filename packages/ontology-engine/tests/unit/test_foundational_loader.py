# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Foundational ontology loader tests (async contract).

The loader has historically been deceptively shallow: ``create_ontology
+ fetch_ontology_from_source`` only registered the metadata + cached
the raw bytes; nothing produced embeddings, so Match could never use
the ontology for grounding. The June 2026 fix routes the fetched
bytes through ``ingest_ontology`` so embeddings are created end-to-end.

The July 2026 fix makes the load **asynchronous**: the endpoint returns
202 + an ``IngestJob`` handle immediately and runs fetch + parse +
embedding on a background thread, driving a DynamoDB-backed IngestJob
(keyed by the ontology URI, same as ``ingest-from-s3``) that the shared
``GET /ontologies/{id}/ingest-status/{job}`` poll endpoint reports on.
Async because fetching + embedding a large ontology (FIBO, Schema.org)
can exceed API Gateway's 29s integration timeout.

These tests pin the post-fix contract:

  * The POST endpoint returns 202 + a PENDING IngestJob keyed by the
    ontology URI, and never blocks on fetch/ingest.
  * The background worker drives the job RUNNING → COMPLETED, calling
    ``ingest`` exactly once and recording the counts on the job result.
  * Idempotent — a 409 from create_ontology (row already exists) doesn't
    stop the load.
  * A non-409 create error bubbles out synchronously (real failure).
  * Fetch/parse failure drives the job FAILED without ingesting.
  * An unknown curated key is a hard 404.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_control_plane_server.models.ingest_job_state import IngestJobState
from fastapi import HTTPException

pytestmark = pytest.mark.unit


def _stub_ingest_result() -> dict:
    """Shape of ``ingest_ontology``'s return value the loader inspects."""
    return {
        "ontology_id": "https://schema.org/",
        "class_count": 800,
        "property_count": 1300,
        "axiom_count": 50_000,
        "embeddings": {
            "count": 800,
            "model_id": "stub-embed",
            "index": "ns-idx",
            "entity_uris": ["https://schema.org/Thing", "https://schema.org/name"],
        },
        "registry": {"parse_status": "ok"},
    }


class _FetchStub:
    """Mimic ``OntologyFetchResponse`` — the loader uses ``getattr`` so
    only the attribute access matters, not the model class itself."""

    def __init__(self, **fields):
        for k, v in fields.items():
            setattr(self, k, v)

    def model_dump(self):
        return {k: v for k, v in self.__dict__.items()}


@pytest.fixture
def patched_loader():
    """Patch the loader's collaborators so we can run it in isolation.

    We replace the seams the worker uses (create/fetch/ingest/stores/open)
    plus the DynamoDB IngestJob writes, and verify the worker sequences
    them correctly and records job state — not re-test the collaborators.
    """
    with (
        patch("coa_ontology.catalog.routers.foundational.create_ontology") as create,
        patch("coa_ontology.catalog.routers.foundational.fetch_ontology_from_source") as fetch,
        patch("coa_ontology.catalog.ingest.ingest_ontology") as ingest,
        patch("coa_ontology.catalog.routers.ontologies._stores") as stores,
        patch("coa_ontology.catalog.routers.foundational.dynamo_store") as dynamo,
        patch("builtins.open", create=True) as mocked_open,
    ):
        # _stores returns (graph_store, vector_store). The worker waits for
        # the embeddings it wrote (entity_uris in the ingest result) to be
        # searchable, so the mock vector store must report them as present.
        mock_vec = MagicMock()
        mock_vec.list_searchable_entity_uris = MagicMock(
            return_value=["https://schema.org/Thing", "https://schema.org/name"]
        )
        stores.return_value = (object(), mock_vec)
        mocked_open.return_value.__enter__.return_value.read.return_value = (
            b"@prefix ex: <http://ex/> . ex:A a <http://www.w3.org/2002/07/owl#Class> ."
        )
        fetch.return_value = _FetchStub(
            parse_status="ok",
            file_path="/tmp/stub-ontology.ttl",
            class_count=1,
            property_count=0,
        )
        ingest.return_value = _stub_ingest_result()

        yield {
            "create": create,
            "fetch": fetch,
            "ingest": ingest,
            "stores": stores,
            "dynamo": dynamo,
            "open": mocked_open,
        }


def test_load_foundational_returns_202_job_and_never_blocks(patched_loader):
    """The POST endpoint registers the row + a PENDING job keyed by URI,
    spawns the worker on a thread, and returns 202 immediately."""
    from coa_ontology.catalog.routers.foundational import (
        _BY_KEY,
        load_foundational_ontology,
    )

    # Don't actually run the worker thread — assert the handshake only.
    with patch("coa_ontology.catalog.routers.foundational.Thread") as thread:
        resp = load_foundational_ontology("schema-org", namespace="ns-1")

    # Registry row created on the request thread.
    patched_loader["create"].assert_called_once()
    # A PENDING job keyed by the ontology URI was persisted.
    put_call = patched_loader["dynamo"].put_ingest_job.call_args
    assert put_call.args[1] == _BY_KEY["schema-org"].uri  # ontology_id == URI
    assert put_call.args[2] == IngestJobState.PENDING
    # Worker started on a background thread (fetch/ingest did NOT run inline).
    thread.assert_called_once()
    thread.return_value.start.assert_called_once()
    patched_loader["fetch"].assert_not_called()
    patched_loader["ingest"].assert_not_called()

    # 202 body carries a PENDING IngestJob handle keyed by the URI.
    assert resp.result.job_id
    assert resp.result.ontology_id == _BY_KEY["schema-org"].uri
    assert resp.result.status == IngestJobState.PENDING


def test_worker_drives_job_to_completed_with_counts(patched_loader):
    """The background worker fetches + ingests once and records
    RUNNING → COMPLETED with the ingest counts on the job result."""
    from coa_ontology.catalog.routers.foundational import (
        _BY_KEY,
        _run_foundational_load,
    )

    _run_foundational_load("job-1", _BY_KEY["schema-org"], namespace="ns-1")

    patched_loader["fetch"].assert_called_once()
    patched_loader["ingest"].assert_called_once()

    dynamo = patched_loader["dynamo"]
    # RUNNING set first, COMPLETED set last with counts.
    states = [c.args[1] for c in dynamo.update_ingest_job.call_args_list]
    assert states[0] == IngestJobState.RUNNING
    assert states[-1] == IngestJobState.COMPLETED
    final = dynamo.update_ingest_job.call_args_list[-1]
    assert final.kwargs["result"]["class_count"] == 800
    assert final.kwargs["result"]["embedding_count"] == 800
    assert final.kwargs["result"]["ontology_id"] == _BY_KEY["schema-org"].uri


def test_load_foundational_idempotent_when_registry_row_exists(patched_loader):
    """A 409 from create_ontology means the row already exists. The
    endpoint must still register the job and start the worker (refresh)."""
    from coa_ontology.catalog.routers.foundational import (
        load_foundational_ontology,
    )

    patched_loader["create"].side_effect = HTTPException(status_code=409, detail="Already exists")

    with patch("coa_ontology.catalog.routers.foundational.Thread") as thread:
        resp = load_foundational_ontology("schema-org", namespace="ns-1")

    # 409 swallowed; job still registered + worker started.
    patched_loader["dynamo"].put_ingest_job.assert_called_once()
    thread.return_value.start.assert_called_once()
    assert resp.result.status == IngestJobState.PENDING


def test_load_foundational_propagates_non_409_create_errors(patched_loader):
    """A non-conflict error during create should bubble out — it's a
    real failure, not an "already exists" we can recover from. No job
    is registered and no worker starts."""
    from coa_ontology.catalog.routers.foundational import (
        load_foundational_ontology,
    )

    patched_loader["create"].side_effect = HTTPException(status_code=500, detail="DDB unreachable")
    with pytest.raises(HTTPException) as exc:
        load_foundational_ontology("schema-org", namespace="ns-1")
    assert exc.value.status_code == 500
    patched_loader["dynamo"].put_ingest_job.assert_not_called()
    patched_loader["fetch"].assert_not_called()
    patched_loader["ingest"].assert_not_called()


def test_worker_marks_job_failed_when_fetch_fails(patched_loader):
    """If parse_status is 'parse_error' the worker must mark the job
    FAILED without ingesting — embeddings on a malformed file would
    fail anyway."""
    from coa_ontology.catalog.routers.foundational import (
        _BY_KEY,
        _run_foundational_load,
    )

    patched_loader["fetch"].return_value = _FetchStub(
        parse_status="parse_error",
        file_path=None,
    )

    _run_foundational_load("job-err", _BY_KEY["schema-org"], namespace="ns-1")

    patched_loader["ingest"].assert_not_called()
    states = [c.args[1] for c in patched_loader["dynamo"].update_ingest_job.call_args_list]
    assert IngestJobState.FAILED in states


def test_load_foundational_unknown_key_raises_404():
    """An unknown curated key is a hard 404 — the loader doesn't
    pretend the user might have meant something else."""
    from coa_ontology.catalog.routers.foundational import (
        load_foundational_ontology,
    )

    with pytest.raises(HTTPException) as exc:
        load_foundational_ontology("not-a-real-key", namespace="ns-1")
    assert exc.value.status_code == 404


def test_load_foundational_and_wait_registers_row_before_fetch(patched_loader):
    """Sync seed path (induction grounding) must register the registry row
    BEFORE the worker calls fetch_ontology_from_source -- else a fresh
    namespace 404s and grounding matches nothing. Pins create -> fetch order."""
    from coa_ontology.catalog.routers.foundational import (
        _BY_KEY,
        _load_foundational_and_wait,
    )

    order: list[str] = []
    stub = patched_loader["fetch"].return_value

    def _rec_create(*a, **k):
        order.append("create")

    def _rec_fetch(*a, **k):
        order.append("fetch")
        return stub

    patched_loader["create"].side_effect = _rec_create
    patched_loader["fetch"].side_effect = _rec_fetch
    patched_loader["dynamo"].get_ingest_job.return_value = {"status": IngestJobState.COMPLETED}

    ok = _load_foundational_and_wait(_BY_KEY["schema-org"], namespace="ns-fresh")

    assert ok is True
    patched_loader["create"].assert_called_once()
    patched_loader["fetch"].assert_called_once()
    assert order.index("create") < order.index("fetch")  # the 404 guard


def test_load_foundational_and_wait_tolerates_existing_row(patched_loader):
    """A 409 (row already exists) from create_ontology must NOT crash the
    sync seed path -- it proceeds to load (idempotent refresh)."""
    from coa_ontology.catalog.routers.foundational import (
        _BY_KEY,
        _load_foundational_and_wait,
    )

    patched_loader["create"].side_effect = HTTPException(status_code=409, detail="Already exists")
    patched_loader["dynamo"].get_ingest_job.return_value = {"status": IngestJobState.COMPLETED}

    ok = _load_foundational_and_wait(_BY_KEY["schema-org"], namespace="ns-1")

    assert ok is True
    patched_loader["create"].assert_called_once()
    patched_loader["fetch"].assert_called_once()  # proceeded past the 409


def test_worker_marks_its_ingest_as_platform_foundational(patched_loader):
    from coa_ontology.catalog.routers.foundational import _BY_KEY, _run_foundational_load

    _run_foundational_load("job-1", _BY_KEY["schema-org"], namespace="ns-1")

    assert patched_loader["ingest"].call_args.kwargs["metadata"].platform_foundational is True
