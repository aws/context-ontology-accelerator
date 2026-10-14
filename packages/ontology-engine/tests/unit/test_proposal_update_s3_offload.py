# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Manual proposal-edit path must offload Turtle to S3, not write it inline.

Regression guard for the DynamoDB 400 KB item-size class of bug: when a user
edits a proposal's ``ontology_turtle``/``r2rml_turtle`` directly,
``proposals.update_proposal`` must store the bodies in S3 and persist only the
S3 key on the item (``ontology_s3_key``/``r2rml_s3_key``). Writing the full
Turtle inline 500'd once ontologies grew past 400 KB — the same failure the
grounding-override path hit.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.config import Config
from coa_common.constants import GRAPH_BASE_URI
from coa_ontology import induce_catalog, proposals
from coa_ontology.inducer.strategies import table_to_ontology
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

# A tiny but valid Turtle doc so the rdflib parse-validation in the handler
# passes (invalid Turtle raises 422 before we reach the persistence path).
_VALID_TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<https://example.org/o> a owl:Ontology .
"""


def test_manual_turtle_edit_offloads_to_s3(monkeypatch):

    # Stub the store: a pending proposal exists; capture put_artifact + update.
    put_calls: list[tuple[str, str, str]] = []
    update_kwargs: dict = {}

    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            put_calls.append((artifact, content, version)) or f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"
        ),
    )

    def _capture_update(proposal_id, namespace="default", **fields):
        update_kwargs.update(fields)

    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", _capture_update)

    body = proposals.UpdateProposalRequest(
        proposal_id="p-edit",
        ontology_turtle=_VALID_TTL,
        r2rml_turtle=_VALID_TTL,
    )
    resp = proposals.update_proposal(body, namespace="ns")

    assert resp == {"proposal_id": "p-edit", "status": "updated"}
    # Both Turtle bodies were written to S3.
    artifacts = {artifact for artifact, _content, _version in put_calls}
    assert artifacts == {"ontology", "r2rml"}
    versions = {version for _artifact, _content, version in put_calls}
    assert len(versions) == 1
    assert next(iter(versions)).startswith("revisions/")
    # The DynamoDB write carries S3 KEYS, never the inline Turtle bodies.
    assert "ontology_s3_key" in update_kwargs
    assert "r2rml_s3_key" in update_kwargs
    assert "ontology_turtle" not in update_kwargs
    assert "r2rml_turtle" not in update_kwargs
    assert update_kwargs["status"] == "updated"


def test_manual_edit_no_longer_canonicalizes_datatypes(monkeypatch):
    """Editing a proposal persists the user's Turtle VERBATIM — the PATCH path
    must NOT silently rewrite malformed datatype tokens anymore. (Supersedes the
    old auto-canonicalize behavior; the DatatypeTokenValidator + consent-gated
    /repair-datatypes flow replaces it.) A lowercase xsd:datetime must survive
    unchanged in the stored artifact so the validator can flag it.
    """

    malformed_ttl = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<https://example.org/p> a owl:DatatypeProperty ; rdfs:range xsd:datetime .
"""
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            stored.update({artifact: content}) or f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"
        ),
    )
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", lambda pid, namespace="default", **f: None)

    body = proposals.UpdateProposalRequest(proposal_id="p-verbatim", ontology_turtle=malformed_ttl)
    proposals.update_proposal(body, namespace="ns")

    # The malformed token is stored VERBATIM — not rewritten to xsd:dateTime.
    assert stored["ontology"] == malformed_ttl
    assert "xsd:datetime" in stored["ontology"]


def test_invalid_turtle_rejected_before_persistence(monkeypatch):

    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    # If we reach persistence, fail loudly — invalid Turtle must 422 first.
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda *a, **k: pytest.fail("should not persist invalid Turtle"),
    )

    body = proposals.UpdateProposalRequest(proposal_id="p-bad", ontology_turtle="this is not turtle {{{")
    with pytest.raises(HTTPException) as exc:
        proposals.update_proposal(body, namespace="ns")
    assert exc.value.status_code == 422


def test_proposal_upload_presigner_binds_create_only_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    s3.generate_presigned_url.return_value = "https://s3.test/signed"
    monkeypatch.setattr(proposals.dynamo_store, "_get_s3", lambda: s3)
    monkeypatch.setattr(proposals.dynamo_store, "_S3_BUCKET", "proposal-artifacts")

    url = proposals.dynamo_store.presign_proposal_artifact_put(
        "ns",
        "p-upload",
        "r2rml",
        version="revisions/06d7f58f-bbfb-42a6-bca3-56e3c65ae159",
    )

    assert url == "https://s3.test/signed"
    s3.generate_presigned_url.assert_called_once_with(
        "put_object",
        Params={
            "Bucket": "proposal-artifacts",
            "Key": "proposals/ns/p-upload/revisions/06d7f58f-bbfb-42a6-bca3-56e3c65ae159/r2rml.ttl",
            "IfNoneMatch": "*",
        },
        ExpiresIn=600,
    )


