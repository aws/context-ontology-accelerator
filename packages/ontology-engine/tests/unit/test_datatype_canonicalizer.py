# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests: XSD datatype canonicalization at the ingest/proposal boundary.

Covers the teammate-reported latent bug class: a mis-cased ``xsd:datetime``
(lowercase ``t``) or SQL alias (``xsd:varchar``/``xsd:timestamp``) is an undefined
datatype URI that slips past both validation and the case-sensitive accept
normalizer. The canonicalizer must repair these to valid, correctly-cased XSD
types while leaving already-valid graphs — AND valid-but-unlisted / unknown
``xsd:`` tokens — untouched (never coercing them to xsd:string, which would
irreversibly corrupt legitimate datatypes on the persisted ontology).
"""

from __future__ import annotations

import pytest
from coa_ontology.datatype_canonicalizer import (
    canonicalize_datatypes,
    canonicalize_turtle,
    detect_datatype_issues,
    detect_datatype_issues_in_turtle,
)
from rdflib import RDFS, Graph, Literal, URIRef
from rdflib.namespace import XSD

pytestmark = pytest.mark.unit

P = URIRef("http://x#p")
S = URIRef("http://x#S")


def _range_graph(dt_uri: str) -> Graph:
    g = Graph()
    g.add((P, RDFS.range, URIRef(dt_uri)))
    return g


class TestCanonicalizeDatatypes:
    def test_lowercase_datetime_becomes_camelcase(self):
        g = _range_graph("http://www.w3.org/2001/XMLSchema#datetime")
        changes = canonicalize_datatypes(g)
        assert (P, RDFS.range, XSD.dateTime) in g
        assert (P, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#datetime")) not in g
        assert changes == [("http://www.w3.org/2001/XMLSchema#datetime", str(XSD.dateTime))]

    def test_sql_alias_varchar_becomes_string(self):
        g = _range_graph("http://www.w3.org/2001/XMLSchema#varchar")
        canonicalize_datatypes(g)
        assert (P, RDFS.range, XSD.string) in g

    def test_timestamp_alias_becomes_datetime(self):
        g = _range_graph("http://www.w3.org/2001/XMLSchema#timestamp")
        canonicalize_datatypes(g)
        assert (P, RDFS.range, XSD.dateTime) in g

    def test_unknown_xsd_token_left_untouched(self):
        # An unrecognized xsd: token must be LEFT ALONE, not coerced to xsd:string.
        # It is opaque to HermiT (no reasoner abort), and rewriting it on the
        # persisted graph would be irreversible corruption. (Regression: MR !593
        # review — canonicalizer previously folded every unknown xsd: token to
        # xsd:string.)
        bad = URIRef("http://www.w3.org/2001/XMLSchema#garbagetype")
        g = _range_graph(str(bad))
        changes = canonicalize_datatypes(g)
        assert changes == []
        assert (P, RDFS.range, bad) in g
        assert (P, RDFS.range, XSD.string) not in g

    @pytest.mark.parametrize(
        "localname",
        ["NCName", "NMTOKEN", "Name", "token", "normalizedString", "dayTimeDuration", "yearMonthDuration"],
    )
    def test_valid_but_unlisted_types_left_untouched(self, localname):
        # These are legitimate XSD built-ins (several are even in the OWL 2 map).
        # The old unknown→string coercion silently corrupted them; they must now
        # pass through unchanged.
        dt = URIRef(f"http://www.w3.org/2001/XMLSchema#{localname}")
        g = _range_graph(str(dt))
        changes = canonicalize_datatypes(g)
        assert changes == []
        assert (P, RDFS.range, dt) in g

    def test_valid_xsd_date_left_unchanged(self):
        # xsd:date is valid + correctly cased — canonicalization must NOT touch it
        # (the tier-1 HermiT substitution handles the OWL2-map exclusion separately).
        g = _range_graph(str(XSD.date))
        changes = canonicalize_datatypes(g)
        assert changes == []
        assert (P, RDFS.range, XSD.date) in g

    def test_valid_datetime_camelcase_unchanged(self):
        g = _range_graph(str(XSD.dateTime))
        assert canonicalize_datatypes(g) == []

    def test_non_xsd_uri_ignored(self):
        other = URIRef("http://example.org/CustomType")
        g = Graph()
        g.add((P, RDFS.range, other))
        assert canonicalize_datatypes(g) == []
        assert (P, RDFS.range, other) in g

    def test_literal_datatype_canonicalized(self):
        g = Graph()
        bad = Literal("2020-01-01T00:00:00", datatype=URIRef("http://www.w3.org/2001/XMLSchema#datetime"))
        g.add((S, P, bad))
        canonicalize_datatypes(g)
        objs = list(g.objects(S, P))
        assert len(objs) == 1
        assert isinstance(objs[0], Literal)
        assert objs[0].datatype == XSD.dateTime


class TestCanonicalizeTurtle:
    def test_clean_turtle_returned_verbatim(self):
        ttl = f"""@prefix xsd: <{XSD}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://x#p> rdfs:range xsd:string .
