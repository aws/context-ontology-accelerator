// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import { CATALOGUES, formatAppMessage } from "./appMessages";

function placeholders(message: string): string[] {
  return [
    ...new Set(
      [...message.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)].map(
        (match) => match[1],
      ),
    ),
  ].sort();
}

describe("formatAppMessage", () => {
  it("returns the English key unchanged for the default locale", () => {
    expect(formatAppMessage("en", "Sources")).toBe("Sources");
  });

  it("returns the Japanese catalogue entry", () => {
    expect(formatAppMessage("ja", "Sources")).toBe("データソース");
  });

  it("interpolates named values after selecting the locale", () => {
    expect(
      formatAppMessage("ja", "Namespace: {name}", { name: "設備管理" }),
    ).toBe("名前空間: 設備管理");
  });

  it("leaves an unresolved placeholder visible", () => {
    expect(formatAppMessage("ja", "Welcome, {name}.")).toBe(
      "ようこそ、{name} さん。",
    );
  });

  it("keeps every English placeholder in each catalogue translation", () => {
    for (const [locale, catalogue] of Object.entries(CATALOGUES)) {
      for (const [english, translated] of Object.entries(catalogue)) {
        expect(placeholders(translated), `${locale}: ${english}`).toEqual(
          placeholders(english),
        );
      }
    }
  });

  it("keeps blank translations limited to deliberate Japanese word-order fragments", () => {
    const blankMessages = Object.entries(CATALOGUES.ja)
      .filter(([, japanese]) => japanese === "")
      .map(([english]) => english)
      .sort();

    expect(blankMessages).toEqual(["Deleting ", "Removing "]);
  });
});