def test_proposal_upload_presigner_signs_create_only_header(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = proposals.dynamo_store.boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="fake-access-key",
        aws_secret_access_key="fake-secret-key",
        config=Config(signature_version="s3v4"),
    )
    monkeypatch.setattr(proposals.dynamo_store, "_get_s3", lambda: s3)
    monkeypatch.setattr(proposals.dynamo_store, "_S3_BUCKET", "proposal-artifacts")

    url = proposals.dynamo_store.presign_proposal_artifact_put(
        "ns",
        "p-upload",
        version="revisions/06d7f58f-bbfb-42a6-bca3-56e3c65ae159",
    )

    query = parse_qs(urlsplit(url).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert "if-none-match" in query["X-Amz-SignedHeaders"][0].split(";")


def test_get_s3_forces_s3v4(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = MagicMock()
    create_client = MagicMock(return_value=s3)
    monkeypatch.setattr(proposals.dynamo_store.boto3, "client", create_client)
    monkeypatch.setattr(proposals.dynamo_store, "_s3", None)

    assert proposals.dynamo_store._get_s3() is s3
    config = create_client.call_args.kwargs["config"]
    assert config.signature_version == "s3v4"


def test_upload_urls_share_one_immutable_revision(monkeypatch):
    revision_id = "06d7f58f-bbfb-42a6-bca3-56e3c65ae159"
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default", hydrate_turtle=True: {
            "proposal_id": pid,
            "status": "pending",
        },
    )
    monkeypatch.setattr(proposals.uuid, "uuid4", lambda: proposals.uuid.UUID(revision_id))
    signed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        proposals.dynamo_store,
        "presign_proposal_artifact_put",
        lambda namespace, pid, artifact, version="latest": (
            signed.append((artifact, version)) or f"https://s3.test/{artifact}"
        ),
    )

    response = proposals.get_proposal_upload_urls("p-upload", namespace="ns")

    assert response.revision_id == revision_id
    assert signed == [
        ("ontology", f"revisions/{revision_id}"),
        ("r2rml", f"revisions/{revision_id}"),
    ]


def test_upload_url_route_serializes_exact_smithy_camel_case_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    revision_id = "06d7f58f-bbfb-42a6-bca3-56e3c65ae159"
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default", hydrate_turtle=True: {
            "proposal_id": pid,
            "status": "pending",
        },
    )
    monkeypatch.setattr(proposals.uuid, "uuid4", lambda: proposals.uuid.UUID(revision_id))
    monkeypatch.setattr(
        proposals.dynamo_store,
        "presign_proposal_artifact_put",
        lambda _namespace, _pid, artifact, version="latest": f"https://s3.test/{version}/{artifact}",
    )
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")

    with TestClient(app) as client:
        response = client.post("/ontology/proposals/p-upload/upload-url?namespace=ns")

    assert response.status_code == 200
    assert response.json() == {
        "revisionId": revision_id,
        "ontologyPutUrl": f"https://s3.test/revisions/{revision_id}/ontology",
        "r2rmlPutUrl": f"https://s3.test/revisions/{revision_id}/r2rml",
    }
    assert {"revision_id", "ontology_put_url", "r2rml_put_url"}.isdisjoint(response.json())


def test_uploaded_artifact_requires_upload_revision_id(monkeypatch):
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_get_artifact_s3",
        lambda *args, **kwargs: pytest.fail("must reject before reading S3"),
    )

    with pytest.raises(HTTPException, match="uploadRevisionId is required") as exc:
        proposals.update_proposal(
            proposals.UpdateProposalRequest(
                proposal_id="p-upload",
                ontology_uploaded=True,
            ),
            namespace="ns",
        )
    assert exc.value.status_code == 400


def test_uploaded_artifact_reads_and_publishes_exact_revision(monkeypatch):
    revision_id = "06d7f58f-bbfb-42a6-bca3-56e3c65ae159"
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    reads: list[tuple[str, str]] = []
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_get_artifact_s3",
        lambda namespace, pid, artifact, version="latest": reads.append((artifact, version)) or _VALID_TTL,
    )
    update_fields: dict[str, object] = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "update_proposal",
        lambda pid, namespace="default", **fields: update_fields.update(fields),
    )

    response = proposals.update_proposal(
        proposals.UpdateProposalRequest(
            proposal_id="p-upload",
            ontology_uploaded=True,
            upload_revision_id=revision_id,
        ),
        namespace="ns",
    )

    assert response["status"] == "updated"
    assert reads == [("ontology", f"revisions/{revision_id}")]
    assert update_fields["ontology_s3_key"] == (f"proposals/ns/p-upload/revisions/{revision_id}/ontology.ttl")
    assert update_fields["revision_id"] == revision_id


