// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";

import {
  extractOffenders,
  groupOffenders,
  proposalHasComputedOntology,
  proposalIsReadOnly,
} from "./ProposalDetail";
import {
  isAcceptTerminalFailure,
  isProposalAcceptInProgress,
  proposalBlocksNewInduction,
} from "./helpers";
import type { ProposalValidationFinding } from "../../../services/ontology-engine";

describe("isProposalAcceptInProgress — gates Accept + the detail-page poll", () => {
  it("is true only for the in-flight accept states", () => {
    expect(isProposalAcceptInProgress("accepting")).toBe(true);
    expect(isProposalAcceptInProgress("embeddings_sync")).toBe(true);
  });

  it("is false for fresh, terminal and failed states", () => {
    // accept_failed in particular must be FALSE so the Accept button
    // re-enables and the user can retry.
    for (const s of [
      "pending",
      "updated",
      "accepted",
      "rejected",
      "cancelled",
      "accept_failed",
      "inducing",
      "failed",
      "",
    ]) {
      expect(isProposalAcceptInProgress(s)).toBe(false);
    }
  });
});

describe("proposalBlocksNewInduction — the Start-induction lock", () => {
  it("blocks on unreviewed work, including accept_failed", () => {
    // accept_failed must block: the backend lock (induce_catalog.py) 409s on it,
    // so leaving it out enables a button whose request always fails.
    for (const s of ["inducing", "pending", "updated", "accept_failed"]) {
      expect(proposalBlocksNewInduction(s)).toBe(true);
    }
  });

  it("blocks while an accept is actively merging", () => {
    // The backend induction lock only spans an INDUCTION, not an accept, so
    // these have to block here too or a new induction can start mid-merge and
    // leave two competing structured proposals in the namespace.
    for (const s of ["accepting", "embeddings_sync"]) {
      expect(proposalBlocksNewInduction(s)).toBe(true);
    }
  });

  it("does not block on resolved states", () => {
    for (const s of ["accepted", "rejected", "cancelled", "failed", ""]) {
      expect(proposalBlocksNewInduction(s)).toBe(false);
    }
  });
});

describe("isAcceptTerminalFailure — when the accept poll stops on a failure", () => {
  it("treats accept_failed as terminal (the worker's failure state)", () => {
    // A step that exhausted its retries. Without this the poll spins until
    // timeout instead of showing the error.
    expect(
      isAcceptTerminalFailure({
        status: "accept_failed",
        accept_error: "Step 's3_persist' failed: ...",
      }),
    ).toBe(true);
  });

  it("treats accept_failed as terminal even if accept_error is missing", () => {
    expect(isAcceptTerminalFailure({ status: "accept_failed" })).toBe(true);
  });

  it("treats legacy pending + accept_error as terminal", () => {
    // Proposals that failed under the pre-accept_failed worker.
    expect(
      isAcceptTerminalFailure({ status: "pending", accept_error: "GSP 503" }),
    ).toBe(true);
  });

  it("does NOT treat bare pending as a failure (fresh proposal / flip lag)", () => {
    expect(isAcceptTerminalFailure({ status: "pending" })).toBe(false);
    expect(
      isAcceptTerminalFailure({ status: "pending", accept_error: "" }),
    ).toBe(false);
  });

  it("does NOT treat in-progress or success states as a failure", () => {
    for (const status of ["accepting", "embeddings_sync", "accepted"]) {
      expect(isAcceptTerminalFailure({ status })).toBe(false);
    }
  });
});

