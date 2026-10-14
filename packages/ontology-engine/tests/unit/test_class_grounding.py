# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for unstructured-class foundational grounding.

Covers :mod:`coa_ontology.inducer.unstructured.services.class_grounding`
— the additive stage that grounds induced classes against loaded
foundational ontologies via the shared ``GroundingService.ground_class``.

The tests use a fake grounding service returning canned
:class:`GroundingResult` values so no Bedrock / AOSS calls are made and the
axiom-emission logic is exercised deterministically.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from coa_common.constants import GRAPH_BASE_URI
from coa_ontology.inducer.schemas import ConceptMatch, MatchCandidate
from coa_ontology.inducer.services.grounding import (
    GroundingCandidate,
    GroundingRerankError,
    GroundingResult,
    GroundingService,
)
from coa_ontology.inducer.unstructured.services.class_grounding import (
    ClassGroundingInput,
    _local_name,
    _normalize_name_key,
    apply_overrides_to_turtle,
    ground_classes,
)
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import SKOS

pytestmark = pytest.mark.unit

_PREFIX = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#"
_CLASS_IRI = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#FinancialInstrument"
_FOUNDATIONAL = "http://fibo/FinancialInstrument"
_MATCH_CONFIDENCE = URIRef(_PREFIX + "matchConfidence")


def _result(match_type: str, *, relationship: str | None, confidence: float | None) -> GroundingResult:
    chosen = (
        GroundingCandidate(
            entity_uri=_FOUNDATIONAL,
            ontology_id="fibo",
            label="FinancialInstrument",
            definition="A financial instrument.",
            lexical_sim=confidence or 0.0,
        )
        if match_type != "novel"
        else None
    )
    return GroundingResult(
        source_table="FinancialInstrument",
        source_column="",
        candidates=[chosen] if chosen else [],
        chosen=chosen,
        match_type=match_type,
        relationship=relationship,
        confidence=confidence,
        reason=None,
        mode="ENHANCED",
    )


def _novel_with_candidates() -> GroundingResult:
    """A 'novel' verdict that still carries recall candidates — the case the
    grounding-override editor exists for (reranker abstained, but a steward
    may still manually pick one of the candidates)."""
    cand = GroundingCandidate(
        entity_uri=_FOUNDATIONAL,
        ontology_id="fibo",
        label="FinancialInstrument",
        definition="A financial instrument.",
        lexical_sim=0.6,
    )
    return GroundingResult(
        source_table="FinancialInstrument",
        source_column="",
        candidates=[cand],
        chosen=None,
        match_type="novel",
        relationship=None,
        confidence=None,
        reason="LLM abstained",
        mode="ENHANCED",
    )


def _svc(result: GroundingResult) -> MagicMock:
    svc = MagicMock()
    svc.ground_class.return_value = result
    # Delegate to_concept_match to the REAL pure mapping so match records are
    # genuine ConceptMatch instances (source_table = class label).
    svc.to_concept_match.side_effect = lambda r: GroundingService.to_concept_match(svc, r)
    return svc


def _one_input() -> list[ClassGroundingInput]:
    return [
        ClassGroundingInput(
            class_iri=_CLASS_IRI,
            label="FinancialInstrument",
            description="An instrument.",
            vector=[0.1, 0.2, 0.3],
        )
    ]


class TestDisabled:
    def test_none_service_returns_empty(self):
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=None,
            ontology_ids=["fibo"],
            model_id="m",
        )
        assert len(g) == 0
        assert n == 0

    def test_empty_ontology_ids_returns_empty(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.99))
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=[],
            model_id="m",
        )
        assert len(g) == 0
        assert n == 0
        svc.ground_class.assert_not_called()

    def test_mode_none_returns_empty(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.99))
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            grounding_mode="NONE",
        )
        assert len(g) == 0
        assert n == 0


