# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""T-Box axiom induction from a graphrag-toolkit lexical knowledge graph.

This module implements the induction rules that convert the lexical
graph's class distribution, sampled entities, facts, and topics into
formal OWL/RDF axioms. Each rule produces an :class:`rdflib.Graph`
fragment; the pipeline orchestrator merges these fragments and the
serializer emits final Turtle.

This file currently implements **Rule 1 only** (Task 12.1, Requirement
10): map distinct entity classifications to ``owl:Class`` declarations.
Rules 2–6 (named individuals, object/datatype properties, concepts,
provenance) are scoped to subsequent tasks (12.2–12.6).

Inputs to Rule 1
----------------

- ``class_distribution``: :class:`ClassRecord` list from
  :meth:`LexicalGraphStore.get_class_nodes`. The ``value`` field is the
  class label as it appears in the ``__SYS_Class__`` node — typically
  CamelCase (e.g. ``"FinancialInstrument"``).
- ``descriptions``: mapping from class label (in the form used upstream
  by the description generator and class normalizer — i.e. the
  space-separated entity-class form, e.g. ``"Financial Instrument"``)
  to the generated ``rdfs:comment`` text (Requirement 4).
- ``normalizer_results``: mapping from the same space-separated class
  label to the :class:`NormalizationResult` produced by the class
  normalizer (Requirement 9). Drives IRI minting (or reuse) and
  ``skos:closeMatch`` link creation.
- ``iri_resolver``: an :class:`IRIResolver` for minting deterministic
  class IRIs.
- ``floor``: minimum ``ClassRecord.count`` for a class to be included
  (Requirement 3.4 / Requirement 10.1, default 3).

Per-class behaviour
-------------------

For each :class:`ClassRecord` whose ``count >= floor``:

1. Find its ``NormalizationResult``. The ``class_iri`` field of that
   result is the IRI to use:
   - For band ``exact`` it's the existing matched IRI (Req 9.3).
   - For band ``close_match`` it's a freshly-minted IRI; the function
     additionally emits ``<new_iri> skos:closeMatch <matched_iri>``
     (Req 9.4).
   - For band ``novel`` it's a freshly-minted IRI with no link
     (Req 9.5).
   The IRI shape for class IRIs is
   ``http://coa.amazon.com/namespace/{ns}/ontology/{name}/class/{normalized-label}``
   — note the literal ``class`` segment (Req 10.2). This disambiguates
   T-Box class IRIs from A-Box individual IRIs when an entity is
   literally named after its class.
2. Emit ``<class_iri> rdf:type owl:Class`` (Req 10.1).
3. Emit ``<class_iri> rdfs:label "<original ClassRecord.value>"^^xsd:string``
   (Req 10.3 — preserves the CamelCase form from ``__SYS_Class__``).
4. Emit ``<class_iri> rdfs:comment "<description>"^^xsd:string``
   (Req 10.4) when a description is available.

Edge cases
----------

- ``count < floor``: skip silently. The sampler/distribution caller is
  responsible for the observability of the threshold.
- ``count >= floor`` but no description and no normalizer result for
  this class: skip with a WARNING log naming the class. The pipeline
  should arrive here only when both maps are populated, so this is a
  defensive guard.
- ``count >= floor`` and normalizer result present but description
  missing: emit the class declaration with ``rdfs:label`` only. This is
  graceful degradation when the description generator legitimately
  fell back (e.g. zero-entity classes use the deterministic
  ``"Class representing N entities including: ..."`` fallback) — the
  point is the class is still a real class.
- Zero classes pass the floor: return an empty graph and log a single
  WARNING (Req 10.5).

Determinism
-----------

Iteration follows the natural order of ``class_distribution``. rdflib
graphs are unordered conceptually, but the per-class log lines use
this ordering to make runs visually comparable.

Usage
-----

::

    from coa_ontology.inducer.unstructured.services.induction_rules import (
        induce_classes,
    )

    g = induce_classes(
        class_distribution=schema.classes,
        descriptions=descriptions_by_label,
        normalizer_results=normalizer_results_by_label,
        iri_resolver=iri_resolver,
        floor=3,
    )
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from coa_common.language_tags import DEFAULT_LANGUAGE
from rdflib import OWL, RDF, RDFS, XSD, Graph, URIRef
from rdflib.namespace import SKOS

from coa_ontology.inducer.services.generated_text import render
from coa_ontology.inducer.services.literal_language import generated_literal
from coa_ontology.inducer.unstructured.services.normalizers import (
    normalize_predicate,
    select_canonical_label,
)
from coa_ontology.inducer.unstructured.services.sampler import (
    _camel_to_spaced,
)

if TYPE_CHECKING:
    from coa_ontology.inducer.unstructured.services.class_normalizer import (
        NormalizationResult,
    )
    from coa_ontology.inducer.unstructured.services.iri_resolver import (
        IRIResolver,
    )
    from coa_ontology.inducer.unstructured.stores.protocol import (
        ClassRecord,
        EntityRecord,
        FactRecord,
        TopicRecord,
    )

log = logging.getLogger(__name__)


# Default floor threshold: matches Requirement 3.4 (Stratified Sampler default).
DEFAULT_FLOOR = 3


def _label_for_lookup(class_record_value: str) -> str:
    """Convert a ``__SYS_Class__`` CamelCase value to the upstream label form.

    The description generator and class normalizer both key by the
    space-separated class label (e.g. ``"Financial Instrument"``) — that's
    the form the entities themselves carry on their ``class`` property.
    The ``__SYS_Class__`` node's ``value`` is CamelCase
    (``"FinancialInstrument"``). This helper performs the same conversion
    used by the sampler so the lookup keys line up.
    """
    return _camel_to_spaced(class_record_value)


