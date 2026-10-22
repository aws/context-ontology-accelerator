# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the induction rules module (Task 12.1, Requirement 10).

Covers ``induce_classes``:

- One ``owl:Class`` per classification with ``count >= floor``
- ``rdfs:label`` carries the original CamelCase ``__SYS_Class__`` value
- ``rdfs:comment`` carries the generated description (when available)
- Floor threshold filters out low-count classes
- Empty result + WARNING when no classes pass the floor
- ``exact`` band reuses the matched IRI (no minting)
- ``close_match`` band mints new IRI AND emits ``skos:closeMatch``
- ``novel`` band mints new IRI without ``skos:closeMatch``
- Defensive paths: missing normalizer entry, empty class_iri
- Standard prefixes are bound on the returned graph

IRI realism
-----------

The fixture IRIs in this file follow the real minting convention from
Requirement 7.1 / 10.2: ``http://coa.amazon.com/namespace/{ns}/
ontology/{name}/class/{normalized-label}``. The helper
``_class_iri(label)`` constructs an IRI exactly the way a real
:class:`InMemoryIRIResolver` would, so test assertions about IRI shape
catch real bugs (like the ``resolve(label, label)`` → ``resolve(label,
"class")`` fix that landed earlier).

``_real_resolver()`` returns a real :class:`InMemoryIRIResolver` wrapped
in a ``MagicMock`` spy: real minting, plus ``.resolve.assert_*``
introspection where it's needed.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from coa_common.constants import GRAPH_BASE_URI

pytestmark = pytest.mark.unit

from coa_ontology.inducer.unstructured.services.class_normalizer import (
    NormalizationResult,
)
from coa_ontology.inducer.unstructured.services.induction_rules import (
    DEFAULT_FLOOR,
    induce_classes,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import (
    InMemoryIRIResolver,
    normalize_string,
)
from coa_ontology.inducer.unstructured.stores.protocol import (
    ClassRecord,
)
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCTERMS, SKOS

# Test namespace + ontology used to construct expected IRIs throughout
# this file. Same convention as ``test_class_normalizer.py``.
_TEST_NS = "test-ns"
_TEST_ONTO = "test-onto"
_TEST_PREFIX = f"{GRAPH_BASE_URI}/namespace/{_TEST_NS}/ontology/{_TEST_ONTO}"


def _class_iri(label: str) -> str:
    """The IRI a real resolver would mint for class ``label``.

    Per Requirement 10.2, class IRIs use the literal token ``"class"``
    in the classification slot. The value (the class label) is
    normalized via the resolver's ``normalize_string`` (lowercase,
    spaces → hyphens, alphanumerics-only).
    """
    return f"{_TEST_PREFIX}/class/{normalize_string(label)}"


def _existing_class_iri(label: str) -> str:
    """An IRI that's 'already known' from a prior pipeline run.

    Same shape as ``_class_iri`` but in a separate namespace to make
    test intent explicit (the ``exact`` / ``close_match`` paths pull
    a previously-minted IRI out of the index, which would have come
    from an earlier run with the same template).
    """
    other_ns = "existing-ns"
    return f"{GRAPH_BASE_URI}/namespace/{other_ns}/ontology/{_TEST_ONTO}/class/{normalize_string(label)}"


def _nr(
    class_iri: str,
    band: str = "novel",
    similarity: float = 0.0,
    matched: str | None = None,
    preferred_label: str | None = None,
    alternative_labels: tuple[str, ...] = (),
) -> NormalizationResult:
    """Compact NormalizationResult builder for tests."""
    return NormalizationResult(
        class_iri=class_iri,
        band=band,  # type: ignore[arg-type]
        similarity_score=similarity,
        matched_existing_iri=matched,
        source_mode="schema",
        preferred_label=preferred_label,
        alternative_labels=alternative_labels,
    )


def _real_resolver() -> MagicMock:
    """Real ``InMemoryIRIResolver`` wrapped in a ``MagicMock`` spy.

    ``induce_classes`` doesn't call ``.resolve()`` directly (the IRI
    comes from the ``NormalizationResult``), but tests can still pass
    this where the signature requires an ``IRIResolver`` and the spy
    can confirm the resolver was indeed not consulted.
    """
    return MagicMock(
        wraps=InMemoryIRIResolver(
            namespace=_TEST_NS,
            ontology_name=_TEST_ONTO,
        )
    )


def _to_dict(g: Graph) -> set[tuple]:
    """Materialize a Graph as a set of ``(s, p, o)`` tuples for easy assertions."""
    return set(g.triples((None, None, None)))


class TestBasicEmission:
    """One class, novel band → one declaration with label and comment."""

    def test_emits_class_label_and_comment_for_novel_band(self):
        cls = ClassRecord(value="Company", count=10)
        nr = _nr(_class_iri("Company"), band="novel", similarity=0.5)

        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Company": "A business organization."},
            normalizer_results={"Company": nr},
            iri_resolver=_real_resolver(),
            floor=3,
        )

        triples = _to_dict(g)
        company_uri = URIRef(_class_iri("Company"))
        assert (company_uri, RDF.type, OWL.Class) in triples
        assert (
            company_uri,
            RDFS.label,
            Literal("Company", datatype=XSD.string),
        ) in triples
        assert (
            company_uri,
            RDFS.comment,
            Literal("A business organization.", datatype=XSD.string),
        ) in triples

    def test_label_is_xsd_string_typed(self):
        """Req 10.3: label must be xsd:string-typed, not a plain literal."""
        cls = ClassRecord(value="Company", count=10)
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Company": "desc"},
            normalizer_results={"Company": _nr(_class_iri("Company"))},
            iri_resolver=_real_resolver(),
        )
        labels = list(g.objects(URIRef(_class_iri("Company")), RDFS.label))
        assert len(labels) == 1
        assert labels[0].datatype == XSD.string  # type: ignore[union-attr]

    def test_comment_is_xsd_string_typed(self):
        """Req 10.4: comment must be xsd:string-typed."""
        cls = ClassRecord(value="Company", count=10)
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Company": "desc"},
            normalizer_results={"Company": _nr(_class_iri("Company"))},
            iri_resolver=_real_resolver(),
        )
        comments = list(g.objects(URIRef(_class_iri("Company")), RDFS.comment))
        assert len(comments) == 1
        assert comments[0].datatype == XSD.string  # type: ignore[union-attr]


class TestCamelCaseLabel:
    """Req 10.3: rdfs:label is the original __SYS_Class__ value (CamelCase)."""

    def test_camelcase_value_preserved_in_label(self):
        cls = ClassRecord(value="FinancialInstrument", count=10)
        # The descriptions and normalizer_results map keys are SPACED.
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Financial Instrument": "Tradable asset."},
            normalizer_results={
                "Financial Instrument": _nr(_class_iri("FinancialInstrument")),
            },
            iri_resolver=_real_resolver(),
        )
        labels = list(g.objects(URIRef(_class_iri("FinancialInstrument")), RDFS.label))
        # Label uses the CamelCase form straight from ClassRecord.value.
        assert labels[0] == Literal("FinancialInstrument", datatype=XSD.string)


class TestClassAlternativeLabels:
    """Exact class variants emit one preferred label plus SKOS alternatives."""

    def test_consolidated_labels_keep_the_original_camelcase_form(self):
        """Req 10.3 holds for every consolidated spelling, not just one.

        The normalizer and the description generator key classes by the
        spaced lookup form (``"Control Panel"``). The emitted labels must
        still be the original ``__SYS_Class__`` values, and the description
        must still be found through the spaced key.
        """
        class_iri = _class_iri("ControlPanel")
        g = induce_classes(
            class_distribution=[
                ClassRecord(value="ControlPanel", count=5),
                ClassRecord(value="制御盤", count=3),
            ],
            descriptions={"Control Panel": "A panel that houses control equipment."},
            normalizer_results={
                "Control Panel": _nr(class_iri, preferred_label="Control Panel"),
                "制御盤": _nr(
                    class_iri,
                    band="exact",
                    similarity=0.99,
                    matched=class_iri,
                    preferred_label="Control Panel",
                    alternative_labels=("制御盤",),
                ),
            },
            iri_resolver=_real_resolver(),
            namespace_language="ja",
        )

        class_uri = URIRef(class_iri)
        assert list(g.objects(class_uri, RDFS.label)) == [Literal("ControlPanel", datatype=XSD.string)]
        assert set(g.objects(class_uri, SKOS.altLabel)) == {Literal("制御盤", lang="ja")}
        assert list(g.objects(class_uri, RDFS.comment)) == [
            Literal("A panel that houses control equipment.", datatype=XSD.string)
        ]

    def test_consolidated_class_keeps_observed_surface_forms(self):
        class_iri = _class_iri("Compressor")
        g = induce_classes(
            class_distribution=[
                ClassRecord(value="Compressor", count=10),
                ClassRecord(value="コンプレッサー", count=7),
            ],
            descriptions={
                "Compressor": "Equipment that compresses a gas.",
                "コンプレッサー": "気体を圧縮する装置。",
            },
            normalizer_results={
                "Compressor": _nr(class_iri, preferred_label="Compressor"),
                "コンプレッサー": _nr(
                    class_iri,
                    band="exact",
                    similarity=0.99,
                    matched=class_iri,
                    preferred_label="Compressor",
                    alternative_labels=("コンプレッサー",),
                ),
            },
            iri_resolver=_real_resolver(),
            namespace_language="ja",
        )

        class_uri = URIRef(class_iri)
        assert list(g.objects(class_uri, RDFS.label)) == [Literal("Compressor", datatype=XSD.string)]
        assert set(g.objects(class_uri, SKOS.altLabel)) == {
            Literal("コンプレッサー", lang="ja"),
        }
        assert len(set(g.subjects(RDF.type, OWL.Class))) == 1
        assert list(g.objects(class_uri, RDFS.comment)) == [
            Literal("Equipment that compresses a gas.", datatype=XSD.string)
        ]

    def test_preferred_label_is_not_repeated_as_alternative(self):
        class_iri = _class_iri("Company")
        g = induce_classes(
            class_distribution=[ClassRecord(value="Company", count=10)],
            descriptions={},
            normalizer_results={
                "Company": _nr(
                    class_iri,
                    preferred_label="Company",
                    alternative_labels=("Company",),
                )
            },
            iri_resolver=_real_resolver(),
            namespace_language="en",
        )

        assert not list(g.objects(URIRef(class_iri), SKOS.altLabel))

    def test_preferred_label_follows_observed_frequency_not_record_order(self):
        """The dominant spelling wins even when a rarer one is seen first.

        Regression: the preferred label used to be whichever record reached
        the consolidated IRI first, so reordering ``class_distribution``
        silently changed the emitted ``rdfs:label``.
        """
        class_iri = _class_iri("Compressor")
        normalizer_results = {
            "Compressor": _nr(class_iri, preferred_label="Compressor"),
            "コンプレッサー": _nr(
                class_iri,
                band="exact",
                similarity=0.99,
                matched=class_iri,
                preferred_label="Compressor",
                alternative_labels=("コンプレッサー",),
            ),
        }
        rarer_form_first = [
            ClassRecord(value="Compressor", count=4),
            ClassRecord(value="コンプレッサー", count=9),
        ]

        g = induce_classes(
            class_distribution=rarer_form_first,
            descriptions={"Compressor": "Equipment that compresses a gas."},
            normalizer_results=normalizer_results,
            iri_resolver=_real_resolver(),
            namespace_language="ja",
        )

        class_uri = URIRef(class_iri)
        assert list(g.objects(class_uri, RDFS.label)) == [Literal("コンプレッサー", lang="ja")]
        assert set(g.objects(class_uri, SKOS.altLabel)) == {Literal("Compressor", datatype=XSD.string)}
        # The description is keyed by the alternative form, so the comment
        # must still be found after the preferred label moved.
        assert list(g.objects(class_uri, RDFS.comment)) == [
            Literal("Equipment that compresses a gas.", datatype=XSD.string)
        ]

        reversed_order = induce_classes(
            class_distribution=list(reversed(rarer_form_first)),
            descriptions={"Compressor": "Equipment that compresses a gas."},
            normalizer_results=normalizer_results,
            iri_resolver=_real_resolver(),
            namespace_language="ja",
        )
        assert isomorphic(g, reversed_order)

    def test_case_only_variants_are_folded_instead_of_emitted(self):
        """A spelling the lookup key calls identical is not an altLabel.

        ``Compressor`` and ``COMPRESSOR`` share one deterministic lookup
        key, so an altLabel for the loser would assert nothing the
        preferred label does not already say.
        """
        class_iri = _class_iri("Compressor")
        g = induce_classes(
            class_distribution=[
                ClassRecord(value="Compressor", count=3),
                ClassRecord(value="COMPRESSOR", count=90),
            ],
            descriptions={},
            normalizer_results={
                "Compressor": _nr(class_iri, preferred_label="Compressor"),
                "COMPRESSOR": _nr(
                    class_iri,
                    band="exact",
                    similarity=1.0,
                    matched=class_iri,
                    preferred_label="Compressor",
                    alternative_labels=("COMPRESSOR",),
                ),
            },
            iri_resolver=_real_resolver(),
            namespace_language="en",
        )

        class_uri = URIRef(class_iri)
        # An English namespace asserts no tags, so the label keeps the
        # untagged ``xsd:string`` form, as it does on ``main``.
        assert list(g.objects(class_uri, RDFS.label)) == [Literal("COMPRESSOR", datatype=XSD.string)]
        assert not list(g.objects(class_uri, SKOS.altLabel))

    def test_class_with_no_usable_label_is_skipped(self):
        """A consolidated IRI whose every spelling is blank emits nothing."""
        class_iri = _class_iri("Blank")
        g = induce_classes(
            class_distribution=[ClassRecord(value="   ", count=10)],
            descriptions={},
            normalizer_results={"   ": _nr(class_iri, preferred_label="  ")},
            iri_resolver=_real_resolver(),
            namespace_language="en",
        )

        assert not list(g.objects(URIRef(class_iri), RDFS.label))
        assert not list(g.subjects(RDF.type, OWL.Class))