@pytest.mark.parametrize(
    ("r2rml_turtle", "expected_status", "expected_detail"),
    [
        pytest.param(None, 400, "no uploaded artifact", id="missing"),
        pytest.param("not valid Turtle {{{", 422, "Invalid R2RML Turtle", id="invalid"),
    ],
)
def test_uploaded_pair_rejects_invalid_second_artifact_before_dynamodb_update(
    monkeypatch: pytest.MonkeyPatch,
    r2rml_turtle: str | None,
    expected_status: int,
    expected_detail: str,
) -> None:
    revision_id = "06d7f58f-bbfb-42a6-bca3-56e3c65ae159"
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda pid, namespace="default": {"proposal_id": pid, "status": "pending"},
    )
    reads: list[tuple[str, str]] = []

    def get_uploaded_artifact(
        _namespace: str,
        _proposal_id: str,
        artifact: str,
        version: str = "latest",
    ) -> str | None:
        reads.append((artifact, version))
        return _VALID_TTL if artifact == "ontology" else r2rml_turtle

    monkeypatch.setattr(proposals.dynamo_store, "_get_artifact_s3", get_uploaded_artifact)
    update_proposal = MagicMock()
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", update_proposal)

    with pytest.raises(HTTPException) as exc:
        proposals.update_proposal(
            proposals.UpdateProposalRequest(
                proposal_id="p-upload",
                ontology_uploaded=True,
                r2rml_uploaded=True,
                upload_revision_id=revision_id,
            ),
            namespace="ns",
        )

    assert exc.value.status_code == expected_status
    assert expected_detail in str(exc.value.detail)
    assert reads == [
        ("ontology", f"revisions/{revision_id}"),
        ("r2rml", f"revisions/{revision_id}"),
    ]
    update_proposal.assert_not_called()


def test_unstructured_grounding_override_patches_turtle(monkeypatch):
    """An embedding-only alternate uses conservative RDF semantics."""

    cls = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#FinancialInstrument"
    old_target = "http://fibo/Agreement"
    new_target = "http://fibo/Commitment"
    induced_ttl = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{cls}> a owl:Class ; rdfs:label "FinancialInstrument" ;
    rdfs:subClassOf <{old_target}> ; skos:exactMatch <{old_target}> .
