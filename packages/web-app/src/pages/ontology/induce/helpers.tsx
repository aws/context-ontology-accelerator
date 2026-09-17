// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import Badge from "@cloudscape-design/components/badge";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { ProposalStatus } from "@coa/control-plane-client";

export function shortId(id: string, len = 8): string {
  return id.length > len ? `${id.slice(0, len)}…` : id;
}

/**
 * Human-readable source-type label for a proposal. ``source_type`` is a
 * first-class field on the proposal (not inside ``metadata``); pre-schema-delta
 * rows lack it and are treated as structured, matching the backend's
 * asymmetric backfill in ``dynamo_store.list_proposals``.
 */
export function proposalSourceTypeLabel(
  sourceType: string | undefined,
): string {
  return sourceType === "UNSTRUCTURED" ? "Unstructured" : "Structured";
}

/**
 * A per-run size label the proposals list can show as a real disambiguator
 * that a single (often generic) ``label`` cannot provide:
 *
 *   - structured runs → tables processed (``metadata.tables_processed``,
 *     written by the catalog induction path)
 *   - unstructured runs → classes induced (``metadata.class_count``,
 *     written by the unstructured induction path; tables are not processed).
 *
 * ``null`` when nothing meaningful is recorded (e.g. an in-flight
 * ``inducing`` stub row that has no metadata yet). ``source_type`` lives
 * on the proposal itself (a first-class column, not inside ``metadata``);
 * pre-schema-delta rows lack it and are treated as structured, matching
 * the backend's asymmetric backfill in ``dynamo_store.list_proposals``.
 */
export function proposalScope(
  metadata: Record<string, unknown> | undefined,
  sourceType: string | undefined,
): string | null {
  const isUnstructured = sourceType === "UNSTRUCTURED";
  const key = isUnstructured ? "class_count" : "tables_processed";
  const value = metadata?.[key];
  // Reject NaN/Infinity AND negatives — a negative count is corrupted data,
  // never a real "N tables"/"N classes". Treated as "no metric" ("—").
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0)
    return null;
  const unit = isUnstructured
    ? value === 1
      ? "class"
      : "classes"
    : value === 1
      ? "table"
      : "tables";
  return `${value} ${unit}`;
}

/**
 * Numeric sort key for the scope column. Returns the raw
 * ``tables_processed`` (structured) or ``class_count`` (unstructured), or
 * ``-1`` for rows with nothing to show (sink-to-bottom on ascending sort).
 */
export function proposalScopeSortKey(
  metadata: Record<string, unknown> | undefined,
  sourceType: string | undefined,
): number {
  const key =
    sourceType === "UNSTRUCTURED" ? "class_count" : "tables_processed";
  const value = metadata?.[key];
  // Mirror proposalScope: negatives are corrupted data → treat as "no metric"
  // (-1) so they sink to the bottom on ascending sort rather than sorting below
  // a legitimate 0.
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : -1;
}

/**
 * Proposal statuses that mean an accept is in flight: a worker is (or was)
 * running, so Accept must stay disabled and the detail page should keep polling.
 * Mirrors the backend's ``_ACCEPT_IN_PROGRESS_STATUSES`` (proposals.py).
 *
 * Values come from the Smithy-generated ``ProposalStatus`` — the API contract is
 * the single source of truth, so a wire value can't drift from what the UI
 * compares against. Single definition on purpose: these were previously compared
 * as inline literals at three separate sites, and missing the Accept-button check
 * would re-enable Accept during a live accept.
 */
export const PROPOSAL_IN_PROGRESS_STATUSES: ReadonlySet<string> = new Set([
  ProposalStatus.ACCEPTING,
  ProposalStatus.EMBEDDINGS_SYNC,
]);

export function isProposalAcceptInProgress(status: string): boolean {
  return PROPOSAL_IN_PROGRESS_STATUSES.has(status);
}

/**
 * Has an in-flight accept reached a terminal FAILURE the poll loop should stop
 * on? (#456/#466/#467)
 *
 *   - ``accept_failed`` — the worker's terminal failure state: a pipeline step
 *     exhausted its retries. Always carries ``accept_error`` naming the step.
 *   - ``pending`` + ``accept_error`` — the LEGACY failure shape (the worker used
 *     to roll back to ``pending``). Still honoured so a proposal that failed
 *     under an older deployment is reported rather than polled to timeout.
 *   - bare ``pending`` (no error) is NOT a failure: it's the initial state of
 *     every fresh proposal, seen while the ``accepting`` flip propagates.
 *
 * Pure helper so the decision is unit-testable without rendering ProposalDetail
 * under jsdom fake timers.
 */
