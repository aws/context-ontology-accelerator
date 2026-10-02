# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared ``ingest_ontology`` helper, the namespace registry
helpers in ``dynamo_store``, and the new hierarchical graph-URI scheme.

All stores and Bedrock are stubbed — no AWS traffic.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_accept_backoff():
    """Zero the accept-step retry backoff: no unit test should sleep on it.

    Autouse so a future retry-path test in this module cannot silently add
    seconds to the suite by forgetting to patch it.
    """
    with patch("coa_ontology.proposals._ACCEPT_STEP_BACKOFF_SECONDS", 0):
        yield


TURTLE = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <https://example.com/onto#> .

<https://example.com/onto> a owl:Ontology ;
    rdfs:label "Test Ontology" ;
    rdfs:comment "An ontology for testing imports" .

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


# ── URI scheme tests ────────────────────────────────────────────────────


class TestGraphURIScheme:
    def test_ontology_uri_default_namespace(self, monkeypatch):
        monkeypatch.delenv("NDB_GRAPH_URI", raising=False)
        monkeypatch.setenv("NDB_GRAPH_URI_BASE", "https://wb.example")
        from coa_ontology.stores import neptune_db_graph as m

        assert m._ontology_graph_uri("https://x", "default") == ("https://wb.example/default/https%3A%2F%2Fx")

    def test_ontology_uri_percent_encoded(self, monkeypatch):
        monkeypatch.setenv("NDB_GRAPH_URI_BASE", "https://wb.example")
        from coa_ontology.stores import neptune_db_graph as m

        out = m._ontology_graph_uri("https://spec.edmcouncil.org/fibo/ontology/FND", "acme")
        assert out == ("https://wb.example/acme/https%3A%2F%2Fspec.edmcouncil.org%2Ffibo%2Fontology%2FFND")

    def test_legacy_NDB_GRAPH_URI_still_works(self, monkeypatch):
        """If only NDB_GRAPH_URI is set, derive the base from it."""
        monkeypatch.delenv("NDB_GRAPH_URI_BASE", raising=False)
        monkeypatch.setenv("NDB_GRAPH_URI", "https://legacy.example/graph/catalog")
        from coa_ontology.stores import neptune_db_graph as m

        assert m._ontology_graph_uri("https://x", "acme") == ("https://legacy.example/acme/https%3A%2F%2Fx")

    def test_base_defaults_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("NDB_GRAPH_URI", raising=False)
        monkeypatch.delenv("NDB_GRAPH_URI_BASE", raising=False)
        from coa_ontology.stores import neptune_db_graph as m

        assert m._ontology_graph_uri("https://x", "default").startswith("https://ontology-workbench.local/default/")


# ── DynamoDB registry tests ─────────────────────────────────────────────


