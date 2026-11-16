# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 2 — Automated, report as scores.

Computes OntoQA structural metrics and coverage scores.
"""

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from coa_ontology.validation.schemas import Severity, StructuralMetrics, ValidationFinding, ValidationTier
from coa_ontology.validation.validators import OntologyValidator


class StructuralMetricsValidator(OntologyValidator):
    """Compute OntoQA-style structural metrics."""

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Compute OntoQA richness metrics and flag low relationship/attribute richness.

        Derives class/property counts, relationship/attribute/inheritance richness,
        and taxonomy max-depth, storing the metrics under ``_metrics_out`` in
        ``kwargs`` for the report builder.

        Args:
            graph: The ontology graph to measure.
            **kwargs: Mutated to carry computed metrics via ``_metrics_out``.

        Returns:
            Warning findings for low relationship or attribute richness; empty
            when the ontology's metrics are within expected ranges.
        """
        findings = []

        classes = set(s for s in graph.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef))
        obj_props = set(s for s in graph.subjects(RDF.type, OWL.ObjectProperty) if isinstance(s, URIRef))
        dt_props = set(s for s in graph.subjects(RDF.type, OWL.DatatypeProperty) if isinstance(s, URIRef))

        subclass_count = sum(1 for _ in graph.triples((None, RDFS.subClassOf, None)))
        total_relations = len(obj_props) + subclass_count

        class_count = len(classes)
        obj_prop_count = len(obj_props)
        dt_prop_count = len(dt_props)

        rr = len(obj_props) / total_relations if total_relations > 0 else None
        ar = dt_prop_count / class_count if class_count > 0 else None
        ir = subclass_count / class_count if class_count > 0 else None

        # Max depth via BFS on subClassOf
        children_map: dict[str, list[str]] = {}
        for s, _, o in graph.triples((None, RDFS.subClassOf, None)):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                children_map.setdefault(str(o), []).append(str(s))

        roots = [str(c) for c in classes if not any(graph.triples((c, RDFS.subClassOf, None)))]
        max_depth = 0
        for root in roots:
            queue = [(root, 0)]
            while queue:
                node, depth = queue.pop(0)
                max_depth = max(max_depth, depth)
                for child in children_map.get(node, []):
                    queue.append((child, depth + 1))

        metrics = StructuralMetrics(
            class_count=class_count,
            object_property_count=obj_prop_count,
            datatype_property_count=dt_prop_count,
            relationship_richness=round(rr, 3) if rr is not None else None,
            attribute_richness=round(ar, 3) if ar is not None else None,
            inheritance_richness=round(ir, 3) if ir is not None else None,
            max_depth=max_depth,
        )

        # Store metrics in kwargs for the report builder to pick up
        kwargs.setdefault("_metrics_out", {}).update(metrics.model_dump())

        if class_count > 0 and rr is not None and rr < 0.1:
            findings.append(
                ValidationFinding(
                    validator="structural_metrics",
                    tier=ValidationTier.tier2_scoring,
                    severity=Severity.warning,
                    code="LOW_RELATIONSHIP_RICHNESS",
                    message=f"Relationship richness is {rr:.3f} — ontology is mostly taxonomic",
                )
            )

        if class_count > 0 and ar is not None and ar < 0.5:
            findings.append(
                ValidationFinding(
                    validator="structural_metrics",
                    tier=ValidationTier.tier2_scoring,
                    severity=Severity.warning,
                    code="LOW_ATTRIBUTE_RICHNESS",
                    message=f"Attribute richness is {ar:.3f} — classes have few datatype properties",
                )
            )

        return findings
