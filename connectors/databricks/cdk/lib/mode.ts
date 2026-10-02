// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { optionalEnv } from "coa-connector-cdk";
import { CONFIG_SOURCE_ENV_VAR, ConnectorMode, MANAGED_MODE } from "./constants";

/** What an unset {@link CONFIG_SOURCE_ENV_VAR} selects, matching the jar's own default. */
export const ENVIRONMENT_MODE: ConnectorMode = "environment";

const MODES: readonly ConnectorMode[] = [ENVIRONMENT_MODE, MANAGED_MODE];

/**
 * Which shape this deploy builds, from `DATABRICKS_CONFIG_SOURCE`.
 *
 * The same variable the app then sets on the Lambda, so synth and runtime cannot disagree about the
 * mode. Unset means `environment`, as in the jar, so a deployed stage-1 stack is unchanged.
 *
 * Unlike the jar's operational settings, a typo is refused rather than defaulted: defaulting would
 * choose the single-endpoint mode for a deployment that asked for the multiplexed one, and the two
 * differ in which variables the stack goes on to require.
 *
 * @throws Error on any value that is not a mode.
 */
export function connectorMode(): ConnectorMode {
  const raw = optionalEnv(CONFIG_SOURCE_ENV_VAR);
  if (raw === undefined || raw.trim() === "") {
    return ENVIRONMENT_MODE;
  }
  const value = raw.trim().toLowerCase();
  const mode = MODES.find((candidate) => candidate === value);
  if (mode === undefined) {
    throw new Error(
      `${CONFIG_SOURCE_ENV_VAR}="${raw}" is not a mode this connector has. Expected ` +
        `"${ENVIRONMENT_MODE}" — one endpoint from this deployment's own DATABRICKS_* variables, ` +
        `which is the default when the variable is unset — or "${MANAGED_MODE}", one endpoint per ` +
        `Athena catalog resolved from Parameter Store, which COA deploys. The two modes require ` +
        `different variables, so this is refused rather than defaulted.`,
    );
  }
  return mode;
}