class _FakeDynamoTable:
    """In-memory stand-in that mimics the boto3 Dynamo Table interface.

    Supports put_item, get_item, update_item (SET), delete_item, query, scan.
    Enough to drive dynamo_store's registry helpers end-to-end.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def put_item(self, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def delete_item(self, Key):
        self.items.pop((Key["PK"], Key["SK"]), None)
        return {}

    def update_item(self, Key, UpdateExpression, **kwargs):
        """Very small subset of DynamoDB UpdateExpression support.

        Handles:

          - ``SET a = :v``                            (simple set)
          - ``SET a = list_append(if_not_exists(a, :empty), :new)``
                                                      (list append)
          - ``ADD a :delta, b :delta2``               (numeric accumulate)

        These are the shapes produced by ``dynamo_store.update_proposal``,
        ``update_ontology_registry``, and ``extend_ontology_registry``.
        Anything else raises AssertionError so tests fail loudly rather
        than silently doing the wrong thing.
        """
        import re

        item = dict(self.items.get((Key["PK"], Key["SK"]), {**Key}))
        values = kwargs.get("ExpressionAttributeValues", {})
        names = kwargs.get("ExpressionAttributeNames", {})

        expr = UpdateExpression.strip()
        # Split into SET-clause and ADD-clause by the ' ADD ' keyword
        # (UpdateExpression syntax is `SET ... ADD ... REMOVE ...` etc.).
        set_clause, add_clause = "", ""
        if " ADD " in expr:
            head, _, add_clause = expr.partition(" ADD ")
            set_clause = head
        else:
            set_clause = expr

        if set_clause.startswith("SET "):
            body = set_clause[4:]
            # Split on commas that aren't inside parentheses (for list_append).
            parts = re.split(r",(?![^()]*\))", body)
            for part in parts:
                lhs, rhs = [s.strip() for s in part.split("=", 1)]
                for alias, real in (names or {}).items():
                    lhs = lhs.replace(alias, real)

                # list_append(if_not_exists(attr, :empty), :new)
                la = re.match(
                    r"list_append\(\s*if_not_exists\((\w+),\s*(:\w+)\)\s*,\s*(:\w+)\s*\)",
                    rhs,
                )
                if la:
                    attr, empty_ref, new_ref = la.group(1), la.group(2), la.group(3)
                    current = item.get(attr, values.get(empty_ref, []))
                    item[lhs] = list(current) + list(values.get(new_ref, []))
                    continue

                # Simple SET: a = :v
                item[lhs] = values.get(rhs, rhs)

        if add_clause:
            # ADD a :delta, b :delta2, ...
            for part in (p.strip() for p in add_clause.split(",") if p.strip()):
                attr, ref = part.split()
                delta = values.get(ref, 0)
                current = item.get(attr, 0)
                # DynamoDB returns Decimal for numbers; tests can compare via ==.
                item[attr] = (current or 0) + delta

        self.items[(Key["PK"], Key["SK"])] = item
        if kwargs.get("ReturnValues") == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    def query(self, **kwargs):
        key_expr = kwargs.get("KeyConditionExpression", "")
        filter_expr = kwargs.get("FilterExpression", "")
        values = kwargs.get("ExpressionAttributeValues", {})
        pk_val = values.get(":pk")
        skp_val = values.get(":skp")
        out = []
        for (pk, sk), item in self.items.items():
            if ":pk" in key_expr and pk != pk_val:
                continue
            if ":skp" in key_expr and not sk.startswith(skp_val):
                continue
            # Tiny filter-expression evaluator: we only need to handle
            # the shapes produced by dynamo_store (``attr = :val`` and
            # ``contains(list_attr, :val)`` joined with AND).
            if filter_expr:
                ok = True
                for clause in (c.strip() for c in filter_expr.split(" AND ")):
                    if "=" in clause and not clause.startswith("contains"):
                        lhs, rhs = [s.strip() for s in clause.split("=", 1)]
                        if item.get(lhs) != values.get(rhs):
                            ok = False
                            break
                    elif clause.startswith("contains("):
                        inner = clause[len("contains(") : -1]
                        lhs, rhs = [s.strip() for s in inner.split(",", 1)]
                        haystack = item.get(lhs) or []
                        if values.get(rhs) not in haystack:
                            ok = False
                            break
                if not ok:
                    continue
            out.append(dict(item))
        return {"Items": out}

    def scan(self, **kwargs):
        return {"Items": [dict(v) for v in self.items.values()]}


@pytest.fixture()
def fake_dynamo(monkeypatch):
    table = _FakeDynamoTable()
    monkeypatch.setattr(
        "coa_ontology.dynamo_store._get_table",
        lambda: table,
    )
    return table


class TestNamespaceRegistry:
    def test_put_and_get_namespace_meta(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_namespace_meta("acme", data={"backend": "opensearch_neptune"})
        meta = ds.get_namespace_meta("acme")
        assert meta["namespace"] == "acme"
        assert meta["backend"] == "opensearch_neptune"
        assert meta["created_at"]
        assert meta["updated_at"]

    def test_put_namespace_meta_preserves_created_at(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        first = ds.put_namespace_meta("acme", data={"backend": "na_only"})
        second = ds.put_namespace_meta("acme", data={"embedding_dimensions": 1024})
        assert first["created_at"] == second["created_at"]
        assert second["backend"] == "na_only"
        assert second["embedding_dimensions"] == 1024

    def test_put_and_list_ontology_registry(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_ontology_registry(
            "acme",
            "https://example.com/a",
            {"uri": "https://example.com/a", "title": "A", "ontology_type": "foundational"},
        )
        ds.put_ontology_registry(
            "acme",
            "https://example.com/b",
            {"uri": "https://example.com/b", "title": "B", "ontology_type": "induced"},
        )
        rows = ds.list_ontologies_registry("acme")
        titles = sorted(r["title"] for r in rows)
        assert titles == ["A", "B"]

        filtered = ds.list_ontologies_registry("acme", ontology_type="foundational")
        assert [r["title"] for r in filtered] == ["A"]

    def test_get_ontology_by_uri(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_ontology_registry("acme", "uri-1", {"uri": "https://u/1", "title": "One"})
        ds.put_ontology_registry("acme", "uri-2", {"uri": "https://u/2", "title": "Two"})

        assert ds.get_ontology_registry_by_uri("acme", "https://u/2")["title"] == "Two"
        assert ds.get_ontology_registry_by_uri("acme", "https://u/99") is None

    def test_update_and_delete_registry(self, fake_dynamo):
        from coa_ontology import dynamo_store as ds

        ds.put_ontology_registry(
            "acme",
            "ont-1",
            {"uri": "ont-1", "title": "Initial", "class_count": 10, "parse_status": "ok"},
        )
        updated = ds.update_ontology_registry("acme", "ont-1", title="Updated", class_count=12)
        assert updated["title"] == "Updated"
        assert updated["class_count"] == 12

        assert ds.delete_ontology_registry("acme", "ont-1") is True
        assert ds.get_ontology_registry("acme", "ont-1") is None
        assert ds.delete_ontology_registry("acme", "ont-1") is False


# ── Shared fixtures for the ingest pipeline ─────────────────────────────


@pytest.fixture()
def import_client(monkeypatch, tmp_path, fake_dynamo):
    """TestClient with GraphStore / VectorStore / Bedrock all mocked."""
    os.environ["WORKBENCH_BACKEND"] = "opensearch_neptune"
    os.environ.setdefault(
        "NDB_ENDPOINT",
        "https://<test-ndb>.cluster-<id>.us-east-1.neptune.amazonaws.com:8182",
    )
    os.environ.setdefault("OSS_ENDPOINT", "https://<test-aoss>.us-east-1.aoss.amazonaws.com")
    os.environ["ONTOLOGY_STORAGE_PATH"] = str(tmp_path / "ontologies")

    mock_graph = MagicMock()
    mock_graph.get_ontology_by_uri = MagicMock(return_value=None)
    mock_graph.get_ontology = MagicMock(return_value=None)
    mock_graph.create_ontology = MagicMock(return_value={"uri": "https://example.com/onto"})
    mock_graph.store_class = MagicMock(return_value={})
    mock_graph.store_property = MagicMock(return_value={})
    mock_graph.update_ontology = MagicMock(return_value={})
    mock_graph.delete_ontology = MagicMock(return_value=True)
    # Reconcile step (_reconcile_counts) queries Neptune for deduped counts.
    # Return None so reconcile is a no-op and the registry retains the
    # ingest-derived class_count this test asserts on.
    mock_graph.count_classes_and_properties = MagicMock(return_value=None)
    mock_graph.load_turtle = MagicMock(
        return_value={
            "status": "ok",
            "graph_uri": "https://ontology-workbench.local/default/https%3A%2F%2Fexample.com%2Fonto",
            "bytes_loaded": len(TURTLE.encode("utf-8")),
        }
    )

    mock_vec = MagicMock()
    # Model a consistent vector index: whatever store_embeddings_batch writes is
    # immediately searchable. The upload path waits for every written embedding
    # to become searchable via list_searchable_entity_uris, so the mock must
    # echo the inserted URIs back — otherwise the readiness wait spins for the
    # full stall timeout on every upload test (minutes), even though it passes.
    _indexed: list[dict] = []

    def _store_batch(items):
        _indexed.extend(items)
        return items

    def _delete_for_ontology(ontology_id, namespace=None):
        before = len(_indexed)
        _indexed[:] = [row for row in _indexed if row.get("ontology_id") != ontology_id]
        return before - len(_indexed)

    mock_vec.store_embeddings_batch = MagicMock(side_effect=_store_batch)
    mock_vec.delete_embeddings_for_ontology = MagicMock(side_effect=_delete_for_ontology)
    mock_vec.list_searchable_entity_uris = MagicMock(
        side_effect=lambda ontology_id, namespace=None, **kw: [
            row["entity_uri"] for row in _indexed if row.get("ontology_id") == ontology_id and row.get("entity_uri")
        ]
    )
    # Expose a namespace → index name mapping so the ingest helper can
    # record it into the registry.
    mock_vec._index_for_namespace = MagicMock(side_effect=lambda ns: f"ontology-workbench-embeddings-{ns}")

    mock_bedrock = MagicMock()
    mock_bedrock.model_id = "fake-titan-v2"
    mock_bedrock.embed_texts = MagicMock(side_effect=lambda texts: [[float(len(t))] * 4 for t in texts])

    # The accept route is async — POST returns 202 and the ingest runs on
    # a daemon thread. Patch ``Thread`` here so the worker runs inline,
    # collapsing accept tests' timeline to a single call.
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)

    # Pre-import the ``proposals`` submodule so ``patch(...proposals.Thread)``
    # below can resolve the attribute path. Without this the fixture fails
    # with ``AttributeError: module 'coa_ontology' has no
    # attribute 'proposals'`` on a cold pytest run.
    import coa_ontology.proposals  # noqa: F401

    with (
        patch("coa_ontology.stores.build_stores", return_value=(mock_graph, mock_vec)),
        patch("coa_ontology.bedrock_embeddings.BedrockEmbeddingClient", return_value=mock_bedrock),
        patch("coa_ontology.proposals.Thread", _InlineThread),
        # S3 persist (#467) builds its own boto3 client + reads Neptune, and now
        # RAISES on failure (a retried accept step) instead of swallowing — so
        # without a stub it would make real network calls and fail these accepts.
        # These tests don't exercise persistence (see test_s3_persistence.py); the
        # persist-failure accept path is covered in test_proposal_turtle_ingest.py.
        patch("coa_ontology.proposals._persist_induced_to_s3", return_value=None),
    ):
        from coa_ontology.main import app

        yield TestClient(app), mock_graph, mock_vec, mock_bedrock, fake_dynamo


# ── Ingest helper / /upload endpoint ────────────────────────────────────


class TestUploadEndpoint:
    def test_happy_path_writes_registry_graph_and_vectors(self, import_client):
        http, mock_graph, mock_vec, mock_bedrock, ddb = import_client

        resp = http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default", "ontology_type": "user_created"},
            files={"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # The response is an OntologyResponse — catalog metadata shape.
        assert body["uri"] == "https://example.com/onto"
        assert body["class_count"] == 2
        assert body["property_count"] == 2
        assert body["axiom_count"] > 0
        assert body["parse_status"] == "ok"
        assert body["embedding_count"] == 4
        assert body["embedding_index"] == "ontology-workbench-embeddings-default"
        assert body["graph_uri"].startswith("https://")
        assert "/default/https%3A%2F%2Fexample.com%2Fonto" in body["graph_uri"]

        # Catalog projection ran: create_ontology on first upload. Classes and
        # properties are projected via the batched path (store_classes_batch /
        # store_properties_batch) — one call each carrying all 2 items — instead
        # of one store_class/store_property per item.
        assert mock_graph.create_ontology.call_count == 1
        assert mock_graph.store_classes_batch.call_count == 1
        assert len(mock_graph.store_classes_batch.call_args.kwargs["classes"]) == 2
        assert mock_graph.store_properties_batch.call_count == 1
        assert len(mock_graph.store_properties_batch.call_args.kwargs["properties"]) == 2
        # load_turtle received the ontology IRI as ontology_uri
        mock_graph.load_turtle.assert_called_once()
        kw = mock_graph.load_turtle.call_args.kwargs
        assert kw["ontology_uri"] == "https://example.com/onto"
        assert "owl:Ontology" in kw["turtle"]

        # Embeddings into the shared namespace index
        mock_bedrock.embed_texts.assert_called_once()
        items = mock_vec.store_embeddings_batch.call_args.args[0]
        assert len(items) == 4
        assert all(it["namespace"] == "default" for it in items)
        assert all(it["ontology_id"] == "https://example.com/onto" for it in items)

        # Registry row written
        reg = {k: v for k, v in ddb.items.items() if k[0].startswith("NAMESPACE#default")}
        sks = sorted(sk for _, sk in reg)
        assert sks == ["META", "ONTOLOGY#https://example.com/onto"]
        ont_row = reg[("NAMESPACE#default", "ONTOLOGY#https://example.com/onto")]
        assert ont_row["ontology_type"] == "user_created"
        assert ont_row["source"] == "upload"

    def test_second_upload_returns_409(self, import_client):
        """Same ontology_id twice in the same namespace → 409 Conflict.

        ``DELETE`` the ontology first if you want to re-upload.
        """
        http, mock_graph, mock_vec, *_ = import_client
        files = {"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")}
        r1 = http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default"},
            files=files,
        )
        assert r1.status_code == 200

        files = {"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")}
        r2 = http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default"},
            files=files,
        )
        assert r2.status_code == 409
        assert "already exists" in r2.json()["detail"]
        # The conflict is raised BEFORE any store writes — create_ontology
        # and load_turtle each ran exactly once, for the first upload.
        assert mock_graph.create_ontology.call_count == 1
        assert mock_graph.load_turtle.call_count == 1
        # And the embedding batch was written exactly once too.
        assert mock_vec.store_embeddings_batch.call_count == 1

    def test_delete_then_reupload_succeeds(self, import_client):
        """After DELETE the registry row is gone, so a fresh upload works."""
        http, *_ = import_client
        files = {"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")}
        http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default"},
            files=files,
        )
        http.delete(
            "/ontologies/https://example.com/onto",
            params={"namespace": "default"},
        )
        files = {"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")}
        r = http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default"},
            files=files,
        )
        assert r.status_code == 200

    def test_malformed_turtle_returns_422(self, import_client):
        http, mock_graph, mock_vec, *_ = import_client
        resp = http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default"},
            files={"file": ("bad.ttl", b"not turtle {{{", "text/turtle")},
        )
        assert resp.status_code == 422
        # No writes happened.
        mock_graph.create_ontology.assert_not_called()
        mock_graph.load_turtle.assert_not_called()

    def test_load_turtle_failure_rolls_back_catalog(self, import_client):
        """If load_turtle fails, the catalog state is rolled back so the
        registry doesn't end up with a row for a half-ingested ontology."""
        http, mock_graph, mock_vec, *_ = import_client
        mock_graph.load_turtle.side_effect = RuntimeError("GSP 503")
        resp = http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default"},
            files={"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")},
        )
        assert resp.status_code == 502
        # Opaque client-facing message — the raw backend exception ("GSP 503")
        # stays server-side via log.exception (public-mirror hygiene).
        assert "GSP 503" not in resp.json()["detail"]
        assert resp.json()["detail"] == "Ontology ingest failed"
        # delete_ontology was called to undo the partial catalog projection.
        mock_graph.delete_ontology.assert_called()
        # No embeddings upserted
        mock_vec.store_embeddings_batch.assert_not_called()

    def test_list_ontologies_reads_the_registry(self, import_client):
        http, *_ = import_client
        files = {"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")}
        http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "acme"},
            files=files,
        )
        # Distinct namespace
        r = http.get("/ontologies/", params={"namespace": "acme"})
        assert r.status_code == 200
        # ``{ontologies: [...]}`` envelope with camelCase members — the
        # Smithy-declared shape (see test_list_ontologies_wire_shape.py).
        rows = r.json()["ontologies"]
        assert len(rows) == 1
        assert rows[0]["uri"] == "https://example.com/onto"
        assert rows[0]["graphUri"].startswith("https://")
        # ``embedding_index`` is registry-only bookkeeping that the contract does
        # not declare, so it is no longer part of this response. The namespace
        # scoping it used to prove is covered by the graph URI + the
        # cross-namespace isolation assertion below.
        assert "embedding_index" not in rows[0]

        # Different namespace sees nothing
        r2 = http.get("/ontologies/", params={"namespace": "other"})
        assert r2.json() == {"ontologies": []}

    def test_delete_clears_registry_and_vectors(self, import_client):
        http, mock_graph, mock_vec, *_ = import_client
        files = {"file": ("onto.ttl", TURTLE.encode("utf-8"), "text/turtle")}
        http.post(
            "/ontologies/https://example.com/onto/upload",
            data={"namespace": "default"},
            files=files,
        )
        r = http.delete(
            "/ontologies/https://example.com/onto",
            params={"namespace": "default"},
        )
        assert r.status_code == 204
        mock_graph.delete_ontology.assert_called()
        mock_vec.delete_embeddings_for_ontology.assert_called()
        listing = http.get("/ontologies/", params={"namespace": "default"}).json()
        assert listing == {"ontologies": []}


