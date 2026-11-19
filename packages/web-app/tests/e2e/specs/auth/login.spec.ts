// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { test, expect } from "../../fixtures/test";
import { LoginPage } from "../../pages/LoginPage";
import { readE2EEnv, MISSING_ENV_REASON } from "../../fixtures/env";

/**
 * Verifies interactive Cognito login lands on an authenticated route. The only
 * spec that logs in interactively; the rest reuse the stored auth state.
 */
test.describe("auth: login", () => {
  test("logs in via Cognito and lands authenticated", async ({ page }) => {
    const env = readE2EEnv();
    test.skip(!env, MISSING_ENV_REASON);

    const login = new LoginPage(page);
    await login.login(env!.username, env!.password);
    await expect(page).toHaveURL(/\/$|\/namespaces/);
  });
});
