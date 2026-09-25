// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Shared grounding types + helpers for the proposal review page.
 *
 * The proposal's ``metadata.matches`` is a list of ``ConceptMatch`` records
 * emitted by the inducer (see ``inducer/schemas.py``). Because ``metadata`` is
 * typed as ``Record<string, unknown>`` on the wire, callers narrow the raw
 * value into ``ConceptMatch[]`` via ``parseConceptMatches`` (a runtime type
 * guard) rather than casting — see ``.claude/rules/frontend-typescript.md``.
 *
 * Table-level matches carry an empty ``source_column``; column-level matches
 * name the specific column. Grounding candidates are keyed per-TABLE
 * (``source_table``), so a class row is mapped to its match by resolving the
 * class → source-table link from the R2RML mapping.
 */
import { splitStatements, stripComments } from "../../../utils/turtleLexer";

export interface MatchCandidate {
  entity_uri: string;
  ontology_id?: string | null;
  lexical_sim: number;
  fused_score?: number | null;
  definition?: string | null;
}

export interface ConceptMatch {
  source_table: string;
  source_column: string;
  matched_class_uri?: string | null;
  matched_ontology_id?: string | null;
  similarity?: number | null;
  match_type: string;
  scoring_strategy?: string | null;
  reason?: string | null;
  candidates?: MatchCandidate[] | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseCandidate(value: unknown): MatchCandidate | null {
  if (!isRecord(value)) return null;
  const { entity_uri, ontology_id, lexical_sim, fused_score, definition } =
    value;
  if (typeof entity_uri !== "string") return null;
  return {
    entity_uri,
    ontology_id:
      typeof ontology_id === "string" || ontology_id === null
        ? ontology_id
        : undefined,
    lexical_sim: typeof lexical_sim === "number" ? lexical_sim : 0,
    fused_score:
      typeof fused_score === "number" || fused_score === null
        ? fused_score
        : undefined,
    definition:
      typeof definition === "string" || definition === null
        ? definition
        : undefined,
  };
}

function parseConceptMatch(value: unknown): ConceptMatch | null {
  if (!isRecord(value)) return null;
  const {
    source_table,
    source_column,
    matched_class_uri,
    matched_ontology_id,
    similarity,
    match_type,
    scoring_strategy,
    reason,
    candidates,
  } = value;
  if (typeof source_table !== "string" || typeof match_type !== "string") {
    return null;
  }
  const parsedCandidates = Array.isArray(candidates)
    ? candidates
        .map(parseCandidate)
        .filter((c): c is MatchCandidate => c !== null)
    : null;
  return {
    source_table,
    source_column: typeof source_column === "string" ? source_column : "",
    matched_class_uri:
      typeof matched_class_uri === "string" || matched_class_uri === null
        ? matched_class_uri
        : undefined,
    matched_ontology_id:
      typeof matched_ontology_id === "string" || matched_ontology_id === null
        ? matched_ontology_id
        : undefined,
    similarity:
      typeof similarity === "number" || similarity === null
        ? similarity
        : undefined,
    match_type,
    scoring_strategy:
      typeof scoring_strategy === "string" || scoring_strategy === null
        ? scoring_strategy
        : undefined,
    reason: typeof reason === "string" || reason === null ? reason : undefined,
    candidates: parsedCandidates,
  };
}

/**
 * Narrow the raw ``metadata.matches`` value into ``ConceptMatch[]``. Anything
 * that doesn't structurally match is dropped, so a malformed record never
 * throws or leaks ``unknown`` into the UI.
 */
export function parseConceptMatches(value: unknown): ConceptMatch[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(parseConceptMatch)
    .filter((m): m is ConceptMatch => m !== null);
}

/**
 * Narrow the payload fetched from ``Proposal.matches_url`` into
 * ``ConceptMatch[]``. The S3 object is an envelope ``{ "matches": [...] }``
 * (written by ``put_proposal_matches_s3``), whereas the legacy inline
 * ``metadata.matches`` is the bare array — this accepts either shape so callers
 * do not have to know which source the value came from. Anything else yields
 * ``[]``.
 */
export function parseConceptMatchesEnvelope(value: unknown): ConceptMatch[] {
  if (Array.isArray(value)) return parseConceptMatches(value);
  if (isRecord(value)) return parseConceptMatches(value.matches);
  return [];
}

export function localName(uri: string): string {
  return uri.split(/[#/]/).pop() ?? uri;
}

/** First quoted-literal object of ``pred`` in a Turtle body (e.g. the
 *  ``rdfs:label`` text), or null when the predicate is absent. */
export function extractQuoted(body: string, pred: string): string | null {
  const re = new RegExp(`${pred}\\s+"([^"]+)"`);
  return body.match(re)?.[1] ?? null;
}

/** First IRI/token object of ``pred`` in a Turtle body, reduced to its
 *  local name (e.g. the ``rdfs:range`` target), or null when absent. */
export function extractUri(body: string, pred: string): string | null {
  const re = new RegExp(`${pred}\\s+([^\\s;,.]+)`);
  const m = body.match(re);
  return m ? localName(m[1]) : null;
}

/**
 * Friendly ontology label from a (possibly long) ontology id/URI.
 *
 * NOTE: this is presentation-only — it maps a known ontology id to a short
 * human-readable name for the UI. The substring tests below are deliberately
 * loose pattern matching over well-known ontology namespaces, NOT a security
 * check: nothing is trusted, authorized, or fetched based on the result, and an
 * unrecognised id falls through to the last path segments. Do not reuse this
 * helper to validate or sanitize a URL.
 */
export function ontologyLabel(ontologyId: string | null | undefined): string {
  if (!ontologyId) return "";
  if (ontologyId.includes("schema.org")) return "schema.org";
  if (ontologyId.includes("prov")) return "PROV-O";
  if (ontologyId.includes("dc/terms") || ontologyId.includes("purl.org/dc"))
    return "Dublin Core";
  if (ontologyId.includes("foaf")) return "FOAF";
  if (ontologyId.includes("/FND/Agreements/Agreements"))
    return "FIBO Agreements";
  if (ontologyId.includes("/FND/Agreements/Contracts")) return "FIBO Contracts";
  if (ontologyId.includes("/FBC/ProductsAndServices"))
    return "FIBO Clients & Accounts";
  if (ontologyId.includes("/FND/Organizations")) return "FIBO Organizations";
  if (ontologyId.includes("/FND/DatesAndTimes")) return "FIBO Dates & Times";
  if (ontologyId.includes("/FND/")) return "FIBO";
  if (ontologyId.includes("/FBC/")) return "FIBO FBC";
  if (ontologyId.includes("fibo")) return "FIBO";
  if (ontologyId.includes("/ontology/induced#")) return "Prior induction";
  return ontologyId.split("/").slice(-2).join("/");
}

/**
 * Table-level matches only (a class row corresponds to a table, not a column).
 * Keyed by ``source_table`` for O(1) lookup from a class row.
 */
export function tableMatchesByTable(
  matches: ConceptMatch[],
): Map<string, ConceptMatch> {
  const map = new Map<string, ConceptMatch>();
  for (const m of matches) {
    if (m.source_column) continue; // column-level, not a table match
    map.set(m.source_table, m);
  }
  return map;
}

// ─── Class-row → ConceptMatch resolution ────────────────────────────
//
// Grounding candidates are keyed per-TABLE (``ConceptMatch.source_table``,
// e.g. ``clinical_trial``) but the review table renders one row per CLASS
// (``ind:ClinicalTrial``). The inducer derives the class local name from the
// table via ``to_pascal(table.name)`` (``clinical_trial`` → ``ClinicalTrial``),
// so a class row's match must be recovered by inverting that link.
//
// The naive "same R2RML block carries both rr:class and rr:tableName" scan
// FAILS on real inductions: rdflib serializes ``rr:class`` on the
// ``#TriplesMap_X/SubjectMap`` IRI and ``rr:tableName`` on the
// ``rr:logicalTable`` blank node of ``#TriplesMap_X`` in SEPARATE
// blank-line-delimited blocks, so they never co-occur. Two complementary,
// layout-independent strategies replace it (tried in order):
//   1. Primary — normalized-name match. Normalize BOTH the class local name
//      and every ``source_table`` to a canonical key (strip non-alphanumerics,
//      lowercase) and join on it. Independent of the R2RML entirely.
//   2. Fallback — TriplesMap-id join over the WHOLE R2RML. Key both predicates
//      by the ``TriplesMap_X`` id embedded in the subject IRI and join
//      class→table→match. Exact; also disambiguates normalized-key collisions.

/**
 * Extract the class local name from a (possibly prefixed / full-IRI) subject.
 * Strips any ``#``/``/`` fragment and a leading ``prefix:`` — so both
 * ``ind:ClinicalTrial`` and ``http://ex.org/ind#ClinicalTrial`` resolve to
 * ``ClinicalTrial``. (``TurtleEntity.subject`` keeps its ``ind:`` prefix, so
 * the prefix strip is load-bearing.)
 */
export function classLocalName(subject: string): string {
  const afterFragment = subject.split(/[#/]/).pop() ?? subject;
  const colon = afterFragment.lastIndexOf(":");
  return colon >= 0 ? afterFragment.slice(colon + 1) : afterFragment;
}

/**
 * Canonical key for name matching: strip everything that is not a letter or
 * digit and lowercase. Inverts the inducer's ``to_pascal`` transform for
 * comparison purposes (``clinical_trial`` → ``clinicaltrial`` ←
 * ``ClinicalTrial``; ``Policy_Amount``/``PolicyAmount`` → ``policyamount``).
 */
export function normalizeNameKey(name: string): string {
  return name.replace(/[^a-zA-Z0-9]/g, "").toLowerCase();
}

/**
 * Turn an R2RML ``rr:tableName`` Turtle string literal into the bare table
 * name. The inducer wraps identifiers as SQL delimited identifiers
 * (``sql_ident("clinical_trial")`` → ``"clinical_trial"``), which in Turtle is
 * serialized double-escaped, so the whole literal (INCLUDING its Turtle
 * delimiters) reads ``"\"clinical_trial\""``.
 *
 * Peel the layers in order:
 *   1. Strip the outer Turtle string delimiters (``"…"``).
 *   2. Turtle-unescape the content (``\"`` → ``"``, ``\\`` → ``\``).
 *   3. Split into SQL identifier segments — the mapping schema-qualifies a table
 *      name that two datasources share (``"public"."customers"``) — dropping the
 *      SQL delimiters and un-doubling embedded ``""`` as it goes.
 *
 * :func:`unquoteSqlIdent` then keeps the trailing segment, because every
 * consumer that JOINS on this value joins on the bare name a ConceptMatch
 * carries; :func:`qualifiedSqlIdent` keeps the whole path, for display.
 *
 * A plain (un-delimited) value such as ``"policies"`` yields ``["policies"]``.
 */
function sqlIdentSegments(turtleLiteral: string): string[] {
  // 1. Outer Turtle delimiters.
  let s = turtleLiteral;
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) {
    s = s.slice(1, -1);
  }
  // 2. Turtle escapes.
  s = s.replace(/\\(.)/g, "$1");
  // 3. Split on the dots BETWEEN delimited identifiers only. A table
  // legitimately named ``q1.results`` is ONE delimited identifier, so splitting
  // on every dot would report its schema as ``q1``. Mirrors
  // ``coa_common.constants.split_sql_ident_path`` on the producing side.
  const segments: string[] = [];
  let buf = "";
  let inQuotes = false;
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    if (ch === '"') {
      if (inQuotes && s[i + 1] === '"') {
        buf += '"';
        i += 1;
        continue;
      }
      inQuotes = !inQuotes;
      continue;
    }
    if (ch === "." && !inQuotes) {
      segments.push(buf);
      buf = "";
      continue;
    }
    buf += ch;
  }
  segments.push(buf);
  return segments;
}

/** The bare table name: the trailing SQL identifier segment. */
export function unquoteSqlIdent(turtleLiteral: string): string {
  const segments = sqlIdentSegments(turtleLiteral);
  return segments[segments.length - 1];
}

/**
 * The table name as written in the mapping, dots and all, for DISPLAY.
 *
 * The bare form is what the ConceptMatch join needs, but it is not what the user
 * should read: when two datasources both expose ``customers`` the mapping
 * qualifies both, and labelling the two class panels "Mapping: customers" and
 * "Mapping: customers" re-fuses in the UI the two tables the mapping just took
 * care to separate.
 */
export function qualifiedSqlIdent(turtleLiteral: string): string {
  return sqlIdentSegments(turtleLiteral).join(".");
}

/**
 * The TriplesMap local name embedded in an IRI, used as the join key.
 *
 * The dot is part of the name, not a delimiter: the RIGOR strategy mints
 * ``TriplesMap_{schema}.{table}`` for a table whose bare name another datasource
 * shares (``inducer/strategies/base.py:subject_template_names``). A ``\w+``
 * class stops at the dot, so both twins keyed on ``public`` and the second
 * overwrote the first — re-fusing in the UI exactly the two tables the mapping
 * separated. ``/`` is excluded, which is what keeps a
 * ``TriplesMap_X/POM_Y/ObjectMap`` IRI resolving to ``X``.
 */
const TRIPLES_MAP_ID = /TriplesMap_([\w.]+)/;

/**
 * TriplesMap-id join over the whole R2RML. Returns ``classLocalName →
 * tableName`` regardless of how ``rr:class`` and ``rr:tableName`` are
 * distributed across Turtle blocks. Both predicates are keyed by the
 * ``TriplesMap_X`` token found in the subject/object IRIs of the statement
 * region they appear in.
 */
export function classToTableFromR2rml(
  r2rmlTurtle: string | null | undefined,
): Map<string, string> {
  const result = new Map<string, string>();
  if (!r2rmlTurtle) return result;

  const triplesMapToClass = new Map<string, string>();
  const triplesMapToTable = new Map<string, string>();

  // Scan line by line, remembering the most recent ``TriplesMap_X`` id seen in
  // any IRI on the line; both ``rr:class`` and ``rr:tableName`` statements sit
  // under a subject/object IRI carrying that id even when they land in
  // different serialized blocks.
  let currentTriplesMap: string | null = null;
  for (const line of r2rmlTurtle.split("\n")) {
    const idMatch = line.match(TRIPLES_MAP_ID);
    if (idMatch) currentTriplesMap = idMatch[1];

    const classMatch = line.match(/rr:class\s+([^\s;,.\]]+)/);
    if (classMatch && currentTriplesMap) {
      triplesMapToClass.set(currentTriplesMap, classLocalName(classMatch[1]));
    }

    // Capture the whole Turtle string literal (allowing backslash-escaped
    // quotes) then unescape + strip SQL-delimiter quotes.
    const tableMatch = line.match(/rr:tableName\s+("(?:\\.|[^"\\])*")/);
    if (tableMatch && currentTriplesMap) {
      triplesMapToTable.set(currentTriplesMap, unquoteSqlIdent(tableMatch[1]));
    }
  }

  for (const [tmId, cls] of triplesMapToClass) {
    const table = triplesMapToTable.get(tmId);
    if (table) result.set(cls, table);
  }
  return result;
}

// ─── R2RML mapping table (per class) ────────────────────────────────
//
// The class-detail panel renders a "Mapping: {table}" section with a
// Source-column / Property / Type sub-table. The old per-class R2RML
// slice (``ProposalEntitiesPanel.r2rmlByClass`` split on blank lines and
// grabbed the block that carried ``rr:class``, then ``parseR2rml`` ran
// ``rr:tableName`` over that slice) is PERMANENTLY broken: rdflib splits
// a TriplesMap into several standalone subject statements, so the block
// carrying ``rr:class`` (the ``…/SubjectMap``) does NOT carry
// ``rr:tableName`` (a ``rr:logicalTable`` blank node on the TriplesMap
// header) and does NOT carry the ``rr:predicate`` / ``rr:column`` maps
// (separate ``…/POM_X`` + ``…/POM_X/ObjectMap`` statements). The result is
// a permanent "Mapping: Unknown" with an empty column list.
//
// The real serialization (verified against the inducer's ``build_r2rml``)
// keys every artifact by the ``TriplesMap_X`` token embedded in its
// subject IRI, and links a predicate to its column via the shared
// ``POM_Y`` token:
//
//   ind:TriplesMap_ClinicalTrial
//       rr:logicalTable [ rr:tableName "\"clinical_trial\"" ] ;
//       rr:predicateObjectMap …/POM_Title, …/POM_TrialId ;
//       rr:subjectMap …/SubjectMap .
//   …/POM_Title            rr:predicate ind:clinicalTrial_title ;
//                          rr:objectMap …/POM_Title/ObjectMap .
//   …/POM_Title/ObjectMap  rr:column "\"title\"" ; rr:datatype xsd:string .
//   …/SubjectMap           rr:class ind:ClinicalTrial ; rr:template … .
//
// So we JOIN across the WHOLE document by ``TriplesMap_X`` (table + class)
// and by ``POM_Y`` (predicate ↔ column/type). This is layout-independent
// and also handles the compact inline form (one ``<#Map>`` statement with
// nested ``[ … ]`` blank nodes) used in unit fixtures, because there the
// class/table/predicate/column all co-occur in the single statement and
// fall through to a per-statement scan.

export interface R2rmlColumnMapping {
  column: string;
  property: string;
  type: "attribute" | "relationship";
  target: string;
}

export interface R2rmlMapping {
  sourceTable: string;
  columnMappings: R2rmlColumnMapping[];
}

/** All matches of ``rr:column``/inline SQL-delimited literal in a chunk. */
function matchColumn(text: string): string | null {
  const m = text.match(/rr:column\s+("(?:\\.|[^"\\])*")/);
  return m ? unquoteSqlIdent(m[1]) : null;
}

/**
 * Parse the whole R2RML document into ``classLocalName → R2rmlMapping``.
 *
 * Two passes over the (string/IRI/bracket-aware) statement split:
 *   1. Collect per-``TriplesMap_X``: table name (``rr:tableName``), class
 *      (``rr:class``), and predicate/column maps keyed by ``POM_Y`` id.
 *   2. Reassemble each TriplesMap's column mappings by joining its POM
 *      predicates to their ObjectMap columns/datatypes/parents.
 *
 * A statement that carries ``rr:class`` AND ``rr:tableName`` AND
 * predicate-object maps inline (the compact ``<#Map>`` fixture form) is
 * handled by the same code path — its single blank-node-nested statement
 * still yields the class, the table, and (via the shared POM scan) the
 * columns.
 */
interface ObjectMapInfo {
  column: string;
  type: "attribute" | "relationship";
  target: string;
}

/**
 * Reduce an ``rr:parentTriplesMap`` target to the parent CLASS name for
 * display. ``classLocalName`` strips the ``#``/``/`` fragment + leading
 * ``prefix:`` (``ind:TriplesMap_Sponsor`` → ``TriplesMap_Sponsor``); the
 * ``TriplesMap_`` id token is the PascalCase of the parent table, which is the
 * same token the inducer uses for the class local name, so peeling the prefix
 * yields the class name (``Sponsor``) that the FK badge should render.
 */
function fkTargetClassName(parentTriplesMapToken: string): string {
  return classLocalName(parentTriplesMapToken).replace(/^TriplesMap_/, "");
}

/** Read the column/datatype/parentTriplesMap of an ObjectMap-bearing chunk. */
function readObjectMap(text: string): ObjectMapInfo | null {
  const column = matchColumn(text);
  const parentMatch = text.match(/rr:parentTriplesMap\s+([^\s;,.\]]+)/);
  const datatypeMatch = text.match(/rr:datatype\s+([^\s;,.\]]+)/);
  if (column === null && !parentMatch) return null;
  return {
    column: column ?? "",
    type: parentMatch ? "relationship" : "attribute",
    // FK target renders as the parent CLASS name (``Sponsor``), NOT the raw
    // ``TriplesMap_Sponsor`` id token. ``classLocalName`` also handles the
    // ``rr:datatype`` case (``xsd:string`` → ``string``).
    target: parentMatch
      ? fkTargetClassName(parentMatch[1])
      : datatypeMatch
        ? classLocalName(datatypeMatch[1])
        : "string",
  };
}

