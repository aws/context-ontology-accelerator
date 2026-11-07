// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Parser, Store, type Prefixes } from "n3";

export interface TurtleEntity {
  subject: string;
  fullSubject: string;
  /** The statement's exact source text, so callers that edit an entity can
   *  splice their change back into the document with a string replace. */
  body: string;
  kind: string;
  /** Absolute subject IRI, expanded with the prefixes n3 read. shortcut:
   *  relative IRIs under `@base` and escaped local names are not resolved;
   *  take the subject from n3's quads if proposals start using them. */
  iri?: string;
}

export interface ParsedTurtle {
  entities: TurtleEntity[];
  prelude: string;
  ontologyDecl: string | null;
}

const RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const OWL = "http://www.w3.org/2002/07/owl#";
const OWL_ONTOLOGY = `${OWL}Ontology`;

// When a subject has several rdf:types, report the one the class list groups
// by (classes before properties), whatever order the source lists them in.
const KIND_PRIORITY = [
  `${OWL}Class`,
  "http://www.w3.org/2000/01/rdf-schema#Class",
  `${OWL}ObjectProperty`,
  `${OWL}DatatypeProperty`,
  `${OWL}AnnotationProperty`,
  "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property",
];

const WHITESPACE = new Set([" ", "\t", "\n", "\r"]);
// A "." ends a statement only when what follows cannot continue a decimal or
// a prefixed name (both of which may contain dots).
const AFTER_TERMINATOR = new Set(["#", "<", '"', "'", "[", "("]);

/** Index just past the string literal that opens at `start`. */
function skipString(text: string, start: number): number {
  const quote = text[start];
  const long = text.startsWith(quote.repeat(3), start);
  let i = start + (long ? 3 : 1);
  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\") {
      i += 2;
      continue;
    }
    if (long ? text.startsWith(quote.repeat(3), i) : ch === quote) {
      return i + (long ? 3 : 1);
    }
    i += 1;
  }
  return text.length;
}

/** Split a Turtle document into top-level statements: a "." outside string
 *  literals, IRIs, comments and `[ ]` / `( )` nesting, or the end of a
 *  SPARQL-style PREFIX / BASE line. Statement boundaries therefore follow the
 *  grammar, not the serializer's blank lines. Each statement is returned as
 *  its exact source text. */
function splitStatements(turtle: string): string[] {
  const statements: string[] = [];
  let start = -1;
  let depth = 0;
  let i = turtle.startsWith("\ufeff") ? 1 : 0;

  while (i < turtle.length) {
    const ch = turtle[i];
    if (start < 0) {
      if (WHITESPACE.has(ch)) {
        i += 1;
        continue;
      }
      if (ch === "#") {
        while (i < turtle.length && turtle[i] !== "\n") i += 1;
        continue;
      }
      start = i;
      const directive = /^(?:PREFIX|BASE)\s/i.exec(turtle.slice(i, i + 7));
      if (directive) {
        const close = turtle.indexOf(">", i);
        const end = close < 0 ? turtle.length : close + 1;
        statements.push(turtle.slice(start, end));
        start = -1;
        i = end;
        continue;
      }
    }

    if (ch === '"' || ch === "'") {
      i = skipString(turtle, i);
    } else if (ch === "<") {
      const close = turtle.indexOf(">", i);
      i = close < 0 ? turtle.length : close + 1;
    } else if (ch === "#") {
      while (i < turtle.length && turtle[i] !== "\n") i += 1;
    } else if (ch === "[" || ch === "(") {
      depth += 1;
      i += 1;
    } else if (ch === "]" || ch === ")") {
      depth = Math.max(0, depth - 1);
      i += 1;
    } else if (ch === "." && depth === 0) {
      const next = turtle[i + 1];
      if (
        next === undefined ||
        WHITESPACE.has(next) ||
        AFTER_TERMINATOR.has(next)
      ) {
        statements.push(turtle.slice(start, i + 1));
        start = -1;
      }
      i += 1;
    } else {
      i += 1;
    }
  }
  if (start >= 0) {
    const rest = turtle.slice(start).trimEnd();
    if (rest) statements.push(rest);
  }
  return statements;
}