export function isAcceptTerminalFailure(proposal: {
  status: string;
  accept_error?: string;
}): boolean {
  if (proposal.status === ProposalStatus.ACCEPT_FAILED) return true;
  return (
    proposal.status === ProposalStatus.PENDING && Boolean(proposal.accept_error)
  );
}

/**
 * Which copy of a proposal's SHACL constraint config to use.
 *
 * ``GET /proposals/{id}`` serves the config out-of-band via a presigned
 * ``constraints_url`` once it has been offloaded to S3, because a wide schema's
 * config (one entry per class, one per constrained property) can exceed the
 * 6 MB API Gateway / Lambda response cap on its own. The inline
 * ``metadata.constraint_config`` is still present for legacy proposals that
 * never offloaded, and it wins when present — same inline-else-URL order the
 * Turtle and grounding-match artifacts use.
 *
 * ``undefined`` means "no config to show", which covers both a proposal with no
 * constraints and a failed S3 fetch. A fetch failure must NOT reject: this is
 * awaited alongside the ontology Turtle, so throwing would blank the whole page
 * over secondary review content. Retrying refresh re-presigns and recovers.
 *
 * ``fetchJson`` is injected so the decision is unit-testable without rendering
 * ProposalDetail (mirrors ``isAcceptTerminalFailure``).
 */
export async function resolveConstraintConfig(
  proposal: {
    metadata?: Record<string, unknown>;
    constraints_url?: string | null;
  },
  fetchJson: (url: string) => Promise<unknown>,
): Promise<unknown> {
  const inline = proposal.metadata?.constraint_config;
  if (inline !== undefined) return inline;
  if (!proposal.constraints_url) return undefined;
  try {
    return await fetchJson(proposal.constraints_url);
  } catch (e) {
    console.warn("Failed to load constraint config:", e);
    return undefined;
  }
}

/**
 * Does this proposal block starting a NEW induction for its ontology?
 *
 * Must match the backend guard (``PROPOSAL_STATUSES_BLOCKING_NEW_INDUCTION`` in
 * proposals.py) or the button stays enabled and the request 409s. Two kinds of
 * "not finished with this one":
 *
 *   - unreviewed work: ``inducing`` (still being produced), ``pending`` /
 *     ``updated`` / ``accept_failed`` (awaiting review). ``accept_failed`` counts
 *     because before that state existed a failed accept rolled back to
 *     ``pending``, which blocked.
 *   - an accept actively merging: ``accepting`` / ``embeddings_sync``. The
 *     backend's induction lock only spans an INDUCTION, so without these a new
 *     induction can start mid-merge and leave two competing proposals.
 */
export const PROPOSAL_BLOCKS_NEW_INDUCTION_STATUSES: ReadonlySet<string> =
  new Set([
    ProposalStatus.INDUCING,
    ProposalStatus.PENDING,
    ProposalStatus.UPDATED,
    ProposalStatus.ACCEPT_FAILED,
    ...PROPOSAL_IN_PROGRESS_STATUSES,
  ]);

export function proposalBlocksNewInduction(status: string): boolean {
  return PROPOSAL_BLOCKS_NEW_INDUCTION_STATUSES.has(status);
}

export function proposalStatusIndicator(status: string) {
  switch (status) {
    case "pending":
      return <StatusIndicator type="pending">Pending</StatusIndicator>;
    case "accepting":
      return <StatusIndicator type="in-progress">Accepting…</StatusIndicator>;
    case "embeddings_sync":
      return (
        <StatusIndicator type="in-progress">
          Syncing embeddings…
        </StatusIndicator>
      );
    case "accepted":
      return <StatusIndicator type="success">Accepted</StatusIndicator>;
    // An accept whose pipeline step exhausted its retries. The proposal is
    // still re-acceptable; ``accept_error`` names the failing step.
    case "accept_failed":
      return <StatusIndicator type="error">Accept failed</StatusIndicator>;
    case "rejected":
      return <StatusIndicator type="error">Rejected</StatusIndicator>;
    case "cancelled":
      return <StatusIndicator type="stopped">Cancelled</StatusIndicator>;
    case "updated":
      return <StatusIndicator type="in-progress">Updated</StatusIndicator>;
    default:
      return <Badge>{status}</Badge>;
  }
}
