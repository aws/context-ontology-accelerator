# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Proposal acceptance → Turtle ingest tests.

``Thread`` is patched so the accept worker runs inline. DynamoDB and
GraphStore are stubbed; no AWS traffic.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from coa_ontology import proposals
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_accept_backoff():
    """Zero the accept-step backoff so retry tests do not sleep."""
    with patch.object(proposals, "_ACCEPT_STEP_BACKOFF_SECONDS", 0):
        yield


class _InlineThread:
    """Stand-in for ``threading.Thread`` that runs the target inline.

    The accept handler returns 202 *before* the work runs, so tests would
    otherwise need to poll DDB. Running the worker synchronously inside
    ``start()`` collapses the timeline into a single call without adding
    sleep loops or wait conditions.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


TURTLE = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <https://example.com/onto#> .

<https://example.com/onto> a owl:Ontology ;
    rdfs:label "Test Ontology" .

ex:Patient a owl:Class ; rdfs:label "Patient" .
ex:Doctor  a owl:Class ; rdfs:label "Doctor" .

ex:treats a owl:ObjectProperty ;
    rdfs:label "treats" ;
    rdfs:domain ex:Doctor ;
    rdfs:range  ex:Patient .

ex:age a owl:DatatypeProperty ;
    rdfs:label "age" ;
    rdfs:domain ex:Patient .
"""


@pytest.fixture()
def accept_client(monkeypatch):
    """FastAPI test client with Dynamo + GraphStore stubbed."""
    monkeypatch.setenv("WORKBENCH_BACKEND", "opensearch_neptune")
    monkeypatch.setenv(
        "NDB_ENDPOINT",
        os.environ.get("NDB_ENDPOINT", "https://<test-ndb-cluster>.cluster-<id>.us-east-1.neptune.amazonaws.com:8182"),
    )
    monkeypatch.setenv(
        "OSS_ENDPOINT",
        os.environ.get("OSS_ENDPOINT", "https://<test-aoss-collection>.<region>.aoss.amazonaws.com"),
    )

    # In-memory Dynamo stand-in
    dynamo = {
        ("default#PROPOSAL#p-1", "META"): {
            "PK": "default#PROPOSAL#p-1",
            "SK": "META",
            "proposal_id": "p-1",
            "job_id": "job-1",
            "namespace": "default",
            "ontology_id": "https://example.com/onto",
            "status": "pending",
            "ontology_turtle": TURTLE,
            "metadata": {"label": "Test Ontology"},
        },
    }

    mock_table = MagicMock()
    mock_table.get_item = MagicMock(
        side_effect=lambda Key: {"Item": dynamo.get((Key["PK"], Key["SK"]))} if (Key["PK"], Key["SK"]) in dynamo else {}
    )

    def fake_update_item(Key, UpdateExpression, **kwargs):
        item = dynamo.get((Key["PK"], Key["SK"]), {**Key})
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})
        # Honor the two status ConditionExpressions dynamo_store emits so the
        # conditional writes behave like real DDB:
        #   ``#s = :expected_status``  — the accept race guard
        #   ``attribute_not_exists(#s) OR #s <> :forbidden_status``
        #                              — _accept_fail's "never downgrade an
        #                                already-accepted proposal" guard
        # Any other shape is a new call site that must be modelled here.
        cond = kwargs.get("ConditionExpression")
        if cond:
            current = item.get("status")
            if cond == "#s = :expected_status":
                unmet = current != values.get(":expected_status")
            elif cond == "attribute_not_exists(#s) OR #s <> :forbidden_status":
                unmet = current is not None and current == values.get(":forbidden_status")
            else:
                # Message hoisted into a local rather than inlined on the assert:
                # ruff 0.8 (the pinned pre-commit hook) and ruff 0.15 (the
                # project / CI formatter via `nx run ontology-engine:lint`) wrap
                # an assert-with-inline-message differently, so an inline message
                # here cannot satisfy both. A plain single-expression assert
                # formats identically under both.
                cond_msg = f"fake_update_item does not model this ConditionExpression: {cond}"
                raise AssertionError(cond_msg)
            if unmet:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "The conditional request failed",
                        }
                    },
                    "UpdateItem",
                )
        for part in UpdateExpression.replace("SET ", "").split(", "):
            field, val_ref = [s.strip() for s in part.split("=")]
            for alias, real in (names or {}).items():
                field = field.replace(alias, real)
            item[field] = values.get(val_ref, val_ref)
        dynamo[(Key["PK"], Key["SK"])] = item

    mock_table.update_item = MagicMock(side_effect=fake_update_item)

    # In-memory S3 stand-in: proposal Turtle/matches are offloaded to S3 and
    # only the key is stored on the DynamoDB item (avoids the 400 KB limit).
    s3_objects: dict[str, bytes] = {}

    class _NoSuchKey(Exception):
        pass

    def fake_put_object(Bucket, Key, Body, **kwargs):
        s3_objects[Key] = Body if isinstance(Body, bytes) else str(Body).encode("utf-8")
        return {}

    def fake_get_object(Bucket, Key, **kwargs):
        if Key not in s3_objects:
            raise _NoSuchKey()
        return {"Body": MagicMock(read=lambda: s3_objects[Key])}

    mock_s3 = MagicMock()
    mock_s3.put_object = MagicMock(side_effect=fake_put_object)
    mock_s3.get_object = MagicMock(side_effect=fake_get_object)
    mock_s3.copy_object = MagicMock(return_value={})
    mock_s3.exceptions.NoSuchKey = _NoSuchKey

    # Mock GraphStore — record every call
    mock_graph = MagicMock()
    mock_graph.get_ontology_by_uri = MagicMock(return_value=None)
    mock_graph.create_ontology = MagicMock(return_value={"uri": "https://example.com/onto"})
    mock_graph.store_class = MagicMock(return_value={})
    mock_graph.store_property = MagicMock(return_value={})
    mock_graph.update_ontology = MagicMock(return_value={})
    mock_graph.load_turtle = MagicMock(
        return_value={
            "status": "ok",
            "graph_uri": "https://example.com/onto",
            "bytes_loaded": len(TURTLE.encode("utf-8")),
        }
    )

    mock_vec = MagicMock()
    # Model a consistent vector index: whatever store_embeddings_batch writes
    # is immediately searchable. The accept path waits for every written
    # embedding to become searchable via list_searchable_entity_uris, so the
    # mock must report the inserted URIs back or the wait spins until timeout.
    _indexed: list[dict] = []

    def _store_batch(items):
        _indexed.extend(items)
        return items

    mock_vec.store_embeddings_batch = MagicMock(side_effect=_store_batch)
    mock_vec.list_searchable_entity_uris = MagicMock(
        side_effect=lambda ontology_id, namespace=None, **kw: [
            row["entity_uri"] for row in _indexed if row.get("ontology_id") == ontology_id and row.get("entity_uri")
        ]
    )

    # Mock Bedrock so we don't call AWS during unit tests.
    mock_bedrock = MagicMock()
    mock_bedrock.model_id = "fake-titan-v2"
    mock_bedrock.embed_texts = MagicMock(side_effect=lambda texts: [[float(len(t))] * 4 for t in texts])

    with (
        patch("coa_ontology.dynamo_store._get_table", return_value=mock_table),
        patch("coa_ontology.dynamo_store._get_s3", return_value=mock_s3),
        patch("coa_ontology.stores.build_stores", return_value=(mock_graph, mock_vec)),
        patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient", return_value=mock_bedrock),
        patch("coa_ontology.proposals.Thread", _InlineThread),
        # S3 persist reaches boto3 and Neptune; stubbed here, covered in test_s3_persistence.py.
        patch("coa_ontology.proposals._persist_induced_to_s3", return_value=None),
        patch("coa_ontology.proposals._reconcile_counts", return_value=None),
        # notify_vkg calls EventBridge put_events; stub it to keep the test hermetic.
        patch("coa_ontology.proposals._emit_ontology_published", return_value=True),
    ):
        from coa_ontology.main import app

        yield TestClient(app), dynamo, mock_graph, mock_vec, mock_bedrock, s3_objects


