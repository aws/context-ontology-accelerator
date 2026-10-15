# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Foundational grounding for unstructured-induced classes.

The unstructured (lexical-graph) induction pipeline mints one ``owl:Class``
per distinct source classification and, in Stage 4, dedupes those classes
against each OTHER within the run (``class_normalizer``). It does NOT, on its
own, align them to the namespace's loaded foundational ontologies (FIBO,
Schema.org, uploaded ontologies, …).

This module adds that alignment by reusing the shared, strategy-agnostic
:class:`~coa_ontology.inducer.services.grounding.GroundingService`
— the exact same recall → exact-name → LLM-rerank pipeline the structured
``table_to_ontology`` strategy uses — via its :meth:`ground_class` entry point.

Given the per-class ``(label, description, embedding vector)`` the pipeline
already computes, :func:`ground_classes` produces a graph of grounding axioms
that MERGE into the induced graph, mirroring the structured emission:

- ``<class> rdfs:subClassOf <foundational>``       (grounded: exact/high-conf)
- ``<class> skos:exactMatch  <foundational>``      (match_type == "exact")
- ``<class> skos:closeMatch  <foundational>``      (match_type == "high_confidence")
- ``<class> skos:relatedMatch <foundational>``     (match_type == "ambiguous")
- ``<class> <ns>matchConfidence "<score>"^^xsd:float``  (annotation)

Design notes:

- **Additive only.** This emits a SEPARATE graph that the pipeline unions
  into the main graph. The within-run ``skos:closeMatch`` links emitted by
  Rule 1 (``induce_classes``) are untouched — those are class↔class dedupe
  links; these are class↔foundational grounding links.
