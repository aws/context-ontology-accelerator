# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 1 — Automated, block on failure.

Checks:
  - Reasoner consistency (owlready2 + HermiT)
  - No unsatisfiable classes
  - No taxonomy cycles (circular rdfs:subClassOf)
  - Full connectivity (single connected component)
  - SHACL shape validation (pyshacl)
  - Malformed/mis-cased/aliased XSD datatype tokens (surfaced, not auto-fixed)
"""

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from coa_ontology.datatype_canonicalizer import (
    detect_datatype_issues,
    detect_datatype_issues_in_turtle,
)
from coa_ontology.validation.schemas import Severity, ValidationFinding, ValidationTier
from coa_ontology.validation.validators import OntologyValidator

# HermiT enforces the OWL 2 datatype map (https://www.w3.org/TR/owl2-syntax/#Datatype_Maps)
# and throws UnsupportedDatatypeException — aborting the WHOLE reasoning run — on ANY
# XSD-namespaced datatype outside it. Three families of out-of-map tokens can reach the
# reasoner:
#   1. valid-but-excluded types our inducer emits from SQL columns (DATE→xsd:date,
#      TIME→xsd:time, the gregorian/duration family) — only xsd:dateTime/xsd:dateTimeStamp
#      are in the map;
#   2. malformed / mis-cased / aliased tokens a user hand-edited or uploaded
#      (xsd:datetime lowercase-t, xsd:varchar) — these are surfaced for consent-gated
#      repair, but until repaired they still abort the reasoner; and
#   3. genuinely unknown xsd: tokens (xsd:garbagetype).
# We rewrite all three to a safe in-map substitute purely on the reasoner's throwaway copy
# of the graph — the stored ontology keeps its original datatypes (repair is the only path
# that mutates those, with user consent).

# XSD members of the OWL 2 datatype map — HermiT accepts exactly these (plus owl:real/
# owl:rational and rdf:PlainLiteral/rdf:XMLLiteral, which our induced ontologies never emit).
# Any XSD-namespaced datatype NOT in this set makes HermiT abort, so it must be substituted
# on the reasoner copy. Sourced from OWL 2 Structural Spec §4 (verified against the W3C spec).
_OWL2_MAP_XSD: frozenset[URIRef] = frozenset(
    {
        XSD.decimal,
        XSD.integer,
        XSD.nonNegativeInteger,
        XSD.nonPositiveInteger,
        XSD.positiveInteger,
        XSD.negativeInteger,
        XSD.long,
        XSD.int,
        XSD.short,
        XSD.byte,
        XSD.unsignedLong,
        XSD.unsignedInt,
        XSD.unsignedShort,
        XSD.unsignedByte,
        XSD.double,
        XSD.float,
        XSD.string,
        XSD.normalizedString,
        XSD.token,
        XSD.language,
        XSD.Name,
        XSD.NCName,
        XSD.NMTOKEN,
        XSD.boolean,
        XSD.hexBinary,
        XSD.base64Binary,
        XSD.anyURI,
        XSD.dateTime,
        XSD.dateTimeStamp,
    }
)

# Preferred substitutes for valid-but-excluded temporal types: map to xsd:dateTime (the
# map's only date/time member) rather than the generic xsd:string fallback, so temporal
# semantics survive on the reasoner copy where possible.
_OWL2_DATATYPE_SUBSTITUTIONS: dict[URIRef, URIRef] = {
    XSD.date: XSD.dateTime,
    XSD.time: XSD.dateTime,
    XSD.gYear: XSD.string,
    XSD.gYearMonth: XSD.string,
    XSD.gMonth: XSD.string,
    XSD.gMonthDay: XSD.string,
    XSD.gDay: XSD.string,
    XSD.duration: XSD.string,
}

_XSD_NS = str(XSD)


def _reasoner_safe_datatype(dt: URIRef) -> URIRef:
    """Return an OWL 2-map-safe datatype for the reasoner's throwaway copy.

    Non-XSD datatypes (custom / rdf:) are returned unchanged. For XSD-namespaced
    tokens: first recover intent from a malformed/mis-cased/aliased token
    (``xsd:datetime`` → ``xsd:dateTime``, ``xsd:varchar`` → ``xsd:string``) via the
    canonicalizer's oracle; if the result is already in the OWL 2 map, keep it;
    otherwise map the temporal family → ``xsd:dateTime`` and anything else still
    out-of-map (incl. unknown ``xsd:`` tokens) → ``xsd:string``. This is applied
    ONLY to the reasoner copy — never the stored ontology.
    """
    from coa_ontology.datatype_canonicalizer import _canonical_for

    canonical = _canonical_for(dt) or dt
    if not str(canonical).startswith(_XSD_NS):
        return canonical  # non-XSD (custom / rdf:) — leave for HermiT to handle
    if canonical in _OWL2_MAP_XSD:
        return canonical  # already reasoner-safe
    if canonical in _OWL2_DATATYPE_SUBSTITUTIONS:
        return _OWL2_DATATYPE_SUBSTITUTIONS[canonical]
    return XSD.string  # any other out-of-map / unknown xsd: token


class ConsistencyValidator(OntologyValidator):
    """Reasoner consistency check via owlready2 + HermiT."""

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Run HermiT over the ontology and report inconsistency or unsatisfiable classes.

        Strips ``owl:imports`` and substitutes OWL 2-unsupported datatypes on a
        reasoner-only copy of the graph before invoking HermiT.
        """
        findings: list[ValidationFinding] = []
        try:
            import os
            import tempfile

            import owlready2

            # Strip owl:imports before reasoning — the reasoner would try to
            # fetch remote ontologies (e.g. https://schema.org) which may not
            # serve parseable RDF.  The induced ontology's own axioms should be
            # consistent independently of imported ontologies.
            local_g = Graph()
            for s, p, o in graph:
                if p == OWL.imports:
                    continue
                # Substitute any OWL 2-map-excluded datatype so HermiT does not abort
                # with UnsupportedDatatypeException. The datatype appears in two shapes
                # and BOTH must be rewritten:
                #   1. as a datatype URIRef — e.g. `rdfs:range xsd:date` (or a malformed
                #      `xsd:datetime`), which clausifies to a DataAllValuesFrom the
                #      reasoner rejects; and
                #   2. as a typed literal — e.g. `"2020-01-01"^^xsd:date` in an
                #      owl:hasValue / DataOneOf / facet, whose datatype HermiT also loads.
                # ``_reasoner_safe_datatype`` covers valid-but-excluded, malformed/aliased,
                # AND unknown xsd: tokens — so an un-repaired malformed token surfaces as a
                # clean MALFORMED_DATATYPE_TOKENS warning instead of a HermiT stack trace.
                if isinstance(o, URIRef) and str(o).startswith(_XSD_NS):
                    o = _reasoner_safe_datatype(o)
                elif isinstance(o, Literal) and o.datatype is not None and str(o.datatype).startswith(_XSD_NS):
                    safe = _reasoner_safe_datatype(o.datatype)
                    if safe != o.datatype:
                        o = Literal(str(o), datatype=safe)
                local_g.add((s, p, o))

            with tempfile.NamedTemporaryFile(suffix=".rdf", delete=False, mode="wb") as f:
                f.write(local_g.serialize(format="xml", encoding="utf-8"))
                tmp_path = f.name

            try:
                onto = owlready2.get_ontology(f"file://{tmp_path}").load()
                with onto:
                    owlready2.sync_reasoner_hermit(infer_property_values=False)

                unsatisfiable = list(onto.inconsistent_classes())
                if unsatisfiable:
                    for cls in unsatisfiable:
                        findings.append(
                            ValidationFinding(
                                validator="consistency",
                                tier=ValidationTier.tier1_blocking,
                                severity=Severity.error,
                                code="UNSATISFIABLE_CLASS",
                                message="Class is unsatisfiable (contradictory axioms)",
                                subject=str(cls.iri),
                            )
                        )
                else:
                    findings.append(
                        ValidationFinding(
                            validator="consistency",
                            tier=ValidationTier.tier1_blocking,
                            severity=Severity.info,
                            code="CONSISTENT",
                            message="HermiT reasoner: ontology is logically consistent, no unsatisfiable classes",
                        )
                    )
            finally:
                os.unlink(tmp_path)

        except Exception as e:
            findings.append(
                ValidationFinding(
                    validator="consistency",
                    tier=ValidationTier.tier1_blocking,
                    severity=Severity.error,
                    code="REASONER_ERROR",
                    message=f"Reasoner failed: {e}",
                )
            )
        return findings