"""
    # Production ENHANCED shape: only the original LLM-selected candidate has
    # fused_score; the alternate carries embedding similarity alone.
    item = {
        "proposal_id": "p-uns",
        "status": "pending",
        "source_type": "UNSTRUCTURED",
        "ontology_id": f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#",
        "metadata": {
            "matches": [
                {
                    "source_table": "FinancialInstrument",
                    "source_column": "",
                    "matched_class_uri": old_target,
                    "matched_ontology_id": "fibo",
                    "similarity": 0.95,
                    "match_type": "exact",
                    "relationship": "exactMatch",
                    "scoring_strategy": "grounding_enhanced",
                    "reason": "Agreement is identical",
                    "candidates": [
                        {
                            "entity_uri": old_target,
                            "ontology_id": "fibo",
                            "lexical_sim": 0.725,
                            "fused_score": 0.95,
                        },
                        {
                            "entity_uri": new_target,
                            "ontology_id": "fibo",
                            "lexical_sim": 0.72,
                            "fused_score": None,
                        },
                    ],
                }
            ]
        },
    }
    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", lambda pid, namespace="default": item)
    monkeypatch.setattr(
        proposals.dynamo_store, "_get_artifact_s3", lambda namespace, pid, artifact, version="latest": induced_ttl
    )

    put_artifacts: dict[str, str] = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            put_artifacts.update({artifact: content}) or f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"
        ),
    )
    persisted_matches: list[dict[str, object]] = []

    def _capture_matches(pid, matches, namespace="default", version="latest"):
        persisted_matches.extend(matches)
        return f"proposals/{namespace}/{pid}/{version}/matches.json"

    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", _capture_matches)
    update_kwargs: dict = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "update_proposal",
        lambda pid, namespace="default", **f: update_kwargs.update(f),
    )

    body = proposals.UpdateProposalRequest(
        proposal_id="p-uns",
        grounding_overrides={"FinancialInstrument": new_target},
    )
    resp = proposals.update_proposal(body, namespace="ns")

    assert resp == {"proposal_id": "p-uns", "status": "updated"}
    # The patched ontology Turtle was written to S3 (no r2rml for unstructured).
    assert "ontology" in put_artifacts
    assert "r2rml" not in put_artifacts
    patched = put_artifacts["ontology"]
    triples = {
        (str(subject), str(predicate), str(obj))
        for subject, predicate, obj in proposals.Graph().parse(data=patched, format="turtle")
    }
    skos = "http://www.w3.org/2004/02/skos/core#"
    rdfs = "http://www.w3.org/2000/01/rdf-schema#"
    # Embedding score 0.72 is ambiguous, so fallback emits relatedMatch only.
    # It must not retain exact semantics or assert a subclass relationship.
    assert (cls, f"{skos}relatedMatch", new_target) in triples
    assert (cls, f"{skos}closeMatch", new_target) not in triples
    assert (cls, f"{skos}exactMatch", new_target) not in triples
    assert (cls, f"{rdfs}subClassOf", new_target) not in triples
    assert old_target not in patched

    persisted = next(match for match in persisted_matches if match["source_table"] == "FinancialInstrument")
    assert persisted["matched_class_uri"] == new_target
    assert persisted["match_type"] == "ambiguous"
    assert persisted["similarity"] == 0.72
    assert persisted["relationship"] is None
    assert persisted["reason"] is None
    # Persisted keys, not inline bodies.
    assert update_kwargs["status"] == "updated"
    assert "ontology_s3_key" in update_kwargs
    assert "matches_s3_key" in update_kwargs


def _class_match(source_table: str = "orders") -> dict[str, object]:
    return {
        "source_table": source_table,
        "source_column": "",
        "matched_class_uri": None,
        "matched_ontology_id": None,
        "similarity": None,
        "match_type": "novel",
    }


def _grounding_item(
    matches: list[dict[str, object]],
    *,
    source_type: str = "STRUCTURED",
    ontology_id: str = "http://example.org/ind#",
) -> dict[str, object]:
    return {
        "proposal_id": "p-edit",
        "status": "pending",
        "source_type": source_type,
        "ontology_id": ontology_id,
        "metadata": {"datasource_ids": ["ds-1"], "matches": matches},
    }


def _assert_override_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    item: dict[str, object],
    overrides: dict[str, str | None],
    expected_detail: str,
) -> None:
    def unexpected_side_effect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("override validation must finish before catalog or persistence side effects")

    monkeypatch.setattr(induce_catalog, "_fetch_catalog", unexpected_side_effect)
    monkeypatch.setattr(induce_catalog, "_fetch_catalog_from_smus", unexpected_side_effect)
    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", unexpected_side_effect)
    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", unexpected_side_effect)
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", unexpected_side_effect)

    with pytest.raises(HTTPException) as exc:
        proposals._apply_grounding_overrides("p-invalid", overrides, item, "ns")

    assert exc.value.status_code == 409
    assert expected_detail in str(exc.value.detail)


def test_grounding_override_unknown_key_rejected_before_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_override_rejected_before_side_effects(
        monkeypatch,
        _grounding_item([_class_match()]),
        {"missing": None},
        "does not identify",
    )


def test_grounding_override_unknown_identity_key_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keys are stable table identities (bare names only while unique). An
    # identity that names no match fails closed.
    match = _class_match()
    match["source_table_identity"] = "DS#ds-1::db.orders"
    _assert_override_rejected_before_side_effects(
        monkeypatch,
        _grounding_item([match]),
        {"DS#ds-2::db.orders": None},
        "does not identify",
    )


def _same_named_class_matches() -> list[dict[str, object]]:
    first = _class_match()
    first["source_table_identity"] = "DS#ds-1::db.orders"
    second = _class_match()
    second["source_table_identity"] = "DS#ds-2::db.orders"
    return [first, second]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"orders": "https://schema.org/Order"}, id="override"),
        pytest.param({"orders": None}, id="novel"),
        pytest.param(
            {"DS#ds-1::db.orders": None, "orders": None},
            id="alongside-a-valid-identity-key",
        ),
    ],
)
def test_grounding_override_ambiguous_bare_key_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str | None],
) -> None:
    # A bare name two datasources share cannot say which table to update, so
    # the whole request is rejected before the catalog is read or anything is
    # written.
    _assert_override_rejected_before_side_effects(
        monkeypatch,
        _grounding_item(_same_named_class_matches()),
        overrides,
        "'orders' is ambiguous",
    )


def test_update_route_returns_409_for_an_ambiguous_bare_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route surfaces the conflict instead of a 200 that saved nothing."""
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda _proposal_id, namespace="default": _grounding_item(_same_named_class_matches()),
    )

    def unexpected_side_effect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an ambiguous override must be rejected before catalog or persistence side effects")

    monkeypatch.setattr(induce_catalog, "_fetch_catalog", unexpected_side_effect)
    monkeypatch.setattr(induce_catalog, "_fetch_catalog_from_smus", unexpected_side_effect)
    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", unexpected_side_effect)
    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", unexpected_side_effect)
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", unexpected_side_effect)
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")

    with TestClient(app) as client:
        response = client.post(
            "/ontology/proposals/update?namespace=ns",
            json={"proposalId": "p-edit", "novelTables": ["orders"]},
        )

    assert response.status_code == 409
    assert "'orders' is ambiguous" in response.text


