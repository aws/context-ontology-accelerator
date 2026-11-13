// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Lightweight Turtle → graph extractor for proposal preview.
 *
 * Delegates tokenisation to ``utils/turtleLexer`` (the shared lexer
 * also used by the R2RML and ClassDetailCard parsers) and walks the
 * resulting triples to assemble the ``{nodes, edges}`` shape
 * ``OntologyGraphFlow`` consumes.
 *
 * Recognises:
 *   - ``<iri> a owl:Class .``
 *   - ``<child> rdfs:subClassOf <parent> .``
 *   - ``<prop> a owl:ObjectProperty .`` + rdfs:domain / rdfs:range
 *
 * IRIs are resolved to absolute form so node ids are stable across
 * encodings. Anything we don't recognise is ignored.
 */

import {
  OWL_CLASS,
  OWL_DATATYPE_PROPERTY,
  OWL_OBJECT_PROPERTY,
  RDF_TYPE,
  RDFS_DOMAIN,
  RDFS_LABEL,
  RDFS_RANGE,
  RDFS_SUBCLASS_OF,
  SCL_SUGGESTED_SUBCLASS_OF,
  SKOS_CLOSE_MATCH,
  SKOS_EXACT_MATCH,
  literalText,
  localName,
  parseTriples,
} from "../../utils/turtleLexer";

export type TurtleGraphNode = {
  id: string;
  data: Record<string, unknown>;
};

export type TurtleGraphEdge = {
  id: string;
  source: string;
  target: string;
  label?: string;
  data?: Record<string, unknown>;
};

export type TurtleGraphData = {
  nodes: TurtleGraphNode[];
  edges: TurtleGraphEdge[];
};

export function turtleToGraph(turtle: string): TurtleGraphData {
  const { triples } = parseTriples(turtle);

  const classes = new Set<string>();
  const labels: Record<string, string> = {};
  const objectProps = new Set<string>();
  const datatypeProps = new Set<string>();
  const propDomain: Record<string, string> = {};
  const propRange: Record<string, string> = {};
  const subclassEdges: Array<[string, string]> = [];
  // Suggested-inheritance pairs, rendered as dashed sub-class edges.
  const suggestedParents: Record<string, string[]> = {};
  // Grounding target: the foundational class a proposed class is grounded to,
  // carried by ``skos:exactMatch`` (preferred) or ``skos:closeMatch``. Exact
  // wins over close; ``rdfs:subClassOf`` is not grounding (it is also used for
  // PK-sharing to sibling ind: classes).
  const groundedTo: Record<string, string> = {};
  const groundedExact: Record<string, boolean> = {};

  for (const { s, p, o } of triples) {
    if (p === RDF_TYPE && o === OWL_CLASS) {
      classes.add(s);
    } else if (p === RDF_TYPE && o === OWL_OBJECT_PROPERTY) {
      objectProps.add(s);
    } else if (p === RDF_TYPE && o === OWL_DATATYPE_PROPERTY) {
      datatypeProps.add(s);
    } else if (p === RDFS_SUBCLASS_OF) {
      subclassEdges.push([s, o]);
      classes.add(s);
      classes.add(o);
    } else if (p === RDFS_DOMAIN) {
      propDomain[s] = o;
    } else if (p === RDFS_RANGE) {
      propRange[s] = o;
    } else if (p === RDFS_LABEL && o.startsWith('"')) {
      labels[s] = literalText(o);
    } else if (p === SCL_SUGGESTED_SUBCLASS_OF) {
      suggestedParents[s] = suggestedParents[s]
        ? [...suggestedParents[s], o]
        : [o];
      classes.add(s);
      classes.add(o);
    } else if (p === SKOS_EXACT_MATCH) {
      // Exact match is the strongest grounding signal; always overwrite.
      groundedTo[s] = o;
      groundedExact[s] = true;
    } else if (p === SKOS_CLOSE_MATCH) {
      // Close match only grounds a class that has no exact match yet.
      if (!groundedExact[s]) groundedTo[s] = o;
    }
  }

  const nodes: TurtleGraphNode[] = [];
  // Attributes (datatype properties) grouped by their domain class, so the
  // graph can reveal a class's attributes on hover without making 800+
  // permanent nodes. Each entry is { label, range } in declaration order.
  const attributesByClass: Record<
    string,
    Array<{ label: string; range?: string }>
  > = {};
  for (const prop of datatypeProps) {
    const domain = propDomain[prop];
    if (!domain || !classes.has(domain)) continue;
    (attributesByClass[domain] ??= []).push({
      label: labels[prop] ?? localName(prop),
      range: propRange[prop] ? localName(propRange[prop]) : undefined,
    });
  }

  for (const iri of classes) {
    nodes.push({
      id: iri,
      data: {
        prefLabel: labels[iri] ?? localName(iri),
        typeIri: OWL_CLASS,
        isGrounded: !!groundedTo[iri],
        attributes: attributesByClass[iri] ?? [],
      },
    });
  }

  const edges: TurtleGraphEdge[] = [];
  let edgeIdx = 0;
  for (const [child, parent] of subclassEdges) {
    if (!classes.has(child) || !classes.has(parent)) continue;
    edges.push({
      id: `sc-${edgeIdx++}`,
      source: child,
      target: parent,
      label: "subClassOf",
      data: {
        kind: "subClassOf",
      },
    });
  }
  // Suggested subclass-of edges render dashed in the graph view — the
  // user is meant to confirm or reject them, not treat them as committed
  // inheritance.
  for (const [child, parents] of Object.entries(suggestedParents)) {
    for (const parent of parents) {
      if (!classes.has(child) || !classes.has(parent)) continue;
      edges.push({
        id: `sc-suggested-${edgeIdx++}`,
        source: child,
        target: parent,
        label: "suggested",
        data: {
          kind: "suggestedSubClassOf",
        },
      });
    }
  }
  for (const prop of objectProps) {
    const domain = propDomain[prop];
    const range = propRange[prop];
    if (!domain || !range) continue;
    if (!classes.has(domain) || !classes.has(range)) continue;
    edges.push({
      id: `op-${edgeIdx++}`,
      source: domain,
      target: range,
      label: labels[prop] ?? localName(prop),
      data: {
        kind: "objectProperty",
      },
    });
  }

  return { nodes, edges };
}