class TaxonomyCycleValidator(OntologyValidator):
    """Detect circular rdfs:subClassOf chains."""

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Walk the subclass graph via DFS and report the first taxonomy cycle found."""
        findings: list[ValidationFinding] = []
        # Build adjacency: child → parents
        children: dict[str, set[str]] = {}
        for s, _, o in graph.triples((None, RDFS.subClassOf, None)):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                children.setdefault(str(s), set()).add(str(o))

        # DFS cycle detection
        visited, in_stack = set(), set()

        def dfs(node):
            if node in in_stack:
                return node
            if node in visited:
                return None
            visited.add(node)
            in_stack.add(node)
            for parent in children.get(node, []):
                cycle_node = dfs(parent)
                if cycle_node:
                    return cycle_node
            in_stack.discard(node)
            return None

        for node in children:
            cycle = dfs(node)
            if cycle:
                findings.append(
                    ValidationFinding(
                        validator="taxonomy_cycle",
                        tier=ValidationTier.tier1_blocking,
                        severity=Severity.error,
                        code="TAXONOMY_CYCLE",
                        message="Circular rdfs:subClassOf chain detected",
                        subject=cycle,
                    )
                )
                break  # one finding is enough

        if not findings:
            findings.append(
                ValidationFinding(
                    validator="taxonomy_cycle",
                    tier=ValidationTier.tier1_blocking,
                    severity=Severity.info,
                    code="NO_CYCLES",
                    message="No circular rdfs:subClassOf chains detected",
                )
            )

        return findings


class ConnectivityValidator(OntologyValidator):
    """Check that the class graph is a single connected component."""

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Partition classes into connected components and report orphans/isolated clusters.

        Builds an undirected graph from ``rdfs:subClassOf`` and object-property
        domain/range edges, then enumerates every connected component.
        """
        findings: list[ValidationFinding] = []
        classes = set()
        edges: dict[str, set[str]] = {}

        for s in graph.subjects(RDF.type, OWL.Class):
            if isinstance(s, URIRef):
                classes.add(str(s))
                edges.setdefault(str(s), set())

        # Add undirected edges for subClassOf, domain/range
        for s, _, o in graph.triples((None, RDFS.subClassOf, None)):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                edges.setdefault(str(s), set()).add(str(o))
                edges.setdefault(str(o), set()).add(str(s))

        for prop_type in (OWL.ObjectProperty,):
            for prop in graph.subjects(RDF.type, prop_type):
                domains = [str(d) for d in graph.objects(prop, RDFS.domain) if isinstance(d, URIRef)]
                ranges = [str(r) for r in graph.objects(prop, RDFS.range) if isinstance(r, URIRef)]
                for d in domains:
                    for r in ranges:
                        if d in classes and r in classes:
                            edges.setdefault(d, set()).add(r)
                            edges.setdefault(r, set()).add(d)

        if not classes:
            return findings

        # All-components BFS: enumerate every connected component so the user
        # can see the partition shape (1 main cluster + N orphans, vs. many
        # small clusters) instead of one arbitrary "first" class's neighbours.
        unvisited = set(classes)
        components: list[set[str]] = []
        while unvisited:
            start = next(iter(unvisited))
            component: set[str] = set()
            queue = [start]
            while queue:
                node = queue.pop(0)
                if node in component:
                    continue
                component.add(node)
                queue.extend(edges.get(node, set()) - component)
            components.append(component)
            unvisited -= component

        components.sort(key=len, reverse=True)

        if len(components) <= 1:
            findings.append(
                ValidationFinding(
                    validator="connectivity",
                    tier=ValidationTier.tier1_blocking,
                    severity=Severity.info,
                    code="CONNECTED",
                    message=f"All {len(classes)} classes form a single connected component",
                )
            )
            return findings

        main_size = len(components[0])
        orphan_count = sum(len(c) for c in components[1:])
        component_sizes = [len(c) for c in components]
        # Pick a representative class IRI from the main cluster so the user
        # can tell *what* the main cluster is — without it, "main cluster"
        # is opaque on a 75-class proposal.
        main_cluster_sample = sorted(components[0])[:3]
        findings.append(
            ValidationFinding(
                validator="connectivity",
                tier=ValidationTier.tier1_blocking,
                severity=Severity.info,
                code="CONNECTIVITY_PARTITION",
                message=(
                    f"Main cluster: {main_size} classes connected. "
                    f"{orphan_count} class{'es' if orphan_count != 1 else ''} outside the main cluster "
                    f"in {len(components) - 1} separate component{'s' if len(components) - 1 != 1 else ''}."
                ),
                details={
                    "component_sizes": component_sizes,
                    "main_size": main_size,
                    "orphan_count": orphan_count,
                    "main_cluster_sample": main_cluster_sample,
                },
            )
        )

        for component in components[1:]:
            members_sorted = sorted(component)
            n = len(component)
            if n == 1:
                # A single orphan class — name it directly so reviewers can
                # see exactly which class is unreachable.
                only_iri = members_sorted[0]
                local = only_iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
                msg = f"Orphan class: {local} is not connected to any other class"
            else:
                msg = f"{n} classes form an isolated cluster, disconnected from the main {main_size}-class cluster"
            findings.append(
                ValidationFinding(
                    validator="connectivity",
                    tier=ValidationTier.tier1_blocking,
                    # warning, not error: orphans are common in noisy real-world
                    # schemas and shouldn't block the proposal — surface them so
                    # the user can review without forcing a manual patch.
                    severity=Severity.warning,
                    code="DISCONNECTED_CLASSES",
                    message=msg,
                    details={
                        "members": members_sorted[:25],
                        "size": n,
                    },
                )
            )

        return findings