def induce_classes(
    class_distribution: list[ClassRecord],
    descriptions: dict[str, str],
    normalizer_results: dict[str, NormalizationResult],
    iri_resolver: IRIResolver,
    floor: int = DEFAULT_FLOOR,
    *,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> Graph:
    """Induce ``owl:Class`` declarations from the lexical graph's class layer.

    Implements Rule 1 (Requirement 10): every classification with
    ``count >= floor`` becomes one ``owl:Class`` with deterministic IRI,
    ``rdfs:label`` (the original CamelCase value), and ``rdfs:comment``
    (the LLM-generated description). When the class normalizer flagged
    a class as ``close_match``, also emits a ``skos:closeMatch`` link
    to the matched existing IRI (Req 9.4).

    Args:
        class_distribution: Class records from the lexical graph
            (typically ``ClassSchemaResult.classes``). Read-only.
        descriptions: Mapping from space-separated class label
            (e.g. ``"Financial Instrument"``) to its generated
            ``rdfs:comment`` text. Missing keys cause the class
            declaration to be emitted without a comment (graceful
            degradation, logged at INFO).
        normalizer_results: Mapping from space-separated class label to
            its :class:`NormalizationResult`. Required for every class
            above the floor — missing entries are skipped with a
            WARNING.
        iri_resolver: An :class:`IRIResolver`. Not used directly because
            the IRI is already on ``NormalizationResult.class_iri``;
            the parameter is kept on the signature for future use
            (e.g. handling normalizer fallback paths) and to match the
            spec (Req 10.2 calls out the resolver as the IRI source).
        floor: Minimum ``ClassRecord.count`` for inclusion (Req 10.1
            and Req 3.4). Defaults to 3.
        namespace_language: Canonical namespace language. Generated labels and
            comments compatible with it carry its tag; everything else keeps
            the untagged ``xsd:string`` form. ``en`` asserts no tags.

    Returns:
        An :class:`rdflib.Graph` containing all class declarations,
        labels, comments, and ``skos:closeMatch`` links produced by
        Rule 1. The graph also has the standard prefixes (owl, rdf,
        rdfs, xsd, skos) bound for readability if serialized in
        isolation. Empty if zero classes pass the floor.
    """
    g = Graph()
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)

    if not class_distribution:
        log.warning("induce_classes: empty class_distribution input — returning empty graph")
        return g

    classes_emitted = 0
    classes_skipped_floor = 0
    classes_skipped_missing_normalizer = 0

    for cls in class_distribution:
        camel_label = cls.value
        spaced_label = _label_for_lookup(camel_label)

        if cls.count < floor:
            classes_skipped_floor += 1
            continue

        nr = normalizer_results.get(spaced_label)
        if nr is None:
            log.warning(
                "induce_classes: no NormalizationResult for class %r (spaced lookup %r); skipping",
                camel_label,
                spaced_label,
            )
            classes_skipped_missing_normalizer += 1
            continue

        if not nr.class_iri:
            # Hard-failure case from the normalizer (e.g. resolver raised).
            log.warning(
                "induce_classes: NormalizationResult for class %r has empty class_iri (band=%s); skipping",
                camel_label,
                nr.band,
            )
            classes_skipped_missing_normalizer += 1
            continue

        class_uri = URIRef(nr.class_iri)

        # Req 10.1 — owl:Class declaration.
        g.add((class_uri, RDF.type, OWL.Class))

        # Req 10.3 — rdfs:label is the original __SYS_Class__ value
        # (CamelCase form), with explicit xsd:string datatype. Extraction names
        # the class in the namespace language, so a compatible label carries
        # the namespace tag instead (see ``generated_literal``).
        g.add((class_uri, RDFS.label, generated_literal(camel_label, namespace_language, datatype=XSD.string)))

        # Req 10.4 — rdfs:comment is the generated description.
        comment = descriptions.get(spaced_label)
        if comment:
            g.add((class_uri, RDFS.comment, generated_literal(comment, namespace_language, datatype=XSD.string)))
        else:
            log.info(
                "induce_classes: no description for class %r; emitting rdfs:label only",
                camel_label,
            )

        # Req 9.4 — close_match band emits a skos:closeMatch link.
        if nr.band == "close_match" and nr.matched_existing_iri:
            g.add(
                (
                    class_uri,
                    SKOS.closeMatch,
                    URIRef(nr.matched_existing_iri),
                )
            )

        classes_emitted += 1

    if classes_emitted == 0:
        # Req 10.5 — empty result + warning when nothing passed the floor.
        log.warning(
            "induce_classes: zero classes passed the floor threshold "
            "(floor=%d, total considered=%d, skipped due to floor=%d, "
            "skipped due to missing normalizer=%d). Returning empty graph.",
            floor,
            len(class_distribution),
            classes_skipped_floor,
            classes_skipped_missing_normalizer,
        )
    else:
        log.info(
            "induce_classes: emitted %d owl:Class declarations "
            "(total considered=%d, below floor=%d, missing normalizer=%d)",
            classes_emitted,
            len(class_distribution),
            classes_skipped_floor,
            classes_skipped_missing_normalizer,
        )

    return g


# ── Rule 2 — Sampled entities → owl:NamedIndividual (A-Box, OPTIONAL) ──
#
# Implements Task 12.2 / Requirement 11. This rule is **optional** —
# the pipeline can produce a strict T-Box-only ontology (Rules 1 + 3-6)
# OR a T-Box + sampled-A-Box ontology when ``include_individuals=True``.
# A-Box assertions are useful for capturing representative instances
# (e.g. for downstream demos or LLM grounding) but the spec explicitly
# treats them as optional because the pipeline's primary value is the
# T-Box (class + property declarations).


