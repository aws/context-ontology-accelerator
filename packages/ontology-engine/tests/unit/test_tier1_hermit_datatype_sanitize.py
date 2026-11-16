# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit test: HermiT-unsupported datatypes are substituted before reasoning.

Regression for ``REASONER_ERROR: UnsupportedDatatypeException: 'xsd:date' is not
part of the OWL 2 datatype map``. HermiT aborts the entire reasoning run on any
datatype outside the OWL 2 map (xsd:date / xsd:time / the gregorian + duration
family). The inducer legitimately emits ``rdfs:range xsd:date`` from SQL DATE
columns. ConsistencyValidator must rewrite those to OWL 2-valid substitutes on
the reasoner's copy of the graph — leaving the original graph (and the stored
ontology) untouched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

pytestmark = pytest.mark.unit

IND = URIRef("http://test.org/ind#")
SIGNUP = URIRef("http://test.org/ind#customers_signupDate")
NAME = URIRef("http://test.org/ind#customers_name")
COHORT = URIRef("http://test.org/ind#Cohort2020")


def _date_ontology() -> Graph:
    g = Graph()
    g.add((SIGNUP, RDF.type, OWL.DatatypeProperty))
    g.add((SIGNUP, RDFS.range, XSD.date))  # HermiT-breaking
    g.add((NAME, RDF.type, OWL.DatatypeProperty))
    g.add((NAME, RDFS.range, XSD.string))  # OWL2-valid, must pass through
    return g


def _capture_owlready2(captured: dict[str, str]) -> MagicMock:
    """Build a fake owlready2 that records the RDF serialized for HermiT."""

    def _fake_get_ontology(uri: str):
        path = uri[len("file://") :]
        with open(path, "rb") as fh:
            captured["rdf"] = fh.read().decode("utf-8")
        onto = MagicMock()
        onto.inconsistent_classes.return_value = []
        onto.load.return_value = onto
        onto.__enter__ = MagicMock(return_value=onto)
        onto.__exit__ = MagicMock(return_value=False)
        return onto

    fake_owlready2 = MagicMock()
    fake_owlready2.get_ontology.side_effect = _fake_get_ontology
    fake_owlready2.sync_reasoner_hermit.return_value = None
    return fake_owlready2


@pytest.mark.unit
def test_xsd_date_substituted_before_reasoner_and_original_graph_untouched() -> None:
    from coa_ontology.validation.validators.tier1 import ConsistencyValidator

    original = _date_ontology()

    captured: dict[str, str] = {}
    with patch.dict("sys.modules", {"owlready2": _capture_owlready2(captured)}):
        findings = ConsistencyValidator().validate(original)

    # The serialized-for-HermiT graph must NOT contain xsd:date; it must be
    # rewritten to xsd:dateTime. xsd:string passes through unchanged.
    reparsed = Graph().parse(data=captured["rdf"], format="xml")
    ranges = {str(o) for o in reparsed.objects(SIGNUP, RDFS.range)}
    assert str(XSD.date) not in ranges, "xsd:date leaked to HermiT"
    assert str(XSD.dateTime) in ranges, "xsd:date should be substituted with xsd:dateTime"
    assert str(XSD.string) in {str(o) for o in reparsed.objects(NAME, RDFS.range)}

    # The ORIGINAL graph the caller passed must be untouched (still xsd:date).
    assert (SIGNUP, RDFS.range, XSD.date) in original

    # A consistent ontology yields a CONSISTENT info finding, not REASONER_ERROR.
    codes = {f.code for f in findings}
    assert "REASONER_ERROR" not in codes
    assert "CONSISTENT" in codes


