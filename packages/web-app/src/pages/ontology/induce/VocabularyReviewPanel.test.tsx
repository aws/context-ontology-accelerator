// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LocaleProvider } from "../../../i18n/LocaleProvider";
import { VocabularyReviewPanel } from "./VocabularyReviewPanel";
import { parseVocabulary, type VocabularyLabel } from "./vocabulary";

const EX = "https://example.com/vocabulary/";

const TURTLE = `
@prefix ex: <${EX}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .

ex:Customer a owl:Class ;
  rdfs:label "顧客"@ja ;
  skos:altLabel "お客様"@ja, "customer"@en .

ex:customerName a owl:DatatypeProperty ;
  rdfs:domain ex:Customer ;
  rdfs:label "顧客名"@ja .

ex:Plan a owl:Class ;
  rdfs:label "Plan"@en-US .

ex:Account a owl:Class ;
  rdfs:label "口座"@ja .
`;

const CUSTOMER_TERMS = [`${EX}Customer`, `${EX}customerName`];

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

function renderPanel(
  props: Partial<React.ComponentProps<typeof VocabularyReviewPanel>> = {},
) {
  const onChange = vi.fn();
  const view = render(
    <VocabularyReviewPanel
      turtle={TURTLE}
      termIris={CUSTOMER_TERMS}
      isReadOnly={false}
      onChange={onChange}
      {...props}
    />,
  );
  return { ...view, onChange };
}

function updatedTurtle(onChange: ReturnType<typeof vi.fn>): string {
  expect(onChange).toHaveBeenCalledOnce();
  return onChange.mock.calls[0][0] as string;
}

