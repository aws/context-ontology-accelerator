// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Parser, Store } from "n3";
import { describe, expect, it } from "vitest";
import { extractQuoted } from "./grounding";
import {
  addAlternativeLabel,
  addPreferredLabel,
  makePreferredLabel,
  parseVocabulary,
  removeAlternativeLabel,
  replacePreferredLabel,
  safeVocabularyDisplayText,
  VocabularyEditError,
  type VocabularyLabel,
} from "./vocabulary";

const SKOS = "http://www.w3.org/2004/02/skos/core#";
const EX = "https://example.com/vocabulary/";
const RDF_LANG_STRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString";

const TURTLE = `
@prefix ex: <${EX}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix skos: <${SKOS}> .
@prefix dcterms: <http://purl.org/dc/terms/> .

ex:Customer a skos:Concept ;
  skos:prefLabel "顧客"@ja ;
  skos:altLabel "お客様"@ja, "customer"@en ;
  skos:inScheme ex:BusinessTerms ;
  dcterms:description "A \\"quoted\\" description"@en .

ex:Account a owl:Class ;
  skos:prefLabel "口座"@ja ;
  skos:altLabel "アカウント"@ja ;
  dcterms:subject ex:Customer .
`;

const label = (value: string, language: string): VocabularyLabel => ({
  value,
  language,
  datatype: RDF_LANG_STRING,
});

function store(turtle: string): Store {
  return new Store(new Parser().parse(turtle));
}

/** The term's labels as sorted `value@language` strings, for exact asserts. */
function labelsOf(
  turtle: string,
  id: string,
): { preferred: string[]; variants: string[] } {
  const concept = parseVocabulary(turtle).find((c) => c.id === id);
  const text = (labels: VocabularyLabel[]) =>
    labels.map((l) => `${l.value}@${l.language}`).sort();
  return {
    preferred: text(concept?.preferredLabels ?? []),
    variants: text(concept?.alternativeLabels ?? []),
  };
}

const PLAN = `
@prefix ex: <${EX}> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <${SKOS}> .

ex:Plan rdfs:label "Plan"@en-US, "プラン"@ja ;
  skos:altLabel "Pricing plan"@en-US .
`;