def induce_individuals(
    sampled_entities: list[EntityRecord],
    class_iris: dict[str, str],
    iri_resolver: IRIResolver,
    *,
    include_individuals: bool = False,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> Graph:
    """Induce ``owl:NamedIndividual`` declarations from sampled entities.

    Implements Rule 2 (Requirement 11): when ``include_individuals=True``,
    every sampled entity becomes an ``owl:NamedIndividual`` with three
    triples — declaration, class typing, and ``rdfs:label``. When
    ``include_individuals=False`` (the default), this function is a
    no-op that returns an empty graph.

    Per-entity behaviour (when ``include_individuals=True``):

    1. Look up the ``owl:Class`` IRI for the entity's classification
       in ``class_iris``. If the class isn't known (e.g. it was below
       the floor threshold in Rule 1), skip the entity with a WARNING
       (Req 11.5).
    2. Mint the individual's IRI via
       ``iri_resolver.resolve(entity.value, entity.classification)``
       (Req 11.2). Per the convention documented in Req 10.2, A-Box
       individual IRIs use the entity's class label as the
       classification slot — NOT the literal ``"class"`` token used
       for T-Box class IRIs. So an entity ``value="Apple",
       classification="Company"`` mints
       ``.../namespace/{ns}/ontology/{name}/Company/Apple`` and is
       cleanly distinguished from the class IRI ``.../class/Company``.
    3. Emit ``<individual_iri> rdf:type owl:NamedIndividual`` (Req 11.1).
    4. Emit ``<individual_iri> rdf:type <class_iri>`` (Req 11.3).
    5. Emit ``<individual_iri> rdfs:label <entity.value>^^xsd:string``
       (Req 11.4).

    On any per-entity failure (resolver exception, missing class IRI),
    the entity is skipped with a WARNING and the rest of the batch
    continues. Programmer errors propagate.

    Args:
        sampled_entities: List of :class:`EntityRecord`. Typically the
            flat union of every class's sampled entities (the orchestrator
            constructs this from the ``Stratified_Sampler`` output).
        class_iris: Mapping from class label (space-separated form, e.g.
            ``"Financial Instrument"``) to the ``owl:Class`` IRI minted
            by Rule 1. Used to look up the type assertion target.
        iri_resolver: An :class:`IRIResolver` for minting deterministic
            individual IRIs.
        include_individuals: Gate flag (Req 11.6). Defaults to ``False``;
            when ``False`` the function returns an empty graph and emits
            a single INFO log line. When ``True`` the rule actually runs.
        namespace_language: Canonical namespace language. Generated labels and
            comments compatible with it carry its tag; everything else keeps
            the untagged ``xsd:string`` form. ``en`` asserts no tags.

    Returns:
        An :class:`rdflib.Graph` with three triples per emitted entity
        (declaration, class typing, label) plus the standard prefix
        bindings. Empty if ``include_individuals=False``, if
        ``sampled_entities`` is empty, or if all entities were skipped.
    """
    g = Graph()
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)

    if not include_individuals:
        log.info(
            "induce_individuals: include_individuals=False — skipping "
            "A-Box individual creation entirely (returning empty graph)"
        )
        return g

    if not sampled_entities:
        log.info("induce_individuals: sampled_entities is empty — returning empty graph")
        return g

    individuals_emitted = 0
    skipped_missing_class = 0
    skipped_resolver_failure = 0

    for entity in sampled_entities:
        cls_label = entity.classification
        cls_iri = class_iris.get(cls_label)
        if not cls_iri:
            log.warning(
                "induce_individuals: entity %r has classification %r but "
                "no owl:Class IRI was minted for that class (likely "
                "below the floor threshold in Rule 1) — skipping",
                entity.value,
                cls_label,
            )
            skipped_missing_class += 1
            continue

        # Mint the individual IRI per Req 11.2: use entity.value as the
        # value and entity.classification as the classification slot.
        try:
            individual_iri_str = iri_resolver.resolve(entity.value, cls_label)
        except Exception as exc:  # noqa: BLE001 — see docstring.
            log.warning(
                "induce_individuals: IRI resolver failed for entity %r (class=%r): %s — skipping",
                entity.value,
                cls_label,
                exc,
            )
            skipped_resolver_failure += 1
            continue

        if not individual_iri_str:
            log.warning(
                "induce_individuals: resolver returned empty IRI for entity %r (class=%r) — skipping",
                entity.value,
                cls_label,
            )
            skipped_resolver_failure += 1
            continue

        individual_uri = URIRef(individual_iri_str)
        class_uri = URIRef(cls_iri)

        # Req 11.1 — owl:NamedIndividual declaration.
        g.add((individual_uri, RDF.type, OWL.NamedIndividual))
        # Req 11.3 — class typing.
        g.add((individual_uri, RDF.type, class_uri))
        # Req 11.4 — rdfs:label with xsd:string.
        g.add(
            (
                individual_uri,
                RDFS.label,
                generated_literal(entity.value, namespace_language, datatype=XSD.string),
            )
        )

        individuals_emitted += 1

    log.info(
        "induce_individuals: emitted %d owl:NamedIndividual declarations "
        "(total considered=%d, skipped due to missing class=%d, "
        "skipped due to resolver failure=%d)",
        individuals_emitted,
        len(sampled_entities),
        skipped_missing_class,
        skipped_resolver_failure,
    )

    return g


# ── Rule 3 — SPO facts → owl:ObjectProperty (T-Box) ────────────────────
#
# Implements Task 12.3 / Requirement 12. SPO facts are
# subject-predicate-object triples where both sides reference
# entities. Each distinct normalized predicate becomes one
# ``owl:ObjectProperty`` declaration whose ``rdfs:domain`` and
# ``rdfs:range`` enumerate every class pair the predicate was observed
# linking. Surface-form variants of the same predicate ("works_for",
# "WORKS_FOR", "works for") collapse to a single property IRI; the
# canonical ``rdfs:label`` is the most-common original spelling.


def _is_spo_fact(fact: FactRecord) -> bool:
    """Return True iff a fact is a fully-populated SPO triple.

    A fact is SPO when both ``object_value`` and ``object_class`` are
    non-empty. The ``complement`` field is the SPC marker; when both
    object and complement are populated the fact is malformed and
    treated as SPO (we log nothing — upstream extractors are expected
    to populate exactly one).
    """
    return bool(fact.object_value) and bool(fact.object_class)