- **Fail-open for per-class faults, fail-loud for rerank infra failures.** A
  genuine per-class error (bad vector, programming bug) is caught and logged and
  that class is left un-grounded (novel), never failing the whole run. But a
  ``GroundingRerankError`` (LLM rerank infrastructure failure — Bedrock error
  after retries, truncated/blocked stopReason, unparseable output) is re-raised
  so the job fails loud instead of silently grounding every class novel — the
  documented fail-loud guarantee (issue #59), matching the structured path. A
  ``GroundingLookupError`` (the lexical metadata listing failed or exceeded a
  bound) is re-raised for the same reason.
- **No-op when disabled.** Empty/None ``ontology_ids`` or a ``None`` service
  → returns an empty graph and the pipeline behaves exactly as pre-grounding.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS

from coa_ontology.inducer.schemas import ConceptMatch
from coa_ontology.inducer.services.grounding import (
    GroundingLookupError,
    GroundingRerankError,
    GroundingService,
)
from coa_ontology.inducer.services.unicode_normalization import (
    filter_letters_marks_numbers,
    simple_case_identity,
    strict_percent_decode,
    width_only_nfkc,
)

log = logging.getLogger(__name__)


def _normalize_name_key(name: str) -> str:
    """Build a canonical key for name matching by stripping separators and lowercasing.

    Mirrors the web-app's ``grounding.ts::normalizeNameKey`` so the SPACED grounding
    label ("Financial Instrument") and the Turtle's CamelCase ``rdfs:label``
    ("FinancialInstrument") both collapse to ``financialinstrument`` and join.
    **Both implementations must change together** — the whole point is that the two
    sides produce the same key, so a fix applied to one alone breaks the join it
    exists to make.

    Keeps letters, digits, and combining marks attached to those base
    characters in every script. Orphan marks are discarded so removed symbols
    cannot leave identical mark-only keys. The previous ``[^a-zA-Z0-9]`` filter
    judged by ASCII range, which broke the join in two ways:

    - A class label in any non-Latin script keyed to ``""``. Since the key is used
      for dictionary lookup and equality, every such label collided on one bucket
      — so grounding results attached to the wrong class rather than simply
      failing to attach.
    - A Latin label lost the accented letter itself rather than its case
      (``HÖHE`` → ``hhe``, ``ÓRGÃO`` → ``rgo``). Both sides mangled it identically,
      so the join still worked; the harm is that two labels differing only in an
      accent became one key.

    Canonical composition runs before filtering so canonically equivalent input
    forms produce one key. Marks that do not compose — such as Devanagari and Thai
    vowel signs — remain part of the key instead of collapsing distinct words.
    Recomposition after filtering also makes separators between Hangul Jamo a
    one-pass operation rather than changing the key on a second normalization.

    Simple one-code-point case folding is shared with the browser contract. It
    unifies ordinary case variants such as Greek final sigma while rejecting
    expansions: ``Straße`` stays distinct from ``STRASSE`` in both runtimes.
    """
    normalized = unicodedata.normalize("NFC", simple_case_identity(width_only_nfkc(name)))
    filtered = filter_letters_marks_numbers(normalized, keep_symbols=True)
    return unicodedata.normalize("NFC", filtered)


def _local_name(uri: str) -> str:
    """Strictly percent-decoded local name of an IRI."""
    local_name = uri.split("#")[-1].split("/")[-1] or uri
    return strict_percent_decode(local_name)


@dataclass
class ClassGroundingInput:
    """One induced class to ground against loaded ontologies.

    Attributes:
        class_iri: The minted ``owl:Class`` IRI (from the normalizer).
        label: The class's human label (CamelCase or spaced form). Used as
            the ``class_label`` the reranker/exact-matcher compares.
        description: The generated ``rdfs:comment`` description, or ``""``.
        vector: The class embedding (reused from Stage 4 so grounding does
            not re-embed). May be empty — grounding is skipped for the class
            when there is no usable vector.
    """

    class_iri: str
    label: str
    description: str
    vector: list[float]


def ground_classes(
    inputs: list[ClassGroundingInput],
    *,
    grounding_service: GroundingService | None,
    ontology_ids: list[str] | None,
    model_id: str,
    grounding_mode: str = "ENHANCED",
    confidence_threshold: float = 0.80,
    rerank_max_tokens: int = 1000,
    uri_prefix: str = "",
) -> tuple[Graph, int, list[ConceptMatch]]:
    """Ground each induced class against the loaded foundational ontologies.

    Args:
        inputs: The induced classes (IRI + label + description + vector).
        grounding_service: The shared :class:`GroundingService`. ``None``
            disables grounding entirely (returns an empty graph).
        ontology_ids: Loaded foundational ontology IDs to ground against.
            Empty / ``None`` disables grounding (returns an empty graph).
        model_id: The embedding model id the class vectors were produced
            with — passed through to ``ground_class`` for recall scoring.
        grounding_mode: ``ENHANCED`` (embedding recall + LLM rerank) or
            ``STANDARD`` (embedding-only). ``NONE`` also disables.
        confidence_threshold: Passed through to the grounding service.
        rerank_max_tokens: Output-token cap for the ENHANCED-mode LLM rerank,
            passed through to ``ground_class``. Default 1000.
        uri_prefix: The proposal namespace prefix, used to mint the
            ``matchConfidence`` annotation-property IRI (mirrors the
            structured builder). When empty, ``matchConfidence`` is omitted.

    Returns:
        A ``(graph, grounded_count, matches)`` tuple.

        - ``graph`` holds only the grounding axioms (empty when disabled or
          nothing matched).
        - ``grounded_count`` is the number of classes that received an
          exact/close/related match.
        - ``matches`` is the per-class :class:`ConceptMatch` list — ONE entry
          per class that produced recall candidates, whether or not the
          reranker chose one. ``source_table`` is the class label (the review
          UI resolves a class to its match by normalized-name key, and R2RML
          is absent for unstructured). These are persisted as the proposal's
          ``metadata.matches`` (→ ``matches.json``) so the review UI shows the
          candidate count and powers the manual grounding-OVERRIDE editor —
          exactly as for structured proposals. Empty when grounding is
          disabled.
    """
    g = Graph()
    matches: list[ConceptMatch] = []

    if grounding_service is None or not ontology_ids or grounding_mode == "NONE":
        log.info(
            "class grounding skipped: service=%s ontology_ids=%s mode=%s",
            "set" if grounding_service else None,
            bool(ontology_ids),
            grounding_mode,
        )
        return g, 0, matches

    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind("skos", SKOS)

    # matchConfidence annotation property — mirrors the structured builder so
    # the two strategies emit grounding provenance identically. Skipped when
    # no uri_prefix is available (nothing to hang the property IRI on).
    match_confidence: URIRef | None = None
    if uri_prefix:
        ns = Namespace(uri_prefix)
        match_confidence = ns["matchConfidence"]
        g.add((match_confidence, RDF.type, OWL.AnnotationProperty))

    grounded = 0
    for item in inputs:
        if not item.class_iri or not item.vector:
            continue
        try:
            result = grounding_service.ground_class(
                class_label=item.label,
                class_description=item.description,
                concept_vector=item.vector,
                model_id=model_id,
                confidence_threshold=confidence_threshold,
                rerank_max_tokens=rerank_max_tokens,
                grounding_mode=grounding_mode,
                ontology_ids=ontology_ids,
                # Don't let a class ground to ITSELF: incremental induction grounds
                # against the namespace's own accepted ontologies, which can contain
                # this very class (same IRI). Exclude it from recall.
                exclude_entity_uri=item.class_iri,
            )
        except GroundingRerankError:
            # NOT an abstention: an LLM rerank infrastructure failure (Bedrock
            # error after retries, truncated/blocked stopReason, or unparseable
            # output). Re-raise so the induction job fails loud instead of
            # silently leaving every class novel — the enhanced-grounding defect
            # (issue #59). The broad handler below would otherwise swallow it,
            # which is exactly the silent all-novel degradation the documented
            # fail-loud guarantee forbids. Must precede the broad except.
            raise
        except GroundingLookupError:
            # The lexical metadata listing failed or exceeded a bound, so the
            # candidate set is incomplete. Leaving the class novel here would
            # drop a grounding the dense recall may already have found, so fail
            # the job, matching the structured path. Must precede the broad except.
            raise
        except Exception:
            # Fail-open for a genuine per-class fault (e.g. a bad vector or a
            # programming bug): one class failing to ground must not abort the
            # run. A rerank INFRASTRUCTURE failure is handled above, not here.
            log.exception("class grounding failed for %r — leaving novel", item.label)
            continue

        # Record a ConceptMatch for every class that had recall candidates,
        # even when the reranker abstained (match_type=="novel"): the review
        # UI's grounding-override editor lets a steward manually pick from
        # these candidates, so they must be persisted regardless of the
        # automatic decision. source_table = the class label so the UI's
        # normalized-name resolver joins it to the induced class.
        if result.candidates:
            matches.append(grounding_service.to_concept_match(result))

        chosen = result.chosen
        if chosen is None or not chosen.entity_uri:
            continue

        added = _emit_grounding_axioms(
            g,
            class_uri=URIRef(item.class_iri),
            match_type=result.match_type,
            target=URIRef(chosen.entity_uri),
            confidence=result.confidence,
            match_confidence_prop=match_confidence,
            relationship=result.relationship,
        )
        if added:
            grounded += 1
        # (novel with candidates: no link asserted, but the ConceptMatch was
        # still recorded above for the override editor.)

    log.info(
        "class grounding complete: %d/%d classes grounded against %d ontology(ies); %d with candidates",
        grounded,
        len(inputs),
        len(ontology_ids),
        len(matches),
    )
    return g, grounded, matches


# ── Grounding-axiom emission (shared by initial induction + override save) ──

# The complete set of class→foundational grounding predicates this module
# emits. Used both to WRITE axioms and to REMOVE the prior ones before a
# steward's override re-writes them (so an override is a clean replace, never
# an append that leaves a stale mapping behind).
# Every class→foundational grounding predicate this module may emit. Used by
# apply_overrides_to_turtle to strip prior grounding before re-emitting. MUST
# include every SKOS relationship _emit_grounding_axioms can produce (incl.
# broad/narrowMatch) or an override could leave a stale link behind.
_GROUNDING_PREDICATES = (
    RDFS.subClassOf,
    SKOS.exactMatch,
    SKOS.closeMatch,
    SKOS.broadMatch,
    SKOS.narrowMatch,
    SKOS.relatedMatch,
)

# The SKOS predicate to emit for each reranker-chosen relationship, and whether
# that relationship implies a forward rdfs:subClassOf (subsumption: the induced
# class IS-A / is narrower-or-equal-to the foundational concept). narrowMatch
# means the foundational concept is NARROWER than our class, so a forward
# subClassOf would be BACKWARDS — we assert only the skos link. relatedMatch is
# associative, not subsumption.
_RELATIONSHIP_TO_SKOS: dict[str, URIRef] = {
    "exactMatch": SKOS.exactMatch,
    "closeMatch": SKOS.closeMatch,
    "broadMatch": SKOS.broadMatch,
    "narrowMatch": SKOS.narrowMatch,
    "relatedMatch": SKOS.relatedMatch,
}
_SUBCLASSOF_RELATIONSHIPS = frozenset({"exactMatch", "closeMatch", "broadMatch"})


def _is_foundational_target(target_uri: str, uri_prefix: str) -> bool:
    """Return True when ``target_uri`` is a foundational grounding target, not an induced class.

    Grounding always points at a foundational ontology IRI, which lives OUTSIDE
    the induced namespace prefix; within-run dedupe links (skos:closeMatch to a
    sibling induced class) and any induced↔induced subClassOf point INSIDE it.
    When ``uri_prefix`` is empty we cannot distinguish, so treat every target as
    foundational (preserving the pre-fix blanket-removal behavior).
    """
    if not uri_prefix:
        return True
    return not target_uri.startswith(uri_prefix)


def _emit_grounding_axioms(
    g: Graph,
    *,
    class_uri: URIRef,
    match_type: str,
    target: URIRef,
    confidence: float | None,
    match_confidence_prop: URIRef | None,
    relationship: str | None = None,
) -> bool:
    """Emit the grounding axioms for one class→foundational match into ``g``.

    ``match_type`` decides ACCEPTANCE (exact / high_confidence / ambiguous are
    accepted; anything else is novel → nothing emitted). The reranker-chosen
    ``relationship`` (exactMatch / closeMatch / broadMatch / narrowMatch /
    relatedMatch) selects the SKOS predicate; a forward ``rdfs:subClassOf`` is
    asserted only for subsumption-implying relationships
    (:data:`_SUBCLASSOF_RELATIONSHIPS`) — never for narrowMatch (the foundational
    concept is narrower than ours, so subClassOf would point the wrong way) or
    relatedMatch (associative, not hierarchical).

    When ``relationship`` is absent (e.g. STANDARD mode, older records), falls
    back to the prior match_type→predicate mapping (exact→exactMatch,
    high_confidence→closeMatch, ambiguous→relatedMatch) so behavior is unchanged.

    Returns True when a grounding link was asserted (i.e. the class counts as
    grounded), False for novel.
    """
    # #2 defensive guard: never ground a class to ITSELF, even if recall
    # exclusion was bypassed (belt-and-suspenders to the exclude_entity_uri
    # filter in GroundingService).
    if str(class_uri).rstrip("/") == str(target).rstrip("/"):
        return False

    if match_type not in ("exact", "high_confidence", "ambiguous"):
        return False

    # Resolve the SKOS predicate from the reranker relationship, falling back to
    # the legacy match_type mapping when no relationship is available.
    skos_pred = _RELATIONSHIP_TO_SKOS.get(relationship or "")
    if skos_pred is None:
        if match_type == "exact":
            relationship, skos_pred = "exactMatch", SKOS.exactMatch
        elif match_type == "high_confidence":
            relationship, skos_pred = "closeMatch", SKOS.closeMatch
        else:  # ambiguous
            relationship, skos_pred = "relatedMatch", SKOS.relatedMatch

    g.add((class_uri, skos_pred, target))
    if relationship in _SUBCLASSOF_RELATIONSHIPS:
        g.add((class_uri, RDFS.subClassOf, target))
    if match_confidence_prop is not None and confidence is not None:
        g.add((class_uri, match_confidence_prop, Literal(confidence, datatype=XSD.float)))
    return True


def apply_overrides_to_turtle(
    turtle: str,
    matches: list[ConceptMatch],
    *,
    uri_prefix: str = "",
) -> tuple[str, int]:
    """Re-apply grounding decisions onto an ALREADY-INDUCED unstructured Turtle.

    This is the unstructured analogue of the structured override rebuild
    (``proposals._apply_grounding_overrides`` →
    ``TableToOntologyStrategy._build_proposal_ontology``). The structured path
    rebuilds the whole ontology from re-fetched DB catalog tables; unstructured
    proposals have no tables, so instead we surgically rewrite ONLY the
    class→foundational grounding axioms on the existing induced graph, leaving
    every other triple (class declarations, properties, concepts, within-run
    dedupe closeMatch links) untouched.

    For each match, the target class is located by ``rdfs:label`` ==
    ``match.source_table`` (the label carried in ``source_table``; see
    ``ground_classes``). Its existing grounding predicates
    (``_GROUNDING_PREDICATES`` + ``matchConfidence``) are removed and re-emitted
    from the (already-patched) match — so an override is a clean replace and a
    "mark novel" simply drops the link.

    Args:
        turtle: The stored induced-ontology Turtle.
        matches: Only the patched ConceptMatch entries selected in this save,
            keyed by ``source_table`` == class label. Untouched historical
            matches are deliberately excluded so a class removed by a prior
            direct Turtle edit cannot block an unrelated grounding change.
        uri_prefix: Proposal namespace prefix, for the ``matchConfidence``
            annotation-property IRI. When empty, matchConfidence is omitted.

    Returns:
        ``(new_turtle, grounded_count)`` — the re-serialized Turtle and the
        total number of classes in the final graph that carry a grounding link.

    Raises:
        ValueError: If a match cannot be associated with exactly one class. The
            caller must abort all persistence so match metadata and RDF cannot
            diverge.
    """
    g = Graph()
    g.parse(data=turtle, format="turtle")

    match_confidence_prop: URIRef | None = None
    if uri_prefix:
        match_confidence_prop = Namespace(uri_prefix)["matchConfidence"]
        g.add((match_confidence_prop, RDF.type, OWL.AnnotationProperty))

    # Index both exact names and the legacy normalized join. Exact label/local
    # identity wins; the lossy key is only a fallback for transforms such as
    # ``Financial Instrument`` → ``FinancialInstrument`` and must be unique.
    exact_name_to_iris: dict[str, set[URIRef]] = {}
    key_to_iris: dict[str, set[URIRef]] = {}
    for cls in g.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        names = [str(label) for label in g.objects(cls, RDFS.label)]
        names.append(_local_name(str(cls)))
        for name in names:
            exact_name_to_iris.setdefault(name, set()).add(cls)
            key = _normalize_name_key(name)
            if key:
                key_to_iris.setdefault(key, set()).add(cls)

    for m in matches:
        if m.source_column:  # class-level matches only
            continue

        exact_uris = exact_name_to_iris.get(m.source_table, set())
        if len(exact_uris) == 1:
            class_uri = next(iter(exact_uris))
        elif len(exact_uris) > 1:
            raise ValueError(f"class label {m.source_table!r} matches multiple classes exactly")
        else:
            match_key = _normalize_name_key(m.source_table)
            if not match_key:
                raise ValueError(f"class label {m.source_table!r} has an empty normalized key")
            class_uris = key_to_iris.get(match_key, set())
            if not class_uris:
                raise ValueError(f"no class matching {m.source_table!r} in ontology Turtle")
            if len(class_uris) > 1:
                raise ValueError(f"normalized class label {m.source_table!r} matches {len(class_uris)} classes")
            class_uri = next(iter(class_uris))

        # Clean replace: drop the prior grounding axioms for this class first —
        # but ONLY the edges pointing at a FOUNDATIONAL target (outside the
        # induced namespace prefix). A blanket ``g.remove((class, pred, None))``
        # would also delete WITHIN-namespace links to sibling induced classes —
        # notably the Rule-1 within-run ``skos:closeMatch`` dedupe links (and any
        # induced-to-induced ``rdfs:subClassOf``) — which are NOT grounding and
        # must survive an override (the docstring promises exactly this).
        for pred in _GROUNDING_PREDICATES:
            for obj in list(g.objects(class_uri, pred)):
                if _is_foundational_target(str(obj), uri_prefix):
                    g.remove((class_uri, pred, obj))
        if match_confidence_prop is not None:
            g.remove((class_uri, match_confidence_prop, None))

        if m.matched_class_uri and m.match_type != "novel":
            _emit_grounding_axioms(
                g,
                class_uri=class_uri,
                match_type=m.match_type,
                target=URIRef(m.matched_class_uri),
                confidence=m.similarity,
                match_confidence_prop=match_confidence_prop,
                relationship=getattr(m, "relationship", None),
            )

    # ``matches`` may contain only the changed subset, so count grounding from
    # the final graph rather than from the number of entries processed above.
    # Every accepted grounding emitted by this module carries exactly one SKOS
    # relationship; using those links avoids counting an ordinary foundational
    # rdfs:subClassOf axiom as a grounding choice.
    grounding_relationships = _GROUNDING_PREDICATES[1:]
    grounded_classes: set[URIRef] = set()
    for predicate in grounding_relationships:
        for subject, target in g.subject_objects(predicate):
            if (
                isinstance(subject, URIRef)
                and isinstance(target, URIRef)
                and _is_foundational_target(str(target), uri_prefix)
            ):
                grounded_classes.add(subject)

    return g.serialize(format="turtle"), len(grounded_classes)
