// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { deriveRuntimeSessionId } from "./runtime-session-id";

const MIN_LENGTH = 33;

describe("deriveRuntimeSessionId", () => {
  it("returns undefined for a missing sub", () => {
    expect(deriveRuntimeSessionId(undefined)).toBeUndefined();
  });

  it("pads a short sub (e.g. enterprise SSO username) to the 33-char minimum", () => {
    const result = deriveRuntimeSessionId("jdoe1234");
    expect(result).toBeDefined();
    expect(result!.length).toBeGreaterThanOrEqual(MIN_LENGTH);
    expect(result).toContain("jdoe1234");
  });

  it("keeps a Cognito-style UUID sub intact and above the minimum", () => {
    const uuidSub = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"; // 36 chars
    const result = deriveRuntimeSessionId(uuidSub);
    expect(result).toBe(`036-${uuidSub}`);
    expect(result!.length).toBeGreaterThanOrEqual(MIN_LENGTH);
  });

  it("is deterministic — same sub always maps to the same session id", () => {
    expect(deriveRuntimeSessionId("jdoe1234")).toBe(
      deriveRuntimeSessionId("jdoe1234"),
    );
  });

  it("does not collide for subs differing only by trailing zeros", () => {
    // Padding alone maps both of these to "jdoe0000…" — one AgentCore session
    // shared by two distinct users.
    expect(deriveRuntimeSessionId("jdoe")).not.toBe(
      deriveRuntimeSessionId("jdoe0"),
    );
  });

  it("is injective across subs that pad to the same string", () => {
    const subs = ["a", "a0", "a00", "jdoe", "jdoe0", "jdoe00000000000000"];
    const derived = subs.map((s) => deriveRuntimeSessionId(s));
    expect(new Set(derived).size).toBe(subs.length);
    for (const id of derived) {
      expect(id!.length).toBeGreaterThanOrEqual(MIN_LENGTH);
    }
  });

  it("does not collide between a short sub and a long sub equal to its padded form", () => {
    const short = "jdoe";
    const padded = short.padEnd(MIN_LENGTH, "0"); // what a naive impl would return
    expect(deriveRuntimeSessionId(short)).not.toBe(
      deriveRuntimeSessionId(padded),
    );
  });
});