def induce_object_properties(
    facts: list[FactRecord],
    class_iris: dict[str, str],
    iri_resolver: IRIResolver,
    *,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> Graph:
    """Induce ``owl:ObjectProperty`` declarations from SPO facts.

    Implements Rule 3 (Requirement 12, Task 12D-amended). Aggregates
    SPO facts by ``(normalized_predicate, subject_class)`` and emits
    one ``owl:ObjectProperty`` per distinct aggregation key. Each
    property has exactly ONE ``rdfs:domain`` triple (the subject
    class) and one or more ``rdfs:range`` triples (one per distinct
    object class observed within the aggregation).

    The change from the original Rule 3 contract: the same predicate
    spelled identically across two different subject classes used to
    collapse into a single property with multiple ``rdfs:domain``
    triples (which OWL formally interprets as the intersection of
    those classes — usually empty). After Task 12D, ``Person.works_for``
    and ``Library.owns`` are distinct properties from ``Vehicle.owns``,
    matching the structured-induction strategy's per-(table, column)
    convention. See ``OntologyDevelopmentNotes/iri-minting-discussion.md``
    for the full reasoning.

    Per aggregation key the function emits:

    - one ``rdf:type owl:ObjectProperty`` declaration (Req 12.1)
    - exactly one ``rdfs:domain`` triple (Req 12.3 — the subject class)
    - one ``rdfs:range`` triple per distinct object class observed
      within this aggregation key (Req 12.4 — range fan-out within a
      single subject class is allowed and reflects genuine relational
      ambiguity that the entailment engine can refine)
    - one ``rdfs:label`` whose value is the canonical-by-frequency
      original predicate string within this aggregation key
      (Req 12.6 / Req 19.3, applied per-key — not pooled across
      subject classes)

    The property IRI is minted via
    ``iri_resolver.resolve(normalized_predicate, f"{subject_class}/ObjectProperty")``
    (Req 12.2). The resulting IRI shape is
    ``{prefix}/{Subject-Class}/ObjectProperty/{normalized_predicate}``
    where the subject class slot is normalized via the resolver's
    case-preserving Pattern-1 rules (so ``"Financial Instrument"``
    becomes ``"Financial-Instrument"``).

    Cross-(predicate, class) equivalence (Req 12.5):

    - Distinct aggregation keys produce distinct property IRIs by
      default. The optional Task 12B.2 property normalizer can
      detect when two distinct IRIs are semantically equivalent and
      emit ``owl:equivalentProperty`` axioms linking them. Without
      a property normalizer, distinct keys remain unlinked.

    Per-fact filtering (Req 12.7):

    - If either ``subject_class`` or ``object_class`` has no entry in
      ``class_iris`` (i.e. that class was below the floor threshold
      in Rule 1), the fact is skipped silently — it cannot contribute
      a meaningful domain or range constraint.
    - SPC facts (whose ``object_value`` is None and ``complement`` is
      populated) are silently filtered out by ``_is_spo_fact`` —
      Rule 4 will handle them.
    - Predicates that normalize to the empty string are logged at
      WARNING and skipped (cannot mint an IRI).
    - If ALL facts for a given aggregation key are skipped this way,
      no property declaration is emitted for that key.

    Per-key fault isolation: a resolver failure for one aggregation
    key logs a WARNING and skips just that key; the rest of the batch
    continues. Empty inputs produce an empty graph.

    Args:
        facts: List of :class:`FactRecord`. Both SPO and SPC may be
            present; SPC are filtered out internally.
        class_iris: Mapping from class label (the entity-side
            space-separated form, e.g. ``"Financial Instrument"``)
            to the ``owl:Class`` IRI minted by Rule 1. Missing
            classes mean the class was below the floor threshold;
            the corresponding facts are skipped.
        iri_resolver: An :class:`IRIResolver` for minting deterministic
            property IRIs.
        namespace_language: Canonical namespace language. Generated labels and
            comments compatible with it carry its tag; everything else keeps
            the untagged ``xsd:string`` form. ``en`` asserts no tags.

    Returns:
        An :class:`rdflib.Graph` containing one declaration block per
        emitted ``owl:ObjectProperty`` plus the standard prefix
        bindings. Empty if no eligible facts were found.
    """
    g = Graph()
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)

    if not facts:
        log.info("induce_object_properties: empty facts input — returning empty graph")
        return g

    # First pass — filter to SPO facts whose both classes are known,
    # then aggregate by (normalized_predicate, subject_class).
    #
    # Aggregation key = (normalized_predicate, subject_class). Value:
    #   "originals":   Counter of original predicate strings observed
    #                  on this exact (predicate, subject_class) pair
    #   "subject_iri": the Rule 1 owl:Class IRI for this aggregation
    #                  key's subject class (constant for all entries
    #                  under the same key)
    #   "ranges":      set of object-class IRIs observed within this
    #                  aggregation key
    #   "fact_count":  total contributing facts (for logging)
    aggregates: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "originals": Counter(),
            "subject_iri": None,
            "ranges": set(),
            "fact_count": 0,
        }
    )

    total_considered = 0
    skipped_spc = 0
    skipped_missing_class = 0
    skipped_empty_predicate = 0

    for fact in facts:
        total_considered += 1

        if not _is_spo_fact(fact):
            skipped_spc += 1
            continue

        # ``_is_spo_fact`` guarantees ``object_class`` is non-None.
        # Assert for the type-checker (the runtime behaviour is
        # unchanged).
        assert fact.object_class is not None
        subj_iri = class_iris.get(fact.subject_class)
        obj_iri = class_iris.get(fact.object_class)
        if subj_iri is None or obj_iri is None:
            log.debug(
                "induce_object_properties: skipping fact predicate=%r — "
                "subject_class=%r missing=%s, object_class=%r missing=%s",
                fact.predicate,
                fact.subject_class,
                subj_iri is None,
                fact.object_class,
                obj_iri is None,
            )
            skipped_missing_class += 1
            continue

        normalized = normalize_predicate(fact.predicate)
        if not normalized:
            # Predicate normalized to empty — cannot mint an IRI.
            log.warning(
                "induce_object_properties: skipping fact with predicate %r that normalizes to empty string",
                fact.predicate,
            )
            skipped_empty_predicate += 1
            continue

        key = (normalized, fact.subject_class)
        agg = aggregates[key]
        agg["originals"][fact.predicate] += 1
        agg["subject_iri"] = subj_iri
        agg["ranges"].add(obj_iri)
        agg["fact_count"] += 1

    if not aggregates:
        log.warning(
            "induce_object_properties: zero ObjectProperty declarations "
            "emitted (total facts considered=%d, skipped SPC=%d, "
            "skipped missing class=%d, skipped empty predicate=%d). "
            "Returning empty graph.",
            total_considered,
            skipped_spc,
            skipped_missing_class,
            skipped_empty_predicate,
        )
        return g

    # Second pass — mint one property per (normalized_predicate,
    # subject_class) aggregation key.
    properties_emitted = 0
    skipped_resolver_failure = 0
    range_triples = 0

    for (normalized, subject_class), agg in aggregates.items():
        # Per Req 12.2 (amended) — the classification slot is
        # ``"<subject_class>/ObjectProperty"``. The resolver's
        # path normalization will rewrite the slash-separated form
        # such that the resulting IRI lands at
        # ``.../<Subject-Class>/ObjectProperty/<normalized_predicate>``.
        # Note: the resolver's case-preserving normalization treats
        # the slash as a path separator that survives unencoded only
        # within the slot string; in practice the resolver percent-
        # encodes the slash, producing a single segment slot like
        # ``ObjectProperty%2FCompany``. This is correct IRI hygiene
        # but cosmetically less readable — we mitigate by issuing the
        # resolver call with two separate sub-calls only at the IRI
        # level: see ``_property_iri`` helper below.
        try:
            property_iri_str = _mint_property_iri(
                iri_resolver,
                normalized=normalized,
                kind="ObjectProperty",
                subject_class=subject_class,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "induce_object_properties: IRI resolver failed for "
                "(normalized=%r, subject_class=%r) (originals=%r): %s "
                "— skipping",
                normalized,
                subject_class,
                dict(agg["originals"]),
                exc,
            )
            skipped_resolver_failure += 1
            continue

        if not property_iri_str:
            log.warning(
                "induce_object_properties: resolver returned empty IRI "
                "for (normalized=%r, subject_class=%r) — skipping",
                normalized,
                subject_class,
            )
            skipped_resolver_failure += 1
            continue

        property_uri = URIRef(property_iri_str)

        # Req 12.1 — declaration.
        g.add((property_uri, RDF.type, OWL.ObjectProperty))

        # Req 12.3 — exactly one rdfs:domain (the subject class IRI).
        g.add((property_uri, RDFS.domain, URIRef(agg["subject_iri"])))

        # Req 12.4 — one rdfs:range per distinct object class observed
        # within this aggregation key. Range fan-out within one
        # (predicate, subject_class) pair is allowed and represents
        # genuine relational ambiguity.
        for rng_iri in sorted(agg["ranges"]):
            g.add((property_uri, RDFS.range, URIRef(rng_iri)))
            range_triples += 1

        # Req 12.6 / Req 19.3 — canonical original-spelling label,
        # selected within this aggregation key (NOT pooled across
        # subject classes).
        canonical = select_canonical_label(dict(agg["originals"]))
        g.add(
            (
                property_uri,
                RDFS.label,
                generated_literal(canonical, namespace_language, datatype=XSD.string),
            )
        )

        properties_emitted += 1

    log.info(
        "induce_object_properties: emitted %d owl:ObjectProperty "
        "declarations (range triples=%d) from %d facts (skipped SPC=%d, "
        "missing class=%d, empty predicate=%d, resolver failure=%d)",
        properties_emitted,
        range_triples,
        total_considered,
        skipped_spc,
        skipped_missing_class,
        skipped_empty_predicate,
        skipped_resolver_failure,
    )

    return g