def test_grounding_override_partial_datasource_failure_aborts_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _grounding_item([_class_match()])
    metadata = item["metadata"]
    assert isinstance(metadata, dict)
    metadata["datasource_ids"] = ["ds-ok", "ds-fail"]
    monkeypatch.setenv("CATALOG_SOURCE", "http")

    fetch_catalog = MagicMock(side_effect=[{"tables": []}, RuntimeError("catalog unavailable")])
    monkeypatch.setattr(induce_catalog, "_fetch_catalog", fetch_catalog)
    monkeypatch.setattr(
        induce_catalog,
        "_catalog_to_tables",
        lambda _catalog: [{"id": "t-1", "name": "orders", "fullyQualifiedName": "public.orders"}],
    )
    strategy_factory = MagicMock()
    monkeypatch.setattr(table_to_ontology, "TableToOntologyStrategy", strategy_factory)
    put_artifact = MagicMock()
    put_matches = MagicMock()
    publish = MagicMock()
    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", put_artifact)
    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", put_matches)
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", publish)

    with pytest.raises(HTTPException) as exc:
        proposals._apply_grounding_overrides("p-edit", {"orders": None}, item, "ns")

    assert exc.value.status_code == 503
    assert "ds-fail" in str(exc.value.detail)
    assert [call.args[1] for call in fetch_catalog.call_args_list] == ["ds-ok", "ds-fail"]
    strategy_factory.assert_not_called()
    put_artifact.assert_not_called()
    put_matches.assert_not_called()
    publish.assert_not_called()


def test_grounding_override_ambiguous_unstructured_join_aborts_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_target = "http://fibo/Agreement"
    new_target = "http://fibo/Commitment"
    uri_prefix = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#"
    first = f"{uri_prefix}First"
    second = f"{uri_prefix}Second"
    induced_ttl = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{first}> a owl:Class ; rdfs:label "A-B" ; skos:closeMatch <{old_target}> .
<{second}> a owl:Class ; rdfs:label "AB" ; skos:closeMatch <{old_target}> .
"""
    match = {
        "source_table": "A B",
        "source_column": "",
        "matched_class_uri": old_target,
        "matched_ontology_id": "fibo",
        "similarity": 0.9,
        "match_type": "high_confidence",
        "candidates": [{"entity_uri": new_target, "ontology_id": "fibo", "lexical_sim": 0.9}],
    }
    item = _grounding_item([match], source_type="UNSTRUCTURED", ontology_id=uri_prefix)
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_get_artifact_s3",
        lambda _namespace, _proposal_id, _artifact, version="latest": induced_ttl,
    )
    put_artifact = MagicMock()
    put_matches = MagicMock()
    publish = MagicMock()
    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", put_artifact)
    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", put_matches)
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", publish)

    with pytest.raises(HTTPException) as exc:
        proposals._apply_grounding_overrides("p-ambiguous", {"A B": new_target}, item, "ns")

    assert exc.value.status_code == 409
    assert "matches 2 classes" in str(exc.value.detail)
    put_artifact.assert_not_called()
    put_matches.assert_not_called()
    publish.assert_not_called()


def test_grounding_override_409_names_the_class_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch override must report which class aborted it, not just the cause.

    One save can carry overrides for many classes. Every ``ValueError`` inside
    ``apply_overrides_to_turtle`` already interpolates the offending
    ``source_table``, and the 409 interpolates the whole exception, so the label
    survives to the client. Without this assertion the label could be dropped
    from either end and the only symptom would be a support ticket that says a
    save failed somewhere in a batch.
    """
    target = "http://fibo/Commitment"
    uri_prefix = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#"
    # Only the first label exists as a class, so the second override aborts.
    induced_ttl = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{uri_prefix}Invoice> a owl:Class ; rdfs:label "Invoice" .
