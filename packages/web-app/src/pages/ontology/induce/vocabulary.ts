// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  DataFactory,
  Lexer,
  Parser,
  Store,
  Writer,
  type Literal,
  type NamedNode,
  type Prefixes,
  type Quad,
  type Term,
} from "n3";
import {
  formatEnglishMessage,
  type AppMessage,
  type AppMessageValues,
} from "../../../i18n/appMessages";

const RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label";
const SKOS = "http://www.w3.org/2004/02/skos/core#";
const SKOS_ALT_LABEL = `${SKOS}altLabel`;
const SKOS_PREF_LABEL = `${SKOS}prefLabel`;
const RDF_LANG_STRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString";
const XSD_STRING = "http://www.w3.org/2001/XMLSchema#string";
const MAX_LABEL_LENGTH = 512;
/** Turtle's LANGTAG production without the `@`. The proposal is saved as
 *  Turtle, so a tag its parser reads is accepted as written apart from case.
 *  ASCII ranges, not the `i` flag, so no non-ASCII letter case-folds in. */
const LANGTAG_PATTERN = /^[A-Za-z]+(?:-[A-Za-z0-9]+)*$/;
const UNSAFE_FORMAT_CONTROL_PATTERN =
  /[\u061c\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]/u;
const UNSAFE_FORMAT_CONTROL_GLOBAL_PATTERN =
  /[\u061c\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]/gu;

const { literal, namedNode, quad } = DataFactory;

/** A failed vocabulary edit. `message` is English; the panel renders
 *  `template` with `values` in the reader's language. */
export class VocabularyEditError extends Error {
  readonly template: AppMessage;
  readonly values?: AppMessageValues;

  constructor(
    template: AppMessage,
    values?: AppMessageValues,
    options?: ErrorOptions,
  ) {
    super(formatEnglishMessage(template, values), options);
    this.name = "VocabularyEditError";
    this.template = template;
    this.values = values;
  }
}

export interface VocabularyLabel {
  value: string;
  language: string;
  datatype: string;
}

export interface VocabularyConcept {
  id: string;
  /** Local names of the term's `rdf:type`s; `""` when it has none. */
  kind: string;
  preferredLabels: VocabularyLabel[];
  alternativeLabels: VocabularyLabel[];
}

interface VocabularyDocument {
  prefixes: Prefixes<string>;
  store: Store;
  /** Each quad's position in the parsed document. The serializer writes a
   *  subject's quads in this order, so an edited label keeps its place and
   *  the first `rdfs:label`, which the class list shows, does not change. */
  order: Map<string, number>;
}

function quadKey(candidate: Quad): string {
  return [
    candidate.subject.id,
    candidate.predicate.id,
    candidate.object.id,
    candidate.graph.id,
  ].join(" ");
}

function parseDocument(turtle: string): VocabularyDocument {
  const prefixes: Prefixes<string> = {};
  try {
    const quads = new Parser().parse(turtle, null, (prefix, prefixNode) => {
      prefixes[prefix] = prefixNode.value;
    });
    const order = new Map<string, number>();
    quads.forEach((candidate, index) => {
      if (!order.has(quadKey(candidate))) order.set(quadKey(candidate), index);
    });
    return { prefixes, store: new Store(quads), order };
  } catch (error) {
    const detail =
      error instanceof Error ? error.message : "unknown parser error";
    throw new VocabularyEditError(
      "Failed to parse Turtle document: {detail}",
      { detail },
      { cause: error },
    );
  }
}

const RDF_FIRST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first";
const RDF_REST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest";
const RDF_NIL = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil";

type QuadObject = Quad["object"];

/**
 * Serialize the store the way the inducer lays proposals out: one statement
 * per subject, and a blank node referenced exactly once written inline as
 * `[ ... ]` (or `( ... )` for an RDF list). n3's Writer emits quads in store
 * order and blank nodes as separate `_:n3-0` statements, which splits a class
 * across several statements; the rest of the proposal page reads the Turtle
 * one subject statement at a time, so that layout would show the class more
 * than once and list each restriction as its own entity.
 */
