// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * Opening a link to a namespace the user cannot open (deleted, never existed,
 * or no access) must say so in the top-bar namespace switcher instead of
 * showing "—", and must not make another namespace current.
 *
 * Read-only: the namespace id is a fresh random UUID that no namespace has, so
 * the server answers the lookup with 404 or 403. Nothing is created.
 */

const LOCALE_KEY = "scl.locale";

const COPY = {
  en: {
    switcher: "Namespace switcher",
    unavailable: "Namespace: Not found or not accessible",
  },
  ja: {
    switcher: "名前空間の切り替え",
    unavailable: "名前空間: 見つからないか、アクセスできません",
  },
} as const;

/** Show the console in `locale` (browser-only state; no backend call). */
async function showConsoleIn(
  page: Page,
  locale: keyof typeof COPY,
): Promise<void> {
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, value),
    [LOCALE_KEY, locale] as const,
  );
}

test.describe("namespaces: switcher for a namespace that cannot be opened", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);
  // A 404 lookup is not retried, so for most users the message follows one
  // round trip. A user who gets 403 sees it after the lookup's retries
  // (about 7 s), and a cold first page load adds to that.
  test.setTimeout(60_000);

  for (const locale of ["en", "ja"] as const) {
    test(`says not found or not accessible (${locale})`, async ({ page }) => {
      await showConsoleIn(page, locale);
      await page.goto(`/namespaces/${randomUUID()}`);

      const switcher = page.getByRole("button", {
        name: COPY[locale].switcher,
      });
      await expect(switcher).toHaveText(COPY[locale].unavailable, {
        timeout: 20_000,
      });
    });
  }

  test("picking a namespace from the switcher replaces the message", async ({
    page,
  }) => {
    await showConsoleIn(page, "en");
    await page.goto(`/namespaces/${randomUUID()}`);
    const switcher = page.getByRole("button", { name: COPY.en.switcher });
    await expect(switcher).toHaveText(COPY.en.unavailable, { timeout: 20_000 });

    await switcher.click();
    const first = page.getByRole("menuitem").first();
    test.skip(
      (await first.getAttribute("aria-disabled")) === "true",
      "no namespace visible to the E2E user",
    );
    const name = (await first.innerText()).trim();
    await first.click();
    await expect(switcher).toHaveText(`Namespace: ${name}`);
  });
});