# ── Multi-proposal merge into a single ontology ─────────────────────────


PROPOSAL_A_TURTLE = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <https://example.com/onto#> .

<https://example.com/onto> a owl:Ontology ;
    rdfs:label "Proposal A Source" .

ex:Patient a owl:Class ; rdfs:label "Patient" .

ex:hasAge a owl:DatatypeProperty ;
    rdfs:label "hasAge" ;
    rdfs:domain ex:Patient .
"""

PROPOSAL_B_TURTLE = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <https://example.com/onto#> .

<https://example.com/onto> a owl:Ontology ;
    rdfs:label "Proposal B Source" .

ex:Doctor a owl:Class ; rdfs:label "Doctor" .
ex:Patient a owl:Class ; rdfs:label "Patient" .

ex:treats a owl:ObjectProperty ;
    rdfs:label "treats" ;
    rdfs:domain ex:Doctor ;
    rdfs:range  ex:Patient .
"""


def _seed_proposal(ddb, *, proposal_id, ontology_id, turtle, namespace="default"):
    ddb.put_item(
        Item={
            "PK": f"{namespace}#PROPOSAL#{proposal_id}",
            "SK": "META",
            "proposal_id": proposal_id,
            "job_id": f"job-{proposal_id}",
            "namespace": namespace,
            "ontology_id": ontology_id,
            "status": "pending",
            "ontology_turtle": turtle,
            "metadata": {"label": "Induced"},
        }
    )


