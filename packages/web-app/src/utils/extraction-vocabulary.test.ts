// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import {
  MAX_CLASSIFICATION_LENGTH,
  MAX_TOPIC_LENGTH,
  MAX_VOCABULARY_ENTRIES,
  labelsRewrittenOnIngest,
  predictStoredForm,
  predictStoredTopicForm,
  toParserForm,
  toTopicStoredForm,
  topicsRewrittenOnIngest,
  validateClassification,
  validateTopic,
} from "./extraction-vocabulary";

describe("toParserForm", () => {
  it("title-cases an all-caps label", () => {
    expect(toParserForm("DRESS")).toBe("Dress");
  });

  it("title-cases a lowercase phrase", () => {
    expect(toParserForm("style archetype")).toBe("Style Archetype");
  });

  it("converts underscores to spaces", () => {
    expect(toParserForm("Style_Archetype")).toBe("Style Archetype");
  });

  it("leaves an already-correct label unchanged", () => {
    expect(toParserForm("Style Archetype")).toBe("Style Archetype");
  });

  it("flattens interior capitals, matching the parser's lossy behaviour", () => {
    // This is the failure the UI warns about: the second capital is lost.
    expect(toParserForm("StyleArchetype")).toBe("Stylearchetype");
  });

  // Python's str.title() capitalises after ANY non-letter, not just spaces.
  // Splitting on spaces alone would give "T-shirt" and silently disagree with
  // what the server stores.
  it.each([
    ["t-shirt", "T-Shirt"],
    ["T-SHIRT", "T-Shirt"],
    ["o'brien", "O'Brien"],
    ["3d", "3D"],
    ["ready-to-wear", "Ready-To-Wear"],
  ])("matches Python title() for %s", (raw, stored) => {
    expect(toParserForm(raw)).toBe(stored);
  });
});

describe("predictStoredForm", () => {
  it.each([["Dress"], ["Style Archetype"], ["Loss Ratio"], ["T-Shirt"]])(
    "returns null when %s round-trips unchanged",
    (raw) => {
      expect(predictStoredForm(raw)).toBeNull();
    },
  );

  it.each([
    ["DRESS", "Dress"],
    ["dress", "Dress"],
    ["loss ratio", "Loss Ratio"],
    ["Style_Archetype", "Style Archetype"],
    ["StyleArchetype", "Stylearchetype"],
    ["t-shirt", "T-Shirt"],
  ])("predicts %s will be recorded as %s", (raw, stored) => {
    expect(predictStoredForm(raw)).toBe(stored);
  });
});

describe("labelsRewrittenOnIngest", () => {
  it("returns nothing when every label round-trips", () => {
    expect(labelsRewrittenOnIngest(["Dress", "Loss Ratio"])).toEqual([]);
  });

  it("reports only the labels that will differ, in order", () => {
    expect(
      labelsRewrittenOnIngest(["Dress", "DRESS", "Loss Ratio", "loss ratio"]),
    ).toEqual([
      { typed: "DRESS", stored: "Dress" },
      { typed: "loss ratio", stored: "Loss Ratio" },
    ]);
  });
});

describe("validateClassification", () => {
  it("accepts a Title Case label", () => {
    expect(validateClassification("Loss Ratio")).toBeNull();
  });

  it("accepts an all-caps label — spelling is not policed", () => {
    expect(validateClassification("DRESS")).toBeNull();
  });

  it("rejects an empty label", () => {
    expect(validateClassification("  ")).toBe("Enter a label.");
  });

  it("rejects an over-length label", () => {
    const tooLong = "A".repeat(MAX_CLASSIFICATION_LENGTH + 1);
    expect(validateClassification(tooLong)).toContain(
      `${MAX_CLASSIFICATION_LENGTH} characters or fewer`,
    );
  });

  it("accepts CamelCase — spelling is the user's choice", () => {
    // Surfaced as a non-blocking notice via labelsRewrittenOnIngest instead.
    expect(validateClassification("StyleArchetype")).toBeNull();
  });
});

describe("validateTopic", () => {
  it("accepts a multi-word topic phrase", () => {
    expect(validateTopic("Black Tie Gala Dress Code")).toBeNull();
  });

  it("accepts CamelCase — topics are not run through format_classification", () => {
    expect(validateTopic("QuietLuxury")).toBeNull();
  });

  it("rejects an empty topic", () => {
    expect(validateTopic("  ")).toBe("Enter a topic name.");
  });

  it("rejects an over-length topic", () => {
    const tooLong = "A".repeat(MAX_TOPIC_LENGTH + 1);
    expect(validateTopic(tooLong)).toContain(
      `${MAX_TOPIC_LENGTH} characters or fewer`,
    );
  });
});