class TestFloorThreshold:
    """Req 10.1 + 10.5: floor controls inclusion; empty graph + warning if zero pass."""

    def test_class_below_floor_skipped(self):
        cls_low = ClassRecord(value="Tiny", count=2)
        cls_ok = ClassRecord(value="Big", count=10)
        g = induce_classes(
            class_distribution=[cls_low, cls_ok],
            descriptions={"Tiny": "x", "Big": "y"},
            normalizer_results={
                "Tiny": _nr(_class_iri("Tiny")),
                "Big": _nr(_class_iri("Big")),
            },
            iri_resolver=_real_resolver(),
            floor=3,
        )
        assert (URIRef(_class_iri("Tiny")), RDF.type, OWL.Class) not in g
        assert (URIRef(_class_iri("Big")), RDF.type, OWL.Class) in g

    def test_class_at_floor_included(self):
        cls = ClassRecord(value="Edge", count=3)
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Edge": "x"},
            normalizer_results={"Edge": _nr(_class_iri("Edge"))},
            iri_resolver=_real_resolver(),
            floor=3,
        )
        assert (URIRef(_class_iri("Edge")), RDF.type, OWL.Class) in g

    def test_default_floor_is_three(self):
        assert DEFAULT_FLOOR == 3

    def test_zero_classes_pass_floor_yields_empty_graph(self, caplog):
        cls = ClassRecord(value="Tiny", count=1)
        with caplog.at_level(logging.WARNING):
            g = induce_classes(
                class_distribution=[cls],
                descriptions={"Tiny": "x"},
                normalizer_results={"Tiny": _nr(_class_iri("Tiny"))},
                iri_resolver=_real_resolver(),
                floor=3,
            )
        # Only the namespace bindings are present; no triples emitted.
        assert len(list(g.triples((None, None, None)))) == 0
        assert "zero classes passed the floor" in caplog.text

    def test_empty_distribution_yields_empty_graph(self, caplog):
        with caplog.at_level(logging.WARNING):
            g = induce_classes(
                class_distribution=[],
                descriptions={},
                normalizer_results={},
                iri_resolver=_real_resolver(),
            )
        assert len(list(g.triples((None, None, None)))) == 0
        assert "empty class_distribution" in caplog.text


class TestNormalizerBands:
    """Req 9.3 / 9.4 / 9.5 — band drives IRI reuse and skos:closeMatch."""

    def test_exact_band_reuses_matched_iri(self):
        """exact band: class_iri IS the matched_existing_iri (per Req 9.3).
        ``induce_classes`` just emits with that IRI; no resolver call."""
        cls = ClassRecord(value="Company", count=10)
        # Simulate exact band: class_iri == matched_existing_iri.
        nr = _nr(
            _existing_class_iri("Company"),
            band="exact",
            similarity=0.99,
            matched=_existing_class_iri("Company"),
        )
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Company": "desc"},
            normalizer_results={"Company": nr},
            iri_resolver=_real_resolver(),
        )
        # Class declaration uses the existing IRI.
        assert (
            URIRef(_existing_class_iri("Company")),
            RDF.type,
            OWL.Class,
        ) in g
        # NO skos:closeMatch is emitted for exact band — the class IS
        # the existing one.
        assert not list(g.triples((None, SKOS.closeMatch, None)))

    def test_close_match_band_emits_skos_close_match(self):
        cls = ClassRecord(value="Business", count=10)
        nr = _nr(
            _class_iri("Business"),
            band="close_match",
            similarity=0.87,
            matched=_existing_class_iri("Company"),
        )
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Business": "desc"},
            normalizer_results={"Business": nr},
            iri_resolver=_real_resolver(),
        )
        # New class declaration uses the freshly minted IRI.
        assert (URIRef(_class_iri("Business")), RDF.type, OWL.Class) in g
        # skos:closeMatch points to the matched existing IRI.
        assert (
            URIRef(_class_iri("Business")),
            SKOS.closeMatch,
            URIRef(_existing_class_iri("Company")),
        ) in g

    def test_novel_band_no_skos_close_match(self):
        cls = ClassRecord(value="Xyzzy", count=10)
        nr = _nr(_class_iri("Xyzzy"), band="novel", similarity=0.5)
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"Xyzzy": "desc"},
            normalizer_results={"Xyzzy": nr},
            iri_resolver=_real_resolver(),
        )
        assert (URIRef(_class_iri("Xyzzy")), RDF.type, OWL.Class) in g
        assert not list(g.triples((None, SKOS.closeMatch, None)))

    def test_close_match_with_no_matched_iri_skips_link(self):
        """Defensive: if band=close_match but matched_existing_iri is
        somehow None, no skos:closeMatch is emitted."""
        cls = ClassRecord(value="X", count=10)
        nr = _nr(
            _class_iri("X"),
            band="close_match",
            similarity=0.85,
            matched=None,  # broken upstream, should still degrade gracefully
        )
        g = induce_classes(
            class_distribution=[cls],
            descriptions={"X": "desc"},
            normalizer_results={"X": nr},
            iri_resolver=_real_resolver(),
        )
        assert (URIRef(_class_iri("X")), RDF.type, OWL.Class) in g
        assert not list(g.triples((None, SKOS.closeMatch, None)))


class TestMissingInputs:
    """Defensive paths: missing description / missing normalizer / empty IRI."""

    def test_missing_description_emits_label_only(self, caplog):
        cls = ClassRecord(value="Company", count=10)
        with caplog.at_level(logging.INFO):
            g = induce_classes(
                class_distribution=[cls],
                descriptions={},  # no description for Company
                normalizer_results={"Company": _nr(_class_iri("Company"))},
                iri_resolver=_real_resolver(),
            )
        # Class declaration + label still emitted.
        assert (URIRef(_class_iri("Company")), RDF.type, OWL.Class) in g
        assert list(g.objects(URIRef(_class_iri("Company")), RDFS.label))
        # No comment.
        assert not list(g.objects(URIRef(_class_iri("Company")), RDFS.comment))

    def test_missing_normalizer_skips_class_with_warning(self, caplog):
        cls_ok = ClassRecord(value="Good", count=10)
        cls_bad = ClassRecord(value="Bad", count=10)
        with caplog.at_level(logging.WARNING):
            g = induce_classes(
                class_distribution=[cls_ok, cls_bad],
                descriptions={"Good": "g", "Bad": "b"},
                normalizer_results={"Good": _nr(_class_iri("Good"))},
                iri_resolver=_real_resolver(),
            )
        assert (URIRef(_class_iri("Good")), RDF.type, OWL.Class) in g
        # "Bad" had no NormalizationResult → not emitted, warning logged.
        assert "Bad" in caplog.text
        # No "Bad" declaration in graph.
        assert not [s for s in g.subjects(RDF.type, OWL.Class) if "Bad" in str(s)]

    def test_empty_class_iri_skips_with_warning(self, caplog):
        """Empty class_iri (the normalizer's hard-failure signal) is skipped."""
        cls = ClassRecord(value="Broken", count=10)
        nr = _nr("", band="novel")  # empty IRI, the documented hard fail
        with caplog.at_level(logging.WARNING):
            g = induce_classes(
                class_distribution=[cls],
                descriptions={"Broken": "x"},
                normalizer_results={"Broken": nr},
                iri_resolver=_real_resolver(),
            )
        # Nothing emitted for the broken class.
        assert not list(g.subjects(RDF.type, OWL.Class))
        assert "empty class_iri" in caplog.text


class TestNamespaceBindings:
    """Standard prefixes are bound on the returned graph for readability."""

    def test_owl_rdfs_xsd_skos_bound(self):
        g = induce_classes(
            class_distribution=[],
            descriptions={},
            normalizer_results={},
            iri_resolver=_real_resolver(),
        )
        bound = {prefix: ns for prefix, ns in g.namespaces()}
        assert "owl" in bound
        assert "rdfs" in bound
        assert "xsd" in bound
        assert "skos" in bound


class TestMultipleClasses:
    """Smoke check: multiple classes produce independent declarations."""

    def test_three_classes_emit_three_declarations(self):
        records = [
            ClassRecord(value="Company", count=10),
            ClassRecord(value="Person", count=20),
            ClassRecord(value="FinancialInstrument", count=15),
        ]
        descriptions = {
            "Company": "Companies are organizations.",
            "Person": "Persons are people.",
            "Financial Instrument": "Instruments traded.",
        }
        normalizer_results = {
            "Company": _nr(_class_iri("Company")),
            "Person": _nr(_class_iri("Person")),
            "Financial Instrument": _nr(_class_iri("FinancialInstrument")),
        }
        g = induce_classes(
            class_distribution=records,
            descriptions=descriptions,
            normalizer_results=normalizer_results,
            iri_resolver=_real_resolver(),
        )
        # 3 declarations × (rdf:type + rdfs:label + rdfs:comment) = 9 triples.
        type_triples = list(g.triples((None, RDF.type, OWL.Class)))
        assert len(type_triples) == 3
        emitted_uris = {str(s) for s, _, _ in type_triples}
        assert emitted_uris == {
            _class_iri("Company"),
            _class_iri("Person"),
            _class_iri("FinancialInstrument"),
        }

    def test_close_match_one_among_many_emits_link_only_for_that_one(self):
        records = [
            ClassRecord(value="Company", count=10),
            ClassRecord(value="Business", count=10),
        ]
        descriptions = {"Company": "Companies.", "Business": "Businesses."}
        normalizer_results = {
            "Company": _nr(_class_iri("Company"), band="novel"),
            "Business": _nr(
                _class_iri("Business"),
                band="close_match",
                similarity=0.85,
                matched=_class_iri("Company"),
            ),
        }
        g = induce_classes(
            class_distribution=records,
            descriptions=descriptions,
            normalizer_results=normalizer_results,
            iri_resolver=_real_resolver(),
        )
        # Two class declarations.
        assert len(list(g.triples((None, RDF.type, OWL.Class)))) == 2
        # Exactly one skos:closeMatch link, from Business → Company.
        close_match_triples = list(g.triples((None, SKOS.closeMatch, None)))
        assert close_match_triples == [(URIRef(_class_iri("Business")), SKOS.closeMatch, URIRef(_class_iri("Company")))]