class TestEmission:
    def test_exact_emits_subclassof_and_exactmatch(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.97))
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix=_PREFIX,
        )
        cls, target = URIRef(_CLASS_IRI), URIRef(_FOUNDATIONAL)
        assert n == 1
        assert (cls, RDFS.subClassOf, target) in g
        assert (cls, SKOS.exactMatch, target) in g
        assert (cls, SKOS.closeMatch, target) not in g
        # matchConfidence annotation property declared + value asserted.
        assert (_MATCH_CONFIDENCE, RDF.type, OWL.AnnotationProperty) in g
        assert any(True for _ in g.triples((cls, _MATCH_CONFIDENCE, None)))

    def test_high_confidence_emits_subclassof_and_closematch(self):
        svc = _svc(_result("high_confidence", relationship="closeMatch", confidence=0.9))
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix=_PREFIX,
        )
        cls, target = URIRef(_CLASS_IRI), URIRef(_FOUNDATIONAL)
        assert n == 1
        assert (cls, RDFS.subClassOf, target) in g
        assert (cls, SKOS.closeMatch, target) in g
        assert (cls, SKOS.exactMatch, target) not in g

    def test_ambiguous_emits_relatedmatch_only(self):
        svc = _svc(_result("ambiguous", relationship="relatedMatch", confidence=0.7))
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix=_PREFIX,
        )
        cls, target = URIRef(_CLASS_IRI), URIRef(_FOUNDATIONAL)
        assert n == 1
        assert (cls, SKOS.relatedMatch, target) in g
        # ambiguous is NOT a subsumption assertion
        assert (cls, RDFS.subClassOf, target) not in g

    def test_novel_emits_nothing(self):
        svc = _svc(_result("novel", relationship=None, confidence=None))
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix=_PREFIX,
        )
        cls = URIRef(_CLASS_IRI)
        assert n == 0
        assert not any(True for _ in g.triples((cls, None, None)))

    def test_no_uri_prefix_skips_match_confidence(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.97))
        g, _, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix="",
        )
        cls = URIRef(_CLASS_IRI)
        # subClassOf still emitted, but no matchConfidence triples.
        assert (cls, RDFS.subClassOf, URIRef(_FOUNDATIONAL)) in g
        assert not any(True for _ in g.triples((cls, _MATCH_CONFIDENCE, None)))


class TestRobustness:
    def test_missing_vector_is_skipped(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.99))
        inputs = [ClassGroundingInput(class_iri=_CLASS_IRI, label="X", description="d", vector=[])]
        g, n, _m = ground_classes(inputs, grounding_service=svc, ontology_ids=["fibo"], model_id="m")
        assert n == 0
        svc.ground_class.assert_not_called()

    def test_missing_class_iri_is_skipped(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.99))
        inputs = [ClassGroundingInput(class_iri="", label="X", description="d", vector=[0.1])]
        g, n, _m = ground_classes(inputs, grounding_service=svc, ontology_ids=["fibo"], model_id="m")
        assert n == 0
        svc.ground_class.assert_not_called()

    def test_per_class_exception_is_fail_open(self):
        svc = MagicMock()
        svc.ground_class.side_effect = RuntimeError("bedrock down")
        g, n, _m = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix=_PREFIX,
        )
        # The exception is swallowed; the class is left un-grounded.
        assert n == 0
        assert len(g) <= 1  # at most the matchConfidence AnnotationProperty decl

    def test_rerank_infra_failure_fails_loud_not_novel(self):
        # A GroundingRerankError (LLM rerank INFRASTRUCTURE failure) must
        # propagate so the induction job fails loud — NOT be swallowed by the
        # fail-open handler into a silent all-novel result. This is the
        # enhanced-grounding defect (issue #59) on the unstructured path:
        # class_grounding's broad `except Exception` used to catch it.
        svc = MagicMock()
        svc.ground_class.side_effect = GroundingRerankError(
            "rerank response truncated (stopReason=max_tokens) — raise rerank_max_tokens (currently 1000)"
        )
        with pytest.raises(GroundingRerankError, match="rerank_max_tokens"):
            ground_classes(
                _one_input(),
                grounding_service=svc,
                ontology_ids=["fibo"],
                model_id="m",
                uri_prefix=_PREFIX,
            )

    def test_passes_through_mode_and_ids(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.95))
        ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo", "schema-org"],
            model_id="titan-v2",
            grounding_mode="STANDARD",
            confidence_threshold=0.7,
            rerank_max_tokens=1500,
            uri_prefix=_PREFIX,
        )
        _, kwargs = svc.ground_class.call_args
        assert kwargs["ontology_ids"] == ["fibo", "schema-org"]
        assert kwargs["grounding_mode"] == "STANDARD"
        assert kwargs["model_id"] == "titan-v2"
        assert kwargs["confidence_threshold"] == 0.7
        assert kwargs["rerank_max_tokens"] == 1500