describe("VocabularyReviewPanel", () => {
  it("lists only the given terms, in a named table", () => {
    renderPanel();
    expect(screen.getByText("Business vocabulary (2)")).toBeTruthy();
    const table = screen.getByRole("table", { name: "Business vocabulary" });
    expect(within(table).getByText("顧客 @ja")).toBeTruthy();
    expect(within(table).getByText("顧客名 @ja")).toBeTruthy();
    expect(within(table).getByText("お客様 @ja")).toBeTruthy();
    expect(within(table).getByText("customer @en")).toBeTruthy();
    expect(screen.queryByText("口座 @ja")).toBeNull();
    expect(screen.queryByText("Plan @en-us")).toBeNull();
  });

  it("says edits apply to this proposal only", () => {
    renderPanel();
    expect(
      screen.getByText(
        "Edits apply to this proposal only. A re-induction builds its labels and variants from the source metadata again, so add lasting synonyms there.",
      ),
    ).toBeTruthy();
  });

  it("offers Edit, Add language, Make preferred, Add variant and Reject only", () => {
    renderPanel({ termIris: [`${EX}Customer`] });
    const names = within(
      screen.getByRole("table", { name: "Business vocabulary" }),
    )
      .getAllByRole("button")
      .map((button) => button.getAttribute("aria-label") ?? button.textContent)
      .sort();
    expect(names).toEqual(
      [
        "Edit preferred label 顧客 @ja",
        "Add language to 顧客",
        "Make preferred customer @en",
        "Reject variant customer @en",
        "Make preferred お客様 @ja",
        "Reject variant お客様 @ja",
        "Add variant to 顧客",
      ].sort(),
    );
  });

  it("rejects exactly the chosen variant", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Reject variant customer @en" }),
    );
    const updated = updatedTurtle(onChange);
    expect(labelsOf(updated, `${EX}Customer`)).toEqual({
      preferred: ["顧客@ja"],
      variants: ["お客様@ja"],
    });
    expect(labelsOf(updated, `${EX}customerName`)).toEqual(
      labelsOf(TURTLE, `${EX}customerName`),
    );
    expect(labelsOf(updated, `${EX}Account`)).toEqual(
      labelsOf(TURTLE, `${EX}Account`),
    );
  });

  it("edits a preferred label to exactly the new value", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Edit preferred label 顧客名 @ja" }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Label"), {
      target: { value: "取引先名" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
    const updated = updatedTurtle(onChange);
    expect(labelsOf(updated, `${EX}customerName`)).toEqual({
      preferred: ["取引先名@ja"],
      variants: [],
    });
    expect(labelsOf(updated, `${EX}Customer`)).toEqual(
      labelsOf(TURTLE, `${EX}Customer`),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("adds exactly one variant", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Label"), {
      target: { value: "  取引先 " },
    });
    fireEvent.change(within(dialog).getByLabelText("Language tag"), {
      target: { value: "JA" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
    expect(labelsOf(updatedTurtle(onChange), `${EX}Customer`)).toEqual({
      preferred: ["顧客@ja"],
      variants: ["customer@en", "お客様@ja", "取引先@ja"].sort(),
    });
  });

  it("adds a preferred label in another language", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Add language to 顧客",
      }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Label"), {
      target: { value: "Client" },
    });
    fireEvent.change(within(dialog).getByLabelText("Language tag"), {
      target: { value: "en" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
    expect(labelsOf(updatedTurtle(onChange), `${EX}Customer`)).toEqual({
      preferred: ["Client@en", "顧客@ja"].sort(),
      variants: ["customer@en", "お客様@ja"].sort(),
    });
  });

  it("promotes a variant and demotes the old preferred label", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Make preferred お客様 @ja",
      }),
    );
    expect(labelsOf(updatedTurtle(onChange), `${EX}Customer`)).toEqual({
      preferred: ["お客様@ja"],
      variants: ["customer@en", "顧客@ja"].sort(),
    });
  });

  it("shows a failed Apply inside the dialog and keeps it open", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", {
        name: "Add language to 顧客",
      }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Label"), {
      target: { value: "得意先" },
    });
    fireEvent.change(within(dialog).getByLabelText("Language tag"), {
      target: { value: "ja" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(
      within(dialog).getByText(
        'This concept already has a preferred label for language "ja"',
      ),
    ).toBeTruthy();
    expect(within(dialog).getByText("The vocabulary edit failed")).toBeTruthy();
  });

  it("rejects en-US when the term already has an en-us preferred label", () => {
    const { onChange } = renderPanel({ termIris: [`${EX}Plan`] });
    for (const tag of ["en-US", "en-us"]) {
      fireEvent.click(
        screen.getByRole("button", {
          name: "Add language to Plan",
        }),
      );
      const dialog = screen.getByRole("dialog");
      fireEvent.change(within(dialog).getByLabelText("Label"), {
        target: { value: "Subscription" },
      });
      fireEvent.change(within(dialog).getByLabelText("Language tag"), {
        target: { value: tag },
      });
      fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
      expect(
        within(dialog).getByText(
          'This concept already has a preferred label for language "en-us"',
        ),
      ).toBeTruthy();
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    }
    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows an invalid language tag inside the dialog", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Edit preferred label 顧客 @ja" }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Language tag"), {
      target: { value: "not_a_language" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));

    expect(onChange).not.toHaveBeenCalled();
    expect(
      within(dialog).getByText('"not_a_language" is not a valid language tag'),
    ).toBeTruthy();
  });

  it("does not apply a dialog opened before the Turtle changed", () => {
    const { onChange, rerender } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    fireEvent.change(
      within(screen.getByRole("dialog")).getByLabelText("Label"),
      {
        target: { value: "取引先" },
      },
    );
    rerender(
      <VocabularyReviewPanel
        turtle={`${TURTLE}\n<${EX}Account> <http://www.w3.org/2004/02/skos/core#altLabel> "勘定"@ja .`}
        termIris={CUSTOMER_TERMS}
        isReadOnly={false}
        onChange={onChange}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByText(
        "The proposal changed while this dialog was open. Close it and try again.",
      ),
    ).toBeTruthy();
    const apply = within(dialog).getByRole("button", { name: "Apply" });
    expect(apply).toBeDisabled();
    fireEvent.click(apply);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not offer Apply for a blank label", () => {
    renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Label"), {
      target: { value: "   " },
    });
    expect(
      within(dialog).getByRole("button", { name: "Apply" }),
    ).toBeDisabled();
  });

  it("does not expose mutation controls for read-only proposals", () => {
    renderPanel({ isReadOnly: true });
    const table = screen.getByRole("table", { name: "Business vocabulary" });
    expect(within(table).getByText("お客様 @ja")).toBeTruthy();
    expect(within(table).queryAllByRole("button")).toHaveLength(0);
  });

  it("names the term and label in the dialog header", () => {
    renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客名" }),
    );
    expect(
      within(screen.getByRole("dialog")).getByText("Add variant to 顧客名"),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Edit preferred label 顧客 @ja" }),
    );
    expect(
      within(screen.getByRole("dialog")).getByText(
        "Edit preferred label 顧客 @ja",
      ),
    ).toBeTruthy();
  });

  it("closes an unchanged Edit without reporting a change", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Edit preferred label 顧客 @ja" }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Language tag"), {
      target: { value: "JA" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("refuses to add a variant the term already has", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Label"), {
      target: { value: "customer" },
    });
    fireEvent.change(within(dialog).getByLabelText("Language tag"), {
      target: { value: "en" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(
      within(dialog).getByText("This concept already has that variant"),
    ).toBeTruthy();
  });

  it("keeps a dialog usable when the same Turtle text is passed again", () => {
    const { onChange, rerender } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    const opened = screen.getByRole("dialog");
    fireEvent.change(within(opened).getByLabelText("Label"), {
      target: { value: "取引先" },
    });
    fireEvent.change(within(opened).getByLabelText("Language tag"), {
      target: { value: "ja" },
    });
    rerender(
      <VocabularyReviewPanel
        turtle={` ${TURTLE}`.slice(1)}
        termIris={[...CUSTOMER_TERMS]}
        isReadOnly={false}
        onChange={onChange}
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).queryByText(
        "The proposal changed while this dialog was open. Close it and try again.",
      ),
    ).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Apply" }));
    expect(labelsOf(updatedTurtle(onChange), `${EX}Customer`).variants).toEqual(
      ["customer@en", "お客様@ja", "取引先@ja"].sort(),
    );
  });

  it("keeps labels that differ only in datatype apart", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      renderPanel({
        turtle: `${TURTLE}\n<${EX}Customer> <http://www.w3.org/2004/02/skos/core#altLabel> "code", "code"^^<${EX}code> .`,
        termIris: [`${EX}Customer`],
      });
      const table = screen.getByRole("table", { name: "Business vocabulary" });
      expect(within(table).getAllByText("code")).toHaveLength(2);
      expect(
        errors.mock.calls.some((call) => String(call[0]).includes("same key")),
      ).toBe(false);
    } finally {
      errors.mockRestore();
    }
  });

  it("applies the dialog on Enter, but not on an Enter that confirms an IME conversion", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    const dialog = screen.getByRole("dialog");
    const value = within(dialog).getByLabelText("Label");
    fireEvent.change(value, { target: { value: "取引先" } });
    fireEvent.keyDown(value, { key: "Enter", isComposing: true });
    fireEvent.keyDown(value, { key: "Enter", keyCode: 229 });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();

    const language = within(dialog).getByLabelText("Language tag");
    fireEvent.change(language, { target: { value: "ja" } });
    fireEvent.keyDown(language, { key: "Enter", keyCode: 13 });
    expect(labelsOf(updatedTurtle(onChange), `${EX}Customer`).variants).toEqual(
      ["customer@en", "お客様@ja", "取引先@ja"].sort(),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("ignores Enter on a blank label until the label is filled in", () => {
    const { onChange } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    const dialog = screen.getByRole("dialog");
    const value = within(dialog).getByLabelText("Label");
    fireEvent.change(value, { target: { value: "   " } });
    fireEvent.keyDown(value, { key: "Enter", keyCode: 13 });
    expect(onChange).not.toHaveBeenCalled();
    expect(within(dialog).queryByText("The vocabulary edit failed")).toBeNull();

    fireEvent.change(value, { target: { value: "取引先" } });
    fireEvent.change(within(dialog).getByLabelText("Language tag"), {
      target: { value: "ja" },
    });
    fireEvent.keyDown(value, { key: "Enter", keyCode: 13 });
    expect(labelsOf(updatedTurtle(onChange), `${EX}Customer`).variants).toEqual(
      ["customer@en", "お客様@ja", "取引先@ja"].sort(),
    );
  });

  it("ignores Enter in a dialog opened before the Turtle changed", () => {
    const changed = `${TURTLE}\n<${EX}Account> <http://www.w3.org/2004/02/skos/core#altLabel> "勘定"@ja .`;
    const { onChange, rerender } = renderPanel();
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    fireEvent.change(
      within(screen.getByRole("dialog")).getByLabelText("Label"),
      { target: { value: "取引先" } },
    );
    rerender(
      <VocabularyReviewPanel
        turtle={changed}
        termIris={CUSTOMER_TERMS}
        isReadOnly={false}
        onChange={onChange}
      />,
    );
    fireEvent.keyDown(
      within(screen.getByRole("dialog")).getByLabelText("Label"),
      { key: "Enter", keyCode: 13 },
    );
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeTruthy();

    // Reopened against the current Turtle, the same Enter applies.
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Cancel",
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Add variant to 顧客" }),
    );
    const reopened = within(screen.getByRole("dialog"));
    fireEvent.change(reopened.getByLabelText("Label"), {
      target: { value: "取引先" },
    });
    fireEvent.change(reopened.getByLabelText("Language tag"), {
      target: { value: "ja" },
    });
    fireEvent.keyDown(reopened.getByLabelText("Label"), {
      key: "Enter",
      keyCode: 13,
    });
    expect(labelsOf(updatedTurtle(onChange), `${EX}Customer`).variants).toEqual(
      ["customer@en", "お客様@ja", "取引先@ja"].sort(),
    );
  });

  it("names a term with no rdf:type a Resource", () => {
    renderPanel({
      turtle: `${TURTLE}\n<${EX}Equipment> <http://www.w3.org/2000/01/rdf-schema#label> "設備"@ja .`,
      termIris: [`${EX}Equipment`],
    });
    expect(screen.getByText("Resource · Equipment")).toBeTruthy();
  });

  it("renders nothing when none of the terms has a label", () => {
    const { container } = renderPanel({ termIris: [`${EX}Unknown`] });
    expect(container.textContent).toBe("");
  });
});

