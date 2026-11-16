// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, vi } from "vitest";
import { runSequential } from "./run-sequential";

describe("runSequential", () => {
  it("runs operations strictly one-at-a-time (no overlap)", async () => {
    let inFlight = 0;
    let maxConcurrent = 0;
    const startOrder: number[] = [];

    const run = (n: number) =>
      new Promise<void>((resolve) => {
        inFlight += 1;
        maxConcurrent = Math.max(maxConcurrent, inFlight);
        startOrder.push(n);
        // Defer resolution to a later tick so any concurrent scheduling
        // would be observed as overlap.
        setTimeout(() => {
          inFlight -= 1;
          resolve();
        }, 0);
      });

    const { succeeded, failed } = await runSequential([1, 2, 3, 4], run);

    expect(maxConcurrent).toBe(1);
    expect(startOrder).toEqual([1, 2, 3, 4]);
    expect(succeeded).toEqual([1, 2, 3, 4]);
    expect(failed).toEqual([]);
  });

  it("continues past failures and partitions succeeded vs failed in order", async () => {
    const run = vi.fn((n: number) =>
      n % 2 === 0 ? Promise.reject(new Error("boom")) : Promise.resolve(n),
    );

    const { succeeded, failed } = await runSequential([1, 2, 3, 4, 5], run);

    expect(run).toHaveBeenCalledTimes(5);
    expect(succeeded).toEqual([1, 3, 5]);
    expect(failed).toEqual([2, 4]);
  });

  it("handles an empty list without invoking the operation", async () => {
    const run = vi.fn();

    const { succeeded, failed } = await runSequential([], run);

    expect(run).not.toHaveBeenCalled();
    expect(succeeded).toEqual([]);
    expect(failed).toEqual([]);
  });
});
