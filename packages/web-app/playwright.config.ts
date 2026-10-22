// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the Context Ontology Accelerator web-app E2E suite.
 *
 * Topology:
 *   - "setup"  runs auth.setup.ts once, persisting a storageState to .auth/user.json
 *   - "auth"   tests the login gate itself (no stored state)
 *   - feature-area projects depend on "setup" and reuse the stored state
 *
 * The base URL comes from E2E_BASE_URL (CI passes the deployed environment URL),
 * defaulting to the local Vite dev server. Credentials are read from the
 * environment by the fixtures; when absent, environment-dependent tests skip.
 *
 * Paths are relative to this config file (no __dirname — the package is ESM).
 */

const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const isCI = !!process.env.CI;
const storageState = "tests/e2e/.auth/user.json";

const featureProject = (name: string) => ({
  name,
  testDir: `tests/e2e/specs/${name}`,
  use: { ...devices["Desktop Chrome"], storageState },
  dependencies: ["setup"],
});

export default defineConfig({
  testDir: "tests/e2e/specs",
  fullyParallel: true,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  workers: isCI ? 4 : undefined,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
    ["junit", { outputFile: "playwright-report/junit.xml" }],
  ],
  outputDir: "test-results",
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "setup",
      testDir: "tests/e2e",
      testMatch: /auth\.setup\.ts/,
      // The setup project is the ONLY place the test-user password is typed
      // into the Cognito form. Disable trace/video here so the password (in the
      // fill action and the sign-in network POST) can never end up in uploaded
      // CI artifacts. Feature projects reuse the stored session and never see
      // the password, so they keep trace-on-retry.
      use: { ...devices["Desktop Chrome"], trace: "off", video: "off" },
    },
    {
      name: "auth",
      testDir: "tests/e2e/specs/auth",
      // The auth specs also type the password (they exercise the login gate
      // directly), so disable trace/video here too to keep secrets out of
      // uploaded artifacts. Screenshots mask password inputs as dots.
      use: { ...devices["Desktop Chrome"], trace: "off", video: "off" },
    },
    featureProject("namespaces"),
    featureProject("data-sources"),
    featureProject("metrics"),
    featureProject("grants"),
    featureProject("playground"),
    featureProject("system-health"),
    featureProject("settings"),
  ],
});