"""
    candidates = [{"entity_uri": target, "ontology_id": "fibo", "lexical_sim": 0.9}]
    matches = [
        {
            "source_table": label,
            "source_column": "",
            "matched_class_uri": None,
            "matched_ontology_id": None,
            "similarity": None,
            "match_type": "novel",
            "candidates": candidates,
        }
        for label in ("Invoice", "請求明細")
    ]
    item = _grounding_item(matches, source_type="UNSTRUCTURED", ontology_id=uri_prefix)
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_get_artifact_s3",
        lambda _namespace, _proposal_id, _artifact, version="latest": induced_ttl,
    )
    put_artifact = MagicMock()
    put_matches = MagicMock()
    publish = MagicMock()
    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", put_artifact)
    monkeypatch.setattr(proposals.dynamo_store, "put_proposal_matches_s3", put_matches)
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", publish)

    with pytest.raises(HTTPException) as exc:
        proposals._apply_grounding_overrides("p-batch", {"Invoice": target, "請求明細": target}, item, "ns")

    detail = str(exc.value.detail)
    assert exc.value.status_code == 409
    assert "請求明細" in detail
    assert "no class matching" in detail
    put_artifact.assert_not_called()
    put_matches.assert_not_called()
    publish.assert_not_called()


def test_mixed_turtle_and_grounding_update_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda _proposal_id, namespace="default": _grounding_item([_class_match()]),
    )
    apply_overrides = MagicMock()
    put_artifact = MagicMock()
    publish = MagicMock()
    monkeypatch.setattr(proposals, "_apply_grounding_overrides", apply_overrides)
    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", put_artifact)
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", publish)

    body = proposals.UpdateProposalRequest(proposal_id="p-edit", ontology_turtle=_VALID_TTL, novel_tables=["orders"])
    with pytest.raises(HTTPException) as exc:
        proposals.update_proposal(body, namespace="ns")

    assert exc.value.status_code == 400
    apply_overrides.assert_not_called()
    put_artifact.assert_not_called()
    publish.assert_not_called()


def test_update_request_binds_all_smithy_camel_case_fields() -> None:
    body = proposals.UpdateProposalRequest.model_validate(
        {
            "proposalId": "p-edit",
            "ontologyTurtle": _VALID_TTL,
            "r2rmlTurtle": _VALID_TTL,
            "groundingOverrides": {"orders": "https://schema.org/Order"},
            "novelTables": ["customers"],
            "ontologyUploaded": True,
            "r2rmlUploaded": True,
            "uploadRevisionId": "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8",
        }
    )

    assert body.proposal_id == "p-edit"
    assert body.ontology_turtle == _VALID_TTL
    assert body.r2rml_turtle == _VALID_TTL
    assert body.grounding_overrides == {"orders": "https://schema.org/Order"}
    assert body.novel_tables == ["customers"]
    assert body.grounding_choices == {
        "orders": "https://schema.org/Order",
        "customers": None,
    }
    assert body.ontology_uploaded is True
    assert body.r2rml_uploaded is True
    assert body.upload_revision_id == "d3bc58e9-52a4-43cf-b8b2-44a2f05b43f8"


def test_update_route_returns_modeled_service_unavailable_body(monkeypatch: pytest.MonkeyPatch) -> None:
    expected_message = "Could not fetch catalog for datasource 'ds-1'; no grounding changes were saved"
    monkeypatch.setattr(
        proposals.dynamo_store,
        "get_proposal_by_id",
        lambda _proposal_id, namespace="default": _grounding_item([_class_match()]),
    )

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise HTTPException(503, expected_message)

    monkeypatch.setattr(proposals, "_apply_grounding_overrides", unavailable)
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")

    with TestClient(app) as client:
        response = client.post(
            "/ontology/proposals/update?namespace=ns",
            json={"proposalId": "p-edit", "novelTables": ["orders"]},
        )

    assert response.status_code == 503
    assert response.json() == {"message": expected_message}


def test_update_route_rejects_null_override_and_points_to_novel_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the migration documented under "Overriding grounding choices".

    main accepted ``groundingOverrides={"orders": null}`` as "mark novel". The
    route now requires ``novelTables`` for that and rejects the null value at
    request validation, before any proposal state is read.
    """

    def must_not_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a rejected request must not read the proposal")

    monkeypatch.setattr(proposals.dynamo_store, "get_proposal_by_id", must_not_read)
    app = FastAPI()
    app.include_router(proposals.router, prefix="/ontology/proposals")

    with TestClient(app) as client:
        response = client.post(
            "/ontology/proposals/update?namespace=ns",
            json={"proposalId": "p-edit", "groundingOverrides": {"orders": None}},
        )

    assert response.status_code == 422
    assert any(error["loc"][-2:] == ["groundingOverrides", "orders"] for error in response.json()["detail"])


@pytest.mark.parametrize(
    "invalid_uri",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("relative/class", id="relative"),
        pytest.param("https://example.org/Class\nInjected", id="non-printable"),
        pytest.param(f"https://example.org/{'x' * 2040}", id="too-long"),
    ],
)
def test_grounding_override_invalid_uri_rejected_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    invalid_uri: str,
) -> None:
    _assert_override_rejected_before_side_effects(
        monkeypatch,
        _grounding_item([_class_match()]),
        {"orders": invalid_uri},
        "must be an HTTP(S) URI",
    )