describe("proposalHasComputedOntology — gates the SHACL / infer-constraints panel", () => {
  // Statuses with a computed ontology → panel shown (accepted/rejected/
  // cancelled show it read-only via isReadOnly; pending/updated allow editing).
  it.each([
    "pending",
    "updated",
    "accepting",
    "embeddings_sync",
    "accepted",
    // A failed accept still has a computed ontology; the user needs it to
    // diagnose the failing step and re-accept.
    "accept_failed",
    "rejected",
    "cancelled",
    "", // unknown/absent status → default to showing (don't hide real data)
  ])("shows the panel for %s (has a computed ontology)", (status) => {
    expect(proposalHasComputedOntology(status)).toBe(true);
  });

  // Only the pre-computation states hide it — nothing exists to show yet.
  it.each([
    "inducing", // in-progress stub written at trigger time
    "failed", // induction errored; no ontology produced
  ])("hides the panel for %s (no ontology yet)", (status) => {
    expect(proposalHasComputedOntology(status)).toBe(false);
  });
});

describe("proposalIsReadOnly — statuses that disable every edit + accept control", () => {
  // Terminal statuses have no more transitions, and "inducing" has no ontology
  // to act on yet. Both disable every edit + accept control on the page.
  it.each([
    "accepted", // already merged — no more edits
    "rejected", // explicitly rejected — terminal
    "cancelled", // explicitly cancelled — terminal
    "failed", // induction errored; no ontology to accept
    "inducing", // worker still running; ontology not ready yet
  ])("disables edits for %s", (status) => {
    expect(proposalIsReadOnly(status)).toBe(true);
  });

  // These statuses have a proposal a user can still act on. "accept_failed"
  // keeps its ontology, so the Accept button must re-enable.
  it.each([
    "pending", // awaiting review
    "updated", // edits saved, not yet accepted
    "accept_failed", // accept failed but re-acceptable
    "accepting", // in-flight accept — the button is gated separately by isAccepting
    "embeddings_sync", // in-flight accept, later step — same
    "", // unknown/absent status defaults to editable (matches proposalHasComputedOntology)
  ])("keeps edits enabled for %s", (status) => {
    expect(proposalIsReadOnly(status)).toBe(false);
  });
});

const XSD = "http://www.w3.org/2001/XMLSchema#";

function datatypeFinding(offenders: unknown): ProposalValidationFinding {
  return {
    validator: "datatype_token",
    tier: "tier1_blocking",
    severity: "warning",
    code: "MALFORMED_DATATYPE_TOKENS",
    message: "…",
    details: { offenders },
  };
}

describe("extractOffenders — narrows the untyped finding payload safely", () => {
  it("returns the well-formed offenders", () => {
    const out = extractOffenders(
      datatypeFinding([
        {
          subject: "http://x#p",
          predicate: `${XSD}range`,
          found: `${XSD}datetime`,
          canonical: `${XSD}dateTime`,
        },
      ]),
    );
    expect(out).toHaveLength(1);
    expect(out[0].canonical).toBe(`${XSD}dateTime`);
  });

  it("drops malformed entries and tolerates non-array/absent details", () => {
    expect(extractOffenders(datatypeFinding([{ subject: 1 }]))).toEqual([]);
    expect(extractOffenders(datatypeFinding("nope"))).toEqual([]);
    expect(extractOffenders(undefined)).toEqual([]);
  });
});

describe("groupOffenders — collapses N triples into distinct fixes", () => {
  it("groups by (found→canonical) with an affected count", () => {
    const groups = groupOffenders([
      {
        subject: "a",
        predicate: "r",
        found: `${XSD}datetime`,
        canonical: `${XSD}dateTime`,
      },
      {
        subject: "b",
        predicate: "r",
        found: `${XSD}datetime`,
        canonical: `${XSD}dateTime`,
      },
      {
        subject: "c",
        predicate: "r",
        found: `${XSD}varchar`,
        canonical: `${XSD}string`,
      },
    ]);
    expect(groups).toHaveLength(2);
    const dt = groups.find((g) => g.found === `${XSD}datetime`);
    expect(dt?.affected).toBe(2);
    // A case-only difference reads as a capitalization fix; an alias reads as
    // a not-a-valid-type fix.
    expect(dt?.reason).toBe("non-standard capitalization");
    const vc = groups.find((g) => g.found === `${XSD}varchar`);
    expect(vc?.reason).toBe("not a valid XSD datatype name");
  });
});
