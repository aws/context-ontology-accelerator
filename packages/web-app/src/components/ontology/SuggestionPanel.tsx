// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * SuggestionPanel — renders inside ClassDetailCard when the selected
 * class has any ``coa:suggestedSubClassOf`` annotations from the
 * inducer's single-child PK-sharing detection.
 *
 * For each suggested parent the panel shows the proposed inheritance,
 * the suggestion's reason, and three actions:
 *
 *   * Confirm — promotes the suggestion to a real ``rdfs:subClassOf``,
 *     drops the ``coa:suggestedSubClassOf`` + ``coa:suggestionReason``
 *     annotations, and tags the new edge with
 *     ``coa:subClassProvenance "USER_CONFIRMED"``. Marks the proposal
 *     dirty so the existing Save flow persists it via updateProposal.
 *   * Reject — drops the soft annotations, leaving the FK
 *     ObjectProperty (the relation stays as has-a).
 *   * Defer — dismisses the panel for this session without rewriting
 *     the proposal Turtle.
 *
 * The Turtle rewrite is intentionally text-based: parse-rewrite-
 * serialise via rdflib in the browser would mean shipping rdflib.js
 * to clients, and the proposal Turtle is small enough (<200 KB) that
 * a line-oriented edit is fine. Each triple the inducer emits looks
 * like ``<child> coa:suggestedSubClassOf <parent>`` (sometimes inside
 * a multi-predicate block on the child); we strip the predicate-
 * object pair and append the replacement triples as a separate
 * statement at the bottom of the file. rdflib normalises on the next
 * Save round-trip.
 */
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  Header,
  SpaceBetween,
} from "@cloudscape-design/components";
import { useMemo, useState } from "react";
import {
  SCL_NS,
  SCL_SUGGESTED_SUBCLASS_OF,
  SCL_SUGGESTION_REASON,
  SCL_SUBCLASS_PROVENANCE,
  expandIri,
  literalText,
  localName,
  nextTokenEnd,
  parsePrefixes,
  parseTriples,
  splitOnSemicolons,
  splitStatements,
} from "../../utils/turtleLexer";
import { useAppI18n } from "../../i18n/LocaleProvider";

export interface SuggestedPair {
  child: string;
  parent: string;
  reason?: string;
}

interface Props {
  /** IRI of the currently selected class. */
  iri: string;
  /** Current proposal Turtle, possibly with unsaved edits. */
  turtle: string;
  /** Called when the user confirms or rejects a suggestion — passes
   *  the new Turtle back to the parent so the existing dirty-state
   *  + Save flow can persist it. */
  onTurtleEdit: (turtle: string) => void;
  /** Map a class IRI to a human label (passes through ``localName``
   *  when the proposal has no rdfs:label). */
  labelOf: (iri: string) => string;
}

