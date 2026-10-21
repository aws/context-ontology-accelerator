# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unstructured ontology induction pipeline orchestrator.

This module defines :class:`UnstructuredInductionPipeline`, the
sequencer that wires the parent-spec services into one runnable job.
The orchestrator emits :class:`InductionJobStatus` stage transitions
via an optional callback BEFORE each stage's work begins, runs each
existing service in turn, merges per-rule rdflib graphs, and returns
a fully-populated :class:`UnstructuredInductionReport`.

The orchestrator is deliberately thin. Every actual induction
behaviour lives in the underlying services; this file just sequences
calls and adapts argument names. See `design.md` § "Orchestrator" for
the reference shape and § "Open Implementation Questions" for the
keyword reconciliations adopted here.

Stage progression (Requirement 2.4 / Requirement 9.1)::

    QUERYING_NEPTUNE → SAMPLING → GENERATING_DESCRIPTIONS →
        RESOLVING_IRIS → APPLYING_RULES → ENTAILMENT → SERIALIZING

The terminal STORING / COMPLETED / FAILED stages are owned by the
worker thread (per design doc). FAILED is also emitted by the
orchestrator's own exception handler before re-raising, so a stage
exception always reaches ``on_status`` even when the worker is not
yet attached.

Usage::

    from coa_ontology.inducer.unstructured.services.pipeline import (
        UnstructuredInductionPipeline,
    )

    pipeline = UnstructuredInductionPipeline()
    report = pipeline.run(config, on_status=callback)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from coa_control_plane_server.models.induction_job_status import (
    InductionJobStatus,
)
from rdflib import OWL, RDF, Graph
from rdflib.namespace import SKOS

from coa_ontology.inducer.unstructured.schemas import (
    UnstructuredInductionConfig,
    UnstructuredInductionReport,
)
from coa_ontology.inducer.unstructured.services.class_grounding import (
    ClassGroundingInput,
    ground_classes,
)
from coa_ontology.inducer.unstructured.services.class_normalizer import (
    InMemoryClassEmbeddingIndex,
    _build_embedding_text,
)
from coa_ontology.inducer.unstructured.services.class_normalizer import (
    normalize as class_normalize,
)
from coa_ontology.inducer.unstructured.services.description_generator import (
    generate_descriptions,
    generate_descriptions_with_schema,
)
from coa_ontology.inducer.unstructured.services.induction_rules import (
    induce_classes,
    induce_concepts,
    induce_datatype_properties,
    induce_individuals,
    induce_object_properties,
)
from coa_ontology.inducer.unstructured.services.sampler import (
    _camel_to_spaced,
)
from coa_ontology.inducer.unstructured.services.sampler import (
    sample as stratified_sample,
)
from coa_ontology.inducer.unstructured.services.schema_context_builder import (
    build_class_schema_contexts_from_schema_result,
)
from coa_ontology.inducer.unstructured.services.serializer import serialize

log = logging.getLogger(__name__)


# v0 limitation strings surfaced to clients via UnstructuredInductionReport.warnings.
# Per Requirement 10.3, every run pre-populates these so consumers know
# which axiom shapes are intentionally absent in v0.
V0_WARNINGS: list[str] = [
    "entailment stage is a pass-through in v0",
    "datatype property output may be sparse — see parent spec Task 12B",
    "class embedding index is rebuilt per run — see parent spec Task 10A",
]