def _final_status(dynamo) -> dict:
    """Return the proposal META row after the inline worker has run."""
    return dynamo[("default#PROPOSAL#p-1", "META")]


class TestProposalTurtleIngest:
    def test_accept_loads_turtle_into_graph_store(self, accept_client):
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client

        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        # 202 carries the in-flight signal; terminal state is on the proposal.
        assert body == {"proposal_id": "p-1", "status": "accepting"}

        # Inline-thread fixture means the worker has finished by now.
        final = _final_status(dynamo)
        assert final["status"] == "accepted"

        # Catalog projection ran — classes/properties via the batched path
        # (one store_classes_batch / store_properties_batch carrying all items).
        assert mock_graph.create_ontology.call_count == 1
        assert mock_graph.store_classes_batch.call_count == 1
        assert len(mock_graph.store_classes_batch.call_args.kwargs["classes"]) == 2  # Patient, Doctor
        assert mock_graph.store_properties_batch.call_count == 1
        assert len(mock_graph.store_properties_batch.call_args.kwargs["properties"]) == 2  # treats, age
        # update_ontology called twice: once for class/property counts, once
        # to set file_path after the turtle is cached to local disk.
        assert mock_graph.update_ontology.call_count == 2

        # Counts propagated into the first update_ontology call
        all_update_calls = mock_graph.update_ontology.call_args_list
        counts_update = all_update_calls[0].args[1]
        assert counts_update["class_count"] == 2
        assert counts_update["property_count"] == 2
        assert counts_update["parse_status"] == "ok"

        # Second call set file_path (local cache for /download)
        file_update = all_update_calls[1].args[1]
        assert "file_path" in file_update
        assert file_update["file_path"].endswith(".ttl")

        mock_graph.load_turtle.assert_called_once()
        call = mock_graph.load_turtle.call_args
        assert call.kwargs["ontology_uri"] == "https://example.com/onto"
        assert "owl:Ontology" in call.kwargs["turtle"]
        # We re-serialise from the parsed rdflib graph before posting
        # (so we can strip dangling-bnode triples), so the bytes are not
        # byte-for-byte identical to the input. Check semantic equivalence
        # by re-parsing both and comparing triple sets.
        from rdflib import Graph as _G

        g_in, g_out = _G(), _G()
        g_in.parse(data=TURTLE, format="turtle")
        g_out.parse(data=call.kwargs["turtle"], format="turtle")
        assert set(g_in) == set(g_out)

        mock_bedrock.embed_texts.assert_called_once()
        texts = mock_bedrock.embed_texts.call_args.args[0]
        assert len(texts) == 4  # 2 classes + 2 properties
        joined = " ".join(texts).lower()
        assert "patient" in joined and "doctor" in joined
        assert "treats" in joined and "age" in joined

        mock_vec.store_embeddings_batch.assert_called_once()
        items = mock_vec.store_embeddings_batch.call_args.args[0]
        assert len(items) == 4
        kinds = sorted((it["entity_type"], it["entity_uri"].rsplit("#", 1)[-1]) for it in items)
        assert kinds == [
            ("class", "Doctor"),
            ("class", "Patient"),
            ("property", "age"),
            ("property", "treats"),
        ]
        for it in items:
            assert it["ontology_id"] == "https://example.com/onto"
            assert it["embedding_type"] == "lexical"
            assert it["model_id"] == "fake-titan-v2"
            assert isinstance(it["vector"], list) and it["vector"]

    def test_accept_sets_embeddings_sync_before_waiting(self, accept_client):
        """The proposal moves to ``embeddings_sync`` before the readiness wait
        begins, then to ``accepted`` once the wait returns."""
        http, dynamo, _mock_graph, _mock_vec, _mock_bedrock, _s3 = accept_client

        # Capture the proposal's stored status at the instant the wait is
        # entered — it must already be ``embeddings_sync``.
        seen_status: dict[str, str] = {}

        def _spy_wait(*, vector_store, ontology_id, namespace, expected_uris, **kw):
            seen_status["at_wait"] = dynamo[("default#PROPOSAL#p-1", "META")]["status"]
            return True

        with patch(
            "coa_ontology.proposals.wait_for_embeddings_searchable",
            side_effect=_spy_wait,
        ) as wait_mock:
            resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202, resp.text

        wait_mock.assert_called_once()
        # State was flipped to embeddings_sync BEFORE the wait ran …
        assert seen_status["at_wait"] == "embeddings_sync"
        # … and to accepted once the worker finished.
        assert _final_status(dynamo)["status"] == "accepted"

    def test_accept_in_embeddings_sync_returns_409(self, accept_client):
        """A re-accept while the proposal is mid embeddings-sync is rejected as
        already-in-progress, not allowed to spawn a second worker."""
        http, dynamo, mock_graph, _mock_vec, _mock_bedrock, _s3 = accept_client

        # Pin the proposal in the embeddings_sync phase (as if a worker is
        # currently waiting) and attempt another accept.
        dynamo[("default#PROPOSAL#p-1", "META")]["status"] = "embeddings_sync"
        resp = http.post("/ontology/proposals/p-1/accept")

        assert resp.status_code == 409
        # No ingest kicked off by the rejected re-accept.
        mock_graph.load_turtle.assert_not_called()

    def test_accept_is_idempotent(self, accept_client):
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client

        r1 = http.post("/ontology/proposals/p-1/accept")
        assert r1.status_code == 202
        assert r1.json()["status"] == "accepting"
        # Worker ran inline → proposal is now accepted in DDB.
        assert _final_status(dynamo)["status"] == "accepted"

        # Re-POST short-circuits: returns 202 with status=accepted, does not
        # re-run ingest.
        r2 = http.post("/ontology/proposals/p-1/accept")
        assert r2.status_code == 202
        assert r2.json() == {"proposal_id": "p-1", "status": "accepted"}
        assert mock_graph.load_turtle.call_count == 1

    def test_accept_race_returns_409_when_status_changed_concurrently(self, accept_client):
        """Concurrent accepts can't both spawn workers.

        A ConditionalCheckFailedException on the ``accepting`` write returns 409.
        """
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client

        from coa_ontology import dynamo_store

        table = dynamo_store._get_table()
        original_update = table.update_item.side_effect

        update_call_count = {"n": 0}

        def update_item_simulating_race(Key, UpdateExpression, **kwargs):
            update_call_count["n"] += 1
            # The first update_item call from accept_proposal is the
            # status flip — pretend a concurrent writer beat us to it.
            if update_call_count["n"] == 1 and kwargs.get("ConditionExpression"):
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ConditionalCheckFailedException",
                            "Message": "raced",
                        }
                    },
                    "UpdateItem",
                )
            return original_update(Key, UpdateExpression, **kwargs)

        with patch.object(table, "update_item", side_effect=update_item_simulating_race):
            resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 409
        assert "concurrently" in resp.json()["detail"].lower()

    def test_accept_rejects_malformed_turtle(self, accept_client):
        """A parse failure is STRUCTURAL — the proposal itself is bad — so the
        worker fails it immediately (no retry) to the terminal ``accept_failed``
        state with ``accept_error`` set.

        Parse failure happens inside the background worker, so the route still
        returns 202; the user observes the failure on the next
        ``GET /proposals/p-1`` poll and can fix the Turtle and re-accept.
        """
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["ontology_turtle"] = "this is not turtle {{{"

        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepting"

        final = _final_status(dynamo)
        assert final["status"] == "accept_failed"
        assert "Invalid ontology Turtle" in final["accept_error"]
        # The bulk-load step never ran because parse failed first.
        mock_graph.load_turtle.assert_not_called()

    def test_accept_surfaces_load_turtle_failures(self, accept_client):
        """A GSP load failure is TRANSIENT — retried, and on exhaustion the
        proposal lands in ``accept_failed`` naming the ``ingest`` step."""
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client
        mock_graph.load_turtle.side_effect = RuntimeError("GSP 503")

        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202

        # Retries exhausted → terminal accept_failed; error visible on the proposal.
        final = _final_status(dynamo)
        assert final["status"] == "accept_failed"
        assert "GSP 503" in final["accept_error"]
        # Retried the configured number of attempts before giving up.
        assert mock_graph.load_turtle.call_count == proposals._ACCEPT_STEP_MAX_ATTEMPTS

        # Catalog writes happened (step 1) even though the bulk load failed.
        assert mock_graph.create_ontology.called
        assert mock_graph.store_classes_batch.called
        # Embeddings were NOT attempted because GSP failed first.
        mock_vec.store_embeddings_batch.assert_not_called()

    def test_accept_raises_on_embedding_failures(self, accept_client):
        """A persistent embedding failure (Bedrock throttled) is transient →
        retried → terminal ``accept_failed``. Prevents silent data loss (a graph
        accepted without searchable embeddings)."""
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client
        mock_bedrock.embed_texts.side_effect = RuntimeError("Bedrock throttled")

        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202
        final = _final_status(dynamo)
        assert final["status"] == "accept_failed"
        mock_vec.store_embeddings_batch.assert_not_called()