export function parseR2rmlByClass(
  r2rmlTurtle: string | null | undefined,
): Map<string, R2rmlMapping> {
  const result = new Map<string, R2rmlMapping>();
  if (!r2rmlTurtle) return result;

  // "Map key" = the ``TriplesMap_X`` id when present, else the statement's
  // own subject IRI (the compact inline ``<#Map>`` fixture form, where the
  // whole mapping is one self-contained statement). This unifies both
  // serializations under one keyed accumulator.
  const mapToClass = new Map<string, string>();
  const mapToTable = new Map<string, string>();
  // POM ids are only unique WITHIN a TriplesMap (both ClinicalTrial and
  // Sponsor can have a ``POM_SponsorId``), so key the split-form predicate /
  // ObjectMap accumulators by the composite ``TriplesMap_X/POM_Y`` to avoid
  // cross-TriplesMap collisions that would swap an FK relationship for a
  // sibling table's datatype column.
  const pomKeyToPredicate = new Map<string, string>();
  const pomKeyToObject = new Map<string, ObjectMapInfo>();
  // Map key → ordered composite POM keys (from the ``rr:predicateObjectMap …``
  // list, resolved against the enclosing TriplesMap).
  const mapToPomKeys = new Map<string, string[]>();
  // Map key → columns fully assembled inline (predicate + column co-located).
  const mapToInlineColumns = new Map<string, R2rmlColumnMapping[]>();

  const firstToken = (text: string): string => text.trim().split(/\s/)[0] ?? "";
  const tmIdOf = (text: string): string | null =>
    text.match(TRIPLES_MAP_ID)?.[1] ?? null;
  const pomIdOf = (text: string): string | null =>
    text.match(/POM_(\w+)/)?.[1] ?? null;

  for (const stmt of splitStatements(stripComments(r2rmlTurtle))) {
    const text = stmt.text;
    if (!text.trim() || text.trim().startsWith("@")) continue;

    const subj = firstToken(text);
    const tmId = tmIdOf(text);
    const pomId = pomIdOf(text);
    // The statement's map key: TriplesMap id if any, else the subject IRI.
    const mapKey = tmId ?? subj;
    // A …/POM_Y/ObjectMap statement carries the column; a …/POM_Y statement
    // carries the predicate. Tell them apart by the subject's own suffix so
    // an inline ``rr:objectMap [ … ]`` on a POM statement isn't misread.
    const subjectIsObjectMap = /\/ObjectMap>?$/.test(subj);

    const tableMatch = text.match(/rr:tableName\s+("(?:\\.|[^"\\])*")/);
    // Qualified here, bare in ``classToTableFromR2rml``: this value is only ever
    // DISPLAYED ("Mapping: {sourceTable}"), while that one is joined against a
    // ConceptMatch's bare table name.
    if (tableMatch) mapToTable.set(mapKey, qualifiedSqlIdent(tableMatch[1]));

    const classMatch = text.match(/rr:class\s+([^\s;,.\]]+)/);
    if (classMatch) mapToClass.set(mapKey, classLocalName(classMatch[1]));

    // Header's ordered POM references (``rr:predicateObjectMap …/POM_A, …``).
    // The header lives on the TriplesMap subject, so ``tmId`` is set and each
    // POM id resolves to its composite ``TriplesMap_X/POM_Y`` key.
    if (tmId && /rr:predicateObjectMap/.test(text)) {
      const keys: string[] = [];
      const pomRe = /POM_(\w+)/g;
      let m: RegExpExecArray | null;
      while ((m = pomRe.exec(text)) !== null) keys.push(`${tmId}/POM_${m[1]}`);
      if (keys.length) mapToPomKeys.set(mapKey, keys);
    }

    const predMatch = text.match(/rr:predicate\s+([^\s;,.\]]+)/);
    const objectMap = readObjectMap(text);

    // Compact inline form: the predicate and its column live in the SAME
    // statement (nested blank nodes). Emit a fully-assembled column mapping.
    if (predMatch && objectMap && !pomId) {
      const list = mapToInlineColumns.get(mapKey) ?? [];
      list.push({
        column: objectMap.column,
        property: classLocalName(predMatch[1]),
        type: objectMap.type,
        target: objectMap.target,
      });
      mapToInlineColumns.set(mapKey, list);
      continue;
    }

    // Split form: predicate on …/POM_Y, column on …/POM_Y/ObjectMap — key
    // each half by the composite ``TriplesMap_X/POM_Y`` so they can be
    // re-joined later without colliding across TriplesMaps.
    if (pomId && tmId) {
      const pomKey = `${tmId}/POM_${pomId}`;
      if (predMatch && !subjectIsObjectMap) {
        pomKeyToPredicate.set(pomKey, classLocalName(predMatch[1]));
      }
      if (objectMap && subjectIsObjectMap)
        pomKeyToObject.set(pomKey, objectMap);
    }
  }

  for (const [mapKey, cls] of mapToClass) {
    const columnMappings: R2rmlColumnMapping[] = [
      ...(mapToInlineColumns.get(mapKey) ?? []),
    ];
    for (const pomKey of mapToPomKeys.get(mapKey) ?? []) {
      const predicate = pomKeyToPredicate.get(pomKey);
      const obj = pomKeyToObject.get(pomKey);
      if (predicate && obj) {
        columnMappings.push({
          column: obj.column,
          property: predicate,
          type: obj.type,
          target: obj.target,
        });
      }
    }
    result.set(cls, {
      sourceTable: mapToTable.get(mapKey) ?? "Unknown",
      columnMappings,
    });
  }
  return result;
}