@pytest.mark.unit
def test_typed_literal_datatype_substituted_before_reasoner() -> None:
    """A typed literal ``"2020-01-01"^^xsd:date` (e.g. owl:hasValue / DataOneOf)
    is also OWL 2-map-excluded and aborts HermiT. The URIRef-range substitution
    alone missed it; the literal's datatype must be
    rewritten too — while the original graph keeps the xsd:date literal."""
    from coa_ontology.validation.validators.tier1 import ConsistencyValidator

    original = Graph()
    original.add((SIGNUP, RDF.type, OWL.DatatypeProperty))
    # A datatype value restriction carrying a typed xsd:date literal.
    original.add((COHORT, SIGNUP, Literal("2020-01-01", datatype=XSD.date)))

    captured: dict[str, str] = {}
    with patch.dict("sys.modules", {"owlready2": _capture_owlready2(captured)}):
        findings = ConsistencyValidator().validate(original)

    reparsed = Graph().parse(data=captured["rdf"], format="xml")
    literals = [o for o in reparsed.objects(COHORT, SIGNUP) if isinstance(o, Literal)]
    assert literals, "typed literal missing from HermiT input"
    datatypes = {lit.datatype for lit in literals}
    assert XSD.date not in datatypes, "xsd:date-typed literal leaked to HermiT"
    assert XSD.dateTime in datatypes, "typed xsd:date literal should be substituted to xsd:dateTime"

    # Original graph the caller passed keeps its xsd:date-typed literal.
    assert (COHORT, SIGNUP, Literal("2020-01-01", datatype=XSD.date)) in original
    assert "REASONER_ERROR" not in {f.code for f in findings}


@pytest.mark.unit
def test_malformed_and_unknown_xsd_tokens_substituted_before_reasoner() -> None:
    """MALFORMED / aliased / unknown xsd: tokens (xsd:datetime lowercase-t,
    xsd:varchar, xsd:garbagetype) must ALSO be substituted on the reasoner copy —
    HermiT rejects ANY out-of-map XSD datatype, not just valid-but-excluded ones.

    Regression for the REASONER_ERROR a user hit after the write-boundary
    canonicalization was removed: an un-repaired malformed token reached HermiT and
    aborted the whole run. It must instead be sanitized on the reasoner copy (so the
    DatatypeTokenValidator can surface a clean MALFORMED_DATATYPE_TOKENS warning),
    while the stored graph keeps the original (malformed) token for consent-gated
    repair."""
    from coa_ontology.validation.validators.tier1 import ConsistencyValidator

    bad_datetime = URIRef(str(XSD) + "datetime")  # lowercase-t, malformed
    bad_varchar = URIRef(str(XSD) + "varchar")  # SQL alias
    bad_unknown = URIRef(str(XSD) + "garbagetype")  # unknown
    p_dt = URIRef("http://test.org/ind#p_dt")
    p_vc = URIRef("http://test.org/ind#p_vc")
    p_un = URIRef("http://test.org/ind#p_un")

    original = Graph()
    for prop, bad in ((p_dt, bad_datetime), (p_vc, bad_varchar), (p_un, bad_unknown)):
        original.add((prop, RDF.type, OWL.DatatypeProperty))
        original.add((prop, RDFS.range, bad))

    captured: dict[str, str] = {}
    with patch.dict("sys.modules", {"owlready2": _capture_owlready2(captured)}):
        findings = ConsistencyValidator().validate(original)

    reparsed = Graph().parse(data=captured["rdf"], format="xml")
    reasoner_ranges = {str(o) for o in reparsed.objects(None, RDFS.range)}
    # No out-of-map token leaked to HermiT.
    assert str(bad_datetime) not in reasoner_ranges
    assert str(bad_varchar) not in reasoner_ranges
    assert str(bad_unknown) not in reasoner_ranges
    # Each was mapped to an in-map substitute.
    assert str(XSD.dateTime) in reasoner_ranges  # xsd:datetime -> xsd:dateTime
    assert str(XSD.string) in reasoner_ranges  # xsd:varchar / xsd:garbagetype -> xsd:string
    # No reasoner abort.
    assert "REASONER_ERROR" not in {f.code for f in findings}
    # Stored graph is UNTOUCHED — the malformed tokens survive for consent-gated repair.
    assert (p_dt, RDFS.range, bad_datetime) in original
    assert (p_vc, RDFS.range, bad_varchar) in original
    assert (p_un, RDFS.range, bad_unknown) in original


@pytest.mark.unit
def test_reasoner_error_still_surfaces_on_genuine_failure() -> None:
    """If HermiT genuinely fails (not a datatype issue), REASONER_ERROR surfaces."""
    from coa_ontology.validation.validators.tier1 import ConsistencyValidator

    fake_owlready2 = MagicMock()
    fake_owlready2.get_ontology.side_effect = RuntimeError("java exploded")

    with patch.dict("sys.modules", {"owlready2": fake_owlready2}):
        findings = ConsistencyValidator().validate(_date_ontology())

    assert any(f.code == "REASONER_ERROR" for f in findings)
