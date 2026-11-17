# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""T-Box axiom induction from a graphrag-toolkit lexical knowledge graph.

Each rule returns an :class:`rdflib.Graph` fragment; the pipeline merges them
and the serializer emits Turtle.

1. Classes with ``count >= floor`` become ``owl:Class`` (``induce_classes``).
2. Sampled entities become ``owl:NamedIndividual`` (``induce_individuals``, optional).
3. SPO facts become ``owl:ObjectProperty`` (``induce_object_properties``).
4. SPC facts become ``owl:DatatypeProperty`` (``induce_datatype_properties``).
5. ``__Topic__`` nodes become ``skos:Concept`` (``induce_concepts``).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from coa_common.language_tags import DEFAULT_LANGUAGE
from rdflib import OWL, RDF, RDFS, XSD, Graph, URIRef
from rdflib.namespace import DCTERMS, SKOS

from coa_ontology.inducer.services.generated_text import render
from coa_ontology.inducer.services.literal_language import generated_literal
from coa_ontology.inducer.unstructured.services.iri_resolver import normalize_for_lookup_key
from coa_ontology.inducer.unstructured.services.normalizers import (
    infer_xsd_type,
    normalize_predicate,
    select_canonical_label,
)
from coa_ontology.inducer.unstructured.services.sampler import (
    _camel_to_spaced,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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


DEFAULT_FLOOR = 3

# Rule 5's class-to-topic link is optional enrichment, but its reverse lookup
# duplicates normalized class-label keys for the lifetime of the emitted graph.
# Ten thousand source entries keeps that auxiliary index in the low-megabyte
# range for ordinary label/IRI sizes while covering the current unstructured
# induction operating envelope. Above the limit, all class-to-topic links are
# disabled before indexing starts: a partial index would make output depend on
# mapping iteration order and could turn an unseen ambiguity into a false exact
# match.
MAX_CONCEPT_CLASS_LOOKUP_ENTRIES = 10_000
_AMBIGUOUS_LABEL_FINGERPRINT_SAMPLE_SIZE = 5
_AMBIGUOUS_LABEL_LOG_KEY = secrets.token_bytes(16)


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


@dataclass
class _ClassVocabulary:
    """Annotations accumulated for one consolidated class IRI.

    ``label_counts`` holds every observed surface form for the IRI keyed to
    its observation weight, in the form the label is emitted: the original
    ``__SYS_Class__`` value, not the spaced lookup form. Only the
    lexical-graph label of a :class:`ClassRecord` carries real evidence;
    sibling forms contributed by the normalizer enter at zero weight so they
    still surface as ``skos:altLabel`` even when their own record fell below
    the floor.
    """

    label_counts: Counter[str] = field(default_factory=Counter)
    close_matches: set[str] = field(default_factory=set)
    # Spaced lookup labels for this IRI, used to find the generated
    # description, which ``descriptions`` keys by the spaced form.
    lookup_labels: set[str] = field(default_factory=set)


def _fold_surface_forms(observed: Mapping[str, int]) -> dict[str, int]:
    """Collapse surface forms the shared lookup key considers identical.

    ``normalize_for_lookup_key`` is the equivalence relation the inducer
    already uses to decide that two labels denote the same thing — it backs
    the normalizer's deterministic class match and the topic/class link in
    Rule 5. Emitting a ``skos:altLabel`` for a form that relation calls
    identical to the preferred label adds a triple that asserts nothing, so
    such forms are folded into one group here and their counts summed.

    The relation is narrower than plain case-folding: it preserves the
    separator, so ``"HAS_PART"`` (``has_part``) and ``"has part"``
    (``has-part``) stay distinct and both survive as labels. What it folds
    is padding and casing noise — ``"  DELIVERED TO  "``, ``"delivered to"``
    and ``"DELIVERED TO"`` collapse to a single group.

    Args:
        observed: Mapping from raw surface form to its observation count.

    Returns:
        Mapping from each group's representative form — the highest-count
        member, ties broken lexicographically — to the group's summed
        count. Whitespace-only forms are dropped. Empty when ``observed``
        holds no usable form.
    """
    groups: dict[str, Counter[str]] = defaultdict(Counter)
    for original, count in observed.items():
        label = (original or "").strip()
        if not label:
            continue
        groups[normalize_for_lookup_key(label) or label][label] += count
    folded: dict[str, int] = {}
    for members in groups.values():
        folded[select_canonical_label(dict(members))] = sum(members.values())
    return folded


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

    Classifications with ``count >= floor`` become ``owl:Class`` resources, with the
    IRI from ``normalizer_results`` (keyed by space-separated label; classes
    without one are skipped with a WARNING, and ``iri_resolver`` is unused). Exact
    matches sharing an IRI are consolidated into one declaration: the spelling
    observed most often (ties broken lexicographically) is ``rdfs:label``, other
    spellings are ``skos:altLabel`` unless the shared lookup key folds them into
    the preferred form. The preferred form's description becomes ``rdfs:comment``
    when ``descriptions`` has one. A ``close_match`` stays a separate class linked
    with ``skos:closeMatch``. Returns an empty graph if zero classes pass the floor.
    Generated labels and comments compatible with ``namespace_language`` carry its
    tag; everything else stays untagged ``xsd:string`` (``en`` asserts no tags).
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

    classes_skipped_floor = 0
    classes_skipped_missing_normalizer = 0
    vocabulary_by_iri: dict[str, _ClassVocabulary] = {}

    # The normalizer and the description generator key classes by the spaced
    # lookup form, but the label is the original ``__SYS_Class__`` value. Map
    # every lookup form back to the original form it came from, across all
    # records (a sibling the normalizer consolidated may belong to a record
    # below the floor). When several originals share one
    # lookup form, the most observed one wins, ties broken lexicographically.
    originals_by_lookup: dict[str, Counter[str]] = defaultdict(Counter)
    for cls in class_distribution:
        originals_by_lookup[_label_for_lookup(cls.value)][cls.value] += max(cls.count, 1)

    def emitted_form(lookup_label: str) -> str:
        originals = originals_by_lookup.get(lookup_label)
        return select_canonical_label(dict(originals)) if originals else lookup_label

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

        preferred_lookup_label = nr.preferred_label or spaced_label
        vocabulary = vocabulary_by_iri.setdefault(nr.class_iri, _ClassVocabulary())

        # Weight the lexical-graph label by the count actually observed, so
        # the dominant spelling — not whichever record the normalizer happened
        # to see first — becomes the preferred label. A record that reached
        # this line passed the floor, so it always outweighs a zero-weight
        # sibling even when ``floor`` was configured down to 0.
        if camel_label:
            vocabulary.label_counts[camel_label] += max(cls.count, 1)
            vocabulary.lookup_labels.add(spaced_label)

        # Retain every other spelling the normalizer consolidated into this
        # IRI. These carry no count of their own: a form whose own record
        # fell below the floor never reaches this loop, and re-weighting the
        # normalizer's identity label here would double-count the record
        # that does carry it.
        for sibling_lookup_label in (preferred_lookup_label, *nr.alternative_labels):
            if not sibling_lookup_label:
                continue
            vocabulary.lookup_labels.add(sibling_lookup_label)
            sibling_label = emitted_form(sibling_lookup_label)
            if sibling_label not in vocabulary.label_counts:
                vocabulary.label_counts[sibling_label] = 0

        if nr.band == "close_match" and nr.matched_existing_iri:
            vocabulary.close_matches.add(nr.matched_existing_iri)

    # Counts emitted declarations, which is a count of consolidated IRIs
    # rather than of input records: several records sharing an IRI produce
    # one declaration carrying their spellings as altLabels.
    classes_emitted = 0
    classes_skipped_no_label = 0

    for class_iri, vocabulary in vocabulary_by_iri.items():
        surface_forms = _fold_surface_forms(vocabulary.label_counts)
        if not surface_forms:
            log.warning(
                "induce_classes: no usable label for class IRI %s (observed=%r); skipping",
                class_iri,
                sorted(vocabulary.label_counts),
            )
            classes_skipped_no_label += 1
            continue

        preferred_label = select_canonical_label(surface_forms)
        alternative_labels = sorted(label for label in surface_forms if label != preferred_label)

        class_uri = URIRef(class_iri)
        g.add((class_uri, RDF.type, OWL.Class))
        # rdfs:label is the original __SYS_Class__ value
        # (CamelCase form), with explicit xsd:string datatype. Extraction names
        # the class in the namespace language, so a compatible label carries
        # the namespace tag instead (see ``generated_literal``). The other
        # consolidated spellings are the same kind of text and follow the
        # same rule as ``skos:altLabel``.
        g.add((class_uri, RDFS.label, generated_literal(preferred_label, namespace_language, datatype=XSD.string)))
        for alternative_label in alternative_labels:
            g.add(
                (
                    class_uri,
                    SKOS.altLabel,
                    generated_literal(alternative_label, namespace_language, datatype=XSD.string),
                )
            )

        # ``descriptions`` is keyed by the spaced lookup form, and the
        # preferred label can be any of the consolidated forms, so try the
        # preferred form's lookup label first and then the others in order.
        preferred_lookup = _label_for_lookup(preferred_label)
        comment = descriptions.get(preferred_lookup) or next(
            (
                descriptions[label]
                for label in sorted(vocabulary.lookup_labels - {preferred_lookup})
                if descriptions.get(label)
            ),
            None,
        )
        if comment:
            g.add((class_uri, RDFS.comment, generated_literal(comment, namespace_language, datatype=XSD.string)))
        else:
            log.info(
                "induce_classes: no description for class %r; emitting rdfs:label only",
                preferred_label,
            )

        for matched_iri in sorted(vocabulary.close_matches):
            g.add((class_uri, SKOS.closeMatch, URIRef(matched_iri)))
        classes_emitted += 1

    if classes_emitted == 0:
        log.warning(
            "induce_classes: zero classes passed the floor threshold "
            "(floor=%d, total considered=%d, skipped due to floor=%d, "
            "skipped due to missing normalizer=%d, skipped due to no usable label=%d). "
            "Returning empty graph.",
            floor,
            len(class_distribution),
            classes_skipped_floor,
            classes_skipped_missing_normalizer,
            classes_skipped_no_label,
        )
    else:
        log.info(
            "induce_classes: emitted %d owl:Class declarations "
            "(total considered=%d, below floor=%d, missing normalizer=%d, no usable label=%d)",
            classes_emitted,
            len(class_distribution),
            classes_skipped_floor,
            classes_skipped_missing_normalizer,
            classes_skipped_no_label,
        )

    return g


def _add_vocabulary_labels(
    graph: Graph,
    subject: URIRef,
    originals: Counter[str],
    namespace_language: str,
) -> None:
    """Emit one preferred label and every other observed surface form.

    Surface forms are folded through :func:`_fold_surface_forms` first, so
    padding and casing noise does not become a ``skos:altLabel`` that repeats
    the preferred label. Emits nothing when no usable form survives folding
    (every observed spelling was whitespace); a label built from padding
    alone would be worse than none. Both the label and the alternatives are
    extracted text, so they follow the same tagging rule
    (:func:`generated_literal`) the label alone followed before.
    """
    surface_forms = _fold_surface_forms(originals)
    if not surface_forms:
        log.warning(
            "_add_vocabulary_labels: no usable surface form for %s (observed=%r); emitting no labels",
            subject,
            sorted(originals),
        )
        return

    canonical = select_canonical_label(surface_forms)
    graph.add(
        (
            subject,
            RDFS.label,
            generated_literal(canonical, namespace_language, datatype=XSD.string),
        )
    )
    for alternative in sorted(label for label in surface_forms if label != canonical):
        graph.add(
            (
                subject,
                SKOS.altLabel,
                generated_literal(alternative, namespace_language, datatype=XSD.string),
            )
        )


# ── Rule 2 — Sampled entities → owl:NamedIndividual (A-Box, optional) ──


def induce_individuals(
    sampled_entities: list[EntityRecord],
    class_iris: dict[str, str],
    iri_resolver: IRIResolver,
    *,
    include_individuals: bool = False,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> Graph:
    """Induce ``owl:NamedIndividual`` declarations from sampled entities.

    A no-op returning an empty graph unless ``include_individuals=True``. Each
    entity whose class is in ``class_iris`` (space-separated label to Rule 1 IRI)
    gets ``rdf:type owl:NamedIndividual``, ``rdf:type <class_iri>`` and an
    ``rdfs:label``. The IRI is ``iri_resolver.resolve(entity.value,
    entity.classification)``, so the class label is the slot (``.../Company/Apple``),
    distinct from the literal ``class`` slot of T-Box class IRIs. Entities with an
    unknown class or a resolver failure are skipped with a WARNING.
    Generated labels and comments compatible with ``namespace_language`` carry its
    tag; everything else stays untagged ``xsd:string`` (``en`` asserts no tags).
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

        g.add((individual_uri, RDF.type, OWL.NamedIndividual))
        g.add((individual_uri, RDF.type, class_uri))
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

    Aggregates SPO facts by ``(normalized_predicate, subject_class)``. Per key it
    emits one declaration, exactly one ``rdfs:domain`` (the subject class; several
    domains would mean their intersection in OWL), one ``rdfs:range`` per distinct
    object class, an ``rdfs:label`` with the most frequent original spelling and
    ``skos:altLabel`` for the rest. The IRI is
    ``{prefix}/{Subject-Class}/ObjectProperty/{normalized_predicate}``.

    Facts whose subject or object class is not in ``class_iris`` are skipped, SPC
    facts are filtered out, and predicates that normalize to empty are skipped
    with a WARNING. A resolver failure skips only that key.
    Generated labels and comments compatible with ``namespace_language`` carry its
    tag; everything else stays untagged ``xsd:string`` (``en`` asserts no tags).
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

        g.add((property_uri, RDF.type, OWL.ObjectProperty))

        g.add((property_uri, RDFS.domain, URIRef(agg["subject_iri"])))

        # One rdfs:range per distinct object class; fan-out within one key is
        # genuine relational ambiguity.
        for rng_iri in sorted(agg["ranges"]):
            g.add((property_uri, RDFS.range, URIRef(rng_iri)))
            range_triples += 1

        _add_vocabulary_labels(g, property_uri, agg["originals"], namespace_language)

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

    The resolver percent-encodes ``/`` in the classification slot; it is restored
    here, which is safe because both halves of the slot are ours.
    """
    composite_slot = f"{subject_class}/{kind}"
    raw = iri_resolver.resolve(normalized, composite_slot)
    return raw.replace("%2F", "/")


# ── Rule 4 — SPC facts → owl:DatatypeProperty (T-Box) ──────────────────


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


def induce_datatype_properties(
    facts: list[FactRecord],
    class_iris: dict[str, str],
    iri_resolver: IRIResolver,
    *,
    namespace_language: str = DEFAULT_LANGUAGE,
) -> Graph:
    """Induce ``owl:DatatypeProperty`` declarations from SPC facts.

    Aggregates SPC facts by ``(normalized_predicate, subject_class)``. Per key it
    emits one declaration, exactly one ``rdfs:domain`` (the subject class), one
    ``rdfs:range`` (the XSD type unanimous across the key's complements, else
    ``xsd:string``), labels as in Rule 3 and an observation-count
    ``rdfs:comment``. The IRI is
    ``{prefix}/{Subject-Class}/DatatypeProperty/{normalized_predicate}``, so a
    predicate seen in both SPO and SPC facts gets two distinct IRIs.

    SPO facts and facts whose subject class is not in ``class_iris`` are skipped;
    predicates that normalize to empty are skipped with a WARNING. A resolver
    failure skips only that key.
    Generated labels and comments compatible with ``namespace_language`` carry its
    tag; everything else stays untagged ``xsd:string`` (``en`` asserts no tags).
    """
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

        g.add((property_uri, RDF.type, OWL.DatatypeProperty))

        g.add((property_uri, RDFS.domain, URIRef(agg["subject_iri"])))

        xsd_type_str = infer_xsd_type(agg["complements"])
        xsd_uri = _XSD_TYPE_MAP.get(xsd_type_str, XSD.string)
        g.add((property_uri, RDFS.range, xsd_uri))

        _add_vocabulary_labels(g, property_uri, agg["originals"], namespace_language)

        # The corpus class name is not interpolated: RDF 1.1 gives a literal one
        # language tag, so a frame around a foreign-language name is untaggable.
        # The name is reachable via rdfs:domain.
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
# Topics label chunks of source text; they are not classes, so SKOS keeps a
# reasoner from inferring that an entity is-a topic.


def induce_concepts(
    topics: list[TopicRecord],
    iri_resolver: IRIResolver,
    namespace_name: str,
    *,
    namespace_language: str = DEFAULT_LANGUAGE,
    class_iris: dict[str, str] | None = None,
) -> Graph:
    """Induce ``skos:Concept`` declarations from ``__Topic__`` nodes.

    Every topic with a non-empty ``value`` becomes a ``skos:Concept`` with a
    ``skos:prefLabel`` and ``skos:inScheme`` to one ``skos:ConceptScheme`` per run
    (labelled ``"{namespace_name} Topic Scheme"``, emitted even if every topic is
    skipped). Concept IRIs are ``{prefix}/Topic/{normalized-value}``; the scheme
    IRI is ``iri_resolver.resolve(namespace_name, "ConceptScheme")``. When
    ``class_iris`` has one unambiguous deterministic label match, the class is
    linked to the concept via ``dcterms:subject``. Empty topics and resolver
    failures are skipped with a WARNING.
    Generated labels and comments compatible with ``namespace_language`` carry its
    tag; everything else stays untagged ``xsd:string`` (``en`` asserts no tags).

    Raises:
        ValueError: If ``namespace_name`` is empty; the scheme cannot be minted.
    """
    g = Graph()
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("skos", SKOS)
    g.bind("dcterms", DCTERMS)

    if not namespace_name:
        # Defensive: namespace_name is the only handle the function has
        # on the run identity. Without it we cannot build the
        # ConceptScheme IRI or label, so degrade visibly rather than
        # silently.
        raise ValueError(
            "induce_concepts: namespace_name is required to mint the skos:ConceptScheme IRI; got empty/None."
        )

    # ── Mint the ConceptScheme first ──────────────
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

    # ── Per-topic emission ──────────
    concepts_emitted = 0
    skipped_empty_value = 0
    skipped_resolver_failure = 0
    skipped_ambiguous_class_link = 0
    class_iri_by_label: dict[str, str | None] = {}
    ambiguous_label_count = 0
    ambiguous_label_fingerprints: list[str] = []
    class_iri_entries = class_iris or {}
    class_lookup_enabled = len(class_iri_entries) <= MAX_CONCEPT_CLASS_LOOKUP_ENTRIES
    if not class_lookup_enabled:
        log.warning(
            "induce_concepts: class-label reverse lookup exceeded its safe limit; "
            "disabling all dcterms:subject links for this call",
            extra={
                "event": "induce_concepts_class_lookup_limit_exceeded",
                "class_iri_entry_count": len(class_iri_entries),
                "class_iri_entry_limit": MAX_CONCEPT_CLASS_LOOKUP_ENTRIES,
            },
        )

    for class_label, class_iri in class_iri_entries.items() if class_lookup_enabled else ():
        lookup_key = normalize_for_lookup_key(class_label)
        if not lookup_key or not class_iri:
            continue

        if lookup_key not in class_iri_by_label:
            class_iri_by_label[lookup_key] = class_iri
            continue

        existing_iri = class_iri_by_label[lookup_key]
        if existing_iri is None or existing_iri == class_iri:
            continue

        # We only need to distinguish exactly one IRI from more than one.
        # Retaining every conflicting IRI would recreate the unbounded-memory
        # problem without changing the matching decision.
        class_iri_by_label[lookup_key] = None
        ambiguous_label_count += 1
        # A process-local keyed fingerprint lets operators correlate repeated
        # collisions within one run without exposing low-entropy customer
        # labels to an offline dictionary attack.
        fingerprint = hashlib.blake2s(
            lookup_key.encode("utf-8"),
            digest_size=6,
            key=_AMBIGUOUS_LABEL_LOG_KEY,
        ).hexdigest()
        ambiguous_label_fingerprints.append(fingerprint)
        ambiguous_label_fingerprints.sort()
        if len(ambiguous_label_fingerprints) > _AMBIGUOUS_LABEL_FINGERPRINT_SAMPLE_SIZE:
            ambiguous_label_fingerprints.pop()

    if class_lookup_enabled and ambiguous_label_count:
        log.debug(
            "induce_concepts: ambiguous normalized class labels detected; "
            "omitting dcterms:subject links for those labels",
            extra={
                "event": "induce_concepts_ambiguous_class_labels",
                "ambiguous_normalized_label_count": ambiguous_label_count,
                "normalized_label_fingerprint_sample": tuple(ambiguous_label_fingerprints),
            },
        )

    for topic in topics:
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

        g.add((concept_uri, RDF.type, SKOS.Concept))
        g.add(
            (
                concept_uri,
                SKOS.prefLabel,
                generated_literal(topic.value, namespace_language, datatype=XSD.string),
            )
        )
        g.add((concept_uri, SKOS.inScheme, scheme_uri))

        # Link a topic to a class only for an unambiguous deterministic
        # label match. Semantic topic/class matching remains a review-time
        # concern; this path deliberately avoids guessing.
        lookup_key = normalize_for_lookup_key(value)
        if class_lookup_enabled and lookup_key in class_iri_by_label:
            matching_class_iri = class_iri_by_label[lookup_key]
            if matching_class_iri is not None:
                g.add((URIRef(matching_class_iri), DCTERMS.subject, concept_uri))
            else:
                # Several class IRIs share this normalized label. The
                # aggregate debug event above identifies the collision
                # without logging customer labels or every matching IRI.
                skipped_ambiguous_class_link += 1

        concepts_emitted += 1

    log.info(
        "induce_concepts: emitted %d skos:Concept declarations "
        "(total topics considered=%d, skipped empty value=%d, "
        "skipped resolver failure=%d, skipped ambiguous class link=%d) within scheme %s",
        concepts_emitted,
        len(topics),
        skipped_empty_value,
        skipped_resolver_failure,
        skipped_ambiguous_class_link,
        scheme_iri_str,
    )

    return g
