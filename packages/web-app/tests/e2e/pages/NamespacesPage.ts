// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, type Page } from "@playwright/test";
import { NamespaceStatus } from "@coa/control-plane-client";
import { NAMESPACE_STATUS_MESSAGES } from "@utils/namespace-status";

// The list's status cell shows these English words, not the raw API enum, so
// the page object reads them from the same table the UI renders from. These
// specs run in English (no locale in the config or the stored auth state).
const SHOWN_STATUS = NAMESPACE_STATUS_MESSAGES;

/**
 * Page Object for the Namespaces feature (list, create, detail, lifecycle).
 * Routes: /namespaces, /namespaces/create, /namespaces/:id
 *
 * The list is a Cloudscape multi-select table; lifecycle actions go through the
 * "Change status" dropdown (Activate / Archive / Delete), Delete confirms in a
 * modal. The status filter defaults to "All statuses" so archived rows stay
 * visible.
 */
export class NamespacesPage {
  private readonly created = new Set<string>();
  /** Ids of created namespaces — teardown by id works even mid-delete. */
  private readonly createdIds = new Set<string>();

  constructor(private readonly page: Page) {}

  async gotoList(): Promise<void> {
    await this.page.goto("/namespaces");
    await expect(
      this.page.getByRole("heading", { name: "Namespaces", level: 1 }),
    ).toBeVisible();
  }

  rowByName(name: string) {
    return this.page.getByRole("row").filter({ hasText: name });
  }

  /**
   * Create a namespace and wait for it in the list. Creation provisions backend
   * resources (roles, Athena workgroup, project), so the redirect can be slow.
   */
  async create(
    displayName: string,
    language: "en" | "ja" = "en",
  ): Promise<void> {
    // Track up-front so teardown can remove it even if creation succeeds
    // server-side but this method fails before confirming the row.
    this.created.add(displayName);
    await this.page.getByRole("button", { name: "Create namespace" }).click();
    await expect(
      this.page.getByRole("heading", { name: "Create namespace", level: 1 }),
    ).toBeVisible();

    await this.page.getByPlaceholder("e.g., Insurance Prod").fill(displayName);
    const languageLabel = language === "ja" ? "Japanese (ja)" : "English (en)";
    await this.page.getByRole("radio", { name: languageLabel }).check();
    // The form's submit button shares the "Create namespace" label.
    await this.page
      .getByRole("button", { name: "Create namespace" })
      .last()
      .click();
    // The language is immutable, so creation goes through a confirmation modal.
    await this.page
      .getByRole("dialog")
      .getByRole("button", { name: "Confirm and create" })
      .click();

    await expect(
      this.page.getByRole("heading", { name: "Namespaces", level: 1 }),
    ).toBeVisible({ timeout: 60_000 });
    // Filter to the new name so it's on the first page (default size 20).
    await this.filterTo(displayName);
    await expect(this.rowByName(displayName)).toBeVisible();
  }

  async filterTo(text: string): Promise<void> {
    const filter = this.page.getByPlaceholder("Find namespaces");
    await filter.fill(text);
  }

  async select(name: string): Promise<void> {
    await this.filterTo(name);
    await this.rowByName(name).getByRole("checkbox").check();
  }

  /** Apply a "Change status" action (Activate / Archive / Delete) to a row. */
  private async changeStatus(name: string, action: string): Promise<void> {
    await this.select(name);
    await this.page.getByRole("button", { name: "Change status" }).click();
    await this.page.getByRole("menuitem", { name: action }).click();
  }

  async archive(name: string): Promise<void> {
    await this.changeStatus(name, "Archive");
    await this.expectRowStatus(name, SHOWN_STATUS[NamespaceStatus.ARCHIVED]);
  }

  async reactivate(name: string): Promise<void> {
    await this.changeStatus(name, "Activate");
    await this.expectRowStatus(name, SHOWN_STATUS[NamespaceStatus.ACTIVE]);
  }

  /**
   * Delete an archived/delete-failed namespace, confirming in the modal.
   * The modal requires typing the namespace's exact name into a confirmation
   * field before the Delete button enables. Deletion is async and can land
   * in DELETE_FAILED when managed resources (DataZone project, Athena
   * workgroup) fail to tear down; DELETE_FAILED → DELETED is allowed, so
   * retry a few times before giving up. The row shows these statuses as
   * "Delete failed" and "Deleted".
   */
  async delete(name: string): Promise<void> {
    for (let attempt = 0; attempt < 4; attempt++) {
      await this.changeStatus(name, "Delete");
      const modal = this.page.getByRole("dialog");
      await expect(modal).toBeVisible();
      await modal.getByRole("textbox").fill(name);
      const confirmDelete = modal.getByRole("button", { name: "Delete" });
      await expect(confirmDelete).toBeEnabled();
      await confirmDelete.click();
      await expect(modal).toBeHidden();

      // Wait for the async delete: row disappears (deleted) or shows
      // "Delete failed" (retry); anything else is still in progress.
      const deadline = Date.now() + 60_000;
      while (Date.now() < deadline) {
        await this.gotoList();
        await this.filterTo(name);
        const row = this.rowByName(name);
        if ((await row.count()) === 0) {
          this.created.delete(name);
          return;
        }
        const text = (await row.first().textContent()) ?? "";
        // Check "Delete failed" first: a failed row must be retried, not
        // counted as deleted.
        if (text.includes(SHOWN_STATUS[NamespaceStatus.DELETE_FAILED])) break;
        if (text.includes(SHOWN_STATUS[NamespaceStatus.DELETED])) {
          this.created.delete(name);
          return;
        }
        await this.page.waitForTimeout(5_000);
      }
    }
    throw new Error(`namespace "${name}" stuck (DELETE_FAILED after retries)`);
  }

  async expectRowStatus(name: string, status: string): Promise<void> {
    await expect(this.rowByName(name)).toContainText(status);
  }

  async openDetails(name: string): Promise<void> {
    await this.filterTo(name);
    await this.rowByName(name).getByRole("link", { name }).click();
    await expect(
      this.page.getByRole("heading", { name, level: 1 }),
    ).toBeVisible();
    await expect(this.page.getByText("Namespace ID")).toBeVisible();
  }

  /** Read the namespace id from the current detail-page URL. */
  currentNamespaceId(): string {
    const parts = new URL(this.page.url()).pathname.split("/");
    const id = parts[2];
    if (!id) throw new Error(`No namespace id in URL: ${this.page.url()}`);
    // Remember it so teardown can delete by id even mid-delete.
    this.createdIds.add(id);
    return id;
  }

  /** Names created (or attempted), for the fixture's API teardown. */
  createdNames(): string[] {
    return [...this.created];
  }

  createdNamespaceIds(): string[] {
    return [...this.createdIds];
  }
}