function serializeDocument({
  prefixes,
  store,
  order,
}: VocabularyDocument): string {
  const writer = new Writer({ prefixes });
  const quads = store.getQuads(null, null, null, null);

  const termKey = (term: Term) => `${term.termType}:${term.value}`;
  const bySubject = new Map<string, Quad[]>();
  const objectUses = new Map<string, number>();
  for (const candidate of quads) {
    const key = termKey(candidate.subject);
    const group = bySubject.get(key);
    if (group) group.push(candidate);
    else bySubject.set(key, [candidate]);
    if (candidate.object.termType === "BlankNode") {
      const objectKey = termKey(candidate.object);
      objectUses.set(objectKey, (objectUses.get(objectKey) ?? 0) + 1);
    }
  }
  const position = (candidate: Quad) =>
    order.get(quadKey(candidate)) ?? Number.MAX_SAFE_INTEGER;
  for (const group of bySubject.values()) {
    group.sort((a, b) => position(a) - position(b));
  }

  const inlinable = (term: Term): boolean =>
    term.termType === "BlankNode" &&
    objectUses.get(termKey(term)) === 1 &&
    bySubject.has(termKey(term));
  const written = new Set<string>();

  const listItems = (head: QuadObject): QuadObject[] | null => {
    const items: QuadObject[] = [];
    const chain: string[] = [];
    let node: QuadObject = head;
    while (!(node.termType === "NamedNode" && node.value === RDF_NIL)) {
      if (!inlinable(node) || chain.includes(termKey(node))) return null;
      const group = bySubject.get(termKey(node)) ?? [];
      const first = group.filter((q) => q.predicate.value === RDF_FIRST);
      const rest = group.filter((q) => q.predicate.value === RDF_REST);
      if (group.length !== 2 || first.length !== 1 || rest.length !== 1) {
        return null;
      }
      chain.push(termKey(node));
      items.push(first[0].object);
      node = rest[0].object;
    }
    for (const key of chain) written.add(key);
    return items;
  };

  const render = (term: QuadObject): QuadObject => {
    if (!inlinable(term) || written.has(termKey(term))) return term;
    const items = listItems(term);
    if (items) return writer.list(items.map(render));
    written.add(termKey(term));
    const group = bySubject.get(termKey(term)) ?? [];
    return writer.blank(
      group.map((q) => ({ predicate: q.predicate, object: render(q.object) })),
    );
  };

  const writeGroup = (key: string) => {
    if (written.has(key)) return;
    written.add(key);
    for (const candidate of bySubject.get(key) ?? []) {
      writer.addQuad(
        candidate.subject,
        candidate.predicate,
        render(candidate.object),
        candidate.graph,
      );
    }
  };

  // Subjects first, then any inlinable blank node no subject reached (for
  // example one that only sits on a cycle of blank nodes), so nothing is lost.
  for (const [key, group] of bySubject) {
    if (!inlinable(group[0].subject)) writeGroup(key);
  }
  for (const key of bySubject.keys()) writeGroup(key);

  let serialized: string | undefined;
  let writeError: Error | undefined;
  writer.end((error, result) => {
    if (error) {
      writeError = error;
      return;
    }
    serialized = result;
  });
  if (writeError) {
    throw new VocabularyEditError(
      "Failed to write Turtle document: {detail}",
      { detail: writeError.message },
      { cause: writeError },
    );
  }
  if (serialized === undefined) {
    throw new VocabularyEditError("The Turtle writer did not produce a result");
  }
  return compactUnicodeNames(serialized, prefixes);
}

/** Whether `prefix:local` is a single prefixed name with exactly that local
 *  part, judged by n3's own Turtle lexer (which follows the PN_LOCAL rules,
 *  Unicode included). */
function isPrefixedName(prefix: string, local: string): boolean {
  try {
    const [first, second] = new Lexer().tokenize(`${prefix}:${local} `);
    return (
      first?.type === "prefixed" &&
      first.prefix === prefix &&
      first.value === local &&
      second?.type === "eof"
    );
  } catch {
    return false;
  }
}

/**
 * Write IRIs back as prefixed names where Turtle allows it. n3's Writer only
 * abbreviates local names made of ASCII letters, digits, `_` and `-`, so a
 * Japanese name such as `ind:受注明細` comes out as `<http://…#受注明細>`.
 * The inducer writes those as prefixed names, and the proposal page matches
 * classes by that text (selection, R2RML lookup), so an edit must not change
 * how an untouched subject is spelled. Prefix declarations and string
 * literals are left as they are.
 */
