// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { CONFIG_SOURCE_ENV_VAR, MANAGED_MODE } from "../lib/constants";
import { ENVIRONMENT_MODE, connectorMode } from "../lib/mode";

describe("the mode selector", () => {
  let saved: string | undefined;

  beforeEach(() => {
    saved = process.env[CONFIG_SOURCE_ENV_VAR];
    delete process.env[CONFIG_SOURCE_ENV_VAR];
  });

  afterEach(() => {
    if (saved === undefined) {
      delete process.env[CONFIG_SOURCE_ENV_VAR];
    } else {
      process.env[CONFIG_SOURCE_ENV_VAR] = saved;
    }
  });

  it("defaults to environment mode, so a deployed stage-1 stack is unchanged", () => {
    expect(connectorMode()).toBe(ENVIRONMENT_MODE);
  });

  it("treats a blank value as unset, since a shell and CDK disagree about empty", () => {
    process.env[CONFIG_SOURCE_ENV_VAR] = "   ";
    expect(connectorMode()).toBe(ENVIRONMENT_MODE);
  });

  it.each([MANAGED_MODE, "COA-Managed", " coa-managed "])(
    "selects managed mode from %p",
    (raw) => {
      process.env[CONFIG_SOURCE_ENV_VAR] = raw;
      expect(connectorMode()).toBe(MANAGED_MODE);
    },
  );

  it("selects environment mode from its own spelling", () => {
    process.env[CONFIG_SOURCE_ENV_VAR] = ENVIRONMENT_MODE;
    expect(connectorMode()).toBe(ENVIRONMENT_MODE);
  });

  // A typo must not silently pick the single-endpoint mode: the two modes require different
  // variables, so the stack would then fail on a missing DATABRICKS_* rather than on the typo.
  it.each(["coa_managed", "managed", "coa-managed-", "environmnet"])(
    "refuses %p rather than defaulting",
    (raw) => {
      process.env[CONFIG_SOURCE_ENV_VAR] = raw;
      expect(() => connectorMode()).toThrow(
        new RegExp(`${CONFIG_SOURCE_ENV_VAR}="${raw}" is not a mode`),
      );
    },
  );
});
