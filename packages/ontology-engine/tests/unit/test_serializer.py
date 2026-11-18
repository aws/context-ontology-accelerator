# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Turtle serializer."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timezone

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.class_normalizer import (
    NormalizationResult,
)
from coa_ontology.inducer.unstructured.services.induction_rules import (
    induce_classes,
    induce_concepts,
    induce_datatype_properties,
    induce_individuals,
    induce_object_properties,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import (
    InMemoryIRIResolver,
)
from coa_ontology.inducer.unstructured.services.serializer import (
    serialize,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
    EntityRecord,
    FactRecord,
    TopicRecord,
)
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCTERMS, PROV, SKOS

# Same prefix shape the rest of the unstructured tests use.
_TEST_NS = "test-ns"
_TEST_ONTO = "test-onto"
_TEST_PREFIX = f"{GRAPH_BASE_URI}/namespace/{_TEST_NS}/ontology/{_TEST_ONTO}"
# The base IRI passed to ``serialize`` — same root as the IRIs that
# Rules 1–5 mint, but with a trailing ``/`` so the prefix expansion
# at parse time matches the IRIs in the graph.
_TEST_BASE_IRI = f"{_TEST_PREFIX}/"
# The ontology subject IRI — same as base, with trailing ``/`` stripped.
_TEST_ONTOLOGY_IRI = _TEST_PREFIX

# Fixed timestamp for deterministic Turtle comparison.
_FIXED_TS = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
_FIXED_TS_ISO_Z = "2026-05-26T12:00:00Z"


def _resolver() -> InMemoryIRIResolver:
    """Real resolver for IRI minting in test inputs."""
    return InMemoryIRIResolver(
        namespace=_TEST_NS,
        ontology_name=_TEST_ONTO,
    )


def _class_iri(label: str) -> str:
    """The IRI a real resolver mints for class ``label``."""
    return f"{_TEST_PREFIX}/class/{label}"


def _nr(class_iri: str, band: str = "novel") -> NormalizationResult:
    return NormalizationResult(
        class_iri=class_iri,
        band=band,  # type: ignore[arg-type]
        similarity_score=0.0,
        matched_existing_iri=None,
        source_mode="schema",
    )


def _entity(value: str, classification: str) -> EntityRecord:
    return EntityRecord(entity_id=f"id-{value}", value=value, classification=classification)


def _spo(
    subject_value: str,
    subject_class: str,
    predicate: str,
    object_value: str,
    object_class: str,
) -> FactRecord:
    return FactRecord(
        subject_value=subject_value,
        subject_class=subject_class,
        predicate=predicate,
        object_value=object_value,
        object_class=object_class,
        complement=None,
    )


def _spc(subject_value: str, subject_class: str, predicate: str, complement: str) -> FactRecord:
    return FactRecord(
        subject_value=subject_value,
        subject_class=subject_class,
        predicate=predicate,
        object_value=None,
        object_class=None,
        complement=complement,
    )


def _topic(value: str, topic_id: str = "t1") -> TopicRecord:
    return TopicRecord(topic_id=topic_id, value=value, statement_count=0)


def _build_full_pipeline_graph(resolver: InMemoryIRIResolver) -> Graph:
    """Build a realistic merged graph using all 5 induction rules.

    Domain: a small finance/business example. Two classes (Company,
    Person), three individuals, two SPO predicates, two SPC
    predicates with different XSD types, two topics.
    """
    # Rule 1 — classes.
    company_iri = _class_iri("Company")
    person_iri = _class_iri("Person")
    class_records = [
        ClassRecord(value="Company", count=10),
        ClassRecord(value="Person", count=8),
    ]
    descriptions = {
        "Company": "A business organization.",
        "Person": "An individual human being.",
    }
    normalizer_results = {
        "Company": _nr(company_iri),
        "Person": _nr(person_iri),
    }

    g = Graph()
    g += induce_classes(
        class_distribution=class_records,
        descriptions=descriptions,
        normalizer_results=normalizer_results,
        iri_resolver=resolver,
        floor=3,
    )

    # Rule 2 — individuals (gated, opt-in).
    class_iris = {"Company": company_iri, "Person": person_iri}
    g += induce_individuals(
        sampled_entities=[
            _entity("Apple", "Company"),
            _entity("Google", "Company"),
            _entity("Tim Cook", "Person"),
        ],
        class_iris=class_iris,
        iri_resolver=resolver,
        include_individuals=True,
    )

    # Rule 3 — object properties (SPO facts).
    spo_facts = [
        _spo("Tim Cook", "Person", "works_for", "Apple", "Company"),
        _spo("Tim Cook", "Person", "works_for", "Google", "Company"),
    ]
    g += induce_object_properties(
        facts=spo_facts,
        class_iris=class_iris,
        iri_resolver=resolver,
    )

    # Rule 4 — datatype properties (SPC facts) with mixed XSD types.
    spc_facts = [
        _spc("Apple", "Company", "founded", "1976"),
        _spc("Google", "Company", "founded", "1998"),
        _spc("Apple", "Company", "is_public", "true"),
        _spc("Google", "Company", "is_public", "true"),
    ]
    g += induce_datatype_properties(
        facts=spc_facts,
        class_iris=class_iris,
        iri_resolver=resolver,
    )

    # Rule 5 — concepts.
    g += induce_concepts(
        topics=[
            _topic("Financial Performance"),
            _topic("Legal Proceedings"),
        ],
        iri_resolver=resolver,
        namespace_name=_TEST_ONTO,
    )

    return g


class TestStandardPrefixBindings:
    """owl, rdf, rdfs, xsd, skos, dcterms, prov, custom."""

    def test_all_standard_prefixes_bound_on_empty_graph(self):
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        bound = {p: str(ns) for p, ns in augmented.namespaces()}
        assert bound["owl"] == str(OWL)
        assert bound["rdf"] == str(RDF)
        assert bound["rdfs"] == str(RDFS)
        assert bound["xsd"] == str(XSD)
        assert bound["skos"] == str(SKOS)
        assert bound["dcterms"] == str(DCTERMS)
        assert bound["prov"] == str(PROV)

    def test_custom_namespace_prefix_lowercased(self):
        """The custom prefix is the namespace name, lowercased."""
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name="My-Graph",
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        bound = {p: str(ns) for p, ns in augmented.namespaces()}
        assert "my-graph" in bound
        assert bound["my-graph"] == _TEST_BASE_IRI

    def test_prefixes_appear_in_turtle_string(self):
        """Smoke check — the prefix declarations make it into the
        Turtle output (rdflib only emits prefixes that are actually
        used, so add a triple that references each).

        Note: rdflib uses ``a`` as shorthand for ``rdf:type`` and
        doesn't emit the ``rdf:`` prefix declaration unless another
        ``rdf:`` term appears in the graph. So ``rdf:`` is not in
        the must-appear list.
        """
        g = Graph()
        # Add a triple that uses each prefix so rdflib emits them.
        s = URIRef(f"{_TEST_BASE_IRI}some-thing")
        g.add((s, RDF.type, OWL.Class))
        g.add((s, RDFS.label, Literal("thing", datatype=XSD.string)))
        g.add((s, SKOS.prefLabel, Literal("thing")))
        g.add((s, DCTERMS.created, Literal(_FIXED_TS_ISO_Z, datatype=XSD.dateTime)))
        g.add((s, PROV.wasDerivedFrom, URIRef("http://example.com/source")))

        turtle_str, _ = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        for prefix in ("owl:", "rdfs:", "xsd:", "skos:", "dcterms:", "prov:"):
            assert f"@prefix {prefix}" in turtle_str, (
                f"Expected prefix declaration {prefix!r} in Turtle. Output:\n{turtle_str}"
            )


class TestOntologyDeclaration:
    """Exactly one owl:Ontology with label, version, created."""

    def test_exactly_one_owl_ontology_declaration(self):
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        ontologies = list(augmented.subjects(RDF.type, OWL.Ontology))
        assert len(ontologies) == 1
        assert str(ontologies[0]) == _TEST_ONTOLOGY_IRI

    def test_ontology_subject_iri_strips_trailing_hash(self):
        """The ontology subject is the namespace IRI without ``#``."""
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=f"{_TEST_PREFIX}#",
            timestamp=_FIXED_TS,
        )
        ontologies = list(augmented.subjects(RDF.type, OWL.Ontology))
        assert str(ontologies[0]) == _TEST_PREFIX
        assert not str(ontologies[0]).endswith("#")

    def test_rdfs_label_is_namespace_name(self):
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name="my-graph",
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        ontology_uri = next(iter(augmented.subjects(RDF.type, OWL.Ontology)))
        labels = list(augmented.objects(ontology_uri, RDFS.label))
        assert len(labels) == 1
        assert str(labels[0]) == "my-graph"
        assert labels[0].datatype == XSD.string

    def test_owl_version_info_iso_z_format(self):
        """owl:versionInfo is a plain string of form
        YYYY-MM-DDTHH:MM:SSZ."""
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        ontology_uri = next(iter(augmented.subjects(RDF.type, OWL.Ontology)))
        versions = list(augmented.objects(ontology_uri, OWL.versionInfo))
        assert len(versions) == 1
        assert str(versions[0]) == _FIXED_TS_ISO_Z
        assert versions[0].datatype is None

    def test_dcterms_created_xsd_datetime(self):
        """dcterms:created is xsd:dateTime.

        Note: rdflib canonicalizes ``Z`` to ``+00:00`` in the
        lexical form (per W3C XSD spec for the canonical form of
        timezone offset), so the test compares the parsed datetime
        rather than the raw string.
        """
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        ontology_uri = next(iter(augmented.subjects(RDF.type, OWL.Ontology)))
        created_lits = list(augmented.objects(ontology_uri, DCTERMS.created))
        assert len(created_lits) == 1
        # Parsed datetime must match the input UTC time.
        parsed = created_lits[0].toPython()
        assert isinstance(parsed, datetime)
        # rdflib parses to a tz-aware datetime; both should be UTC.
        assert parsed.replace(tzinfo=None) == _FIXED_TS.replace(tzinfo=None)
        assert created_lits[0].datatype == XSD.dateTime

    def test_naive_datetime_treated_as_utc(self):
        """A naive datetime input is assumed UTC."""
        g = Graph()
        naive_ts = datetime(2026, 5, 26, 12, 0, 0)  # no tzinfo
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=naive_ts,
        )
        ontology_uri = next(iter(augmented.subjects(RDF.type, OWL.Ontology)))
        versions = list(augmented.objects(ontology_uri, OWL.versionInfo))
        assert str(versions[0]) == "2026-05-26T12:00:00Z"

    def test_aware_datetime_converted_to_utc(self):
        """A timezone-aware datetime is converted to UTC."""
        from datetime import timedelta

        g = Graph()
        # 2026-05-26T17:00:00+05:00 = 2026-05-26T12:00:00Z
        tz_plus_5 = timezone(timedelta(hours=5))
        ts_plus_5 = datetime(2026, 5, 26, 17, 0, 0, tzinfo=tz_plus_5)
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=ts_plus_5,
        )
        ontology_uri = next(iter(augmented.subjects(RDF.type, OWL.Ontology)))
        versions = list(augmented.objects(ontology_uri, OWL.versionInfo))
        assert str(versions[0]) == "2026-05-26T12:00:00Z"

    def test_idempotent_serialization(self):
        """Calling serialize twice on the same graph does not
        duplicate the ontology annotations.
        """
        g = Graph()
        _, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        # Second call with a different timestamp — should REPLACE the
        # annotations, not duplicate them.
        ts2 = datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC)
        _, augmented = serialize(
            graph=augmented,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=ts2,
        )
        assert len(list(augmented.subjects(RDF.type, OWL.Ontology))) == 1
        ontology_uri = next(iter(augmented.subjects(RDF.type, OWL.Ontology)))
        versions = list(augmented.objects(ontology_uri, OWL.versionInfo))
        assert len(versions) == 1
        assert str(versions[0]) == "2027-01-01T00:00:00Z"
        labels = list(augmented.objects(ontology_uri, RDFS.label))
        assert len(labels) == 1
        created = list(augmented.objects(ontology_uri, DCTERMS.created))
        assert len(created) == 1