class UnstructuredInductionPipeline:
    """Sequences the unstructured induction services into a runnable job.

    The class has no per-instance state — its single :meth:`run` method
    accepts a fully-built :class:`UnstructuredInductionConfig` and
    returns the report. Construction is parameterless so callers can
    keep a single instance around or instantiate one per job.
    """

    def run(
        self,
        config: UnstructuredInductionConfig,
        on_status: Callable[[InductionJobStatus], None] | None = None,
    ) -> UnstructuredInductionReport:
        """Execute the full unstructured induction pipeline.

        Args:
            config: An already-built :class:`UnstructuredInductionConfig`
                carrying the lexical store, Bedrock embedding client,
                LLM description client, IRI resolver, and per-run
                parameters (sampling, description mode, gate flags).
            on_status: Optional callback invoked with each
                :class:`InductionJobStatus` value BEFORE the matching
                stage starts. The callback is also invoked with
                ``InductionJobStatus.FAILED`` on any stage exception
                before the exception is re-raised.

        Returns:
            A fully-populated :class:`UnstructuredInductionReport` with
            counts derived from the merged graph, the serialized
            Turtle, and the v0 limitation warnings.

        Raises:
            Re-raises any exception raised by an underlying service.
            ``on_status(InductionJobStatus.FAILED)`` is invoked first
            so the worker thread / caller can persist the terminal
            state.
        """
        run_id = str(uuid.uuid4())
        started_at = datetime.now(UTC)
        warnings = list(V0_WARNINGS)

        # entailment_enabled=True is a forward-compat field; v0 logs
        # and records a warning but otherwise behaves identically
        # (Requirement 9.3).
        if config.entailment_enabled:
            log.warning("entailment_enabled=True is not yet implemented; treating as False")
            warnings.append("entailment_enabled=True is not yet implemented; treating as False")

        emit = on_status if on_status is not None else _noop_status

        try:
            # ── Stage 1 — querying_neptune ──────────────────────────
            emit(InductionJobStatus.QUERYING_NEPTUNE)
            schema = config.lexical_store.get_graph_schema()
            log.info(
                "queried lexical graph schema: %d classes, %d relations",
                len(schema.classes),
                len(schema.relations),
            )

            # ── Stage 2 — sampling ──────────────────────────────────
            emit(InductionJobStatus.SAMPLING)
            # The sampler returns a dict keyed by the SPACED entity-class
            # label (e.g. "Financial Instrument"), value = list of
            # entity IDs. This is the canonical key used throughout the
            # rest of the pipeline (description generator, normalizer,
            # induce_classes lookup).
            sampled: dict[str, list[str]] = stratified_sample(
                class_distribution=schema.classes,
                store=config.lexical_store,
                k_min=config.sampling.k_min,
                floor=config.sampling.floor,
            )

            # Resolve sampled IDs back to entity records. We use these
            # records both for the description-prompt label list AND
            # (when include_individuals=True) as the input to Rule 2.
            class_to_entities = _resolve_sampled_entities(
                sampled=sampled,
                store=config.lexical_store,
            )

            # Flatten the sampled entity IDs for the facts query.
            sampled_entity_ids: list[str] = [
                entity.entity_id for records in class_to_entities.values() for entity in records
            ]
            facts = config.lexical_store.get_facts_for_entities(sampled_entity_ids)
            topics = config.lexical_store.get_topics()
            log.info(
                "sampled %d entities across %d classes; loaded %d facts, %d topics",
                len(sampled_entity_ids),
                len(class_to_entities),
                len(facts),
                len(topics),
            )

            # ── Stage 3 — generating_descriptions ───────────────────
            emit(InductionJobStatus.GENERATING_DESCRIPTIONS)
            class_to_entity_labels = _build_class_to_entity_labels(class_to_entities)
            descriptions = _generate_descriptions(
                description_mode=config.description_mode,
                class_to_entity_labels=class_to_entity_labels,
                schema=schema,
                description_llm=config.description_llm,
                namespace_language=config.namespace_language,
            )

            # ── Stage 4 — resolving_iris ────────────────────────────
            #
            # The class normalizer mints one IRI per class via the
            # configured IRIResolver. We rebuild a fresh
            # InMemoryClassEmbeddingIndex per run — the persistent
            # backend (parent-spec Task 10A) is a documented v0
            # limitation.
            emit(InductionJobStatus.RESOLVING_IRIS)
            # TODO(parent-spec-task-10A): replace InMemoryClassEmbeddingIndex
            # with a persistent backend for cross-run convergence.
            # We hold the index instance so its embed_cache (label+description
            # → vector) can be reused for foundational grounding below,
            # avoiding a second embedding pass over the class population.
            class_index = InMemoryClassEmbeddingIndex()
            normalizer_results = class_normalize(
                classes=descriptions,
                embedding_client=config.embedding_client,
                iri_resolver=config.iri_resolver,
                index=class_index,
                source_mode=config.description_mode,
            )

            # Build the (spaced-class-label → owl:Class IRI) map that
            # Rules 2/3/4 expect. Built from the normalizer results
            # rather than re-resolving via the IRI resolver so the
            # shape match (exact / close_match / novel) is preserved.
            class_iris: dict[str, str] = {
                label: result.class_iri for label, result in normalizer_results.items() if result.class_iri
            }

            # ── Stage 5 — applying_rules ────────────────────────────
            emit(InductionJobStatus.APPLYING_RULES)
            graph = Graph()

            # Rule 1 — owl:Class declarations.
            graph += induce_classes(
                class_distribution=schema.classes,
                descriptions=descriptions,
                normalizer_results=normalizer_results,
                iri_resolver=config.iri_resolver,
                floor=config.sampling.floor,
                namespace_language=config.namespace_language,
            )

            # Rule 2 — owl:NamedIndividual A-Box (gated). Rule 2 takes
            # a flat list of EntityRecord, not a per-class dict.
            if config.include_individuals:
                flat_entities = [entity for records in class_to_entities.values() for entity in records]
                graph += induce_individuals(
                    sampled_entities=flat_entities,
                    class_iris=class_iris,
                    iri_resolver=config.iri_resolver,
                    include_individuals=True,
                    namespace_language=config.namespace_language,
                )

            # Rule 3 — owl:ObjectProperty declarations from SPO facts.
            graph += induce_object_properties(
                facts=facts,
                class_iris=class_iris,
                iri_resolver=config.iri_resolver,
                namespace_language=config.namespace_language,
            )

            # Rule 4 — owl:DatatypeProperty declarations from SPC facts.
            # NOTE: dev-graph data gap (parent-spec-task-12B) — datatype
            # property output may be sparse. The warning is surfaced in
            # the report regardless.
            graph += induce_datatype_properties(
                facts=facts,
                class_iris=class_iris,
                iri_resolver=config.iri_resolver,
                namespace_language=config.namespace_language,
            )

            # Rule 5 — skos:Concept declarations from __Topic__ nodes.
            graph += induce_concepts(
                topics=topics,
                iri_resolver=config.iri_resolver,
                namespace_name=config.name,
                namespace_language=config.namespace_language,
                class_iris=class_iris,
            )

            # ── Foundational grounding (optional, additive) ─────────
            #
            # Ground the induced classes against the namespace's LOADED
            # foundational ontologies via the shared GroundingService —
            # the same recall→exact→LLM-rerank the structured path uses.
            # Emits rdfs:subClassOf + skos:exactMatch/closeMatch/relatedMatch
            # (+ matchConfidence) that MERGE into the induced graph. This is
            # additive to Rule 1's within-run class↔class closeMatch dedupe
            # links. No-op (empty graph) when grounding_service is None or
            # grounding_ontology_ids is empty; fail-open per class otherwise.
            grounding_count = 0
            grounding_matches: list = []
            if config.grounding_service is not None and config.grounding_ontology_ids:
                grounding_inputs = _build_grounding_inputs(
                    normalizer_results=normalizer_results,
                    descriptions=descriptions,
                    index=class_index,
                )
                grounding_graph, grounding_count, grounding_matches = ground_classes(
                    grounding_inputs,
                    grounding_service=config.grounding_service,
                    ontology_ids=config.grounding_ontology_ids,
                    model_id=config.embedding_client.model_id,
                    grounding_mode=config.grounding_mode,
                    confidence_threshold=config.confidence_threshold,
                    rerank_max_tokens=config.rerank_max_tokens,
                    uri_prefix=config.ontology_uri_prefix,
                )
                graph += grounding_graph

            # ── Stage 6 — entailment ────────────────────────────────
            #
            # Pass-through in v0 (Requirement 9.2): zero hierarchy
            # axioms emitted regardless of entailment_enabled. The
            # warning is already on the report's `warnings` list when
            # entailment_enabled=True.
            #
            # TODO(parent-spec-task-13): wire the OntoEKG-style entailment LLM here when entailment_enabled=True
            emit(InductionJobStatus.ENTAILMENT)

            # ── Stage 7 — serializing ───────────────────────────────
            emit(InductionJobStatus.SERIALIZING)
            turtle, _augmented = serialize(
                graph=graph,
                namespace_name=config.name,
                namespace_iri=config.ontology_uri_prefix,
                timestamp=datetime.now(UTC),
            )

            # Compute counts from the merged graph (Requirement 1.5).
            tbox_class_count = _count_subjects(graph, OWL.Class)
            object_property_count = _count_subjects(graph, OWL.ObjectProperty)
            datatype_property_count = _count_subjects(graph, OWL.DatatypeProperty)
            concept_count = _count_subjects(graph, SKOS.Concept)
            individual_count = _count_subjects(graph, OWL.NamedIndividual)
            close_match_axiom_count = sum(1 for _ in graph.triples((None, SKOS.closeMatch, None)))

            log.info(
                "induction complete: classes=%d obj_properties=%d "
                "datatype_properties=%d concepts=%d individuals=%d "
                "close_match=%d foundational_grounded=%d",
                tbox_class_count,
                object_property_count,
                datatype_property_count,
                concept_count,
                individual_count,
                close_match_axiom_count,
                grounding_count,
            )

            return UnstructuredInductionReport(
                pipeline_run_id=run_id,
                namespace=config.namespace,
                ontology_id=config.name,
                description_mode=config.description_mode,
                tboxClassCount=tbox_class_count,
                objectPropertyCount=object_property_count,
                datatypePropertyCount=datatype_property_count,
                conceptCount=concept_count,
                individualCount=individual_count,
                closeMatchAxiomCount=close_match_axiom_count,
                foundationalGroundedCount=grounding_count,
                groundingMatches=[m.model_dump() for m in grounding_matches],
                started_at=started_at,
                completed_at=datetime.now(UTC),
                turtle=turtle,
                warnings=warnings,
            )

        except Exception:
            # Per Requirement 1.4: emit FAILED before re-raising so the
            # worker thread / caller can persist terminal state. The
            # original exception (and its traceback) propagates intact.
            emit(InductionJobStatus.FAILED)
            raise


