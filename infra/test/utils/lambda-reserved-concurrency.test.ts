// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import { resolveLambdaReservedConcurrency } from "../../lib/context";
import { DEFAULT_LAMBDA_RESERVED_CONCURRENCY } from "../../lib/constants";

function nodeWith(context: Record<string, unknown>): cdk.App {
  return new cdk.App({ context });
}

describe("resolveLambdaReservedConcurrency", () => {
  test("defaults to DEFAULT_LAMBDA_RESERVED_CONCURRENCY when unset", () => {
    expect(resolveLambdaReservedConcurrency(nodeWith({}).node)).toBe(
      DEFAULT_LAMBDA_RESERVED_CONCURRENCY,
    );
  });

  test("returns the configured value", () => {
    expect(
      resolveLambdaReservedConcurrency(
        nodeWith({ lambda_reserved_concurrency: 3 }).node,
      ),
    ).toBe(3);
  });

  test("maps 0 to undefined (omit the reservation)", () => {
    expect(
      resolveLambdaReservedConcurrency(
        nodeWith({ lambda_reserved_concurrency: 0 }).node,
      ),
    ).toBeUndefined();
  });

  test("accepts a numeric string from the CLI --context bridge", () => {
    // deploy.sh passes `--context lambda_reserved_concurrency=$VAR`, which
    // arrives as a string; Number() must coerce it.
    expect(
      resolveLambdaReservedConcurrency(
        nodeWith({ lambda_reserved_concurrency: "7" }).node,
      ),
    ).toBe(7);
    expect(
      resolveLambdaReservedConcurrency(
        nodeWith({ lambda_reserved_concurrency: "0" }).node,
      ),
    ).toBeUndefined();
  });

  test.each([-1, 2.5, "abc", "5x"])(
    "throws on invalid value %p",
    (bad) => {
      expect(() =>
        resolveLambdaReservedConcurrency(
          nodeWith({ lambda_reserved_concurrency: bad }).node,
        ),
      ).toThrow(/non-negative integer/);
    },
  );
});