_R2RML_PATIENT = """@prefix rr:  <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex:  <https://example.com/onto#> .

<https://example.com/r2rml#TM_Patient> a rr:TriplesMap ;
    coa:datasourceId "ds-clinical-1" ;
    rr:subjectMap [ rr:class ex:Patient ] .
"""

_R2RML_DOCTOR = """@prefix rr:  <http://www.w3.org/ns/r2rml#> .
@prefix coa: <http://coa.amazon.com/vocab/coa#> .
@prefix ex:  <https://example.com/onto#> .

<https://example.com/r2rml#TM_Doctor> a rr:TriplesMap ;
    coa:datasourceId "ds-clinical-2" ;
    rr:subjectMap [ rr:class ex:Doctor ] .
"""


class TestAcceptProposalR2rmlWiring:
    """The proposal's R2RML must thread from the DDB/reviewed item all the way
    into ingest so the Tier-2 coa:isMapped marker is derived. This is the glue
    that makes the answerability feature real; the lower-layer ingest tests call
    ingest_ontology directly with R2RML, so this covers the accept→ingest wiring
    and the reviewed-over-item precedence specifically.
    """

    def _is_mapped_by_class(self, mock_graph) -> dict[str, bool]:
        # Ingest projects classes via the batched store_classes_batch(classes=[
        # {class_uri, is_mapped, ...}]) path; read the is_mapped tag from the
        # batch specs.
        out: dict[str, bool] = {}
        for call in mock_graph.store_classes_batch.call_args_list:
            for c in call.kwargs["classes"]:
                out[c["class_uri"]] = c.get("is_mapped")
        return out

    def test_item_r2rml_threads_into_ingest_and_tags_mapped(self, accept_client):
        http, dynamo, mock_graph, _mock_vec, _mock_bedrock, _s3 = accept_client
        # R2RML on the proposal item maps ONLY Patient.
        dynamo[("default#PROPOSAL#p-1", "META")]["r2rml_turtle"] = _R2RML_PATIENT

        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202, resp.text
        assert _final_status(dynamo)["status"] == "accepted"

        by_class = self._is_mapped_by_class(mock_graph)
        # Patient is the rr:class of a TriplesMap → mapped; Doctor is not.
        assert by_class["https://example.com/onto#Patient"] is True
        assert by_class["https://example.com/onto#Doctor"] is False

    def test_no_r2rml_on_item_tags_all_unmapped(self, accept_client):
        http, dynamo, mock_graph, _mock_vec, _mock_bedrock, _s3 = accept_client
        # No r2rml_turtle key at all (unstructured proposal shape).
        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202
        assert _final_status(dynamo)["status"] == "accepted"

        by_class = self._is_mapped_by_class(mock_graph)
        assert by_class["https://example.com/onto#Patient"] is False
        assert by_class["https://example.com/onto#Doctor"] is False

    def test_reviewed_r2rml_takes_precedence_over_item(self, accept_client):
        http, dynamo, mock_graph, _mock_vec, _mock_bedrock, _s3 = accept_client
        # Item maps Patient; the steward-REVIEWED copy instead maps Doctor.
        # accept reads the reviewed copy via dynamo_store.get_reviewed_proposal
        # and must prefer it → Doctor mapped, Patient not.
        dynamo[("default#PROPOSAL#p-1", "META")]["r2rml_turtle"] = _R2RML_PATIENT

        with patch(
            "coa_ontology.dynamo_store.get_reviewed_proposal",
            return_value={"r2rml_turtle": _R2RML_DOCTOR},
        ):
            resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202, resp.text
        assert _final_status(dynamo)["status"] == "accepted"

        by_class = self._is_mapped_by_class(mock_graph)
        assert by_class["https://example.com/onto#Doctor"] is True
        assert by_class["https://example.com/onto#Patient"] is False


