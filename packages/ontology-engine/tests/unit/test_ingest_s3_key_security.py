# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Security: ingest-from-s3 must reject s3_keys outside the caller's namespace.

The ontology-artifacts bucket is multi-tenant. ``POST .../ingest-from-s3``
takes a caller-supplied ``s3_key``; without validation a user in namespace A
could pass another tenant's key (e.g. ``proposals/B/<id>/latest/ontology.ttl``)
and have its content read + ingested into A. The handler must enforce that the
key sits under ``ontology-uploads/{namespace}/`` and reject path traversal,
BEFORE any S3 access or job creation.
"""

from __future__ import annotations

import pytest
from coa_control_plane_server.models.ingest_ontology_from_s3_request_content import (
    IngestOntologyFromS3RequestContent as IngestFromS3Request,
)
from coa_ontology.catalog.routers.ontologies import (
    ingest_ontology_from_s3,
)
from fastapi import HTTPException
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _call(s3_key: str, namespace: str = "ns-a", monkeypatch=None, *, unvalidated: bool = False):
    # Guard: if validation is bypassed, put_ingest_job firing proves the key
    # reached the persistence/ingest path — fail loudly.
    if monkeypatch is not None:
        import coa_ontology.dynamo_store as ds

        monkeypatch.setattr(
            ds,
            "put_ingest_job",
            lambda *a, **k: pytest.fail("rejected key must not reach put_ingest_job"),
        )
    # ``unvalidated`` skips the contract model's own key pattern, to prove the
    # handler's check still holds on its own (defense in depth).
    body = IngestFromS3Request.model_construct(s3_key=s3_key) if unvalidated else IngestFromS3Request(s3_key=s3_key)
    return ingest_ontology_from_s3(
        ontology_id="https://example.org/o",
        body=body,
        namespace=namespace,
    )


@pytest.mark.parametrize(
    "bad_key",
    [
        "proposals/ns-b/p-1/latest/ontology.ttl",  # another tenant's artifact
        "ontology-uploads/ns-b/abc/x.ttl",  # another namespace's upload prefix
        "ontologies/ns-a/induced/x.ttl",  # wrong (non-upload) prefix
        "ontology-uploads/ns-a-evil/abc/x.ttl",  # prefix-confusion (ns-a is a substring)
    ],
)
def test_rejects_keys_outside_namespace_prefix(bad_key, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _call(bad_key, namespace="ns-a", monkeypatch=monkeypatch)
    assert exc.value.status_code == 403


# Shapes the contract's ``OntologyUploadKey`` pattern refuses outright.
_MALFORMED_KEYS = [
    "ontology-uploads/ns-a/../ns-b/abc/x.ttl",  # traversal out of the prefix
    "ontology-uploads/ns-a/abc/..",  # trailing traversal segment
    "/ontology-uploads/ns-a/abc/x.ttl",  # absolute
    "ontology-uploads\\ns-a\\abc\\x.ttl",  # backslash separators
    "",  # empty
]


@pytest.mark.parametrize("bad_key", _MALFORMED_KEYS)
def test_contract_model_rejects_malformed_keys(bad_key):
    with pytest.raises(ValidationError):
        IngestFromS3Request(s3_key=bad_key)


@pytest.mark.parametrize("bad_key", _MALFORMED_KEYS)
def test_handler_check_still_rejects_malformed_keys(bad_key, monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _call(bad_key, namespace="ns-a", monkeypatch=monkeypatch, unvalidated=True)
    assert exc.value.status_code == 403


def test_contract_pattern_accepts_dots_inside_a_filename():
    """Upload keys come from the server's own sanitizer, which keeps runs of ``.``."""
    IngestFromS3Request(s3_key="ontology-uploads/ns-a/upload123/urn_x..v2.ttl")


def test_accepts_valid_namespace_scoped_key(monkeypatch):
    import coa_ontology.catalog.routers.ontologies as mod

    created: dict = {}
    monkeypatch.setattr(
        mod.dynamo_store,
        "put_ingest_job",
        lambda job_id, ontology_id, status, namespace="default": created.update({"job_id": job_id, "ns": namespace}),
    )
    # No pre-existing registry row → the handler writes an "ingesting" stub so
    # the ontology shows in the list immediately. Mock both the lookup and the
    # stub write (they hit DynamoDB, absent in unit tests) and capture the stub.
    monkeypatch.setattr(mod.dynamo_store, "get_ontology_registry", lambda ns, oid: None)
    stub: dict = {}
    monkeypatch.setattr(
        mod.dynamo_store,
        "update_ontology_registry",
        lambda ns, oid, **fields: stub.update({"ns": ns, "oid": oid, **fields}),
    )
    # Don't actually run the background ingest.
    monkeypatch.setattr(mod, "Thread", None, raising=False)

    class _NoopThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", _NoopThread)

    out = ingest_ontology_from_s3(
        ontology_id="https://example.org/o",
        body=IngestFromS3Request(s3_key="ontology-uploads/ns-a/upload123/my.ttl"),
        namespace="ns-a",
    )
    # Typed IngestOntologyFromS3ResponseContent → result.status is the enum.
    assert out.result.status == "pending"
    assert created["ns"] == "ns-a"
    # The up-front stub row makes the upload visible immediately with a spinner.
    assert stub["status"] == mod.ONTOLOGY_STATUS_INGESTING
    assert stub["parse_status"] == "pending"
    assert stub["ns"] == "ns-a"


@pytest.mark.parametrize(
    ("fmt", "ontology_type"),
    # The web app's upload flow sends these (services/ontology-engine.ts).
    [("turtle", "user_uploaded"), ("rdf+xml", "user_uploaded"), ("json-ld", "user_uploaded"), (None, None)],
)
def test_contract_enums_reach_the_ingest_as_plain_strings(fmt, ontology_type, monkeypatch):
    import coa_ontology.catalog.routers.ontologies as mod

    monkeypatch.setattr(mod.dynamo_store, "put_ingest_job", lambda *a, **k: None)
    monkeypatch.setattr(mod.dynamo_store, "get_ontology_registry", lambda ns, oid: None)
    stub: dict = {}
    monkeypatch.setattr(mod.dynamo_store, "update_ontology_registry", lambda ns, oid, **fields: stub.update(fields))
    started: dict = {}

    class _CapturingThread:
        def __init__(self, *, target, args, daemon):
            started["args"] = args

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", _CapturingThread)

    body = IngestFromS3Request.model_validate(
        {"s3_key": "ontology-uploads/ns-a/u1/my.ttl", "format": fmt, "ontology_type": ontology_type}
    )
    ingest_ontology_from_s3(ontology_id="https://example.org/o", body=body, namespace="ns-a")

    _job, _oid, _key, sent_fmt, _title, sent_type, _ns = started["args"]
    assert type(sent_fmt) is str and sent_fmt == (fmt or "turtle")
    assert type(sent_type) is str and sent_type == (ontology_type or "user_created")
    assert type(stub["format"]) is str and type(stub["ontology_type"]) is str


def test_contract_rejects_an_unknown_format():
    with pytest.raises(ValidationError):
        IngestFromS3Request.model_validate({"s3_key": "ontology-uploads/ns-a/u1/my.ttl", "format": "yaml"})
