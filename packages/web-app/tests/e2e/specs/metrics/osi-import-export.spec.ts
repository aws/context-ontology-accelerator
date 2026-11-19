// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * OSI export control + import modal wiring. Export is disabled with no metrics,
 * so this creates a fresh namespace and asserts the disabled state — exercising
 * the list/control wiring without backend metric data. Deeper import validation
 * needs an approved source fixture.
 */
test.describe("metrics: OSI import/export", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);
  test.setTimeout(120_000);

  test("export control reflects metric availability", async ({
    namespacesPage,
    metricsPage,
    uniqueName,
  }) => {
    const nsName = uniqueName("ns-osi");

    await namespacesPage.gotoList();
    await namespacesPage.create(nsName);
    await namespacesPage.openDetails(nsName);
    const namespaceId = namespacesPage.currentNamespaceId();

    await metricsPage.gotoList(namespaceId);

    // A new namespace has no metrics, so Export OSI is disabled.
    await expect(metricsPage.exportButton).toBeDisabled();
  });
});
