// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";
import { LanguageMenu } from "../../pages/LanguageMenu";

/**
 * Settings → Language (Phase 1 of #1024): switching the UI locale localizes
 * Cloudscape's built-in component strings, is reflected on <html lang>, and
 * survives a reload. Read-only — the preference lives in this test's browser
 * context only; nothing is written to the backend.
 */
test.describe("settings: language", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);

  test("switches Cloudscape strings to Japanese and back, and persists across reload", async ({
    page,
  }) => {
    const menu = new LanguageMenu(page);
    await menu.gotoNamespaces();

    // Default: English, served synchronously from the statically imported bundle.
    await menu.expectDocumentLang("en");
    await menu.expectNextPageLabel("Next page");
    await menu.expectCurrent("English");

    await menu.choose("日本語");
    await menu.expectDocumentLang("ja");
    // The ja bundle is a lazily loaded chunk; expect() retries until it lands.
    await menu.expectNextPageLabel("次のページ");
    await menu.expectCurrent("日本語");

    // The preference is stored, so a fresh document starts in Japanese.
    await page.reload();
    await menu.expectDocumentLang("ja");
    await menu.expectNextPageLabel("次のページ");

    await menu.choose("English");
    await menu.expectDocumentLang("en");
    await menu.expectNextPageLabel("Next page");
  });
});
