// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, type Page } from "@playwright/test";

/**
 * Page Object for the Language submenu in the top-navigation Settings menu.
 *
 * The switcher only changes what the browser shows: the locale is kept in
 * localStorage and the Cloudscape message bundle is a same-origin static chunk,
 * so these steps mutate no backend state. Assertions target a Cloudscape
 * built-in string (the pagination "next page" control on the Namespaces list),
 * which is exactly the layer Phase 1 of #1024 localizes — app-authored copy is
 * expected to stay English here.
 */
export class LanguageMenu {
  constructor(private readonly page: Page) {}

  /** A route that renders a Cloudscape Pagination regardless of data. */
  async gotoNamespaces(): Promise<void> {
    await this.page.goto("/namespaces");
    await expect(
      this.page.getByRole("heading", { name: "Namespaces", level: 1 }),
    ).toBeVisible();
  }

  /** Pick a language by its self-named label ("English" / "日本語"). */
  async choose(label: string): Promise<void> {
    await this.page.getByRole("button", { name: "Settings" }).click();
    await this.page.getByRole("menuitem", { name: label }).click();
  }

  /** The current language's own entry is disabled (and checked) in the menu. */
  async expectCurrent(label: string): Promise<void> {
    await this.page.getByRole("button", { name: "Settings" }).click();
    await expect(
      this.page.getByRole("menuitem", { name: label }),
    ).toHaveAttribute("aria-disabled", "true");
    await this.page.keyboard.press("Escape");
  }

  async expectDocumentLang(lang: string): Promise<void> {
    await expect(this.page.locator("html")).toHaveAttribute("lang", lang);
  }

  /** Cloudscape's own pagination label, in the active locale. */
  async expectNextPageLabel(label: string): Promise<void> {
    await expect(
      this.page.getByRole("button", { name: label, exact: true }),
    ).toBeVisible();
  }
}