export function SuggestionPanel({ iri, turtle, onTurtleEdit, labelOf }: Props) {
  const { t } = useAppI18n();
  const [deferred, setDeferred] = useState<Set<string>>(new Set());
  const [batchPrompt, setBatchPrompt] = useState<{
    parent: string;
    pairs: SuggestedPair[];
  } | null>(null);

  // Parse out the suggestions for this class. Memoised on turtle so a
  // confirm/reject re-runs the parser with the rewritten Turtle.
  const suggestions = useMemo<SuggestedPair[]>(() => {
    const { triples } = parseTriples(turtle);
    const pairs: SuggestedPair[] = [];
    const reasons: Record<string, string> = {};
    for (const { s, p, o } of triples) {
      if (p === SCL_SUGGESTION_REASON && o.startsWith('"')) {
        reasons[s] = literalText(o);
      }
    }
    for (const { s, p, o } of triples) {
      if (s === iri && p === SCL_SUGGESTED_SUBCLASS_OF) {
        pairs.push({ child: s, parent: o, reason: reasons[s] });
      }
    }
    return pairs;
  }, [turtle, iri]);

  if (suggestions.length === 0 && batchPrompt === null) return null;

  const handleConfirm = (pair: SuggestedPair) => {
    const updated = applyConfirm(turtle, [pair]);
    onTurtleEdit(updated);
    // Look for other suggestions sharing this parent — surface as a
    // batch prompt so the user can confirm them all in one click.
    const peers = findSuggestionsWithParent(updated, pair.parent);
    if (peers.length > 0) {
      setBatchPrompt({ parent: pair.parent, pairs: peers });
    }
  };

  const handleReject = (pair: SuggestedPair) => {
    const updated = applyReject(turtle, [pair]);
    onTurtleEdit(updated);
  };

  const handleDefer = (pair: SuggestedPair) => {
    setDeferred((prev) => {
      const next = new Set(prev);
      next.add(pair.parent);
      return next;
    });
  };

  const handleBatchApply = () => {
    if (!batchPrompt) return;
    onTurtleEdit(applyConfirm(turtle, batchPrompt.pairs));
    setBatchPrompt(null);
  };

  return (
    <Container
      header={<Header variant="h3">{t("Suggested inheritance")}</Header>}
      data-testid="suggestion-panel"
    >
      <SpaceBetween size="s">
        {suggestions
          .filter((p) => !deferred.has(p.parent))
          .map((pair) => (
            <Alert
              key={`${pair.child}-${pair.parent}`}
              type="info"
              data-testid={`suggestion-${localName(pair.parent)}`}
              header={t("Looks like {child} could be a subclass of {parent}", {
                child: labelOf(pair.child),
                parent: labelOf(pair.parent),
              })}
            >
              <SpaceBetween size="xs">
                <Box variant="small">
                  {t(
                    "Auto-detected from PK-sharing — child and parent share the same primary-key column. Confirm to promote to rdfs:subClassOf, reject to leave the FK as a regular has-a relation.",
                  )}
                </Box>
                {pair.reason && (
                  <Box>
                    <Badge>{pair.reason}</Badge>
                  </Box>
                )}
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="primary"
                    onClick={() => handleConfirm(pair)}
                    data-testid={`confirm-${localName(pair.parent)}`}
                  >
                    {t("Confirm")}
                  </Button>
                  <Button onClick={() => handleReject(pair)}>
                    {t("Reject")}
                  </Button>
                  <Button variant="link" onClick={() => handleDefer(pair)}>
                    {t("Defer")}
                  </Button>
                </SpaceBetween>
              </SpaceBetween>
            </Alert>
          ))}
        {batchPrompt && (
          <Alert
            type="success"
            header={t(
              batchPrompt.pairs.length === 1
                ? "Found {count} similar pending suggestion on {parent}"
                : "Found {count} similar pending suggestions on {parent}",
              {
                count: batchPrompt.pairs.length,
                parent: labelOf(batchPrompt.parent),
              },
            )}
            data-testid="batch-suggestion-card"
          >
            <SpaceBetween size="xs">
              <Box variant="small">
                {t(
                  "{children} share the same parent. Apply the same confirm to all?",
                  {
                    children: batchPrompt.pairs
                      .map((p) => labelOf(p.child))
                      .join(", "),
                  },
                )}
              </Box>
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="primary"
                  onClick={handleBatchApply}
                  data-testid="batch-apply-all"
                >
                  {t("Apply all {count}", {
                    count: batchPrompt.pairs.length,
                  })}
                </Button>
                <Button onClick={() => setBatchPrompt(null)}>
                  {t("Review individually")}
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Alert>
        )}
      </SpaceBetween>
    </Container>
  );
}

/**
 * Find every ``coa:suggestedSubClassOf`` triple whose object is the
 * given parent IRI. Used by the batch-confirm flow.
 */
export function findSuggestionsWithParent(
  turtle: string,
  parent: string,
): SuggestedPair[] {
  const { triples } = parseTriples(turtle);
  const reasons: Record<string, string> = {};
  for (const { s, p, o } of triples) {
    if (p === SCL_SUGGESTION_REASON && o.startsWith('"')) {
      reasons[s] = literalText(o);
    }
  }
  return triples
    .filter((t) => t.p === SCL_SUGGESTED_SUBCLASS_OF && t.o === parent)
    .map((t) => ({ child: t.s, parent: t.o, reason: reasons[t.s] }));
}

