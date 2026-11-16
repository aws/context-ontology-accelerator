// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Suggested-subclass panel shown by ClassDetailCard and ClassDetailPanel.
 * Confirm promotes ``coa:suggestedSubClassOf`` to ``rdfs:subClassOf``, Reject
 * drops it, Defer dismisses the panel for this session.
 *
 * The Turtle is edited as text (statement splicing) because shipping rdflib.js
 * to the browser is not an option.
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
  /** Receives the rewritten Turtle after a confirm or reject. */
  onTurtleEdit: (turtle: string) => void;
  /** Maps a class IRI to a display label. */
  labelOf: (iri: string) => string;
}

export function SuggestionPanel({ iri, turtle, onTurtleEdit, labelOf }: Props) {
  const { t } = useAppI18n();
  const [deferred, setDeferred] = useState<Set<string>>(new Set());
  const [batchPrompt, setBatchPrompt] = useState<{
    parent: string;
    pairs: SuggestedPair[];
  } | null>(null);

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
 * Promote a suggestion to a real ``rdfs:subClassOf`` edge tagged
 * ``coa:subClassProvenance "USER_CONFIRMED"``. Applying the same pair twice
 * appends a duplicate ``rdfs:subClassOf`` statement.
 */
export function applyConfirm(turtle: string, pairs: SuggestedPair[]): string {
  let out = turtle;
  for (const { child, parent } of pairs) {
    out = stripSuggestion(out, child, parent);
  }
  // Reuse the proposal's COA prefix, else declare one.
  const prefixes = parsePrefixes(out);
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
 * Why surgical instead of parse-rebuild: the lexer flattens ``owl:hasKey``
 * lists, so a parse-and-re-emit would lose them. Splicing the original bytes
 * keeps everything else identical.
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

  for (const stmt of stmts) {
    const trimmed = stmt.text.trimStart();
    if (!trimmed || trimmed.startsWith("@")) continue;
    const subjEnd = nextTokenEnd(trimmed, 0);
    if (subjEnd <= 0) continue;
    const subjectIri = expandIri(trimmed.slice(0, subjEnd), prefixes);
    if (subjectIri !== child) continue;

    const rewritten = stripPredicatesFromStatement(
      turtle.slice(stmt.startOffset, stmt.endOffset),
      prefixes,
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
 * Remove the ``coa:suggestedSubClassOf <parent>`` pair from one statement, plus
 * the ``coa:suggestionReason`` pair directly after it. Returns null when
 * nothing matched.
 */
function stripPredicatesFromStatement(
  source: string,
  prefixes: Record<string, string>,
  parent: string,
): string | null {
  const dotIdx = source.lastIndexOf(".");
  if (dotIdx < 0) return null;
  const head = source.slice(0, dotIdx);
  const tail = source.slice(dotIdx); // includes the dot + any trailing whitespace

  const trimmed = head.trimStart();
  const leadingWs = head.slice(0, head.length - trimmed.length);
  const subjEnd = nextTokenEnd(trimmed, 0);
  if (subjEnd <= 0) return null;
  const subjectText = trimmed.slice(0, subjEnd);
  const restAfterSubject = trimmed.slice(subjEnd);

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

  const surviving = pairs.filter((_, i) => !dropFlags[i]);
  if (surviving.length === 0) {
    return ""; // caller's surrounding whitespace covers the gap
  }
  // splitOnSemicolons kept each pair's original surrounding whitespace, so
  // re-joining on ";" reproduces the source layout of the surviving pairs.
  const body = surviving.join(";").trimEnd();
  return leadingWs + subjectText + body + tail;
}