class TestRoundTripIsomorphism:
    """Parse-back matches the original."""

    def test_empty_graph_round_trips(self):
        """Even an empty graph (just the ontology block) round-trips."""
        g = Graph()
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        reparsed = Graph()
        reparsed.parse(data=turtle_str, format="turtle")
        assert isomorphic(augmented, reparsed)

    def test_rule1_only_round_trips(self):
        """Graph with only Rule 1 output (classes) round-trips."""
        resolver = _resolver()
        g = induce_classes(
            class_distribution=[ClassRecord(value="Company", count=10)],
            descriptions={"Company": "A business org."},
            normalizer_results={"Company": _nr(_class_iri("Company"))},
            iri_resolver=resolver,
        )
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        reparsed = Graph()
        reparsed.parse(data=turtle_str, format="turtle")
        assert isomorphic(augmented, reparsed)

    def test_full_pipeline_graph_round_trips(self):
        """Realistic merged graph (all 5 rules) round-trips."""
        resolver = _resolver()
        g = _build_full_pipeline_graph(resolver)
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        reparsed = Graph()
        reparsed.parse(data=turtle_str, format="turtle")
        assert isomorphic(augmented, reparsed)


class TestRule1OnlyCombination:
    """Serialize a Rule-1-only graph (classes, no individuals/properties)."""

    def test_produces_owl_class_subjects_in_turtle(self):
        resolver = _resolver()
        g = induce_classes(
            class_distribution=[
                ClassRecord(value="Company", count=10),
                ClassRecord(value="Person", count=8),
            ],
            descriptions={
                "Company": "Business org.",
                "Person": "Human being.",
            },
            normalizer_results={
                "Company": _nr(_class_iri("Company")),
                "Person": _nr(_class_iri("Person")),
            },
            iri_resolver=resolver,
        )
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )

        classes = list(augmented.subjects(RDF.type, OWL.Class))
        assert URIRef(_class_iri("Company")) in classes
        assert URIRef(_class_iri("Person")) in classes

        assert not list(augmented.subjects(RDF.type, OWL.NamedIndividual))
        assert not list(augmented.subjects(RDF.type, OWL.ObjectProperty))
        assert not list(augmented.subjects(RDF.type, OWL.DatatypeProperty))
        assert not list(augmented.subjects(RDF.type, SKOS.Concept))

        assert "owl:Class" in turtle_str
        assert "Company" in turtle_str
        assert "Person" in turtle_str