/**
 * Promote a suggestion to a real ``rdfs:subClassOf`` edge.
 *
 *   1. Drop the ``coa:suggestedSubClassOf <parent>`` predicate from the
 *      child's predicate-object block.
 *   2. Drop the ``coa:suggestionReason "..."`` predicate from the
 *      child's predicate-object block.
 *   3. Append a fresh statement: ``<child> rdfs:subClassOf <parent> ;
 *      coa:subClassProvenance "USER_CONFIRMED" .``
 *
 * The text edit is line-oriented and idempotent — applying the same
 * pair twice no-ops the first round (nothing left to drop) but still
 * appends the new triple. Save → re-load via rdflib normalises that.
 */
export function applyConfirm(turtle: string, pairs: SuggestedPair[]): string {
  let out = turtle;
  for (const { child, parent } of pairs) {
    out = stripSuggestion(out, child, parent);
  }
  // Append the confirmed-edge statements.
  const prefixes = parsePrefixes(out);
  // Use the COA prefix the proposal already declares — fallback to
  // adding our own if the proposal doesn't have one (rare; the
  // inducer always emits it).
  const sclPrefix =
    Object.entries(prefixes).find(([, base]) => base === SCL_NS)?.[0] ?? "coa";
  const ensurePrefixDecl = !prefixes[sclPrefix]
    ? `@prefix ${sclPrefix}: <${SCL_NS}> .\n`
    : "";
  const additions = pairs
    .map(
      ({ child, parent }) =>
        `<${child}> <http://www.w3.org/2000/01/rdf-schema#subClassOf> <${parent}> ;\n  ${sclPrefix}:subClassProvenance "USER_CONFIRMED" .\n`,
    )
    .join("");
  return ensurePrefixDecl + out + (out.endsWith("\n") ? "" : "\n") + additions;
}

/**
 * Drop the ``coa:suggestedSubClassOf`` + ``coa:suggestionReason``
 * annotations for one or more pairs without adding rdfs:subClassOf.
 * Leaves the regular FK ObjectProperty in place — the user explicitly
 * said this is not inheritance.
 */
export function applyReject(turtle: string, pairs: SuggestedPair[]): string {
  let out = turtle;
  for (const { child, parent } of pairs) {
    out = stripSuggestion(out, child, parent);
  }
  return out;
}

/**
 * Strip the ``coa:suggestedSubClassOf <parent>`` and matching
 * ``coa:suggestionReason`` predicate-object pairs from the child's
 * statement, leaving every other byte of the document untouched.
 *
 * Why surgical instead of parse-rebuild: the previous implementation
 * round-tripped through ``parseTriples`` then re-emitted one triple
 * per line. That destroyed ``owl:hasKey`` lists (the lexer flattens
 * them via a synthetic ``__hasKeyList__`` predicate that re-emit
 * cannot represent), normalised whitespace, dropped multi-predicate
 * blocks, and made every confirm/reject change the file in ways the
 * user didn't ask for. Surgical splice on the original bytes keeps
 * everything else byte-identical.
 *
 * Only the ``coa:suggestionReason`` paired with the targeted parent
 * is removed — a child with multiple suggested parents (rare today
 * but supported by the schema) keeps its other reasons intact.
 */
function stripSuggestion(
  turtle: string,
  child: string,
  parent: string,
): string {
  const prefixes = parsePrefixes(turtle);
  const stmts = splitStatements(turtle);

  // Locate the statement whose subject is the child class. Match by
  // expanding the leading token through the document's prefix table
  // so both ``ind:Child`` and ``<...child>`` shapes are recognised.
  for (const stmt of stmts) {
    const trimmed = stmt.text.trimStart();
    if (!trimmed || trimmed.startsWith("@")) continue;
    const subjEnd = nextTokenEnd(trimmed, 0);
    if (subjEnd <= 0) continue;
    const subjectIri = expandIri(trimmed.slice(0, subjEnd), prefixes);
    if (subjectIri !== child) continue;

    // Found the child's statement. Walk its predicate-object pairs
    // and drop the ones we need to remove. Splice the rewritten
    // statement back into the original document at its byte offset.
    const rewritten = stripPredicatesFromStatement(
      turtle.slice(stmt.startOffset, stmt.endOffset),
      prefixes,
      child,
      parent,
    );
    if (rewritten === null) continue;
    return (
      turtle.slice(0, stmt.startOffset) +
      rewritten +
      turtle.slice(stmt.endOffset)
    );
  }
  return turtle;
}