"""
        out, changes = canonicalize_turtle(ttl)
        assert changes == []
        assert out == ttl  # byte-for-byte unchanged when nothing to fix

    def test_malformed_turtle_repaired(self):
        ttl = """@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<http://x#p> rdfs:range xsd:datetime .
"""
        out, changes = canonicalize_turtle(ttl)
        assert len(changes) == 1
        reparsed = Graph()
        reparsed.parse(data=out, format="turtle")
        assert (P, RDFS.range, XSD.dateTime) in reparsed


class TestDetectDatatypeIssues:
    """Read-only detection used by DatatypeTokenValidator — must be the exact
    inverse of the repair (flags precisely what canonicalize rewrites)."""

    def test_malformed_range_flagged_with_subject_and_predicate(self):
        g = _range_graph("http://www.w3.org/2001/XMLSchema#datetime")
        issues = detect_datatype_issues(g)
        assert len(issues) == 1
        issue = issues[0]
        assert issue["subject"] == str(P)
        assert issue["predicate"] == str(RDFS.range)
        assert issue["found"] == "http://www.w3.org/2001/XMLSchema#datetime"
        assert issue["canonical"] == str(XSD.dateTime)

    def test_sql_alias_flagged(self):
        issues = detect_datatype_issues(_range_graph("http://www.w3.org/2001/XMLSchema#varchar"))
        assert len(issues) == 1
        assert issues[0]["canonical"] == str(XSD.string)

    def test_valid_xsd_date_not_flagged(self):
        # xsd:date is a legitimate XSD type (out of the OWL2 map, but the tier-1
        # reasoner substitution handles that separately) — detection must ignore it.
        assert detect_datatype_issues(_range_graph(str(XSD.date))) == []

    def test_unknown_token_not_flagged(self):
        # Regression: an unrecognized xsd: token must be neither flagged nor
        # (by parity) rewritten — leaving it alone avoids irreversible corruption.
        assert detect_datatype_issues(_range_graph("http://www.w3.org/2001/XMLSchema#garbagetype")) == []

    def test_non_xsd_uri_not_flagged(self):
        assert detect_datatype_issues(_range_graph("http://example.org/CustomType")) == []

    def test_typed_literal_datatype_flagged(self):
        g = Graph()
        g.add((S, P, Literal("2020", datatype=URIRef("http://www.w3.org/2001/XMLSchema#datetime"))))
        issues = detect_datatype_issues(g)
        assert len(issues) == 1
        assert issues[0]["found"] == "http://www.w3.org/2001/XMLSchema#datetime"

    def test_detection_is_inverse_of_repair(self):
        # Every flagged token must be exactly one canonicalize rewrites (parity).
        g = Graph()
        g.add((URIRef("http://x#a"), RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#datetime")))
        g.add((URIRef("http://x#b"), RDFS.range, XSD.date))  # valid — ignored by both
        g.add((URIRef("http://x#c"), RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#varchar")))
        g.add((URIRef("http://x#d"), RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#garbagetype")))  # unknown
        detected = detect_datatype_issues(g)
        repaired = canonicalize_datatypes(g)
        assert len(detected) == len(repaired) == 2


class TestDetectDatatypeIssuesInTurtle:
    def test_r2rml_rr_datatype_flagged(self):
        r2rml = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://x#om> rr:datatype xsd:datetime .
"""
        issues = detect_datatype_issues_in_turtle(r2rml)
        assert len(issues) == 1
        assert issues[0]["canonical"] == str(XSD.dateTime)

    def test_none_and_empty_and_unparseable_yield_empty(self):
        assert detect_datatype_issues_in_turtle(None) == []
        assert detect_datatype_issues_in_turtle("") == []
        assert detect_datatype_issues_in_turtle("this is not turtle {{{") == []