def test_generated_update_contract_represents_mark_novel_without_null_map_values() -> None:
    from coa_control_plane_server.models.update_proposal_request_content import (
        UpdateProposalRequestContent,
    )
    from pydantic import ValidationError

    proposal_id = "7c2e9a1b-4d6f-4b8a-9e3c-1f0a2b5d7c9e"
    generated = UpdateProposalRequestContent.model_validate(
        {
            "proposalId": proposal_id,
            "groundingOverrides": {"orders": "https://schema.org/Order"},
            "novelTables": ["customers"],
        }
    )
    assert generated.grounding_overrides == {"orders": "https://schema.org/Order"}
    assert generated.novel_tables == ["customers"]

    with pytest.raises(ValidationError):
        UpdateProposalRequestContent.model_validate(
            {
                "proposalId": proposal_id,
                "groundingOverrides": {"orders": None},
            }
        )


def test_grounding_override_rejects_uri_outside_stored_candidates_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _class_match()
    match["candidates"] = [
        {
            "entity_uri": "https://schema.org/Order",
            "ontology_id": "schema",
            "lexical_sim": 0.9,
        }
    ]
    _assert_override_rejected_before_side_effects(
        monkeypatch,
        _grounding_item([match]),
        {"orders": "https://attacker.example/Injected"},
        "must select one of",
    )


def test_unstructured_override_ignores_stale_untouched_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uri_prefix = "https://example.org/ind#"
    order_class = f"{uri_prefix}Order"
    target = "https://schema.org/Order"
    turtle = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{order_class}> a owl:Class ; rdfs:label "orders" ;
    rdfs:subClassOf <{target}> ; skos:closeMatch <{target}> .
"""
    order_match = {
        "source_table": "orders",
        "source_column": "",
        "matched_class_uri": target,
        "matched_ontology_id": "schema",
        "similarity": 0.9,
        "match_type": "high_confidence",
        "candidates": [{"entity_uri": target, "ontology_id": "schema", "lexical_sim": 0.9}],
    }
    stale_match = _class_match("historical_deleted_class")
    item = _grounding_item([order_match, stale_match], source_type="UNSTRUCTURED", ontology_id=uri_prefix)
    item["ontology_turtle"] = turtle

    persisted_matches: list[dict[str, object]] = []
    monkeypatch.setattr(
        proposals.dynamo_store,
        "put_proposal_matches_s3",
        lambda _pid, matches, namespace="default", version="latest": (
            persisted_matches.extend(matches) or f"proposals/{namespace}/p/{version}/matches.json"
        ),
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"
        ),
    )
    update_kwargs: dict[str, object] = {}
    monkeypatch.setattr(
        proposals.dynamo_store,
        "update_proposal",
        lambda _pid, namespace="default", **fields: update_kwargs.update(fields),
    )

    result = proposals._apply_grounding_overrides("p-edit", {"orders": None}, item, "ns")

    assert result["status"] == "updated"
    assert next(m for m in persisted_matches if m["source_table"] == "orders")["match_type"] == "novel"
    assert any(m["source_table"] == "historical_deleted_class" for m in persisted_matches)
    assert update_kwargs["status"] == "updated"


def test_unstructured_override_ignores_ambiguous_untouched_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uri_prefix = "https://example.org/ind#"
    old_target = "https://schema.org/Thing"
    customer_target = "https://schema.org/Customer"
    turtle = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{uri_prefix}First> a owl:Class ; rdfs:label "A-B" ;
    skos:closeMatch <{old_target}> .
<{uri_prefix}Second> a owl:Class ; rdfs:label "AB" .
<{uri_prefix}Customer> a owl:Class ; rdfs:label "Customer" .
"""
    ambiguous_untouched_match = {
        "source_table": "A B",
        "source_column": "",
        "matched_class_uri": old_target,
        "matched_ontology_id": "schema",
        "similarity": 0.9,
        "match_type": "high_confidence",
        "candidates": [],
    }
    customer_match = {
        "source_table": "Customer",
        "source_column": "",
        "matched_class_uri": None,
        "matched_ontology_id": None,
        "similarity": None,
        "match_type": "novel",
        "candidates": [
            {
                "entity_uri": customer_target,
                "ontology_id": "schema",
                "lexical_sim": 0.9,
            }
        ],
    }
    item = _grounding_item(
        [ambiguous_untouched_match, customer_match],
        source_type="UNSTRUCTURED",
        ontology_id=uri_prefix,
    )
    item["ontology_turtle"] = turtle

    persisted_matches: list[dict[str, object]] = []
    written_turtle: list[str] = []
    monkeypatch.setattr(
        proposals.dynamo_store,
        "put_proposal_matches_s3",
        lambda _pid, matches, namespace="default", version="latest": (
            persisted_matches.extend(matches) or f"proposals/{namespace}/p/{version}/matches.json"
        ),
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "_put_artifact_s3",
        lambda namespace, pid, artifact, content, version="latest": (
            written_turtle.append(content) or f"proposals/{namespace}/{pid}/{version}/{artifact}.ttl"
        ),
    )
    monkeypatch.setattr(
        proposals.dynamo_store,
        "update_proposal",
        lambda _pid, namespace="default", **_fields: None,
    )

    result = proposals._apply_grounding_overrides(
        "p-edit",
        {"Customer": customer_target},
        item,
        "ns",
    )

    assert result["status"] == "updated"
    assert any(match["source_table"] == "A B" for match in persisted_matches)
    graph = proposals.Graph().parse(data=written_turtle[0], format="turtle")
    assert any(str(obj) == customer_target for obj in graph.objects())