# ── Internal helpers ────────────────────────────────────────────────────


def _noop_status(_status: InductionJobStatus) -> None:
    """Default on_status callback when the caller didn't provide one."""


def _build_grounding_inputs(
    normalizer_results,
    descriptions: dict[str, str],
    index: InMemoryClassEmbeddingIndex,
) -> list[ClassGroundingInput]:
    """Assemble per-class grounding inputs for :func:`ground_classes`.

    Reuses the class embedding vectors already computed during Stage 4
    (``class_normalize`` populated ``index.embed_cache`` keyed by the same
    ``"{label}: {description}"`` text). Only classes with a minted IRI AND a
    cached vector are grounded; anything missing a vector is skipped
    (grounding treats it as novel). ``normalizer_results`` is keyed by the
    spaced class label — the same key used for ``descriptions``.
    """
    # Group by canonical class_iri, not by label. The normalizer can map two
    # distinct spaced labels to the SAME minted class_iri (e.g. a close-match
    # pair that collapsed, or a spaced-vs-CamelCase duplicate). Emitting one
    # ClassGroundingInput per label would then ground the SAME IRI twice, from
    # two independent service calls, and write two potentially CONFLICTING
    # decisions onto it (e.g. subClassOf to two different foundational parents).
    # One input per IRI — first label with a usable vector wins (deterministic
    # by normalizer_results iteration order).
    inputs: list[ClassGroundingInput] = []
    seen_iris: set[str] = set()
    for label, nr in normalizer_results.items():
        if not nr.class_iri or nr.class_iri in seen_iris:
            continue
        description = descriptions.get(label, "")
        vector = index.embed_cache.get(_build_embedding_text(label, description))
        if not vector:
            continue
        seen_iris.add(nr.class_iri)
        inputs.append(
            ClassGroundingInput(
                class_iri=nr.class_iri,
                label=label,
                description=description,
                vector=vector,
            )
        )
    return inputs


