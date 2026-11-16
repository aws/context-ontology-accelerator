// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Shared Turtle lexer. The public surface is ``parseTriples`` plus a few
 * helpers.
 *
 * The tokeniser is intentionally not a full Turtle parser. The
 * inducer's output is a pretty-printed mix of:
 *
 *   1. Single-line triples: ``<s> <p> <o> .``
 *   2. Multi-predicate blocks on one subject:
 *      ``ind:Foo a owl:Class ;
 *           rdfs:label "Foo" ;
 *           rdfs:subClassOf ind:Bar .``
 *   3. Inline lists ``( <a> <b> )`` as objects (only owl:hasKey today).
 *   4. Inline blank nodes ``[ ... ]`` as objects (only the R2RML
 *      consumer needs to walk into these; the OWL consumer skips
 *      them and that's fine — the proposal's class graph is already
 *      flat-emitted by rdflib).
 *
 * Blank-node-only constructs and complex nested lists are skipped
 * silently — the proposal Turtle is flat enough that this coverage is
 * sufficient.
 */

export const RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
export const OWL_CLASS = "http://www.w3.org/2002/07/owl#Class";
export const OWL_OBJECT_PROPERTY =
  "http://www.w3.org/2002/07/owl#ObjectProperty";
export const OWL_DATATYPE_PROPERTY =
  "http://www.w3.org/2002/07/owl#DatatypeProperty";
export const OWL_HAS_KEY = "http://www.w3.org/2002/07/owl#hasKey";
export const RDFS_SUBCLASS_OF =
  "http://www.w3.org/2000/01/rdf-schema#subClassOf";
export const RDFS_DOMAIN = "http://www.w3.org/2000/01/rdf-schema#domain";
export const RDFS_RANGE = "http://www.w3.org/2000/01/rdf-schema#range";
export const RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label";

export const SCL_NS = "http://coa.amazon.com/vocab/coa#";
export const SCL_DATASOURCE_ID = `${SCL_NS}datasourceId`;
export const SCL_SOURCE_SCHEMA = `${SCL_NS}sourceSchema`;
export const SCL_FK_PROVENANCE = `${SCL_NS}fkProvenance`;
export const SCL_SUBCLASS_PROVENANCE = `${SCL_NS}subClassProvenance`;
export const SCL_SUGGESTED_SUBCLASS_OF = `${SCL_NS}suggestedSubClassOf`;
export const SCL_SUGGESTION_REASON = `${SCL_NS}suggestionReason`;

// Grounding is keyed off ``skos:exactMatch`` / ``skos:closeMatch``, not
// ``coa:groundedTo`` or ``rdfs:subClassOf``; the latter is also used for
// PK-sharing to sibling ``ind:`` classes and would false-positive.
export const SKOS_NS = "http://www.w3.org/2004/02/skos/core#";
export const SKOS_EXACT_MATCH = `${SKOS_NS}exactMatch`;
export const SKOS_CLOSE_MATCH = `${SKOS_NS}closeMatch`;

/** Synthetic predicate emitted by the tokeniser for ``owl:hasKey`` list
 *  members — one triple per list member, in declaration order. */
export const HAS_KEY_LIST_PRED = "__hasKeyList__";

export type Triple = { s: string; p: string; o: string };

/** A statement body plus the source line range it spans (1-indexed,
 *  inclusive) and its character offsets in the original document.
 *  Offsets enable surgical text edits — splice replacement back into
 *  the original between ``startOffset`` and ``endOffset`` (exclusive
 *  of the terminating ``.``) without disturbing comments, prefix
 *  declarations, or inter-statement whitespace. */
export interface StatementSpan {
  text: string;
  startLine: number;
  endLine: number;
  /** Character offset of the first non-whitespace character of the
   *  statement in the original document. */
  startOffset: number;
  /** Character offset just past the statement's ``.`` terminator in
   *  the original document. Equal to source.length when the document
   *  ends without a terminator. */
  endOffset: number;
}

