// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
// Lives under infra/test because libs/ts-shared has no test runner of its own
// (its "lint" script is just `tsc --noEmit`); jest here maps @coa/shared to it.
import { sanitizePrincipalKey } from "@coa/shared";

describe("sanitizePrincipalKey", () => {
  describe("regression: group names containing spaces", () => {
    // The seeder wrote this raw while every reader queried it encoded, so the
    // grant was silently unresolvable and the user got an authorization deny.
    it("encodes spaces so the key matches what readers query", () => {
      expect(sanitizePrincipalKey("Platform Admins")).toBe("Platform%20Admins");
    });
  });

  describe("characters that must survive unchanged", () => {
    it.each([
      "platform-admins",
      "platform_admins",
      "platform.admins",
      "tilde~group",
      "plain",
      "MixedCaseGroup",
      "group123",
    ])("passes %s through verbatim", (value) => {
      expect(sanitizePrincipalKey(value)).toBe(value);
    });
  });

  describe("characters that must be escaped", () => {
    it.each([
      ["a+b", "a%2Bb"],
      ["a#b", "a%23b"],
      ["a/b", "a%2Fb"],
      ["100%", "100%25"],
      ["Admins (Staging)", "Admins%20%28Staging%29"],
      ["comma,name", "comma%2Cname"],
      ["colon:name", "colon%3Aname"],
      ["DOMAIN\\user", "DOMAIN%5Cuser"],
    ])("encodes %s to %s", (value, expected) => {
      expect(sanitizePrincipalKey(value)).toBe(expected);
    });
  });

  // encodeURIComponent disagrees with Python's quote() on exactly these, so
  // they are the cases a naive implementation gets wrong.
  describe("divergences from bare encodeURIComponent", () => {
    it.each([
      ["bang!name", "bang%21name"],
      ["star*name", "star%2Aname"],
      ["quote'name", "quote%27name"],
      ["paren(name)", "paren%28name%29"],
    ])("escapes %s like Python does", (value, expected) => {
      expect(sanitizePrincipalKey(value)).toBe(expected);
      expect(sanitizePrincipalKey(value)).not.toBe(encodeURIComponent(value));
    });

    it("leaves ~ unescaped, matching Python's unreserved set", () => {
      expect(sanitizePrincipalKey("~")).toBe("~");
    });
  });

  describe("email normalization", () => {
    it("keeps @ and . readable rather than percent-encoding them", () => {
      expect(sanitizePrincipalKey("alice@example.com")).toBe(
        "alice@example.com",
      );
    });

    it("lowercases and trims identifiers containing @", () => {
      expect(sanitizePrincipalKey("  Alice@Example.COM  ")).toBe(
        "alice@example.com",
      );
    });

    it("encodes + in emails so plus-addressing resolves", () => {
      expect(sanitizePrincipalKey("foo+123@example.com")).toBe(
        "foo%2B123@example.com",
      );
    });

    it("does not lowercase identifiers without @, which are case-sensitive", () => {
      expect(sanitizePrincipalKey("MixedCase-Group")).toBe("MixedCase-Group");
    });
  });

  describe("non-ascii", () => {
    it.each([
      ["ünïcode", "%C3%BCn%C3%AFcode"],
      ["日本語", "%E6%97%A5%E6%9C%AC%E8%AA%9E"],
    ])("utf-8 percent-encodes %s", (value, expected) => {
      expect(sanitizePrincipalKey(value)).toBe(expected);
    });
  });

  describe("idempotence guard", () => {
    // Re-encoding doubles the escapes, which is why callers must encode once
    // at the boundary rather than defensively re-encoding.
    it("is not idempotent, so it must be applied exactly once", () => {
      const once = sanitizePrincipalKey("a b");
      expect(once).toBe("a%20b");
      expect(sanitizePrincipalKey(once)).toBe("a%2520b");
    });
  });
});