def _count_subjects(graph: Graph, type_iri) -> int:
    """Count subjects of ``rdf:type type_iri`` in ``graph``."""
    return sum(1 for _ in graph.subjects(RDF.type, type_iri))


def _resolve_sampled_entities(
    sampled: dict[str, list[str]],
    store,
) -> dict[str, list]:
    """Resolve sampled entity IDs back to ``EntityRecord`` per class.

    The sampler returns IDs only; downstream stages need the surface
    labels (``EntityRecord.value``). We fetch every entity for each
    sampled class once and filter by the sampled-ID set so callers
    can iterate per-class without making N additional graph
    round-trips.

    Args:
        sampled: Mapping from spaced-class-label (e.g. ``"Financial
            Instrument"``) to the list of sampled entity IDs.
        store: A :class:`LexicalGraphStore` used to look up the entity
            records via ``get_entities_by_class``.

    Returns:
        A dict with the same keys as ``sampled``; values are the
        :class:`EntityRecord` instances corresponding to the sampled
        IDs (in store-fetch order, NOT necessarily input order).
        Classes whose sampled IDs failed to resolve are dropped.
    """
    out: dict[str, list] = {}
    for class_label, entity_ids in sampled.items():
        if not entity_ids:
            continue
        # ``get_entities_by_class`` is keyed on the entity-side spaced
        # class value, which is exactly what the sampler returns.
        records = store.get_entities_by_class(
            classification=class_label,
            limit=10_000,
        )
        wanted = set(entity_ids)
        matched = [r for r in records if r.entity_id in wanted]
        if matched:
            out[class_label] = matched
    return out


