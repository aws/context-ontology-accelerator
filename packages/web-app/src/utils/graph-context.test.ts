// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { normalizeGraphContext } from "./graph-context";

describe("normalizeGraphContext", () => {
  it("keeps the canonical shape and drops malformed members", () => {
    const normalized = normalizeGraphContext({
      entities: [
        { uri: "e:kept", label: "Kept" },
        { uri: 7, label: "Bad uri" },
        "not an entity",
      ],
      relationships: [
        { sourceUri: "e:kept", predicateUri: "p:rel", targetUri: "e:other" },
        { sourceUri: "e:kept", targetUri: "e:other" },
      ],
    });

    expect(normalized).toEqual({
      entities: [{ uri: "e:kept", label: "Kept" }],
      relationships: [
        { sourceUri: "e:kept", predicateUri: "p:rel", targetUri: "e:other" },
      ],
    });
  });

  it("normalizes the legacy top-level entity array", () => {
    expect(
      normalizeGraphContext([{ uri: "e:legacy", label: "Legacy entity" }]),
    ).toEqual({ entities: [{ uri: "e:legacy", label: "Legacy entity" }] });
  });

  it("returns undefined for a non-object value", () => {
    expect(normalizeGraphContext(undefined)).toBeUndefined();
    expect(normalizeGraphContext(null)).toBeUndefined();
    expect(normalizeGraphContext("abc")).toBeUndefined();
  });

  it("replaces a string entities field with an empty array", () => {
    const normalized = normalizeGraphContext({ entities: "abc" });

    expect(normalized?.entities).toEqual([]);
    expect(Array.isArray(normalized?.entities)).toBe(true);
  });

  it("replaces an object entities field with an empty array", () => {
    const normalized = normalizeGraphContext({ entities: {} });

    expect(normalized?.entities).toEqual([]);
    expect(Array.isArray(normalized?.entities)).toBe(true);
  });

  it("replaces a non-array relationships field with an empty array", () => {
    const fromString = normalizeGraphContext({ relationships: "abc" });
    const fromObject = normalizeGraphContext({ relationships: {} });

    expect(fromString?.relationships).toEqual([]);
    expect(fromObject?.relationships).toEqual([]);
  });

  it("never returns a non-array for either field, whatever the input", () => {
    for (const bad of [
      "abc",
      {},
      0,
      1,
      true,
      null,
      undefined,
      { length: 3 },
    ] as unknown[]) {
      const normalized = normalizeGraphContext({
        entities: bad,
        relationships: bad,
      });
      expect(Array.isArray(normalized?.entities)).toBe(true);
      expect(Array.isArray(normalized?.relationships)).toBe(true);
    }
  });

  it("sanitizes nested entity and relationship fields", () => {
    const normalized = normalizeGraphContext({
      entities: [
        {
          uri: "e:1",
          label: "One",
          type: { x: 1 },
          properties: "abc",
          relationships: [null, "abc", { predicate: "p", target_uri: {} }],
        },
        { uri: "e:2", label: "Two", relationships: "abc" },
      ],
      relationships: [
        {
          sourceUri: "e:1",
          predicateUri: "p:rel",
          targetUri: "e:2",
          predicateLabel: { x: 1 },
        },
      ],
    });

    expect(normalized?.entities).toEqual([
      { uri: "e:1", label: "One", relationships: [{ predicate: "p" }] },
      { uri: "e:2", label: "Two" },
    ]);
    expect(normalized?.relationships).toEqual([
      { sourceUri: "e:1", predicateUri: "p:rel", targetUri: "e:2" },
    ]);
  });
});
