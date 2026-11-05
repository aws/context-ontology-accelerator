// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * A metric's display label: set on create, shown in the list and on the detail
 * page with the name still visible, then removed. Creating a metric needs an
 * APPROVED database source, so this runs in an existing namespace that has one
 * (E2E_METRIC_NAMESPACE_ID) and deletes the metric it creates.
 */
const NAMESPACE_ID = process.env.E2E_METRIC_NAMESPACE_ID;

test.describe("metrics: display label", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);
  test.skip(
    !NAMESPACE_ID,
    "E2E_METRIC_NAMESPACE_ID not set (a namespace with an APPROVED database source) — skipping",
  );
  test.setTimeout(180_000);

  let created: { namespaceId: string; name: string } | undefined;

  test.afterEach(async ({ metricsPage }) => {
    if (created) {
      await metricsPage.delete(created.namespaceId, created.name);
      created = undefined;
    }
  });

  test("set, show and remove a display label", async ({
    page,
    metricsPage,
  }, testInfo) => {
    const stamp = Date.now();
    // Metric names allow letters, digits and underscores only.
    const name = `e2e_label_${stamp}_${testInfo.workerIndex}`;
    const label = `売上合計 ${stamp}`;

    const namespaceId = NAMESPACE_ID ?? "";
    await metricsPage.gotoList(namespaceId);
    await metricsPage.create(name, {
      label,
      description: "E2E display label check",
      expression: "SELECT COUNT(*) FROM orders",
    });
    created = { namespaceId, name };

    // Detail: the label is the heading, and the name is still shown.
    await expect(
      page.getByRole("heading", { name: label, exact: true, level: 1 }),
    ).toBeVisible();
    await expect(
      page.getByRole("main").getByText(name, { exact: true }),
    ).toBeVisible();

    // List: the label links to the metric, with the name on its own line
    // underneath rather than run into the label.
    await metricsPage.gotoList(namespaceId);
    const row = metricsPage.rowByName(name);
    const link = row.getByRole("link", { name: label });
    const nameLine = row.getByText(name, { exact: true });
    await expect(link).toBeVisible();
    await expect(nameLine).toBeVisible();
    const linkBox = await link.boundingBox();
    const nameBox = await nameLine.boundingBox();
    expect(nameBox?.y ?? 0).toBeGreaterThanOrEqual(
      (linkBox?.y ?? 0) + (linkBox?.height ?? 0) - 1,
    );

    // Clearing the field removes the label: the name is the heading again.
    await metricsPage.editLabel(namespaceId, name, "");
    await metricsPage.gotoList(namespaceId);
    await expect(
      metricsPage.rowByName(name).getByRole("link", { name }),
    ).toBeVisible();
    await expect(metricsPage.rowByName(name).getByText(label)).toHaveCount(0);
  });
});