/**
 * Take a single statement's source text (subject + ``;``-separated
 * predicate-object pairs + trailing ``.``) and remove just the
 * ``coa:suggestedSubClassOf <parent>`` predicate-object pair plus any
 * ``coa:suggestionReason`` paired with it.
 *
 * "Paired with" here means: the reason triple sits next to the
 * suggestion triple in the same multi-predicate block. We're
 * conservative — when the child has multiple suggested parents we
 * remove only the suggestion + the *one* reason adjacent to it.
 *
 * Returns null when the statement has no targeted predicate to drop
 * (which means the caller can keep walking).
 */
function stripPredicatesFromStatement(
  source: string,
  prefixes: Record<string, string>,
  child: string,
  parent: string,
): string | null {
  // Split off the trailing ``.`` so we can rewrite the body and put
  // the terminator back. The terminator might have whitespace before
  // it; preserve that exactly.
  const dotIdx = source.lastIndexOf(".");
  if (dotIdx < 0) return null;
  const head = source.slice(0, dotIdx);
  const tail = source.slice(dotIdx); // includes the dot + any trailing whitespace

  // Re-locate the subject token within head.
  const trimmed = head.trimStart();
  const leadingWs = head.slice(0, head.length - trimmed.length);
  const subjEnd = nextTokenEnd(trimmed, 0);
  if (subjEnd <= 0) return null;
  const subjectIri = expandIri(trimmed.slice(0, subjEnd), prefixes);
  if (subjectIri !== child) return null;
  const subjectText = trimmed.slice(0, subjEnd);
  const restAfterSubject = trimmed.slice(subjEnd);

  // Walk the predicate-object pairs. We track which pair (by index)
  // is the suggestion targeting this parent and which (if any)
  // immediately-following reason pair needs to come out with it.
  const pairs = splitOnSemicolons(restAfterSubject);
  const dropFlags = pairs.map(() => false);
  let droppedSomething = false;

  for (let i = 0; i < pairs.length; i++) {
    const pair = pairs[i].trim();
    if (!pair) continue;
    const predEnd = nextTokenEnd(pair, 0);
    if (predEnd <= 0) continue;
    const predIri = expandIri(pair.slice(0, predEnd), prefixes);
    if (!predIri) continue;
    const objText = pair.slice(predEnd).trim();

    if (predIri === SCL_SUGGESTED_SUBCLASS_OF) {
      const objEnd = nextTokenEnd(objText, 0);
      if (objEnd <= 0) continue;
      const objIri = expandIri(objText.slice(0, objEnd), prefixes);
      if (objIri === parent) {
        dropFlags[i] = true;
        droppedSomething = true;
        // If the *next* non-empty pair is a suggestionReason, drop
        // it too — that's the reason adjacent to this suggestion.
        for (let j = i + 1; j < pairs.length; j++) {
          const peer = pairs[j].trim();
          if (!peer) continue;
          const peerEnd = nextTokenEnd(peer, 0);
          if (peerEnd <= 0) break;
          const peerPred = expandIri(peer.slice(0, peerEnd), prefixes);
          if (peerPred === SCL_SUGGESTION_REASON) {
            dropFlags[j] = true;
          }
          break;
        }
      }
    }
  }

  if (!droppedSomething) return null;

  // Reassemble. Keep only pairs that survived the filter.
  const surviving = pairs.filter((_, i) => !dropFlags[i]);
  // splitOnSemicolons returns the head pair without a leading
  // separator; joining with ` ;` reproduces the original shape, but
  // we need to be careful when the surviving body starts empty
  // (every predicate dropped — extremely rare for this codepath
  // because the child class would have to consist solely of
  // suggestion + reason, which the inducer never emits — but handle
  // it gracefully by removing the whole statement).
  if (surviving.length === 0) {
    return ""; // caller's surrounding whitespace covers the gap
  }
  // splitOnSemicolons kept each pair's original surrounding whitespace, so
  // re-joining on ";" reproduces the source layout of the surviving pairs.
  const body = surviving.join(";").trimEnd();
  return leadingWs + subjectText + body + tail;
}

// Re-export the lexer constant so the test file can use it.
export { SCL_SUBCLASS_PROVENANCE };
