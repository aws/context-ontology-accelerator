// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * E2E environment resolution. Reads the target URL and test-user credentials
 * from env vars (never hardcoded): E2E_BASE_URL, E2E_USERNAME, E2E_PASSWORD.
 */

export interface E2EEnv {
  baseURL: string;
  username: string;
  password: string;
}

/** Resolved env, or null when any required var is missing (callers test.skip). */
export function readE2EEnv(): E2EEnv | null {
  const baseURL = process.env.E2E_BASE_URL;
  const username = process.env.E2E_USERNAME;
  const password = process.env.E2E_PASSWORD;

  if (!baseURL || !username || !password) {
    return null;
  }
  return { baseURL, username, password };
}

/** Reason used in test.skip(...) when the env is absent. */
export const MISSING_ENV_REASON =
  "E2E env not configured (E2E_BASE_URL / E2E_USERNAME / E2E_PASSWORD) — skipping";

/** Glue connection config for the source lifecycle test. */
export interface GlueSourceEnv {
  catalogId: string;
  databaseName: string;
}

// The catalog id is an AWS account id, so it is env-only with no default —
// the same rule the credentials above follow. The database name keeps a
// default because it names a dataset, not an account.
const DEFAULT_GLUE_DATABASE_NAME = "hcp360";

/** Reason used in test.skip(...) when the Glue catalog is not configured. */
export const MISSING_GLUE_ENV_REASON =
  "Glue source env not configured (E2E_GLUE_CATALOG_ID) — skipping";

/** Resolved Glue config, or null when the catalog id is missing (callers test.skip). */
export function readGlueSourceEnv(): GlueSourceEnv | null {
  const catalogId = process.env.E2E_GLUE_CATALOG_ID;
  if (!catalogId) {
    return null;
  }
  return {
    catalogId,
    databaseName:
      process.env.E2E_GLUE_DATABASE_NAME ?? DEFAULT_GLUE_DATABASE_NAME,
  };
}

/** Like readGlueSourceEnv but throws — used inside a test already gated on it. */
export function requireGlueSourceEnv(): GlueSourceEnv {
  const env = readGlueSourceEnv();
  if (!env) {
    throw new Error(
      "Glue source env is not configured. Set E2E_GLUE_CATALOG_ID (and optionally E2E_GLUE_DATABASE_NAME).",
    );
  }
  return env;
}
