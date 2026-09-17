// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect } from "@playwright/test";
import { test } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * The extraction-vocabulary controls on the Documents step of the connect wizard.
 *
 * These assert the parts a unit test cannot: that the controls are actually
 * rendered in the deployed bundle, that the chip inputs only appear once
 * "Define my own" is selected, and that the validation the user sees fires in a
 * real browser rather than only in a helper function.
 *
 * No source is created — the wizard is exercised up to the point of submission,
 * so these are fast and leave nothing behind to clean up.
 */
test.describe("data-sources: extraction vocabulary controls", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);

  const NAMESPACE_ID_ENV = process.env.E2E_NAMESPACE_ID;
  test.skip(
    !NAMESPACE_ID_ENV,
    "set E2E_NAMESPACE_ID to an existing namespace to run",
  );

  /** Open the wizard on the Documents step, where the controls live. */
  async function openDocumentsStep(page: import("@playwright/test").Page) {
    await page.goto(`/namespaces/${NAMESPACE_ID_ENV}/sources/connect`);
    await expect(
      page.getByRole("heading", { name: "Choose source type", level: 1 }),
    ).toBeVisible();
    await page.getByText("Documents", { exact: true }).click();
    await page.getByRole("button", { name: "Next" }).click();
    // Its own collapsed container, separate from the document-lifecycle toggles
    // under "Advanced options".
    await page
      .getByText("Entity classes and topics (optional)", { exact: true })
      .click();
  }

  test("both controls render and default to the non-custom option", async ({
    page,
  }) => {
    await openDocumentsStep(page);

    await expect(
      page.getByText("Entity classes", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Topics", { exact: true })).toBeVisible();

    // Defaults must match server-side behaviour: infer classes, auto-name topics.
    await expect(
      page.getByRole("radio", { name: /Infer from my documents/ }).first(),
    ).toBeChecked();
    await expect(
      page.getByRole("radio", { name: /Automatic/ }).first(),
    ).toBeChecked();

    // The chip inputs are hidden until the user opts into supplying a list.
    await expect(
      page.getByPlaceholder("e.g. Loss Ratio, Claim Handler"),
    ).toBeHidden();
    await expect(
      page.getByPlaceholder("e.g. Black Tie Gala, Quiet Luxury"),
    ).toBeHidden();
  });

  test("choosing Define my own reveals the vocabulary chip input", async ({
    page,
  }) => {
    await openDocumentsStep(page);

    // Two "Define my own" radios exist (one per axis); take the first, which
    // belongs to Entity vocabulary.
    await page
      .getByRole("radio", { name: /Define my own/ })
      .first()
      .check();

    const input = page.getByPlaceholder("e.g. Loss Ratio, Claim Handler");
    await expect(input).toBeVisible();

    await input.fill("Loss Ratio");
    await input.press("Enter");
    // The committed value is rendered as a dismissable chip.
    await expect(page.getByText("Loss Ratio").first()).toBeVisible();

    // The limit is stated, not just enforced.
    await expect(page.getByText(/of 100 classes/)).toBeVisible();
  });

  test("duplicate label is rejected in the browser", async ({ page }) => {
    await openDocumentsStep(page);
    await page
      .getByRole("radio", { name: /Define my own/ })
      .first()
      .check();

    const input = page.getByPlaceholder("e.g. Loss Ratio, Claim Handler");
    await input.fill("Policy");
    await input.press("Enter");
    // Same label in a different case — the API treats these as one class, so the
    // UI must refuse the pair rather than let the request 400 later.
    await input.fill("POLICY");
    await input.press("Enter");

    await expect(page.getByText(/duplicates a label/)).toBeVisible();
  });

  test("a label that will be recorded differently is flagged, not blocked", async ({
    page,
  }) => {
    await openDocumentsStep(page);
    await page
      .getByRole("radio", { name: /Define my own/ })
      .first()
      .check();

    const input = page.getByPlaceholder("e.g. Loss Ratio, Claim Handler");
    await input.fill("StyleArchetype");
    await input.press("Enter");

    // Accepted — spelling is the user's choice — but the difference is surfaced.
    await expect(page.getByText("StyleArchetype").first()).toBeVisible();
    await expect(page.getByText(/Extraction will record/)).toBeVisible();
  });

  test("a comma-separated list is split into one chip per entry", async ({
    page,
  }) => {
    await openDocumentsStep(page);
    await page
      .getByRole("radio", { name: /Define my own/ })
      .first()
      .check();

    const input = page.getByPlaceholder("e.g. Loss Ratio, Claim Handler");
    // Filling in one go is what a paste looks like to the page.
    await input.fill("Dress, Coat, Shirt");
    await input.press("Enter");

    // Three chips, not one chip spelled "Dress, Coat, Shirt".
    for (const label of ["Dress", "Coat", "Shirt"]) {
      await expect(
        page.getByText(label, { exact: true }).first(),
      ).toBeVisible();
    }
    await expect(page.getByText("Added 3 classes.")).toBeVisible();
    await expect(page.getByText(/3 of 100 classes/)).toBeVisible();
  });

  test("a batch keeps the valid entries and reports the rejected one", async ({
    page,
  }) => {
    await openDocumentsStep(page);
    await page
      .getByRole("radio", { name: /Define my own/ })
      .first()
      .check();

    const input = page.getByPlaceholder("e.g. Loss Ratio, Claim Handler");
    // "Dress" repeats the first entry, so it must be skipped, not fail the batch.
    await input.fill("Dress, Coat, Dress");
    await input.press("Enter");

    await expect(
      page.getByText("Dress", { exact: true }).first(),
    ).toBeVisible();
    await expect(page.getByText("Coat", { exact: true }).first()).toBeVisible();
    await expect(page.getByText(/2 of 100 classes/)).toBeVisible();
  });

  test("choosing Define my own without adding anything blocks the step", async ({
    page,
  }) => {
    await openDocumentsStep(page);
    // Name and a file, so nothing ELSE is holding the step back.
    await page
      .getByPlaceholder("e.g. product-docs")
      .first()
      .fill("vocab-guard-check");

    await page
      .getByRole("radio", { name: /Define my own/ })
      .first()
      .check();
    // Deliberately add no classes, then try to advance.
    await page.getByRole("button", { name: "Next" }).click();

    // Without this guard the payload silently falls back to inference, doing the
    // opposite of what was selected.
    await expect(page.getByText(/Add at least one class/)).toBeVisible();
    // Still on the same step.
    await expect(
      page.getByText("Entity classes and topics (optional)", { exact: true }),
    ).toBeVisible();
  });

  test("choosing Define my own for topics without adding anything is flagged", async ({
    page,
  }) => {
    await openDocumentsStep(page);
    await page
      .getByPlaceholder("e.g. product-docs")
      .first()
      .fill("topic-guard-check");

    await page
      .getByRole("radio", { name: /Define my own/ })
      .nth(1)
      .check();
    await page.getByRole("button", { name: "Next" }).click();

    await expect(page.getByText(/Add at least one topic/)).toBeVisible();
  });

  test("topics accept a phrase and are not flagged for capitalisation", async ({
    page,
  }) => {
    await openDocumentsStep(page);

    // The second "Define my own" radio belongs to Topics.
    await page
      .getByRole("radio", { name: /Define my own/ })
      .nth(1)
      .check();

    const input = page.getByPlaceholder("e.g. Black Tie Gala, Quiet Luxury");
    await expect(input).toBeVisible();
    await input.fill("QuietLuxury");
    await input.press("Enter");

    await expect(page.getByText("QuietLuxury").first()).toBeVisible();
    // Topics are stored verbatim, so the class-name notice must NOT appear.
    await expect(page.getByText(/Extraction will record/)).toBeHidden();
    await expect(page.getByText(/of 100 topics/)).toBeVisible();
  });
});