def _mint_property_iri(
    iri_resolver: IRIResolver,
    *,
    normalized: str,
    kind: str,
    subject_class: str,
) -> str:
    """Mint a property IRI of the form ``{prefix}/<Subject-Class>/<kind>/<normalized>``.

    The IRI resolver's ``resolve()`` method takes a single
    ``classification`` slot. We want the slot to be
    ``<subject_class>/<kind>`` (e.g. ``Company/DatatypeProperty``) so
    the final IRI groups by class first and kind second — matching
    the linked-data convention that all of a class's related
    resources (the class itself, its individuals, and its properties)
    share a common ``.../<Subject-Class>/...`` path prefix. The kind
    discriminator (``ObjectProperty`` / ``DatatypeProperty``) lives
    underneath the class segment.

    The default resolver percent-encodes any slash inside the
    classification value, producing a single-segment slot like
    ``Company%2FDatatypeProperty`` — correct IRI but ugly.

    This helper post-processes the resolver's output: it replaces the
    percent-encoded slash with a literal slash so the IRI reads as
    intended. This is safe because we control both halves of the
    classification slot (they're class label + kind token), so the
    only ``%2F`` in the slot ever comes from our intentional join.
    The case-folded lookup-key invariant is preserved because we
    always call ``resolve`` with the same composite slot string for
    a given ``(subject_class, kind)`` pair, so subsequent lookups
    return the same minted IRI.

    Args:
        iri_resolver: The :class:`IRIResolver` to mint via.
        normalized: The normalized predicate string (per Req 19.1),
            e.g. ``"works_for"``.
        kind: The property kind segment, either ``"ObjectProperty"``
            or ``"DatatypeProperty"``.
        subject_class: The source-graph class label (space-separated
            form, e.g. ``"Financial Instrument"``).

    Returns:
        The full property IRI as a string.
    """
    composite_slot = f"{subject_class}/{kind}"
    raw = iri_resolver.resolve(normalized, composite_slot)
    # Replace the percent-encoded slash with a literal slash so the
    # IRI has the desired path structure.
    return raw.replace("%2F", "/")


# ── Rule 4 — SPC facts → owl:DatatypeProperty (T-Box) ──────────────────
#
# Implements Task 12.4 / Requirement 13. SPC ("subject-predicate-
# complement") facts have a populated ``complement`` literal but no
# object entity. The complement is the literal value (e.g. "1976",
# "true", "$2.4 billion"); ``predicate`` is the verb-phrase
# ("FOUNDED IN", "IS PUBLIC", "REVENUE_FOR_FY"). Each distinct
# normalized predicate becomes one ``owl:DatatypeProperty`` whose
# ``rdfs:domain`` enumerates every subject class observed and whose
# ``rdfs:range`` is the XSD type inferred from all complement values
# (unanimous → that type, mixed → ``xsd:string``).


# Map ``infer_xsd_type``'s short string output to the rdflib URIRef.
# rdflib's ``XSD`` namespace is the standard XML Schema namespace.
_XSD_TYPE_MAP = {
    "xsd:integer": XSD.integer,
    "xsd:decimal": XSD.decimal,
    "xsd:boolean": XSD.boolean,
    "xsd:date": XSD.date,
    "xsd:dateTime": XSD.dateTime,
    "xsd:string": XSD.string,
}


def _is_spc_fact(fact: FactRecord) -> bool:
    """Return True iff a fact is a fully-populated SPC literal-fact.

    A fact is SPC when ``complement`` is populated and ``object_value``
    is None. Defensively excludes facts that have BOTH populated
    (malformed upstream data); those are filtered out and won't
    contribute to either Rule 3 or Rule 4.
    """
    return bool(fact.complement) and not fact.object_value


