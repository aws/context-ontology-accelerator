# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Golden-ontology comparison for induction benchmark scoring.

Compares an induced ontology (Turtle or in-memory rdflib Graph) against
a reference "golden" ontology and reports per-class / per-property match
rates, precision, recall, and F1.

Matching rules:
  - Exact: the induced ontology contains a class/property whose IRI or
    local-name equals the golden one (case-insensitive).
  - Aligned: the induced ontology contains a class whose rdfs:label or
    rdfs:subClassOf chain identifies it with the golden class via a
    fuzzy local-name match (e.g. induced "ClaimInstance" → golden "Claim").
  - Missing: golden class has no corresponding induced class.

This is intentionally conservative — we only score structural overlap,
not semantic equivalence. An "ontology match" in this sense means the
induced pipeline found a concept the golden declares. It does NOT mean
the pipeline's induced definition is semantically correct; that's the
job of downstream validation (HermiT, OntoQA).

Designed to be called from demo.py (Step 4b) and
scripts/live_induction_run.py with the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rdflib import OWL, RDF, RDFS, Graph, URIRef


@dataclass
class ClassMatch:
    """Per-class comparison result."""

    golden_uri: str
    golden_label: str
    induced_uri: str | None = None
    induced_label: str | None = None
    match_type: str = "missing"  # exact | aligned | missing
    grounded_to: str | None = None  # IRI this class was grounded to (foundational match)
    grounded_tier: str | None = None  # "customer" | "foundational" | None


@dataclass
class PropertyMatch:
    """Per-property comparison result."""

    golden_uri: str
    golden_label: str
    kind: str  # 'object' | 'datatype'
    induced_uri: str | None = None
    induced_label: str | None = None
    match_type: str = "missing"  # exact | aligned | missing


@dataclass
class BenchmarkScore:
    """Full scorecard.

    Precision = correct induced entities / total induced entities
    Recall    = matched golden entities / total golden entities
    F1        = harmonic mean
    """

    class_matches: list[ClassMatch] = field(default_factory=list)
    property_matches: list[PropertyMatch] = field(default_factory=list)
    induced_classes_total: int = 0
    induced_properties_total: int = 0
    induced_classes_novel: int = 0  # induced but not in golden
    induced_properties_novel: int = 0

    # Grounding — subset of induced entities that were aligned to a
    # foundational class (not a novel class). Filled in from the induced
    # ontology's rdfs:subClassOf / owl:equivalentClass edges.
    grounded_classes: int = 0
    grounded_properties: int = 0
    # Tier breakdown of grounded classes. Populated when compare_to_golden
    # is called with a tier_classifier.
    grounded_classes_customer: int = 0
    grounded_classes_foundational: int = 0

    @property
    def class_recall(self) -> float:
        """Fraction of golden classes matched (exact or aligned) by the induced ontology."""
        if not self.class_matches:
            return 0.0
        matched = sum(1 for m in self.class_matches if m.match_type != "missing")
        return matched / len(self.class_matches)

    @property
    def class_precision(self) -> float:
        """Fraction of induced classes that matched a golden class."""
        if not self.induced_classes_total:
            return 0.0
        matched = sum(1 for m in self.class_matches if m.match_type != "missing")
        return matched / self.induced_classes_total

    @property
    def class_f1(self) -> float:
        """Harmonic mean of class precision and class recall."""
        p, r = self.class_precision, self.class_recall
        if p + r == 0:
            return 0.0
        return 2 * p * r / (p + r)

    @property
    def property_recall(self) -> float:
        """Fraction of golden properties matched (exact or aligned) by the induced ontology."""
        if not self.property_matches:
            return 0.0
        matched = sum(1 for m in self.property_matches if m.match_type != "missing")
        return matched / len(self.property_matches)

    @property
    def property_precision(self) -> float:
        """Fraction of induced properties that matched a golden property."""
        if not self.induced_properties_total:
            return 0.0
        matched = sum(1 for m in self.property_matches if m.match_type != "missing")
        return matched / self.induced_properties_total

    @property
    def property_f1(self) -> float:
        """Harmonic mean of property precision and property recall."""
        p, r = self.property_precision, self.property_recall
        if p + r == 0:
            return 0.0
        return 2 * p * r / (p + r)

    @property
    def combined_f1(self) -> float:
        """Unweighted mean of class F1 and property F1."""
        return (self.class_f1 + self.property_f1) / 2

    def as_dict(self) -> dict[str, Any]:
        """Serialize the scorecard to a nested dict of class/property counts and metrics.

        Returns:
            A dict with ``class`` and ``property`` sub-dicts (golden/induced totals,
            novel/grounded counts, exact/aligned/missing breakdowns, and rounded
            precision/recall/f1), plus a top-level rounded ``combined_f1``.
        """
        return {
            "class": {
                "total_golden": len(self.class_matches),
                "total_induced": self.induced_classes_total,
                "novel": self.induced_classes_novel,
                "grounded": self.grounded_classes,
                "grounded_customer": self.grounded_classes_customer,
                "grounded_foundational": self.grounded_classes_foundational,
                "exact": sum(1 for m in self.class_matches if m.match_type == "exact"),
                "aligned": sum(1 for m in self.class_matches if m.match_type == "aligned"),
                "missing": sum(1 for m in self.class_matches if m.match_type == "missing"),
                "precision": round(self.class_precision, 3),
                "recall": round(self.class_recall, 3),
                "f1": round(self.class_f1, 3),
            },
            "property": {
                "total_golden": len(self.property_matches),
                "total_induced": self.induced_properties_total,
                "novel": self.induced_properties_novel,
                "grounded": self.grounded_properties,
                "exact": sum(1 for m in self.property_matches if m.match_type == "exact"),
                "aligned": sum(1 for m in self.property_matches if m.match_type == "aligned"),
                "missing": sum(1 for m in self.property_matches if m.match_type == "missing"),
                "precision": round(self.property_precision, 3),
                "recall": round(self.property_recall, 3),
                "f1": round(self.property_f1, 3),
            },
            "combined_f1": round(self.combined_f1, 3),
        }


