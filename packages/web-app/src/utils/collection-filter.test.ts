// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { matchesFilteringText } from "./collection-filter";

describe("matchesFilteringText", () => {
  const item = {
    name: "sales",
    status: "ACTIVE",
    sourceCount: 3,
    tags: ["hidden"],
  };

  it("matches everything when the filter is empty", () => {
    expect(matchesFilteringText(item, "", [])).toBe(true);
  });

  it("keeps the default match on raw fields, case-insensitively", () => {
    expect(matchesFilteringText(item, "active", [])).toBe(true);
    expect(matchesFilteringText(item, "SAL", [])).toBe(true);
    expect(matchesFilteringText(item, "3", [])).toBe(true);
  });

  it("does not search inside object or array fields", () => {
    expect(matchesFilteringText(item, "hidden", [])).toBe(false);
  });

  it("matches the texts the row shows", () => {
    expect(matchesFilteringText(item, "アクティブ", ["アクティブ"])).toBe(true);
    expect(matchesFilteringText(item, "アーカイブ", ["アクティブ"])).toBe(
      false,
    );
    expect(matchesFilteringText(item, "x", [undefined])).toBe(false);
  });
});
