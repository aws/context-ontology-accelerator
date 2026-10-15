# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Builder for type-level (T-Box) class schema fingerprints.

Takes the inter-class relationship aggregates returned by
:meth:`~coa_ontology.inducer.unstructured.stores.protocol.LexicalGraphStore.get_class_relations`
(or the equivalent ``ClassSchemaResult.relations`` from
``get_graph_schema``) and projects them onto a per-class
:class:`~coa_ontology.inducer.unstructured.services.description_generator.ClassSchemaContext`.

This is the bridge between raw __SYS_RELATION__ aggregates and the
schema-mode description generator.

The output is **deterministic and instance-free**: only predicate types
and the classes they connect appear. No entity surface forms, no
instance-specific values.

Usage::

    from coa_ontology.inducer.unstructured.services.schema_context_builder import (
        build_class_schema_contexts,
    )

    schema = store.get_graph_schema()
    schemas = build_class_schema_contexts(
        schema.relations,
        class_labels=["Company", "Person"],
    )
    # schemas: {"Company": ClassSchemaContext(...), "Person": ClassSchemaContext(...)}
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from coa_ontology.inducer.unstructured.services.description_generator import (
    ClassSchemaContext,
    SchemaEdge,
)
from coa_ontology.inducer.unstructured.services.normalizers import (
    normalize_predicate,
)

if TYPE_CHECKING:
    from coa_ontology.inducer.unstructured.stores.protocol import (
        RelationRecord,
    )

log = logging.getLogger(__name__)


def _coalesce_to_class_label(
    raw_class_value: str,
    class_label_to_canonical: dict[str, str],
) -> str | None:
    """Map a __SYS_RELATION__ class value to a caller-canonical class label.

    The graph stores class names in CamelCase (e.g. ``FinancialInstrument``)
    while the description generator and the integ test work with
    space-separated forms (e.g. ``Financial Instrument``). Callers supply
    a label-to-canonical mapping; this helper returns the canonical form
    or ``None`` if the value isn't in the caller's class set.

    The mapping is constructed by the caller: typically::

        from coa_ontology.inducer.unstructured.services.sampler import (
            _camel_to_spaced,
        )
        mapping = {raw: _camel_to_spaced(raw) for raw in raw_class_values}
    """
    return class_label_to_canonical.get(raw_class_value)