describe("business vocabulary Turtle editing", () => {
  it("reports a Turtle syntax error with the parser detail", () => {
    expect(() => parseVocabulary("<unterminated")).toThrow(
      /Failed to parse Turtle document:/,
    );
  });

  it("parses one row per labelled resource with language tags", () => {
    const concepts = parseVocabulary(TURTLE);
    expect(concepts).toHaveLength(2);
    expect(concepts[1]).toMatchObject({
      id: `${EX}Customer`,
      kind: "Concept",
      preferredLabels: [label("顧客", "ja")],
      alternativeLabels: [label("customer", "en"), label("お客様", "ja")],
    });
  });

  it("includes resources that currently have only an rdfs:label", () => {
    const turtle = `
      @prefix ex: <${EX}> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      ex:Equipment rdfs:label "設備"@ja .
    `;

    expect(parseVocabulary(turtle)).toMatchObject([
      {
        id: `${EX}Equipment`,
        preferredLabels: [label("設備", "ja")],
        alternativeLabels: [],
      },
    ]);
  });

  it("replaces a preferred label without damaging escaped literals", () => {
    const updated = replacePreferredLabel(
      TURTLE,
      `${EX}Customer`,
      label("顧客", "ja"),
      label("取引先", "ja"),
    );
    expect(
      parseVocabulary(updated).find(
        (concept) => concept.id === `${EX}Customer`,
      )!.preferredLabels,
    ).toEqual([label("取引先", "ja")]);
    expect(
      store(updated).countQuads(
        `${EX}Customer`,
        "http://purl.org/dc/terms/description",
        null,
        null,
      ),
    ).toBe(1);
  });

  it("adds, rejects, and promotes an alternative label", () => {
    const added = addAlternativeLabel(
      TURTLE,
      `${EX}Customer`,
      label("購入者", "ja"),
    );
    const rejected = removeAlternativeLabel(
      added,
      `${EX}Customer`,
      label("customer", "en"),
    );
    const promoted = makePreferredLabel(
      rejected,
      `${EX}Customer`,
      label("購入者", "ja"),
    );
    expect(labelsOf(added, `${EX}Customer`)).toEqual({
      preferred: ["顧客@ja"],
      variants: ["customer@en", "お客様@ja", "購入者@ja"].sort(),
    });
    expect(labelsOf(rejected, `${EX}Customer`)).toEqual({
      preferred: ["顧客@ja"],
      variants: ["お客様@ja", "購入者@ja"].sort(),
    });
    expect(labelsOf(promoted, `${EX}Customer`)).toEqual({
      preferred: ["購入者@ja"],
      variants: ["お客様@ja", "顧客@ja"].sort(),
    });
    expect(labelsOf(promoted, `${EX}Account`)).toEqual(
      labelsOf(TURTLE, `${EX}Account`),
    );
  });

  it("does not allow the same literal to be preferred and alternative", () => {
    expect(() =>
      addAlternativeLabel(TURTLE, `${EX}Customer`, label("顧客", "ja")),
    ).toThrow(/preferred label/);
  });

  it("rejects blank or oversized vocabulary labels", () => {
    expect(() =>
      addAlternativeLabel(TURTLE, `${EX}Customer`, label("   ", "ja")),
    ).toThrow(/must not be blank/);
    expect(() =>
      addAlternativeLabel(
        TURTLE,
        `${EX}Customer`,
        label("x".repeat(513), "ja"),
      ),
    ).toThrow(/must not exceed 512/);
    expect(() =>
      addAlternativeLabel(
        TURTLE,
        `${EX}Customer`,
        label("safe\u202espoofed", "ja"),
      ),
    ).toThrow(/unsafe invisible formatting controls/);
    expect(() =>
      addAlternativeLabel(
        TURTLE,
        `${EX}Customer`,
        label("safe\u200bspoofed", "ja"),
      ),
    ).toThrow(/unsafe invisible formatting controls/);
    expect(safeVocabularyDisplayText("safe\u200bhidden\u202espoofed")).toBe(
      "safe\ufffdhidden\ufffdspoofed",
    );
    expect(() =>
      addAlternativeLabel(TURTLE, `${EX}Customer`, label("क्\u200dष", "hi")),
    ).not.toThrow();
  });

  it("keeps the English message and the catalogue template on a failed edit", () => {
    let caught: unknown;
    try {
      addAlternativeLabel(TURTLE, `${EX}Customer`, label(" ", "ja"));
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(VocabularyEditError);
    expect(caught).toMatchObject({
      template: "A vocabulary label must not be blank",
      message: "A vocabulary label must not be blank",
    });
  });

  it("treats language tags that differ only in case as one language", () => {
    for (const tag of ["en-US", "en-us", "EN-us"]) {
      expect(() =>
        addPreferredLabel(PLAN, `${EX}Plan`, label("Subscription", tag)),
      ).toThrow(
        'This concept already has a preferred label for language "en-us"',
      );
    }
    expect(() =>
      replacePreferredLabel(
        PLAN,
        `${EX}Plan`,
        label("プラン", "ja"),
        label("Subscription", "EN-US"),
      ),
    ).toThrow(/already has a preferred label for language "en-us"/);
    expect(() =>
      addAlternativeLabel(PLAN, `${EX}Plan`, label("Plan", "EN-US")),
    ).toThrow("A preferred label cannot also be added as a variant");
  });

  it("accepts any Turtle LANGTAG as written apart from case", () => {
    // Intl.getCanonicalLocales would turn `iw` into `he` and throw on the
    // private-use `x-foo`; the proposal is Turtle, so its grammar decides.
    for (const [tag, stored] of [
      ["a", "a"],
      ["iw", "iw"],
      ["x-foo", "x-foo"],
      ["zh-Hant-TW", "zh-hant-tw"],
      ["de-CH-1901", "de-ch-1901"],
    ]) {
      expect(
        labelsOf(
          addAlternativeLabel(TURTLE, `${EX}Customer`, label("取引先", tag)),
          `${EX}Customer`,
        ).variants,
      ).toContain(`取引先@${stored}`);
    }
  });

  it("rejects a tag outside the Turtle LANGTAG grammar", () => {
    // `\u212a` is the Kelvin sign, which a case-insensitive Unicode match
    // would read as `k`.
    for (const tag of [
      "en_US",
      "en-",
      "-en",
      "en--us",
      "1en",
      "en us",
      "日本",
      "\u212a",
    ]) {
      expect(() =>
        addAlternativeLabel(TURTLE, `${EX}Customer`, label("取引先", tag)),
      ).toThrow(`"${tag}" is not a valid language tag`);
    }
  });

  it("leaves the kind empty for a term with no rdf:type", () => {
    const turtle = `
      @prefix ex: <${EX}> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      ex:Equipment rdfs:label "設備"@ja .
      ex:Thing a rdfs:Resource ; rdfs:label "もの"@ja .
    `;
    const kinds = Object.fromEntries(
      parseVocabulary(turtle).map((concept) => [concept.id, concept.kind]),
    );
    expect(kinds).toEqual({
      [`${EX}Equipment`]: "",
      [`${EX}Thing`]: "Resource",
    });
  });

  it("stores a canonical lowercased tag and still allows another region", () => {
    const updated = addPreferredLabel(
      PLAN,
      `${EX}Plan`,
      label("Subscription", "en-GB"),
    );
    expect(labelsOf(updated, `${EX}Plan`)).toEqual({
      preferred: ["Plan@en-us", "Subscription@en-gb", "プラン@ja"].sort(),
      variants: ["Pricing plan@en-us"],
    });
  });

  it("promotes a variant over the preferred label of the same language in any case", () => {
    const promoted = makePreferredLabel(
      PLAN,
      `${EX}Plan`,
      label("Pricing plan", "en-us"),
    );
    expect(labelsOf(promoted, `${EX}Plan`)).toEqual({
      preferred: ["Pricing plan@en-us", "プラン@ja"].sort(),
      variants: ["Plan@en-us"],
    });
  });

  it("keeps an edited label first, so the class name shown elsewhere stays", () => {
    const turtle = `
      @prefix ex: <${EX}> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      ex:Order rdfs:label "受注"@ja .
    `;
    const withEnglish = addPreferredLabel(
      turtle,
      `${EX}Order`,
      label("Order", "en"),
    );
    const edited = replacePreferredLabel(
      withEnglish,
      `${EX}Order`,
      label("受注", "ja"),
      label("受注伝票", "ja"),
    );
    expect(extractQuoted(edited, "rdfs:label")).toBe("受注伝票");
    expect(labelsOf(edited, `${EX}Order`).preferred).toEqual(
      ["Order@en", "受注伝票@ja"].sort(),
    );
  });

  it("swaps a promoted variant into the preferred label's place", () => {
    const turtle = `
      @prefix ex: <${EX}> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      @prefix skos: <${SKOS}> .
      ex:Order rdfs:label "注文"@ja, "Order"@en ;
        skos:altLabel "受注"@ja .
    `;
    const promoted = makePreferredLabel(
      turtle,
      `${EX}Order`,
      label("受注", "ja"),
    );
    expect(extractQuoted(promoted, "rdfs:label")).toBe("受注");
    expect(labelsOf(promoted, `${EX}Order`)).toEqual({
      preferred: ["Order@en", "受注@ja"].sort(),
      variants: ["注文@ja"],
    });
  });

  it("returns the Turtle unchanged when an edit changes nothing", () => {
    expect(
      replacePreferredLabel(
        TURTLE,
        `${EX}Customer`,
        label("顧客", "ja"),
        label(" 顧客 ", "JA"),
      ),
    ).toBe(TURTLE);
  });

  it("keeps a custom datatype on an untagged label", () => {
    const turtle = `
      @prefix ex: <${EX}> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      ex:Code rdfs:label "A-1"^^ex:code .
    `;
    const current = {
      value: "A-1",
      language: "",
      datatype: `${EX}code`,
    };
    expect(replacePreferredLabel(turtle, `${EX}Code`, current, current)).toBe(
      turtle,
    );
    const edited = replacePreferredLabel(turtle, `${EX}Code`, current, {
      ...current,
      value: "A-2",
    });
    expect(parseVocabulary(edited)[0].preferredLabels).toEqual([
      { value: "A-2", language: "", datatype: `${EX}code` },
    ]);
  });

  it("refuses to add a label the term already has", () => {
    expect(() =>
      addAlternativeLabel(TURTLE, `${EX}Customer`, label("お客様", "ja")),
    ).toThrow("This concept already has that variant");
    expect(() =>
      addPreferredLabel(TURTLE, `${EX}Customer`, label("顧客", "ja")),
    ).toThrow("This concept already has that preferred label");
  });

  it("reuses the last parse for the same Turtle text", () => {
    const first = parseVocabulary(TURTLE);
    expect(parseVocabulary(` ${TURTLE}`.slice(1))).toBe(first);
    expect(parseVocabulary(PLAN)).not.toBe(first);
  });

  it("edits a preferred label to the same language without a conflict", () => {
    const updated = replacePreferredLabel(
      PLAN,
      `${EX}Plan`,
      label("Plan", "en-us"),
      label("Membership plan", "EN-US"),
    );
    expect(labelsOf(updated, `${EX}Plan`)).toEqual({
      preferred: ["Membership plan@en-us", "プラン@ja"].sort(),
      variants: ["Pricing plan@en-us"],
    });
  });
});