def _local_name(uri: str) -> str:
    """Return the lower-cased local name (fragment or last path segment) of a URI.

    e.g. ``http://example.com/vocab#Claim`` → ``'claim'`` and
    ``http://example.com/vocab/Claim`` → ``'claim'``.
    """
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s.lower()


def _fuzzy_class_match(golden_local: str, induced_local: str) -> bool:
    """True if induced_local looks like a plausible rename of golden_local.

    Covers common variations the inducer produces:
      - Exact: Claim == Claim
      - Substring: ClaimRecord contains Claim; Claim contained in InsuranceClaim
      - Concatenation: 'PolicyCoverageDetail' vs 'policycoveragedetail' or 'policy_coverage'
    """
    g = golden_local.replace("_", "").replace("-", "").lower()
    i = induced_local.replace("_", "").replace("-", "").lower()
    if g == i:
        return True
    # Require at least a 4-char substring overlap to avoid spurious matches
    return len(g) >= 4 and (g in i or i in g)


def _extract_class_info(g: Graph) -> dict[str, dict]:
    """Return {local_name: {uri, label, grounded_to_iri}}.

    grounded_to_iri is the first foreign ontology URI referenced via
    rdfs:subClassOf or owl:equivalentClass — indicating the induced class
    was aligned to a foundational concept rather than generated novel.
    """
    result: dict[str, dict] = {}
    seen = set()
    for cls in g.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        local = _local_name(str(cls))
        if local in seen:
            continue
        seen.add(local)
        label = next(g.objects(cls, RDFS.label), local)
        # Look for grounding via rdfs:subClassOf or owl:equivalentClass to a
        # class in a different namespace
        cls_ns = str(cls).rsplit("#", 1)[0].rsplit("/", 1)[0]
        grounded_to = None
        for parent in g.objects(cls, RDFS.subClassOf):
            if isinstance(parent, URIRef):
                parent_ns = str(parent).rsplit("#", 1)[0].rsplit("/", 1)[0]
                if parent_ns != cls_ns:
                    grounded_to = str(parent)
                    break
        if not grounded_to:
            for equiv in g.objects(cls, OWL.equivalentClass):
                if isinstance(equiv, URIRef):
                    equiv_ns = str(equiv).rsplit("#", 1)[0].rsplit("/", 1)[0]
                    if equiv_ns != cls_ns:
                        grounded_to = str(equiv)
                        break
        result[local] = {
            "uri": str(cls),
            "label": str(label),
            "grounded_to": grounded_to,
        }
    return result