def _build_class_to_entity_labels(
    class_to_entities: dict[str, list],
) -> dict[str, list[str]]:
    """Project ``EntityRecord`` lists down to surface-label lists.

    The label-only and instance-mode description generators take a
    ``dict[str, list[str]]`` of entity surface labels keyed by class.
    """
    return {class_label: [r.value for r in records] for class_label, records in class_to_entities.items()}


def _generate_descriptions(
    description_mode: str,
    class_to_entity_labels: dict[str, list[str]],
    schema,
    description_llm,
    namespace_language: str = "en",
) -> dict[str, str]:
    """Dispatch to the right description-generator function.

    The schema-context mode requires building the per-class T-Box
    schema fingerprints first (via
    :func:`build_class_schema_contexts_from_schema_result`); the
    label-only / instance modes can use the simpler
    :func:`generate_descriptions` directly.

    The "instance" mode currently shares the label-only path: the
    enriched-context API
    (:func:`generate_descriptions_with_context`) requires
    ``EntityContext`` records with per-entity facts that the v0
    orchestrator does not yet build. This is an acceptable degradation
    — clients explicitly select mode and the report records the
    actual mode used (per Requirement 7.3 the proposal metadata
    surfaces this).
    """
    if description_mode == "schema":
        # Build the per-class schema fingerprint from the
        # ClassSchemaResult. The schema-context builder needs the
        # canonical (spaced) class labels and a mapping from raw
        # CamelCase values to the spaced form so it can match
        # __SYS_RELATION__ source/target classes.
        canonical_class_labels = list(class_to_entity_labels.keys())
        raw_to_canonical = {cls.value: _camel_to_spaced(cls.value) for cls in schema.classes}
        contexts = build_class_schema_contexts_from_schema_result(
            schema,
            class_labels=canonical_class_labels,
            class_label_to_canonical=raw_to_canonical,
            namespace_language=namespace_language,
        )
        return generate_descriptions_with_schema(
            class_schemas=contexts,
            llm_client=description_llm,
            namespace_language=namespace_language,
        )

    # label_only / instance — both share the bare label-list path in
    # v0. The instance enrichment lives behind a separate
    # generate_descriptions_with_context API that requires
    # per-entity facts the orchestrator does not yet assemble.
    return generate_descriptions(
        class_entities=class_to_entity_labels,
        llm_client=description_llm,
        namespace_language=namespace_language,
    )
