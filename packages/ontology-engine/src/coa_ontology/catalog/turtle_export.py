# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Compact, grouped Turtle serialization for the ontology download endpoint.

Neptune's GSP GET emits a verbose, prefix-light document where every
subject carries its full URI and triples are interleaved without
grouping by RDF type. This module reformats that document into a
compact view organized by:

    @prefix declarations
    # ── Ontology ──             (owl:Ontology subjects)
    # ── Classes ──              (owl:Class subjects)
    # ── Object properties ──    (owl:ObjectProperty subjects)
    # ── Datatype properties ──  (owl:DatatypeProperty subjects)
    # ── Other ──                everything else (annotations, lone triples)

The transformation is loss-less: subjects that don't fall into any of
the four named categories land in the "Other" section so no triples
are dropped. Anonymous (BNode) targets reachable from any classified
subject are pulled into that subject's sub-graph so embedded
``rdfs:subClassOf [ a owl:Restriction … ]`` constructs round-trip.
"""

from __future__ import annotations

import re

from coa_common.constants import VOCAB_PREFIX, VOCAB_URI
from rdflib import BNode, Graph, Namespace
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, SKOS, XSD

# Workbench-specific vocab namespaces seen in the engine's emitted
# Turtle. Binding short prefixes for these makes the output materially
# more compact.
_WB = Namespace("https://ontology-workbench.local/vocab#")
_NEPTUNE = Namespace("http://aws.amazon.com/neptune/vocab/v01/")
# The engine's own vocabulary (isMapped, fkProvenance, distinctValues,
# subClassProvenance, …). Neptune's GSP output is prefix-light, so this
# namespace arrives unbound; without an explicit binding rdflib auto-mints a
# throwaway prefix on each section sub-graph that the prefix-block rebuild then
# discards, yielding an undeclared prefix and unparseable Turtle. Sourced from
# the shared constant so it tracks any rebrand of the vocab base.
_VOCAB = Namespace(VOCAB_URI)

# Turtle directives that declare a prefix or a base and carry no triples:
# ``@prefix p: <iri> .``, ``@base <iri> .`` and the SPARQL-style ``PREFIX p: <iri>``
# / ``BASE <iri>``. Matched as whole tokens, so a directive and a triple on the
# same line still leave the triple behind.
_DIRECTIVE_RE = re.compile(
    r"@prefix\s+[^\s:]*:\s*<[^>]*>\s*\."
    r"|@base\s*<[^>]*>\s*\."
    r"|(?i:\bprefix)\s+[^\s:]*:\s*<[^>]*>"
    r"|(?i:\bbase)\s*<[^>]*>"
)


def turtle_has_triples(raw_turtle: str) -> bool:
    """Whether a Turtle document holds anything beyond directives and comments.

    Neptune's GSP GET can answer an unknown or empty named graph with a header of
    ``@prefix`` lines and no triples. Stripping the directives and comment lines
    tells that apart without parsing the document a second time (the download
    route parses it once to reformat). It errs toward ``True``: anything left over
    counts as content.
    """
    for line in _DIRECTIVE_RE.sub(" ", raw_turtle).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def format_grouped_turtle(raw_turtle: str) -> str:
    """Reformat a raw Turtle document into a prefix-rich, grouped view.

    Parses the input with rdflib, binds a curated set of well-known
    prefixes (rdf, rdfs, owl, xsd, dct, skos, plus workbench-specific
    bindings) including the ontology's own namespace as the empty
    prefix, and re-serializes the graph in labeled sections.

    Returns the original document unchanged if parsing fails.
    """
    g = Graph(bind_namespaces="none")
    try:
        g.parse(data=raw_turtle, format="turtle")
    except Exception:
        # Fall back to the source text rather than producing nothing if
        # the input isn't valid Turtle (very unlikely for Neptune output).
        return raw_turtle

    # Curated prefix bindings — rdflib doesn't auto-bind anything when
    # constructed with ``bind_namespaces='none'``, so this set plus
    # whatever the source declared is exactly what ends up in the
    # output.
    g.bind("rdf", RDF, override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("owl", OWL, override=True, replace=True)
    g.bind("xsd", XSD, override=True, replace=True)
    g.bind("dct", DCTERMS, override=True, replace=True)
    g.bind("skos", SKOS, override=True, replace=True)
    g.bind("wb", _WB, override=True, replace=True)
    g.bind("neptune", _NEPTUNE, override=True, replace=True)
    g.bind(VOCAB_PREFIX, _VOCAB, override=True, replace=True)

    # Bind the ontology's own namespace as the empty prefix so subjects
    # render as e.g. ``:Claim`` instead of ``<https://…/onto/ds-foo#Claim>``.
    onto_uri = next(iter(g.subjects(RDF.type, OWL.Ontology)), None)
    if onto_uri:
        ns_str = str(onto_uri)
        if not ns_str.endswith("#") and not ns_str.endswith("/"):
            ns_str = ns_str + "#"
        g.bind("", Namespace(ns_str), override=True, replace=True)

    # Categorize subjects.
    onto_subjects = sorted(g.subjects(RDF.type, OWL.Ontology), key=str)
    class_subjects = sorted(g.subjects(RDF.type, OWL.Class), key=str)
    obj_prop_subjects = sorted(g.subjects(RDF.type, OWL.ObjectProperty), key=str)
    data_prop_subjects = sorted(g.subjects(RDF.type, OWL.DatatypeProperty), key=str)
    classified: set = set(onto_subjects) | set(class_subjects) | set(obj_prop_subjects) | set(data_prop_subjects)

    # Anything else with at least one outgoing triple — but skip blank
    # nodes (they're emitted inline as part of their parent subject's
    # sub-graph) and skip subjects already classified above.
    other_subjects = sorted(
        {s for s in g.subjects() if s not in classified and not isinstance(s, BNode)},
        key=str,
    )

    # Structural guard against undeclared prefixes. The curated binds above cover
    # today's vocabularies, but any namespace that reaches serialization unbound
    # (a future predicate, a source that declared nothing) would be auto-assigned
    # a throwaway prefix (ns1, ns2, …) on each section sub-graph — a binding that
    # lives only on that sub-graph and is dropped when the body's @prefix lines
    # are stripped, leaving the prefix block (rebuilt from ``g``) missing it and
    # the output unparseable. Serializing the parent graph once forces rdflib to
    # mint and RECORD those prefixes on ``g`` itself; ``_subgraph_for`` then copies
    # the now-complete map into every sub-graph, so bodies and the prefix block
    # reference the same declared prefixes. The result is discarded.
    g.serialize(format="turtle")

    sections: list[str] = []

    def section(name: str, subjects: list) -> None:
        if not subjects:
            return
        sub = _subgraph_for(g, subjects)
        ttl = sub.serialize(format="turtle")
        body_lines = [ln for ln in ttl.splitlines() if not ln.startswith("@prefix") and not ln.startswith("@base")]
        body = "\n".join(body_lines).strip()
        if body:
            sections.append(f"# ── {name} ──────────────────────\n\n{body}")

    section("Ontology", onto_subjects)
    section("Classes", class_subjects)
    section("Object properties", obj_prop_subjects)
    section("Datatype properties", data_prop_subjects)
    section("Other", other_subjects)

    # Prefix block: render once, sorted, deterministic.
    base_line: str | None = None
    other_prefix_lines: list[str] = []
    for prefix, ns in g.namespaces():
        if prefix == "":
            base_line = f"@prefix : <{ns}> ."
        else:
            other_prefix_lines.append(f"@prefix {prefix}: <{ns}> .")
    other_prefix_lines.sort()
    prefix_block = "\n".join(([base_line] if base_line else []) + other_prefix_lines)

    return prefix_block + "\n\n" + "\n\n".join(sections) + "\n"


def _subgraph_for(src: Graph, subjects) -> Graph:
    """Build a sub-graph rooted at ``subjects`` plus transitively reachable BNode triples.

    Namespace bindings are copied so the sub-graph serializes with the same
    prefixes as the source.
    """
    sub = Graph(bind_namespaces="none")
    for prefix, ns in src.namespaces():
        sub.bind(prefix, ns, override=True, replace=True)
    visited: set = set()
    queue = list(subjects)
    while queue:
        s = queue.pop()
        if s in visited:
            continue
        visited.add(s)
        for p, o in src.predicate_objects(s):
            sub.add((s, p, o))
            if isinstance(o, BNode):
                queue.append(o)
    return sub
