// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { tableDetailPath } from "./routes";

describe("tableDetailPath", () => {
  it("encodes a UUID namespaceId/sourceId as a no-op", () => {
    const path = tableDetailPath("ns-1", "src-1", "sales.orders");
    expect(path).toBe("/namespaces/ns-1/sources/src-1/tables/sales.orders");
  });

  it.each([
    ["sales#2026", "sales%232026"],
    ["sales%2026", "sales%252026"],
    ["sales 2026", "sales%202026"],
    ["売上明細", "%E5%A3%B2%E4%B8%8A%E6%98%8E%E7%B4%B0"],
  ])("encodes tableId %s as %s", (tableId, encoded) => {
    expect(tableDetailPath("ns-1", "src-1", tableId)).toBe(
      `/namespaces/ns-1/sources/src-1/tables/${encoded}`,
    );
  });
});