def _proposal_status(ddb, proposal_id: str, namespace: str = "default") -> dict:
    """Return the proposal META row's terminal state after the inline
    worker has completed."""
    return ddb.get_item(Key={"PK": f"{namespace}#PROPOSAL#{proposal_id}", "SK": "META"})["Item"]


class TestProposalMergeIntoOneOntology:
    """Multiple proposals → same target ontology URI → merged triples + one registry row.

    The accept route returns 202; the inline-thread fixture means the
    worker has already run by the time the test continues. Result shape
    is observable on the ``NAMESPACE#default / ONTOLOGY#…`` registry row,
    not on the response body.
    """

    def test_first_accept_creates_registry_row(self, import_client):
        http, mock_graph, mock_vec, mock_bedrock, ddb = import_client
        _seed_proposal(
            ddb,
            proposal_id="p-A",
            ontology_id="https://example.com/onto",
            turtle=PROPOSAL_A_TURTLE,
        )
        r = http.post(
            "/ontology/proposals/p-A/accept",
            json={"ontology_id": "https://target.example/master"},
        )
        assert r.status_code == 202, r.text
        assert r.json() == {"proposal_id": "p-A", "status": "accepting"}

        # Worker ran inline → proposal flipped to ``accepted``.
        assert _proposal_status(ddb, "p-A")["status"] == "accepted"

        # Graph store wrote to the TARGET ontology, not the proposal's.
        mock_graph.create_ontology.assert_called_once()
        created = mock_graph.create_ontology.call_args.args[0]
        assert created["uri"] == "https://target.example/master"

        mock_graph.load_turtle.assert_called_once()
        kw = mock_graph.load_turtle.call_args.kwargs
        assert kw["ontology_uri"] == "https://target.example/master"

        # Registry row under NAMESPACE#default / ONTOLOGY#<target>
        row = ddb.get_item(Key={"PK": "NAMESPACE#default", "SK": "ONTOLOGY#https://target.example/master"})["Item"]
        assert row["uri"] == "https://target.example/master"
        assert row["source_proposals"] == ["p-A"]
        assert row["class_count"] == 1
        assert row["property_count"] == 1
        assert row["embedding_count"] == 2  # 1 class + 1 property

    def test_second_accept_appends_to_same_ontology(self, import_client):
        http, mock_graph, mock_vec, mock_bedrock, ddb = import_client
        _seed_proposal(
            ddb,
            proposal_id="p-A",
            ontology_id="https://example.com/onto",
            turtle=PROPOSAL_A_TURTLE,
        )
        _seed_proposal(
            ddb,
            proposal_id="p-B",
            ontology_id="https://example.com/onto",
            turtle=PROPOSAL_B_TURTLE,
        )

        target = "https://target.example/master"
        r1 = http.post("/ontology/proposals/p-A/accept", json={"ontology_id": target})
        assert r1.status_code == 202
        assert _proposal_status(ddb, "p-A")["status"] == "accepted"

        r2 = http.post("/ontology/proposals/p-B/accept", json={"ontology_id": target})
        assert r2.status_code == 202, r2.text
        assert _proposal_status(ddb, "p-B")["status"] == "accepted"

        # create_ontology only ran for the first proposal; the second
        # skipped it to avoid stomping dct:created.
        assert mock_graph.create_ontology.call_count == 1

        # But load_turtle ran twice — once per proposal, both targeting
        # the same graph URI, so triples merge in the named graph.
        assert mock_graph.load_turtle.call_count == 2
        loaded_uris = {c.kwargs["ontology_uri"] for c in mock_graph.load_turtle.call_args_list}
        assert loaded_uris == {target}

        # Classes/properties are projected via the batched path: one
        # store_classes_batch per accept (2 accepts), together carrying
        # 1 (A) + 2 (B) = 3 classes and 1 + 1 = 2 properties, all targeting the
        # same ontology_uri (the target).
        class_calls = mock_graph.store_classes_batch.call_args_list
        prop_calls = mock_graph.store_properties_batch.call_args_list
        assert len(class_calls) == 2  # one batch per accept
        assert sum(len(c.kwargs["classes"]) for c in class_calls) == 3
        assert sum(len(c.kwargs["properties"]) for c in prop_calls) == 2
        for c in class_calls:
            assert c.kwargs["ontology_uri"] == target
        for c in prop_calls:
            assert c.kwargs["ontology_uri"] == target

        # Registry reflects the accumulation.
        row = ddb.get_item(Key={"PK": "NAMESPACE#default", "SK": f"ONTOLOGY#{target}"})["Item"]
        assert row["source_proposals"] == ["p-A", "p-B"]
        # Counts are cumulative (not dedup'd): A(1) + B(2) = 3 classes
        assert row["class_count"] == 3
        assert row["property_count"] == 2
        assert row["embedding_count"] == 5  # 2 + 3

    def test_accept_without_target_defaults_to_proposal_ontology_id(self, import_client):
        """No body → uses the proposal's own ontology_id, preserving the
        old one-proposal-per-ontology behaviour for callers that don't
        care about merging."""
        _, mock_graph, _, _, ddb = import_client
        http, *_ = import_client
        _seed_proposal(
            ddb=ddb,
            proposal_id="p-A",
            ontology_id="https://example.com/onto",
            turtle=PROPOSAL_A_TURTLE,
        )
        r = http.post("/ontology/proposals/p-A/accept")
        assert r.status_code == 202
        assert _proposal_status(ddb, "p-A")["status"] == "accepted"
        # Graph store target was the proposal's own ontology_id.
        loaded = mock_graph.load_turtle.call_args.kwargs["ontology_uri"]
        assert loaded == "https://example.com/onto"


