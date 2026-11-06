// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, type Page } from "@playwright/test";

/**
 * Page Object for the Metrics feature.
 * Routes: /namespaces/:id/metrics, /metrics/create, /metrics/:name, /edit
 *
 * The list has Import OSI / Export OSI / Create metric actions and a multi-
 * select table with a bulk-delete modal. The create form needs a name,
 * description, data source, source table, and one dialect expression — so it
 * requires an APPROVED database source to exist.
 */
export class MetricsPage {
  constructor(private readonly page: Page) {}

  async gotoList(namespaceId: string): Promise<void> {
    await this.page.goto(`/namespaces/${namespaceId}/metrics`);
    await expect(
      this.page.getByRole("button", { name: "Create metric" }),
    ).toBeVisible();
  }

  rowByName(name: string) {
    return this.page.getByRole("row").filter({ hasText: name });
  }

  /** The Export OSI button (disabled when the namespace has no metrics). */
  get exportButton() {
    return this.page.getByRole("button", { name: "Export OSI" });
  }

  /**
   * Create a metric. Needs an existing APPROVED data source; picks the first
   * available source and table and fills one PostgreSQL expression.
   */
  async create(
    name: string,
    opts: { description: string; expression: string; label?: string },
  ): Promise<void> {
    await this.page.getByRole("button", { name: "Create metric" }).click();
    await expect(
      this.page.getByRole("heading", { name: "Create metric" }),
    ).toBeVisible();

    await this.page.getByPlaceholder("monthly_revenue").fill(name);
    if (opts.label !== undefined) {
      await this.labelInput.fill(opts.label);
    }
    await this.page
      .getByPlaceholder("Total revenue from all orders in a calendar month")
      .fill(opts.description);

    await this.page.getByText("Select a data source").click();
    await this.page.getByRole("option").first().click();

    await this.page.getByText("Select a table").click();
    await this.page.getByRole("option").first().click();

    await this.page
      .getByPlaceholder("SUM(orders.total_amount)")
      .fill(opts.expression);

    await this.page
      .getByRole("button", { name: "Create metric" })
      .last()
      .click();

    // On success the app navigates to the metric detail page.
    await expect(
      this.page.getByText(name, { exact: false }).first(),
    ).toBeVisible({ timeout: 30_000 });
  }

  /** Edit a metric's first expression from its detail page. */
  async editExpression(
    namespaceId: string,
    name: string,
    newExpression: string,
  ): Promise<void> {
    await this.page.goto(
      `/namespaces/${namespaceId}/metrics/${encodeURIComponent(name)}/edit`,
    );
    await expect(
      this.page.getByRole("heading", { name: `Edit ${name}` }),
    ).toBeVisible();
    const expr = this.page.getByPlaceholder("SUM(orders.total_amount)").first();
    await expr.fill(newExpression);
    await this.page.getByRole("button", { name: "Save changes" }).click();
    await expect(
      this.page.getByText(name, { exact: false }).first(),
    ).toBeVisible({ timeout: 30_000 });
  }

  /** The form's Display label field. */
  get labelInput() {
    return this.page.getByPlaceholder("Monthly revenue");
  }

  /** Set a metric's display label from its edit page; "" removes it. */
  async editLabel(
    namespaceId: string,
    name: string,
    label: string,
  ): Promise<void> {
    await this.page.goto(
      `/namespaces/${namespaceId}/metrics/${encodeURIComponent(name)}/edit`,
    );
    await expect(
      this.page.getByRole("heading", { name: `Edit ${name}` }),
    ).toBeVisible();
    // The heading comes from the URL; wait for the stored metric to fill the
    // form, or Save submits an empty one.
    await expect(this.page.getByPlaceholder("monthly_revenue")).toHaveValue(
      name,
    );
    await this.labelInput.fill(label);
    await this.page.getByRole("button", { name: "Save changes" }).click();
    // A saved edit lands on the detail page. Wait for it there: the edit
    // page's own "Edit <name>" heading would match a non-exact name.
    await this.page.waitForURL((url) => !url.pathname.endsWith("/edit"), {
      timeout: 30_000,
    });
    await expect(
      this.page.getByRole("heading", {
        name: label || name,
        exact: true,
        level: 1,
      }),
    ).toBeVisible();
  }

  /** Delete a metric via the list's bulk-delete confirmation modal. */
  async delete(namespaceId: string, name: string): Promise<void> {
    await this.gotoList(namespaceId);
    await this.rowByName(name).getByRole("checkbox").check();
    await this.page
      .getByRole("button", { name: /Delete( \d+ selected)?/ })
      .click();
    const modal = this.page.getByRole("dialog");
    await expect(modal).toBeVisible();
    await modal.getByRole("button", { name: "Delete" }).click();
    // The modal closes once the delete returns, which takes longer than the
    // default 5 s on a deployed stack.
    await expect(modal).toBeHidden({ timeout: 30_000 });
  }

  /** Export metrics as OSI; returns the download's suggested name. */
  async exportOsi(): Promise<string> {
    const downloadPromise = this.page.waitForEvent("download");
    await this.page.getByRole("button", { name: "Export OSI" }).click();
    const download = await downloadPromise;
    return download.suggestedFilename();
  }

  /** Open the Import OSI modal and upload a YAML file. */
  async importOsi(filePath: string): Promise<void> {
    await this.page.getByRole("button", { name: "Import OSI" }).click();
    const modal = this.page.getByRole("dialog");
    await expect(modal).toBeVisible();
    await modal.locator('input[type="file"]').setInputFiles(filePath);
    await modal.getByRole("button", { name: "Import" }).click();
  }
}
