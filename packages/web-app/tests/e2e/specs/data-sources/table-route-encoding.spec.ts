// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

const NAMESPACE_ID = "00000000-0000-4000-8000-000000001090";
const SOURCE_ID = "source-route-encoding";
const TABLE_ID = "売上 #2026% plan";

test.describe("data-sources: encoded table route", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);

  test("opens TableDetail with one encoded path segment", async ({ page }) => {
    const env = readE2EEnv();
    if (!env) throw new Error(MISSING_ENV_REASON);

    const corsHeaders = {
      "access-control-allow-origin": new URL(env.baseURL).origin,
      "access-control-allow-headers": "authorization,content-type",
      "access-control-allow-methods": "GET,OPTIONS",
    };
    const fulfillJson = (body: object) => ({
      status: 200,
      contentType: "application/json",
      headers: corsHeaders,
      body: JSON.stringify(body),
    });

    await page.route("**/namespaces**", async (route) => {
      const request = route.request();
      if (request.method() === "OPTIONS") {
        await route.fulfill({ status: 204, headers: corsHeaders });
        return;
      }
      if (request.method() !== "GET") {
        await route.continue();
        return;
      }

      const pathname = decodeURIComponent(new URL(request.url()).pathname);
      const sourcePath = `/namespaces/${NAMESPACE_ID}/sources/${SOURCE_ID}`;

      if (pathname.endsWith(`${sourcePath}/tables/${TABLE_ID}`)) {
        await route.fulfill(
          fulfillJson({
            tableId: TABLE_ID,
            name: TABLE_ID,
            database: "sales",
            reviewStatus: "PENDING_REVIEW",
            columns: [],
          }),
        );
        return;
      }
      if (pathname.endsWith(`${sourcePath}/tables`)) {
        await route.fulfill(
          fulfillJson({
            items: [
              {
                tableId: TABLE_ID,
                name: TABLE_ID,
                database: "sales",
                reviewStatus: "PENDING_REVIEW",
                columnCount: 1,
                columnsApproved: 0,
              },
            ],
            skippedAssets: 0,
          }),
        );
        return;
      }
      if (pathname.endsWith(`${sourcePath}/scan-jobs`)) {
        await route.fulfill(fulfillJson({ items: [] }));
        return;
      }
      if (pathname.endsWith(sourcePath)) {
        await route.fulfill(
          fulfillJson({
            sourceId: SOURCE_ID,
            namespaceId: NAMESPACE_ID,
            name: "Encoded route source",
            sourceType: "DATABASE",
            sourceSubType: "GLUE",
            status: "PENDING_REVIEW",
            createdAt: "2026-09-27T00:00:00Z",
            databaseDetails: {
              metadataEnrichmentEnabled: false,
            },
          }),
        );
        return;
      }
      if (pathname.endsWith("/namespaces")) {
        await route.fulfill(
          fulfillJson({
            namespaces: [
              {
                namespaceId: NAMESPACE_ID,
                name: "route-encoding",
                displayName: "Route encoding",
                status: "ACTIVE",
              },
            ],
          }),
        );
        return;
      }

      await route.continue();
    });

    await page.goto(`/namespaces/${NAMESPACE_ID}/sources/${SOURCE_ID}`);
    await page.getByRole("button", { name: TABLE_ID }).click();

    await expect
      .poll(() => new URL(page.url()).pathname)
      .toBe(
        `/namespaces/${NAMESPACE_ID}/sources/${SOURCE_ID}/tables/${encodeURIComponent(TABLE_ID)}`,
      );
    await expect(
      page.getByRole("heading", { name: TABLE_ID, exact: false }).first(),
    ).toBeVisible();
  });
});