class TestRule1Plus5Combination:
    """T-Box only: classes + concepts (no A-Box, no properties).

    This is the most realistic "small T-Box" output mode.
    """

    def test_classes_and_concepts_both_appear(self):
        resolver = _resolver()
        g = Graph()
        g += induce_classes(
            class_distribution=[ClassRecord(value="Company", count=10)],
            descriptions={"Company": "Business org."},
            normalizer_results={"Company": _nr(_class_iri("Company"))},
            iri_resolver=resolver,
        )
        g += induce_concepts(
            topics=[_topic("Financial Performance")],
            iri_resolver=resolver,
            namespace_name=_TEST_ONTO,
        )
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )

        assert URIRef(_class_iri("Company")) in augmented.subjects(RDF.type, OWL.Class)
        concepts = list(augmented.subjects(RDF.type, SKOS.Concept))
        assert len(concepts) == 1
        schemes = list(augmented.subjects(RDF.type, SKOS.ConceptScheme))
        assert len(schemes) == 1

        assert not list(augmented.subjects(RDF.type, OWL.NamedIndividual))
        assert not list(augmented.subjects(RDF.type, OWL.ObjectProperty))
        assert not list(augmented.subjects(RDF.type, OWL.DatatypeProperty))

        assert "owl:Class" in turtle_str
        assert "skos:Concept" in turtle_str
        assert "skos:ConceptScheme" in turtle_str