describe("list-level limits", () => {
  // Enforced in the UI as well as the API so the user is stopped at the point of
  // entry rather than discovering it as a 400 after filling in the whole form.
  const atCap = Array.from(
    { length: MAX_VOCABULARY_ENTRIES },
    (_, i) => `Label ${i}`,
  );

  it("rejects a classification once the list is at the cap", () => {
    expect(validateClassification("One More", atCap)).toBe(
      `You can add at most ${MAX_VOCABULARY_ENTRIES} labels.`,
    );
  });

  it("accepts a classification one below the cap", () => {
    expect(validateClassification("One More", atCap.slice(0, -1))).toBeNull();
  });

  it("rejects a topic once the list is at the cap", () => {
    expect(validateTopic("One More", atCap)).toBe(
      `You can add at most ${MAX_VOCABULARY_ENTRIES} topics.`,
    );
  });

  it("rejects a case-insensitive duplicate classification, as the API does", () => {
    expect(validateClassification("DRESS", ["Dress"])).toContain("duplicates");
  });

  it("rejects a case-insensitive duplicate topic", () => {
    expect(validateTopic("black tie gala", ["Black Tie Gala"])).toContain(
      "duplicates",
    );
  });

  it("allows a distinct label alongside existing ones", () => {
    expect(validateClassification("Outerwear", ["Dress"])).toBeNull();
  });
});

// Topics take a different transform from classes: format_value then
// strip_full_stop, with NO title-casing (topic_utils.py:79).
describe("toTopicStoredForm", () => {
  it("leaves capitalisation alone, unlike a class label", () => {
    expect(toTopicStoredForm("QuietLuxury")).toBe("QuietLuxury");
    expect(toTopicStoredForm("everyday elegance")).toBe("everyday elegance");
  });

  it("turns underscores into spaces", () => {
    expect(toTopicStoredForm("Quiet_Luxury")).toBe("Quiet Luxury");
  });

  it("drops a single trailing full stop", () => {
    expect(toTopicStoredForm("Black Tie Gala.")).toBe("Black Tie Gala");
  });

  it("drops only the last full stop, leaving interior ones", () => {
    expect(toTopicStoredForm("A.W. Tailoring.")).toBe("A.W. Tailoring");
  });

  it("applies both transforms together", () => {
    expect(toTopicStoredForm("Quiet_Luxury.")).toBe("Quiet Luxury");
  });
});

describe("predictStoredTopicForm", () => {
  it("returns null when the name round-trips unchanged", () => {
    expect(predictStoredTopicForm("Black Tie Gala")).toBeNull();
    // A run-together name is fine for a topic — it is only classes that get
    // title-cased and so lose the word boundary.
    expect(predictStoredTopicForm("QuietLuxury")).toBeNull();
  });

  it("returns the recorded form when it differs", () => {
    expect(predictStoredTopicForm("Quiet_Luxury")).toBe("Quiet Luxury");
    expect(predictStoredTopicForm("Black Tie Gala.")).toBe("Black Tie Gala");
  });
});

describe("topicsRewrittenOnIngest", () => {
  it("lists only the topics whose recorded name differs", () => {
    expect(
      topicsRewrittenOnIngest(["Black Tie Gala", "Quiet_Luxury", "Resort."]),
    ).toEqual([
      { typed: "Quiet_Luxury", stored: "Quiet Luxury" },
      { typed: "Resort.", stored: "Resort" },
    ]);
  });

  it("returns nothing when every topic survives as written", () => {
    expect(topicsRewrittenOnIngest(["Black Tie Gala", "QuietLuxury"])).toEqual(
      [],
    );
  });
});

// Dedupe folding must agree with the API, which compares with Python's
// str.lower(). It deliberately does NOT use casefold(), which folds "ß" to "ss"
// and would reject a pair this accepts — the caller would then hit a 400 only
// after filling in the whole form.
describe("duplicate detection matches the API's folding", () => {
  it("does not treat ß and ss as the same label", () => {
    expect(validateClassification("STRASSE", ["Straße"])).toBeNull();
    expect(validateTopic("STRASSE", ["Straße"])).toBeNull();
  });

  it("still rejects a plain case-only difference", () => {
    expect(validateClassification("POLICY", ["Policy"])).toContain(
      "duplicates",
    );
    expect(validateTopic("BLACK TIE GALA", ["Black Tie Gala"])).toContain(
      "duplicates",
    );
  });
});
