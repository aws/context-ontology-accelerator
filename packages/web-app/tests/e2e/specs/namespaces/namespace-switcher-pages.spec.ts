// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Request, Route } from "@playwright/test";
import { test, expect } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * A namespace that is NOT on the first page of the namespace list must still
 * become the current namespace when opened by URL: the top-bar switcher shows
 * and lists it, the MCP page shows it, and a Playground question is sent to it.
 *
 * The list API returns 25 namespaces per page. Creating 26+ namespaces per run
 * would be slow (each one provisions backend resources) and would leave the
 * outcome dependent on how many other namespaces the test account can see, so
 * the browser's ListNamespaces responses are rewritten instead: the namespace
 * this test creates is removed from every real page and served alone on one
 * extra page after the last real page. The detail lookup (GET
 * /namespaces/{id}) and every other call go to the real API unchanged.
 */

const EXTRA_PAGE_TOKEN = "e2e-extra-page";

interface NamespaceRow {
  namespaceId?: string;
}

interface ListPage {
  namespaces?: NamespaceRow[];
  nextToken?: string;
}

/** The ListNamespaces API call (not the /namespaces page or a preflight). */
function isListNamespacesCall(request: Request): boolean {
  return (
    request.method() === "GET" &&
    ["fetch", "xhr"].includes(request.resourceType()) &&
    new URL(request.url()).pathname.endsWith("/namespaces")
  );
}

test.describe("namespaces: switcher beyond the first page", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);
  test.setTimeout(180_000);

  test("a namespace on a later list page becomes the current one", async ({
    page,
    namespacesPage,
    uniqueName,
  }) => {
    const name = uniqueName("ns-page2");
    await namespacesPage.gotoList();
    await namespacesPage.create(name);
    await namespacesPage.openDetails(name);
    const id = namespacesPage.currentNamespaceId();

    let target: NamespaceRow | undefined;
    let extraPageServed = 0;
    const listPath = (url: URL) => url.pathname.endsWith("/namespaces");
    await page.route(listPath, async (route: Route) => {
      if (!isListNamespacesCall(route.request())) {
        await route.fallback();
        return;
      }
      const url = new URL(route.request().url());
      if (url.searchParams.get("nextToken") === EXTRA_PAGE_TOKEN) {
        extraPageServed += 1;
        await route.fulfill({ json: { namespaces: target ? [target] : [] } });
        return;
      }
      const response = await route.fetch();
      const body: ListPage = await response.json();
      const rows = body.namespaces ?? [];
      target = rows.find((n) => n.namespaceId === id) ?? target;
      const json: ListPage = {
        ...body,
        namespaces: rows.filter((n) => n.namespaceId !== id),
        nextToken: body.nextToken ?? EXTRA_PAGE_TOKEN,
      };
      await route.fulfill({ response, json });
    });

    // Capture the namespace a Playground question would go to, and abort the
    // question so nothing is actually asked. Other runtime calls (sessions,
    // history) pass through.
    const asked: string[] = [];
    await page.route(/bedrock-agentcore.*invocations/, async (route: Route) => {
      const raw = route.request().postData() ?? "";
      let body: { query?: string; namespace?: string } = {};
      try {
        body = JSON.parse(raw);
      } catch {
        // not JSON: not a question
      }
      if (body.query) {
        asked.push(body.namespace ?? "");
        await route.abort();
        return;
      }
      await route.continue();
    });

    // Fresh app load, so the namespace list is fetched through the rewrite.
    await page.goto(`/namespaces/${id}`);
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();

    const switcher = page.getByRole("button", { name: "Namespace switcher" });
    await expect(switcher).toContainText(name);
    await switcher.click();
    await expect(page.getByRole("menuitem", { name })).toBeVisible();
    await page.keyboard.press("Escape");
    expect(
      target,
      "the created namespace was in a real list page",
    ).toBeTruthy();
    expect(extraPageServed, "the app loaded the extra page").toBeGreaterThan(0);

    // In-app navigation keeps the chosen namespace (a reload would not).
    await page.locator("nav a[href='/mcp']").first().click();
    await expect(page.getByText(`(${id})`)).toBeVisible();

    await page.locator("nav a[href='/playground']").first().click();
    await expect(switcher).toContainText(name);
    const query = page.getByRole("textbox", { name: "Query" });
    await expect(query).toBeEnabled({ timeout: 30_000 });
    await query.fill("ping");
    await page.getByRole("button", { name: "Run query" }).click();
    await expect
      .poll(() => asked.length, { timeout: 15_000 })
      .toBeGreaterThan(0);
    expect(asked.every((n) => n === id)).toBe(true);
  });
});