/**
 * Build a resolver ``classSubject → ConceptMatch | null`` for the review
 * table. Primary strategy is the R2RML-independent normalized-name match; the
 * TriplesMap-id R2RML join is the fallback for the rare cases the name
 * transform can't be inverted (e.g. characters ``to_pascal`` drops, or two
 * tables that normalize to the same key).
 *
 * Only table-level matches (empty ``source_column``) participate. Classes with
 * no match resolve to ``null`` so the caller falls through to the
 * Turtle-derived badge instead of erroring.
 *
 * Collision handling: if two source tables normalize to the same key we do NOT
 * attach either by normalized name (ambiguous — could mis-attach); such tables
 * remain reachable via the R2RML fallback, which is exact.
 */
export function buildClassMatchResolver(
  matches: ConceptMatch[],
  r2rmlTurtle: string | null | undefined,
): (classSubject: string) => ConceptMatch | null {
  const byTable = tableMatchesByTable(matches);

  // Normalized-name index. Detect collisions so we never mis-attach.
  const byNormalizedName = new Map<string, ConceptMatch>();
  const ambiguousKeys = new Set<string>();
  for (const [table, match] of byTable) {
    const key = normalizeNameKey(table);
    if (!key) continue;
    if (byNormalizedName.has(key)) {
      ambiguousKeys.add(key);
    } else {
      byNormalizedName.set(key, match);
    }
  }
  for (const key of ambiguousKeys) byNormalizedName.delete(key);

  // R2RML fallback: classLocalName → tableName (exact).
  const classToTable = classToTableFromR2rml(r2rmlTurtle);

  return (classSubject: string): ConceptMatch | null => {
    const local = classLocalName(classSubject);

    // Primary: normalized-name match (R2RML-independent).
    const byName = byNormalizedName.get(normalizeNameKey(local));
    if (byName) return byName;

    // Fallback: TriplesMap-id join → table → match (exact; handles collisions
    // and any name-transform edge cases the normalized key can't invert).
    const table = classToTable.get(local);
    if (table) return byTable.get(table) ?? null;

    return null;
  };
}
