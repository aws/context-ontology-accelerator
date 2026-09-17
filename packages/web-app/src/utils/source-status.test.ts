// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { sourceStatusLabel, sourceStatusType } from "./source-status";

describe("sourceStatusLabel", () => {
  it("renders a re-scan review as prose, not the raw status", () => {
    // Both functions fall through to a default, so a status missing a case is
    // not a compile error: it silently shows the raw enum name and a neutral
    // indicator. Only a test catches that.
    expect(sourceStatusLabel("RESCAN_REVIEW")).toBe("Re-scan review");
  });

  it.each([
    ["APPROVED", "Approved"],
    ["PENDING_REVIEW", "Pending review"],
    ["SCAN_FAILED", "Scan failed"],
  ])("keeps rendering %s as %s", (status, label) => {
    expect(sourceStatusLabel(status)).toBe(label);
  });

  it("falls back to the raw value for an unknown status, and an em dash for none", () => {
    expect(sourceStatusLabel("SOMETHING_NEW")).toBe("SOMETHING_NEW");
    expect(sourceStatusLabel(undefined)).toBe("—");
  });
});

describe("sourceStatusType", () => {
  it("treats a re-scan review like a first-scan review", () => {
    // Both are "waiting on a steward", so they get the same indicator rather
    // than the neutral info default.
    expect(sourceStatusType("RESCAN_REVIEW")).toBe("warning");
    expect(sourceStatusType("RESCAN_REVIEW")).toBe(
      sourceStatusType("PENDING_REVIEW"),
    );
  });

  it.each([
    ["APPROVED", "success"],
    ["SCAN_FAILED", "error"],
    ["SCANNING", "in-progress"],
    ["REGISTERED", "pending"],
    ["REJECTED", "stopped"],
  ])("keeps mapping %s to %s", (status, type) => {
    expect(sourceStatusType(status)).toBe(type);
  });

  it("falls back to info for an unknown status", () => {
    expect(sourceStatusType("SOMETHING_NEW")).toBe("info");
  });
});