def build_class_schema_contexts(
    relations: list[RelationRecord],
    class_labels: list[str],
    *,
    class_label_to_canonical: dict[str, str] | None = None,
    namespace_language: str = "en",
) -> dict[str, ClassSchemaContext]:
    """Assemble per-class T-Box schema fingerprints from SYS_RELATION aggregates.

    For each class in ``class_labels``:

    - Walk all ``relations`` whose source OR target matches the class
      (after canonicalization, see ``class_label_to_canonical``).
    - Normalize each predicate via
      :func:`~coa_ontology.inducer.unstructured.services.normalizers.normalize_predicate`
      so casing/spacing variants merge.
    - Aggregate counts per ``(predicate, other_class)`` pair, separately
      for outgoing (this class is source) and incoming (this class is
      target) directions.
    - Sort each direction's edges by frequency descending, ties broken
      lexicographically by ``(predicate, other_class)``.

    Edges referencing a class NOT in ``class_labels`` are kept — the
    description generator wants the full graph context, not just the
    classes under test. The ``other_class`` field carries whichever raw
    or canonicalized class value the SYS_RELATION node provided.

    Args:
        relations: List of ``RelationRecord`` from
            ``LexicalGraphStore.get_class_relations()``.
        class_labels: The classes to build contexts for (in the form the
            caller wants returned, e.g. ``"Financial Instrument"``).
        class_label_to_canonical: Optional mapping from RAW class values
            (as they appear in ``RelationRecord.source_class`` /
            ``target_class``) to the canonical labels in ``class_labels``.
            If omitted, raw values are matched as-is — appropriate when
            the graph already stores classes in the same form as
            ``class_labels``.
        namespace_language: Canonical namespace language, forwarded to
            ``normalize_predicate``.

    Returns:
        Mapping from each entry in ``class_labels`` to its
        :class:`ClassSchemaContext`. Classes with zero matching edges
        appear with empty ``outgoing`` and ``incoming`` lists.
    """
    canonical_set = set(class_labels)

    # Build a fast lookup: raw_class_value → canonical_class_label.
    if class_label_to_canonical is None:
        class_label_to_canonical = {label: label for label in canonical_set}
    else:
        # Defensive copy; we don't want to mutate the caller's dict and
        # we add identity entries for the canonical names themselves so
        # downstream code can do a single lookup whether the input was
        # raw or already canonical.
        class_label_to_canonical = dict(class_label_to_canonical)
        for label in canonical_set:
            class_label_to_canonical.setdefault(label, label)

    # Per-direction aggregator keyed by (canonical_class, predicate, other_class).
    out_agg: dict[tuple[str, str, str], int] = {}
    in_agg: dict[tuple[str, str, str], int] = {}

    for rel in relations:
        predicate = normalize_predicate(rel.predicate)
        if not predicate:
            continue

        src_canonical = _coalesce_to_class_label(rel.source_class, class_label_to_canonical)
        tgt_canonical = _coalesce_to_class_label(rel.target_class, class_label_to_canonical)

        # Outgoing: this class is the source.
        if src_canonical in canonical_set:
            other = tgt_canonical or rel.target_class
            key = (src_canonical, predicate, other)
            out_agg[key] = out_agg.get(key, 0) + rel.count

        # Incoming: this class is the target.
        # Note: if both src and tgt are in the canonical set (a self-loop
        # or sibling), the same edge contributes once to ``out_agg`` (for
        # the source) and once to ``in_agg`` (for the target). That is
        # intentional — both classes' descriptions benefit from seeing
        # the relationship from their own perspective.
        if tgt_canonical in canonical_set:
            other = src_canonical or rel.source_class
            key = (tgt_canonical, predicate, other)
            in_agg[key] = in_agg.get(key, 0) + rel.count

    # Materialize ClassSchemaContext per class with sorted edges.
    result: dict[str, ClassSchemaContext] = {label: ClassSchemaContext(class_label=label) for label in class_labels}

    for (cls, predicate, other), freq in out_agg.items():
        result[cls].outgoing.append(SchemaEdge(predicate=predicate, other_class=other, frequency=freq))
    for (cls, predicate, other), freq in in_agg.items():
        result[cls].incoming.append(SchemaEdge(predicate=predicate, other_class=other, frequency=freq))

    # Sort by (-freq, predicate, other_class) for deterministic output.
    for ctx in result.values():
        ctx.outgoing.sort(key=lambda e: (-e.frequency, e.predicate, e.other_class))
        ctx.incoming.sort(key=lambda e: (-e.frequency, e.predicate, e.other_class))

    log.debug(
        "Built schema contexts for %d classes; outgoing edge counts: %s; incoming edge counts: %s",
        len(result),
        {k: len(v.outgoing) for k, v in result.items()},
        {k: len(v.incoming) for k, v in result.items()},
    )

    return result


def build_class_schema_contexts_from_schema_result(
    schema_result,
    class_labels: list[str],
    *,
    class_label_to_canonical: dict[str, str] | None = None,
    namespace_language: str = "en",
) -> dict[str, ClassSchemaContext]:
    """Convenience wrapper accepting the full ``ClassSchemaResult`` directly.

    Args:
        schema_result: A
            :class:`~coa_ontology.inducer.unstructured.stores.protocol.ClassSchemaResult`
            from ``LexicalGraphStore.get_graph_schema()``.
        class_labels: As :func:`build_class_schema_contexts`.
        class_label_to_canonical: As :func:`build_class_schema_contexts`.
        namespace_language: As :func:`build_class_schema_contexts`.
    """
    return build_class_schema_contexts(
        schema_result.relations,
        class_labels,
        class_label_to_canonical=class_label_to_canonical,
        namespace_language=namespace_language,
    )


# Helper to encourage callers to use ``Iterable`` for the relations
# argument; not part of the public API itself.
def _iter_relations(rels: Iterable[RelationRecord]) -> list[RelationRecord]:
    """Materialise an iterable into a list; used by the test scaffolding."""
    return list(rels)
