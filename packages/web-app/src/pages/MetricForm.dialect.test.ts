// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { normalizeLoadedDialect } from "./MetricForm";

// A loaded dialect can be mis-cased, and the Select matches options case-sensitively,
// so a mis-cased value would fall back to the first option.
describe("normalizeLoadedDialect", () => {
  it("upper-cases a mis-cased known dialect so it matches its option", () => {
    expect(normalizeLoadedDialect("redshift")).toBe("REDSHIFT");
    expect(normalizeLoadedDialect("Redshift")).toBe("REDSHIFT");
    expect(normalizeLoadedDialect("trino")).toBe("TRINO");
    expect(normalizeLoadedDialect("mysql")).toBe("MYSQL");
  });

  it("leaves an already-correct dialect unchanged", () => {
    expect(normalizeLoadedDialect("REDSHIFT")).toBe("REDSHIFT");
    expect(normalizeLoadedDialect("POSTGRESQL")).toBe("POSTGRESQL");
  });

  it("returns empty string for an empty value", () => {
    expect(normalizeLoadedDialect("")).toBe("");
  });

  it("does not coerce an unknown value to a valid one (preserves it as-is)", () => {
    expect(normalizeLoadedDialect("oracle")).toBe("oracle");
  });
});