class TestMatches:
    """ground_classes returns per-class ConceptMatch records (metadata.matches
    → matches.json) that drive the review UI's candidate count + override
    editor — one per class that produced recall candidates, chosen or not."""

    def test_grounded_class_yields_match_keyed_by_label(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.97))
        _g, _n, matches = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix=_PREFIX,
        )
        assert len(matches) == 1
        m = matches[0]
        # source_table = the class LABEL so the UI's normalized-name resolver
        # joins the match back to the induced class.
        assert m.source_table == "FinancialInstrument"
        assert m.matched_class_uri == _FOUNDATIONAL
        assert m.match_type == "exact"
        assert m.candidates and m.candidates[0].entity_uri == _FOUNDATIONAL

    def test_novel_with_candidates_still_yields_a_match(self):
        # The override case: reranker abstained (novel) but candidates exist.
        # A ConceptMatch MUST still be recorded so the steward can override.
        svc = _svc(_novel_with_candidates())
        g, n, matches = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=["fibo"],
            model_id="m",
            uri_prefix=_PREFIX,
        )
        assert n == 0  # nothing auto-grounded
        assert len(g) <= 1  # only the matchConfidence AnnotationProperty decl (no edges)
        assert len(matches) == 1  # but the candidate record IS persisted
        assert matches[0].match_type == "novel"
        assert matches[0].matched_class_uri is None
        assert matches[0].candidates  # candidates preserved for the override editor

    def test_disabled_yields_no_matches(self):
        svc = _svc(_result("exact", relationship="exactMatch", confidence=0.97))
        _g, _n, matches = ground_classes(
            _one_input(),
            grounding_service=svc,
            ontology_ids=[],  # disabled
            model_id="m",
        )
        assert matches == []