class TestOneOntologyPerNamespaceGuard:
    """Accept enforces one induced ontology per namespace.

    The UI induces every source under a namespace-derived prefix, so all
    sources share one ontology_id. A direct-API caller could induce sources
    under DISTINCT ``ontology_uri_prefix`` values, producing distinct
    ontology_ids in one namespace; accepting the second used to overwrite the
    first's per-namespace VKG ``latest/`` payload. The guard rejects that with
    409 unless the caller explicitly names the target via ``body.ontology_id``
    (the deliberate merge-into-target path).
    """

    def _seed_registry(self, ddb: Any, namespace: str, ontology_id: str, ontology_type: str = "induced") -> None:
        from coa_ontology import dynamo_store as ds

        ds.put_ontology_registry(
            namespace,
            ontology_id,
            {"uri": ontology_id, "title": "Existing", "ontology_type": ontology_type},
        )

    def test_second_ontology_distinct_id_rejected_409(self, import_client):
        http, _mock_graph, _mv, _mb, ddb = import_client
        # An ontology already exists in the namespace (a prior accept).
        self._seed_registry(ddb, "default", "http://coa/namespace/default/ontology/induced#")
        # A proposal induced under a DIFFERENT prefix (distinct ontology_id).
        _seed_proposal(
            ddb,
            proposal_id="p-X",
            ontology_id="https://rogue.example/other#",
            turtle=PROPOSAL_A_TURTLE,
        )
        r = http.post("/ontology/proposals/p-X/accept")  # no override
        assert r.status_code == 409, r.text
        assert "exactly one induced ontology" in r.json().get("detail", "")
        # Proposal not consumed — still pending, no overwrite happened.
        assert _proposal_status(ddb, "p-X")["status"] == "pending"

    def test_same_id_as_existing_allowed(self, import_client):
        http, _mock_graph, _mv, _mb, ddb = import_client
        oid = "http://coa/namespace/default/ontology/induced#"
        self._seed_registry(ddb, "default", oid)
        # Proposal under the SAME ontology_id → merges, allowed.
        _seed_proposal(ddb, proposal_id="p-Y", ontology_id=oid, turtle=PROPOSAL_A_TURTLE)
        r = http.post("/ontology/proposals/p-Y/accept")
        assert r.status_code == 202, r.text
        assert _proposal_status(ddb, "p-Y")["status"] == "accepted"

    def test_first_ontology_in_empty_namespace_allowed(self, import_client):
        http, _mock_graph, _mv, _mb, ddb = import_client
        # No existing ontology in the namespace → any id is fine.
        _seed_proposal(
            ddb,
            proposal_id="p-Z",
            ontology_id="https://first.example/onto#",
            turtle=PROPOSAL_A_TURTLE,
        )
        r = http.post("/ontology/proposals/p-Z/accept")
        assert r.status_code == 202, r.text
        assert _proposal_status(ddb, "p-Z")["status"] == "accepted"

    def test_foundational_ontology_does_not_block_first_induced_accept(self, import_client):
        """Regression: a foundational (grounding) ontology present in the namespace
        must NOT trip the guard on the FIRST induced accept.

        Foundational/reference ontologies are ONTOLOGY# registry rows too, but
        they are meant to coexist with the induced ontology — the invariant is
        "one *induced* ontology per namespace". The guard scopes its existing-id
        check to ontology_type="induced"; without that scoping, loading FIBO
        before inducing would wrongly 409-reject the first legitimate induction.
        """
        http, _mock_graph, _mv, _mb, ddb = import_client
        # A foundational ontology is loaded (as grounding requires), distinct id.
        self._seed_registry(ddb, "default", "https://spec.edmcouncil.org/fibo/ontology#", ontology_type="foundational")
        # First induced proposal, under the namespace's own induced prefix.
        _seed_proposal(
            ddb,
            proposal_id="p-F",
            ontology_id="http://coa/namespace/default/ontology/induced#",
            turtle=PROPOSAL_A_TURTLE,
        )
        r = http.post("/ontology/proposals/p-F/accept")  # no override
        assert r.status_code == 202, f"foundational ontology wrongly blocked first induced accept: {r.text}"
        assert _proposal_status(ddb, "p-F")["status"] == "accepted"

    def test_explicit_override_bypasses_guard(self, import_client):
        http, _mock_graph, _mv, _mb, ddb = import_client
        # Existing ontology present, proposal under a distinct id, BUT caller
        # explicitly names a target → deliberate merge, guard does not fire.
        self._seed_registry(ddb, "default", "http://coa/namespace/default/ontology/induced#")
        _seed_proposal(
            ddb,
            proposal_id="p-W",
            ontology_id="https://rogue.example/other#",
            turtle=PROPOSAL_A_TURTLE,
        )
        r = http.post(
            "/ontology/proposals/p-W/accept",
            json={"ontology_id": "https://target.example/master"},
        )
        assert r.status_code == 202, r.text
        assert _proposal_status(ddb, "p-W")["status"] == "accepted"

    def test_load_turtle_failure_on_append_preserves_prior_state(self, import_client):
        """If the merge-into-existing load fails (after its retries), we must NOT
        delete the already-loaded ontology, and proposal B lands in the terminal
        ``accept_failed`` state with ``accept_error`` set."""
        http, mock_graph, mock_vec, mock_bedrock, ddb = import_client
        _seed_proposal(
            ddb,
            proposal_id="p-A",
            ontology_id="https://example.com/onto",
            turtle=PROPOSAL_A_TURTLE,
        )
        _seed_proposal(
            ddb,
            proposal_id="p-B",
            ontology_id="https://example.com/onto",
            turtle=PROPOSAL_B_TURTLE,
        )
        target = "https://target.example/master"

        # First accept succeeds.
        r1 = http.post("/ontology/proposals/p-A/accept", json={"ontology_id": target})
        assert r1.status_code == 202
        assert _proposal_status(ddb, "p-A")["status"] == "accepted"

        # Second accept: load_turtle fails inside the worker (all retries).
        # The retry backoff is zeroed for this module by ``_no_accept_backoff``.
        mock_graph.delete_ontology.reset_mock()
        mock_graph.load_turtle.side_effect = RuntimeError("GSP 503")
        r2 = http.post("/ontology/proposals/p-B/accept", json={"ontology_id": target})
        assert r2.status_code == 202

        b_final = _proposal_status(ddb, "p-B")
        assert b_final["status"] == "accept_failed"
        assert "GSP 503" in b_final["accept_error"]

        # Critical: prior state must NOT be wiped.
        mock_graph.delete_ontology.assert_not_called()
        # The registry row still carries proposal A.
        row = ddb.get_item(Key={"PK": "NAMESPACE#default", "SK": f"ONTOLOGY#{target}"})["Item"]
        assert row["source_proposals"] == ["p-A"]