# NOTE: dev-graph data gap (parent-spec-task-12B) — datatype property output may be sparse
def induce_datatype_properties(
    facts: list[FactRecord],
    class_iris: dict[str, str],
    iri_resolver: IRIResolver,
    *,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> Graph:
    """Induce ``owl:DatatypeProperty`` declarations from SPC facts.

    Implements Rule 4 (Requirement 13, Task 12D-amended). Aggregates
    SPC facts by ``(normalized_predicate, subject_class)`` and emits
    one ``owl:DatatypeProperty`` per distinct aggregation key. Each
    property has exactly ONE ``rdfs:domain`` triple (the subject
    class) and ONE ``rdfs:range`` triple (the XSD type inferred from
    the complement values within this aggregation key — i.e. per-class
    inference, NOT pooled across subject classes).

    The change from the original Rule 4 contract: the same predicate
    spelled identically across two different subject classes used to
    collapse into a single property with multiple ``rdfs:domain``
    triples and a pooled XSD inference. After Task 12D,
    ``Vehicle.weight`` (in tons) and ``Person.weight`` (in kg) are
    distinct properties, each with its own domain and per-class XSD
    range. See ``OntologyDevelopmentNotes/iri-minting-discussion.md``
    for the full reasoning.

    Per aggregation key the function emits:

    - one ``rdf:type owl:DatatypeProperty`` declaration (Req 13.2)
    - exactly one ``rdfs:domain`` triple (Req 13.4 — the subject class)
    - exactly one ``rdfs:range`` triple with the XSD type inferred
      from this aggregation key's complement values (Req 13.5, see
      Req 18)
    - one ``rdfs:label`` whose value is the canonical-by-frequency
      original predicate string within this aggregation key
      (Req 13.7 / Req 19.3, applied per-key — not pooled across
      subject classes)
    - one ``rdfs:comment`` summarizing observation frequency on this
      single subject class (Req 13.8)

    The property IRI is minted via
    ``iri_resolver.resolve(normalized_predicate, f"{subject_class}/DatatypeProperty")``
    (Req 13.3). The resulting IRI shape is
    ``{prefix}/{Subject-Class}/DatatypeProperty/{normalized_predicate}``.

    Cross-(predicate, class) equivalence (Req 13.6):

    - Distinct aggregation keys produce distinct property IRIs by
      default. The optional Task 12B.2 property normalizer can
      detect when two distinct IRIs are semantically equivalent and
      emit ``owl:equivalentProperty`` axioms linking them.

    Per-fact filtering (Req 13.9):

    - SPO facts (object_value populated) are silently filtered out by
      ``_is_spc_fact``; Rule 3 will handle them.
    - Facts whose ``subject_class`` has no entry in ``class_iris``
      (i.e. that class was below the floor threshold in Rule 1) are
      skipped silently.
    - A predicate that normalizes to the empty string is logged at
      WARNING and skipped.
    - If ALL facts for a given aggregation key are skipped, no
      property declaration is emitted for that key.

    XSD range inference (Req 18):

    - Within each aggregation key, every contributing fact's
      complement value is inspected via ``infer_xsd_type``. The
      result is a single XSD type:
        - unanimous across all values for this key → that type
        - mixed → ``xsd:string``
        - empty list → ``xsd:string``

    Same-predicate dual emission (Object/Datatype):

    If the same normalized predicate appears in BOTH SPO and SPC
    facts on the same subject class, Rule 3 emits an
    ``owl:ObjectProperty`` and Rule 4 emits an ``owl:DatatypeProperty``
    at DIFFERENT IRIs (the kind segment differs:
    ``ObjectProperty/<class>`` vs ``DatatypeProperty/<class>``).
    A downstream reviewer can merge them via ``owl:equivalentProperty``
    if desired.

    Per-key fault isolation: a resolver failure for one aggregation
    key logs a WARNING and skips just that key; the rest of the
    batch continues. Empty inputs return an empty graph.

    Args:
        facts: List of :class:`FactRecord`. Both SPO and SPC may be
            present; SPO are filtered out internally.
        class_iris: Mapping from class label (the entity-side
            space-separated form, e.g. ``"Financial Instrument"``)
            to the ``owl:Class`` IRI minted by Rule 1.
        iri_resolver: An :class:`IRIResolver` for minting deterministic
            property IRIs.
        namespace_language: Canonical namespace language. Generated labels and
            comments compatible with it carry its tag; everything else keeps
            the untagged ``xsd:string`` form. ``en`` asserts no tags.

    Returns:
        An :class:`rdflib.Graph` containing one declaration block per
        emitted ``owl:DatatypeProperty`` plus the standard prefix
        bindings. Empty if no eligible facts were found.
    """
    # Local import — keep the type-inference helper colocated with
    # its rule.
    from coa_ontology.inducer.unstructured.services.normalizers import (
        infer_xsd_type,
    )

    g = Graph()
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)

    if not facts:
        log.info("induce_datatype_properties: empty facts input — returning empty graph")
        return g

    # First pass — filter to SPC facts whose subject class is known,
    # then aggregate by (normalized_predicate, subject_class).
    #
    # Aggregation key = (normalized_predicate, subject_class). Value:
    #   "originals":   Counter of original predicate strings observed
    #                  on this exact (predicate, subject_class) pair
    #   "subject_iri": the Rule 1 owl:Class IRI for this aggregation
    #                  key's subject class (constant for all entries
    #                  under the same key)
    #   "complements": list of complement value strings for this key
    #   "fact_count":  total contributing facts (for comment)
    aggregates: dict[tuple[str, str], dict] = defaultdict(
        lambda: {
            "originals": Counter(),
            "subject_iri": None,
            "complements": [],
            "fact_count": 0,
        }
    )

    total_considered = 0
    skipped_spo = 0
    skipped_missing_class = 0
    skipped_empty_predicate = 0

    for fact in facts:
        total_considered += 1

        if not _is_spc_fact(fact):
            skipped_spo += 1
            continue

        subj_iri = class_iris.get(fact.subject_class)
        if subj_iri is None:
            log.debug(
                "induce_datatype_properties: skipping fact predicate=%r "
                "subject_class=%r — no owl:Class IRI for that class",
                fact.predicate,
                fact.subject_class,
            )
            skipped_missing_class += 1
            continue

        normalized = normalize_predicate(fact.predicate)
        if not normalized:
            log.warning(
                "induce_datatype_properties: skipping fact with predicate %r that normalizes to empty string",
                fact.predicate,
            )
            skipped_empty_predicate += 1
            continue

        key = (normalized, fact.subject_class)
        agg = aggregates[key]
        agg["originals"][fact.predicate] += 1
        agg["subject_iri"] = subj_iri
        # complement is non-None per _is_spc_fact; the cast is for the
        # type-checker.
        agg["complements"].append(fact.complement or "")
        agg["fact_count"] += 1

    if not aggregates:
        log.warning(
            "induce_datatype_properties: zero DatatypeProperty declarations "
            "emitted (total facts considered=%d, skipped SPO=%d, "
            "skipped missing class=%d, skipped empty predicate=%d). "
            "Returning empty graph.",
            total_considered,
            skipped_spo,
            skipped_missing_class,
            skipped_empty_predicate,
        )
        return g

    # Second pass — mint one property per (normalized_predicate,
    # subject_class) aggregation key.
    properties_emitted = 0
    skipped_resolver_failure = 0

    for (normalized, subject_class), agg in aggregates.items():
        try:
            property_iri_str = _mint_property_iri(
                iri_resolver,
                normalized=normalized,
                kind="DatatypeProperty",
                subject_class=subject_class,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "induce_datatype_properties: IRI resolver failed for "
                "(normalized=%r, subject_class=%r) (originals=%r): %s "
                "— skipping",
                normalized,
                subject_class,
                dict(agg["originals"]),
                exc,
            )
            skipped_resolver_failure += 1
            continue

        if not property_iri_str:
            log.warning(
                "induce_datatype_properties: resolver returned empty IRI "
                "for (normalized=%r, subject_class=%r) — skipping",
                normalized,
                subject_class,
            )
            skipped_resolver_failure += 1
            continue

        property_uri = URIRef(property_iri_str)

        # Req 13.2 — declaration.
        g.add((property_uri, RDF.type, OWL.DatatypeProperty))

        # Req 13.4 — exactly one rdfs:domain (the subject class IRI).
        g.add((property_uri, RDFS.domain, URIRef(agg["subject_iri"])))

        # Req 13.5 + Req 18 — XSD range inference (per aggregation
        # key, NOT pooled across subject classes). ``infer_xsd_type``
        # returns the string form ("xsd:integer"); map to the URIRef.
        xsd_type_str = infer_xsd_type(agg["complements"])
        xsd_uri = _XSD_TYPE_MAP.get(xsd_type_str, XSD.string)
        g.add((property_uri, RDFS.range, xsd_uri))

        # Req 13.7 / Req 19.3 — canonical original-spelling label,
        # selected within this aggregation key.
        canonical = select_canonical_label(dict(agg["originals"]))
        g.add(
            (
                property_uri,
                RDFS.label,
                generated_literal(canonical, namespace_language, datatype=XSD.string),
            )
        )

        # Req 13.8 — observation-frequency comment. After 12D each
        # property has exactly one subject class, so the wording is
        # always single-class.
        #
        # The class name is deliberately NOT interpolated into the sentence. It
        # comes from the corpus, so on a non-English corpus an English frame around
        # it produced a literal holding two languages ("Observed across 1 fact(s)
        # in class 技術的特性"). RDF 1.1 gives a literal one language tag or one
        # datatype, never a tag per fragment, so such a literal cannot be tagged
        # correctly under any scheme — and this one is manufactured by us, so no
        # amount of extraction-prompt work can remove it. The name is already
        # reachable from this property's rdfs:domain, so stating it here only
        # duplicated it in a form that made the literal untaggable. The same holds
        # for a Japanese frame around a Latin-mixed class name such as PLC制御盤.
        comment = render("datatype_property_observation", namespace_language, count=agg["fact_count"])
        g.add(
            (
                property_uri,
                RDFS.comment,
                generated_literal(comment, namespace_language, datatype=XSD.string),
            )
        )

        properties_emitted += 1

    log.info(
        "induce_datatype_properties: emitted %d owl:DatatypeProperty "
        "declarations from %d facts (skipped SPO=%d, missing class=%d, "
        "empty predicate=%d, resolver failure=%d)",
        properties_emitted,
        total_considered,
        skipped_spo,
        skipped_missing_class,
        skipped_empty_predicate,
        skipped_resolver_failure,
    )

    return g


