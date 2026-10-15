// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import {
  NAMESPACE_LANGUAGE_OPTIONS,
  formatNamespaceLanguage,
  isNamespaceLanguage,
} from "./namespace-language";

describe("namespace language display", () => {
  it("offers exactly en and ja", () => {
    expect(NAMESPACE_LANGUAGE_OPTIONS.map((o) => o.value)).toEqual([
      "en",
      "ja",
    ]);
  });

  it("labels offered languages and passes other tags through", () => {
    expect(formatNamespaceLanguage("en")).toBe("English (en)");
    expect(formatNamespaceLanguage("ja")).toBe("Japanese (ja)");
    expect(formatNamespaceLanguage("pt-BR")).toBe("pt-BR");
    expect(formatNamespaceLanguage(undefined)).toBe("—");
  });

  it("narrows only offered languages", () => {
    expect(isNamespaceLanguage("ja")).toBe(true);
    expect(isNamespaceLanguage("fr")).toBe(false);
  });
});