class TestRule1Plus3Combination:
    """T-Box: classes + object properties (no concepts, no individuals)."""

    def test_object_property_with_domain_and_range_in_turtle(self):
        resolver = _resolver()
        company_iri = _class_iri("Company")
        person_iri = _class_iri("Person")

        g = Graph()
        g += induce_classes(
            class_distribution=[
                ClassRecord(value="Company", count=10),
                ClassRecord(value="Person", count=8),
            ],
            descriptions={
                "Company": "Business org.",
                "Person": "Human being.",
            },
            normalizer_results={
                "Company": _nr(company_iri),
                "Person": _nr(person_iri),
            },
            iri_resolver=resolver,
        )
        g += induce_object_properties(
            facts=[
                _spo("Tim Cook", "Person", "works_for", "Apple", "Company"),
            ],
            class_iris={"Company": company_iri, "Person": person_iri},
            iri_resolver=resolver,
        )
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )

        # The property IRI shape is /<Subject-Class>/ObjectProperty/<predicate>.
        prop_iri = URIRef(f"{_TEST_PREFIX}/Person/ObjectProperty/works_for")
        assert (prop_iri, RDF.type, OWL.ObjectProperty) in augmented
        assert (prop_iri, RDFS.domain, URIRef(person_iri)) in augmented
        assert (prop_iri, RDFS.range, URIRef(company_iri)) in augmented

        assert "Person/ObjectProperty/works_for" in turtle_str
        assert "owl:ObjectProperty" in turtle_str