function parseQuads(turtle: string): {
  store: Store;
  prefixes: Prefixes<string>;
} {
  const prefixes: Prefixes<string> = {};
  const quads = new Parser().parse(turtle, null, (prefix, node) => {
    prefixes[prefix] = node.value;
  });
  return { store: new Store(quads), prefixes };
}

/** Absolute IRI of a subject written as `<iri>` or `prefix:local`. */
function subjectIri(
  subject: string,
  prefixes: Prefixes<string>,
): string | null {
  if (subject.startsWith("<") && subject.endsWith(">")) {
    return subject.slice(1, -1);
  }
  const colon = subject.indexOf(":");
  if (colon < 0 || subject.startsWith("_:")) return null;
  const namespace = prefixes[subject.slice(0, colon)];
  return namespace == null ? null : `${namespace}${subject.slice(colon + 1)}`;
}

function compactIri(iri: string, prefixes: Prefixes<string>): string {
  let best: string | null = null;
  let bestLength = -1;
  for (const [prefix, namespace] of Object.entries(prefixes)) {
    const local = iri.slice(namespace.length);
    if (
      namespace.length > bestLength &&
      iri.startsWith(namespace) &&
      /^[A-Za-z_][\w.-]*$/u.test(local)
    ) {
      best = `${prefix}:${local}`;
      bestLength = namespace.length;
    }
  }
  return best ?? `<${iri}>`;
}

function primaryType(types: string[]): string | undefined {
  return KIND_PRIORITY.find((iri) => types.includes(iri)) ?? types[0];
}

/** Read the proposal Turtle into the entity list the class list and editors
 *  use. Each entity's types (and so the class list and the ontology
 *  declaration) come from the quads n3 parses; statements are cut by the
 *  Turtle grammar, so re-serialized Turtle (for example n3's own output, which
 *  has no blank lines between subjects) reads the same as the inducer's
 *  pretty-printed form. Throws on malformed Turtle. */
export function parseTurtleEntities(turtle: string): ParsedTurtle {
  const { store, prefixes } = parseQuads(turtle);

  const preludeParts: string[] = [];
  let ontologyDecl: string | null = null;
  const entities: TurtleEntity[] = [];

  for (const statement of splitStatements(turtle)) {
    if (/^(?:@prefix|@base|PREFIX\s|BASE\s)/i.test(statement)) {
      preludeParts.push(statement);
      continue;
    }

    const subject =
      /^(<[^>]*>|[^\s;,()[\]]+)/u.exec(statement)?.[1] ?? statement;
    const iri = subjectIri(subject, prefixes);
    const types = iri
      ? store
          .getObjects(iri, RDF_TYPE, null)
          .filter((term) => term.termType === "NamedNode")
          .map((term) => term.value)
      : [];

    if (types.includes(OWL_ONTOLOGY)) {
      ontologyDecl ??= statement;
      continue;
    }

    const kindIri = primaryType(types);
    const kind = kindIri ? compactIri(kindIri, prefixes) : "";
    const local = subject.split(/[#/]/).pop() ?? subject;
    entities.push({
      subject: local,
      fullSubject: subject,
      body: statement,
      kind,
      ...(iri ? { iri } : {}),
    });
  }

  const kindOrder: Record<string, number> = {
    "owl:Class": 0,
    "owl:ObjectProperty": 1,
    "owl:DatatypeProperty": 2,
  };
  // Sort by kind (Classes → object properties → datatype properties), then
  // alphabetically by name (case-insensitive) within each kind. The proposal
  // entities panel and graph derive their ordering from this list, so this
  // makes classes/properties read in a predictable lexicographic order instead
  // of the inducer's raw emit order.
  entities.sort(
    (a, b) =>
      (kindOrder[a.kind] ?? 9) - (kindOrder[b.kind] ?? 9) ||
      a.subject.localeCompare(b.subject, undefined, { sensitivity: "base" }),
  );

  return { entities, prelude: preludeParts.join("\n"), ontologyDecl };
}
