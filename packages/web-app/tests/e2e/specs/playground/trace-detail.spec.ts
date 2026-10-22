// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from "../../fixtures/test";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * Trace detail for a Tier-2 NL→SQL answer: the Tier outcome names the strategy
 * that ran, the same way the Rationale panel does, instead of "ontology".
 *
 * Needs a namespace that answers at Tier 2 — an induced ontology over a source
 * the question can be answered from. Set E2E_TIER2_NAMESPACE to its display name
 * and E2E_TIER2_QUERY to a question it answers through NL→SQL.
 */
const tier2Namespace = process.env.E2E_TIER2_NAMESPACE;
const tier2Query =
  process.env.E2E_TIER2_QUERY ?? "what is the average order amount";

test.describe("playground: trace detail", () => {
  test.skip(!readE2EEnv(), MISSING_ENV_REASON);
  test.skip(
    !tier2Namespace,
    "E2E_TIER2_NAMESPACE not set (needs a namespace that answers at Tier 2)",
  );
  test.setTimeout(180_000);

  test("NL→SQL answer: Tier outcome names the strategy", async ({
    page,
    playgroundPage,
  }) => {
    await playgroundPage.goto();
    await playgroundPage.waitForConnected();
    await playgroundPage.switchNamespace(tier2Namespace!);

    await playgroundPage.submitQuery(tier2Query);
    await playgroundPage.openLatestFullTrace();

    await expect(
      page.getByText("Tier 2 — Structured Query (NL→SQL)"),
    ).toBeVisible();
    await expect(page.getByText(/Tier 2 — ontology/)).toHaveCount(0);
  });
});