class TestProposalCancel:
    def test_cancel_pending_proposal(self, accept_client):
        http, dynamo, *_ = accept_client
        resp = http.post("/ontology/proposals/p-1/cancel")
        assert resp.status_code == 200
        assert resp.json() == {"proposal_id": "p-1", "status": "cancelled"}
        assert _final_status(dynamo)["status"] == "cancelled"

    def test_cancel_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.post("/ontology/proposals/nonexistent/cancel")
        assert resp.status_code == 404

    def test_cancel_accepted_fails(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["status"] = "accepted"
        resp = http.post("/ontology/proposals/p-1/cancel")
        assert resp.status_code == 409


class TestProposalReject:
    def test_reject_pending_proposal(self, accept_client):
        http, dynamo, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/reject",
            json={"proposal_ids": ["p-1"], "reason": "Not relevant"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["status"] == "rejected"
        assert _final_status(dynamo)["status"] == "rejected"

    def test_reject_already_accepted(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["status"] = "accepted"
        resp = http.post(
            "/ontology/proposals/reject",
            json={"proposal_ids": ["p-1"], "reason": "Too late"},
        )
        assert resp.status_code == 200
        assert resp.json()[0]["error"] == "already accepted"

    def test_reject_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/reject",
            json={"proposal_ids": ["ghost"], "reason": "Gone"},
        )
        assert resp.status_code == 200
        assert resp.json()[0]["error"] == "not found"


class TestProposalUpdate:
    def test_update_ontology_turtle(self, accept_client):
        http, dynamo, mock_graph, mock_vec, mock_bedrock, s3_objects = accept_client
        new_turtle = TURTLE.replace("Test Ontology", "Updated Ontology")
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1", "ontology_turtle": new_turtle},
        )
        assert resp.status_code == 200
        final = _final_status(dynamo)
        assert final["status"] == "updated"
        # Turtle is offloaded to S3 (the DDB item carries only the key, not the
        # inline body — guarding the 400 KB item-size limit).
        assert "ontology_s3_key" in final
        s3_key = final["ontology_s3_key"]
        assert "Updated Ontology" in s3_objects[s3_key].decode("utf-8")

    def test_update_invalid_turtle_422(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1", "ontology_turtle": "not valid {{{"},
        )
        assert resp.status_code == 422

    def test_update_accepted_proposal_409(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["status"] = "accepted"
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1", "ontology_turtle": TURTLE},
        )
        assert resp.status_code == 409

    def test_update_not_found_404(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "ghost", "ontology_turtle": TURTLE},
        )
        assert resp.status_code == 404

    def test_update_empty_body_400(self, accept_client):
        http, *_ = accept_client
        resp = http.post(
            "/ontology/proposals/update",
            json={"proposal_id": "p-1"},
        )
        assert resp.status_code == 400


class TestProposalValidate:
    def test_validate_starts_job(self, accept_client):
        http, dynamo, *_ = accept_client
        resp = http.post("/ontology/proposals/p-1/validate")
        assert resp.status_code == 200
        body = resp.json()
        assert body["proposal_id"] == "p-1"
        assert "job_id" in body

    def test_validate_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.post("/ontology/proposals/ghost/validate")
        assert resp.status_code == 404

    def test_validate_no_turtle_409(self, accept_client):
        http, dynamo, *_ = accept_client
        dynamo[("default#PROPOSAL#p-1", "META")]["ontology_turtle"] = ""
        resp = http.post("/ontology/proposals/p-1/validate")
        assert resp.status_code == 409

    def test_get_validation_job(self, accept_client):
        http, *_ = accept_client
        # Start a validation job first
        r = http.post("/ontology/proposals/p-1/validate")
        job_id = r.json()["job_id"]
        # Poll it
        resp = http.get(f"/ontology/proposals/p-1/validate/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["job_id"] == job_id

    def test_get_validation_job_not_found(self, accept_client):
        http, *_ = accept_client
        resp = http.get("/ontology/proposals/p-1/validate/jobs/fake-id")
        assert resp.status_code == 404


class TestStatusSetsComeFromSmithyEnum:
    """The stored statuses are the Smithy-generated ``ProposalStatus`` values.

    Guards against drift between the API contract, the values the worker writes,
    and the literals ``dynamo_store`` needs for its sweep (which cannot import
    proposals.py — circular — so it imports the same generated enum).
    """

    def test_accept_status_constants_match_the_generated_enum(self):
        from coa_control_plane_server.models.proposal_status import ProposalStatus

        assert ProposalStatus.EMBEDDINGS_SYNC.value == proposals.PROPOSAL_STATUS_EMBEDDINGS_SYNC
        assert ProposalStatus.ACCEPT_FAILED.value == proposals.PROPOSAL_STATUS_ACCEPT_FAILED

    def test_dynamo_store_sweep_set_matches_the_accept_guard(self):
        from coa_ontology import dynamo_store

        assert frozenset(proposals._ACCEPT_IN_PROGRESS_STATUSES) == dynamo_store._PROPOSAL_IN_PROGRESS_STATUSES
        assert dynamo_store._PROPOSAL_ACCEPT_FAILED_STATUS == proposals.PROPOSAL_STATUS_ACCEPT_FAILED


class TestInductionBlockingStatuses:
    """A new induction is blocked by unreviewed work AND by an in-flight accept.

    ``acquire_induction_lock`` is held only for the duration of an INDUCTION, so
    without the accept-in-progress states a user could trigger a new induction
    while a worker is mid-merge and leave the namespace holding two competing
    structured proposals.
    """

    def test_blocks_on_unreviewed_work(self):
        for status in ("pending", "updated", "accept_failed"):
            assert status in proposals.PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

    def test_blocks_while_an_accept_is_merging(self):
        for status in ("accepting", "embeddings_sync"):
            assert status in proposals.PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION

    def test_does_not_block_on_resolved_states(self):
        for status in ("accepted", "rejected", "cancelled", "failed"):
            assert status not in proposals.PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION


class TestJobStatusMirrorIsAPipelineStep:
    """The job-status mirror is a retried, non-fatal accept step."""

    def test_mirror_failure_is_retried_then_skipped_and_accept_still_succeeds(self, accept_client):
        http, dynamo, *_ = accept_client
        calls = {"n": 0}

        def flaky_mirror(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("DDB throttled")

        with patch.object(proposals.dynamo_store, "update_job_status", side_effect=flaky_mirror):
            resp = http.post("/ontology/proposals/p-1/accept")

        assert resp.status_code == 202
        # Non-fatal: the accept completes even though the mirror never succeeded.
        assert _final_status(dynamo)["status"] == "accepted"
        # ...but it was retried like a real step, not attempted once.
        assert calls["n"] == proposals._ACCEPT_STEP_MAX_ATTEMPTS

    def test_mirror_is_invoked_with_the_ingested_status(self, accept_client):
        http, *_ = accept_client
        with patch.object(proposals.dynamo_store, "update_job_status") as mock_mirror:
            http.post("/ontology/proposals/p-1/accept")
        assert any(c.args[1] == "ingested" for c in mock_mirror.call_args_list)


class TestRunAcceptStep:
    """Unit tests for the per-step retry/checkpoint primitive.

    ``_run_accept_step`` is the core of the resilient accept pipeline: it runs
    one step, checkpoints progress (heartbeat), retries transient failures with
    backoff, fails structural errors immediately, and raises ``_AcceptStepError``
    naming the step once retries are exhausted.
    """

    def test_success_checkpoints_progress_and_returns_result(self):
        with patch.object(proposals, "dynamo_store") as mock_ds:
            out = proposals._run_accept_step("ingest", "p-1", "ns", lambda: {"ok": 1})
        assert out == {"ok": 1}
        # Checkpoint written for this step (heartbeat: update_proposal bumps updated_at).
        mock_ds.update_proposal.assert_called_once()
        kwargs = mock_ds.update_proposal.call_args.kwargs
        assert kwargs["accept_progress"] == {"step": "ingest", "state": "done"}

    def test_false_return_is_checkpointed_skipped_not_done(self):
        """A step that reports 'did not complete' without raising (e.g.
        wait_for_embeddings_searchable on a stalled index) must checkpoint
        'skipped' — recording 'done' for a step that did not run is a checkpoint
        that lies."""
        with patch.object(proposals, "dynamo_store") as mock_ds:
            out = proposals._run_accept_step("embeddings_searchable", "p-1", "ns", lambda: False)
        assert out is False
        kwargs = mock_ds.update_proposal.call_args.kwargs
        assert kwargs["accept_progress"] == {"step": "embeddings_searchable", "state": "skipped"}

    def test_true_and_none_returns_are_checkpointed_done(self):
        """Only an explicit False means skipped; True/None are normal success."""
        for retval in (True, None, {"ok": 1}):
            with patch.object(proposals, "dynamo_store") as mock_ds:
                proposals._run_accept_step("notify_vkg", "p-1", "ns", lambda v=retval: v)
            assert mock_ds.update_proposal.call_args.kwargs["accept_progress"]["state"] == "done"

    def test_transient_error_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("AOSS 503")
            return "ok"

        with patch.object(proposals, "dynamo_store"):
            out = proposals._run_accept_step("s3_persist", "p-1", "ns", flaky)
        assert out == "ok"
        assert calls["n"] == 2  # failed once, succeeded on retry

    def test_transient_error_exhausts_retries_and_raises_named_step(self):
        def always_fail():
            raise RuntimeError("Neptune down")

        with (
            patch.object(proposals, "dynamo_store") as mock_ds,
            patch.object(proposals, "_ACCEPT_STEP_MAX_ATTEMPTS", 3),
            pytest.raises(proposals._AcceptStepError) as ei,
        ):
            proposals._run_accept_step("s3_persist", "p-1", "ns", always_fail)
        assert ei.value.step == "s3_persist"
        assert "Neptune down" in str(ei.value)
        # No checkpoint written when the step never succeeded.
        mock_ds.update_proposal.assert_not_called()

    def test_non_fatal_step_is_retried_then_skipped_not_raised(self):
        """``fatal=False`` buys the SAME retry as a fatal step; only the outcome
        of exhaustion differs (skipped + accept continues, vs _AcceptStepError).
        """
        calls = {"n": 0}

        def always_fail():
            calls["n"] += 1
            raise RuntimeError("eventbridge down")

        with (
            patch.object(proposals, "dynamo_store") as mock_ds,
            patch.object(proposals, "_ACCEPT_STEP_MAX_ATTEMPTS", 3),
        ):
            out = proposals._run_accept_step("notify_vkg", "p-1", "ns", always_fail, fatal=False)

        assert out is False  # swallowed — the accept goes on
        assert calls["n"] == 3  # ...but only after a real retry
        kwargs = mock_ds.update_proposal.call_args.kwargs
        assert kwargs["accept_progress"] == {"step": "notify_vkg", "state": "skipped"}

    def test_non_fatal_step_that_recovers_on_retry_is_done_not_skipped(self):
        """A non-fatal step that succeeds on attempt 2 is a normal success."""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("transient")
            return True

        with patch.object(proposals, "dynamo_store") as mock_ds:
            out = proposals._run_accept_step("reconcile_counts", "p-1", "ns", flaky, fatal=False)

        assert out is True
        assert calls["n"] == 2
        assert mock_ds.update_proposal.call_args.kwargs["accept_progress"]["state"] == "done"

    def test_structural_error_is_not_swallowed_by_non_fatal(self):
        """``fatal=False`` governs EXHAUSTED RETRIES, not error classification: a
        structural error still propagates immediately so a bad proposal can never
        be silently skipped past."""
        from coa_ontology.catalog.ingest import IngestParseError

        with (
            patch.object(proposals, "dynamo_store"),
            pytest.raises(IngestParseError),
        ):
            proposals._run_accept_step(
                "reconcile_counts", "p-1", "ns", lambda: (_ for _ in ()).throw(IngestParseError("nope")), fatal=False
            )

    def test_structural_error_is_not_retried(self):
        from coa_ontology.catalog.ingest import IngestParseError

        calls = {"n": 0}

        def bad_turtle():
            calls["n"] += 1
            raise IngestParseError("nope")

        with patch.object(proposals, "dynamo_store"), pytest.raises(IngestParseError):
            proposals._run_accept_step("ingest", "p-1", "ns", bad_turtle)
        assert calls["n"] == 1  # immediate — structural errors never retry

    def test_progress_write_failure_does_not_fail_step(self):
        with patch.object(proposals, "dynamo_store") as mock_ds:
            mock_ds.update_proposal.side_effect = RuntimeError("DDB hiccup")
            # Step result still returned even though the checkpoint write failed.
            out = proposals._run_accept_step("ingest", "p-1", "ns", lambda: "done")
        assert out == "done"


class TestAcceptRetryAndRecovery:
    """End-to-end: the accept worker retries transient step
    failures, records heartbeat progress, lands on the terminal ``accept_failed``
    state when retries are exhausted, and a failed proposal is re-acceptable and
    converges on re-run."""

    def test_transient_ingest_failure_recovers_on_retry(self, accept_client):
        """A single transient GSP blip is retried and the accept still succeeds."""
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client
        # Fail once, then return the fixture's realistic load result (right
        # graph_uri) so downstream reconcile/persist mocks behave as usual.
        ok = mock_graph.load_turtle.return_value
        mock_graph.load_turtle.side_effect = [RuntimeError("GSP 503 (transient)"), ok]

        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202
        final = _final_status(dynamo)
        assert final["status"] == "accepted"
        assert mock_graph.load_turtle.call_count == 2  # failed once, retried, succeeded

    def test_accept_failed_records_step_in_progress(self, accept_client):
        """On exhausted retries the proposal is ``accept_failed`` and
        ``accept_progress`` names the failing step for diagnosis."""
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client
        mock_graph.load_turtle.side_effect = RuntimeError("GSP down")

        http.post("/ontology/proposals/p-1/accept")
        final = _final_status(dynamo)
        assert final["status"] == "accept_failed"
        assert final["accept_progress"]["state"] == "failed"
        assert final["accept_progress"]["step"] == "ingest"

    def test_successful_accept_reaches_accepted_and_clears_error(self, accept_client):
        """A clean accept ends with status == accepted and no error."""
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client
        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202
        final = _final_status(dynamo)
        assert final["status"] == "accepted"
        assert final.get("accept_error", "") == ""
        # Progress advanced through the pipeline; last checkpoint is a real step
        # in the done state (not a failure).
        assert final.get("accept_progress", {}).get("state") == "done"

    def test_accept_failed_proposal_is_reacceptable_and_converges(self, accept_client):
        """A proposal left in ``accept_failed`` (dead worker / prior failure) can
        be accepted again; once the transient cause clears, re-accept converges to
        ``accepted``."""
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client

        # First attempt: permanent-for-now failure → accept_failed.
        mock_graph.load_turtle.side_effect = RuntimeError("GSP down")
        http.post("/ontology/proposals/p-1/accept")
        assert _final_status(dynamo)["status"] == "accept_failed"

        # Cause clears; re-accept from accept_failed must be allowed (not 409)
        # and succeed. Clearing side_effect restores the fixture's realistic
        # return_value (correct graph_uri) for downstream reconcile/persist.
        mock_graph.load_turtle.side_effect = None
        resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202, resp.text  # accept_failed is re-acceptable
        assert _final_status(dynamo)["status"] == "accepted"


class TestS3PersistStepFailure:
    """S3 persist failures are retried and recorded on the proposal."""

    def test_persist_failure_is_recorded_but_does_not_downgrade_accepted(self, accept_client):
        """``s3_persist`` runs after ``flip_accepted``, so its failure must not downgrade
        the status: other call sites select on ``status="accepted"`` to rebuild R2RML.
        The failed step is still recorded.
        """
        http, dynamo, mock_graph, mock_vec, mock_bedrock, _s3 = accept_client
        # Override the fixture's no-op persist stub with a hard failure.
        with patch(
            "coa_ontology.proposals._persist_induced_to_s3",
            side_effect=RuntimeError("S3 PutObject 500"),
        ):
            resp = http.post("/ontology/proposals/p-1/accept")
        assert resp.status_code == 202
        final = _final_status(dynamo)
        assert final["status"] == "accepted"  # NOT downgraded — the ontology is live
        assert final["accept_progress"] == {"step": "s3_persist", "state": "failed"}
        assert "S3 PutObject 500" in final["accept_error"]

    def test_persist_module_function_raises_not_swallows(self):
        """The primitive itself must propagate: a mocked S3 failure raises
        rather than being logged-and-swallowed."""
        with (
            patch("boto3.client") as mock_client,
            patch("coa_ontology.stores.neptune_db_graph._gsp_get_turtle", return_value=":X a owl:Class ."),
            patch("coa_ontology.proposals.dynamo_store") as mock_ds,
        ):
            mock_ds.list_proposals.return_value = []
            mock_s3 = MagicMock()
            mock_s3.put_object.side_effect = RuntimeError("S3 boom")
            mock_client.return_value = mock_s3
            with pytest.raises(Exception, match="S3 boom"):
                proposals._persist_induced_to_s3("http://x.org/o#", "ns", "https://graph/uri")


class TestNaReadTimeoutBounded:
    """The NA execute_query client must carry a bounded read_timeout
    so a hung Neptune Analytics call can't block the accept worker forever.

    Asserts on the CONSTRUCTED boto client config, not just the module constant —
    a test that only checks the constant would still pass if the ``read_timeout=``
    kwarg were dropped from the client, which is the actual regression to catch.
    """

    def _build_client_config(self, monkeypatch, env_value=None):
        """Reload na_store (so the env-derived constant is re-read), build the
        client with boto3 patched, and return the botocore Config it was given."""
        import importlib

        from coa_ontology.catalog import na_store

        if env_value is None:
            monkeypatch.delenv("NA_READ_TIMEOUT", raising=False)
        else:
            monkeypatch.setenv("NA_READ_TIMEOUT", env_value)
        importlib.reload(na_store)
        na_store._client = None  # force a rebuild (module caches the client)
        with patch.object(na_store.boto3, "client") as mock_boto:
            na_store._get_client()
        assert mock_boto.called, "expected na_store to construct a boto3 client"
        return na_store, mock_boto.call_args.kwargs["config"]

    def test_default_read_timeout_is_bounded_on_the_boto_config(self, monkeypatch):
        na_store, cfg = self._build_client_config(monkeypatch)
        assert cfg.read_timeout is not None
        assert cfg.read_timeout > 0
        assert cfg.read_timeout == na_store.NA_READ_TIMEOUT

    def test_read_timeout_is_env_tunable(self, monkeypatch):
        _na_store, cfg = self._build_client_config(monkeypatch, env_value="45")
        assert cfg.read_timeout == 45

    def test_zero_restores_unbounded_escape_hatch(self, monkeypatch):
        """NA_READ_TIMEOUT=0 is the documented escape hatch back to the old
        unbounded behavior (read_timeout=None), for the rare case a legitimately
        very long NA query must not be cut off."""
        _na_store, cfg = self._build_client_config(monkeypatch, env_value="0")
        assert cfg.read_timeout is None

    def test_sdk_retries_stay_disabled(self, monkeypatch):
        """Bounding the timeout must not re-enable SDK retries — NA rejects
        retried execute_query requests with UnprocessableException, so retries
        are handled at our own layer."""
        _na_store, cfg = self._build_client_config(monkeypatch)
        assert cfg.retries["total_max_attempts"] == 1