class TestApplyOverridesToTurtle:
    """apply_overrides_to_turtle re-applies grounding decisions onto an
    already-induced Turtle by surgically rewriting only the grounding axioms
    (the unstructured analogue of the structured table rebuild)."""

    # A minimal induced Turtle: one class, initially grounded to FIBO Agreement.
    _CLS = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#FinancialInstrument"
    _OLD_TARGET = "http://fibo/Agreement"
    _NEW_TARGET = "http://fibo/Commitment"
    _BASE_TTL = f"""
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
    <{_CLS}> a owl:Class ;
        rdfs:label "FinancialInstrument" ;
        rdfs:subClassOf <{_OLD_TARGET}> ;
        skos:closeMatch <{_OLD_TARGET}> .
    """

    def _match(self, *, matched_uri, match_type, candidates_uris=("http://fibo/Agreement", "http://fibo/Commitment")):
        return ConceptMatch(
            source_table="FinancialInstrument",  # == rdfs:label
            source_column="",
            matched_class_uri=matched_uri,
            matched_ontology_id="fibo" if matched_uri else None,
            similarity=0.9 if matched_uri else None,
            match_type=match_type,
            candidates=[MatchCandidate(entity_uri=u, ontology_id="fibo", lexical_sim=0.9) for u in candidates_uris],
        )

    def test_repick_replaces_grounding_target(self):
        m = self._match(matched_uri=self._NEW_TARGET, match_type="high_confidence")
        new_ttl, grounded = apply_overrides_to_turtle(self._BASE_TTL, [m], uri_prefix=_PREFIX)

        g = Graph()
        g.parse(data=new_ttl, format="turtle")
        cls = URIRef(self._CLS)
        # Old target fully removed, new target asserted.
        assert (cls, SKOS.closeMatch, URIRef(self._OLD_TARGET)) not in g
        assert (cls, RDFS.subClassOf, URIRef(self._OLD_TARGET)) not in g
        assert (cls, RDFS.subClassOf, URIRef(self._NEW_TARGET)) in g
        assert (cls, SKOS.closeMatch, URIRef(self._NEW_TARGET)) in g
        assert grounded == 1
        # Non-grounding triples preserved.
        assert (cls, RDF.type, OWL.Class) in g
        assert (cls, RDFS.label, Literal("FinancialInstrument")) in g

    def test_mark_novel_drops_grounding(self):
        m = self._match(matched_uri=None, match_type="novel")
        new_ttl, grounded = apply_overrides_to_turtle(self._BASE_TTL, [m], uri_prefix=_PREFIX)

        g = Graph()
        g.parse(data=new_ttl, format="turtle")
        cls = URIRef(self._CLS)
        # No grounding predicates remain.
        assert (cls, RDFS.subClassOf, None) not in g
        assert not any(True for _ in g.triples((cls, SKOS.closeMatch, None)))
        assert not any(True for _ in g.triples((cls, SKOS.exactMatch, None)))
        assert grounded == 0
        # Class declaration + label still intact.
        assert (cls, RDF.type, OWL.Class) in g

    def test_partial_override_counts_untouched_grounded_classes(self):
        other_class = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#Other"
        other_target = "http://fibo/Other"
        turtle = (
            self._BASE_TTL
            + f"""
<{other_class}> a owl:Class ;
    rdfs:label "Other" ;
    rdfs:subClassOf <{other_target}> ;
    skos:closeMatch <{other_target}> .
"""
        )
        changed = self._match(matched_uri=None, match_type="novel")

        new_ttl, grounded = apply_overrides_to_turtle(turtle, [changed], uri_prefix=_PREFIX)

        graph = Graph().parse(data=new_ttl, format="turtle")
        assert (URIRef(other_class), SKOS.closeMatch, URIRef(other_target)) in graph
        assert grounded == 1

    def test_unknown_label_aborts(self):
        m = self._match(matched_uri=self._NEW_TARGET, match_type="exact")
        m.source_table = "NoSuchClass"
        with pytest.raises(ValueError, match="no class matching"):
            apply_overrides_to_turtle(self._BASE_TTL, [m], uri_prefix=_PREFIX)

    def test_empty_normalized_label_aborts(self):
        """An empty punctuation-only key must abort rather than report success."""
        ttl = self._BASE_TTL.replace('"FinancialInstrument"', '"!!!"')
        m = self._match(matched_uri=self._NEW_TARGET, match_type="exact")
        m.source_table = "???"
        with pytest.raises(ValueError, match="empty normalized key"):
            apply_overrides_to_turtle(ttl, [m], uri_prefix=_PREFIX)

    def test_ambiguous_normalized_label_aborts(self):
        """A lossy fallback key must never choose one of multiple classes."""
        first = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#First"
        second = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#Second"
        ttl = f"""
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
        <{first}> a owl:Class ; rdfs:label "A-B" ; skos:closeMatch <{self._OLD_TARGET}> .
        <{second}> a owl:Class ; rdfs:label "AB" ; skos:closeMatch <{self._OLD_TARGET}> .
        """
        m = self._match(matched_uri=self._NEW_TARGET, match_type="exact")
        m.source_table = "A B"
        with pytest.raises(ValueError, match="matches 2 classes"):
            apply_overrides_to_turtle(ttl, [m], uri_prefix=_PREFIX)

    def test_exact_label_wins_when_normalized_key_collides(self):
        first = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#First"
        second = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#Second"
        ttl = f"""
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
        <{first}> a owl:Class ; rdfs:label "A-B" ; skos:closeMatch <{self._OLD_TARGET}> .
        <{second}> a owl:Class ; rdfs:label "AB" ; skos:closeMatch <{self._OLD_TARGET}> .
        """
        m = self._match(matched_uri=self._NEW_TARGET, match_type="exact")
        m.source_table = "AB"
        new_ttl, grounded = apply_overrides_to_turtle(ttl, [m], uri_prefix=_PREFIX)
        graph = Graph().parse(data=new_ttl, format="turtle")
        assert (URIRef(second), SKOS.exactMatch, URIRef(self._NEW_TARGET)) in graph
        assert (URIRef(first), SKOS.closeMatch, URIRef(self._OLD_TARGET)) in graph
        # The untouched colliding class remains grounded while the exact-label
        # class receives the override.
        assert grounded == 2