# ── Rule 5 — __Topic__ nodes → skos:Concept (T-Box) ────────────────────
#
# Implements Task 12.5 / Requirement 14. Topics in the lexical graph
# are thematic groupings of statements (e.g. ``"Financial Holdings
# Summary"``, ``"Legal Proceedings and Litigation"``). Each topic
# becomes a ``skos:Concept`` and all concepts are linked via
# ``skos:inScheme`` to a single ``skos:ConceptScheme`` per pipeline
# run.
#
# Why SKOS and not OWL? Topics are not classes — they are *labels*
# applied to chunks of source text. SKOS is the W3C-standard vocabulary
# for "lightweight" knowledge organization (taxonomies, thesauri,
# subject classification). Using ``skos:Concept`` rather than
# ``owl:Class`` keeps the T-Box clean: classes describe what entities
# *are*; concepts describe what entities are *about*. A reasoner over
# this ontology won't accidentally infer that an ``Apple`` (the
# Company) is-a ``Financial Holdings Summary`` — it will see two
# disjoint semantic worlds joined only by application-level metadata
# (e.g. ``dcterms:subject``) which Rule 6 may add later.


def induce_concepts(
    topics: list[TopicRecord],
    iri_resolver: IRIResolver,
    namespace_name: str,
    *,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> Graph:
    """Induce ``skos:Concept`` declarations from ``__Topic__`` nodes.

    Implements Rule 5 (Requirement 14): every ``__Topic__`` with a
    non-empty ``value`` becomes one ``skos:Concept`` typed and labelled
    via ``skos:prefLabel``. All concepts are linked to a single
    ``skos:ConceptScheme`` (one per pipeline run) via ``skos:inScheme``.

    Per topic the function emits:

    - one ``rdf:type skos:Concept`` declaration (Req 14.1)
    - one ``skos:prefLabel`` with the topic's ``value`` as
      ``xsd:string`` (Req 14.3)
    - one ``skos:inScheme`` linking the concept to the run's
      ``skos:ConceptScheme`` (Req 14.4)

    Per pipeline run the function emits exactly one
    ``skos:ConceptScheme`` block (Req 14.4 / Req 14.5):

    - ``rdf:type skos:ConceptScheme`` declaration
    - ``rdfs:label`` of the form ``"{namespace_name} Topic Scheme"``
      with ``xsd:string`` datatype

    The concept IRI is minted via
    ``iri_resolver.resolve(topic.value, "Topic")`` (Req 14.2),
    producing IRIs of shape
    ``{prefix}/Topic/{normalized-topic-value}``. The classification
    slot is the literal token ``"Topic"`` — topics are global concepts
    (members of a single ``ConceptScheme``), not per-class entities,
    so the slot does NOT include a class qualifier. This is the same
    asymmetry as the ``"class"`` literal used for ``owl:Class`` IRIs
    (Req 10.2).

    The ConceptScheme IRI is minted via
    ``iri_resolver.resolve(namespace_name, "ConceptScheme")``,
    producing an IRI like
    ``{prefix}/ConceptScheme/{normalized-namespace-name}``.

    Per-topic filtering (Req 14.6):

    - Topics whose ``value`` is empty or whitespace-only are skipped
      with a WARNING log including the topic's ``topic_id``. They
      cannot have a meaningful ``skos:prefLabel`` and the resolver
      would refuse to mint an empty-segment IRI.
    - Topics whose ``value`` normalizes to empty (e.g. only
      special characters) cause the resolver to raise; the function
      catches the resolver error and skips the topic with a WARNING.
    - The ConceptScheme is emitted regardless — even if every topic
      is skipped, the scheme is a valid pipeline artifact (a future
      run may add concepts to it via Task 12B.x cross-run reuse).

    Per-topic fault isolation: a resolver failure on one topic logs
    a WARNING and skips just that topic; the rest of the batch
    continues. Programmer errors (TypeError, ImportError) propagate.

    Args:
        topics: List of :class:`TopicRecord` from
            :meth:`LexicalGraphStore.get_topics`. Each has a
            ``topic_id``, ``value``, and ``statement_count``. The
            ``statement_count`` is informational only — Rule 5 does
            not filter on it (a topic with zero statements is still
            a valid concept; whether to filter is the orchestrator's
            concern).
        iri_resolver: An :class:`IRIResolver` for minting deterministic
            concept and ConceptScheme IRIs.
        namespace_name: The pipeline's namespace name (e.g.
            ``"my-graph"``). Used to build the ConceptScheme IRI and
            its ``rdfs:label`` (``"{namespace_name} Topic Scheme"``).
        namespace_language: Canonical namespace language. Generated labels and
            comments compatible with it carry its tag; everything else keeps
            the untagged ``xsd:string`` form. ``en`` asserts no tags.

    Returns:
        An :class:`rdflib.Graph` containing:
          - one ``skos:ConceptScheme`` declaration (always emitted)
          - three triples per emitted concept (declaration, prefLabel,
            inScheme)
        plus the standard prefix bindings. Empty if ``namespace_name``
        is empty (we cannot mint the scheme without it).

    Raises:
        ValueError: If ``namespace_name`` is empty or None — the
            ConceptScheme cannot be minted without it. Topics that
            individually fail to mint are logged and skipped; the
            scheme-level error is fatal because there's nothing
            sensible to return.
    """
    g = Graph()
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)

    if not namespace_name:
        # Defensive: namespace_name is the only handle the function has
        # on the run identity. Without it we cannot build the
        # ConceptScheme IRI or label, so degrade visibly rather than
        # silently.
        raise ValueError(
            "induce_concepts: namespace_name is required to mint the skos:ConceptScheme IRI; got empty/None."
        )

    # ── Mint the ConceptScheme first (Req 14.4 / 14.5) ──────────────
    #
    # The scheme is always emitted. Even if every topic is skipped, a
    # valid ``skos:ConceptScheme`` is a useful pipeline output (it
    # serves as an anchor that future runs can attach to).
    try:
        scheme_iri_str = iri_resolver.resolve(namespace_name, "ConceptScheme")
    except Exception as exc:  # noqa: BLE001
        # If the scheme itself cannot be minted, the rule's whole
        # output is meaningless — surface the error.
        raise ValueError(
            f"induce_concepts: failed to mint ConceptScheme IRI for namespace_name={namespace_name!r}: {exc}"
        ) from exc

    scheme_uri = URIRef(scheme_iri_str)
    g.add((scheme_uri, RDF.type, SKOS.ConceptScheme))
    g.add(
        (
            scheme_uri,
            RDFS.label,
            generated_literal(
                render("topic_scheme_label", namespace_language, namespace=namespace_name),
                namespace_language,
                datatype=XSD.string,
            ),
        )
    )

    if not topics:
        log.info("induce_concepts: empty topics input — emitted skos:ConceptScheme only (no skos:Concept triples)")
        return g

    # ── Per-topic emission (Req 14.1 / 14.2 / 14.3 / 14.4) ──────────
    concepts_emitted = 0
    skipped_empty_value = 0
    skipped_resolver_failure = 0

    for topic in topics:
        # Req 14.6 — topics with empty/null value are skipped with
        # a WARNING naming the topic_id.
        value = (topic.value or "").strip()
        if not value:
            log.warning(
                "induce_concepts: topic with empty/null value (topic_id=%r) — skipping",
                topic.topic_id,
            )
            skipped_empty_value += 1
            continue

        try:
            concept_iri_str = iri_resolver.resolve(topic.value, "Topic")
        except Exception as exc:  # noqa: BLE001
            # Per-topic fault isolation. Most likely cause: the value
            # normalizes to empty (e.g. all-punctuation strings) and
            # the resolver refuses to mint an empty-segment IRI.
            log.warning(
                "induce_concepts: IRI resolver failed for topic (topic_id=%r, value=%r): %s — skipping",
                topic.topic_id,
                topic.value,
                exc,
            )
            skipped_resolver_failure += 1
            continue

        if not concept_iri_str:
            log.warning(
                "induce_concepts: resolver returned empty IRI for topic (topic_id=%r, value=%r) — skipping",
                topic.topic_id,
                topic.value,
            )
            skipped_resolver_failure += 1
            continue

        concept_uri = URIRef(concept_iri_str)

        # Req 14.1 — declaration.
        g.add((concept_uri, RDF.type, SKOS.Concept))
        # Req 14.3 — prefLabel with xsd:string datatype.
        g.add(
            (
                concept_uri,
                SKOS.prefLabel,
                generated_literal(topic.value, namespace_language, datatype=XSD.string),
            )
        )
        # Req 14.4 — link concept to its scheme.
        g.add((concept_uri, SKOS.inScheme, scheme_uri))

        concepts_emitted += 1

    log.info(
        "induce_concepts: emitted %d skos:Concept declarations "
        "(total topics considered=%d, skipped empty value=%d, "
        "skipped resolver failure=%d) within scheme %s",
        concepts_emitted,
        len(topics),
        skipped_empty_value,
        skipped_resolver_failure,
        scheme_iri_str,
    )

    return g