function compactUnicodeNames(
  turtle: string,
  prefixes: Prefixes<string>,
): string {
  const namespaces = Object.entries(prefixes).sort(
    ([, a], [, b]) => b.length - a.length,
  );
  if (namespaces.length === 0) return turtle;
  const cache = new Map<string, string | null>();

  const abbreviate = (iri: string): string | null => {
    if (cache.has(iri)) return cache.get(iri) ?? null;
    let result: string | null = null;
    for (const [prefix, namespace] of namespaces) {
      const local = iri.slice(namespace.length);
      if (
        local &&
        iri.startsWith(namespace) &&
        !/[\\%]/u.test(local) &&
        isPrefixedName(prefix, local)
      ) {
        result = `${prefix}:${local}`;
        break;
      }
    }
    cache.set(iri, result);
    return result;
  };

  return turtle
    .split("\n")
    .map((line) => {
      if (/^\s*(?:@prefix|@base|PREFIX\s|BASE\s)/iu.test(line)) return line;
      let out = "";
      let i = 0;
      while (i < line.length) {
        const ch = line[i];
        if (ch === '"' || ch === "'") {
          let j = i + 1;
          while (j < line.length && line[j] !== ch)
            j += line[j] === "\\" ? 2 : 1;
          out += line.slice(i, j + 1);
          i = j + 1;
        } else if (ch === "<") {
          const close = line.indexOf(">", i);
          if (close < 0) {
            out += line.slice(i);
            break;
          }
          const iri = line.slice(i + 1, close);
          out += abbreviate(iri) ?? line.slice(i, close + 1);
          i = close + 1;
        } else {
          out += ch;
          i += 1;
        }
      }
      return out;
    })
    .join("\n");
}

function asLabel(term: Literal): VocabularyLabel {
  return {
    value: term.value,
    language: term.language,
    datatype: term.datatype.value,
  };
}

/** Language tags compare case-insensitively; n3 reads them lowercased. */
function sameLanguage(left: string, right: string): boolean {
  return left.toLowerCase() === right.toLowerCase();
}

function normalizedLabel(label: VocabularyLabel): VocabularyLabel {
  const value = label.value.trim();
  if (!value) {
    throw new VocabularyEditError("A vocabulary label must not be blank");
  }
  if (UNSAFE_FORMAT_CONTROL_PATTERN.test(value)) {
    throw new VocabularyEditError(
      "A vocabulary label must not contain unsafe invisible formatting controls",
    );
  }
  if (value.length > MAX_LABEL_LENGTH) {
    throw new VocabularyEditError(
      "A vocabulary label must not exceed {maximum} characters",
      { maximum: MAX_LABEL_LENGTH },
    );
  }
  const language = label.language.trim();
  if (!language) {
    // An untagged label keeps a custom datatype (`"a"^^ex:t`); a language
    // string without a tag becomes a plain string.
    const datatype =
      label.datatype && label.datatype !== RDF_LANG_STRING
        ? label.datatype
        : XSD_STRING;
    return { ...label, value, language: "", datatype };
  }

  if (!LANGTAG_PATTERN.test(language)) {
    throw new VocabularyEditError('"{language}" is not a valid language tag', {
      language,
    });
  }
  return {
    ...label,
    value,
    language: language.toLowerCase(),
    datatype: RDF_LANG_STRING,
  };
}

function duplicateLanguageError(language: string): VocabularyEditError {
  return language
    ? new VocabularyEditError(
        'This concept already has a preferred label for language "{language}"',
        { language },
      )
    : new VocabularyEditError(
        "This concept already has an untagged preferred label",
      );
}

function toLiteral(label: VocabularyLabel): Literal {
  if (label.language) return literal(label.value, label.language);
  if (label.datatype && label.datatype !== XSD_STRING) {
    return literal(label.value, namedNode(label.datatype));
  }
  return literal(label.value);
}

/** Identity of a label: two labels with the same key are the same literal. */
export function labelKey(label: VocabularyLabel): string {
  return `${label.language.toLowerCase()}\u0000${label.datatype}\u0000${label.value}`;
}

function labelsEqual(left: VocabularyLabel, right: VocabularyLabel): boolean {
  return labelKey(left) === labelKey(right);
}