class TestRule1Plus4Combination:
    """T-Box: classes + datatype properties with XSD inference."""

    def test_xsd_integer_range_for_year(self):
        resolver = _resolver()
        company_iri = _class_iri("Company")
        g = Graph()
        g += induce_classes(
            class_distribution=[ClassRecord(value="Company", count=10)],
            descriptions={"Company": "Business org."},
            normalizer_results={"Company": _nr(company_iri)},
            iri_resolver=resolver,
        )
        g += induce_datatype_properties(
            facts=[
                _spc("Apple", "Company", "founded", "1976"),
                _spc("Google", "Company", "founded", "1998"),
            ],
            class_iris={"Company": company_iri},
            iri_resolver=resolver,
        )
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )

        prop_iri = URIRef(f"{_TEST_PREFIX}/Company/DatatypeProperty/founded")
        assert (prop_iri, RDF.type, OWL.DatatypeProperty) in augmented
        # Range = xsd:integer (unanimous integer values).
        assert (prop_iri, RDFS.range, XSD.integer) in augmented
        assert (prop_iri, RDFS.domain, URIRef(company_iri)) in augmented

        assert "xsd:integer" in turtle_str


class TestFullPipelineCombination:
    """All 5 rules merged into one Turtle — the realistic shape."""

    def test_full_graph_has_all_rule_outputs(self):
        resolver = _resolver()
        g = _build_full_pipeline_graph(resolver)
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )

        n_classes = sum(1 for _ in augmented.subjects(RDF.type, OWL.Class))
        assert n_classes == 2

        n_individuals = sum(1 for _ in augmented.subjects(RDF.type, OWL.NamedIndividual))
        assert n_individuals == 3

        n_obj_props = sum(1 for _ in augmented.subjects(RDF.type, OWL.ObjectProperty))
        assert n_obj_props >= 1

        n_dt_props = sum(1 for _ in augmented.subjects(RDF.type, OWL.DatatypeProperty))
        assert n_dt_props == 2

        n_schemes = sum(1 for _ in augmented.subjects(RDF.type, SKOS.ConceptScheme))
        assert n_schemes == 1
        n_concepts = sum(1 for _ in augmented.subjects(RDF.type, SKOS.Concept))
        assert n_concepts == 2

        n_ont = sum(1 for _ in augmented.subjects(RDF.type, OWL.Ontology))
        assert n_ont == 1

        for token in (
            "owl:Class",
            "owl:NamedIndividual",
            "owl:ObjectProperty",
            "owl:DatatypeProperty",
            "skos:Concept",
            "skos:ConceptScheme",
            "owl:Ontology",
        ):
            assert token in turtle_str, (
                f"Expected {token!r} to appear in serialized Turtle but it didn't. Turtle:\n{turtle_str}"
            )