const PREFIX_RE = /@prefix\s+([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*<([^>]+)>\s*\./g;

export function parsePrefixes(turtle: string): Record<string, string> {
  const prefixes: Record<string, string> = {};
  for (const match of turtle.matchAll(PREFIX_RE)) {
    prefixes[match[1]] = match[2];
  }
  return prefixes;
}

/** Expand a Turtle token (compact IRI, ``<...>`` IRI, or the ``a``
 *  shorthand) to an absolute IRI string. Returns ``null`` when the
 *  prefix is unknown or the token is malformed. */
export function expandIri(
  token: string,
  prefixes: Record<string, string>,
): string | null {
  const trimmed = token.trim();
  if (!trimmed) return null;
  if (trimmed === "a") return RDF_TYPE;
  if (trimmed.startsWith("<") && trimmed.endsWith(">")) {
    return trimmed.slice(1, -1);
  }
  const colonAt = trimmed.indexOf(":");
  if (colonAt < 0) return null;
  const prefix = trimmed.slice(0, colonAt);
  const local = trimmed.slice(colonAt + 1);
  const base = prefixes[prefix];
  if (base === undefined) return null;
  return `${base}${local}`;
}

/** Local-name part of an absolute IRI (``…#X`` → ``X``, ``…/X`` → ``X``). */
export function localName(iri: string): string {
  const hash = iri.lastIndexOf("#");
  if (hash >= 0) return iri.slice(hash + 1);
  const slash = iri.lastIndexOf("/");
  if (slash >= 0) return iri.slice(slash + 1);
  return iri;
}

/** Strip Turtle line comments (``# … EOL``). String-, IRI-, and
 *  newline-aware. */
export function stripComments(s: string): string {
  let out = "";
  let inStr = false;
  let inIri = false;
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (inStr) {
      out += c;
      if (c === '"' && s[i - 1] !== "\\") inStr = false;
      i++;
      continue;
    }
    if (inIri) {
      out += c;
      if (c === ">") inIri = false;
      i++;
      continue;
    }
    if (c === '"') {
      inStr = true;
      out += c;
      i++;
      continue;
    }
    if (c === "<") {
      inIri = true;
      out += c;
      i++;
      continue;
    }
    if (c === "#") {
      // Eat to (but preserve) the newline so line-numbering still works.
      while (i < s.length && s[i] !== "\n" && s[i] !== "\r") i++;
      continue;
    }
    out += c;
    i++;
  }
  return out;
}

/**
 * Split a Turtle document on ``.`` terminators, respecting strings,
 * IRIs, and parenthesised/bracketed lists. Returns each statement body
 * with the 1-indexed line range it spans in the original document.
 *
 * A naive ``str.split('.')`` would break on ``.`` characters inside
 * IRIs, decimals in literals, and dotted compact IRIs.
 */
export function splitStatements(s: string): StatementSpan[] {
  const out: StatementSpan[] = [];
  let depth = 0;
  let inStr = false;
  let inIri = false;
  let buf = "";
  let line = 1;
  let stmtStartLine = 1;
  let stmtStartOffset = 0;
  let bufHasContent = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (!bufHasContent && !/\s/.test(c)) {
      stmtStartLine = line;
      stmtStartOffset = i;
      bufHasContent = true;
    }
    if (inStr) {
      buf += c;
      if (c === '"' && s[i - 1] !== "\\") inStr = false;
      if (c === "\n") line++;
      continue;
    }
    if (inIri) {
      buf += c;
      if (c === ">") inIri = false;
      if (c === "\n") line++;
      continue;
    }
    if (c === '"') {
      inStr = true;
      buf += c;
      continue;
    }
    if (c === "<") {
      inIri = true;
      buf += c;
      continue;
    }
    if (c === "(" || c === "[") depth++;
    if (c === ")" || c === "]") depth = Math.max(0, depth - 1);
    if (c === "." && depth === 0) {
      out.push({
        text: buf,
        startLine: stmtStartLine,
        endLine: line,
        startOffset: stmtStartOffset,
        endOffset: i + 1,
      });
      buf = "";
      bufHasContent = false;
      continue;
    }
    if (c === "\n") line++;
    buf += c;
  }
  if (buf.trim()) {
    out.push({
      text: buf,
      startLine: stmtStartLine,
      endLine: line,
      startOffset: stmtStartOffset,
      endOffset: s.length,
    });
  }
  return out;
}