# ── Rule 2 — induce_individuals (Task 12.2, Requirement 11) ─────────────


def _individual_iri(value: str, classification: str) -> str:
    """The IRI a real resolver would mint for an individual.

    Per Requirement 11.2, A-Box individuals use the entity's classification
    label (e.g. ``"Company"``) as the classification slot — distinct from
    T-Box classes which use the literal ``"class"`` token.
    """
    return f"{_TEST_PREFIX}/{normalize_string(classification)}/{normalize_string(value)}"


def _entity(value: str, classification: str, eid: str = "") -> EntityRecord:
    """Build a test ``EntityRecord``."""
    from coa_ontology.inducer.unstructured.stores.protocol import (
        EntityRecord as _ER,
    )

    return _ER(
        entity_id=eid or f"id-{value}",
        value=value,
        classification=classification,
    )


from coa_ontology.inducer.unstructured.services.induction_rules import (  # noqa: E402
    induce_individuals,
)
from coa_ontology.inducer.unstructured.stores.protocol import (  # noqa: E402
    EntityRecord,  # re-exported for type-check at module scope
)


class TestIncludeIndividualsFlag:
    """Req 11.6 — gate flag controls whether the rule runs at all."""

    def test_default_returns_empty_graph(self):
        """Default ``include_individuals=False`` → empty graph, no work."""
        from rdflib import OWL, RDF

        entities = [_entity("Apple", "Company")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        assert not list(g.triples((None, None, None)))
        # No owl:NamedIndividual declarations.
        assert not list(g.subjects(RDF.type, OWL.NamedIndividual))

    def test_explicit_false_returns_empty_graph(self):
        entities = [_entity("Apple", "Company")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=False,
        )
        assert not list(g.triples((None, None, None)))


class TestNamedIndividualEmission:
    """Req 11.1, 11.3, 11.4 — three triples per emitted entity."""

    def test_one_individual_three_triples(self):
        from rdflib import OWL, RDF, RDFS, XSD, Literal, URIRef

        entities = [_entity("Apple", "Company")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )

        expected_iri = _individual_iri("Apple", "Company")
        expected_class_iri = _class_iri("Company")
        triples = _to_dict(g)
        # Req 11.1: owl:NamedIndividual declaration.
        assert (URIRef(expected_iri), RDF.type, OWL.NamedIndividual) in triples
        # Req 11.3: class typing.
        assert (URIRef(expected_iri), RDF.type, URIRef(expected_class_iri)) in triples
        # Req 11.4: rdfs:label, xsd:string-typed.
        assert (
            URIRef(expected_iri),
            RDFS.label,
            Literal("Apple", datatype=XSD.string),
        ) in triples

    def test_label_is_xsd_string_typed(self):
        from rdflib import RDFS, XSD, URIRef

        entities = [_entity("Apple", "Company")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )
        labels = list(g.objects(URIRef(_individual_iri("Apple", "Company")), RDFS.label))
        assert len(labels) == 1
        assert labels[0].datatype == XSD.string  # type: ignore[union-attr]

    def test_label_preserves_entity_value_verbatim(self):
        """Req 11.4: label is the entity's ``value`` exactly as supplied."""
        from rdflib import RDFS, URIRef

        entities = [_entity("Acme Corp.", "Company")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )
        # IRI uses normalized form (period stays per Pattern-1).
        expected_iri = _individual_iri("Acme Corp.", "Company")
        labels = list(g.objects(URIRef(expected_iri), RDFS.label))
        # Label is the original ``value`` string.
        assert str(labels[0]) == "Acme Corp."


class TestIRIShape:
    """Req 11.2 — individual IRIs use the entity's class as classification slot.

    This is the specific disambiguation from class IRIs (which use the
    literal ``"class"`` slot per Req 10.2).
    """

    def test_individual_iri_uses_class_slot_not_literal_class_keyword(self):
        from rdflib import RDF, URIRef

        entities = [_entity("Apple", "Company")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )

        # Individual IRI is .../Company/Apple — NOT .../class/Apple.
        individual_iri = _individual_iri("Apple", "Company")
        assert "/Company/Apple" in individual_iri
        assert "/class/Apple" not in individual_iri
        # And the corresponding triple exists.
        assert (URIRef(individual_iri), RDF.type, None) in [(s, p, None) for s, p, _ in g]

    def test_individual_iri_distinct_from_class_iri(self):
        """The class itself and an entity literally named after the class
        get distinct IRIs — this is the disambiguation Req 10.2 / 11.2
        is designed for."""
        from rdflib import RDF, URIRef

        # Entity literally named "Company", classification "Company".
        entities = [_entity("Company", "Company")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )
        individual_iri = _individual_iri("Company", "Company")
        class_iri = _class_iri("Company")
        # Distinct IRIs even though the name matches.
        assert individual_iri != class_iri
        # The individual exists.
        assert (URIRef(individual_iri), RDF.type, None) in [(s, p, None) for s, p, _ in g]
        # And the IRIs differ because of the slot:
        assert "/class/Company" in class_iri
        assert "/Company/Company" in individual_iri


class TestMissingClassIRI:
    """Req 11.5 — entity with no class IRI is skipped with a warning."""

    def test_missing_class_skips_with_warning(self, caplog):
        import logging

        from rdflib import OWL, RDF

        entities = [
            _entity("Apple", "Company"),  # ok
            _entity("Bob", "Person"),  # missing class IRI
        ]
        # Only Company has an IRI; Person was below the floor or otherwise
        # absent from Rule 1's output.
        class_iris = {"Company": _class_iri("Company")}

        with caplog.at_level(logging.WARNING):
            g = induce_individuals(
                sampled_entities=entities,
                class_iris=class_iris,
                iri_resolver=_real_resolver(),
                include_individuals=True,
            )

        # Apple is emitted, Bob is not.
        emitted = list(g.subjects(RDF.type, OWL.NamedIndividual))
        assert len(emitted) == 1
        assert "Apple" in str(emitted[0])
        # Warning mentions Bob and Person.
        assert "Bob" in caplog.text
        assert "Person" in caplog.text

    def test_all_missing_yields_empty_graph(self, caplog):
        import logging

        from rdflib import OWL, RDF

        entities = [
            _entity("Bob", "Person"),
            _entity("Alice", "Person"),
        ]
        class_iris: dict[str, str] = {}  # No classes available.

        with caplog.at_level(logging.WARNING):
            g = induce_individuals(
                sampled_entities=entities,
                class_iris=class_iris,
                iri_resolver=_real_resolver(),
                include_individuals=True,
            )
        # No declarations emitted.
        assert not list(g.subjects(RDF.type, OWL.NamedIndividual))
        # Both entities warned about.
        assert caplog.text.count("Bob") >= 1
        assert caplog.text.count("Alice") >= 1


class TestEmptyAndDefensive:
    """Empty inputs and per-entity fault isolation."""

    def test_empty_entity_list_returns_empty_graph(self):
        g = induce_individuals(
            sampled_entities=[],
            class_iris={"Company": _class_iri("Company")},
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )
        assert not list(g.triples((None, None, None)))

    def test_resolver_failure_skips_entity_with_warning(self, caplog):
        """An entity whose value normalizes to empty (e.g. all whitespace)
        causes the resolver to raise — we catch it and skip the entity."""
        import logging

        from rdflib import OWL, RDF

        entities = [
            _entity("Apple", "Company"),  # ok
            _entity("   ", "Company"),  # whitespace-only → resolver raises
            _entity("Globex", "Company"),  # ok
        ]
        class_iris = {"Company": _class_iri("Company")}

        with caplog.at_level(logging.WARNING):
            g = induce_individuals(
                sampled_entities=entities,
                class_iris=class_iris,
                iri_resolver=_real_resolver(),
                include_individuals=True,
            )

        # Apple and Globex are emitted; the whitespace-only entity is not.
        emitted = sorted(str(s) for s in g.subjects(RDF.type, OWL.NamedIndividual))
        assert len(emitted) == 2
        assert any("Apple" in s for s in emitted)
        assert any("Globex" in s for s in emitted)
        # Warning was logged about the failing entity.
        assert "resolver" in caplog.text.lower()


class TestMultipleEntities:
    """Smoke checks across multiple entities and classes."""

    def test_three_entities_three_classes(self):
        from rdflib import OWL, RDF

        entities = [
            _entity("Apple", "Company"),
            _entity("Bob", "Person"),
            _entity("Treasury Bond", "Financial Instrument"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
            "Financial Instrument": _class_iri("Financial Instrument"),
        }
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )

        # Three NamedIndividual declarations.
        named = list(g.subjects(RDF.type, OWL.NamedIndividual))
        assert len(named) == 3
        # Each one has the class typing too (rdf:type <class_iri>).
        # Triple count: 3 entities × 3 triples each = 9.
        all_triples = list(g.triples((None, None, None)))
        # Plus namespace bindings don't generate triples; the only
        # triples are the per-entity ones.
        assert len(all_triples) == 9

    def test_same_class_multiple_entities(self):
        """Two entities of the same class get the same class typing
        target."""
        from rdflib import RDF, URIRef

        entities = [
            _entity("Apple", "Company"),
            _entity("Globex", "Company"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )

        # Both individuals point to the same class.
        company_uri = URIRef(_class_iri("Company"))
        typed = list(g.subjects(RDF.type, company_uri))
        assert len(typed) == 2

    def test_idempotent_within_a_call(self):
        """Adding the same entity twice yields a single set of triples
        (rdflib graphs are sets — duplicates collapse). Verifies we
        don't double-mint."""
        from rdflib import OWL, RDF

        entities = [
            _entity("Apple", "Company"),
            _entity("Apple", "Company"),  # duplicate
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_individuals(
            sampled_entities=entities,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )
        # Set-semantics on rdflib graphs collapse identical triples.
        named = list(g.subjects(RDF.type, OWL.NamedIndividual))
        assert len(named) == 1


class TestRule2NamespaceBindings:
    """Standard prefixes are bound on the returned graph."""

    def test_standard_prefixes_bound_when_disabled(self):
        g = induce_individuals(
            sampled_entities=[],
            class_iris={},
            iri_resolver=_real_resolver(),
            include_individuals=False,
        )
        bound = {prefix: ns for prefix, ns in g.namespaces()}
        for required in ("owl", "rdfs", "xsd", "skos"):
            assert required in bound

    def test_standard_prefixes_bound_when_enabled(self):
        g = induce_individuals(
            sampled_entities=[_entity("Apple", "Company")],
            class_iris={"Company": _class_iri("Company")},
            iri_resolver=_real_resolver(),
            include_individuals=True,
        )
        bound = {prefix: ns for prefix, ns in g.namespaces()}
        for required in ("owl", "rdfs", "xsd", "skos"):
            assert required in bound


# ──────────────────────────────────────────────────────────────────────
# Rule 3 — induce_object_properties (Task 12.3, Requirement 12)
# ──────────────────────────────────────────────────────────────────────


from coa_ontology.inducer.unstructured.services.induction_rules import (  # noqa: E402
    induce_object_properties,
)
from coa_ontology.inducer.unstructured.stores.protocol import (  # noqa: E402
    FactRecord,
)


def _spo(
    subj_value: str,
    subj_class: str,
    predicate: str,
    obj_value: str,
    obj_class: str,
) -> FactRecord:
    """Build an SPO ``FactRecord`` for tests."""
    return FactRecord(
        subject_value=subj_value,
        subject_class=subj_class,
        predicate=predicate,
        object_value=obj_value,
        object_class=obj_class,
        complement=None,
    )


def _spc(
    subj_value: str,
    subj_class: str,
    predicate: str,
    complement: str,
) -> FactRecord:
    """Build an SPC (subject-predicate-complement) ``FactRecord`` for tests."""
    return FactRecord(
        subject_value=subj_value,
        subject_class=subj_class,
        predicate=predicate,
        object_value=None,
        object_class=None,
        complement=complement,
    )


def _property_iri(normalized_predicate: str, subject_class: str = "Company") -> str:
    """The IRI a real resolver would mint for an ``owl:ObjectProperty``
    after Task 12D.

    Per the amended Req 12.2, object property IRIs are minted per
    ``(normalized_predicate, subject_class)`` aggregation key. The
    classification slot is the path-normalized subject class label
    followed by the literal token ``"ObjectProperty"`` — placing the
    class segment first to keep all of a class's resources under a
    common ``.../<Subject-Class>/...`` prefix.

    Default ``subject_class`` is ``"Company"`` because most tests
    in this file use Company-subject SPO facts; override per test
    when the subject class differs.
    """
    return f"{_TEST_PREFIX}/{normalize_string(subject_class)}/ObjectProperty/{normalize_string(normalized_predicate)}"


class TestRule3BasicEmission:
    """Req 12.1, 12.2 — one ``owl:ObjectProperty`` per distinct
    normalized predicate, with the deterministic property IRI."""

    def test_single_spo_fact_emits_object_property(self):
        from rdflib import OWL, RDF, URIRef

        facts = [_spo("Apple", "Company", "operates_in", "USA", "Location")]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("operates_in"))
        assert (prop_uri, RDF.type, OWL.ObjectProperty) in g

    def test_property_iri_uses_object_property_slot(self):
        """Req 12.2 — classification slot is the literal ``"ObjectProperty"``."""
        from rdflib import OWL, RDF

        facts = [_spo("Apple", "Company", "operates_in", "USA", "Location")]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        properties = list(g.subjects(RDF.type, OWL.ObjectProperty))
        assert len(properties) == 1
        iri_str = str(properties[0])
        assert f"{_TEST_PREFIX}/Company/ObjectProperty/operates_in" == iri_str

    def test_empty_input_yields_empty_graph(self):
        g = induce_object_properties(
            facts=[],
            class_iris={"Company": _class_iri("Company")},
            iri_resolver=_real_resolver(),
        )
        assert len(g) == 0


class TestRule3DomainAndRange:
    """Req 12.3, 12.4, 12.5 — domain/range emission, including
    multiple-class fan-out for shared predicates."""

    def test_domain_and_range_for_single_fact(self):
        from rdflib import RDFS, URIRef

        facts = [_spo("Apple", "Company", "operates_in", "USA", "Location")]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("operates_in"))
        # Req 12.3 — rdfs:domain points at subject class.
        assert (prop_uri, RDFS.domain, URIRef(_class_iri("Company"))) in g
        # Req 12.4 — rdfs:range points at object class.
        assert (prop_uri, RDFS.range, URIRef(_class_iri("Location"))) in g

    def test_distinct_subject_classes_produce_distinct_properties(self):
        """Req 12.2/12.3 (Task 12D-amended): the same predicate on
        different subject classes produces TWO distinct property IRIs,
        each with exactly one rdfs:domain."""
        from rdflib import RDFS, URIRef

        facts = [
            _spo("Apple", "Company", "operates_in", "USA", "Location"),
            _spo("Bob", "Person", "operates_in", "USA", "Location"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        # Two distinct properties — one per (predicate, subject_class).
        from rdflib import OWL, RDF

        decls = list(g.subjects(RDF.type, OWL.ObjectProperty))
        assert len(decls) == 2

        company_prop = URIRef(_property_iri("operates_in", "Company"))
        person_prop = URIRef(_property_iri("operates_in", "Person"))
        assert company_prop in decls
        assert person_prop in decls

        # Each has exactly one rdfs:domain — its own subject class.
        company_domains = list(g.objects(company_prop, RDFS.domain))
        person_domains = list(g.objects(person_prop, RDFS.domain))
        assert company_domains == [URIRef(_class_iri("Company"))]
        assert person_domains == [URIRef(_class_iri("Person"))]

    def test_multiple_distinct_ranges_fan_out(self):
        """Req 12.5: shared predicate across different object classes
        produces one rdfs:range per distinct class."""
        from rdflib import RDFS, URIRef

        facts = [
            _spo("Apple", "Company", "operates_in", "USA", "Location"),
            _spo("Apple", "Company", "operates_in", "tech", "Industry"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
            "Industry": _class_iri("Industry"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("operates_in"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert URIRef(_class_iri("Location")) in ranges
        assert URIRef(_class_iri("Industry")) in ranges
        assert len(ranges) == 2

    def test_repeated_same_pair_collapses_to_one_triple(self):
        """rdflib graphs are sets — duplicate (predicate, domain, range)
        triples from many facts collapse into one each."""
        from rdflib import RDFS, URIRef

        # Three facts, same subject/object class pair.
        facts = [
            _spo("Apple", "Company", "operates_in", "USA", "Location"),
            _spo("Acme", "Company", "operates_in", "Canada", "Location"),
            _spo("Globex", "Company", "operates_in", "Mexico", "Location"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("operates_in"))
        domains = list(g.objects(prop_uri, RDFS.domain))
        ranges = list(g.objects(prop_uri, RDFS.range))
        assert len(domains) == 1
        assert len(ranges) == 1


class TestRule3PredicateNormalization:
    """Req 12.1 + Req 19.2 — surface-form variants collapse into one
    property; Req 12.6 / Req 19.3 — canonical original-spelling label."""

    def test_surface_variants_collapse_to_one_property(self):
        from rdflib import OWL, RDF

        facts = [
            _spo("a", "Company", "WORKS_FOR", "x", "Person"),
            _spo("b", "Company", "works for", "x", "Person"),
            _spo("c", "Company", "works_for", "x", "Person"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = list(g.subjects(RDF.type, OWL.ObjectProperty))
        assert len(decls) == 1
        # And the IRI uses the normalized form, in the per-class slot.
        assert str(decls[0]).endswith("/Company/ObjectProperty/works_for")

    def test_canonical_label_is_most_common_original(self):
        """Req 12.6 / Req 19.3 — most-frequent original spelling wins."""
        from rdflib import RDFS, URIRef

        facts = [
            _spo("a", "Company", "WORKS_FOR", "x", "Person"),
            _spo("b", "Company", "WORKS_FOR", "x", "Person"),
            _spo("c", "Company", "WORKS_FOR", "x", "Person"),
            _spo("d", "Company", "works_for", "x", "Person"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("works_for"))
        labels = list(g.objects(prop_uri, RDFS.label))
        assert len(labels) == 1
        assert str(labels[0]) == "WORKS_FOR"

    def test_canonical_label_lex_tiebreak(self):
        """Req 19.3 — when counts tie, lexicographic-first wins."""
        from rdflib import RDFS, URIRef

        # Both spellings appear once → tie → lex-first ("WORKS_FOR" < "works_for"
        # in Unicode order because uppercase comes first).
        facts = [
            _spo("a", "Company", "WORKS_FOR", "x", "Person"),
            _spo("b", "Company", "works_for", "x", "Person"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("works_for"))
        labels = list(g.objects(prop_uri, RDFS.label))
        assert len(labels) == 1
        assert str(labels[0]) == "WORKS_FOR"

    def test_noncanonical_surface_forms_are_alt_labels(self):
        facts = [
            _spo("a", "Company", "WORKS_FOR", "x", "Person"),
            _spo("b", "Company", "WORKS_FOR", "y", "Person"),
            _spo("c", "Company", "works for", "z", "Person"),
        ]
        g = induce_object_properties(
            facts=facts,
            class_iris={
                "Company": _class_iri("Company"),
                "Person": _class_iri("Person"),
            },
            iri_resolver=_real_resolver(),
            namespace_language="en",
        )

        prop_uri = URIRef(_property_iri("works_for"))
        assert list(g.objects(prop_uri, RDFS.label)) == [Literal("WORKS_FOR", datatype=XSD.string)]
        assert set(g.objects(prop_uri, SKOS.altLabel)) == {
            Literal("works for", datatype=XSD.string),
        }

    def test_padding_and_case_noise_do_not_become_alt_labels(self):
        """Surface forms differing only by padding/case fold into one.

        Regression: every raw spelling became a ``skos:altLabel``, so a
        predicate could carry alternatives like ``"  DELIVERED TO  "`` that
        repeat the preferred label with different whitespace.
        """
        facts = [
            _spo("a", "Company", "DELIVERED TO", "x", "Person"),
            _spo("b", "Company", "DELIVERED TO", "y", "Person"),
            _spo("c", "Company", "  DELIVERED TO  ", "z", "Person"),
            _spo("d", "Company", "delivered to", "w", "Person"),
            _spo("e", "Company", "Delivered To", "v", "Person"),
            _spo("f", "Company", "DELIVERED_TO", "u", "Person"),
        ]
        g = induce_object_properties(
            facts=facts,
            class_iris={
                "Company": _class_iri("Company"),
                "Person": _class_iri("Person"),
            },
            iri_resolver=_real_resolver(),
            namespace_language="en",
        )

        prop_uri = URIRef(_property_iri("delivered_to"))
        assert list(g.objects(prop_uri, RDFS.label)) == [Literal("DELIVERED TO", datatype=XSD.string)]
        # ``DELIVERED_TO`` keeps its own lookup key (underscore vs space), so
        # it survives folding; the padding and casing variants do not. An
        # English namespace asserts no tags, so every label stays untagged.
        assert set(g.objects(prop_uri, SKOS.altLabel)) == {Literal("DELIVERED_TO", datatype=XSD.string)}

    def test_label_is_xsd_string_typed(self):
        from rdflib import RDFS, XSD, URIRef

        facts = [_spo("a", "Company", "operates_in", "x", "Location")]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("operates_in"))
        labels = list(g.objects(prop_uri, RDFS.label))
        assert len(labels) == 1
        assert labels[0].datatype == XSD.string  # type: ignore[union-attr]


class TestRule3MissingClassFiltering:
    """Req 12.7 — facts referencing a class below the floor (i.e. not
    in ``class_iris``) are skipped; the property gets no contribution
    from them, and is omitted entirely if all its facts are skipped."""

    def test_subject_class_below_floor_skips_fact(self):
        from rdflib import OWL, RDF

        facts = [
            _spo("a", "RareClass", "operates_in", "x", "Location"),
            _spo("b", "Company", "operates_in", "y", "Location"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
            # "RareClass" deliberately omitted.
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = list(g.subjects(RDF.type, OWL.ObjectProperty))
        assert len(decls) == 1
        # Only Company is a domain — RareClass was filtered out.
        from rdflib import RDFS, URIRef

        prop_uri = URIRef(_property_iri("operates_in"))
        domains = set(g.objects(prop_uri, RDFS.domain))
        assert URIRef(_class_iri("Company")) in domains
        assert len(domains) == 1

    def test_object_class_below_floor_skips_fact(self):
        from rdflib import RDFS, URIRef

        facts = [
            _spo("a", "Company", "operates_in", "x", "RareClass"),
            _spo("b", "Company", "operates_in", "y", "Location"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("operates_in"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert URIRef(_class_iri("Location")) in ranges
        assert len(ranges) == 1

    def test_all_facts_filtered_omits_property(self):
        """Req 12.7 (corollary): if every fact for a predicate is
        skipped, no property declaration is emitted."""
        from rdflib import OWL, RDF

        facts = [
            _spo("a", "RareClass", "operates_in", "x", "Location"),
        ]
        class_iris = {
            "Location": _class_iri("Location"),
            # RareClass missing → fact skipped → predicate unobserved.
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        assert not list(g.subjects(RDF.type, OWL.ObjectProperty))

    def test_canonical_label_only_counts_kept_facts(self):
        """When facts are filtered out due to missing class IRIs, those
        original predicate strings shouldn't influence label selection.

        Three facts with predicate ``"BIG_LOUD_NAME"`` are filtered out
        (RareClass below floor). One kept fact uses ``"works_for"``.
        The canonical label should be ``"works_for"`` — not the
        more-numerous-but-filtered-out variant.
        """
        from rdflib import RDFS, URIRef

        facts = [
            _spo("a", "RareClass", "BIG_LOUD_NAME", "x", "Person"),
            _spo("b", "RareClass", "BIG_LOUD_NAME", "x", "Person"),
            _spo("c", "RareClass", "BIG_LOUD_NAME", "x", "Person"),
            _spo("d", "Company", "works_for", "x", "Person"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
            # RareClass missing → first three skipped.
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_property_iri("works_for"))
        labels = list(g.objects(prop_uri, RDFS.label))
        assert len(labels) == 1
        assert str(labels[0]) == "works_for"


class TestRule3SPCFiltering:
    """SPC facts (no object entity) are filtered out — Rule 4 will
    handle them. Mixing SPO and SPC in the same input must not break
    Rule 3."""

    def test_spc_fact_alone_yields_empty_graph(self):
        from rdflib import OWL, RDF

        facts = [_spc("Apple", "Company", "founded", "1976")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        assert not list(g.subjects(RDF.type, OWL.ObjectProperty))

    def test_mixed_spo_spc_keeps_only_spo(self):
        from rdflib import OWL, RDF

        facts = [
            _spo("Apple", "Company", "operates_in", "USA", "Location"),
            _spc("Apple", "Company", "founded", "1976"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = list(g.subjects(RDF.type, OWL.ObjectProperty))
        # Only the SPO contributed a property.
        assert len(decls) == 1
        assert str(decls[0]).endswith("/Company/ObjectProperty/operates_in")


class TestRule3DefensivePaths:
    """Edge cases: empty predicate, resolver failure isolation."""

    def test_predicate_normalizes_to_empty_skips_with_warning(self, caplog):
        """A predicate that strips to empty (e.g. ``"!!!"``) cannot
        produce a valid IRI — skip with WARNING, don't crash."""
        from rdflib import OWL, RDF

        facts = [
            _spo("a", "Company", "!!!", "x", "Location"),
            _spo("b", "Company", "operates_in", "y", "Location"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        with caplog.at_level(logging.WARNING):
            g = induce_object_properties(
                facts=facts,
                class_iris=class_iris,
                iri_resolver=_real_resolver(),
            )

        decls = list(g.subjects(RDF.type, OWL.ObjectProperty))
        # Only the well-formed predicate produced a property.
        assert len(decls) == 1
        assert str(decls[0]).endswith("/Company/ObjectProperty/operates_in")
        # And the bad one was warned about.
        assert any("normalizes to empty" in rec.message for rec in caplog.records)

    def test_resolver_failure_isolates_to_one_predicate(self, caplog):
        """A resolver exception for one predicate must not stop the
        others from being emitted."""
        from rdflib import OWL, RDF

        facts = [
            _spo("a", "Company", "operates_in", "x", "Location"),
            _spo("b", "Company", "works_for", "x", "Person"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
            "Location": _class_iri("Location"),
        }

        # Wrap a real resolver but intercept ``resolve`` for one
        # predicate.
        real = InMemoryIRIResolver(namespace=_TEST_NS, ontology_name=_TEST_ONTO)
        spy = MagicMock(wraps=real)

        def _selective_resolve(value, classification):
            if value == "operates_in":
                raise RuntimeError("intentional test failure")
            return real.resolve(value, classification)

        spy.resolve.side_effect = _selective_resolve

        with caplog.at_level(logging.WARNING):
            g = induce_object_properties(
                facts=facts,
                class_iris=class_iris,
                iri_resolver=spy,
            )

        # The good one was emitted.
        decls = [str(s) for s in g.subjects(RDF.type, OWL.ObjectProperty)]
        assert any(d.endswith("/Company/ObjectProperty/works_for") for d in decls)
        # The bad one was NOT emitted.
        assert not any(d.endswith("/Company/ObjectProperty/operates_in") for d in decls)
        # And there's a warning naming the predicate.
        assert any("operates_in" in rec.message and "resolver" in rec.message.lower() for rec in caplog.records)


class TestRule3MultipleProperties:
    """Multiple predicates emit independent property declarations."""

    def test_three_predicates_three_properties(self):
        from rdflib import OWL, RDF

        facts = [
            _spo("a", "Company", "operates_in", "x", "Location"),
            _spo("b", "Company", "owns", "y", "Product"),
            _spo("c", "Person", "works_for", "z", "Company"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
            "Product": _class_iri("Product"),
            "Location": _class_iri("Location"),
        }
        g = induce_object_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = {str(s) for s in g.subjects(RDF.type, OWL.ObjectProperty)}
        assert any(d.endswith("/Company/ObjectProperty/operates_in") for d in decls)
        assert any(d.endswith("/Company/ObjectProperty/owns") for d in decls)
        assert any(d.endswith("/Person/ObjectProperty/works_for") for d in decls)
        assert len(decls) == 3


class TestRule3NamespaceBindings:
    """Standard prefixes are bound on the returned graph."""

    def test_standard_prefixes_bound(self):
        g = induce_object_properties(
            facts=[],
            class_iris={},
            iri_resolver=_real_resolver(),
        )
        bound = {prefix: ns for prefix, ns in g.namespaces()}
        for required in ("owl", "rdfs", "xsd", "skos"):
            assert required in bound

    def test_empty_facts_logs_info_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            induce_object_properties(
                facts=[],
                class_iris={"Company": _class_iri("Company")},
                iri_resolver=_real_resolver(),
            )
        # Empty input is normal — no warning expected.
        assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)


# ──────────────────────────────────────────────────────────────────────
# Rule 4 — induce_datatype_properties (Task 12.4, Requirement 13)
# ──────────────────────────────────────────────────────────────────────


from coa_ontology.inducer.unstructured.services.induction_rules import (  # noqa: E402
    induce_datatype_properties,
)


def _datatype_property_iri(normalized_predicate: str, subject_class: str = "Company") -> str:
    """The IRI a real resolver would mint for an ``owl:DatatypeProperty``
    after Task 12D.

    Per the amended Req 13.3, datatype property IRIs are minted per
    ``(normalized_predicate, subject_class)`` aggregation key. The
    classification slot is the path-normalized subject class label
    followed by the literal token ``"DatatypeProperty"`` — placing
    the class segment first to keep all of a class's resources under
    a common ``.../<Subject-Class>/...`` prefix.

    Default ``subject_class`` is ``"Company"`` because most tests
    in this file use Company-subject SPC facts; override per test
    when the subject class differs.
    """
    return f"{_TEST_PREFIX}/{normalize_string(subject_class)}/DatatypeProperty/{normalize_string(normalized_predicate)}"


class TestRule4BasicEmission:
    """Req 13.1, 13.2, 13.3 — one ``owl:DatatypeProperty`` per distinct
    normalized predicate, with the deterministic property IRI."""

    def test_single_spc_fact_emits_datatype_property(self):
        from rdflib import OWL, RDF, URIRef

        facts = [_spc("Apple", "Company", "founded", "1976")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("founded"))
        assert (prop_uri, RDF.type, OWL.DatatypeProperty) in g

    def test_property_iri_uses_datatype_property_slot(self):
        """Req 13.3 (Task 12D-amended) — classification slot is
        ``"DatatypeProperty/<subject_class>"``."""
        from rdflib import OWL, RDF

        facts = [_spc("Apple", "Company", "founded", "1976")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        properties = list(g.subjects(RDF.type, OWL.DatatypeProperty))
        assert len(properties) == 1
        iri_str = str(properties[0])
        assert f"{_TEST_PREFIX}/Company/DatatypeProperty/founded" == iri_str

    def test_empty_input_yields_empty_graph(self):
        g = induce_datatype_properties(
            facts=[],
            class_iris={"Company": _class_iri("Company")},
            iri_resolver=_real_resolver(),
        )
        assert len(g) == 0


class TestRule4DomainAndRange:
    """Req 13.4, 13.5, 13.6 — domain emission, XSD range inference,
    multi-class domain fan-out for shared predicates."""

    def test_domain_for_single_fact(self):
        from rdflib import RDFS, URIRef

        facts = [_spc("Apple", "Company", "founded", "1976")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("founded"))
        # Req 13.4 — rdfs:domain points at subject class.
        assert (prop_uri, RDFS.domain, URIRef(_class_iri("Company"))) in g

    def test_xsd_integer_range_inferred_from_unanimous_integers(self):
        """Req 13.5 + Req 18.1 + Req 18.8 — unanimous integer values
        yield ``xsd:integer`` range."""
        from rdflib import RDFS, XSD, URIRef

        facts = [
            _spc("Apple", "Company", "founded", "1976"),
            _spc("Microsoft", "Company", "founded", "1975"),
            _spc("Amazon", "Company", "founded", "1994"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("founded"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert ranges == {XSD.integer}

    def test_xsd_decimal_range_inferred_from_unanimous_decimals(self):
        """Req 18.2 — unanimous decimal values yield ``xsd:decimal``."""
        from rdflib import RDFS, XSD, URIRef

        facts = [
            _spc("Apple", "Company", "revenue", "394.328"),
            _spc("Microsoft", "Company", "revenue", "211.915"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("revenue"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert ranges == {XSD.decimal}

    def test_xsd_boolean_range_inferred_from_unanimous_booleans(self):
        """Req 18.3 — unanimous boolean values yield ``xsd:boolean``."""
        from rdflib import RDFS, XSD, URIRef

        facts = [
            _spc("Apple", "Company", "is_public", "true"),
            _spc("Microsoft", "Company", "is_public", "true"),
            _spc("PrivateCo", "Company", "is_public", "false"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("is_public"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert ranges == {XSD.boolean}

    def test_xsd_date_range_inferred(self):
        """Req 18.4 — unanimous ISO-8601 date values yield ``xsd:date``."""
        from rdflib import RDFS, XSD, URIRef

        facts = [
            _spc("Apple", "Company", "ipo_date", "1980-12-12"),
            _spc("Microsoft", "Company", "ipo_date", "1986-03-13"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("ipo_date"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert ranges == {XSD.date}

    def test_xsd_string_range_default_for_freeform_text(self):
        """Req 18.6 — values that match no pattern default to ``xsd:string``."""
        from rdflib import RDFS, XSD, URIRef

        facts = [
            _spc("Apple", "Company", "headquarters", "Cupertino, CA"),
            _spc("Microsoft", "Company", "headquarters", "Redmond, WA"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("headquarters"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert ranges == {XSD.string}

    def test_xsd_string_for_mixed_types(self):
        """Req 18.7 — when complement values infer to different types,
        the property's range is ``xsd:string``."""
        from rdflib import RDFS, XSD, URIRef

        facts = [
            _spc("Apple", "Company", "size", "150000"),  # integer
            _spc("Microsoft", "Company", "size", "large"),  # string
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("size"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert ranges == {XSD.string}

    def test_distinct_subject_classes_produce_distinct_properties(self):
        """Req 13.3 (Task 12D-amended) — same predicate across different
        subject classes produces TWO distinct ``owl:DatatypeProperty``
        declarations, each with exactly one ``rdfs:domain``."""
        from rdflib import OWL, RDF, RDFS, URIRef

        facts = [
            _spc("Apple", "Company", "founded", "1976"),
            _spc("Bob", "Person", "founded", "1965"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
        }
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = list(g.subjects(RDF.type, OWL.DatatypeProperty))
        assert len(decls) == 2

        company_prop = URIRef(_datatype_property_iri("founded", "Company"))
        person_prop = URIRef(_datatype_property_iri("founded", "Person"))
        assert company_prop in decls
        assert person_prop in decls

        # Each has exactly one rdfs:domain — its own subject class.
        company_domains = list(g.objects(company_prop, RDFS.domain))
        person_domains = list(g.objects(person_prop, RDFS.domain))
        assert company_domains == [URIRef(_class_iri("Company"))]
        assert person_domains == [URIRef(_class_iri("Person"))]

    def test_xsd_inference_is_per_subject_class(self):
        """Req 13.5 (Task 12D-amended): the XSD inference operates
        within each (predicate, subject_class) aggregation, NOT pooled
        across subject classes.

        ``Vehicle.weight`` (in tons, decimal) and ``Person.weight``
        (in kg, integer) infer to ``xsd:decimal`` and ``xsd:integer``
        respectively. Under the old contract these would have been
        pooled and inferred to ``xsd:string`` because the pool would
        be mixed. After 12D each property has its own clean inference.
        """
        from rdflib import RDFS, XSD, URIRef

        facts = [
            # Vehicle.weight in tons (decimal)
            _spc("Truck", "Vehicle", "weight", "2.5"),
            _spc("Car", "Vehicle", "weight", "1.4"),
            # Person.weight in kg (integer)
            _spc("Alice", "Person", "weight", "70"),
            _spc("Bob", "Person", "weight", "80"),
        ]
        class_iris = {
            "Vehicle": _class_iri("Vehicle"),
            "Person": _class_iri("Person"),
        }
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        vehicle_prop = URIRef(_datatype_property_iri("weight", "Vehicle"))
        person_prop = URIRef(_datatype_property_iri("weight", "Person"))

        # Per-class XSD inference: vehicle decimals → xsd:decimal,
        # person integers → xsd:integer. Critically, NEITHER is
        # xsd:string (which is what a pooled inference would yield).
        vehicle_ranges = set(g.objects(vehicle_prop, RDFS.range))
        person_ranges = set(g.objects(person_prop, RDFS.range))
        assert vehicle_ranges == {XSD.decimal}
        assert person_ranges == {XSD.integer}


class TestRule4LabelAndComment:
    """Req 13.7, 13.8, 19.3 — canonical-by-frequency label and
    observation-frequency comment."""

    def test_canonical_label_is_most_common_original(self):
        from rdflib import RDFS, URIRef

        facts = [
            _spc("a", "Company", "FOUNDED", "1976"),
            _spc("b", "Company", "FOUNDED", "1975"),
            _spc("c", "Company", "FOUNDED", "1994"),
            _spc("d", "Company", "founded", "2000"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("founded"))
        labels = list(g.objects(prop_uri, RDFS.label))
        assert len(labels) == 1
        assert str(labels[0]) == "FOUNDED"

    def test_noncanonical_surface_forms_are_alt_labels(self):
        facts = [
            _spc("a", "Company", "MODEL_NO", "A-1"),
            _spc("b", "Company", "MODEL_NO", "B-2"),
            _spc("c", "Company", "model no", "C-3"),
        ]
        g = induce_datatype_properties(
            facts=facts,
            class_iris={"Company": _class_iri("Company")},
            iri_resolver=_real_resolver(),
            namespace_language="en",
        )

        prop_uri = URIRef(_datatype_property_iri("model_no"))
        assert list(g.objects(prop_uri, RDFS.label)) == [Literal("MODEL_NO", datatype=XSD.string)]
        assert set(g.objects(prop_uri, SKOS.altLabel)) == {
            Literal("model no", datatype=XSD.string),
        }

    def test_label_is_xsd_string_typed(self):
        from rdflib import RDFS, XSD, URIRef

        facts = [_spc("a", "Company", "founded", "1976")]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("founded"))
        labels = list(g.objects(prop_uri, RDFS.label))
        assert len(labels) == 1
        assert labels[0].datatype == XSD.string  # type: ignore[union-attr]

    def test_observation_frequency_comment_single_class(self):
        """Req 13.8 — the comment names the count; the class comes from rdfs:domain.

        The class name is deliberately NOT interpolated into the sentence. It comes
        from the corpus, so an English frame around it manufactures a literal
        holding two languages on a non-English corpus ("Observed across 1 fact(s)
        in class 技術的特性"). RDF 1.1 cannot language-tag such a literal, and no
        extraction-prompt change can remove it because we generate it ourselves.
        """
        from rdflib import RDFS, XSD, URIRef

        facts = [
            _spc("Apple", "Company", "founded", "1976"),
            _spc("Microsoft", "Company", "founded", "1975"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("founded"))
        comments = list(g.objects(prop_uri, RDFS.comment))
        assert len(comments) == 1
        comment = comments[0]
        assert comment.datatype == XSD.string  # type: ignore[union-attr]
        text = str(comment)
        assert "2" in text  # fact count
        # No corpus-derived text at all — that is what keeps the literal
        # single-language whatever the corpus is written in.
        assert text.isascii()
        # The class is not lost: rdfs:domain carries it, which is where a consumer
        # should read it from anyway.
        assert URIRef(_class_iri("Company")) in set(g.objects(prop_uri, RDFS.domain))

    def test_observation_frequency_comment_per_subject_class(self):
        """Req 13.8 (Task 12D-amended) — the same predicate observed on TWO
        subject classes produces TWO distinct properties, each with its own
        observation-frequency comment and its own ``rdfs:domain``.

        The per-class distinction is asserted on ``rdfs:domain`` rather than on the
        comment text: the class name no longer appears in the sentence, because
        interpolating corpus-derived text into an English frame manufactures a
        mixed-language literal (see the single-class test above).
        """
        from rdflib import RDFS, URIRef

        facts = [
            _spc("Apple", "Company", "founded", "1976"),
            _spc("Microsoft", "Company", "founded", "1975"),
            _spc("Bob", "Person", "founded", "1965"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Person": _class_iri("Person"),
        }
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        # Two distinct properties — one per (predicate, subject_class).
        company_prop = URIRef(_datatype_property_iri("founded", "Company"))
        person_prop = URIRef(_datatype_property_iri("founded", "Person"))

        company_comments = list(g.objects(company_prop, RDFS.comment))
        assert len(company_comments) == 1
        company_text = str(company_comments[0])
        assert "2" in company_text  # two facts on Company

        person_comments = list(g.objects(person_prop, RDFS.comment))
        assert len(person_comments) == 1
        person_text = str(person_comments[0])
        assert "1" in person_text  # one fact on Person

        # Each property is still attributed to exactly its own class, via domain.
        assert set(g.objects(company_prop, RDFS.domain)) == {URIRef(_class_iri("Company"))}
        assert set(g.objects(person_prop, RDFS.domain)) == {URIRef(_class_iri("Person"))}


class TestRule4MissingClassFiltering:
    """Req 13.9 — facts whose subject class is below the floor are
    skipped; the property is omitted entirely if all its facts are
    skipped."""

    def test_subject_class_below_floor_skips_fact(self):
        from rdflib import OWL, RDF, RDFS, URIRef

        facts = [
            _spc("a", "RareClass", "founded", "1900"),
            _spc("b", "Company", "founded", "1976"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            # "RareClass" deliberately omitted.
        }
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = list(g.subjects(RDF.type, OWL.DatatypeProperty))
        assert len(decls) == 1
        # Only Company is a domain.
        prop_uri = URIRef(_datatype_property_iri("founded"))
        domains = set(g.objects(prop_uri, RDFS.domain))
        assert URIRef(_class_iri("Company")) in domains
        assert len(domains) == 1

    def test_all_facts_filtered_omits_property(self):
        from rdflib import OWL, RDF

        facts = [
            _spc("a", "RareClass", "founded", "1900"),
        ]
        class_iris = {
            # RareClass missing.
            "Company": _class_iri("Company"),
        }
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        assert not list(g.subjects(RDF.type, OWL.DatatypeProperty))

    def test_xsd_inference_only_uses_kept_facts(self):
        """When facts are filtered out due to missing class IRIs,
        their complement values must NOT influence the XSD inference.
        """
        from rdflib import RDFS, XSD, URIRef

        # Three filtered-out facts have non-numeric complements; the
        # surviving fact has an integer. The result must be xsd:integer
        # — using only the kept fact.
        facts = [
            _spc("a", "RareClass", "size", "huge"),
            _spc("b", "RareClass", "size", "tiny"),
            _spc("c", "RareClass", "size", "medium"),
            _spc("d", "Company", "size", "150000"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop_uri = URIRef(_datatype_property_iri("size"))
        ranges = set(g.objects(prop_uri, RDFS.range))
        assert ranges == {XSD.integer}


class TestRule4SPOFiltering:
    """SPO facts (object_value populated) are filtered out — Rule 3
    handles them. Mixing SPO and SPC in the same input must not break
    Rule 4."""

    def test_spo_fact_alone_yields_empty_graph(self):
        from rdflib import OWL, RDF

        facts = [_spo("Apple", "Company", "operates_in", "USA", "Location")]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        assert not list(g.subjects(RDF.type, OWL.DatatypeProperty))

    def test_mixed_spo_spc_keeps_only_spc(self):
        from rdflib import OWL, RDF

        facts = [
            _spo("Apple", "Company", "operates_in", "USA", "Location"),
            _spc("Apple", "Company", "founded", "1976"),
        ]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = list(g.subjects(RDF.type, OWL.DatatypeProperty))
        # Only the SPC contributed a property.
        assert len(decls) == 1
        assert str(decls[0]).endswith("/Company/DatatypeProperty/founded")


class TestRule4DefensivePaths:
    """Edge cases: empty predicate, resolver failure isolation."""

    def test_predicate_normalizes_to_empty_skips_with_warning(self, caplog):
        from rdflib import OWL, RDF

        facts = [
            _spc("a", "Company", "!!!", "1900"),
            _spc("b", "Company", "founded", "1976"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        with caplog.at_level(logging.WARNING):
            g = induce_datatype_properties(
                facts=facts,
                class_iris=class_iris,
                iri_resolver=_real_resolver(),
            )

        decls = list(g.subjects(RDF.type, OWL.DatatypeProperty))
        # Only the well-formed predicate produced a property.
        assert len(decls) == 1
        assert str(decls[0]).endswith("/Company/DatatypeProperty/founded")
        # And the bad one was warned about.
        assert any("normalizes to empty" in rec.message for rec in caplog.records)

    def test_resolver_failure_isolates_to_one_predicate(self, caplog):
        """A resolver exception for one predicate must not stop the
        others from being emitted."""
        from rdflib import OWL, RDF

        facts = [
            _spc("a", "Company", "founded", "1976"),
            _spc("b", "Company", "revenue", "100.5"),
        ]
        class_iris = {"Company": _class_iri("Company")}

        real = InMemoryIRIResolver(namespace=_TEST_NS, ontology_name=_TEST_ONTO)
        spy = MagicMock(wraps=real)

        def _selective_resolve(value, classification):
            if value == "founded":
                raise RuntimeError("intentional test failure")
            return real.resolve(value, classification)

        spy.resolve.side_effect = _selective_resolve

        with caplog.at_level(logging.WARNING):
            g = induce_datatype_properties(
                facts=facts,
                class_iris=class_iris,
                iri_resolver=spy,
            )

        decls = [str(s) for s in g.subjects(RDF.type, OWL.DatatypeProperty)]
        assert any(d.endswith("/Company/DatatypeProperty/revenue") for d in decls)
        assert not any(d.endswith("/Company/DatatypeProperty/founded") for d in decls)
        # Warning naming the predicate.
        assert any("founded" in rec.message and "resolver" in rec.message.lower() for rec in caplog.records)


class TestRule4SamePredicateAsObjectAndDatatype:
    """If a predicate appears in BOTH SPO and SPC facts, Rule 3 emits
    an ObjectProperty and Rule 4 emits a DatatypeProperty at distinct
    IRIs (different classification slots)."""

    def test_object_and_datatype_iris_are_distinct(self):
        # Run Rule 3 and Rule 4 with the same predicate appearing
        # in both SPO and SPC contexts. Verify the two properties
        # land at distinct IRIs.
        from rdflib import OWL, RDF

        spo_facts = [_spo("Apple", "Company", "based", "USA", "Location")]
        spc_facts = [_spc("Microsoft", "Company", "based", "Redmond, WA")]
        class_iris = {
            "Company": _class_iri("Company"),
            "Location": _class_iri("Location"),
        }
        resolver = _real_resolver()

        rule3_g = induce_object_properties(facts=spo_facts, class_iris=class_iris, iri_resolver=resolver)
        rule4_g = induce_datatype_properties(facts=spc_facts, class_iris=class_iris, iri_resolver=resolver)

        op_iris = list(rule3_g.subjects(RDF.type, OWL.ObjectProperty))
        dp_iris = list(rule4_g.subjects(RDF.type, OWL.DatatypeProperty))
        assert len(op_iris) == 1
        assert len(dp_iris) == 1
        assert str(op_iris[0]) != str(dp_iris[0])
        assert str(op_iris[0]).endswith("/Company/ObjectProperty/based")
        assert str(dp_iris[0]).endswith("/Company/DatatypeProperty/based")


class TestRule4MultipleProperties:
    """Multiple predicates emit independent declarations."""

    def test_three_predicates_three_properties(self):
        from rdflib import OWL, RDF

        facts = [
            _spc("Apple", "Company", "founded", "1976"),
            _spc("Apple", "Company", "revenue", "394.328"),
            _spc("Apple", "Company", "is_public", "true"),
        ]
        class_iris = {"Company": _class_iri("Company")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        decls = {str(s) for s in g.subjects(RDF.type, OWL.DatatypeProperty)}
        assert any(d.endswith("/Company/DatatypeProperty/founded") for d in decls)
        assert any(d.endswith("/Company/DatatypeProperty/revenue") for d in decls)
        assert any(d.endswith("/Company/DatatypeProperty/is_public") for d in decls)
        assert len(decls) == 3


class TestRule4NamespaceBindings:
    """Standard prefixes are bound on the returned graph."""

    def test_standard_prefixes_bound(self):
        g = induce_datatype_properties(
            facts=[],
            class_iris={},
            iri_resolver=_real_resolver(),
        )
        bound = {prefix: ns for prefix, ns in g.namespaces()}
        for required in ("owl", "rdfs", "xsd", "skos"):
            assert required in bound

    def test_empty_facts_logs_info_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            induce_datatype_properties(
                facts=[],
                class_iris={"Company": _class_iri("Company")},
                iri_resolver=_real_resolver(),
            )
        assert not any(rec.levelno >= logging.WARNING for rec in caplog.records)


# ── Rule 5 — induce_concepts (Task 12.5, Requirement 14) ────────────────


from coa_ontology.inducer.unstructured.services.induction_rules import (  # noqa: E402
    MAX_CONCEPT_CLASS_LOOKUP_ENTRIES,
    induce_concepts,
)
from coa_ontology.inducer.unstructured.services.iri_resolver import (  # noqa: E402
    normalize_for_iri_path,
)
from coa_ontology.inducer.unstructured.stores.protocol import (  # noqa: E402
    TopicRecord,
)

# Test namespace name used to assert against ConceptScheme IRI / label.
# Reuses the unit-test prefix so all IRIs share the same shape.
_TOPIC_NS = "test-ns"


def _topic(value: str, topic_id: str = "t1", count: int = 0) -> TopicRecord:
    """Compact TopicRecord builder for tests."""
    return TopicRecord(topic_id=topic_id, value=value, statement_count=count)


def _topic_iri(value: str) -> str:
    """The IRI a real resolver would mint for a topic with this value.

    Per Requirement 14.2, topic concept IRIs use the literal token
    ``"Topic"`` in the classification slot. The value (the topic
    string) is normalized via the resolver's case-preserving
    Pattern-1 form (spaces → ``-``, alphanumerics preserved).
    """
    return f"{_TEST_PREFIX}/Topic/{normalize_for_iri_path(value)}"


def _scheme_iri(namespace_name: str = _TOPIC_NS) -> str:
    """The ConceptScheme IRI a real resolver would mint."""
    return f"{_TEST_PREFIX}/ConceptScheme/{normalize_for_iri_path(namespace_name)}"


class TestSchemeAlwaysEmitted:
    """Req 14.4 / 14.5 — the ConceptScheme is emitted on every run.

    Even when the topic input is empty, the scheme is a meaningful
    pipeline artifact (a future run can attach concepts to it).
    """

    def test_empty_topics_still_emits_scheme(self):
        g = induce_concepts(
            topics=[],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )

        scheme_uri = URIRef(_scheme_iri())
        assert (scheme_uri, RDF.type, SKOS.ConceptScheme) in g
        assert (
            scheme_uri,
            RDFS.label,
            Literal(f"{_TOPIC_NS} Topic Scheme", datatype=XSD.string),
        ) in g
        # No concepts emitted.
        assert not list(g.subjects(RDF.type, SKOS.Concept))

    def test_scheme_label_uses_namespace_name(self):
        """Req 14.5 — label literal is exactly ``"<ns> Topic Scheme"``."""
        g = induce_concepts(
            topics=[],
            iri_resolver=_real_resolver(),
            namespace_name="my-graph",
        )
        scheme_uri = URIRef(f"{_TEST_PREFIX}/ConceptScheme/my-graph")
        labels = list(g.objects(scheme_uri, RDFS.label))
        assert len(labels) == 1
        lit = labels[0]
        assert isinstance(lit, Literal)
        assert str(lit) == "my-graph Topic Scheme"
        assert lit.datatype == XSD.string

    def test_scheme_emitted_when_all_topics_skipped(self):
        """Topics with empty values are skipped, but scheme is still emitted."""
        g = induce_concepts(
            topics=[_topic("", topic_id="t-empty"), _topic("   ", topic_id="t-ws")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        scheme_uri = URIRef(_scheme_iri())
        assert (scheme_uri, RDF.type, SKOS.ConceptScheme) in g
        # No concepts emitted.
        assert not list(g.subjects(RDF.type, SKOS.Concept))

    def test_missing_namespace_raises(self):
        """The function refuses to run without a namespace_name."""
        with pytest.raises(ValueError, match="namespace_name"):
            induce_concepts(
                topics=[_topic("Some Topic")],
                iri_resolver=_real_resolver(),
                namespace_name="",
            )


class TestConceptEmission:
    """Req 14.1 / 14.3 / 14.4 — three triples per emitted topic."""

    def test_single_topic_emits_three_concept_triples(self):
        topic = _topic("Financial Holdings Summary", topic_id="t-fhs")
        g = induce_concepts(
            topics=[topic],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )

        concept_uri = URIRef(_topic_iri("Financial Holdings Summary"))
        scheme_uri = URIRef(_scheme_iri())

        # Req 14.1 — declaration.
        assert (concept_uri, RDF.type, SKOS.Concept) in g
        # Req 14.3 — prefLabel with xsd:string datatype, exact value.
        assert (
            concept_uri,
            SKOS.prefLabel,
            Literal("Financial Holdings Summary", datatype=XSD.string),
        ) in g
        # Req 14.4 — link to scheme.
        assert (concept_uri, SKOS.inScheme, scheme_uri) in g

        # Exactly four triples for the concept (decl + prefLabel + inScheme)
        # plus two for the scheme (decl + label).
        assert len(g) == 5

    def test_matching_class_label_links_topic_as_subject(self):
        class_iri = _class_iri("Compressor")
        concept_iri = URIRef(_topic_iri("コンプレッサー"))
        g = induce_concepts(
            topics=[_topic("コンプレッサー")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
            namespace_language="ja",
            class_iris={
                "Compressor": class_iri,
                "コンプレッサー": class_iri,
            },
        )

        assert (URIRef(class_iri), DCTERMS.subject, concept_iri) in g

    def test_unmatched_or_ambiguous_class_label_does_not_link_topic(self):
        g = induce_concepts(
            topics=[_topic("Equipment")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
            class_iris={
                "equipment": _class_iri("EquipmentA"),
                "EQUIPMENT": _class_iri("EquipmentB"),
            },
        )

        assert not list(g.triples((None, DCTERMS.subject, None)))

    def test_equivalent_labels_for_same_iri_remain_an_exact_match(self):
        class_iri = _class_iri("Equipment")
        g = induce_concepts(
            topics=[_topic("Equipment")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
            class_iris={
                "equipment": class_iri,
                "EQUIPMENT": class_iri,
            },
        )

        assert (URIRef(class_iri), DCTERMS.subject, URIRef(_topic_iri("Equipment"))) in g

    @pytest.mark.parametrize(
        "class_count",
        [
            MAX_CONCEPT_CLASS_LOOKUP_ENTRIES - 1,
            MAX_CONCEPT_CLASS_LOOKUP_ENTRIES,
        ],
    )
    def test_class_lookup_at_or_below_limit_preserves_exact_match(self, class_count):
        class_iris = {f"Class {index}": _class_iri(f"Class {index}") for index in range(class_count - 1)}
        matching_iri = _class_iri("Target")
        class_iris["Target"] = matching_iri

        g = induce_concepts(
            topics=[_topic("Target")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
            class_iris=class_iris,
        )

        assert (URIRef(matching_iri), DCTERMS.subject, URIRef(_topic_iri("Target"))) in g

    def test_class_lookup_above_limit_disables_all_links_without_partial_match(self, caplog):
        target_iri = _class_iri("TargetA")
        entries = [("Target", target_iri)]
        entries.extend(
            (f"Class {index}", _class_iri(f"Class {index}")) for index in range(MAX_CONCEPT_CLASS_LOOKUP_ENTRIES - 1)
        )
        # At exactly limit + 1, this conflicting alias falls after the limit
        # in one order. A partial index would incorrectly retain TargetA as an
        # exact match.
        entries.append(("TARGET", _class_iri("TargetB")))

        graphs = []
        with caplog.at_level(logging.WARNING):
            for ordered_entries in (entries, list(reversed(entries))):
                graphs.append(
                    induce_concepts(
                        topics=[_topic("Target")],
                        iri_resolver=_real_resolver(),
                        namespace_name=_TOPIC_NS,
                        class_iris=dict(ordered_entries),
                    )
                )

        assert all(not list(graph.triples((None, DCTERMS.subject, None))) for graph in graphs)
        assert isomorphic(graphs[0], graphs[1])
        limit_records = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "induce_concepts_class_lookup_limit_exceeded"
        ]
        assert len(limit_records) == 2
        assert all(
            record.class_iri_entry_limit == MAX_CONCEPT_CLASS_LOOKUP_ENTRIES
            and record.class_iri_entry_count == MAX_CONCEPT_CLASS_LOOKUP_ENTRIES + 1
            for record in limit_records
        )
        assert "Target" not in "\n".join(record.message for record in limit_records)
        assert target_iri not in "\n".join(record.message for record in limit_records)

    def test_ambiguous_class_labels_emit_one_redacted_debug_event(self, caplog):
        customer_label = "Confidential Turbine"
        first_iri = _class_iri("TurbineA")
        second_iri = _class_iri("TurbineB")

        with caplog.at_level(logging.DEBUG):
            g = induce_concepts(
                topics=[
                    _topic(customer_label, topic_id="topic-1"),
                    _topic(customer_label.upper(), topic_id="topic-2"),
                ],
                iri_resolver=_real_resolver(),
                namespace_name=_TOPIC_NS,
                class_iris={
                    customer_label: first_iri,
                    customer_label.upper(): second_iri,
                },
            )

        assert not list(g.triples((None, DCTERMS.subject, None)))
        ambiguity_records = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "induce_concepts_ambiguous_class_labels"
        ]
        assert len(ambiguity_records) == 1
        record = ambiguity_records[0]
        assert record.ambiguous_normalized_label_count == 1
        assert len(record.normalized_label_fingerprint_sample) == 1
        rendered_log = caplog.text
        assert customer_label not in rendered_log
        assert first_iri not in rendered_log
        assert second_iri not in rendered_log

    def test_ambiguity_debug_sample_is_bounded_and_order_independent(self, caplog):
        ambiguous_pairs = [
            (
                f"Sensitive Label {index}",
                _class_iri(f"SensitiveA{index}"),
                _class_iri(f"SensitiveB{index}"),
            )
            for index in range(12)
        ]

        fingerprints = []
        with caplog.at_level(logging.DEBUG):
            for pairs in (ambiguous_pairs, list(reversed(ambiguous_pairs))):
                class_iris = {}
                for label, first_iri, second_iri in pairs:
                    class_iris[label] = first_iri
                    class_iris[label.upper()] = second_iri
                induce_concepts(
                    topics=[_topic("Unmatched")],
                    iri_resolver=_real_resolver(),
                    namespace_name=_TOPIC_NS,
                    class_iris=class_iris,
                )
                record = next(
                    record
                    for record in reversed(caplog.records)
                    if getattr(record, "event", None) == "induce_concepts_ambiguous_class_labels"
                )
                fingerprints.append(record.normalized_label_fingerprint_sample)

        assert fingerprints[0] == fingerprints[1]
        assert len(fingerprints[0]) == 5

    def test_preflabel_is_xsd_string(self):
        """Req 14.3 — prefLabel must be xsd:string-typed."""
        g = induce_concepts(
            topics=[_topic("Legal Proceedings")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        concept_uri = URIRef(_topic_iri("Legal Proceedings"))
        labels = list(g.objects(concept_uri, SKOS.prefLabel))
        assert len(labels) == 1
        assert isinstance(labels[0], Literal)
        assert labels[0].datatype == XSD.string
        assert str(labels[0]) == "Legal Proceedings"

    def test_preserves_topic_value_verbatim_in_preflabel(self):
        """The prefLabel literal contains the topic.value EXACTLY,
        including punctuation, casing, and surrounding whitespace
        (after the leading/trailing strip).
        """
        # Real-world examples from the dev graph.
        topic = _topic("Cash Equivalents and Marketable Securities Summary as of April 30, 2023")
        g = induce_concepts(
            topics=[topic],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        # Find the concept.
        concepts = list(g.subjects(RDF.type, SKOS.Concept))
        assert len(concepts) == 1
        labels = list(g.objects(concepts[0], SKOS.prefLabel))
        assert str(labels[0]) == "Cash Equivalents and Marketable Securities Summary as of April 30, 2023"

    def test_multiple_topics_emit_independent_concepts(self):
        topics = [
            _topic("Financial Performance", topic_id="t1"),
            _topic("Legal Proceedings", topic_id="t2"),
            _topic("iPhone Net Sales", topic_id="t3"),
        ]
        g = induce_concepts(
            topics=topics,
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )

        concepts = set(g.subjects(RDF.type, SKOS.Concept))
        assert len(concepts) == 3
        assert URIRef(_topic_iri("Financial Performance")) in concepts
        assert URIRef(_topic_iri("Legal Proceedings")) in concepts
        assert URIRef(_topic_iri("iPhone Net Sales")) in concepts

        # All linked to the same scheme.
        scheme_uri = URIRef(_scheme_iri())
        for c in concepts:
            assert (c, SKOS.inScheme, scheme_uri) in g


class TestRule5IRIShape:
    """Req 14.2 — concept IRIs use ``"Topic"`` as the classification slot.

    These tests pin the IRI shape so a regression in either the
    resolver normalization OR the rule's slot choice would fail loudly.
    """

    def test_iri_uses_literal_topic_slot(self):
        g = induce_concepts(
            topics=[_topic("Apple")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        iris = list(g.subjects(RDF.type, SKOS.Concept))
        assert len(iris) == 1
        iri_str = str(iris[0])
        assert iri_str == f"{_TEST_PREFIX}/Topic/Apple"
        assert "/Topic/" in iri_str

    def test_iri_normalizes_spaces_to_hyphens(self):
        """Per Pattern-1: spaces collapse to ``-``, casing preserved."""
        g = induce_concepts(
            topics=[_topic("Financial Performance")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        iri_str = str(next(iter(g.subjects(RDF.type, SKOS.Concept))))
        assert iri_str == f"{_TEST_PREFIX}/Topic/Financial-Performance"

    def test_iri_preserves_camelcase(self):
        """Per Pattern-1: source casing is preserved verbatim."""
        g = induce_concepts(
            topics=[_topic("iPhone Net Sales Performance")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        iri_str = str(next(iter(g.subjects(RDF.type, SKOS.Concept))))
        assert iri_str == f"{_TEST_PREFIX}/Topic/iPhone-Net-Sales-Performance"

    def test_punctuation_percent_encoded_or_dropped(self):
        """Per Pattern-1: parens/colons/etc. are percent-encoded, then
        consecutive hyphens collapse and trailing hyphens trim.
        ``"Topic (with punct.)"`` → ``"Topic-with-punct."`` (period is
        unreserved per RFC 3986).
        """
        g = induce_concepts(
            topics=[_topic("Topic A (preview)")],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        iri_str = str(next(iter(g.subjects(RDF.type, SKOS.Concept))))
        # The exact normalization rules are tested in the resolver
        # tests; this test pins the shape end-to-end.
        expected = f"{_TEST_PREFIX}/Topic/{normalize_for_iri_path('Topic A (preview)')}"
        assert iri_str == expected

    def test_concept_iri_disjoint_from_class_iri(self):
        """Req 14.2 — concept IRIs use "Topic", class IRIs use "class".

        A topic literally named "Person" must NOT collide with the
        ``owl:Class`` IRI for the Person class. The slot disambiguates.
        """
        resolver = _real_resolver()
        # Mint a class IRI using the same resolver instance.
        class_iri_str = resolver.resolve("Person", "class")

        g = induce_concepts(
            topics=[_topic("Person")],
            iri_resolver=resolver,
            namespace_name=_TOPIC_NS,
        )
        concept_iri_str = str(next(iter(g.subjects(RDF.type, SKOS.Concept))))

        assert class_iri_str != concept_iri_str
        assert class_iri_str.endswith("/class/Person")
        assert concept_iri_str.endswith("/Topic/Person")


class TestRule5EmptyAndDefensive:
    """Req 14.6 — defensive paths."""

    def test_empty_value_topic_skipped_with_warning(self, caplog):
        topics = [_topic("", topic_id="t-empty"), _topic("Real", topic_id="t-real")]
        with caplog.at_level(logging.WARNING):
            g = induce_concepts(
                topics=topics,
                iri_resolver=_real_resolver(),
                namespace_name=_TOPIC_NS,
            )

        # Only the real topic is emitted.
        concepts = list(g.subjects(RDF.type, SKOS.Concept))
        assert len(concepts) == 1
        assert URIRef(_topic_iri("Real")) in concepts

        # Warning mentions the skipped topic_id.
        warnings_text = "\n".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
        assert "t-empty" in warnings_text
        # The "Real" topic must not appear in warnings.
        assert "t-real" not in warnings_text

    def test_whitespace_only_value_skipped_with_warning(self, caplog):
        topics = [_topic("   ", topic_id="t-ws")]
        with caplog.at_level(logging.WARNING):
            g = induce_concepts(
                topics=topics,
                iri_resolver=_real_resolver(),
                namespace_name=_TOPIC_NS,
            )
        # No concepts.
        assert not list(g.subjects(RDF.type, SKOS.Concept))
        # Warning mentions the topic_id.
        warnings_text = "\n".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
        assert "t-ws" in warnings_text

    def test_resolver_failure_per_topic_isolation(self, caplog):
        """One topic's resolver failure does NOT abort the whole batch."""
        # Build a real resolver and intercept ``.resolve()`` so that a
        # specific value raises. The other topics resolve normally.
        real_resolver = InMemoryIRIResolver(
            namespace=_TEST_NS,
            ontology_name=_TEST_ONTO,
        )

        original_resolve = real_resolver.resolve

        def resolve_with_poison(value, classification):
            if value == "POISON":
                raise ValueError("synthetic resolver failure for testing")
            return original_resolve(value, classification)

        spy = MagicMock(wraps=real_resolver)
        spy.resolve.side_effect = resolve_with_poison

        topics = [
            _topic("Real Topic", topic_id="t-real"),
            _topic("POISON", topic_id="t-bad"),
            _topic("Another Real", topic_id="t-real2"),
        ]
        with caplog.at_level(logging.WARNING):
            g = induce_concepts(
                topics=topics,
                iri_resolver=spy,
                namespace_name=_TOPIC_NS,
            )

        concepts = set(g.subjects(RDF.type, SKOS.Concept))
        # Two real topics emitted.
        assert URIRef(_topic_iri("Real Topic")) in concepts
        assert URIRef(_topic_iri("Another Real")) in concepts
        # Bad one not emitted.
        assert len(concepts) == 2

        warnings_text = "\n".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
        assert "t-bad" in warnings_text

    def test_duplicate_values_resolve_to_same_iri(self):
        """If two topics share a value (modulo case), the resolver's
        case-folded lookup returns the same IRI for both — so the graph
        ends up with one ``skos:Concept`` declaration, two
        ``skos:prefLabel`` triples (one per topic), and two
        ``skos:inScheme`` triples (RDF set semantics will dedupe).

        This mirrors how the resolver's first-spelling-wins works for
        case-only variants.
        """
        topics = [
            _topic("Apple Inc.", topic_id="t-1"),
            _topic("apple inc.", topic_id="t-2"),  # case-only variant
        ]
        g = induce_concepts(
            topics=topics,
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )

        concepts = set(g.subjects(RDF.type, SKOS.Concept))
        # Same IRI for both — case-only variants collide.
        assert len(concepts) == 1


class TestRule5NamespaceBindings:
    """Standard prefixes are bound on the returned graph."""

    def test_standard_prefixes_bound(self):
        g = induce_concepts(
            topics=[],
            iri_resolver=_real_resolver(),
            namespace_name=_TOPIC_NS,
        )
        bound = {prefix: ns for prefix, ns in g.namespaces()}
        # Rule 5's primary vocabulary is SKOS; OWL is also bound for
        # consistency with the other rules.
        for required in ("owl", "rdf", "rdfs", "xsd", "skos"):
            assert required in bound


class TestGeneratedCommentsAreSingleLanguage:
    """A comment COA writes itself must not mix languages, whatever the corpus.

    Issue #897's blocker is that RDF 1.1 gives a literal one language tag or one
    datatype, never a tag per fragment — so a literal holding two languages cannot
    be tagged correctly under any scheme. Extraction-prompt work can reduce
    mixed-language labels coming FROM the corpus, but it cannot touch a literal
    that COA manufactures by interpolating a corpus-derived name into an English
    frame. Those have to be fixed here.
    """

    def test_a_non_ascii_class_name_does_not_leak_into_the_comment(self):
        """The measured case: 17 such literals in one real induced ontology.

        The frame was ``f"Observed across {n} fact(s) in class {domain_name}"``, and
        ``domain_name`` is the class's own label — Japanese on a Japanese corpus.
        """
        from rdflib import RDFS

        facts = [
            _spc("測定器A", "技術的特性", "設置場所", "屋内"),
            _spc("測定器B", "技術的特性", "設置場所", "屋外"),
        ]
        class_iris = {"技術的特性": _class_iri("技術的特性")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        comments = [str(c) for _s, _p, c in g.triples((None, RDFS.comment, None))]
        assert comments, "expected at least one generated comment"
        for text in comments:
            assert text.isascii(), f"generated comment carries corpus text: {text!r}"

    def test_the_class_is_still_recoverable_from_domain(self):
        """Dropping the name from the prose must not lose the attribution.

        A consumer that wants the class reads ``rdfs:domain``; the comment restated
        it only in a form that made the literal untaggable.
        """
        from rdflib import RDFS, URIRef

        facts = [_spc("測定器A", "技術的特性", "設置場所", "屋内")]
        class_iris = {"技術的特性": _class_iri("技術的特性")}
        g = induce_datatype_properties(
            facts=facts,
            class_iris=class_iris,
            iri_resolver=_real_resolver(),
        )
        prop = URIRef(_datatype_property_iri("設置場所", "技術的特性"))
        assert set(g.objects(prop, RDFS.domain)) == {URIRef(_class_iri("技術的特性"))}
