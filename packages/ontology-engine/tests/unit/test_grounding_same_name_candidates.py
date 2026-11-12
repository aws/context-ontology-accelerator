# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""ENHANCED grounding when candidates from different ontologies share a name."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from coa_ontology import dynamo_store
from coa_ontology.inducer.services.grounding import GroundingCandidate, GroundingService
from coa_ontology.inducer.unstructured.services.class_grounding import ClassGroundingInput, ground_classes

pytestmark = pytest.mark.unit

_FOAF_PERSON = "http://xmlns.com/foaf/0.1/Person"
_SCHEMA_PERSON = "https://schema.org/Person"


def _candidates() -> list[GroundingCandidate]:
    return [
        GroundingCandidate(
            entity_uri=_FOAF_PERSON,
            ontology_id="http://xmlns.com/foaf/0.1/",
            label="Person",
            definition="A person.",
            lexical_sim=0.71,
        ),
        GroundingCandidate(
            entity_uri=_SCHEMA_PERSON,
            ontology_id="https://schema.org/",
            label="Person",
            definition="A person (alive, dead, undead, or fictional).",
            lexical_sim=0.70,
        ),
        GroundingCandidate(
            entity_uri="https://schema.org/Organization",
            ontology_id="https://schema.org/",
            label="Organization",
            definition="An organization such as a school or corporation.",
            lexical_sim=0.55,
        ),
    ]


def _ground(choice: object, captured: dict | None = None, candidates: list[GroundingCandidate] | None = None):
    captured = {} if captured is None else captured

    def converse(**kwargs):
        captured["prompt"] = kwargs["messages"][0]["content"][0]["text"]
        body = {"choice": choice, "relationship": "exactMatch", "confidence": 0.9, "reason": "people records"}
        return {"output": {"message": {"content": [{"text": json.dumps(body)}]}}}

    service = GroundingService(ontology_catalog=MagicMock(), embedding_generator=MagicMock())
    with (
        patch.object(service, "_recall", return_value=candidates or _candidates()),
        patch.object(type(service), "bedrock", property(lambda self: MagicMock(converse=converse))),
    ):
        return service.ground_table(
            table_name="person",
            table_description="",
            columns=[{"name": "full_name", "dataType": "string"}],
            concept_vector=[0.1],
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["http://xmlns.com/foaf/0.1/", "https://schema.org/"],
        )


def test_rerank_prompt_lists_each_candidate_iri():
    captured: dict = {}
    _ground("NONE", captured)

    assert f"<{_FOAF_PERSON}>" in captured["prompt"]
    assert f"<{_SCHEMA_PERSON}>" in captured["prompt"]


@pytest.mark.parametrize("choice", [_SCHEMA_PERSON, f"<{_SCHEMA_PERSON}>"])
def test_ground_table_iri_choice_resolves_same_name_candidate(choice):
    result = _ground(choice)

    assert result.chosen is not None
    assert result.chosen.entity_uri == _SCHEMA_PERSON
    assert result.match_type == "exact"


def test_ground_table_miscopied_iri_of_shared_name_keeps_its_vocabulary():
    result = _ground("http://schema.org/Person")

    assert result.chosen is not None
    assert result.chosen.entity_uri == _SCHEMA_PERSON


def test_ground_table_miscopied_iri_never_swaps_vocabulary():
    """With only FOAF's Organization recalled, a schema.org answer is not FOAF's class."""
    foaf_org = GroundingCandidate(
        entity_uri="http://xmlns.com/foaf/0.1/Organization",
        ontology_id="http://xmlns.com/foaf/0.1/",
        label="Organization",
        definition="An organization.",
        lexical_sim=0.6,
    )
    candidates = [c for c in _candidates() if c.label != "Organization"] + [foaf_org]
    result = _ground("https://schema.org/Organization", candidates=candidates)

    assert result.chosen is None
    assert result.match_type == "novel"


def test_ground_table_null_choice_is_not_in_candidates():
    result = _ground(None)

    assert result.chosen is None
    assert result.match_type == "novel"
    assert "not in candidates" in (result.reason or "")


def test_ground_table_tie_lends_the_capped_verdict_only_to_tied_candidates():
    scores = {c.entity_uri: c.rerank_score for c in _ground("Person").candidates}

    assert scores == {_FOAF_PERSON: 0.84, _SCHEMA_PERSON: 0.84, "https://schema.org/Organization": None}


