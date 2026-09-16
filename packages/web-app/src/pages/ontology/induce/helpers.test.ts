// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi } from "vitest";
import {
  proposalScope,
  proposalScopeSortKey,
  proposalSourceTypeLabel,
  resolveConstraintConfig,
} from "./helpers";

describe("proposalSourceTypeLabel", () => {
  it("maps the source-type enum to a human label", () => {
    expect(proposalSourceTypeLabel("STRUCTURED")).toBe("Structured");
    expect(proposalSourceTypeLabel("UNSTRUCTURED")).toBe("Unstructured");
  });

  it("treats a missing source_type as structured, matching the backend's asymmetric backfill", () => {
    expect(proposalSourceTypeLabel(undefined)).toBe("Structured");
  });
});

describe("proposalScope", () => {
  it("reports table count for structured runs", () => {
    expect(proposalScope({ tables_processed: 46 }, "STRUCTURED")).toBe(
      "46 tables",
    );
  });

  it("reports class count for unstructured runs (and ignores tables_processed there)", () => {
    // Unstructured proposals carry class_count but not tables_processed.
    // Even if some pathological row had both, the source_type decides
    // which metric is meaningful — mixing them would be misleading.
    expect(
      proposalScope({ class_count: 18, tables_processed: 999 }, "UNSTRUCTURED"),
    ).toBe("18 classes");
  });

  it("pluralises correctly at 1", () => {
    expect(proposalScope({ tables_processed: 1 }, "STRUCTURED")).toBe(
      "1 table",
    );
    expect(proposalScope({ class_count: 1 }, "UNSTRUCTURED")).toBe("1 class");
  });

  it("preserves a legitimate zero", () => {
    expect(proposalScope({ tables_processed: 0 }, "STRUCTURED")).toBe(
      "0 tables",
    );
  });

  it("treats a missing source_type as structured, matching the backend's asymmetric backfill", () => {
    expect(proposalScope({ tables_processed: 12 }, undefined)).toBe(
      "12 tables",
    );
  });

  it("returns null when the relevant metric is absent, non-numeric, or metadata missing", () => {
    expect(proposalScope({}, "STRUCTURED")).toBeNull();
    expect(proposalScope({ tables_processed: "12" }, "STRUCTURED")).toBeNull();
    expect(
      proposalScope({ class_count: Number.NaN }, "UNSTRUCTURED"),
    ).toBeNull();
    expect(proposalScope(undefined, "STRUCTURED")).toBeNull();
  });

  it("rejects negative counts as corrupted data rather than rendering '-5 tables'", () => {
    expect(proposalScope({ tables_processed: -5 }, "STRUCTURED")).toBeNull();
    expect(proposalScope({ class_count: -1 }, "UNSTRUCTURED")).toBeNull();
  });
});

describe("proposalScopeSortKey", () => {
  it("returns the numeric metric for the source type", () => {
    expect(proposalScopeSortKey({ tables_processed: 46 }, "STRUCTURED")).toBe(
      46,
    );
    expect(proposalScopeSortKey({ class_count: 18 }, "UNSTRUCTURED")).toBe(18);
  });

  it("sinks rows with no metric to the bottom on ascending sort", () => {
    expect(proposalScopeSortKey({}, "STRUCTURED")).toBe(-1);
    expect(proposalScopeSortKey(undefined, "UNSTRUCTURED")).toBe(-1);
  });

  it("treats a negative count as no metric (-1), consistent with proposalScope", () => {
    expect(proposalScopeSortKey({ tables_processed: -5 }, "STRUCTURED")).toBe(
      -1,
    );
  });
});

describe("resolveConstraintConfig — the constraints out-of-band read path", () => {
  const CONFIG = { classes: [{ class_uri: "http://ex.org#A" }] };

  it("fetches from constraints_url when the response carries no inline copy", async () => {
    // The regression this guards: GET /proposals/{id} strips
    // metadata.constraint_config whenever an S3 artifact exists, and constraints
    // are offloaded for EVERY proposal that has them. Without this fetch the
    // editor silently shows no constraints and the Infer merge dedups against an
    // empty set, duplicating classes and dropping prior edits.
    const fetchJson = vi.fn().mockResolvedValue(CONFIG);

    await expect(
      resolveConstraintConfig(
        {
          metadata: { has_constraint_config: true },
          constraints_url: "s3://x",
        },
        fetchJson,
      ),
    ).resolves.toEqual(CONFIG);
    expect(fetchJson).toHaveBeenCalledWith("s3://x");
  });

  it("prefers an inline copy and skips the fetch (legacy proposals)", async () => {
    const fetchJson = vi.fn();

    await expect(
      resolveConstraintConfig(
        { metadata: { constraint_config: CONFIG }, constraints_url: null },
        fetchJson,
      ),
    ).resolves.toEqual(CONFIG);
    expect(fetchJson).not.toHaveBeenCalled();
  });

  it("returns undefined when the proposal has neither copy", async () => {
    const fetchJson = vi.fn();

    await expect(
      resolveConstraintConfig({}, fetchJson),
    ).resolves.toBeUndefined();
    expect(fetchJson).not.toHaveBeenCalled();
  });

  it("degrades to undefined on a failed fetch rather than rejecting", async () => {
    // It is awaited alongside the ontology Turtle, so a rejection would blank
    // the whole detail page over secondary review content.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchJson = vi.fn().mockRejectedValue(new Error("403 expired"));

    await expect(
      resolveConstraintConfig({ constraints_url: "s3://x" }, fetchJson),
    ).resolves.toBeUndefined();
    warn.mockRestore();
  });
});