/** Split a predicate-object block on ``;`` separators, respecting
 *  strings, IRIs, and parenthesised/bracketed lists. */
export function splitOnSemicolons(s: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let inStr = false;
  let inIri = false;
  let buf = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inStr) {
      buf += c;
      if (c === '"' && s[i - 1] !== "\\") inStr = false;
      continue;
    }
    if (inIri) {
      buf += c;
      if (c === ">") inIri = false;
      continue;
    }
    if (c === '"') {
      inStr = true;
      buf += c;
      continue;
    }
    if (c === "<") {
      inIri = true;
      buf += c;
      continue;
    }
    if (c === "(" || c === "[") depth++;
    if (c === ")" || c === "]") depth = Math.max(0, depth - 1);
    if (c === ";" && depth === 0) {
      out.push(buf);
      buf = "";
      continue;
    }
    buf += c;
  }
  if (buf.trim()) out.push(buf);
  return out;
}

/** Split a comma-separated object list on top-level ``,`` separators
 *  (strings, IRIs, lists are preserved). Useful for triples like
 *  ``rr:predicateObjectMap pom:A, pom:B, pom:C .`` that rdflib emits
 *  when a subject has many object references for the same predicate. */
export function splitOnCommas(s: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let inStr = false;
  let inIri = false;
  let buf = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inStr) {
      buf += c;
      if (c === '"' && s[i - 1] !== "\\") inStr = false;
      continue;
    }
    if (inIri) {
      buf += c;
      if (c === ">") inIri = false;
      continue;
    }
    if (c === '"') {
      inStr = true;
      buf += c;
      continue;
    }
    if (c === "<") {
      inIri = true;
      buf += c;
      continue;
    }
    if (c === "(" || c === "[") depth++;
    if (c === ")" || c === "]") depth = Math.max(0, depth - 1);
    if (c === "," && depth === 0) {
      out.push(buf);
      buf = "";
      continue;
    }
    buf += c;
  }
  if (buf.trim()) out.push(buf);
  return out;
}

/** Index of the first whitespace-or-list-terminator after a token
 *  starting at ``start``, treating ``<...>`` IRIs as a single token.
 *  Returns -1 if no token. */
export function nextTokenEnd(s: string, start: number): number {
  let i = start;
  while (i < s.length && /\s/.test(s[i])) i++;
  if (i >= s.length) return -1;
  if (s[i] === "<") {
    const close = s.indexOf(">", i + 1);
    return close < 0 ? -1 : close + 1;
  }
  let j = i;
  while (j < s.length && !/\s/.test(s[j])) j++;
  return j;
}

/** Extract the text content of a Turtle string literal. ``"value"@en``
 *  and ``"value"^^xsd:string`` strip to ``value``. Handles
 *  backslash-escaped inner quotes (``"\"x\""`` → ``\"x\"``) — the
 *  inducer wraps SQL identifiers in double-quoted strings and rdflib
 *  serialises them with backslash escapes. */
export function literalText(literal: string): string {
  if (!literal.startsWith('"')) return literal;
  // Walk forward looking for the first unescaped closing quote.
  for (let i = 1; i < literal.length; i++) {
    if (literal[i] === "\\") {
      i++; // skip the escaped character
      continue;
    }
    if (literal[i] === '"') {
      return literal.slice(1, i);
    }
  }
  return literal;
}

export interface ParsedTriples {
  triples: Triple[];
  /** Per-triple source line range. ``tripleLines[i]`` corresponds to
   *  ``triples[i]``. */
  tripleLines: Array<{ startLine: number; endLine: number }>;
  prefixes: Record<string, string>;
}

/** Tokenise a Turtle document into ``{s, p, o}`` triples, with each triple's
 *  source line range in ``tripleLines``. */
