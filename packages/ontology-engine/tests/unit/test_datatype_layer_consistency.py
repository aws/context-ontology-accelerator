# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Cross-layer datatype fidelity for the VKG path.

Three artifacts must agree on every column's type or Ontop misbehaves:

    1. the H2 validation schema  (_CATALOG_TO_H2)      — what Ontop validates against
    2. the R2RML rr:datatype     (_SQL_TO_XSD/xsd_for) — what the mapping declares
    3. the ontology rdfs:range   (_clean_ontology_for_vkg) — what the TBox declares

Disagreement between 2 and 3 raises MappingOntologyMismatchException at load time
(loud). Agreement on the WRONG type is silent and far worse: temporal columns were
declared xsd:string at all three layers, so Ontop's type reasoner found every
comparison against an xsd:date literal disjoint, proved the query unsatisfiable, and
emitted "SELECT 1 AS uselessVariable" — returning no rows for every date-filtered
query while reporting success.

These tests pin type FIDELITY, not merely internal consistency.
"""

from __future__ import annotations

import pytest
from coa_ontology.inducer.strategies.base import _SQL_TO_XSD, xsd_for
from coa_ontology.proposals import _CATALOG_TO_H2
from rdflib import RDFS, XSD, Graph, URIRef

pytestmark = pytest.mark.unit

# Source SQL type -> (expected XSD datatype, expected H2 column type family)
_EXPECTED: dict[str, tuple[object, str]] = {
    "DATE": (XSD.date, "DATE"),
    "TIMESTAMP": (XSD.dateTime, "TIMESTAMP"),
    "DATETIME": (XSD.dateTime, "TIMESTAMP"),
    "TIME": (XSD.time, "TIME"),
    "INT": (XSD.integer, "INTEGER"),
    "DOUBLE": (XSD.double, "DOUBLE"),
    "DECIMAL": (XSD.decimal, "DECIMAL"),
    "BOOLEAN": (XSD.boolean, "BOOLEAN"),
    "VARCHAR": (XSD.string, "VARCHAR"),
}


@pytest.mark.parametrize(("sql_type", "expected"), sorted(_EXPECTED.items()))
def test_xsd_mapping_is_faithful(sql_type: str, expected: tuple) -> None:
    """xsd_for() must return the real XSD counterpart, never a string fallback."""
    expected_xsd, _ = expected
    assert xsd_for(sql_type) == expected_xsd, f"{sql_type} lost its type"


@pytest.mark.parametrize(("sql_type", "expected"), sorted(_EXPECTED.items()))
def test_h2_mapping_is_faithful(sql_type: str, expected: tuple) -> None:
    """The H2 validation schema must not widen a typed column to VARCHAR."""
    _, expected_h2 = expected
    assert _CATALOG_TO_H2[sql_type].startswith(expected_h2), (
        f"{sql_type} -> {_CATALOG_TO_H2[sql_type]}, expected {expected_h2}"
    )


def test_every_sql_type_has_an_h2_counterpart() -> None:
    """The two tables must cover the same source types — no silent VARCHAR default."""
    missing = sorted(set(_SQL_TO_XSD) - set(_CATALOG_TO_H2))
    assert not missing, f"types known to R2RML but not to the H2 schema: {missing}"


def test_ontology_temporal_ranges_survive_vkg_cleaning() -> None:
    """_clean_ontology_for_vkg must preserve temporal ranges."""
    from coa_ontology.proposals import _clean_ontology_for_vkg

    turtle = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
    @prefix ex: <http://example.com/onto#> .
    ex:Shipment a owl:Class .
    ex:shipDate a owl:DatatypeProperty ; rdfs:domain ex:Shipment ; rdfs:range xsd:date .
    ex:shipAt a owl:DatatypeProperty ; rdfs:domain ex:Shipment ; rdfs:range xsd:dateTime .
    """
    out = _clean_ontology_for_vkg(turtle)
    g = Graph().parse(data=out, format="turtle")
    assert (URIRef("http://example.com/onto#shipDate"), RDFS.range, XSD.date) in g, "xsd:date range was rewritten away"
    assert "xsd:dateTime" in out or str(XSD.dateTime) in out, "xsd:dateTime range was rewritten away"