def test_unstructured_override_s3_failure_never_replaces_active_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uri_prefix = "https://example.org/ind#"
    target = "https://schema.org/Order"
    turtle = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<{uri_prefix}Order> a owl:Class ; rdfs:label "orders" .
"""
    match = _class_match()
    match["candidates"] = [
        {
            "entity_uri": target,
            "ontology_id": "schema",
            "lexical_sim": 0.9,
        }
    ]
    item = _grounding_item([match], source_type="UNSTRUCTURED", ontology_id=uri_prefix)
    item.update(
        {
            "ontology_turtle": turtle,
            "ontology_s3_key": "proposals/ns/p-edit/latest/ontology.ttl",
            "matches_s3_key": "proposals/ns/p-edit/latest/matches.json",
        }
    )
    active_pointers = {
        "ontology_s3_key": item["ontology_s3_key"],
        "matches_s3_key": item["matches_s3_key"],
    }
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        proposals.dynamo_store,
        "put_proposal_matches_s3",
        lambda _pid, _matches, namespace="default", version="latest": (
            writes.append(("matches", version)) or f"proposals/{namespace}/p-edit/{version}/matches.json"
        ),
    )

    def fail_ontology_write(
        _namespace: str,
        _proposal_id: str,
        artifact: str,
        _content: str,
        version: str = "latest",
    ) -> str:
        writes.append((artifact, version))
        raise RuntimeError("simulated ontology write failure")

    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", fail_ontology_write)
    publish = MagicMock()
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", publish)

    with pytest.raises(RuntimeError, match="simulated ontology write failure"):
        proposals._apply_grounding_overrides("p-edit", {"orders": target}, item, "ns")

    publish.assert_not_called()
    versions = {version for _artifact, version in writes}
    assert len(versions) == 1
    assert next(iter(versions)).startswith("revisions/")
    assert item["ontology_s3_key"] == active_pointers["ontology_s3_key"]
    assert item["matches_s3_key"] == active_pointers["matches_s3_key"]


def test_structured_override_s3_failure_never_replaces_active_pointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "https://schema.org/Order"
    match = _class_match()
    match["candidates"] = [{"entity_uri": target, "ontology_id": "schema", "lexical_sim": 0.9}]
    item = _grounding_item([match])
    item.update(
        {
            "ontology_s3_key": "proposals/ns/p-edit/latest/ontology.ttl",
            "r2rml_s3_key": "proposals/ns/p-edit/latest/r2rml.ttl",
            "matches_s3_key": "proposals/ns/p-edit/latest/matches.json",
        }
    )
    monkeypatch.setenv("CATALOG_SOURCE", "http")
    monkeypatch.setattr(induce_catalog, "_fetch_catalog", lambda *_args, **_kwargs: {"tables": []})
    monkeypatch.setattr(
        induce_catalog,
        "_catalog_to_tables",
        lambda _catalog: [{"id": "t-1", "name": "orders", "fullyQualifiedName": "public.orders"}],
    )

    class _Graph:
        def serialize(self, format="turtle"):
            return _VALID_TTL

    strategy = MagicMock()
    strategy._build_proposal_ontology.return_value = (_Graph(), set())
    strategy.build_r2rml.return_value = _Graph()
    monkeypatch.setattr(table_to_ontology, "TableToOntologyStrategy", lambda: strategy)

    writes: list[tuple[str, str]] = []

    def put_artifact(_namespace, _pid, artifact, _content, version="latest"):
        writes.append((artifact, version))
        if artifact == "r2rml":
            raise RuntimeError("simulated S3 failure")
        return f"proposals/ns/p-edit/{version}/{artifact}.ttl"

    monkeypatch.setattr(proposals.dynamo_store, "_put_artifact_s3", put_artifact)
    monkeypatch.setattr(
        proposals.dynamo_store,
        "put_proposal_matches_s3",
        lambda _pid, _matches, namespace="default", version="latest": (
            writes.append(("matches", version)) or f"proposals/{namespace}/p-edit/{version}/matches.json"
        ),
    )
    publish = MagicMock()
    monkeypatch.setattr(proposals.dynamo_store, "update_proposal", publish)

    with pytest.raises(RuntimeError, match="simulated S3 failure"):
        proposals._apply_grounding_overrides("p-edit", {"orders": target}, item, "ns")

    publish.assert_not_called()
    versions = {version for _artifact, version in writes}
    assert len(versions) == 1
    assert next(iter(versions)).startswith("revisions/")