class TestLiteralValuePreservation:
    """A datatype repair must never alter or invalidate the literal's value.

    The alias table maps SQL type names onto XSD types with much narrower lexical
    spaces (bigint→long, number→decimal, timestamp→dateTime). Rewriting the
    datatype alone turned "abc"^^xsd:bigint into the ill-typed "abc"^^xsd:long,
    and constructing the replacement Literal without normalize=False rewrote
    "2024-01-15" to "2024-01-15T00:00:00". Both are irreversible: this module runs
    at the persist boundary and drops the original token.
    """

    def test_incompatible_value_is_left_untouched(self):
        g = Graph()
        g.add((S, P, Literal("abc", datatype=URIRef(f"{XSD}bigint"))))

        changes = canonicalize_datatypes(g)

        assert changes == [], "must not rewrite a token whose value would become ill-typed"
        assert (S, P, Literal("abc", datatype=URIRef(f"{XSD}bigint"))) in g

    def test_incompatible_value_never_yields_an_ill_typed_literal(self):
        g = Graph()
        g.add((S, P, Literal("abc", datatype=URIRef(f"{XSD}bigint"))))

        canonicalize_datatypes(g)

        for _s, _p, o in g:
            assert not (isinstance(o, Literal) and o.ill_typed), f"produced ill-typed literal: {o!r}"

    def test_lexical_form_survives_a_compatible_repair(self):
        """timestamp→dateTime must not reformat the value."""
        g = Graph()
        g.add((S, P, Literal("2024-01-15", datatype=URIRef(f"{XSD}timestamp"))))

        canonicalize_datatypes(g)

        obj = next(o for _s, _p, o in g)
        assert str(obj) == "2024-01-15", f"lexical form was rewritten to {str(obj)!r}"
        assert obj.datatype == XSD.dateTime

    def test_compatible_values_are_still_repaired(self):
        """The guard must not disable the repair it is protecting."""
        g = Graph()
        g.add((S, P, Literal("hello", datatype=URIRef(f"{XSD}varchar"))))
        g.add((URIRef("http://x#b"), P, Literal("42.5", datatype=URIRef(f"{XSD}number"))))

        changes = canonicalize_datatypes(g)

        assert len(changes) == 2
        datatypes = {o.datatype for _s, _p, o in g}
        assert datatypes == {XSD.string, XSD.decimal}

    def test_incompatible_token_is_still_reported_to_the_user(self):
        """Skipping the repair must not hide the malformed token."""
        g = Graph()
        g.add((S, P, Literal("abc", datatype=URIRef(f"{XSD}bigint"))))

        issues = detect_datatype_issues(g)

        assert len(issues) == 1
        assert issues[0]["found"] == f"{XSD}bigint"
        assert issues[0]["repairable"] == "false"

    def test_repairable_flag_is_true_for_a_safe_rewrite(self):
        g = Graph()
        g.add((S, P, Literal("hello", datatype=URIRef(f"{XSD}varchar"))))

        issues = detect_datatype_issues(g)

        assert issues[0]["repairable"] == "true"

    def test_datatype_uri_objects_are_unaffected_by_the_guard(self):
        """rdfs:range / rr:datatype carry no value, so they always repair."""
        g = Graph()
        g.add((S, RDFS.range, URIRef(f"{XSD}bigint")))

        changes = canonicalize_datatypes(g)

        assert changes == [(f"{XSD}bigint", f"{XSD}long")]
        assert (S, RDFS.range, XSD.long) in g

    def test_turtle_round_trip_preserves_an_incompatible_literal(self):
        ttl = (
            "@prefix ex: <http://ex.org/> .\n"
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
            'ex:a ex:p "abc"^^xsd:bigint .\n'
        )

        out, changes = canonicalize_turtle(ttl)

        assert changes == []
        assert out == ttl, "a graph with nothing safely repairable must keep its bytes"