class TestOverrideLabelNormalization:
    """Regression: matches.json stores the SPACED grounding label
    ('Financial Instrument') but the induced Turtle's rdfs:label is CamelCase
    ('FinancialInstrument'). apply_overrides_to_turtle must join them via the
    normalized-name key, or overrides silently no-op (grounding stays stale)."""

    _CLS = f"{GRAPH_BASE_URI}/namespace/ns/ontology/unstructured/class/Financial-Instrument"
    _TARGET = "http://fibo/Agreement"
    _TTL = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{_CLS}> a owl:Class ; rdfs:label "FinancialInstrument" ;
    rdfs:subClassOf <{_TARGET}> ; skos:closeMatch <{_TARGET}> .
"""

    def _novel_match_spaced_label(self):
        return ConceptMatch(
            source_table="Financial Instrument",  # SPACED — differs from rdfs:label
            source_column="",
            matched_class_uri=None,
            matched_ontology_id=None,
            similarity=None,
            match_type="novel",
            candidates=[MatchCandidate(entity_uri=self._TARGET, ontology_id="fibo", lexical_sim=0.7)],
        )

    def test_spaced_label_override_matches_camelcase_class(self):
        new_ttl, grounded = apply_overrides_to_turtle(self._TTL, [self._novel_match_spaced_label()], uri_prefix=_PREFIX)
        g = Graph()
        g.parse(data=new_ttl, format="turtle")
        cls = URIRef(self._CLS)
        # The override (mark novel) MUST have dropped the grounding despite the
        # spaced-vs-CamelCase label difference.
        assert (cls, RDFS.subClassOf, URIRef(self._TARGET)) not in g
        assert not any(True for _ in g.triples((cls, SKOS.closeMatch, None)))
        assert grounded == 0
        # Class + label still present.
        assert (cls, RDF.type, OWL.Class) in g

    def test_percent_encoded_local_name_matches_unicode_label(self):
        class_iri = f"{GRAPH_BASE_URI}/namespace/ns/ontology/unstructured/class/क%E0%A4%BF"
        ttl = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
<{class_iri}> a owl:Class ; skos:closeMatch <{self._TARGET}> .
"""
        match = self._novel_match_spaced_label()
        match.source_table = "कि"
        new_ttl, grounded = apply_overrides_to_turtle(ttl, [match], uri_prefix=_PREFIX)
        graph = Graph().parse(data=new_ttl, format="turtle")
        assert (URIRef(class_iri), SKOS.closeMatch, URIRef(self._TARGET)) not in graph
        assert grounded == 0