export function safeVocabularyDisplayText(value: string): string {
  return value.replace(UNSAFE_FORMAT_CONTROL_GLOBAL_PATTERN, "\ufffd");
}

function uniqueLabels(labels: VocabularyLabel[]): VocabularyLabel[] {
  const byKey = new Map<string, VocabularyLabel>();
  for (const label of labels) byKey.set(labelKey(label), label);
  return [...byKey.values()].sort(
    (a, b) =>
      a.language.localeCompare(b.language) ||
      a.value.localeCompare(b.value) ||
      a.datatype.localeCompare(b.datatype),
  );
}

function literalLabels(
  store: Store,
  subject: NamedNode,
  predicate: string,
): VocabularyLabel[] {
  return store
    .getObjects(subject, namedNode(predicate), null)
    .filter((term): term is Literal => term.termType === "Literal")
    .map(asLabel);
}

function preferredPredicate(store: Store, subject: NamedNode): string {
  return store.countQuads(subject, namedNode(SKOS_PREF_LABEL), null, null) > 0
    ? SKOS_PREF_LABEL
    : RDFS_LABEL;
}

function localName(iri: string): string {
  const hash = iri.lastIndexOf("#");
  const slash = iri.lastIndexOf("/");
  return iri.slice(Math.max(hash, slash) + 1) || iri;
}

function conceptFromSubject(
  store: Store,
  subject: NamedNode,
): VocabularyConcept {
  const skosPreferred = literalLabels(store, subject, SKOS_PREF_LABEL);
  const preferredLabels =
    skosPreferred.length > 0
      ? skosPreferred
      : literalLabels(store, subject, RDFS_LABEL);
  const alternativeLabels = literalLabels(store, subject, SKOS_ALT_LABEL);
  const typeIris = store
    .getObjects(subject, namedNode(RDF_TYPE), null)
    .filter((term): term is NamedNode => term.termType === "NamedNode")
    .map((term) => term.value)
    .sort();

  return {
    id: subject.value,
    kind: typeIris.map(localName).join(", "),
    preferredLabels: uniqueLabels(preferredLabels),
    alternativeLabels: uniqueLabels(alternativeLabels),
  };
}

// Each class side panel reads the whole proposal; cache the last parse so
// opening another class does not parse the same Turtle again.
let lastParsed: { turtle: string; concepts: VocabularyConcept[] } | null = null;

export function parseVocabulary(turtle: string): VocabularyConcept[] {
  if (lastParsed?.turtle === turtle) return lastParsed.concepts;
  const { store } = parseDocument(turtle);
  const subjects = new Map<string, NamedNode>();
  for (const predicate of [SKOS_PREF_LABEL, RDFS_LABEL, SKOS_ALT_LABEL]) {
    for (const subject of store.getSubjects(namedNode(predicate), null, null)) {
      if (subject.termType === "NamedNode")
        subjects.set(subject.value, subject);
    }
  }

  const concepts = [...subjects.values()]
    .map((subject) => conceptFromSubject(store, subject))
    .sort(
      (a, b) =>
        (a.preferredLabels[0]?.value ?? a.id).localeCompare(
          b.preferredLabels[0]?.value ?? b.id,
        ) || a.id.localeCompare(b.id),
    );
  lastParsed = { turtle, concepts };
  return concepts;
}

function findLabel(
  store: Store,
  subject: NamedNode,
  predicate: string,
  label: VocabularyLabel,
): Quad | undefined {
  return store
    .getQuads(subject, namedNode(predicate), null, null)
    .find(
      (candidate) =>
        candidate.object.termType === "Literal" &&
        labelsEqual(asLabel(candidate.object), label),
    );
}

function removeLabel(
  store: Store,
  subject: NamedNode,
  predicate: string,
  label: VocabularyLabel,
): void {
  for (const candidate of store.getQuads(
    subject,
    namedNode(predicate),
    null,
    null,
  )) {
    if (
      candidate.object.termType === "Literal" &&
      labelsEqual(asLabel(candidate.object), label)
    ) {
      store.removeQuad(candidate);
    }
  }
}

/** Adds the label unless it is already there, and reports whether it was
 *  added. `placeOf` is a removed quad whose position the new one takes. */
