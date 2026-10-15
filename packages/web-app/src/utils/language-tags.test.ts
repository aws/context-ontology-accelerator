// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import {
  DEFAULT_LANGUAGE,
  LanguageTagError,
  canonicalizeLanguageTag,
  primaryLanguage,
} from "@coa/shared";
// The same rows drive libs/common/tests/unit/test_language_tags.py, so the
// browser and the API cannot disagree about which tags are valid.
import vectors from "../../../../libs/common/tests/data/language_tag_conformance.json";

describe("canonicalizeLanguageTag conformance", () => {
  it.each(vectors.canonical.map((row) => [JSON.stringify(row.input), row]))(
    "accepts %s",
    (_label, row) => {
      expect(canonicalizeLanguageTag(row.input)).toBe(row.canonical);
      expect(primaryLanguage(row.input)).toBe(row.primary);
      expect(canonicalizeLanguageTag(row.canonical)).toBe(row.canonical);
    },
  );

  it.each(
    vectors.invalid.map((row) => [
      `${row.reason}: ${JSON.stringify(row.input)}`,
      row,
    ]),
  )("rejects %s", (_label, row) => {
    expect(() => canonicalizeLanguageTag(row.input)).toThrow(LanguageTagError);
    expect(() => primaryLanguage(row.input)).toThrow(LanguageTagError);
  });

  it("uses canonical English as the default", () => {
    expect(canonicalizeLanguageTag(DEFAULT_LANGUAGE)).toBe("en");
  });
});