class SHACLValidator(OntologyValidator):
    """Validate against SHACL shapes."""

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Validate the ontology graph against caller-supplied SHACL shapes via pyshacl."""
        shapes_turtle = kwargs.get("shacl_shapes_turtle")
        if not shapes_turtle:
            return []

        findings: list[ValidationFinding] = []
        try:
            from pyshacl import validate as shacl_validate

            shapes_graph = Graph()
            shapes_graph.parse(data=shapes_turtle, format="turtle")

            conforms, results_graph, results_text = shacl_validate(
                data_graph=graph,
                shacl_graph=shapes_graph,
                inference="none",
                abort_on_first=False,
            )

            if not conforms:
                findings.append(
                    ValidationFinding(
                        validator="shacl",
                        tier=ValidationTier.tier1_blocking,
                        severity=Severity.error,
                        code="SHACL_VIOLATION",
                        message="SHACL shape validation failed",
                        details={"results": results_text[:2000]},
                    )
                )
            else:
                findings.append(
                    ValidationFinding(
                        validator="shacl",
                        tier=ValidationTier.tier1_blocking,
                        severity=Severity.info,
                        code="SHACL_PASS",
                        message="All SHACL shape constraints satisfied",
                    )
                )
        except Exception as e:
            findings.append(
                ValidationFinding(
                    validator="shacl",
                    tier=ValidationTier.tier1_blocking,
                    severity=Severity.error,
                    code="SHACL_ERROR",
                    message=f"SHACL validation error: {e}",
                )
            )
        return findings


class DatatypeTokenValidator(OntologyValidator):
    """Surface malformed / mis-cased / aliased XSD datatype tokens for repair.

    Detects tokens like ``xsd:datetime`` (lowercase ``t``) or SQL aliases
    (``xsd:varchar``) in datatype-property ranges, typed literals, and R2RML
    ``rr:datatype`` declarations — the tokens that reach Ontop as *undefined*
    datatypes and silently break VKG queries. This validator only REPORTS them
    (severity=warning, does not set ``passed=False``); the user chooses to fix
    them via the consent-gated "Repair" action, replacing the previous silent
    write-boundary canonicalization.

    Detection reuses the exact ``_canonical_for`` oracle the repair uses, so a
    flagged token is precisely one repair would rewrite — and valid types
    (incl. ``xsd:date``, which is out of the OWL 2 map but a legitimate XSD
    type) and unknown ``xsd:`` tokens are neither flagged nor rewritten. Emits a
    single ``MALFORMED_DATATYPE_TOKENS`` finding whose ``details`` carries the
    per-element offender list (``subject``/``predicate``/``found``/``canonical``)
    for the UI preview; emits nothing when the ontology is clean.
    """

    def validate(self, graph: Graph, **kwargs) -> list[ValidationFinding]:
        """Scan the ontology (and optional R2RML Turtle) for malformed XSD datatype tokens."""
        offenders = detect_datatype_issues(graph)
        # R2RML rr:datatype tokens are a distinct break path: the accept-time
        # range-alignment step copies rr:datatype into the served ontology range,
        # so a malformed rr:datatype corrupts the VKG copy even when the ontology
        # turtle is clean. Scan it too when the caller provides it.
        r2rml_turtle = kwargs.get("r2rml_turtle")
        offenders.extend(detect_datatype_issues_in_turtle(r2rml_turtle))

        if not offenders:
            return []

        distinct_elements = {o["subject"] for o in offenders}
        distinct_fixes = {(o["found"], o["canonical"]) for o in offenders}
        return [
            ValidationFinding(
                validator="datatype_token",
                tier=ValidationTier.tier1_blocking,
                # warning, not error: the served VKG copy is auto-canonicalized at
                # accept, so a skipped repair can't break queries — this must not
                # fake-'fail' validation. Mirrors DISCONNECTED_CLASSES (tier1 +
                # warning + structured details).
                severity=Severity.warning,
                code="MALFORMED_DATATYPE_TOKENS",
                message=(
                    (
                        f"{len(offenders)} datatype references use non-standard spelling"
                        if len(offenders) != 1
                        else "1 datatype reference uses non-standard spelling"
                    )
                    + f" ({len(distinct_fixes)} distinct fix{'es' if len(distinct_fixes) != 1 else ''}). "
                    "They still work in queries (auto-corrected when served), but repairing "
                    "makes your saved ontology consistent with what gets served."
                ),
                subject=offenders[0]["subject"],
                details={
                    "offenders": offenders,
                    "count": len(offenders),
                    "element_count": len(distinct_elements),
                },
            )
        ]
