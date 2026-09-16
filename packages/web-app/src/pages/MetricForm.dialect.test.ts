// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { normalizeLoadedDialect } from "./MetricForm";

// #140: a metric imported before the OSI parser fix can carry a mis-cased dialect
// (e.g. "redshift"). The edit form matches dialect options case-sensitively, so it
// must upper-case a loaded value that resolves to a known option — otherwise the
// Select silently falls back to its first option (PostgreSQL).
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
