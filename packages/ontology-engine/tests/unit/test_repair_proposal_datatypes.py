# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests: POST /proposals/{id}/repair-datatypes.

The consent-gated repair applies canonicalization to the STORED proposal Turtle
(ontology + R2RML), persists the result via S3 offload, flips status to
``updated``, and returns the change list. It is idempotent (clean proposal →
repaired_count 0, no persist) and refuses accepted/rejected proposals (409).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

_MALFORMED = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<https://example.org/p> a owl:DatatypeProperty ; rdfs:range xsd:datetime .
"""

_CLEAN = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<https://example.org/p> a owl:DatatypeProperty ; rdfs:range xsd:string .
"""


def _stub_store(monkeypatch, item, put_sink, update_sink):
    from coa_ontology import proposals

    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", lambda pid, namespace="default": item)
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            put_sink.update({artifact: content, f"{artifact}_version": version})
            or f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"
        ),
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "update_proposal",
        lambda pid, namespace="default", **f: update_sink.update(f),
    )
    return proposals


def test_repair_rewrites_malformed_ontology_and_persists(monkeypatch):
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "p1", "status": "pending", "ontology_turtle": _MALFORMED, "r2rml_turtle": ""}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    resp = proposals.repair_proposal_datatypes("p1", namespace="ns")

    assert resp.repaired_count == 1
    assert resp.changes == [
        {"found": "http://www.w3.org/2001/XMLSchema#datetime", "canonical": "http://www.w3.org/2001/XMLSchema#dateTime"}
    ]
    # Stored artifact is the canonicalized version.
    assert "ontology" in put_sink
    assert "xsd:datetime" not in put_sink["ontology"]
    assert update_sink["status"] == "updated"
    assert "ontology_s3_key" in update_sink
    assert put_sink["ontology_version"].startswith("revisions/")
    assert update_sink["revision_id"] == put_sink["ontology_version"].removeprefix("revisions/")


def test_repair_also_fixes_r2rml(monkeypatch):
    r2rml = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://x#om> rr:datatype xsd:datetime .
"""
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "p2", "status": "updated", "ontology_turtle": _CLEAN, "r2rml_turtle": r2rml}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    resp = proposals.repair_proposal_datatypes("p2", namespace="ns")

    assert resp.repaired_count == 1
    # R2RML artifact was rewritten; ontology (clean) was not.
    assert "r2rml" in put_sink
    assert "ontology" not in put_sink
    assert put_sink["r2rml_version"].startswith("revisions/")


def test_repair_clean_proposal_is_noop(monkeypatch):
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "p3", "status": "pending", "ontology_turtle": _CLEAN, "r2rml_turtle": ""}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    resp = proposals.repair_proposal_datatypes("p3", namespace="ns")

    assert resp.repaired_count == 0
    assert put_sink == {}  # nothing persisted
    assert update_sink == {}  # status not touched


def test_repair_not_found(monkeypatch):
    from coa_ontology import proposals

    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", lambda pid, namespace="default": None)
    with pytest.raises(HTTPException) as exc:
        proposals.repair_proposal_datatypes("missing", namespace="ns")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("status", ["accepted", "rejected"])
def test_repair_refuses_terminal_proposals(monkeypatch, status):
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "pT", "status": status, "ontology_turtle": _MALFORMED, "r2rml_turtle": ""}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    with pytest.raises(HTTPException) as exc:
        proposals.repair_proposal_datatypes("pT", namespace="ns")
    assert exc.value.status_code == 409
    assert put_sink == {}  # never persisted


def test_dry_run_returns_count_without_persisting(monkeypatch):
    """Accept-time guard path: dry_run reports what WOULD change but mutates nothing."""
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "pd", "status": "pending", "ontology_turtle": _MALFORMED, "r2rml_turtle": ""}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    resp = proposals.repair_proposal_datatypes(
        "pd", body=proposals.RepairDatatypesRequest(dry_run=True), namespace="ns"
    )

    assert resp.repaired_count == 1
    assert resp.dry_run is True
    assert resp.changes  # the preview carries the change list
    assert put_sink == {}  # NOTHING persisted
    assert update_sink == {}  # status untouched


def test_dry_run_allowed_on_terminal_proposal(monkeypatch):
    # A read-only preview must not 409 — only the mutating path is state-gated.
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "pa", "status": "accepted", "ontology_turtle": _MALFORMED, "r2rml_turtle": ""}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    resp = proposals.repair_proposal_datatypes(
        "pa", body=proposals.RepairDatatypesRequest(dry_run=True), namespace="ns"
    )
    assert resp.repaired_count == 1
    assert put_sink == {}


def test_dry_run_clean_proposal_reports_zero(monkeypatch):
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "pc", "status": "pending", "ontology_turtle": _CLEAN, "r2rml_turtle": ""}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    resp = proposals.repair_proposal_datatypes(
        "pc", body=proposals.RepairDatatypesRequest(dry_run=True), namespace="ns"
    )
    assert resp.repaired_count == 0
    assert resp.dry_run is True
    assert put_sink == {}


_UNPARSEABLE = "@prefix : <bad .\nthis is not valid turtle {{{"


@pytest.mark.parametrize("dry_run", [False, True])
def test_repair_unparseable_stored_turtle_returns_422_not_500(monkeypatch, dry_run):
    """Stored Turtle is normally validated on write, but out-of-band corruption
    (manual DDB/S3 edit, truncated object) can leave it unparseable. The endpoint
    must return an actionable 422 — matching _validate_turtle — not a 500 + rdflib
    traceback. Covers BOTH apply and dry_run (the accept-time guard uses dry_run,
    which would otherwise surface a 500 to the guard)."""
    put_sink: dict = {}
    update_sink: dict = {}
    item = {"proposal_id": "pbad", "status": "pending", "ontology_turtle": _UNPARSEABLE, "r2rml_turtle": ""}
    proposals = _stub_store(monkeypatch, item, put_sink, update_sink)

    body = proposals.RepairDatatypesRequest(dry_run=dry_run) if dry_run else None
    with pytest.raises(HTTPException) as exc:
        proposals.repair_proposal_datatypes("pbad", body=body, namespace="ns")
    assert exc.value.status_code == 422
    assert "parseable" in str(exc.value.detail).lower()
    assert put_sink == {}  # nothing persisted on the error path


def test_repair_does_not_publish_partial_revision_when_second_s3_write_fails(monkeypatch):
    from coa_ontology import proposals

    r2rml = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://x#om> rr:datatype xsd:datetime .
"""
    item = {
        "proposal_id": "p-atomic",
        "status": "pending",
        "ontology_turtle": _MALFORMED,
        "r2rml_turtle": r2rml,
    }
    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", lambda pid, namespace="default": item)
    writes: list[tuple[str, str]] = []

    def _put(namespace, pid, artifact, content, version="latest"):
        writes.append((artifact, version))
        if artifact == "r2rml":
            raise RuntimeError("simulated S3 failure")
        return f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"

    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", _put)
    update_call = []
    monkeypatch.setattr(
        proposals.dynamo_store,
        "update_proposal",
        lambda pid, namespace="default", **fields: update_call.append(fields),
    )

    with pytest.raises(RuntimeError, match="simulated S3 failure"):
        proposals.repair_proposal_datatypes("p-atomic", namespace="ns")

    assert len(writes) == 2
    assert writes[0][1] == writes[1][1]
    assert writes[0][1].startswith("revisions/")
    assert update_call == []