class TestEmissionRelationship:
    """Finding #6: match_type decides ACCEPTANCE; the reranker's `relationship`
    selects the SKOS predicate and whether a forward subClassOf is implied.
    broad/narrowMatch must be honored (not collapsed to exact/close)."""

    def test_broadmatch_emits_broadmatch_and_subclassof(self):
        # A high_confidence acceptance but the reranker said the candidate is a
        # BROADER concept → skos:broadMatch + forward subClassOf (subsumption).
        svc = _svc(_result("high_confidence", relationship="broadMatch", confidence=0.9))
        g, n, _m = ground_classes(
            _one_input(), grounding_service=svc, ontology_ids=["fibo"], model_id="m", uri_prefix=_PREFIX
        )
        cls, target = URIRef(_CLASS_IRI), URIRef(_FOUNDATIONAL)
        assert n == 1
        assert (cls, SKOS.broadMatch, target) in g
        assert (cls, RDFS.subClassOf, target) in g
        assert (cls, SKOS.closeMatch, target) not in g  # not collapsed to close

    def test_narrowmatch_emits_narrowmatch_and_NO_subclassof(self):
        # narrowMatch: the foundational concept is NARROWER than our class, so a
        # forward subClassOf would point the wrong way — must NOT be asserted.
        svc = _svc(_result("high_confidence", relationship="narrowMatch", confidence=0.9))
        g, n, _m = ground_classes(
            _one_input(), grounding_service=svc, ontology_ids=["fibo"], model_id="m", uri_prefix=_PREFIX
        )
        cls, target = URIRef(_CLASS_IRI), URIRef(_FOUNDATIONAL)
        assert n == 1
        assert (cls, SKOS.narrowMatch, target) in g
        assert (cls, RDFS.subClassOf, target) not in g

    def test_relationship_absent_falls_back_to_match_type(self):
        # No relationship (e.g. STANDARD mode / legacy) → legacy mapping.
        svc = _svc(_result("exact", relationship=None, confidence=0.97))
        g, n, _m = ground_classes(
            _one_input(), grounding_service=svc, ontology_ids=["fibo"], model_id="m", uri_prefix=_PREFIX
        )
        cls, target = URIRef(_CLASS_IRI), URIRef(_FOUNDATIONAL)
        assert n == 1
        assert (cls, SKOS.exactMatch, target) in g
        assert (cls, RDFS.subClassOf, target) in g

    def test_concept_match_carries_relationship(self):
        # Finding #6: to_concept_match must persist `relationship` so the override
        # editor and re-emission can use it.
        svc = _svc(_result("high_confidence", relationship="broadMatch", confidence=0.9))
        _g, _n, matches = ground_classes(
            _one_input(), grounding_service=svc, ontology_ids=["fibo"], model_id="m", uri_prefix=_PREFIX
        )
        assert matches and matches[0].relationship == "broadMatch"


class TestSelfGroundingGuard:
    """Finding #2: a class must never ground to ITSELF (its own IRI), even if the
    recall-exclusion is bypassed — the emission guard is the backstop."""

    def test_class_does_not_ground_to_its_own_iri(self):
        # Reranker 'chose' the class's OWN IRI as the grounding target.
        self_uri = _CLASS_IRI
        chosen = GroundingCandidate(
            entity_uri=self_uri, ontology_id="self", label="FinancialInstrument", definition="x", lexical_sim=1.0
        )
        result = GroundingResult(
            source_table="FinancialInstrument",
            source_column="",
            candidates=[chosen],
            chosen=chosen,
            match_type="exact",
            relationship="exactMatch",
            confidence=1.0,
            reason=None,
            mode="ENHANCED",
        )
        g, n, _m = ground_classes(
            _one_input(), grounding_service=_svc(result), ontology_ids=["fibo"], model_id="m", uri_prefix=_PREFIX
        )
        cls = URIRef(_CLASS_IRI)
        assert n == 0  # self-match rejected
        assert (cls, RDFS.subClassOf, cls) not in g
        assert not any(True for _ in g.triples((cls, SKOS.exactMatch, cls)))


class TestOverridePreservesWithinRunLinks:
    """Finding #3: apply_overrides_to_turtle must strip only FOUNDATIONAL
    grounding edges, preserving within-namespace links (e.g. the Rule-1
    skos:closeMatch dedupe link to a SIBLING induced class)."""

    _CLS = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#FinancialInstrument"
    _SIBLING = f"{GRAPH_BASE_URI}/namespace/ns/ontology/induced#Security"  # within-namespace
    _OLD_FOUNDATIONAL = "http://fibo/Agreement"
    _NEW_FOUNDATIONAL = "http://fibo/Commitment"

    def _ttl(self):
        return f"""
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
        <{self._CLS}> a owl:Class ;
            rdfs:label "FinancialInstrument" ;
            rdfs:subClassOf <{self._OLD_FOUNDATIONAL}> ;
            skos:closeMatch <{self._OLD_FOUNDATIONAL}> ;
            skos:closeMatch <{self._SIBLING}> .
        """

    def test_within_run_sibling_closematch_survives_override(self):
        m = ConceptMatch(
            source_table="FinancialInstrument",
            source_column="",
            matched_class_uri=self._NEW_FOUNDATIONAL,
            matched_ontology_id="fibo",
            similarity=0.9,
            match_type="high_confidence",
            relationship="closeMatch",
            candidates=[MatchCandidate(entity_uri=self._NEW_FOUNDATIONAL, ontology_id="fibo", lexical_sim=0.9)],
        )
        new_ttl, grounded = apply_overrides_to_turtle(self._ttl(), [m], uri_prefix=_PREFIX)
        g = Graph()
        g.parse(data=new_ttl, format="turtle")
        cls = URIRef(self._CLS)
        # Foundational old target removed, new asserted.
        assert (cls, SKOS.closeMatch, URIRef(self._OLD_FOUNDATIONAL)) not in g
        assert (cls, SKOS.closeMatch, URIRef(self._NEW_FOUNDATIONAL)) in g
        # The within-namespace sibling closeMatch MUST survive (not a grounding edge).
        assert (cls, SKOS.closeMatch, URIRef(self._SIBLING)) in g
        assert grounded == 1


