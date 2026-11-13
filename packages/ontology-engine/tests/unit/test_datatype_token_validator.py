# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests: DatatypeTokenValidator surfaces malformed XSD datatype tokens.

Validation REPORTS malformed/mis-cased/aliased tokens (as a non-blocking
warning) so the user can consent to a repair. The finding must carry the
per-element offender list for the UI preview, must be silent when the ontology
is clean, must never flag valid/unknown types, and must also catch malformed
R2RML rr:datatype tokens (an accept-time break path).
"""

from __future__ import annotations

import pytest
from coa_ontology.validation.schemas import Severity, ValidationTier
from coa_ontology.validation.validators.tier1 import DatatypeTokenValidator
from rdflib import RDFS, Graph, URIRef
from rdflib.namespace import XSD

pytestmark = pytest.mark.unit

P = URIRef("http://x#p")


def _range_graph(dt: str) -> Graph:
    g = Graph()
    g.add((P, RDFS.range, URIRef(dt)))
    return g


def test_malformed_token_emits_warning_finding_with_details():
    findings = DatatypeTokenValidator().validate(_range_graph("http://www.w3.org/2001/XMLSchema#datetime"))
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "MALFORMED_DATATYPE_TOKENS"
    assert f.severity == Severity.warning  # non-blocking — must NOT fail validation
    assert f.tier == ValidationTier.tier1_blocking
    assert f.details is not None
    assert f.details["count"] == 1
    assert f.details["element_count"] == 1
    offenders = f.details["offenders"]
    assert offenders[0]["found"] == "http://www.w3.org/2001/XMLSchema#datetime"
    assert offenders[0]["canonical"] == str(XSD.dateTime)
    assert offenders[0]["subject"] == str(P)


def test_clean_ontology_emits_no_finding():
    assert DatatypeTokenValidator().validate(_range_graph(str(XSD.string))) == []


def test_valid_out_of_map_date_not_flagged():
    # xsd:date is valid XSD — the reasoner substitution handles the OWL2-map issue.
    assert DatatypeTokenValidator().validate(_range_graph(str(XSD.date))) == []


def test_unknown_token_not_flagged():
    assert DatatypeTokenValidator().validate(_range_graph("http://www.w3.org/2001/XMLSchema#garbagetype")) == []


def test_r2rml_malformed_datatype_flagged_via_kwarg():
    # Clean ontology graph, but malformed rr:datatype in the R2RML — the accept-time
    # range-alignment step would copy this into the served ontology range, so it
    # must be surfaced even though the ontology turtle itself is clean.
    r2rml = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://x#om> rr:datatype xsd:datetime .
"""
    findings = DatatypeTokenValidator().validate(Graph(), r2rml_turtle=r2rml)
    assert len(findings) == 1
    assert findings[0].details["count"] == 1


def test_ontology_and_r2rml_offenders_combined():
    findings = DatatypeTokenValidator().validate(
        _range_graph("http://www.w3.org/2001/XMLSchema#varchar"),
        r2rml_turtle="""@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://x#om> rr:datatype xsd:datetime .
""",
    )
    assert len(findings) == 1
    assert findings[0].details["count"] == 2
