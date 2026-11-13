// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, type Page } from "@playwright/test";

/**
 * Page Object for the Data Sources feature.
 * Routes: /namespaces/:id/sources, /sources/connect, /sources/:sourceId
 *
 * The connect flow is a Cloudscape Wizard: step 1 picks a kind (Glue / JDBC /
 * Documents), step 2 fills connection details. On submit the app navigates to
 * the new source's detail page.
 */
export class SourcesPage {
  constructor(private readonly page: Page) {}

  async gotoList(namespaceId: string): Promise<void> {
    await this.page.goto(`/namespaces/${namespaceId}/sources`);
    await expect(
      this.page.getByRole("heading", { name: "Sources" }).first(),
    ).toBeVisible();
  }

  rowByName(name: string) {
    return this.page.getByRole("row").filter({ hasText: name });
  }

  /**
   * Open the connect-source wizard by navigating directly to the connect route.
   * ConnectSource reads the target namespace from the URL, so direct navigation
   * reliably scopes the new source to the right namespace.
   */
  async startConnect(namespaceId: string): Promise<void> {
    await this.page.goto(`/namespaces/${namespaceId}/sources/connect`);
    await expect(
      this.page.getByRole("heading", { name: "Choose source type", level: 1 }),
    ).toBeVisible();
  }

  /** Step 1: choose the source kind tile, then advance the wizard. */
  private async chooseKind(label: string): Promise<void> {
    await this.page.getByText(label, { exact: true }).click();
    await this.page.getByRole("button", { name: "Next" }).click();
  }

  /** Onboard a Glue source; returns once the new source is shown. */
  async onboardGlue(
    namespaceId: string,
    name: string,
    opts: { catalogId: string; databaseName: string },
  ): Promise<void> {
    await this.startConnect(namespaceId);
    await this.chooseKind("Glue database");
    await this.page.getByPlaceholder("My Glue Database").fill(name);
    await this.page.getByPlaceholder("123456789012").fill(opts.catalogId);
    await this.page.getByPlaceholder("my_glue_db").fill(opts.databaseName);
    await this.submitWizard();
    await this.expectSourceVisible(name);
  }

  /**
   * Advance to the final wizard step and submit. Database sources have
   * intermediate steps (configure → enrichment → review); wait for each to
   * settle, else clicking "Next" races the transition and can submit early.
   */
  private async submitWizard(): Promise<void> {
    const submit = this.page.getByRole("button", { name: "Connect source" });
    const next = this.page.getByRole("button", { name: "Next" });
    for (let i = 0; i < 6; i++) {
      if (await submit.isVisible().catch(() => false)) break;
      if (await next.isEnabled().catch(() => false)) {
        await next.click();
        await this.page.waitForTimeout(1500);
      } else {
        await this.page.waitForTimeout(1000);
      }
    }
    await expect(submit).toBeVisible({ timeout: 15_000 });
    await submit.click();
  }

  /**
   * Assert onboarding created the source: the app navigates to /sources/<uuid>
   * on success. Asserting on the URL (not just the name, which also shows on
   * the review step) avoids a false positive when submit silently fails.
   */
  private async expectSourceVisible(name: string): Promise<void> {
    await expect
      .poll(() => new URL(this.page.url()).pathname, { timeout: 60_000 })
      .toMatch(/\/sources\/[0-9a-f-]{8,}/i);
    await expect(
      this.page.getByText(name, { exact: false }).first(),
    ).toBeVisible({ timeout: 30_000 });
  }

  async openDetail(namespaceId: string, name: string): Promise<void> {
    await this.gotoList(namespaceId);
    await this.rowByName(name).getByRole("link", { name }).click();
    await expect(
      this.page.getByText(name, { exact: false }).first(),
    ).toBeVisible();
  }

  /**
   * Delete a source from its detail page and assert it's gone from the list.
   * The Delete button is disabled while the source is in an active state
   * (REGISTERED / SCANNING / ENRICHING / DELETING / APPROVING / REJECTING), so
   * wait for it to settle. The namespace must be ACTIVE.
   */
  async deleteSource(namespaceId: string, name: string): Promise<void> {
    await this.openDetail(namespaceId, name);
    const del = this.page.getByRole("button", { name: "Delete" }).first();
    await expect(del).toBeEnabled({ timeout: 360_000 });
    await del.click();
    const modal = this.page.getByRole("dialog");
    await expect(modal).toBeVisible();
    await modal.getByRole("button", { name: "Delete" }).click();
    await expect(modal).toBeHidden();
    await this.gotoList(namespaceId);
    await expect(this.rowByName(name)).toHaveCount(0);
  }
}
