// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { expect, test } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";
import { LanguageMenu } from "../../pages/LanguageMenu";
import type { Page, Route } from "@playwright/test";

/**
 * Japanese console: API values (namespace status, VKG health, ontology type)
 * and Playground errors render as Japanese copy, not as raw API values or the
 * server's English sentence.
 *
 * Read-only against the deployed backend. The namespace list and namespace
 * detail are real API responses; only the ontology list and the Playground's
 * streaming response are served by the test (page.route), so the spec does
 * not depend on which ontologies exist or on the signed-in user lacking a
 * grant. Nothing is written to the backend.
 */

/** API values that must never be shown verbatim in Japanese mode. */
const RAW_NAMESPACE_STATUSES = [
  "ACTIVE",
  "ARCHIVED",
  "DELETING",
  "DELETED",
  "DELETE_FAILED",
];
const RAW_VKG_HEALTH = [
  "HEALTHY",
  "DEGRADED",
  "UNAVAILABLE",
  "PROVISIONING",
  "UNKNOWN",
];

async function switchToJapanese(page: Page): Promise<LanguageMenu> {
  const menu = new LanguageMenu(page);
  await menu.gotoNamespaces();
  await menu.choose("日本語");
  await menu.expectDocumentLang("ja");
  return menu;
}

/** The first namespace row's detail link, or null when the list is empty. */
async function firstNamespaceHref(page: Page): Promise<string | null> {
  // Row links only: Cloudscape may render a separate sticky-header table.
  const link = page.locator('main table a[href^="/namespaces/"]').first();
  // The list loads after the table frame renders; give it time to fill.
  try {
    await link.waitFor({ state: "visible", timeout: 15_000 });
  } catch {
    return null;
  }
  return link.getAttribute("href");
}

test.describe("settings: Japanese console strings", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);

  test.afterEach(async ({ page }) => {
    // Leave the browser context in English for later specs in this worker.
    await new LanguageMenu(page).choose("English").catch(() => {});
  });

  test("namespace list and detail show status and VKG health in Japanese", async ({
    page,
  }) => {
    await switchToJapanese(page);
    const href = await firstNamespaceHref(page);
    test.skip(href === null, "no namespace visible to the E2E user");

    const rows = page.locator("main table tbody");
    for (const raw of RAW_NAMESPACE_STATUSES) {
      await expect(rows.getByText(raw, { exact: true })).toHaveCount(0);
    }
    // The status cell uses the same word as the (already translated) filter.
    await expect(rows.getByText("アクティブ").first()).toBeVisible();

    await page.goto(href!);
    const summary = page.locator("main");
    await expect(page.getByText("VKG の状態").first()).toBeVisible();
    for (const raw of [...RAW_NAMESPACE_STATUSES, ...RAW_VKG_HEALTH]) {
      await expect(summary.getByText(raw, { exact: true })).toHaveCount(0);
    }
  });

  test("namespace list finds rows by the Japanese status word", async ({
    page,
  }) => {
    await switchToJapanese(page);
    const href = await firstNamespaceHref(page);
    test.skip(href === null, "no namespace visible to the E2E user");

    const rows = page.locator("main table tbody tr");
    const activeRows = rows.filter({ hasText: "アクティブ" });
    test.skip(
      (await activeRows.count()) === 0,
      "no active namespace visible to the E2E user",
    );

    // Type the word shown in the status cell, not the hidden API value.
    await page.getByPlaceholder("名前空間を検索").fill("アクティブ");
    await expect(activeRows.first()).toBeVisible();
    const shown = await rows.count();
    expect(shown).toBeGreaterThan(0);
    for (let i = 0; i < shown; i++) {
      await expect(rows.nth(i)).toContainText("アクティブ");
    }
  });

  test("namespace ontologies tab shows the ontology type in Japanese", async ({
    page,
  }) => {
    await page.route(/\/namespaces\/[^/]+\/ontologies(\?.*)?$/, (route) =>
      route.request().method() === "GET" &&
      route.request().resourceType() !== "document"
        ? route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              ontologies: [
                {
                  ontologyId: "urn:e2e:uploaded",
                  title: "e2e uploaded",
                  ontologyType: "user_created",
                  classCount: 1,
                  propertyCount: 0,
                },
                {
                  ontologyId: "urn:e2e:induced",
                  title: "e2e induced",
                  ontologyType: "induced",
                  classCount: 1,
                  propertyCount: 0,
                },
              ],
            }),
          })
        : route.fallback(),
    );
    await switchToJapanese(page);
    const href = await firstNamespaceHref(page);
    test.skip(href === null, "no namespace visible to the E2E user");

    await page.goto(href!);
    await page.getByRole("tab", { name: "オントロジー" }).click();
    await expect(page.getByText("e2e uploaded")).toBeVisible();
    await expect(page.getByText("user_created", { exact: true })).toHaveCount(
      0,
    );
    await expect(page.getByText("induced", { exact: true })).toHaveCount(0);
  });

  test("Playground shows an access deny in Japanese without Retry", async ({
    page,
  }) => {
    // Serve the deny the server sends when namespace admission fails, so the
    // spec does not need a second, grant-less user.
    await page.route(/bedrock-agentcore\..*\/invocations/, (route: Route) => {
      const body = route.request().postData() ?? "";
      if (route.request().method() !== "POST" || !body.includes('"stream"')) {
        return route.fallback();
      }
      let requestId = "e2e";
      try {
        const parsed: unknown = JSON.parse(body);
        if (
          typeof parsed === "object" &&
          parsed !== null &&
          "requestId" in parsed &&
          typeof parsed.requestId === "string"
        ) {
          requestId = parsed.requestId;
        }
      } catch {
        // keep the placeholder id
      }
      const event = {
        type: "error",
        requestId,
        timestamp: new Date().toISOString(),
        payload: {
          error: "AccessDenied",
          message: "Access denied",
          statusCode: 403,
        },
      };
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: `data: ${JSON.stringify(event)}\n\n`,
      });
    });
    await switchToJapanese(page);
    await page.goto("/playground");
    await expect(
      page.getByRole("heading", { name: "プレイグラウンド" }),
    ).toBeVisible();
    const input = page.getByRole("textbox", { name: "クエリ" });
    await expect(input).toBeEnabled({ timeout: 30_000 });
    await input.fill("注文は何件ありますか");
    await page.getByRole("button", { name: "クエリを実行" }).click();

    await expect(page.getByText("アクセスが拒否されました")).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByText("Access denied", { exact: true })).toHaveCount(
      0,
    );
    await expect(page.getByRole("button", { name: "再試行" })).toHaveCount(0);
  });
});