export function parseTriples(turtle: string): ParsedTriples {
  const prefixes = parsePrefixes(turtle);
  const stripped = stripComments(turtle);
  const stmts = splitStatements(stripped);
  const triples: Triple[] = [];
  const tripleLines: Array<{ startLine: number; endLine: number }> = [];

  for (const stmt of stmts) {
    // Collapse multi-line whitespace within the statement so the
    // tokeniser can scan linearly. Line numbers are tracked per
    // statement, not per object; precise within ~one statement.
    const text = stmt.text.replace(/\r?\n/g, " ");
    const trimmed = text.trim();
    if (!trimmed || trimmed.startsWith("@")) continue;
    const subjEnd = nextTokenEnd(trimmed, 0);
    if (subjEnd <= 0) continue;
    const subject = trimmed.slice(0, subjEnd);
    const subjectIri = expandIri(subject, prefixes);
    if (!subjectIri) continue;
    const rest = trimmed.slice(subjEnd).trim();
    for (const pair of splitOnSemicolons(rest)) {
      const pairTrim = pair.trim();
      if (!pairTrim) continue;
      const predEnd = nextTokenEnd(pairTrim, 0);
      if (predEnd <= 0) continue;
      const predToken = pairTrim.slice(0, predEnd);
      const predIri = expandIri(predToken, prefixes);
      if (!predIri) continue;
      const objRaw = pairTrim.slice(predEnd).trim();
      if (!objRaw) continue;
      // Comma-separated object list: ``<s> <p> <o1>, <o2>, <o3> .``
      // rdflib emits this for predicates with multiple URI-ref objects
      // (the canonical case for our proposal Turtle is
      // ``rr:predicateObjectMap`` with several POM URIs).
      for (const objExpr of splitOnCommas(objRaw)) {
        const objText = objExpr.trim();
        if (!objText) continue;
        if (objText.startsWith("(")) {
          const closeIdx = objText.indexOf(")");
          if (closeIdx < 0) continue;
          const inner = objText.slice(1, closeIdx).trim();
          const members = inner.split(/\s+/).filter(Boolean);
          for (const m of members) {
            const memberIri = expandIri(m, prefixes);
            if (memberIri) {
              triples.push({
                s: subjectIri,
                p: predIri === OWL_HAS_KEY ? HAS_KEY_LIST_PRED : predIri,
                o: memberIri,
              });
              tripleLines.push({
                startLine: stmt.startLine,
                endLine: stmt.endLine,
              });
            }
          }
          continue;
        }
        if (objText.startsWith('"')) {
          // Find the first unescaped closing quote — rdflib wraps SQL
          // identifiers as ``"\"x\""`` so plain indexOf would close at
          // the first ``\"``.
          let closing = -1;
          for (let k = 1; k < objText.length; k++) {
            if (objText[k] === "\\") {
              k++;
              continue;
            }
            if (objText[k] === '"') {
              closing = k;
              break;
            }
          }
          if (closing < 0) continue;
          let end = closing + 1;
          if (objText[end] === "@") {
            while (
              end < objText.length &&
              /[A-Za-z0-9-]/.test(objText[end + 1])
            ) {
              end++;
            }
            end++;
          } else if (objText[end] === "^" && objText[end + 1] === "^") {
            end += 2;
            while (end < objText.length && !/[\s,;.]/.test(objText[end])) {
              end++;
            }
          }
          triples.push({
            s: subjectIri,
            p: predIri,
            o: objText.slice(0, end),
          });
          tripleLines.push({
            startLine: stmt.startLine,
            endLine: stmt.endLine,
          });
          continue;
        }
        const objEnd = nextTokenEnd(objText, 0);
        if (objEnd <= 0) continue;
        const objToken = objText.slice(0, objEnd);
        const objIri = expandIri(objToken, prefixes);
        if (objIri) {
          triples.push({ s: subjectIri, p: predIri, o: objIri });
          tripleLines.push({
            startLine: stmt.startLine,
            endLine: stmt.endLine,
          });
        }
      }
    }
  }

  return { triples, tripleLines, prefixes };
}
