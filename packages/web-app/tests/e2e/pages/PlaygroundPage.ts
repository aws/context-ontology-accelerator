// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, type Page } from "@playwright/test";

/**
 * Page Object for the Playground (Playground) chat (/playground).
 * Uses SSE streaming for query responses. Input is a
 * Cloudscape Input labelled "Query"; submit is the "Run query" icon button.
 * Responses stream into the chat list, so assertions are on visible text.
 */
export class PlaygroundPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto("/playground");
    await expect(
      this.page.getByRole("heading", {
        name: "Playground",
      }),
    ).toBeVisible();
  }

  get queryInput() {
    return this.page.getByRole("textbox", { name: "Query" });
  }

  get runButton() {
    return this.page.getByRole("button", { name: "Run query" });
  }

  /**
   * Wait until the playground is ready for input. The Query input is enabled
   * only once connected (Playground's `isReady`), so it's a stable ready signal
   * regardless of existing chat history.
   */
  async waitForConnected(): Promise<void> {
    await expect(this.queryInput).toBeEnabled({ timeout: 30_000 });
  }

  /** Submit a query and wait for it to echo into the chat. */
  async submitQuery(query: string): Promise<void> {
    await this.queryInput.fill(query);
    await this.runButton.click();
    await expect(
      this.page.getByText(query, { exact: false }).first(),
    ).toBeVisible();
  }

  /** Assert a response streamed in (input clears once a new bubble appears). */
  async expectResponse(): Promise<void> {
    await expect(this.queryInput).toHaveValue("", { timeout: 30_000 });
  }

  async switchNamespace(displayName: string): Promise<void> {
    await this.namespaceSwitcher.click();
    await this.page.getByRole("menuitem", { name: displayName }).click();
  }

  /**
   * Top-nav namespace switcher. Cloudscape exposes the ariaLabel ("Namespace
   * switcher") as the accessible name while the visible text is "Namespace:
   * <name>" — match the aria label for stability.
   */
  get namespaceSwitcher() {
    return this.page.getByRole("button", { name: "Namespace switcher" });
  }

  /** Assert a submitted query is still in the conversation. */
  async expectInHistory(query: string): Promise<void> {
    await expect(
      this.page.getByText(query, { exact: false }).first(),
    ).toBeVisible();
  }

  /**
   * Assert the conversation has been cleared. "New chat" clears only after the
   * server confirms a new session (an HTTP round-trip), which can lag while
   * prior turns are still streaming — so allow a generous window.
   */
  async expectHistoryCleared(query: string): Promise<void> {
    await expect(this.page.getByText(query, { exact: false })).toHaveCount(0, {
      timeout: 30_000,
    });
  }

  async newChat(): Promise<void> {
    await this.page.getByRole("button", { name: "New chat" }).click();
  }

  /**
   * Open the full Trace detail page for the latest answer: "Show trace" opens
   * the Rationale side panel, whose "Open full trace" link routes to
   * /playground/trace/:sessionId/:requestId.
   */
  async openLatestFullTrace(): Promise<void> {
    await this.page
      .getByRole("button", { name: "Show trace" })
      .last()
      .click({ timeout: 90_000 });
    await this.page.getByRole("button", { name: "Open full trace" }).click();
    await expect(
      this.page.getByRole("heading", { name: "Trace detail" }),
    ).toBeVisible();
  }
}
