// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

export interface SequentialResult<T> {
  /** Items whose operation resolved, in input order. */
  succeeded: T[];
  /** Items whose operation rejected, in input order. */
  failed: T[];
}

/**
 * Runs an async operation for each item strictly one-at-a-time, awaiting each
 * before starting the next, and continues past individual failures.
 *
 * Per-column review writes are a read-modify-write of one table asset
 * revision, so parallel calls clobber each other.
 */
export async function runSequential<T>(
  items: readonly T[],
  run: (item: T) => Promise<unknown>,
): Promise<SequentialResult<T>> {
  const succeeded: T[] = [];
  const failed: T[] = [];
  for (const item of items) {
    try {
      await run(item);
      succeeded.push(item);
    } catch {
      failed.push(item);
    }
  }
  return { succeeded, failed };
}