function addLabel(
  { store, order }: VocabularyDocument,
  subject: NamedNode,
  predicate: string,
  label: VocabularyLabel,
  placeOf?: Quad,
): boolean {
  if (findLabel(store, subject, predicate, label)) return false;
  const added = quad(subject, namedNode(predicate), toLiteral(label));
  store.addQuad(added);
  const place = placeOf && order.get(quadKey(placeOf));
  if (place !== undefined) order.set(quadKey(added), place);
  return true;
}

export function replacePreferredLabel(
  turtle: string,
  conceptId: string,
  current: VocabularyLabel,
  replacement: VocabularyLabel,
): string {
  const document = parseDocument(turtle);
  const subject = namedNode(conceptId);
  const predicate = preferredPredicate(document.store, subject);
  const normalizedReplacement = normalizedLabel(replacement);
  if (labelsEqual(normalizedReplacement, current)) return turtle;
  const conflictingPreferred = literalLabels(
    document.store,
    subject,
    predicate,
  ).find(
    (candidate) =>
      !labelsEqual(candidate, current) &&
      sameLanguage(candidate.language, normalizedReplacement.language),
  );
  if (conflictingPreferred) {
    throw duplicateLanguageError(normalizedReplacement.language);
  }
  const currentQuad = findLabel(document.store, subject, predicate, current);
  removeLabel(document.store, subject, predicate, current);
  removeLabel(document.store, subject, SKOS_ALT_LABEL, normalizedReplacement);
  addLabel(document, subject, predicate, normalizedReplacement, currentQuad);
  return serializeDocument(document);
}

export function addPreferredLabel(
  turtle: string,
  conceptId: string,
  label: VocabularyLabel,
): string {
  const document = parseDocument(turtle);
  const subject = namedNode(conceptId);
  const predicate = preferredPredicate(document.store, subject);
  const normalized = normalizedLabel(label);
  const preferred = literalLabels(document.store, subject, predicate);
  if (preferred.some((candidate) => labelsEqual(candidate, normalized))) {
    throw new VocabularyEditError(
      "This concept already has that preferred label",
    );
  }
  if (
    preferred.some((candidate) =>
      sameLanguage(candidate.language, normalized.language),
    )
  ) {
    throw duplicateLanguageError(normalized.language);
  }
  removeLabel(document.store, subject, SKOS_ALT_LABEL, normalized);
  addLabel(document, subject, predicate, normalized);
  return serializeDocument(document);
}

export function addAlternativeLabel(
  turtle: string,
  conceptId: string,
  label: VocabularyLabel,
): string {
  const document = parseDocument(turtle);
  const subject = namedNode(conceptId);
  const normalized = normalizedLabel(label);
  const preferred = conceptFromSubject(document.store, subject).preferredLabels;
  if (preferred.some((candidate) => labelsEqual(candidate, normalized))) {
    throw new VocabularyEditError(
      "A preferred label cannot also be added as a variant",
    );
  }
  if (!addLabel(document, subject, SKOS_ALT_LABEL, normalized)) {
    throw new VocabularyEditError("This concept already has that variant");
  }
  return serializeDocument(document);
}

export function removeAlternativeLabel(
  turtle: string,
  conceptId: string,
  label: VocabularyLabel,
): string {
  const document = parseDocument(turtle);
  removeLabel(document.store, namedNode(conceptId), SKOS_ALT_LABEL, label);
  return serializeDocument(document);
}

/** Promotes a variant to the preferred label of its language. The two labels
 *  swap places, so the order of the term's labels does not change. */
export function makePreferredLabel(
  turtle: string,
  conceptId: string,
  label: VocabularyLabel,
): string {
  const document = parseDocument(turtle);
  const subject = namedNode(conceptId);
  const predicate = preferredPredicate(document.store, subject);
  const preferred = literalLabels(document.store, subject, predicate);
  const variantQuad = findLabel(document.store, subject, SKOS_ALT_LABEL, label);

  removeLabel(document.store, subject, SKOS_ALT_LABEL, label);
  let preferredQuad: Quad | undefined;
  for (const current of preferred.filter((candidate) =>
    sameLanguage(candidate.language, label.language),
  )) {
    preferredQuad ??= findLabel(document.store, subject, predicate, current);
    removeLabel(document.store, subject, predicate, current);
    addLabel(document, subject, SKOS_ALT_LABEL, current, variantQuad);
  }
  addLabel(document, subject, predicate, label, preferredQuad);
  return serializeDocument(document);
}