def _extract_property_info(g: Graph) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (object_props, datatype_props), each {local_name: {uri, label}}."""
    obj_props: dict[str, dict] = {}
    dt_props: dict[str, dict] = {}
    for prop, target in [
        (OWL.ObjectProperty, obj_props),
        (OWL.DatatypeProperty, dt_props),
    ]:
        for p in g.subjects(RDF.type, prop):
            if not isinstance(p, URIRef):
                continue
            local = _local_name(str(p))
            if local in target:
                continue
            label = next(g.objects(p, RDFS.label), local)
            target[local] = {"uri": str(p), "label": str(label)}
    # Also consider rdf:Property (some induction strategies emit these)
    for p in g.subjects(RDF.type, RDF.Property):
        if not isinstance(p, URIRef):
            continue
        local = _local_name(str(p))
        if local in obj_props or local in dt_props:
            continue
        label = next(g.objects(p, RDFS.label), local)
        # No way to tell object vs datatype from plain rdf:Property;
        # put in datatype bucket as a conservative default.
        dt_props[local] = {"uri": str(p), "label": str(label)}
    return obj_props, dt_props


def compare_to_golden(
    induced_turtle: str | Graph,
    golden_turtle: str | Graph,
    tier_classifier=None,
) -> BenchmarkScore:
    """Compare an induced ontology against a golden reference.

    Accepts either rdflib Graphs or Turtle strings.

    ``tier_classifier`` is an optional callable ``(iri: str) -> str`` that
    returns 'customer' or 'foundational' (or '' / None) for each grounded_to
    IRI. When provided, the scorecard reports grounded_classes_customer and
    grounded_classes_foundational separately so callers can show the
    three-tier breakdown (customer / foundational / novel).
    """
    induced_g = (
        induced_turtle if isinstance(induced_turtle, Graph) else Graph().parse(data=induced_turtle, format="turtle")
    )
    golden_g = golden_turtle if isinstance(golden_turtle, Graph) else Graph().parse(data=golden_turtle, format="turtle")

    golden_classes = _extract_class_info(golden_g)
    golden_obj_props, golden_dt_props = _extract_property_info(golden_g)

    induced_classes = _extract_class_info(induced_g)
    induced_obj_props, induced_dt_props = _extract_property_info(induced_g)

    score = BenchmarkScore()
    score.induced_classes_total = len(induced_classes)
    score.induced_properties_total = len(induced_obj_props) + len(induced_dt_props)

    # ── Class matching ───────────────────────────────────────────────
    # Two passes so exact matches claim their induced class before fuzzy
    # matches get a chance (prevents e.g. golden "PolicyHolder" fuzzy-matching
    # induced "Policy" when the latter is already exact-matched to golden "Policy").
    matched_induced_classes: set[str] = set()
    golden_by_local = dict(golden_classes)

    def _classify_tier(grounded_iri: str | None) -> str | None:
        if not grounded_iri or not tier_classifier:
            return None
        try:
            t = tier_classifier(grounded_iri)
        except Exception:  # noqa: BLE001 — classifier is user-supplied
            return None
        return t if t in ("customer", "foundational") else None

    # Pass 1: exact matches
    exactly_matched_golden: set[str] = set()
    for golden_local, gold_info in golden_by_local.items():
        if golden_local in induced_classes:
            ind = induced_classes[golden_local]
            tier = _classify_tier(ind.get("grounded_to"))
            score.class_matches.append(
                ClassMatch(
                    golden_uri=gold_info["uri"],
                    golden_label=gold_info["label"],
                    induced_uri=ind["uri"],
                    induced_label=ind["label"],
                    match_type="exact",
                    grounded_to=ind.get("grounded_to"),
                    grounded_tier=tier,
                )
            )
            matched_induced_classes.add(golden_local)
            exactly_matched_golden.add(golden_local)
            if ind.get("grounded_to"):
                score.grounded_classes += 1
                if tier == "customer":
                    score.grounded_classes_customer += 1
                elif tier == "foundational":
                    score.grounded_classes_foundational += 1

    # Pass 2: fuzzy matches for remaining golden classes, preferring
    # unclaimed induced classes
    for golden_local, gold_info in golden_by_local.items():
        if golden_local in exactly_matched_golden:
            continue
        fuzzy_hit = None
        for ind_local in induced_classes:
            if ind_local in matched_induced_classes:
                continue  # already claimed by another golden
            if _fuzzy_class_match(golden_local, ind_local):
                fuzzy_hit = ind_local
                break
        if fuzzy_hit:
            ind = induced_classes[fuzzy_hit]
            tier = _classify_tier(ind.get("grounded_to"))
            score.class_matches.append(
                ClassMatch(
                    golden_uri=gold_info["uri"],
                    golden_label=gold_info["label"],
                    induced_uri=ind["uri"],
                    induced_label=ind["label"],
                    match_type="aligned",
                    grounded_to=ind.get("grounded_to"),
                    grounded_tier=tier,
                )
            )
            matched_induced_classes.add(fuzzy_hit)
            if ind.get("grounded_to"):
                score.grounded_classes += 1
                if tier == "customer":
                    score.grounded_classes_customer += 1
                elif tier == "foundational":
                    score.grounded_classes_foundational += 1
        else:
            score.class_matches.append(
                ClassMatch(
                    golden_uri=gold_info["uri"],
                    golden_label=gold_info["label"],
                    match_type="missing",
                )
            )

    score.induced_classes_novel = len(induced_classes) - len(matched_induced_classes)

    # ── Property matching ────────────────────────────────────────────
    matched_induced_props: set[str] = set()
    for kind_name, golden_props, induced_props in [
        ("object", golden_obj_props, {**induced_obj_props, **induced_dt_props}),
        ("datatype", golden_dt_props, {**induced_dt_props, **induced_obj_props}),
    ]:
        # We check against the union of induced object+datatype props because
        # induction strategies don't always get the OWL subtype right
        for golden_local, gold_info in golden_props.items():
            if golden_local in induced_props:
                ind = induced_props[golden_local]
                score.property_matches.append(
                    PropertyMatch(
                        golden_uri=gold_info["uri"],
                        golden_label=gold_info["label"],
                        kind=kind_name,
                        induced_uri=ind["uri"],
                        induced_label=ind["label"],
                        match_type="exact",
                    )
                )
                matched_induced_props.add(golden_local)
                continue
            fuzzy_hit = None
            for ind_local in induced_props:
                if _fuzzy_class_match(golden_local, ind_local):
                    fuzzy_hit = ind_local
                    break
            if fuzzy_hit:
                ind = induced_props[fuzzy_hit]
                score.property_matches.append(
                    PropertyMatch(
                        golden_uri=gold_info["uri"],
                        golden_label=gold_info["label"],
                        kind=kind_name,
                        induced_uri=ind["uri"],
                        induced_label=ind["label"],
                        match_type="aligned",
                    )
                )
                matched_induced_props.add(fuzzy_hit)
            else:
                score.property_matches.append(
                    PropertyMatch(
                        golden_uri=gold_info["uri"],
                        golden_label=gold_info["label"],
                        kind=kind_name,
                        match_type="missing",
                    )
                )

    all_induced_prop_locals = set(induced_obj_props) | set(induced_dt_props)
    score.induced_properties_novel = len(all_induced_prop_locals) - len(matched_induced_props)

    return score


__all__ = ["BenchmarkScore", "ClassMatch", "PropertyMatch", "compare_to_golden"]