def _person(uri: str, ontology_id: str, sim: float = 0.6) -> GroundingCandidate:
    return GroundingCandidate(entity_uri=uri, ontology_id=ontology_id, label="Person", definition="", lexical_sim=sim)


def test_ground_table_tied_candidate_recalled_past_the_tenth_is_listed_first():
    """Only the first ten candidates are persisted, so a tied one recalled at position 12 must move up."""
    fillers = [
        GroundingCandidate(
            entity_uri=f"https://schema.org/Thing{i}",
            ontology_id="https://schema.org/",
            label=f"Thing{i}",
            definition="",
            lexical_sim=0.6 - i * 0.01,
        )
        for i in range(10)
    ]
    candidates = [
        _person(_FOAF_PERSON, "http://xmlns.com/foaf/0.1/", 0.7),
        *fillers,
        _person(_SCHEMA_PERSON, "https://schema.org/", 0.4),
    ]
    result = _ground("Person", candidates=candidates)

    assert result.match_type == "ambiguous"
    assert {c.entity_uri for c in result.candidates[:2]} == {_FOAF_PERSON, _SCHEMA_PERSON}


@pytest.mark.parametrize("answer", [_FOAF_PERSON, "Person"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ground_table_one_iri_under_two_ontologies_credits_its_own_vocabulary(answer, reverse):
    """foaf:Person cataloged under schema.org by an older load and under FOAF resolves to FOAF's copy."""
    candidates = [
        _person(_FOAF_PERSON, "https://schema.org/", 0.7),
        _person(_FOAF_PERSON, "http://xmlns.com/foaf/0.1/", 0.6),
    ]
    if reverse:
        candidates.reverse()
    result = _ground(answer, candidates=candidates)

    assert result.chosen is not None
    assert result.chosen.entity_uri == _FOAF_PERSON
    assert result.chosen.ontology_id == "http://xmlns.com/foaf/0.1/"


@pytest.mark.parametrize(
    ("iri", "credited", "other"),
    [
        (_FOAF_PERSON, "http://xmlns.com/foaf/0.1/", "https://catalog.example.org/imported/vocabularies/"),
        ("http://www.w3.org/2006/time#Instant", "http://www.w3.org/2006/time", "https://catalog.example.org/vocabs/"),
        ("http://www.w3.org/2006/time/Instant", "http://www.w3.org/2006/time", "https://catalog.example.org/vocabs/"),
        ("http://example.org/ontology2#C", "https://catalog.example.org/vocabs/", "http://example.org/onto"),
    ],
)
def test_ground_table_same_iri_is_credited_to_its_owning_vocabulary(iri, credited, other):
    """The owning ontology is credited, even over a longer id; an owning id must end at a separator."""
    result = _ground(iri, candidates=[_person(iri, other, 0.7), _person(iri, credited, 0.6)])

    assert result.chosen is not None
    assert result.chosen.ontology_id == credited


def test_recall_merges_same_iri_copies_under_the_owning_ontology():
    """Dense recall merges copies of one IRI: best score kept, owning ontology credited."""
    hits = [
        {"entity_uri": _FOAF_PERSON, "ontology_id": "https://schema.org/", "score": 0.81},
        {"entity_uri": _FOAF_PERSON, "ontology_id": "http://xmlns.com/foaf/0.1/", "score": 0.79},
    ]
    catalog = MagicMock()
    catalog.search_embeddings.side_effect = lambda **kw: [h for h in hits if h["ontology_id"] == kw["ontology_id"]]
    catalog.list_embeddings_for_ontology.return_value = []
    service = GroundingService(ontology_catalog=catalog, embedding_generator=MagicMock())

    candidates = service._recall([0.1], "titan", ["https://schema.org/", "http://xmlns.com/foaf/0.1/"], top_k=10)

    person = [c for c in candidates if c.entity_uri == _FOAF_PERSON]
    assert len(person) == 1
    assert person[0].ontology_id == "http://xmlns.com/foaf/0.1/"
    assert person[0].lexical_sim == 0.81


_NESTED_OWNER_CASES = [
    pytest.param(
        "http://example.org/onto",
        "http://example.org/onto/Person",
        "http://example.org/onto#Person",
        id="hash-sibling",
    ),
    pytest.param(
        "http://example.org/onto/",
        "http://example.org/onto/Person",
        "http://example.org/onto/nested/Person",
        id="slash-sibling",
    ),
    pytest.param(
        "http://example.org/onto/nested",
        "http://example.org/onto/nested/Person",
        "http://example.org/onto/nested#Person",
        id="nested-owner",
    ),
]


def _owner_and_importer_recall(owner: str, imported: str, sibling: str) -> GroundingService:
    """``imported`` is recalled under its owner and, scoring higher, under an importer; ``sibling`` under the owner."""
    importer = "https://catalog.example.org/vocabs/"
    hits = [
        {"entity_uri": imported, "ontology_id": importer, "score": 0.81},
        {"entity_uri": imported, "ontology_id": owner, "score": 0.79},
        {"entity_uri": sibling, "ontology_id": owner, "score": 0.80},
    ]
    catalog = MagicMock()
    catalog.search_embeddings.side_effect = lambda **kw: [h for h in hits if h["ontology_id"] == kw["ontology_id"]]
    catalog.list_embeddings_for_ontology.return_value = []
    return GroundingService(ontology_catalog=catalog, embedding_generator=MagicMock())


@pytest.mark.parametrize(("owner", "imported", "sibling"), _NESTED_OWNER_CASES)
def test_recall_keeps_a_same_name_sibling_of_an_iri_credited_to_its_owner(owner, imported, sibling):
    service = _owner_and_importer_recall(owner, imported, sibling)

    candidates = service._recall([0.1], "titan", ["https://catalog.example.org/vocabs/", owner], top_k=10)

    assert sorted(c.entity_uri for c in candidates) == sorted([imported, sibling])


@pytest.mark.parametrize(("owner", "imported", "sibling"), _NESTED_OWNER_CASES)
def test_ground_table_name_answer_stays_tied_when_one_iri_is_credited_to_its_owner(owner, imported, sibling):
    """A bare "Person" answer names both IRIs, so neither is chosen and the tie stays below high confidence."""
    service = _owner_and_importer_recall(owner, imported, sibling)
    body = {"choice": "Person", "relationship": "exactMatch", "confidence": 0.8, "reason": "people records"}
    converse = MagicMock(return_value={"output": {"message": {"content": [{"text": json.dumps(body)}]}}})
    with patch.object(type(service), "bedrock", property(lambda self: MagicMock(converse=converse))):
        result = service.ground_table(
            table_name="person",
            table_description="",
            columns=[{"name": "full_name", "dataType": "string"}],
            concept_vector=[0.1],
            model_id="titan",
            grounding_mode="ENHANCED",
            ontology_ids=["https://catalog.example.org/vocabs/", owner],
        )

    assert result.chosen is None
    assert result.match_type == "ambiguous"
    assert {c.entity_uri for c in result.candidates} == {imported, sibling}


def _refuse_nonfinite(constant: str):
    raise ValueError(f"matches.json holds {constant}, which a browser's JSON.parse rejects")


@pytest.mark.parametrize("raw_confidence", ["-1e999", "NaN"])
def test_stored_matches_of_an_unstructured_tie_are_strict_json(raw_confidence, monkeypatch):
    """A raw rerank confidence that parses to a non-finite float must not reach the stored matches.json."""
    raw = f'{{"choice": "Person", "relationship": "exactMatch", "confidence": {raw_confidence}, "reason": "people"}}'
    converse = MagicMock(return_value={"output": {"message": {"content": [{"text": raw}]}}})
    service = GroundingService(ontology_catalog=MagicMock(), embedding_generator=MagicMock())
    stored: dict = {}
    s3 = MagicMock(put_object=lambda **kw: stored.update(kw))
    monkeypatch.setattr(dynamo_store, "_get_s3", lambda: s3)
    with (
        patch.object(service, "_recall", return_value=_candidates()),
        patch.object(type(service), "bedrock", property(lambda self: MagicMock(converse=converse))),
    ):
        _graph, _grounded, matches = ground_classes(
            [ClassGroundingInput(class_iri="https://ex.test/Person", label="Person", description="", vector=[0.1])],
            grounding_service=service,
            ontology_ids=["http://xmlns.com/foaf/0.1/", "https://schema.org/"],
            model_id="titan",
        )
    dynamo_store.put_proposal_matches_s3("p-1", [m.model_dump() for m in matches], namespace="ns")

    persisted = json.loads(stored["Body"], parse_constant=_refuse_nonfinite)

    assert persisted["matches"][0]["match_type"] == "ambiguous"
    tied = [c["fused_score"] for c in persisted["matches"][0]["candidates"] if c["fused_score"] is not None]
    assert tied == [0.5, 0.5]