# ── Property type coverage ─────────────────────────────────────────────


TURTLE_MIXED_PROPERTY_TYPES = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix ex:  <https://example.com/mixed#> .

<https://example.com/mixed> a owl:Ontology ; rdfs:label "Mixed Property Types" .

ex:Person a owl:Class ; rdfs:label "Person" .
ex:Organization a owl:Class ; rdfs:label "Organization" .

ex:name a owl:DatatypeProperty ;
    rdfs:label "name" ;
    rdfs:domain ex:Person ;
    rdfs:range xsd:string .

ex:worksFor a owl:ObjectProperty ;
    rdfs:label "works for" ;
    rdfs:domain ex:Person ;
    rdfs:range ex:Organization .

ex:creator a owl:AnnotationProperty ;
    rdfs:label "creator" .

ex:hasSSN a owl:DatatypeProperty, owl:FunctionalProperty ;
    rdfs:label "has SSN" ;
    rdfs:domain ex:Person ;
    rdfs:range xsd:string .

ex:hasCEO a owl:FunctionalProperty ;
    rdfs:label "has CEO" ;
    rdfs:domain ex:Organization ;
    rdfs:range ex:Person .
"""


class TestMixedPropertyTypes:
    """Verify that AnnotationProperty, FunctionalProperty, and mixed-type
    properties are correctly ingested without double-counting."""

    def test_all_property_types_counted_without_overlap(self, import_client):
        http, mock_graph, mock_vec, mock_bedrock, ddb = import_client

        resp = http.post(
            "/ontologies/https://example.com/mixed/upload",
            data={"namespace": "default", "ontology_type": "user_created"},
            files={"file": ("mixed.ttl", TURTLE_MIXED_PROPERTY_TYPES.encode("utf-8"), "text/turtle")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["class_count"] == 2
        # 5 property declarations but hasSSN is both DatatypeProperty and
        # FunctionalProperty — should count as ONE property, not two.
        # name (dt), worksFor (obj), creator (annotation), hasSSN (dt+functional), hasCEO (functional)
        # dt_props: {name, hasSSN}  obj_props: {worksFor, creator, hasCEO} (functional - dt_props)
        assert body["property_count"] == 5

    def test_annotation_property_gets_embedded(self, import_client):
        http, mock_graph, mock_vec, mock_bedrock, ddb = import_client

        resp = http.post(
            "/ontologies/https://example.com/mixed/upload",
            data={"namespace": "default", "ontology_type": "user_created"},
            files={"file": ("mixed.ttl", TURTLE_MIXED_PROPERTY_TYPES.encode("utf-8"), "text/turtle")},
        )
        assert resp.status_code == 200, resp.text

        # All classes + properties should be embedded
        items = mock_vec.store_embeddings_batch.call_args.args[0]
        entity_uris = {it["entity_uri"] for it in items}
        assert "https://example.com/mixed#creator" in entity_uris
        assert "https://example.com/mixed#hasCEO" in entity_uris
        assert "https://example.com/mixed#hasSSN" in entity_uris

    def test_functional_property_not_double_counted(self, import_client):
        http, mock_graph, mock_vec, mock_bedrock, ddb = import_client

        resp = http.post(
            "/ontologies/https://example.com/mixed/upload",
            data={"namespace": "default", "ontology_type": "user_created"},
            files={"file": ("mixed.ttl", TURTLE_MIXED_PROPERTY_TYPES.encode("utf-8"), "text/turtle")},
        )
        assert resp.status_code == 200, resp.text

        # store_property should be called once per unique property, not once
        # per type declaration. hasSSN has 2 types but is 1 property.
        items = mock_vec.store_embeddings_batch.call_args.args[0]
        ssn_items = [it for it in items if "hasSSN" in it["entity_uri"]]
        assert len(ssn_items) == 1