class TestNormalizeNameKeyIsScriptAgnostic:
    """The key drives dict lookup, so an empty key attaches results to the wrong class.

    ``_normalize_name_key`` joins the grounding side's SPACED label with the
    Turtle's CamelCase ``rdfs:label``. The previous ``[^a-zA-Z0-9]`` filter judged
    by ASCII range, so a non-Latin class label keyed to ``""`` — and because the
    key is a dict key, every such label collided on one bucket rather than simply
    failing to join.

    These assertions must stay in step with ``grounding.ts::normalizeNameKey``:
    the join works only because both sides produce the same key.
    """

    @pytest.mark.parametrize(
        "label",
        ["設置場所", "メーカー", "安装位置", "제조사", "ИЗГОТОВИТЕЛЬ"],
    )
    def test_a_non_latin_label_keeps_a_key(self, label):
        assert _normalize_name_key(label) != ""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("設置場所", "メーカー", id="ja-unrelated"),
            pytest.param("엘리베이터", "제조사", id="ko-unrelated"),
        ],
    )
    def test_unrelated_non_latin_labels_do_not_share_a_key(self, left, right):
        assert _normalize_name_key(left) != _normalize_name_key(right)

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            pytest.param("HÖHE", "höhe", id="de-umlaut"),
            pytest.param("ÓRGÃO", "órgão", id="pt-accents"),
            pytest.param("MODÈLE", "modèle", id="fr-grave"),
        ],
    )
    def test_a_latin_label_keeps_its_accented_letter(self, label, expected):
        """Previously ``HÖHE`` keyed to ``hhe`` — the letter went, not just its case.

        Unlike ``grounding._normalize``, this key deliberately does NOT fold
        diacritics: it joins two spellings of the same label, not two labels that
        merely resemble each other.
        """
        assert _normalize_name_key(label) == expected

    @pytest.mark.parametrize(
        ("spaced", "camel"),
        [
            pytest.param("Financial Instrument", "FinancialInstrument", id="spaced-vs-camel"),
            pytest.param("clinical_trial", "ClinicalTrial", id="snake-vs-camel"),
            pytest.param("Policy_Amount", "PolicyAmount", id="mixed-separators"),
        ],
    )
    def test_still_joins_the_spellings_it_exists_for(self, spaced, camel):
        assert _normalize_name_key(spaced) == _normalize_name_key(camel)

    def test_still_strips_punctuation(self):
        assert _normalize_name_key("Policy (v2)") == "policyv2"
        assert _normalize_name_key("設置場所（屋内）") == "設置場所屋内"

    def test_preserves_symbols_and_unifies_sigma_without_expanding_sharp_s(self):
        assert _normalize_name_key("Brand™") == "brand™"
        assert _normalize_name_key("Temperature ℃") == "temperature℃"
        assert _normalize_name_key("Σ") == _normalize_name_key("σ") == _normalize_name_key("ς")
        assert _normalize_name_key("Maße") != _normalize_name_key("Masse")

    @pytest.mark.parametrize("local_name", ["%FF", "A%20B%ZZ", "%E0%A4%BF%ZZ", "%", "%2"])
    def test_local_name_preserves_whole_value_when_any_percent_escape_is_invalid(self, local_name: str):
        assert _local_name(f"http://example.test/class/{local_name}") == local_name
