// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { formatTimestamp } from "../../../src/utils/helpers";

describe("formatTimestamp", () => {
  it("returns em dash for undefined", () => {
    expect(formatTimestamp(undefined)).toBe("—");
  });

  it("formats a Date object", () => {
    const d = new Date("2025-01-15T10:30:00Z");
    const result = formatTimestamp(d);
    expect(result).toContain("2025");
  });

  it("formats a date string", () => {
    const result = formatTimestamp("2025-06-01T00:00:00Z");
    expect(result).toContain("2025");
  });
});