describe("VocabularyReviewPanel in Japanese", () => {
  beforeEach(() => {
    window.localStorage.setItem("scl.locale", "ja");
  });

  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = "";
  });

  it("localizes the panel, the dialog and edit errors", async () => {
    render(
      <LocaleProvider>
        <VocabularyReviewPanel
          turtle={TURTLE}
          termIris={CUSTOMER_TERMS}
          isReadOnly={false}
          onChange={vi.fn()}
        />
      </LocaleProvider>,
    );
    expect(await screen.findByText("業務用語（2）")).toBeTruthy();
    expect(screen.getByRole("table", { name: "業務用語" })).toBeTruthy();
    expect(screen.getByText("優先ラベル")).toBeTruthy();
    expect(screen.getByText("表記ゆれ")).toBeTruthy();
    expect(screen.getByText("表記ゆれなし")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "優先ラベル 顧客 @ja を編集" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "表記ゆれ お客様 @ja を却下" }),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "顧客 に言語を追加" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("顧客 に言語を追加")).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText("ラベル"), {
      target: { value: "得意先" },
    });
    fireEvent.change(within(dialog).getByLabelText("言語タグ"), {
      target: { value: "ja" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "適用" }));
    expect(
      within(dialog).getByText(
        "この概念には言語「ja」の優先ラベルがすでにあります",
      ),
    ).toBeTruthy();
  });

  it("translates the kind of a term with no rdf:type", async () => {
    render(
      <LocaleProvider>
        <VocabularyReviewPanel
          turtle={`${TURTLE}\n<${EX}Equipment> <http://www.w3.org/2000/01/rdf-schema#label> "設備"@ja .`}
          termIris={[`${EX}Equipment`]}
          isReadOnly={false}
          onChange={vi.fn()}
        />
      </LocaleProvider>,
    );
    expect(await screen.findByText("リソース · Equipment")).toBeTruthy();
  });
});