class TestEmptyInput:
    """Empty input still produces valid Turtle with the ontology block."""

    def test_empty_graph_produces_only_ontology_block(self):
        g = Graph()
        turtle_str, augmented = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )

        all_subjects = set(augmented.subjects())
        assert len(all_subjects) == 1
        assert URIRef(_TEST_ONTOLOGY_IRI) in all_subjects

        # (declaration + label + versionInfo + created = 4 triples)
        assert len(augmented) == 4

        assert "owl:Ontology" in turtle_str
        assert "@prefix" in turtle_str

    def test_empty_namespace_name_raises(self):
        with pytest.raises(ValueError, match="namespace_name"):
            serialize(
                graph=Graph(),
                namespace_name="",
                namespace_iri=_TEST_BASE_IRI,
                timestamp=_FIXED_TS,
            )

    def test_empty_namespace_iri_raises(self):
        with pytest.raises(ValueError, match="namespace_iri"):
            serialize(
                graph=Graph(),
                namespace_name=_TEST_ONTO,
                namespace_iri="",
                timestamp=_FIXED_TS,
            )


class TestInputGraphMutation:
    """The serializer mutates the input graph in place — verify."""

    def test_input_graph_gains_ontology_block(self):
        g = Graph()
        assert len(g) == 0
        _, returned_g = serialize(
            graph=g,
            namespace_name=_TEST_ONTO,
            namespace_iri=_TEST_BASE_IRI,
            timestamp=_FIXED_TS,
        )
        assert returned_g is g
        assert (
            URIRef(_TEST_ONTOLOGY_IRI),
            RDF.type,
            OWL.Ontology,
        ) in g


class TestSizeCap:
    """Output capped at 50 MB."""

    def test_oversized_graph_raises(self, monkeypatch):
        """Force the cap to a tiny value to exercise the guard
        without actually building a 50 MB graph."""
        from coa_ontology.inducer.unstructured.services import (
            serializer,
        )

        monkeypatch.setattr(serializer, "_MAX_TURTLE_BYTES", 100)

        g = Graph()
        for i in range(20):
            g.add(
                (
                    URIRef(f"http://example.com/s{i}"),
                    RDFS.label,
                    Literal(f"label-with-some-text-{i}"),
                )
            )

        with pytest.raises(ValueError, match="exceeds 50 MB cap"):
            serializer.serialize(
                graph=g,
                namespace_name=_TEST_ONTO,
                namespace_iri=_TEST_BASE_IRI,
                timestamp=_FIXED_TS,
            )


class TestLogging:
    """Smoke check on the INFO log line."""

    def test_emits_summary_info(self, caplog):
        resolver = _resolver()
        g = _build_full_pipeline_graph(resolver)
        with caplog.at_level(logging.INFO, logger="coa_ontology.inducer.unstructured.services.serializer"):
            serialize(
                graph=g,
                namespace_name=_TEST_ONTO,
                namespace_iri=_TEST_BASE_IRI,
                timestamp=_FIXED_TS,
            )

        info_lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("Turtle" in line for line in info_lines)
